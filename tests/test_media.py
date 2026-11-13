import json
import os
from pathlib import Path
import threading
import time
import wave

import pytest

from freya.control_plane import build_local_control_plane
from freya.media.analysis import Analyzer, Cancelled
from freya.media.config import MediaConfig
from freya.media.export import MediaExporter, memory_record, to_markdown
from freya.media.files import open_checked, resolve_inside, scan_roots, UnsafePath
from freya.media.jellyfin import JellyfinClient
from freya.media.jobs import AnalysisQueue
from freya.media.ranges import CHUNK_BYTES
from freya.media.service import MEDIA_CAPABILITIES
from freya.media.setup import build_media_service
from freya.media.store import MediaGone, MediaStore
from freya.media.transport import HttpAnswer
from freya.media.whisper import WhisperTranscriber
from freya.media.youtube import video_id


def item(store, name="Teaching video"):
    return store.add_media("youtube", "abcdefghijk", name, "video", "bookmark", source_url="https://www.youtube.com/watch?v=abcdefghijk")["media_id"]


@pytest.fixture(autouse=True)
def synthetic_handle_resolution(tmp_path, monkeypatch):
    # The Windows app sandbox refuses final-handle path queries. These
    # tests authorize only synthetic files and inject that OS boundary;
    # production code retains fail-closed handle validation.
    original = Path.resolve
    def resolve(path, strict=False):
        try:
            return original(path, strict=strict)
        except PermissionError:
            if path.absolute().is_relative_to(tmp_path):
                return original(path, strict=False)
            raise
    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr("freya.media.files.opened_path", lambda handle: Path(handle.name).absolute())


@pytest.fixture
def store(tmp_path):
    result = MediaStore(tmp_path / "media.db")
    yield result
    result.close()


@pytest.fixture
def service(tmp_path, fake_ollama):
    plane = build_local_control_plane(tmp_path / "control.db", None)
    root = tmp_path / "downloads"
    root.mkdir()
    permissions = lambda: {name: plane.policy.decide("local-owner", name).allow for name in MEDIA_CAPABILITIES}
    service = build_media_service(plane.broker, lambda messages: "The speaker explains circuits.", "fake-model",
                                  config=MediaConfig(downloads=root), database_path=tmp_path / "media.db",
                                  export_dir=tmp_path / "exports", permissions=permissions)
    plane.register_media(service)
    yield service, plane, root
    service.stop()
    plane.close()


def grant(plane):
    for name in MEDIA_CAPABILITIES:
        plane.policy.add_rule("local-owner", name, "allow", "Synthetic test grant")


def wait_job(store, identifier):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = store.job(identifier)
        if result["state"] not in ("queued", "running"):
            return result
        time.sleep(0.01)
    raise AssertionError("Analysis did not finish")


def test_owner_import_play_revoke_and_delete(service):
    media, plane, root = service
    assert media.handle({"action": "overview"})["denied"] == "media.catalog.read"
    grant(plane)
    path = root / "lesson.mp4"
    path.write_bytes(b"abcdefghij")
    os.utime(path, (1, 1))
    (root / "unfinished.mp4.crdownload").write_bytes(b"partial")
    scan = media.handle({"action": "scan"})
    assert scan["still_downloading"] == 1
    identifier = media.handle({"action": "import", "candidates": [scan["candidates"][0]["candidate"]]})["imported"][0]["item"]["id"]
    assert media.handle({"action": "import", "candidates": [scan["candidates"][0]["candidate"]]})["imported"][0]["new"] is False
    played = media.handle({"action": "play", "media_id": identifier})
    ticket = played["player"]["url"].split("/")[-1]
    assert media.stream(ticket, "bytes=2-4").body == b"cde"
    assert media.stream(ticket, "bytes=-2").body == b"ij"
    assert media.stream(ticket, "bytes=50-").status == 416
    plane.policy.add_rule("local-owner", "media.playback", "deny", "Revoke playback")
    assert media.stream(ticket, "bytes=0-1").status == 403
    media.handle({"action": "delete", "media_id": identifier})
    assert path.read_bytes() == b"abcdefghij"
    assert media.store.get(identifier) is None


