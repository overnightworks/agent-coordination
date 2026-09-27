"""One command run's static repository facts (issue #457, slice A of #418).

A `RunContext` answers the questions every store and forge command asks
about the checkout it runs in -- its toplevel, its tracked board
configuration, the canonical remote and where that remote points, the forge
repository it names, the default branch, the trunk, the forge itself, and
its one observation of `refs/aco/state` (issue #477). Each fact is
read the first time a command asks for it and held for the rest of that run,
never before: a command that refuses early, or never needs a fact, never
pays the git, filesystem, or `gh` read behind it.

One context stands for one directory. `for_directory` answers for another
checkout (`start`'s freshly created worktree, `rescope`'s checkout resolved
from its own paths); `fresh` re-reads the same
directory from scratch (`board --serve` takes one per request, so nothing is
held across requests); `observed_afresh` drops exactly its state-ref
observation and the forge built from it, and keeps every other fact it read
-- toplevel, board configuration, remote, forge repository, default
branch, trunk -- so the next ask re-reads only the state ref. `protect`
never builds one: it judges from its own payload's path.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cached_property
from pathlib import Path
from typing import cast

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
    one. A file absent altogether is no repair `git add -f` could make
    (issue #505): its own sentence names the one-time adoption instead."""
    if not checkout.path_is_tracked(board.CONFIG_PATH.as_posix(), directory=toplevel):
        if not (toplevel / board.CONFIG_PATH).exists():
            raise protocol.ClaimUnavailableError(
                f"{board.CONFIG_PATH} does not exist in this checkout; merge a pull request "
                f"adding only {board.CONFIG_PATH} into the default branch first, without aco"
            )
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

