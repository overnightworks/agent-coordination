"""The `start` command (`specs/start.spec.md`)."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import board, body, checkout, cmd_claim, forge, protocol
from .session import RunContext

_MOVED_REFUSAL = "the trunk moved after start checked it; run start again"


def _start_worktree_path(main_checkout: Path, *, number: int, slug: str) -> Path:
    return checkout.sibling_worktree_path(main_checkout, f"issue-{number}-{slug}")


def _runs_in_lane_worktree(context: RunContext, lane_branch: str) -> bool:
    """Whether `start` runs inside a linked worktree checked out on
    `lane_branch` -- the live claim's own lane (issue #479), whatever slug
    its path carries."""
    caller = checkout.resolve_path_checkout(context.toplevel)
    return (
        caller is not None
        and caller.kind is checkout.CheckoutKind.LINKED_WORKTREE
        and caller.branch == lane_branch
    )


def _resumable_start_claim(
    live: protocol.ActiveClaim | None, branch: str
) -> protocol.ActiveClaim | None:
    """`live`, when `start` resumes it rather than claiming afresh.
    `protocol.claim_key` alone is an issue-only key for an `protocol.IssueIdentity` (it never
    folds `branch` into the key at all): a live record found under it may
    belong to a different agent, a different role, or a different branch
    entirely, so resume requires this session's own agent, `start`'s own
    claiming role, and the expected lane branch too -- the same facts
    `release`'s own claimant/branch checks require, plus the role a
    reviewer claim on the same item must never satisfy (review/gate
    finding). A mismatch falls through to the fresh claim, which refuses
    with the store's own "is claimed by ..." conflict."""
    if (
        live is not None
        and live.agent == checkout.resolved_agent(None)
        and live.role == cmd_claim.DEFAULT_CLAIM_ROLE
        and live.branch == branch
    ):
        return live
    return None


_RESUME_SCOPE_MISMATCH = "live claim scope differs; release it first"


def _refuse_resume_scope_mismatch(live: protocol.ActiveClaim, parsed: argparse.Namespace) -> None:
    if parsed.scope is not None and protocol.valid_scope(parsed.scope) != live.scope:
        raise protocol.ClaimUnavailableError(_RESUME_SCOPE_MISMATCH)


def _checked_start_resume(
    live: protocol.ActiveClaim,
    parsed: argparse.Namespace,
    *,
    context: RunContext,
    revision: str | None = None,
) -> cmd_claim.ScopeVersioning:
    """Every refusal `start`'s resume of `live` can meet, run before anything
    is printed or built (issue #479): an explicit `--scope` that disagrees
    with the live claim's own stored scope (review/gate finding: resume must
    not silently ignore it), then the stored scope's own shape and width
    against `context`'s checkout -- or `revision`, the fetched trunk a gone
    worktree is rebuilt from. `--whole` is never required here even when the
    live scope is wide, since the stored claim's own `whole_reason` already
    justified it once."""
    _refuse_resume_scope_mismatch(live, parsed)
    versioning, _effective_whole = cmd_claim.scope_versioning(
        live.scope,
        parsed.whole if parsed.whole is not None else live.whole_reason,
        context=context,
        revision=revision,
    )
    return versioning


def _print_start_resume(
    live: protocol.ActiveClaim,
    observed: protocol.ClaimState,
    storage: body.Storage,
    versioning: cmd_claim.ScopeVersioning,
) -> None:
    """`start`'s own resume path (issue #322 review finding 1): prints the
    same `CLAIMED ...`/cost-line grammar a fresh claim prints, for the live
    claim `run` already found in the store -- the same
    `observed.claims` lookup `status`/`release` use -- rather than minting a
    second, fresh id for an item that already has one."""
    print(f"CLAIMED {board.claim_subject(live, storage)}: {live.claim_id}")
    touches = protocol.conflicting_claims(tuple(observed.claims.values()), live)
    print(
        cmd_claim.claim_cost_line(
            versioning.versioned_files,
            versioning.versioned_files_total,
            live.scope,
            touches,
            storage,
        )
    )


