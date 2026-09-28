"""SQLite storage for the bakery agent: menu, stock, orders.

One file, no ORM. DB_PATH points at a persistent disk in production; on Render
the free tier's filesystem is ephemeral, so set DB_PATH to a mounted disk or
the orders board empties on every redeploy.
"""
import json
import os
import random
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(os.environ.get("DB_PATH") or Path(__file__).parent / "bakery.db")

# Shop hours, used by both the slot checker and the system prompt.
OPEN_HOUR = 9
CLOSE_HOUR = 19
SLOT_MINUTES = 30
SLOT_CAPACITY = 4          # orders that can be picked up in one 30-minute slot
LEAD_TIME_MINUTES = 90     # earliest pickup from now, so the kitchen can bake

_lock = threading.Lock()


def now() -> datetime:
    """Shop-local wall-clock time. A hosted server runs in UTC, but "tomorrow
    at four" means four o'clock where the bakery is, so set SHOP_TZ (an IANA
    name such as America/Los_Angeles) anywhere the server's clock is not the
    shop's."""
    name = os.environ.get("SHOP_TZ")
    if name:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(name)).replace(tzinfo=None)
        except Exception:
            pass
    return datetime.now()

SCHEMA = """
CREATE TABLE IF NOT EXISTS menu (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    category      TEXT NOT NULL,
    price_cents   INTEGER NOT NULL,
    unit          TEXT NOT NULL DEFAULT 'each',
    stock         INTEGER NOT NULL DEFAULT 0,
    aliases       TEXT NOT NULL DEFAULT '[]',
    alternative   TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no      TEXT NOT NULL UNIQUE,
    customer_name TEXT NOT NULL,
    phone         TEXT NOT NULL,
    items_json    TEXT NOT NULL,
    pickup_time   TEXT NOT NULL,
    notes         TEXT,
    status        TEXT NOT NULL DEFAULT 'new',
    total_cents   INTEGER NOT NULL DEFAULT 0,
    session_id    TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_pickup ON orders(pickup_time);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_phone ON orders(phone);

-- Every time a caller asks for something we could not sell. This is the data
-- a missed call never gives you: what people wanted and did not get.
CREATE TABLE IF NOT EXISTS demand (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    spoken        TEXT NOT NULL,
    item          TEXT,
    reason        TEXT NOT NULL,          -- not_on_menu | sold_out | not_enough
    quantity      INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

-- Questions the agent must not answer itself (allergies, complaints, anything
-- it does not know) become a call-back list for the owner.
CREATE TABLE IF NOT EXISTS callbacks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT NOT NULL,
    phone         TEXT NOT NULL,
    reason        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',
    created_at    TEXT NOT NULL
);

-- One row per finished call, filled in by the webhook and the post-call
-- analysis: what happened, how it went, and how fast the agent answered.
CREATE TABLE IF NOT EXISTS calls (
    session_id    TEXT PRIMARY KEY,
    channel       TEXT,                   -- browser | phone
    from_number   TEXT,
    started_at    TEXT,
    duration_s    REAL,
    outcome       TEXT,                   -- order_placed | status_check | callback | enquiry | no_sale | abandoned
    sentiment     TEXT,                   -- positive | neutral | negative
    summary       TEXT,
    follow_up     TEXT,
    unmet         TEXT NOT NULL DEFAULT '[]',
    turns         INTEGER,
    tool_calls    INTEGER,
    first_audio_ms INTEGER,               -- median time to first agent audio
    tool_ms       INTEGER,                -- median tool round trip
    state         TEXT NOT NULL DEFAULT 'pending',   -- pending | done | failed
    error         TEXT,
    updated_at    TEXT NOT NULL
);

-- Webhook deliveries are at-least-once; remember what we have seen.
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id      TEXT PRIMARY KEY,
    received_at   TEXT NOT NULL
);
"""

