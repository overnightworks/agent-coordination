"""Behaviour of `agent_coordination.session.RunContext` (issue #457): one
command run's static repository facts, read lazily, held once read, and
answered per directory."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from cli_fixtures import (
    _push_repository_trunk,
    _real_git,
    _real_repository_with_bare_remote,
    fetched_once_then_read,
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


def _checkout_configuring(tmp_path: Path, remote: str, board_config: str) -> Path:
    """A real checkout `worktree` that configures `remote`, with
    `board_config` as its board configuration."""
    worktree = tmp_path / "worktree"
    _real_git(tmp_path, "init", "-q", str(worktree))
    _real_git(worktree, "remote", "add", remote, "git@github.com:owner/repo.git")
    _write_board_config(worktree, board_config)
    return worktree


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
    worktree = _checkout_configuring(tmp_path, "origin", "")

    _ = _context().for_directory(worktree, is_toplevel=True).repository_id

    assert discovered_for == [worktree]


@pytest.mark.parametrize("canonical_remote", ["origin", "hub"])
def test_default_branch_under_state_ref_reads_the_canonical_remotes_head_in_the_context_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, canonical_remote: str
) -> None:
    """Issue #322 review finding 1, now the context's own fact (issue #457):
    a context for `start`'s created worktree reads the recorded `HEAD`
    there, never from the calling process's own cwd -- the canonical
    remote's, whichever it is (issue #490)."""
    worktree = _checkout_configuring(
        tmp_path,
        canonical_remote,
        f'storage = "state-ref"\ncanonical_remote = "{canonical_remote}"\n',
    )
    reads: list[tuple[str, Path | None]] = []

    def recorded_default_branch(remote: str, *, directory: Path | None = None) -> str:
        reads.append((remote, directory))
        return "main"

    monkeypatch.setattr(checkout, "recorded_default_branch", recorded_default_branch)

    branch = _context().for_directory(worktree).default_branch

    assert (branch, reads) == ("main", [(canonical_remote, worktree)])


def _hub_canonical_repository(tmp_path: Path, *, origin_url: str | None) -> Path:
    """A repository whose canonical remote `hub` names `owner/repo` on
    GitHub, with an `origin` at `origin_url` beside it when given."""
    repository = tmp_path / "repo"
    _real_git(tmp_path, "init", "-q", str(repository))
    _write_board_config(repository, 'canonical_remote = "hub"\n')
    _real_git(repository, "remote", "add", "hub", "git@github.com:owner/repo.git")
    if origin_url is not None:
        _real_git(repository, "remote", "add", "origin", origin_url)
    return repository


@pytest.mark.parametrize(
    "origin_url",
    [pytest.param(None, id="no-origin"), pytest.param("git@github.com:fork/repo.git", id="fork")],
)
def test_repository_id_is_discovered_from_the_canonical_remotes_url(
    tmp_path: Path, origin_url: str | None
) -> None:
    """#310 finding 138: with `hub` canonical, the repository is discovered
    from `hub`'s URL, with or without an `origin` -- a fork's `origin` is
    never read."""
    repository = _hub_canonical_repository(tmp_path, origin_url=origin_url)

    context = RunContext(None, build_forge=_forge_never_built, directory=repository)

    assert context.repository_id == forge.RepositoryId(github.GITHUB_HOST, ("owner",), "repo")


def test_repository_id_refuses_a_repo_naming_the_fork_beside_the_canonical_remote(
    tmp_path: Path,
) -> None:
    """#310 finding 138: `--repo` naming the fork an `origin` points at,
    while `hub` is canonical, refuses loudly rather than reading the fork."""
    repository = _hub_canonical_repository(tmp_path, origin_url="git@github.com:fork/repo.git")
    fork = forge.RepositoryId(github.GITHUB_HOST, ("fork",), "repo")
    context = RunContext(fork, build_forge=_forge_never_built, directory=repository)

    with pytest.raises(ClaimUnavailableError, match="does not match canonical remote owner/repo"):
        _ = context.repository_id


