"""The `claim` command (`specs/claim.spec.md`)."""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from . import (
    board,
    board_read,
    body,
    checkout,
    forge,
    items,
    policy,
    protocol,
    report,
    session,
    store,
)

DEFAULT_CLAIM_ROLE = "builder"


def _touch_line(
    own_scope: tuple[str, ...], claim: protocol.ScopedClaim, storage: body.Storage
) -> str:
    """One overlapping claim, named with the paths where its scope meets
    `own_scope` -- the fact a claimant needs to know they hold both scopes
    at once, not only the other item's name (issue #206)."""
    meeting = protocol.scope_overlap_paths(own_scope, claim.scope)
    return f"{board.claim_subject(claim, storage)} on {protocol.named_with_overflow_count(meeting)}"


def _touch_summary(
    own_scope: tuple[str, ...], touches: tuple[protocol.ScopedClaim, ...], storage: body.Storage
) -> str:
    if not touches:
        return "overlaps no other open claims"
    return "overlaps " + ", ".join(_touch_line(own_scope, claim, storage) for claim in touches)


def claim_cost_line(
    n: int,
    total: int,
    own_scope: tuple[str, ...],
    touches: tuple[protocol.ScopedClaim, ...],
    storage: body.Storage,
) -> str:
    percent = 0 if total == 0 else round(100 * n / total)
    return (
        f"{n} of {total} versioned files ({percent}%); "
        f"{_touch_summary(own_scope, touches, storage)}"
    )


def _resolved_claim_branch(arguments: argparse.Namespace, *, directory: Path | None = None) -> str:
    """`claim`'s own branch: `--branch` when given, else `directory`'s
    checked-out branch, read via `-C` when given (issue #322: `start`'s own
    resolved worktree, never a process-wide `os.chdir`) or the calling
    process's own cwd otherwise -- the one resolution `_request` and
    `run` (issue #337, which needs it before `_request` builds a full
    request) both bind to, so it stays a single owner rather than two copies
    of the same git call and validation."""
    branch = (
        checkout.attached_branch(checkout.current_branch(directory=directory))
        if arguments.branch is None
        else arguments.branch
    )
    return protocol.valid_branch({"branch": branch})


def _request(
    arguments: argparse.Namespace,
    *,
    default_branch: Callable[[], str | None],
    directory: Path | None = None,
) -> protocol.ClaimRequest:
    """`claim_request`, checked against the checkout it is made in:
    `claim` stands in the worktree it claims, judged against the canonical
    remote's recorded default branch `default_branch` answers (issue
    #490)."""
    request = claim_request(arguments, directory=directory)
    checkout.validate_checkout(request, default_branch=default_branch, directory=directory)
    return request


def claim_request(
    arguments: argparse.Namespace, *, directory: Path | None = None
) -> protocol.ClaimRequest:
    """The `protocol.ClaimRequest` `claim` submits, `arguments.scope` bound as-is
    when given. Omitted -- issue mode only (issue #337); lane mode still
    refuses it, `checked_claim`'s own first check -- it binds the empty
    tuple instead of raising: `checked_claim` replaces it with the item's
    own body scope, or a live claim's stored scope on replay, before this
    request's scope ever reaches a wide-scope check or a write. With
    `--base` and `--branch` given it reads no checkout at all (issue #479:
    `start` checks its claim before the worktree exists)."""
    agent = protocol.outbound_text(checkout.resolved_agent(arguments.agent), "agent", maximum=128)
    role = protocol.outbound_text(arguments.role, "role", maximum=64)
    base = (
        checkout.git_output(["rev-parse", "HEAD"], directory=directory)
        if arguments.base is None
        else arguments.base
    )
    if protocol.COMMIT_PATTERN.fullmatch(base) is None:
        raise protocol.ClaimError("base must be a full lowercase commit SHA")
    branch = _resolved_claim_branch(arguments, directory=directory)
    issue = protocol.optional_issue_number(arguments.issue)
    identity = protocol.resolved_identity(issue, branch)
    claim_id = arguments.claim_id or uuid.uuid4().hex
    protocol.ClaimId(claim_id)
    whole_reason = None if arguments.whole is None else body.bounded_whole_reason(arguments.whole)
    resource = getattr(arguments, "resource", None)
    if resource is not None:
        resource = protocol.outbound_resource_name(resource)
    return protocol.ClaimRequest(
        identity=identity,
        agent=agent,
        role=role,
        base=base,
        branch=branch,
        scope=() if arguments.scope is None else protocol.valid_scope(arguments.scope),
        claim_id=claim_id,
        out_of_order_reason=arguments.out_of_order,
        whole_reason=whole_reason,
        resource=resource,
    )


@dataclass(frozen=True)
class ScopeVersioning:
    """How much of the claimed scope's Git history the checkout already has,
    for the `--json` claim payload's `versioned_files`/`share` fields."""

    versioned_files: int
    versioned_files_total: int
    share: float


