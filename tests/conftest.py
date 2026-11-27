import json
import os
import socket
import time
import uuid
from urllib.parse import urlencode

import fakeredis
import pytest

# Set before app modules are imported; env vars win over any local .env file.
os.environ.update(
    DRY_RUN="true",
    WEBHOOK_SECRET="test-webhook-secret",
    SLACK_SIGNING_SECRET="test-slack-secret",
    REDIS_URL="redis://localhost:1/0",
    PR_CONFIDENCE_THRESHOLD="0.6",
)

from fastapi.testclient import TestClient  # noqa: E402
from slack_sdk.signature import SignatureVerifier  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.security import sign_webhook  # noqa: E402
from app.main import app  # noqa: E402
from app.services import incident_store  # noqa: E402
from app.services.runbook_store import HashEmbedding, RunbookStore  # noqa: E402
from app.worker import tasks  # noqa: E402
from app.worker.celery_app import celery_app  # noqa: E402
from scripts.seed_runbooks import RUNBOOKS  # noqa: E402
from scripts.simulate_crash import crash_payload  # noqa: E402


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if anything leaves the machine."""
    real_connect = socket.socket.connect

    def guarded(sock, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
            raise AssertionError(f"network access attempted: {address}")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture(autouse=True)
def redis_fake(monkeypatch):
    fake = fakeredis.FakeRedis(decode_responses=True, server=fakeredis.FakeServer())
    monkeypatch.setattr(incident_store, "get_redis", lambda: fake)
    return fake


@pytest.fixture(scope="session")
def runbook_store(tmp_path_factory):
    store = RunbookStore(str(tmp_path_factory.mktemp("chroma")), HashEmbedding())
    store.add(RUNBOOKS)
    return store


@pytest.fixture(autouse=True)
def fake_runbooks(monkeypatch, runbook_store):
    monkeypatch.setattr(tasks, "get_runbook_store", lambda: runbook_store)


@pytest.fixture
def eager_celery():
    # task_eager_propagates stays False so eager retries run instead of raising Retry.
    celery_app.conf.task_always_eager = True
    yield
    celery_app.conf.task_always_eager = False


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def payload():
    return crash_payload()


@pytest.fixture
def post_crash(client, settings):
    def post(data: dict, signature: str | None = None, age: int = 0, timestamp: str | None = None):
        body = json.dumps(data).encode()
        ts = timestamp if timestamp is not None else str(int(time.time()) - age)
        header = signature or sign_webhook(settings.webhook_secret, body, ts)
        return client.post(
            "/webhook/crash",
            content=body,
            headers={"X-Signature-256": header, "X-Webhook-Timestamp": ts},
        )

    return post


@pytest.fixture
def post_slack(client, settings):
    def post(
        incident_id: str,
        age: int = 0,
        secret: str | None = None,
        user: str = "U_TEST",
        response_url: str | None = "https://hooks.slack.com/actions/T1/B1",
        nonce: str | None = None,
    ):
        interaction = {
            "user": {"id": user},
            "response_url": response_url,
            "actions": [{"action_id": "create_hotfix_pr", "value": incident_id}],
            # Distinct per call unless a test pins it, like two separate real clicks.
            "trigger_id": nonce or uuid.uuid4().hex,
        }
        body = urlencode({"payload": json.dumps(interaction)}).encode()
        timestamp = str(int(time.time()) - age)
        signature = SignatureVerifier(secret or settings.slack_signing_secret).generate_signature(
            timestamp=timestamp, body=body
        )
        return client.post(
            "/slack/actions",
            content=body,
            headers={"X-Slack-Request-Timestamp": timestamp, "X-Slack-Signature": signature},
        )

    return post
