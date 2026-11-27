"""Cheap static checks on a model-written file before a human is offered the PR.

Not a substitute for CI. It only stops patches that cannot possibly work: a Python file
that no longer parses, invalid JSON/TOML, or a patch that deletes most of the file.
Nothing here imports or executes the patched code.
"""

import ast
import json
import tomllib

# A "fix" that keeps less than this fraction of the original lines is a rewrite, not a hotfix.
MIN_KEPT_LINE_RATIO = 0.5


def check_patch(path: str, original: str, patched: str) -> str | None:
    """Return why the patch must not be offered, or None if it passes."""
    lower = path.lower()
    try:
        if lower.endswith((".py", ".pyi")):
            ast.parse(patched, filename=path)
        elif lower.endswith(".json"):
            json.loads(patched)
        elif lower.endswith(".toml"):
            tomllib.loads(patched)
    except SyntaxError as exc:
        return f"patched file does not parse (SyntaxError at line {exc.lineno}: {exc.msg})"
    except (ValueError, tomllib.TOMLDecodeError) as exc:
        return f"patched file is not valid {lower.rsplit('.', 1)[-1].upper()}: {exc}"

    original_lines = [line for line in original.splitlines() if line.strip()]
    if len(original_lines) >= 10:
        patched_lines = set(line for line in patched.splitlines() if line.strip())
        kept = sum(1 for line in original_lines if line in patched_lines)
        if kept / len(original_lines) < MIN_KEPT_LINE_RATIO:
            return (
                f"patch rewrites too much of the file ({kept}/{len(original_lines)} "
                "original lines kept); fix manually"
            )
    return None
