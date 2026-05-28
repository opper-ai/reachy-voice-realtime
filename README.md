# Reachy Mini playground

Code for driving a [Reachy Mini Lite](https://huggingface.co/reachy-mini) (USB),
a small expressive desktop robot. Two interfaces live here:

- **`behaviors.py`**: a handful of canned emote behaviors with a one-shot CLI.
- **`reachy_agent/`**: a real-time voice agent. The model becomes Reachy, hears
  through Reachy's mic, sees through its camera, talks through its speaker, and
  calls motion tools to physically react.

The voice agent runs on **[OpenAI's realtime API (`gpt-realtime-2`)](https://platform.openai.com/docs/guides/realtime)**,
proxied through **[Opper](https://opper.ai)**. Opper handles auth (OAuth device
flow, no client-side OpenAI key needed), mints short-lived WebSocket tickets,
and you only need an Opper credential (`OPPER_API_KEY` or `--opper-login`).

## Quickstart

```bash
# One-time install (needs Python 3.12+)
python3.12 -m venv reachy_mini_env
source reachy_mini_env/bin/activate
pip install reachy-mini websockets httpx fastapi 'uvicorn[standard]' sounddevice Pillow python-dotenv
```

Plug Reachy in over USB, then open two terminals (activate the venv in each
with `source reachy_mini_env/bin/activate`):

```bash
# Terminal 1: daemon (owns the USB connection to Reachy)
reachy-mini-daemon --fastapi-port 1111

# Terminal 2: voice agent (first run only needs --opper-login)
python -m reachy_agent --opper-login
```

Open <http://localhost:1080> and talk to Reachy. Details below.

## Layout

```
.
├── behaviors.py            Standalone emotes: greet, yes, no, excitement, …
├── reachy_agent/           Voice-agent package (python -m reachy_agent)
│   ├── main.py             Orchestrator + CLI
│   ├── realtime.py         Opper realtime WebSocket client
│   ├── audio.py            Mic capture + speaker output (direct USB audio)
│   ├── vision.py           Camera → image.input
│   ├── tools.py            19 motion/perception tools the model can call
│   ├── prompt.py           "You are Reachy" system prompt
│   ├── server.py           Tiny FastAPI sidecar (SSE + frame stream)
│   └── static/index.html   Observability UI
└── reachy_mini_env/        Python 3.12 venv (not checked in)
```

## Run the daemon

The `reachy-mini-daemon` owns the USB connection to the robot. It must be
running before the SDK or the agent can do anything. Use port 1111 to keep
out of the way of other local services.

### Start (foreground)

```bash
reachy-mini-daemon --fastapi-port 1111
```

Leave it in its own terminal. The API is at `http://localhost:1111/docs`.
**Ctrl-C** stops it.

### Start (background)

```bash
nohup reachy-mini-daemon --fastapi-port 1111 > /tmp/reachy-daemon.log 2>&1 &
disown
```

### Check status

```bash
curl -s http://localhost:1111/api/daemon/status | jq .   # robot state + control loop stats
lsof -nP -iTCP:1111 -sTCP:LISTEN                         # who's holding the port (PID)
ps -ax | grep reachy-mini-daemon                         # process list
tail -f /tmp/reachy-daemon.log                           # live log
```

### Pause / resume the robot (motors only, daemon stays up)

```bash
curl -X POST http://localhost:1111/api/daemon/stop       # disengage motors, keep API alive
curl -X POST http://localhost:1111/api/daemon/start      # re-engage
curl -X POST http://localhost:1111/api/daemon/restart    # both, atomically
```

Use this when you want to manually pose the robot or just calm it down without
killing the process.

### Stop the process entirely

```bash
# Find the PID by port and kill it cleanly.
kill "$(lsof -nP -iTCP:1111 -sTCP:LISTEN -t)"

# Or by name.
pkill -f reachy-mini-daemon
```

> **Heads up:** only one client can hold the USB serial port. If the Reachy
> Mini Control desktop app is open, it will fight the daemon. Quit one or the
> other.

## `behaviors.py`: quick emotes

Single CLI, one behavior per invocation:

```bash
python behaviors.py greet         # head sweep + antenna wiggle + "Hi!"
python behaviors.py yes           # 3 quick nods
python behaviors.py no            # head shake
python behaviors.py excitement    # rapid antenna wiggle
python behaviors.py questionable  # head tilt + confused sound
python behaviors.py dance         # body sway + head bob + antennas + music
python behaviors.py wave          # antennas wag + small body sway
python behaviors.py lean          # head/body lean (defaults to left)
python behaviors.py look_around   # head sweep -45° → 45° → 0°
```

Both default to the daemon on `localhost:1111`. Override with `REACHY_PORT`:

```bash
REACHY_PORT=8000 python behaviors.py dance
```

Or import and call directly:

```python
from behaviors import greet, dance
from reachy_mini import ReachyMini

with ReachyMini(port=1111) as mini:
    greet(mini)
    dance(mini)
```

## `reachy_agent`: voice agent

The model hears via Reachy's mic, sees via Reachy's camera, replies through
Reachy's speaker, and calls motion tools to move while it talks.

```bash
export OPPER_API_KEY=op-...     # one option
python -m reachy_agent

# or, no key at all — sign in via OAuth device flow (one-time):
python -m reachy_agent --opper-login
```

Then open <http://localhost:1080> and talk to Reachy.

### Auth

Two ways to authenticate, in priority order:

1. **`OPPER_API_KEY`**: env var, or in a `.env` next to where you run from.
   Wins over everything else; right for CI, scripts, or pinning a project to a
   specific key.
2. **`~/.opper/config.json`**: shared with the [Opper CLI](https://github.com/opper-ai/cli).
   If you've already run `opper login`, reachy uses that credential. Otherwise
   `python -m reachy_agent --opper-login` runs the device flow itself
   (opens your browser, you confirm a short code, the key gets stored).

No client-side secret is involved. The device flow exchanges a user
confirmation for an API key the server mints for you.

### Common flags

```bash
python -m reachy_agent --opper-login                 # sign in via OAuth (no API key)
python -m reachy_agent --voice cedar                 # 10 voices available (see below)
python -m reachy_agent --reasoning-effort medium     # better tool sequencing
python -m reachy_agent --reasoning-effort low        # snappier, simpler choices
python -m reachy_agent --reasoning-effort minimal    # absolute fastest, less tool use
python -m reachy_agent --vad-silence-ms 1800         # be more patient before responding
python -m reachy_agent --vad-threshold 0.7           # less sensitive to ambient noise
python -m reachy_agent --auto-orient                 # Reachy physically faces whoever speaks
python -m reachy_agent --mac-mic                     # debug: use Mac mic instead of Reachy
python -m reachy_agent --no-ambient-vision           # disable the 10s periodic frame
python -m reachy_agent --ambient-vision-seconds 30   # slower periodic frame
python -m reachy_agent --no-ui                       # skip the FastAPI sidecar
python -m reachy_agent --ui-port 8080                # different UI port
python -m reachy_agent --daemon-port 1111            # if you change the daemon port
```

Stop with **Ctrl-C** in the terminal, or click **Disconnect** in the UI.

### What the model can do

19 tools, dispatched mid-conversation:

| Category | Tools |
|---|---|
| Emotes (canned) | `greet`, `nod`, `shake_head`, `wiggle_antennas`, `dance`, `look_confused`, `wave`, `lean`, `look_around`, `sleep`, `wake_up` |
| Motor primitives | `set_head`, `set_antennas`, `set_body_yaw`, `look_at` (image-space) |
| Composing | `perform_sequence`: list of keyframes for ad-hoc gestures (used to mirror your wave / nod / lean) |
| Perception | `look` (fresh camera frame as image.input), `get_sound_direction` (DoA), `turn_toward_sound` (orient body) |

The prompt tells the model to mimic the human: see you wave → wave back; see
you nod → nod back; see you tilt your head → tilt its head; hear sound off to
the side → turn toward it.

### Voices

`gpt-realtime-2` ships with 10 voices. Pick with `--voice <name>`. Default is
`marin`. All are English-native, OpenAI's standard realtime cast:

| Voice | Feel |
|---|---|
| `alloy` | Neutral, balanced |
| `ash` | Warm, conversational |
| `ballad` | Softer, slower, narrative |
| `coral` | Bright, friendly |
| `echo` | Calm, deeper |
| `sage` | Measured, soft-spoken |
| `shimmer` | Bright, warm |
| `verse` | Expressive, narrative |
| `marin` | Friendly, expressive (default) |
| `cedar` | Deeper, warm |

### Reasoning effort

`--reasoning-effort {minimal,low,medium,high,xhigh}`. `low` is the default and
right for most chat. Bump to `medium` when you want better tool sequencing
(e.g. "look first, then react" chains). `minimal` is snappiest but uses tools
less. `xhigh` is slow; use it only for puzzles you've explicitly told it to
think through.

### Sound localisation

A background thread polls Reachy's direction-of-arrival mic array (`get_DoA`)
4× a second and keeps the most recent **voice-positive** angle in a buffer.

- The `turn_toward_sound` tool reads from the buffer (DoA captured *while* you
  were talking, not the silent moment after).
- `--auto-orient` runs an ambient loop that smoothly nudges body + head
  together toward the buffered direction whenever Reachy isn't speaking, so
  the robot physically faces whoever just spoke without the model having to
  decide to. Head leads, body follows, looks like a real turn.

### UI

`http://localhost:1080`:

- Live camera frame (1 Hz refresh)
- Scrolling transcript (you + Reachy)
- Tool-call log (name + args + result)
- State badge (idle / listening / speaking / disconnecting)
- Disconnect button (triggers a graceful shutdown, same as Ctrl-C)

## Troubleshooting

**`command not found: reachy-mini-daemon`** or **`No module named 'reachy_mini'`**:
venv isn't active in this shell. Run `source reachy_mini_env/bin/activate`
(the prompt will show `(reachy_mini_env)`). Or invoke the binary by absolute
path: `./reachy_mini_env/bin/reachy-mini-daemon --fastapi-port 1111`.

**`PermissionError [Errno 13]` on a port**: macOS reserves ports below 1024.
Pick a higher one with `--ui-port`.

**Audio comes from MacBook speakers, not Reachy**: should not happen anymore;
the agent writes directly to the `Reachy Mini Audio` USB device via
`sounddevice`. If it returns, check that the device shows up in
`python -c "import sounddevice; print(sounddevice.query_devices())"`.

**Daemon dies after a while / port already in use**: only one daemon can hold
the USB serial port. Find the holder with
`lsof -nP -iTCP:1111 -sTCP:LISTEN`, kill it, restart.

**`conversation_already_has_active_response`**: racing the model. The `look`
tool no longer triggers a second response.create; if you add new tools that
attach data to the conversation, do *not* send `response.create` from within a
tool that runs mid-turn.

**Pyright/IDE complains about missing imports**: your editor isn't using the
venv interpreter. Point it at `reachy_mini_env/bin/python`. The code runs fine.

## License

MIT

## Author

Jose Sabater
