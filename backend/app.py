"""Bakery voice-agent backend.

Three audiences share one FastAPI app:

  /tools/*, /voice/*   what AssemblyAI calls: the six HTTP tools mid-call, and
                       the pre-connect lookup before a phone call is answered.
                       Guarded by a shared secret so only our agent can write.
  /webhooks/assemblyai signed session and call events. Each finished call is
                       pulled from the Sessions API and summarised through the
                       AssemblyAI LLM Gateway.
  /api/*, /            the owner dashboard: live orders board, insights, menu
                       and stock editor, call-backs, and call history.

Tool replies are deliberately terse. AssemblyAI caps a tool response body at
8 KiB before handing it to the model, and every extra token is latency the
caller hears as silence.
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from difflib import get_close_matches
from pathlib import Path
from typing import Any


def _load_root_env() -> None:
    """Read the starter's .env so local runs need no exported variables. The
    real environment wins, which is what Render and a shell override want.
    Runs before `import db` so DB_PATH and SHOP_TZ from .env apply."""
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

from fastapi import BackgroundTasks, Body, FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import db  # noqa: E402

TOOL_API_KEY = os.environ.get("TOOL_API_KEY", "")
ASSEMBLYAI_API_KEY = os.environ.get("ASSEMBLYAI_API_KEY", "")
WEBHOOK_SECRET = os.environ.get("AAI_WEBHOOK_SECRET", "")
SHOP_NAME = os.environ.get("SHOP_NAME", "Rosewood Bakery")
# The model that writes post-call summaries, through the AssemblyAI LLM Gateway.
# Any gateway model works; set SUMMARY_MODEL=claude-sonnet-4-6 on an account
# with access to it. https://www.assemblyai.com/docs/llm-gateway/available-models
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "qwen3.5-4b-32k-fast")
LLM_GATEWAY = os.environ.get("LLM_GATEWAY_URL", "https://llm-gateway.assemblyai.com/v1")
# Whichever agent was published last. AGENT_ID overrides, as it does upstream.
AGENT_ID = os.environ.get("AGENT_ID") or os.environ.get("AGENT_ID_BAKERY", "")
AGENT_FILE = Path(__file__).resolve().parent.parent / "agents" / "bakery.jsonc"
STATIC = Path(__file__).parent / "static"

app = FastAPI(title=SHOP_NAME + " agent backend")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
# The Streamlit demo's call widget runs in an iframe on another origin and
# fetches its session token from here. Reads only; nothing here is private
# that a plain GET could not already see.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])

# Dashboards subscribe here; every write publishes so the board moves the
# moment a call ends.
_subscribers: set = set()
_loop: asyncio.AbstractEventLoop | None = None


def boot(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Startup work, callable by a host that mounts this app (the Streamlit
    demo does), since a mounted sub-app never sees its own startup event."""
    global _loop
    _loop = loop or _loop
    db.init()
    if os.environ.get("DEMO_SEED"):
        import seed_demo
        seed_demo.seed()


@app.on_event("startup")
async def _startup() -> None:
    boot(asyncio.get_running_loop())


def _publish(event: str, payload: Any) -> None:
    """Safe from any thread: tool handlers run in the threadpool and the
    post-call analysis runs in a background thread, but the SSE queues belong
    to the event loop."""
    message = "event: " + event + "\ndata: " + json.dumps(payload, default=str) + "\n\n"
    for queue in list(_subscribers):
        if _loop and _loop.is_running():
            _loop.call_soon_threadsafe(queue.put_nowait, message)
        else:
            queue.put_nowait(message)


def _require_tool_key(supplied) -> None:
    if not TOOL_API_KEY:
        return  # unset in local dev; set it before the endpoint is public
    if not supplied or not hmac.compare_digest(supplied, TOOL_API_KEY):
        raise HTTPException(status_code=401, detail="bad tool key")


def _money(cents: int) -> str:
    return "$" + format(cents / 100, ".2f")


def _digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


