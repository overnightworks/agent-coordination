"""Owns `aco protect`'s own judgement (issue #394, caller: architecture-audit
distributor #389 finding 2): `judge` reads one already-parsed hook payload
and returns a typed `Verdict` -- allow or deny, with the deny reason -- from
the payload envelope checks (PROT-03..) through the shared Checkout/
Default-Branch/Claim-Scope/Bash-pattern chain every mutating tool call runs.
`cli` keeps only reading stdin, calling `judge`, and printing whatever the
verdict's `stdout_text` and `stderr_text` carry -- nothing for an allow, the
deny object on stdout and its sentence on stderr for a deny -- under one
`except Exception` frame (PROT-17); this module never touches stdin, stdout,
or stderr itself. `specs/protect.spec.md` owns every denial reason, the order
they are judged in, and the output and exit codes `Verdict` produces -- this
file cites those IDs rather than restating them.

`judge` takes `canonical_remote_for` and `lane_shared_for` as explicit
dependencies rather than reading the configuration itself: reading
`.agent-claim/board.toml` behind its tracked-file precondition (PIN-01), and
the lane-shared registry files from the trunk's committed copy of it
(PROT-46), is the board-configuration concern every store command shares,
not something this lower layer re-implements or reaches upward for.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import TypeVar

from . import checkout, hook_input, protocol, store


class Decision(StrEnum):
    """`judge`'s two outcomes (PROT-01/PROT-02)."""

    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class Verdict:
    """`judge`'s own typed result and the one owner of what each output
    channel carries (PROT-01/PROT-02) -- `reason` is always `None` for
    `ALLOW` and always a sentence for `DENY`."""

    decision: Decision
    reason: str | None = None

    @classmethod
    def allow(cls) -> Verdict:
        return cls(Decision.ALLOW)

    @classmethod
    def deny(cls, reason: str) -> Verdict:
        return cls(Decision.DENY, reason=reason)

    @property
    def exit_code(self) -> int:
        return 0 if self.decision is Decision.ALLOW else 2

    @property
    def stdout_text(self) -> str | None:
        """What stdout carries (PROT-01/PROT-02): nothing for an allow, since
        an allow object would fail Claude Code's hook schema, and the deny
        object Grok reads."""
        if self.decision is Decision.ALLOW:
            return None
        return json.dumps({"decision": "deny", "reason": self.reason})

    @property
    def stderr_text(self) -> str | None:
        """What stderr carries (PROT-01/PROT-02): nothing for an allow, the
        deny's own sentence for the hosts that read it on exit 2."""
        return self.reason


_CanonicalRemoteFor = Callable[[Path], str]
_LaneSharedFor = Callable[[Path], tuple[str, ...]]


class HookToolEffect(StrEnum):
    """What a `PreToolUse` hook name does to files, for `protect`'s verdict.

    `READ` never touches a file's contents, so it clears without a claim
    check. `COMMAND_TEXT` names no path key at all -- its own `command`
    text is scanned for a recognized write pattern instead (issue #380);
    a command naming none allows exactly like `READ`, since `protect`
    cannot judge what its own pattern grammar does not recognize.
    `MUTATING` can write, so it is gated on a live overlapping claim
    exactly as today. Any name in neither set is unproven -- `protect` must
    fail closed on it rather than default it to either bucket (issue #238).
    """

    READ = "read"
    COMMAND_TEXT = "command_text"
    MUTATING = "mutating"


APPLY_PATCH_TOOL_NAME = "apply_patch"
NOTEBOOK_EDIT_TOOL_NAME = "NotebookEdit"


