"""Behaviour of `agent_coordination.session.RunContext` (issue #457): one
command run's static repository facts, read lazily, held once read, and
answered per directory."""

from __future__ import annotations

from pathlib import Path

import pytest
from cli_fixtures import stub_board_config_tracked

from agent_coordination import checkout, forge, github, session
from agent_coordination.protocol import ClaimUnavailableError
from agent_coordination.session import RunContext


@pytest.fixture(autouse=True)
def _tracked_board_config(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_board_config_tracked(monkeypatch)


def _forge_never_built(_context: RunContext) -> forge.ForgeReader:
    pytest.fail("this fact must not build the forge")


def _context(repo: str | None = None) -> RunContext:
    return RunContext(repo, build_forge=_forge_never_built)


def _write_board_config(toplevel: Path, text: str) -> None:
    config_dir = toplevel / ".agent-claim"
    config_dir.mkdir(parents=True)
    (config_dir / "board.toml").write_text(text)


def test_remote_location_parses_the_canonical_remote_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkout, "remote_url", lambda remote: "git@github.com:owner/repo.git")

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
    monkeypatch.setattr(checkout, "remote_url", lambda remote: "file:///srv/git/repo.git")

    def unused(*_args: object, **_kwargs: object) -> forge.RepositoryId:
        pytest.fail("a non-GitHub canonical remote must refuse before discover_repository runs")

    monkeypatch.setattr(github, "discover_repository", unused)
    context = _context()

    with pytest.raises(ClaimUnavailableError, match="no forge adapter for host file"):
        _ = context.repository_id


def test_repository_id_checks_erwartung_6_against_a_github_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkout, "remote_url", lambda remote: "git@github.com:owner/repo.git")
    monkeypatch.setattr(
        github,
        "discover_repository",
        lambda repo, remote_url: forge.RepositoryId(github.GITHUB_HOST, ("owner",), "repo"),
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
