"""Replay protection, approver allowlist and failure alerting."""

import json
import time
from unittest.mock import MagicMock

import pytest

from app.core.config import Settings
from app.core.security import sign_webhook, verify_webhook_signature
from app.services import incident_store
from app.services.slack_service import SlackService, is_slack_response_url
from app.worker import tasks

SECRET = "s3cret"


# --- webhook timestamps ---------------------------------------------------------------


def test_webhook_signature_binds_timestamp():
    body, ts = b'{"a":1}', "1700000000"
    sig = sign_webhook(SECRET, body, ts)
    assert verify_webhook_signature(SECRET, body, ts, sig, now=1700000000)
    # Same signature presented with a fresh timestamp fails: the timestamp is signed.
    assert not verify_webhook_signature(SECRET, body, "1700000100", sig, now=1700000100)


@pytest.mark.parametrize("ts", [None, "", "yesterday", "1.5"])
def test_webhook_rejects_missing_or_bad_timestamp(ts):
    assert not verify_webhook_signature(SECRET, b"{}", ts, "sha256=00", now=0)


@pytest.mark.parametrize("age", [301, -301])
def test_stale_or_future_webhook_rejected(post_crash, payload, monkeypatch, age):
    delay = MagicMock()
    monkeypatch.setattr(tasks.triage_incident, "delay", delay)
    assert post_crash(payload, age=age).status_code == 401
    delay.assert_not_called()


def test_fresh_webhook_within_tolerance_accepted(post_crash, payload, monkeypatch):
    monkeypatch.setattr(tasks.triage_incident, "delay", MagicMock())
    assert post_crash(payload, age=250).status_code == 202


def test_exact_webhook_replay_is_deduplicated(post_crash, payload, monkeypatch):
    delay = MagicMock()
    monkeypatch.setattr(tasks.triage_incident, "delay", delay)
    ts = str(int(time.time()))
    first = post_crash(payload, timestamp=ts)
    replay = post_crash(payload, timestamp=ts)
    assert (first.status_code, replay.status_code) == (202, 200)
    assert replay.json()["status"] == "deduplicated"
    delay.assert_called_once()


# --- slack replay protection ----------------------------------------------------------


def test_slack_replay_is_acked_but_ignored(post_slack, monkeypatch):
    delay = MagicMock()
    monkeypatch.setattr(tasks.create_hotfix_pr, "delay", delay)
    assert post_slack("abc", nonce="same").status_code == 200
    assert post_slack("abc", nonce="same").status_code == 200  # identical bytes + timestamp
    delay.assert_called_once_with("abc")


def test_distinct_clicks_both_enqueue(post_slack, monkeypatch):
    delay = MagicMock()
    monkeypatch.setattr(tasks.create_hotfix_pr, "delay", delay)
    post_slack("abc")
    post_slack("abc")
    assert delay.call_count == 2  # the hotfix task itself guarantees one PR


def test_failed_enqueue_releases_replay_key(post_slack, monkeypatch):
    delay = MagicMock(side_effect=[ConnectionError, None])
    monkeypatch.setattr(tasks.create_hotfix_pr, "delay", delay)
    assert post_slack("abc", nonce="retry").status_code == 503
    assert post_slack("abc", nonce="retry").status_code == 200
    assert delay.call_count == 2


# --- approver allowlist ---------------------------------------------------------------


def test_can_approve_rules():
    assert Settings(dry_run=True).can_approve("U1")  # dev convenience
    assert not Settings(dry_run=False).can_approve("U1")  # live + empty list: fail closed
    live = Settings(dry_run=False, slack_approver_ids=" U1, U2 ,")
    assert live.approver_ids == {"U1", "U2"}
    assert live.can_approve("U2") and not live.can_approve("U3") and not live.can_approve(None)


@pytest.fixture
def allowlist(monkeypatch, settings):
    monkeypatch.setattr(settings, "slack_approver_ids", "U_BOSS")
    return settings


def test_unlisted_user_gets_ephemeral_and_no_pr(post_slack, monkeypatch, allowlist):
    hotfix, notify = MagicMock(), MagicMock()
    monkeypatch.setattr(tasks.create_hotfix_pr, "delay", hotfix)
    monkeypatch.setattr(tasks.notify_unauthorised, "delay", notify)
    assert post_slack("abc", user="U_INTERN").status_code == 200
    hotfix.assert_not_called()
    notify.assert_called_once_with("https://hooks.slack.com/actions/T1/B1", "abc")


