from typing import Any

import anthropic

from app.core.config import get_settings
from app.models.schemas import Commit, CrashPayload, SourceFile
from app.services.github_service import DEMO_FIXES, match_frames_to_files

TOOL_NAME = "submit_rca_report"

RCA_TOOL = {
    "name": TOOL_NAME,
    "description": "Submit the root cause analysis report. Call exactly once.",
    "strict": True,
    # Strict mode rejects numeric bounds, so the 0-1 range is enforced by Pydantic.
    "input_schema": {
        "type": "object",
        "properties": {
            "suspected_commit_sha": {"type": "string", "description": "Full SHA from the list"},
            "culprit_author": {"type": "string"},
            "root_cause": {
                "type": "string",
                "description": "Concise explanation, under 1500 chars",
            },
            "confidence_score": {"type": "number", "description": "Between 0 and 1"},
            "affected_file": {"type": "string", "description": "A path from the provided files"},
            "file_patch": {
                "type": "string",
                "description": "The FULL new content of affected_file, not a diff",
            },
        },
        "required": [
            "suspected_commit_sha",
            "culprit_author",
            "root_cause",
            "confidence_score",
            "affected_file",
            "file_patch",
        ],
        "additionalProperties": False,
    },
}


class LLMError(Exception):
    """The model gave no usable answer; retrying the whole task would not help."""


class AnthropicLLM:
    def __init__(self, client: anthropic.Anthropic | None = None):
        s = get_settings()
        # max_retries=0: Celery owns retry/backoff.
        self.client = client or anthropic.Anthropic(
            api_key=s.anthropic_api_key or None, max_retries=0, timeout=600
        )
        self.model = s.anthropic_model

    def analyze(self, system: str, prompt: str, **_context: Any) -> dict:
        try:
            # Streaming: a full-file patch can be long. Forced tool_choice is rejected by
            # current models, so the prompt demands the tool and `strict` pins the schema.
            with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=64_000,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                tools=[RCA_TOOL],
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ) as stream:
                message = stream.get_final_message()
        except anthropic.APIStatusError as exc:
            if exc.status_code in (408, 409, 429) or exc.status_code >= 500:
                raise
            raise LLMError(f"Claude API rejected the request: {exc.status_code}") from exc
        if message.stop_reason in ("refusal", "max_tokens"):
            raise LLMError(f"unusable response: stop_reason={message.stop_reason}")
        for block in message.content:
            if block.type == "tool_use" and block.name == TOOL_NAME:
                return block.input
        raise LLMError("model did not call the report tool")


class FakeLLM:
    """DRY_RUN: deterministic heuristic, no network.

    Blames the newest commit touching the highest stack frame; if the trace matches
    nothing, blames the newest commit with low confidence.
    """

    def analyze(
        self,
        system: str,
        prompt: str,
        *,
        payload: CrashPayload,
        commits: list[Commit],
        files: dict[str, SourceFile],
    ) -> dict:
        changed = [f.path for c in commits for f in c.files]
        culprit, path, confidence = None, None, 0.3
        for candidate in match_frames_to_files(payload.stack_trace, changed):
            if candidate in files:
                culprit = next(c for c in commits if any(f.path == candidate for f in c.files))
                path, confidence = candidate, 0.87
                break
        if culprit is None:
            culprit = next((c for c in commits if any(f.path in files for f in c.files)), None)
            if culprit is None:
                raise LLMError("no readable changed files to analyse")
            path = next(f.path for f in culprit.files if f.path in files)
        original = files[path].content
        return {
            "suspected_commit_sha": culprit.sha,
            "culprit_author": culprit.author,
            "root_cause": f"{payload.error_type} in {path}: introduced by "
            f"'{culprit.message.splitlines()[0]}'.",
            "confidence_score": confidence,
            "affected_file": path,
            "file_patch": DEMO_FIXES.get(
                path, original + "# TODO(hotfix): guard the failing call\n"
            ),
        }


def get_llm() -> AnthropicLLM | FakeLLM:
    return FakeLLM() if get_settings().dry_run else AnthropicLLM()