def test_transcript_analysis_recall_and_export_are_distinct_media(service):
    media, plane, _ = service
    grant(plane)
    identifier = media.handle({"action": "bookmark", "url": "https://youtu.be/abcdefghijk", "title": "Circuit lesson"})["item"]["id"]
    response = media.handle({"action": "transcript-import", "media_id": identifier,
                             "text": "1\n00:00:12,000 --> 00:00:14,000\nA resistor limits circuit current.\n"})
    assert response["segments"] == 1
    queued = media.handle({"action": "analyze", "media_id": identifier})["job"]
    assert wait_job(media.store, queued["job_id"])["state"] == "completed"
    found = plane.broker.invoke("media.recall", query="resistor")
    assert found.success
    assert found.result["results"][0]["at_seconds"] == 12
    assert found.result["results"][0]["trust"] == "untrusted-source"
    media.handle({"action": "note-add", "media_id": identifier, "at": 13, "text": "Review this example"})
    exported = media.handle({"action": "export", "media_id": identifier})
    memory = json.loads(media.download(identifier, "json").body)
    assert memory["media"]["source_type"] == "media"
    assert memory["transcript"][0]["start"] == 12
    assert memory["notes"][0]["evidence"] == "owner-note"
    assert memory["media"]["coverage"]["visual"]["analyzed"] is False
    media.handle({"action": "delete", "media_id": identifier})
    assert media.store.search("resistor") == []
    assert not (media.exporter.folder / exported["export"]["json"]).exists()
    assert media.download(identifier, "json").status != 200


def test_replacing_bad_transcript_preserves_prior_source(store):
    identifier = item(store)
    store.replace_transcript(identifier, [(10, 12, "Original statement")], "owner-subtitles")
    with pytest.raises(ValueError):
        store.replace_transcript(identifier, [(float("nan"), 2, "Bad")], "owner-subtitles")
    assert store.search("Original")[0]["start"] == 10
    store.replace_summaries(identifier, [("whole", 10, 12, "Original summary")], "fake")
    store.replace_transcript(identifier, [(20, 22, "Replacement statement")], "owner-subtitles")
    assert store.search("Original") == []
    assert store.summaries(identifier) == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True])
def test_invalid_note_timestamps(store, value):
    with pytest.raises(ValueError):
        store.add_note(item(store), "note", value)


def test_cancel_during_final_model_call_never_publishes(store):
    identifier = item(store)
    store.replace_transcript(identifier, [(n * 2, n * 2 + 1, str(n) * 2000) for n in range(4)], "owner-subtitles")
    entered, release = threading.Event(), threading.Event()
    calls = []
    def chat(messages):
        calls.append(messages)
        if len(calls) == 3:
            entered.set()
            assert release.wait(5)
        return "A synthetic summary"
    analysis = Analyzer(store, chat, "fake")
    jobs = AnalysisQueue(store, analysis)
    jobs.start()
    try:
        job = jobs.submit(identifier)
        assert entered.wait(5)
        assert jobs.cancel(job["job_id"])
        release.set()
        assert wait_job(store, job["job_id"])["state"] == "cancelled"
        assert jobs.stop(5)
        assert store.summaries(identifier) == []
    finally:
        release.set()
        jobs.stop(5)


def test_worker_cannot_publish_after_source_deleted(store):
    identifier = item(store)
    job = store.add_job(identifier)
    store.update_job(job["job_id"], state="running")
    store.delete_media(identifier)
    with store.job_scope(job["job_id"], lambda: True):
        with pytest.raises(MediaGone):
            store.add_segments(identifier, [(0, 1, "Late result")], "whisper:fake")
    assert store.search("Late") == []


def test_restarted_jobs_are_reauthorized(store):
    identifier = item(store)
    job = store.add_job(identifier)
    class Analysis:
        def run(self, *args):
            pytest.fail("Revoked job must not execute")
    jobs = AnalysisQueue(store, Analysis(), authorize=lambda: False)
    jobs.start()
    try:
        assert wait_job(store, job["job_id"])["state"] == "cancelled"
    finally:
        jobs.stop()


def test_library_path_and_actual_open_boundary(tmp_path, monkeypatch):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"sample")
    info = path.stat()
    for relative in ("../video.mp4", "C:/video.mp4", "a\\video.mp4"):
        with pytest.raises(UnsafePath):
            resolve_inside([tmp_path], tmp_path, relative)
    with open_checked(path, info.st_size, info.st_mtime_ns) as handle:
        assert handle.read() == b"sample"
    monkeypatch.setattr("freya.media.files.opened_path", lambda handle: tmp_path / "outside.mp4")
    with pytest.raises(UnsafePath):
        open_checked(path, info.st_size, info.st_mtime_ns)