def test_listed_user_can_approve(post_slack, monkeypatch, allowlist):
    hotfix = MagicMock()
    monkeypatch.setattr(tasks.create_hotfix_pr, "delay", hotfix)
    post_slack("abc", user="U_BOSS")
    hotfix.assert_called_once_with("abc")


def test_unauthorised_without_slack_response_url_sends_nothing(post_slack, monkeypatch, allowlist):
    notify = MagicMock()
    monkeypatch.setattr(tasks.notify_unauthorised, "delay", notify)
    monkeypatch.setattr(tasks.create_hotfix_pr, "delay", MagicMock())
    post_slack("abc", user="U_INTERN", response_url="https://evil.example/collect")
    notify.assert_not_called()


def test_response_url_guard():
    assert is_slack_response_url("https://hooks.slack.com/actions/T/B/x")
    for url in (None, "", "http://hooks.slack.com/x", "https://hooks.slack.com.evil.io/x"):
        assert not is_slack_response_url(url)
    with pytest.raises(ValueError):
        SlackService(MagicMock()).send_ephemeral("https://evil.example/", "hi")


def test_notify_task_posts_ephemeral(monkeypatch):
    slack = MagicMock()
    monkeypatch.setattr(tasks, "get_slack", lambda: slack)
    tasks.notify_unauthorised.run("https://hooks.slack.com/actions/T1/B1", "abc")
    url, text = slack.send_ephemeral.call_args.args
    assert url.endswith("/B1") and "not on the hotfix approver list" in text


# --- failure alerting -----------------------------------------------------------------


@pytest.fixture
def slack(monkeypatch):
    mock = MagicMock()
    mock.post_alert.return_value = ("C1", "1.0")
    monkeypatch.setattr(tasks, "get_slack", lambda: mock)
    return mock


def _failure_alerts(slack):
    return [c for c in slack.post_alert.call_args_list if "failed" in c.args[0]]


def test_triage_exhausting_retries_alerts_once(eager_celery, monkeypatch, slack, payload):
    run = MagicMock(side_effect=ConnectionError("github down"))
    monkeypatch.setattr(tasks, "run_triage", run)
    tasks.triage_incident.delay("inc1", payload)
    assert run.call_count == tasks.MAX_RETRIES + 1
    alerts = _failure_alerts(slack)
    assert len(alerts) == 1
    text = json.dumps(alerts[0].args[1])
    assert "github down" in text and payload["service_name"] in text
    record = incident_store.get("inc1")
    assert record["status"] == "triage_failed" and "github down" in record["last_error"]


def test_permanent_triage_error_alerts_without_retrying(eager_celery, monkeypatch, slack, payload):
    run = MagicMock(side_effect=ValueError("bad token"))
    monkeypatch.setattr(tasks, "run_triage", run)
    tasks.triage_incident.delay("inc2", payload)
    assert run.call_count == 1
    assert len(_failure_alerts(slack)) == 1


def test_hotfix_failure_alerts(eager_celery, monkeypatch, slack, payload):
    tasks.run_triage("inc3", payload)
    monkeypatch.setattr(tasks, "run_hotfix", MagicMock(side_effect=PermissionError("403")))
    tasks.create_hotfix_pr.delay("inc3")
    (alert,) = _failure_alerts(slack)
    assert "hotfix failed" in json.dumps(alert.args[1])
    assert incident_store.get("inc3")["status"] == "hotfix_failed"


def test_alerting_failure_never_masks_original_error(eager_celery, monkeypatch, payload):
    broken = MagicMock()
    broken.post_alert.side_effect = RuntimeError("slack down too")
    monkeypatch.setattr(tasks, "get_slack", lambda: broken)
    monkeypatch.setattr(tasks, "run_triage", MagicMock(side_effect=ValueError("boom")))
    result = tasks.triage_incident.delay("inc4", payload)
    assert isinstance(result.result, ValueError)


def test_failure_alert_escapes_error_text(eager_celery, monkeypatch, slack, payload):
    monkeypatch.setattr(tasks, "run_triage", MagicMock(side_effect=ValueError("<!channel> pwn")))
    tasks.triage_incident.delay("inc5", payload)
    text = json.dumps(_failure_alerts(slack)[0].args[1])
    assert "<!channel>" not in text and "&lt;!channel&gt;" in text


def test_unparseable_payload_still_alerts(eager_celery, monkeypatch, slack):
    monkeypatch.setattr(tasks, "run_triage", MagicMock(side_effect=ValueError("bad")))
    tasks.triage_incident.delay("inc6", {"nonsense": True})
    assert "unparseable crash payload" in json.dumps(_failure_alerts(slack)[0].args[1])
