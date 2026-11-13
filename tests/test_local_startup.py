import threading

import numpy as np
import pytest

from freya.voice import tts
from freya.window import build_status


def test_missing_voice_model_leaves_safe_text_only_voice(monkeypatch, capsys):
    def missing(**kwargs):
        raise OSError("Synthetic missing voice model")

    monkeypatch.setattr(tts, "KPipeline", missing)
    voice = tts.FreyaVoice()
    assert voice.enabled is False
    assert voice.pipeline is None
    assert voice._worker is None
    assert voice.startup_error == "Synthetic missing voice model"
    assert "text chat remains available" in capsys.readouterr().out
    voice.speak("Hello.")
    voice.interrupt()
    voice.wait_until_done()
    voice.shutdown()
    assert not voice.is_busy()
    assert voice._speech_queue.unfinished_tasks == 0


def test_explicit_text_only_mode_never_loads_voice(monkeypatch):
    monkeypatch.setattr(tts, "KPipeline", lambda **kwargs: pytest.fail("Model loaded"))
    voice = tts.FreyaVoice(enabled=False)
    voice.shutdown()
    assert voice._worker is None


def test_saved_blend_does_not_retry_missing_voice_assets(tmp_path, monkeypatch):
    from freya.speech import helper, setup
    from freya.speech.kokoro_engine import KokoroEngine
    from freya.speech.references import ReferenceStore
    from freya.speech.store import ClipStore, VoiceProfileStore

    monkeypatch.setattr(helper, "DATA_DIR", tmp_path)
    monkeypatch.setattr(KokoroEngine, "voice_for", lambda *args: pytest.fail("Unavailable voice was loaded again"))
    profiles = VoiceProfileStore(tmp_path / "profiles.json")
    profile = profiles.add("Synthetic blend", "kokoro", "blend", {"language": "a"})
    profiles.set_active(profile["id"])
    studio = setup.build_voice_studio(tts.FreyaVoice(enabled=False), profiles=profiles,
                                     clips=ClipStore(tmp_path / "clips"), references=ReferenceStore(tmp_path / "references"))
    studio.restore_live_voice()
    assert profiles.active_id == profile["id"]


def test_audio_device_failure_does_not_strand_worker_or_shutdown(monkeypatch):
    played = threading.Event()

    def pipeline(**kwargs):
        return lambda *args, **kwargs: iter([(None, None, np.zeros(8))])

    def unavailable(*args, **kwargs):
        played.set()
        raise RuntimeError("Synthetic audio device unavailable")

    monkeypatch.setattr(tts, "KPipeline", pipeline)
    monkeypatch.setattr(tts.sd, "play", unavailable)
    monkeypatch.setattr(tts.sd, "stop", lambda: None)
    voice = tts.FreyaVoice()
    try:
        voice.speak("Hello.")
        assert played.wait(2)
        voice.wait_until_done()
        assert not voice.is_busy()
    finally:
        voice.shutdown()
    assert not voice._worker.is_alive()
    voice.shutdown()


def test_status_reports_voice_fallback(tmp_path, fake_ollama):
    from freya.control_plane import build_local_control_plane
    from freya.settings import Settings

    plane = build_local_control_plane(tmp_path / "control.db", None)
    try:
        voice = tts.FreyaVoice(enabled=False)
        assert build_status(None, Settings(tmp_path / "settings.json"), plane, voice)["voice"] == "unavailable"
        voice.enabled = True
        assert build_status(None, Settings(tmp_path / "settings.json"), plane, voice)["voice"] == "ready"
    finally:
        plane.close()


def test_scratch_window_chat_survives_voice_failure_and_restarts(tmp_path, fake_ollama, monkeypatch):
    import importlib.util
    from pathlib import Path
    from freya.memory import memory_manager
    from freya.settings import Settings

    script = Path(__file__).resolve().parents[1] / "scripts" / "local_smoke.py"
    spec = importlib.util.spec_from_file_location("local_smoke", script)
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    original = memory_manager.DATABASE_PATH

    def missing(**kwargs):
        raise OSError("Synthetic missing voice model")

    monkeypatch.setattr(tts, "KPipeline", missing)
    with smoke.ScratchSession(tmp_path, fake_ollama, voice_enabled=True) as session:
        assert not session.voice.enabled
        assert smoke.protocol_exchange(session, "Synthetic amber fox") == "Hello from the fake model."
        session.settings.set_voice_volume(37)
        gateway = session.window._gateway
    assert not session.window.running
    assert memory_manager.DATABASE_PATH == original
    assert Settings(tmp_path / "settings.json").voice_volume == 37
    with smoke.ScratchSession(tmp_path, fake_ollama) as restarted:
        rows = restarted.assistant.memory.connection.execute("SELECT content FROM conversation_turn ORDER BY id").fetchall()
        assert [row[0] for row in rows] == ["Synthetic amber fox", "Hello from the fake model."]
    assert not any(thread.is_alive() for thread in gateway._threads)
