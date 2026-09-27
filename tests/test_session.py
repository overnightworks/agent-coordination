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
from cli_fixtures import (
    _push_repository_trunk,
    _real_git,
    _real_repository_with_bare_remote,
    landed_from_another_clone,
    main_exit_code,
    stub_board_config_tracked,
    trunk_git_calls,
)

from agent_coordination import checkout, cli, forge, github, process, session, store
from agent_coordination.protocol import ClaimError, ClaimState, ClaimUnavailableError
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
        lambda **_kwargs: forge.RepositoryId(github.GITHUB_HOST, ("owner",), "repo"),
    )

    target = _context().repository_id

    assert target == forge.RepositoryId(github.GITHUB_HOST, ("owner",), "repo")


def test_repository_id_discovers_the_repository_of_the_context_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #472: a context for another directory asks about that
    directory's repository, never the calling process's own cwd."""
    monkeypatch.setattr(
        checkout, "remote_url", lambda remote, **_kwargs: "git@github.com:owner/repo.git"
    )
    discovered_for: list[Path | None] = []

    def discover_repository(*, directory: Path | None, **_kwargs: object) -> forge.RepositoryId:
        discovered_for.append(directory)
        return forge.RepositoryId(github.GITHUB_HOST, ("owner",), "repo")

    monkeypatch.setattr(github, "discover_repository", discover_repository)

    _ = _context().for_directory(tmp_path, is_toplevel=True).repository_id

    assert discovered_for == [tmp_path]


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


def test_a_failed_observation_is_fetched_again_and_a_successful_one_is_held(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #477 (CAS-53): a fetch of `refs/aco/state` that fails is never held --
    the context's next ask fetches again, from its toplevel over its
    canonical remote -- while one that succeeds answers every later ask."""
    worktree = tmp_path / "worktree"
    _write_board_config(worktree, "")
    monkeypatch.setattr(
        checkout, "_git_output", lambda _arguments, *, directory=None: str(directory)
    )
    observed = ClaimState(tip=None, claims={})
    fetched_from: list[tuple[Path, str]] = []

    def fetch_state(*, worktree: Path, remote: str) -> ClaimState:
        fetched_from.append((worktree, remote))
        if len(fetched_from) == 1:
            raise ClaimUnavailableError("the remote is unreachable")
        return observed

    monkeypatch.setattr(store, "fetch_state", fetch_state)
    context = _context().for_directory(worktree)

    with pytest.raises(ClaimUnavailableError, match="the remote is unreachable"):
        _ = context.observation
    later_asks = (context.observation, context.observation)

    assert (later_asks, fetched_from) == ((observed, observed), [(worktree, "origin")] * 2)


def _commit(repository: Path, message: str) -> str:
    _real_git(repository, "commit", "-q", "--allow-empty", "-m", message)
    return _real_git(repository, "rev-parse", "HEAD").stdout.strip()


def _pushed_repository(tmp_path: Path, board_config: str = "") -> Path:
    """A repository with one commit pushed to its bare `origin`, no `HEAD`
    recorded for it, and `board_config` as its board configuration."""
    repository, _remote = _real_repository_with_bare_remote(tmp_path)
    _commit(repository, "initial")
    _real_git(repository, "push", "-q", "origin", "main")
    _write_board_config(repository, board_config)
    return repository


def _git_version() -> tuple[int, ...]:
    version = _real_git(Path.cwd(), "--version").stdout.split()[2]
    return tuple(int(part) for part in version.split(".")[:2])


def test_a_fetched_trunk_is_resolved_after_the_fetch_never_from_a_trunk_held_before_it(
    tmp_path: Path,
) -> None:
    """Issue #488: with no recorded `HEAD` and no remote-tracking trunk yet,
    the held trunk is the stale local `main`; once the context fetches, it
    resolves the trunk again and names the remote's tip, never the ref it
    held before the fetch."""
    repository = _pushed_repository(tmp_path)
    _real_git(repository, "update-ref", "-d", "refs/remotes/origin/main")
    remote_tip = landed_from_another_clone(
        tmp_path, "commit", "-q", "--allow-empty", "-m", "landed elsewhere"
    )
    context = _context().for_directory(repository)

    held = context.trunk_ref
    fetched = context.fetched_trunk_ref()

    assert (held, fetched, context.trunk_ref) == ("main", "refs/remotes/origin/main", fetched)
    assert checkout.trunk_commit(fetched, directory=repository) == remote_tip


@pytest.mark.skipif(
    _git_version() < (2, 48), reason="git records a fetched remote's HEAD from 2.48 on"
)
def test_a_fetch_records_the_remote_head_the_trunk_then_names(tmp_path: Path) -> None:
    """Issue #488 proof 4: a checkout that never recorded its remote's
    `HEAD` has it recorded by the context's own fetch, and the trunk is read
    from it."""
    repository = _pushed_repository(tmp_path)
    before = checkout.recorded_head_ref("origin", directory=repository)

    fetched = _context().for_directory(repository).fetched_trunk_ref()

    assert (before, checkout.recorded_head_ref("origin", directory=repository), fetched) == (
        None,
        "refs/remotes/origin/main",
        "refs/remotes/origin/main",
    )


def test_every_trunk_read_names_the_canonical_remote_never_a_diverging_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #488 proof 2: `hub` is canonical beside an `origin` one commit
    behind it; the held trunk, the fetched trunk, and the landings walked
    from it are all `hub`'s, and git never fetches or reads `origin`'s
    `HEAD` for them."""
    repository = _pushed_repository(tmp_path, 'canonical_remote = "hub"\n')
    hub = tmp_path / "hub.git"
    _real_git(tmp_path, "init", "-q", "--bare", "-b", "main", str(hub))
    _real_git(repository, "remote", "add", "hub", str(hub))
    _commit(repository, "second")
    _push_repository_trunk(repository, "hub")
    origin_reads = trunk_git_calls(monkeypatch, "origin")
    context = _context().for_directory(repository)

    held, fetched = context.trunk_ref, context.fetched_trunk_ref()
    walked = checkout.trunk_landings(fetched, 20, directory=context.toplevel)

    assert (held, fetched, len(walked), origin_reads) == (
        "refs/remotes/hub/main",
        "refs/remotes/hub/main",
        2,
        [],
    )


def test_a_failed_trunk_fetch_fails_loud_and_is_fetched_again_on_the_next_ask(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #488: a fetch that fails is never held as done -- the next ask
    fetches again -- while one that succeeds answers every later ask."""
    repository = _pushed_repository(tmp_path)
    launch = checkout._git_run
    fetched_from: list[str] = []

    def failing_first_fetch(
        arguments: list[str], *, directory: Path | None = None
    ) -> process.CapturedResult:
        if arguments[0] == "fetch":
            fetched_from.append(arguments[1])
            if len(fetched_from) == 1:
                return process.CapturedResult(1, b"", b"fatal: could not read from remote")
        return launch(arguments, directory=directory)

    monkeypatch.setattr(checkout, "_git_run", failing_first_fetch)
    context = _context().for_directory(repository)

    with pytest.raises(ClaimError, match="could not read from remote"):
        context.fetched_trunk_ref()
    later_asks = (context.fetched_trunk_ref(), context.fetched_trunk_ref())

    assert (later_asks, fetched_from) == (("refs/remotes/origin/main",) * 2, ["origin"] * 2)


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

    assert main_exit_code(command) == exit_code


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

    assert main_exit_code(["--repo", repo, *command]) == 2
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

    assert main_exit_code(["protect"]) == 0
