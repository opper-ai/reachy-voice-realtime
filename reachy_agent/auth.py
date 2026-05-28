"""Opper OAuth device-flow login for the reachy agent.

Mirrors the Opper CLI's storage at ~/.opper/config.json so a single
`opper login` (or `python -m reachy_agent --opper-login`) covers both tools.

Key resolution order used by main.py:
  1. OPPER_API_KEY env / .env
  2. ~/.opper/config.json default slot
  3. device flow (only if --opper-login was passed)
"""

from __future__ import annotations

import json
import os
import sys
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx


CLIENT_ID = "opper_app_mJmK1Qhpc83yYu3j6IKMMQ"
DEFAULT_BASE_URL = "https://api.opper.ai"


def opper_home() -> Path:
    return Path(os.environ.get("OPPER_HOME") or (Path.home() / ".opper"))


def config_path() -> Path:
    return opper_home() / "config.json"


# --- config storage (compatible with the Opper CLI's JSON layout) ---

def read_config() -> Optional[dict[str, Any]]:
    p = config_path()
    try:
        return json.loads(p.read_text())
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        raise RuntimeError(f"malformed config at {p}: {e}") from e


def write_config(cfg: dict[str, Any]) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def get_default_slot() -> Optional[dict[str, Any]]:
    cfg = read_config()
    if not cfg:
        return None
    key = cfg.get("defaultKey") or "default"
    return (cfg.get("keys") or {}).get(key)


def set_slot(name: str, slot: dict[str, Any]) -> None:
    cfg = read_config() or {"version": 1, "defaultKey": "default", "keys": {}}
    keys = cfg.setdefault("keys", {})
    first = len(keys) == 0
    keys[name] = slot
    if first:
        cfg["defaultKey"] = name
    write_config(cfg)


# --- device flow ---

@dataclass
class DeviceAuth:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: Optional[str]
    expires_in: int
    interval: int


def _start_device_auth(base_url: str) -> DeviceAuth:
    resp = httpx.post(
        f"{base_url}/oauth/device",
        data={"client_id": CLIENT_ID},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"device authorization failed ({resp.status_code}): {resp.text}")
    d = resp.json()
    return DeviceAuth(
        device_code=d["device_code"],
        user_code=d["user_code"],
        verification_uri=d["verification_uri"],
        verification_uri_complete=d.get("verification_uri_complete"),
        expires_in=int(d.get("expires_in", 600)),
        interval=int(d.get("interval", 5)),
    )


def _poll_device_token(base_url: str, dev: DeviceAuth) -> dict[str, Any]:
    interval = dev.interval
    deadline = time.monotonic() + dev.expires_in
    with httpx.Client(timeout=30.0) as client:
        while time.monotonic() < deadline:
            time.sleep(interval)
            resp = client.post(
                f"{base_url}/oauth/device/token",
                data={"device_code": dev.device_code, "client_id": CLIENT_ID},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code < 400:
                return resp.json()
            err = {}
            try:
                err = resp.json()
            except json.JSONDecodeError:
                pass
            detail = err.get("detail") or ""
            if not detail and isinstance(err.get("errors"), list) and err["errors"]:
                detail = err["errors"][0].get("detail") or ""
            if detail == "authorization_pending":
                continue
            if detail == "slow_down":
                interval += 5
                continue
            if detail == "access_denied":
                raise RuntimeError("user denied access")
            raise RuntimeError(f"device authorization failed: {detail or resp.text}")
    raise RuntimeError("device authorization timed out")


def run_device_flow(base_url: Optional[str] = None) -> dict[str, Any]:
    """Run the OAuth device flow and return a slot dict (apiKey, user, ...)."""
    url = base_url or os.environ.get("OPPER_BASE_URL") or DEFAULT_BASE_URL
    dev = _start_device_auth(url)
    target = dev.verification_uri_complete or dev.verification_uri
    print("→ Sign in to Opper:")
    print(f"   open: {target}")
    print(f"   code: {dev.user_code}")
    print("   (opening browser…)")
    try:
        webbrowser.open(target)
    except Exception:
        pass
    print("→ waiting for browser approval…")
    result = _poll_device_token(url, dev)
    slot: dict[str, Any] = {
        "apiKey": result["api_key"],
        "obtainedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "device-flow",
    }
    if "user" in result and result["user"]:
        slot["user"] = result["user"]
    if base_url:
        slot["baseUrl"] = base_url
    return slot


# --- resolution chain used by main.py ---

def resolve_api_key(force_login: bool) -> str:
    """Return an Opper API key, running the device flow if needed.

    - If --opper-login was passed, always run the device flow and save the slot.
    - Otherwise: env var → config default slot → exit with a helpful message.
    """
    if force_login:
        slot = run_device_flow()
        set_slot("default", slot)
        who = (slot.get("user") or {}).get("email") or "default"
        print(f"✓ signed in as {who}; saved to {config_path()}")
        return slot["apiKey"]

    env_key = os.environ.get("OPPER_API_KEY")
    if env_key:
        return env_key

    slot = get_default_slot()
    if slot and slot.get("apiKey"):
        return slot["apiKey"]

    print(
        "error: no Opper credentials found.\n"
        "  set OPPER_API_KEY in env / .env, or run with --opper-login to sign in.",
        file=sys.stderr,
    )
    sys.exit(1)