# --------------------------------------------------------------------------
# Item matching. The transcriber gives us what the caller said, not a SKU, so
# match on name, then aliases, then fuzzily. keyterms in the agent file keep
# "tres leches" from arriving as "trey lecher" in the first place.
# --------------------------------------------------------------------------
_DIETARY = ("gluten free", "gluten-free", "vegan", "dairy free", "dairy-free", "nut free",
            "nut-free", "sugar free", "sugar-free", "lactose free", "egg free", "eggless", "keto",
            "halal", "kosher")


def _dietary(spoken: str) -> list:
    text = (spoken or "").lower()
    return [q for q in _DIETARY if q in text]


def _find_item(spoken: str):
    """Match what the caller said to a menu row. A dietary qualifier is never
    fuzzed away: "gluten free cupcakes" must not match the ordinary cupcakes."""
    row = _match_item(spoken)
    wanted = _dietary(spoken)
    if row and wanted:
        described = (row["name"] + " " + " ".join(json.loads(row["aliases"]))).lower()
        if not all(q.replace("-", " ") in described.replace("-", " ") for q in wanted):
            return None
    return row


def _match_item(spoken: str):
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
# Pickup-time parsing. The model does not know today's date, so asking it for
# an ISO timestamp invites a confident guess at the wrong day. Instead it
# passes the time as the caller said it ("tomorrow at four", "Saturday
# 10:30am") and the shop's clock resolves it. ISO still works, which is what
# create_order sends back after a slot is confirmed.
# --------------------------------------------------------------------------
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_CLOCK = re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?(?![\d])")


def _parse_time(value: str):
    if not value:
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return _parse_spoken_time(text.lower())


_NUMBER_WORDS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve".split())}
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MONTH_DAY = re.compile(r"\b(" + "|".join(_MONTHS) + r")[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b")


