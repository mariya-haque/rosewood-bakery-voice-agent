# Bakery backend

The six HTTP tools the voice agent calls, the pre-connect lookup, the webhook
that reviews every finished call, and the owner dashboard, in one FastAPI app
over SQLite.

| Path | Who calls it |
| --- | --- |
| `POST /tools/check_availability` | AssemblyAI, mid-call |
| `POST /tools/check_pickup_slot` | AssemblyAI, mid-call |
| `POST /tools/create_order` | AssemblyAI, mid-call |
| `POST /tools/get_order_status` | AssemblyAI, mid-call |
| `POST /tools/lookup_customer` | AssemblyAI, mid-call (browser: no caller ID) |
| `POST /tools/request_callback` | AssemblyAI, mid-call |
| `POST /voice/pre_connect` | AssemblyAI, before a phone call is answered |
| `POST /webhooks/assemblyai` | AssemblyAI, signed, when a session or call ends |
| `GET /` | the owner: live board, insights, menu and stock, call history |
| `GET /api/events` | the board's SSE stream: orders, demand, call-backs, reviews |
| `GET /api/insights` | orders, revenue, unmet demand, call-backs, call reviews |
| `POST /api/menu` | add an item, then push menu keyterms to the agent |
| `GET /api/calls`, `/api/calls/{id}`, `/api/calls/{id}/timeline` | proxies the AssemblyAI Sessions API |
| `POST /api/calls/{id}/analyse` | review a call on demand |

Tool and pre-connect calls must carry `X-Tool-Key: $TOOL_API_KEY`. Webhooks
are verified against `AAI_WEBHOOK_SECRET` instead (HMAC-SHA256 over the raw
body, five-minute replay window) and deduplicated on `event_id`. Leave `TOOL_API_KEY` unset
and the check is skipped, which is fine on localhost and not fine once the
service is public.

## Run it

```sh
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
TOOL_API_KEY=devsecret .venv/Scripts/python -m uvicorn app:app --port 8000
```

Dashboard at http://localhost:8000.

## Expose it

AssemblyAI calls the tools from its own servers, so the URL in
`agents/bakery.jsonc` must be public HTTPS. `http://` is rejected at publish
time, and private, loopback and link-local addresses are blocked at call time.
Redirects are not followed.

In development, tunnel it:

```sh
cloudflared tunnel --url http://localhost:8000
```

Put the `https://...trycloudflare.com` origin in `.env` as `BACKEND_URL`
(no trailing slash), then `AGENT=bakery python publish.py` from the repo root.
The tunnel URL changes every restart, so republish each time.

## Environment

| Variable | Purpose |
| --- | --- |
| `TOOL_API_KEY` | shared secret checked on `/tools/*`. Must match `.env` at the repo root. |
| `ASSEMBLYAI_API_KEY` | only for the call-history tab, which reads `agents.assemblyai.com/v1/sessions`. |
| `DB_PATH` | where `bakery.db` lives. Point at a mounted disk in production. |
| `SHOP_NAME` | shown in the dashboard title. |
| `SHOP_TZ` | IANA timezone of the shop, e.g. `America/Los_Angeles`. Pickup times resolve against it. |
| `AAI_WEBHOOK_SECRET` | verifies webhook deliveries. `python setup_webhook.py` writes it to `.env`. |
| `SUMMARY_MODEL` | LLM Gateway model for call reviews. Default `qwen3.5-4b-32k-fast`. |
| `DEMO_SEED` | `1` seeds sample regulars, demand and call-backs into an empty database. |
| `TWILIO_PHONE_NUMBER` | shown on the dashboard as a click-to-call badge. |

## Tests

```sh
.venv/Scripts/python -m pip install pytest httpx
.venv/Scripts/python -m pytest -q
```

## Shop rules

They live in `db.py` as constants, because the tools and the system prompt have
to agree: `OPEN_HOUR` 9, `CLOSE_HOUR` 19, `SLOT_MINUTES` 30,
`SLOT_CAPACITY` 4 orders per slot, `LEAD_TIME_MINUTES` 90 minimum notice.
Change them there and change the SHOP FACTS paragraph in
`agents/bakery.jsonc` to match.

## Menu

Seeded on first boot from `SEED` in `db.py`: name, category, price, unit,
stock, spoken aliases, and the item to offer when it is sold out. Stock is
edited from the dashboard and decremented when an order is saved. Delete
`bakery.db` to reseed.

Item names arrive as the caller said them, so matching goes exact name,
then alias, then substring, then fuzzy. The `keyterms` list in the agent file
is the first line of defence: it stops "tres leches" being transcribed as
something the matcher then has to guess at.