def test_a_context_for_another_directory_reads_its_remotes_there(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #457 proof 7: `for_directory` answers the canonical remote and
    the repository it names from its own checkout, never from the calling
    process's cwd, so a child context never pairs its own configuration
    with another checkout's remote -- one read of the canonical remote's
    URL answers both (#310 finding 138)."""
    worktree = _checkout_configuring(tmp_path, "origin", "")
    read_from: list[tuple[str, Path | None]] = []

    def remote_url(remote: str, *, directory: Path | None = None) -> str:
        read_from.append((remote, directory))
        return "git@github.com:owner/repo.git"

    monkeypatch.setattr(checkout, "remote_url", remote_url)

    target = _context().for_directory(worktree).repository_id

    assert (target, read_from) == (
        forge.RepositoryId(github.GITHUB_HOST, ("owner",), "repo"),
        [("origin", worktree)],
    )


def test_a_failed_observation_is_fetched_again_and_a_successful_one_is_held(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #477 (CAS-53): a fetch of `refs/aco/state` that fails is never held --
    the context's next ask fetches again, from its toplevel over its
    canonical remote -- while one that succeeds answers every later ask."""
    worktree = _checkout_configuring(tmp_path, "origin", "")
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


def test_a_default_branch_held_before_the_fetch_never_names_the_fetched_ref(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #492: under `state-ref` the default branch held before the
    fetch is `main`; once the recorded `HEAD` names `trunk`, the fetched
    default-branch ref and every later ask name `trunk`, and the trunk asked
    after it shares that one fetch."""
    repository = _pushed_repository(tmp_path, 'storage = "state-ref"\n')
    _real_git(repository, "push", "-q", "origin", "main:trunk")
    _real_git(repository, "remote", "set-head", "origin", "main")
    context = _context().for_directory(repository)
    held = context.default_branch
    _real_git(repository, "remote", "set-head", "origin", "trunk")
    trunk_calls = trunk_git_calls(monkeypatch, "origin")

    fetched = context.fetched_default_branch_ref()
    trunk = context.fetched_trunk_ref()

    assert (held, fetched, context.default_branch, trunk) == (
        "main",
        "refs/remotes/origin/trunk",
        "trunk",
        "refs/remotes/origin/trunk",
    )
    assert fetched_once_then_read(trunk_calls) == {repository.resolve(): True}


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


def test_a_fresh_context_whose_configuration_names_another_remote_fetches_that_remote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #488 with #457 proof 6: the run fetched `origin`, then the
    landed configuration names `hub`, whose tracking ref stands one commit
    behind it; the fresh context rereads that configuration and fetches
    `hub` before answering, never taking `origin`'s fetch for `hub`'s."""
    repository = _pushed_repository(tmp_path)
    hub = tmp_path / "hub.git"
    _real_git(tmp_path, "init", "-q", "--bare", "-b", "main", str(hub))
    _real_git(repository, "remote", "add", "hub", str(hub))
    stale = _commit(repository, "second")
    _push_repository_trunk(repository, "hub")
    landed = _commit(repository, "landed")
    _real_git(repository, "push", "-q", "hub", "main")
    _real_git(repository, "update-ref", "refs/remotes/hub/main", stale)
    context = _context().for_directory(repository)
    context.fetched_trunk_ref()
    (repository / ".agent-claim" / "board.toml").write_text('canonical_remote = "hub"\n')
    hub_reads = trunk_git_calls(monkeypatch, "hub")

    fetched = context.fresh().fetched_trunk_ref()

    assert (fetched, checkout.trunk_commit(fetched, directory=repository)) == (
        "refs/remotes/hub/main",
        landed,
    )
    assert fetched_once_then_read(hub_reads) == {repository.resolve(): True}


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


def test_a_trunk_the_fetch_pruned_fails_loud_on_every_ask_never_answered_by_the_held_ref(
    tmp_path: Path,
) -> None:
    """Issue #488: the trunk held before the fetch names `origin/main`; the
    fetch prunes it and nothing else resolves -- no recorded `HEAD`, no
    local `main` or `master` -- so every later ask fails loud instead of
    answering with the ref held from before the fetch."""
    repository = _pushed_repository(tmp_path)
    _real_git(repository, "branch", "-m", "main", "work")
    _real_git(repository, "config", "fetch.prune", "true")
    context = _context().for_directory(repository)
    held = context.trunk_ref
    _real_git(tmp_path / "remote.git", "update-ref", "-d", "refs/heads/main")

    with pytest.raises(ClaimError, match="cannot determine the trunk"):
        context.fetched_trunk_ref()
    with pytest.raises(ClaimError, match="cannot determine the trunk"):
        context.fetched_trunk_ref()

    assert held == "refs/remotes/origin/main"


def _hub_never_added(_repository: Path) -> None:
    """The board names `hub`, but this clone only ever added `origin`."""


def _hub_removed_leaving_its_refs(repository: Path) -> None:
    """`hub` was added, fetched with its `HEAD` recorded, then dropped from
    the configuration: its remote-tracking refs outlive it."""
    _real_git(repository, "remote", "add", "hub", str(repository.parent / "remote.git"))
    _real_git(repository, "fetch", "-q", "hub")
    _real_git(repository, "remote", "set-head", "hub", "main")
    _real_git(repository, "config", "--remove-section", "remote.hub")


@pytest.mark.parametrize(
    "unconfigure_hub",
    [
        pytest.param(_hub_never_added, id="never-added"),
        pytest.param(_hub_removed_leaving_its_refs, id="removed-leaving-its-refs"),
    ],
)
@pytest.mark.parametrize(
    "read",
    [
        pytest.param(lambda context: context.trunk_ref, id="trunk"),
        pytest.param(lambda context: context.fetched_trunk_ref(), id="fetched-trunk"),
        pytest.param(lambda context: context.observation, id="observation"),
        pytest.param(lambda context: context.default_branch, id="default-branch"),
        pytest.param(lambda context: context.repository_id, id="repository"),
    ],
)
def test_every_read_of_a_canonical_remote_the_checkout_does_not_configure_refuses_naming_it(
    tmp_path: Path,
    unconfigure_hub: Callable[[Path], None],
    read: Callable[[RunContext], object],
) -> None:
    """Issue #508 proof 1, against real git: the board names `hub`, which
    this checkout does not configure, so its trunk, its fetch, its state
    ref, its default branch and the repository it names all refuse by
    naming it -- never answered by the local `main` or by the refs a
    removed `hub` left behind."""
    repository = _pushed_repository(tmp_path, 'storage = "state-ref"\ncanonical_remote = "hub"\n')
    unconfigure_hub(repository)
    context = _context().for_directory(repository)

    with pytest.raises(ClaimError) as refusal:
        read(context)

    assert str(refusal.value) == (
        "cannot determine the trunk: canonical remote 'hub' is not configured"
    )


def test_a_configured_canonical_remote_without_branches_still_guesses_the_local_trunk(
    tmp_path: Path,
) -> None:
    """Issue #508 proof 2: `hub` is configured but carries no branch yet --
    a fresh or offline repository -- so the trunk is still the local
    `main` (issue #492 ruling)."""
    repository = _pushed_repository(tmp_path, 'canonical_remote = "hub"\n')
    _real_git(repository, "remote", "add", "hub", str(tmp_path / "hub.git"))

    assert _context().for_directory(repository).trunk_ref == "main"


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
