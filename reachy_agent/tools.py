"""Tool schemas the realtime model sees, and the Python dispatcher that
maps tool.call events to actions on the robot.

Behaviors live in /Users/jose/work/reachy/behaviors.py and are imported here
unchanged so the dispatcher and the standalone CLI share one source of truth.
"""

from __future__ import annotations

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

# Import top-level behaviors.py from the parent dir.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import behaviors as B  # noqa: E402

from reachy_mini.utils import create_head_pose  # noqa: E402

from .orient import DoaBuffer  # noqa: E402
from .vision import Eyes  # noqa: E402

# --------------------------------------------------------------------------- #
# JSON-Schema tool definitions sent at mint time.
# --------------------------------------------------------------------------- #

LEAN_DIRS = ["left", "right", "forward", "back"]

TOOL_SCHEMAS: list[dict[str, Any]] = [
    # ------- High-level emotes (one tool per behavior) -------
    {
        "name": "nod",
        "description": "Nod your head yes 3 times. Use when agreeing.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "shake_head",
        "description": "Shake your head no. Use when disagreeing.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "wiggle_antennas",
        "description": "Wiggle your antennas rapidly. Use when excited or happy.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "dance",
        "description": "Play music and do a short dance routine.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "look_confused",
        "description": "Tilt your head and play a confused sound. Use when puzzled.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "greet",
        "description": "Greet someone: head sweep, antenna wiggle, audible Hi!",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "wave",
        "description": "Wave hello/goodbye — antennas wag side to side with a small body sway.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "lean",
        "description": "Lean in a direction (left/right/forward/back). Use to show interest or react.",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": LEAN_DIRS},
                "amount": {
                    "type": "number",
                    "description": "0.1 (subtle) to 1.0 (full). Default 1.0.",
                },
            },
            "required": ["direction"],
        },
    },
    {
        "name": "look_around",
        "description": "Sweep head left-right to scan the scene.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "sleep",
        "description": "Go to rest pose (motors disengage). Only when explicitly told to sleep.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "wake_up",
        "description": "Wake up from sleep pose.",
        "parameters": {"type": "object", "properties": {}},
    },
    # ------- Primitive motor tools (compose anything) -------
    {
        "name": "set_head",
        "description": "Set head pose. Each axis in degrees. Pitch and roll clamp to ±40°; yaw to ±180°.",
        "parameters": {
            "type": "object",
            "properties": {
                "yaw": {"type": "number"},
                "pitch": {"type": "number"},
                "roll": {"type": "number"},
                "duration": {
                    "type": "number",
                    "description": "Seconds for the motion. Default 0.4.",
                },
            },
        },
    },
    {
        "name": "set_antennas",
        "description": "Set both antenna positions. Values roughly -1.0 to 1.0.",
        "parameters": {
            "type": "object",
            "properties": {
                "left": {"type": "number"},
                "right": {"type": "number"},
                "duration": {"type": "number"},
            },
            "required": ["left", "right"],
        },
    },
    {
        "name": "set_body_yaw",
        "description": "Rotate body to angle in degrees. Combined with head_yaw the daemon clamps to ±65°.",
        "parameters": {
            "type": "object",
            "properties": {
                "angle": {"type": "number"},
                "duration": {"type": "number"},
            },
            "required": ["angle"],
        },
    },
    {
        "name": "look_at",
        "description": "Look at a point in your camera frame. u and v are normalised 0..1 (0,0 = top-left).",
        "parameters": {
            "type": "object",
            "properties": {
                "u": {"type": "number"},
                "v": {"type": "number"},
            },
            "required": ["u", "v"],
        },
    },
    {
        "name": "perform_sequence",
        "description": (
            "Run a short custom motion as a list of keyframes (3-6 frames recommended). "
            "Use this to mirror gestures or choreograph beats. Each frame is one goto_target call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "frames": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "head": {
                                "type": "object",
                                "properties": {
                                    "yaw": {"type": "number"},
                                    "pitch": {"type": "number"},
                                    "roll": {"type": "number"},
                                },
                            },
                            "antennas": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 2,
                                "maxItems": 2,
                                "description": "[left, right]",
                            },
                            "body_yaw": {"type": "number"},
                            "duration": {
                                "type": "number",
                                "description": "0.2-0.4 typical",
                            },
                        },
                        "required": ["duration"],
                    },
                }
            },
            "required": ["frames"],
        },
    },
    # ------- Perception -------
    {
        "name": "look",
        "description": (
            "Take a fresh look — capture a camera frame and send it to yourself as image input "
            "before continuing. Use whenever you need current visual context."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_sound_direction",
        "description": "Return the direction of the last detected sound (degrees from front, ±180) and whether it was voice.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "turn_toward_sound",
        "description": "Read the sound direction and rotate your body to face it. Use when someone speaks off to the side.",
        "parameters": {"type": "object", "properties": {}},
    },
]


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #


