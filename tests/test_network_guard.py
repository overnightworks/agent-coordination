"""Behavioral tests for `tests/network_guard.py` (issue #530).

Git is the boundary the guard constrains, so each proof pushes with real git
from a repository under `tmp_path`. The refused remotes name a reserved
`.invalid` host; git refuses their transport before it connects anywhere.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from cli_fixtures import _real_git, _real_repository_with_bare_remote
from network_guard import GIT_ALLOW_PROTOCOL_ENV

_PROJECT_CONFIGURATION = Path(__file__).parent.parent / "pyproject.toml"


def _push_to(tmp_path: Path, remote_url: Callable[[Path], str]) -> subprocess.CompletedProcess[str]:
    repository, bare_remote = _real_repository_with_bare_remote(tmp_path)
    _real_git(repository, "commit", "-q", "--allow-empty", "-m", "first")
    _real_git(repository, "remote", "set-url", "origin", remote_url(bare_remote))
    return subprocess.run(
        ["git", "push", "-q", "origin", "main"],
        cwd=repository,
        env={**os.environ, "LC_ALL": "C"},
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
        pytest.param("https", "https://example.invalid/owner/repo.git", id="https"),
        pytest.param("ssh", "ssh://git@example.invalid/owner/repo.git", id="ssh"),
    ],
)
def test_push_to_a_non_local_remote_is_refused(tmp_path: Path, transport: str, url: str) -> None:
    push = _push_to(tmp_path, lambda _bare_remote: url)

    assert push.returncode != 0
    assert f"transport '{transport}' not allowed" in push.stderr


def test_a_module_outside_tests_run_with_the_project_configuration_is_guarded(
    tmp_path: Path,
) -> None:
    """The same https refusal, copied into a scratch directory and run by a
    pytest whose environment carries no guard of its own: only the plugin the
    project configuration loads can make it pass (#530 line 2)."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    probe = shutil.copy(__file__, scratch / "test_scratch_probe.py")
    unguarded_environment = {
        name: value for name, value in os.environ.items() if name != GIT_ALLOW_PROTOCOL_ENV
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
        "-k",
        "test_push_to_a_non_local_remote_is_refused and https",
        str(probe),
    ]

    run = subprocess.run(
        command, cwd=scratch, env=unguarded_environment, capture_output=True, text=True, check=False
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert "1 passed" in run.stdout
