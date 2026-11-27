import difflib
import hashlib

from github import Auth, Github, GithubException, UnknownObjectException

from app.core.config import get_settings
from app.core.logging import pretty
from app.models.schemas import Commit, FileDiff, SourceFile, StackFrame


class StaleFileError(Exception):
    """The file changed since triage; committing the full-file patch would revert that change."""


def truncate_diff(patch: str, limit: int) -> str:
    if len(patch) <= limit:
        return patch
    return patch[:limit] + f"\n... [diff truncated, {len(patch) - limit} chars omitted]"


def _segments(path: str) -> list[str]:
    parts = path.replace("\\", "/").split("/")
    return [p for p in parts if p not in ("", ".") and not p.endswith(":")]


def match_frames_to_files(frames: list[StackFrame], paths: list[str]) -> list[str]:
    """Changed files referenced by the stack trace, in frame order (suffix match on segments).

    The shorter path must be a whole-segment suffix of the longer one; when several
    files match a frame, only the most specific (longest) matches are kept.
    """
    matched: list[str] = []
    for frame in frames:
        frame_parts = _segments(frame.filename)
        if not frame_parts or {"site-packages", "dist-packages"} & set(frame_parts):
            continue
        scores = {}
        for path in paths:
            parts = _segments(path)
            n = min(len(parts), len(frame_parts))
            if n and parts[-n:] == frame_parts[-n:]:
                scores[path] = n
        best = max(scores.values(), default=0)
        matched += [p for p, n in scores.items() if n == best and p not in matched]
    return matched


class GitHubService:
    def __init__(self, repo=None):
        if repo is None:
            s = get_settings()
            # retry=None: Celery owns retries, so a rate limit never blocks a worker asleep.
            client = Github(auth=Auth.Token(s.github_token), retry=None, timeout=15)
            repo = client.get_repo(s.github_repo)
        self.repo = repo
        self.base = get_settings().github_base_branch

    def recent_commits(self, limit: int = 5) -> list[Commit]:
        max_chars = get_settings().max_diff_chars
        commits = []
        for c in self.repo.get_commits(sha=self.base)[:limit]:
            commits.append(
                Commit(
                    sha=c.sha,
                    author=c.author.login if c.author else c.commit.author.name,
                    message=c.commit.message,
                    files=[
                        FileDiff(
                            path=f.filename,
                            status=f.status,
                            patch=truncate_diff(f.patch or "", max_chars),
                        )
                        for f in c.files
                    ],
                )
            )
        return commits

    def get_file(self, path: str) -> SourceFile | None:
        try:
            f = self.repo.get_contents(path, ref=self.base)
            if isinstance(f, list) or f.encoding != "base64":  # directory or >1MB file
                return None
            return SourceFile(content=f.decoded_content.decode("utf-8"), blob_sha=f.sha)
        except (UnknownObjectException, UnicodeDecodeError):
            return None

    def create_hotfix_pr(
        self, branch: str, path: str, content: str, base_blob_sha: str, title: str, body: str
    ) -> str:
        """Safe to re-run after a partial failure: reuses the branch, commit and PR."""
        head = f"{self.repo.owner.login}:{branch}"
        for pr in self.repo.get_pulls(state="all", head=head):
            return pr.html_url
        if self.repo.get_contents(path, ref=self.base).sha != base_blob_sha:
            raise StaleFileError(f"{path} changed on {self.base} since triage")
        try:
            base_sha = self.repo.get_branch(self.base).commit.sha
            self.repo.create_git_ref(f"refs/heads/{branch}", base_sha)
        except GithubException as exc:
            if exc.status != 422:  # 422 = ref already exists
                raise
        current = self.repo.get_contents(path, ref=branch)
        if current.decoded_content != content.encode("utf-8"):
            if current.sha != base_blob_sha:
                raise StaleFileError(f"{path} on {branch} has unexpected changes")
            self.repo.update_file(path, title, content, current.sha, branch=branch)
        return self.repo.create_pull(title=title, body=body, head=branch, base=self.base).html_url


# ---------------------------------------------------------------------------
# DRY_RUN fake: canned repository, no network.
# ---------------------------------------------------------------------------

DEMO_FILES = {
    "app/payments/processor.py": """\
from app.payments.customers import find_customer
from app.payments.gateway import gateway


def charge(order):
    customer = find_customer(order.customer_id)
    receipt_email = customer.email
    return gateway.charge(order.total_cents, receipt_email)
""",
}

DEMO_FIXES = {
    "app/payments/processor.py": DEMO_FILES["app/payments/processor.py"].replace(
        "    receipt_email = customer.email\n",
        "    receipt_email = customer.email if customer else order.guest_email\n",
    ),
}

DEMO_COMMITS = [
    Commit(
        sha="9f1c2d7a4b3e8f60a1d5c9e2b7f4a8d3c6e0b1f2",
        author="priya-n",
        message="docs: describe retry policy",
        files=[FileDiff(path="README.md", patch="@@ -1 +1,2 @@\n # Payments\n+Retries: 3\n")],
    ),
    Commit(
        sha="4a7be19c0d2f5e8b3a6c1f9d4e7b0a2c5f8d1e3a",
        author="rahul-k",
        message="refactor: extract customer lookup into find_customer()",
        files=[
            FileDiff(
                path="app/payments/processor.py",
                patch="@@ -4,6 +4,5 @@\n def charge(order):\n"
                "-    customer = Customer.query.get_or_404(order.customer_id)\n"
                "+    customer = find_customer(order.customer_id)\n"
                "     receipt_email = customer.email\n",
            ),
            FileDiff(
                path="app/payments/customers.py",
                status="added",
                patch="@@ -0,0 +1,2 @@\n+def find_customer(customer_id):\n"
                "+    return Customer.query.get(customer_id)\n",
            ),
        ],
    ),
    Commit(
        sha="c3d8a51f7e2b9c04d6a1e8f3b5c7d9a0e2f4b6c8",
        author="meera-s",
        message="chore: bump gateway client",
        files=[
            FileDiff(path="requirements.txt", patch="@@ -1 +1 @@\n-gateway==2.0\n+gateway==2.1\n")
        ],
    ),
]


class FakeGitHub:
    def __init__(self, commits=None, files=None):
        self.commits = DEMO_COMMITS if commits is None else commits
        self.files = DEMO_FILES if files is None else files

    def recent_commits(self, limit: int = 5) -> list[Commit]:
        return self.commits[:limit]

    def get_file(self, path: str) -> SourceFile | None:
        if path not in self.files:
            return None
        content = self.files[path]
        return SourceFile(content=content, blob_sha=hashlib.sha1(content.encode()).hexdigest())

    def create_hotfix_pr(
        self, branch: str, path: str, content: str, base_blob_sha: str, title: str, body: str
    ) -> str:
        diff = difflib.unified_diff(
            self.files.get(path, "").splitlines(keepends=True),
            content.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
        pretty(
            "DRY_RUN pull request",
            f"branch: {branch}\ntitle: {title}\n\n{body}\n\n{''.join(diff)}",
        )
        return f"https://github.example/dry-run/repo/pull/{branch.rsplit('-', 1)[-1]}"


def get_github() -> GitHubService | FakeGitHub:
    return FakeGitHub() if get_settings().dry_run else GitHubService()
