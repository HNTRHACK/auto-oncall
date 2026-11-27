from unittest.mock import MagicMock

import anthropic
import httpx2
import pytest
from github import GithubException, RateLimitExceededException, UnknownObjectException
from slack_sdk.errors import SlackApiError

from app.services.github_service import GitHubService, StaleFileError, truncate_diff
from app.worker import tasks

BRANCH = "hotfix/incident-abc"


def content(text: str, sha: str):
    return MagicMock(decoded_content=text.encode(), sha=sha, encoding="base64")


def make_repo(main=("old", "s1"), branch=("old", "s1"), pulls=()):
    repo = MagicMock()
    repo.owner.login = "acme"
    repo.get_pulls.return_value = list(pulls)
    repo.get_contents.side_effect = lambda path, ref: content(*(main if ref == "main" else branch))
    repo.create_pull.return_value.html_url = "https://github.com/acme/r/pull/1"
    return repo


def open_pr(repo):
    return GitHubService(repo).create_hotfix_pr(BRANCH, "a.py", "new", "s1", "title", "body")


def test_creates_branch_commit_and_pr():
    repo = make_repo()
    assert open_pr(repo).endswith("/pull/1")
    repo.create_git_ref.assert_called_once()
    assert repo.create_git_ref.call_args.args[0] == f"refs/heads/{BRANCH}"
    repo.update_file.assert_called_once_with("a.py", "title", "new", "s1", branch=BRANCH)
    repo.create_pull.assert_called_once_with(title="title", body="body", head=BRANCH, base="main")


def test_existing_pr_is_returned_without_writes():
    repo = make_repo(pulls=[MagicMock(html_url="https://existing")])
    assert open_pr(repo) == "https://existing"
    repo.get_pulls.assert_called_once_with(state="all", head=f"acme:{BRANCH}")
    repo.create_git_ref.assert_not_called()
    repo.update_file.assert_not_called()
    repo.create_pull.assert_not_called()


def test_resumes_after_partial_failure():
    repo = make_repo(branch=("new", "s2"))
    repo.create_git_ref.side_effect = GithubException(422, {"message": "Reference already exists"})
    open_pr(repo)
    repo.update_file.assert_not_called()
    repo.create_pull.assert_called_once()


@pytest.mark.parametrize("kwargs", [{"main": ("moved", "s9")}, {"branch": ("hand edit", "s9")}])
def test_never_overwrites_newer_changes(kwargs):
    repo = make_repo(**kwargs)
    with pytest.raises(StaleFileError):
        open_pr(repo)
    repo.update_file.assert_not_called()
    repo.create_pull.assert_not_called()


def test_recent_commits_truncates_large_diffs(settings):
    big = MagicMock(filename="a.py", status="modified", patch="x" * (settings.max_diff_chars + 50))
    binary = MagicMock(filename="b.png", status="added", patch=None)
    commit = MagicMock(sha="a" * 40, files=[big, binary])
    commit.author.login = "dev"
    commit.commit.message = "msg"
    repo = MagicMock()
    repo.get_commits.return_value = [commit] * 8
    commits = GitHubService(repo).recent_commits()
    repo.get_commits.assert_called_once_with(sha="main")
    assert len(commits) == 5
    assert "50 chars omitted" in commits[0].files[0].patch
    assert commits[0].files[1].patch == ""


def test_truncate_leaves_small_diffs():
    assert truncate_diff("abc", 3) == "abc"


def test_get_file():
    repo = make_repo()
    assert GitHubService(repo).get_file("a.py").blob_sha == "s1"
    repo.get_contents.side_effect = UnknownObjectException(404, {})
    assert GitHubService(repo).get_file("gone.py") is None


NOW = 1_000_000.0


def test_rate_limit_waits_for_reset():
    exc = RateLimitExceededException(
        403, {}, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(NOW) + 900)}
    )
    assert 901 <= tasks.retry_delay(exc, 0, now=NOW) <= 902


def test_retry_after_header_is_honoured():
    exc = GithubException(403, {}, {"Retry-After": "60"})
    assert 60 <= tasks.retry_delay(exc, 0, now=NOW) <= 61
    slack = SlackApiError("x", MagicMock(status_code=429, headers={"Retry-After": "30"}))
    assert 30 <= tasks.retry_delay(slack, 0, now=NOW) <= 31


def test_backoff_grows_with_jitter():
    for attempt in range(5):
        delay = tasks.retry_delay(ConnectionError(), attempt)
        assert 2**attempt <= delay <= 2 ** (attempt + 1)


@pytest.mark.parametrize(
    "exc",
    [
        GithubException(404, {}, {}),
        GithubException(403, {}, {"x-ratelimit-remaining": "42", "x-ratelimit-reset": "1"}),
        SlackApiError("x", MagicMock(status_code=200, headers={})),
        ValueError("bug"),
    ],
)
def test_permanent_errors_are_not_retried(exc):
    assert tasks.retry_delay(exc, 0) is None


def test_anthropic_rate_limit_is_retryable():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(429, headers={"retry-after": "7"}, request=request)
    exc = anthropic.RateLimitError("slow down", response=response, body=None)
    assert 7 <= tasks.retry_delay(exc, 0) <= 8


def test_task_retries_then_gives_up(eager_celery, monkeypatch):
    run = MagicMock(side_effect=ConnectionError("down"))
    monkeypatch.setattr(tasks, "run_triage", run)
    result = tasks.triage_incident.delay("abc", {})
    assert isinstance(result.result, ConnectionError)
    assert run.call_count == tasks.MAX_RETRIES + 1


def test_task_recovers_after_transient_error(eager_celery, monkeypatch):
    run = MagicMock(side_effect=[ConnectionError("blip"), None])
    monkeypatch.setattr(tasks, "run_hotfix", run)
    tasks.create_hotfix_pr.delay("abc")
    assert run.call_count == 2
