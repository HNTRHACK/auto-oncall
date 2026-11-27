"""Run 10 synthetic crashes with known culprit commits and report accuracy.

The GitHub data is always synthetic. DRY_RUN=true scores the built-in heuristic (a pipeline
check, not a model benchmark); DRY_RUN=false scores Claude and costs API credits.
"""

import hashlib
from datetime import datetime, timezone

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.models.schemas import Commit, CrashPayload, FileDiff
from app.services import rca_agent
from app.services.github_service import FakeGitHub
from app.services.llm_client import get_llm
from app.services.runbook_store import get_runbook_store


def _commit(name: str, message: str, path: str, removed: str, added: str) -> Commit:
    patch = "@@ -1,3 +1,3 @@\n" + (f"-{removed}\n" if removed else "") + f"+{added}\n"
    return Commit(
        sha=hashlib.sha1(f"{name}:{message}".encode()).hexdigest(),
        author=f"dev-{name}",
        message=message,
        files=[FileDiff(path=path, patch=patch)],
    )


def _scenario(name, error_type, error, trace, commits, culprit_index):
    """commits: (message, path, removed_line, added_line), newest first."""
    built = [_commit(name, *c) for c in commits]
    files: dict[str, str] = {}
    for _message, path, _removed, added in commits:
        files[path] = files.get(path, "") + added + "\n"
    payload = CrashPayload(
        service_name=name,
        environment="production",
        error_type=error_type,
        error_message=error,
        stack_trace=[{"filename": f"/srv/{p}", "lineno": 1, "function": fn} for p, fn in trace],
        timestamp=datetime.now(timezone.utc),
    )
    return payload, built, files, built[culprit_index].sha


SCENARIOS = [
    _scenario(
        "payments",
        "AttributeError",
        "'NoneType' object has no attribute 'email'",
        [("app/pay/processor.py", "charge")],
        [
            ("docs: readme", "README.md", "", "Retries: 3"),
            (
                "refactor: customer lookup",
                "app/pay/processor.py",
                "customer = get_or_404(cid)",
                "customer = Customer.query.get(cid)",
            ),
        ],
        1,
    ),
    _scenario(
        "orders",
        "TimeoutError",
        "QueuePool limit of size 5 reached",
        [("app/db/session.py", "get_session")],
        [
            ("perf: shrink pool", "app/db/session.py", "POOL_SIZE = 20", "POOL_SIZE = 5"),
            ("test: add fixtures", "tests/fixtures.py", "", "ORDER = {}"),
        ],
        0,
    ),
    _scenario(
        "catalog",
        "TimeoutError",
        "statement timeout after 30000 ms",
        [("app/catalog/serializers.py", "to_dict")],
        [
            ("chore: lint", "setup.cfg", "", "max-line-length = 100"),
            (
                "feat: show seller name",
                "app/catalog/serializers.py",
                "name = row.seller_name",
                "name = Seller.query.get(row.seller_id).name",
            ),
        ],
        1,
    ),
    _scenario(
        "notifier",
        "RateLimitError",
        "429 Too Many Requests",
        [("app/notify/sms.py", "send")],
        [
            (
                "refactor: drop sms cache",
                "app/notify/sms.py",
                "return cache.get(n) or lookup(n)",
                "return lookup(n)",
            ),
            ("docs: runbook link", "docs/oncall.md", "", "See wiki"),
        ],
        0,
    ),
    _scenario(
        "search",
        "KeyError",
        "'index_name'",
        [("app/search/client.py", "query")],
        [
            (
                "feat: configurable index",
                "app/search/client.py",
                "name = 'products'",
                "name = cfg['index_name']",
            )
        ],
        0,
    ),
    _scenario(
        "auth",
        "TypeError",
        "can't compare offset-naive and offset-aware datetimes",
        [("app/auth/tokens.py", "is_expired")],
        [
            ("chore: bump deps", "requirements.txt", "pyjwt==2.7", "pyjwt==2.8"),
            (
                "fix: use utcnow",
                "app/auth/tokens.py",
                "now = datetime.now(timezone.utc)",
                "now = datetime.utcnow()",
            ),
        ],
        1,
    ),
    _scenario(
        "billing",
        "ZeroDivisionError",
        "division by zero",
        [("app/billing/proration.py", "prorate"), ("app/billing/api.py", "post")],
        [
            ("feat: api logging", "app/billing/api.py", "", "log.info('prorate')"),
            (
                "refactor: period length",
                "app/billing/proration.py",
                "days = period.days or 1",
                "days = period.days",
            ),
        ],
        1,
    ),
    _scenario(
        "uploads",
        "FileNotFoundError",
        "No such file or directory: '/data/uploads'",
        [("app/uploads/storage.py", "save")],
        [
            (
                "feat: new upload root",
                "app/uploads/storage.py",
                "ROOT = '/var/uploads'",
                "ROOT = '/data/uploads'",
            )
        ],
        0,
    ),
    # Hard: a later cosmetic commit touches the same file as the real culprit.
    _scenario(
        "inventory",
        "IndexError",
        "list index out of range",
        [("app/inventory/stock.py", "first_batch")],
        [
            ("style: add docstring", "app/inventory/stock.py", "", '"""Stock helpers."""'),
            (
                "refactor: remove empty guard",
                "app/inventory/stock.py",
                "return batches[0] if batches else None",
                "return batches[0]",
            ),
        ],
        1,
    ),
    # Hard: a config deploy is the culprit but never appears in the stack trace.
    _scenario(
        "gateway",
        "ValueError",
        "invalid literal for int() with base 10: '30s'",
        [("app/gateway/settings.py", "load")],
        [
            ("feat: log settings", "app/gateway/settings.py", "", "log.debug(raw)"),
            ("config: new timeout", "config/prod.yaml", "timeout: 30", "timeout: 30s"),
        ],
        1,
    ),
]


def evaluate(llm, runbook_store, verbose: bool = False) -> int:
    correct = 0
    for payload, commits, files, culprit in SCENARIOS:
        github = FakeGitHub(commits, files)
        selected = rca_agent.select_files(
            payload, commits, github.get_file, get_settings().max_file_chars
        )
        runbooks = runbook_store.search(f"{payload.error_type} {payload.error_message}")
        report = rca_agent.analyze(llm, payload, commits, selected, runbooks)
        hit = report.suspected_commit_sha == culprit
        correct += hit
        if verbose:
            print(
                f"{'PASS' if hit else 'FAIL'}  {payload.service_name:<10} "
                f"expected {culprit[:8]}  got {report.suspected_commit_sha[:8]}  "
                f"confidence {report.confidence_score:.2f}"
            )
    return correct


if __name__ == "__main__":
    configure_logging("WARNING")
    dry_run = get_settings().dry_run
    print(f"LLM: {'FakeLLM heuristic (DRY_RUN)' if dry_run else get_settings().anthropic_model}")
    correct = evaluate(get_llm(), get_runbook_store(), verbose=True)
    print(f"Accuracy: {correct}/{len(SCENARIOS)} ({correct / len(SCENARIOS):.0%})")
