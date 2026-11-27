import random
import time
import urllib.error

import anthropic
import redis
import requests
import structlog
from github import GithubException, RateLimitExceededException
from slack_sdk.errors import SlackApiError

from app.core.config import get_settings
from app.models.schemas import CrashPayload, RCAReport
from app.services import incident_store, metrics, rca_agent
from app.services.github_service import StaleFileError, get_github
from app.services.llm_client import get_llm
from app.services.patch_check import check_patch
from app.services.runbook_store import get_runbook_store
from app.services.slack_service import build_alert_blocks, build_failure_blocks, get_slack
from app.worker.celery_app import celery_app

log = structlog.get_logger(__name__)

MAX_RETRIES = 5
BACKOFF_BASE = 2
BACKOFF_CAP = 300
LOCK_TTL = 120

NETWORK_ERRORS = (
    ConnectionError,
    TimeoutError,
    urllib.error.URLError,  # slack-sdk transport
    requests.ConnectionError,  # PyGithub transport
    requests.Timeout,
    anthropic.APIConnectionError,
    redis.ConnectionError,
    redis.TimeoutError,
)


class HotfixInProgress(Exception):
    def __init__(self, retry_after: int):
        super().__init__("another worker holds the hotfix lock")
        self.retry_after = retry_after


def retry_delay(exc: Exception, attempt: int, now: float | None = None) -> float | None:
    """Seconds to wait before retrying, or None if the error is permanent.

    Exponential backoff with jitter, never earlier than the server's Retry-After
    or GitHub's X-RateLimit-Reset.
    """
    now = time.time() if now is None else now
    server_wait = 0.0
    if isinstance(exc, GithubException):
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        exhausted = headers.get("x-ratelimit-remaining") == "0"
        limited = (
            isinstance(exc, RateLimitExceededException)
            or exc.status == 429
            or (exc.status == 403 and (exhausted or "retry-after" in headers))
        )
        if not (limited or exc.status >= 500):
            return None
        if "retry-after" in headers:
            server_wait = float(headers["retry-after"])
        elif exhausted and "x-ratelimit-reset" in headers:
            server_wait = float(headers["x-ratelimit-reset"]) - now + 1
    elif isinstance(exc, anthropic.APIStatusError):
        if not (exc.status_code in (408, 409, 429) or exc.status_code >= 500):
            return None
        server_wait = float(exc.response.headers.get("retry-after", 0))
    elif isinstance(exc, SlackApiError):
        status = exc.response.status_code
        if not (status == 429 or status >= 500):
            return None
        server_wait = float(exc.response.headers.get("Retry-After", 0))
    elif isinstance(exc, HotfixInProgress):
        server_wait = exc.retry_after
    elif not isinstance(exc, NETWORK_ERRORS):
        return None
    ceiling = min(BACKOFF_CAP, BACKOFF_BASE * 2**attempt)
    jittered = random.uniform(ceiling / 2, ceiling)
    return max(jittered, server_wait + random.uniform(0, 1) if server_wait > 0 else 0)


def _retry_or_raise(task, exc: Exception, on_give_up=None):
    delay = retry_delay(exc, task.request.retries)
    if delay is None or task.request.retries >= task.max_retries:
        log.error(
            "task_failed",
            task=task.name,
            attempts=task.request.retries + 1,
            permanent=delay is None,
            error=repr(exc),
        )
        if on_give_up is not None:
            on_give_up(exc, task.request.retries + 1)
        raise exc
    log.warning(
        "task_retry",
        task=task.name,
        attempt=task.request.retries + 1,
        countdown=round(delay, 1),
        error=repr(exc),
    )
    raise task.retry(exc=exc, countdown=delay)