class ClaimReason(StrEnum):
    """`aco claim`'s own `--json` `reason` vocabulary (issue #406,
    `specs/claim.spec.md`): `claimed` the only success. `precondition_failed`
    covers the shared `checks` array `refuse_claim` reports (out-of-order,
    blocked, container, closed/missing, body-incomplete, missing-parent --
    CLM-08..14). `target_invalid` and `body_invalid` are the two failures a
    scope actually being *derived* from an item's own body can hit before
    those checks ever run: the item itself is unusable (missing, a pull
    request), or its body cannot supply or confirm a scope (a malformed
    `aco` block, no `scope` field, or one differing from an explicit
    `--scope`). `claim_conflict` is `protocol.apply()`'s own single failure surface
    for a claim write -- identity already claimed, claim id already
    consumed, or a resource conflict or format issue -- never split further
    here. Every other refusal -- a checkout precondition, scope grammar, an
    unsafe branch or claim id -- falls to `unavailable`, matching `ask`/
    `rule`/`brief`'s own catch-all."""

    CLAIMED = "claimed"
    PRECONDITION_FAILED = "precondition_failed"
    TARGET_INVALID = "target_invalid"
    BODY_INVALID = "body_invalid"
    CLAIM_CONFLICT = "claim_conflict"
    INVALID_USAGE = "invalid_usage"
    UNAVAILABLE = "unavailable"


def _claim_json(
    claimed: protocol.ActiveClaim,
    *,
    versioning: ScopeVersioning,
    touches: tuple[protocol.ActiveClaim, ...],
    checks: tuple[_SliceCheck, ...],
) -> int:
    report.emit_json(
        True,
        ClaimReason.CLAIMED,
        **protocol.claim_identity_fields(claimed),
        **protocol.resource_fields(claimed.resource),
        versioned_files=versioning.versioned_files,
        versioned_files_total=versioning.versioned_files_total,
        share=versioning.share,
        touches=[protocol.touch_json(claim) for claim in touches],
        checks=[check.as_json() for check in checks],
    )
    return 0


@dataclass(frozen=True)
class _SliceCheck:
    """One slice-rule finding — the `check` table `#79` rules.

    `slice`/`issue` carry whichever numbers the message names, so a `--json`
    caller can act on the finding without re-parsing `text`; either is
    `None` when the check has nothing of that kind to name.
    """

    level: str
    check: str
    text: str
    slice: int | None = None
    issue: int | None = None

    def render(self) -> str:
        prefix = "ERROR" if self.level == "error" else "WARNING"
        return f"{prefix}: {board.terminal_text(self.text)}"

    def as_json(self) -> dict[str, object]:
        return {
            "level": self.level,
            "check": self.check,
            "text": self.text,
            "slice": self.slice,
            "issue": self.issue,
        }


def _issue_reference_state(
    client: forge.ForgeReader,
    open_by_number: dict[int, board.Issue],
    number: int,
) -> tuple[forge.ItemState, str | None, str | None]:
    open_issue = open_by_number.get(number)
    if open_issue is not None:
        return forge.ItemState.OPEN, open_issue.title, open_issue.body
    reference = board_read.fetch_issue_reference(client, number)
    return reference.state, reference.title, reference.body


def _out_of_order_check(
    projected: board.Board,
    issue: int | None,
    out_of_order_reason: str | None,
    storage: body.Storage,
) -> _SliceCheck | None:
    highest = board.highest_scored_actionable(projected)
    if highest is None or issue is None:
        return None
    claimed_item = next((item for item in projected.items if item.number == issue), None)
    if claimed_item is None or board.board_rank(highest) >= board.board_rank(claimed_item):
        return None
    return _SliceCheck(
        "warning" if out_of_order_reason is not None else "error",
        "out-of-order",
        f"higher-priority actionable item {board.item_label(highest.number, storage)} "
        f"(score {highest.score}) is free: {highest.title}; "
        "use --out-of-order REASON to proceed",
        issue=highest.number,
    )


def _blocked_check(
    item: board.BoardItem | None,
    out_of_order_reason: str | None,
    repository: str,
    storage: body.Storage,
) -> _SliceCheck | None:
    if item is None or not item.open_blockers:
        return None
    blockers = ", ".join(
        board.open_blocker_label(reference, repository, storage) for reference in item.open_blockers
    )
    return _SliceCheck(
        "warning" if out_of_order_reason is not None else "error",
        "blocked",
        f"{board.item_label(item.number, storage)} is blocked by {blockers} (open); "
        "pass --out-of-order REASON to claim it anyway",
        issue=item.number,
    )


def _parent_checks(
    client: forge.ForgeReader, repository: str, issue: int, title: str, storage: body.Storage
) -> _SliceCheck | None:
    """Warn when a slice-shaped title names a parent GitHub does not record as one."""
    match = board.slice_title_match(title)
    if match is None:
        return None
    slice_number, parent_issue = match
    if board.names_no_item(parent_issue, storage):
        return None
    parent = client.parent_issue(issue)
    if parent is not None and parent.reference == board.IssueReference(repository, parent_issue):
        return None
    parent_label = board.item_label(parent_issue, storage)
    return _SliceCheck(
        "warning",
        "missing-parent",
        f"looks like slice {slice_number} of {parent_label} but is no sub-issue "
        f"of {parent_label}; the parent inherits nothing",
        slice=slice_number,
        issue=parent_issue,
    )


