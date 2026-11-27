from contextlib import nullcontext
from unittest.mock import MagicMock

import anthropic
import httpx2
import pytest
from pydantic import ValidationError

from app.models.schemas import Commit, CrashPayload, FileDiff, RCAReport, StackFrame
from app.services import incident_store, rca_agent
from app.services.github_service import DEMO_COMMITS, DEMO_FILES, FakeGitHub, match_frames_to_files
from app.services.llm_client import RCA_TOOL, AnthropicLLM, FakeLLM, LLMError
from app.worker import tasks
from scripts.eval_rca import SCENARIOS, evaluate

CULPRIT = DEMO_COMMITS[1]
PATH = "app/payments/processor.py"
VALID = {
    "suspected_commit_sha": CULPRIT.sha[:10],
    "culprit_author": "someone the model made up",
    "root_cause": "find_customer can return None",
    "confidence_score": 0.9,
    "affected_file": PATH,
    "file_patch": "fixed\n",
}


def run(llm, payload, runbooks=()):
    github = FakeGitHub()
    crash = CrashPayload.model_validate(payload)
    files = rca_agent.select_files(crash, DEMO_COMMITS, github.get_file, 60_000)
    return rca_agent.analyze(llm, crash, DEMO_COMMITS, files, list(runbooks))


def test_valid_output_is_parsed_and_author_comes_from_github(payload):
    llm = MagicMock()
    llm.analyze.return_value = VALID
    report = run(llm, payload)
    assert report.suspected_commit_sha == CULPRIT.sha
    assert report.culprit_author == "rahul-k"
    assert report.confidence_score == 0.9
    llm.analyze.assert_called_once()


@pytest.mark.parametrize(
    "bad",
    [
        {},
        VALID | {"confidence_score": 1.5},
        VALID | {"suspected_commit_sha": "deadbeefdead"},
        VALID | {"affected_file": "../../etc/passwd"},
        VALID | {"extra": 1},
    ],
)
def test_malformed_output_retries_once_then_falls_back(payload, bad):
    llm = MagicMock()
    llm.analyze.return_value = bad
    report = run(llm, payload)
    assert llm.analyze.call_count == 2
    assert report.confidence_score == 0.0
    assert report.suspected_commit_sha == "unknown"
    assert report.file_patch == ""


def test_retry_can_succeed(payload):
    llm = MagicMock()
    llm.analyze.side_effect = [LLMError("no tool call"), VALID]
    assert run(llm, payload).confidence_score == 0.9


def test_transport_errors_propagate_for_celery_to_retry(payload):
    llm = MagicMock()
    llm.analyze.side_effect = ConnectionError
    with pytest.raises(ConnectionError):
        run(llm, payload)


@pytest.mark.parametrize("score,ok", [(0, True), (1, True), (-0.01, False), (1.01, False)])
def test_confidence_bounds(score, ok):
    context = nullcontext() if ok else pytest.raises(ValidationError)
    with context:
        RCAReport.model_validate(VALID | {"confidence_score": score})


@pytest.mark.parametrize(
    "frame,expected",
    [
        ("/srv/app/payments/processor.py", [PATH]),
        ("C:\\deploy\\app\\payments\\processor.py", [PATH]),
        ("./payments/processor.py", [PATH]),
        ("processor.py", [PATH]),
        ("/srv/app/payments/other_processor.py", []),
        ("/venv/site-packages/app/payments/processor.py", []),
    ],
)
def test_frame_to_diff_mapping(frame, expected):
    changed = ["README.md", PATH]
    frames = [StackFrame(filename=frame, lineno=1, function="f")]
    assert match_frames_to_files(frames, changed) == expected


def test_mapping_prefers_most_specific_and_follows_frame_order():
    changed = ["utils.py", "app/billing/utils.py", "app/api.py"]
    frames = [
        StackFrame(filename=name, lineno=1, function="f")
        for name in ("/srv/app/api.py", "/srv/app/billing/utils.py")
    ]
    assert match_frames_to_files(frames, changed) == ["app/api.py", "app/billing/utils.py"]


def test_total_diff_budget_keeps_selected_files():
    big = "x" * rca_agent.MAX_TOTAL_DIFF_CHARS
    commit = Commit(
        sha="a" * 40,
        author="d",
        message="m",
        files=[FileDiff(path="vendor.js", patch=big), FileDiff(path=PATH, patch="keep me")],
    )
    [dumped] = rca_agent.limit_diffs([commit], {PATH: None})
    assert [f["patch"] for f in dumped["files"]] == ["[diff omitted: size budget]", "keep me"]


