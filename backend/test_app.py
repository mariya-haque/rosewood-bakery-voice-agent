"""The tools are what the caller hears, so they are what gets tested.

    cd backend
    .venv/Scripts/python -m pytest -q        # Windows
    .venv/bin/python -m pytest -q            # macOS, Linux

No network: AssemblyAI and the LLM Gateway are replaced with fakes.
"""
import hashlib
import hmac
import json
import os
import tempfile
import time
from datetime import datetime, timedelta

import pytest

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["TOOL_API_KEY"] = "test-key"
os.environ["AAI_WEBHOOK_SECRET"] = "s" * 40
os.environ.pop("SHOP_TZ", None)

import app  # noqa: E402
import db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

app.TOOL_API_KEY = "test-key"
app.WEBHOOK_SECRET = "s" * 40
app.AGENT_ID = "agent-1"
KEY = {"X-Tool-Key": "test-key"}


@pytest.fixture(autouse=True)
def fresh_db():
    if os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db.init()
    app._LOOKUPS.clear()
    yield


@pytest.fixture
def client():
    with TestClient(app.app) as c:
        yield c


def tool(client, name, **payload):
    res = client.post(f"/tools/{name}", json=payload, headers=KEY)
    assert res.status_code == 200, res.text
    return res.json()


def future(hours=26, hour=15):
    when = (db.now() + timedelta(hours=hours)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return when


# --- auth -----------------------------------------------------------------------


def test_tools_reject_a_missing_key(client):
    assert client.post("/tools/check_availability", json={"item": "baguette"}).status_code == 401


# --- availability and demand ----------------------------------------------------------


def test_alias_and_fuzzy_matching(client):
    assert tool(client, "check_availability", item="tres leches")["item"] == "Tres leches cake"
    assert tool(client, "check_availability", item="red velvit cake")["item"] == "Red velvet cake"


def test_sold_out_offers_alternative_and_logs_demand(client):
    out = tool(client, "check_availability", item="black forest")
    assert out["available"] is False and out["reason"] == "sold out"
    assert out["alternative"]["item"] == "Chocolate fudge cake"
    [row] = db.demand_summary()
    assert row["what"] == "Black forest cake" and row["reason"] == "sold_out"


def test_dietary_qualifier_is_never_fuzzed_away(client):
    # "dozen cupcakes" is a close fuzzy match, but it is not gluten free.
    out = tool(client, "check_availability", item="gluten free cupcakes")
    assert out["available"] is False and "gluten free" in out["reason"]
    assert "call-back" in out["say"]
    assert db.demand_summary()[0]["what"] == "gluten free cupcakes"
    # And create_order refuses it too, whatever the model sends.
    tool(client, "lookup_customer", phone="4155550100")
    res = tool(client, "create_order", customer_name="A", phone="4155550100",
               items=[{"item": "vegan red velvet cake", "quantity": 1}],
               pickup_time=future().isoformat())
    assert res["ok"] is False


def test_unknown_item_is_logged_as_unmet_demand(client):
    out = tool(client, "check_availability", item="cinnamon roll")
    assert out["reason"] == "not on the menu" and out["we_do_have"]
    assert db.demand_summary()[0]["what"] == "cinnamon roll"


# --- pickup times -----------------------------------------------------------------


def test_spoken_times_resolve_against_the_shop_clock():
    now = db.now()
    assert app._parse_time("tomorrow at 4") == datetime(
        *(now + timedelta(days=1)).timetuple()[:3], 16, 0)
    assert app._parse_time("tomorrow at four thirty").minute == 30
    assert app._parse_time("tomorrow 10:30am").hour == 10
    assert app._parse_time("sometime soon") is None


def test_slot_too_soon_offers_the_nearest(client):
    out = tool(client, "check_pickup_slot", time=(db.now() + timedelta(minutes=10)).isoformat())
    assert out["ok"] is False and "nearest_slot_iso" in out


def test_slot_out_of_hours_offers_the_nearest(client):
    out = tool(client, "check_pickup_slot", time=future(hour=21).isoformat())
    assert out["ok"] is False and out["reason"] == "closed then"


def test_slot_ok_returns_iso_for_create_order(client):
    out = tool(client, "check_pickup_slot", time="tomorrow at 3pm")
    assert out["ok"] is True
    assert datetime.fromisoformat(out["pickup_time_iso"]).hour == 15


# --- orders -----------------------------------------------------------------------


def order(client, phone="4155550142", item="Butter croissant", qty=2, name="Dana Whitfield"):
    tool(client, "lookup_customer", phone=phone)
    return tool(client, "create_order", customer_name=name, phone=phone,
                items=[{"item": item, "quantity": qty}], pickup_time=future().isoformat())


def test_create_order_requires_a_lookup_first(client):
    # A model that skips the lookup cannot put an invented customer on the board.
    res = tool(client, "create_order", customer_name="Sarah", phone="4155550142",
               items=[{"item": "Sourdough loaf", "quantity": 2}], pickup_time=future().isoformat())
    assert res["ok"] is False and "lookup_customer" in res["reason"]


def test_name_that_does_not_match_the_number_on_file_needs_confirming(client):
    order(client)  # Dana is now on file
    app._LOOKUPS.clear()
    found = tool(client, "lookup_customer", phone="4155550142")
    assert found["say"].startswith("Welcome back, Dana.")
    res = tool(client, "create_order", customer_name="Sarah", phone="4155550142",
               items=[{"item": "Sourdough loaf", "quantity": 2}], pickup_time=future().isoformat())
    assert res["ok"] is False and "Dana Whitfield" in res["reason"]
    res = tool(client, "create_order", customer_name="Sarah", phone="4155550142", name_confirmed=True,
               items=[{"item": "Sourdough loaf", "quantity": 2}], pickup_time=future().isoformat())
    assert res["ok"] is True


def test_unknown_number_gets_a_scripted_line_without_a_welcome(client):
    out = tool(client, "lookup_customer", phone="2125550000")
    assert out["found"] is False and "welcome" not in out["say"].lower()


def test_create_order_prices_decrements_stock_and_is_findable(client):
    before = next(r for r in db.menu_rows() if r["name"] == "Butter croissant")["stock"]
    out = order(client)
    assert out["ok"] and out["total"] == "$9.00" and len(out["order_number"]) == 4
    after = next(r for r in db.menu_rows() if r["name"] == "Butter croissant")["stock"]
    assert after == before - 2
    status = tool(client, "get_order_status", order_id=" ".join(out["order_number"]))
    assert status["found"] and status["status"] == "new"


def test_create_order_refuses_more_than_stock(client):
    out = order(client, item="Tres leches cake", qty=50)
    assert out["ok"] is False and "no longer has" in out["reason"]


# --- regulars -------------------------------------------------------------------------


def test_lookup_finds_a_regular_and_their_usual(client):
    order(client)
    order(client)
    order(client, item="Sourdough loaf", qty=1)
    out = tool(client, "lookup_customer", phone="415 555 0142")
    assert out["found"] and out["name"] == "Dana Whitfield"
    assert out["usual"] == "Butter croissant" and out["orders_before"] == 3
    assert tool(client, "lookup_customer", phone="2125550000")["found"] is False


def test_pre_connect_matches_e164_and_writes_the_greeting(client):
    order(client)
    out = client.post("/voice/pre_connect", json={"caller_number": "+14155550142"},
                      headers=KEY).json()
    assert out["customer"]["name"] == "Dana Whitfield"
    assert "Dana" in out["greeting"] and "butter croissant" in out["greeting"]
    # Unknown callers get nothing back, so the agent's own greeting plays.
    assert client.post("/voice/pre_connect", json={"caller_number": "+12125550000"},
                       headers=KEY).json() == {}


# --- call-backs --------------------------------------------------------------------------


def test_callback_needs_a_number_and_lands_on_the_list(client):
    assert tool(client, "request_callback", customer_name="Marcus", phone="12",
                reason="nut allergy")["ok"] is False
    assert tool(client, "request_callback", customer_name="Marcus", phone="4155550177",
                reason="nut allergy")["ok"] is True
    [cb] = db.callbacks()
    assert cb["reason"] == "nut allergy"
    # A placeholder name is refused, and a second request from the same number
    # updates the first rather than doubling the owner's list.
    assert tool(client, "request_callback", customer_name="Unknown", phone="4155550177",
                reason="nut allergy")["ok"] is False
    tool(client, "request_callback", customer_name="Marcus Lee", phone="4155550177",
         reason="tree nut allergy, almond croissant")
    [cb] = db.callbacks()
    assert cb["customer_name"] == "Marcus Lee" and "almond" in cb["reason"]
    client.post(f"/api/callbacks/{cb['id']}", json={"status": "done"})
    assert db.callbacks() == []


# --- webhooks and post-call review ---------------------------------------------------


def signed(body: dict, secret="s" * 40, stamp=None):
    raw = json.dumps(body).encode()
    stamp = stamp or int(time.time())
    sig = hmac.new(secret.encode(), f"{stamp}.".encode() + raw, hashlib.sha256).hexdigest()
    return raw, {"X-AAI-Signature": f"t={stamp},v1={sig}", "Content-Type": "application/json"}


EVENT = {"event_id": "ev-1", "event": "session.completed",
         "session": {"session_id": "sess_1", "agent_id": "agent-1", "duration_seconds": 64.2,
                     "created_at": "2026-09-24T10:00:00Z"}}


def test_webhook_rejects_bad_and_stale_signatures(client):
    raw, headers = signed(EVENT, secret="wrong" * 8)
    assert client.post("/webhooks/assemblyai", content=raw, headers=headers).status_code == 401
    raw, headers = signed(EVENT, stamp=int(time.time()) - 900)
    assert client.post("/webhooks/assemblyai", content=raw, headers=headers).status_code == 401


def test_webhook_reviews_the_call_once(client, monkeypatch):
    timeline = {"turns": [
        {"trigger": "greeting", "agent_text": "Rosewood Bakery.", "time_to_first_audio_ms": 400},
        {"trigger": "user_speech", "user_transcript": "Two croissants for tomorrow at ten.",
         "agent_text": "Done, order 4821.", "time_to_first_audio_ms": 900,
         "tool_calls": [{"name": "create_order", "arguments": {}, "result": "{\"ok\": true}",
                         "duration_ms": 120, "is_error": False}]},
        {"trigger": "user_speech", "user_transcript": "Thanks.", "agent_text": "Bye.",
         "time_to_first_audio_ms": 700},
    ]}
    session = {"id": "sess_1", "duration_seconds": 64.2, "created_at": "2026-09-24T10:00:00Z",
               "artifacts": [{"type": "timeline", "url": "https://s3/timeline.json"}]}
    monkeypatch.setattr(app, "_get_json", lambda url, auth=True, timeout=20:
                        timeline if url.endswith("timeline.json") else session)
    reviews = []

    def fake_summary(transcript):
        reviews.append(transcript)
        return {"outcome": "order_placed", "sentiment": "positive",
                "summary": "Two croissants for tomorrow.", "follow_up": "", "unmet": []}

    monkeypatch.setattr(app, "_summarise", fake_summary)

    raw, headers = signed(EVENT)
    assert client.post("/webhooks/assemblyai", content=raw, headers=headers).json() == {"ok": True}
    raw, headers = signed(EVENT)
    assert client.post("/webhooks/assemblyai", content=raw, headers=headers).json()["duplicate"]

    row = db.call("sess_1")
    assert row["state"] == "done" and row["outcome"] == "order_placed"
    assert row["turns"] == 2 and row["tool_calls"] == 1
    assert row["first_audio_ms"] == 900  # the greeting is not counted
    assert len(reviews) == 1 and "Caller: Two croissants" in reviews[0]

    totals = client.get("/api/insights").json()["totals"]
    assert totals["calls"] == 1 and totals["calls_with_order"] == 1


def test_webhook_ignores_other_agents(client):
    other = {**EVENT, "event_id": "ev-2", "session": {**EVENT["session"], "agent_id": "agent-2"}}
    raw, headers = signed(other)
    assert client.post("/webhooks/assemblyai", content=raw, headers=headers).json()["ignored"]


# --- menu and keyterms -------------------------------------------------------------------


def test_new_menu_item_becomes_a_keyterm(client, monkeypatch):
    monkeypatch.setattr(app, "sync_keyterms", lambda: {"synced": True})
    res = client.post("/api/menu", json={"name": "Pain au chocolat", "price": "4.75",
                                         "stock": 12, "aliases": "chocolate croissant"})
    assert res.status_code == 200 and res.json()["agent"]["synced"]
    terms = app.menu_keyterms(["Rosewood Bakery"])
    assert "Pain au chocolat" in terms and "chocolate croissant" in terms
    assert terms[0] == "Rosewood Bakery" and len(terms) == len({t.lower() for t in terms})
    assert tool(client, "check_availability", item="pain au chocolat")["unit_price"] == "$4.75"


def test_agent_input_block_reads_from_the_agent_file():
    agent_input = app._agent_input()
    assert agent_input["voice_focus"] == "near-field" and "Rosewood Bakery" in agent_input["keyterms"]


def test_seed_demo_creates_regulars():
    import seed_demo
    assert seed_demo.seed() is True
    assert db.customer_by_phone("+15105550199")["usual"] == "Sourdough loaf"
    assert seed_demo.seed() is False
