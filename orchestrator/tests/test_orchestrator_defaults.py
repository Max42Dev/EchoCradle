"""Public no-ID flows with mocked hosts/store: no weights, devices or network."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from orchestrator import ModelOrchestrator, Modality
from orchestrator.catalog import Catalog
from orchestrator.errors import ModelNotAvailableError
from orchestrator.probe import ProbeReport
from orchestrator.store import InstalledModel


@pytest.fixture
def mo(monkeypatch, tmp_path) -> ModelOrchestrator:
    import orchestrator.orchestrator as module

    for name in ("TextHost", "TtsHost", "SttHost"):
        host = Mock(loaded_model_id=None, speaker_id=0, num_speakers=11)

        def load(installed: InstalledModel, host=host) -> None:
            host.loaded_model_id = installed.descriptor.id
            host.num_speakers = installed.descriptor.params.get("num_speakers", 1)

        host.load.side_effect = load
        monkeypatch.setattr(module, name, Mock(return_value=host))
    store = Mock()
    store.ensure.side_effect = lambda model, **kwargs: InstalledModel(model, tmp_path / model.id)
    return ModelOrchestrator(
        store=store, report=ProbeReport(ram_gb=16.0), cache=tmp_path
    )


def test_public_chat_speech_and_transcription_need_no_ids(mo, tmp_path) -> None:
    messages = [{"role": "user", "content": "Hello"}]
    mo.text.chat.return_value = "Hello there."
    mo.stt.transcribe_stream.return_value = "Hello there."
    with mo:
        assert mo.chat(messages) == "Hello there."
        mo.speak("Hello there.", tmp_path / "reply.wav")
        assert mo.transcribe(tmp_path / "reply.wav") == "Hello there."
        assert mo.text.loaded_model_id == "granite-4.2-8b-q4km"
        assert mo.tts.loaded_model_id == "kokoro-en-v0_19"
        assert mo.tts.speaker_id == 7
        assert mo.stt.loaded_model_id == "sensevoice-small"
        mo.tts.synthesize.assert_called_once_with("Hello there.", tmp_path / "reply.wav")
    for host in mo._hosts.values():
        host.unload.assert_called_once()


def test_public_streaming_and_listening_need_no_ids(mo, tmp_path) -> None:
    mo.text.stream_chat.return_value = iter(["Hello", " there."])
    mo.text.stream_chat_with_tools.side_effect = (
        lambda messages, registry, **kwargs: (kwargs["on_text"]("Hello") or "Hello", [])
    )
    mo.tts.stream_sentences.return_value = iter([])
    mo.stt.transcribe_mic.return_value = "recorded"
    mo.stt.listen.return_value = "continuous"
    assert list(mo.stream_chat([])) == ["Hello", " there."]
    seen: list[str] = []
    assert mo.stream_chat_with_tools([], Mock(), on_text=seen.append) == ("Hello", [])
    assert seen == ["Hello"]  # callback ran synchronously before returning
    assert list(mo.speak_stream(iter(["Hello."]), tmp_path)) == []
    mo.speak_streaming("Hello.", tmp_path)
    assert mo.tts.speaker_id == 7
    assert mo.listen(Mock()) == "recorded"
    assert mo.listen_continuous(Mock()) == "continuous"
    for host in mo._hosts.values():
        host.load.assert_called_once()


def test_horizon_uses_existing_selection_and_reuses_loaded_model(mo) -> None:
    model_id = "k2-horizon-7b-q4km"
    messages = [{"role": "user", "content": "Hello"}]
    mo.text.chat.return_value = "Hello there."
    mo.text.stream_chat.return_value = iter(["Hello there."])
    assert mo.chat(messages, model_id=model_id) == "Hello there."
    assert list(mo.stream_chat(messages, model_id=model_id)) == ["Hello there."]
    assert mo.text.loaded_model_id == model_id
    mo.store.ensure.assert_called_once()
    mo.text.load.assert_called_once()
    # No override reuses the selected model, just as for every other catalog entry.
    mo.chat(messages)
    assert mo.text.loaded_model_id == model_id
    mo.ensure_model(Modality.TEXT)
    assert mo.text.loaded_model_id == "granite-4.2-8b-q4km"


def test_voice_override_persists_until_model_switch_then_resets(mo, tmp_path) -> None:
    mo.speak("Hello.", tmp_path / "one.wav", speaker_id=9)
    mo.speak("Again.", tmp_path / "two.wav")
    assert mo.tts.speaker_id == 9
    mo.ensure_model(Modality.TTS, model_id="piper-en-amy-low")
    assert mo.tts.speaker_id == 0
    mo.ensure_model(Modality.TTS)
    assert mo.tts.speaker_id == 7


def test_small_hardware_speech_fallback_uses_safe_voice(mo, tmp_path) -> None:
    mo.report.ram_gb = 0.3
    mo.speak("Hello.", tmp_path / "reply.wav")
    assert mo.tts.loaded_model_id == "piper-en-lessac-medium"
    assert mo.tts.speaker_id == 0


def test_shippable_flow_cannot_bypass_filter_with_loaded_explicit_id(mo, tmp_path) -> None:
    mo.ensure_model(Modality.STT)
    mo.shippable_only = True
    with pytest.raises(ModelNotAvailableError, match="shippable"):
        mo.transcribe(tmp_path / "reply.wav", model_id="sensevoice-small")
    mo.transcribe(tmp_path / "reply.wav")
    assert mo.stt.loaded_model_id == "whisper-base-en"


def test_empty_custom_catalog_is_not_replaced_by_shipped_catalog(monkeypatch, tmp_path) -> None:
    import orchestrator.orchestrator as module

    monkeypatch.setattr(module, "ModelStore", Mock())
    catalog = Catalog([])
    mo = ModelOrchestrator(catalog=catalog, report=ProbeReport(ram_gb=16), cache=tmp_path)
    assert mo.catalog is catalog
    with pytest.raises(ModelNotAvailableError):
        mo.ensure_model(Modality.TEXT)


def test_invalid_catalog_voice_falls_back_to_zero(mo, tmp_path) -> None:
    mo.catalog.get("kokoro-en-v0_19").params["speaker_id"] = 100
    mo.speak("Hello.", tmp_path / "reply.wav")
    assert mo.tts.speaker_id == 0