"""Speech host: TTS and streaming STT via ``sherpa-onnx``.

Speech is a **CPU-only** concern (exp. 0109): it costs zero VRAM, works on
Tier 0, and never competes with the GPU models. One Apache-2.0 runtime covers
TTS, true streaming ASR and VAD.

Two properties matter and are preserved here:

* **TTS streams at sentence granularity** — the caller feeds text chunks and
  gets audio back per sentence, so speech starts before the LLM has finished.
* **STT is a true streaming recogniser** — it emits partial hypotheses while
  the audio is still being fed, not one transcript at the end.

``sherpa_onnx`` is imported lazily so the rest of the orchestrator works on a
machine where speech is not installed.
"""

from __future__ import annotations

import collections
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from orchestrator.hosts.base import HostError
from orchestrator.store import InstalledModel


def _require_sherpa():
    try:
        import sherpa_onnx  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise HostError(
            "sherpa-onnx is not installed. Install the speech extra: "
            "pip install 'echocradle-orchestrator[speech]'"
        ) from exc
    return sherpa_onnx


def write_wav(path: Path, samples, sample_rate: int) -> Path:
    """Write float32 samples in [-1, 1] to a 16-bit mono WAV."""
    import numpy as np  # noqa: PLC0415

    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
    return path


class AudioPlayer:
    """Streams float32 audio to the default output device.

    Playback is callback-driven: :meth:`play` queues PCM without blocking.
    :meth:`delivery` estimates rendered samples using driver DAC timestamps;
    :meth:`abort` immediately discards pending PCM without draining it.

    ``sounddevice`` is imported lazily and playback degrades to a no-op when no
    output device exists, so a headless machine still runs the experiment.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        device: int | None = None,
        tail_s: float = 0.5,
    ) -> None:
        self.enabled = enabled
        self.device = device
        # Extra time allowed for the device buffer to empty after the last
        # sample has been handed to the stream. Needed because a loopback
        # microphone will otherwise pick up the tail of our own playback.
        self.tail_s = tail_s
        self._sd = None
        self._stream = None
        self._rate = 0
        self._error: str | None = None
        self._written = 0
        self._started_at = 0.0
        self._pending: Any = collections.deque()
        self._tickets: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def begin_turn(self) -> None:
        """Start a delivery ledger after previous playback has ended."""
        with self._lock:
            self._tickets.clear()

    def delivery(self) -> list[dict[str, Any]]:
        """Snapshot estimated audible PCM progress for each sentence.

        DAC timestamps exclude queued driver buffers. Word positions within
        partially delivered sentences remain approximate without TTS alignment.
        """
        import time

        now = time.monotonic()
        with self._lock:
            return [
                {"text": ticket["text"], "total_samples": ticket["total"],
                 "heard_samples": min(ticket["total"], sum(
                     max(0, min(count, int((now - due) * self._rate)))
                     for due, count in ticket["segments"]
                 )) if self._rate else ticket.get("heard", 0)}
                for ticket in self._tickets
            ]

    @property
    def available(self) -> bool:
        return self.enabled and self._error is None

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def playing(self) -> bool:
        """True while content remains queued or scheduled for the DAC."""
        with self._lock:
            pending = bool(self._pending)
        return pending or any(
            item["heard_samples"] < item["total_samples"] for item in self.delivery()
        ) if self._stream is not None else False

    def _ensure_stream(self, sample_rate: int):
        if self._stream is not None and self._rate == sample_rate:
            return self._stream
        self.close()
        try:
            import sounddevice as sd  # noqa: PLC0415
        except ImportError as exc:
            self._error = f"sounddevice not installed: {exc}"
            return None
        try:
            self._sd = sd
            self._stream = sd.OutputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="float32",
                device=self.device,
                blocksize=max(1, sample_rate // 100),
                latency="low",
                callback=self._output_callback,
            )
            self._rate = sample_rate
            self._stream.start()
            self._written = 0
            self._started_at = 0.0
        except Exception as exc:  # noqa: BLE001 - no device, driver error, ...
            self._error = f"cannot open audio output: {exc}"
            self._stream = None
        return self._stream

    def _output_callback(self, outdata, frames, timing, status) -> None:
        """Copy PCM only; synthesis and file I/O never run on the audio thread."""
        import time

        outdata.fill(0)
        # Never wait behind control work on the real-time audio thread.
        if not self._lock.acquire(blocking=False):
            return
        try:
            now = time.monotonic()
            latency = max(0.0, timing.outputBufferDacTime - timing.currentTime)
            offset = 0
            while offset < frames and self._pending:
                samples, position, ticket = self._pending[0]
                count = min(frames - offset, len(samples) - position)
                outdata[offset:offset + count, 0] = samples[position:position + count]
                ticket["segments"].append((now + latency + offset / self._rate, count))
                position += count
                offset += count
                if position == len(samples):
                    self._pending.popleft()
                else:
                    self._pending[0] = (samples, position, ticket)
        finally:
            self._lock.release()

    def play(self, samples, sample_rate: int, *, text: str = "") -> bool:
        """Queue audio for playback. Returns False if audio is unavailable."""
        if not self.enabled:
            return False
        stream = self._ensure_stream(sample_rate)
        if stream is None:
            return False
        import numpy as np  # noqa: PLC0415

        pcm = np.asarray(samples, dtype=np.float32).reshape(-1).copy()
        ticket = {"text": text, "total": len(pcm), "segments": []}
        with self._lock:
            self._tickets.append(ticket)
            if len(pcm):
                self._pending.append((pcm, 0, ticket))
        return True

    def wait(self, should_stop: Any = None) -> bool:
        """Wait for DAC delivery, polling cancellation; True means interrupted."""
        import time  # noqa: PLC0415

        while self.playing:
            if should_stop is not None and should_stop():
                return True
            time.sleep(0.01)
        return False

    def abort(self) -> None:
        """Stop playback immediately, discarding anything still queued.

        Used for barge-in: the player has started speaking, so the rest of the
        AI's line must not be heard. The stream is closed rather than paused —
        a paused stream still holds the queued audio, and resuming it would
        play the very words the interruption was meant to cut off. The next
        :meth:`play` reopens a fresh stream.
        """
        self.close()

    def close(self) -> None:
        snapshot = self.delivery()
        if self._stream is not None:
            try:
                self._stream.abort()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None
        with self._lock:
            self._pending.clear()
            for ticket, progress in zip(self._tickets, snapshot):
                ticket["heard"] = progress["heard_samples"]
        self._rate = 0
        self._written = 0
        self._started_at = 0.0


class AudioRecorder:
    """Captures live microphone audio, one utterance at a time.

    Endpointing is **energy-based**: the recorder waits for the input to rise
    above a noise floor, then stops once it has been quiet for
    ``silence_s``. This is deliberately simple — a VAD model can be layered on
    later, but a threshold keeps the orchestrator free of another dependency
    and works well enough for push-to-talk-style dialogue.

    The default threshold is tuned for **remote-desktop microphone
    redirection** (measured on NICE DCV): speech arrives around -30 dBFS, which
    is roughly ten times quieter than a local microphone. A local mic would
    want something closer to 0.01. :meth:`calibrate` measures the room and
    picks a suitable value.

    Like :class:`AudioPlayer`, ``sounddevice`` is imported lazily and the
    recorder degrades to ``available == False`` on a machine with no input
    device, so nothing crashes headless.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        device: int | None = None,
        sample_rate: int = 16000,
        block_ms: int = 100,
        silence_s: float = 1.2,
        max_utterance_s: float = 30.0,
        no_speech_s: float = 8.0,
        threshold: float = 0.0025,
    ) -> None:
        self.enabled = enabled
        self.device = device
        self.sample_rate = sample_rate
        self.block_ms = block_ms
        self.silence_s = silence_s
        self.max_utterance_s = max_utterance_s
        self.no_speech_s = no_speech_s
        self.threshold = threshold
        self._error: str | None = None

    @property
    def available(self) -> bool:
        return self.enabled and self._error is None

    @property
    def error(self) -> str | None:
        return self._error

    @classmethod
    def calibrate(cls, *, seconds: float = 1.0, device: int | None = None,
                  sample_rate: int = 16000, floor_multiple: float = 4.0,
                  minimum: float = 0.0025) -> float:
        """Measure the room and return a speech threshold.

        Takes the median level while quiet and multiplies it, with a floor so
        that a silent room does not produce a threshold of zero. The floor is
        deliberately low because redirected microphone audio is quiet; the cost
        of a low threshold is only that a stray noise may open an utterance,
        which the recogniser then returns as empty text.
        """
        import sounddevice as sd  # noqa: PLC0415

        block = max(1, int(sample_rate * 0.1))
        levels: list[float] = []
        try:
            with sd.InputStream(samplerate=sample_rate, channels=1, dtype="float32",
                                blocksize=block, device=device) as stream:
                for _ in range(max(1, int(seconds * 10))):
                    data, _ = stream.read(block)
                    mono = np.asarray(data, dtype=np.float32).reshape(-1)
                    levels.append(float(np.sqrt(np.mean(mono**2))))
        except Exception:  # noqa: BLE001 - no device; fall back to the floor
            return minimum
        if not levels:
            return minimum
        return max(minimum, float(np.median(levels)) * floor_multiple)

    def record_utterance(self, *, on_block=None, on_ready=None) -> Any | None:
        """Record until the speaker stops. Returns float32 samples, or None.

        **Every** captured block is streamed to ``on_block`` from the first
        block, not only after speech is detected. Gating what the recogniser
        sees would swallow the onset of the utterance — the first word is
        exactly where the player's answer lives. Energy is therefore used only
        to decide *when the utterance has ended*, never to decide what is fed.

        ``on_ready`` is called once the input stream is genuinely capturing.
        Opening a capture device takes a moment, and anything said during that
        window is lost — a caller that cues the user with a sound should cue
        them from ``on_ready``, not before calling this.

        Returns ``None`` when no speech was detected at all (a stray click,
        or an empty room).
        """
        if not self.enabled:
            return None
        try:
            import sounddevice as sd  # noqa: PLC0415
        except ImportError as exc:
            self._error = f"sounddevice not installed: {exc}"
            return None

        import numpy as np  # noqa: PLC0415

        block = max(1, int(self.sample_rate * self.block_ms / 1000))
        silence_blocks = max(1, int(self.silence_s * 1000 / self.block_ms))
        max_blocks = max(1, int(self.max_utterance_s * 1000 / self.block_ms))
        no_speech_blocks = max(1, int(self.no_speech_s * 1000 / self.block_ms))
        # A short run above the floor confirms speech rather than a click.
        start_blocks = 2

        collected: list[Any] = []
        quiet_run = 0
        loud_run = 0
        started = False

        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=block,
                device=self.device,
            ) as stream:
                if on_ready is not None:
                    # The device is open, so anything said from now on is
                    # captured. This is the earliest safe moment to cue the
                    # user to start speaking.
                    on_ready()
                for index in range(max_blocks):
                    data, _overflow = stream.read(block)
                    mono = np.asarray(data, dtype=np.float32).reshape(-1)
                    level = float(np.sqrt(np.mean(mono**2)))

                    # Always keep and always stream the block: the recogniser
                    # needs the run-up to the first word.
                    collected.append(mono)
                    if on_block is not None:
                        on_block(mono)

                    if level >= self.threshold:
                        loud_run += 1
                        if loud_run >= start_blocks:
                            started = True
                        quiet_run = 0
                    else:
                        loud_run = 0
                        quiet_run += 1

                    if started and quiet_run >= silence_blocks:
                        break
                    # Silence before any speech: give up rather than wait out
                    # the full utterance budget.
                    if not started and index >= no_speech_blocks:
                        return None
        except HostError:
            # An error raised by the consumer (on_block) must not be reported
            # as a device failure; let it surface as itself.
            raise
        except Exception as exc:  # noqa: BLE001 - no device, driver error, ...
            self._error = f"cannot open audio input: {exc}"
            return None

        if not started:
            return None
        return np.concatenate(collected) if collected else None


