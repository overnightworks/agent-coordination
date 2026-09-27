"""Shared test isolation.

Only isolation fixtures live here -- the autouse ones and the isolation more
than one module asks for by name; everything else stays local to its test
module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_coordination import checkout, protect


@pytest.fixture(autouse=True)
def _isolate_protect_unguarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test inherits the session's own `ACO_PROTECT_UNGUARDED`: a
    developer's scratchpad exemption would otherwise let a protect test
    allow what it means to prove denied."""
    monkeypatch.delenv(protect.PROTECT_UNGUARDED_ENV, raising=False)


_SHOW_TOPLEVEL_ARGUMENTS = ["rev-parse", "--show-toplevel"]


@pytest.fixture(autouse=True)
def _isolate_git_toplevel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No test may resolve this repository's own board configuration.

    `board.CONFIG_PATH` is read relative to `rev-parse --show-toplevel`, so a
    test that never overrides it would otherwise load and be governed by this
    checkout's own `.agent-claim/board.toml`. A test that needs a specific
    toplevel keeps its own `monkeypatch.setattr(checkout, "_git_output", ...)`,
    which takes precedence over this default.
    """
    real_git_output = checkout._git_output

    def fake_git_output(arguments: list[str], *, directory: Path | None = None) -> str:
        if arguments == _SHOW_TOPLEVEL_ARGUMENTS and directory is None:
            return str(tmp_path)
        return real_git_output(arguments, directory=directory)

    monkeypatch.setattr(checkout, "_git_output", fake_git_output)


@pytest.fixture
def isolated_global_git_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Real git reads an empty global configuration, never the operator's
    own (#310 finding 184): an operator's `remote.hub.url` or
    `[remote "hub"]` line would otherwise answer whether a test's checkout
    configures its canonical remote. A test that needs a global line
    writes it into the returned file."""
    global_config = tmp_path / "global.gitconfig"
    global_config.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    return global_config
