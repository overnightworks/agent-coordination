"""The `rescope` command (`specs/rescope.spec.md`)."""

from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from . import board, board_read, body, checkout, policy, protocol, report, session, store


class _RescopeReason(StrEnum):
    """`aco rescope`'s own `--json` `reason` vocabulary (issue #406,
    `specs/rescope.spec.md`): `rescoped` the only success. `invalid_usage`
    covers a malformed `--add`/`--drop` value (a relative entry, one outside
    the checkout, a comma-bearing entry matching no versioned file, or a
    combination `protocol.combined_scope` refuses); `precondition_failed`
    covers this claim's own current state disallowing the rescope (no live
    claim on the target identity/branch, a different agent than the
    claimant, or a scope too wide for the width gate without `--whole`).
    Every other refusal -- an unresolved checkout, a missing state ref, a
    corrupted claim record -- falls to `unavailable`, matching `ask`/`rule`/
    `brief`'s own catch-all."""

    RESCOPED = "rescoped"
    PRECONDITION_FAILED = "precondition_failed"
    INVALID_USAGE = "invalid_usage"
    UNAVAILABLE = "unavailable"


def _rescope_json(claimed: protocol.ActiveClaim) -> None:
    report.emit_json(True, _RescopeReason.RESCOPED, **protocol.claim_identity_fields(claimed))


@dataclass(frozen=True)
class _RescopePaths:
    """`rescope`'s own `--add`/`--drop` entries, each made absolute (issue
    #554): an absolute entry as given, a relative one joined to the toplevel
    of the checkout the command runs in -- the repository-relative form an
    item body's own `scope` list prints, so an entry copied from it works as
    typed from anywhere inside that checkout."""

    add: tuple[str, ...]
    drop: tuple[str, ...]

    def named(self) -> tuple[tuple[str, str], ...]:
        """Every entry beside the flag that named it, `--add` first."""
        return (
            *(("--add", path) for path in self.add),
            *(("--drop", path) for path in self.drop),
        )


def _rescope_paths(add: list[str] | None, drop: list[str] | None) -> _RescopePaths:
    return _RescopePaths(
        add=_absolute_rescope_entries(add, flag="--add"),
        drop=_absolute_rescope_entries(drop, flag="--drop"),
    )


def _absolute_rescope_entries(raw_paths: list[str] | None, *, flag: str) -> tuple[str, ...]:
    return tuple(
        raw_path if Path(raw_path).is_absolute() else _joined_to_run_checkout(raw_path, flag=flag)
        for raw_path in raw_paths or ()
    )


def _joined_to_run_checkout(raw_path: str, *, flag: str) -> str:
    """A relative entry joined to the run checkout's toplevel. One that
    climbs out of that checkout refuses here (RESC-05), naming the entry as
    typed: the joined path would otherwise locate whichever checkout it
    lands in and be judged there instead."""
    toplevel = _run_checkout_toplevel(raw_path, flag=flag)
    joined = toplevel / raw_path
    if checkout.checkout_relative(joined, toplevel=toplevel) is None:
        raise _RescopeInvalidUsageError(_outside_checkout_reason(flag, raw_path, toplevel))
    return str(joined)


def _run_checkout_toplevel(raw_path: str, *, flag: str) -> Path:
    """The toplevel a relative `raw_path` is read against: the checkout the
    command runs in. Outside every repository there is none, and the
    refusal names the entry and how to give it instead."""
    cwd = Path.cwd()
    run_checkout = checkout.resolve_named_path_checkout(cwd)
    if run_checkout is None:
        raise _RescopeInvalidUsageError(
            f"{flag} path {raw_path!r} is relative and {cwd} is "
            f"{checkout.NOT_IN_A_REPOSITORY_REASON}; pass it as an absolute path"
        )
    return run_checkout.toplevel


def _rescope_scope_entries(
    absolute_paths: tuple[str, ...], *, toplevel: Path, flag: str
) -> tuple[str, ...]:
    """One `--add`/`--drop` list, already absolute (`_RescopePaths`),
    canonicalized to repository-relative scope entries against `toplevel`.
    A path no claim could ever cover refuses with the sentence `protect`
    denies it with (issue #483)."""
    if not absolute_paths:
        return ()
    canonical: list[str] = []
    for raw_path in absolute_paths:
        unscopable = checkout.unscopable_path_reason(raw_path, toplevel=toplevel)
        if unscopable is not None:
            raise _RescopeInvalidUsageError(unscopable)
        relative = checkout.relative_scope_entry(raw_path, toplevel=toplevel)
        if relative is None:
            raise _RescopeInvalidUsageError(_outside_checkout_reason(flag, raw_path, toplevel))
        canonical.append(relative)
    return protocol.valid_scope(canonical)


def _outside_checkout_reason(flag: str, path: str, toplevel: Path) -> str:
    return f"{flag} path {path!r} is outside the resolved checkout {toplevel}"


