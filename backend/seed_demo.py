"""Sample history for a demo bakery, so the board, the regulars and the
insights have something to show before the first real call.

    python seed_demo.py            # adds sample data if there are no orders yet
    FORCE=1 python seed_demo.py    # adds it anyway

Or set DEMO_SEED=1 on the server and it runs on boot against an empty
database, which is what an ephemeral hosted disk needs. Every name, number and
request here is invented.
"""
import json
import os
import random
from datetime import timedelta

import db

# Regulars: the phone numbers to call from (or read out in the browser) in a
# demo, to hear the agent recognise someone.
REGULARS = [
    ("Dana Whitfield", "4155550142", [("Red velvet cake", 2, "2 kg, write Happy Birthday Mira")] * 1
     + [("Butter croissant", 6, "")] * 3),
    ("Sam Ortiz", "5105550199", [("Sourdough loaf", 2, "")] * 4),
    ("Priya Natarajan", "6505550123", [("Tres leches cake", 1, "1 kg, no message")]
     + [("Almond croissant", 4, "")] * 2),
]

# What callers asked for and did not get: the shape a real week produces.
DEMAND = [
    ("gluten free cake", None, "not_on_menu", 6),
    ("black forest cake", "Black forest cake", "sold_out", 5),
    ("vegan brownies", None, "not_on_menu", 3),
    ("delivery", None, "not_on_menu", 3),
    ("tres leches cake", "Tres leches cake", "not_enough", 2),
    ("cinnamon rolls", None, "not_on_menu", 2),
]

CALLBACKS = [
    ("Marcus Lee", "4155550177", "is the carrot cake safe for a walnut allergy"),
    ("Hannah Brooks", "4155550163", "catering quote for 60 pastries on Saturday"),
]


def seed(force: bool = False) -> bool:
    db.init()
    if db.orders() and not force:
        return False
    random.seed(7)
    now = db.now()
    menu = {r["name"]: r for r in db.menu_rows()}
    with db._lock, db.connect() as conn:
        for name, phone, history in REGULARS:
            for i, (item, qty, custom) in enumerate(history):
                placed = now - timedelta(days=3 + i * 6, hours=random.randint(1, 8))
                pickup = placed + timedelta(days=1)
                pickup = pickup.replace(hour=random.choice([10, 11, 15, 16]), minute=0, second=0,
                                        microsecond=0)
                price = menu[item]["price_cents"] * qty
                conn.execute(
                    "INSERT INTO orders (order_no, customer_name, phone, items_json, pickup_time,"
                    " notes, status, total_cents, created_at) VALUES (?, ?, ?, ?, ?, '', ?, ?, ?)",
                    (str(random.randint(1000, 9999)), name, phone,
                     json.dumps([{"item": item, "quantity": qty, "customization": custom,
                                  "line_total": "$" + format(price / 100, ".2f")}]),
                     pickup.isoformat(), "collected", price,
                     placed.isoformat(timespec="seconds")),
                )
        for spoken, item, reason, times in DEMAND:
            for _ in range(times):
                at = now - timedelta(days=random.randint(0, 6), hours=random.randint(0, 9))
                conn.execute(
                    "INSERT INTO demand (spoken, item, reason, quantity, created_at)"
                    " VALUES (?, ?, ?, 1, ?)", (spoken, item, reason, at.isoformat(timespec="seconds")))
        for name, phone, reason in CALLBACKS:
            conn.execute(
                "INSERT INTO callbacks (customer_name, phone, reason, created_at) VALUES (?, ?, ?, ?)",
                (name, phone, reason, (now - timedelta(hours=random.randint(1, 5))).isoformat(
                    timespec="seconds")))
    return True


if __name__ == "__main__":
    added = seed(force=bool(os.environ.get("FORCE")))
    print("Seeded sample orders, demand and call-backs." if added
          else "Orders already exist; nothing added. FORCE=1 to add anyway.")
