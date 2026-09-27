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


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config) -> None:
    # The first hook a `-p` plugin receives: the initial conftests, collection,
    # every fixture scope and the xdist workers started from this process all
    # see the guard, whereas `pytest_configure` runs after those conftests.
    _guard.setenv(GIT_ALLOW_PROTOCOL_ENV, LOCAL_PROTOCOLS_ONLY)


def pytest_unconfigure(config: pytest.Config) -> None:
    _guard.undo()
