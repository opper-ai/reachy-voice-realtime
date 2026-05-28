"""Audio I/O between Reachy and the Opper realtime WS.

MicCapture   — thread reads from mic (Reachy default, Mac fallback) via the
               SDK's media manager, resamples to 24 kHz, hands int16 PCM
               chunks to a callback.
SpeakerPlayer — writes model audio (int16 @ 24 kHz, b64) directly to the
               Reachy USB audio device using sounddevice. We bypass the
               SDK's local-audio path here because it routes to the macOS
               system default output (MacBook speakers), not the robot.
"""

from __future__ import annotations

import base64
import queue
import threading
from typing import Callable, Optional

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

MODEL_SAMPLE_RATE = 24_000  # gpt-realtime-2 default; confirmed via session.started
REACHY_AUDIO_DEVICE = "Reachy Mini Audio"


def _to_int16_bytes(f32: np.ndarray) -> bytes:
    clipped = np.clip(f32, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def _b64_to_float32(b64: str) -> np.ndarray:
    raw = base64.b64decode(b64)
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def _resample(f32: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    if from_rate == to_rate:
        return f32
    g = np.gcd(from_rate, to_rate)
    return resample_poly(f32, to_rate // g, from_rate // g).astype(np.float32)


class MicCapture:
    """Pumps mic samples → int16 PCM chunks @ 24 kHz → callback."""

    def __init__(self, mini, source: str, on_chunk: Callable[[bytes], None]):
        self._mini = mini
        self._source = source  # "reachy" or "mac"
        self._on_chunk = on_chunk
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sd_stream = None

    def start(self) -> None:
        if self._source == "reachy":
            self._mini.media.start_recording()
            self._thread = threading.Thread(target=self._reachy_loop, daemon=True)
            self._thread.start()
        else:
            self._start_mac()

    def stop(self) -> None:
        self._stop.set()
        if self._sd_stream is not None:
            try:
                self._sd_stream.stop()
                self._sd_stream.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._source == "reachy":
            try:
                self._mini.media.stop_recording()
            except Exception:
                pass

    def _reachy_loop(self) -> None:
        in_rate = self._mini.media.get_input_audio_samplerate()
        while not self._stop.is_set():
            sample = self._mini.media.get_audio_sample()
            if sample is None or len(sample) == 0:
                self._stop.wait(0.01)
                continue
            f32 = sample.astype(np.float32)
            if f32.ndim == 2:
                f32 = f32.mean(axis=1)
            f32 = _resample(f32, in_rate, MODEL_SAMPLE_RATE)
            self._on_chunk(_to_int16_bytes(f32))

    def _start_mac(self) -> None:
        def cb(indata, frames, time_info, status):  # noqa: ARG001
            if self._stop.is_set():
                return
            self._on_chunk(bytes(indata))

        self._sd_stream = sd.RawInputStream(
            samplerate=MODEL_SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=1024,
            callback=cb,
        )
        self._sd_stream.start()


class SpeakerPlayer:
    """Plays int16 PCM @ 24 kHz onto the Reachy USB audio device.

    push(b64) decodes + resamples + queues. A worker thread drains and writes
    to the sounddevice stream. stop_playback() aborts the stream (drops any
    buffered audio) for clean barge-in.
    """

    def __init__(self, device_name: str = REACHY_AUDIO_DEVICE):
        info = sd.query_devices(device_name, "output")
        self._device = info["name"]  # use the canonical name sounddevice gave us
        self._out_rate = int(info["default_samplerate"])
        self._channels = min(int(info["max_output_channels"]) or 1, 2)
        self._stream: Optional[sd.RawOutputStream] = None
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._stream = sd.RawOutputStream(
            device=self._device,
            samplerate=self._out_rate,
            channels=self._channels,
            dtype="int16",
        )
        self._stream.start()
        self._thread.start()

    def push(self, b64: str) -> None:
        f32 = _b64_to_float32(b64)
        f32 = _resample(f32, MODEL_SAMPLE_RATE, self._out_rate)
        i16 = (np.clip(f32, -1.0, 1.0) * 32767.0).astype("<i2")
        if self._channels == 2:
            i16 = np.column_stack([i16, i16])
        self._q.put(i16.tobytes())

    def stop_playback(self) -> None:
        # Barge-in: drop queued chunks and abort whatever the device is mid-playing.
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        if self._stream is not None:
            try:
                self._stream.abort()
                self._stream.start()
            except Exception:
                pass

    def close(self) -> None:
        self._stop.set()
        self._q.put(None)
        self._thread.join(timeout=1.0)
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            chunk = self._q.get()
            if chunk is None or self._stream is None:
                return
            try:
                self._stream.write(chunk)
            except Exception:
                pass
