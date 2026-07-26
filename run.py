"""
run.py — GRUNTMASTER 6000

First run / reconfigure:
    python run.py           → setup wizard, then Locust web UI
    python run.py --setup   → force wizard even if .env is already configured

Diagnostics:
    python run.py --auth-probe            → resolve target agent + test SSO/token-exchange, print verdict
    python run.py --auth-probe "question" → same, using a custom probe question

Headless (pre-authenticate all profiles first):
    locust -f run.py --headless -u 10 -r 1
"""

# gevent monkey-patch must happen before any I/O library imports
import gevent
import gevent.monkey
import threading as _threading_pre_patch   # save real Thread before gevent patches it
_RealThread = _threading_pre_patch.Thread
gevent.monkey.patch_all()

import base64
import collections
import csv
import hashlib
import itertools
import json
import logging
import math
import os
import random
import re
import shutil
import subprocess
import sys
import queue as _queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import colorama; colorama.init()
import msal
import requests
import websocket
from cryptography.fernet import Fernet
from flask import jsonify, request
from locust import User, events, task
from locust.env import Environment
from locust.exception import StopUser
from rich import box as rich_box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

log     = logging.getLogger(__name__)
console = Console()

# ── Paths ─────────────────────────────────────────────────────────────────────

_HERE          = Path(__file__).parent
PROFILES_JSON  = _HERE / "profiles" / "profiles.json"
TOKENS_DIR     = _HERE / "profiles" / ".tokens"
UTTERANCES_DIR = _HERE / "utterances"
REPORT_DIR     = _HERE / "report"
TMP_DIR        = REPORT_DIR / "tmp"
TOKENS_DIR.mkdir(parents=True, exist_ok=True)
REPORT_DIR.mkdir(exist_ok=True)
TMP_DIR.mkdir(exist_ok=True)

# ── Windows Credential Manager helpers ───────────────────────────────────────

_KR_SERVICE = "copilot-load-test"


def _load_credential(key: str) -> str:
    try:
        import keyring
        val = keyring.get_password(_KR_SERVICE, key)
        if val:
            return val
    except Exception:
        pass
    return os.getenv(key, "")


def _save_credential(key: str, value: str):
    import keyring
    keyring.set_password(_KR_SERVICE, key, value)
    os.environ[key] = value


# ── Shared config dict (read by Locust User classes) ─────────────────────────

class _TestConfig(dict):
    _BOUNDS: dict = {
        "response_timeout": (15.0, 300.0),
        "think_min":        (0, 3600),
        "think_max":        (0, 3600),
        "p95_target_ms":    (100, 60_000),
        "max_error_rate":   (0.0, 1.0),
        "users":            (1, 10_000),
        "spawn_rate":       (1, 1_000),
    }
    def __setitem__(self, key: str, value) -> None:
        if key in self._BOUNDS:
            lo, hi = self._BOUNDS[key]
            value = type(lo)(value)
            if not (lo <= value <= hi):
                raise ValueError(
                    f"test_config['{key}'] = {value!r} out of range [{lo}, {hi}]"
                )
        super().__setitem__(key, value)

test_config = _TestConfig({
    "response_timeout": float(os.environ.get("GRUNTMASTER_RESPONSE_TIMEOUT", 30.0)),
    "think_min":        30,
    "think_max":        60,
    "p95_target_ms":    2000,
    "max_error_rate":   0.5,
    "users":            10,
    "spawn_rate":       5,
    "run_time_mins":    5,
    "transport":        os.environ.get("GRUNTMASTER_TRANSPORT", "websocket").lower(),
})

from requests.adapters import HTTPAdapter

_session = requests.Session()

def _init_session(user_count: int):
    """Resize connection pool to match test scale. Called once before test starts."""
    adapter = HTTPAdapter(
        pool_connections=1,
        pool_maxsize=user_count + 50,
        pool_block=False,
    )
    _session.mount("https://", adapter)
    _session.mount("http://", adapter)

class _RunState:
    __slots__ = ("dashboard", "circuit_open_until", "cpu_warn_ts", "spawning_complete", "phase", "mode",
                 "pipeline_spawns_total", "pipeline_done")
    def __init__(self) -> None:
        self.dashboard: "Optional[_DashboardState]" = None
        self.circuit_open_until: float = 0.0
        self.cpu_warn_ts: float = 0.0
        self.spawning_complete: bool = False
        self.phase: str = "RAMP_UP"   # Concurrent: "RAMP_UP"|"AT_PEAK"|"RAMP_DOWN"  Pipeline: "RUNNING"
        self.mode: str = "Concurrent"  # "Concurrent" | "Pipeline"
        self.pipeline_spawns_total: int = 0
        self.pipeline_done: int = 0

_run_state = _RunState()
_spawn_counters: dict[str, int] = {}   # class_name → spawn sequence number
_spawn_lock     = threading.Lock()

# ── Credentials (read from Windows Credential Manager, fallback to env vars) ──

TENANT_ID           = _load_credential("CS_TENANT_ID")
CLIENT_ID           = _load_credential("CS_CLIENT_ID")
AGENT_APP_ID        = _load_credential("CS_AGENT_APP_ID")
ENC_PASSWORD        = _load_credential("TOKEN_ENCRYPTION_PASSWORD")
DL_SECRET           = _load_credential("CS_DIRECTLINE_SECRET")
TOKEN_ENDPOINT      = _load_credential("CS_TOKEN_ENDPOINT")
ENDPOINT_NEEDS_AUTH = _load_credential("CS_TOKEN_ENDPOINT_REQUIRES_AUTH").lower() == "true"
DIRECTLINE_BASE = "https://directline.botframework.com"

_SILENCE_TIMEOUT    = 15.0    # seconds of silence after last bot reply before declaring response complete
_DIRECTLINE_RPS_CAP = 133.0   # 8000 RPM hard ceiling — knee only meaningful above 75% of this

_GUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _scopes() -> list[str]:
    if AGENT_APP_ID:
        return [f"api://{AGENT_APP_ID}/access_as_user"]
    return ["https://api.powerplatform.com/.default"]


# ── Encryption + token store ──────────────────────────────────────────────────

def _get_encryption_key() -> bytes:
    try:
        import keyring
        key = keyring.get_password("copilot-load-test", "fernet-key")
        if not key:
            raw = Fernet.generate_key()
            keyring.set_password("copilot-load-test", "fernet-key", raw.decode())
            return raw
        return key.encode()
    except Exception:
        if not ENC_PASSWORD:
            print(
                "\n[ERROR] keyring unavailable and TOKEN_ENCRYPTION_PASSWORD is not set.\n"
                "Run: python run.py --setup\n"
            )
            sys.exit(1)
        dk = hashlib.pbkdf2_hmac(
            "sha256", ENC_PASSWORD.encode(), b"copilot-load-test-salt",
            iterations=260000, dklen=32,
        )
        return base64.urlsafe_b64encode(dk)


def _fernet() -> Fernet:
    return Fernet(_get_encryption_key())


def _token_path(username: str) -> Path:
    safe = username.replace("@", "_").replace(".", "_")
    return TOKENS_DIR / f"{safe}.enc"


def save_token(username: str, token_data: dict):
    payload = json.dumps(token_data).encode()
    _token_path(username).write_bytes(_fernet().encrypt(payload))


def load_token(username: str) -> dict | None:
    path = _token_path(username)
    if not path.exists():
        return None
    try:
        return json.loads(_fernet().decrypt(path.read_bytes()))
    except Exception:
        return None


def is_token_valid(token_data: dict, min_ttl_seconds: int = 600) -> bool:
    exp = token_data.get("expires_on")
    if not exp:
        return False
    return (datetime.fromtimestamp(exp, tz=timezone.utc) - datetime.now(tz=timezone.utc)).total_seconds() > min_ttl_seconds


# ── MSAL auth ─────────────────────────────────────────────────────────────────

def get_valid_token(username: str) -> str:
    token_data = load_token(username)
    if token_data and is_token_valid(token_data):
        return token_data["access_token"]

    if token_data and token_data.get("refresh_token"):
        app = msal.PublicClientApplication(
            CLIENT_ID, authority=f"https://login.microsoftonline.com/{TENANT_ID}"
        )
        result = app.acquire_token_by_refresh_token(token_data["refresh_token"], scopes=_scopes())
        if "access_token" in result:
            token_data.update({
                "access_token":  result["access_token"],
                "expires_on":    int(result.get("expires_on") or (time.time() + result.get("expires_in", 3600))),
                "refresh_token": result.get("refresh_token", token_data.get("refresh_token")),
            })
            save_token(username, token_data)
            return result["access_token"]

    raise RuntimeError(f"No valid token for {username}. Run: python run.py --setup")


def authenticate_profile(username: str) -> bool:
    print(f"\n{'='*60}\n  Authenticating: {username}\n{'='*60}")
    app = msal.PublicClientApplication(
        CLIENT_ID, authority=f"https://login.microsoftonline.com/{TENANT_ID}"
    )
    flow = app.initiate_device_flow(scopes=_scopes())
    if "user_code" not in flow:
        raise RuntimeError(f"Device flow failed: {flow.get('error_description')}")

    print(f"\n  1. Open:  {flow['verification_uri']}")
    print(f"  2. Enter: {flow['user_code']}")
    print("\n  Waiting for sign-in", end="", flush=True)

    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        print(f"\n  [FAILED] {result.get('error_description', result.get('error', 'Unknown'))}")
        return False

    save_token(username, {
        "access_token":  result["access_token"],
        "refresh_token": result.get("refresh_token", ""),
        "expires_on":    int(result.get("expires_on") or (time.time() + result.get("expires_in", 3600))),
        "username":      username,
    })
    print(f"\n  [OK] Token saved for {username}")
    return True


def _start_device_flow() -> tuple:
    """Start MSAL device code flow. Returns (app, flow) for use by the caller."""
    app = msal.PublicClientApplication(
        CLIENT_ID, authority=f"https://login.microsoftonline.com/{TENANT_ID}"
    )
    flow = app.initiate_device_flow(scopes=_scopes())
    if "user_code" not in flow:
        raise RuntimeError(f"Device flow failed: {flow.get('error_description', 'unknown')}")
    return app, flow


def load_profiles() -> list[dict]:
    if not PROFILES_JSON.exists():
        return []
    with open(PROFILES_JSON, encoding="utf-8") as f:
        return json.load(f)


# ── DirectLine ────────────────────────────────────────────────────────────────

@dataclass
class Conversation:
    id: str
    token: str
    stream_url: str


@dataclass
class Response:
    activities: list[dict]
    latency_ms: float
    timed_out:  bool
    ws_closed:  bool = False   # DirectLine terminated the stream before bot replied


