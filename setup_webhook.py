#!/usr/bin/env python3
"""Subscribe the bakery backend to the agent's session and call events.

    python setup_webhook.py

Every finished call then reaches BACKEND_URL/webhooks/assemblyai, signed with
AAI_WEBHOOK_SECRET, and the backend reviews it: outcome, sentiment, a one-line
summary, what the caller wanted and could not get, and how fast the agent
answered. Run it again after BACKEND_URL changes; it updates the subscription
it made last time rather than adding another.

https://www.assemblyai.com/docs/voice-agents/voice-agent-api/webhooks
"""

import os
import secrets
import sys

from lib import ApiError, aai, load_env, required, save_env, stored_agent_id

EVENTS = ["session.completed", "call.ended", "call.failed"]


def main() -> None:
    load_env()
    required("ASSEMBLYAI_API_KEY", "get one at https://www.assemblyai.com/dashboard/api-keys")
    backend = required("BACKEND_URL", "the public https origin of backend/app.py").rstrip("/")
    name = os.environ.get("AGENT", "bakery")
    agent_id = stored_agent_id(name)
    if not agent_id:
        sys.exit(f"No agent id for {name}. Run `AGENT={name} python publish.py` first.")

    # The secret is ours to choose and the API never returns it, so it is made
    # once and kept in .env beside the key the backend reads it from.
    secret = os.environ.get("AAI_WEBHOOK_SECRET")
    if not secret:
        secret = secrets.token_urlsafe(32)
        if not save_env("AAI_WEBHOOK_SECRET", secret):
            sys.exit("Could not write .env. Set AAI_WEBHOOK_SECRET yourself and run again.")
        print("Generated AAI_WEBHOOK_SECRET and saved it to .env.")

    url = backend + "/webhooks/assemblyai"
    body = {"url": url, "events": EVENTS, "secret": secret}
    existing = os.environ.get("AAI_WEBHOOK_ID")
    if existing:
        try:
            aai(f"/webhook-subscriptions/{existing}", method="PATCH", body=body)
            print(f"Updated webhook {existing} -> {url}")
            return
        except ApiError as err:
            if err.status != 404:
                raise
            print(f"Webhook {existing} no longer exists, creating a new one")

    created = aai("/webhook-subscriptions", method="POST", body={**body, "agent_id": agent_id})
    save_env("AAI_WEBHOOK_ID", created["id"])
    print(f"Subscribed {', '.join(EVENTS)} for agent {agent_id}")
    print(f"  -> {url}")
    print(f"AAI_WEBHOOK_ID={created['id']} saved to .env.")
    print("\nThe backend needs the same AAI_WEBHOOK_SECRET. Locally it reads .env;"
          " on Render, paste it under Environment.")


if __name__ == "__main__":
    try:
        main()
    except ApiError as err:
        sys.exit(str(err))