def run_triage(incident_id: str, payload_data: dict) -> None:
    """Idempotent: the saved RCA is reused and an alert already posted is not reposted."""
    settings = get_settings()
    record = incident_store.get(incident_id)
    if record.get("slack_ts"):
        log.info("triage_already_done", incident_id=incident_id)
        return
    payload = CrashPayload.model_validate(payload_data)

    if "rca" in record:
        report = RCAReport.model_validate_json(record["rca"])
        can_pr = record["can_pr"] == "1"
        patch_problem = record.get("patch_problem", "")
    else:
        github = get_github()
        commits = github.recent_commits(5)
        files = rca_agent.select_files(payload, commits, github.get_file, settings.max_file_chars)
        query = f"{payload.error_type} {payload.error_message}"
        runbooks = get_runbook_store().search(query, top_k=2)
        log.info(
            "triage_context",
            incident_id=incident_id,
            commits=len(commits),
            files=list(files),
            runbooks=[r.id for r in runbooks],
        )
        report = rca_agent.analyze(get_llm(), payload, commits, files, runbooks)
        source = files.get(report.affected_file)
        patch = report.file_patch
        if source and patch and source.content.endswith("\n") and not patch.endswith("\n"):
            patch += "\n"
            report = report.model_copy(update={"file_patch": patch})
        can_pr = bool(
            source
            and patch.strip()
            and patch != source.content
            # CI definitions run with repo secrets; never let a model rewrite them.
            and not report.affected_file.startswith(".github/")
            and report.confidence_score >= settings.pr_confidence_threshold
        )
        patch_problem = ""
        if can_pr:
            patch_problem = check_patch(report.affected_file, source.content, patch) or ""
            if patch_problem:
                log.warning("patch_rejected", incident_id=incident_id, reason=patch_problem)
                can_pr = False
        incident_store.save(
            incident_id,
            incident=payload.model_dump_json(),
            rca=report.model_dump_json(),
            base_blob_sha=source.blob_sha if source else "",
            can_pr="1" if can_pr else "0",
            patch_problem=patch_problem,
        )

    channel, ts = get_slack().post_alert(
        f"{payload.error_type} in {payload.service_name}",
        build_alert_blocks(
            incident_id,
            payload,
            report,
            can_pr,
            note=f"No automatic hotfix: {patch_problem}." if patch_problem else None,
        ),
    )
    incident_store.save(incident_id, slack_channel=channel, slack_ts=ts)
    metrics.incr("triage_completed")
    log.info(
        "triage_complete",
        incident_id=incident_id,
        confidence=report.confidence_score,
        commit=report.suspected_commit_sha,
        pr_offered=can_pr,
    )


def run_hotfix(incident_id: str) -> None:
    """Idempotent: one PR per incident however often the button is clicked or redelivered."""
    record = incident_store.get(incident_id)
    if "rca" not in record or record.get("can_pr") != "1":
        # Expired incident, or a PR was never offered: re-check server side.
        log.warning("hotfix_refused", incident_id=incident_id)
        return
    payload = CrashPayload.model_validate_json(record["incident"])
    report = RCAReport.model_validate_json(record["rca"])

    r = incident_store.get_redis()
    lock = f"hotfix-lock:{incident_id}"
    if not r.set(lock, "1", nx=True, ex=LOCK_TTL):
        raise HotfixInProgress(max(r.ttl(lock), 1))
    try:
        pr_url = incident_store.get(incident_id).get("pr_url")
        note = None
        if not pr_url:
            body = (
                f"Automated hotfix for incident `{incident_id}` "
                f"({payload.service_name}, {payload.environment}).\n\n"
                f"**Suspected commit:** {report.suspected_commit_sha}\n"
                f"**Confidence:** {report.confidence_score:.0%}\n\n"
                # Zero-width space stops model text from @-mentioning people.
                f"**Root cause (AI-generated):** {report.root_cause.replace('@', '@' + chr(0x200B))}\n\n"
                "⚠️ Review carefully before merging."
            )
            try:
                pr_url = get_github().create_hotfix_pr(
                    branch=f"hotfix/incident-{incident_id}",
                    path=report.affected_file,
                    content=report.file_patch,
                    base_blob_sha=record["base_blob_sha"],
                    title=f"Hotfix: {payload.error_type} in {payload.service_name} ({incident_id})",
                    body=body,
                )
                incident_store.save(incident_id, pr_url=pr_url)
                metrics.incr("hotfix_prs_created")
                log.info("hotfix_pr_created", incident_id=incident_id, pr_url=pr_url)
            except StaleFileError as exc:
                note = f"Hotfix PR not created: {exc}. Please fix manually."
                log.warning("hotfix_stale", incident_id=incident_id, error=str(exc))
        else:
            log.info("hotfix_already_exists", incident_id=incident_id, pr_url=pr_url)
        if record.get("slack_ts"):
            get_slack().update_message(
                record["slack_channel"],
                record["slack_ts"],
                f"Hotfix update for incident {incident_id}",
                build_alert_blocks(incident_id, payload, report, False, pr_url=pr_url, note=note),
            )
    finally:
        # shortcut: no owner token; if hotfixes ever outlive LOCK_TTL, use redis-py's Lock.
        r.delete(lock)