def test_runbook_search_returns_relevant_top_two(runbook_store):
    results = runbook_store.search("AttributeError 'NoneType' object has no attribute 'email'")
    assert len(results) == 2
    assert results[0].id == "null-pointer-after-refactor"


def test_anthropic_client_returns_tool_input():
    client = MagicMock()
    block = MagicMock(type="tool_use", input=VALID)
    block.name = RCA_TOOL["name"]
    stream = client.beta.messages.stream.return_value.__enter__.return_value
    stream.get_final_message.return_value = MagicMock(stop_reason="tool_use", content=[block])
    assert AnthropicLLM(client).analyze("system", "prompt") == VALID
    kwargs = client.beta.messages.stream.call_args.kwargs
    assert kwargs["tools"][0]["strict"] is True
    assert kwargs["tool_choice"]["type"] == "auto"


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens", "end_turn"])
def test_anthropic_client_unusable_response(stop_reason):
    client = MagicMock()
    stream = client.beta.messages.stream.return_value.__enter__.return_value
    stream.get_final_message.return_value = MagicMock(stop_reason=stop_reason, content=[])
    with pytest.raises(LLMError):
        AnthropicLLM(client).analyze("system", "prompt")


@pytest.mark.parametrize("status,expected", [(400, LLMError), (429, anthropic.APIStatusError)])
def test_anthropic_client_error_classes(status, expected):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    client = MagicMock()
    client.beta.messages.stream.side_effect = anthropic.APIStatusError(
        "boom", response=httpx2.Response(status, request=request), body=None
    )
    with pytest.raises(expected):
        AnthropicLLM(client).analyze("system", "prompt")


def test_fake_llm_is_deterministic(payload):
    first = run(FakeLLM(), payload)
    assert first == run(FakeLLM(), payload)
    assert first.suspected_commit_sha == CULPRIT.sha
    assert "if customer else" in first.file_patch


def test_triage_saves_incident_and_is_idempotent(payload, monkeypatch, redis_fake):
    slack = MagicMock()
    slack.post_alert.return_value = ("C1", "1.0")
    monkeypatch.setattr(tasks, "get_slack", lambda: slack)
    tasks.run_triage("abc", payload)
    tasks.run_triage("abc", payload)  # redelivery
    slack.post_alert.assert_called_once()
    record = incident_store.get("abc")
    assert record["can_pr"] == "1" and record["slack_ts"] == "1.0"
    assert redis_fake.ttl("incident:abc") > 6 * 24 * 3600


def test_triage_retry_after_slack_failure_reuses_rca(payload, monkeypatch):
    llm = MagicMock(wraps=FakeLLM())
    slack = MagicMock()
    slack.post_alert.side_effect = [ConnectionError, ("C1", "1.0")]
    monkeypatch.setattr(tasks, "get_llm", lambda: llm)
    monkeypatch.setattr(tasks, "get_slack", lambda: slack)
    with pytest.raises(ConnectionError):
        tasks.run_triage("abc", payload)
    tasks.run_triage("abc", payload)
    llm.analyze.assert_called_once()
    assert incident_store.get("abc")["slack_ts"] == "1.0"


@pytest.mark.parametrize(
    "path,patch,confidence",
    [
        (".github/workflows/ci.yml", "evil\n", 0.99),
        (PATH, DEMO_FILES[PATH].rstrip("\n"), 0.99),  # no-op patch
        (PATH, "fixed\n", 0.59),
    ],
)
def test_pr_not_offered_for_unsafe_or_weak_patches(payload, monkeypatch, path, patch, confidence):
    report = RCAReport.model_validate(
        VALID | {"affected_file": path, "file_patch": patch, "confidence_score": confidence}
    )
    github = FakeGitHub(files=DEMO_FILES | {".github/workflows/ci.yml": "on: push\n"})
    monkeypatch.setattr(tasks, "get_github", lambda: github)
    monkeypatch.setattr(tasks.rca_agent, "select_files", lambda *a: {path: github.get_file(path)})
    monkeypatch.setattr(tasks.rca_agent, "analyze", lambda *a: report)
    tasks.run_triage("abc", payload)
    assert incident_store.get("abc")["can_pr"] == "0"


def test_eval_runs_all_scenarios(runbook_store):
    assert len(SCENARIOS) == 10
    # Two scenarios are built to defeat the heuristic.
    assert evaluate(FakeLLM(), runbook_store) == 8
