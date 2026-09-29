"""Rosewood's live demo page: call the bakery from the browser, and watch the
owner's side fill in as you talk.

Everything reads the same SQLite database the backend's tools write to, in
the same process, so an order placed on the call lands on the board below
within a couple of seconds.
"""
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from demo import host  # noqa: E402
from demo.host import backend, db  # noqa: E402

REPO_URL = "https://github.com/mariya-haque/rosewood-bakery-voice-agent"

st.set_page_config(page_title="Rosewood · AI order line", page_icon="🥐", layout="wide")
host.ensure_db()


# ---------------------------------------------------------------------------
# Going public: once per server process, not per visitor.
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def connection(_origin: str, _port: int) -> dict:
    try:
        return host.go_public(_origin, _port)
    except Exception as err:  # noqa: BLE001 - shown in the sidebar, retryable
        return {"base": "", "how": "", "agent_id": "", "agent": "", "error": str(err)[:300]}


def _origin() -> str:
    url = st.context.url or ""
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return parsed.scheme + "://" + parsed.netloc
    host_header = st.context.headers.get("host", "")
    return "https://" + host_header if host_header else ""


with st.spinner("Opening the phone line: publishing the agent and its tools…"):
    conn = connection(_origin(), int(st.get_option("server.port") or 8501))


def money(cents) -> str:
    return "$" + format((cents or 0) / 100, ",.2f")


def spoken(iso: str) -> str:
    try:
        return backend._spoken_time(datetime.fromisoformat(iso))
    except (TypeError, ValueError):
        return iso or ""


# ---------------------------------------------------------------------------
# Sidebar: what is live, and what to try.
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 🥐 Rosewood Bakery")
    st.caption("An AssemblyAI voice agent that answers a small bakery's phone.")

    st.markdown("**Try saying**")
    st.markdown(
        "- *Two kilos of red velvet for tomorrow at four, write Happy Birthday Mira.*\n"
        "- *Do you have black forest?* (sold out: watch Missed demand)\n"
        "- *Are your cupcakes gluten free?* (becomes a call-back)\n"
        "- Give **415 555 0142** as your number to be greeted as Dana, a regular.\n"
        "- *Is order 1234 ready?*"
    )

    st.divider()
    st.markdown("**Phone line status**")
    if conn.get("error"):
        st.error(conn["error"])
    elif conn.get("agent_id"):
        st.success("Live: the agent's tools point at this app.", icon="✅")
    else:
        st.warning(conn.get("agent") or "Agent not published.")
    with st.expander("Details"):
        st.write({k: v for k, v in conn.items() if v})
    if st.button("Reconnect", width="stretch"):
        connection.clear()
        st.rerun()

    st.divider()
    st.markdown(f"[Source on GitHub]({REPO_URL})")
    st.caption("Demo data only. Every name and number here is invented.")


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title("Rosewood: the AI order line that knows your regulars")
st.markdown(
    "Small food shops miss calls in the rush, when nobody can pick up. Rosewood answers every one. "
    "It **takes real orders against live stock**, **greets regulars by name**, **routes allergy "
    "questions to a human**, and shows the owner **what callers wanted and couldn't buy**."
)


@st.fragment(run_every="3s")
def headline():
    t = db.insights(7)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Orders this week", t["orders"])
    c2.metric("Revenue from the phone", money(t["revenue_cents"]))
    c3.metric("Calls reviewed", t["calls"])
    c4.metric("Missed requests", t["unmet_requests"])
    c5.metric("Open call-backs", t["open_callbacks"])


headline()

call_tab, board_tab, insights_tab, menu_tab, how_tab = st.tabs(
    ["📞 Call the bakery", "🧾 Live board", "📈 Insights", "🍰 Menu & stock", "⚙️ How it works"])