def _malformed_checks(item: board.BoardItem) -> tuple[_SliceCheck, ...] | None:
    """The one refusal a malformed block body gets (#150) -- every other
    body-contract check (blocker state, completeness) never runs, since
    neither the parsed projections nor the blocker set can be trusted once
    the body itself failed to read."""
    if item.read_state is body.BodyReadState.MALFORMED:
        return tuple(
            _SliceCheck("error", "body-contract", body.body_defect_text(defect))
            for defect in item.contract.defects
        )
    return None


def _body_contract_checks(item: board.BoardItem, storage: body.Storage) -> tuple[_SliceCheck, ...]:
    malformed = _malformed_checks(item)
    if malformed is not None:
        return malformed
    contract = item.contract
    checks = [_SliceCheck("error", "body-contract", defect.message) for defect in contract.defects]
    # Read the two atomic facts directly rather than `item.actionable_reason`:
    # that reason is the *first* one `board._actionable_reason` finds (frozen,
    # claimed, blocked, then incomplete), so an item that is both blocked and
    # incomplete would report only "blocked" there -- masking the incomplete
    # body this check exists to name. A freshly `cut` child (an incomplete
    # but defect-free skeleton) is refused here exactly as it is invisible to
    # `next`, regardless of what else may also be true of it.
    if not item.contract_complete and not item.projectionless_idea:
        missing = ", ".join(body.missing_or_empty_sections(contract))
        checks.append(
            _SliceCheck(
                "error",
                "body-incomplete",
                f"{board.item_label(item.number, storage)} body incomplete: {missing}",
                issue=item.number,
            )
        )
    return tuple(checks)


@dataclass(frozen=True)
class _BoardReferenceLookup:
    """The board client, its repository, and the currently open issues it can
    resolve `#reference`s against — what every cross-issue slice/parent check
    below needs to look a referenced issue up."""

    client: forge.ForgeReader
    repository: str
    open_by_number: dict[int, board.Issue]


def _slice_rule_checks(
    lookup: _BoardReferenceLookup,
    issue: int,
    projected: board.Board,
    out_of_order_reason: str | None,
    storage: body.Storage,
) -> tuple[_SliceCheck, ...]:
    checks: list[_SliceCheck] = []
    out_of_order = _out_of_order_check(projected, issue, out_of_order_reason, storage)
    if out_of_order is not None:
        checks.append(out_of_order)
    label = board.item_label(issue, storage)
    item = next((item for item in projected.items if item.number == issue), None)
    if item is not None and item.kind is body.ItemKind.CONTAINER:
        checks.append(
            _SliceCheck("error", "container", f"{label} is a container; claim a child", issue=issue)
        )
    blocked = _blocked_check(item, out_of_order_reason, lookup.repository, storage)
    if blocked is not None:
        checks.append(blocked)
    state, title, _body = _issue_reference_state(lookup.client, lookup.open_by_number, issue)
    if state is forge.ItemState.CLOSED:
        checks.append(_SliceCheck("error", "closed-issue", f"issue {label} is closed", issue=issue))
    elif state is forge.ItemState.MISSING:
        checks.append(
            _SliceCheck("error", "missing-issue", f"issue {label} does not exist here", issue=issue)
        )
    if item is not None:
        checks.extend(_body_contract_checks(item, storage))
    if title is not None:
        parent_check = _parent_checks(lookup.client, lookup.repository, issue, title, storage)
        if parent_check is not None:
            checks.append(parent_check)
    return tuple(checks)


def refuse_claim(as_json: bool, issue: int | None, checks: tuple[_SliceCheck, ...]) -> None:
    if as_json:
        report.emit_json(
            False,
            ClaimReason.PRECONDITION_FAILED,
            issue=issue,
            checks=[c.as_json() for c in checks],
        )
        return
    for check in checks:
        print(check.render(), file=sys.stderr)


def _claim_intent_from_request(
    request: protocol.ClaimRequest, operation_id: str, item_pin: protocol.ItemPin | None
) -> protocol.ClaimIntent:
    """`_request`'s validated `protocol.ClaimRequest`, converted to the store's own
    intent (issue #176, §1: `protocol.ClaimRequest` stays the CLI-facing input,
    `protocol.ClaimIntent` is what `protocol.apply` actually consumes), pinned to `item_pin`
    when the claim's checks judged a state-ref item open (issue #496)."""
    resource_name = None
    resource_value = None
    if request.resource is not None:
        resource_name = request.resource
        resource_value = request.resource_value
    return protocol.ClaimIntent(
        identity=request.identity,
        agent=request.agent,
        role=request.role,
        base=protocol.ObjectId(request.base),
        branch=request.branch,
        scope=request.scope,
        claim_id=protocol.ClaimId(request.claim_id),
        operation_id=operation_id,
        whole_reason=request.whole_reason,
        resource_name=resource_name,
        resource_value=resource_value,
        item_pin=item_pin,
    )


