"""Sound-localisation behaviours.

DoaBuffer — background thread that polls `mini.media.get_DoA()` ~4 Hz and
            remembers the most recent *voice-positive* direction (angle in
            degrees, voice/non-voice flag, timestamp).

AutoOrient — async task that nudges body_yaw + head_yaw toward the latest
            buffered voice direction so Reachy physically faces whoever
            spoke last. Combines head (faster, smaller travel) with body
            (slower, larger travel) so it reads as Reachy *turning*, not
            just rotating its torso. Suppressed while Reachy is speaking
            and while a recent tool motion is still settling.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Callable, Optional, Tuple

from reachy_mini.utils import create_head_pose


class DoaBuffer:
    """Latest voice-positive DoA reading + timestamp."""

    def __init__(self, mini, poll_hz: float = 4.0):
        self._mini = mini
        self._period = 1.0 / max(0.1, poll_hz)
        self._lock = threading.Lock()
        self._last_voice: Optional[Tuple[float, float]] = None  # (angle°, t)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    def latest_voice(self, max_age: float = 2.0) -> Optional[float]:
        """Most recent voice-positive angle, or None if stale or absent."""
        with self._lock:
            if self._last_voice is None:
                return None
            angle, t = self._last_voice
        if time.time() - t > max_age:
            return None
        return angle

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                doa = self._mini.media.get_DoA()
                if doa is not None:
                    angle, is_voice = doa
                    if is_voice:
                        with self._lock:
                            self._last_voice = (float(angle), time.time())
            except Exception:
                pass
            self._stop.wait(self._period)


class AutoOrient:
    """Smoothly orient body + head toward the latest voice DoA.

    Tick at 4 Hz. Each tick:
      * No-op if Reachy is currently speaking (model's voice playing) — we
        don't want to fight motion the model is driving.
      * No-op if no fresh voice DoA in the buffer.
      * Otherwise compute split target: 0.6×angle for body, 0.4×angle for
        head_yaw. Rate-limit step size so the motion looks deliberate.

    Combined head + body movement reads as Reachy *turning to face* the
    sound rather than just rotating its base.
    """

    BODY_FRACTION = 0.6
    HEAD_FRACTION = 0.4
    DEADBAND_DEG = 8.0
    STEP_DEG_PER_TICK = 6.0   # at 4 Hz → ~24°/s max
    TICK_HZ = 4.0
    MAX_BODY = 70.0
    MAX_HEAD_YAW = 60.0

    def __init__(self, mini, doa: DoaBuffer, is_speaking: Callable[[], bool]):
        self._mini = mini
        self._doa = doa
        self._is_speaking = is_speaking
        self._period = 1.0 / self.TICK_HZ
        self._task: Optional[asyncio.Task] = None
        self._stop = False
        self._current_body = 0.0
        self._current_head = 0.0

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._task = loop.create_task(self._loop(), name="auto-orient")

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        while not self._stop:
            try:
                self._tick()
            except Exception as e:
                print(f"  (auto-orient error: {e})")
            await asyncio.sleep(self._period)

    def _tick(self) -> None:
        if self._is_speaking():
            return
        target = self._doa.latest_voice(max_age=2.0)
        if target is None:
            return
        target = max(-90.0, min(90.0, float(target)))

        body_goal = max(-self.MAX_BODY, min(self.MAX_BODY, target * self.BODY_FRACTION))
        head_goal = max(-self.MAX_HEAD_YAW, min(self.MAX_HEAD_YAW, target * self.HEAD_FRACTION))

        dbody = body_goal - self._current_body
        dhead = head_goal - self._current_head
        if abs(dbody) < self.DEADBAND_DEG and abs(dhead) < self.DEADBAND_DEG:
            return

        sb = _clip(dbody, self.STEP_DEG_PER_TICK) if abs(dbody) >= self.DEADBAND_DEG else 0.0
        sh = _clip(dhead, self.STEP_DEG_PER_TICK) if abs(dhead) >= self.DEADBAND_DEG else 0.0
        self._current_body += sb
        self._current_head += sh

        try:
            self._mini.set_target(
                head=create_head_pose(yaw=self._current_head),
                body_yaw=self._current_body,
            )
        except Exception:
            pass


def _clip(value: float, max_abs: float) -> float:
    return max(-max_abs, min(max_abs, value))