HOOK_TOOL_EFFECTS: Mapping[str, HookToolEffect] = {
    # Read-only: cannot mutate a file, so no claim check is needed.
    # `shell` and the snake_case terminal names below are here too -- the
    # hook payload carries no file path for those, so `protect` cannot gate
    # what it cannot see; this is a named limit (README, "PreToolUse write
    # gate"), not an oversight. `Bash` left this bucket for its own
    # command-text one below (issue #380).
    "Read": HookToolEffect.READ,
    "Glob": HookToolEffect.READ,
    "Grep": HookToolEffect.READ,
    "LS": HookToolEffect.READ,
    "WebFetch": HookToolEffect.READ,
    "WebSearch": HookToolEffect.READ,
    "TodoWrite": HookToolEffect.READ,
    "Task": HookToolEffect.READ,
    "Agent": HookToolEffect.READ,
    # Claude Code's own session tools (issue #448): each steers the session,
    # a subagent, or a workflow, or talks to the operator, and none takes a
    # file path to write -- failing closed on them stalled every session.
    "ToolSearch": HookToolEffect.READ,
    "SendMessage": HookToolEffect.READ,
    "TaskStop": HookToolEffect.READ,
    "TaskOutput": HookToolEffect.READ,
    "StructuredOutput": HookToolEffect.READ,
    "Skill": HookToolEffect.READ,
    "AskUserQuestion": HookToolEffect.READ,
    "ListAgents": HookToolEffect.READ,
    "ScheduleWakeup": HookToolEffect.READ,
    "SendFeedback": HookToolEffect.READ,
    "Workflow": HookToolEffect.READ,
    "Artifact": HookToolEffect.READ,
    "shell": HookToolEffect.READ,
    # Other providers' names for the same read-only or path-blind operations
    # (Grok, Codex): a snake_case terminal command is the same blind spot as
    # `shell` above, and the rest never write a file.
    "read_file": HookToolEffect.READ,
    "grep": HookToolEffect.READ,
    "list_dir": HookToolEffect.READ,
    "run_terminal_command": HookToolEffect.READ,
    "spawn_subagent": HookToolEffect.READ,
    # Command-text: no path key at all -- `hook_input.hook_command_paths`
    # scans the call's own `command` for a recognized write pattern (issue
    # #380); each recognized path then runs the same judgement chain as a
    # mutating tool's own path. `Monitor` runs its own `command` as a shell
    # script exactly like `Bash` does (issue #448 review finding).
    "Bash": HookToolEffect.COMMAND_TEXT,
    "Monitor": HookToolEffect.COMMAND_TEXT,
    # Mutating: gated on a live claim whose scope overlaps the written path.
    "Edit": HookToolEffect.MUTATING,
    "MultiEdit": HookToolEffect.MUTATING,
    "Write": HookToolEffect.MUTATING,
    "search_replace": HookToolEffect.MUTATING,
    "write": HookToolEffect.MUTATING,
    NOTEBOOK_EDIT_TOOL_NAME: HookToolEffect.MUTATING,
    APPLY_PATCH_TOOL_NAME: HookToolEffect.MUTATING,
    "create_file": HookToolEffect.MUTATING,
    "str_replace_editor": HookToolEffect.MUTATING,
}


def _unknown_hook_tool_reason(tool_name: str) -> str:
    return (
        f"{tool_name!r} is not in aco's hook tool table (HOOK_TOOL_EFFECTS, "
        "issue #238); add it there as read-only or mutating before use"
    )


def _hook_field(payload: dict[str, object], *keys: str) -> object:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


_GENERIC_PATH_KEYS = ("path", "file_path", "filePath")


