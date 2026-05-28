"""Reachy Mini behaviors. Run a single behavior via:

    python behaviors.py <name>          # e.g. python behaviors.py yes

Or import and call from your own code:

    from behaviors import greet, yes, no, excitement
    with ReachyMini() as mini:
        greet(mini)
"""

import subprocess
import sys
import threading
import time
from pathlib import Path

import reachy_mini
from reachy_mini import ReachyMini
from reachy_mini.utils import create_head_pose

HI_WAV = Path("/tmp/reachy_hi.wav")
SDK_ASSETS = Path(reachy_mini.__file__).parent / "assets"
DANCE_WAV = SDK_ASSETS / "dance1.wav"
CONFUSED_WAV = SDK_ASSETS / "confused1.wav"


def _ensure_hi_wav() -> None:
    if not HI_WAV.exists():
        subprocess.run(
            ["say", "-o", str(HI_WAV), "--file-format=WAVE",
             "--data-format=LEI16@16000", "Hi!"],
            check=True,
        )


def greet(mini: ReachyMini) -> None:
    _ensure_hi_wav()
    threading.Thread(
        target=mini.media.play_sound, args=(str(HI_WAV),), daemon=True
    ).start()

    mini.goto_target(head=create_head_pose(yaw=25), duration=0.35)
    mini.goto_target(head=create_head_pose(yaw=-25), duration=0.5)
    mini.goto_target(head=create_head_pose(yaw=25), duration=0.5)
    mini.goto_target(head=create_head_pose(yaw=0), duration=0.35)

    mini.goto_target(antennas=[0.6, -0.6], duration=0.3)
    mini.goto_target(antennas=[-0.6, 0.6], duration=0.3)
    mini.goto_target(antennas=[0.0, 0.0], duration=0.3)


def yes(mini: ReachyMini) -> None:
    for _ in range(3):
        mini.goto_target(head=create_head_pose(pitch=-25), duration=0.18)
        mini.goto_target(head=create_head_pose(pitch=15), duration=0.18)
    mini.goto_target(head=create_head_pose(pitch=0), duration=0.2)


def no(mini: ReachyMini) -> None:
    mini.goto_target(head=create_head_pose(yaw=-25), duration=0.22)
    mini.goto_target(head=create_head_pose(yaw=25), duration=0.28)
    mini.goto_target(head=create_head_pose(yaw=-25), duration=0.28)
    mini.goto_target(head=create_head_pose(yaw=25), duration=0.28)
    mini.goto_target(head=create_head_pose(yaw=0), duration=0.22)


def excitement(mini: ReachyMini) -> None:
    for a in (0.8, -0.8, 0.8, -0.8, 0.8, -0.8):
        mini.goto_target(antennas=[a, -a], duration=0.15)
    mini.goto_target(antennas=[0.0, 0.0], duration=0.2)


def questionable(mini: ReachyMini) -> None:
    threading.Thread(
        target=mini.media.play_sound, args=(str(CONFUSED_WAV),), daemon=True
    ).start()
    mini.goto_target(head=create_head_pose(roll=25, pitch=-5), duration=0.5)
    time.sleep(0.6)
    mini.goto_target(head=create_head_pose(roll=0, pitch=0), duration=0.4)


def wave(mini: ReachyMini) -> None:
    for left, right, body_yaw in (
        (0.9, -0.2, 10),
        (-0.2, 0.9, -10),
        (0.9, -0.2, 10),
        (-0.2, 0.9, -10),
    ):
        mini.goto_target(antennas=[left, right], body_yaw=body_yaw, duration=0.22)
    mini.goto_target(antennas=[0.0, 0.0], body_yaw=0.0, duration=0.25)


_LEAN_HEAD: dict[str, dict[str, float]] = {
    "left": {"roll": 20.0},
    "right": {"roll": -20.0},
    "forward": {"pitch": -25.0},
    "back": {"pitch": 25.0},
}
_LEAN_BODY_YAW: dict[str, float] = {
    "left": -15.0, "right": 15.0, "forward": 0.0, "back": 0.0,
}


def lean(mini: ReachyMini, direction: str = "left", amount: float = 1.0) -> None:
    if direction not in _LEAN_HEAD:
        direction = "left"
    scale = max(0.1, min(1.0, float(amount)))
    head_kw = {k: v * scale for k, v in _LEAN_HEAD[direction].items()}
    body_yaw = _LEAN_BODY_YAW[direction] * scale
    mini.goto_target(
        head=create_head_pose(**head_kw), body_yaw=body_yaw, duration=0.5
    )
    time.sleep(0.5)
    mini.goto_target(head=create_head_pose(), body_yaw=0.0, duration=0.5)


def look_around(mini: ReachyMini) -> None:
    for yaw in (-45, 0, 45, 0):
        mini.goto_target(head=create_head_pose(yaw=yaw), duration=0.55)


def dance(mini: ReachyMini) -> None:
    threading.Thread(
        target=mini.media.play_sound, args=(str(DANCE_WAV),), daemon=True
    ).start()

    for body_yaw in (30, -30, 30, -30):
        mini.goto_target(
            head=create_head_pose(pitch=-10),
            antennas=[0.7, -0.7],
            body_yaw=body_yaw,
            duration=0.35,
        )
        mini.goto_target(
            head=create_head_pose(pitch=10),
            antennas=[-0.7, 0.7],
            body_yaw=-body_yaw,
            duration=0.35,
        )

    for roll in (20, -20, 20, -20):
        mini.goto_target(
            head=create_head_pose(roll=roll, pitch=5),
            antennas=[0.5 if roll > 0 else -0.5, -0.5 if roll > 0 else 0.5],
            duration=0.25,
        )

    mini.goto_target(
        head=create_head_pose(),
        antennas=[0.0, 0.0],
        body_yaw=0.0,
        duration=0.4,
    )


BEHAVIORS = {
    "greet": greet,
    "yes": yes,
    "no": no,
    "excitement": excitement,
    "questionable": questionable,
    "dance": dance,
    "wave": wave,
    "lean": lean,
    "look_around": look_around,
}


if __name__ == "__main__":
    import os
    if len(sys.argv) != 2 or sys.argv[1] not in BEHAVIORS:
        print(f"usage: python behaviors.py <{'|'.join(BEHAVIORS)}>")
        print(f"       REACHY_PORT={os.environ.get('REACHY_PORT', '1111')} (override with env var)")
        sys.exit(1)
    port = int(os.environ.get("REACHY_PORT", "1111"))
    with ReachyMini(port=port) as mini:
        BEHAVIORS[sys.argv[1]](mini)
