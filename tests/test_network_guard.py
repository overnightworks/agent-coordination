"""Behavioral tests for `tests/network_guard.py` (issues #530 and #534).

Git is the boundary the guard constrains, so each proof runs real git under
`tmp_path`. The refused remotes point at a closed
loopback port, so a push the guard failed to stop still ends on this machine.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from cli_fixtures import _real_git, _real_repository_with_bare_remote, sealed_git_environment
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
_SCRATCH_GH_LOGIN_MODULE = """
import subprocess

import pytest


@pytest.mark.parametrize("host_arguments", [(), ("--hostname", "github.com")])
def test_gh_finds_no_login(host_arguments):
    token = subprocess.run(
        ["gh", "auth", "token", *host_arguments], capture_output=True, text=True, check=False
    )
    assert token.returncode != 0, "gh found a login"
"""

_WITH_AND_WITHOUT_THE_PLUGIN = pytest.mark.parametrize(
    ("plugin_arguments", "guarded"),
    [
        pytest.param([], True, id="plugin-loaded"),
        pytest.param(["-p", "no:network_guard"], False, id="plugin-blocked"),
    ],
)


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


def _push_a_commit(
    tmp_path: Path, remote_url: Callable[[Path], str]
) -> subprocess.CompletedProcess[str]:
    """A first commit of the shared real-git repository pushed to
    `remote_url` of its bare remote, the push allowed to fail."""
    repository, bare_remote = _real_repository_with_bare_remote(tmp_path)
    _real_git(repository, "commit", "-q", "--allow-empty", "-m", "first")
    return _real_git(repository, "push", "-q", remote_url(bare_remote), "main", check=False)


@pytest.mark.parametrize(
    "remote_url",
    [
        pytest.param(str, id="local-path"),
        pytest.param(Path.as_uri, id="file-url"),
    ],
)
@pytest.mark.usefixtures("hostile_operator_git_template")
def test_the_shared_helper_pushes_to_a_local_remote_despite_a_hostile_template(
    tmp_path: Path, remote_url: Callable[[Path], str]
) -> None:
    """The guard lets local remotes through, and the shared real-git helper
    seeds nothing from the operator's template, whose rewrite would send the
    `file://` push to a refused https port (#534 line 2)."""
    push = _push_a_commit(tmp_path, remote_url)

    assert push.returncode == 0, push.stderr


@pytest.mark.parametrize(
    ("transport", "url"),
    [
        pytest.param("https", "https://127.0.0.1:9/x.git", id="https"),
        pytest.param("ssh", "ssh://git@127.0.0.1:9/x.git", id="ssh"),
    ],
)
def test_push_to_a_non_local_remote_is_refused(tmp_path: Path, transport: str, url: str) -> None:
    push = _push_a_commit(tmp_path, lambda _bare_remote: url)

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

    push = _push_a_commit(tmp_path, lambda _bare_remote: "https://127.0.0.1:9/x.git")

    assert "127.0.0.1 port 9" in push.stderr


@_WITH_AND_WITHOUT_THE_PLUGIN
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
    _real_git(operator_repository, "init", "-q")
    _real_git(operator_repository, "config", "protocol.https.allow", "never")
    # Its own repository ends discovery at the scratch directory, whatever encloses it.
    _real_git(scratch, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(operator_repository / ".git"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.https.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "never")
    (scratch / "conftest.py").write_text(_SCRATCH_CONFTEST)
    unguarded_environment = {
        name: value
        for name, value in sealed_git_environment().items()
        if name != GIT_ALLOW_PROTOCOL_ENV
    }

    run = _run_scratch_pytest(
        scratch, _SCRATCH_TEST_MODULE, plugin_arguments, unguarded_environment
    )

    assert (run.returncode == 0) is guarded, run.stdout + run.stderr


@_WITH_AND_WITHOUT_THE_PLUGIN
def test_a_run_started_with_a_hostile_gh_login_finds_no_login(
    tmp_path: Path, plugin_arguments: list[str], guarded: bool
) -> None:
    """A pytest run started with the operator's gh configuration and every
    gh token variable set asks gh for a login from its module: it finds none
    only while the plugin displaces them before the run begins (#534 line
    1). The blocked run proves the seeded login is one gh would use."""
    hostile_config = tmp_path / "operator-gh-config"
    hostile_config.mkdir()
    (hostile_config / "hosts.yml").write_text(
        "".join(
            f'"{host}":\n    oauth_token: operator-config-token\n    user: operator\n'
            for host in ("github.com", UNREACHABLE_GH_HOST)
        )
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    hostile_environment = sealed_git_environment() | {
        "GH_CONFIG_DIR": str(hostile_config),
        "GH_HOST": "github.com",
        "GH_TOKEN": "operator-token",
        "GITHUB_TOKEN": "operator-token",
        "GH_ENTERPRISE_TOKEN": "operator-token",
        "GITHUB_ENTERPRISE_TOKEN": "operator-token",
    }

    run = _run_scratch_pytest(
        scratch, _SCRATCH_GH_LOGIN_MODULE, plugin_arguments, hostile_environment
    )

    assert (run.returncode == 0) is guarded, run.stdout + run.stderr


def _run_scratch_pytest(
    scratch: Path, probe_source: str, plugin_arguments: list[str], environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """A pytest run of `probe_source` in `scratch` under the project
    configuration alone, started with exactly `environment`."""
    probe = scratch / "test_scratch_probe.py"
    probe.write_text(probe_source)
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
    return subprocess.run(
        command, cwd=scratch, env=environment, capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize(
    ("gh_arguments", "refusal"),
    [
        pytest.param(
            ("auth", "token"), f"no oauth token found for {UNREACHABLE_GH_HOST}", id="no-login"
        ),
        pytest.param(
            ("auth", "token", "--hostname", "github.com"),
            "no oauth token found for github.com",
            id="no-keyring-login",
        ),
        pytest.param(("api", "user"), f"https://{UNREACHABLE_GH_HOST}/api/", id="no-network"),
        pytest.param(
            ("api", "--hostname", "example.invalid", "user"),
            f"proxyconnect tcp: dial tcp {UNREACHABLE_GH_HOST}",
            id="no-network-for-a-named-host",
        ),
    ],
)
def test_gh_under_the_plugin_has_no_login_and_stays_on_the_machine(
    tmp_path: Path, gh_arguments: tuple[str, ...], refusal: str
) -> None:
    """The real gh binary finds no login for its default host nor, in the
    operator's keyring, for github.com, and a request for its default host
    or a host it is told to name ends at the closed loopback port (#534
    line 1)."""
    gh = subprocess.run(
        ["gh", *gh_arguments], cwd=tmp_path, capture_output=True, text=True, check=False
    )

    assert gh.returncode != 0
    assert refusal in gh.stderr, gh.stderr