# ---------------------------------------------------------------------------
# Call
# ---------------------------------------------------------------------------
with call_tab:
    left, right = st.columns([3, 2], gap="large")
    with left:
        if conn.get("base") and conn.get("agent_id"):
            # Built from values fixed for the life of the process, so reruns
            # leave the iframe (and a call in progress) alone.
            st.iframe(host.call_widget(conn["base"], conn["agent_id"]), height=470)
            st.caption("Uses your microphone. Headphones stop the agent hearing itself.")
        else:
            st.info("The phone line isn't connected yet, so calls are off. The owner's side below "
                    "still works on the demo data. See **Phone line status** in the sidebar.")

    with right:
        @st.fragment(run_every="2s")
        def signals():
            st.markdown("**What the shop's system just saw**")
            latest = db.orders(limit=3)
            demand = db.demand_summary(1)[:4]
            callbacks = db.callbacks("open")[:3]
            if latest:
                for o in latest:
                    items = ", ".join(f'{i["quantity"]} × {i["item"]}' for i in o["items"])
                    st.markdown(f"🧾 **#{o['order_no']}** {o['customer_name']}: {items} · "
                                f"{money(o['total_cents'])} · {spoken(o['pickup_time'])}")
            for d in demand:
                label = {"sold_out": "sold out", "not_enough": "not enough left",
                         "not_on_menu": "not on the menu"}.get(d["reason"], d["reason"])
                st.markdown(f"⚠️ Caller asked for **{d['what']}**: {label}")
            for c in callbacks:
                st.markdown(f"📞 Call back **{c['customer_name']}**: {c['reason']}")
        signals()


# ---------------------------------------------------------------------------
# Live board
# ---------------------------------------------------------------------------
NEXT = {"new": ("baking", "Start baking"), "baking": ("ready", "Mark ready"),
        "ready": ("collected", "Collected")}

with board_tab:
    @st.fragment(run_every="3s")
    def board():
        cols = st.columns(4)
        for col, status in zip(cols, ["new", "baking", "ready", "collected"]):
            rows = db.orders(status, limit=6 if status == "collected" else 50)
            col.markdown(f"**{status.title()}** · {len(rows)}")
            for o in rows:
                with col.container(border=True):
                    st.markdown(f"**#{o['order_no']}** · {o['customer_name']}")
                    for i in o["items"]:
                        extra = f" ({i['customization']})" if i.get("customization") else ""
                        st.caption(f"{i['quantity']} × {i['item']}{extra}")
                    st.caption(f"Pickup {spoken(o['pickup_time'])} · {money(o['total_cents'])}")
                    if status in NEXT:
                        nxt, label = NEXT[status]
                        if st.button(label, key=f"mv-{o['order_no']}", width="stretch"):
                            backend._publish("order", db.set_status(o["order_no"], nxt))
                            st.rerun(scope="fragment")
    board()


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------
with insights_tab:
    @st.fragment(run_every="5s")
    def insights():
        a, b = st.columns(2, gap="large")
        with a:
            st.markdown("**Missed demand, last 7 days**")
            st.caption("Every sold-out or off-menu request. What to bake more of, and what to add.")
            demand = db.demand_summary(7)
            if demand:
                df = pd.DataFrame(demand).rename(columns={"what": "Request", "asks": "Times asked"})
                st.bar_chart(df, x="Request", y="Times asked", horizontal=True, color="#9f1239")
            else:
                st.caption("Nothing missed yet.")
        with b:
            st.markdown("**Call-backs**")
            st.caption("Allergy questions, complaints and catering: the agent never guesses.")
            for c in db.callbacks("open"):
                with st.container(border=True):
                    st.markdown(f"**{c['customer_name']}** · {c['phone']}")
                    st.caption(c["reason"])
                    if st.button("Done", key=f"cb-{c['id']}"):
                        backend._publish("callback", db.set_callback_status(c["id"], "done"))
                        st.rerun(scope="fragment")

        st.markdown("**Call reviews**")
        st.caption("About a minute after each hang-up, a signed webhook fires; the backend pulls the "
                   "session timeline from the Sessions API and the LLM Gateway writes the review.")
        calls = db.calls(20)
        if calls:
            st.dataframe(pd.DataFrame([{
                "When": (c.get("started_at") or c.get("updated_at") or "")[:16].replace("T", " "),
                "Outcome": (c.get("outcome") or c["state"]).replace("_", " "),
                "Sentiment": c.get("sentiment") or "",
                "Summary": c.get("summary") or c.get("error") or "",
                "Next step": c.get("follow_up") or "",
                "Reply latency (ms)": c.get("first_audio_ms"),
                "Tool calls": c.get("tool_calls"),
            } for c in calls]), hide_index=True, width="stretch")
        else:
            st.caption("No calls reviewed yet. Make one on the Call tab; its review appears here.")
    insights()


