"""Behaviour of `agent_coordination.session.RunContext` (issue #457): one
command run's static repository facts, read lazily, held once read, and
answered per directory."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from cli_fixtures import stub_board_config_tracked

from agent_coordination import checkout, cli, forge, github, session
from agent_coordination.protocol import ClaimUnavailableError
from agent_coordination.session import RunContext


@pytest.fixture(autouse=True)
def _tracked_board_config(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_board_config_tracked(monkeypatch)


def _forge_never_built(_context: RunContext) -> forge.ForgeReader:
    pytest.fail("this fact must not build the forge")


def _context() -> RunContext:
    return RunContext(None, build_forge=_forge_never_built)


def _write_board_config(toplevel: Path, text: str) -> None:
    config_dir = toplevel / ".agent-claim"
    config_dir.mkdir(parents=True)
    (config_dir / "board.toml").write_text(text)


def test_remote_location_parses_the_canonical_remote_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        checkout, "remote_url", lambda remote, **_kwargs: "git@github.com:owner/repo.git"
    )

    location = _context().remote_location

    assert location == checkout.RemoteLocation(github.GITHUB_HOST, "owner/repo")


def test_refuse_unsupported_forge_host_allows_github() -> None:
    session.refuse_unsupported_forge_host(checkout.RemoteLocation(github.GITHUB_HOST, "owner/repo"))


def test_refuse_unsupported_forge_host_refuses_another_host() -> None:
    """No forge adapter but GitHub's exists yet (#230 slice 2) -- a forge
    command against any other host refuses by its own name (issue #245),
    never with GitHub's "does not name a GitHub repository" text."""
    location = checkout.RemoteLocation("gitlab.com", "o/r")

    with pytest.raises(ClaimUnavailableError, match=r"no forge adapter for host gitlab\.com"):
        session.refuse_unsupported_forge_host(location)


def test_refuse_canonical_remote_mismatch_allows_a_matching_target() -> None:
    session.refuse_canonical_remote_mismatch(
        forge.RepositoryId(github.GITHUB_HOST, ("owner",), "repo"),
        checkout.RemoteLocation(github.GITHUB_HOST, "owner/repo"),
    )


def test_refuse_canonical_remote_mismatch_names_both_repositories() -> None:
    mismatched = forge.RepositoryId(github.GITHUB_HOST, ("other",), "repo")
    canonical_remote = checkout.RemoteLocation(github.GITHUB_HOST, "owner/repo")

    with pytest.raises(
        ClaimUnavailableError,
        match="forge target other/repo does not match canonical remote owner/repo",
    ):
        session.refuse_canonical_remote_mismatch(mismatched, canonical_remote)


def test_repository_id_refuses_before_asking_gh_on_a_non_github_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GitHub repository gates on the canonical remote's own host
    before it ever calls `discover_repository` (issue #245): a `gh` call
    here would fail the test outright."""
    monkeypatch.setattr(
        checkout, "remote_url", lambda remote, **_kwargs: "file:///srv/git/repo.git"
    )

    def unused(*_args: object, **_kwargs: object) -> forge.RepositoryId:
        pytest.fail("a non-GitHub canonical remote must refuse before discover_repository runs")

    monkeypatch.setattr(github, "discover_repository", unused)
    context = _context()

    with pytest.raises(ClaimUnavailableError, match="no forge adapter for host file"):
        _ = context.repository_id


def test_repository_id_checks_erwartung_6_against_a_github_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        checkout, "remote_url", lambda remote, **_kwargs: "git@github.com:owner/repo.git"
    )
    monkeypatch.setattr(
        github,
        "discover_repository",
        lambda remote_url: forge.RepositoryId(github.GITHUB_HOST, ("owner",), "repo"),
    )

    target = _context().repository_id

    assert target == forge.RepositoryId(github.GITHUB_HOST, ("owner",), "repo")


def test_default_branch_under_state_ref_reads_origin_head_of_the_context_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #322 review finding 1, now the context's own fact (issue #457):
    a context for `start`'s created worktree reads `origin/HEAD` there,
    never from the calling process's own cwd."""
    worktree = tmp_path / "worktree"
    _write_board_config(worktree, 'storage = "state-ref"\n')
    monkeypatch.setattr(
        checkout, "_git_output", lambda _arguments, *, directory=None: str(directory)
    )
    read_from: list[Path | None] = []

    def default_branch_name(*, directory: Path | None = None) -> str:
        read_from.append(directory)
        return "main"

    monkeypatch.setattr(checkout, "default_branch_name", default_branch_name)

    branch = _context().for_directory(worktree).default_branch

    assert (branch, read_from) == ("main", [worktree])


def test_a_context_for_another_directory_reads_its_remotes_there(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #457 proof 7: `for_directory` answers the canonical remote and
    the repository it names from its own checkout, never from the calling
    process's cwd, so a child context never pairs its own configuration
    with another checkout's remote."""
    worktree = tmp_path / "worktree"
    _write_board_config(worktree, "")
    monkeypatch.setattr(
        checkout, "_git_output", lambda _arguments, *, directory=None: str(directory)
    )
    read_from: list[tuple[str, Path | None]] = []

    def remote_url(remote: str, *, directory: Path | None = None) -> str:
        read_from.append((remote, directory))
        return "git@github.com:owner/repo.git"

    monkeypatch.setattr(checkout, "remote_url", remote_url)

    target = _context().for_directory(worktree).repository_id

    assert (target, read_from) == (
        forge.RepositoryId(github.GITHUB_HOST, ("owner",), "repo"),
        [("origin", worktree), ("origin", worktree)],
    )


def _exit_code(command: list[str]) -> int | str | None:
    """`main`'s exit code, whether it returns it or argparse exits with it."""
    try:
        return cli.main(command)
    except SystemExit as exit_request:
        return exit_request.code


def _forbid_context_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every fact a context holds starts from its toplevel, so forbidding it
    forbids every read a context could make."""

    def unused(_context: RunContext) -> Path:
        pytest.fail("this command must not read the run's repository context")

    monkeypatch.setattr(RunContext, "toplevel", property(unused))


@pytest.mark.parametrize(
    ("command", "stdin", "exit_code"),
    [
        pytest.param(["claim", "--no-such-flag"], "", 2, id="parse-error"),
        pytest.param(["body"], "", 2, id="body-without-its-mode"),
        pytest.param(["--repo", "owner/repo", "run"], "", 2, id="workspace-run"),
        pytest.param(["run"], "", 2, id="workspace-run-without-a-configuration"),
    ],
)
def test_a_command_that_needs_no_repository_reads_no_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: list[str],
    stdin: str,
    exit_code: int,
) -> None:
    """Issue #457 proof 2: the run's context is built only after the
    workspace dispatch, and lazily, so a parse refusal and a workspace
    command never read the repository a context would answer for."""
    _forbid_context_reads(monkeypatch)
    monkeypatch.setattr(cli, "_workspace_config_path", lambda: tmp_path / "workspace.toml")
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))

    assert _exit_code(command) == exit_code


@pytest.mark.parametrize(
    ("repo", "command", "envelope"),
    [
        pytest.param("/tmp/x", ["bootstrap"], "", id="absolute-path"),
        pytest.param("./x", ["bootstrap"], "", id="relative-path"),
        pytest.param("owner", ["status"], "", id="bare-owner"),
        pytest.param("", ["status"], "", id="empty"),
        pytest.param("o/.", ["status"], "", id="current-directory-name"),
        pytest.param("o/..", ["status"], "", id="parent-directory-name"),
        pytest.param(
            "a/b/c",
            ["next", "--json"],
            '{"ok": false, "reason": "invalid_usage", '
            '"message": "repository must be OWNER/REPO, not \'a/b/c\'"}\n',
            id="three-segments-under-json",
        ),
    ],
)
def test_a_repo_that_is_not_owner_slash_repo_refuses_before_any_git_or_gh_call(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    repo: str,
    command: list[str],
    envelope: str,
) -> None:
    """Issue #465 proof 1: `--repo` names a GitHub repository or nothing --
    a path is never silently dropped for the checkout's own remote."""
    _forbid_context_reads(monkeypatch)

    def no_process(*_args: object, **_kwargs: object) -> None:
        pytest.fail("an invalid --repo must refuse before any git or gh process starts")

    monkeypatch.setattr(subprocess, "Popen", no_process)

    assert _exit_code(["--repo", repo, *command]) == 2
    refusal = f"ERROR: repository must be OWNER/REPO, not '{repo}'\n"
    assert capsys.readouterr() == (envelope, refusal)


def test_protect_judges_its_payload_without_ever_building_a_run_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #457 proof 2: `protect` is dispatched before the run's context
    exists and judges from its own payload's path -- here a real one
    outside every repository, which it allows unjudged. What an allow
    prints is the hook protocol's (PROT-01), not this proof's."""

    def unused(*_args: object, **_kwargs: object) -> None:
        pytest.fail("protect must never build a run context")

    monkeypatch.setattr(RunContext, "__init__", unused)
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "notes.txt")}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    assert _exit_code(["protect"]) == 0