def _start_branch_and_slug(parsed: argparse.Namespace, context: RunContext) -> tuple[str, str]:
    """The lane branch and slug `start` builds for an open item, refusing a
    missing or closed one, an unusable slug, and an unsafe prefix before any
    git write."""
    number = parsed.item
    item = context.forge.item_reference(number)
    label = board.item_label(number, context.config.storage)
    if item.state is forge.ItemState.MISSING:
        raise protocol.ClaimUnavailableError(f"issue {label} does not exist here")
    if item.state is forge.ItemState.CLOSED:
        raise protocol.ClaimUnavailableError(f"issue {label} is closed")
    slug = (
        checkout.validate_slug(parsed.slug)
        if parsed.slug is not None
        else checkout.slug_from_title(item.title or "")
    )
    prefix = checkout.branch_prefix_for_identity()
    branch = f"{prefix}/issue-{number}-{slug}"
    checkout.refuse_unsafe_start_branch(branch, prefix=prefix)
    return branch, slug


def run(parsed: argparse.Namespace, context: RunContext) -> int:
    """Checks, then builds (issue #479): a worktree already standing -- the
    caller's own lane, or a prior `start`'s at the computed path -- is
    claimed in place; otherwise the claim is checked against the freshly
    fetched trunk before the worktree is built, so a refusal leaves
    nothing behind. The item, the store, and every check the claim makes
    read the main checkout's own board configuration, never a lane
    worktree's the caller stands in or claims in: a lane may be changing
    its `board.toml`."""
    caller = context
    main_context = caller.for_directory(
        checkout.main_checkout_root(toplevel=caller.toplevel, command="start"), is_toplevel=True
    )
    branch, slug = _start_branch_and_slug(parsed, main_context)
    observed = main_context.observation
    protocol.require_state_ref(observed)
    live = observed.claims.get(
        protocol.claim_key(protocol.resolved_identity(parsed.item, branch), branch)
    )
    if live is not None and _runs_in_lane_worktree(caller, live.branch):
        own_lane = cmd_claim.LaneWorktree(caller.toplevel, live.branch)
        return _claim_in_start_worktree(parsed, main_context, own_lane, observed, live)
    target = cmd_claim.LaneWorktree(
        _start_worktree_path(main_context.toplevel, number=parsed.item, slug=slug), branch
    )
    if checkout.existing_start_worktree(target.path, branch):
        return _claim_in_start_worktree(parsed, main_context, target, observed, live)
    resumed = _resumable_start_claim(live, branch)
    if resumed is not None:
        return _rebuild_and_resume(parsed, main_context, target, observed, resumed)
    return _check_build_and_claim(parsed, main_context, target)


def _start_claim_arguments(
    parsed: argparse.Namespace,
    *,
    base: str | None = None,
    branch: str | None = None,
    claim_id: str | None = None,
) -> argparse.Namespace:
    """The `aco claim` arguments `start` claims with: this session's own
    agent as the builder, and a fresh id, exactly as a bare `aco claim`
    mints one (issue #322 review finding 1) -- `start` resumes a live claim
    by name before it ever claims, so CLAIM-15's own replay-by-claim-id
    logic never needs to recognize a `start`-minted id as special. A
    resume passes its live claim's own `claim_id`, so it never computes a
    second one."""
    return argparse.Namespace(
        issue=parsed.item,
        agent=None,
        role=cmd_claim.DEFAULT_CLAIM_ROLE,
        base=base,
        branch=branch,
        scope=parsed.scope,
        claim_id=claim_id,
        out_of_order=parsed.out_of_order,
        whole=parsed.whole,
        resource=None,
        json=False,
    )