def _claimed_item_pin(
    identity: protocol.ClaimIdentity, observed: protocol.ClaimState, storage: body.Storage
) -> protocol.ItemPin | None:
    """The state-ref item a claim's checks judged open, pinned to its blob in
    `observed`, the observation those checks read (issue #496). `None` for a
    lane, and under `github`, whose forge holds the item's state while the
    ledger holds no fact of it; `None` too for an item `observed` lacks,
    which the checks already refuse as missing."""
    if storage is not body.Storage.STATE_REF or not isinstance(identity, protocol.IssueIdentity):
        return None
    item_id = items.format_item_id(identity.issue)
    oid = observed.items.get(item_id)
    return None if oid is None else protocol.ItemPin(item_id, oid)


def _matching_store_claim(
    observed: protocol.ClaimState, request: protocol.ClaimRequest
) -> protocol.ActiveClaim | None:
    """The live store claim an interrupted, replayed `claim` invocation may
    reuse (criterion 2): same identity, agent, role, branch, scope, *and*
    claim id -- `protocol.apply` itself only replays on an exact claim-id match
    (§1 "Retry of an interrupted identical request"), so a CLI-level replay
    check that skips this slice's dispatch rules must use the same test, not
    the ledger's looser field-only match. Issueless lanes keep today's
    one-claim-per-branch contract; only a numbered item gets replay
    detection at all.
    """
    if not isinstance(request.identity, protocol.IssueIdentity):
        return None
    existing = observed.claims.get(protocol.claim_key(request.identity, request.branch))
    if (
        existing is not None
        and existing.claim_id == request.claim_id
        and existing.agent == request.agent
        and existing.role == request.role
        and existing.branch == request.branch
        and existing.scope == request.scope
    ):
        return existing
    return None


_LANE_CLAIM_SCOPE_REQUIRED = "lane claim requires --scope; a lane names no item to derive it from"


_CLAIM_SCOPE_MISSING = "item names no scope; pass --scope"


_CLAIM_SCOPE_MISMATCH = "claim scope differs from the item's scope; correct the item first"


class _ClaimTargetInvalidError(protocol.ClaimError):
    """The item a claim's scope is being *derived* from is unusable --
    missing, or a pull request -- discovered while reading its body, before
    any slice-rule check ever runs (issue #406). Named so `--json` can
    choose `target_invalid` over the broad `unavailable` every other claim
    refusal falls to."""


class _ClaimBodyInvalidError(protocol.ClaimError):
    """The item a claim's scope is being *derived* from has a body that
    cannot supply or confirm one -- a malformed `aco` block (issue
    #310 finding 43: named *before* "item names no scope", reusing the same
    block-defect reader `body --check` uses), no `scope` field at all, or
    one differing from an explicit `--scope` (issue #406). Named so
    `--json` can choose `body_invalid`. Also covers CLM-37: a malformed
    body on the whole fallback for an explicit or replayed wide scope."""


def _item_target_body(
    client: forge.ForgeReader,
    open_by_number: Mapping[int, board.Issue],
    number: int,
    storage: body.Storage,
) -> str:
    """The item's own body for `_item_scope`/`_item_whole` (issue #337):
    from `open_by_number` -- the open-board listing `claim` needs anyway
    for its slice-rule checks -- when the target is open, so a derived
    scope costs no separate body read; a closed, missing, or pull-request
    target never appears there, and falls back to the one single-item
    lookup `_issue_reference_state` uses for the same reason (issue #245).
    `board_read.item_body_or_refuse`'s own `protocol.ClaimUnavailableError` -- missing or a
    pull request, CLM-24's own two conditions -- becomes
    `_ClaimTargetInvalidError` by name (issue #406); a forge outage or
    malformed response (`forge.ForgeError`, raised by `client.item_reference`
    itself) is a different failure and passes through unwrapped to
    `run`'s `unavailable` catch-all (CLM-27)."""
    issue = open_by_number.get(number)
    if issue is not None:
        return issue.body
    try:
        return board_read.item_body_or_refuse(client, number, command="claim", storage=storage)
    except protocol.ClaimUnavailableError as error:
        raise _ClaimTargetInvalidError(str(error)) from error


def _read_item_body(
    client: forge.ForgeReader,
    open_by_number: Mapping[int, board.Issue],
    number: int,
    *,
    storage: body.Storage,
) -> body.ParsedBody:
    """Scope and whole readers refuse the target's first body defect."""
    raw_body = _item_target_body(client, open_by_number, number, storage)
    parsed = body.parse_body(raw_body, storage=storage)
    if parsed.read_state is body.BodyReadState.MALFORMED:
        defect = parsed.contract.defects[0]
        raise _ClaimBodyInvalidError(
            f"{board.item_label(number, storage)} {body.body_defect_text(defect)}"
        )
    return parsed


def _item_scope(
    client: forge.ForgeReader,
    open_by_number: Mapping[int, board.Issue],
    number: int,
    *,
    storage: body.Storage,
) -> tuple[str, ...] | None:
    return _read_item_body(client, open_by_number, number, storage=storage).scope


