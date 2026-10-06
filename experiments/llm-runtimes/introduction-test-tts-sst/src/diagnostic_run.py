"""Run the unchanged voice interview with local, per-session diagnostic capture."""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
import traceback
import wave
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np

import main as app


class SessionCapture:
    """Flush structured events immediately so an interrupted run remains inspectable."""

    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.started = time.monotonic()
        self._lock = threading.Lock()
        self._events = (directory / "events.jsonl").open("w", encoding="utf-8")

    def event(self, kind: str, **data: Any) -> None:
        payload = {"event": kind, "elapsed_s": time.monotonic() - self.started, **data}
        with self._lock:
            self._events.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            self._events.flush()

    def close(self) -> None:
        self._events.close()


class Tee:
    """Mirror console output to a UTF-8 transcript without hiding interactive output."""

    def __init__(self, stream: Any, target: Any) -> None:
        self.stream = stream
        self.target = target

    def write(self, text: str) -> int:
        self.stream.write(text)
        self.target.write(text)
        self.target.flush()
        return len(text)

    def flush(self) -> None:
        self.stream.flush()
        self.target.flush()


class MicrophoneCapture:
    """Bound diagnostic buffering; only the writer thread touches files.

    WAV samples are compacted when blocks are dropped. Block events map WAV
    offsets to continuous input indices, so gaps must not be treated as silence.
    """

    def __init__(self, capture: SessionCapture, sample_rate: int,
                 max_blocks: int = 128, max_block_bytes: int = 1024 * 1024) -> None:
        self.capture = capture
        self.sample_rate = sample_rate
        self.max_block_bytes = max_block_bytes
        self._queue: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._slots = threading.BoundedSemaphore(max_blocks)
        self._sample_index = 0
        self._dropped_blocks = 0
        self._dropped_samples = 0
        self._overflow_blocks = 0
        self._error: str | None = None
        self._closed = False
        self._worker = threading.Thread(target=self._write, name="diagnostic-microphone")
        self._worker.start()

    def enqueue(self, indata: Any, frames: int, timing: Any, status: Any) -> None:
        """Audio callback: bounded copy and metadata only, never disk I/O."""
        index = self._sample_index
        self._sample_index += frames
        overflow = bool(getattr(status, "input_overflow", False))
        self._overflow_blocks += int(overflow)
        if self._closed or indata.nbytes > self.max_block_bytes:
            self._dropped_blocks += 1
            self._dropped_samples += frames
            return
        if not self._slots.acquire(blocking=False):
            self._dropped_blocks += 1
            self._dropped_samples += frames
            return
        try:
            audio = indata.copy()
            metadata = {
                "sample_start": index, "sample_end": index + frames, "frames": frames,
                "input_overflow": overflow, "status": str(status),
                "input_buffer_adc_time": getattr(timing, "inputBufferAdcTime", None),
                "current_time": getattr(timing, "currentTime", None),
                "output_buffer_dac_time": getattr(timing, "outputBufferDacTime", None),
                "callback_monotonic_s": time.monotonic(),
            }
            self._queue.put((audio, metadata))
        except Exception:
            self._slots.release()
            self._dropped_blocks += 1
            self._dropped_samples += frames

    def _write(self) -> None:
        recording = None
        wav_index = 0
        try:
            recording = wave.open(str(self.capture.directory / "microphone.wav"), "wb")
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(self.sample_rate)
        except Exception as exc:
            self._error = repr(exc)
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                audio, metadata = item
                try:
                    if self._error is None:
                        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
                        recording.writeframesraw(pcm.tobytes())
                        self.capture.event(
                            "microphone_block", **metadata, wav_sample_start=wav_index,
                            wav_sample_end=wav_index + len(audio), sample_rate=self.sample_rate,
                        )
                        wav_index += len(audio)
                except Exception as exc:
                    self._error = repr(exc)
                finally:
                    self._slots.release()
        finally:
            if recording is not None:
                try:
                    recording.close()
                except Exception as exc:
                    self._error = repr(exc)

    def close(self) -> None:
        """Call after the input stream stops; drain and join before closing events."""
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        self._worker.join()
        self.capture.event(
            "microphone_capture_end", total_input_samples=self._sample_index,
            dropped_blocks=self._dropped_blocks, dropped_samples=self._dropped_samples,
            input_overflow_blocks=self._overflow_blocks, error=self._error,
            wav_gaps_compacted=True,
        )


