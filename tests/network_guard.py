"""Keeps every test run off non-local git remotes (issue #530).

A test that escapes its store fakes once pushed a claim into the live claim
store on origin. Git refuses any transport outside `GIT_ALLOW_PROTOCOL`, so
allowing only `file` stops ssh and https whichever code path starts git,
while local paths and `file://` remotes keep working.

The project's pytest configuration loads this module as a plugin, so the
guard holds for a test module outside `tests/` run with `-c pyproject.toml`
too, not only where `tests/conftest.py` is found.
"""

from __future__ import annotations

import pytest

GIT_ALLOW_PROTOCOL_ENV = "GIT_ALLOW_PROTOCOL"
LOCAL_PROTOCOLS_ONLY = "file"


_guard = pytest.MonkeyPatch()


def pytest_configure(config: pytest.Config) -> None:
    # Configure runs before collection, so module imports and fixtures of every
    # scope already see the guard, as do xdist workers started from this process.
    _guard.setenv(GIT_ALLOW_PROTOCOL_ENV, LOCAL_PROTOCOLS_ONLY)


def pytest_unconfigure(config: pytest.Config) -> None:
    _guard.undo()
