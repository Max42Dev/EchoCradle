"""The orchestrator: the single object the game and experiments talk to.

It wires together the catalog, probe, store, planner and hosts, and exposes a
small, blocking-but-quick API. Every call returns as soon as it has an answer;
heavy work happens inside the hosts.

Typical use::

    mo = ModelOrchestrator()
    mo.start()                       # probe + load the text model
    reply = mo.chat([{"role": "user", "content": "hi"}])
    audio = mo.speak("hello", out_path)
    text  = mo.transcribe(wav_path)
    mo.stop()

The orchestrator is intentionally *not* a web service yet. The design doc's
HTTP surface can be layered on top later without changing this API.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterator

from orchestrator.catalog import Catalog, Modality
from orchestrator.errors import ModelNotAvailableError, OrchestratorError
from orchestrator.hosts.base import HostError
from orchestrator.hosts.speech import SpeechResult, SttHost, TtsHost
from orchestrator.hosts.text import TextHost
from orchestrator.paths import cache_dir
from orchestrator.planner import Planner
from orchestrator.probe import ProbeReport, probe
from orchestrator.store import ModelStore

log = logging.getLogger("orchestrator")


class ModelOrchestrator:
    """Owns models, picks them, loads them, and runs requests."""

    def __init__(
        self,
        *,
        catalog: Catalog | None = None,
        store: ModelStore | None = None,
        report: ProbeReport | None = None,
        text_base_url: str | None = None,
        shippable_only: bool = False,
        cache: Path | None = None,
    ) -> None:
        self.catalog = catalog or Catalog.default()
        self.store = store or ModelStore()
        self.report = report or probe()
        self.planner = Planner(self.catalog, self.report)
        self.shippable_only = shippable_only
        self.cache = cache or cache_dir()

        self.text = TextHost(base_url=text_base_url)
        self.tts = TtsHost()
        self.stt = SttHost()

        self._hosts = {
            Modality.TEXT: self.text,
            Modality.TTS: self.tts,
            Modality.STT: self.stt,
        }
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self, *, warm: tuple[Modality, ...] = (Modality.TEXT,)) -> None:
        """Probe is already done; optionally pre-load models for ``warm``."""
        for modality in warm:
            try:
                self.ensure_model(modality)
            except (ModelNotAvailableError, OrchestratorError) as exc:
                log.warning("could not warm %s: %s", modality.value, exc)
        self._started = True

    def stop(self) -> None:
        for host in self._hosts.values():
            try:
                host.unload()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                log.exception("error unloading %s", host.modality)
        self._started = False

    def __enter__(self) -> ModelOrchestrator:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- model management --------------------------------------------------

    def create_vad(self):
        """Load the small CPU Silero speech detector from the shared model store.

        This is an auxiliary speech component, not a competing task model.
        A missing model is downloaded once from sherpa-onnx's official release.
        """
        import urllib.request

        from orchestrator.vad import SileroVad

        directory = self.store.root / "silero-vad"
        path = directory / "silero_vad.onnx"
        if not path.exists():
            directory.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".part")
            try:
                request = urllib.request.Request(
                    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                    "asr-models/silero_vad.onnx"
                )
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = response.read(2 * 1024 * 1024 + 1)
                if not 100_000 <= len(payload) <= 2 * 1024 * 1024:
                    raise OrchestratorError("Unexpected Silero VAD download size")
                temporary.write_bytes(payload)
                SileroVad(temporary)  # Validate before installing the model.
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return SileroVad(path)

    def ensure_model(
        self,
        modality: Modality,
        *,
        model_id: str | None = None,
        progress=None,
    ):
        """Plan, download if needed, and load a model for ``modality``."""
        plan = self.planner.plan(
            modality,
            shippable_only=self.shippable_only,
            model_id=model_id,
        )
        log.info("planned %s -> %s (%s)", modality.value, plan.model.id, plan.reason)
        installed = self.store.ensure(plan.model, progress=progress)
        host = self._hosts[modality]
        if host.loaded_model_id != plan.model.id:
            host.load(installed)
        return plan.model

    def capabilities(self) -> dict[str, Any]:
        """Probe report plus what the planner would choose per modality."""
        choices: dict[str, Any] = {}
        for modality in Modality:
            try:
                plan = self.planner.plan(modality, shippable_only=self.shippable_only)
                choices[modality.value] = {
                    "model_id": plan.model.id,
                    "quality": plan.model.quality,
                    "reason": plan.reason,
                }
            except ModelNotAvailableError as exc:
                choices[modality.value] = {"model_id": None, "reason": str(exc)}
        return {"probe": self.report.to_dict(), "planned": choices}

    # -- text --------------------------------------------------------------

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 512,
        temperature: float = 0.7,
        model_id: str | None = None,
    ) -> str:
        """One text completion. Loads the text model on first use."""
        self._ensure_loaded(Modality.TEXT, model_id)
        return self.text.chat(
            messages,
            json_schema=json_schema,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 512,
        temperature: float = 0.7,
        model_id: str | None = None,
    ) -> Iterator[str]:
        """Stream a text completion as deltas."""
        self._ensure_loaded(Modality.TEXT, model_id)
        return self.text.stream_chat(
            messages, max_tokens=max_tokens, temperature=temperature
        )

    def chat_with_tools(
        self,
        messages: list[dict[str, str]],
        registry: Any,
        *,
        max_tokens: int = 512,
        temperature: float = 0.7,
        max_rounds: int = 4,
        model_id: str | None = None,
    ) -> tuple[str, list[Any]]:
        """Chat, letting the model call tools from ``registry`` mid-turn.

        Returns ``(final_text, tool_calls)``.
        """
        self._ensure_loaded(Modality.TEXT, model_id)
        return self.text.chat_with_tools(
            messages,
            registry,
            max_tokens=max_tokens,
            temperature=temperature,
            max_rounds=max_rounds,
        )

    def stream_chat_with_tools(
        self,
        messages: list[dict[str, str]],
        registry: Any,
        *,
        max_tokens: int = 512,
        temperature: float = 0.7,
        max_rounds: int = 4,
        on_text: Any = None,
        should_stop: Any = None,
        model_id: str | None = None,
    ) -> tuple[str, list[Any]]:
        """Stream a chat turn, executing tool calls, reporting text as it arrives.

        ``on_text(chunk)`` receives each text delta so the caller can speak while
        the model is still writing. ``should_stop()`` is polled between deltas;
        when it returns True generation is abandoned and the text so far is
        returned — the mechanism behind barge-in.
        """
        self._ensure_loaded(Modality.TEXT, model_id)
        return self.text.stream_chat_with_tools(
            messages,
            registry,
            max_tokens=max_tokens,
            temperature=temperature,
            max_rounds=max_rounds,
            on_text=on_text,
            should_stop=should_stop,
        )

    # -- speech ------------------------------------------------------------

    def speak(
        self,
        text: str,
        out_path: Path | str,
        *,
        model_id: str | None = None,
        speaker_id: int | None = None,
    ) -> SpeechResult:
        """Synthesize ``text`` to a WAV file."""
        self._ensure_loaded(Modality.TTS, model_id)
        if speaker_id is not None:
            self.tts.speaker_id = speaker_id
        return self.tts.synthesize(text, Path(out_path))

    def speak_streaming(
        self,
        text: str,
        out_dir: Path | str,
        player: Any | None = None,
        *,
        model_id: str | None = None,
        speaker_id: int | None = None,
        prefix: str = "line",
        index: int = 0,
    ) -> list[SpeechResult]:
        """Speak ``text`` sentence by sentence, playing each as it is ready.

        Pass an :class:`~orchestrator.hosts.speech.AudioPlayer` to hear it; the
        WAVs are written either way.
        """
        self._ensure_loaded(Modality.TTS, model_id)
        if speaker_id is not None:
            self.tts.speaker_id = speaker_id
        return self.tts.speak_streaming(
            text, Path(out_dir), player, prefix=prefix, index=index
        )

    def speak_stream(
        self,
        chunks: Iterator[str],
        out_dir: Path | str,
        *,
        model_id: str | None = None,
        prefix: str = "tts",
    ) -> Iterator[SpeechResult]:
        """Synthesize a text stream sentence by sentence, as it arrives."""
        self._ensure_loaded(Modality.TTS, model_id)
        from orchestrator.streaming import stream_sentences  # noqa: PLC0415

        return self.tts.stream_sentences(
            stream_sentences(chunks), Path(out_dir), prefix=prefix
        )

    def transcribe(
        self,
        wav_path: Path | str,
        *,
        model_id: str | None = None,
        on_partial=None,
    ) -> str:
        """Transcribe a WAV file, reporting partials as they are recognised."""
        self._ensure_loaded(Modality.STT, model_id)
        return self.stt.transcribe_stream(Path(wav_path), on_partial=on_partial)

    def listen(
        self,
        recorder: Any,
        *,
        model_id: str | None = None,
        on_partial=None,
        on_ready=None,
        on_block=None,
    ) -> str:
        """Record one microphone utterance and transcribe it.

        Partials arrive while the player is still speaking when the loaded
        model is a streaming one. ``on_ready`` fires once the input device is
        actually capturing, which is the correct moment to tell the player to
        start talking. ``on_block`` receives each captured audio block, which is
        useful for saving the raw audio or showing a level meter.
        """
        self._ensure_loaded(Modality.STT, model_id)
        return self.stt.transcribe_mic(
            recorder,
            on_partial=on_partial,
            on_ready=on_ready,
            on_block=on_block,
        )

    def listen_continuous(
        self,
        recorder: Any,
        *,
        model_id: str | None = None,
        on_partial=None,
        on_block=None,
    ) -> str:
        """Transcribe one utterance from an always-open :class:`ContinuousRecorder`.

        Streaming models are fed the live block stream; offline models capture
        the whole utterance first and decode it in one pass.
        """
        self._ensure_loaded(Modality.STT, model_id)
        return self.stt.listen(recorder, on_partial=on_partial, on_block=on_block)

    # -- internals ---------------------------------------------------------

    def _ensure_loaded(self, modality: Modality, model_id: str | None) -> None:
        host = self._hosts[modality]
        if model_id is not None and host.loaded_model_id == model_id:
            return
        if model_id is None and host.loaded_model_id is not None:
            return
        self.ensure_model(modality, model_id=model_id)


__all__ = ["ModelOrchestrator", "HostError"]