class ContinuousRecorder:
    """Keep the microphone open, retaining idle pre-roll and pending speech.

    Idle capture retains about 300 ms. Consecutive loud blocks confirm speech;
    candidate onset and all ensuing blocks survive until consumed. Offline
    capture endpoints on sample-duration quiet; streaming STT endpoints itself.

    That is what makes "record all the time" work — nothing said while the AI is
    speaking or thinking is lost, because the device is never closed.

    It also **detects speech** (:meth:`speech_detected`), which is what makes
    barge-in possible: while the AI is talking, the caller polls this and stops
    playback and generation the moment the player speaks. Detection is a level
    threshold, deliberately crude — it only has to notice that *something* was
    said, and the recogniser then decides what it was.

    Capture uses a **callback** stream, not a blocking ``stream.read()``. On the
    virtual audio devices of a remote-desktop session (NICE DCV on AWS) a
    blocking read returns zeros *immediately* instead of waiting for audio, so a
    ten-second recording produced hundreds of seconds of silence. The callback
    is driven by the driver and delivers real audio on the same devices.

    The device's native default rate is used when metadata is available;
    consumers still receive float32 blocks at ``sample_rate`` (16kHz by default).
    Continuous linear interpolation preserves fractional phase across callbacks
    without adding a dependency, but is not an anti-aliasing low-pass filter.

    Only safe with **headphones**: on a loopback device the microphone hears the
    AI's own voice, which would both transcribe as the player's speech and
    trigger barge-in immediately.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        device: int | None = None,
        sample_rate: int = 16000,
        block_ms: int = 100,
        threshold: float = 0.01,
        start_blocks: int = 2,
        vad: Any = None,
    ) -> None:
        self.enabled = enabled
        self.device = device
        self.sample_rate = sample_rate
        self.block_ms = block_ms
        self.threshold = threshold
        self.start_blocks = start_blocks
        if vad is not None and sample_rate != 16000:
            raise ValueError("VAD capture requires 16000 Hz model-rate audio")
        self.vad = vad
        self._last_block_speech = False
        self._worker: Any = None
        self._worker_stop = False
        self._pending_vad: Any = None
        self._pending_vad_samples = 0
        self._worker_queue_samples = sample_rate * 5
        self._error: str | None = None
        self._queue: Any = None
        self._stream: Any = None
        self._capture_sample_rate = sample_rate
        self._capture_status: str | None = None
        self._detected = False
        self._loud_run = 0
        self._calibrating = False
        self._buffered_samples = 0
        self._pre_roll_samples = max(1, int(sample_rate * (0.5 if vad is not None else 0.3)))
        from threading import Condition, RLock  # noqa: PLC0415

        self._condition = Condition()
        self._vad_lock = RLock()

    @property
    def available(self) -> bool:
        return self.enabled and self._error is None

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def capture_sample_rate(self) -> int:
        """Device rate; queued blocks always use the public model sample_rate."""
        return self._capture_sample_rate

    @property
    def capture_status(self) -> str | None:
        """Latest non-empty driver callback status, cleared when capture starts."""
        return self._capture_status

    def start(self) -> bool:
        """Open the device and begin queueing blocks from the driver callback."""
        if not self.enabled:
            return False
        if self._stream is not None:
            return self.available
        try:
            from collections import deque  # noqa: PLC0415

            import numpy as np  # noqa: PLC0415
            import sounddevice as sd  # noqa: PLC0415
        except ImportError as exc:
            self._error = f"sounddevice not installed: {exc}"
            return False

        self._capture_sample_rate = self.sample_rate
        self._capture_status = None
        try:
            device_info = sd.query_devices(self.device, "input")
            native_rate = float(device_info["default_samplerate"])
            if np.isfinite(native_rate) and native_rate >= 1:
                self._capture_sample_rate = int(round(native_rate))
        except Exception:  # noqa: BLE001 - optional metadata/PortAudio query can fail
            # Metadata is optional (including in lightweight sounddevice fakes).
            pass

        with self._condition:
            self._queue = deque()
            self._buffered_samples = 0
            self._detected = False
            self._loud_run = 0
            self._last_block_speech = False
            self._pending_vad = deque()
            self._pending_vad_samples = 0
            self._worker_stop = False
            self._error = None
        if self.vad is not None:
            from threading import Thread  # noqa: PLC0415

            try:
                with self._vad_lock:
                    reset = getattr(self.vad, "reset", None)
                    if reset is not None:
                        reset()
                self._worker = Thread(target=self._classify_blocks, name="speech-vad", daemon=True)
                self._worker.start()
            except Exception as exc:  # noqa: BLE001 - surface VAD initialization failure
                self._fail_vad(exc)
                return False
        block = max(1, int(self.capture_sample_rate * self.block_ms / 1000))
        captured_samples = 0
        output_samples = 0
        previous_sample: Any = None

        def callback(indata: Any, _frames: int, _time_info: Any, status: Any) -> None:
            nonlocal captured_samples, output_samples, previous_sample
            if status:
                self._capture_status = str(status)
            # Copy the reused native buffer. Vectorised interpolation is bounded
            # to one (normally 100ms) block; no inference or IO on this thread.
            mono = np.asarray(indata, dtype=np.float32).reshape(-1).copy()
            if not len(mono):
                return
            if self.capture_sample_rate != self.sample_rate:
                end = captured_samples + len(mono)
                # Absolute rational sample positions prevent per-block rounding
                # drift at 44.1kHz or with irregular driver callback lengths.
                count = (end - 1) * self.sample_rate // self.capture_sample_rate + 1
                positions = (np.arange(output_samples, count, dtype=np.float64)
                             * self.capture_sample_rate / self.sample_rate - captured_samples)
                source = mono
                offset = 0
                if previous_sample is not None:
                    source = np.concatenate(([previous_sample], mono))
                    offset = -1
                previous_sample = mono[-1]
                mono = np.interp(positions, np.arange(len(source)) + offset, source).astype(
                    np.float32
                )
                captured_samples = end
                output_samples = count
                # A fractional boundary awaits the next native sample instead
                # of inventing/repeating an endpoint at every callback.
                if not len(mono):
                    return
            if self.vad is not None:
                with self._condition:
                    if self._worker_stop or self._error is not None:
                        return
                    if self._pending_vad_samples + len(mono) > self._worker_queue_samples:
                        self._error = "VAD worker queue overflow; capture classification stopped"
                        self._capture_status = self._error
                        self._worker_stop = True
                        self._detected = False
                        self._condition.notify_all()
                        return
                    self._pending_vad.append(mono)
                    self._pending_vad_samples += len(mono)
                    self._condition.notify_all()
                return
            level = float(np.sqrt(np.mean(mono**2))) if len(mono) else 0.0
            with self._condition:
                self._queue.append(mono)
                self._buffered_samples += len(mono)
                if not self._calibrating and level >= self.threshold:
                    self._loud_run += 1
                    if self._loud_run >= self.start_blocks:
                        self._detected = True
                else:
                    self._loud_run = 0
                self._trim_idle()
                self._condition.notify()

        try:
            self._stream = sd.InputStream(
                samplerate=self.capture_sample_rate,
                channels=1,
                dtype="float32",
                blocksize=block,
                device=self.device,
                callback=callback,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001 - no device, driver error, ...
            self._error = f"cannot open audio input: {exc}"
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:  # noqa: BLE001
                    pass
            self._stream = None
            self.stop()
            return False
        return self.available

    def _fail_vad(self, exc: Exception) -> None:
        with self._condition:
            self._error = f"VAD classification failed: {exc}"
            self._capture_status = self._error
            self._worker_stop = True
            self._detected = False
            self._condition.notify_all()

    def _classify_blocks(self) -> None:
        """Single stateful decoder; driver callback only enqueues copied PCM."""
        sample_index = 0
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._worker_stop or bool(self._pending_vad)
                    )
                    if self._worker_stop:
                        return
                # Calibration/reset uses the same lock through queue flush and reset.
                with self._vad_lock:
                    with self._condition:
                        if self._worker_stop:
                            return
                        if not self._pending_vad:
                            continue
                        block = self._pending_vad.popleft()
                        self._pending_vad_samples -= len(block)
                        calibrating = self._calibrating
                    speech = False if calibrating else bool(self.vad.process(block))
                    observer = getattr(self, "on_vad_block", None)
                    if observer is not None:
                        observer(sample_index, len(block), speech, calibrating)
                    sample_index += len(block)
                    with self._condition:
                        if self._worker_stop:
                            return
                        self._queue.append((block, speech))
                        self._buffered_samples += len(block)
                        if not self._calibrating and speech:
                            self._loud_run += 1
                            if self._loud_run >= self.start_blocks:
                                self._detected = True
                        else:
                            self._loud_run = 0
                        self._trim_idle()
                        self._condition.notify_all()
        except Exception as exc:  # noqa: BLE001 - no silent fallback to energy
            self._fail_vad(exc)

    def next_block(self, timeout: float | None = None) -> Any | None:
        """Return the next audio block, or None on timeout."""
        with self._condition:
            if self._queue is None:
                return None
            if not self._condition.wait_for(
                lambda: bool(self._queue) or self._worker_stop or self._error is not None,
                timeout=timeout,
            ):
                return None
            if not self._queue:
                return None
            block = self._queue.popleft()
            if self.vad is not None:
                block, self._last_block_speech = block
            self._buffered_samples -= len(block)
            return block

    def _trim_idle(self) -> None:
        """Called under the condition lock; never trim confirmed/candidate speech."""
        if self._detected or self._loud_run or self._queue is None:
            return
        while self._queue and self._buffered_samples > self._pre_roll_samples:
            excess = self._buffered_samples - self._pre_roll_samples
            item = self._queue[0]
            block = item[0] if self.vad is not None else item
            if len(block) <= excess:
                self._queue.popleft()
                self._buffered_samples -= len(block)
            else:
                trimmed = block[excess:].copy()
                self._queue[0] = (trimmed, item[1]) if self.vad is not None else trimmed
                self._buffered_samples -= excess

    def calibrate(
        self,
        *,
        seconds: float = 0.6,
        floor_multiple: float = 4.0,
        minimum: float = 0.001,
    ) -> float:
        """Measure the room's noise floor and set the speech threshold.

        A fixed threshold cannot suit both a local microphone and a quiet
        remote-desktop one. Measuring the floor and multiplying it adapts to
        either, with a floor so a silent room does not produce a threshold of
        zero. The floor is deliberately not too low: a threshold below the
        device's own noise trips barge-in on nothing. Drains the queue, so call
        it before the first turn.
        """
        import time  # noqa: PLC0415

        import numpy as np  # noqa: PLC0415

        with self._vad_lock:
            with self._condition:
                self._calibrating = True
                self._detected = False
                self._loud_run = 0
        levels: list[float] = []
        deadline = time.monotonic() + seconds
        try:
            while time.monotonic() < deadline:
                remaining = max(0.0, deadline - time.monotonic())
                block = self.next_block(timeout=min(0.2, remaining))
                if block is not None and len(block):
                    levels.append(float(np.sqrt(np.mean(block**2))))
            if levels:
                self.threshold = max(minimum, float(np.median(levels)) * floor_multiple)
        finally:
            with self._vad_lock:
                try:
                    if self.vad is not None:
                        set_floor = getattr(self.vad, "set_noise_floor", None)
                        reset = getattr(self.vad, "reset", None)
                        if set_floor is not None and levels:
                            set_floor(float(np.median(levels)), floor_multiple=floor_multiple)
                        elif reset is not None:
                            reset()
                except Exception as exc:  # noqa: BLE001
                    self._fail_vad(exc)
                finally:
                    with self._condition:
                        if self._queue is not None:
                            self._queue.clear()
                        if self._pending_vad is not None:
                            self._pending_vad.clear()
                        self._pending_vad_samples = 0
                        self._buffered_samples = 0
                        self._last_block_speech = False
                        self._detected = False
                        self._loud_run = 0
                        self._calibrating = False
        return self.threshold

    def record_utterance(
        self,
        *,
        silence_s: float = 1.0,
        max_s: float = 30.0,
        no_speech_s: float = 8.0,
        on_block: Any = None,
    ) -> Any | None:
        """Collect one utterance using stored VAD decisions (or energy without VAD).

        Waits for speech to start, then collects until ``silence_s`` of quiet.
        Keep bounded pre-roll and all candidate blocks before confirmation.
        Queued silence is scanned without spending the wall-clock wait budget.
        Acknowledge only consumed audio, preserving any queued following turn.

        This is the path for an **offline** recogniser, which cannot decode
        incrementally: the whole utterance is captured first, then decoded in
        one pass. The streaming recogniser endpoints itself and does not use
        this.
        """
        import time  # noqa: PLC0415
        from collections import deque  # noqa: PLC0415

        import numpy as np  # noqa: PLC0415

        pre_roll: Any = deque()
        pre_samples = 0
        candidates: list[Any] = []
        collected: list[Any] = []
        started = False
        quiet_samples = 0
        utterance_samples = 0
        waited = 0.0
        while True:
            # Always drain pending audio first, even when the wait budget expired.
            block = self.next_block(timeout=0.0)
            if block is None:
                timeout = 1.0 if started else max(0.0, no_speech_s - waited)
                if timeout <= 0:
                    break
                before = time.monotonic()
                block = self.next_block(timeout=timeout)
                waited += time.monotonic() - before
            if block is None:
                break
            # Save metadata before a user callback can interact with the recorder.
            speech = self._last_block_speech if self.vad is not None else (
                bool(len(block)) and float(np.sqrt(np.mean(block**2))) >= self.threshold
            )
            if on_block is not None:
                on_block(block)
            if not started:
                if speech:
                    candidates.append(block)
                    if len(candidates) < self.start_blocks:
                        continue
                    started = True
                    collected = list(pre_roll) + candidates
                    utterance_samples = sum(len(b) for b in candidates)
                    candidates = []
                else:
                    for pending in candidates + [block]:
                        pre_roll.append(pending)
                        pre_samples += len(pending)
                    candidates = []
                    while pre_roll and pre_samples > self._pre_roll_samples:
                        excess = pre_samples - self._pre_roll_samples
                        first = pre_roll.popleft()
                        if len(first) > excess:
                            pre_roll.appendleft(first[excess:].copy())
                        pre_samples -= min(len(first), excess)
                    continue
            else:
                collected.append(block)
                utterance_samples += len(block)
                quiet_samples = 0 if speech else quiet_samples + len(block)
                if quiet_samples >= max(1, silence_s * self.sample_rate):
                    break
            if utterance_samples >= max(1, max_s * self.sample_rate):
                break
        self.acknowledge()
        if not started:
            return None
        return np.concatenate(collected) if collected else None

    def speech_detected(self) -> bool:
        """True until consumed speech is acknowledged (including queued turns)."""
        with self._condition:
            return self._detected

    def acknowledge(self) -> None:
        """Re-arm from unconsumed blocks, without discarding the following turn.

        Offline capture calls this at its endpoint. Direct/streaming consumers
        should call it when their recogniser consumes an utterance endpoint.
        """
        import numpy as np  # noqa: PLC0415

        with self._condition:
            self._detected = False
            self._loud_run = 0
            if self._queue is not None and not self._calibrating:
                for item in self._queue:
                    if self.vad is not None:
                        _, speech = item
                    else:
                        speech = bool(len(item)) and (
                            float(np.sqrt(np.mean(item**2))) >= self.threshold
                        )
                    self._loud_run = self._loud_run + 1 if speech else 0
                    if self._loud_run >= self.start_blocks:
                        self._detected = True
            self._trim_idle()

    def reset(self) -> None:
        """Re-arm without discarding pending speech; not a between-turn flush.

        Calibration is the only startup flush. No reset is needed between turns.
        """
        self.acknowledge()

    def stop(self) -> None:
        with self._condition:
            self._worker_stop = True
            self._condition.notify_all()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None
        if self._worker is not None:
            self._worker.join()
            self._worker = None
        with self._condition:
            if self._pending_vad is not None:
                self._pending_vad.clear()
            self._pending_vad_samples = 0


def pick_input_device(*, sample_rate: int = 16000, seconds: float = 0.4) -> int | None:
    """Prefer working WASAPI capture, probing each input at its native rate.

    A remote-desktop session exposes the same physical microphone under several
    backends (MME, DirectSound, WASAPI, WDM-KS) and only some of them carry
    audio — the default device can be exact digital silence while another index
    works. Reject failed, non-finite and effectively silent probes first, then
    prefer WASAPI over other backends. Peak level breaks ties within a backend
    preference, not across it. A quiet microphone need not reach speech level.
    """
    try:
        import numpy as np  # noqa: PLC0415
        import sounddevice as sd  # noqa: PLC0415
    except ImportError:
        return None

    try:
        devices = sd.query_devices()
    except Exception:  # noqa: BLE001 - no devices or PortAudio unavailable
        return None
    try:
        hostapis = sd.query_hostapis()
    except Exception:  # noqa: BLE001 - backend metadata is optional
        hostapis = []
    best: int | None = None
    best_score = (False, 0.0)
    for index, device in enumerate(devices):
        if device["max_input_channels"] < 1:
            continue
        rate = sample_rate
        try:
            native_rate = float(device["default_samplerate"])
            if np.isfinite(native_rate) and native_rate >= 1:
                rate = int(round(native_rate))
        except (KeyError, TypeError, ValueError):
            pass
        peak = _probe_input(index, rate, seconds)
        if peak is None or not np.isfinite(peak) or peak <= 1e-6:
            continue
        try:
            backend = hostapis[device["hostapi"]]["name"]
        except (KeyError, IndexError, TypeError):
            backend = ""
        score = ("wasapi" in backend.lower(), peak)
        if score > best_score:
            best_score = score
            best = index
    return best


def _probe_input(index: int, sample_rate: int, seconds: float) -> float | None:
    """Peak level from a short callback capture, or None if the device fails."""
    import time  # noqa: PLC0415

    import numpy as np  # noqa: PLC0415
    import sounddevice as sd  # noqa: PLC0415

    chunks: list[Any] = []

    def callback(indata, _frames, _time_info, _status) -> None:
        chunks.append(np.asarray(indata, dtype=np.float32).reshape(-1).copy())

    block = max(1, int(sample_rate * 0.1))
    try:
        stream = sd.InputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            blocksize=block,
            device=index,
            callback=callback,
        )
        stream.start()
        time.sleep(seconds)
        stream.stop()
        stream.close()
    except Exception:  # noqa: BLE001 - device rejects the format; skip it
        return None
    if not chunks:
        return None
    return float(np.max(np.abs(np.concatenate(chunks))))


def normalise_audio(samples: Any, *, target_peak: float = 0.9, max_gain: float = 100.0) -> Any:
    """Scale audio so its peak reaches ``target_peak``.

    Redirected microphones (NICE DCV) deliver speech around -40 dBFS, which is
    far too quiet for the recognisers: the *same* recording transcribes to
    garbage at its native level and to near-correct text once amplified. This is
    the fix for that, and it is why the level matters more than the model.
    """
    import numpy as np  # noqa: PLC0415

    audio = np.asarray(samples, dtype=np.float32)
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak <= 0.0:
        return audio
    return audio * min(target_peak / peak, max_gain)


class _RunningAgc:
    """Per-block gain that tracks the running peak, for live streaming.

    A streaming recogniser is fed block by block, so the whole utterance is not
    available to normalise against. This applies the same idea incrementally:
    each block is scaled by the gain implied by the loudest sample seen so far.
    """

    def __init__(self, *, target_peak: float = 0.9, max_gain: float = 100.0) -> None:
        self.target_peak = target_peak
        self.max_gain = max_gain
        self._peak = 0.0

    def apply(self, block: Any) -> Any:
        import numpy as np  # noqa: PLC0415

        audio = np.asarray(block, dtype=np.float32)
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        self._peak = max(self._peak, peak)
        if self._peak <= 0.0:
            return audio
        return audio * min(self.target_peak / self._peak, self.max_gain)


def read_wav(path: Path) -> tuple[Any, int]:
    """Read a mono 16-bit WAV into float32 samples in [-1, 1]."""
    import numpy as np  # noqa: PLC0415

    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1:
            raise HostError(f"{path}: expected mono audio, got {handle.getnchannels()} channels")
        if handle.getsampwidth() != 2:
            raise HostError(f"{path}: expected 16-bit PCM, got {handle.getsampwidth() * 8}-bit")
        frames = handle.readframes(handle.getnframes())
        rate = handle.getframerate()
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    return samples, rate


def _config_key(family: str) -> str:
    """Map a catalog family name to the ``OfflineTtsModelConfig`` field."""
    lowered = family.lower()
    if lowered.startswith("kokoro"):
        return "kokoro"
    if lowered.startswith("matcha"):
        return "matcha"
    return "vits"


@dataclass
class SpeechResult:
    """What a speech task produced."""

    path: Path
    text: str = ""
    sample_rate: int = 0
    duration_s: float = 0.0


class TtsHost:
    """Offline text-to-speech. One model, many voices via speaker id."""

    modality = "tts"

    def __init__(self, *, num_threads: int = 2, speaker_id: int = 0, speed: float = 1.0) -> None:
        self.num_threads = num_threads
        self.speaker_id = speaker_id
        self.speed = speed
        self._tts = None
        self._model: InstalledModel | None = None

    @property
    def loaded_model_id(self) -> str | None:
        return self._model.descriptor.id if self._model else None

    @property
    def sample_rate(self) -> int:
        if self._tts is None:
            raise HostError("no TTS model loaded")
        return int(self._tts.sample_rate)

    @property
    def num_speakers(self) -> int:
        if self._tts is None:
            return 0
        return int(getattr(self._tts, "num_speakers", 1))

    def load(self, model: InstalledModel) -> None:
        if self._model and self._model.descriptor.id == model.descriptor.id:
            return
        self.unload()
        sherpa_onnx = _require_sherpa()
        params = model.descriptor.params

        # Kokoro and VITS take different model configs; pick by family.
        if model.descriptor.family.lower().startswith("kokoro"):
            model_config = sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=str(model.file(params["model_file"])),
                voices=str(model.file(params["voices"])),
                tokens=str(model.file(params["tokens"])),
                data_dir=str(model.file(params["data_dir"])) if params.get("data_dir") else "",
                lang=params.get("lang", "en-us"),
            )
        else:
            model_config = sherpa_onnx.OfflineTtsVitsModelConfig(
                model=str(model.file(params["model_file"])),
                tokens=str(model.file(params["tokens"])),
                data_dir=str(model.file(params["data_dir"])) if params.get("data_dir") else "",
            )

        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                **{_config_key(model.descriptor.family): model_config},
                num_threads=self.num_threads,
                provider="cpu",
            ),
            max_num_sentences=1,
        )
        if not config.validate():
            raise HostError(f"invalid TTS config for {model.descriptor.id}")
        self._tts = sherpa_onnx.OfflineTts(config)
        self._model = model

    def unload(self) -> None:
        self._tts = None
        self._model = None

    def synthesize(self, text: str, out_path: Path) -> SpeechResult:
        """Synthesize one utterance to a WAV file."""
        if self._tts is None:
            raise HostError("no TTS model loaded")
        sherpa_onnx = _require_sherpa()
        gen = sherpa_onnx.GenerationConfig()
        gen.sid = self.speaker_id
        gen.speed = self.speed
        audio = self._tts.generate(text, gen)
        write_wav(out_path, audio.samples, audio.sample_rate)
        duration = len(audio.samples) / float(audio.sample_rate)
        return SpeechResult(out_path, text=text, sample_rate=audio.sample_rate, duration_s=duration)

    def synthesize_samples(self, text: str):
        """Synthesize one utterance and return ``(samples, sample_rate)``."""
        if self._tts is None:
            raise HostError("no TTS model loaded")
        sherpa_onnx = _require_sherpa()
        gen = sherpa_onnx.GenerationConfig()
        gen.sid = self.speaker_id
        gen.speed = self.speed
        audio = self._tts.generate(text, gen)
        return audio.samples, int(audio.sample_rate)

    def speak_streaming(
        self,
        text: str,
        out_dir: Path,
        player: AudioPlayer | None = None,
        *,
        prefix: str = "line",
        index: int = 0,
    ) -> list[SpeechResult]:
        """Speak ``text`` sentence by sentence, playing each as it is ready.

        The first sentence is synthesised and queued for playback before the
        last one is synthesised, so speech starts almost immediately instead of
        after the whole reply is rendered. Each sentence is also written to a
        WAV so the run can be inspected afterwards.
        """
        from orchestrator.streaming import stream_sentences  # noqa: PLC0415

        out_dir.mkdir(parents=True, exist_ok=True)
        results: list[SpeechResult] = []
        for n, sentence in enumerate(stream_sentences([text])):
            if not sentence.strip():
                continue
            samples, rate = self.synthesize_samples(sentence)
            path = out_dir / f"{prefix}_{index:03d}_{n:02d}.wav"
            write_wav(path, samples, rate)
            duration = len(samples) / float(rate)
            results.append(
                SpeechResult(path, text=sentence, sample_rate=rate, duration_s=duration)
            )
            if player is not None:
                player.play(samples, rate)
        return results

    def stream_sentences(
        self,
        sentences: Iterator[str],
        out_dir: Path,
        *,
        prefix: str = "tts",
    ) -> Iterator[SpeechResult]:
        """Synthesize each sentence as it arrives (sentence-level streaming)."""
        out_dir.mkdir(parents=True, exist_ok=True)
        for index, sentence in enumerate(sentences):
            if not sentence.strip():
                continue
            yield self.synthesize(sentence, out_dir / f"{prefix}_{index:03d}.wav")

    def run(self, spec: dict[str, Any]) -> SpeechResult:
        return self.synthesize(spec["text"], Path(spec["out_path"]))


def trim_silence(samples: Any, *, rate: int = 16000, threshold: float = 0.004,
                 block_ms: int = 20, pad_ms: int = 100) -> Any:
    """Drop leading and trailing quiet from a waveform, keeping a small pad.

    Needed before offline recognition: Whisper treats long stretches of silence
    as a cue to keep generating, which produces repeated text. A little padding
    is kept so the first and last phonemes are not clipped.
    """
    import numpy as np  # noqa: PLC0415

    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    block = max(1, int(rate * block_ms / 1000))
    if len(audio) <= block:
        return audio
    # Per-block RMS, then find the span above the threshold.
    count = len(audio) // block
    energies = np.array([
        float(np.sqrt(np.mean(audio[i * block:(i + 1) * block] ** 2)))
        for i in range(count)
    ])
    loud = np.flatnonzero(energies >= threshold)
    if not len(loud):
        return audio
    pad = max(1, int(rate * pad_ms / 1000))
    start = max(0, loud[0] * block - pad)
    end = min(len(audio), (loud[-1] + 1) * block + pad)
    return audio[start:end]


class SttHost:
    """Streaming speech-to-text. Emits partials, then a final transcript."""

    modality = "stt"

    def __init__(self, *, num_threads: int = 2, chunk_ms: int = 100) -> None:
        self.num_threads = num_threads
        self.chunk_ms = chunk_ms
        self._recognizer = None
        self._model: InstalledModel | None = None
        self._last_partial = ""
        self._streaming = True
        #: Whether to scale input to unit peak before decoding. Whisper needs
        #: it on quiet remote-desktop audio; SenseVoice does it internally.
        self._normalise_level = False
        #: Optional diagnostic observer; receives the exact prepared float32 input.
        self.on_decode_audio: Any = None
        #: Text from segments the recogniser has already closed at a pause.
        self._segments: list[str] = []

    @property
    def loaded_model_id(self) -> str | None:
        return self._model.descriptor.id if self._model else None

    @property
    def sample_rate(self) -> int:
        """The rate the loaded recogniser expects (16 kHz for these models)."""
        if self._model is None:
            return 16000
        return int(self._model.descriptor.params.get("sample_rate", 16000))

    def load(self, model: InstalledModel) -> None:
        if self._model and self._model.descriptor.id == model.descriptor.id:
            return
        self.unload()
        sherpa_onnx = _require_sherpa()
        params = model.descriptor.params
        kind = str(params.get("kind", "zipformer-streaming"))

        if kind == "whisper":
            # Offline and non-streaming: no partials, but far more accurate on
            # this hardware (measured 10% WER against 23% for the streaming
            # transducer, and 78% for the old 20M model).
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
                encoder=str(model.file(params["encoder"])),
                decoder=str(model.file(params["decoder"])),
                tokens=str(model.file(params["tokens"])),
                num_threads=self.num_threads,
                language=str(params.get("language", "en")),
            )
            self._streaming = False
            self._normalise_level = True
        elif kind == "zipformer-streaming":
            self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=str(model.file(params["tokens"])),
                encoder=str(model.file(params["encoder"])),
                decoder=str(model.file(params["decoder"])),
                joiner=str(model.file(params["joiner"])),
                num_threads=self.num_threads,
                provider="cpu",
                sample_rate=int(params.get("sample_rate", 16000)),
                feature_dim=80,
                decoding_method="greedy_search",
                enable_endpoint_detection=True,
            )
            self._streaming = True
        elif kind == "sensevoice":
            # SenseVoice: encoder-only, non-autoregressive, so it cannot fall
            # into the repetition loop the Whisper decoder shows. Measured both
            # the most accurate and by far the fastest on real microphone audio.
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(model.file(params["model"])),
                tokens=str(model.file(params["tokens"])),
                num_threads=self.num_threads,
                language=str(params.get("language", "en")),
                use_itn=True,
            )
            self._streaming = False
            self._normalise_level = False
        else:
            raise HostError(f"unsupported STT model kind: {kind!r}")
        self._model = model

    def unload(self) -> None:
        self._recognizer = None
        self._model = None
        self._normalise_level = False
        self._segments = []

    def _new_stream(self) -> Any:
        if self._recognizer is None:
            raise HostError("no STT model loaded")
        self._last_partial = ""
        self._segments = []
        return self._recognizer.create_stream()

    @property
    def is_streaming(self) -> bool:
        """Whether the loaded model produces partial results as audio arrives."""
        return self._streaming

    def _drain(self, stream: Any, on_partial=None) -> None:
        """Decode whatever audio has been accepted, reporting new partials."""
        while self._recognizer.is_ready(stream):
            self._recognizer.decode_stream(stream)
        if on_partial is not None:
            partial = self.full_text(stream)
            if partial and partial != self._last_partial:
                self._last_partial = partial
                on_partial(partial)

    def full_text(self, stream: Any) -> str:
        """Everything recognised so far, including segments already closed.

        The recogniser ends a segment at a natural pause. Its text then has to
        be captured and the stream reset, or ``reset`` discards those words and
        the transcription silently loses the start of every utterance.
        """
        current = self._recognizer.get_result(stream)
        parts = [*self._segments]
        if current:
            parts.append(current)
        return " ".join(parts).strip()

    def _close_segment(self, stream: Any) -> None:
        """Keep the finished segment's words, then start a fresh segment."""
        text = self._recognizer.get_result(stream)
        if text:
            self._segments.append(text)
            self._last_partial = ""
        self._recognizer.reset(stream)

    def _finish(self, stream: Any, rate: int, on_partial=None) -> str:
        """Flush trailing silence so the last words decode, then return text."""
        import numpy as np  # noqa: PLC0415

        stream.accept_waveform(rate, np.zeros(int(0.66 * rate), dtype=np.float32))
        stream.input_finished()
        self._drain(stream, on_partial)
        return self.full_text(stream)

    def transcribe_samples(
        self,
        blocks: Iterable[Any],
        *,
        rate: int,
        on_partial=None,
    ) -> str:
        """Recognise a stream of float32 blocks as they arrive.

        This is the shared path for both file and live-microphone input: the
        caller yields blocks and gets partials back while the utterance is
        still in progress.
        """
        stream = self._new_stream()
        agc = _RunningAgc()
        for mono in blocks:
            stream.accept_waveform(rate, agc.apply(mono))
            self._drain(stream, on_partial)
            if self._recognizer.is_endpoint(stream):
                break
        return self._finish(stream, rate, on_partial)

    def transcribe_stream(
        self,
        wav_path: Path,
        *,
        on_partial=None,
    ) -> str:
        """Transcribe a WAV file, reporting partials as they are recognised.

        Offline models (Whisper) are given the whole waveform at once and report
        no partials; streaming models are fed in chunks.
        """
        if self._recognizer is None:
            raise HostError("no STT model loaded")
        samples, rate = read_wav(wav_path)
        if not self._streaming:
            return self._transcribe_offline(samples, rate)
        return self.transcribe_samples(
            (samples[i : i + max(1, int(rate * self.chunk_ms / 1000))]
             for i in range(0, len(samples), max(1, int(rate * self.chunk_ms / 1000)))),
            rate=rate,
            on_partial=on_partial,
        )

    def _transcribe_offline(self, samples: Any, rate: int) -> str:
        """Decode a complete waveform with an offline recogniser.

        The waveform is trimmed of leading and trailing silence first. Whisper
        is known to loop on padding — fed a recording with a second of quiet at
        the end it can emit ``"Hello Hello Hello ..."`` indefinitely — so
        trimming is not cosmetic, it is what makes the transcript usable. A
        short lead-in is then added back, because Whisper can also drop a word
        when the waveform begins mid-phoneme.
        """
        import numpy as np  # noqa: PLC0415

        audio = np.asarray(samples, dtype=np.float32)
        if not len(audio):
            return ""
        if rate != 16000:
            count = int(len(audio) * 16000 / rate)
            positions = np.linspace(0, len(audio) - 1, count)
            audio = np.interp(positions, np.arange(len(audio)), audio).astype(np.float32)
        # Whisper needs silence trimming to avoid decoder repetition. SenseVoice
        # does not: trimming changed saved "Quit" to "Twet" in the real replay
        # and can remove useful boundary context from short utterances.
        if self._normalise_level:
            audio = trim_silence(audio)
        # Redirected microphones deliver speech around -40 dBFS, which is far
        # too quiet for any recogniser. Normalising the whole utterance is the
        # single biggest accuracy win on this hardware.
        audio = normalise_audio(audio)
        if self._normalise_level:
            # Whisper can drop a word when the waveform starts mid-phoneme.
            audio = np.concatenate(
                [np.zeros(int(0.2 * 16000), dtype=np.float32), audio]
            )
        if self.on_decode_audio is not None:
            self.on_decode_audio(audio.copy(), 16000)
        stream = self._recognizer.create_stream()
        stream.accept_waveform(16000, audio)
        self._recognizer.decode_stream(stream)
        return str(stream.result.text).strip()

    def transcribe_utterance(self, samples: Any, rate: int) -> str:
        """Transcribe one already-captured utterance.

        Used by the continuous recorder, which captures audio in the background
        and hands finished utterances here. Offline models decode the whole
        waveform; streaming models are fed in chunks.
        """
        if self._recognizer is None:
            raise HostError("no STT model loaded")
        if not self._streaming:
            return self._transcribe_offline(samples, rate)
        chunk = max(1, int(rate * self.chunk_ms / 1000))
        return self.transcribe_samples(
            (samples[i : i + chunk] for i in range(0, len(samples), chunk)), rate=rate
        )

    def listen(
        self,
        recorder: Any,
        *,
        on_partial=None,
        on_block=None,
    ) -> str:
        """Transcribe one utterance from a live :class:`ContinuousRecorder`.

        A **streaming** recogniser is fed blocks as they arrive and endpoints
        itself. An **offline** recogniser cannot decode incrementally, so the
        recorder captures the whole utterance first (energy-endpointed) and it
        is decoded in one pass — which is far more accurate on this hardware.
        """
        if self._recognizer is None:
            raise HostError("no STT model loaded")

        if self._streaming:
            def blocks():
                while True:
                    block = recorder.next_block(timeout=120.0)
                    if block is None:
                        return
                    if on_block is not None:
                        on_block(block)
                    yield block

            return self.listen_stream(blocks(), rate=recorder.sample_rate, on_partial=on_partial)

        captured = recorder.record_utterance(on_block=on_block)
        if captured is None:
            return ""
        return self._transcribe_offline(captured, recorder.sample_rate)

    def listen_stream(
        self,
        blocks: Iterator[Any],
        *,
        rate: int,
        on_partial=None,
    ) -> str:
        """Consume a live block stream and return one utterance's text.

        The recogniser does the endpointing: blocks are fed as they arrive and
        the utterance ends when the model says so (``is_endpoint``), not when an
        energy threshold says so. That is both simpler and more accurate — the
        model knows where speech ends, a volume level does not.

        Offline models cannot decode incrementally, so they buffer until the
        stream ends; streaming models emit partials as they go.
        """
        if self._recognizer is None:
            raise HostError("no STT model loaded")

        if not self._streaming:
            collected: list[Any] = []
            for mono in blocks:
                collected.append(mono)
            if not collected:
                return ""
            import numpy as np  # noqa: PLC0415

            return self._transcribe_offline(np.concatenate(collected), rate)

        stream = self._new_stream()
        agc = _RunningAgc()
        for mono in blocks:
            stream.accept_waveform(rate, agc.apply(mono))
            self._drain(stream, on_partial)
            # The recogniser endpoints on silence as well as on speech, so an
            # endpoint can fire before the player has said anything. Honouring
            # it then would return an empty utterance and the caller would move
            # on. Only end once there is actually text to return.
            if self._recognizer.is_endpoint(stream) and self.full_text(stream):
                break
        return self._finish(stream, rate, on_partial)

    def transcribe_mic(
        self,
        recorder: Any,
        *,
        on_partial=None,
        on_block=None,
        on_ready=None,
    ) -> str:
        """Record one utterance from the microphone and transcribe it.

        Streaming models are fed each block as it arrives, so partials appear
        while the player is still speaking. Offline models (Whisper) cannot
        decode incrementally, so the utterance is buffered and decoded once the
        player stops — that is the price of the much higher accuracy.
        """
        if self._recognizer is None:
            raise HostError("no STT model loaded")

        if not self._streaming:
            blocks: list[Any] = []

            def collect(mono: Any) -> None:
                blocks.append(mono)
                if on_block is not None:
                    on_block(mono)

            captured = recorder.record_utterance(on_block=collect, on_ready=on_ready)
            if captured is None or not blocks:
                return ""
            import numpy as np  # noqa: PLC0415

            audio = np.concatenate(blocks)
            return self._transcribe_offline(audio, self.sample_rate)

        rate = self.sample_rate
        stream = self._new_stream()
        agc = _RunningAgc()

        def feed(mono: Any) -> None:
            stream.accept_waveform(rate, agc.apply(mono))
            self._drain(stream, on_partial)
            if on_block is not None:
                on_block(mono)

        captured = recorder.record_utterance(on_block=feed, on_ready=on_ready)
        if captured is None:
            # No speech detected: whatever the recogniser made of the ambient
            # noise is not an answer, so do not return a hallucinated word.
            return ""
        return self._finish(stream, rate, on_partial)

    def run(self, spec: dict[str, Any]) -> SpeechResult:
        text = self.transcribe_stream(Path(spec["wav_path"]), on_partial=spec.get("on_partial"))
        return SpeechResult(Path(spec["wav_path"]), text=text)
