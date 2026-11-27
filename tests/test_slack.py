import json
from unittest.mock import MagicMock

import pytest

from app.models.schemas import CrashPayload, RCAReport
from app.services import incident_store
from app.services.github_service import FakeGitHub, StaleFileError
from app.services.slack_service import SlackService, build_alert_blocks
from app.worker import tasks


def report(confidence=0.9):
    return RCAReport(
        suspected_commit_sha="a" * 40,
        culprit_author="dev",
        root_cause="<!channel> & friends",
        confidence_score=confidence,
        affected_file="a.py",
        file_patch="x\n",
    )


def has_button(blocks):
    return any(b["type"] == "actions" for b in blocks)


@pytest.fixture
def slack(monkeypatch):
    mock = MagicMock()
    mock.post_alert.return_value = ("C1", "1.0")
    monkeypatch.setattr(tasks, "get_slack", lambda: mock)
    return mock


@pytest.fixture
def github(monkeypatch):
    mock = MagicMock(wraps=FakeGitHub())
    monkeypatch.setattr(tasks, "get_github", lambda: mock)
    return mock


def test_valid_slack_signature_enqueues(post_slack, monkeypatch):
    delay = MagicMock()
    monkeypatch.setattr(tasks.create_hotfix_pr, "delay", delay)
    assert post_slack("abc").status_code == 200
    delay.assert_called_once_with("abc")


@pytest.mark.parametrize("kwargs", [{"secret": "wrong"}, {"age": 301}, {"age": -301}])
def test_invalid_slack_signature_rejected(post_slack, monkeypatch, kwargs):
    delay = MagicMock()
    monkeypatch.setattr(tasks.create_hotfix_pr, "delay", delay)
    assert post_slack("abc", **kwargs).status_code == 401
    delay.assert_not_called()


def test_missing_or_garbage_slack_headers(client):
    assert client.post("/slack/actions", content=b"payload={}").status_code == 401
    headers = {"X-Slack-Request-Timestamp": "soon", "X-Slack-Signature": "v0=00"}
    assert client.post("/slack/actions", content=b"x", headers=headers).status_code == 401


def test_slack_enqueue_failure_is_503(post_slack, monkeypatch):
    monkeypatch.setattr(tasks.create_hotfix_pr, "delay", MagicMock(side_effect=ConnectionError))
    assert post_slack("abc").status_code == 503


def test_low_confidence_hides_button(payload, monkeypatch, slack):
    llm = MagicMock()
    llm.analyze.return_value = {}  # forces the zero-confidence fallback
    monkeypatch.setattr(tasks, "get_llm", lambda: llm)
    tasks.run_triage("low", payload)
    assert not has_button(slack.post_alert.call_args.args[1])


def test_button_shown_and_text_escaped(payload):
    blocks = build_alert_blocks("abc", CrashPayload.model_validate(payload), report(), True)
    assert has_button(blocks)
    text = json.dumps(blocks)
    assert "<!channel>" not in text and "&lt;!channel&gt;" in text


def test_real_slack_service_calls():
    client = MagicMock()
    client.chat_postMessage.return_value = {"channel": "C9", "ts": "2.0"}
    service = SlackService(client)
    assert service.post_alert("t", []) == ("C9", "2.0")
    service.update_message("C9", "2.0", "t", [])
    client.chat_update.assert_called_once_with(channel="C9", ts="2.0", text="t", blocks=[])


def test_clicking_twice_creates_one_pr(
    eager_celery, post_crash, post_slack, payload, slack, github
):
    incident_id = post_crash(payload).json()["incident_id"]
    assert has_button(slack.post_alert.call_args.args[1])
    assert post_slack(incident_id).status_code == 200
    assert post_slack(incident_id).status_code == 200

    github.create_hotfix_pr.assert_called_once()
    assert github.create_hotfix_pr.call_args.kwargs["branch"] == f"hotfix/incident-{incident_id}"
    pr_url = incident_store.get(incident_id)["pr_url"]
    channel, ts, _text, blocks = slack.update_message.call_args.args
    assert (channel, ts) == ("C1", "1.0")
    assert pr_url in json.dumps(blocks) and not has_button(blocks)


def test_hotfix_waits_when_locked(payload, slack, github, redis_fake):
    tasks.run_triage("abc", payload)
    redis_fake.set("hotfix-lock:abc", "1", ex=100)
    with pytest.raises(tasks.HotfixInProgress) as exc:
        tasks.run_hotfix("abc")
    assert 0 < exc.value.retry_after <= 100
    github.create_hotfix_pr.assert_not_called()


def test_hotfix_failure_releases_lock_and_retries_cleanly(payload, slack, github, redis_fake):
    tasks.run_triage("abc", payload)
    github.create_hotfix_pr.side_effect = [ConnectionError, "https://pr/1"]
    with pytest.raises(ConnectionError):
        tasks.run_hotfix("abc")
    assert not redis_fake.exists("hotfix-lock:abc")
    tasks.run_hotfix("abc")
    assert incident_store.get("abc")["pr_url"] == "https://pr/1"


def test_stale_file_is_reported_in_slack(payload, slack, github):
    tasks.run_triage("abc", payload)
    github.create_hotfix_pr.side_effect = StaleFileError("changed")
    tasks.run_hotfix("abc")
    assert "not created" in json.dumps(slack.update_message.call_args.args[3])
    assert "pr_url" not in incident_store.get("abc")


def test_hotfix_refused_for_unknown_or_low_confidence(slack, github):
    tasks.run_hotfix("missing")
    incident_store.save("low", rca=report(0.1).model_dump_json(), can_pr="0")
    tasks.run_hotfix("low")
    github.create_hotfix_pr.assert_not_called()
