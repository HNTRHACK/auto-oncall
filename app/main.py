import json
import uuid
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

import redis
import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.security import (
    TIMESTAMP_TOLERANCE_SECONDS,
    verify_slack_signature,
    verify_webhook_signature,
)
from app.models.schemas import CrashPayload, fingerprint
from app.services import incident_store, metrics
from app.services.sentry_adapter import NotACrash, parse_sentry_webhook, verify_sentry_signature
from app.services.slack_service import PR_ACTION_ID, is_slack_response_url
from app.worker.tasks import create_hotfix_pr, notify_unauthorised, triage_incident

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging(get_settings().log_level)
    log.info("api_started", dry_run=get_settings().dry_run)
    yield


app = FastAPI(title="Auto-OnCall", lifespan=lifespan)


async def raw_body(request: Request) -> bytes:
    """Signatures cover the exact bytes; reading here lets handlers stay sync (threadpool)."""
    body = await request.body()
    if len(body) > 1_000_000:
        raise HTTPException(413, "payload too large")
    return body


def accept_crash(payload: CrashPayload, failure_status: int = 503, on_failure=None):
    """Dedup by fingerprint and enqueue triage. Shared by every ingestion route."""
    settings = get_settings()
    key = f"dedup:{fingerprint(payload)}"
    incident_id = uuid.uuid4().hex[:12]
    try:
        r = incident_store.get_redis()
        # SET NX GET (Redis 7): one atomic step returns the owner of an existing key.
        existing = r.set(key, incident_id, nx=True, ex=settings.dedup_ttl_seconds, get=True)
    except redis.RedisError as exc:
        log.error("redis_unavailable", error=repr(exc))
        if on_failure:
            on_failure()
        raise HTTPException(failure_status, "redis unavailable") from None
    if existing:
        log.info("incident_deduplicated", incident_id=existing)
        metrics.incr("incidents_deduplicated")
        return JSONResponse({"status": "deduplicated", "incident_id": existing}, status_code=200)

    try:
        triage_incident.delay(incident_id, payload.model_dump(mode="json"))
    except Exception as exc:
        log.error("enqueue_failed", incident_id=incident_id, error=repr(exc))
        try:
            r.delete(key)  # let the sender's retry through
        except redis.RedisError:
            log.error("dedup_key_release_failed", incident_id=incident_id)
        if on_failure:
            on_failure()
        raise HTTPException(failure_status, "could not enqueue triage") from None
    log.info("incident_accepted", incident_id=incident_id, service=payload.service_name)
    metrics.incr("incidents_accepted")
    return {"status": "accepted", "incident_id": incident_id}


@app.post("/webhook/crash", status_code=202)
def crash_webhook(
    body: bytes = Depends(raw_body),
    x_signature_256: str | None = Header(default=None),
    x_webhook_timestamp: str | None = Header(default=None),
):
    settings = get_settings()
    # The timestamp is signed and must be fresh, so a captured request dies after 5 minutes;
    # inside that window an exact replay is just a duplicate and the fingerprint dedups it.
    if not verify_webhook_signature(
        settings.webhook_secret, body, x_webhook_timestamp, x_signature_256
    ):
        metrics.incr("webhook_rejected")
        raise HTTPException(401, "invalid signature")
    try:
        payload = CrashPayload.model_validate_json(body)
    except ValidationError as exc:
        metrics.incr("webhook_rejected")
        raise HTTPException(
            422, exc.errors(include_url=False, include_context=False, include_input=False)
        ) from None

    return accept_crash(payload)