class ToolDispatcher:
    """Runs tool calls. Long-running motions execute on a thread pool so the
    realtime WS event loop can return tool.result immediately.
    """

    def __init__(
        self,
        mini,
        eyes: Eyes,
        send_event: Callable[[dict[str, Any]], Awaitable[None]],
        loop: asyncio.AbstractEventLoop,
        doa_buffer: Optional[DoaBuffer] = None,
    ):
        self._mini = mini
        self._eyes = eyes
        self._send_event = send_event
        self._loop = loop
        self._doa_buffer = doa_buffer
        self._exec = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool")
        self._frame_dims: tuple[int, int] | None = None  # (W, H) cached on first look_at

    async def dispatch(self, name: str, args: dict[str, Any]) -> str:
        handler = _HANDLERS.get(name)
        if handler is None:
            return f"unknown tool: {name}"
        try:
            return await handler(self, args)
        except Exception as e:
            return f"error in {name}: {e}"

    # -- helpers used by individual handlers --

    def _run_blocking(self, fn: Callable[[], Any]) -> Awaitable[Any]:
        return self._loop.run_in_executor(self._exec, fn)

    def _fire_and_forget(self, fn: Callable[[], Any]) -> None:
        self._exec.submit(fn)

    def _frame_size(self) -> tuple[int, int]:
        if self._frame_dims is None:
            frame = self._mini.media.get_frame()
            if frame is not None and frame.ndim >= 2:
                h, w = frame.shape[:2]
                self._frame_dims = (w, h)
            else:
                self._frame_dims = (640, 480)
        return self._frame_dims


# --------------------------------------------------------------------------- #
# Handlers — async; each takes (dispatcher, args) and returns the result str.
# --------------------------------------------------------------------------- #


async def _h_emote(d: ToolDispatcher, args: dict[str, Any], func, desc: str) -> str:
    d._fire_and_forget(lambda: func(d._mini))
    return desc


async def nod(d, _a): return await _h_emote(d, _a, B.yes, "nodded yes")
async def shake_head(d, _a): return await _h_emote(d, _a, B.no, "shook head")
async def wiggle_antennas(d, _a): return await _h_emote(d, _a, B.excitement, "wiggled excitedly")
async def dance(d, _a): return await _h_emote(d, _a, B.dance, "danced")
async def look_confused(d, _a): return await _h_emote(d, _a, B.questionable, "tilted head, confused")
async def greet(d, _a): return await _h_emote(d, _a, B.greet, "greeted")
async def wave(d, _a): return await _h_emote(d, _a, B.wave, "waved")
async def look_around(d, _a): return await _h_emote(d, _a, B.look_around, "looked around")


async def lean(d: ToolDispatcher, args: dict[str, Any]) -> str:
    direction = args.get("direction", "left")
    amount = float(args.get("amount", 1.0))
    d._fire_and_forget(lambda: B.lean(d._mini, direction=direction, amount=amount))
    return f"leaning {direction}"


async def sleep(d: ToolDispatcher, _a: dict[str, Any]) -> str:
    d._fire_and_forget(d._mini.goto_sleep)
    return "going to sleep"


async def wake_up(d: ToolDispatcher, _a: dict[str, Any]) -> str:
    d._fire_and_forget(d._mini.wake_up)
    return "awake"


async def set_head(d: ToolDispatcher, args: dict[str, Any]) -> str:
    kw = {k: float(args[k]) for k in ("yaw", "pitch", "roll") if k in args}
    duration = float(args.get("duration", 0.4))
    pose = create_head_pose(**kw)
    d._fire_and_forget(lambda: d._mini.goto_target(head=pose, duration=duration))
    return f"head → {kw}"


async def set_antennas(d: ToolDispatcher, args: dict[str, Any]) -> str:
    left = float(args["left"])
    right = float(args["right"])
    duration = float(args.get("duration", 0.3))
    d._fire_and_forget(
        lambda: d._mini.goto_target(antennas=[left, right], duration=duration)
    )
    return f"antennas → [{left:.2f}, {right:.2f}]"


async def set_body_yaw(d: ToolDispatcher, args: dict[str, Any]) -> str:
    angle = float(args["angle"])
    duration = float(args.get("duration", 0.4))
    d._fire_and_forget(
        lambda: d._mini.goto_target(body_yaw=angle, duration=duration)
    )
    return f"body → {angle:.0f}°"


