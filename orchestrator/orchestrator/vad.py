"""Local, stateful Silero VAD; normalization never changes recorded PCM."""

from __future__ import annotations

import math
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

import numpy as np

from .hosts.base import HostError


class BlockVad(Protocol):
    """One serialized classification per model-rate block."""

    def process(self, block: Any) -> bool: ...


class SileroVad:
    """CPU sherpa VAD with bounded, running input gain and a calibrated noise floor.

    Gain controls are explicit replay parameters, not recorder onset thresholds.
    Calibrate on quiet audio before capture to leave ambient noise unamplified.
    Without calibration, Silero itself remains responsible for noise rejection.
    """

    def __init__(
        self,
        model: Path,
        *,
        sample_rate: int = 16000,
        threshold: float = 0.5,
        min_speech_duration: float = 0.16,
        min_silence_duration: float = 0.35,
        target_rms: float = 0.1,
        peak_limit: float = 0.5,
        max_gain: float = 100.0,
        gain_time_s: float = 0.2,
    ) -> None:
        if sample_rate != 16000:
            raise ValueError("SileroVad requires 16000 Hz model-rate audio")
        if not model.is_file():
            raise FileNotFoundError(model)
        if not 0 < threshold < 1:
            raise ValueError("threshold must be between zero and one")
        controls = (target_rms, peak_limit, max_gain, gain_time_s,
                    min_speech_duration, min_silence_duration)
        if not all(math.isfinite(value) and value > 0 for value in controls):
            raise ValueError("VAD durations and gain controls must be finite and positive")
        if max_gain < 1 or peak_limit > 1:
            raise ValueError("max_gain must be >= 1 and peak_limit <= 1")
        try:
            import sherpa_onnx  # noqa: PLC0415
        except ImportError as exc:
            raise HostError("sherpa-onnx is required for Silero VAD") from exc
        config = sherpa_onnx.VadModelConfig(
            silero_vad=sherpa_onnx.SileroVadModelConfig(
                model=str(model), threshold=threshold, window_size=512,
                min_speech_duration=min_speech_duration,
                min_silence_duration=min_silence_duration,
            ),
            sample_rate=sample_rate, num_threads=1, provider="cpu",
        )
        if not config.validate():
            raise HostError(f"Invalid Silero VAD configuration: {model}")
        self._detector = sherpa_onnx.VoiceActivityDetector(
            config, buffer_size_in_seconds=30,
        )
        self.sample_rate = sample_rate
        self._target_rms = target_rms
        self._peak_limit = peak_limit
        self._max_gain = max_gain
        self._gain_time_s = gain_time_s
        self._noise_threshold = 0.0
        self._running_rms = 0.0
        self._pending = np.empty(0, dtype=np.float32)
        self._lock = RLock()

    def set_noise_floor(self, noise_rms: float, *, floor_multiple: float = 4.0) -> None:
        """Use measured native-PCM noise, never a global amplitude threshold."""
        if not math.isfinite(noise_rms) or noise_rms < 0:
            raise ValueError("noise_rms must be finite and nonnegative")
        if not math.isfinite(floor_multiple) or floor_multiple <= 0:
            raise ValueError("floor_multiple must be finite and positive")
        with self._lock:
            self._noise_threshold = noise_rms * floor_multiple
            self.reset()

    def reset(self) -> None:
        """Reset stream state, retaining the measured noise floor."""
        with self._lock:
            self._detector.reset()
            self._pending = np.empty(0, dtype=np.float32)
            self._running_rms = 0.0

    def process(self, block: Any) -> bool:
        """Feed complete 512-sample windows once; drain generated segments."""
        samples = np.asarray(block, dtype=np.float32)
        if samples.ndim != 1 or not np.all(np.isfinite(samples)):
            raise ValueError("VAD audio must be finite mono samples")
        with self._lock:
            self._pending = np.concatenate((self._pending, samples))
            detected = False
            processed = len(self._pending) // 512 * 512
            for offset in range(0, processed, 512):
                window = self._pending[offset:offset + 512]
                rms = float(np.sqrt(np.mean(window.astype(np.float64) ** 2)))
                peak = float(np.max(np.abs(window)))
                alpha = math.exp(-512 / (self.sample_rate * self._gain_time_s))
                self._running_rms = (alpha * self._running_rms + (1 - alpha) * rms
                                     if self._running_rms else rms)
                gain = min(1.0, self._peak_limit / peak) if peak else 1.0
                if rms > self._noise_threshold and peak > 0:
                    gain = min(self._max_gain,
                               self._target_rms / max(rms, self._running_rms),
                               self._peak_limit / peak)
                self._detector.accept_waveform((window * gain).astype(np.float32))
                detected = bool(self._detector.is_speech_detected()) or detected
                while not self._detector.empty():
                    self._detector.pop()
            self._pending = self._pending[processed:].copy()
            return detected if processed else bool(self._detector.is_speech_detected())