@app.post("/webhook/sentry", status_code=202)
def sentry_webhook(
    body: bytes = Depends(raw_body),
    sentry_hook_resource: str | None = Header(default=None),
    sentry_hook_signature: str | None = Header(default=None),
):
    """Sentry Integration Platform webhook (issue alerts and error events).

    Sentry expects an answer within 1 second, only retries 5xx other than 503/504, and
    disables integrations that keep failing. So: verify, dedup, enqueue, answer; ignore
    (with 200) anything that is not a crash; and fail with 500, not 503, so it retries.
    """
    settings = get_settings()
    if not verify_sentry_signature(settings.sentry_client_secret, body, sentry_hook_signature):
        metrics.incr("webhook_rejected")
        raise HTTPException(401, "invalid signature")
    try:
        event_id, payload = parse_sentry_webhook(sentry_hook_resource, json.loads(body))
    except NotACrash as exc:
        log.info("sentry_webhook_ignored", resource=sentry_hook_resource, reason=str(exc))
        return JSONResponse({"status": "ignored", "reason": str(exc)}, status_code=200)
    except (ValueError, TypeError, AttributeError, ValidationError) as exc:
        metrics.incr("webhook_rejected")
        log.warning("sentry_webhook_unparseable", error=repr(exc))
        raise HTTPException(422, "unrecognised Sentry payload") from None

    # The body signature has no timestamp, so remember event ids: a replay of the same event
    # (or Sentry re-delivering it) after the dedup window is still dropped.
    seen_key = f"sentry-event:{event_id}" if event_id else None
    if seen_key:
        try:
            first = incident_store.claim_once(seen_key, settings.incident_ttl_seconds)
        except redis.RedisError:
            raise HTTPException(500, "redis unavailable") from None
        if not first:
            log.info("sentry_event_already_seen", sentry_event_id=event_id)
            metrics.incr("incidents_deduplicated")
            return JSONResponse({"status": "duplicate_event"}, status_code=200)

    def release():
        if seen_key:
            try:
                incident_store.release(seen_key)
            except redis.RedisError:
                log.error("sentry_event_key_release_failed", sentry_event_id=event_id)

    log.info("sentry_event_received", sentry_event_id=event_id, service=payload.service_name)
    return accept_crash(payload, failure_status=500, on_failure=release)


@app.post("/slack/actions")
def slack_actions(
    body: bytes = Depends(raw_body),
    x_slack_signature: str | None = Header(default=None),
    x_slack_request_timestamp: str | None = Header(default=None),
):
    settings = get_settings()
    if not verify_slack_signature(
        settings.slack_signing_secret, body, x_slack_request_timestamp, x_slack_signature
    ):
        raise HTTPException(401, "invalid signature")
    try:
        interaction = json.loads(parse_qs(body.decode())["payload"][0])
        incident_ids = {
            a["value"] for a in interaction.get("actions", []) if a.get("action_id") == PR_ACTION_ID
        }
        user_id = (interaction.get("user") or {}).get("id")
        response_url = interaction.get("response_url")
    except (KeyError, ValueError, TypeError, AttributeError):
        raise HTTPException(400, "malformed interaction payload") from None

    # Replay protection: Slack never resends an interaction, so a second request with the
    # same signature inside the timestamp window is a replay. Ack it and do nothing.
    replay_key = f"slack-replay:{x_slack_signature}"
    try:
        first_time = incident_store.claim_once(replay_key, TIMESTAMP_TOLERANCE_SECONDS + 60)
    except redis.RedisError as exc:
        log.error("redis_unavailable", error=repr(exc))
        raise HTTPException(503, "redis unavailable") from None
    if not first_time:
        log.warning("slack_replay_rejected", slack_user=user_id)
        metrics.incr("slack_replays_rejected")
        return {}

    try:
        if not settings.can_approve(user_id):
            log.warning("hotfix_unauthorised", slack_user=user_id, incidents=sorted(incident_ids))
            metrics.incr("hotfix_unauthorised")
            if incident_ids and is_slack_response_url(response_url):
                notify_unauthorised.delay(response_url, sorted(incident_ids)[0])
            return {}
        # Only enqueue here: Slack needs the ack within 3 seconds.
        for incident_id in incident_ids:
            create_hotfix_pr.delay(incident_id)
            log.info("hotfix_requested", incident_id=incident_id, slack_user=user_id)
    except Exception as exc:
        log.error("enqueue_failed", incidents=sorted(incident_ids), error=repr(exc))
        try:
            incident_store.release(replay_key)  # a manual retry of the click must get through
        except redis.RedisError:
            log.error("replay_key_release_failed")
        raise HTTPException(503, "could not enqueue hotfix") from None
    return {}


@app.get("/health")
def health():
    try:
        incident_store.get_redis().ping()
    except redis.RedisError:
        return JSONResponse({"status": "unhealthy", "redis": "down"}, status_code=503)
    return {"status": "ok", "redis": "up"}


@app.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics():
    """Prometheus text format. Counters are summed across the API and all workers via Redis."""
    try:
        values = metrics.snapshot()
    except redis.RedisError:
        raise HTTPException(503, "redis unavailable") from None
    return PlainTextResponse(
        metrics.render_prometheus(values), media_type="text/plain; version=0.0.4"
    )