def _item_whole(
    client: forge.ForgeReader,
    open_by_number: Mapping[int, board.Issue],
    number: int,
    *,
    storage: body.Storage,
) -> str | None:
    return _read_item_body(client, open_by_number, number, storage=storage).whole


def _whole_from_item_body(
    context: session.RunContext,
    identity: protocol.ClaimIdentity,
    *,
    open_by_number: Mapping[int, board.Issue] | None,
) -> Callable[[], str | None] | None:
    """Read only the target when a wide claim has no explicit reason.

    Lane claims have no body fallback. Narrow replays and live start resumes
    avoid this read; a fallback that does read shares the scope reader's
    malformed-body refusal. Reuse the board listing when already available.
    """
    if not isinstance(identity, protocol.IssueIdentity):
        return None
    number = identity.issue

    def resolve() -> str | None:
        client = context.forge
        storage = context.config.storage
        return _item_whole(client, open_by_number or {}, number, storage=storage)

    return resolve


def _resolved_claim_request(
    requested: protocol.ClaimRequest,
    observed: protocol.ClaimState,
    context: session.RunContext,
    storage: body.Storage,
) -> tuple[protocol.ClaimRequest, dict[int, board.Issue] | None]:
    """`requested` with its scope filled in for an issue-mode claim that
    omitted `--scope` (issue #337), paired with the open-board listing that
    filling it cost -- `None` when it cost nothing, so `run` never
    fetches that board a second time for its own slice-rule checks.

    A live claim already on this identity and branch is a replayed,
    interrupted request even when the retry drops `--scope`: its own stored
    scope is taken outright, with no forge call and no body read at all, so
    scope resolution never refuses merely because the body changed, or lost
    its scope, since the original claim was opened. CLM-37's later whole
    fallback still refuses a malformed body for a wide replay without `--whole`.
    `_matching_store_claim`
    is guaranteed `None` for an identity with no live claim at all, so this
    is the only place that needs to look. Otherwise the item's own body
    `scope = [...]` is the source of truth, fetched from the same open board
    `run` needs anyway for its slice-rule checks, so the caller reuses
    it rather than asking again."""
    identity = requested.identity
    assert isinstance(identity, protocol.IssueIdentity)
    live = observed.claims.get(protocol.claim_key(identity, requested.branch))
    if live is not None:
        return replace(requested, scope=live.scope), None
    client = context.forge
    open_by_number = {issue.number: issue for issue in client.list_open_board_issues()}
    item_scope = _item_scope(client, open_by_number, identity.issue, storage=storage)
    if item_scope is None:
        raise _ClaimBodyInvalidError(_CLAIM_SCOPE_MISSING)
    return replace(requested, scope=item_scope), open_by_number


def _reject_scope_mismatch(
    open_by_number: Mapping[int, board.Issue],
    target_issue: int,
    scope: tuple[str, ...],
    storage: body.Storage,
) -> None:
    """Refuses an explicit `--scope` whose canonical set differs from the
    target item's own body scope (issue #337) -- reusing the open board
    `run` fetches once, whether for a derived scope or for its
    slice-rule checks, so this costs no second forge read. A target item
    outside the fetched open list, or one with no `scope` field of its own,
    has nothing to differ from and is silently accepted, exactly as before
    this field existed. A derived scope always came from this same body, so
    the comparison here is trivially satisfied -- never an extra read, just
    an inexpensive no-op check."""
    issue = open_by_number.get(target_issue)
    if issue is None:
        return
    item_scope = body.parse_body(issue.body, storage=storage).scope
    if item_scope is not None and item_scope != scope:
        raise _ClaimBodyInvalidError(_CLAIM_SCOPE_MISMATCH)


def scope_versioning(
    scope: tuple[str, ...],
    whole_reason: str | None,
    *,
    context: session.RunContext,
    whole_from_body: Callable[[], str | None] | None = None,
    revision: str | None = None,
) -> tuple[ScopeVersioning, str | None]:
    """`claim`'s local, forge-free scope checks (issue #207's comma guard,
    the wide-scope width gate) against the real checkout, run once the
    requested scope is final -- whether it came from `--scope` or was
    derived from the item's own body. Read from `context`'s directory via
    `-C` when it names one (issue #322: `start`'s own resolved worktree,
    never a process-wide `os.chdir`) or the calling process's own cwd
    otherwise. The second element is the effective `whole_reason` the width
    gate actually admitted the scope with (issue #399): `whole_reason`
    itself, or `whole_from_body()`'s own result when the gate tripped and
    needed it -- the caller's own claim persists this, not the raw
    `whole_reason` it passed in. Given `revision`, the scope is measured
    against that commit's tree instead of the checkout (issue #479:
    `start`'s fetched trunk, before any worktree of it exists)."""
    versioned = checkout.versioned_paths(directory=context.directory, revision=revision)
    policy.reject_ungrounded_comma_scope(scope, versioned, flag="--scope")
    directories = checkout.scope_directories(
        scope, directory=context.directory, toplevel=lambda: context.toplevel, revision=revision
    )
    n, total, share, effective_whole_reason = policy.reject_wide_scope(
        scope, versioned, whole_reason, directories=directories, whole_from_body=whole_from_body
    )
    return ScopeVersioning(n, total, share), effective_whole_reason


