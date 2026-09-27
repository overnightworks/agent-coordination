"""One command run's static repository facts (issue #457, slice A of #418).

A `RunContext` answers the questions every store and forge command asks
about the checkout it runs in -- its toplevel, its tracked board
configuration, the canonical remote and where that remote points, the forge
repository it names, the default branch, the forge itself, and its one
observation of `refs/aco/state` (issue #477). Each fact is
read the first time a command asks for it and held for the rest of that run,
never before: a command that refuses early, or never needs a fact, never
pays the git, filesystem, or `gh` read behind it.

One context stands for one directory. `for_directory` answers for another
checkout (`start`'s freshly created worktree, `rescope`'s checkout resolved
from its own paths); `fresh` re-reads the same
directory from scratch (`board --serve` takes one per request, so nothing is
held across requests); `observed_afresh` re-reads only its state-ref
observation and the forge built from it. `protect` never builds one: it judges from its own
payload's path.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cached_property
from pathlib import Path

from . import board, body, checkout, forge, github, protocol, store

ForgeBuilder = Callable[["RunContext"], forge.ForgeReader]


class RepoMeaninglessUnderStateRefError(protocol.ClaimUnavailableError):
    """`--repo` under `storage = "state-ref"`, typed (issue #396) so
    `aco brief`'s `--json` can report `invalid_usage` instead of its own
    generic `unavailable` bucket for every other forge-resolution refusal."""


def board_config(toplevel: Path) -> board.BoardConfig:
    """The repository's board configuration, refused before
    `board.load_config` ever runs when `.agent-claim/board.toml` is not
    actually tracked by git (#315): a `.gitignore` that ignores every
    dot-directory keeps a freshly written pin off every worktree unless it
    is force-added, and the prior silent `storage = github` default then
    surfaced as the unrelated "no forge adapter for host ..." the moment a
    forge command resolved a non-GitHub canonical remote. Reads `toplevel`
    explicitly (issue #314 gate B3), never the calling process's own cwd:
    `protect` and `rescope` pass their payload's own resolved checkout, so a
    foreign cwd can never wrongly deny a valid config or bless an untracked
    one."""
    if not checkout.path_is_tracked(board.CONFIG_PATH.as_posix(), directory=toplevel):
        raise protocol.ClaimUnavailableError(
            f"{board.CONFIG_PATH} is not tracked in this checkout, so its "
            f"storage pin cannot be trusted: git add -f {board.CONFIG_PATH}"
        )
    return board.load_config(toplevel / board.CONFIG_PATH)


def refuse_unsupported_forge_host(location: checkout.RemoteLocation) -> None:
    """The GitHub-storage forge target's precondition beyond Erwartung 6:
    GitHub is the one forge adapter reached by host, so a canonical remote
    on any other host refuses by name here, before ever asking `gh`. Never
    reached under `storage = "state-ref"` (issue #248), where a non-GitHub
    canonical remote is no error."""
    if location.host != github.GITHUB_HOST:
        raise protocol.ClaimUnavailableError(f"no forge adapter for host {location.host}")


def refuse_canonical_remote_mismatch(
    forge_target: forge.RepositoryId, location: checkout.RemoteLocation
) -> None:
    """Every GitHub forge target's shared refusal (Erwartung 6): `--repo`,
    or whatever `discover_repository` resolved, must name the same
    repository the canonical remote's own URL points at, or nothing is read
    or written."""
    if (forge_target.host, forge_target.path) != (location.host, location.path):
        raise protocol.ClaimUnavailableError(
            f"forge target {forge_target.path} does not match canonical remote "
            f"{location.path}; run aco from that repository's checkout"
        )


# The facts a `RunContext` holds that stand on its one state-ref
# observation rather than on the checkout: `observed_afresh` drops exactly
# these and keeps every other fact it read.
_OBSERVATION_BOUND_FACTS = frozenset({"observation", "forge"})


class RunContext:
    """The static facts of one command run in one directory, each read
    lazily and held once read (see the module docstring).

    `directory` is the checkout this context answers for, or `None` for
    the calling process's own cwd. `build_forge` turns this context's facts
    into the storage pin's forge adapter; the context holds what it builds.
    """

    def __init__(
        self,
        repo: forge.RepositoryId | None,
        *,
        build_forge: ForgeBuilder,
        directory: Path | None = None,
    ) -> None:
        self.repo = repo
        self.directory = directory
        self._build_forge = build_forge

    def for_directory(self, directory: Path, *, is_toplevel: bool = False) -> RunContext:
        """A context for another checkout of the same run (`start`'s
        created worktree, `rescope`'s resolved checkout): nothing this
        context read carries over. `is_toplevel` says the caller already
        resolved `directory` as its checkout's toplevel (`rescope`), so the
        child holds it as read instead of asking git a second time."""
        child = RunContext(self.repo, build_forge=self._build_forge, directory=directory)
        if is_toplevel:
            child.toplevel = directory
        return child

    def fresh(self) -> RunContext:
        """The same directory with nothing read yet (`board --serve`'s
        per-request context)."""
        return RunContext(self.repo, build_forge=self._build_forge, directory=self.directory)

    def observed_afresh(self) -> RunContext:
        """The same directory still holding every static fact this context
        read, with its observation of `refs/aco/state` -- and the forge
        built from it -- dropped, so the next ask fetches the state ref
        again (`start` once its trunk fetch is done, CAS-55) without
        reading any static fact twice."""
        child = self.fresh()
        vars(child).update(
            (fact, value)
            for fact, value in vars(self).items()
            if fact not in _OBSERVATION_BOUND_FACTS
        )
        return child

    @cached_property
    def toplevel(self) -> Path:
        """The checkout's toplevel, and the directory every store read runs
        git in: git lists and archives a state tree relative to its own
        working directory, so a read from a subdirectory would see an empty
        tree (#460). Without a working tree there is no configuration to
        read, so the command refuses rather than running on guessed
        defaults (#178)."""
        try:
            return Path(
                checkout._git_output(["rev-parse", "--show-toplevel"], directory=self.directory)
            )
        except protocol.ClaimError as error:
            raise protocol.ClaimUnavailableError(
                "this command reads the repository's body contract from "
                ".agent-claim/board.toml and needs a checkout (a shallow one is "
                f"enough): {error}"
            ) from error

    @cached_property
    def config(self) -> board.BoardConfig:
        return board_config(self.toplevel)

    @property
    def canonical_remote(self) -> str:
        return self.config.canonical_remote

    @cached_property
    def remote_location(self) -> checkout.RemoteLocation:
        """The canonical remote's configured URL, parsed host-neutrally
        (issue #245) -- read only when a forge target is resolved, so a
        forge-free command never errs on a non-GitHub canonical remote."""
        return checkout.parse_remote_location(
            checkout.remote_url(self.canonical_remote, directory=self.directory)
        )

    @cached_property
    def repository_id(self) -> forge.RepositoryId:
        """The repository this run's forge talks to, refused by name before
        any forge is built (issues #176, #245, #248): under `state-ref` the
        canonical remote's own host and path, with `--repo` meaningless;
        under `github` `--repo` or the discovered repository, which must
        match the canonical remote."""
        if self.config.storage is body.Storage.STATE_REF:
            if self.repo is not None:
                raise RepoMeaninglessUnderStateRefError(
                    "--repo is meaningless under storage = state-ref"
                )
            return forge.RepositoryId(self.remote_location.host, (), self.remote_location.path)
        refuse_unsupported_forge_host(self.remote_location)
        target = (
            self.repo
            if self.repo is not None
            else github.discover_repository(
                remote_url=self._origin_remote_url, directory=self.directory
            )
        )
        refuse_canonical_remote_mismatch(target, self.remote_location)
        return target

    def _origin_remote_url(self) -> str:
        return checkout.origin_remote_url(directory=self.directory)

    @cached_property
    def default_branch(self) -> str:
        """The default branch by provider: `origin/HEAD` of this directory
        under `state-ref` (every repository piloting that pin names its
        canonical remote `origin`), the forge's own answer under `github`."""
        if self.config.storage is not body.Storage.STATE_REF:
            return self.forge.default_branch()
        branch = checkout.default_branch_name(directory=self.directory)
        if branch is None:
            raise protocol.ClaimUnavailableError(
                "cannot resolve the default branch; run aco from a checkout with origin/HEAD set"
            )
        return branch

    @cached_property
    def forge(self) -> forge.ForgeReader:
        return self._build_forge(self)

    @cached_property
    def observation(self) -> protocol.ClaimState:
        """This directory's one fetch of `refs/aco/state` (issue #477), from
        its toplevel over the canonical remote: the state-ref board and
        every CLI check read this same snapshot. A failed fetch raises and
        is not held. No command asks again after its own write (`land`
        releases through `fresh`); a transition still fetches for itself
        until #418 B2 hands it this observation."""
        return store.fetch_state(worktree=self.toplevel, remote=self.canonical_remote)
