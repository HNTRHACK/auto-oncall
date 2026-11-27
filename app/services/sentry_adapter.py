"""Turn a Sentry Integration Platform webhook into a CrashPayload.

Teams already route their crashes to Sentry, so instead of asking them to send our own
format we accept Sentry's issue-alert (`event_alert`) and error (`error`) webhooks as they are.

Setup in Sentry: Settings -> Custom Integrations -> New internal integration, set the
Webhook URL to https://<host>/webhook/sentry, enable "Alert Rule Action" (and/or the `error`
webhook), copy the Client Secret into SENTRY_CLIENT_SECRET, then add the integration as the
action of an issue alert rule.
"""

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any

from app.models.schemas import CrashPayload, StackFrame

# Resources carrying a full event with a stack trace.
EVENT_RESOURCES = {"event_alert", "error"}


class NotACrash(ValueError):
    """The webhook is valid but carries nothing we can triage (no exception or frames)."""


def verify_sentry_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Sentry signs the raw body: bare lowercase hex HMAC-SHA256 keyed by the client secret.
    The Sentry-Hook-Timestamp header is NOT signed, so it is not used for security."""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.strip().lower().encode())


def _tags(event: dict) -> dict[str, str]:
    tags = event.get("tags") or []
    if isinstance(tags, dict):
        return {str(k): str(v) for k, v in tags.items()}
    out = {}
    for item in tags:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            out[str(item[0])] = str(item[1])
        elif isinstance(item, dict) and "key" in item:
            out[str(item["key"])] = str(item.get("value", ""))
    return out


def _frames(exception: dict) -> list[StackFrame]:
    raw = ((exception.get("stacktrace") or {}).get("frames")) or []
    raw = [f for f in raw if isinstance(f, dict)]
    # Library frames rarely point at the bug; keep the app's own code when Sentry knows it.
    in_app = [f for f in raw if f.get("in_app")]
    chosen = in_app or raw
    frames = []
    for f in reversed(chosen):  # Sentry lists oldest first; ours puts the raising frame first
        path = f.get("filename") or f.get("abs_path") or f.get("module")
        if not path:
            continue
        frames.append(
            StackFrame(
                filename=str(path)[:1000],
                lineno=max(int(f.get("lineno") or 0), 0),
                function=str(f.get("function") or "<unknown>")[:500],
            )
        )
    return frames[:200]


def _timestamp(event: dict) -> datetime:
    value = event.get("datetime") or event.get("timestamp")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def parse_sentry_webhook(resource: str | None, body: dict[str, Any]) -> tuple[str, CrashPayload]:
    """Return (sentry_event_id, payload). Raises NotACrash for anything we should ignore."""
    if resource not in EVENT_RESOURCES:
        raise NotACrash(f"resource {resource!r} is not an error event")
    data = body.get("data") or {}
    event = data.get("event") or data.get("error")
    if not isinstance(event, dict):
        raise NotACrash("no event in payload")
    values = ((event.get("exception") or {}).get("values")) or []
    values = [v for v in values if isinstance(v, dict)]
    if not values:
        raise NotACrash("event has no exception (message-only events are not triaged)")
    # With chained exceptions the last value is the one that was finally raised.
    exception = values[-1]
    frames = _frames(exception)
    if not frames:
        frames = next((fs for fs in (_frames(v) for v in reversed(values)) if fs), [])
    if not frames:
        raise NotACrash("exception has no stack frames")

    tags = _tags(event)
    service = (
        tags.get("service")
        or event.get("project_slug")
        or event.get("project_name")
        or f"sentry-project-{event.get('project', 'unknown')}"
    )
    payload = CrashPayload(
        service_name=str(service)[:200],
        environment=str(event.get("environment") or tags.get("environment") or "unknown")[:100],
        error_type=str(exception.get("type") or "Error")[:200],
        error_message=str(exception.get("value") or event.get("title") or "")[:10_000],
        stack_trace=frames,
        timestamp=_timestamp(event),
        source_url=event.get("web_url")
        if str(event.get("web_url", "")).startswith("https://")
        else None,
    )
    event_id = str(event.get("event_id") or "")
    return event_id, payload