@dataclass(frozen=True)
class _ClaimTargetContext:
    """The board-environment scalars `_claim_target_checks` needs beyond
    the claim request itself, bundled once instead of PLR0913's
    five-scalar ceiling (issue #337): `storage` and `worktree` `run`
    already resolved before this point, and `open_by_number` -- the open
    board, when deriving the scope already fetched it, `None` otherwise --
    so a fresh issue claim that did not derive its scope still fetches the
    board exactly once, inside `_claim_target_checks` itself."""

    storage: body.Storage
    worktree: Path
    open_by_number: dict[int, board.Issue] | None


def _claim_target_checks(
    context: session.RunContext,
    requested: protocol.ClaimRequest,
    observed: protocol.ClaimState,
    target: _ClaimTargetContext,
) -> tuple[tuple[_SliceCheck, ...], int | None, protocol.ActiveClaim | None]:
    """`run`'s slice-rule checks against its own target issue,
    `()`/`None`/`None` for a lane claim -- there is no target issue to
    check against. A replayed claim (`_matching_store_claim`) skips the
    forge and the checks entirely, since an interrupted retry was already
    accepted once. A fresh issue claim reuses `target.open_by_number` when
    deriving the scope already fetched it (issue #337), so this never
    re-fetches the open board a second time, then runs the mismatch and
    slice-rule gates against it. `context.forge` -- built and
    Erwartung-6-checked on this first call (issue #245) -- runs only on
    this fresh-claim path, never for a lane claim or a replay."""
    if not isinstance(requested.identity, protocol.IssueIdentity):
        return (), None, None
    target_issue = requested.identity.issue
    replayed = _matching_store_claim(observed, requested)
    if replayed is not None:
        return (), target_issue, replayed
    client = context.forge
    open_by_number = target.open_by_number
    if open_by_number is None:
        open_by_number = {issue.number: issue for issue in client.list_open_board_issues()}
    _reject_scope_mismatch(open_by_number, target_issue, requested.scope, target.storage)
    projected = board_read.build_board(
        context,
        tuple(observed.claims.values()),
        issues=tuple(open_by_number.values()),
        history=board_read.ClaimHistory(ages=board_read.claim_ages(target.worktree, observed)),
    )
    checks = _slice_rule_checks(
        _BoardReferenceLookup(client, client.repository.path, open_by_number),
        target_issue,
        projected,
        requested.out_of_order_reason,
        target.storage,
    )
    return checks, target_issue, replayed


def run(parsed: argparse.Namespace, context: session.RunContext) -> int:
    try:
        return claim_write(parsed, context)
    except protocol.ClaimError as error:
        return refuse_claim_error(error, context, as_json=parsed.json)


def refuse_claim_error(
    error: protocol.ClaimError, context: session.RunContext, *, as_json: bool
) -> int:
    """`claim`'s own `--json` refusals (issue #406, `ClaimReason`):
    `claim_write`'s typed exceptions choose `target_invalid`/
    `body_invalid`/`claim_conflict`; `session.RepoMeaninglessUnderStateRefError`
    chooses `invalid_usage`; every other `protocol.ClaimError` -- a checkout
    precondition, scope grammar, an unsafe branch or claim id -- falls to
    `unavailable`, matching `ask`/`rule`/`brief`'s own catch-all. Only a
    conflict reads `context`'s board configuration, to name its item."""
    match error:
        case session.RepoMeaninglessUnderStateRefError():
            reason = ClaimReason.INVALID_USAGE
        case _ClaimTargetInvalidError():
            reason = ClaimReason.TARGET_INVALID
        case _ClaimBodyInvalidError():
            reason = ClaimReason.BODY_INVALID
        case protocol.ClaimConflictError():
            # `protocol.apply()`'s own conflict, met on the observed state or by the
            # claim's write (CLM-25), whether or not its push was sent; a
            # transport, git, lineage, or retry-exhaustion failure is another
            # `protocol.ClaimError` and falls to `unavailable` below (CLM-27).
            return report.refuse(
                ClaimReason.CLAIM_CONFLICT,
                board.named_refusal(error, context.config.storage),
                as_json=as_json,
            )
        case _:
            reason = ClaimReason.UNAVAILABLE
    return report.refuse(reason, error, as_json=as_json)


def claim_write(
    parsed: argparse.Namespace, context: session.RunContext, *, worktree: Path | None = None
) -> int:
    """`claim` made in the session's own checkout, or in `worktree` when
    given (`start` claiming in a lane worktree that already stands, issue
    #479): then the request is read and checked in `worktree` and the scope
    measured against its HEAD, while the claim's checks read the session's
    own board configuration -- the main checkout's."""

    def default_branch() -> str | None:
        # A remote this checkout does not configure records no default
        # branch, so the `main`/`master` guess would judge the checkout
        # against a trunk nobody named (issue #512): name the remote first.
        context.refuse_unconfigured_canonical_remote()
        return context.recorded_default_branch

    if worktree is None:
        requested = _request(parsed, default_branch=default_branch, directory=context.directory)
        plan = checked_claim(requested, context)
        writer = context
    else:
        requested = _request(parsed, default_branch=default_branch, directory=worktree)
        plan = checked_claim(requested, context, revision=requested.base)
        writer = context.for_lane_worktree(worktree)
    if plan.refused:
        refuse_claim(parsed.json, plan.target_issue, plan.checks)
        return 2
    print_claim_checks(plan, as_json=parsed.json)
    claimed, live = committed_claim(plan, writer)
    return report_claim(plan, claimed, live, as_json=parsed.json)