def _rescope_checkout(paths: _RescopePaths) -> checkout.PathCheckout:
    """`rescope`'s checkout, resolved from its first entry whenever one
    names a location (issue #314), else from the process's own cwd, read
    through the same path-based resolver `protect` uses. A checkout with no
    commit yet denies here too (gate G3), the same precondition `protect`
    enforces on its own resolved checkout. The path resolves exactly as
    `protect` resolves a payload path -- a checkout root as its own
    checkout, a file in a directory not created yet from its nearest
    existing ancestor (issues #474, #483)."""
    named = paths.named()
    location = Path(named[0][1]) if named else Path.cwd()
    path_checkout = checkout.resolve_named_path_checkout(location)
    if path_checkout is None:
        raise protocol.ClaimUnavailableError(_outside_every_repository_reason(named))
    if not path_checkout.has_commit:
        raise protocol.ClaimUnavailableError(checkout.NO_COMMIT_CHECKOUT_REASON)
    return path_checkout


def _outside_every_repository_reason(named: tuple[tuple[str, str], ...]) -> str:
    """PROT-10's own sentence, naming the entry that located no checkout
    whenever an entry did the locating (issue #554)."""
    if not named:
        return checkout.NOT_IN_A_REPOSITORY_REASON
    flag, path = named[0]
    return f"{flag} path {path!r} is {checkout.NOT_IN_A_REPOSITORY_REASON}"


def _rescope_command(
    parsed: argparse.Namespace,
    paths: _RescopePaths,
    path_checkout: checkout.PathCheckout,
    checkout_context: session.RunContext,
) -> protocol.RescopeRequest:
    """The rescope `parsed` asks for in `path_checkout`, judged against the
    resolved checkout's own recorded default branch (issue #490)."""
    branch = path_checkout.branch
    if not branch:
        raise protocol.ClaimUnavailableError(
            "rescope requires a non-empty current branch; "
            "check out the claim branch, or pass an issue number"
        )
    checkout.refuse_shared_checkout(
        path_checkout,
        default_branch=checkout_context.recorded_default_branch,
        canonical_remote=checkout_context.canonical_remote,
        repair=checkout.WorktreeRepair.RETURN_TO_CLAIM,
    )
    identity = protocol.lookup_identity(
        protocol.optional_issue_number(parsed.issue),
        branch,
        holders=lambda: protocol.claims_on_branch(checkout_context.observation.claims, branch),
    )
    return protocol.RescopeRequest(
        identity=identity,
        agent=parsed.agent,
        add=_rescope_scope_entries(paths.add, toplevel=path_checkout.toplevel, flag="--add"),
        drop=_rescope_scope_entries(paths.drop, toplevel=path_checkout.toplevel, flag="--drop"),
        claim_id=parsed.claim_id,
        branch=branch,
        whole_reason=None if parsed.whole is None else body.bounded_whole_reason(parsed.whole),
    )


class _RescopeInvalidUsageError(protocol.ClaimError):
    """A rescope `--add`/`--drop` value itself is malformed or contradictory
    -- relative, outside the checkout, a comma-bearing entry matching no
    versioned file, or a combination `protocol.combined_scope` refuses
    (issue #406) -- named so `--json` can choose `invalid_usage` over the
    broad `unavailable` every checkout- or store-level refusal falls to."""


class _RescopePreconditionError(protocol.ClaimError):
    """The rescope cannot proceed given this claim's own current state: no
    live claim on the target identity/branch, a different agent than the
    one holding it, or a scope too wide for the width gate without
    `--whole` (issue #406) -- named so `--json` can choose
    `precondition_failed`."""


_RESCOPE_COMMAND = "rescope"


@dataclass(frozen=True)
class _RescopedItemBody:
    """The item body a rescope keeps in step with its claim (issue #554):
    the body as read, its located `aco` block, and the `whole`
    reason the block names."""

    number: int
    text: str
    located: body.LocatedBlock
    whole: str | None


def _rescoped_item_body(
    context: session.RunContext, identity: protocol.ClaimIdentity
) -> _RescopedItemBody | None:
    """The claimed item's own body, read before any write so an item whose
    body cannot take the new scope refuses with nothing changed; `None` for
    a lane claim, which names no item."""
    if not isinstance(identity, protocol.IssueIdentity):
        return None
    client = context.forge_writer
    storage = context.config.storage
    number = identity.issue
    try:
        board_read.require_update_item_body(client, command=_RESCOPE_COMMAND)
        text = board_read.item_body_or_refuse(
            client, number, command=_RESCOPE_COMMAND, storage=storage
        )
        located = board.located_block_or_refuse(
            number, text, command=_RESCOPE_COMMAND, storage=storage
        )
    except protocol.ClaimError as error:
        raise _RescopePreconditionError(str(error)) from error
    whole = body.parse_body(text, storage=storage).whole
    return _RescopedItemBody(number=number, text=text, located=located, whole=whole)