def alert_failure(incident_id: str, stage: str, exc: Exception, attempts: int) -> None:
    """Best effort: tell on-call the pipeline gave up. Never raises (Slack may be the cause)."""
    try:
        record = incident_store.get(incident_id)
        summary = "unknown incident"
        if "incident" in record:
            incident = CrashPayload.model_validate_json(record["incident"])
            summary = f"{incident.error_type} in {incident.service_name} ({incident.environment})"
        elif stage == "triage" and record.get("summary"):
            summary = record["summary"]
        error = f"{type(exc).__name__}: {exc}"[:800]
        incident_store.save(incident_id, status=f"{stage}_failed", last_error=error)
        metrics.incr(f"{stage}_failed")
        get_slack().post_alert(
            f"Auto-OnCall {stage} failed for incident {incident_id}",
            build_failure_blocks(incident_id, stage, summary, error, attempts),
        )
        log.info("failure_alert_sent", incident_id=incident_id, stage=stage)
    except Exception as alert_exc:  # noqa: BLE001 - alerting must not mask the real error
        log.error("failure_alert_failed", incident_id=incident_id, error=repr(alert_exc))


def _triage_summary(payload: dict) -> str:
    try:
        crash = CrashPayload.model_validate(payload)
        return f"{crash.error_type} in {crash.service_name} ({crash.environment})"
    except Exception:  # noqa: BLE001 - a bad payload is exactly when we still want an alert
        return "unparseable crash payload"


@celery_app.task(bind=True, name="triage_incident", max_retries=MAX_RETRIES, acks_late=True)
def triage_incident(self, incident_id: str, payload: dict) -> None:
    try:
        run_triage(incident_id, payload)
    except Exception as exc:

        def give_up(error: Exception, attempts: int) -> None:
            if "incident" not in incident_store.get(incident_id):
                incident_store.save(incident_id, summary=_triage_summary(payload))
            alert_failure(incident_id, "triage", error, attempts)

        _retry_or_raise(self, exc, give_up)


@celery_app.task(bind=True, name="create_hotfix_pr", max_retries=MAX_RETRIES, acks_late=True)
def create_hotfix_pr(self, incident_id: str) -> None:
    try:
        run_hotfix(incident_id)
    except Exception as exc:
        _retry_or_raise(
            self, exc, lambda error, attempts: alert_failure(incident_id, "hotfix", error, attempts)
        )


@celery_app.task(bind=True, name="notify_unauthorised", max_retries=3, acks_late=True)
def notify_unauthorised(self, response_url: str, incident_id: str) -> None:
    """Ephemeral 'not authorised' reply to a click from someone outside the allowlist."""
    try:
        get_slack().send_ephemeral(
            response_url,
            f"You are not on the hotfix approver list, so no PR was created for incident "
            f"{incident_id}. Ask an approver to click the button.",
        )
    except Exception as exc:
        _retry_or_raise(self, exc)