@dataclass(frozen=True)
class _ClaimPlan:
    """What `claim`'s check phase accepted, and everything its commit phase
    writes and reports (issue #479): `start` runs the two phases around the
    worktree it builds, so a refusal comes before the build."""

    requested: protocol.ClaimRequest
    observed: protocol.ClaimState
    storage: body.Storage
    versioning: ScopeVersioning
    checks: tuple[_SliceCheck, ...]
    target_issue: int | None
    replayed: protocol.ActiveClaim | None
    intent: protocol.ClaimIntent

    @property
    def refused(self) -> bool:
        return any(check.level == "error" for check in self.checks)


def checked_claim(
    requested: protocol.ClaimRequest, context: session.RunContext, *, revision: str | None = None
) -> _ClaimPlan:
    """`claim`'s check phase: every refusal a claim meets before its one
    ledger write -- the scope's shape and width, the store, the target's
    slice rules, and the ledger's own conflict, `protocol.apply` run on the
    observed state (issue #479). Refusing slice rules come back in the plan
    for the caller to report in its own shape; every other refusal raises.
    `revision` names the commit the scope is measured against when the
    session's own checkout is not the one claimed in (`start`'s fetched
    trunk before its worktree exists, or the HEAD of a lane worktree that
    already stands); the session's own checkout otherwise."""
    if isinstance(requested.identity, protocol.LaneIdentity) and not requested.scope:
        raise protocol.ClaimUnavailableError(_LANE_CLAIM_SCOPE_REQUIRED)
    open_by_number: dict[int, board.Issue] | None = None
    if requested.scope or not isinstance(requested.identity, protocol.IssueIdentity):
        # Scope already final (given, or a lane's own required value): the
        # local shape checks run first, exactly as before this field
        # existed, so a comma or width refusal never touches the store or
        # resolves the repository toplevel -- unless the scope is actually
        # wide and names no `--whole` of its own, in which case the width
        # gate's own lazy fallback (issue #399) resolves the toplevel itself
        # to read the item's body, never this branch.
        versioning, effective_whole = scope_versioning(
            requested.scope,
            requested.whole_reason,
            context=context,
            whole_from_body=_whole_from_item_body(context, requested.identity, open_by_number=None),
            revision=revision,
        )
        requested = replace(requested, whole_reason=effective_whole)
        observed = context.observation
        protocol.require_state_ref(observed)
        storage = context.config.storage
    else:
        # `--scope` was omitted in issue mode: the item's own scope has to
        # come from the store, and usually the forge, before it can even be
        # shape-checked -- both observed exactly once, here, so an omitted-
        # scope claim never fetches either a second time (issue #337).
        observed = context.observation
        protocol.require_state_ref(observed)
        storage = context.config.storage
        requested, open_by_number = _resolved_claim_request(requested, observed, context, storage)
        versioning, effective_whole = scope_versioning(
            requested.scope,
            requested.whole_reason,
            context=context,
            whole_from_body=_whole_from_item_body(
                context, requested.identity, open_by_number=open_by_number
            ),
            revision=revision,
        )
        requested = replace(requested, whole_reason=effective_whole)
    worktree = context.toplevel
    checks, target_issue, replayed = _claim_target_checks(
        context, requested, observed, _ClaimTargetContext(storage, worktree, open_by_number)
    )
    plan = _ClaimPlan(
        requested=requested,
        observed=observed,
        storage=storage,
        versioning=versioning,
        checks=checks,
        target_issue=target_issue,
        replayed=replayed,
        intent=_claim_intent_from_request(
            requested,
            uuid.uuid4().hex,
            _claimed_item_pin(requested.identity, observed, storage),
        ),
    )
    if replayed is None and not plan.refused:
        protocol.apply(observed, plan.intent)
    return plan


def print_claim_checks(plan: _ClaimPlan, *, as_json: bool) -> None:
    for check in plan.checks:
        print(check.render(), file=sys.stderr if as_json else sys.stdout)


def committed_claim(
    plan: _ClaimPlan, writer: session.RunContext
) -> tuple[protocol.ActiveClaim, tuple[protocol.ActiveClaim, ...]]:
    """`claim`'s commit phase: the plan's one ledger write, made from the
    checkout `writer` stands for, or the replayed claim it already names,
    with every live claim after it."""
    if plan.replayed is not None:
        return plan.replayed, tuple(plan.observed.claims.values())
    requested = plan.requested
    new_state = writer.transition(
        store.transition_subject("claim", requested.identity, requested.branch), plan.intent
    )
    claimed = new_state.claims[protocol.claim_key(requested.identity, requested.branch)]
    return claimed, tuple(new_state.claims.values())