# ---------------------------------------------------------------------------
# Menu & stock
# ---------------------------------------------------------------------------
with menu_tab:
    @st.fragment
    def menu():
        st.caption("Stock is live: the agent checks it before agreeing to anything. Set a cake to 0 "
                   "and ask for it on a call.")
        rows = db.menu_rows()
        df = pd.DataFrame([{"id": r["id"], "Item": r["name"], "Category": r["category"],
                            "Price": r["price_cents"] / 100, "Unit": r["unit"], "Stock": r["stock"],
                            "If sold out, offer": r["alternative"] or ""} for r in rows])
        edited = st.data_editor(
            df, key="menu_editor", hide_index=True, width="stretch",
            disabled=[c for c in df.columns if c != "Stock"],
            column_config={"id": None, "Price": st.column_config.NumberColumn(format="$%.2f"),
                           "Stock": st.column_config.NumberColumn(min_value=0, step=1)})
        for before, after in zip(df.itertuples(), edited.itertuples()):
            if int(after.Stock) != int(before.Stock):
                backend._publish("menu", db.set_stock(int(before.id), int(after.Stock)))
                st.toast(f"{before.Item}: stock set to {int(after.Stock)}")

        st.markdown("**Add an item**")
        st.caption("New names are pushed to the agent's speech recognition as keyterms, so the next "
                   "caller who asks for it is heard correctly.")
        with st.form("add_item", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            name = c1.text_input("Name", placeholder="Pain au chocolat")
            category = c2.selectbox("Category", ["pastry", "cake", "bread"])
            price = c3.number_input("Price ($)", min_value=0.0, value=4.5, step=0.5)
            c4, c5, c6 = st.columns(3)
            unit = c4.selectbox("Unit", ["each", "per kg", "per box"])
            stock = c5.number_input("Stock", min_value=0, value=12, step=1)
            aliases = c6.text_input("Other ways to say it", placeholder="chocolate croissant")
            if st.form_submit_button("Add to menu"):
                try:
                    out = backend.api_add_item({"name": name, "category": category, "price": price,
                                                "unit": unit, "stock": stock, "aliases": aliases})
                except backend.HTTPException as err:
                    st.error(err.detail)
                else:
                    agent = out["agent"]
                    st.success(f"Added {out['item']['name']}. " + (
                        f"Agent now listens for {agent['keyterms']} keyterms." if agent.get("synced")
                        else f"Keyterms not synced: {agent.get('reason')}"))
                    st.rerun(scope="fragment")
    menu()


# ---------------------------------------------------------------------------
# How it works
# ---------------------------------------------------------------------------
with how_tab:
    st.markdown("""
**One agent definition** ([agents/bakery.jsonc](%(repo)s/blob/main/agents/bakery.jsonc)) serves this
browser call and a real phone number unchanged. Everything below runs inside this Streamlit app:
the FastAPI backend is mounted beside the page, and AssemblyAI calls it mid-sentence.

| AssemblyAI capability | What it does here |
| --- | --- |
| **Voice Agent API** | Streaming speech-to-text, the conversational model and the voice, over one WebSocket. The page gets a 60-second token; the API key never reaches the browser. |
| **Six HTTP tools** | `check_availability`, `check_pickup_slot`, `create_order`, `get_order_status`, `lookup_customer`, `request_callback`. Every price, slot and order number comes from the shop's database. |
| **Pre-connect requests** | On a phone call the caller's number is looked up before the call is answered, so a regular hears their name and their usual. |
| **Keyterms, synced with the menu** | Add an item on the Menu tab and its name is pushed to speech recognition. A wrong flavour is a wrong cake. |
| **Transcription prompt + Voice Focus** | Primed for kilograms, phone numbers and pickup times; oven fans don't become words. |
| **Webhooks + Sessions API** | Every finished call triggers a signed webhook; the backend pulls the timeline, with per-turn reply latency. |
| **LLM Gateway** | Turns each timeline into an outcome, sentiment, one-line summary and next step. |

A call costs about 7.5 cents a minute. A custom cake order is worth $40 to $100.
""" % {"repo": REPO_URL})
