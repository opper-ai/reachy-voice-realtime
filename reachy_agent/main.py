"""Reachy voice-agent orchestrator.

Run with:
    python -m reachy_agent [--mac-mic] [--no-ui] [--ui-port 8080]
                           [--ambient-vision-seconds 10] [--no-ambient-vision]
                           [--auto-orient]

Reads OPPER_API_KEY from the environment (or a .env file alongside the working
directory). Requires `reachy-mini-daemon` to be running.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

# Ensure the parent dir (which has behaviors.py + the venv) is on sys.path so
# both `python -m reachy_agent` and `python reachy_agent/main.py` work.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reachy_mini import ReachyMini  # noqa: E402

from .audio import MicCapture, SpeakerPlayer  # noqa: E402
from .auth import resolve_api_key  # noqa: E402
from .orient import AutoOrient, DoaBuffer  # noqa: E402
from .prompt import SYSTEM_PROMPT  # noqa: E402
from .realtime import Realtime, mint_ticket  # noqa: E402
from .server import UIServer  # noqa: E402
from .tools import TOOL_SCHEMAS, ToolDispatcher  # noqa: E402
from .vision import Eyes  # noqa: E402


VOICES = ["alloy", "ash", "ballad", "coral", "echo", "sage",
          "shimmer", "verse", "marin", "cedar"]
REASONING_EFFORTS = ["minimal", "low", "medium", "high", "xhigh"]


def build_mint_config(voice: str, reasoning_effort: str,
                      vad_threshold: float, vad_silence_ms: int) -> dict[str, Any]:
    return {
        "model": "openai/gpt-realtime-2",
        "instructions": SYSTEM_PROMPT,
        "voice": voice,
        "modalities": ["audio"],
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm16",
        "input_transcription": True,
        "output_transcription": True,
        "turn_detection": {
            "type": "server_vad",
            "threshold": vad_threshold,
            "silence_duration_ms": vad_silence_ms,
        },
        "tools": TOOL_SCHEMAS,
        "reasoning_effort": reasoning_effort,
        "temperature": 0.7,
    }


class Agent:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.mini: Optional[ReachyMini] = None
        self.rt: Optional[Realtime] = None
        self.eyes: Optional[Eyes] = None
        self.mic: Optional[MicCapture] = None
        self.speaker: Optional[SpeakerPlayer] = None
        self.dispatcher: Optional[ToolDispatcher] = None
        self.ui: Optional[UIServer] = None
        self.doa_buffer: Optional[DoaBuffer] = None
        self.auto_orient: Optional[AutoOrient] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.mic_q: Optional[asyncio.Queue[bytes]] = None
        self._reachy_msg_id: Optional[str] = None
        self._reachy_msg_buf: str = ""
        # True while we're actively decoding/playing model audio. Auto-orient
        # checks this so it doesn't fight the model's own motion.
        self._reachy_speaking: bool = False

    # -- event publishing helpers --

    def _publish(self, ev: dict[str, Any]) -> None:
        if self.ui is not None:
            self.ui.publish(ev)

    # -- main lifecycle --

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.mic_q = asyncio.Queue(maxsize=200)

        load_dotenv()  # picks up .env in CWD if present
        api_key = resolve_api_key(force_login=self.args.opper_login)

        print(f"→ connecting to Reachy daemon on :{self.args.daemon_port}…")
        self.mini = ReachyMini(port=self.args.daemon_port)
        self.mini.__enter__()
        try:
            self.eyes = Eyes(self.mini)

            print(
                f"→ minting realtime ticket "
                f"(voice={self.args.voice}, effort={self.args.reasoning_effort}, "
                f"vad threshold={self.args.vad_threshold} "
                f"silence={self.args.vad_silence_ms}ms)…"
            )
            ticket = await mint_ticket(
                build_mint_config(
                    self.args.voice,
                    self.args.reasoning_effort,
                    self.args.vad_threshold,
                    self.args.vad_silence_ms,
                ),
                ttl_seconds=60,
                api_key=api_key,
            )
            print(f"  expires {ticket.get('expires_at')}")

            print("→ opening WebSocket…")
            self.rt = await Realtime.connect(ticket)
            print("→ session.start sent")

            # Graceful shutdown — set when SIGINT/SIGTERM hits, or when the
            # UI POSTs /api/shutdown via the Disconnect button.
            stop = asyncio.Event()

            # Optional UI sidecar.
            ui_task = None
            if not self.args.no_ui:
                loop = self.loop
                assert loop is not None
                self.ui = UIServer(
                    self.eyes,
                    on_shutdown=lambda: loop.call_soon_threadsafe(stop.set),
                )
                ui_task = asyncio.create_task(self._serve_ui())
                print(f"→ UI at http://localhost:{self.args.ui_port}")

            # Audio + tools.
            self.speaker = SpeakerPlayer()
            self.speaker.start()

            # DoA buffer is always on (cheap, ~4 Hz) — feeds both
            # turn_toward_sound and AutoOrient.
            self.doa_buffer = DoaBuffer(self.mini)
            self.doa_buffer.start()

            self.dispatcher = ToolDispatcher(
                self.mini, self.eyes, self.rt.send, self.loop,
                doa_buffer=self.doa_buffer,
            )

            if self.args.auto_orient:
                self.auto_orient = AutoOrient(
                    self.mini, self.doa_buffer,
                    is_speaking=lambda: self._reachy_speaking,
                )
                self.auto_orient.start(self.loop)
                print("→ auto-orient: on")

            def on_mic_chunk(b: bytes) -> None:
                # Called from the mic thread; bridge to the asyncio queue.
                try:
                    self.loop.call_soon_threadsafe(self.mic_q.put_nowait, b)
                except Exception:
                    pass

            mic_source = "mac" if self.args.mac_mic else "reachy"
            self.mic = MicCapture(self.mini, mic_source, on_mic_chunk)
            self.mic.start()
            print(f"→ mic: {mic_source}; speaker: reachy")

            self._publish({"type": "state", "value": "idle"})

            tasks = [
                asyncio.create_task(self._event_loop(), name="events"),
                asyncio.create_task(self._mic_sender(), name="mic"),
            ]
            if not self.args.no_ambient_vision:
                tasks.append(asyncio.create_task(self._ambient_vision(), name="vision"))
            if ui_task is not None:
                tasks.append(ui_task)

            # Hook Ctrl-C into the same `stop` event the UI uses.
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    self.loop.add_signal_handler(sig, stop.set)
                except NotImplementedError:
                    pass
            stop_task = asyncio.create_task(stop.wait(), name="stop")

            done, pending = await asyncio.wait(
                tasks + [stop_task], return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for t in done:
                if t is not stop_task and not t.cancelled() and t.exception():
                    raise t.exception()  # type: ignore[misc]

        finally:
            await self._shutdown()

    async def _serve_ui(self) -> None:
        import uvicorn

        assert self.ui is not None
        config = uvicorn.Config(
            self.ui.app,
            host="127.0.0.1",
            port=self.args.ui_port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        await server.serve()

    # -- audio out: mic → WS --

    async def _mic_sender(self) -> None:
        assert self.rt is not None
        while True:
            chunk = await self.mic_q.get()
            b64 = base64.b64encode(chunk).decode("ascii")
            await self.rt.send({"type": "audio.append", "audio": b64})

    # -- vision: ambient frame every N seconds, no nudge --

    async def _ambient_vision(self) -> None:
        assert self.rt is not None and self.eyes is not None
        interval = max(1.0, float(self.args.ambient_vision_seconds))
        # Give the camera a moment to warm up.
        await asyncio.sleep(2.0)
        while True:
            try:
                data_url = await self.loop.run_in_executor(None, self.eyes.snapshot)
                if data_url:
                    await self.rt.send(
                        {
                            "type": "image.input",
                            "image_url": data_url,
                            "image_detail": "auto",
                        }
                    )
            except Exception as e:
                print(f"  (ambient vision error: {e})")
            await asyncio.sleep(interval)

    # -- main event router --

    async def _event_loop(self) -> None:
        assert self.rt is not None
        async for ev in self.rt.events():
            t = ev.get("type")
            if t == "session.started":
                rates = (ev.get("input_sample_rate"), ev.get("output_sample_rate"))
                print(f"  session.started rates={rates}")

            elif t == "audio.delta":
                if self.speaker is not None and ev.get("audio"):
                    self.speaker.push(ev["audio"])
                self._reachy_speaking = True
                self._publish({"type": "state", "value": "speaking"})

            elif t == "speech.started":
                if self.speaker is not None:
                    self.speaker.stop_playback()
                self._reachy_speaking = False
                # End any in-flight reachy bubble — user's turn now.
                self._reachy_msg_id = None
                self._reachy_msg_buf = ""
                self._publish({"type": "state", "value": "listening"})

            elif t == "speech.stopped":
                self._publish({"type": "state", "value": "idle"})

            elif t == "transcript.committed":
                text = ev.get("transcript") or ""
                if text.strip():
                    self._publish(
                        {
                            "type": "transcript",
                            "who": "user",
                            "id": uuid.uuid4().hex[:8],
                            "text": text,
                        }
                    )

            elif t == "text.delta":
                delta = ev.get("delta") or ""
                if not delta:
                    continue
                if self._reachy_msg_id is None:
                    self._reachy_msg_id = uuid.uuid4().hex[:8]
                    self._reachy_msg_buf = ""
                self._reachy_msg_buf += delta
                self._publish(
                    {
                        "type": "transcript",
                        "who": "reachy",
                        "id": self._reachy_msg_id,
                        "text": self._reachy_msg_buf,
                    }
                )

            elif t == "response.completed" or t == "response.done":
                self._reachy_speaking = False
                self._reachy_msg_id = None
                self._reachy_msg_buf = ""
                self._publish({"type": "state", "value": "idle"})

            elif t == "tool.call":
                await self._handle_tool_call(ev)

            elif t == "error":
                print(f"  WS error: {ev}")

    async def _handle_tool_call(self, ev: dict[str, Any]) -> None:
        assert self.dispatcher is not None and self.rt is not None
        name = ev.get("tool_name") or ""
        raw = ev.get("tool_arguments")
        if isinstance(raw, str):
            try:
                import json
                args = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                args = {}
        else:
            args = raw or {}
        call_id = ev.get("tool_call_id") or ""

        t0 = time.perf_counter()
        result = await self.dispatcher.dispatch(name, args)
        dt = (time.perf_counter() - t0) * 1000
        print(f"  tool {name}({args}) → {result}  [{dt:.0f} ms]")
        self._publish(
            {"type": "tool", "name": name, "args": args, "result": result}
        )
        await self.rt.send(
            {
                "type": "tool.result",
                "tool_call_id": call_id,
                "tool_result": {"result": result},
            }
        )

    async def _shutdown(self) -> None:
        print("\n→ shutting down…")
        if self.auto_orient is not None:
            with contextlib.suppress(Exception):
                await self.auto_orient.stop()
        if self.doa_buffer is not None:
            with contextlib.suppress(Exception):
                self.doa_buffer.stop()
        if self.mic is not None:
            with contextlib.suppress(Exception):
                self.mic.stop()
        if self.speaker is not None:
            with contextlib.suppress(Exception):
                self.speaker.close()
        if self.rt is not None:
            with contextlib.suppress(Exception):
                await self.rt.close()
        if self.mini is not None:
            with contextlib.suppress(Exception):
                self.mini.__exit__(None, None, None)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="reachy_agent")
    p.add_argument(
        "--opper-login", action="store_true",
        help="sign in to Opper via OAuth device flow (no API key needed). "
             "Stored in ~/.opper/config.json (shared with the Opper CLI).",
    )
    p.add_argument("--mac-mic", action="store_true", help="use Mac mic instead of Reachy's")
    p.add_argument("--no-ui", action="store_true", help="skip the web UI sidecar")
    p.add_argument("--ui-port", type=int, default=1080)
    p.add_argument("--daemon-port", type=int, default=1111, help="reachy-mini-daemon port")
    p.add_argument("--ambient-vision-seconds", type=float, default=10.0)
    p.add_argument("--no-ambient-vision", action="store_true", help="disable periodic camera frames")
    p.add_argument("--auto-orient", action="store_true", help="ambient body-yaw toward DoA (off by default)")
    p.add_argument(
        "--voice", choices=VOICES, default="marin",
        help=f"realtime voice (default: marin). Options: {', '.join(VOICES)}",
    )
    p.add_argument(
        "--reasoning-effort", choices=REASONING_EFFORTS, default="low",
        help="model reasoning effort. minimal=fastest, low=snappy (default), "
             "medium=better tool sequencing, high=thoughtful, xhigh=slowest",
    )
    p.add_argument(
        "--vad-threshold", type=float, default=0.6,
        help="server VAD sensitivity 0.0..1.0 (higher = less sensitive, fewer interruptions)",
    )
    p.add_argument(
        "--vad-silence-ms", type=int, default=1200,
        help="ms of silence before considering the user done speaking",
    )
    return p.parse_args()


def main() -> None:
    # Line-buffered stdout so users see progress immediately.
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:
        pass
    args = parse_args()
    asyncio.run(Agent(args).run())


if __name__ == "__main__":
    main()