SEED = [
    # name, category, price_cents, unit, stock, aliases, alternative
    ("Red velvet cake",      "cake",   3800, "per kg", 6,
     ["red velvet", "redvelvet"], "Chocolate fudge cake"),
    ("Chocolate fudge cake", "cake",   3500, "per kg", 8,
     ["chocolate cake", "choc fudge", "fudge cake"], "Red velvet cake"),
    ("Tres leches cake",     "cake",   4200, "per kg", 3,
     ["tres leches", "three milks"], "Vanilla sponge cake"),
    ("Vanilla sponge cake",  "cake",   3200, "per kg", 10,
     ["vanilla cake", "sponge cake"], "Chocolate fudge cake"),
    ("Black forest cake",    "cake",   4000, "per kg", 0,
     ["black forest"], "Chocolate fudge cake"),
    ("Butter croissant",     "pastry",  450, "each", 24,
     ["croissant"], "Almond croissant"),
    ("Almond croissant",     "pastry",  550, "each", 12,
     ["almond"], "Butter croissant"),
    ("Blueberry muffin",     "pastry",  400, "each", 18,
     ["muffin", "blueberry"], "Chocolate chip cookie"),
    ("Chocolate chip cookie","pastry",  250, "each", 40,
     ["cookie", "choc chip cookie"], "Blueberry muffin"),
    ("Sourdough loaf",       "bread",   700, "each", 9,
     ["sourdough", "sour dough"], "Baguette"),
    ("Baguette",             "bread",   450, "each", 15,
     ["french bread"], "Sourdough loaf"),
    ("Cupcake box of 12",    "pastry", 2800, "per box", 5,
     ["cupcakes", "cupcake box", "dozen cupcakes"], "Blueberry muffin"),
]


@contextmanager
def connect():
    """Commit on success, roll back on error, and always close. A bare
    sqlite3 connection used as a context manager only commits, so the handle
    would linger until garbage collection and hold the file open."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        with conn:
            yield conn
    finally:
        conn.close()


def init() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock, connect() as conn:
        conn.executescript(SCHEMA)
        already = conn.execute("SELECT COUNT(*) AS n FROM menu").fetchone()["n"]
        if not already:
            conn.executemany(
                "INSERT INTO menu (name, category, price_cents, unit, stock, aliases, alternative)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(n, c, p, u, s, json.dumps(a), alt) for n, c, p, u, s, a, alt in SEED],
            )


def menu_rows() -> list:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM menu ORDER BY category, name")]


def set_stock(item_id: int, stock: int) -> dict | None:
    with _lock, connect() as conn:
        conn.execute("UPDATE menu SET stock = ? WHERE id = ?", (max(0, stock), item_id))
        row = conn.execute("SELECT * FROM menu WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def next_order_no() -> str:
    """Four digits, easy to read aloud and to repeat back."""
    with connect() as conn:
        for _ in range(50):
            candidate = f"{random.randint(1000, 9999)}"
            hit = conn.execute(
                "SELECT 1 FROM orders WHERE order_no = ?", (candidate,)
            ).fetchone()
            if not hit:
                return candidate
    raise RuntimeError("could not allocate an order number")


def slot_load(pickup: datetime) -> int:
    """How many orders already sit in the 30-minute window around `pickup`."""
    start = pickup.replace(second=0, microsecond=0)
    start -= timedelta(minutes=start.minute % SLOT_MINUTES)
    end = start + timedelta(minutes=SLOT_MINUTES)
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM orders"
            " WHERE status != 'cancelled' AND pickup_time >= ? AND pickup_time < ?",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
    return row["n"]


def create_order(customer_name, phone, items, pickup_time, notes, total_cents, session_id=None) -> str:
    order_no = next_order_no()
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO orders (order_no, customer_name, phone, items_json, pickup_time,"
            " notes, status, total_cents, session_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)",
            (order_no, customer_name, phone, json.dumps(items), pickup_time,
             notes, total_cents, session_id, now().isoformat(timespec="seconds")),
        )
        # Decrement stock for what was actually ordered.
        for line in items:
            conn.execute(
                "UPDATE menu SET stock = MAX(0, stock - ?) WHERE LOWER(name) = LOWER(?)",
                (int(line.get("quantity", 1)), line.get("item", "")),
            )
    return order_no


def order_by_no(order_no: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
    return _hydrate(row) if row else None


def orders(status: str | None = None, limit: int = 100) -> list:
    sql = "SELECT * FROM orders"
    args: tuple = ()
    if status:
        sql += " WHERE status = ?"
        args = (status,)
    sql += " ORDER BY datetime(created_at) DESC LIMIT ?"
    with connect() as conn:
        rows = conn.execute(sql, (*args, limit)).fetchall()
    return [_hydrate(r) for r in rows]


def set_status(order_no: str, status: str) -> dict | None:
    with _lock, connect() as conn:
        conn.execute("UPDATE orders SET status = ? WHERE order_no = ?", (status, order_no))
    return order_by_no(order_no)


def _hydrate(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["items"] = json.loads(d.pop("items_json"))
    return d


def _now() -> str:
    return now().isoformat(timespec="seconds")


# --- menu editing -------------------------------------------------------------


def add_item(name, category, price_cents, unit, stock, aliases, alternative=None) -> dict:
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO menu (name, category, price_cents, unit, stock, aliases, alternative)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, category, price_cents, unit, max(0, stock), json.dumps(aliases), alternative),
        )
        row = conn.execute("SELECT * FROM menu WHERE name = ?", (name,)).fetchone()
    return dict(row)


# --- customers ----------------------------------------------------------------


def _last_digits(phone: str) -> str:
    """Orders store digits as the caller read them; the phone network sends
    E.164. Comparing the last ten digits matches +1 510 555 0199 to 5105550199."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return digits[-10:]


