"""Keeps every test run off non-local git remotes and off the operator's
GitHub login (issues #530 and #534).

A test that escapes its store fakes once pushed a claim into the live claim
store on origin. Git refuses any transport outside `GIT_ALLOW_PROTOCOL`, so
allowing only `file` stops ssh and https whichever code path starts git,
while local paths and `file://` remotes keep working.

The gh CLI would otherwise write to the live board with the operator's
login. It reads an empty configuration directory, no token from the
environment, no keyring, whose session bus address is unusable, and a
default host on a closed loopback port. Without the keyring cut, a call
naming github.com explicitly would find the operator's token there, since
the empty configuration holds no entry for it. A nonexistent host name would not do: gh
sends an enterprise host its request unauthenticated, so the name would
still leave the machine as a DNS query.

The project's pytest configuration loads this module as a plugin, so the
guard holds for a test module outside `tests/` run with `-c pyproject.toml`
too, not only where `tests/conftest.py` is found.
"""

from __future__ import annotations

import tempfile

import pytest

GIT_ALLOW_PROTOCOL_ENV = "GIT_ALLOW_PROTOCOL"
LOCAL_PROTOCOLS_ONLY = "file"

UNREACHABLE_GH_HOST = "127.0.0.1:9"
# gh's keyring is the Secret Service on the session bus; a `disabled:` address opens no bus.
_UNUSABLE_SESSION_BUS_ADDRESS = "disabled:"
# gh reads the enterprise pair for every host but github.com, and GH_HOST names one.
_GH_TOKEN_ENVS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")


_guard = pytest.MonkeyPatch()
_empty_gh_config = tempfile.TemporaryDirectory(prefix="aco-test-gh-config-")


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config) -> None:
    # The first hook a `-p` plugin receives: the initial conftests, collection,
    # every fixture scope and the xdist workers started from this process all
    # see the guard, whereas `pytest_configure` runs after those conftests.
    _guard.setenv(GIT_ALLOW_PROTOCOL_ENV, LOCAL_PROTOCOLS_ONLY)
    _guard.setenv("GH_CONFIG_DIR", _empty_gh_config.name)
    _guard.setenv("GH_HOST", UNREACHABLE_GH_HOST)
    _guard.setenv("DBUS_SESSION_BUS_ADDRESS", _UNUSABLE_SESSION_BUS_ADDRESS)
    for token_env in _GH_TOKEN_ENVS:
        _guard.setenv(token_env, "")


def pytest_unconfigure(config: pytest.Config) -> None:
    _guard.undo()
    _empty_gh_config.cleanup()