def fetch_directline_token(aad_token: Optional[str] = None) -> str:
    if TOKEN_ENDPOINT:
        headers = {}
        if ENDPOINT_NEEDS_AUTH and aad_token:
            headers["Authorization"] = f"Bearer {aad_token}"
        resp = _session.get(TOKEN_ENDPOINT, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()["token"]

    if DL_SECRET:
        resp = _session.post(
            f"{DIRECTLINE_BASE}/v3/directline/tokens/generate",
            headers={"Authorization": f"Bearer {DL_SECRET}"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["token"]

    raise RuntimeError("Neither CS_DIRECTLINE_SECRET nor CS_TOKEN_ENDPOINT is configured. Run: python run.py --setup")


def start_conversation(dl_token: str) -> Conversation:
    resp = _session.post(
        f"{DIRECTLINE_BASE}/v3/directline/conversations",
        headers={"Authorization": f"Bearer {dl_token}", "Content-Type": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return Conversation(id=data["conversationId"], token=data["token"], stream_url=data["streamUrl"])


def refresh_stream(conversation: Conversation) -> websocket.WebSocket:
    """Renew the DirectLine token and reconnect the WebSocket on the same conversation.
    Bot context is preserved — only the stream URL changes."""
    resp = _session.get(
        f"{DIRECTLINE_BASE}/v3/directline/conversations/{conversation.id}",
        headers={"Authorization": f"Bearer {conversation.token}"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    conversation.token      = data.get("token", conversation.token)
    conversation.stream_url = data["streamUrl"]
    return _retry_call(lambda: open_websocket(conversation.stream_url))


def open_websocket(stream_url: str) -> websocket.WebSocket:
    ws = websocket.WebSocket(sslopt={"check_hostname": True})
    ws.connect(stream_url, timeout=20)
    return ws


def send_utterance(conversation: Conversation, utterance: str) -> tuple[str, float, float]:
    send_time = time.time()
    send_mono = time.monotonic()
    resp = _session.post(
        f"{DIRECTLINE_BASE}/v3/directline/conversations/{conversation.id}/activities",
        headers={"Authorization": f"Bearer {conversation.token}", "Content-Type": "application/json"},
        json={"locale": "en-US", "type": "message", "from": {"id": "load-test-user"}, "text": utterance},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["id"], send_time, send_mono


def send_token_exchange(conversation: Conversation, invoke_id: str, connection_name: str, aad_token: str):
    """
    Responds to a signin/tokenExchange invoke from the bot.
    Copilot Studio sends this when 'Authenticate manually' is enabled —
    the client must reply with the user's AAD token to complete SSO.
    """
    resp = _session.post(
        f"{DIRECTLINE_BASE}/v3/directline/conversations/{conversation.id}/activities",
        headers={"Authorization": f"Bearer {conversation.token}", "Content-Type": "application/json"},
        json={
            "type": "invoke",
            "name": "signin/tokenExchange",
            "value": {
                "id": invoke_id,
                "connectionName": connection_name,
                "token": aad_token,
            },
            "from": {"id": "load-test-user"},
        },
        timeout=10,
    )
    if not resp.ok:
        log.debug("Token exchange HTTP %s body: %s", resp.status_code, resp.text[:300])
        resp.raise_for_status()
    log.debug("Token exchange sent: invoke=%s connection=%s", invoke_id, connection_name)


def read_response(
    ws: websocket.WebSocket,
    activity_id: str,
    response_timeout: float = 30.0,
    conversation: Optional[Conversation] = None,
    aad_token: Optional[str] = None,
    send_mono: Optional[float] = None,
) -> Response:
    """
    Reads WebSocket frames until the bot replies to activity_id.
    Waits up to response_timeout for the first reply, then _SILENCE_TIMEOUT
    seconds after the last reply before declaring the response complete.
    When conversation + aad_token are provided, handles signin/tokenExchange
    invokes automatically so SSO-authenticated bots work without manual sign-in.
    send_mono: monotonic-clock reading captured when send_utterance fired the POST
    (used for the true e2e latency delta — immune to wall-clock/NTP adjustments).
    """
    matched, last_match_time = [], None
    start_time = send_mono if send_mono is not None else time.monotonic()

    while True:
        now       = time.monotonic()
        remaining = (
            _SILENCE_TIMEOUT - (now - last_match_time)
            if matched else
            response_timeout - (now - start_time)
        )
        if remaining <= 0:
            break
        try:
            ws.settimeout(remaining)
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            break
        except websocket.WebSocketConnectionClosedException:
            if _run_state.dashboard is not None:
                _run_state.dashboard.on_event("⚠", "DirectLine closed WebSocket — reconnecting")
            _log_event("⚠", "ws_closed", "DirectLine closed WebSocket")
            return Response(activities=[], latency_ms=(time.monotonic()-start_time)*1000,
                            timed_out=True, ws_closed=True)
        except (websocket.WebSocketException, OSError) as _exc:
            if _run_state.dashboard is not None:
                _run_state.dashboard.on_event("⚠", f"Stream error ({type(_exc).__name__}) — reconnecting")
            _log_event("⚠", "ws_error", f"Stream error: {type(_exc).__name__}")
            return Response(activities=[], latency_ms=(time.monotonic()-start_time)*1000,
                            timed_out=True, ws_closed=True)

        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        for activity in data.get("activities", []):
            # Teams SSO: bot sends signin/tokenExchange invoke directly
            if (activity.get("type") == "invoke"
                    and activity.get("name") == "signin/tokenExchange"
                    and conversation and aad_token):
                val = activity.get("value", {})
                try:
                    send_token_exchange(conversation, val.get("id", ""), val.get("connectionName", ""), aad_token)
                except Exception as e:
                    log.warning("Token exchange (invoke) failed: %s", e)
                continue

            # "Authenticate manually" SSO: bot sends message with OAuthCard attachment.
            # Client must respond with signin/tokenExchange invoke carrying the AAD token.
            if activity.get("type") == "message" and conversation and aad_token:
                for attach in activity.get("attachments", []):
                    if attach.get("contentType") == "application/vnd.microsoft.card.oauth":
                        content      = attach.get("content", {})
                        token_res    = content.get("tokenExchangeResource", {})
                        if token_res:
                            try:
                                send_token_exchange(
                                    conversation,
                                    token_res.get("id", ""),
                                    content.get("connectionName", ""),
                                    aad_token,
                                )
                                log.debug("OAuthCard token exchange sent: connection=%s", content.get("connectionName", ""))
                            except Exception as e:
                                log.warning("Token exchange (OAuthCard) failed: %s", e)
                        break  # only one OAuthCard per activity
                else:
                    # No OAuthCard — check if this is a real bot reply
                    if (activity.get("from", {}).get("role") == "bot"
                            and activity.get("replyToId") == activity_id):
                        matched.append(activity)
                        last_match_time = time.monotonic()
                continue

            if (activity.get("type") == "message"
                    and activity.get("from", {}).get("role") == "bot"
                    and activity.get("replyToId") == activity_id):
                matched.append(activity)
                last_match_time = time.monotonic()

    end_time   = last_match_time or time.monotonic()
    latency_ms = (end_time - start_time) * 1000
    return Response(activities=matched, latency_ms=latency_ms, timed_out=len(matched) == 0)


def close_websocket(ws: websocket.WebSocket):
    try:
        ws.close()
    except Exception:
        pass


# ── Startup sequence (fires when Locust initialises in interactive mode) ──────

_TITLE = [
    " ██████╗ ██████╗ ██████╗ ██╗██╗      ██████╗ ████████╗",
    "██╔════╝██╔═══██╗██╔══██╗██║██║     ██╔═══██╗╚══██╔══╝",
    "██║     ██║   ██║██████╔╝██║██║     ██║   ██║   ██║   ",
    "██║     ██║   ██║██╔═══╝ ██║██║     ██║   ██║   ██║   ",
    "╚██████╗╚██████╔╝██║     ██║███████╗╚██████╔╝   ██║   ",
    " ╚═════╝ ╚═════╝ ╚═╝     ╚═╝╚══════╝ ╚═════╝    ╚═╝   ",
    "",
    "██╗      ██████╗  █████╗ ██████╗     ████████╗███████╗███████╗████████╗",
    "██║     ██╔═══██╗██╔══██╗██╔══██╗    ╚══██╔══╝██╔════╝██╔════╝╚══██╔══╝",
    "██║     ██║   ██║███████║██║  ██║       ██║   █████╗  ███████╗   ██║   ",
    "██║     ██║   ██║██╔══██║██║  ██║       ██║   ██╔══╝  ╚════██║   ██║   ",
    "███████╗╚██████╔╝██║  ██║██████╔╝       ██║   ███████╗███████║   ██║   ",
    "╚══════╝ ╚═════╝ ╚═╝  ╚═╝╚═════╝        ╚═╝   ╚══════╝╚══════╝   ╚═╝   ",
]
_ROCKET_BODY = [
    "         *    .  *       .         *    .",
    "    .  *    .       *  .    *   .     *  ",
    "       .        *    .        .   *      ",
    "  *  .    *   .    *    .   *    .    *  ",
    "                   /\\                   ",
    "                  /  \\                  ",
    "                 / 🔥 \\                 ",
    "                /______\\                ",
    "               /        \\               ",
    "              /  COPILOT \\              ",
    "             /____________\\             ",
    "                  |  |                  ",
    "                  |  |                  ",
    "                 /|  |\\                 ",
]
_ROCKET_EXHAUST = [
    "             ~ ~ ~ ~ ~               ",
    "            ~~ ~ ~ ~ ~~              ",
    "           ~ ~ ~ ~ ~ ~ ~             ",
    "          ~~ ~ ~ ~ ~ ~ ~~            ",
]
_EXPLOSION = """
         . * . * . * . * . * .
       *   \\  |  💥  |  /   *
         * - -BOOM!- - *
       *   /  |  💥  |  \\   *
         * . * . * . * . * . *
"""
_SPINNER = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]


# ── Gum TUI helpers ───────────────────────────────────────────────────────────

# 256-colour constants
_G_CYAN   = "14"
_G_PURPLE = "99"
_G_GREEN  = "82"
_G_YELLOW = "214"
_G_RED    = "196"
_G_DIM    = "240"
_G_WHITE  = "255"
_G_BLACK  = "0"


def _gum_ok() -> bool:
    """Return True if the gum binary is on PATH."""
    return bool(shutil.which("gum"))


def _gum_require():
    """Exit with install instructions if gum is not found."""
    if _gum_ok():
        return
    print()
    print("  Gum is required for the interactive UI.")
    print()
    print("  Install it with one of:")
    print("    Windows : winget install charmbracelet.gum")
    print("    macOS   : brew install gum")
    print("    Scoop   : scoop install charm-gum")
    print("    Manual  : https://github.com/charmbracelet/gum/releases")
    print()
    sys.exit(1)


def _gstyle(text: str, *, border: str = "", fg: str = _G_WHITE,
            border_fg: str = "", bold: bool = False,
            padding: str = "0 1", margin: str = "0 0",
            align: str = "left") -> str:
    """Return Lipgloss-styled text via `gum style`."""
    cmd = ["gum", "style",
           "--foreground", fg,
           "--align",      align,
           "--padding",    padding,
           "--margin",     margin]
    if border:
        cmd += ["--border", border,
                "--border-foreground", border_fg or fg]
    if bold:
        cmd += ["--bold"]
    cmd.append(text)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    return r.stdout


def _gprint(text: str, **kwargs):
    """Print a gum-styled line (no extra trailing newline)."""
    sys.stdout.write(_gstyle(text, **kwargs))
    sys.stdout.flush()


def _ginput(placeholder: str = "", *, header: str = "", default: str = "",
            password: bool = False, width: int = 72) -> str:
    """Single-line styled input via `gum input`."""
    cmd = ["gum", "input",
           "--placeholder",          placeholder,
           "--prompt",               "  ❯ ",
           "--prompt.foreground",    _G_CYAN,
           "--cursor.foreground",    _G_CYAN,
           "--header.foreground",    _G_DIM,
           "--width",                str(width),
           ]
    if header:
        cmd += ["--header", f"\n  {header}\n"]
    if default:
        cmd += ["--value", default]
    if password:
        cmd += ["--password"]
    r = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, encoding="utf-8")
    return r.stdout.strip()


def _gconfirm(prompt: str, *, default: bool = False) -> bool:
    """Yes/No prompt via `gum confirm`. Returns True for Yes."""
    cmd = [
        "gum", "confirm", prompt,
        "--affirmative",              "Yes",
        "--negative",                 "No",
        "--prompt.foreground",        "255",
        "--selected.background",      "213",
        "--selected.foreground",      _G_BLACK,

    ]
    if default:
        cmd.append("--default")
    r = subprocess.run(cmd, text=True, encoding="utf-8")
    return r.returncode == 0


def _gchoose(*items: str, header: str = "", height: int = 12,
             selected_fg: str = "213") -> str:
    """Arrow-key selection via `gum choose`. Returns chosen item or ''."""
    cmd = ["gum", "choose",
           "--cursor.foreground",   _G_CYAN,
           "--selected.foreground", selected_fg,
           "--header.foreground",   _G_PURPLE,
           "--cursor",              "▸ ",
           "--height",              str(height),
           ]
    if header:
        cmd += ["--header", header]
    cmd += list(items)
    r = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, encoding="utf-8")
    return r.stdout.strip() if r.returncode == 0 else ""


def _gchoose_multi(*items: str, header: str = "", height: int = 12) -> list:
    """Multi-select via `gum choose --no-limit`. Space toggles, Enter confirms."""
    cmd = ["gum", "choose", "--no-limit",
           "--cursor.foreground",        _G_CYAN,
           "--selected.foreground",      "213",
           "--selected.background",      "236",
           "--header.foreground",        _G_PURPLE,
           "--cursor",                   "▸ ",
           "--height",                   str(height),
           ]
    if header:
        cmd += ["--header", header]
    cmd += list(items)
    r = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, encoding="utf-8")
    return [line for line in r.stdout.splitlines() if line.strip()] if r.returncode == 0 else []


def _gfile(start_path: str = ".") -> str:
    """Interactive file browser via `gum file`. Returns selected path or ''."""
    r = subprocess.run(["gum", "file", start_path],
                       text=True, stdout=subprocess.PIPE, encoding="utf-8")
    return r.stdout.strip()


def _gwrite(placeholder: str = "", *, header: str = "",
            width: int = 72, height: int = 8) -> str:
    """Multi-line text input via `gum write`. Ctrl-D to confirm, Esc to cancel."""
    cmd = ["gum", "write",
           "--placeholder",       placeholder,
           "--prompt.foreground", _G_CYAN,
           "--cursor.foreground", _G_CYAN,
           "--header.foreground", _G_DIM,
           "--width",             str(width),
           "--height",            str(height),
   
           "--char-limit",        "2000"]
    if header:
        cmd += ["--header", f"\n  {header}\n"]
    r = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, encoding="utf-8")
    return r.stdout.strip()


def _gformat(text: str):
    """Render markdown text via `gum format`, falls back to plain print."""
    r = subprocess.run(["gum", "format", "--", text],
                       text=True, encoding="utf-8")
    if r.returncode != 0:
        print(text)


def _gpager(text: str):
    """Show text in a scrollable pager via `gum pager`, falls back to plain print."""
    if not _gum_ok():
        print(text)
        return
    subprocess.run(["gum", "pager", "--show-line-numbers=false"], input=text,
                   text=True, encoding="utf-8")


_GUM_ENV = {**os.environ, "PYTHONUTF8": "1"}   # ensure UTF-8 I/O on Windows

# ── Banner animation helpers ──────────────────────────────────────────────────

_BANNER_PALETTE = [
    255, 253, 251, 15,               # bright white
    14,  51,  45,  39,               # cyan
    33,  27,  99,  129,              # blue → purple
    165, 201, 213, 219,              # magenta → pink → back to white
]
_SPARKLE_CHARS = list("✦✧⋆★✸·✺✼❋*◦")


def _ansi_col(code: int, text: str, bold: bool = True) -> str:
    b = "1;" if bold else ""
    return f"\033[{b}38;5;{code}m{text}\033[0m"


def _rainbow(line: str, offset: int) -> str:
    pal, out, idx = _BANNER_PALETTE, [], 0
    for ch in line:
        if ch != " ":
            out.append(_ansi_col(pal[(idx + offset) % len(pal)], ch))
            idx += 1
        else:
            out.append(ch)
    return "".join(out)


def _section_header(title: str):
    """Vivid section header via gum style (stable, no shimmer)."""
    _gprint(f"  {title}  ", border="rounded", fg=_G_CYAN,
            bold=True, padding="0 3", margin="1 0", align="center")


def _ok_line(label: str, note: str = ""):
    _gprint(f"  ✓  {label:<32} {note}", fg=_G_GREEN, bold=True, padding="0 0")


def _fail_line(label: str, detail: str = ""):
    _gprint(f"  ✗  {label:<32} {detail}", fg=_G_RED, bold=True, padding="0 0")


def _dim_line(label: str, note: str = ""):
    _gprint(f"  ─  {label:<32} {note}", fg=_G_DIM, padding="0 0")


def _celebrate(msg: str):
    """Clean success box."""
    _gprint(f"  ✓  {msg}", border="rounded", fg=_G_GREEN,
            bold=True, padding="1 3", margin="1 0", border_fg=_G_GREEN)


def _wizard_rocket_float():
    """Floating rocket animation shown before the setup wizard."""
    def _stars(seed: int) -> str:
        rng = random.Random(seed)
        s = "  "
        for _ in range(55):
            s += (_ansi_col(rng.choice(_BANNER_PALETTE),
                            rng.choice(_SPARKLE_CHARS), bold=False)
                  if rng.random() < 0.11 else " ")
        return s

    ROCKET = [
        ("        /\\          ", 214, True),
        ("       /  \\         ", 214, True),
        ("      / 🔥 \\        ", 220, True),
        ("     /______\\       ", 214, True),
        ("    /        \\      ", 255, False),
        ("   / GM-6000  \\     ", 51,  True),
        ("  /____________\\    ", 214, True),
        ("       |  |         ", 255, False),
        ("       |  |         ", 255, False),
        ("   ~ ~ ~ ~ ~ ~   ", 202, False),
        ("  ~ ~ ~ ~ ~ ~ ~  ", 196, False),
    ]
    FRAME_H = 2 + 2 + len(ROCKET) + 2  # stars + offset space + rocket + stars

    os.system("cls" if os.name == "nt" else "clear")
    sys.stdout.write("\033[?25l")  # hide cursor
    sys.stdout.flush()
    try:
        first = True
        for seed, offset in [(1,2),(2,1),(3,1),(4,0),(5,1),(6,0)]:
            if not first:
                sys.stdout.write(f"\033[{FRAME_H + offset + 1}A")
            first = False
            sys.stdout.write("\n" + _stars(seed) + "\n" + _stars(seed + 50) + "\n")
            for _ in range(offset):
                sys.stdout.write("\n")
            for text, col, bold in ROCKET:
                sys.stdout.write("  " + _ansi_col(col, text, bold=bold) + "\n")
            sys.stdout.write(_stars(seed + 100) + "\n" + _stars(seed + 150) + "\n")
            sys.stdout.flush()
            time.sleep(0.12)
        time.sleep(0.2)
    finally:
        sys.stdout.write("\033[?25h")  # restore cursor
        sys.stdout.flush()
    os.system("cls" if os.name == "nt" else "clear")


def _with_spinner(title: str, fn, *, spinner: str = "dot", timeout: float = 120.0):
    """Run fn() while showing a gum spinner. Returns fn's result, re-raises exceptions."""
    done  = threading.Event()
    box   = [None, None]   # [result, exception]

    def _worker():
        try:
            box[0] = fn()
        except Exception as exc:
            box[1] = exc
        finally:
            done.set()

    threading.Thread(target=_worker, daemon=True).start()
    spin = subprocess.Popen(
        ["gum", "spin",
         "--spinner", spinner,
         "--title",   f"  {title}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=_GUM_ENV,
    )
    finished = done.wait(timeout=timeout)
    spin.terminate()
    spin.wait()
    print()
    if not finished:
        raise TimeoutError(f"'{title}' did not complete within {int(timeout)}s")
    if box[1]:
        raise box[1]
    return box[0]


def _show_startup_title():
    os.system("cls" if os.name == "nt" else "clear")

    # Phase 1: sparkle burst — flash then clear
    for _ in range(3):
        row = "  "
        for _ in range(56):
            row += _ansi_col(random.choice(_BANNER_PALETTE), random.choice(_SPARKLE_CHARS))
        sys.stdout.write(row + "\n")
    sys.stdout.flush()
    time.sleep(0.15)
    os.system("cls" if os.name == "nt" else "clear")

    if _gum_ok():
        # Styled title block via gum — no ASCII art needed
        subprocess.run(
            ["gum", "style",
             "--border",            "double",
             "--border-foreground", "99",
             "--foreground",        "213",
             "--bold",
             "--padding",           "1 8",
             "--margin",            "1 2",
             "--align",             "center",
             "--width",             "58",
             "GRUNTMASTER 6000"],
            env=_GUM_ENV,
        )
    else:
        # ASCII art fallback
        sys.stdout.write("\n")
        offset = 0
        for line in _TITLE:
            if line:
                sys.stdout.write("  " + _rainbow(line, offset) + "\n")
                offset = (offset + 4) % len(_BANNER_PALETTE)
            else:
                sys.stdout.write("\n")
            sys.stdout.flush()
            time.sleep(0.025)

    # Sparkle trail
    trail = "  "
    for _ in range(56):
        if random.random() < 0.45:
            trail += _ansi_col(random.choice(_BANNER_PALETTE), random.choice(_SPARKLE_CHARS), bold=False)
        else:
            trail += " "
    sys.stdout.write(trail + "\n\n")
    sys.stdout.flush()
    time.sleep(0.2)

    # Subtitle
    if _gum_ok():
        _gprint(
            "  DirectLine 3.0  ·  Entra ID Auth  ·  Copilot Studio",
            fg="219", bold=True, padding="0 0", margin="0 1",
        )
    else:
        sys.stdout.write(f"  \033[1;38;5;219m  DirectLine 3.0  ·  Entra ID Auth  ·  Copilot Studio\033[0m\n\n")
        sys.stdout.flush()


def _user_auth_required() -> bool:
    """AAD user auth is only needed when the token endpoint requires it, or SSO is configured."""
    return (bool(TOKEN_ENDPOINT) and ENDPOINT_NEEDS_AUTH) or bool(AGENT_APP_ID)


def _show_profile_status(profiles: list[dict]) -> list[str]:
    _section_header("✦  PROFILE STATUS  ✦")

    needs_auth = []
    _seen_auth = set()
    for i, profile in enumerate(profiles):
        username = profile["username"]
        display  = profile.get("display_name", username)

        if not _user_auth_required():
            _ok_line(display, "READY  (no auth required)")
            continue

        try:
            _with_spinner(f"Checking {display}…", lambda u=username: get_valid_token(u))
            sys.stdout.write("  " + _rainbow(f"  ✓  {display:<24}  READY ✓", i * 3) + "\n")
            sys.stdout.flush()
        except RuntimeError:
            _fail_line(display, "NEEDS AUTH ✗")
            if username not in _seen_auth:
                needs_auth.append(username)
                _seen_auth.add(username)

    print()
    return needs_auth


def _rocket_auth(username: str) -> bool:
    if _gum_ok():
        # ── Step header ───────────────────────────────────────────────────
        _gprint(
            f"  Authentication Required\n\n  {username}",
            border="rounded", fg=_G_YELLOW, bold=True,
            padding="1 3", margin="1 0", border_fg=_G_YELLOW,
        )
        try:
            app, flow = _start_device_flow()
        except RuntimeError as exc:
            _fail_line("Device flow failed", str(exc))
            return False

        # ── Prominent device code ─────────────────────────────────────────
        _gprint(
            f"  {flow['user_code']}",
            border="rounded", fg=_G_GREEN, bold=True,
            padding="1 6", margin="0 1", border_fg=_G_GREEN,
        )
        _gprint(
            f"  Go to:  {flow['verification_uri']}",
            fg=_G_CYAN, padding="0 2", margin="0 0",
        )
        print()

        result = {}
        for _attempt in range(3):
            try:
                result = _with_spinner(
                    "Waiting for sign-in…",
                    lambda: app.acquire_token_by_device_flow(flow),
                    spinner="moon",
                    timeout=float(flow.get("expires_in", 900)),
                )
                break
            except (requests.exceptions.ConnectionError, ConnectionError, OSError):
                if _attempt < 2:
                    _fail_line("Connection dropped", f"Retrying… (attempt {_attempt + 2}/3)")
                    time.sleep(2)
                    continue
                _fail_line("Auth failed", "Network error — check connectivity and try again")
                return False

        if "access_token" not in result:
            _fail_line("Auth failed", result.get("error_description", result.get("error", "unknown")))
            return False

        save_token(username, {
            "access_token":  result["access_token"],
            "refresh_token": result.get("refresh_token", ""),
            "expires_on":    int(result.get("expires_on") or (time.time() + result.get("expires_in", 3600))),
            "username":      username,
        })
        _ok_line("Signed in", username)
        time.sleep(0.4)
        return True

    # ── Fallback: original Rich/rocket animation ───────────────────────────
    console.print()
    console.print(Panel(
        f"[bold yellow]  🚀  INITIATING AUTH SEQUENCE FOR  🚀\n  [white]{username}[/white][/bold yellow]",
        border_style="yellow",
    ))
    console.print()
    for line in _ROCKET_BODY:
        console.print(f"[bold cyan]{line}[/bold cyan]")
    result_box = {"success": False, "done": False}
    def _run():
        result_box["success"] = authenticate_profile(username)
        result_box["done"]    = True
    threading.Thread(target=_run, daemon=True).start()
    exhaust_cycle = itertools.cycle(_ROCKET_EXHAUST)
    stars = ["✦", "✧", "·", "•", "⋆", "*"]
    while not result_box["done"]:
        console.print(
            f"[bold orange3]{next(exhaust_cycle)}[/bold orange3]  [dim yellow]{random.choice(stars)}[/dim yellow]",
            end="\r",
        )
        time.sleep(0.15)
    console.print(" " * 60)
    if result_box["success"]:
        console.print(f"\n  [bold green]✓ AUTH COMPLETE — {username}[/bold green]\n")
    else:
        console.print(f"\n  [bold red]✗ AUTH FAILED — {username}[/bold red]\n")
    time.sleep(0.5)
    return result_box["success"]


def _bomb_countdown():
    if _gum_ok():
        _gprint(
            "  ALL SYSTEMS GO",
            border="double", fg=_G_RED, bold=True,
            padding="0 6", margin="1 0", border_fg=_G_RED,
        )
        fuse_length = 20
        for i in range(fuse_length, -1, -1):
            sys.stdout.write(
                f"  \033[31m{'~' * (fuse_length - i)}\033[2m{'─' * i}\033[0m 💣\r"
            )
            sys.stdout.flush()
            time.sleep(0.07)
        sys.stdout.write(" " * 60 + "\r")
        for n in range(3, 0, -1):
            sys.stdout.write(f"  \033[1;31mT-{n}\033[0m\r")
            sys.stdout.flush()
            time.sleep(0.7)
        _gprint("  GO! 🚀", fg=_G_GREEN, bold=True, padding="0 4", margin="0 1")
        print()
        return
    # Fallback
    console.print()
    console.print(Rule("[bold red]ALL SYSTEMS GO[/bold red]", style="bold red"))
    console.print()
    fuse_length = 20
    for i in range(fuse_length, -1, -1):
        console.print(
            f"  [bold red]{'~' * (fuse_length - i)}[/bold red][dim]{'─' * i}[/dim] 💣",
            end="\r",
        )
        time.sleep(0.07)
    console.print(" " * 60)
    for n in range(3, 0, -1):
        console.print(f"  [bold red]T-{n}[/bold red]", end="\r")
        time.sleep(0.7)
    console.print(f"  [bold green]GO! 🚀   [/bold green]")
    console.print()


def _check_credentials() -> str:
    """
    Scans Credential Manager for required values.
    Returns 'ok' silently when all credentials are present.
    Returns 'missing' and shows detail when something is absent.
    """
    # Silent fast-path — no UI if everything is already set
    tenant_val = _load_credential("CS_TENANT_ID")
    client_val = _load_credential("CS_CLIENT_ID")
    secret_val = _load_credential("CS_DIRECTLINE_SECRET")
    endpt_val  = _load_credential("CS_TOKEN_ENDPOINT")
    tenant_ok  = bool(tenant_val and _GUID_RE.match(tenant_val))
    client_ok  = bool(client_val and _GUID_RE.match(client_val))
    dl_ok      = bool(secret_val or endpt_val)
    if tenant_ok and client_ok and dl_ok:
        return "ok"

    # Something is missing — show what's wrong
    if _gum_ok():
        _gprint("  CREDENTIAL CHECK", border="rounded", fg=_G_CYAN,
                bold=True, padding="0 3", margin="1 0", border_fg=_G_PURPLE)
        print()
        _ok_line("Entra Tenant ID",             "FOUND")    if tenant_ok else _fail_line("Entra Tenant ID",             "MISSING")
        _ok_line("App Registration Client ID",  "FOUND")    if client_ok else _fail_line("App Registration Client ID",  "MISSING")
        if secret_val and endpt_val:
            _ok_line("DirectLine Secret",   "FOUND")
            _ok_line("Token Endpoint URL",  "FOUND  (both set — Token Endpoint takes priority)")
        elif secret_val:
            _ok_line("DirectLine Secret",   "FOUND")
        elif endpt_val:
            _ok_line("Token Endpoint URL",  "FOUND")
        else:
            _fail_line("DirectLine Secret",  "MISSING  (need Secret or Token Endpoint)")
            _fail_line("Token Endpoint URL", "MISSING  (need Secret or Token Endpoint)")
        print()
        _gprint("  ✗  Missing credentials — run:  python run.py --setup",
                border="rounded", fg=_G_RED,
                padding="1 2", margin="0 1", border_fg=_G_RED)
        print()
        return "missing"

    # ── Rich fallback — gum not available; values already loaded above ──────
    def _row(ok: bool, label: str, note: str = ""):
        icon   = "[bold green]✓[/bold green]" if ok else "[bold red]✗[/bold red]"
        status = "[bold green]FOUND[/bold green]" if ok else "[bold red]MISSING[/bold red]"
        extra  = f"  [dim]{note}[/dim]" if note else ""
        console.print(f"  {icon}  {label:<35} {status}{extra}")

    console.print(Panel("[bold cyan]  CREDENTIAL CHECK  [/bold cyan]", border_style="cyan"))
    console.print()
    _row(tenant_ok, "Entra Tenant ID")
    _row(client_ok, "App Registration Client ID")
    if secret_val and endpt_val:
        _row(True, "DirectLine Secret")
        _row(True, "Token Endpoint URL", "(both set — Token Endpoint takes priority)")
    elif secret_val:
        _row(True, "DirectLine Secret")
    elif endpt_val:
        _row(True, "Token Endpoint URL")
    else:
        _row(False, "DirectLine Secret",  "(need Secret or Token Endpoint)")
        _row(False, "Token Endpoint URL", "(need Secret or Token Endpoint)")
    console.print()
    console.print(Panel(
        "[bold red]  ✗  Missing credentials — run:  python run.py --setup[/bold red]",
        border_style="red",
    ))
    console.print()
    return "missing"


_ERROR_HINTS = {
    "IntegratedAuthenticationNotSupportedInChannel": (
        "Your bot has 'Authenticate with Microsoft' enabled — switch to Token Endpoint mode.\n\n"
        "  Fix (recommended) — use the Token Endpoint instead of a DirectLine Secret:\n"
        "    python run.py --setup  →  leave 'DirectLine Secret' blank,\n"
        "    paste 'Token Endpoint URL' from  Copilot Studio → Settings → Channels → Direct Line\n\n"
        "  Fix (alternative) — enable SSO token exchange via Bot Client ID:\n"
        "    python run.py --setup  →  fill in 'Bot Client ID'\n"
        "    (Copilot Studio → Settings → Security → Authentication → Client ID)"
    ),
    "Forbidden": (
        "The DirectLine secret may be wrong or the Direct Line channel is not enabled.\n"
        "  • Re-run setup:  python run.py --setup\n"
        "  • Verify:  Copilot Studio → Settings → Channels → Direct Line"
    ),
}

_ERROR_HINTS_DEFAULT = (
    "Check that the bot is published and the Direct Line channel is configured.\n"
    "  Re-run setup if credentials may have changed:  python run.py --setup"
)


def _print_error_hint(error_code: str):
    hint = _ERROR_HINTS.get(error_code, _ERROR_HINTS_DEFAULT)
    console.print(Panel(f"[yellow]{hint}[/yellow]", border_style="yellow", title="[dim]how to fix[/dim]"))
    console.print()


def _preflight_bot_check(profiles: list[dict]) -> bool:
    """
    Per-profile token validation, then a single end-to-end bot ping with 'hi'.
    Returns False and prints a clear error if anything fails.
    """
    _section_header("🚀  BOT PRE-FLIGHT CHECK  🚀")

    def _spin(title: str, fn):
        return _with_spinner(title, fn, spinner="globe") if _gum_ok() else fn()

    # ── Per-profile token check ───────────────────────────────────────────
    _gprint("  Profile tokens", fg=_G_WHITE, bold=True, padding="0 1", margin="0 0")
    print()

    aad_token_for_bot = None
    if _user_auth_required():
        for profile in profiles:
            username = profile["username"]
            display  = profile.get("display_name", username)
            try:
                tok = _spin(f"Checking token — {display}…", lambda u=username: get_valid_token(u))
                if aad_token_for_bot is None:
                    aad_token_for_bot = tok
                _ok_line(display, "token valid")
            except Exception as e:
                _fail_line(display, f"FAILED — {e}")
                print()
                _gprint("  Re-run setup and re-authenticate profiles.",
                        fg=_G_YELLOW, padding="0 2", margin="0 1")
                return False
    else:
        for profile in profiles:
            _dim_line(profile.get("display_name", profile["username"]), "no auth required")

    print()

    # ── Bot connectivity ping ─────────────────────────────────────────────
    _gprint("  Bot connectivity", fg=_G_WHITE, bold=True, padding="0 1", margin="0 0")
    print()

    try:
        dl_token = _spin("Fetching DirectLine token…",
                         lambda: fetch_directline_token(aad_token_for_bot))
        _ok_line("DirectLine token", "OK")
        _bot_id = _jwt_claims(dl_token).get("bot")
        if _bot_id:
            _ok_line("Target agent id", _bot_id)
    except Exception as e:
        _fail_line("DirectLine token", f"FAILED — {e}")
        print()
        _gprint(
            "  Possible causes:\n"
            "  • Wrong DirectLine secret → re-run setup\n"
            "  • Direct Line channel not enabled in Copilot Studio\n"
            "  • Bot uses Enhanced Authentication → switch to Token Endpoint",
            border="rounded", fg=_G_YELLOW,
            padding="1 2", margin="0 1", border_fg=_G_YELLOW,
        )
        print()
        return False

    try:
        conversation = _spin("Starting conversation…",
                             lambda: start_conversation(dl_token))
        _ok_line("Conversation started", f"OK  ({conversation.id[:16]}…)")
    except Exception as e:
        _fail_line("Start conversation", f"FAILED — {e}")
        print()
        return False

    try:
        ws = _spin("Opening WebSocket…",
                   lambda: open_websocket(conversation.stream_url))
        _ok_line("WebSocket", "OK")
    except Exception as e:
        _fail_line("WebSocket", f"FAILED — {e}")
        print()
        return False

    try:
        activity_id, _, _ = _spin("Sending 'hi'…",
                               lambda: send_utterance(conversation, "hi"))
        _ok_line("Sent 'hi'", "OK")
    except Exception as e:
        _fail_line("Send utterance", f"FAILED — {e}")
        close_websocket(ws)
        return False

    _pf_log = TMP_DIR / "_preflight_last.txt"
    try:
        with open(_pf_log, "w") as _f:
            _f.write(f"activity_id={activity_id}\naad_token={'set' if aad_token_for_bot else 'none'}\n")
    except Exception:
        pass

    _pf_timeout = max(30.0, float(test_config.get("response_timeout", 30.0)))
    print(f"  Waiting for bot reply ({int(_pf_timeout)}s)…", flush=True)
    try:
        response = read_response(ws, activity_id, response_timeout=_pf_timeout,
                                 conversation=conversation,
                                 aad_token=aad_token_for_bot)
    except Exception as e:
        _fail_line("Bot response", f"FAILED — {e}")
        return False
    finally:
        close_websocket(ws)

    try:
        with open(_pf_log, "a") as _f:
            _f.write(f"timed_out={response.timed_out}\nws_closed={response.ws_closed}\n"
                     f"matched={len(response.activities)}\n"
                     f"activities={[a.get('type') + ':' + a.get('from', {}).get('role', '?') for a in response.activities]}\n")
    except Exception:
        pass

    if response.timed_out:
        _fail_line("Bot response", f"NO REPLY ({int(_pf_timeout)}s timeout)")
        print()
        _gprint(
            "  No matching reply received.\n\n"
            "  Possible causes:\n"
            "  • Direct Line channel disabled in Copilot Studio → Channels\n"
            "  • Bot sends adaptive cards or typing events only — not plain text\n"
            "  • replyToId not set in bot response\n"
            "  • Network / proxy blocking directline.botframework.com",
            border="rounded", fg=_G_YELLOW, padding="1 2", margin="0 1", border_fg=_G_YELLOW,
        )
        print(f"  (Debug log: {_pf_log})")
        print()
        try:
            ans = input("  Bot did not respond. Continue to test anyway? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans == "y":
            return True
        return False

    first_reply = response.activities[0].get("text", "").strip()

    _agent_name = None
    for _a in response.activities:
        _nm = _a.get("from", {}).get("name")
        if _nm:
            _agent_name = _nm
            break
    if _agent_name:
        _ok_line("Agent", _agent_name)

    error_code = None
    m = re.search(r"Error code:\s*(\S+)", first_reply)
    if m:
        error_code = m.group(1).rstrip(".")

    if error_code:
        _fail_line("Bot responded with error", error_code)
        print()
        _gprint(f"  Bot said:\n\n  {first_reply[:400]}",
                border="rounded", fg=_G_RED,
                padding="1 2", margin="0 1", border_fg=_G_RED)
        print()
        _print_error_hint(error_code)
        return False

    _ok_line("Bot responded", f"{response.latency_ms:.0f}ms")
    print()
    _gprint(f"  Bot said:\n\n  {first_reply[:300]}",
            border="rounded", fg=_G_GREEN,
            padding="1 2", margin="0 1", border_fg=_G_GREEN)
    print()
    return True


def run_startup_sequence(environment, profiles: list[dict]):
    if "--headless" in sys.argv or "-headless" in sys.argv:
        return
    _show_startup_title()

    cred_status = _check_credentials()
    if cred_status == "missing":
        environment.runner.quit()
        return
    if cred_status == "update":
        run_wizard()
        profiles = load_profiles()  # reload in case profiles changed

    needs_auth = _show_profile_status(profiles)
    if needs_auth:
        console.print(Panel(
            f"[yellow]  {len(needs_auth)} profile(s) need sign-in.\n"
            "  Watch the prompts below — open the URL and enter the code.[/yellow]",
            border_style="yellow",
        ))
        console.print()
        for username in needs_auth:
            if not _rocket_auth(username):
                console.print(f"[bold red]Auth failed for {username}. Stopping.[/bold red]")
                environment.runner.quit()
                return

    if not _preflight_bot_check(profiles):
        environment.runner.quit()
        return

    _bomb_countdown()
    console.print(Panel(
        "[bold green]  🌐  TEST LAUNCHED — OPEN YOUR BROWSER  🌐[/bold green]\n\n"
        "  [bold white]http://localhost:8089[/bold white]\n\n"
        "  [dim]Fill in parameters and click [bold]Start[/bold] to begin the assault[/dim]",
        border_style="bold green",
        title="[bold green]READY[/bold green]",
    ))
    console.print()


# ── Setup wizard ──────────────────────────────────────────────────────────────

def _is_configured() -> bool:
    tenant = _load_credential("CS_TENANT_ID")
    client = _load_credential("CS_CLIENT_ID")
    has_dl = _load_credential("CS_DIRECTLINE_SECRET") or _load_credential("CS_TOKEN_ENDPOINT")
    if not tenant or not client or not has_dl:
        return False
    if not _GUID_RE.match(tenant):
        return False
    return len(load_profiles()) > 0


def _save_credentials(config: dict):
    global TENANT_ID, CLIENT_ID, DL_SECRET, AGENT_APP_ID, TOKEN_ENDPOINT, ENDPOINT_NEEDS_AUTH
    import keyring
    for key, value in config.items():
        keyring.set_password(_KR_SERVICE, key, value)
        os.environ[key] = value
    TENANT_ID          = config.get("CS_TENANT_ID",                    TENANT_ID)
    CLIENT_ID          = config.get("CS_CLIENT_ID",                    CLIENT_ID)
    DL_SECRET          = config.get("CS_DIRECTLINE_SECRET",            DL_SECRET)
    AGENT_APP_ID       = config.get("CS_AGENT_APP_ID",                 AGENT_APP_ID)
    TOKEN_ENDPOINT     = config.get("CS_TOKEN_ENDPOINT",               TOKEN_ENDPOINT)
    ENDPOINT_NEEDS_AUTH = config.get("CS_TOKEN_ENDPOINT_REQUIRES_AUTH", "false").lower() == "true"


def _write_profiles(profiles: list[dict]):
    PROFILES_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(PROFILES_JSON, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2)


def run_wizard(*, _screen1_mode: bool = False) -> None:
    _gum_require()
    if not _screen1_mode:
        _wizard_rocket_float()

    state = {
        "tenant":              _load_credential("CS_TENANT_ID"),
        "client":              _load_credential("CS_CLIENT_ID"),
        "agent_app":           _load_credential("CS_AGENT_APP_ID"),
        "secret":              _load_credential("CS_DIRECTLINE_SECRET"),
        "endpoint":            _load_credential("CS_TOKEN_ENDPOINT"),
        "endpoint_needs_auth": _load_credential("CS_TOKEN_ENDPOINT_REQUIRES_AUTH").lower() == "true",
        "profiles":            load_profiles(),
    }

    _SAVE     = "  ✓  Save & continue"
    _CONTINUE = "  ▶  Continue"
    _ADD      = "  +  Add profile"
    _BACK     = "  ←  Back"
    _EXIT     = "  ✕  Exit"

    def _val(v: str, *, masked: bool = False, opt_note: str = "") -> str:
        if not v:
            return opt_note or "(not set)"
        if masked:
            return "●●●●●●●● (saved)"
        return (v[:44] + "…") if len(v) > 45 else v

    def _mrow(label: str, value: str, ok: bool | None = None, hint: str = "") -> str:
        mark     = "   ✓" if ok is True else ("   ✗" if ok is False else "    ")
        hint_col = f"  {hint}" if hint else ""
        return f"      {label:<28}  {value:<46}{mark}{hint_col}"

    while True:
        os.system("cls" if os.name == "nt" else "clear")
        _gprint(
            "  ✦  GRUNTMASTER 6000  ·  SETUP WIZARD  ✦\n\n"
            "  Connect GRUNTMASTER to your Copilot Studio agent.\n\n"
            "  You need: an Azure AD app registration (Tenant ID + Client ID + Bot Client ID),\n"
            "  a DirectLine Secret, and at least one M365 test account.\n"
            "  All credentials are saved to Windows Credential Manager.",
            border="double", fg=_G_CYAN, bold=True,
            border_fg=_G_PURPLE, padding="1 3", margin="1 0",
        )

        t_ok  = bool(state["tenant"]    and _GUID_RE.match(state["tenant"]))
        c_ok  = bool(state["client"]    and _GUID_RE.match(state["client"]))
        a_ok  = bool(state["agent_app"] and _GUID_RE.match(state["agent_app"]))
        dl_ok = bool(state["secret"])

        items = [
            _mrow("Tenant ID",           _val(state["tenant"]),
                  t_ok if state["tenant"] else False,
                  "required  ·  Azure portal → Entra ID → Overview → Tenant ID"),
            _mrow("Client ID",           _val(state["client"]),
                  c_ok if state["client"] else False,
                  "required  ·  App registrations → your app → Application (client) ID"),
            _mrow("Bot Client ID (SSO)", _val(state["agent_app"], opt_note="(not set)"),
                  a_ok if state["agent_app"] else False,
                  "required  ·  Copilot Studio → Settings → Security → Authentication → Client ID"),
            _mrow("DirectLine Secret",   _val(state["secret"], masked=True,
                  opt_note="(not set)"),
                  True if state["secret"] else False,
                  "required  ·  Copilot Studio → Channels → Direct Line → Secret keys"),
        ]
        _N_CRED = len(items)

        items.append("")
        items.append("  ─  PROFILES  ─  Each profile is a real M365 account. Assign a scenario")
        items.append("     to control which utterances it sends. Multiple profiles = more load.")

        _needs_auth_profiles: list[str] = []
        for p_idx, p in enumerate(state["profiles"]):
            display  = p.get("display_name", p["username"])
            tok      = load_token(p["username"])
            ready    = (not _user_auth_required()) or bool(tok and is_token_valid(tok))
            if _screen1_mode and not ready:
                _needs_auth_profiles.append(p["username"])
            scenario  = f"  → {p['scenario']}" if p.get("scenario") else "  → (auto-assign)"
            auth_hint = "" if ready else "select to authenticate"
            items.append(_mrow(f"Profile [{p_idx}]: {display}",
                               p["username"] + scenario, ready, auth_hint))

        if _screen1_mode:
            _all_ok = t_ok and c_ok and a_ok and dl_ok and bool(state["profiles"])
            items += ["", _ADD]
            if _all_ok:
                items.append(_CONTINUE)
            items.append(_EXIT)
        else:
            items += ["", _ADD, _SAVE, _BACK, _EXIT]

        choice = _gchoose(
            *items,
            header="\n  ↑ ↓  navigate     Enter  select\n",
            height=min(len(items) + 4, 26),
        )

        if choice and choice.strip() == _EXIT.strip():
            os.system("cls" if os.name == "nt" else "clear")
            sys.exit(0)

        if choice and choice.strip() == _BACK.strip():
            os.system("cls" if os.name == "nt" else "clear")
            return

        if choice and _screen1_mode and choice.strip() == _CONTINUE.strip():
            break  # all_ok guaranteed — fall through to save block

        if _screen1_mode and not choice:
            continue  # Escape in screen1 mode → just re-render

        if not choice or choice.strip() == _SAVE.strip():
            errs = []
            if not t_ok:
                errs.append("Tenant ID is required and must be a valid GUID.")
            if not c_ok:
                errs.append("Client ID is required and must be a valid GUID.")
            if not a_ok:
                errs.append("Bot Client ID (SSO) is required and must be a valid GUID.")
            if not dl_ok:
                errs.append("DirectLine Secret is required.")
            if not state["profiles"]:
                errs.append("At least one profile is required.")
            if errs:
                _gprint(
                    "\n".join(f"  ✗  {e}" for e in errs),
                    border="rounded", fg=_G_RED,
                    border_fg=_G_RED, padding="1 2", margin="0 1",
                )
                print()
                input("  Press Enter to go back...")
                continue
            break

        if choice.strip() == _ADD.strip():
            while True:
                os.system("cls" if os.name == "nt" else "clear")
                _section_header("✦  ADD PROFILE  ✦")

                uname = _ginput(
                    "loadtest.user@yourcompany.com",
                    header="Username (UPN) — the Microsoft 365 email for this test account",
                )
                if not uname:
                    break
                disp_def = uname.split("@")[0]
                disp = _ginput(
                    disp_def,
                    header="Display name  (label shown in terminal — press Enter to accept default)",
                    default=disp_def,
                ) or disp_def
                available_csvs = sorted(p.stem for p in UTTERANCES_DIR.glob("*.csv"))
                scenario = ""
                if available_csvs:
                    csv_pick = _gchoose(
                        "(none — auto assign)", *available_csvs,
                        header=f"\n  Which scenario CSV does this profile use?\n"
                               f"  Available: {', '.join(available_csvs)}\n",
                        height=min(len(available_csvs) + 5, 12),
                    )
                    scenario = "" if csv_pick.startswith("(none") else csv_pick.strip()
                new_p: dict = {"username": uname, "display_name": disp}
                if scenario:
                    new_p["scenario"] = scenario
                state["profiles"].append(new_p)
                _do_auth = _gconfirm("Authenticate this profile now? (requires browser sign-in)", default=True)
                if _do_auth:
                    _rocket_auth(uname)
                _gprint(f"  ✓  Added: {disp}  ({uname})",
                        fg=_G_GREEN, bold=True, padding="0 2", margin="0 1")
                if not _gconfirm("Add another profile?", default=False):
                    break
            continue

        # ── Match choice back to item index ──────────────────────────────
        idx = next((i for i, it in enumerate(items)
                    if it.strip() == choice.strip()), -1)

        if idx == 0:
            os.system("cls" if os.name == "nt" else "clear")
            v = _ginput("xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                        header="Tenant ID  ·  Azure portal → Microsoft Entra ID → Overview → Tenant ID",
                        default=state["tenant"])
            if v:
                if _GUID_RE.match(v.strip()):
                    state["tenant"] = v.strip()
                else:
                    _gprint("  Not a valid GUID.  Format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                            fg=_G_RED, padding="0 2", margin="0 1")
                    time.sleep(1.5)

        elif idx == 1:
            os.system("cls" if os.name == "nt" else "clear")
            v = _ginput("xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                        header="Client ID  ·  Azure portal → App registrations → [your app] → Application (client) ID",
                        default=state["client"])
            if v:
                if _GUID_RE.match(v.strip()):
                    state["client"] = v.strip()
                else:
                    _gprint("  Not a valid GUID.  Format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                            fg=_G_RED, padding="0 2", margin="0 1")
                    time.sleep(1.5)

        elif idx == 2:
            os.system("cls" if os.name == "nt" else "clear")
            v = _ginput(
                "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                header="Bot Client ID  ·  Copilot Studio → Settings → Security → Authentication → Client ID",
                default=state["agent_app"],
            )
            if v:
                if _GUID_RE.match(v.strip()):
                    state["agent_app"] = v.strip()
                else:
                    _gprint("  Not a valid GUID.  Format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                            fg=_G_RED, padding="0 2", margin="0 1")
                    time.sleep(1.5)

        elif idx == 3:
            os.system("cls" if os.name == "nt" else "clear")
            _gprint(
                "  DirectLine Secret\n\n"
                "  Copilot Studio → Settings → Channels → Direct Line → Secret keys → Show\n"
                "  Leave blank to keep the existing saved value.",
                border="rounded", fg=_G_DIM,
                border_fg=_G_DIM, padding="1 2", margin="0 1",
            )
            print()
            v = _ginput("paste secret here (input is hidden)", password=True)
            if v:
                state["secret"] = v
                try:
                    _test_resp = _session.post(
                        f"{DIRECTLINE_BASE}/v3/directline/tokens/generate",
                        headers={"Authorization": f"Bearer {v}"},
                        timeout=8,
                    )
                    if _test_resp.ok:
                        _gprint("  ✓  DirectLine Secret validated — token endpoint reachable.", fg=_G_GREEN, padding="0 2")
                    else:
                        _gprint(f"  ⚠  Validation returned HTTP {_test_resp.status_code} — check the secret.", fg=_G_YELLOW, padding="0 2")
                except Exception as _ve:
                    _gprint(f"  ⚠  Could not validate (no network?): {_ve}", fg=_G_YELLOW, padding="0 2")
                time.sleep(1.2)

        elif idx == 4:
            os.system("cls" if os.name == "nt" else "clear")
            v = _ginput(
                "https://…",
                header="Token Endpoint URL  ·  Copilot Studio → Settings → Channels → Direct Line → Token Endpoint\n"
                       "  Leave blank if using DirectLine Secret instead.",
                default=state["endpoint"],
            )
            state["endpoint"] = v.strip() if v else ""
            if state["endpoint"]:
                state["endpoint_needs_auth"] = _gconfirm(
                    "Does this Token Endpoint require an AAD Bearer token?",
                    default=state.get("endpoint_needs_auth", False),
                )
                try:
                    _test_resp = _session.get(state["endpoint"], timeout=8)
                    if _test_resp.ok:
                        _gprint("  ✓  Token endpoint reachable.", fg=_G_GREEN, padding="0 2")
                    else:
                        _gprint(f"  ⚠  Endpoint returned HTTP {_test_resp.status_code}.", fg=_G_YELLOW, padding="0 2")
                except Exception as _ve:
                    _gprint(f"  ⚠  Could not reach endpoint: {_ve}", fg=_G_YELLOW, padding="0 2")
                time.sleep(1.2)

        elif _N_CRED + 3 <= idx < _N_CRED + 3 + len(state["profiles"]):
            p_idx = idx - _N_CRED - 3
            p     = state["profiles"][p_idx]
            os.system("cls" if os.name == "nt" else "clear")
            _gprint(
                f"  Profile: {p.get('display_name', p['username'])}\n  {p['username']}",
                border="rounded", fg=_G_CYAN,
                bold=True, padding="1 2", margin="1 0",
            )
            action = _gchoose(
                "  Edit username / display name / scenario",
                "  Re-authenticate now",
                "  Delete this profile",
                "  ← Cancel",
                header="\n  What would you like to do?\n",
                height=8,
            )
            if "Edit" in action:
                _edit_action = _gchoose(
                    "  Edit username / UPN",
                    "  Edit display name",
                    "  Change scenario CSV",
                    "  ← Back",
                    header="\n  What would you like to edit?\n",
                    height=8,
                )
                if "← Back" in _edit_action:
                    pass   # return to profile menu without saving
                else:
                    uname = p["username"]
                    disp  = p.get("display_name", uname.split("@")[0])
                    if "username" in _edit_action.lower() or "upn" in _edit_action.lower():
                        uname = _ginput("", header="Username (UPN)", default=uname) or uname
                    if "display" in _edit_action.lower():
                        disp_def = uname.split("@")[0]
                        disp = _ginput("", header="Display name",
                                       default=p.get("display_name", disp_def)) or disp_def
                    scenario = p.get("scenario", "")
                    if "scenario" in _edit_action.lower():
                        available_csvs = sorted(q.stem for q in UTTERANCES_DIR.glob("*.csv"))
                        if available_csvs:
                            csv_pick = _gchoose(
                                "(none)", *available_csvs,
                                header="\n  Scenario CSV\n",
                                height=min(len(available_csvs) + 4, 10),
                            )
                            scenario = "" if csv_pick.strip() == "(none)" else csv_pick.strip()
                    new_p: dict = {"username": uname, "display_name": disp}
                    if scenario:
                        new_p["scenario"] = scenario
                    state["profiles"][p_idx] = new_p

            elif "Re-authenticate" in action:
                print()
                _rocket_auth(p["username"])

            elif "Delete" in action:
                if _gconfirm(f"Delete profile {p.get('display_name', p['username'])}?"):
                    state["profiles"].pop(p_idx)
                    _gprint("  Profile deleted.",
                            fg=_G_YELLOW, padding="0 2", margin="0 1")
                    time.sleep(0.6)

    # ── Save ──────────────────────────────────────────────────────────────────
    os.system("cls" if os.name == "nt" else "clear")
    sys.stdout.write("\n  " + _ansi_col(240, "  Saving…", bold=True) + "\n\n")
    sys.stdout.flush()
    _save_credentials({
        "CS_TENANT_ID":                    state["tenant"],
        "CS_CLIENT_ID":                    state["client"],
        "CS_AGENT_APP_ID":                 state["agent_app"],
        "CS_DIRECTLINE_SECRET":            state["secret"],
        "CS_TOKEN_ENDPOINT":               "",
        "CS_TOKEN_ENDPOINT_REQUIRES_AUTH": "false",
    })
    _write_profiles(state["profiles"])

    if _screen1_mode:
        # Called as the first-page screen — just save and return to the run flow.
        return

    _ok_line("Credentials saved to Windows Credential Manager.")

    # ── Auth profiles that still need it ──────────────────────────────────
    # Deduplicate by username — multiple profiles can share the same M365 account
    needs_auth = list(dict.fromkeys(
        p["username"] for p in state["profiles"]
        if not is_token_valid(load_token(p["username"]) or {})
    ))
    if needs_auth:
        print()
        _gprint(
            f"  {len(needs_auth)} profile(s) need sign-in.\n"
            "  Open the URL shown below and enter the code in a browser.",
            border="rounded", fg=_G_YELLOW,
            border_fg=_G_YELLOW, padding="1 2", margin="0 1",
        )
        print()
        if len(needs_auth) > 1 and _gum_ok():
            chosen = _gchoose_multi(
                *needs_auth,
                header="\n  Select profiles to authenticate now (Space = toggle, Enter = confirm):\n",
                height=min(len(needs_auth) + 4, 14),
            )
            needs_auth = chosen if chosen else needs_auth
        for username in needs_auth:
            if not _gconfirm(f"Authenticate {username} now?", default=True):
                continue
            _rocket_auth(username)

    _celebrate("  ✦  SETUP COMPLETE!  ALL SYSTEMS READY  ✦")


# ── Locust web UI extension ───────────────────────────────────────────────────

@events.init.add_listener
def _on_locust_init_ui(environment, **kwargs):
    if not hasattr(environment, "web_ui") or environment.web_ui is None:
        return

    app = environment.web_ui.app

    @app.route("/cs-config", methods=["POST"])
    def set_cs_config():
        data = request.get_json(force=True, silent=True) or {}
        test_config["response_timeout"] = max(15.0, float(data.get("response_timeout", 30)))
        test_config["think_min"]      = int(data.get("think_min", 30))
        test_config["think_max"]      = int(data.get("think_max", 60))
        test_config["p95_target_ms"]  = int(data.get("p95_target_ms", 2000))
        test_config["max_error_rate"] = float(data.get("max_error_rate", 0.5))
        if data.get("dl_secret"):
            os.environ["CS_DIRECTLINE_SECRET"] = data["dl_secret"]
        if data.get("token_endpoint"):
            os.environ["CS_TOKEN_ENDPOINT"] = data["token_endpoint"]
        return jsonify({"status": "ok", "config": test_config})

    @app.route("/cs-profiles", methods=["GET"])
    def get_profiles():
        return jsonify({"profiles": [r["username"] for r in load_profiles()]})

    profiles_list = load_profiles()
    profile_opts  = "".join(f'<option value="{p["username"]}">{p["username"]}</option>' for p in profiles_list)
    dl_secret_val = _load_credential("CS_DIRECTLINE_SECRET")
    token_ep_val  = _load_credential("CS_TOKEN_ENDPOINT")

    custom_html = f"""
<style>
  #cs-config-panel {{
    background: #f8f9fa; border: 1px solid #dee2e6; border-radius: 6px;
    padding: 20px 24px; margin-bottom: 20px; font-family: inherit;
  }}
  #cs-config-panel h3 {{
    margin-top: 0; margin-bottom: 16px; font-size: 15px; color: #333;
    border-bottom: 1px solid #dee2e6; padding-bottom: 8px;
  }}
  .cs-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px 24px; }}
  .cs-field label {{ display: block; font-size: 13px; font-weight: 600; color: #555; margin-bottom: 4px; }}
  .cs-field input, .cs-field select {{
    width: 100%; padding: 6px 8px; border: 1px solid #ced4da;
    border-radius: 4px; font-size: 13px; box-sizing: border-box;
  }}
  .cs-hint {{ font-size: 11px; color: #888; margin-top: 2px; }}
  .cs-section-title {{
    font-size: 12px; font-weight: 700; text-transform: uppercase;
    color: #888; margin: 14px 0 8px; letter-spacing: 0.5px;
  }}
</style>
<div id="cs-config-panel">
  <h3>Copilot Studio Test Configuration</h3>
  <div class="cs-section-title">DirectLine Connection</div>
  <div class="cs-grid">
    <div class="cs-field">
      <label>DirectLine Secret</label>
      <input type="password" id="cs-dl-secret" value="{dl_secret_val}" placeholder="From Credential Manager if blank">
      <div class="cs-hint">Leave blank to use stored credential</div>
    </div>
    <div class="cs-field">
      <label>Token Endpoint URL</label>
      <input type="text" id="cs-token-endpoint" value="{token_ep_val}" placeholder="Or use Token Endpoint">
      <div class="cs-hint">Alternative to DirectLine Secret</div>
    </div>
  </div>
  <div class="cs-section-title">Timing</div>
  <div class="cs-grid">
    <div class="cs-field">
      <label>Response Timeout (seconds)</label>
      <input type="number" id="cs-response-timeout" value="30" min="15" max="300">
      <div class="cs-hint">Max wait for first bot reply (min 15s)</div>
    </div>
    <div class="cs-field">
      <label>Think Time Min (seconds)</label>
      <input type="number" id="cs-think-min" value="30" min="5" max="300">
      <div class="cs-hint">Official guidance: 30–60s between turns</div>
    </div>
    <div class="cs-field">
      <label>Think Time Max (seconds)</label>
      <input type="number" id="cs-think-max" value="60" min="5" max="300">
    </div>
  </div>
  <div class="cs-section-title">Success Criteria</div>
  <div class="cs-grid">
    <div class="cs-field">
      <label>95th Percentile Target (ms)</label>
      <input type="number" id="cs-p95" value="2000" min="100">
      <div class="cs-hint">Baseline: 2000ms per CS guidance</div>
    </div>
    <div class="cs-field">
      <label>Max Error Rate (%)</label>
      <input type="number" id="cs-error-rate" value="0.5" min="0" max="100" step="0.1">
      <div class="cs-hint">Baseline: 0.5% per CS guidance</div>
    </div>
  </div>
  <div class="cs-section-title">Profiles</div>
  <div class="cs-grid">
    <div class="cs-field">
      <label>Active Profile Set</label>
      <select id="cs-profiles">
        <option value="all">All profiles ({len(profiles_list)} loaded)</option>
        {profile_opts}
      </select>
      <div class="cs-hint">{len(profiles_list)} profile(s) found in profiles/profiles.json</div>
    </div>
  </div>
  <div style="margin-top:14px;padding:10px 12px;background:#fff3cd;border:1px solid #ffc107;border-radius:4px;font-size:12px;color:#856404;">
    <strong>Sign-in:</strong> If any profile needs authentication, a sign-in prompt will appear
    in the terminal. Open the URL shown there and enter the code.
  </div>
</div>
<script>
(function() {{
  document.addEventListener('DOMContentLoaded', function() {{
    var startForm = document.querySelector('form[action*="swarm"]') ||
                    document.getElementById('start-form') ||
                    document.querySelector('form');
    if (!startForm) return;
    startForm.addEventListener('submit', function(e) {{
      e.preventDefault(); e.stopImmediatePropagation();
      var config = {{
        dl_secret:      document.getElementById('cs-dl-secret').value,
        token_endpoint: document.getElementById('cs-token-endpoint').value,
        response_timeout: parseFloat(document.getElementById('cs-response-timeout').value),
        think_min:      parseInt(document.getElementById('cs-think-min').value),
        think_max:      parseInt(document.getElementById('cs-think-max').value),
        p95_target_ms:  parseInt(document.getElementById('cs-p95').value),
        max_error_rate: parseFloat(document.getElementById('cs-error-rate').value),
      }};
      fetch('/cs-config', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(config)
      }}).then(function() {{ startForm.submit(); }})
        .catch(function() {{ startForm.submit(); }});
    }}, true);
  }});
}})();
</script>
"""

    @environment.web_ui.app.after_request
    def inject_cs_panel(response):
        if (
            response.content_type.startswith("text/html")
            and (b'id="start-form"' in response.data or b"new-test" in response.data)
        ):
            html = response.get_data(as_text=True)
            html = html.replace('<div class="container">', '<div class="container">' + custom_html, 1)
            response.set_data(html)
        return response


# ── Locust event hooks ────────────────────────────────────────────────────────

@events.init.add_listener
def _on_locust_init(environment, **kwargs):
    if os.environ.get("CS_SETUP_DONE") == "1":
        return  # python run.py already ran the full startup sequence
    profiles = load_profiles()
    run_startup_sequence(environment, profiles)


# ── Per-request detail log ────────────────────────────────────────────────────
# Writes one CSV row per bot reply: timestamp, profile, scenario,
# conversation_id, utterance, response_ms, timed_out.
# File: report/detail_YYYYMMDD_HHMMSS.csv  (new file per test run)

class _CsvWriter:
    """Background-thread CSV writer — non-blocking puts from Locust greenlets."""

    _COLS = [
        "event_category", "event", "timestamp", "elapsed_s", "user_count",
        "spawn_num", "profile", "conversation_id",
        "utterance_num", "scenario", "utterance", "bot_response",
        "utterance_sent_at", "response_received_at", "response_ms", "timed_out",
        "icon", "message",
    ]

    def __init__(self) -> None:
        self._q: _queue.Queue = _queue.Queue()
        self.log_path: Path | None = None
        self._thread: threading.Thread | None = None

    def start(self, log_path: Path) -> None:
        self.log_path = log_path
        self._thread = threading.Thread(target=self._run, name="csv-writer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._q.put(None)   # sentinel — drain then exit
        if self._thread:
            self._thread.join(timeout=8)

    def write(self, row: list) -> None:
        self._q.put(row)

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                try:
                    while True:
                        item2 = self._q.get_nowait()
                        if item2 is not None:
                            self._flush(item2)
                except _queue.Empty:
                    pass
                return
            self._flush(item)

    def _flush(self, row: list) -> None:
        if self.log_path:
            with open(self.log_path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(row)


_csv_writer = _CsvWriter()


def _init_run_log() -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = REPORT_DIR / f"run_log_{ts}.csv"
    with open(log_path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(_CsvWriter._COLS)
    _csv_writer.start(log_path)
    log.info("Run log → %s", log_path)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds")


def _elapsed() -> float:
    if _run_state.dashboard is not None:
        return round(time.time() - _run_state.dashboard.start_time, 1)
    return 0.0


def _log_utterance(spawn_num: int, profile: str, utterance_num: int, scenario: str,
                   conv_id: str, utterance: str, bot_response: str,
                   send_time: float, response_ms: float, timed_out: bool,
                   user_count: int = 0) -> None:
    if not _csv_writer.log_path:
        return
    sent_at     = datetime.fromtimestamp(send_time, tz=timezone.utc).isoformat(timespec="milliseconds")
    received_at = datetime.fromtimestamp(send_time + response_ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds")
    _csv_writer.write([
        "USER", "UTTERANCE", sent_at, _elapsed(), user_count,
        spawn_num, profile, conv_id,
        utterance_num, scenario, utterance, bot_response,
        sent_at, received_at, f"{response_ms:.0f}", "1" if timed_out else "0",
        "", "",
    ])


def _log_user_spawn(spawn_num: int, profile: str, conv_id: str, user_count: int) -> None:
    if _run_state.dashboard is not None:
        _run_state.dashboard.on_event("→", f"User {spawn_num} spawned — {profile}")
    if not _csv_writer.log_path:
        return
    _csv_writer.write([
        "USER", "SPAWNED", _now_iso(), _elapsed(), user_count,
        spawn_num, profile, conv_id,
        "", "", "", "", "", "", "", "",
        "", "",
    ])


def _log_user_exit(spawn_num: int, profile: str, conv_id: str, user_count: int) -> None:
    if _run_state.dashboard is not None:
        _run_state.dashboard.on_event("■", f"User {spawn_num} exited — {profile}")
    if not _csv_writer.log_path:
        return
    _csv_writer.write([
        "USER", "EXITED", _now_iso(), _elapsed(), user_count,
        spawn_num, profile, conv_id,
        "", "", "", "", "", "", "", "",
        "", "",
    ])


def _log_event(icon: str, event_type: str, message: str) -> None:
    if not _csv_writer.log_path:
        return
    ramp_window = 0
    if _run_state.dashboard is not None:
        with _run_state.dashboard._lock:
            ramp_window = _run_state.dashboard._cur_ramp_idx + 1
    _csv_writer.write([
        "SYSTEM", event_type, _now_iso(), _elapsed(), ramp_window,
        "", "", "",
        "", "", "", "", "", "", "", "",
        icon, message,
    ])


@events.test_start.add_listener
def _on_test_start(environment, **kwargs):
    _init_run_log()
    if "--headless" not in sys.argv and "-headless" not in sys.argv:
        return
    if not _user_auth_required():
        print("\n[Auth] No AAD auth required — proceeding.\n")
        return
    profiles = load_profiles()
    for profile in profiles:
        token_data = load_token(profile["username"])
        if not token_data or not is_token_valid(token_data):
            print(f"\n[Auth] No valid token for {profile['username']}."
                  " Pre-authenticate all profiles before running headless.\n")
            environment.runner.quit()
            return
    print("\n[Auth] All profiles have valid tokens — proceeding.\n")


# ── Locust User classes ───────────────────────────────────────────────────────

_profiles_list = load_profiles()
_csv_files     = sorted(UTTERANCES_DIR.glob("*.csv"))


def _load_utterances(path: Path) -> list[str]:
    with open(path, newline="") as f:
        rows = [r["utterance"] for r in csv.DictReader(f)]
    if not rows:
        raise RuntimeError(f"Utterances file is empty: {path}")
    return rows


def _fire_metric(environment, name: str, latency_ms: float, error: Exception = None):
    environment.events.request.fire(
        request_type="CopilotStudio", name=name,
        response_time=latency_ms, response_length=0, exception=error,
    )


def _retry_call(fn, attempts: int = 3, base_delay: float = 1.0):
    """Call fn() up to `attempts` times with exponential back-off + jitter (gevent-safe)."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if i == attempts - 1:
                raise
            delay = base_delay * (2 ** i) + random.uniform(0, 1)
            gevent.sleep(delay)


def _err_reason(e: Exception, max_len: int = 70) -> str:
    """Extract a short human-readable reason from an exception for EVENTS feed."""
    if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
        reason = f"HTTP {e.response.status_code}"
        try:
            body = e.response.json()
            msg  = body.get("message") or body.get("error", {}).get("message", "")
        except Exception:
            msg = (e.response.text or "").strip()
        if msg:
            msg = msg.replace("\n", " ")[:max_len]
            reason += f" — {msg}"
        return reason
    msg = str(e).strip().replace("\n", " ")
    if not msg or msg == type(e).__name__:
        return type(e).__name__
    return msg[:max_len]


def _trip_circuit():
    _run_state.circuit_open_until = time.time() + 60.0
    if _run_state.dashboard is not None:
        _run_state.dashboard.on_event("⚡", "429 rate limit hit — circuit open for 60s")
        _run_state.dashboard.on_429()
    _log_event("⚡", "rate_limit", "429 rate limit hit — circuit open for 60s")
    log.warning("Circuit breaker tripped — all users pausing 60s")

def _is_circuit_open() -> bool:
    return time.time() < _run_state.circuit_open_until


class _CpuWarnHandler(logging.Handler):
    def emit(self, record):
        if "CPU usage above" in self.format(record):
            _run_state.cpu_warn_ts = time.time()
            if _run_state.dashboard is not None:
                _run_state.dashboard.on_event("⚠", "CPU >90% — latency readings may be distorted")
            _log_event("⚠", "cpu_warn", "CPU >90% — latency readings may be distorted")

_cpu_warn_handler = _CpuWarnHandler()
_cpu_warn_handler.setLevel(logging.WARNING)
logging.getLogger("locust.runners").addHandler(_cpu_warn_handler)


# ── Transport strategy ────────────────────────────────────────────────────────
# _WsTransport and _HttpTransport encapsulate the stream-level differences
# so CopilotBaseUser doesn't need to know which wire it's talking over.

class _WsTransport:
    """WebSocket stream transport for DirectLine."""

    def __init__(self) -> None:
        self._ws = None
        self._opened_at: float = 0.0

    def open(self, conversation: "Conversation", environment) -> None:
        try:
            self._ws = _retry_call(lambda: open_websocket(conversation.stream_url))
            self._opened_at = time.time()
        except Exception as e:
            log.error("WebSocket open failed after retries: %s", e)
            _fire_metric(environment, "Open WebSocket", 0, error=e)
            _reason = _err_reason(e)
            if _run_state.dashboard is not None:
                _run_state.dashboard.on_event("✗", f"WS open failed — {_reason}")
            _log_event("✗", "ws_open_failed", f"WS open failed — {_reason}")
            raise StopUser()

    def close(self) -> None:
        if self._ws:
            close_websocket(self._ws)
            self._ws = None

    def needs_refresh(self, response_timeout: float) -> bool:
        threshold = max(10.0, 60.0 - response_timeout - 5.0)
        return time.time() - self._opened_at > threshold

    def refresh(self, conversation: "Conversation", environment) -> bool:
        try:
            self._ws = _retry_call(lambda: refresh_stream(conversation))
            self._opened_at = time.time()
            return True
        except Exception as e:
            _fire_metric(environment, "Refresh Stream", 0, error=e)
            return False

    def read(self, activity_id: str, response_timeout: float,
             conversation: "Conversation", aad_token: Optional[str],
             send_mono: float) -> "Response":
        return read_response(
            self._ws, activity_id,
            response_timeout=response_timeout,
            conversation=conversation,
            aad_token=aad_token,
            send_mono=send_mono,
        )


class _HttpTransport:
    """HTTP polling transport for DirectLine. Stateless — no stream to manage."""

    def open(self, conversation, environment) -> None:
        pass

    def close(self) -> None:
        pass

    def needs_refresh(self, response_timeout: float) -> bool:
        return False

    def refresh(self, conversation, environment) -> bool:
        return True

    def read(self, activity_id: str, response_timeout: float,
             conversation, aad_token: Optional[str],
             send_mono: float) -> "Response":
        return read_response_http(
            conversation, activity_id,
            response_timeout=response_timeout,
            aad_token=aad_token,
            send_mono=send_mono,
        )


class CopilotBaseUser(User):
    abstract       = True
    utterances     = []   # class-level list, set per subclass — read-only
    scenario_name  = ""
    fixed_profile  = {}   # pinned at class creation time
    _transport_cls = _WsTransport

    def on_start(self):
        self.profile     = self.__class__.fixed_profile
        self._transport  = self.__class__._transport_cls()
        self.conversation          = None
        self._idx                  = 0
        self._consecutive_timeouts = 0
        with _spawn_lock:
            key = self.__class__.__name__
            _spawn_counters[key] = _spawn_counters.get(key, 0) + 1
            self._spawn_num = _spawn_counters[key]
            if _run_state.mode == "Pipeline":
                _run_state.pipeline_spawns_total += 1
                self._is_pipeline_original = (_run_state.pipeline_spawns_total <= test_config.get("users", 999999))
            else:
                self._is_pipeline_original = True
        if not self._is_pipeline_original:
            return
        self._open_conversation()
        _log_user_spawn(
            self._spawn_num,
            self.profile.get("display_name", self.profile.get("username", "?")),
            self.conversation.id if self.conversation else "",
            getattr(self.environment.runner, "user_count", 0),
        )

    def on_stop(self):
        if _run_state.mode == "Pipeline" and getattr(self, '_is_pipeline_original', True):
            _run_state.pipeline_done += 1
        if not getattr(self, '_is_pipeline_original', True):
            return
        _log_user_exit(
            self._spawn_num,
            self.profile.get("display_name", self.profile.get("username", "?")),
            self.conversation.id if self.conversation else "",
            getattr(self.environment.runner, "user_count", 0),
        )
        self._transport.close()

    def _open_conversation(self):
        """Opens a fresh DirectLine conversation. Replaces any existing stream."""
        self._transport.close()
        self._consecutive_timeouts = 0

        _who = self.profile.get("display_name", self.profile.get("username", "?"))
        self.aad_token = None
        if _user_auth_required():
            try:
                from gevent import Timeout as _GTimeout
                with _GTimeout(30, RuntimeError("get_valid_token timed out after 30s")):
                    self.aad_token = get_valid_token(self.profile["username"])
            except Exception as e:
                log.error("Auth failed for %s: %s", self.profile["username"], e)
                _reason = _err_reason(e)
                if _run_state.dashboard is not None:
                    _run_state.dashboard.on_event("✗", f"Auth failed — {_who} — {_reason}")
                _log_event("✗", "auth_error", f"Auth failed — {_who} — {_reason}")
                raise StopUser()

        try:
            from gevent import Timeout as _GTimeout
            with _GTimeout(15, RuntimeError("fetch_directline_token timed out after 15s")):
                dl_token = _retry_call(lambda: fetch_directline_token(self.aad_token))
        except Exception as e:
            log.error("DirectLine token fetch failed after retries: %s", e)
            _fire_metric(self.environment, "Fetch Token", 0, error=e)
            _reason = _err_reason(e)
            if _run_state.dashboard is not None:
                _run_state.dashboard.on_event("✗", f"DL token fetch failed — {_who} — {_reason}")
            _log_event("✗", "token_error", f"DL token fetch failed — {_who} — {_reason}")
            raise StopUser()

        try:
            from gevent import Timeout as _GTimeout
            with _GTimeout(15, RuntimeError("start_conversation timed out after 15s")):
                self.conversation = _retry_call(lambda: start_conversation(dl_token))
        except Exception as e:
            log.error("Start conversation failed after retries: %s", e)
            _fire_metric(self.environment, "Start Conversation", 0, error=e)
            _reason = _err_reason(e)
            if _run_state.dashboard is not None:
                _run_state.dashboard.on_event("✗", f"Start conversation failed — {_who} — {_reason}")
            _log_event("✗", "conv_error", f"Start conversation failed — {_who} — {_reason}")
            raise StopUser()

        self._idx = 0
        self._transport.open(self.conversation, self.environment)

    def _refresh_stream(self):
        """Refresh stream — same conversation, bot context preserved. Falls back to new conversation."""
        if not self._transport.refresh(self.conversation, self.environment):
            self._open_conversation()

    def _send_and_measure(self):
        if self._idx >= len(self.utterances):
            # Concurrent: loop same conversation unless ramp-down flag is set
            if _run_state.mode == "Concurrent" and _run_state.phase != "RAMP_DOWN":
                self._idx = 0
            else:
                raise StopUser()
        cur = self._idx
        utterance = self.utterances[cur]

        if _is_circuit_open():
            gevent.sleep(1)
            return   # retry same utterance once the circuit closes — index not advanced

        _rt = test_config.get("response_timeout", 30.0)
        if self._transport.needs_refresh(_rt):
            self._refresh_stream()

        _base_label   = self.profile.get("display_name", self.profile.get("username", ""))
        profile_label = f"{_base_label} #{self._spawn_num}"
        _metric_name  = f"Copilot Response — {_base_label} · {self.scenario_name}"

        for _attempt in range(2):
            try:
                activity_id, send_time, send_mono = send_utterance(self.conversation, utterance)
                break
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 429:
                    _trip_circuit()
                    _fire_metric(self.environment, "Send Utterance", 0, error=e)
                    gevent.sleep(1)
                    return
                if _attempt == 1:
                    log.error("Send utterance failed: %s", e)
                    _fire_metric(self.environment, "Send Utterance", 0, error=e)
                    _reason = _err_reason(e)
                    if _run_state.dashboard is not None:
                        _run_state.dashboard.on_event("✗", f"Send failed — {profile_label} — {_reason}")
                    _log_event("✗", "send_error", f"Send failed — {profile_label} — {_reason}")
                    raise StopUser()
                gevent.sleep(1)
            except Exception as e:
                if _attempt == 1:
                    log.error("Send utterance failed: %s", e)
                    _fire_metric(self.environment, "Send Utterance", 0, error=e)
                    _reason = _err_reason(e)
                    if _run_state.dashboard is not None:
                        _run_state.dashboard.on_event("✗", f"Send failed — {profile_label} — {_reason}")
                    _log_event("✗", "send_error", f"Send failed — {profile_label} — {_reason}")
                    raise StopUser()
                gevent.sleep(1)

        # Utterance sent successfully — advance the index now so a retry or a
        # loop-around won't re-send it. The circuit-open and 429 returns above
        # leave the index unchanged so the same utterance is retried later.
        self._idx = cur + 1

        response_timeout = max(15.0, _rt)
        try:
            response = self._transport.read(
                activity_id, response_timeout,
                self.conversation, self.aad_token, send_mono,
            )
        except Exception as e:
            log.error("Read response failed: %s", e)
            _fire_metric(self.environment, "Copilot Response", 0, error=e)
            _reason = _err_reason(e)
            if _run_state.dashboard is not None:
                _run_state.dashboard.on_event("✗", f"Read failed — {profile_label} — {_reason}")
            _log_event("✗", "read_error", f"Read failed — {profile_label} — {_reason}")
            raise StopUser()

        _uc = _run_state.dashboard._current_users if _run_state.dashboard is not None else 0
        if response.timed_out:
            if response.ws_closed:
                # DirectLine closed the stream — not a bot failure. Reconnect silently.
                self._refresh_stream()
                return
            _log_utterance(self._spawn_num, profile_label, self._idx, self.scenario_name,
                           self.conversation.id, utterance, "",
                           send_time, response.latency_ms, timed_out=True,
                           user_count=_uc)
            _fire_metric(self.environment, _metric_name, response.latency_ms,
                         error=Exception("No bot reply received"))
            if _run_state.dashboard is not None:
                _run_state.dashboard.on_utterance(
                    utterance, self.scenario_name, id(self), profile_label,
                    self._idx, len(self.utterances), response.latency_ms, True)
            # Fire a P1 event for every timeout — not just after 2 consecutive
            utt_short = (utterance[:35] + "…") if len(utterance) > 36 else utterance
            _to_msg = (f"T/O — {profile_label} — no reply in {int(response.latency_ms/1000)}s"
                       f" — \"{utt_short}\"")
            if _run_state.dashboard is not None:
                _run_state.dashboard.on_event("✗", _to_msg)
            _log_event("✗", "timeout", _to_msg)
            self._consecutive_timeouts += 1
            if self._consecutive_timeouts >= 2:
                if _run_state.phase == "RAMP_DOWN" or _run_state.mode == "Pipeline":
                    # Ramp-down: don't restart, exit cleanly so count drops naturally
                    # Pipeline: each user runs once, failure means stop
                    raise StopUser()
                if _run_state.dashboard is not None:
                    _run_state.dashboard.on_event("✗", f"2 consecutive T/Os — restarting — {profile_label}")
                _log_event("✗", "timeout_consecutive", f"2 consecutive T/Os — restarting — {profile_label}")
                self._open_conversation()   # fetches fresh token — handles expiry
            return

        bot_text = " | ".join(
            a.get("text", "").strip()
            for a in response.activities
            if a.get("text", "").strip()
        )[:500]
        _log_utterance(self._spawn_num, profile_label, self._idx, self.scenario_name,
                       self.conversation.id, utterance, bot_text,
                       send_time, response.latency_ms, timed_out=False,
                       user_count=_uc)
        self._consecutive_timeouts = 0
        _fire_metric(self.environment, _metric_name, response.latency_ms)
        if _run_state.dashboard is not None:
            _run_state.dashboard.on_utterance(
                utterance, self.scenario_name, id(self), profile_label,
                self._idx, len(self.utterances), response.latency_ms, False,
                bot_response=bot_text)
        # Pipeline: no think-time after the last utterance — exit immediately
        if _run_state.mode == "Pipeline" and self._idx >= len(self.utterances):
            raise StopUser()
        gevent.sleep(random.randint(test_config.get("think_min", 30), test_config.get("think_max", 60)))


# Dynamically create one User class per CSV file found in utterances/.
# Each class is pinned to one profile: csv[i] → profile[i % len(profiles)].
# Drop any CSV into utterances/ and it becomes a Locust scenario automatically.
def _make_user_class(class_name: str, utterances: list[str], scenario: str, profile: dict) -> type:
    def send(self):
        if not getattr(self, '_is_pipeline_original', True):
            gevent.sleep(0.5)
            return
        self._send_and_measure()
    send.__name__ = "send"
    base = CopilotHttpUser if test_config["transport"] == "http" else CopilotBaseUser
    return type(class_name, (base,), {
        "utterances":    utterances,
        "scenario_name": scenario,
        "fixed_profile": profile,
        "weight":        1,
        "send":          task(send),
    })


# ── HTTP polling transport (alternative to WebSocket) ────────────────────────
# Polls GET /v3/directline/conversations/{id}/activities?watermark=...
# until the bot replies to the given activity_id.
# SSO/OAuthCard handling mirrors the WebSocket implementation in read_response().

def read_response_http(
    conversation: Conversation,
    activity_id: str,
    response_timeout: float = 30.0,
    aad_token: Optional[str] = None,
    send_mono: Optional[float] = None,
) -> Response:
    matched: list[dict]       = []
    last_match_time           = None
    start_time                = send_mono if send_mono is not None else time.monotonic()
    watermark: Optional[str]  = None

    while time.monotonic() - start_time < response_timeout:
        url = f"{DIRECTLINE_BASE}/v3/directline/conversations/{conversation.id}/activities"
        if watermark is not None:
            url += f"?watermark={watermark}"
        try:
            r = _session.get(
                url,
                headers={"Authorization": f"Bearer {conversation.token}"},
                timeout=5,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.debug("HTTP poll error: %s", e)
            gevent.sleep(0.5)
            continue

        watermark = data.get("watermark", watermark)

        for activity in data.get("activities", []):
            # SSO: bot sends signin/tokenExchange invoke
            if (activity.get("type") == "invoke"
                    and activity.get("name") == "signin/tokenExchange"
                    and aad_token):
                val = activity.get("value", {})
                try:
                    send_token_exchange(conversation, val.get("id", ""),
                                        val.get("connectionName", ""), aad_token)
                except Exception as e:
                    log.warning("Token exchange (invoke) failed: %s", e)
                continue

            # SSO: bot sends message with OAuthCard attachment
            if activity.get("type") == "message" and aad_token:
                for attach in activity.get("attachments", []):
                    if attach.get("contentType") == "application/vnd.microsoft.card.oauth":
                        content   = attach.get("content", {})
                        token_res = content.get("tokenExchangeResource", {})
                        if token_res:
                            try:
                                send_token_exchange(
                                    conversation,
                                    token_res.get("id", ""),
                                    content.get("connectionName", ""),
                                    aad_token,
                                )
                            except Exception as e:
                                log.warning("Token exchange (OAuthCard) failed: %s", e)
                        break
                else:
                    if (activity.get("from", {}).get("role") == "bot"
                            and activity.get("replyToId") == activity_id):
                        matched.append(activity)
                        last_match_time = time.monotonic()
                continue

            if (activity.get("type") == "message"
                    and activity.get("from", {}).get("role") == "bot"
                    and activity.get("replyToId") == activity_id):
                matched.append(activity)
                last_match_time = time.monotonic()

        if matched:
            break

        gevent.sleep(0.5)

    end_time   = last_match_time or time.monotonic()
    latency_ms = (end_time - start_time) * 1000
    return Response(activities=matched, latency_ms=latency_ms, timed_out=len(matched) == 0)


class CopilotHttpUser(CopilotBaseUser):
    """HTTP polling transport. Inherits all auth, logging, and metrics from CopilotBaseUser."""
    abstract = True
    _transport_cls = _HttpTransport


# Strategy: one Locust user class per (CSV × profile) combination.
# Every profile runs every scenario in parallel — Locust distributes the
# configured user count evenly across all classes (weight=1 each).
# Pinned profiles (profile["scenario"] == csv stem) are exclusive to that CSV.

_pinned     = {p["scenario"]: p for p in _profiles_list if p.get("scenario")}
_free       = [p for p in _profiles_list if not p.get("scenario")] or [{}]

for _csv in _csv_files:
    _scenario    = _csv.stem.replace("_", " ").title()
    _base_name   = "".join(w.capitalize() for w in _csv.stem.split("_")) + "User"
    _utterances  = _load_utterances(_csv)

    if _csv.stem in _pinned:
        # Pinned: exactly one profile for this CSV
        _profiles_for_csv = [_pinned[_csv.stem]]
    else:
        # All free profiles run this scenario
        _profiles_for_csv = _free

    for _pi, _profile in enumerate(_profiles_for_csv):
        # Unique class name: ScenarioUser if single profile, ScenarioUser_2 etc.
        _class_name = _base_name if _pi == 0 else f"{_base_name}_{_pi + 1}"
        globals()[_class_name] = _make_user_class(_class_name, _utterances, _scenario, _profile)


# ── Live dashboard ────────────────────────────────────────────────────────────

class _DashboardState:
    def __init__(self, target_users: int, p95_target: int, profile_map: dict):
        self._lock        = threading.RLock()   # reentrant: on_event called from within on_request
        self.target_users = target_users
        self.p95_target   = p95_target
        self.profile_map  = profile_map          # scenario → display_name
        self.start_time   = time.time()
        self._times:       dict[str, list[float]]         = {}
        self._tout:        dict[str, int]                 = {}
        self._ts:          list[tuple[float, float]]      = []
        self._scenario_ts: dict[str, list[tuple[float, float]]] = {}
        self._errs:        list[tuple[float, bool]]       = []
        self._utt_times:   dict[str, list[float]]         = {}
        self._utt_tout:    dict[str, int]                 = {}
        self._utt_response: dict[str, str]               = {}  # worst-instance bot reply per key
        self._utt_worst_ms: dict[str, float]             = {}  # running max latency per key (for _utt_response)
        self._users:       dict[int, dict]                = {}
        self.events        = collections.deque(maxlen=20)
        # Ramp tracking — one row per 60-second window, added when next window starts
        self._current_users:    int         = 0
        self._ramp_window:      float       = 60.0
        self._ramps_done:       list        = []   # finalized ramp dicts
        self._cur_ramp_ms:      list        = []   # response_ms in current window
        self._cur_ramp_tout:    int         = 0
        self._cur_ramp_429:     int         = 0
        self._cur_ramp_idx:     int         = 0    # which 60s window we're in
        self._cur_ramp_users:   int         = 0    # last user count seen this window

    def set_user_count(self, n: int):
        with self._lock:
            self._current_users = n

    def on_request(self, **kwargs):
        name      = kwargs.get("name", "")
        rt        = float(kwargs.get("response_time") or 0)
        exception = kwargs.get("exception")
        if not name.startswith("Copilot Response"):
            return
        scenario = name.split(" — ", 1)[1] if " — " in name else "Unknown"
        is_err   = exception is not None
        elapsed  = time.time() - self.start_time
        with self._lock:
            self._times.setdefault(scenario, [])
            self._tout.setdefault(scenario, 0)
            if is_err:
                self._tout[scenario] += 1
            else:
                self._times[scenario].append(rt)
            self._ts.append((elapsed, rt))
            self._scenario_ts.setdefault(scenario, []).append((elapsed, rt))
            self._errs.append((elapsed, is_err))
            # Ramp window tracking: finalize completed 60s windows on each new request
            ramp_idx = int(elapsed // self._ramp_window)
            while self._cur_ramp_idx < ramp_idx:
                if self._cur_ramp_ms or self._cur_ramp_tout:
                    count = len(self._cur_ramp_ms) + self._cur_ramp_tout
                    self._ramps_done.append({
                        "ramp":         len(self._ramps_done) + 1,
                        "users":        self._cur_ramp_users,
                        "requests":     count,
                        "rps":          round(count / self._ramp_window, 2),
                        "p50":          _pct(self._cur_ramp_ms, 0.50),
                        "p75":          _pct(self._cur_ramp_ms, 0.75),
                        "p85":          _pct(self._cur_ramp_ms, 0.85),
                        "p95":          _pct(self._cur_ramp_ms, 0.95),
                        "p99":          _pct(self._cur_ramp_ms, 0.99),
                        "timeouts":     self._cur_ramp_tout,
                        "rate_limited": self._cur_ramp_429,
                        "active":       False,
                    })
                self._cur_ramp_idx  += 1
                self._cur_ramp_ms    = []
                self._cur_ramp_tout  = 0
                self._cur_ramp_429   = 0
                self._cur_ramp_users = self._current_users
            self._cur_ramp_users = self._current_users
            if is_err:
                self._cur_ramp_tout += 1
            else:
                self._cur_ramp_ms.append(rt)

    def on_utterance(self, utterance: str, scenario: str, user_id: int,
                     display: str, idx: int, total: int,
                     response_ms: float, timed_out: bool, bot_response: str = ""):
        filled  = int((idx / max(1, total)) * 10)
        bar     = "█" * filled + "░" * (10 - filled)
        status  = "✗ timeout" if timed_out else f"✓ {int(response_ms)}ms"
        key = f"{display}||{scenario}||{utterance}"
        with self._lock:
            self._utt_times.setdefault(key, [])
            self._utt_tout.setdefault(key, 0)
            if timed_out:
                self._utt_tout[key] += 1
            else:
                self._utt_times[key].append(response_ms)
                # Keep the bot reply from the slowest call for this key.
                # Track the running max in O(1); the first call to hit the max
                # wins ties, so the stored reply is deterministic.
                if response_ms > self._utt_worst_ms.get(key, -1.0):
                    self._utt_worst_ms[key] = response_ms
                    self._utt_response[key] = bot_response
            self._users[user_id] = {"name": display, "idx": idx, "total": total}

    def on_event(self, icon: str, message: str):
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self.events.appendleft({
                "ts":    ts,
                "icon":  icon,
                "message": message,
                "ramp":  self._cur_ramp_idx + 1,
            })

    def on_429(self):
        with self._lock:
            self._cur_ramp_429 += 1

    def snapshot(self) -> dict:
        with self._lock:
            # Build ramp list: finalized rows + current in-progress row
            ramps = list(self._ramps_done)
            if self._cur_ramp_ms or self._cur_ramp_tout:
                count = len(self._cur_ramp_ms) + self._cur_ramp_tout
                elapsed_in_window = max(
                    (time.time() - self.start_time) - self._cur_ramp_idx * self._ramp_window,
                    1.0,
                )
                ramps.append({
                    "ramp":         len(self._ramps_done) + 1,
                    "users":        self._cur_ramp_users,
                    "requests":     count,
                    "rps":          round(count / elapsed_in_window, 2),
                    "p50":          _pct(self._cur_ramp_ms, 0.50),
                    "p75":          _pct(self._cur_ramp_ms, 0.75),
                    "p85":          _pct(self._cur_ramp_ms, 0.85),
                    "p95":          _pct(self._cur_ramp_ms, 0.95),
                    "p99":          _pct(self._cur_ramp_ms, 0.99),
                    "timeouts":     self._cur_ramp_tout,
                    "rate_limited": self._cur_ramp_429,
                    "active":       True,
                })
            return {
                "times":       {k: list(v) for k, v in self._times.items()},
                "tout":        dict(self._tout),
                "ts":          list(self._ts),
                "scenario_ts": {k: list(v) for k, v in self._scenario_ts.items()},
                "errs":        list(self._errs),
                "utt_times":    {k: list(v) for k, v in self._utt_times.items()},
                "utt_tout":     dict(self._utt_tout),
                "utt_response": dict(self._utt_response),
                "events":       list(self.events),
                "ramps":       ramps,
            }


def _pct(values: list, p: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    # Nearest-rank (lower) percentile — matches numpy/pandas method="lower"
    # and report.py._pct, so the live dashboard, HTML report, and AUDIT agree.
    return int(s[int(p * (len(s) - 1))])


def _fmt_s(ms) -> str:
    """Format a millisecond latency as seconds for display (e.g. 31450 -> '31.4').
    Percentiles/targets are stored in ms internally; only the display is converted."""
    try:
        return f"{float(ms) / 1000.0:.1f}"
    except (TypeError, ValueError):
        return str(ms)


# Reliability / shape thresholds for _latency_verdict. Validated against an
# adversarial suite (bimodal, time-trend, lone spike, low-n, degenerate inputs).
_LV_MIN_N      = 5      # below this, even the median is only indicative
_LV_TAIL_N     = 100    # need ~5 samples above p95 → n ≥ 5/0.05 before p95 is trusted
_LV_TIGHT_TAIL = 1.30   # p95/median ≤ this  → consistent
_LV_TIGHT_MAX  = 1.50   # worst/median ≤ this → no meaningful spike
_LV_BROAD_TAIL = 1.50   # p95/median ≥ this  → a real slow population (or 2nd mode)
_LV_SPIKE_MAX  = 2.00   # worst/median ≥ this (with a tight tail) → a lone outlier
_LV_TREND_R    = 1.30   # 2nd-half median vs 1st-half median ratio → a time trend


def _latency_verdict(times_ms: list, p95_target_ms: float = 0.0) -> dict:
    """Plain-English read on one row's response times (ms), in completion/time order.

    Answers 'which number should I trust, and is the tail one fluke or a pattern?'
    Uses percentile *ratios* (not outlier tests, which cannot see a balanced 2nd
    mode) plus a time-order trend check. Robust to low n, bimodal, trend, spike and
    degenerate inputs — see the adversarial cases the design was validated against.
    Returns display fields + a one-line verdict.
    """
    n = len(times_ms)
    if n == 0:
        return {"n": 0, "typ": 0, "p95": 0, "worst": 0, "tier": "none",
                "trend": None, "slow_frac": 0.0, "tail_reliable": False,
                "icon": " ", "verdict": "no measured replies (all timeouts/errors?) — nothing to report"}

    med   = _pct(times_ms, 0.50)
    p95   = _pct(times_ms, 0.95)
    worst = max(times_ms)
    d     = max(1, med)                      # guard median == 0 (sub-ms/degenerate)
    tail_r    = p95 / d
    max_r     = worst / d
    slow_frac = sum(1 for v in times_ms if v > med * 1.5) / n

    # Time trend: compare first-half vs second-half median (needs enough points).
    trend = None
    if n >= 20:
        h  = n // 2
        m1 = _pct(times_ms[:h], 0.50)
        m2 = _pct(times_ms[h:], 0.50)
        if   m2 >= m1 * _LV_TREND_R: trend = "rising"
        elif m1 >= m2 * _LV_TREND_R: trend = "falling"

    if   tail_r <= _LV_TIGHT_TAIL and max_r <= _LV_TIGHT_MAX: tier = "tight"
    elif tail_r >= _LV_BROAD_TAIL:                            tier = "broad"
    elif max_r  >= _LV_SPIKE_MAX:                             tier = "spike"
    else:                                                     tier = "skewed"

    tail_reliable = n >= _LV_TAIL_N
    low = not tail_reliable

    # A clear time trend overrides a broad/skewed shape (it's degradation, not a 2nd mode).
    if trend and tier in ("broad", "skewed"):
        h    = n // 2
        word = "slower over time (degrading)" if trend == "rising" else "faster over time (recovering)"
        sc   = (f"latency is trending {word} — "
                f"{_fmt_s(_pct(times_ms[:h], 0.50))}s→{_fmt_s(_pct(times_ms[h:], 0.50))}s.")
        tier = "trend-up" if trend == "rising" else "trend-down"
    elif tier == "tight":
        sc = "latency is consistent."
    elif tier == "spike":
        sc = (f"worst {_fmt_s(worst)}s — possibly a fluke at this sample size."
              if low else
              f"worst {_fmt_s(worst)}s = a few isolated slow replies; Typical unaffected.")
    elif tier == "broad":
        sc = (f"early sign ~{slow_frac * 100:.0f}% of replies slower (up to {_fmt_s(worst)}s) — confirm with more data."
              if low else
              f"~{slow_frac * 100:.0f}% of replies much slower (up to {_fmt_s(worst)}s) — a real slow group, not a fluke.")
    else:  # skewed
        sc = f"moderate spread (worst {_fmt_s(worst)}s)."

    # Health vs the configured p95 target — only trustworthy once the tail is reliable.
    hc = ""
    if p95_target_ms and tail_reliable and p95 > p95_target_ms:
        hc = f" ⚠ p95 over target {_fmt_s(p95_target_ms)}s."

    if n < _LV_MIN_N:
        verdict = f"only {n} sample{'' if n == 1 else 's'} — indicative only; Typical ~{_fmt_s(med)}s."
        icon    = "⚠"
    elif low:
        verdict = f"{n} samples — trust Typical {_fmt_s(med)}s; " + sc + hc
        icon    = "⚠" if (tier in ("broad", "trend-up") or hc) else "•"
    else:
        verdict = f"{n} samples — Tail p95 {_fmt_s(p95)}s reliable. " + sc + hc
        icon    = "⚠" if (tier in ("broad", "trend-up") or hc) else "✓"

    return {"n": n, "typ": med, "p95": p95, "worst": worst, "tier": tier,
            "trend": trend, "slow_frac": slow_frac, "tail_reliable": tail_reliable,
            "icon": icon, "verdict": verdict}


def _find_knee(values: list) -> int:
    """Return index of the knee point using perpendicular distance from the line start→end.
    Returns -1 if too few points to detect a knee."""
    n = len(values)
    if n < 3:
        return -1
    x0, y0 = 0.0, float(values[0])
    x1, y1 = float(n - 1), float(values[n - 1])
    dx, dy = x1 - x0, y1 - y0
    length = (dx * dx + dy * dy) ** 0.5
    if length == 0:
        return -1
    distances = [
        abs(dy * i - dx * float(values[i]) + x1 * y0 - y1 * x0) / length
        for i in range(n)
    ]
    return distances.index(max(distances))


def _sparkline(ts: list, width: int = 20, bucket_s: float = 600.0) -> str:
    if not ts:
        return "▁" * width
    blocks  = "▁▂▃▄▅▆▇█"
    max_t   = max(t for t, _ in ts)
    n       = max(1, int(max_t / bucket_s) + 1)
    buckets: list = [[] for _ in range(n)]
    for t, v in ts:
        buckets[min(int(t / bucket_s), n - 1)].append(v)
    vals = [_pct(b, 0.95) if b else 0 for b in buckets]
    mx   = max(vals) or 1
    line = "".join(blocks[min(int(v / mx * 7), 7)] for v in vals)
    return line[-width:].ljust(width, "▁")


def _error_sparkline(errs: list, width: int = 20, bucket_s: float = 600.0) -> str:
    """Count errors per time bucket — correct for low error rates where p95 always returns 0."""
    if not errs:
        return "▁" * width
    blocks = "▁▂▃▄▅▆▇█"
    max_t  = max(t for t, _ in errs)
    n      = max(1, int(max_t / bucket_s) + 1)
    counts = [0] * n
    for t, e in errs:
        if e:
            counts[min(int(t / bucket_s), n - 1)] += 1
    mx   = max(counts) or 1
    line = "".join(blocks[min(int(c / mx * 7), 7)] for c in counts)
    return line[-width:].ljust(width, "▁")


def _compute_dashboard_vm(snap: dict, runner, params: dict, state: "_DashboardState") -> dict:
    """Pure metrics computation. No Rich objects. Returns a view-model dict for _render_dashboard."""
    elapsed  = int(time.time() - state.start_time)
    target   = params["users"]
    curr     = getattr(runner, "user_count", 0)
    p95_tgt  = state.p95_target

    all_times = [v for vlist in snap["times"].values() for v in vlist]
    all_tout  = sum(snap["tout"].values())
    all_reqs  = sum(len(v) for v in snap["times"].values()) + all_tout
    all_p50   = _pct(all_times, 0.50)
    all_p75   = _pct(all_times, 0.75)
    all_p85   = _pct(all_times, 0.85)
    all_p95   = _pct(all_times, 0.95)
    all_p99   = _pct(all_times, 0.99)
    all_worst = max(all_times) if all_times else 0
    all_vdict = _latency_verdict(all_times, p95_tgt)

    err_rate = (all_tout / max(1, all_reqs)) * 100
    recent   = [t for t, _ in snap["ts"] if t > elapsed - 30]
    rps      = len(recent) / min(30.0, max(1.0, float(elapsed)))

    _rstate = str(getattr(runner, "state", "")).lower()
    if _rstate in ("stopping", "stopped", "cleanup"):
        health, hcol = "● STOPPED", "white"
    elif curr == 0 and all_reqs == 0:
        health, hcol = "● STARTING", "yellow"
    else:
        health, hcol = "● RUNNING", "green"

    filled    = int((curr / max(1, target)) * 10)
    spawn_bar = "▓" * filled + "░" * (10 - filled)
    p95_fill  = min(int((all_p95 / max(1, p95_tgt)) * 10), 10)
    p95_bar   = "█" * p95_fill + "░" * (10 - p95_fill)
    p95_warn  = " ⚠" if all_p95 > p95_tgt else ""

    _phase    = _run_state.phase
    _mode     = _run_state.mode
    _finished = curr == 0 and _run_state.spawning_complete
    if _finished:
        phase_label, phase_style = "FINISHED ",  "bold cyan"
        spawn_bar = "▓" * 10
    elif _mode == "Pipeline":
        phase_label, phase_style = "RUNNING  ",  "bold green"
    elif _phase == "RAMP_DOWN":
        phase_label, phase_style = "RAMP DOWN",  "bold yellow"
    elif _phase == "AT_PEAK":
        phase_label, phase_style = "AT PEAK  ",  "bold green"
    else:
        phase_label, phase_style = "RAMPING UP", "bold white"

    # Ramp data
    ramps      = snap.get("ramps", [])
    _finalized = [r for r in ramps if not r.get("active")]
    _active_r  = [r for r in ramps if r.get("active")]
    ramps_disp = (_finalized + _active_r)[-5:]
    events_by_ramp: dict = {}
    for _ev in snap.get("events", []):
        if isinstance(_ev, dict):
            events_by_ramp.setdefault(_ev.get("ramp", 0), []).append(_ev)
    knee_ramp = -1
    if len(_finalized) >= 3:
        _knee_idx = _find_knee([r["rps"] for r in _finalized])
        if _knee_idx >= 0 and _finalized[_knee_idx]["rps"] >= _DIRECTLINE_RPS_CAP * 0.75:
            knee_ramp = _finalized[_knee_idx]["ramp"]

    # Per-scenario rows for profile table
    scenario_rows = []
    for scenario, times in sorted(snap["times"].items()):
        tout  = snap["tout"].get(scenario, 0)
        reqs  = len(times) + tout
        p50_v = _pct(times, 0.50)
        p95_v = _pct(times, 0.95)
        worst_v = max(times) if times else 0
        vdict = _latency_verdict(times, p95_tgt)
        rcol  = "bold red" if p95_v > p95_tgt else "white"
        disp  = state.profile_map.get(scenario, "")
        label = f"{disp} · {scenario}" if disp else scenario
        spark = _sparkline(snap["scenario_ts"].get(scenario, []))
        scenario_rows.append((label, reqs, p50_v, p95_v, worst_v, tout, rcol, spark, vdict))

    all_spark = _sparkline(snap["ts"])
    err_spark = _error_sparkline(snap["errs"])

    # Utterance merging
    _utt_merged: dict = {}
    for key, times in snap["utt_times"].items():
        parts = key.split("||", 2)
        if len(parts) == 3:
            profile_k, scenario_k, utt = parts
        elif len(parts) == 2:
            scenario_k, utt = parts
            profile_k = state.profile_map.get(scenario_k, scenario_k)
        else:
            profile_k, scenario_k, utt = "", "", key
        tout_u = snap["utt_tout"].get(key, 0)
        mk = (scenario_k, utt)
        if mk not in _utt_merged:
            _utt_merged[mk] = {"times": [], "tout": 0,
                                "worst_profile": profile_k, "worst_p95": 0, "worst_response": ""}
        _utt_merged[mk]["times"].extend(times)
        _utt_merged[mk]["tout"] += tout_u
        inst_p95 = _pct(times, 0.95)
        if inst_p95 > _utt_merged[mk]["worst_p95"]:
            _utt_merged[mk]["worst_p95"]     = inst_p95
            _utt_merged[mk]["worst_profile"] = profile_k
            _utt_merged[mk]["worst_response"] = snap.get("utt_response", {}).get(key, "")
    utt_data = [
        (utt, d["worst_profile"], d["times"], d["tout"], d["worst_response"])
        for (_sc, utt), d in _utt_merged.items()
    ]

    p_events = [ev for ev in snap.get("events", []) if isinstance(ev, dict) and ev.get("icon") != "▶"]

    return {
        "elapsed": elapsed, "h": elapsed // 3600, "m": (elapsed % 3600) // 60, "s": elapsed % 60,
        "target": target, "curr": curr, "p95_tgt": p95_tgt,
        "all_p50": all_p50, "all_p75": all_p75, "all_p85": all_p85, "all_p95": all_p95, "all_p99": all_p99,
        "all_worst": all_worst, "all_vdict": all_vdict,
        "all_reqs": all_reqs, "all_tout": all_tout,
        "err_rate": err_rate, "rps": rps,
        "health": health, "hcol": hcol,
        "spawn_bar": spawn_bar, "p95_bar": p95_bar, "p95_warn": p95_warn,
        "phase_label": phase_label, "phase_style": phase_style, "finished": _finished,
        "ramps": ramps, "ramps_disp": ramps_disp,
        "finalized_count": len(_finalized),
        "knee_ramp": knee_ramp, "events_by_ramp": events_by_ramp,
        "scenario_rows": scenario_rows,
        "all_spark": all_spark, "err_spark": err_spark,
        "utt_data": utt_data,
        "p_events": p_events,
        "cpu_warn_active": time.time() - _run_state.cpu_warn_ts < 120,
        "circuit_open": _is_circuit_open(),
        "circuit_remaining": max(0, int(_run_state.circuit_open_until - time.time())),
    }


def _render_dashboard(snap: dict, runner, params: dict, state: "_DashboardState") -> Table:
    vm = _compute_dashboard_vm(snap, runner, params, state)
    h, m, s   = vm["h"], vm["m"], vm["s"]
    curr      = vm["curr"]
    target    = vm["target"]
    p95_tgt   = vm["p95_tgt"]
    all_p95   = vm["all_p95"]
    all_p50   = vm["all_p50"]
    all_p75   = vm["all_p75"]
    all_p85   = vm["all_p85"]
    all_p99   = vm["all_p99"]
    all_reqs  = vm["all_reqs"]
    all_tout  = vm["all_tout"]
    err_rate  = vm["err_rate"]
    rps       = vm["rps"]
    health    = vm["health"]
    hcol      = vm["hcol"]
    spawn_bar = vm["spawn_bar"]
    p95_bar   = vm["p95_bar"]
    p95_warn  = vm["p95_warn"]
    elapsed   = vm["elapsed"]

    root = Table.grid(expand=True, padding=(0, 0))
    root.add_column()

    # ── Header box ───────────────────────────────────────────────────────────
    _user_col = "bold green" if curr > 0 else f"color({_G_DIM})"
    hdr = Table.grid(expand=True, padding=(0, 2))
    hdr.add_column(ratio=4)
    hdr.add_column(ratio=2, justify="center")
    hdr.add_column(ratio=3, justify="center")
    hdr.add_column(ratio=2, justify="right")
    hdr.add_row(
        Text("  GRUNTMASTER 6000  ·  " + ("FINISHED" if vm["finished"] else "LIVE"), style="bold cyan"),
        Text(f"⬤  {curr} / {target} active", style=_user_col),
        Text(health, style=f"bold {hcol}"),
        Text(f"{h:02d}:{m:02d}:{s:02d}", style="bold white"),
    )
    root.add_row(Panel(hdr, border_style="cyan", padding=(0, 1)))

    # ── Spawning bar (own line) ───────────────────────────────────────────────
    root.add_row(Text(
        f"  {vm['phase_label']}  {spawn_bar}",
        style=vm["phase_style"],
    ))
    # ── Config FYI ───────────────────────────────────────────────────────────
    # (User count is already in the header as "curr / target active" — not repeated here.)
    root.add_row(Text(
        f"  Mode: {params.get('mode', 'Concurrent')}   "
        f"Ramp: {params.get('spawn_rate', 0)}/min"
        + (f"   Run time: {params.get('run_time', 0) // 60} min"
           if params.get('mode', 'Concurrent') == 'Concurrent' else ""),
        style=f"color({_G_DIM})",
    ))
    # ── Stats summary (own line) ──────────────────────────────────────────────
    root.add_row(Text(
        f"  RPS: {rps:.1f}/s   Errors: {err_rate:.1f}%   "
        f"p95: [{p95_bar}] {_fmt_s(all_p95)}s / {_fmt_s(p95_tgt)}s{p95_warn}",
        style="bold white",
    ))
    if vm["cpu_warn_active"]:
        root.add_row(Text(
            "  ⚠ CPU >90%  —  Locust may give inaccurate latency readings; "
            "consider reducing users or spawning fewer per second",
            style="bold red",
        ))
    if vm["circuit_open"]:
        root.add_row(Text(
            f"  ⚡ CIRCUIT OPEN — DirectLine rate limit (429) hit — "
            f"all users paused — resuming in {vm['circuit_remaining']}s",
            style="bold red on dark_red",
        ))

    # ── Ramp steps ───────────────────────────────────────────────────────────
    ramps_disp = vm["ramps_disp"]
    if vm["ramps"]:
        root.add_row(Text(
            f"  RAMP STEPS  ({vm['finalized_count']} completed)",
            style="bold cyan",
        ))
        st = Table(show_header=True, header_style="bold cyan",
                   box=rich_box.SIMPLE_HEAD, padding=(0, 2), expand=True)
        st.add_column("Ramp",     justify="right", min_width=5)
        st.add_column("Users",    justify="right", min_width=6)
        st.add_column("Requests", justify="right", min_width=8)
        st.add_column("RPS (~=live)", justify="right", min_width=10)
        st.add_column("p50 (s)",  justify="right", min_width=7)
        st.add_column("p75 (s)",  justify="right", min_width=7)
        st.add_column("p85 (s)",  justify="right", min_width=7)
        st.add_column("p95 (s)",  justify="right", min_width=7)
        st.add_column("p99 (s)",  justify="right", min_width=7)
        st.add_column("T/O",      justify="right", min_width=5)
        st.add_column("Throttle", justify="right", min_width=8)
        knee_ramp      = vm["knee_ramp"]

        for s in ramps_disp:
            live      = s.get("active", False)
            is_knee   = (s["ramp"] == knee_ramp)
            past_knee = knee_ramp >= 0 and s["ramp"] > knee_ramp and not live
            p95c  = "bold red" if s["p95"] > p95_tgt else ("bold yellow" if is_knee else ("cyan" if live else "white"))
            toc   = "bold red" if s["timeouts"] > 0 else ("cyan" if live else "white")
            rlc   = "bold red" if s.get("rate_limited", 0) > 0 else ("cyan" if live else "white")
            knee_marker = " ◀" if is_knee else ("  !" if past_knee else "")
            rn     = f'▶ {s["ramp"]}' if live else f'{s["ramp"]}{knee_marker}'
            rstyle = "bold cyan" if live else ("bold yellow" if is_knee else "dim")
            rps_str = f'{s["rps"]:.2f}~' if live else f'{s["rps"]:.2f}'
            st.add_row(
                Text(rn,                 style="bold cyan" if live else ("bold yellow" if is_knee else "white")),
                Text(str(s["users"]),    style=rstyle),
                Text(str(s["requests"]), style=rstyle),
                Text(rps_str,            style=rstyle),
                Text(_fmt_s(s["p50"]),   style=rstyle),
                Text(_fmt_s(s["p75"]),   style=rstyle),
                Text(_fmt_s(s["p85"]),   style=rstyle),
                Text(_fmt_s(s["p95"]),   style=p95c),
                Text(_fmt_s(s["p99"]),   style=rstyle),
                Text(str(s["timeouts"]), style=toc),
                Text(str(s.get("rate_limited", 0)), style=rlc),
            )
        root.add_row(st)

    # ── Profile stats ─────────────────────────────────────────────────────────
    root.add_row(Text("  PROFILE STATS", style="bold cyan"))
    tbl = Table(show_header=True, header_style="bold cyan",
                box=rich_box.SIMPLE_HEAD, padding=(0, 2), expand=True)
    tbl.add_column("User · Scenario", min_width=32)
    tbl.add_column("n",        justify="right", min_width=6)
    tbl.add_column("Typical",  justify="right", min_width=8)
    tbl.add_column("Tail p95", justify="right", min_width=9)
    tbl.add_column("Worst",    justify="right", min_width=8)
    tbl.add_column("T/O",      justify="right", min_width=5)
    tbl.add_column("Latency trend / 10m", min_width=22)

    _verdict_lines = []
    for label, reqs, p50_v, p95_v, worst_v, tout, rcol, spark, vdict in vm["scenario_rows"]:
        _tail_disp = _fmt_s(p95_v) if vdict["tail_reliable"] else f"({_fmt_s(p95_v)})"
        tbl.add_row(
            Text(label, style=rcol),
            Text(str(vdict["n"]), style=rcol),
            Text(_fmt_s(p50_v)),
            Text(_tail_disp, style=rcol),
            Text(_fmt_s(worst_v), style="yellow" if worst_v > p50_v * 2 else "white"),
            Text(str(tout),  style="bold red" if tout > 0 else "white"),
            Text(spark, style="cyan"),
        )
        _verdict_lines.append((label, vdict))

    tbl.add_row(
        Text("ALL USERS", style="bold white"),
        Text(str(vm["all_vdict"]["n"]), style="bold white"),
        Text(_fmt_s(all_p50),  style="bold white"),
        Text(_fmt_s(all_p95) if vm["all_vdict"]["tail_reliable"] else f"({_fmt_s(all_p95)})",
             style="bold red" if all_p95 > p95_tgt else "bold white"),
        Text(_fmt_s(vm["all_worst"]), style="bold white"),
        Text(str(all_tout), style="bold red" if all_tout > 0 else "bold white"),
        Text(vm["all_spark"], style="bold cyan"),
    )
    root.add_row(tbl)
    root.add_row(Text(
        "  Typical = median (outlier-proof)  ·  Tail p95 in (parens) = too few samples to trust  ·  Worst = single slowest reply",
        style=f"color({_G_DIM})",
    ))
    root.add_row(Text(
        "  Trend bar = p95 latency per 10-minute window  ·  taller = slower  ·  ▁ low  █ high",
        style=f"color({_G_DIM})",
    ))
    _vstyles = {"⚠": "yellow", "✓": "green", "•": f"color({_G_DIM})", " ": f"color({_G_DIM})"}
    for _lbl, _vd in _verdict_lines + [("ALL USERS", vm["all_vdict"])]:
        _short = _lbl.split(" · ")[0]
        root.add_row(Text(f"  {_vd['icon']} {_short}: {_vd['verdict']}",
                          style=_vstyles.get(_vd["icon"], "white")))
    root.add_row(Text(f"  {vm['err_spark']}  error rate  (bar height = errors in bucket)", style="red"))
    root.add_row(Text(""))

    # ── Utterance tables ──────────────────────────────────────────────────────
    utt_data = vm["utt_data"]
    if utt_data:
        slowest = sorted(utt_data, key=lambda x: _pct(x[2], 0.95), reverse=True)
        fastest = sorted(utt_data, key=lambda x: _pct(x[2], 0.95))

        def _utt_rows(ut: Table, rows: list, col: str):
            for utt, profile_k, times, tout_u, bot_resp in rows[:4]:
                p95_u  = _pct(times, 0.95)
                plabel = (profile_k[:20] + "…") if len(profile_k) > 21 else profile_k
                ulabel = (utt[:28] + "…") if len(utt) > 29 else utt
                _resp  = " ".join(bot_resp.split())
                rlabel = (_resp[:38] + "…") if len(_resp) > 39 else _resp
                ut.add_row(Text(plabel, style="cyan"),
                           Text(ulabel, style=col),
                           Text(_fmt_s(p95_u), style=col),
                           Text(rlabel, style="dim"))

        root.add_row(Text("  UTTERANCES", style="bold cyan"))
        ut = Table(show_header=True, header_style="bold cyan",
                   box=rich_box.SIMPLE_HEAD, padding=(0, 2), expand=True)
        ut.add_column("Profile",       min_width=22)
        ut.add_column("Utterance",     min_width=30)
        ut.add_column("Response (s)",  justify="right", min_width=12)
        ut.add_column("Bot Response",  min_width=40)
        ut.add_row(Text("── slowest ──", style="dim"),
                   Text(""), Text(""), Text(""))
        _utt_rows(ut, slowest, "bold red")
        ut.add_row(Text("── fastest ──", style="dim"),
                   Text(""), Text(""), Text(""))
        _utt_rows(ut, fastest, "bold green")
        root.add_row(ut)

    # ── Events feed ───────────────────────────────────────────────────────────
    if vm["p_events"]:
        root.add_row(Text("  EVENTS", style="bold cyan"))
        for ev in vm["p_events"][:8]:
            icon = ev.get("icon", "")
            if icon == "⚡":
                prefix, style = "[P0] ", "bold red"
                line = f"  {ev['ts']}  R{ev['ramp']}  {icon}  {prefix}{ev['message']}"
            elif icon in ("✗", "⚠"):
                prefix = "[P1] "
                style  = "bold yellow" if icon == "⚠" else "red"
                line = f"  {ev['ts']}  R{ev['ramp']}  {icon}  {prefix}{ev['message']}"
            else:
                style = "dim"
                line = f"  {icon}  {ev['message']}"
            root.add_row(Text(line, style=style))

    # ── Acronym legend ────────────────────────────────────────────────────────
    legend = (
        "  p50 = median response (s)   "
        "p75 = 75% of requests faster than this (s)   "
        "p85 = 85% faster than this (s)   "
        "p95 = 95% faster than this (s)   "
        "p99 = 99th percentile (s)   "
        "T/O = Timeout   "
        "RPS = Requests / second"
    )
    root.add_row(Panel(Text(legend, style="dim"), border_style="dim", padding=(0, 1)))

    root.add_row(Text("  Press Q to stop test and go to New Run", style="dim"))
    return root


def _audit(csv_path: Path, snapshot: dict):
    """Plain-English sanity checks on the results, printed to the console."""
    import numpy as np
    import pandas as pd

    console.print()
    console.print(Text("  RESULTS HEALTH CHECK", style="bold cyan"))
    console.print(Text("  Confirms the numbers above are trustworthy before you share them.", style="dim"))
    console.print(Text("  " + "─" * 65, style="dim"))

    all_ok = True

    # Read the detail CSV once and reuse it across every check below.
    try:
        _df_full = pd.read_csv(csv_path)
    except Exception as e:
        console.print(Text(f"  Audit skipped — cannot read {csv_path.name}: {e}", style="dim"))
        console.print(Text("  " + "─" * 65, style="dim"))
        console.print()
        return

    # ── 1. Timestamp recheck ─────────────────────────────────────────
    # NOTE: response_received_at is derived from send_time + response_ms/1000 (see _log_utterance).
    # This check verifies that CSV serialisation round-trips cleanly, not measurement independence.
    try:
        df = _df_full[_df_full["event"] == "UTTERANCE"].copy() if "event" in _df_full.columns else _df_full.copy()
        if "utterance_sent_at" in df.columns and "response_received_at" in df.columns:
            sent = pd.to_datetime(df["utterance_sent_at"], utc=True, errors="coerce")
            recv = pd.to_datetime(df["response_received_at"], utc=True, errors="coerce")
            df["_recomputed_ms"] = (recv - sent).dt.total_seconds() * 1000
            df["_delta"] = (df["_recomputed_ms"] - df["response_ms"].astype(float)).abs()
            bad = (df["_delta"] > 10).sum()
            max_delta = int(df["_delta"].max())
            total = len(df)
            if bad == 0:
                console.print(Text(f"  Timing adds up          Every answer's response time matches its start/finish timestamps ({total} of {total})   ✓", style="green"))
            else:
                console.print(Text(f"  Timing adds up          {bad} of {total} answers have response times that don't match their timestamps (up to {_fmt_s(max_delta)}s off)   ✗", style="bold red"))
                all_ok = False
        else:
            console.print(Text("  Timing adds up          Skipped — this run didn't record start/finish timestamps", style="dim"))
    except Exception as e:
        console.print(Text(f"  Timing adds up          Couldn't check ({e})", style="dim"))

    # ── 2. Count reconciliation ──────────────────────────────────────
    try:
        df = _df_full[_df_full["event"] == "UTTERANCE"] if "event" in _df_full.columns else _df_full
        csv_total = len(df)
        dash_total = sum(len(v) for v in snapshot["times"].values()) + sum(snapshot["tout"].values())
        if csv_total == dash_total:
            console.print(Text(f"  Nothing was lost        All {csv_total} answers shown on screen were saved to the results file   ✓", style="green"))
        else:
            console.print(Text(f"  Nothing was lost        Mismatch — {dash_total} shown on screen but {csv_total} saved to file ({abs(dash_total - csv_total)} unaccounted for)   ✗", style="bold red"))
            all_ok = False
    except Exception as e:
        console.print(Text(f"  Nothing was lost        Couldn't check ({e})", style="dim"))

    # ── 3. Percentile cross-check ────────────────────────────────────
    try:
        all_times = [v for vlist in snapshot["times"].values() for v in vlist]
        if len(all_times) >= 20:
            our_p95   = _pct(all_times, 0.95)
            np_p95    = int(np.percentile(all_times, 95, method="lower"))
            pd_p95    = int(pd.Series(all_times).quantile(0.95, interpolation="lower"))
            if our_p95 == np_p95 == pd_p95:
                console.print(Text(f"  Percentiles verified    The p95 figure ({_fmt_s(our_p95)}s) is the same when calculated three independent ways   ✓", style="green"))
            else:
                console.print(Text(f"  Percentiles verified    The p95 figure disagrees between methods ({_fmt_s(our_p95)}s / {_fmt_s(np_p95)}s / {_fmt_s(pd_p95)}s)   ✗", style="bold red"))
                all_ok = False
        else:
            console.print(Text("  Percentiles verified    Skipped — needs at least 20 answers to compute a reliable p95 (this run had fewer)", style="dim"))
    except Exception as e:
        console.print(Text(f"  Percentiles verified    Couldn't check ({e})", style="dim"))

    # ── 4. Every answer is attributed to a user ──────────────────────
    # Count only real answer rows (UTTERANCE); system/lifecycle events have no
    # profile and must NOT be included in the total, or the sum looks short.
    try:
        df = _df_full[_df_full["event"] == "UTTERANCE"].copy() if "event" in _df_full.columns else _df_full.copy()
        if "profile" in df.columns and len(df) > 0:
            df["base_profile"] = df["profile"].astype(str).str.replace(r'\s*#\d+$', '', regex=True).str.strip()
            attributed = int(df["base_profile"].str.len().gt(0).sum())
            total = len(df)
            per_profile = df[df["base_profile"].str.len() > 0].groupby("base_profile").size().to_dict()
            parts = ", ".join(f"{k}: {v}" for k, v in sorted(per_profile.items()))
            if attributed == total:
                console.print(Text(f"  Answers traced to users All {total} answers map to a known user ({parts})   ✓", style="green"))
            else:
                console.print(Text(f"  Answers traced to users {total - attributed} of {total} answers aren't linked to any user   ✗", style="bold red"))
                all_ok = False
        else:
            console.print(Text("  Answers traced to users Skipped — no user information recorded in this run", style="dim"))
    except Exception as e:
        console.print(Text(f"  Answers traced to users Couldn't check ({e})", style="dim"))

    # ── 5. Dropped connections (informational — NOT a failure) ───────
    # A DirectLine WebSocket that drops is auto-reconnected on the same
    # conversation; no answer is lost. This is normal for slow agents, so it
    # is reported for visibility only and never fails the health check.
    try:
        _df_all = _df_full
        if "event_category" in _df_all.columns:
            edf = _df_all[_df_all["event_category"] == "SYSTEM"]
            ddf = _df_all[(_df_all["event_category"] == "USER") & (_df_all["event"] == "UTTERANCE")]
        else:
            edf = pd.DataFrame()
            ddf = _df_all
        ws_closed_count = int((edf["event"] == "ws_closed").sum()) if not edf.empty and "event" in edf.columns else 0
        if ws_closed_count == 0:
            console.print(Text("  Connection drops        None — the streaming connection stayed up the whole run   ✓", style="green"))
        else:
            console.print(Text(
                f"  Connection drops        {ws_closed_count} — the connection dropped and reconnected automatically; no answers lost (normal for slow agents)",
                style="yellow"))
    except Exception as e:
        console.print(Text(f"  Connection drops        Couldn't check ({e})", style="dim"))

    # ── 6. Response time bounds sanity ───────────────────────────────
    # Non-timeout rows must be < (response_timeout + silence_timeout + 2s buffer).
    # Timeout rows must be ≥ response_timeout. Both must be > 0.
    try:
        ddf = _df_full if "ddf" not in dir() else ddf
        if "response_ms" in ddf.columns and "timed_out" in ddf.columns:
            _rt_ms    = test_config.get("response_timeout", 30.0) * 1000
            _ceiling  = _rt_ms + _SILENCE_TIMEOUT * 1000 + 2000
            ms        = ddf["response_ms"].astype(float)
            is_to     = ddf["timed_out"].astype(str) == "1"
            neg       = int((ms < 0).sum())
            over_ceil = int((ms > _ceiling).sum())
            to_too_fast = int((is_to & (ms < _rt_ms * 0.9)).sum())
            if neg == 0 and over_ceil == 0 and to_too_fast == 0:
                console.print(Text(
                    f"  Response times sane     All {len(ddf)} answers fall within the expected range (0–{_fmt_s(_ceiling)}s)   ✓",
                    style="green"))
            else:
                parts = []
                if neg:          parts.append(f"{neg} with a negative time")
                if over_ceil:    parts.append(f"{over_ceil} longer than the {_fmt_s(_ceiling)}s limit")
                if to_too_fast:  parts.append(f"{to_too_fast} marked timed-out but finished too quickly")
                console.print(Text(
                    f"  Response times sane     Found {', '.join(parts)}   ✗",
                    style="bold red"))
                all_ok = False
        else:
            console.print(Text("  Response times sane     Skipped — response-time data not recorded in this run", style="dim"))
    except Exception as e:
        console.print(Text(f"  Response times sane     Couldn't check ({e})", style="dim"))

    console.print(Text("  " + "─" * 65, style="dim"))
    if all_ok:
        console.print(Text("  ✓ All checks passed — these results are safe to share.", style="green"))
    else:
        console.print(Text("  ⚠ Some checks need a look before you share these results.", style="bold red"))
    console.print()


# ── Run parameters ────────────────────────────────────────────────────────────

_HELP_MD = """
# GRUNTMASTER 6000 — Quick Reference

## What does it test?
Fires concurrent simulated users at your Copilot Studio bot, measures
response latency for each utterance, and generates a per-profile HTML report.
Each simulated user follows a CSV script — one message per row — then stops.

## Settings

**Concurrent users**
The number of simulated users running simultaneously at peak load.
Start with a number close to your expected production concurrency.
Too high will affect real users; too low won't stress the bot.

**Spawn rate (users/min)**
How quickly users are added. At 5/min, a new user joins every 12 seconds.
A slower ramp gives you clearer per-step data; a fast ramp reaches peak sooner.

**Think time (seconds)**
How long each user pauses between sending messages. Simulates reading time.
Minimum 25s — shorter values produce unrealistically high load.

**Reply timeout (seconds)**
How long to wait for the bot's first reply before recording a timeout.
Set this above your bot's known worst-case response time. Minimum 15s.
The silence window (15s fixed) adds on top — total wait = timeout + 15s.

**Max run time (safety cap)**
Forces the test to stop at this many minutes even if users are still running.
Set it above the Est. total so it only triggers as a safety net.

## Key metrics
| Metric | Meaning |
|--------|---------|
| **p50** | Median — half of responses were faster than this |
| **p95** | 95th percentile — only 5% of responses took longer |
| **p99** | 99th percentile — worst 1% threshold |
| **T/O** | Timeouts — bot did not reply within the Reply Timeout |
| **RPS** | Requests per second at each user-count level |

## Ramp steps
The dashboard and HTML report track p50 / p95 / p99 / RPS at each 60-second
window as load increases. Use this to find the point where response times
start climbing — that is your bot's capacity knee.

## Trend column (live dashboard)
Each bar = p95 latency in a 10-minute window. Taller bar = slower responses.
▁ = fast   █ = slow

## Tips
- Run for at least 5 minutes at peak load for stable p95 readings.
- A T/O rate above 5% means the bot is struggling — reduce Peak users or
  increase Reply timeout.
- Press Q at any time to stop the test and go straight to results.
- Results are saved automatically — the HTML report opens after the test.
"""


def _collect_run_params() -> "dict | None":
    """Show all run params with defaults pre-filled. Returns None if user goes Back."""
    _START = "  ▶  Start test"
    _BACK  = "  ←  Back"
    _HELP  = "  ?  Help"
    _EXIT  = "  ✕  Exit"

    _utt_count = max(
        (len(_load_utterances(f)) for f in _csv_files),
        default=1,
    )

    def _estimates(users: int, spawn: int, think: int, timeout: int, mode: str, runtime_m: int) -> dict:
        spawn_safe   = max(1, spawn)
        ramp_mins    = round(users / spawn_safe, 1)
        script_min_s = _utt_count * (think + 5)
        script_max_s = _utt_count * (think + timeout)
        script_min_m = round(script_min_s / 60, 1)
        script_max_m = round(script_max_s / 60, 1)
        if mode == "Concurrent":
            # ramp_up + plateau + ramp_down (ramp_down ≈ ramp_up, natural stagger)
            total_min_m = round(ramp_mins * 2 + runtime_m, 1)
            total_max_m = round(ramp_mins * 2 + runtime_m, 1)
        else:
            # Pipeline: ramp_up + last user's utterances
            total_min_m = round(ramp_mins + script_min_m, 1)
            total_max_m = round(ramp_mins + script_max_m, 1)
        return dict(
            ramp_mins=ramp_mins,
            script_range=f"{script_min_m}–{script_max_m}",
            total_range=f"{total_min_m}–{total_max_m}",
        )

    _LAST_RUN_PATH = _HERE / "profiles" / "last_run.json"
    _saved: dict = {}
    try:
        _saved = json.loads(_LAST_RUN_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass

    state = {
        "mode":    _saved.get("mode",    "Concurrent"),
        "users":   _saved.get("users",   test_config["users"]),
        "spawn":   _saved.get("spawn",   test_config["spawn_rate"]),
        "think":   _saved.get("think",   test_config["think_min"]),
        "timeout": _saved.get("timeout", int(test_config["response_timeout"])),
        "runtime": _saved.get("runtime", test_config.get("run_time_mins", 5)),
        "notes":   "",
    }

    def _prow(label: str, value, unit: str, hint: str) -> str:
        return f"    {label:<36}  {str(value):<8} {unit:<8}  {hint}"

    def _divider() -> str:
        return f"    {'─' * 80}"

    def _edit(prompt: str, current) -> int:
        v = _ginput(str(current), header=prompt, default=str(current))
        try:
            return int(v.strip()) if v and v.strip() else current
        except ValueError:
            return current

    while True:
        os.system("cls" if os.name == "nt" else "clear")
        _gprint(
            "  ✦  RUN CONFIGURATION  ✦\n\n"
            "  Select any setting to change it, then start the test.",
            border="double", fg="213", bold=True,
            border_fg="99", padding="1 4", margin="1 0 1 0",
        )

        _mode       = state["mode"]
        _concurrent = _mode == "Concurrent"
        est         = _estimates(state["users"], state["spawn"], state["think"],
                                 state["timeout"], _mode, state["runtime"])
        notes_label = (state["notes"][:40] + "…") if len(state["notes"]) > 40 else (state["notes"] or "none")
        _users_label = "Concurrent users" if _concurrent else "Total users"
        _users_hint  = (
            f"Number of users running simultaneously at peak load — each loops indefinitely for {state['runtime']} min"
            if _concurrent else
            f"Total users to run — each sends {_utt_count} message(s) once then exits"
        )

        # Build items + parallel action_keys for mode-independent dispatch
        items       = []
        action_keys = []

        items.append(_prow("Mode", _mode, "",
                           "Concurrent = users loop at peak load for run time  ·  Pipeline = each user runs once"))
        action_keys.append("mode")

        items.append(_prow(_users_label, state["users"], "users", _users_hint))
        action_keys.append("users")

        items.append(_prow("Spawn rate", state["spawn"], "users/min",
                           f"New users per minute — 1 user every {round(60/max(1,state['spawn']),0):.0f}s"))
        action_keys.append("spawn")

        items.append(_prow("Think time", state["think"], "seconds",
                           "Pause between sending messages (simulates reading time)"))
        action_keys.append("think")

        items.append(_prow("Reply timeout", state["timeout"], "seconds",
                           f"Abort and record timeout if bot has not replied within this long (min 15s)"))
        action_keys.append("timeout")

        if _concurrent:
            items.append(_prow("Run time", state["runtime"], "min",
                               "Time to sustain peak load — ramp-up and ramp-down are separate"))
            action_keys.append("runtime")

        items.append(_divider())
        action_keys.append(None)

        items.append(_prow("  ↳ Est. ramp-up", est["ramp_mins"], "min",
                           f"{state['users']} users ÷ {state['spawn']}/min"))
        action_keys.append("_est")

        if _concurrent:
            items.append(_prow("  ↳ Est. total", est["total_range"], "min",
                               "Ramp-up + run time + ramp-down  (ramp-down mirrors ramp-up)"))
        else:
            items.append(_prow("  ↳ Est. total", est["total_range"], "min",
                               "Ramp-up + last user's utterances — ends when last user exits"))
        action_keys.append("_est")

        items.append(_prow("Silence window", int(_SILENCE_TIMEOUT), "seconds",
                           "Fixed — extra wait after bot's last message before declaring response complete"))
        action_keys.append("_silence")

        items.append(_prow("Protocol",
                           "HTTP ⚠ TEST MODE" if test_config["transport"] == "http" else "WebSocket 🔒", "",
                           "set by GRUNTMASTER_TRANSPORT env var" if test_config["transport"] == "http"
                           else "DirectLine WebSocket over TLS — traffic is encrypted"))
        action_keys.append("_protocol")

        items.append(_prow("Notes", notes_label, "",
                           "Free-text label embedded in the HTML report for this run"))
        action_keys.append("notes")

        items.append(_START)
        action_keys.append(None)
        items.append(_BACK)
        action_keys.append(None)
        items.append(_HELP)
        action_keys.append(None)
        items.append(_EXIT)
        action_keys.append(None)

        choice = _gchoose(*items,
                          header="\n  ↑ ↓  navigate     Enter  select     ↳ rows update as you change settings\n",
                          height=min(len(items) + 4, 22))

        if not choice:
            continue
        if choice.strip() == _EXIT.strip():
            os.system("cls" if os.name == "nt" else "clear")
            sys.exit(0)
        if choice.strip() == _BACK.strip():
            return None
        if choice.strip() == _START.strip():
            break
        if choice.strip() == _HELP.strip():
            _gpager(_HELP_MD)
            continue

        idx    = next((i for i, it in enumerate(items) if it.strip() == choice.strip()), -1)
        action = action_keys[idx] if 0 <= idx < len(action_keys) else None

        if action == "users":
            state["users"] = max(1, _edit(
                f"{'Concurrent users at peak load' if _concurrent else 'Total users to run once'}"
                f"  (current: {state['users']})",
                state["users"]))
        elif action == "spawn":
            state["spawn"] = max(1, _edit(
                f"New users per minute  (e.g. 5 = one every 12s)\n  Current: {state['spawn']}",
                state["spawn"]))
        elif action == "think":
            state["think"] = max(5, _edit(
                f"Pause between messages  (min 5s)\n  Current: {state['think']}s",
                state["think"]))
        elif action == "timeout":
            state["timeout"] = max(15, _edit(
                f"Seconds to wait for bot reply  (min 15s, silence window {int(_SILENCE_TIMEOUT)}s adds on top)\n"
                f"  Current: {state['timeout']}s",
                state["timeout"]))
        elif action == "runtime":
            state["runtime"] = max(1, _edit(
                f"Minutes to sustain peak load  (ramp-up and ramp-down are not counted)\n"
                f"  Current: {state['runtime']} min",
                state["runtime"]))
        elif action == "mode":
            _picked = _gchoose("  Concurrent", "  Pipeline",
                               header="\n  Concurrent — users loop at peak load\n"
                                      "  Pipeline   — each user runs utterances once then exits\n")
            if _picked:
                state["mode"] = _picked.strip()
        elif action == "_est":
            _gprint(
                f"  Est. ramp-up {est['ramp_mins']} min  ·  Est. total {est['total_range']} min\n"
                f"  These update automatically as you change settings.",
                fg=_G_DIM, padding="0 2",
            )
        elif action == "_silence":
            _gprint(f"  Silence window is fixed at {int(_SILENCE_TIMEOUT)}s — not configurable.",
                    fg=_G_DIM, padding="0 2")
        elif action == "_protocol":
            _gprint("  Protocol is set by the GRUNTMASTER_TRANSPORT environment variable.",
                    fg=_G_DIM, padding="0 2")
        elif action == "notes":
            if _gum_ok():
                _nv = _gwrite("Describe this test run…",
                              header="Test notes  (Ctrl-D to save · Esc to cancel)",
                              width=68, height=6)
                if _nv:
                    state["notes"] = _nv
            else:
                _nv = _ginput("Test notes", header="Describe this test run")
                if _nv:
                    state["notes"] = _nv

    try:
        _LAST_RUN_PATH.write_text(
            json.dumps({
                "mode":    state["mode"],
                "users":   state["users"],
                "spawn":   state["spawn"],
                "think":   state["think"],
                "timeout": state["timeout"],
                "runtime": state["runtime"],
            }, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    test_config["think_min"]        = state["think"]
    test_config["think_max"]        = state["think"]
    test_config["response_timeout"] = max(15.0, float(state["timeout"]))
    test_config["users"]            = state["users"]
    test_config["spawn_rate"]       = state["spawn"]
    test_config["run_time_mins"]    = state["runtime"]

    return {
        "mode":       state["mode"],
        "users":      state["users"],
        "spawn_rate": state["spawn"],
        "run_time":   state["runtime"] * 60 if state["mode"] == "Concurrent" else 0,
        "notes":      state["notes"],
    }




def _screen1() -> None:
    run_wizard(_screen1_mode=True)


def _jwt_claims(tok: str) -> dict:
    """Best-effort decode of a JWT payload (no signature check) for diagnostics."""
    import base64
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception as e:
        return {"<decode error>": str(e)}


def _auth_probe(question: str = "What is the Vehicle Identification Number (VIN) and where is it located?") -> bool:
    """
    Fast auth/agent diagnostic. Opens ONE fresh conversation, resolves the target
    agent, sends one question, and — if the bot requests sign-in — performs the
    signin/tokenExchange and prints its HTTP status + body. Prints a clear summary
    so you can verify a Copilot Studio auth fix in ~10s without a full load run.
    Returns True if the bot produced a real (non-fallback, signed-in) answer.
    """
    print("=" * 70)
    print("  GRUNTMASTER 6000 — AUTH PROBE")
    print("=" * 70)

    try:
        profiles = load_profiles()
    except Exception as e:
        print(f"  ✗ Could not load profiles: {e}")
        return False
    if not profiles:
        print("  ✗ No profiles found. Run: python run.py --setup")
        return False

    username = profiles[0]["username"]
    display  = profiles[0].get("display_name", username)
    print(f"  Profile        : {display}")
    print(f"  Auth required  : {_user_auth_required()}")
    print(f"  CS_AGENT_APP_ID: {AGENT_APP_ID or '<none>'}")

    # ── AAD token (SSO) ───────────────────────────────────────────────────
    aad = None
    if _user_auth_required():
        try:
            aad = get_valid_token(username)
        except Exception as e:
            print(f"  ✗ AAD token acquisition failed: {e}")
            return False
        claims = _jwt_claims(aad)
        print(f"  AAD token      : {len(aad)} chars  aud={claims.get('aud')}  scp={claims.get('scp')}")

    # ── DirectLine token → identify the target agent ──────────────────────
    try:
        dl = fetch_directline_token(aad)
    except Exception as e:
        print(f"  ✗ DirectLine token fetch failed: {e}")
        return False
    bot_id = _jwt_claims(dl).get("bot")
    print(f"  Target agent id: {bot_id}   (DirectLine 'bot' claim)")

    try:
        conv = start_conversation(dl)
        ws   = refresh_stream(conv)
    except Exception as e:
        print(f"  ✗ Conversation/WebSocket open failed: {e}")
        return False
    print(f"  Conversation   : {conv.id}")

    try:
        activity_id, _st, _sm = send_utterance(conv, question)
    except Exception as e:
        print(f"  ✗ Send utterance failed: {e}")
        close_websocket(ws)
        return False
    print(f"  Question       : {question!r}")
    print("-" * 70)

    matched, last = [], None
    start = time.monotonic()
    bot_name        = None
    oauth_cards     = 0
    exchange_status = None
    exchange_body   = None
    exchange_conn   = None
    while True:
        now       = time.monotonic()
        remaining = (_SILENCE_TIMEOUT - (now - last)) if matched else (60.0 - (now - start))
        if remaining <= 0:
            break
        try:
            ws.settimeout(remaining)
            raw = ws.recv()
        except Exception:
            break
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for a in data.get("activities", []):
            atype = a.get("type")
            frm   = a.get("from", {})
            if frm.get("role") == "bot" and frm.get("name") and bot_name is None:
                bot_name = frm.get("name")

            # signin/tokenExchange invoke from the bot
            if (atype == "invoke" and a.get("name") == "signin/tokenExchange"
                    and aad and exchange_status is None):
                v = a.get("value", {})
                exchange_conn = v.get("connectionName")
                exchange_status, exchange_body = _probe_exchange(conv, v.get("id", ""), v.get("connectionName", ""), aad)
                continue

            # OAuthCard message → respond with token exchange
            if atype == "message" and aad:
                is_card = False
                for att in a.get("attachments", []):
                    if att.get("contentType") == "application/vnd.microsoft.card.oauth":
                        is_card = True
                        oauth_cards += 1
                        content = att.get("content", {})
                        tr      = content.get("tokenExchangeResource", {})
                        exchange_conn = content.get("connectionName")
                        if tr and exchange_status is None:
                            exchange_status, exchange_body = _probe_exchange(
                                conv, tr.get("id", ""), content.get("connectionName", ""), aad)
                        break
                if is_card:
                    continue

            if (atype == "message" and frm.get("role") == "bot"
                    and a.get("replyToId") == activity_id):
                matched.append(a)
                last = time.monotonic()

    close_websocket(ws)

    text    = " ".join(m.get("text", "").strip() for m in matched if m.get("text", "").strip())
    low     = text.lower()
    signin  = ("sign in" in low or "don't have access" in low or "do not have access" in low)
    fallbk  = ("i'm sorry" in low or "not sure how to help" in low)
    latency = (last - start) if last else -1

    print(f"  Agent name     : {bot_name!r}")
    print(f"  OAuth cards     : {oauth_cards}")
    if exchange_status is not None:
        print(f"  Token exchange  : connection={exchange_conn!r}")
        print(f"                    HTTP {exchange_status}")
        if exchange_status != 200:
            print(f"                    body: {(exchange_body or '')[:400]!r}")
    else:
        print(f"  Token exchange  : (none requested by bot)")
    print(f"  Latency         : {latency:.1f}s" if latency >= 0 else "  Latency         : n/a")
    print(f"  Answer text     : {text[:300]!r}")
    print("-" * 70)

    if not text:
        verdict, ok = "NO REPLY (timeout)", False
    elif signin:
        verdict, ok = "BLOCKED — sign-in / no access (token exchange did not complete)", False
    elif fallbk:
        verdict, ok = "FALLBACK — bot answered but could not ground the answer", False
    else:
        verdict, ok = "OK — real, signed-in answer", True
    print(f"  VERDICT: {verdict}")
    print("=" * 70)
    return ok


def _probe_exchange(conversation: "Conversation", invoke_id: str, connection_name: str, aad_token: str):
    """POST a signin/tokenExchange invoke and return (status_code, body) for diagnostics."""
    try:
        r = _session.post(
            f"{DIRECTLINE_BASE}/v3/directline/conversations/{conversation.id}/activities",
            headers={"Authorization": f"Bearer {conversation.token}", "Content-Type": "application/json"},
            json={
                "type": "invoke", "name": "signin/tokenExchange",
                "value": {"id": invoke_id, "connectionName": connection_name, "token": aad_token},
                "from": {"id": "load-test-user"},
            },
            timeout=15,
        )
        return r.status_code, r.text
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if "--auth-probe" in sys.argv:
        _q = None
        for i, a in enumerate(sys.argv):
            if a == "--auth-probe" and i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("-"):
                _q = sys.argv[i + 1]
        ok = _auth_probe(_q) if _q else _auth_probe()
        sys.exit(0 if ok else 1)

    # First run — no credentials at all → jump straight into wizard
    if not any([_load_credential("CS_TENANT_ID"), _load_credential("CS_CLIENT_ID"),
                _load_credential("CS_DIRECTLINE_SECRET"), _load_credential("CS_TOKEN_ENDPOINT")]):
        run_wizard()

    _show_startup_title()

    os.environ["CS_SETUP_DONE"] = "1"

    _user_classes = [v for k, v in globals().items()
                     if isinstance(v, type) and issubclass(v, User)
                     and v not in (User, CopilotBaseUser, CopilotHttpUser)]

    # "full"         → screen1 + preflight + collect params + run
    # "params_only"  → collect params + run  (skip checks)
    # "rerun"        → run with same params  (skip everything)
    _next: str       = "full"
    _params: dict    = {}

    while True:
        if _next == "full":
            # ── Screen 1: credentials + profiles ─────────────────────
            _screen1()

            # ── Screen 2: preflight ───────────────────────────────────
            _profiles = load_profiles()
            _ok = _preflight_bot_check(_profiles)
            if not _ok:
                # preflight failed — offer retry or go back to edit
                _choice = _gchoose(
                    "  ↺  Retry preflight",
                    "  ⚙  Edit credentials / profiles",
                    "  ✕  Exit",
                    header="\n  Preflight failed. What next?\n",
                    height=7,
                )
                if not _choice or "Exit" in _choice:
                    os.system("cls" if os.name == "nt" else "clear")
                    sys.exit(0)
                if "Edit" in _choice:
                    _next = "full"
                    continue
                # Retry → loop back (still "full")
                continue

            # ── Screen 3: test params ─────────────────────────────────
            _params = _collect_run_params()
            if _params is None:       # Back → return to credentials screen
                continue

        elif _next == "params_only":
            _params = _collect_run_params()
            if _params is None:       # Back → return to credentials screen
                _next = "full"
                continue

        # else: "rerun" — keep existing _params

        _run_path = _next        # remember before resetting
        _next = "full"           # default for next outer-loop iteration
        if _run_path != "rerun":
            os.system("cls" if os.name == "nt" else "clear")
            sys.stdout.write("  Starting test…\n")
            sys.stdout.flush()

        _profile_map = {
            cls.scenario_name: cls.fixed_profile.get(
                "display_name", cls.fixed_profile.get("username", cls.scenario_name)
            )
            for cls in _user_classes
            if hasattr(cls, "scenario_name") and hasattr(cls, "fixed_profile")
        }
        _env    = Environment(user_classes=_user_classes)
        _runner = _env.create_local_runner()
        _dash   = _DashboardState(
            target_users=_params["users"],
            p95_target=test_config["p95_target_ms"],
            profile_map=_profile_map,
        )
        _run_state.spawning_complete = False
        _run_state.circuit_open_until = 0.0
        _run_state.pipeline_spawns_total = 0
        _run_state.pipeline_done = 0
        _run_state.dashboard = _dash
        _env.events.request.add_listener(_dash.on_request)
        def _on_spawning_complete(user_count, **kw):
            _run_state.spawning_complete = True
        _env.events.spawning_complete.add_listener(_on_spawning_complete)

        def _on_user_error(user_instance, exception, tb, **kw):
            from locust.exception import StopUser as _StopUser
            if isinstance(exception, _StopUser):
                return
            _reason = _err_reason(exception)
            if _run_state.dashboard is not None:
                _run_state.dashboard.on_event("✗", f"Unhandled error — {type(exception).__name__}: {_reason}")
            _log_event("✗", "unhandled_error", f"{type(exception).__name__}: {_reason}")

        _env.events.user_error.add_listener(_on_user_error)
        _env.events.test_start.add_listener(_on_test_start)

        _run_state.mode  = _params["mode"]
        _run_state.phase = "RAMP_UP" if _params["mode"] == "Concurrent" else "RUNNING"
        _ramp_rate_ps    = max(0.01, _params["spawn_rate"] / 60)   # users / second
        _plateau_start   = 0.0

        _init_session(_params["users"])
        _runner.start(user_count=_params["users"], spawn_rate=_ramp_rate_ps)

        _stop_run   = [False]
        _prev_users = [0]

        def _keywatch():
            if os.name == "nt":
                import msvcrt as _m
                while not _stop_run[0]:
                    try:
                        if _m.kbhit():
                            ch = _m.getch()
                            if ch in (b"q", b"Q"):
                                _stop_run[0] = True
                                return
                            if ch in (b"\x00", b"\xe0") and _m.kbhit():
                                _m.getch()  # consume second byte of extended key
                    except Exception:
                        pass
                    gevent.sleep(0.05)
            else:
                # POSIX (Linux/macOS, incl. Docker): read stdin in cbreak mode so a
                # single 'q' keypress registers without waiting for Enter.
                try:
                    import termios, tty
                    import select as _select
                except Exception:
                    return
                try:
                    if not sys.stdin.isatty():
                        return
                    _fd = sys.stdin.fileno()
                    _old = termios.tcgetattr(_fd)
                except Exception:
                    return
                try:
                    tty.setcbreak(_fd)
                    while not _stop_run[0]:
                        try:
                            _r, _, _ = _select.select([sys.stdin], [], [], 0)
                            if _r:
                                ch = sys.stdin.read(1)
                                if ch in ("q", "Q"):
                                    _stop_run[0] = True
                                    return
                        except Exception:
                            pass
                        gevent.sleep(0.05)
                finally:
                    try:
                        termios.tcsetattr(_fd, termios.TCSADRAIN, _old)
                    except Exception:
                        pass

        _kw = gevent.spawn(_keywatch)

        # Silence all log output to the terminal during the live dashboard — events feed carries what matters
        _null_handler = logging.NullHandler()
        logging.root.addHandler(_null_handler)
        _prev_log_level = logging.root.level
        logging.root.setLevel(logging.CRITICAL)
        _dash_err: "Exception | None" = None
        try:
            with Live(console=console, auto_refresh=False, screen=True) as _live:
                # Large safety backstop — natural exit fires first via spawning_complete + curr==0
                _deadline = time.time() + 8 * 3600
                while time.time() < _deadline and not _stop_run[0]:
                    _curr = getattr(_runner, "user_count", 0)
                    _now  = time.time()

                    # ── Phase transitions (Concurrent only) ──────────────────
                    if _run_state.mode == "Concurrent":
                        if _run_state.phase == "RAMP_UP" and _run_state.spawning_complete:
                            _run_state.phase = "AT_PEAK"
                            _plateau_start   = _now
                            _log_event("⬤", "phase", f"AT PEAK — {_curr} users active")
                            if _run_state.dashboard is not None:
                                _run_state.dashboard.on_event("⬤", f"AT PEAK — {_curr} users active")

                        elif _run_state.phase == "AT_PEAK":
                            # Plateau duration elapsed → start ramp-down
                            if _now - _plateau_start >= _params["run_time"]:
                                _run_state.phase = "RAMP_DOWN"
                                _log_event("▼", "phase", f"RAMP DOWN — {_curr} users finishing current cycle")
                                if _run_state.dashboard is not None:
                                    _run_state.dashboard.on_event("▼", f"RAMP DOWN — {_curr} users finishing current cycle")
                                # Tell Locust to stop spawning replacements
                                try:
                                    _runner.start(user_count=0, spawn_rate=_ramp_rate_ps)
                                except Exception:
                                    pass

                    if _curr != _prev_users[0]:
                        _dash.set_user_count(_curr)
                        _prev_users[0] = _curr
                    _live.update(_render_dashboard(_dash.snapshot(), _runner, _params, _dash))
                    _live.refresh()
                    if _run_state.mode == "Pipeline":
                        if _run_state.spawning_complete and _run_state.pipeline_done >= _params["users"]:
                            break
                    else:
                        if _run_state.spawning_complete and _curr == 0:
                            break
                    gevent.sleep(0.5)
        except Exception as _e:
            _dash_err = _e
            import traceback as _tb
            _debug_log_path = TMP_DIR / "_debug.log"
            with open(_debug_log_path, "a", encoding="utf-8") as _f:
                _f.write(f"\n[{datetime.now().isoformat()}] Dashboard crash:\n")
                _tb.print_exc(file=_f)
            console.print(f"\n[bold red]Dashboard error: {_e}[/bold red]")
            console.print(f"[dim]Details written to {_debug_log_path}[/dim]")

        _stop_run[0] = True
        _kw.kill(block=False)
        try:
            _runner.quit()
            _runner.greenlet.join(timeout=8)   # don't hang if a user greenlet is mid-sleep
        except Exception:
            pass

        # Flush CSV writer before reading files for audit / report
        _csv_writer.stop()

        # Restore logging now that the live display is gone
        logging.root.setLevel(_prev_log_level)
        logging.root.removeHandler(_null_handler)

        # Print final dashboard snapshot to normal screen — options appear below it
        _final_snap = _dash.snapshot()
        console.print(_render_dashboard(_final_snap, _runner, _params, _dash))
        _log_path = _csv_writer.log_path
        if _log_path and _log_path.exists():
            _audit(_log_path, _final_snap)

        # ── CI JSON summary ───────────────────────────────────────────────────
        _ts_ci = (_log_path.stem.replace("run_log_", "") if _log_path else
                  datetime.now().strftime("%Y%m%d_%H%M%S"))
        _snap_times = _final_snap.get("times", {})
        _snap_tout  = _final_snap.get("tout", {})
        _ci_all_times = [v for vlist in _snap_times.values() for v in vlist]
        _ci_total_req = sum(len(v) for v in _snap_times.values()) + sum(_snap_tout.values())
        _ci_total_err = sum(_snap_tout.values())
        _ci_result = {
            "total_requests": _ci_total_req,
            "total_errors":   _ci_total_err,
            "p50":  _pct(_ci_all_times, 0.50),
            "p95":  _pct(_ci_all_times, 0.95),
            "p99":  _pct(_ci_all_times, 0.99),
            "error_rate": round(_ci_total_err / max(1, _ci_total_req), 4),
            "passed": _pct(_ci_all_times, 0.95) <= test_config["p95_target_ms"],
        }
        try:
            _ci_json_path = REPORT_DIR / f"ci_{_ts_ci}.json"
            _ci_json_path.write_text(json.dumps(_ci_result, indent=2), encoding="utf-8")
        except Exception:
            pass
        if os.getenv("GRUNTMASTER_CI"):
            print(json.dumps(_ci_result, indent=2))
            sys.exit(0 if _ci_result["passed"] else 1)

        _run_state.dashboard = None
        with _spawn_lock:
            _spawn_counters.clear()

        if _log_path and _log_path.exists():
            sys.stdout.write("\n  ⏳  Generating report…")
            sys.stdout.flush()
            # Run in a real OS thread (pre-gevent Thread) so pandas/plotly are
            # completely isolated from gevent's monkey-patched locks and I/O.
            import queue as _rq
            _rep_q: _rq.Queue = _rq.Queue()
            def _run_report_thread():
                try:
                    from report import generate_report as _gen_report
                    _run_notes = _params.get("notes", "")
                    _all_logs = sorted(REPORT_DIR.glob("run_log_*.csv"), key=lambda p: p.stat().st_mtime)
                    _other = [p for p in _all_logs if p != _log_path]
                    _baseline = _other[-1] if _other else None
                    _rep = _gen_report(
                        _log_path,
                        p95_target=test_config["p95_target_ms"],
                        notes=_run_notes,
                        response_timeout=test_config["response_timeout"],
                        silence_timeout=_SILENCE_TIMEOUT,
                        run_config=_params,
                        baseline_csv=_baseline,
                    )
                    _rep_q.put(("ok", _rep))
                except ImportError as _ie:
                    _rep_q.put(("skip", str(_ie)))
                except Exception as _re:
                    _rep_q.put(("err", str(_re)))
            _rt = _RealThread(target=_run_report_thread, daemon=True, name="report-gen")
            _rt.start()
            _rt.join(timeout=120)
            # Clear the "Generating report…" spinner line before printing the result.
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            try:
                _kind, _val = _rep_q.get_nowait()
                if _kind == "ok":
                    _gprint(f"  ✓  Report → {_val}", fg=_G_CYAN, bold=True, padding="0 2", margin="0 1")
                elif _kind == "skip":
                    sys.stdout.write(f"  HTML report skipped — install pandas + plotly to enable\n")
                    sys.stdout.flush()
                else:
                    sys.stdout.write(f"  Report error: {_val}\n")
                    sys.stdout.flush()
            except _rq.Empty:
                sys.stdout.write("  Report timed out (>120s) — CSV saved, report skipped\n")
                sys.stdout.flush()

        # Drain any keypresses that accumulated during the test (e.g. 'q' to quit)
        # so they don't auto-select a menu item in gum.
        if os.name == "nt":
            import msvcrt as _msvcrt
            while _msvcrt.kbhit():
                _msvcrt.getch()

        while True:
            _post = _gchoose(
                "  ▶  Run again  (same settings)",
                "  ⚙  New run  (edit params)",
                "  ✎  Edit credentials",
                "  ✕  Exit",
                header="\n  What next?\n",
                height=8,
            )
            if not _post:
                continue
            if "Edit credentials" in _post:
                _next = "full"
                break
            elif "Exit" in _post:
                os.system("cls" if os.name == "nt" else "clear")
                sys.exit(0)
            elif "Run again" in _post:
                _next = "rerun"
                break
            else:  # New run (edit params)
                _next = "params_only"
                break


if __name__ == "__main__":
    main()