# The facts a `RunContext` resolves from the canonical remote's recorded
# `HEAD`: a fetch may record or move it, so `_fetch_canonical_remote_once`
# drops these once it fetched and never keeps one read before (issue #484
# ruling).
_RECORDED_HEAD_FACTS = frozenset({"trunk_ref", "recorded_default_branch"})


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
        self._fetched_trunk_remote: str | None = None

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
        per-request context, `land`'s delegated release). The run's trunk
        fetch is no read and carries over with the remote it fetched, so
        the fresh context resolves the trunk anew without fetching that
        remote a second time, yet fetches the canonical remote its reread
        configuration names when that is another (issue #488)."""
        child = RunContext(self.repo, build_forge=self._build_forge, directory=self.directory)
        child._fetched_trunk_remote = self._fetched_trunk_remote
        return child

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
        return checkout.parse_remote_location(self.canonical_remote_url)

    @cached_property
    def canonical_remote_is_configured(self) -> bool:
        """Whether the checkout configures the canonical remote at all,
        answered once per context (issue #508) -- the one answer to that
        question. `configured_canonical_remote` refuses on it before the
        remote's URL, state ref, trunk, fetch and `default_branch`;
        `recorded_default_branch`, the offline checks' read, answers `None`
        on it instead and leaves `None` to each check's own rule. Neither
        a remote-tracking ref a removed remote left behind nor a local
        branch ever answers for a remote that is not there."""
        return checkout.remote_is_configured(self.canonical_remote, directory=self.directory)

    @property
    def configured_canonical_remote(self) -> str:
        """The canonical remote's name, refused by name when the checkout
        does not configure it (issue #508)."""
        if not self.canonical_remote_is_configured:
            raise protocol.ClaimError(
                checkout.unconfigured_trunk_remote_refusal(self.canonical_remote)
            )
        return self.canonical_remote

    @cached_property
    def canonical_remote_url(self) -> str:
        """The canonical remote's configured URL, as git records it: the
        URL a forge target is compared against and discovered from (#310
        finding 138)."""
        return checkout.remote_url(self.configured_canonical_remote, directory=self.directory)

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
                remote_url=self.canonical_remote_url, directory=self.directory
            )
        )
        refuse_canonical_remote_mismatch(target, self.remote_location)
        return target

    @property
    def default_branch(self) -> str:
        """The default branch by provider: the canonical remote's recorded
        default branch under `state-ref`, where that remote is the forge,
        the forge's own answer under `github` (issue #484 rulings). Under
        `state-ref` it holds nothing of its own, so the fetch that drops the
        recorded default branch drops this answer with it (issue #492)."""
        remote = self.configured_canonical_remote
        if self.config.storage is not body.Storage.STATE_REF:
            return self._forge_default_branch
        branch = self.recorded_default_branch
        if branch is None:
            raise protocol.ClaimUnavailableError(
                f"cannot resolve the default branch; run aco from a checkout with {remote}/HEAD set"
            )
        return branch

    @cached_property
    def _forge_default_branch(self) -> str:
        return self.forge.default_branch()

    @cached_property
    def recorded_default_branch(self) -> str | None:
        """The canonical remote's recorded default branch in this checkout,
        or `None` when none is recorded, it dangles (issue #490), or the
        checkout does not configure that remote, whose leftover `HEAD`
        records nothing (issue #508): the offline checks' default branch,
        each check keeping its own rule for `None` -- `rescope`'s names the
        unconfigured remote itself (PROT-45)."""
        if not self.canonical_remote_is_configured:
            return None
        return checkout.recorded_default_branch(self.canonical_remote, directory=self.toplevel)

    @cached_property
    def trunk_ref(self) -> str:
        """The canonical remote's trunk ref in this checkout, as the last
        fetch left it, without fetching (issue #488): the ref a command
        walks for landings or diffs a lane against."""
        return self._resolved_trunk_ref()

    def fetched_trunk_ref(self) -> str:
        """The canonical remote's trunk ref once this checkout fetched that
        remote -- at most once per run and remote (issue #488): the ref `start` builds
        from and a `state-ref` landing's worktree cleanup judges. The
        recorded `HEAD` is read again after the fetch, never one held from
        before it, since a fetch may record or move it: the held trunk and
        recorded default branch are dropped before the resolution, so one
        that fails is asked again."""
        self._fetch_canonical_remote_once()
        return self.trunk_ref

    def fetched_default_branch_ref(self) -> str:
        """The default branch's tracking ref of the canonical remote once
        this checkout fetched that remote, sharing `fetched_trunk_ref`'s one
        fetch per run and remote (issue #492): the ref `land` fast-forwards
        to and `release --merged` walks under `github`, named by the same
        `default_branch` their pull request checks compare against, so the
        forge's default branch counts even where the remote records no `HEAD`."""
        self._fetch_canonical_remote_once()
        return f"refs/remotes/{self.canonical_remote}/{self.default_branch}"

    def _fetch_canonical_remote_once(self) -> None:
        remote = self.configured_canonical_remote
        if self._fetched_trunk_remote == remote:
            return
        checkout.fetch_remote(remote, directory=self.toplevel)
        self._fetched_trunk_remote = remote
        for fact in _RECORDED_HEAD_FACTS:
            self.__dict__.pop(fact, None)

    def _resolved_trunk_ref(self) -> str:
        remote = self.configured_canonical_remote
        return checkout.trunk_ref_after(
            remote,
            checkout.recorded_head_ref(remote, directory=self.toplevel),
            directory=self.toplevel,
        )

    @cached_property
    def forge(self) -> forge.ForgeReader:
        return self._build_forge(self)

    @property
    def forge_writer(self) -> forge.ForgeWriter:
        """The same resolved forge, narrowed to its writing surface (issue
        #248, #283). The cast is honest, not a suppression: every adapter
        this tool builds -- `github.GitHubForge`, `state_board.StateRefBoard`
        (its `ItemWriter` injected by `_state_ref_forge`), and every test
        fake standing in for either -- already implements the full
        `ForgeWriter` surface, an unsupported operation as a method raising
        `forge.ForgeUnsupportedError`, checked at each call site by
        `capability()`, never by `isinstance`."""
        return cast(forge.ForgeWriter, self.forge)

    @cached_property
    def observation(self) -> protocol.ClaimState:
        """This directory's one fetch of `refs/aco/state` (issue #477), from
        its toplevel over the canonical remote: the state-ref board, every
        CLI check, and the run's first transition read this same snapshot. A
        failed fetch raises and is not held. After a transition it holds the
        state that transition wrote (`transition`); no command fetches again
        to judge its own write (`land` releases through `fresh`)."""
        return store.fetch_state(worktree=self.toplevel, remote=self.configured_canonical_remote)

    def for_lane_worktree(self, worktree: Path) -> RunContext:
        """A context for a lane worktree of this checkout that writes the
        claim store from there, so the lane's own fetch anchor and lineage
        stamp start at its claim (CAS-09): it holds `worktree` as its
        toplevel and this context's board configuration -- the main
        checkout's governs the store, never a lane's own -- and observes the
        state ref from the worktree itself."""
        child = self.for_directory(worktree, is_toplevel=True)
        child.config = self.config
        return child

    def transition(
        self,
        subject: store.TransitionSubject | store.ClaimTransitionSubject,
        intent: protocol.ClaimTransitionIntent,
    ) -> protocol.ClaimState:
        """Write one transition onto this run's observation of the state ref
        (issue #494) and hold the state it wrote as the new observation, so
        a second write of the same run (`cut`) applies to the first one's
        result instead of refusing against a snapshot it already moved."""
        written = store.commit_transition(
            observed=store.Observation(self.toplevel, self.canonical_remote, self.observation),
            subject=subject,
            intent=intent,
        )
        self.observation = written
        return written
