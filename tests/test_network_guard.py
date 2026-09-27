"""Behavioral tests for `tests/network_guard.py` (issues #530 and #534).

Git is the boundary the guard constrains, so each proof runs real git under
`tmp_path`. The refused remotes point at a closed
loopback port, so a push the guard failed to stop still ends on this machine.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from functools import cache
from pathlib import Path

import pytest
from network_guard import GIT_ALLOW_PROTOCOL_ENV, UNREACHABLE_GH_HOST

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


@cache
def _repository_selecting_git_variables() -> frozenset[str]:
    """Git's own list of the variables that select a repository (`GIT_DIR`,
    `GIT_WORK_TREE`, ...), whose local configuration could rewrite a URL or
    set a proxy."""
    local_variables = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True, check=True
    )
    return frozenset(local_variables.stdout.split())


def _machine_local_git_environment() -> dict[str, str]:
    """The inherited environment without any route the operator configured:
    no proxy, no operator repository or git configuration beyond the probed
    repository's own, and an ssh that reads no config file, so a probe the
    guard failed to stop still dials the loopback address it names."""
    operator_git_variables = _repository_selecting_git_variables() | set(_OPERATOR_GIT_ROUTES)
    inherited = {
        name: value
        for name, value in os.environ.items()
        if not name.lower().endswith("_proxy")
        and not name.startswith("GIT_CONFIG")
        and name not in operator_git_variables
    }
    return inherited | {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_SSH_COMMAND": f"ssh -F {os.devnull} -o BatchMode=yes",
        "LC_ALL": "C",
    }


@pytest.fixture
def hostile_operator_git_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator git template whose config would reroute a `file://` remote
    to a closed https port and refuse https outright, so any repository
    these proofs initialize from it contaminates both the probe and its
    control."""
    template = tmp_path / "operator-template"
    template.mkdir()
    (template / "config").write_text(
        '[url "https://127.0.0.1:9/"]\n\tinsteadOf = file://\n[protocol "https"]\n\tallow = never\n'
    )
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))


def _machine_local_git(
    directory: Path, *arguments: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=directory,
        env=_machine_local_git_environment(),
        capture_output=True,
        text=True,
        check=check,
    )


def _init_machine_local_repository(directory: Path, *arguments: str) -> None:
    """`git init` from a known-empty template, so no operator template
    (`GIT_TEMPLATE_DIR`, `init.templateDir`) seeds a URL rewrite or protocol
    rule into the repository a proof then runs git in."""
    with tempfile.TemporaryDirectory() as empty_template:
        _machine_local_git(directory, "init", "-q", f"--template={empty_template}", *arguments)


def _push_to(tmp_path: Path, remote_url: Callable[[Path], str]) -> subprocess.CompletedProcess[str]:
    bare_remote = tmp_path / "remote.git"
    repository = tmp_path / "repository"
    bare_remote.mkdir()
    repository.mkdir()
    _init_machine_local_repository(bare_remote, "--bare", "-b", "main")
    _init_machine_local_repository(repository, "-b", "main")
    _machine_local_git(
        repository,
        *("-c", "user.name=Test", "-c", "user.email=test@example.com"),
        *("commit", "-q", "--allow-empty", "-m", "first"),
    )
    _machine_local_git(repository, "remote", "add", "origin", remote_url(bare_remote))
    return _machine_local_git(repository, "push", "-q", "origin", "main", check=False)


@pytest.mark.parametrize(
    "remote_url",
    [
        pytest.param(str, id="local-path"),
        pytest.param(Path.as_uri, id="file-url"),
    ],
)
@pytest.mark.usefixtures("hostile_operator_git_template")
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
@pytest.mark.usefixtures("hostile_operator_git_template")
def test_a_module_outside_tests_is_guarded_by_the_project_plugin_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plugin_arguments: list[str],
    guarded: bool,
) -> None:
    """A scratch directory whose initial conftest, module import and test
    each ask git for an https remote passes only when the plugin the project
    configuration loads guards before the initial conftests (#530 lines 1
    and 2). The operator's git configuration refuses https here too: at
    runtime, in the repository `GIT_DIR` selects, and in that same
    repository enclosing the scratch directory under a path git cannot name
    as a discovery ceiling; the blocked run proves the scratch pytest never
    inherits any of them."""
    operator_repository = tmp_path / "operator:temp"
    scratch = operator_repository / "scratch"
    scratch.mkdir(parents=True)
    _init_machine_local_repository(operator_repository)
    _machine_local_git(operator_repository, "config", "protocol.https.allow", "never")
    # Its own repository ends discovery at the scratch directory, whatever encloses it.
    _init_machine_local_repository(scratch)
    monkeypatch.setenv("GIT_DIR", str(operator_repository / ".git"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.https.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "never")
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


@pytest.mark.parametrize(
    ("gh_arguments", "refusal"),
    [
        pytest.param(
            ("auth", "token"), f"no oauth token found for {UNREACHABLE_GH_HOST}", id="no-login"
        ),
        pytest.param(("api", "user"), f"https://{UNREACHABLE_GH_HOST}/api/", id="no-network"),
    ],
)
def test_gh_under_the_plugin_has_no_login_and_stays_on_the_machine(
    tmp_path: Path, gh_arguments: tuple[str, ...], refusal: str
) -> None:
    """The real gh binary finds no login for its default host, and the one
    request it still sends ends at the closed loopback port (#534 line 1)."""
    gh = subprocess.run(
        ["gh", *gh_arguments], cwd=tmp_path, capture_output=True, text=True, check=False
    )

    assert gh.returncode != 0
    assert refusal in gh.stderr, gh.stderr