def report_claim(
    plan: _ClaimPlan,
    claimed: protocol.ActiveClaim,
    live: tuple[protocol.ActiveClaim, ...],
    *,
    as_json: bool,
) -> int:
    touches = protocol.conflicting_claims(live, claimed)
    if as_json:
        return _claim_json(claimed, versioning=plan.versioning, touches=touches, checks=plan.checks)
    print(f"CLAIMED {board.claim_subject(claimed, plan.storage)}: {claimed.claim_id}")
    print(
        claim_cost_line(
            plan.versioning.versioned_files,
            plan.versioning.versioned_files_total,
            plan.requested.scope,
            touches,
            plan.storage,
        )
    )
    return 0


@dataclass(frozen=True)
class LaneWorktree:
    """The lane worktree and its branch."""

    path: Path
    branch: str


@dataclass(frozen=True)
class LaneBuild:
    """The worktree build and its caller-owned refusal wording."""

    worktree: LaneWorktree
    start_point: str
    command: str
    moved_refusal: str


def print_lane_worktree(target: LaneWorktree) -> None:
    print(f"worktree: {target.path}")
    print(f"branch: {target.branch}")


def _remove_refused_worktree(target: LaneWorktree, *, command: str) -> None:
    """Undo a build whose claim was refused after the check
    phase passed (issue #479), saying so: a refusal must leave no worktree
    or branch behind. When git will not remove the worktree, that is said
    too, beside the refusal rather than in its place."""
    try:
        outcome = checkout.remove_linked_worktree(target.path, branch=target.branch)
    except protocol.ClaimError as error:
        print(
            f"worktree {target.path} and branch '{target.branch}' this {command} created "
            f"kept: git failure: {error}",
            file=sys.stderr,
        )
        return
    if outcome.branch.fate is checkout.BranchFate.REMOVED:
        print(
            f"removed worktree {target.path} and branch '{target.branch}' this {command} created",
            file=sys.stderr,
        )
        return
    print(
        f"removed worktree {target.path} this {command} created; "
        f"branch '{target.branch}' {outcome.branch.fate}: {outcome.branch.reason}",
        file=sys.stderr,
    )


def validate_built_worktree(
    request: protocol.ClaimRequest,
    target: LaneWorktree,
    context: session.RunContext,
    *,
    moved_refusal: str,
) -> None:
    """Validate the built checkout against the checked request and the main
    checkout's recorded default branch. The caller owns the repair message
    when the start point moved between checking and building (START-26)."""
    try:
        checkout.validate_checkout(
            request,
            default_branch=lambda: context.recorded_default_branch,
            directory=target.path,
        )
    except checkout.CheckoutBaseMismatchError as error:
        raise protocol.ClaimUnavailableError(moved_refusal) from error


def report_uncertain_worktree_claim(
    error: protocol.ClaimError, target: LaneWorktree, *, command: str
) -> int:
    """A claim write whose outcome the store cannot tell after its push was
    sent: the claim may have landed, so the worktree it may name stays, and
    the next invocation resumes whichever it finds (START-25)."""
    status = report.refuse(ClaimReason.UNAVAILABLE, error, as_json=False)
    print(
        f"the claim's push was sent, its outcome unknown; worktree {target.path} and "
        f"branch '{target.branch}' kept; run {command} again to resume it",
        file=sys.stderr,
    )
    return status


def refuse_built_worktree(error: protocol.ClaimError, target: LaneWorktree, *, command: str) -> int:
    try:
        return report.refuse(ClaimReason.UNAVAILABLE, error, as_json=False)
    finally:
        # A refusal that cannot be written (a closed stderr) must still
        # leave nothing behind (START-18).
        _remove_refused_worktree(target, command=command)


def claim_in_new_worktree(
    plan: _ClaimPlan,
    requested: protocol.ClaimRequest,
    context: session.RunContext,
    build: LaneBuild,
) -> tuple[protocol.ActiveClaim, tuple[protocol.ActiveClaim, ...]] | int:
    """Build around a checked claim, retaining the pair only for an uncertain write."""
    worktree = build.worktree
    checkout.create_linked_worktree(
        worktree.path,
        branch=worktree.branch,
        start_point=build.start_point,
        directory=context.toplevel,
    )
    print_lane_worktree(worktree)
    print_claim_checks(plan, as_json=False)
    try:
        validate_built_worktree(requested, worktree, context, moved_refusal=build.moved_refusal)
        # The lane's own lineage stamp and fetch anchor start at its claim (CAS-09).
        return committed_claim(plan, context.for_lane_worktree(worktree.path))
    except protocol.UncertainWriteError as error:
        return report_uncertain_worktree_claim(
            board.named_refusal(error, context.config.storage), worktree, command=build.command
        )
    except protocol.ClaimError as error:
        return refuse_built_worktree(
            board.named_refusal(error, context.config.storage), worktree, command=build.command
        )