def _parse_spoken_time(text: str):
    now = db.now()
    # Transcripts usually carry digits, but the model may pass words through.
    text = re.sub(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", lambda m: _NUMBER_WORDS[m.group(1)], text)
    text = re.sub(r"\b(\d{1,2}) (thirty|fifteen|forty[- ]five)\b",
                  lambda m: m.group(1) + ":" + {"thirty": "30", "fifteen": "15"}.get(m.group(2), "45"),
                  text)
    day = None
    month_day = _MONTH_DAY.search(text)
    if month_day:
        month = _MONTHS.index(month_day.group(1)) + 1
        year = now.year + (1 if month < now.month else 0)
        try:
            day = datetime(year, month, int(month_day.group(2))).date()
        except ValueError:
            return None
        text = text[:month_day.start()] + text[month_day.end():]  # keep its digits out of the clock
    elif "day after tomorrow" in text:
        day = now.date() + timedelta(days=2)
    elif "tomorrow" in text:
        day = now.date() + timedelta(days=1)
    elif "today" in text or "tonight" in text or "this afternoon" in text or "this evening" in text:
        day = now.date()
    else:
        for index, name in enumerate(_WEEKDAYS):
            if name in text:
                ahead = (index - now.weekday()) % 7
                if ahead == 0 and "next" in text:
                    ahead = 7
                day = now.date() + timedelta(days=ahead)
                break

    if "noon" in text or "midday" in text:
        hour, minute = 12, 0
    else:
        # Skip digits that belong to a date, like the 26 in "Sept 26 at 3pm".
        clock = None
        for match in _CLOCK.finditer(text):
            if match.group(2) or match.group(3) or "at " + match.group(0).strip() in text:
                clock = match
                break
            clock = clock or match
        if not clock:
            return None
        hour = int(clock.group(1))
        minute = int(clock.group(2) or 0)
        meridiem = (clock.group(3) or "").replace(".", "")
        if hour > 23 or minute > 59:
            return None
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        elif not meridiem and 1 <= hour <= 7:
            hour += 12  # "at four" in a shop open nine to seven means 4pm
        if "half past" in text:
            minute = 30
        elif "quarter past" in text:
            minute = 15

    if day is None:
        when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when < now:
            when += timedelta(days=1)
        return when
    return datetime(day.year, day.month, day.day, hour, minute)


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
    today = db.now().date()
    if when.date() == today:
        day = "today"
    elif when.date() == today + timedelta(days=1):
        day = "tomorrow"
    elif when.date() - today < timedelta(days=7):
        day = when.strftime("%A")
    else:
        day = when.strftime("%A %B ") + str(when.day)
    clock = when.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
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
        db.log_demand(item, None, "not_on_menu", quantity)
        _publish("demand", {"spoken": item, "reason": "not_on_menu"})
        wanted = _dietary(item)
        if wanted:
            return {"available": False,
                    "reason": "we have no " + " ".join(wanted) + " version",
                    "say": "offer a call-back so the owner can talk through dietary options"}
        names = [r["name"] for r in db.menu_rows() if r["stock"] > 0][:5]
        return {"available": False, "reason": "not on the menu", "we_do_have": names}

    if row["stock"] >= quantity:
        return {
            "available": True,
            "item": row["name"],
            "quantity": quantity,
            "unit": row["unit"],
            "unit_price": _money(row["price_cents"]),
            "line_total": _money(row["price_cents"] * quantity),
        }

    reason = "sold_out" if row["stock"] == 0 else "not_enough"
    db.log_demand(item, row["name"], reason, quantity)
    _publish("demand", {"spoken": item, "item": row["name"], "reason": reason})
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
    now = db.now()
    it_is = _spoken_time(now).replace("today at ", "")
    if not when:
        return {"ok": False, "reason": "could not read that time, ask for a day and a time",
                "hours": "9am to 7pm daily", "it_is_now": it_is}

    earliest = now + timedelta(minutes=db.LEAD_TIME_MINUTES)
    if when < earliest:
        slot = _next_open_slot(earliest)
        return {"ok": False, "reason": "too soon to bake, custom orders need 90 minutes",
                "nearest_slot": _spoken_time(slot), "nearest_slot_iso": slot.isoformat(),
                "it_is_now": it_is}

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
    phone = _digits(payload.get("phone"))
    raw_items = payload.get("items") or []
    when = _parse_time(str(payload.get("pickup_time", "")))

    if not name or not phone or not raw_items or not when:
        return {"ok": False, "reason": "missing name, phone, items or pickup time"}
    if len(phone) < 7:
        return {"ok": False, "reason": "phone number looks incomplete"}
    looked_up = _recent_lookup(phone)
    if not looked_up:
        return {"ok": False, "reason": "call lookup_customer with this phone number first,"
                " then call create_order again"}
    on_file = looked_up[1]
    if on_file and on_file.split(" ")[0].lower() != name.split(" ")[0].lower()             and not payload.get("name_confirmed"):
        return {"ok": False, "reason": f"this number is on file for {on_file}. Ask the caller"
                " which name the order is for, then call create_order again with"
                " name_confirmed true"}

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
    order_no = _digits(payload.get("order_id"))
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


# Numbers looked up recently, so create_order can insist the model checked
# who it is talking to rather than inventing a regular. Phone tail -> (when,
# the name on file or None).
_LOOKUPS: dict = {}
LOOKUP_TTL = timedelta(minutes=20)


def _remember_lookup(phone: str, customer: dict | None) -> None:
    tail = _digits(phone)[-10:]
    if len(tail) >= 7:
        _LOOKUPS[tail] = (datetime.now(), customer["name"] if customer else None)


def _recent_lookup(phone: str):
    hit = _LOOKUPS.get(_digits(phone)[-10:])
    if hit and datetime.now() - hit[0] < LOOKUP_TTL:
        return hit
    return None


@app.post("/tools/lookup_customer")
def lookup_customer(payload: dict = Body(...), x_tool_key: str = Header(None)):
    """The browser has no caller ID, so the agent asks for the number and looks
    it up, the way a pizza shop pulls up a regular. On the phone, the
    pre-connect request below does this before the first ring is answered.

    The welcome line is written here, not by the model: a model primed to
    greet regulars will otherwise greet one who does not exist."""
    _require_tool_key(x_tool_key)
    phone = str(payload.get("phone", ""))
    customer = db.customer_by_phone(phone)
    _remember_lookup(phone, customer)
    if not customer:
        return {"found": False, "say": "Thanks. And what name should I put the order under?"}
    usual = customer["usual"]
    say = f"Welcome back, {customer['first_name']}."
    say += f" Is it the {usual.lower()} again, or something different today?" if usual         else " What can I get for you today?"
    return {"found": True, "name": customer["name"], "orders_before": customer["orders"],
            "usual": usual, "last_order": customer["last_order"], "say": say}


@app.post("/tools/request_callback")
def request_callback(payload: dict = Body(...), x_tool_key: str = Header(None)):
    """Anything the agent must not answer (allergies, complaints, questions it
    has no tool for) goes on the owner's call-back list instead of a guess."""
    _require_tool_key(x_tool_key)
    name = str(payload.get("customer_name", "")).strip()
    phone = _digits(payload.get("phone"))
    reason = str(payload.get("reason", "")).strip()
    if name.lower() in ("", "unknown", "caller", "customer", "n/a", "none"):
        return {"ok": False, "reason": "ask the caller for their name first"}
    if len(phone) < 7:
        return {"ok": False, "reason": "need a full phone number to call back"}
    if not reason:
        return {"ok": False, "reason": "say what the owner should call about"}
    row = db.add_callback(name, phone, reason)
    _publish("callback", row)
    return {"ok": True, "promise": "the owner calls back today before closing"}


# --------------------------------------------------------------------------
# Pre-connect: runs before a phone call is answered, with the caller's number.
# AssemblyAI gives it 800 ms and fails open, so this is one indexed query.
# https://www.assemblyai.com/docs/voice-agents/voice-agent-api/pre-connect-requests
# --------------------------------------------------------------------------
@app.post("/voice/pre_connect")
def pre_connect(payload: dict = Body(default={}), x_tool_key: str = Header(None)):
    _require_tool_key(x_tool_key)
    customer = db.customer_by_phone(str(payload.get("caller_number", "")))
    _remember_lookup(str(payload.get("caller_number", "")), customer)
    if not customer:
        return {}  # the agent's own greeting plays; nothing is captured
    usual = customer["usual"]
    greeting = f"Rosewood Bakery, hi {customer['first_name']}."
    greeting += f" Is it the {usual.lower()} again, or something new?" if usual \
        else " What can I get started for you?"
    return {
        "customer": {
            "name": customer["name"],
            "phone": customer["phone"],
            "usual": usual,
            "last_order": customer["last_order"],
        },
        "greeting": greeting,
    }


# --------------------------------------------------------------------------
# Webhooks and post-call analysis. A session ends, AssemblyAI signs and posts
# the event, we fetch the recording's timeline from the Sessions API and have
# the LLM Gateway say what happened on the call.
# https://www.assemblyai.com/docs/voice-agents/voice-agent-api/webhooks
# --------------------------------------------------------------------------
def _verify_signature(raw: bytes, header: str | None) -> bool:
    if not WEBHOOK_SECRET:
        return False
    fields = {}
    for pair in (header or "").split(","):
        key, sep, value = pair.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    try:
        stamp = int(fields.get("t", ""))
    except ValueError:
        return False
    expected = hmac.new(WEBHOOK_SECRET.encode(), f"{stamp}.".encode() + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, fields.get("v1", "")) and abs(time.time() - stamp) <= 300


@app.post("/webhooks/assemblyai")
async def webhook(request: Request, background: BackgroundTasks,
                  x_aai_signature: str = Header(None)):
    raw = await request.body()
    if not _verify_signature(raw, x_aai_signature):
        raise HTTPException(status_code=401, detail="invalid signature")
    event = json.loads(raw)
    if db.seen_event(event.get("event_id", "")):
        return {"ok": True, "duplicate": True}

    kind = event.get("event", "")
    if kind == "session.completed":
        s = event.get("session") or {}
        if AGENT_ID and s.get("agent_id") not in (None, AGENT_ID):
            return {"ok": True, "ignored": "another agent"}
        db.upsert_call(s["session_id"], channel="browser", started_at=s.get("created_at"),
                       duration_s=s.get("duration_seconds"))
        background.add_task(_analyse_session, s["session_id"])
    elif kind in ("call.ended", "call.failed"):
        c = event.get("call") or {}
        if AGENT_ID and c.get("agent_id") not in (None, AGENT_ID):
            return {"ok": True, "ignored": "another agent"}
        db.upsert_call(c["session_id"], channel="phone", from_number=c.get("from_number"),
                       started_at=c.get("created_at"))
        if kind == "call.ended":
            background.add_task(_analyse_session, c["session_id"])
        else:
            db.upsert_call(c["session_id"], state="failed", error="call failed")
    # Acknowledge fast; the analysis runs after the response is sent.
    return {"ok": True}


def _get_json(url: str, auth: bool = True, timeout: int = 20) -> Any:
    headers = {"Authorization": ASSEMBLYAI_API_KEY} if auth else {}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _analyse_session(session_id: str) -> dict:
    """Wait for the timeline artifact (written up to ~90 s after the session
    ends), measure the call, then summarise it. Safe to run twice."""
    try:
        session = {}
        for _ in range(36):  # three minutes at five-second intervals
            session = _get_json("https://agents.assemblyai.com/v1/sessions/" + session_id)
            if any(a.get("type") == "timeline" for a in session.get("artifacts") or []):
                break
            time.sleep(5)
        timeline_url = next((a["url"] for a in session.get("artifacts") or []
                             if a.get("type") == "timeline"), None)
        if not timeline_url:
            return db.upsert_call(session_id, state="failed", error="no timeline after 3 minutes")
        timeline = _get_json(timeline_url, auth=False)

        transcript, first_audio, tool_ms, tool_count, turns = _flatten(timeline)
        fields = dict(
            started_at=session.get("created_at"),
            duration_s=session.get("duration_seconds"),
            turns=turns,
            tool_calls=tool_count,
            first_audio_ms=_median(first_audio),
            tool_ms=_median(tool_ms),
        )
        if not transcript.strip():
            row = db.upsert_call(session_id, **fields, outcome="abandoned", sentiment="neutral",
                                 summary="Nobody spoke on this call.", state="done")
        else:
            row = db.upsert_call(session_id, **fields, **_summarise(transcript), state="done")
    except Exception as err:  # noqa: BLE001 - one bad call must not stop the next
        row = db.upsert_call(session_id, state="failed", error=str(err)[:300])
    _publish("call", row)
    return row


def _flatten(timeline: dict):
    lines, first_audio, tool_ms, tool_count, turns = [], [], [], 0, 0
    for turn in timeline.get("turns", []):
        if turn.get("user_transcript"):
            lines.append("Caller: " + turn["user_transcript"])
            turns += 1
        for call_ in turn.get("tool_calls", []):
            tool_count += 1
            if call_.get("duration_ms") is not None:
                tool_ms.append(call_["duration_ms"])
            lines.append(f"[tool {call_.get('name')} {json.dumps(call_.get('arguments'))}"
                         f" -> {str(call_.get('result'))[:300]}]")
        if turn.get("agent_text"):
            lines.append("Agent: " + turn["agent_text"])
        # The greeting has no caller turn to answer, so it says nothing about
        # responsiveness; every reply to a caller does.
        if turn.get("trigger") != "greeting" and turn.get("time_to_first_audio_ms") is not None:
            first_audio.append(turn["time_to_first_audio_ms"])
    return "\n".join(lines), first_audio, tool_ms, tool_count, turns


def _median(values: list):
    values = sorted(v for v in values if v is not None)
    return int(values[len(values) // 2]) if values else None


SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["order_placed", "status_check", "callback",
                                               "enquiry", "no_sale", "abandoned"]},
        "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
        "summary": {"type": "string"},
        "follow_up": {"type": "string"},
        "unmet": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["outcome", "sentiment", "summary", "follow_up", "unmet"],
    "additionalProperties": False,
}

SUMMARY_PROMPT = (
    "You review phone calls to a bakery's AI order line for the owner. From the transcript,"
    " return: outcome (order_placed if create_order succeeded, status_check, callback if a"
    " call-back was requested, enquiry for questions only, no_sale if the caller wanted"
    " something and left without ordering, abandoned if they hung up early); sentiment of the"
    " caller; summary, one plain sentence the owner can skim; follow_up, what the owner should"
    " do next, or an empty string if nothing; unmet, short names of things the caller asked"
    " for that the bakery could not provide (sold out, not on the menu, services like"
    " delivery), empty if none."
)


def _gateway(body: dict) -> str:
    req = urllib.request.Request(
        LLM_GATEWAY + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": ASSEMBLYAI_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as err:
        raise GatewayError(err.code, err.read().decode()[:300]) from None


class GatewayError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"LLM Gateway {status}: {body}")
        self.status = status


def _summarise(transcript: str) -> dict:
    """Structured output where the model supports it; otherwise ask for JSON
    in the prompt and let the gateway's json-repair step tidy it."""
    messages = [
        {"role": "system", "content": SUMMARY_PROMPT},
        {"role": "user", "content": transcript[:24000]},
    ]
    try:
        content = _gateway({
            "model": SUMMARY_MODEL, "messages": messages, "max_tokens": 600,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "call_review", "schema": SUMMARY_SCHEMA,
                                                "strict": True}},
        })
    except GatewayError as err:
        if err.status != 400:
            raise
        messages[0] = {"role": "system", "content": SUMMARY_PROMPT + (
            " Reply with only a JSON object with the keys outcome, sentiment, summary,"
            " follow_up and unmet, and nothing else.")}
        content = _gateway({
            "model": SUMMARY_MODEL, "messages": messages, "max_tokens": 600,
            "post_processing_steps": [{"type": "json-repair"}],
        })
    return _clean_review(content)


