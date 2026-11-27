"""Sentry Integration Platform webhook adapter."""

import copy
import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.models.schemas import CrashPayload, fingerprint
from app.services import incident_store
from app.services.sentry_adapter import NotACrash, parse_sentry_webhook, verify_sentry_signature
from app.services.slack_service import build_alert_blocks
from app.worker import tasks
from scripts.simulate_crash import crash_payload

SECRET = "sentry-client-secret"
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sentry_event_alert.json").read_text())


@pytest.fixture
def event():
    return copy.deepcopy(FIXTURE)


@pytest.fixture(autouse=True)
def sentry_secret(monkeypatch, settings):
    monkeypatch.setattr(settings, "sentry_client_secret", SECRET)


@pytest.fixture
def enqueue(monkeypatch):
    delay = MagicMock()
    monkeypatch.setattr(tasks.triage_incident, "delay", delay)
    return delay


def post_sentry(client, data: dict, resource="event_alert", secret=SECRET, signature=None):
    body = json.dumps(data).encode()
    sig = signature or hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhook/sentry",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Sentry-Hook-Resource": resource,
            "Sentry-Hook-Signature": sig,
            "Sentry-Hook-Timestamp": "1764190800",
            "Request-ID": "3f1c0e2a",
        },
    )


# --- parsing ---------------------------------------------------------------------------


def test_parse_maps_sentry_event_to_crash(event):
    event_id, payload = parse_sentry_webhook("event_alert", event)
    assert event_id == "e4874d664c3540c1a32eab185f12c5ab"
    assert payload.service_name == "payments-api"
    assert payload.environment == "production"
    assert payload.error_type == "AttributeError"
    assert "order 48213" in payload.error_message
    assert payload.source_url.startswith("https://acme.sentry.io/issues/")
    # Sentry is oldest-first; ours puts the raising frame first and drops library frames.
    assert [f.filename for f in payload.stack_trace] == [
        "app/payments/processor.py",
        "app/payments/views.py",
    ]
    assert payload.stack_trace[0].lineno == 7 and payload.stack_trace[0].function == "charge"


def test_sentry_and_native_reports_of_the_same_crash_dedup_together(event):
    native = CrashPayload.model_validate(crash_payload())
    native = native.model_copy(
        update={
            "stack_trace": [
                f.model_copy(update={"filename": "app/payments/processor.py"})
                for f in native.stack_trace[:1]
            ]
        }
    )
    _, from_sentry = parse_sentry_webhook("event_alert", event)
    assert fingerprint(native) == fingerprint(from_sentry)


def test_all_library_frames_are_kept_when_nothing_is_in_app(event):
    frames = event["data"]["event"]["exception"]["values"][0]["stacktrace"]["frames"]
    for frame in frames:
        frame["in_app"] = False
    _, payload = parse_sentry_webhook("event_alert", event)
    assert len(payload.stack_trace) == 3
    assert payload.stack_trace[-1].filename == "gunicorn/workers/sync.py"


def test_chained_exception_uses_the_last_raised(event):
    values = event["data"]["event"]["exception"]["values"]
    values.insert(0, {"type": "KeyError", "value": "'email'", "stacktrace": {"frames": []}})
    _, payload = parse_sentry_webhook("event_alert", event)
    assert payload.error_type == "AttributeError"


def test_error_resource_and_fallbacks(event):
    raw = event["data"].pop("event")
    raw.pop("environment")
    raw["tags"] = {"environment": "staging"}  # dict form, older payloads
    raw.pop("web_url")
    raw["tags"].pop("service", None)
    _, payload = parse_sentry_webhook("error", {"data": {"error": raw}})
    assert payload.environment == "staging"
    assert payload.service_name == "sentry-project-1"
    assert payload.source_url is None


def test_frames_with_missing_fields_are_tolerated(event):
    frames = event["data"]["event"]["exception"]["values"][0]["stacktrace"]["frames"]
    frames.append({"abs_path": "/srv/app/x.py", "lineno": None, "function": None, "in_app": True})
    frames.append({"in_app": True})  # nothing usable: skipped
    _, payload = parse_sentry_webhook("event_alert", event)
    assert payload.stack_trace[0].filename == "/srv/app/x.py"
    assert payload.stack_trace[0].lineno == 0
    assert payload.stack_trace[0].function == "<unknown>"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e["data"]["event"].pop("exception"),
        lambda e: e["data"]["event"]["exception"]["values"][0]["stacktrace"].update(frames=[]),
        lambda e: e["data"].pop("event"),
    ],
)
def test_non_crashes_are_rejected(event, mutate):
    mutate(event)
    with pytest.raises(NotACrash):
        parse_sentry_webhook("event_alert", event)


