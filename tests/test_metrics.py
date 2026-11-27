from unittest.mock import MagicMock

import redis

from app.services import incident_store, metrics
from app.worker import tasks


def scrape(client) -> dict[str, int]:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    values = {}
    for line in response.text.splitlines():
        if line and not line.startswith("#"):
            name, value = line.split()
            values[name.removeprefix("auto_oncall_").removesuffix("_total")] = int(value)
    return values


def test_metrics_start_at_zero_with_help_lines(client):
    assert set(scrape(client).values()) == {0}
    text = client.get("/metrics").text
    assert "# TYPE auto_oncall_incidents_accepted_total counter" in text


def test_full_flow_is_counted(eager_celery, client, post_crash, post_slack, payload, monkeypatch):
    slack = MagicMock()
    slack.post_alert.return_value = ("C1", "1.0")
    monkeypatch.setattr(tasks, "get_slack", lambda: slack)

    incident_id = post_crash(payload).json()["incident_id"]
    post_crash(payload)  # duplicate
    post_crash(payload, signature="sha256=bad")
    post_slack(incident_id)

    values = scrape(client)
    assert values["incidents_accepted"] == 1
    assert values["incidents_deduplicated"] == 1
    assert values["webhook_rejected"] == 1
    assert values["triage_completed"] == 1
    assert values["hotfix_prs_created"] == 1


def test_metric_failures_never_raise(monkeypatch):
    broken = MagicMock()
    broken.hincrby.side_effect = redis.ConnectionError
    monkeypatch.setattr(incident_store, "get_redis", lambda: broken)
    metrics.incr("incidents_accepted")  # no exception


def test_metrics_endpoint_503_when_redis_down(client, monkeypatch):
    broken = MagicMock()
    broken.hgetall.side_effect = redis.ConnectionError
    monkeypatch.setattr(incident_store, "get_redis", lambda: broken)
    assert client.get("/metrics").status_code == 503