def _clean_review(content: str) -> dict:
    # Small models wrap JSON in a code fence or add a sentence around it.
    match = re.search(r"\{.*\}", content, re.S)
    review = json.loads(match.group(0) if match else content)
    props = SUMMARY_SCHEMA["properties"]
    outcome = str(review.get("outcome", "")).strip().lower()
    sentiment = str(review.get("sentiment", "")).strip().lower()
    unmet = review.get("unmet") or []
    return {
        "outcome": outcome if outcome in props["outcome"]["enum"] else "enquiry",
        "sentiment": sentiment if sentiment in props["sentiment"]["enum"] else "neutral",
        "summary": str(review.get("summary") or "").strip(),
        "follow_up": str(review.get("follow_up") or "").strip(),
        "unmet": [str(u) for u in unmet] if isinstance(unmet, list) else [str(unmet)],
    }


# --------------------------------------------------------------------------
# Keeping the speech recogniser in step with the menu. Every menu name and
# alias is a keyterm, so a new cake added on the dashboard is transcribed
# correctly on the very next call.
# https://www.assemblyai.com/docs/voice-agents/voice-agent-api/transcription-prompt
# --------------------------------------------------------------------------
def _agent_input() -> dict:
    """The input block from agents/bakery.jsonc, so the file stays the source
    of truth for everything except the menu-derived keyterms."""
    sys_path = str(AGENT_FILE.parent.parent)
    if sys_path not in sys.path:
        sys.path.insert(0, sys_path)
    from lib import parse_jsonc
    return parse_jsonc(AGENT_FILE.read_text(encoding="utf-8")).get("input", {})