def test_non_http_source_url_is_dropped(event):
    event["data"]["event"]["web_url"] = "javascript:alert(1)"
    assert parse_sentry_webhook("event_alert", event)[1].source_url is None


def test_signature_check():
    body = b'{"a": 1}'
    good = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert verify_sentry_signature(SECRET, body, good)
    assert verify_sentry_signature(SECRET, body, good.upper())
    assert not verify_sentry_signature(SECRET, body + b" ", good)
    assert not verify_sentry_signature("", body, good)  # not configured: fail closed
    assert not verify_sentry_signature(SECRET, body, None)


# --- endpoint --------------------------------------------------------------------------


def test_valid_sentry_alert_is_accepted(client, event, enqueue):
    response = post_sentry(client, event)
    assert response.status_code == 202
    incident_id, data = enqueue.call_args.args
    assert response.json()["incident_id"] == incident_id
    assert data["service_name"] == "payments-api"
    assert data["stack_trace"][0]["filename"] == "app/payments/processor.py"


@pytest.mark.parametrize("kwargs", [{"secret": "wrong"}, {"signature": "00" * 32}])
def test_bad_sentry_signature_rejected(client, event, enqueue, kwargs):
    assert post_sentry(client, event, **kwargs).status_code == 401
    enqueue.assert_not_called()


def test_unconfigured_secret_rejects_everything(client, event, enqueue, monkeypatch, settings):
    monkeypatch.setattr(settings, "sentry_client_secret", "")
    assert post_sentry(client, event, secret="").status_code == 401


@pytest.mark.parametrize("resource", ["installation", "issue", "comment", None])
def test_other_resources_are_acknowledged_and_ignored(client, event, enqueue, resource):
    response = post_sentry(client, event, resource=resource or "")
    assert response.status_code == 200 and response.json()["status"] == "ignored"
    enqueue.assert_not_called()


def test_same_event_twice_is_dropped_even_after_dedup_window(client, event, enqueue, redis_fake):
    assert post_sentry(client, event).status_code == 202
    for key in redis_fake.keys("dedup:*"):
        redis_fake.delete(key)  # simulate the 5-minute dedup window passing
    response = post_sentry(client, event)
    assert response.status_code == 200 and response.json()["status"] == "duplicate_event"
    enqueue.assert_called_once()


def test_new_event_of_same_crash_is_deduplicated(client, event, enqueue):
    first = post_sentry(client, event)
    event["data"]["event"]["event_id"] = "f" * 32
    second = post_sentry(client, event)
    assert second.status_code == 200
    assert second.json() == {"status": "deduplicated", "incident_id": first.json()["incident_id"]}


def test_enqueue_failure_is_500_so_sentry_retries(client, event, monkeypatch, redis_fake):
    delay = MagicMock(side_effect=[ConnectionError, None])
    monkeypatch.setattr(tasks.triage_incident, "delay", delay)
    assert post_sentry(client, event).status_code == 500  # Sentry does not retry 503
    assert not redis_fake.keys("sentry-event:*") and not redis_fake.keys("dedup:*")
    assert post_sentry(client, event).status_code == 202


def test_garbage_json_is_422(client, enqueue):
    body = b"not json"
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    response = client.post(
        "/webhook/sentry",
        content=body,
        headers={"Sentry-Hook-Resource": "event_alert", "Sentry-Hook-Signature": sig},
    )
    assert response.status_code == 422


def test_full_sentry_flow_links_back_in_slack(eager_celery, client, event, monkeypatch):
    slack = MagicMock()
    slack.post_alert.return_value = ("C1", "1.0")
    monkeypatch.setattr(tasks, "get_slack", lambda: slack)
    incident_id = post_sentry(client, event).json()["incident_id"]
    blocks = json.dumps(slack.post_alert.call_args.args[1])
    assert "acme.sentry.io/issues/1117540176" in blocks and "View original report" in blocks
    assert incident_store.get(incident_id)["slack_ts"] == "1.0"


def test_slack_link_refuses_injection():
    payload = CrashPayload.model_validate(
        {**crash_payload(), "source_url": "https://x.io/a|<!channel>"}
    )
    report = MagicMock(
        confidence_score=0.1,
        suspected_commit_sha="a",
        culprit_author="b",
        affected_file="c",
        root_cause="d",
    )
    assert "View original report" not in json.dumps(build_alert_blocks("i", payload, report, False))
