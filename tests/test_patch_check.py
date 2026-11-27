import json
from unittest.mock import MagicMock

import pytest

from app.services import incident_store
from app.services.github_service import DEMO_FIXES
from app.services.patch_check import check_patch
from app.worker import tasks

ORIGINAL = "\n".join(f"line_{i} = {i}" for i in range(20)) + "\n"


def test_valid_python_patch_passes():
    assert check_patch("a.py", ORIGINAL, ORIGINAL + "extra = 1\n") is None


def test_broken_python_is_rejected_with_line_number():
    problem = check_patch("a.py", ORIGINAL, ORIGINAL + "def broken(:\n")
    assert problem.startswith("patched file does not parse") and "line 21" in problem


def test_patch_code_is_never_executed():
    # A patch that would have side effects if run must only be parsed.
    assert check_patch("a.py", "x = 1\n", "import os\nos._exit(3)\n") is None


@pytest.mark.parametrize(
    ("path", "bad"), [("cfg.json", "{not json"), ("pyproject.toml", "name = = 1")]
)
def test_invalid_json_and_toml_rejected(path, bad):
    assert "not valid" in check_patch(path, "", bad)


def test_other_file_types_are_not_parsed():
    assert check_patch("app.js", "", "function (") is None


def test_rewrite_of_most_of_the_file_is_rejected():
    rewritten = "\n".join(f"other_{i} = {i}" for i in range(20))
    assert "rewrites too much" in check_patch("a.py", ORIGINAL, rewritten)


def test_small_files_can_change_freely():
    assert check_patch("a.py", "x = 1\n", "y = 2\n") is None


def test_demo_fix_passes():
    for path, content in DEMO_FIXES.items():
        assert check_patch(path, content, content) is None


def test_broken_patch_hides_button_and_explains(payload, monkeypatch):
    slack = MagicMock()
    slack.post_alert.return_value = ("C1", "1.0")
    monkeypatch.setattr(tasks, "get_slack", lambda: slack)
    real = tasks.get_llm()

    class BrokenPatchLLM:
        def analyze(self, system, prompt, **ctx):
            result = real.analyze(system, prompt, **ctx)
            return {**result, "file_patch": result["file_patch"] + "\ndef oops(:\n"}

    monkeypatch.setattr(tasks, "get_llm", lambda: BrokenPatchLLM())
    tasks.run_triage("bad", payload)
    blocks = slack.post_alert.call_args.args[1]
    assert not any(b["type"] == "actions" for b in blocks)
    assert "does not parse" in json.dumps(blocks)
    record = incident_store.get("bad")
    assert record["can_pr"] == "0" and "SyntaxError" in record["patch_problem"]