def test_scan_bounds_one_large_directory(tmp_path, monkeypatch):
    for index in range(6):
        (tmp_path / f"file{index}.mp4").write_bytes(b"x")
    monkeypatch.setattr("freya.media.files.MAX_VISITED", 3)
    assert scan_roots([tmp_path]).truncated


def test_jellyfin_credentials_remain_headers_and_short_ranges_are_checked():
    calls = []
    def transport(method, url, headers, **kwargs):
        calls.append((url, headers))
        return HttpAnswer(206, {"content-type": "video/mp4", "content-range": "bytes 0-9/20"}, b"0123456789")
    client = JellyfinClient("http://127.0.0.1:8096", "synthetic-token", transport=transport)
    assert client.stream("a" * 32, "video", "bytes=0-2").status == 502
    assert "synthetic-token" not in calls[0][0]
    assert calls[0][1]["X-Emby-Token"] == "synthetic-token"
    assert "synthetic-token" not in repr(client)


@pytest.mark.parametrize("url", ["https://youtube.com.evil.test/watch?v=abcdefghijk", "javascript:alert(1)", "https://user@youtube.com/watch?v=abcdefghijk", "https://youtube.com/watch?v=abcdefghijk&v=zyxwvutsrqp"])
def test_hostile_youtube_links(url):
    assert video_id(url) is None


def test_exported_source_text_cannot_become_markdown_image(store):
    identifier = item(store, "![tracking](https://example.test/track)")
    text = to_markdown(memory_record(store, identifier))
    assert "![tracking]" not in text
    assert "\\!\\[tracking\\]" in text


def test_decoder_handles_synthetic_audio_in_bounded_chunks(tmp_path, monkeypatch):
    import numpy as np
    path = tmp_path / "synthetic.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(np.zeros(16000 * 31, dtype="<i2").tobytes())
    lengths = []
    class Speech:
        def transcribe_media_chunk(self, audio):
            lengths.append(len(audio))
            return [(0, min(len(audio) / 16000, 1), "Synthetic speech")]
    from freya.media import decode_worker
    import io
    output = []
    monkeypatch.setattr(decode_worker, "checked", lambda handle, path: None)
    monkeypatch.setattr(decode_worker, "emit", output.append)
    stat = path.stat()
    decode_worker.decode(path, stat.st_size, stat.st_mtime_ns)
    class Process:
        stdout = io.BytesIO(b"".join(json.dumps(value).encode() + b"\n" for value in output))
        def poll(self): return 0
        def wait(self, timeout): return 0
    monkeypatch.setattr("freya.media.whisper.subprocess.Popen", lambda *args, **kwargs: Process())
    transcriber = WhisperTranscriber(lambda: Speech())
    segments, info = transcriber.transcribe(path, size=stat.st_size, modified_ns=stat.st_mtime_ns)
    values = list(segments)
    assert max(lengths) <= 16000 * 30
    assert len(values) == 2 and values[1][0] == 30
    assert info["complete"] is True


@pytest.mark.parametrize("change", ["size", "modified"])
def test_analysis_rejects_changed_imported_media(store, tmp_path, monkeypatch, change):
    path = tmp_path / "imported.wav"
    path.write_bytes(b"synthetic original")
    original = path.stat()
    record = store.add_media("local", str(path), "Synthetic audio", "audio", "local-folder",
                             root=str(tmp_path), relative=path.name, size=original.st_size,
                             modified_ns=original.st_mtime_ns, fingerprint="synthetic-original")
    if change == "size":
        path.write_bytes(b"synthetic replacement of a different size")
    else:
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns + 1000000000))

    class Speech:
        def transcribe_media_chunk(self, audio):
            pytest.fail("Changed media must not reach speech recognition")

    monkeypatch.setattr("freya.media.whisper.subprocess.Popen", lambda *args, **kwargs: pytest.fail("Changed media must not start decoding"))
    analyzer = Analyzer(store, lambda messages: pytest.fail("Changed media must not reach the model"), "synthetic",
                        transcriber=WhisperTranscriber(lambda: Speech()),
                        resolve_file=lambda item: resolve_inside([tmp_path], item["root"], item["relative"]))
    with pytest.raises(UnsafePath, match="changed since it was imported"):
        analyzer.run(record["media_id"], threading.Event())
    assert store.segments(record["media_id"]) == []
    assert store.summaries(record["media_id"]) == []
    assert store.get(record["media_id"])["fingerprint"] == "synthetic-original"


