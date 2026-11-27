from unittest.mock import MagicMock

import pytest
import redis

from app.models.schemas import CrashPayload, fingerprint, normalize_message
from app.services import incident_store
from app.worker.tasks import triage_incident


@pytest.fixture
def delay(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(triage_incident, "delay", mock)
    return mock


def test_new_crash_is_accepted_and_enqueued_once(post_crash, payload, delay, redis_fake):
    response = post_crash(payload)
    assert response.status_code == 202
    incident_id = response.json()["incident_id"]
    delay.assert_called_once()
    assert delay.call_args.args[0] == incident_id
    [key] = redis_fake.keys("dedup:*")
    assert 0 < redis_fake.ttl(key) <= 300


def test_duplicate_is_deduplicated(post_crash, payload, delay):
    first = post_crash(payload)
    second = post_crash(payload)
    assert second.status_code == 200
    assert second.json() == {"status": "deduplicated", "incident_id": first.json()["incident_id"]}
    delay.assert_called_once()


def test_messages_differing_only_in_ids_dedupe(post_crash, payload, delay):
    template = 'order {} for user {!r} failed at {} (req {}) after {}ms: "{}"'
    payload["error_message"] = template.format(
        1, "alice", "0x7ffde4a1", "550e8400-e29b-41d4-a716-446655440000", 12.5, "a"
    )
    assert post_crash(payload).status_code == 202
    payload["error_message"] = template.format(
        98765, "bob", "0xdeadbeef", "123e4567-e89b-12d3-a456-426614174000", 3, "b"
    )
    assert post_crash(payload).json()["status"] == "deduplicated"
    delay.assert_called_once()


def test_normalize_keeps_apostrophes_and_words():
    assert normalize_message("can't open it, won't retry") == "can't open it, won't retry"


@pytest.mark.parametrize(
    "change",
    [
        {"service_name": "other"},
        {"error_message": "something else"},
        {"stack_trace": [{"filename": "b.py", "lineno": 1, "function": "g"}]},
    ],
)
def test_distinct_crashes_have_distinct_fingerprints(payload, change):
    base = CrashPayload.model_validate(payload)
    assert fingerprint(base) != fingerprint(CrashPayload.model_validate(payload | change))


@pytest.mark.parametrize("signature", ["sha256=" + "0" * 64, "garbage", "sha256=é"])
def test_bad_signature_is_rejected(post_crash, payload, delay, signature):
    assert post_crash(payload, signature=signature.encode("utf-8")).status_code == 401
    delay.assert_not_called()


def test_missing_signature_and_empty_secret_are_rejected(
    client, post_crash, payload, delay, settings, monkeypatch
):
    assert client.post("/webhook/crash", content=b"{}").status_code == 401
    monkeypatch.setattr(settings, "webhook_secret", "")
    assert post_crash(payload).status_code == 401
    delay.assert_not_called()


def test_invalid_payload_is_422(post_crash, payload, delay):
    payload["stack_trace"] = []
    assert post_crash(payload).status_code == 422
    delay.assert_not_called()


def test_enqueue_failure_releases_key(post_crash, payload, delay, redis_fake):
    delay.side_effect = ConnectionError("broker down")
    assert post_crash(payload).status_code == 503
    assert redis_fake.keys("dedup:*") == []
    delay.side_effect = None
    assert post_crash(payload).status_code == 202


def test_redis_down_is_503(post_crash, payload, delay, monkeypatch):
    broken = MagicMock()
    broken.set.side_effect = redis.ConnectionError
    broken.ping.side_effect = redis.ConnectionError
    monkeypatch.setattr(incident_store, "get_redis", lambda: broken)
    assert post_crash(payload).status_code == 503
    delay.assert_not_called()


def test_health(client, monkeypatch):
    assert client.get("/health").json() == {"status": "ok", "redis": "up"}
    broken = MagicMock()
    broken.ping.side_effect = redis.ConnectionError
    monkeypatch.setattr(incident_store, "get_redis", lambda: broken)
    assert client.get("/health").status_code == 503