def _hook_path(tool_input: dict[str, object], *, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


PATH_REQUIRED = "path required"
MISSING_HOOK_IDENTITY = (
    f"agent identity is required: set {checkout.IDENTITY_ENVIRONMENT_ORDER} "
    f"({checkout.ACO_AGENT_ENV} can sit in the hook line)"
)


class _HookPathSource(StrEnum):
    """Where a mutating tool's path lives in its `tool_input` -- one owner
    per tool name (issue #252) so a tool can only be read from a key it
    actually sends; a decoy value under a key it does not send (an in-scope
    `path` next to `NotebookEdit`'s real, out-of-scope `notebook_path`) is
    never looked at."""

    GENERIC_KEYS = "generic_keys"
    NOTEBOOK_PATH = "notebook_path"
    PATCH_TEXT = "patch_text"


_HOOK_PATH_SOURCES: dict[str, _HookPathSource] = {
    APPLY_PATCH_TOOL_NAME: _HookPathSource.PATCH_TEXT,
    NOTEBOOK_EDIT_TOOL_NAME: _HookPathSource.NOTEBOOK_PATH,
}


def _protect_hook_paths(tool_name: str, payload: dict[str, object]) -> tuple[str, ...]:
    """Every path this hook call's `tool_input` names, read only from the
    key(s) this specific tool sends.

    `apply_patch` (Codex) carries no path key at all -- its patch text sits
    under `command` and can touch several files in one call, so it is parsed
    by the dedicated patch grammar instead. `NotebookEdit` (Claude Code)
    carries only `notebook_path`. Every other tool still yields at most one
    path, from the shared `path`/`file_path`/`filePath` keys."""
    tool_input = _hook_field(payload, "toolInput", "tool_input")
    if not isinstance(tool_input, dict):
        return ()
    source = _HOOK_PATH_SOURCES.get(tool_name, _HookPathSource.GENERIC_KEYS)
    if source is _HookPathSource.PATCH_TEXT:
        command = tool_input.get("command")
        if not isinstance(command, str):
            return ()
        return hook_input.hook_patch_paths(command)
    keys = ("notebook_path",) if source is _HookPathSource.NOTEBOOK_PATH else _GENERIC_PATH_KEYS
    single = _hook_path(tool_input, keys=keys)
    return (single,) if single is not None else ()


_ProtectStateOutcome = tuple[protocol.ClaimState | None, str | None]
_ProtectStateCache = dict[Path, _ProtectStateOutcome]


def _protect_claim_state_or_denial(worktree: Path, canonical_remote: str) -> _ProtectStateOutcome:
    """`protect`'s live snapshot (issue #176, §1): one fetch, no positive
    cache (D2). A non-`None` second element names a denial reason for a
    store the hook cannot trust -- unreachable, auth, malformed tree,
    lineage break -- with the same named text (Erwartung 8) instead of the
    generic 'claim first', which would send the agent toward a command that
    cannot fix a transient fetch failure. `worktree` is the payload path's
    own resolved checkout (issue #314), never the hook process's cwd, so a
    subagent editing a linked worktree is fetched against that worktree's
    own per-worktree fetch/lineage state.
    """
    try:
        state = store.fetch_state(worktree=worktree, remote=canonical_remote)
    except protocol.ClaimError as error:
        return None, f"cannot reach {store.STATE_REF}: {error}"
    if state.tip is None:
        return None, f"cannot reach {store.STATE_REF}: {protocol.MISSING_STATE_REF}"
    return state, None


@dataclass(frozen=True, slots=True)
class _ProtectContext:
    """One hook invocation's own shared dependencies, threaded through the
    whole judgement chain together (issue #394): `state_cache`,
    `canonical_remote_for` and `lane_shared_for` are always needed by the
    same callers, so bundling them keeps every chain function's own
    parameter list short instead of parallel threads of the same values.
    `canonical_remote_for` and `lane_shared_for` are `cli`'s own
    board-configuration readers: resolving `.agent-claim/board.toml`'s
    storage pin, and the trunk's lane-shared files (PROT-46), is that
    layer's own concern, handed down here rather than re-read from this
    lower module."""

    state_cache: _ProtectStateCache
    canonical_remote_for: _CanonicalRemoteFor
    lane_shared_for: _LaneSharedFor


def _protect_cached_claim_state_or_denial(
    path_checkout: checkout.PathCheckout, *, context: _ProtectContext
) -> _ProtectStateOutcome:
    """`_protect_claim_state_or_denial`, fetched at most once per repository
    per hook invocation (issue #314 gate G5): several payload paths in one
    `apply_patch` call can name the same repository through different
    worktrees, and re-fetching for each would let each path be judged
    against a different snapshot of a store that can move between them --
    passing a rescope that narrowed coverage between fetches, for instance,
    though no single live claim ever covered the whole patch. Cached by
    `common_directory`, the one fact every worktree of one repository
    shares, not by `toplevel`, which differs per worktree."""
    cached = context.state_cache.get(path_checkout.common_directory)
    if cached is not None:
        return cached
    canonical_remote = context.canonical_remote_for(path_checkout.toplevel)
    outcome = _protect_claim_state_or_denial(path_checkout.toplevel, canonical_remote)
    context.state_cache[path_checkout.common_directory] = outcome
    return outcome


def _protect_overlapping_claim_exists(
    state: protocol.ClaimState, *, agent: str, branch: str, relative: str
) -> bool:
    return any(
        claim.agent == agent
        and claim.branch == branch
        and protocol.scopes_overlap(claim.scope, (relative,))
        for claim in state.claims.values()
    )


def _protect_session_claim_exists(state: protocol.ClaimState, *, agent: str, branch: str) -> bool:
    return any(claim.agent == agent and claim.branch == branch for claim in state.claims.values())


def _protect_scope_denial(
    state: protocol.ClaimState,
    *,
    agent: str,
    question: _ClaimQuestion,
    lane_shared_for: _LaneSharedFor,
    miss_denial: str,
) -> str | None:
    """Whether a live claim covers `question`'s path, or `miss_denial` when
    not. A file the trunk's `lane_shared` names exactly (PROT-46, issue
    #575) is covered by any live claim this session holds on the branch,
    whatever its scope: it follows every code change mechanically, so no
    scope can name it ahead. The trunk is read only on a scope miss.

    The one overlap check `_protect_path_denial` and `_protect_bash_path_denial`
    both share once they have a trustworthy state and a resolved checkout
    (issue #380 delta, gate finding: Bash used to reimplement this same check
    inline instead of sharing it, risking the two drifting apart) -- each
    caller builds its own `miss_denial` text: `_protect_path_denial`
    distinguishes `claim first` from `{relative} outside claim scope` for
    `apply_patch` (issue #252); `_protect_bash_path_denial` always names both
    the recognized pattern and the path (PROT-33), since a command's own
    several paths need telling apart."""
    branch = question.path_checkout.branch
    relative = question.relative
    if _protect_overlapping_claim_exists(state, agent=agent, branch=branch, relative=relative):
        return None
    if _protect_session_claim_exists(
        state, agent=agent, branch=branch
    ) and relative in lane_shared_for(question.path_checkout.toplevel):
        return None
    return miss_denial


def _protect_single_path_scope_miss_denial(
    state: protocol.ClaimState, *, agent: str, branch: str, relative: str, distinguish_scope: bool
) -> str:
    """`_protect_path_denial`'s own scope-miss text (issue #252): `claim
    first` when this session holds no live claim on the branch at all, or
    when `distinguish_scope` is off (every mutating tool but `apply_patch`,
    which cannot name any other path anyway); `{relative} outside claim
    scope` under `apply_patch` when a live claim exists but simply misses
    this one path."""
    if distinguish_scope and _protect_session_claim_exists(state, agent=agent, branch=branch):
        return f"{relative} outside claim scope"
    return "claim first"


def _protect_not_main_denial(
    path_checkout: checkout.PathCheckout, *, context: _ProtectContext
) -> str | None:
    """`None` when `path_checkout` is a linked worktree off the repository's
    default branch; otherwise the "not main" family of denials gate G4
    names: the shared main checkout, a linked worktree that happens to sit
    on the default branch, or -- never `claim`'s own `{main, master}` guess
    -- a checkout whose default branch cannot even be resolved. The default
    branch is the checkout's canonical remote's recorded one (issue #490),
    so a linked worktree whose board configuration cannot be read denies
    with that refusal first (PROT-12)."""
    if path_checkout.kind is checkout.CheckoutKind.MAIN:
        return checkout.PROTECT_NOT_MAIN_REASON
    toplevel = path_checkout.toplevel
    canonical_remote = context.canonical_remote_for(toplevel)
    default_branch = checkout.recorded_default_branch(canonical_remote, directory=toplevel)
    unknown = checkout.default_branch_unknown_reason(
        canonical_remote, default_branch, directory=toplevel
    )
    if unknown is not None:
        return unknown
    if path_checkout.branch == default_branch:
        return checkout.PROTECT_NOT_MAIN_REASON
    return None


class _LinkOperation(StrEnum):
    """What a write does to a symlink it names as its own path."""

    WRITE = "write"
    """Every file tool and every other recognized pattern: lands wherever
    the link points."""
    REMOVE_OR_RENAME = "remove or rename"
    """`rm` and `mv`: remove or rename a file link itself -- as `mv`'s
    source or the file destination it replaces -- never its target. A
    directory link they name is judged where it lands, erring closed:
    `rm -rf link/` empties its target and `mv x link` moves into it, and
    the operand `hook_input` recognizes never says which one is `mv`'s
    destination (issue #483 review findings)."""

    def writes_through(self, link: Path) -> bool:
        return self is _LinkOperation.WRITE or link.is_dir()


def _resolved_path_checkout(
    absolute_path: str, *, operation: _LinkOperation
) -> checkout.PathCheckout | None:
    """The checkout `absolute_path` belongs to, or `None` when it sits
    outside every repository (PROT-32: not aco's to judge, issue #448) --
    resolved from the path itself (issue #314), never from the hook
    process's cwd, so the same absolute path yields the same verdict from
    any cwd, through the resolver `rescope` shares (issue #483). A path
    outside every repository by its own directory is judged where a write
    to it lands (`_landing_checkout_outside_every_repository`). A symlink
    inside a checkout resolves as the link's own checkout here; the one its
    write lands in is `_link_target_in_another_checkout`'s."""
    path = Path(os.path.normpath(absolute_path))
    own_checkout = checkout.resolve_named_path_checkout(path)
    if own_checkout is None:
        return _landing_checkout_outside_every_repository(path, operation=operation)
    return own_checkout


def _link_target_in_another_checkout(
    absolute_path: str, link_checkout: checkout.PathCheckout, *, operation: _LinkOperation
) -> tuple[str, checkout.PathCheckout] | None:
    """The target a write through the symlink `absolute_path` lands at and
    that target's checkout, when the `operation` writes through the link
    and the target lies in a checkout other than `link_checkout` (issue
    #486: a claim in one checkout must never authorize bytes landing in
    another). `None` for a path that is no symlink, for `rm` or `mv` of a
    file link itself, which never touches the target, and for a target
    in the link's own checkout, its own repository's git directory, or
    outside every repository -- the link's own checkout answers for those
    alone. Raises git's own failure for a target no checkout can be
    resolved for, another repository's git directory among them."""
    path = Path(os.path.normpath(absolute_path))
    if not path.is_symlink() or not operation.writes_through(path):
        return None
    target = os.path.realpath(path)
    if Path(target).is_relative_to(link_checkout.common_directory.resolve()):
        return None
    target_checkout = checkout.resolve_named_path_checkout(Path(target))
    if target_checkout is None or target_checkout.toplevel == link_checkout.toplevel:
        return None
    return target, target_checkout


def _landing_checkout_outside_every_repository(
    path: Path, *, operation: _LinkOperation
) -> checkout.PathCheckout | None:
    """The checkout a write to `path`, whose own directory sits outside
    every repository, still lands in, or `None` when it lands outside every
    repository too. Any symlink on the way -- the file itself or an
    ancestor directory, a dangling one included -- carries the write into
    its target's checkout (issue #448 review finding: a
    `~/.claude/CLAUDE.md` link into a main checkout; issue #483 review
    finding: `mkdir -p` makes a dangling ancestor's target real before the
    write), so that checkout judges it. An operation on the link itself --
    `rm` or `mv` of it -- never touches the target, so it stays outside. A
    directory here lies outside every repository wherever it resolves:
    its own checkout was already resolved through its links."""
    if path.is_dir() or not operation.writes_through(path):
        return None
    target = Path(os.path.realpath(path))
    if target == path:
        return None
    return checkout.resolve_nearest_existing_checkout(target.parent)


def _protect_checkout_denial(
    path_checkout: checkout.PathCheckout, *, context: _ProtectContext
) -> str | None:
    """The denial a resolved checkout earns before any live claim is weighed:
    a checkout with no commit yet (gate G3 -- its branch name could
    otherwise coincidentally match a still-live claim's), or the "not main"
    family `_protect_not_main_denial` owns (gate G4)."""
    if not path_checkout.has_commit:
        return checkout.NO_COMMIT_CHECKOUT_REASON
    return _protect_not_main_denial(path_checkout, context=context)


_SESSION_SETTINGS_DIRECTORY = ".claude/"


def _is_ignored_session_setting(relative: str, path_checkout: checkout.PathCheckout) -> bool:
    """Whether `relative` is a file under the checkout's own `.claude/` that
    git ignores -- the session's own settings, `.claude/settings.local.json`
    with the hook itself among them (PROT-38, issue #448). Such a file never
    reaches a commit, so no claim can answer for it, and denying it made a
    misconfigured hook impossible to switch off without the operator. A
    tracked `.claude/` file is shared configuration and stays gated like any
    other path; Claude Code's own permission prompt for settings edits is
    untouched by this allow."""
    return relative.startswith(_SESSION_SETTINGS_DIRECTORY) and checkout.path_is_ignored(
        relative, directory=path_checkout.toplevel
    )


PROTECT_UNGUARDED_ENV = "ACO_PROTECT_UNGUARDED"


def _unguarded_setting() -> str:
    """`ACO_PROTECT_UNGUARDED` as the session set it, unparsed; empty when
    unset, which names no unguarded directory at all."""
    return os.environ.get(PROTECT_UNGUARDED_ENV, "")


def _names_unguarded_directories() -> bool:
    """Whether the session set `ACO_PROTECT_UNGUARDED` to anything at all;
    unset or empty, every repository is guarded."""
    return bool(_unguarded_setting())


def _unguarded_directories() -> tuple[Path, ...]:
    """The directories `ACO_PROTECT_UNGUARDED` names (`os.pathsep`-separated,
    issue #483), symlink-resolved; unset or empty names none. Any other
    entry that is not an existing absolute directory -- an empty one
    between separators included -- fails closed (PROT-41), raised for
    `cli`'s deny frame like a missing identity: a typo must never silently
    guard nothing, nor exempt whatever a relative entry happens to meet."""
    if not _names_unguarded_directories():
        return ()
    entries = _unguarded_setting().split(os.pathsep)
    for entry in entries:
        if not (os.path.isabs(entry) and os.path.isdir(entry)):
            raise protocol.ClaimError(
                f"{PROTECT_UNGUARDED_ENV}: {entry} is not an absolute directory"
            )
    return tuple(Path(entry).resolve() for entry in entries)


def _is_unguarded(path_checkout: checkout.PathCheckout) -> bool:
    """Whether the repository `path_checkout` belongs to sits at or below an
    `ACO_PROTECT_UNGUARDED` directory (PROT-40) -- a tester's throwaway
    repository in its scratchpad. Matched on the repository's common git
    directory, never the checkout's own path: a linked worktree of a
    guarded repository is never exempted by where the worktree itself
    lies."""
    common_directory = path_checkout.common_directory.resolve()
    return any(common_directory.is_relative_to(directory) for directory in _unguarded_directories())


def _is_exempt(relative: str | None, path_checkout: checkout.PathCheckout) -> bool:
    """Whether a path inside a checkout allows unjudged: an ignored session
    setting (PROT-38) or a path in an unguarded repository (PROT-40) -- a
    write through a symlink out of one is still judged by its target's
    checkout too (`_link_target_in_another_checkout`). The session
    setting is weighed first, so a malformed `ACO_PROTECT_UNGUARDED`
    (PROT-41) never locks the session out of the file that repairs it."""
    if relative is not None and _is_ignored_session_setting(relative, path_checkout):
        return True
    return _is_unguarded(path_checkout)


_ProtectMissDenialBuilder = Callable[[protocol.ClaimState, checkout.PathCheckout, str, str], str]


def _hook_session_agent() -> str:
    """This session's own agent, or `MISSING_HOOK_IDENTITY` raised for
    `cli`'s deny frame (PROT-08): the hook line has no `--agent` flag, so
    the sentence names only the ways a hook can be given one."""
    agent = checkout.session_agent()
    if agent is None:
        raise protocol.ClaimError(MISSING_HOOK_IDENTITY)
    return agent


def _protect_checkout_scope_denial(
    raw_path: str,
    *,
    operation: _LinkOperation,
    context: _ProtectContext,
    miss_denial: _ProtectMissDenialBuilder,
) -> str | None:
    """The one Outside-Repository/Checkout/Default-Branch/Claim-Scope chain
    every already-absolute write path runs -- a payload path's own
    (`_protect_path_denial`) and a Bash-recognized one
    (`_protect_bash_path_denial`) alike -- parameterised only by `raw_path`
    and each caller's own scope-miss sentence (issue #380 delta, gate
    finding: the two used to run two separately written copies of it).

    A path outside every repository allows before anything else is read
    (PROT-32, issue #448): the session's memory, scratchpad, and `/tmp` are
    not aco's to judge; so does an ignored session setting under `.claude/`
    (PROT-38), in any checkout, the main one included, and any path in a
    repository `ACO_PROTECT_UNGUARDED` exempts (PROT-40). A path no claim
    could ever cover denies with `rescope`'s own sentence for it before the
    store is read (PROT-14, PROT-42, PROT-43). Agent identity
    resolves last, only once a checkout, a relative scope entry, and a live
    state are in hand (PROT-08, issue #448): a write that never gets that
    far never needed an identity, so a session without one is gated only
    where a claim could answer for it.
    `miss_denial` builds each caller's own scope-miss sentence from the
    state, checkout, agent, and relative scope entry now in hand.

    A write through a symlink the path itself names into another checkout
    is judged in both checkouts, the target's first (issue #486): either
    denial denies, and the target's wins when both do, so neither
    checkout's claim answers for the other's bytes. Both checkouts'
    store-free checks run before either store or the identity is read, so
    a link in a main checkout denies "not main" without them. Once both
    pass, each checkout's claim check runs, even after the target's
    denies or fails to read its board, store, or identity. A checkout
    whose board configuration, store, or identity cannot be read denies
    with that failure in the same target-first order: the target's failure
    wins over the link's denial from the same check or a later one, and
    the link's failure yields to any denial of the target's. Only the
    check order itself overrides that: the link's store-free denial wins
    over a failure in the target's claim check, since no claim check runs
    while a store-free check denies. A target git cannot
    resolve is the target's denial too, so it denies with that failure
    before the link's own checkout is judged."""
    path_checkout = _resolved_path_checkout(raw_path, operation=operation)
    if path_checkout is None:
        return None
    link_target = _link_target_in_another_checkout(raw_path, path_checkout, operation=operation)
    judged = (link_target,) if link_target is not None else ()
    store_free_outcomes = [
        _outcome_or_failure(
            partial(_protect_store_free_outcome, judged_path, judged_checkout, context=context)
        )
        for judged_path, judged_checkout in (*judged, (raw_path, path_checkout))
    ]
    first_store_free_verdict = next(
        (outcome for outcome in store_free_outcomes if isinstance(outcome, str | Exception)),
        None,
    )
    if isinstance(first_store_free_verdict, str):
        return first_store_free_verdict
    claim_verdicts = [
        outcome
        if isinstance(outcome, Exception)
        else _outcome_or_failure(
            partial(_protect_claim_denial, outcome, context=context, miss_denial=miss_denial)
        )
        for outcome in store_free_outcomes
        if isinstance(outcome, _ClaimQuestion | Exception)
    ]
    verdict = next((verdict for verdict in claim_verdicts if verdict is not None), None)
    if isinstance(verdict, Exception):
        raise verdict
    return verdict


_CheckoutOutcome = TypeVar("_CheckoutOutcome")


def _outcome_or_failure(judge: Callable[[], _CheckoutOutcome]) -> _CheckoutOutcome | Exception:
    """`judge`'s answer for one checkout, or the failure reading that
    checkout's board, store, or identity raised: `cli`'s denial of this
    checkout alone, held back so the other checkout is still judged; which
    of the two verdicts wins follows `_protect_checkout_scope_denial`'s
    target-first order (issue #486)."""
    try:
        return judge()
    except Exception as error:
        return error


@dataclass(frozen=True, slots=True)
class _ClaimQuestion:
    """A write path the store-free checks left for a live claim to answer:
    its checkout and its repository-relative scope entry."""

    path_checkout: checkout.PathCheckout
    relative: str


def _protect_store_free_outcome(
    raw_path: str, path_checkout: checkout.PathCheckout, *, context: _ProtectContext
) -> str | _ClaimQuestion | None:
    """What `path_checkout` alone says about a write to `raw_path` before
    any store or identity is read: `None` when it is exempt, a denial when
    the checkout or the path already rules the write out, otherwise the
    `_ClaimQuestion` a live claim must answer."""
    relative = checkout.relative_scope_entry(raw_path, toplevel=path_checkout.toplevel)
    if _is_exempt(relative, path_checkout):
        return None
    checkout_denial = _protect_checkout_denial(path_checkout, context=context)
    denial = checkout_denial or checkout.unscopable_path_reason(
        raw_path, toplevel=path_checkout.toplevel
    )
    if denial is not None:
        return denial
    if relative is None:
        return PATH_REQUIRED
    return _ClaimQuestion(path_checkout=path_checkout, relative=relative)


def _protect_claim_denial(
    question: _ClaimQuestion, *, context: _ProtectContext, miss_denial: _ProtectMissDenialBuilder
) -> str | None:
    """Whether a live claim of this session covers `question`'s path,
    reading the store and then the identity."""
    state, denial = _protect_cached_claim_state_or_denial(question.path_checkout, context=context)
    if state is None:
        return denial
    agent = _hook_session_agent()
    return _protect_scope_denial(
        state,
        agent=agent,
        question=question,
        lane_shared_for=context.lane_shared_for,
        miss_denial=miss_denial(state, question.path_checkout, agent, question.relative),
    )


def _protect_path_denial(
    raw_path: str, *, distinguish_scope: bool, context: _ProtectContext
) -> str | None:
    """The deny reason for one payload path's write, or `None` to allow.

    `apply_patch` sets `distinguish_scope` (issue #252): with several paths
    in one call, the payload never told the agent which one was the problem,
    so the repair sentence must -- `claim first` when this session holds no
    live claim on the path's own checkout at all, `{path} outside claim
    scope` when it does but this path is not in it. A single-path tool call
    keeps the simpler `claim first` either way, matching its own payload's
    inability to name any other path.
    """
    if not Path(raw_path).is_absolute():
        return checkout.RELATIVE_PAYLOAD_PATH_DENIAL

    def miss_denial(
        state: protocol.ClaimState, path_checkout: checkout.PathCheckout, agent: str, relative: str
    ) -> str:
        return _protect_single_path_scope_miss_denial(
            state,
            agent=agent,
            branch=path_checkout.branch,
            relative=relative,
            distinguish_scope=distinguish_scope,
        )

    return _protect_checkout_scope_denial(
        raw_path, operation=_LinkOperation.WRITE, context=context, miss_denial=miss_denial
    )


_ProtectItem = TypeVar("_ProtectItem")


def _protect_first_denial(
    items: tuple[_ProtectItem, ...],
    *,
    context: _ProtectContext,
    denial_for: Callable[[_ProtectItem, _ProtectContext], str | None],
) -> Verdict:
    """Judges every one of `items` -- a mutating tool's own payload paths, or
    a Bash command's own recognized `(pattern, path)` pairs -- against
    `denial_for`'s own per-item chain, sharing the invocation's one
    `_ProtectContext` (and so one state-fetch cache) between all of them (gate G5: several items in
    one call can name the same repository through different worktrees, and
    re-fetching per item would let each be judged against a different
    snapshot of a store that can move between them). The first denial wins;
    naming none at all allows (issue #380 delta, gate finding: `_protect_write`
    and `_protect_bash` used to each build their own cache and run their own
    copy of this same loop instead of sharing it, risking the two drifting
    apart)."""
    for item in items:
        denial = denial_for(item, context)
        if denial is not None:
            return Verdict.deny(denial)
    return Verdict.allow()


def _protect_write(
    tool_name: str, payload: dict[str, object], *, context: _ProtectContext
) -> Verdict:
    """`protect` is forge-free (issue #245): it authorizes a write from the
    live store state alone, never a forge target, so it never resolves a
    repository or calls `gh` -- `--repo` is meaningless here and simply
    unused. Several paths in one `apply_patch` call may each sit in a
    different checkout (issue #314): each is judged in its own via
    `_protect_first_denial`, and the first denial wins."""
    raw_paths = _protect_hook_paths(tool_name, payload)
    if not raw_paths:
        return Verdict.deny(PATH_REQUIRED)
    distinguish_scope = tool_name == APPLY_PATCH_TOOL_NAME
    return _protect_first_denial(
        raw_paths,
        context=context,
        denial_for=lambda raw_path, context: _protect_path_denial(
            raw_path, distinguish_scope=distinguish_scope, context=context
        ),
    )


def _protect_bash_cwd(payload: dict[str, object]) -> str | None:
    cwd = _hook_field(payload, "cwd")
    return cwd if isinstance(cwd, str) and cwd else None


_LINK_OPERATION_BY_PATTERN = {
    hook_input.PATTERN_REMOVE: _LinkOperation.REMOVE_OR_RENAME,
    hook_input.PATTERN_MOVE: _LinkOperation.REMOVE_OR_RENAME,
}


def _protect_bash_path_denial(
    pattern: str, raw_path: str, *, context: _ProtectContext
) -> str | None:
    """The deny reason for one Bash-recognized `(pattern, raw_path)` write,
    or `None` to allow. `raw_path` already carries `hook_input`'s own
    `cwd`/`cd` resolution (issue #380 delta): a path still relative here
    means no absolute base was ever known, so this allows outright before
    ever resolving identity, a checkout, or the store (PROT-31) -- the same
    "no identity, no repository lookup at all" order a command naming no
    recognized pattern gets. Every remaining, absolute path runs
    `_protect_checkout_scope_denial`'s own shared chain, whose scope miss
    here denies naming both the recognized pattern and the path (PROT-33)
    rather than a bare `claim first`, since a command's own several paths
    need telling apart."""
    if not Path(raw_path).is_absolute():
        return None
    return _protect_checkout_scope_denial(
        raw_path,
        operation=_LINK_OPERATION_BY_PATTERN.get(pattern, _LinkOperation.WRITE),
        context=context,
        miss_denial=lambda _state, _path_checkout, _agent, relative: (
            f"{pattern} {relative} outside claim scope"
        ),
    )


def _protect_bash(payload: dict[str, object], *, context: _ProtectContext) -> Verdict:
    """`Bash`'s (and `Monitor`'s) own command-text judgment (issue #380): every
    `(pattern, path)` pair `hook_input.hook_command_paths` recognizes in
    the call's own `command` runs `_protect_bash_path_denial`'s chain via
    `_protect_first_denial`, the first denial winning. A missing or
    non-string `command`, or one naming no recognized pattern at all,
    allows outright without ever resolving identity, git, or the store --
    `protect` cannot judge what it cannot see (`specs/protect.spec.md`'s
    own `## Never`), and failing closed here would block the overwhelming
    majority of harmless shell calls."""
    tool_input = _hook_field(payload, "toolInput", "tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return Verdict.allow()
    pairs = hook_input.hook_command_paths(command, cwd=_protect_bash_cwd(payload))
    if not pairs:
        return Verdict.allow()
    return _protect_first_denial(
        pairs,
        context=context,
        denial_for=lambda pair, context: _protect_bash_path_denial(
            pair[0], pair[1], context=context
        ),
    )


def _protect_dispatch(
    effect: HookToolEffect,
    tool_name: str,
    payload: dict[str, object],
    *,
    context: _ProtectContext,
) -> Verdict:
    if effect is HookToolEffect.READ:
        return Verdict.allow()
    if effect is HookToolEffect.COMMAND_TEXT:
        return _protect_bash(payload, context=context)
    return _protect_write(tool_name, payload, context=context)


def judge(
    payload: dict[str, object] | None,
    *,
    canonical_remote_for: _CanonicalRemoteFor,
    lane_shared_for: _LaneSharedFor,
) -> Verdict:
    """`protect`'s own verdict for one already-parsed hook payload (issue
    #394): `payload` is `None` for unreadable stdin or invalid JSON (PROT-03,
    `cli`'s own concern before this ever runs); everything else -- a
    non-object payload reaching here as a plain `dict` already rules that
    out for its caller -- is judged here through to a `Verdict`. Raises
    exactly what `canonical_remote_for`, `lane_shared_for` or the store
    boundary itself raises
    (a board-configuration precondition failure, PROT-29; a store fetch
    failure, PROT-15/PROT-16); `cli`'s own `except Exception` frame (PROT-17)
    is what turns any of those, or any other uncaught exception, into a deny
    with that exception's own bare text."""
    if payload is None:
        return Verdict.deny("invalid hook payload")
    tool_name = _hook_field(payload, "toolName", "tool_name")
    if not isinstance(tool_name, str):
        return Verdict.deny("invalid hook payload")
    effect = HOOK_TOOL_EFFECTS.get(tool_name)
    if effect is None:
        return Verdict.deny(_unknown_hook_tool_reason(tool_name))
    context = _ProtectContext(
        state_cache={},
        canonical_remote_for=canonical_remote_for,
        lane_shared_for=lane_shared_for,
    )
    return _protect_dispatch(effect, tool_name, payload, context=context)