def test_decoder_rechecks_imported_identity_after_parent_validation(tmp_path, monkeypatch):
    import io
    from freya.media import decode_worker

    path = tmp_path / "racing.wav"
    path.write_bytes(b"synthetic original")
    original = path.stat()
    monkeypatch.setattr(decode_worker, "checked", lambda handle, source: None)

    class Speech:
        def transcribe_media_chunk(self, audio):
            pytest.fail("Replacement media must not reach speech recognition")

    class Process:
        stdout = io.BytesIO(json.dumps({"type": "error", "message": "Input changed."}).encode() + b"\n")
        def poll(self): return 1
        def wait(self, timeout): return 1

    def start(command, **kwargs):
        assert command[-2:] == [str(original.st_size), str(original.st_mtime_ns)]
        path.write_bytes(b"synthetic replacement between validation and opening")
        monkeypatch.setattr("av.open", lambda *args, **kwargs: pytest.fail("Replacement media must not be decoded"))
        with pytest.raises(ValueError, match="Input changed"):
            decode_worker.decode(path, int(command[-2]), int(command[-1]))
        return Process()

    monkeypatch.setattr("freya.media.whisper.subprocess.Popen", start)
    transcriber = WhisperTranscriber(lambda: Speech())
    segments, _ = transcriber.transcribe(path, size=original.st_size, modified_ns=original.st_mtime_ns)
    with pytest.raises(RuntimeError, match="Input changed"):
        list(segments)


def test_media_schema_reopen_and_tombstone_persistence(tmp_path):
    path = tmp_path / "media.db"
    store = MediaStore(path)
    identifier = item(store)
    store.add_note(identifier, "Keep source provenance", 7)
    store.close()
    store = MediaStore(path)
    assert store.search("provenance")[0]["start"] == 7
    store.delete_media(identifier)
    store.close()
    store = MediaStore(path)
    assert store.is_deleted(identifier) and store.search("provenance") == []
    store.close()


def test_gateway_media_is_authenticated_and_reply_is_private(service, tmp_path):
    import http.client
    from websockets.sync.client import connect
    from freya.events import EventBus
    from freya.gateway.server import Gateway
    from freya.image.gallery import ImageGallery
    from freya.window import WindowActions
    media, plane, root = service
    grant(plane)
    path = root / "test.mp4"
    path.write_bytes(b"0123456789")
    os.utime(path, (1, 1))
    scan = media.handle({"action": "scan"})
    identifier = media.handle({"action": "import", "candidates": [scan["candidates"][0]["candidate"]]})["imported"][0]["item"]["id"]
    stream_path = media.handle({"action": "play", "media_id": identifier})["player"]["url"]
    bus = EventBus()
    actions = WindowActions(lambda text: None, lambda text: None, lambda muted: None, lambda: None,
                            lambda: {}, media_request=media.handle, media_stream=media.stream,
                            media_download=media.download)
    gateway = Gateway(bus, ImageGallery(tmp_path / "images"), actions, port=0)
    gateway.start()
    def get(path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", gateway.port, timeout=5)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response, body
    try:
        assert get(stream_path)[0].status == 403
        response, _ = get(gateway.new_window_url().split(f":{gateway.port}", 1)[1])
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        response, body = get(stream_path, {"Cookie": cookie, "Range": "bytes=2-5"})
        assert response.status == 206 and body == b"2345"
        assert response.getheader("Content-Range") == "bytes 2-5/10"
        assert get(stream_path, {"Cookie": cookie, "Host": "evil.test"})[0].status == 403
        origin = f"http://127.0.0.1:{gateway.port}"
        with connect(origin.replace("http", "ws") + "/ws", origin=origin, additional_headers={"Cookie": cookie}) as first, connect(origin.replace("http", "ws") + "/ws", origin=origin, additional_headers={"Cookie": cookie}) as second:
            first.recv(timeout=2)
            second.recv(timeout=2)
            first.send(json.dumps({"type": "media", "action": "overview", "request": "private-test"}))
            reply = json.loads(first.recv(timeout=2))
            assert reply["type"] == "media" and reply["request"] == "private-test"
            with pytest.raises(TimeoutError):
                second.recv(timeout=0.1)
    finally:
        gateway.stop()
