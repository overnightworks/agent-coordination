"""Behavioral tests for the CI rewrite gate in `scripts/test_inventory.py`.

Each case drives `rewritten_modules` against a real, freshly initialized git
repository under `tmp_path`. Git itself is the boundary the gate queries, so
faking its output would test the wrong layer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from script_fixtures import load_script

rewritten_modules = load_script("test_inventory").rewritten_modules

_MODULE = "tests/test_example.py"
_TEN_LINES = "".join(f"assert {number} == {number}\n" for number in range(10))
_TWO_LINES = "assert 0 == 0\nassert 1 == 1\n"


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo_root: Path, content: str | None) -> str:
    """Commit *content* as the example module (``None`` removes it); return the commit."""
    module = repo_root / _MODULE
    if content is None:
        module.unlink(missing_ok=True)
    else:
        module.parent.mkdir(exist_ok=True)
        module.write_text(content, encoding="utf-8")
    _git(repo_root, "add", "--all")
    _git(repo_root, "commit", "--quiet", "--allow-empty", "--message", "step")
    return _git(repo_root, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    ("base_content", "head_content", "expected"),
    [
        (_TEN_LINES, None, []),
        (None, _TEN_LINES, []),
        (_TEN_LINES, _TWO_LINES, [_MODULE]),
    ],
    ids=[
        "deleted-module-is-not-a-rewrite",
        "new-module-is-not-a-rewrite",
        "shrunk-past-half-is-a-rewrite",
    ],
)
def test_rewritten_modules_names_only_a_module_shrunk_past_the_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base_content: str | None,
    head_content: str | None,
    expected: list[str],
) -> None:
    _git(tmp_path, "init", "--quiet")
    base = _commit(tmp_path, base_content)
    _commit(tmp_path, head_content)
    monkeypatch.chdir(tmp_path)

    assert rewritten_modules(base) == expected