def customer_by_phone(phone: str) -> dict | None:
    """A regular is anyone who has ordered before. Their usual is the item they
    have ordered most often."""
    tail = _last_digits(phone)
    if len(tail) < 7:
        return None
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE status != 'cancelled' AND phone LIKE ?"
            " ORDER BY datetime(created_at) DESC",
            ("%" + tail,),
        ).fetchall()
    if not rows:
        return None
    orders_ = [_hydrate(r) for r in rows]
    counts: dict = {}
    for o in orders_:
        for line in o["items"]:
            counts[line["item"]] = counts.get(line["item"], 0) + 1
    usual = max(counts, key=counts.get) if counts else ""
    last = orders_[0]
    return {
        "name": last["customer_name"],
        "first_name": last["customer_name"].split(" ")[0],
        "phone": last["phone"],
        "orders": len(orders_),
        "usual": usual,
        "last_order": ", ".join(f'{l["quantity"]} {l["item"]}' for l in last["items"]),
        "last_order_date": last["created_at"][:10],
    }


# --- demand and callbacks -------------------------------------------------------


def log_demand(spoken: str, item: str | None, reason: str, quantity: int = 1) -> None:
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO demand (spoken, item, reason, quantity, created_at) VALUES (?, ?, ?, ?, ?)",
            (spoken, item, reason, quantity, _now()),
        )


