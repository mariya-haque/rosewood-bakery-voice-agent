"""Bakery voice-agent backend.

Two audiences share one FastAPI app:

  /tools/*      the four HTTP tools AssemblyAI calls mid-conversation. Guarded
                by a shared secret so only our agent can write orders.
  /api/*, /     the owner dashboard: live orders board, menu and stock editor,
                and call history read back from the AssemblyAI Sessions API.

Tool replies are deliberately terse. AssemblyAI caps a tool response body at
8 KiB before handing it to the model, and every extra token is latency the
caller hears as silence.
"""
import asyncio
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from difflib import get_close_matches
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import db


def _load_root_env() -> None:
    """Read the starter's .env so local runs need no exported variables. The
    real environment wins, which is what Render and a shell override want."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = raw.strip().strip('"').strip("'")


_load_root_env()

TOOL_API_KEY = os.environ.get("TOOL_API_KEY", "")
ASSEMBLYAI_API_KEY = os.environ.get("ASSEMBLYAI_API_KEY", "")
SHOP_NAME = os.environ.get("SHOP_NAME", "Rosewood Bakery")
# Whichever agent was published last. AGENT_ID overrides, as it does upstream.
AGENT_ID = os.environ.get("AGENT_ID") or os.environ.get("AGENT_ID_BAKERY", "")
STATIC = Path(__file__).parent / "static"

app = FastAPI(title=SHOP_NAME + " agent backend")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# Dashboards subscribe here; every write publishes so the board moves the
# moment a call ends.
_subscribers: set = set()


@app.on_event("startup")
def _startup() -> None:
    db.init()


def _publish(event: str, payload: Any) -> None:
    message = "event: " + event + "\ndata: " + json.dumps(payload, default=str) + "\n\n"
    for queue in list(_subscribers):
        queue.put_nowait(message)


def _require_tool_key(supplied) -> None:
    if not TOOL_API_KEY:
        return  # unset in local dev; set it before the endpoint is public
    if supplied != TOOL_API_KEY:
        raise HTTPException(status_code=401, detail="bad tool key")


def _money(cents: int) -> str:
    return "$" + format(cents / 100, ".2f")


# --------------------------------------------------------------------------
# Item matching. The transcriber gives us what the caller said, not a SKU, so
# match on name, then aliases, then fuzzily. keyterms in the agent file keep
# "tres leches" from arriving as "trey lecher" in the first place.
# --------------------------------------------------------------------------
def _find_item(spoken: str):
    if not spoken:
        return None
    needle = spoken.strip().lower()
    rows = db.menu_rows()

    for row in rows:
        if row["name"].lower() == needle:
            return row
    for row in rows:
        if needle in [a.lower() for a in json.loads(row["aliases"])]:
            return row
    for row in rows:
        if needle in row["name"].lower() or row["name"].lower() in needle:
            return row

    haystack = {}
    for row in rows:
        haystack[row["name"].lower()] = row
        for alias in json.loads(row["aliases"]):
            haystack[alias.lower()] = row
    close = get_close_matches(needle, list(haystack), n=1, cutoff=0.6)
    return haystack[close[0]] if close else None


# --------------------------------------------------------------------------
# Pickup-time parsing. The model is asked for an ISO date-time, but a caller
# saying "tomorrow at four" can still produce something loose, so fall back
# rather than erroring out mid-call.
# --------------------------------------------------------------------------
def _parse_time(value: str):
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p", "%m/%d/%Y %H:%M", "%H:%M", "%I:%M %p"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.year == 1900:  # time only: assume today, roll over if already past
            today = datetime.now()
            parsed = parsed.replace(year=today.year, month=today.month, day=today.day)
            if parsed < today:
                parsed += timedelta(days=1)
        return parsed
    return None


def _slot_start(when: datetime) -> datetime:
    start = when.replace(second=0, microsecond=0)
    return start - timedelta(minutes=start.minute % db.SLOT_MINUTES)


def _within_hours(when: datetime) -> bool:
    return db.OPEN_HOUR <= when.hour < db.CLOSE_HOUR


def _next_open_slot(after: datetime) -> datetime:
    slot = _slot_start(after) + timedelta(minutes=db.SLOT_MINUTES)
    for _ in range(96):  # two days of slots is plenty
        if not _within_hours(slot):
            slot = slot.replace(hour=db.OPEN_HOUR, minute=0)
            if slot <= after:
                slot += timedelta(days=1)
            continue
        if db.slot_load(slot) < db.SLOT_CAPACITY:
            return slot
        slot += timedelta(minutes=db.SLOT_MINUTES)
    return slot


def _spoken_time(when: datetime) -> str:
    today = datetime.now().date()
    if when.date() == today:
        day = "today"
    elif when.date() == today + timedelta(days=1):
        day = "tomorrow"
    else:
        day = when.strftime("%A")
    clock = when.strftime("%I:%M %p").lstrip("0")
    return day + " at " + clock


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
@app.post("/tools/check_availability")
def check_availability(payload: dict = Body(...), x_tool_key: str = Header(None)):
    _require_tool_key(x_tool_key)
    item = str(payload.get("item", ""))
    quantity = int(payload.get("quantity") or 1)

    row = _find_item(item)
    if not row:
        names = [r["name"] for r in db.menu_rows() if r["stock"] > 0][:5]
        return {"available": False, "reason": "not on the menu", "we_do_have": names}

    if row["stock"] >= quantity:
        return {
            "available": True,
            "item": row["name"],
            "quantity": quantity,
            "unit_price": _money(row["price_cents"]),
            "line_total": _money(row["price_cents"] * quantity),
        }

    alt = _find_item(row["alternative"] or "")
    out = {
        "available": False,
        "item": row["name"],
        "in_stock": row["stock"],
        "reason": "sold out" if row["stock"] == 0 else "not enough left",
    }
    if alt and alt["stock"] >= quantity:
        out["alternative"] = {"item": alt["name"], "unit_price": _money(alt["price_cents"])}
    return out


@app.post("/tools/check_pickup_slot")
def check_pickup_slot(payload: dict = Body(...), x_tool_key: str = Header(None)):
    _require_tool_key(x_tool_key)
    when = _parse_time(str(payload.get("time", "")))
    if not when:
        return {"ok": False, "reason": "could not read that time", "hours": "9am to 7pm daily"}

    earliest = datetime.now() + timedelta(minutes=db.LEAD_TIME_MINUTES)
    if when < earliest:
        slot = _next_open_slot(earliest)
        return {"ok": False, "reason": "too soon to bake",
                "nearest_slot": _spoken_time(slot), "nearest_slot_iso": slot.isoformat()}

    if not _within_hours(when):
        slot = _next_open_slot(when)
        return {"ok": False, "reason": "closed then", "hours": "9am to 7pm daily",
                "nearest_slot": _spoken_time(slot), "nearest_slot_iso": slot.isoformat()}

    if db.slot_load(when) >= db.SLOT_CAPACITY:
        slot = _next_open_slot(when)
        return {"ok": False, "reason": "that slot is full",
                "nearest_slot": _spoken_time(slot), "nearest_slot_iso": slot.isoformat()}

    return {"ok": True, "pickup_time": _spoken_time(when), "pickup_time_iso": when.isoformat()}


@app.post("/tools/create_order")
def create_order(payload: dict = Body(...), x_tool_key: str = Header(None)):
    _require_tool_key(x_tool_key)
    name = str(payload.get("customer_name", "")).strip()
    phone = "".join(ch for ch in str(payload.get("phone", "")) if ch.isdigit())
    raw_items = payload.get("items") or []
    when = _parse_time(str(payload.get("pickup_time", "")))

    if not name or not phone or not raw_items or not when:
        return {"ok": False, "reason": "missing name, phone, items or pickup time"}
    if len(phone) < 7:
        return {"ok": False, "reason": "phone number looks incomplete"}

    priced = []
    total = 0
    for line in raw_items:
        row = _find_item(str(line.get("item", "")))
        if not row:
            return {"ok": False, "reason": str(line.get("item")) + " is not on the menu"}
        qty = int(line.get("quantity") or 1)
        if row["stock"] < qty:
            return {"ok": False,
                    "reason": row["name"] + " no longer has that many available",
                    "in_stock": row["stock"]}
        total += row["price_cents"] * qty
        priced.append({
            "item": row["name"],
            "quantity": qty,
            "customization": str(line.get("customization", "")).strip(),
            "line_total": _money(row["price_cents"] * qty),
        })

    order_no = db.create_order(
        name, phone, priced, when.isoformat(), str(payload.get("notes", "")).strip(), total,
        session_id=str(payload.get("session_id", "")) or None,
    )
    _publish("order", db.order_by_no(order_no))
    return {"ok": True, "order_number": order_no, "total": _money(total),
            "pickup_time": _spoken_time(when)}


@app.post("/tools/get_order_status")
def get_order_status(payload: dict = Body(...), x_tool_key: str = Header(None)):
    _require_tool_key(x_tool_key)
    order_no = "".join(ch for ch in str(payload.get("order_id", "")) if ch.isdigit())
    order = db.order_by_no(order_no)
    if not order:
        return {"found": False, "reason": "no order with that number"}
    when = _parse_time(order["pickup_time"])
    return {
        "found": True,
        "order_number": order["order_no"],
        "status": order["status"],
        "customer_name": order["customer_name"],
        "items": [str(i["quantity"]) + " x " + i["item"] for i in order["items"]],
        "pickup_time": _spoken_time(when) if when else order["pickup_time"],
        "total": _money(order["total_cents"]),
    }


# --------------------------------------------------------------------------
# Dashboard API
# --------------------------------------------------------------------------
@app.get("/api/orders")
def api_orders(status: str = None):
    return {"orders": db.orders(status)}


@app.post("/api/orders/{order_no}/status")
def api_set_status(order_no: str, payload: dict = Body(...)):
    status = str(payload.get("status", "")).lower()
    if status not in {"new", "baking", "ready", "collected", "cancelled"}:
        raise HTTPException(status_code=400, detail="unknown status")
    order = db.set_status(order_no, status)
    if not order:
        raise HTTPException(status_code=404, detail="no such order")
    _publish("order", order)
    return order


@app.get("/api/menu")
def api_menu():
    return {"menu": db.menu_rows(), "shop_name": SHOP_NAME}


@app.post("/api/menu/{item_id}/stock")
def api_set_stock(item_id: int, payload: dict = Body(...)):
    row = db.set_stock(item_id, int(payload.get("stock", 0)))
    if not row:
        raise HTTPException(status_code=404, detail="no such item")
    _publish("menu", row)
    return row


@app.get("/api/events")
async def api_events(request: Request):
    """Server-sent events: the live board's connection to the kitchen."""
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.add(queue)

    async def stream():
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _subscribers.discard(queue)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------
# Call history, read straight from the AssemblyAI Sessions API. The key stays
# on this server; the page only ever sees what we hand back.
# --------------------------------------------------------------------------
def _aai(path: str) -> dict:
    if not ASSEMBLYAI_API_KEY:
        raise HTTPException(status_code=503, detail="ASSEMBLYAI_API_KEY not set on the backend")
    req = urllib.request.Request(
        "https://agents.assemblyai.com" + path,
        headers={"Authorization": "Bearer " + ASSEMBLYAI_API_KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        raise HTTPException(status_code=err.code, detail=err.read().decode()[:400])


@app.get("/api/calls")
def api_calls(limit: int = 25):
    return _aai("/v1/sessions?limit=" + str(limit))


# --------------------------------------------------------------------------
# The call itself. The page needs a short-lived token and the agent to name;
# the API key never leaves this process.
# --------------------------------------------------------------------------
@app.get("/api/token")
def api_token():
    return _aai("/v1/token?product=voice_agent&expires_in_seconds=60")


@app.get("/api/agent")
def api_agent():
    if not AGENT_ID:
        return {"id": "", "name": "no agent published"}
    try:
        agent = _aai("/v1/agents/" + AGENT_ID)
    except HTTPException:
        return {"id": AGENT_ID, "name": "agent"}
    return {"id": AGENT_ID, "name": agent.get("name") or "agent"}


@app.post("/api/live")
def api_live(payload: dict = Body(...)):
    """The calling page mirrors its transcript here so a second screen — the
    projector in a demo — shows the same call as it happens."""
    _publish("live", payload)
    return {"ok": True}


@app.get("/api/calls/{session_id}")
def api_call(session_id: str):
    return _aai("/v1/sessions/" + session_id)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def dashboard():
    return FileResponse(STATIC / "index.html")