def main(argv: list[str] | None = None) -> int:
    parser = app.build_parser()
    parser.add_argument("--session-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    directory = args.session_dir or (
        app.DEFAULT_OUT.parent / "sessions" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    )
    args.out = directory / "config.json"
    args.capture = True
    capture = SessionCapture(directory)
    original_turn = app._run_turn
    original_listen = app._listen
    base_orchestrator = app.ModelOrchestrator
    base_recorder = app.ContinuousRecorder
    recorders: list[Any] = []

    class TracedPlayer(app.AudioPlayer):
        def play(self, samples: Any, sample_rate: int, *, text: str = "") -> bool:
            try:
                queued = super().play(samples, sample_rate, text=text)
            except BaseException as exc:
                capture.event("playback_error", text=text, error=repr(exc))
                raise
            capture.event("playback_queued", text=text, queued=queued,
                          samples=int(np.asarray(samples).size), sample_rate=sample_rate,
                          device=self.device, playback_error=self.error,
                          delivery=self.delivery())
            return queued

        def abort(self) -> None:
            before = self.delivery()
            try:
                super().abort()
            finally:
                capture.event("playback_abort", before=before, after=self.delivery(),
                              playback_error=self.error)

    class TracedOrchestrator(base_orchestrator):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._decode_index = 0

            def decode_audio(audio: Any, rate: int) -> None:
                index = self._decode_index
                self._decode_index += 1
                target = directory / "model-input" / f"utterance_{index:03d}"
                target.parent.mkdir(exist_ok=True)
                np.save(target.with_suffix(".npy"), audio, allow_pickle=False)
                app.write_wav(target.with_suffix(".wav"), audio, rate)
                capture.event("decode_audio", index=index, samples=len(audio),
                              sample_rate=rate, float32_path=target.with_suffix(".npy"),
                              wav_path=target.with_suffix(".wav"))

            self.stt.on_decode_audio = decode_audio
            original = self.text.stream_chat_with_tools

            def stream(messages: Any, registry: Any, **options: Any) -> Any:
                capture.event("model_request", messages=messages,
                              options={k: v for k, v in options.items() if not callable(v)})
                chunks: list[str] = []
                callback = options.get("on_text")

                def on_text(chunk: str) -> None:
                    chunks.append(chunk)
                    capture.event("model_delta", text=chunk)
                    if callback is not None:
                        callback(chunk)

                options["on_text"] = on_text
                try:
                    result = original(messages, registry, **options)
                    capture.event("model_result", generated_text="".join(chunks),
                                  returned_text=result[0], tool_calls=result[1])
                    return result
                except BaseException as exc:
                    capture.event("model_error", generated_text="".join(chunks), error=repr(exc))
                    raise

            self.text.stream_chat_with_tools = stream

    class TracedRecorder(base_recorder):
        def start(self) -> bool:
            if getattr(self, "_diagnostic_capture", None) is not None:
                return self._stream is not None
            if not self.enabled:
                return False
            self.on_vad_block = lambda start, count, speech, calibrating: capture.event(
                "vad_block", sample_start=start, sample_count=count,
                sample_rate=self.sample_rate, speech=speech, calibrating=calibrating,
            )
            try:
                import sounddevice as sd
            except ImportError as exc:
                self._error = f"sounddevice not installed: {exc}"
                capture.event("microphone_start_failed", error=self._error)
                return False

            self._diagnostic_capture = None
            recorders.append(self)
            original_input = sd.InputStream

            def input_stream(*positional: Any, **options: Any) -> Any:
                callback = options["callback"]
                self._diagnostic_capture = MicrophoneCapture(
                    capture, int(options["samplerate"])
                )

                def record(indata: Any, frames: Any, timing: Any, status: Any) -> None:
                    self._diagnostic_capture.enqueue(indata, frames, timing, status)
                    callback(indata, frames, timing, status)

                options["callback"] = record
                return original_input(*positional, **options)

            try:
                try:
                    metadata = dict(sd.query_devices(self.device, "input"))
                    host_api = dict(sd.query_hostapis(metadata["hostapi"]))
                    capture.event("microphone_device", device=self.device,
                                  metadata=metadata, host_api=host_api)
                except Exception as exc:
                    capture.event("microphone_device_error", error=repr(exc))
                capture.event("microphone_start", device=self.device,
                              sample_rate=self.sample_rate)
                with patch.object(sd, "InputStream", input_stream):
                    started = super().start()
                if not started:
                    capture.event("microphone_start_failed", error=self.error)
                    self.stop()
                return started
            except BaseException:
                self.stop()
                raise

        def stop(self) -> None:
            try:
                super().stop()
            finally:
                writer = getattr(self, "_diagnostic_capture", None)
                if writer is not None:
                    writer.close()
                    self._diagnostic_capture = None
                    capture.event("microphone_stop")

    def run_turn(interview: Any, *positional: Any, **options: Any) -> Any:
        capture.event("turn_start", turn=interview.turns + 1, **options)
        try:
            result = original_turn(interview, *positional, **options)
            turn, interrupted, _ = result
            capture.event("turn_end", turn=interview.turns, interrupted=interrupted,
                          emitted_text=turn.say, history=interview.history,
                          config=turn.config,
                          tool_calls=[{"name": c.name, "arguments": c.arguments}
                                      for c in turn.tool_calls])
            delivery_path = directory / "delivery.jsonl"
            if delivery_path.exists():
                capture.event("playback_delivery", **json.loads(
                    delivery_path.read_text(encoding="utf-8").splitlines()[-1]
                ))
            (directory / "history.json").write_text(
                json.dumps(interview.history, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (directory / "config-partial.json").write_text(
                json.dumps(turn.config, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return result
        except BaseException as exc:
            capture.event("turn_error", error=repr(exc), history=interview.history)
            raise

    def listen(*positional: Any, **options: Any) -> str:
        capture.event("listen_start", **options)
        try:
            text = original_listen(*positional, **options)
        except BaseException as exc:
            capture.event("listen_error", error=repr(exc))
            raise
        capture.event("transcript", text=text, **options)
        return text

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    with (directory / "transcript.log").open("w", encoding="utf-8") as transcript:
        with patch.object(sys, "stdout", Tee(sys.stdout, transcript)), \
             patch.object(sys, "stderr", Tee(sys.stderr, transcript)), \
             patch.object(app, "ModelOrchestrator", TracedOrchestrator), \
             patch.object(app, "ContinuousRecorder", TracedRecorder), \
             patch.object(app, "AudioPlayer", TracedPlayer), \
             patch.object(app, "_run_turn", run_turn), \
             patch.object(app, "_listen", listen):
            print(f"Session capture: {directory}")
            print("Use headphones. Say 'quit' to stop. Stay quiet during calibration.")
            print("Generated text is captured only up to cancellation; "
                "ungenerated words are unknown.")
            capture.event("session_start", arguments=vars(args))
            try:
                result = app.run(args)
                capture.event("session_end", exit_code=result)
                return result
            except KeyboardInterrupt:
                capture.event("session_end", exit_code=130)
                print("\n(interrupted)")
                return 130
            except BaseException as exc:
                details = traceback.format_exc()
                (directory / "exception.txt").write_text(details, encoding="utf-8")
                capture.event("session_error", error=repr(exc), traceback=details)
                capture.event("session_end", exit_code=1)
                print(f"Session failed: {exc!r}", file=sys.stderr)
                return 1
            finally:
                try:
                    for recorder in recorders:
                        recorder.stop()
                finally:
                    capture.close()


if __name__ == "__main__":
    raise SystemExit(main())