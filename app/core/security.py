import hashlib
import hmac
import time

from slack_sdk.signature import SignatureVerifier

# Signed requests older (or newer) than this are rejected as replays.
TIMESTAMP_TOLERANCE_SECONDS = 300


def sign_webhook(secret: str, body: bytes, timestamp: str) -> str:
    """Stripe-style: the signature covers `<timestamp>.<body>`, so a captured request
    cannot be replayed later with a fresh timestamp."""
    message = timestamp.encode() + b"." + body
    return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_webhook_signature(
    secret: str,
    body: bytes,
    timestamp: str | None,
    header: str | None,
    now: float | None = None,
) -> bool:
    if not secret or not header or not timestamp:
        return False
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    now = time.time() if now is None else now
    if abs(now - sent_at) > TIMESTAMP_TOLERANCE_SECONDS:
        return False
    # Compare bytes: compare_digest raises TypeError on non-ASCII str input.
    return hmac.compare_digest(sign_webhook(secret, body, timestamp).encode(), header.encode())


def verify_slack_signature(
    secret: str, body: bytes, timestamp: str | None, signature: str | None
) -> bool:
    """Slack v0 signature; the SDK also rejects timestamps more than 5 minutes off."""
    if not secret or not timestamp or not signature:
        return False
    try:
        return SignatureVerifier(secret).is_valid(body, timestamp, signature)
    except (ValueError, TypeError, UnicodeDecodeError):
        return False