def _write_rescoped_body(
    context: session.RunContext, item: _RescopedItemBody, scope: tuple[str, ...], whole: str | None
) -> None:
    """The body's own `scope` set to the claim's new one, and `--whole`
    written beside it when the call names one."""
    new_data: dict[str, object] = {**item.located.data, "scope": list(scope)}
    if whole is not None:
        new_data["whole"] = whole
    context.forge_writer.update_item_body(
        item.number, body.replace_block(item.text, item.located, new_data)
    )


def _rescope_item_and_claim(
    context: session.RunContext,
    subject: store.ClaimTransitionSubject,
    intent: protocol.RescopeIntent,
    item: _RescopedItemBody,
    whole: str | None,
) -> protocol.ClaimState:
    """The body first, then the claim's own transition (issue #554): a
    refused body write leaves the claim as it stood, and a claim write that
    fails after it says the body already names the new scope and how to
    bring the claim in step -- the body write is not undone, and the same
    rescope again writes the same body and the claim."""
    _write_rescoped_body(context, item, intent.scope, whole)
    try:
        return context.transition(subject, intent)
    except protocol.ClaimError as error:
        label = board.item_label(item.number, context.config.storage)
        raise protocol.ClaimUnavailableError(
            f"{label} body scope now reads {list(intent.scope)}, but the claim was not "
            f"rescoped: {error}; run the same rescope again"
        ) from error


def _rescope_write(parsed: argparse.Namespace, run_context: session.RunContext) -> int:
    paths = _rescope_paths(parsed.add, parsed.drop)
    path_checkout = _rescope_checkout(paths)
    checkout_context = run_context.for_directory(path_checkout.toplevel, is_toplevel=True)
    requested = _rescope_command(parsed, paths, path_checkout, checkout_context)
    worktree = checkout_context.toplevel
    observed = checkout_context.observation
    protocol.require_state_ref(observed)
    try:
        selected = board.selected_store_claim(
            observed,
            requested.identity,
            requested.branch,
            requested.claim_id,
            checkout_context.config.storage,
        )
    except protocol.ClaimError as error:
        raise _RescopePreconditionError(str(error)) from error
    if requested.agent != selected.agent:
        raise _RescopePreconditionError(
            "only the original claimant may rescope "
            f"(holder={protocol.claimant_text(selected.agent, selected.role)!r}, "
            f"this session={protocol.claimant_text(requested.agent, selected.role)!r})"
        )
    versioned = checkout.versioned_paths(directory=worktree)
    try:
        policy.reject_ungrounded_comma_scope(requested.add, versioned, flag="--add")
        combined = protocol.combined_scope(selected.scope, requested.add, requested.drop)
    except protocol.ClaimError as error:
        raise _RescopeInvalidUsageError(str(error)) from error
    item = _rescoped_item_body(checkout_context, selected.identity)
    try:
        _n, _total, _share, whole_reason = policy.reject_wide_scope(
            combined,
            versioned,
            requested.whole_reason or selected.whole_reason,
            directories=checkout.scope_directories(
                combined,
                directory=checkout_context.directory,
                toplevel=lambda: checkout_context.toplevel,
            ),
            whole_from_body=None if item is None else lambda: item.whole,
        )
    except protocol.ClaimError as error:
        raise _RescopePreconditionError(str(error)) from error
    intent = protocol.RescopeIntent(
        claim_id=selected.claim_id,
        agent=requested.agent,
        role=selected.role,
        scope=combined,
        operation_id=uuid.uuid4().hex,
        whole_reason=whole_reason,
    )
    subject = store.transition_subject(_RESCOPE_COMMAND, selected.identity, selected.branch)
    new_state = (
        checkout_context.transition(subject, intent)
        if item is None
        else _rescope_item_and_claim(
            checkout_context, subject, intent, item, requested.whole_reason
        )
    )
    rescoped = new_state.claims[protocol.claim_key(selected.identity, selected.branch)]
    if parsed.json:
        _rescope_json(rescoped)
        return 0
    subject = board.claim_subject(rescoped, checkout_context.config.storage)
    print(f"RESCOPED {subject}: {rescoped.claim_id}")
    return 0


def run(parsed: argparse.Namespace, context: session.RunContext) -> int:
    """`rescope`'s own `--json` refusals (issue #406, `_RescopeReason`):
    `_rescope_write`'s two typed exceptions choose `invalid_usage`/
    `precondition_failed`; every other `protocol.ClaimError` -- an
    unresolved checkout, a missing state ref, a corrupted claim record --
    falls to `unavailable`, matching `ask`/`rule`/`brief`'s own catch-all."""
    as_json = parsed.json
    try:
        return _rescope_write(parsed, context)
    except _RescopeInvalidUsageError as error:
        return report.refuse(_RescopeReason.INVALID_USAGE, error, as_json=as_json)
    except _RescopePreconditionError as error:
        return report.refuse(_RescopeReason.PRECONDITION_FAILED, error, as_json=as_json)
    except protocol.ClaimError as error:
        return report.refuse(_RescopeReason.UNAVAILABLE, error, as_json=as_json)
