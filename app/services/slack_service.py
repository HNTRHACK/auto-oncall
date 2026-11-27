import json

from slack_sdk import WebClient
from slack_sdk.webhook import WebhookClient

from app.core.config import get_settings
from app.core.logging import pretty
from app.models.schemas import CrashPayload, RCAReport

PR_ACTION_ID = "create_hotfix_pr"
SLACK_RESPONSE_URL_PREFIX = "https://hooks.slack.com/"


def is_slack_response_url(url: str | None) -> bool:
    """Only ever POST to Slack's own hooks host (no SSRF via a crafted response_url)."""
    return bool(url) and url.startswith(SLACK_RESPONSE_URL_PREFIX)


def escape(text: str, limit: int = 2500) -> str:
    """Escape mrkdwn control characters so crash data cannot inject <!channel> or links."""
    text = text[:limit]
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}


def build_alert_blocks(
    incident_id: str,
    payload: CrashPayload,
    report: RCAReport,
    can_create_pr: bool,
    pr_url: str | None = None,
    note: str | None = None,
) -> list[dict]:
    title = f"🚨 {payload.error_type} in {payload.service_name} ({payload.environment})"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": title[:150]}},
        _section(f"```{escape(payload.error_message, 1000)}```"),
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*{name}*\n{escape(value, 300) or '—'}"}
                for name, value in (
                    ("Incident", incident_id),
                    ("Confidence", f"{report.confidence_score:.0%}"),
                    ("Suspected commit", report.suspected_commit_sha[:10]),
                    ("Author", report.culprit_author),
                    ("File", report.affected_file),
                )
            ],
        },
        _section(f"*Root cause (AI-generated, verify before acting)*\n{escape(report.root_cause)}"),
    ]
    url = payload.source_url
    if url and url.startswith("https://") and not any(c in url for c in "<>| \n"):
        blocks.append(_section(f"🔗 <{url}|View original report>"))
    if pr_url:
        blocks.append(_section(f"✅ *Hotfix PR opened:* <{pr_url}>"))
    elif note:
        blocks.append(_section(f"⚠️ {escape(note)}"))
    elif can_create_pr:
        button = {
            "type": "button",
            "action_id": PR_ACTION_ID,
            "style": "primary",
            "text": {"type": "plain_text", "text": "Create Hotfix PR"},
            "value": incident_id,
        }
        blocks.append({"type": "actions", "elements": [button]})
    else:
        blocks.append(_section("_No automatic hotfix offered: low confidence or no usable patch._"))
    return blocks


def build_failure_blocks(
    incident_id: str, stage: str, summary: str, error: str, attempts: int
) -> list[dict]:
    """Shown when a task gives up, so a broken pipeline is never silent."""
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🔴 Auto-OnCall {stage} failed"[:150]},
        },
        _section(f"*{escape(summary, 300)}*"),
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Incident*\n{escape(incident_id, 100)}"},
                {"type": "mrkdwn", "text": f"*Attempts*\n{attempts}"},
            ],
        },
        _section(f"*Last error*\n```{escape(error, 800)}```"),
        _section("_Automatic triage stopped. Investigate manually; see worker logs for details._"),
    ]


class SlackService:
    def __init__(self, client: WebClient | None = None):
        self.client = client or WebClient(token=get_settings().slack_bot_token, timeout=10)

    def post_alert(self, text: str, blocks: list[dict]) -> tuple[str, str]:
        response = self.client.chat_postMessage(
            channel=get_settings().slack_channel, text=text, blocks=blocks
        )
        return response["channel"], response["ts"]

    def update_message(self, channel: str, ts: str, text: str, blocks: list[dict]) -> None:
        self.client.chat_update(channel=channel, ts=ts, text=text, blocks=blocks)

    def send_ephemeral(self, response_url: str, text: str) -> None:
        if not is_slack_response_url(response_url):
            raise ValueError("refusing to post to a non-Slack response_url")
        WebhookClient(response_url, timeout=10).send(
            text=text, response_type="ephemeral", replace_original=False
        )


class FakeSlack:
    def post_alert(self, text: str, blocks: list[dict]) -> tuple[str, str]:
        pretty("DRY_RUN Slack message", json.dumps(blocks, indent=2, ensure_ascii=False))
        return "DRYRUN", "0.000001"

    def update_message(self, channel: str, ts: str, text: str, blocks: list[dict]) -> None:
        pretty("DRY_RUN Slack message update", json.dumps(blocks, indent=2, ensure_ascii=False))

    def send_ephemeral(self, response_url: str, text: str) -> None:
        pretty("DRY_RUN Slack ephemeral reply", text)


def get_slack() -> SlackService | FakeSlack:
    return FakeSlack() if get_settings().dry_run else SlackService()
