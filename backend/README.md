# Bakery backend

The four HTTP tools the voice agent calls, plus the owner dashboard, in one
FastAPI app over SQLite.

| Path | Who calls it |
| --- | --- |
| `POST /tools/check_availability` | AssemblyAI, mid-call |
| `POST /tools/check_pickup_slot` | AssemblyAI, mid-call |
| `POST /tools/create_order` | AssemblyAI, mid-call |
| `POST /tools/get_order_status` | AssemblyAI, mid-call |
| `GET /` | the owner: live board, menu and stock, call history |
| `GET /api/events` | the board's SSE stream, so orders appear as calls end |
| `GET /api/calls`, `/api/calls/{id}` | proxies the AssemblyAI Sessions API |

Tool calls must carry `X-Tool-Key: $TOOL_API_KEY`. Leave `TOOL_API_KEY` unset
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