def _claim_in_start_worktree(
    parsed: argparse.Namespace,
    context: RunContext,
    target: cmd_claim.LaneWorktree,
    observed: protocol.ClaimState,
    live: protocol.ActiveClaim | None,
) -> int:
    """Claim, or resume, in a worktree that already stands: nothing is
    built, so a refusal has nothing to undo, and a claim whose outcome the
    store cannot tell says the worktree stays as a fresh build's would
    (START-25). `context` is the main checkout's; only the scope is
    measured in the worktree itself."""
    resumed = _resumable_start_claim(live, target.branch)
    if resumed is not None:
        versioning = _checked_start_resume(
            resumed, parsed, context=context.for_directory(target.path)
        )
        cmd_claim.print_lane_worktree(target)
        _print_start_resume(resumed, observed, context.config.storage, versioning)
        return 0
    cmd_claim.print_lane_worktree(target)
    # The main checkout's one observation, the same snapshot the lookup above
    # read (CAS-53): nothing fetched since.
    try:
        return cmd_claim.claim_write(_start_claim_arguments(parsed), context, worktree=target.path)
    except protocol.UncertainWriteError as error:
        named = board.named_refusal(error, context.config.storage)
        return cmd_claim.report_uncertain_worktree_claim(named, target, command="start")
    except protocol.ClaimError as error:
        return cmd_claim.refuse_claim_error(error, context, as_json=False)


def _rebuild_and_resume(
    parsed: argparse.Namespace,
    context: RunContext,
    target: cmd_claim.LaneWorktree,
    observed: protocol.ClaimState,
    resumed: protocol.ActiveClaim,
) -> int:
    """A live claim of this session's whose worktree is gone: its scope is
    checked against the fetched trunk first, then the worktree is built
    again from that trunk and the claim reprinted, never a second one
    minted. A worktree that stands on another commit -- another fetch moved
    the trunk after the checks -- is removed again, as a fresh build's is
    (START-18)."""
    trunk_ref = context.fetched_trunk_ref()
    trunk = checkout.commit_at(trunk_ref, directory=context.toplevel)
    versioning = _checked_start_resume(resumed, parsed, context=context, revision=trunk)
    checked = cmd_claim.claim_request(
        _start_claim_arguments(parsed, base=trunk, branch=target.branch, claim_id=resumed.claim_id)
    )
    checkout.create_linked_worktree(
        target.path, branch=target.branch, start_point=trunk_ref, directory=context.toplevel
    )
    cmd_claim.print_lane_worktree(target)
    try:
        cmd_claim.validate_built_worktree(checked, target, context, moved_refusal=_MOVED_REFUSAL)
    except protocol.ClaimError as error:
        return cmd_claim.refuse_built_worktree(error, target, command="start")
    _print_start_resume(resumed, observed, context.config.storage, versioning)
    return 0


def _check_build_and_claim(
    parsed: argparse.Namespace, context: RunContext, target: cmd_claim.LaneWorktree
) -> int:
    """Fetch the trunk, run `claim`'s check phase against that one commit,
    then build the worktree from the trunk and run the commit phase (issue
    #479). A refusal after the build removes it again (START-18): the trunk
    moved under another fetch meanwhile, the new worktree failing `claim`'s
    own checkout preconditions, or the ledger refusing the write -- a claim
    or close that landed after the checks, a store it cannot reach -- also
    once a sent push was rejected and the store re-read nothing of it
    written (issue #498). Only a sent write whose outcome the store cannot
    tell keeps the worktree and says so (START-25). An interrupt or an
    unexpected error is no refusal."""
    trunk_ref = context.fetched_trunk_ref()
    trunk = checkout.commit_at(trunk_ref, directory=context.toplevel)
    # The main checkout observed afresh, never the observation the
    # item-existence read already holds: the fetch above may take a while,
    # and the claim must read the item as it stands once the fetch is done,
    # not the snapshot that read took (issue #322 review finding 2, CAS-55).
    check_context = context.observed_afresh()
    requested = cmd_claim.claim_request(
        _start_claim_arguments(parsed, base=trunk, branch=target.branch)
    )
    storage = check_context.config.storage
    try:
        plan = cmd_claim.checked_claim(requested, check_context, revision=trunk)
    except protocol.ClaimConflictError as error:
        raise board.named_refusal(error, storage) from error
    if plan.refused:
        cmd_claim.refuse_claim(False, plan.target_issue, plan.checks)
        return 2
    result = cmd_claim.claim_in_new_worktree(
        plan,
        requested,
        check_context,
        cmd_claim.LaneBuild(
            worktree=target,
            start_point=trunk_ref,
            command="start",
            moved_refusal=_MOVED_REFUSAL,
        ),
    )
    if isinstance(result, int):
        return result
    claimed, claims = result
    return cmd_claim.report_claim(plan, claimed, claims, as_json=False)
