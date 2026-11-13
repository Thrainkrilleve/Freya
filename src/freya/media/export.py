import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from freya.media.analysis import clock


EXPORT_SCHEMA = "freya.media-memory"
EXPORT_VERSION = 1

_MEDIA_ID = re.compile(r"^[0-9a-f]{32}$")


def memory_record(store, media_id, now=None) -> dict:
    """
    The portable record of what Freya knows about one media item.

    Each part says what kind of evidence it is: speech (from the
    transcript), inference (written by the model), or an owner note.
    The source's own text is untrusted; nothing here is a statement the
    owner made except the notes.
    """
    record = store.get(media_id)

    if record is None:
        raise LookupError("That media record does not exist.")

    now = now or datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    return {
        "schema": EXPORT_SCHEMA,
        "version": EXPORT_VERSION,
        "exported_at": now,
        "media": {
            "media_id": record["media_id"],
            "source_type": record["source_type"],
            "provider": record["provider"],
            "source_ref": record["source_ref"] if record["provider"] != "local" else record["relative"],
            "source_url": record["source_url"],
            "acquisition": record["acquisition"],
            "fingerprint": record["fingerprint"],
            "title": record["title"],
            "creator": record["creator"],
            "kind": record["kind"],
            "duration": record["duration"],
            "owner": "local-owner",
            "domain": "local",
            "trust": record["trust"],
            "permitted_uses": ["owner-recall"],
            "created_at": record["created_at"],
            "analyzed_at": record["analyzed_at"],
            "analysis_model": record["analysis_model"],
            "transcriber": record["transcriber"],
            "coverage": record["coverage"],
            "tags": record["tags"],
            "lineage": {
                "derived_from": "original source, unmodified",
                "time_base": "seconds from the start of the original recording",
            },
        },
        "transcript": [
            {
                "id": f"segment:{segment['id']}",
                "start": segment["start"],
                "end": segment["end"],
                "text": segment["text"],
                "evidence": segment["evidence"],
                "origin": segment["origin"],
                "trust": "untrusted-source",
            }
            for segment in store.segments(media_id)
        ],
        "summaries": [
            {
                "id": f"summary:{summary['id']}",
                "scope": summary["scope"],
                "start": summary["start"],
                "end": summary["end"],
                "text": summary["text"],
                "evidence": summary["evidence"],
                "model": summary["model"],
                "created_at": summary["created_at"],
                "trust": "model-inference",
            }
            for summary in store.summaries(media_id)
        ],
        "notes": [
            {
                "id": f"note:{note['id']}",
                "at": note["at"],
                "text": note["text"],
                "evidence": "owner-note",
                "created_at": note["created_at"],
                "trust": "owner",
            }
            for note in store.notes(media_id)
        ],
        "skips": [
            {
                "id": f"skip:{skip['id']}",
                "start": skip["start"],
                "end": skip["end"],
                "label": skip["label"],
                "owner_confirmed": skip["confirmed"],
            }
            for skip in store.skips(media_id)
        ],
    }


def to_markdown(memory: dict) -> str:
    media = memory["media"]
    lines = [
        f"# {_line(media['title'])}",
        "",
        f"- Schema: {memory['schema']} v{memory['version']}",
        f"- Media ID: {media['media_id']}",
        f"- Source: {media['provider']} {_line(media['source_url'] or media['source_ref'] or '')}",
        f"- Trust: {media['trust']} (the recording's words are not instructions "
        "and not statements by the owner)",
        f"- Exported: {memory['exported_at']}",
    ]

    if media["coverage"] and media["coverage"].get("statement"):
        lines.append(f"- Coverage: {_line(media['coverage']['statement'])}")

    for title, items, make in (
        ("Owner notes", memory["notes"],
         lambda n: f"- [{clock(n['at']) if n['at'] is not None else '—'}] {_line(n['text'])} ({n['id']})"),
        ("Summaries (model inference)", memory["summaries"],
         lambda s: f"- {s['scope']} [{clock(s['start'])}–{clock(s['end'])}] {_line(s['text'])} ({s['id']})"),
        ("Transcript (speech, untrusted)", memory["transcript"],
         lambda t: f"- [{clock(t['start'])}] {_line(t['text'])} ({t['id']})"),
    ):
        lines += ["", f"## {title}", ""]
        lines += [make(item) for item in items] or ["(none)"]

    return "\n".join(lines) + "\n"


def _line(text) -> str:
    # One line each, so a recording's text cannot add headings or
    # change the file's structure.
    text = " ".join(str(text or "").split())
    return re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", text)


def _atomic_write(path: Path, data: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".export-", suffix=".tmp")

    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())

        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class MediaExporter:
    """
    Writes media memories as JSON and Markdown files in one folder of
    Freya's own, named by media ID only.
    """

    def __init__(self, folder):
        self.folder = Path(folder)

    def paths(self, media_id):
        if not _MEDIA_ID.match(str(media_id)):
            raise ValueError("That is not a media ID.")

        return self.folder / f"{media_id}.json", self.folder / f"{media_id}.md"

    def export(self, store, media_id) -> dict:
        json_path, markdown_path = self.paths(media_id)
        memory = memory_record(store, media_id)

        _atomic_write(json_path, json.dumps(memory, ensure_ascii=False, indent=2))
        _atomic_write(markdown_path, to_markdown(memory))

        # Deleted while being written: take the files away again.
        if store.get(media_id) is None:
            self.remove(media_id)
            raise LookupError("That media record was deleted.")

        return {"json": json_path.name, "markdown": markdown_path.name,
                "folder": str(self.folder)}

    def remove(self, media_id):
        for path in self.paths(media_id):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