def demand_summary(days: int = 7) -> list:
    """Unmet requests grouped by what was asked for, most wanted first."""
    since = (now() - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as conn:
        rows = conn.execute(
            "SELECT COALESCE(item, LOWER(spoken)) AS what, reason,"
            " COUNT(*) AS asks, SUM(quantity) AS units, MAX(created_at) AS last_at"
            " FROM demand WHERE created_at >= ?"
            " GROUP BY what, reason ORDER BY asks DESC, last_at DESC LIMIT 20",
            (since,),
        ).fetchall()
    return [dict(r) for r in rows]


def add_callback(customer_name: str, phone: str, reason: str) -> dict:
    """One open call-back per number: a model that files the same request
    twice in a call updates the first instead of doubling the owner's list."""
    since = (now() - timedelta(minutes=30)).isoformat(timespec="seconds")
    with _lock, connect() as conn:
        existing = conn.execute(
            "SELECT id FROM callbacks WHERE phone = ? AND status = 'open' AND created_at >= ?"
            " ORDER BY id DESC LIMIT 1", (phone, since)).fetchone()
        if existing:
            conn.execute("UPDATE callbacks SET customer_name = ?, reason = ? WHERE id = ?",
                         (customer_name, reason, existing["id"]))
            callback_id = existing["id"]
        else:
            callback_id = conn.execute(
                "INSERT INTO callbacks (customer_name, phone, reason, created_at) VALUES (?, ?, ?, ?)",
                (customer_name, phone, reason, _now()),
            ).lastrowid
        row = conn.execute("SELECT * FROM callbacks WHERE id = ?", (callback_id,)).fetchone()
    return dict(row)


def callbacks(status: str | None = "open") -> list:
    sql = "SELECT * FROM callbacks"
    args: tuple = ()
    if status:
        sql += " WHERE status = ?"
        args = (status,)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id DESC LIMIT 100", args)]


def set_callback_status(callback_id: int, status: str) -> dict | None:
    with _lock, connect() as conn:
        conn.execute("UPDATE callbacks SET status = ? WHERE id = ?", (status, callback_id))
        row = conn.execute("SELECT * FROM callbacks WHERE id = ?", (callback_id,)).fetchone()
    return dict(row) if row else None


# --- calls and webhooks ---------------------------------------------------------


def seen_event(event_id: str) -> bool:
    """True if this webhook event was already handled; records it otherwise."""
    with _lock, connect() as conn:
        try:
            conn.execute("INSERT INTO webhook_events (event_id, received_at) VALUES (?, ?)",
                         (event_id, _now()))
            return False
        except sqlite3.IntegrityError:
            return True


CALL_FIELDS = ("channel", "from_number", "started_at", "duration_s", "outcome", "sentiment",
               "summary", "follow_up", "unmet", "turns", "tool_calls", "first_audio_ms",
               "tool_ms", "state", "error")


def upsert_call(session_id: str, **fields) -> dict:
    fields = {k: v for k, v in fields.items() if k in CALL_FIELDS}
    if "unmet" in fields and not isinstance(fields["unmet"], str):
        fields["unmet"] = json.dumps(fields["unmet"])
    with _lock, connect() as conn:
        conn.execute("INSERT OR IGNORE INTO calls (session_id, updated_at) VALUES (?, ?)",
                     (session_id, _now()))
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE calls SET {sets}, updated_at = ? WHERE session_id = ?",
                         (*fields.values(), _now(), session_id))
        row = conn.execute("SELECT * FROM calls WHERE session_id = ?", (session_id,)).fetchone()
    return _call(row)


def call(session_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM calls WHERE session_id = ?", (session_id,)).fetchone()
    return _call(row) if row else None


def calls(limit: int = 50) -> list:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM calls ORDER BY COALESCE(started_at, updated_at) DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_call(r) for r in rows]


def _call(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["unmet"] = json.loads(d.get("unmet") or "[]")
    return d


def insights(days: int = 7) -> dict:
    """The numbers the owner cares about: what the phone line earned this week."""
    since = (now() - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as conn:
        o = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(total_cents), 0) AS cents FROM orders"
            " WHERE status != 'cancelled' AND created_at >= ?", (since,)
        ).fetchone()
        c = conn.execute(
            "SELECT COUNT(*) AS n,"
            " SUM(CASE WHEN outcome = 'order_placed' THEN 1 ELSE 0 END) AS orders,"
            " COALESCE(SUM(duration_s), 0) AS seconds"
            " FROM calls WHERE state = 'done' AND COALESCE(started_at, updated_at) >= ?", (since,)
        ).fetchone()
        latencies = [r[0] for r in conn.execute(
            "SELECT first_audio_ms FROM calls WHERE first_audio_ms IS NOT NULL"
            " AND COALESCE(started_at, updated_at) >= ?", (since,))]
        open_callbacks = conn.execute(
            "SELECT COUNT(*) FROM callbacks WHERE status = 'open'").fetchone()[0]
        lost = conn.execute(
            "SELECT COUNT(*) FROM demand WHERE created_at >= ?", (since,)).fetchone()[0]
    latencies.sort()
    return {
        "days": days,
        "orders": o["n"],
        "revenue_cents": o["cents"],
        "calls": c["n"],
        "calls_with_order": c["orders"] or 0,
        "call_minutes": round((c["seconds"] or 0) / 60, 1),
        "median_first_audio_ms": latencies[len(latencies) // 2] if latencies else None,
        "open_callbacks": open_callbacks,
        "unmet_requests": lost,
    }
