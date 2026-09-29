"""Hosting glue for the Streamlit demo.

The backend in backend/app.py is unchanged; this module decides where the
world reaches it. AssemblyAI calls the tools from its own servers, so the
agent needs a public https origin, and on a hosted demo that origin is not
known until the first visitor arrives. In order of preference:

  1. PUBLIC_BACKEND_URL, if set (a Render service, say).
  2. The Streamlit app's own URL, where streamlit_app.py mounts the backend
     at /rosewood, if the host forwards that path.
  3. A Cloudflare quick tunnel to the backend running in this process.

Whichever wins, the bakery agent and its webhook are re-pointed at it, the
same thing `publish.py` and `setup_webhook.py` do by hand.
"""
import ipaddress
import json
import os
import secrets
import sys
import threading
import time
import tomllib
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT, ROOT / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def _load_secrets() -> None:
    """Streamlit secrets as environment variables, before the backend reads
    its settings at import. Real environment variables win."""
    try:
        data = tomllib.loads((ROOT / ".streamlit" / "secrets.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return
    for key, value in data.items():
        if isinstance(value, (str, int, float, bool)) and key not in os.environ:
            os.environ[key] = str(value)


_load_secrets()
os.environ.setdefault("DEMO_SEED", "1")

import app as backend  # noqa: E402
import db  # noqa: E402
import lib  # noqa: E402

MOUNT = "/rosewood"
LOCAL_PORT = 8765
WEBHOOK_EVENTS = ["session.completed", "call.ended", "call.failed"]

_lock = threading.Lock()
_booted = False


def ensure_db() -> None:
    global _booted
    with _lock:
        if not _booted:
            backend.boot()
            _booted = True


def healthy(base: str, timeout: float = 5) -> bool:
    try:
        with urllib.request.urlopen(base + "/healthz", timeout=timeout) as resp:
            return json.loads(resp.read()).get("service") == "rosewood"
    except Exception:  # noqa: BLE001 - any failure means "not here"
        return False


def is_public(url: str) -> bool:
    host = urlparse(url).hostname or ""
    if not host or host == "localhost" or host.endswith(".local"):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def _serve_backend() -> str:
    """The backend on its own port, for when Streamlit runs without the
    mount (an older runner executes streamlit_app.py as a plain script)."""
    import uvicorn
    base = f"http://127.0.0.1:{LOCAL_PORT}"
    if not healthy(base, 1):
        server = uvicorn.Server(uvicorn.Config(backend.app, host="127.0.0.1", port=LOCAL_PORT,
                                               log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(40):
            if healthy(base, 1):
                break
            time.sleep(0.25)
    return base


def _tunnel(local_base: str) -> str:
    from pycloudflared import try_cloudflare
    parsed = urlparse(local_base)
    url = try_cloudflare(port=parsed.port, verbose=False).tunnel.rstrip("/") + parsed.path
    # A fresh trycloudflare hostname takes a few seconds to resolve.
    for _ in range(45):
        if healthy(url):
            return url
        time.sleep(2)
    raise RuntimeError("the tunnel came up but " + url + " never answered")


def go_public(origin: str, streamlit_port: int) -> dict:
    """Find a public origin for the backend and point the agent at it."""
    explicit = os.environ.get("PUBLIC_BACKEND_URL", "").rstrip("/")
    if explicit:
        return _connect(explicit, "PUBLIC_BACKEND_URL")
    if origin and is_public(origin):
        for base in (origin + MOUNT, origin + "/~/+" + MOUNT):
            if healthy(base):
                return _connect(base, "Streamlit app URL")
    local = f"http://127.0.0.1:{streamlit_port}{MOUNT}"
    if not healthy(local, 2):
        local = _serve_backend()
    return _connect(_tunnel(local), "Cloudflare tunnel")


def _connect(base: str, how: str) -> dict:
    out = {"base": base, "how": how, "agent_id": "", "agent": "", "keyterms": "", "webhook": ""}
    if not os.environ.get("ASSEMBLYAI_API_KEY"):
        out["agent"] = "ASSEMBLYAI_API_KEY is not set, so there is no agent to call"
        return out
    os.environ["BACKEND_URL"] = base
    try:
        agent = lib.read_agent("bakery")
        result = lib.publish_agent(agent, name="bakery", reuse_by_name=True)
    except SystemExit as err:  # read_agent exits on a missing variable
        out["agent"] = str(err)
        return out
    except lib.ApiError as err:
        out["agent"] = str(err)[:300]
        return out
    out["agent_id"] = result["id"]
    out["agent"] = "created" if result["created"] else "updated"
    os.environ["AGENT_ID_BAKERY"] = result["id"]
    backend.AGENT_ID = result["id"]
    # publish_agent wrote the file's keyterms; add the live menu back on top.
    synced = backend.sync_keyterms()
    out["keyterms"] = (f"{synced['keyterms']} synced" if synced.get("synced")
                       else synced.get("reason", "not synced"))
    out["webhook"] = _webhook(base, result["id"])
    return out


def _webhook(base: str, agent_id: str) -> str:
    """Re-point the one subscription at this origin rather than adding
    another each boot, which would leave deliveries going to dead tunnels."""
    secret = os.environ.get("AAI_WEBHOOK_SECRET") or secrets.token_urlsafe(32)
    os.environ["AAI_WEBHOOK_SECRET"] = secret
    backend.WEBHOOK_SECRET = secret
    body = {"url": base + "/webhooks/assemblyai", "events": WEBHOOK_EVENTS, "secret": secret}
    try:
        existing = os.environ.get("AAI_WEBHOOK_ID") or _find_webhook(agent_id)
        if existing:
            try:
                lib.aai(f"/webhook-subscriptions/{existing}", method="PATCH", body=body)
                return "updated"
            except lib.ApiError as err:
                if err.status != 404:
                    raise
        created = lib.aai("/webhook-subscriptions", method="POST", body={**body, "agent_id": agent_id})
        os.environ["AAI_WEBHOOK_ID"] = created.get("id", "")
        return "created"
    except lib.ApiError as err:
        return str(err)[:200]


def _find_webhook(agent_id: str) -> str:
    try:
        listing = lib.aai("/webhook-subscriptions")
    except lib.ApiError:
        return ""
    rows = next((v for v in listing.values() if isinstance(v, list)), []) \
        if isinstance(listing, dict) else listing
    for row in rows or []:
        if row.get("agent_id") == agent_id and str(row.get("url", "")).endswith("/webhooks/assemblyai"):
            return row.get("id", "")
    return ""


def call_widget(api_base: str, agent_id: str) -> str:
    """The browser call as one self-contained page for st.iframe. The audio
    engine is backend/static/voice.js, inlined so there is one copy of it."""
    engine = (ROOT / "backend" / "static" / "voice.js").read_text(encoding="utf-8")
    swaps = {"export const VoiceCall": "const VoiceCall",
             "fetch('/api/token')": "fetch(API + '/api/token')"}
    for old, new in swaps.items():
        if old not in engine:
            raise RuntimeError("voice.js changed; update call_widget: " + old)
        engine = engine.replace(old, new)
    page = (Path(__file__).parent / "call.html").read_text(encoding="utf-8")
    return (page.replace("/*API*/", json.dumps(api_base))
                .replace("/*AGENT_ID*/", json.dumps(agent_id))
                .replace("/*ENGINE*/", engine))