async def look_at(d: ToolDispatcher, args: dict[str, Any]) -> str:
    u = float(args["u"]); v = float(args["v"])
    w, h = d._frame_size()
    px, py = int(u * w), int(v * h)
    d._fire_and_forget(lambda: d._mini.look_at_image(px, py))
    return f"looking at ({u:.2f},{v:.2f})"


async def perform_sequence(d: ToolDispatcher, args: dict[str, Any]) -> str:
    frames = args.get("frames", [])
    if not isinstance(frames, list) or not frames:
        return "no frames"

    def run():
        for f in frames:
            duration = float(f.get("duration", 0.3))
            kwargs: dict[str, Any] = {"duration": duration}
            head = f.get("head")
            if isinstance(head, dict):
                hk = {k: float(head[k]) for k in ("yaw", "pitch", "roll") if k in head}
                kwargs["head"] = create_head_pose(**hk)
            antennas = f.get("antennas")
            if isinstance(antennas, list) and len(antennas) == 2:
                kwargs["antennas"] = [float(antennas[0]), float(antennas[1])]
            if "body_yaw" in f:
                kwargs["body_yaw"] = float(f["body_yaw"])
            d._mini.goto_target(**kwargs)

    d._fire_and_forget(run)
    return f"performing {len(frames)} frames"


async def look(d: ToolDispatcher, _a: dict[str, Any]) -> str:
    # Capture a frame and feed it to the conversation as image.input. We do
    # NOT send response.create here — the model is already mid-response when
    # it called this tool, and a second response.create would race the active
    # one (Opper rejects with "conversation_already_has_active_response").
    # The image stays in context for the next turn.
    data_url = await d._run_blocking(d._eyes.snapshot)
    if not data_url:
        return "no camera frame yet"
    await d._send_event(
        {
            "type": "image.input",
            "image_url": data_url,
            "image_detail": "auto",
            "text": "Here is what I see right now.",
        }
    )
    return "ok, looking now (image attached for this turn)"


async def get_sound_direction(d: ToolDispatcher, _a: dict[str, Any]) -> str:
    # Prefer the buffered voice-positive reading; fall back to instantaneous.
    if d._doa_buffer is not None:
        angle = d._doa_buffer.latest_voice(max_age=3.0)
        if angle is not None:
            return f"angle={angle:.0f}° voice=True (buffered)"
    doa = d._mini.media.get_DoA()
    if doa is None:
        return "no recent sound"
    angle, is_voice = doa
    return f"angle={angle:.0f}° voice={bool(is_voice)}"


async def turn_toward_sound(d: ToolDispatcher, _a: dict[str, Any]) -> str:
    # Use the rolling buffer — DoA captured DURING the user's turn, not stale
    # silence by the time the model dispatches this tool.
    angle: Optional[float] = None
    if d._doa_buffer is not None:
        angle = d._doa_buffer.latest_voice(max_age=4.0)
    if angle is None:
        doa = d._mini.media.get_DoA()
        if doa is None:
            return "no recent sound"
        angle = float(doa[0])
    target = max(-70.0, min(70.0, float(angle)))
    # Two-phase orient: head leads (snappy glance), body catches up.
    body_target = target * 0.7
    head_target = target * 0.3

    def run():
        from reachy_mini.utils import create_head_pose
        # Phase 1 — head snaps toward sound while body just begins to turn.
        d._mini.goto_target(
            head=create_head_pose(yaw=target * 0.6),
            body_yaw=target * 0.2,
            duration=0.25,
        )
        # Phase 2 — body completes the turn; head settles to its share.
        d._mini.goto_target(
            head=create_head_pose(yaw=head_target),
            body_yaw=body_target,
            duration=0.45,
        )

    d._fire_and_forget(run)
    return f"turning to {target:.0f}°"


_HANDLERS: dict[str, Callable[[ToolDispatcher, dict[str, Any]], Awaitable[str]]] = {
    "nod": nod,
    "shake_head": shake_head,
    "wiggle_antennas": wiggle_antennas,
    "dance": dance,
    "look_confused": look_confused,
    "greet": greet,
    "wave": wave,
    "lean": lean,
    "look_around": look_around,
    "sleep": sleep,
    "wake_up": wake_up,
    "set_head": set_head,
    "set_antennas": set_antennas,
    "set_body_yaw": set_body_yaw,
    "look_at": look_at,
    "perform_sequence": perform_sequence,
    "look": look,
    "get_sound_direction": get_sound_direction,
    "turn_toward_sound": turn_toward_sound,
}
