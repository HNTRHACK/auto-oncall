"""Send a signed crash twice (second is deduplicated).

--approve additionally waits for triage, then clicks "Create Hotfix PR" twice with a
signed Slack interaction (one PR results). Local testing only.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from slack_sdk.signature import SignatureVerifier

from app.core.config import get_settings
from app.core.security import sign_webhook
from app.services import incident_store
from app.services.slack_service import PR_ACTION_ID


def crash_payload() -> dict:
    return {
        "service_name": "payments-api",
        "environment": "production",
        "error_type": "AttributeError",
        "error_message": "'NoneType' object has no attribute 'email' (order 48213)",
        "stack_trace": [  # top frame first
            {"filename": "/srv/app/payments/processor.py", "lineno": 7, "function": "charge"},
            {"filename": "/srv/app/payments/views.py", "lineno": 31, "function": "post"},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def send_crash(client: httpx.Client, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    response = client.post(
        "/webhook/crash",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": timestamp,
            "X-Signature-256": sign_webhook(get_settings().webhook_secret, body, timestamp),
        },
    )
    print(f"POST /webhook/crash -> {response.status_code} {response.text}")
    response.raise_for_status()
    return response.json()


def click_button(client: httpx.Client, incident_id: str) -> None:
    interaction = {
        "type": "block_actions",
        "user": {"id": "U_SIMULATED"},
        "response_url": "https://hooks.slack.com/actions/T00000000/SIMULATED",
        "actions": [{"action_id": PR_ACTION_ID, "value": incident_id}],
        # Two real clicks are two distinct interactions; this keeps their signatures distinct.
        "trigger_id": f"{time.time_ns()}",
    }
    body = urlencode({"payload": json.dumps(interaction)}).encode()
    timestamp = str(int(time.time()))
    signature = SignatureVerifier(get_settings().slack_signing_secret).generate_signature(
        timestamp=timestamp, body=body
    )
    response = client.post(
        "/slack/actions",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": signature,
        },
    )
    print(f"POST /slack/actions -> {response.status_code}")
    response.raise_for_status()


def wait_for(incident_id: str, field: str, timeout: float = 60) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = incident_store.get(incident_id).get(field)
        if value:
            return value
        time.sleep(0.5)
    sys.exit(f"timed out waiting for {field!r}; is the worker running?")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--approve", action="store_true", help="also approve the hotfix PR")
    args = parser.parse_args()

    payload = crash_payload()
    with httpx.Client(base_url=args.url, timeout=10) as client:
        incident_id = send_crash(client, payload)["incident_id"]
        print("Sending the same crash with a different order number...")
        payload["error_message"] = payload["error_message"].replace("48213", "99107")
        send_crash(client, payload)

        if args.approve:
            wait_for(incident_id, "slack_ts")
            print("Triage finished. Clicking 'Create Hotfix PR' twice...")
            click_button(client, incident_id)
            click_button(client, incident_id)
            print(f"Hotfix PR: {wait_for(incident_id, 'pr_url')}")


if __name__ == "__main__":
    main()
