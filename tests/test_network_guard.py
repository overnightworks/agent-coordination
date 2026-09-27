"""Behavioral tests for `tests/network_guard.py` (issue #530).

Git is the boundary the guard constrains, so each proof runs real git under
`tmp_path`. The refused remotes point at a closed
loopback port, so a push the guard failed to stop still ends on this machine.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from cli_fixtures import _real_git, _real_repository_with_bare_remote
from network_guard import GIT_ALLOW_PROTOCOL_ENV

_PROJECT_CONFIGURATION = Path(__file__).parent.parent / "pyproject.toml"

_ASSERT_GIT_REFUSES_HTTPS = """
import subprocess


def assert_git_refuses_https():
    ls_remote = subprocess.run(
        ["git", "ls-remote", "https://127.0.0.1:9/x.git"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "transport 'https' not allowed" in ls_remote.stderr, ls_remote.stderr
"""
_SCRATCH_CONFTEST = _ASSERT_GIT_REFUSES_HTTPS + "\n\nassert_git_refuses_https()\n"
_SCRATCH_TEST_MODULE = (
    _ASSERT_GIT_REFUSES_HTTPS
    + """

assert_git_refuses_https()


def test_git_refuses_https_while_the_test_runs():
    assert_git_refuses_https()
"""
)


_OPERATOR_GIT_ROUTES = ("GIT_PROXY_COMMAND", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT")


def _machine_local_git_environment() -> dict[str, str]:
    """The inherited environment without any route the operator configured:
    no proxy, no git configuration beyond the repository's own, and an ssh
    that reads no config file, so a probe the guard failed to stop still
    dials the loopback address it names."""
    inherited = {
        name: value
        for name, value in os.environ.items()
        if not name.lower().endswith("_proxy")
        and not name.startswith("GIT_CONFIG")
        and name not in _OPERATOR_GIT_ROUTES
    }
    return inherited | {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_SSH_COMMAND": f"ssh -F {os.devnull} -o BatchMode=yes",
        "LC_ALL": "C",
    }


def _push_to(tmp_path: Path, remote_url: Callable[[Path], str]) -> subprocess.CompletedProcess[str]:
    repository, bare_remote = _real_repository_with_bare_remote(tmp_path)
    _real_git(repository, "commit", "-q", "--allow-empty", "-m", "first")
    _real_git(repository, "remote", "set-url", "origin", remote_url(bare_remote))
    return subprocess.run(
        ["git", "push", "-q", "origin", "main"],
        cwd=repository,
        env=_machine_local_git_environment(),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "remote_url",
    [
        pytest.param(str, id="local-path"),
        pytest.param(Path.as_uri, id="file-url"),
    ],
)
def test_push_to_a_local_remote_still_works(
    tmp_path: Path, remote_url: Callable[[Path], str]
) -> None:
    push = _push_to(tmp_path, remote_url)

    assert push.returncode == 0, push.stderr


@pytest.mark.parametrize(
    ("transport", "url"),
    [
        pytest.param("https", "https://127.0.0.1:9/x.git", id="https"),
        pytest.param("ssh", "ssh://git@127.0.0.1:9/x.git", id="ssh"),
    ],
)
def test_push_to_a_non_local_remote_is_refused(tmp_path: Path, transport: str, url: str) -> None:
    push = _push_to(tmp_path, lambda _bare_remote: url)

    assert push.returncode != 0
    assert f"transport '{transport}' not allowed" in push.stderr


def test_a_refused_remote_probe_ignores_operator_proxies_when_the_guard_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both proxy routes point at another loopback port, so this run stays
    on the machine even while it proves the probe would bypass them."""
    operator_proxy = "http://127.0.0.1:1"
    monkeypatch.delenv(GIT_ALLOW_PROTOCOL_ENV)
    monkeypatch.setenv("HTTPS_PROXY", operator_proxy)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.proxy")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", operator_proxy)

    push = _push_to(tmp_path, lambda _bare_remote: "https://127.0.0.1:9/x.git")

    assert "127.0.0.1 port 9" in push.stderr


@pytest.mark.parametrize(
    ("plugin_arguments", "guarded"),
    [
        pytest.param([], True, id="plugin-loaded"),
        pytest.param(["-p", "no:network_guard"], False, id="plugin-blocked"),
    ],
)
def test_a_module_outside_tests_is_guarded_by_the_project_plugin_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plugin_arguments: list[str],
    guarded: bool,
) -> None:
    """A scratch directory whose initial conftest, module import and test
    each ask git for an https remote passes only when the plugin the project
    configuration loads guards before the initial conftests (#530 lines 1
    and 2). The operator's runtime git configuration refuses https here
    too; the blocked run proves the scratch pytest never inherits it."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.https.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "never")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "conftest.py").write_text(_SCRATCH_CONFTEST)
    probe = scratch / "test_scratch_probe.py"
    probe.write_text(_SCRATCH_TEST_MODULE)
    unguarded_environment = {
        name: value
        for name, value in _machine_local_git_environment().items()
        if name != GIT_ALLOW_PROTOCOL_ENV
    }
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-c",
        str(_PROJECT_CONFIGURATION),
        # Without it collection walks every directory between / and the probe.
        "--confcutdir",
        str(scratch),
        "-p",
        "no:cacheprovider",
        *plugin_arguments,
        str(probe),
    ]

    run = subprocess.run(
        command, cwd=scratch, env=unguarded_environment, capture_output=True, text=True, check=False
    )

    assert (run.returncode == 0) is guarded, run.stdout + run.stderr