def menu_keyterms(base: list | None = None) -> list:
    terms: list = []
    for term in (base or []) + [t for r in db.menu_rows()
                                for t in [r["name"], *json.loads(r["aliases"])]]:
        term = term.strip()
        if term and term.lower() not in {t.lower() for t in terms}:
            terms.append(term)
    return terms[:100]  # the API's limit


def sync_keyterms() -> dict:
    if not (AGENT_ID and ASSEMBLYAI_API_KEY):
        return {"synced": False, "reason": "AGENT_ID or ASSEMBLYAI_API_KEY not set"}
    agent_input = _agent_input()
    agent_input["keyterms"] = menu_keyterms(agent_input.get("keyterms"))
    req = urllib.request.Request(
        "https://agents.assemblyai.com/v1/agents/" + AGENT_ID,
        data=json.dumps({"input": agent_input}).encode(),
        headers={"Authorization": ASSEMBLYAI_API_KEY, "Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=20):
            pass
    except urllib.error.HTTPError as err:
        return {"synced": False, "reason": err.read().decode()[:300]}
    except urllib.error.URLError as err:
        return {"synced": False, "reason": str(err.reason)}
    return {"synced": True, "keyterms": len(agent_input["keyterms"])}


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


@app.post("/api/menu")
def api_add_item(payload: dict = Body(...)):
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if _find_item(name) and _find_item(name)["name"].lower() == name.lower():
        raise HTTPException(status_code=409, detail="already on the menu")
    try:
        price_cents = round(float(payload.get("price", 0)) * 100)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="price must be a number")
    aliases = [a.strip() for a in str(payload.get("aliases", "")).split(",") if a.strip()]
    row = db.add_item(name, str(payload.get("category") or "pastry"), price_cents,
                      str(payload.get("unit") or "each"), int(payload.get("stock") or 0), aliases,
                      str(payload.get("alternative") or "") or None)
    _publish("menu", row)
    return {"item": row, "agent": sync_keyterms()}


@app.post("/api/menu/{item_id}/stock")
def api_set_stock(item_id: int, payload: dict = Body(...)):
    row = db.set_stock(item_id, int(payload.get("stock", 0)))
    if not row:
        raise HTTPException(status_code=404, detail="no such item")
    _publish("menu", row)
    return row


@app.post("/api/agent/sync")
def api_sync_agent():
    return sync_keyterms()


@app.get("/api/insights")
def api_insights(days: int = 7):
    return {"totals": db.insights(days), "demand": db.demand_summary(days),
            "callbacks": db.callbacks("open"), "calls": db.calls(20)}


@app.post("/api/callbacks/{callback_id}")
def api_callback_done(callback_id: int, payload: dict = Body(default={})):
    row = db.set_callback_status(callback_id, str(payload.get("status") or "done"))
    if not row:
        raise HTTPException(status_code=404, detail="no such call-back")
    _publish("callback", row)
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
    try:
        return _get_json("https://agents.assemblyai.com" + path)
    except urllib.error.HTTPError as err:
        raise HTTPException(status_code=err.code, detail=err.read().decode()[:400])
    except urllib.error.URLError as err:
        raise HTTPException(status_code=502, detail=str(err.reason))


@app.get("/api/calls")
def api_calls(limit: int = 25):
    path = "/v1/sessions?limit=" + str(limit)
    if AGENT_ID:
        path += "&agent_id=" + AGENT_ID
    data = _aai(path)
    reviewed = {c["session_id"]: c for c in db.calls(200)}
    for s in data.get("sessions", []):
        s["review"] = reviewed.get(s.get("id"))
    return data


@app.post("/api/calls/{session_id}/analyse")
def api_analyse(session_id: str):
    """Review a call on demand, for sessions from before the webhook existed.
    Runs in a thread so the button returns at once; the result arrives over SSE."""
    db.upsert_call(session_id, state="pending")
    threading.Thread(target=_analyse_session, args=(session_id,), daemon=True).start()
    return {"ok": True}


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
    return {"id": AGENT_ID, "name": agent.get("name") or "agent",
            "phone": os.environ.get("TWILIO_PHONE_NUMBER", "")}


@app.post("/api/live")
def api_live(payload: dict = Body(...)):
    """The calling page mirrors its transcript here so a second screen — the
    projector in a demo — shows the same call as it happens."""
    _publish("live", payload)
    return {"ok": True}


@app.get("/api/calls/{session_id}")
def api_call(session_id: str):
    data = _aai("/v1/sessions/" + session_id)
    data["review"] = db.call(session_id)
    return data


@app.get("/api/calls/{session_id}/timeline")
def api_call_timeline(session_id: str):
    """The timeline artifact is a pre-signed S3 URL; fetching it here keeps
    the page free of CORS and expiry surprises."""
    session = _aai("/v1/sessions/" + session_id)
    url = next((a["url"] for a in session.get("artifacts") or [] if a.get("type") == "timeline"), None)
    if not url:
        return {"turns": []}
    return _get_json(url, auth=False)


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "rosewood"}


@app.get("/")
def dashboard():
    return FileResponse(STATIC / "index.html")
