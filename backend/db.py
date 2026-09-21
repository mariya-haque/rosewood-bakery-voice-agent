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
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(os.environ.get("DB_PATH", Path(__file__).parent / "bakery.db"))

# Shop hours, used by both the slot checker and the system prompt.
OPEN_HOUR = 9
CLOSE_HOUR = 19
SLOT_MINUTES = 30
SLOT_CAPACITY = 4          # orders that can be picked up in one 30-minute slot
LEAD_TIME_MINUTES = 90     # earliest pickup from now, so the kitchen can bake

_lock = threading.Lock()

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


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


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
             notes, total_cents, session_id, datetime.now().isoformat(timespec="seconds")),
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
