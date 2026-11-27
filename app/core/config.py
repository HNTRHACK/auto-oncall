from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    dry_run: bool = True
    log_level: str = "INFO"
    redis_url: str = "redis://localhost:6379/0"

    # Empty secrets reject every request (fail closed).
    webhook_secret: str = ""
    # Client Secret of the Sentry internal integration (enables POST /webhook/sentry).
    sentry_client_secret: str = ""
    slack_signing_secret: str = ""

    slack_bot_token: str = ""
    slack_channel: str = "#oncall"
    # Comma-separated Slack user IDs allowed to approve hotfix PRs (e.g. "U012AB3CD,U045EF6GH").
    # Empty: anyone may approve in DRY_RUN, nobody in live mode (fail closed).
    slack_approver_ids: str = ""

    github_token: str = ""
    github_repo: str = ""  # "owner/name"
    github_base_branch: str = "main"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"

    chroma_path: str = ".chroma"
    pr_confidence_threshold: float = 0.6
    dedup_ttl_seconds: int = 300
    incident_ttl_seconds: int = 7 * 24 * 3600
    max_diff_chars: int = 4000
    max_file_chars: int = 60_000

    @property
    def approver_ids(self) -> set[str]:
        return {uid.strip() for uid in self.slack_approver_ids.split(",") if uid.strip()}

    def can_approve(self, user_id: str | None) -> bool:
        if self.approver_ids:
            return user_id in self.approver_ids
        return self.dry_run


@lru_cache
def get_settings() -> Settings:
    return Settings()
