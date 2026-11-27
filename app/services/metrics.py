"""Pipeline counters shared by the API and every worker process.

Kept in one Redis hash instead of in-process prometheus_client counters: the API and
Celery workers are separate processes (often separate containers), and a scrape of
/metrics must see the totals of all of them.
"""

import redis
import structlog

from app.services import incident_store

log = structlog.get_logger(__name__)

KEY = "metrics:counters"

COUNTERS = {
    "webhook_rejected": "Crash webhooks rejected (bad signature, stale timestamp or invalid body)",
    "incidents_accepted": "New incidents accepted and queued for triage",
    "incidents_deduplicated": "Crash webhooks folded into an existing incident",
    "triage_completed": "Incidents fully triaged and alerted in Slack",
    "triage_failed": "Incidents whose triage gave up after retries",
    "hotfix_prs_created": "Hotfix pull requests opened",
    "hotfix_failed": "Hotfix PR attempts that gave up after retries",
    "hotfix_unauthorised": "Hotfix clicks from users outside the approver list",
    "slack_replays_rejected": "Replayed Slack interactions ignored",
}


def incr(name: str, amount: int = 1) -> None:
    """Never raises: losing a counter must not fail a request or a task."""
    if name not in COUNTERS:
        raise KeyError(f"unknown counter {name!r}")
    try:
        incident_store.get_redis().hincrby(KEY, name, amount)
    except redis.RedisError as exc:
        log.warning("metric_increment_failed", metric=name, error=repr(exc))


def snapshot() -> dict[str, int]:
    raw = incident_store.get_redis().hgetall(KEY)
    return {name: int(raw.get(name, 0)) for name in COUNTERS}


def render_prometheus(values: dict[str, int]) -> str:
    lines = []
    for name, help_text in COUNTERS.items():
        metric = f"auto_oncall_{name}_total"
        lines += [
            f"# HELP {metric} {help_text}",
            f"# TYPE {metric} counter",
            f"{metric} {values[name]}",
        ]
    return "\n".join(lines) + "\n"
