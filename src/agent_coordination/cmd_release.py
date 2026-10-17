"""The `release` command (`specs/release.spec.md`)."""

from __future__ import annotations

import argparse
import shlex
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from . import (
    board,
    board_read,
    body,
    checkout,
    forge,
    github,
    protocol,
    report,
    state_board,
    store,
)
from .session import RunContext

KEEP_WORKTREE_FLAG = "--keep-worktree"


class _ReleaseReason(StrEnum):
    """`aco release`'s own `--json` `reason` vocabulary (issue #425,
    `specs/release.spec.md`): `merged`/`abandoned` name which outcome flag
    the caller gave -- a state-ref `--merged` landing (`LandedRelease`)
    reports `merged` too, since it is the same `--merged` outcome as a
    github pull request landing, only verified against the trunk walk
    instead of a pull request. `outcome`, this release's own prose sentence
    (`"merged #<n>"`, `"abandoned: <explanation>"`, or `"landed <sha>"`),
    stays a payload sibling rather than a second enum. Every refusal past
    the parser (REL-01's usage errors excepted, which argparse itself
    reports) is `precondition_failed`, matching REL-24's single shape:
    this claim's own current state, its identity/branch resolution, or the
    forge disallowing the release."""

    MERGED = "merged"
    ABANDONED = "abandoned"
    PRECONDITION_FAILED = "precondition_failed"


def _release_reason(outcome: protocol.ReleaseOutcome) -> _ReleaseReason:
    return (
        _ReleaseReason.ABANDONED
        if isinstance(outcome, protocol.AbandonedRelease)
        else _ReleaseReason.MERGED
    )


def _release_json(release_report: _ReleaseReport) -> None:
    released, landing = release_report.selected, release_report.landing
    payload: dict[str, object] = {
        "outcome": release_report.outcome.reason,
        **protocol.identity_json(released.identity),
        "branch": released.branch,
        "claim_id": released.claim_id,
        "agent": release_report.agent,
        "role": release_report.role if release_report.role is not None else released.role,
    }
    if isinstance(landing, _ReleaseLanding):
        payload["freed"] = list(landing.freed)
        payload["next"] = None if landing.next_item is None else landing.next_item.number
        payload["parent_closable"] = landing.parent_closable
    if release_report.worktree is not None:
        payload["worktree"] = _worktree_cleanup_outcome_text(release_report.worktree)
    report.emit_json(True, _release_reason(release_report.outcome), **payload)


@dataclass(frozen=True)
class _ReleaseLanding:
    """What a merged release's lazy board read (issue #256) adds to its
    report once the release itself has already succeeded: every open item
    this landing fully freed, the same next pick `aco next` would recommend
    right after it, and the parent container this landing's own child just
    made closable, if any (issue #348)."""

    freed: tuple[int, ...]
    next_item: board.BoardItem | None
    parent_closable: int | None


def _release_landing(
    context: RunContext,
    new_state: protocol.ClaimState,
    landed: board.IssueReference | None,
    storage: body.Storage,
    trunk_ref: str,
) -> _ReleaseLanding:
    """A merged release's own board read (issue #256) of `new_state`, the
    claims its release transition left: fetched once, lazily, only after
    that transition already committed -- the caller wraps this in one broad
    `forge.ForgeError` catch, so a forge hiccup here can never undo or fail
    a release that already stood. The dependency fetch below is the one
    round trip both `board.freed_item_numbers` and `board_read.build_board` need for
    this same candidate set; passing it into `board_read.build_board` keeps this a
    single fetch rather than two (issue #256 review). The board walks `trunk_ref`, the ref the
    release judged its landing on (LANDCMD-21, issue #492)."""
    client = context.forge
    issues = client.list_open_board_issues()
    candidates = tuple(issue.number for issue in issues if issue.blocked_by_count > 0)
    dependencies = board_read.validated_dependencies(
        issues, board_read.fetch_dependencies(client, candidates)
    )
    freed = () if landed is None else board.freed_item_numbers(dependencies, landed)
    projected = board_read.build_board(
        context,
        tuple(new_state.claims.values()),
        issues=issues,
        history=board_read.ClaimHistory(ages=board_read.claim_ages(context.toplevel, new_state)),
        landing=board_read.LandingReads(dependencies, trunk_ref),
    )
    action = board.next_action(projected)
    parent_closable = (
        None
        if landed is None
        else board_read.parent_closable_number(client, landed.number, storage)
    )
    return _ReleaseLanding(
        freed, None if action is None else board.next_action_item(action), parent_closable
    )


def _release_next_line(item: board.BoardItem | None, storage: body.Storage) -> str:
    if item is None:
        return "next: none"
    label = board.item_label(item.number, storage)
    return f"next: {label} score {item.score}: {board.terminal_text(item.title)}"


def _release_outcome(merged: int | None, abandoned: str | None) -> protocol.ReleaseOutcome:
    if merged is not None:
        return protocol.MergedRelease(merged)
    return protocol.AbandonedRelease(
        protocol.outbound_text(abandoned, "abandoned reason", maximum=512)
    )


@dataclass(frozen=True)
class _MergedLandingClose:
    """The still-open issue `_pending_landing_close` found and the pull
    request that landed it (issue #359 R1): read only once the claim is
    resolved and the claimant authorized, so an unauthorized or
    mismatched-claim `--merged` release never reaches a forge write, or even
    this read, at all. A REL-47 release with no live claim (issue #605)
    never reads it: it has nothing to close."""

    issue: int
    pull_request: int


@dataclass(frozen=True)
class _VerifiedMerge:
    """What `_verify_merged_release` proved about a merged pull request:
    `landed_head`, the head the forge recorded as landed -- the one tip a
    squashed lane's worktree and branch are removed by, on a first run or
    a rerun alike (issue #590) -- the branch it was merged from, which the
    released claim must sit on (issue #605), the first-parent trunk commits
    from its merge onward, none of which the released claim may be based on
    (issue #605: such a claim was opened after this landing)."""

    landed_head: str
    source_branch: str
    trunk_since_landing: frozenset[str]


def _verify_merged_release(
    context: RunContext,
    client: github.GitHubForge,
    identity: protocol.ClaimIdentity,
    merged: protocol.MergedRelease,
) -> _VerifiedMerge:
    """Refuse a `--merged` release the landing itself does not support, and
    report the head the forge recorded as landed (issue #590): `run`
    calls this only after the claim is already resolved and the claimant
    already authorized, so a defect or a transient forge failure there never
    runs ahead of authorization -- except REL-47's release with no live
    claim (issue #605), which has no claimant to authorize and verifies
    here. Whether the work item is still open to close is
    `_pending_landing_close`'s read, which only a release of a claim makes
    (issue #359 Card 1/R1), so REL-47 never reads it. `state_board.py`'s own
    `LandingIntent` path is `storage = state-ref`'s equivalent, so this only
    ever runs under `storage = github` (see `run`).

    Every release's authority -- an issue's own closing reference and an
    issue-less lane's own declaration alike -- is the merge commit's own
    trailer block (issue #397, Befund 41; issue #405 gate follow-up),
    verified through the walked first-parent trunk exactly as `storage =
    state-ref` already does -- never the pull request's own `body`, which
    stays mutable long after the merge and once was edited to remove the
    very `Work-Item:` line a release depended on.
    """
    detail = client.landing(merged.pull_request)
    if not detail.merged:
        raise protocol.ClaimUnavailableError(f"pull request #{detail.number} is not merged")
    default_branch = context.default_branch
    if detail.target_branch != default_branch:
        raise protocol.ClaimUnavailableError(
            f"pull request #{detail.number} merged into {detail.target_branch!r}, "
            f"not the default branch {default_branch!r}"
        )
    assert detail.merge_commit is not None  # `detail.merged` is true; github.py guarantees this.
    landings = checkout.trunk_landings(
        context.fetched_default_branch_ref(),
        board_read.TRUNK_LANDING_DEPTH,
        directory=context.toplevel,
    )
    checkout.verify_landing_authority(
        identity, landings, detail.number, detail.merge_commit, context.config.storage
    )
    return _VerifiedMerge(
        landed_head=detail.head_sha,
        source_branch=detail.source_branch,
        trunk_since_landing=checkout.trunk_since(landings, detail.merge_commit),
    )


def _pending_landing_close(
    client: github.GitHubForge, identity: protocol.ClaimIdentity, pull_request: int
) -> _MergedLandingClose | None:
    """The still-open item a verified landing leaves for `run` to
    close, or `None`: an issue-less lane has none."""
    if isinstance(identity, protocol.LaneIdentity):
        return None
    reference = board_read.fetch_issue_reference(client, identity.issue)
    if reference.state is forge.ItemState.OPEN:
        return _MergedLandingClose(identity.issue, pull_request)
    if reference.state is not forge.ItemState.CLOSED:
        raise protocol.ClaimUnavailableError(
            f"work item #{identity.issue} is {reference.state.value}, not closed"
        )
    return None


@dataclass(frozen=True)
class _ResolvedRelease:
    """The live claim `release` targets and the role it releases under
    (issue #359): factored out of `run` so `_cmd_release_landed`
    (the `storage = "state-ref"` `--merged <sha>` path) shares the exact
    same selection, `--branch`/`--claim-id` agreement, and claimant checks
    rather than a second copy of them."""

    selected: protocol.ActiveClaim
    resolved_role: str


def _resolve_release_claimant(
    parsed: argparse.Namespace,
    observed: protocol.ClaimState,
    identity: protocol.ClaimIdentity,
    release_branch: str | None,
    storage: body.Storage,
) -> _ResolvedRelease:
    selected = select_release_claim(parsed, observed, identity, release_branch, storage)
    return authorize_releaser(parsed, selected, _release_repeat_command(parsed))


def _unclaimed_rerun_branch(
    parsed: argparse.Namespace,
    observed: protocol.ClaimState,
    identity: protocol.ClaimIdentity,
    release_branch: str | None,
) -> str | None:
    """The release's own branch when a `--merged` release has nothing left
    to release (issue #605, REL-47): no `--claim-id` and no live claim on its
    identity, the state a rerun after a completed release finds. The missing
    claim is the trigger, never a closed item: GitHub closes the item at
    merge time through `Closes #<n>`. For an issue-less lane the result is
    not final: its claim is keyed by its branch, so once the merge verifies,
    `_report_merged_pull_request_left_nothing` still refuses REL-09 when the
    pull request came from another branch (REL-54). A `--claim-id` names one
    claim, so its absence stays `select_release_claim`'s refusal; `None`
    hands every other release to that selection."""
    if parsed.merged is None or parsed.claim_id is not None or not release_branch:
        return None
    if protocol.claim_key(identity, release_branch) in observed.claims:
        return None
    return release_branch


def _refuse_a_claim_the_landing_did_not_land(
    selected: protocol.ActiveClaim,
    landing: str,
    landing_branch: str,
    trunk_since_landing: frozenset[str],
    storage: body.Storage,
) -> None:
    """REL-48/REL-53 (issues #605, #611), the one judge under both storage
    pins of whether `selected` belongs to `landing` -- `pull request #<n>`
    from its source branch, or `commit <sha>` from an explicit `--branch`,
    else the claim's own branch (REL-56). An issue's claim is keyed by the
    issue alone, so an old landing's rerun would otherwise release a newer
    claim on the same issue -- a lane on another branch (REL-48), or one a
    fresh `start` built on the same branch from a trunk that already holds
    this landing (START-01, REL-53). That claim is never released and never skipped. A same-branch
    claim based off the trunk, such as START-11 or `aco claim` in a lane
    worktree still standing, passes both: the residual #310 finding 356
    owns."""
    subject = protocol.identity_summary(
        selected.identity, selected.branch, board.item_labeller(storage)
    )
    if selected.branch != landing_branch:
        raise protocol.ClaimUnavailableError(
            f"{subject} is claimed on {selected.branch!r}, not on {landing}'s branch "
            f"{landing_branch!r}; release that claim by itself"
        )
    if selected.base in trunk_since_landing:
        raise protocol.ClaimUnavailableError(
            f"{subject} was claimed on {selected.branch!r} after {landing} landed; "
            "release that claim by itself"
        )


def select_release_claim(
    parsed: argparse.Namespace,
    observed: protocol.ClaimState,
    identity: protocol.ClaimIdentity,
    release_branch: str | None,
    storage: body.Storage,
) -> protocol.ActiveClaim:
    selected = board.selected_store_claim(
        observed, identity, release_branch, parsed.claim_id, storage
    )
    if (
        parsed.branch is not None
        and parsed.claim_id is not None
        and selected.branch != parsed.branch
    ):
        raise protocol.ClaimUnavailableError(
            f"--branch {parsed.branch!r} and --claim-id {parsed.claim_id!r} disagree: the "
            f"claim's own branch is {selected.branch!r}; drop --branch or pass its own value"
        )
    return selected


def authorize_releaser(
    parsed: argparse.Namespace, selected: protocol.ActiveClaim, repeat: Sequence[str]
) -> _ResolvedRelease:
    """This session's right to release `selected`: its own claimant, or an
    explicit coordinator override. `repeat` is the refused command without
    its identity flags, which the refusal names run as the holder."""
    role = parsed.role
    if not parsed.coordinator_override:
        if role is None:
            role = selected.role
        if (parsed.agent, role) != (selected.agent, selected.role):
            raise protocol.ClaimUnavailableError(
                "only the original claimant may release; repeat as the holder with "
                f"`{_holder_repeat_command(repeat, selected, role)}`, or use an explicit "
                "coordinator override "
                f"(holder={protocol.claimant_text(selected.agent, selected.role)!r}, "
                f"this session={protocol.claimant_text(parsed.agent, role)!r})"
            )
    resolved_role = role if role is not None else selected.role
    return _ResolvedRelease(selected, resolved_role)


def _holder_repeat_command(
    repeat: Sequence[str], holder: protocol.ActiveClaim, session_role: str
) -> str:
    """`repeat` -- the refused command, without its identity flags -- run
    as `holder` instead (issue #578): a claim taken under an explicit
    `--agent` releases from the same session only when it names that agent
    again, so the refusal spells out that one repeat before any override."""
    identity = ["--agent", holder.agent]
    if session_role != holder.role:
        identity += ["--role", holder.role]
    return shlex.join([*repeat, *identity])


def _release_repeat_command(parsed: argparse.Namespace) -> tuple[str, ...]:
    """The `aco release` command line `parsed` came from, without its
    identity flags, for `_holder_repeat_command`."""
    command = ["aco", "release"]
    if parsed.issue is not None:
        command.append(str(protocol.optional_issue_number(parsed.issue)))
    if parsed.branch is not None:
        command += ["--branch", parsed.branch]
    if parsed.claim_id is not None:
        command += ["--claim-id", parsed.claim_id]
    if parsed.merged is None:
        command += ["--abandoned", parsed.abandoned]
    else:
        command += ["--merged", parsed.merged] if parsed.merged else ["--merged"]
    if parsed.keep_worktree:
        command.append(KEEP_WORKTREE_FLAG)
    if parsed.json:
        command.append(report.JSON_FLAG)
    return tuple(command)


def run(parsed: argparse.Namespace, context: RunContext) -> int:
    """`release`'s own `--json` envelope (issue #425): every refusal this
    function or `_cmd_release_landed` raises -- REL-01's own usage errors
    excepted, which the parser reports as `invalid_usage` before either ever
    runs (issue #432) -- is caught here and reported as
    `precondition_failed`, rather than escaping the envelope entirely."""
    as_json = parsed.json
    try:
        release_transition(parsed, context, _release_branch_for(parsed))
    except protocol.ClaimError as error:
        return report.refuse(_ReleaseReason.PRECONDITION_FAILED, error, as_json=as_json)
    return 0


class ReleaseEnding(StrEnum):
    """How one `release` ended (issue #605): it released a claim, or a
    `--merged` rerun found nothing left to release (REL-47), after which
    `aco land` prints no reinstall line (LANDCMD-38)."""

    RELEASED = "released"
    NOTHING_LEFT = "nothing-left"


def release_transition(
    parsed: argparse.Namespace, context: RunContext, release_branch: str | None
) -> ReleaseEnding:
    """A `--merged` release judges a squashed lane by the head the forge
    recorded as landed (issue #590), whether `aco land` merged it a moment
    ago, a rerun resumes it, or a standalone release follows a merge made
    elsewhere: a merged pull request's head no longer moves."""
    issue = protocol.optional_issue_number(parsed.issue)
    identity = protocol.lookup_identity(
        issue,
        release_branch or "",
        holders=lambda: protocol.claims_on_branch(context.observation.claims, release_branch or ""),
    )
    storage = context.config.storage
    if parsed.merged is not None and storage is body.Storage.STATE_REF:
        return _cmd_release_landed(parsed, context, identity, storage, release_branch)
    merged = None if parsed.merged is None else github.pull_request_number(parsed.merged)
    outcome = _release_outcome(merged, parsed.abandoned)
    observed = context.observation
    protocol.require_state_ref(observed)
    unclaimed_branch = _unclaimed_rerun_branch(parsed, observed, identity, release_branch)
    if isinstance(outcome, protocol.MergedRelease) and unclaimed_branch is not None:
        _report_merged_pull_request_left_nothing(
            parsed, context, identity, outcome, unclaimed_branch
        )
        return ReleaseEnding.NOTHING_LEFT
    resolved = _resolve_release_claimant(parsed, observed, identity, release_branch, storage)
    lane = (
        _verify_and_close_merged_release(context, identity, outcome, resolved.selected)
        if isinstance(outcome, protocol.MergedRelease)
        else None
    )
    intent = protocol.ReleaseIntent(
        claim_id=resolved.selected.claim_id,
        agent=parsed.agent,
        role=resolved.resolved_role,
        outcome=outcome,
        operation_id=uuid.uuid4().hex,
        coordinator_override=parsed.coordinator_override,
    )
    new_state = context.transition(
        store.transition_subject("release", resolved.selected.identity, resolved.selected.branch),
        intent,
    )
    # The run already fetched the canonical remote and asked the forge's
    # default branch to verify the merge, so this ref costs no read.
    landing = (
        None
        if lane is None
        else _landing_report(
            context, identity, new_state, storage, context.fetched_default_branch_ref()
        )
    )
    worktree_cleanup = (
        None
        if lane is None
        else _cleanup_landed_worktree(parsed, lane, context, context.fetched_default_branch_ref)
    )
    _print_release_result(
        _ReleaseReport(
            resolved.selected,
            parsed.agent,
            resolved.resolved_role,
            outcome,
            landing,
            storage,
            worktree_cleanup,
        ),
        as_json=parsed.json,
    )
    return ReleaseEnding.RELEASED


def _verify_and_close_merged_release(
    context: RunContext,
    identity: protocol.ClaimIdentity,
    merged: protocol.MergedRelease,
    selected: protocol.ActiveClaim,
) -> _LandedLane:
    """A github `--merged` release's forge half, run once `selected` is
    resolved and its releaser authorized (issue #359 R1): an unauthorized
    or mismatched-claim release never reaches the forge at all, so it can
    neither verify, comment on, nor close a pull request's issue. `storage`
    is proven `github` here (`release_transition` returned for
    `state-ref`), so the cast is honest: `session._build_forge` builds exactly a
    `github.GitHubForge` for every other storage pin. The close runs before
    the release transition: a close failure -- a transient forge error,
    most often -- raises before that transition ever runs, so the claim it
    would have released stays exactly as live as it was."""
    client = cast(github.GitHubForge, context.forge)
    storage = context.config.storage
    verified = _verify_merged_release(context, client, identity, merged)
    _refuse_a_claim_the_landing_did_not_land(
        selected,
        f"pull request #{merged.pull_request}",
        verified.source_branch,
        verified.trunk_since_landing,
        storage,
    )
    pending_close = _pending_landing_close(client, identity, merged.pull_request)
    if pending_close is not None:
        client.close_landed_item(pending_close.issue, pull_request=pending_close.pull_request)
    return _landed_lane(
        identity, storage, str(merged.pull_request), selected.branch, verified.landed_head
    )


def _report_merged_pull_request_left_nothing(
    parsed: argparse.Namespace,
    context: RunContext,
    identity: protocol.ClaimIdentity,
    merged: protocol.MergedRelease,
    unclaimed_branch: str,
) -> None:
    """REL-47 under `storage = "github"` (issue #605): the merge verifies
    exactly as a release's would, then only its lane is cleaned up -- on
    the pull request's own source branch, the one fact the forge records
    about where the lane lived (REL-50). An issue-less lane's claim is keyed
    by its branch, so `unclaimed_branch` -- the branch found unclaimed --
    says nothing about another lane's claim unless the pull request came
    from it: otherwise REL-09 refuses as before, and that lane is never
    cleaned up behind its live claim."""
    client = cast(github.GitHubForge, context.forge)
    storage = context.config.storage
    verified = _verify_merged_release(context, client, identity, merged)
    if isinstance(identity, protocol.LaneIdentity) and unclaimed_branch != verified.source_branch:
        raise board.no_active_build_claim(identity, unclaimed_branch, storage)
    _report_nothing_left_to_release(
        parsed,
        context,
        f"pull request #{merged.pull_request}",
        _landed_lane(
            identity,
            storage,
            str(merged.pull_request),
            verified.source_branch,
            verified.landed_head,
        ),
        context.fetched_default_branch_ref,
    )


_NOTHING_LEFT_TO_RELEASE = "nothing left to release"


def _report_nothing_left_to_release(
    parsed: argparse.Namespace,
    context: RunContext,
    landing: str,
    lane: _LandedLane,
    fetched_trunk_ref: Callable[[], str],
) -> None:
    """REL-49/REL-51 (issue #605): a verified landing whose release already
    happened closes nothing and writes no claim state; it reports that, then
    its lane's cleanup, which may still be due in this checkout."""
    worktree = _worktree_cleanup_outcome_text(
        _cleanup_unless_claimed_again(parsed, context, lane, fetched_trunk_ref)
    )
    if parsed.json:
        report.emit_json(
            True,
            _ReleaseReason.MERGED,
            outcome=_NOTHING_LEFT_TO_RELEASE,
            **protocol.identity_json(lane.identity),
            branch=lane.branch,
            worktree=worktree,
        )
        return
    print(f"LANDED {landing} already; {_NOTHING_LEFT_TO_RELEASE}")
    print(f"worktree: {worktree}")


def _cleanup_unless_claimed_again(
    parsed: argparse.Namespace,
    context: RunContext,
    lane: _LandedLane,
    fetched_trunk_ref: Callable[[], str],
) -> checkout.WorktreeCleanupOutcome:
    """REL-55 (issue #605): the missing claim REL-47 acts on was read before
    the landing's verification; a claim taken meanwhile -- `start` reopening
    the lane on its clean worktree (START-11) -- would lose that worktree to
    this cleanup, so the claim state is read afresh right before it. A claim
    taken between that read and the removal is the accepted residual: the
    worktree removed is clean and merged, and `start` builds it again."""
    if not parsed.keep_worktree:
        fresh = context.observed_afresh().observation
        if _unclaimed_rerun_branch(parsed, fresh, lane.identity, lane.branch) is None:
            return checkout.worktree_cleanup_kept(_WORKTREE_KEPT_CLAIMED_AGAIN_REASON)
    return _cleanup_landed_worktree(parsed, lane, context, fetched_trunk_ref)


@dataclass(frozen=True)
class _LandedLane:
    """The lane a `--merged` release cleans up after (issue #605): its
    identity and branch, the head the forge recorded as landed (`None` under
    `storage = "state-ref"`, which has no pull request), and `rerun`, the
    `release` line that repeats this cleanup in the checkout holding its
    worktree (REL-52)."""

    identity: protocol.ClaimIdentity
    branch: str
    landed_head: str | None
    rerun: str


def _landed_lane(
    identity: protocol.ClaimIdentity,
    storage: body.Storage,
    landing: str,
    branch: str,
    landed_head: str | None,
) -> _LandedLane:
    """`branch`'s lane, landed as `landing` -- a pull request number, or the
    verified trunk sha under `storage = "state-ref"` -- with its `rerun`
    rendered by the one advice renderer, so it runs as printed under either
    storage pin and from any checkout (REL-52)."""
    item = (
        ()
        if isinstance(identity, protocol.LaneIdentity)
        else (board.item_argument(identity.issue, storage),)
    )
    rerun = board.advice_command("release", *item, "--merged", landing, "--branch", branch)
    return _LandedLane(identity, branch, landed_head, rerun)


_WORKTREE_KEPT_FLAG_REASON = "--keep-worktree was given"


_WORKTREE_KEPT_RAN_FROM_INSIDE_REASON = "release ran from inside it"


_WORKTREE_KEPT_CLAIMED_AGAIN_REASON = "claimed again while this release ran"


# Names the repair (issues #578, #605): a landing from a separate clone holds
# no lane worktree, which stays where it lives for that checkout to remove.
_WORKTREE_KEPT_NO_WORKTREE_REASON = (
    "no linked worktree on {branch} in this checkout; run {rerun} in the checkout that holds it"
)


def _worktree_cleanup_outcome_text(outcome: checkout.WorktreeCleanupOutcome) -> str:
    """The tail `release` prints after `worktree: ` in text, and the exact
    `--json` value of its `worktree` field (issue #322 review/gate finding
    4): one owner, so the two shapes can never drift apart. A branch-deletion
    failure after the worktree is already gone names both halves -- never a
    bare `kept`, which would hide that the worktree itself is gone; a
    section git kept after the branch itself went names that section, never
    the branch (issue #578); a delete that timed out reads the branch
    unknown, never kept (issue #603)."""
    if not outcome.worktree.removed:
        return f"kept -- {outcome.worktree.reason}"
    if outcome.branch.fate is not checkout.BranchFate.REMOVED:
        return f"removed; branch {outcome.branch.fate} -- {outcome.branch.reason}"
    section_kept = outcome.branch.section_kept
    if section_kept is not None:
        return f"removed; {section_kept.section} section kept -- {section_kept.reason}"
    return "removed"


def _cleanup_landed_worktree(
    parsed: argparse.Namespace,
    lane: _LandedLane,
    context: RunContext,
    fetched_trunk_ref: Callable[[], str],
) -> checkout.WorktreeCleanupOutcome:
    """After a successful `--merged` release, remove the lane's local
    worktree and local branch when both are safe to remove, and report
    exactly what happened either way (issue #322 review/gate finding 4):
    loud for every outcome, never a silent decline. `--keep-worktree` opts
    out outright; a worktree cannot remove its own cwd, so a release running
    from inside it keeps both; neither needs `checkout.py`'s own merged/
    elsewhere/dirty/removal policy (`checkout.cleanup_landed_worktree`),
    since both read only this process's own cwd and worktree listing. A git
    failure at any step -- including one resolving which worktree matches
    `branch` at all -- is reported in the same `kept` line rather than
    swallowed. The run's own checkout is judged from the toplevel its
    context already holds, never resolved a second time (issue #472).
    `fetched_trunk_ref` names the ref the lane must be merged into -- the
    one its release judged the landing on (issue #492) -- asked only here,
    so a failure to resolve it reads as `kept` too. The remote branch
    stays the forge merge's own business either way."""
    if parsed.keep_worktree:
        return checkout.worktree_cleanup_kept(_WORKTREE_KEPT_FLAG_REASON)
    branch = lane.branch
    try:
        toplevel = context.toplevel
        others = tuple(path for path in store.list_worktrees(toplevel) if path != toplevel)
        if checkout.current_branch(directory=toplevel) == branch:
            return checkout.worktree_cleanup_kept(_WORKTREE_KEPT_RAN_FROM_INSIDE_REASON)
        matching = checkout.worktree_on_branch(others, branch)
        if matching is None:
            return checkout.worktree_cleanup_kept(
                _WORKTREE_KEPT_NO_WORKTREE_REASON.format(branch=branch, rerun=lane.rerun)
            )
        return checkout.cleanup_landed_worktree(
            matching,
            branch,
            trunk=fetched_trunk_ref(),
            landed_head=lane.landed_head,
            directory=toplevel,
        )
    except protocol.ClaimError as error:
        return checkout.worktree_cleanup_kept(f"git failure: {error}")


def _cmd_release_landed(
    parsed: argparse.Namespace,
    context: RunContext,
    identity: protocol.ClaimIdentity,
    storage: body.Storage,
    release_branch: str | None,
) -> ReleaseEnding:
    """`release --merged <sha|empty>` under `storage = "state-ref"` (issue
    #359, LAND-47/LAND-52): the trunk walk (`checkout.trunk_landings`,
    issue #304) names, or verifies, the landing commit; one
    `protocol.LandingIntent` then closes the item and releases the claim in
    one commit, one CAS -- `run`'s own `ReleaseIntent` path never
    runs for this storage pin's `--merged`. With no live claim left to
    release (REL-47) it writes nothing and only cleans up, and a claim the
    landing did not land refuses (REL-48, REL-53), both before the item
    write is prepared: that write refuses an item the landing already
    closed (issues #605, #611).
    """
    if not isinstance(identity, protocol.IssueIdentity):
        raise protocol.ClaimUnavailableError(
            "--merged under storage = state-ref requires an issue number; "
            "an issue-less lane has no item to close"
        )
    trunk_ref = context.trunk_ref
    landings = checkout.trunk_landings(
        trunk_ref, board_read.TRUNK_LANDING_DEPTH, directory=context.toplevel
    )
    commit = checkout.landed_commit(
        landings, identity.issue, cast(str, parsed.merged), context.config.storage
    )
    # `storage` is already proven `state-ref` by `release_transition`, so
    # this cast is honest: `session._build_forge` builds exactly a `StateRefBoard`.
    client = cast(state_board.StateRefBoard, context.forge)
    if client.item_reference(identity.issue).state is forge.ItemState.MISSING:
        raise protocol.ClaimUnavailableError(
            board_read.missing_item_refusal(identity.issue, client, context.config.storage)
        )
    observed = context.observation
    protocol.require_state_ref(observed)
    unclaimed_branch = _unclaimed_rerun_branch(parsed, observed, identity, release_branch)
    landing_label = f"commit {commit}"
    if unclaimed_branch is not None:
        _report_nothing_left_to_release(
            parsed,
            context,
            landing_label,
            _landed_lane(identity, storage, commit, unclaimed_branch, None),
            context.fetched_trunk_ref,
        )
        return ReleaseEnding.NOTHING_LEFT
    resolved = _resolve_release_claimant(parsed, observed, identity, release_branch, storage)
    _refuse_a_claim_the_landing_did_not_land(
        resolved.selected,
        landing_label,
        resolved.selected.branch if parsed.branch is None else parsed.branch,
        checkout.trunk_since(landings, commit),
        storage,
    )
    write = client.prepare_landing(identity.issue)
    worktree = context.toplevel
    new_oid = store.hash_blob(worktree, write.content)
    outcome = protocol.LandedRelease(commit=protocol.ObjectId(commit))
    intent = protocol.LandingIntent(
        item_id=write.item_id,
        item_expected=write.expected,
        item_new_oid=new_oid,
        claim_id=resolved.selected.claim_id,
        agent=parsed.agent,
        role=resolved.resolved_role,
        outcome=outcome,
        operation_id=uuid.uuid4().hex,
        coordinator_override=parsed.coordinator_override,
    )
    new_state = context.transition(
        store.transition_subject("release", resolved.selected.identity, resolved.selected.branch),
        intent,
    )
    client.mark_landed(write, new_oid)
    landing = _landing_report(context, identity, new_state, storage, trunk_ref)
    worktree_cleanup = _cleanup_landed_worktree(
        parsed,
        _landed_lane(identity, storage, commit, resolved.selected.branch, None),
        context,
        context.fetched_trunk_ref,
    )
    _print_release_result(
        _ReleaseReport(
            resolved.selected,
            parsed.agent,
            resolved.resolved_role,
            outcome,
            landing,
            storage,
            worktree_cleanup,
        ),
        as_json=parsed.json,
    )
    return ReleaseEnding.RELEASED


def _landing_report(
    context: RunContext,
    identity: protocol.ClaimIdentity,
    new_state: store.ClaimState,
    storage: body.Storage,
    trunk_ref: str,
) -> _ReleaseLanding | str:
    """The landing `run` prints once its release transition
    already committed (issue #256), or the hint that replaces it: a forge
    hiccup here, or a state-ref store the board read refuses (issue #447),
    can only ever downgrade the report, never undo or fail that release."""
    landed = (
        board.IssueReference(context.forge.repository.path, identity.issue)
        if isinstance(identity, protocol.IssueIdentity)
        else None
    )
    return board_read.board_read_after_write(
        lambda: _release_landing(context, new_state, landed, storage, trunk_ref)
    )


@dataclass(frozen=True)
class _ReleaseReport:
    """Everything `_print_release_result` needs to render one `release`
    outcome (issue #256), bundled so the printer itself takes one argument
    instead of PLR0913's five-scalar ceiling: the just-released claim, the
    caller identity that performed it, the outcome it recorded, and the
    merged-landing board read, or the hint that replaced it (issue #546),
    `None` for a release that runs no board read. `worktree` is the merged
    outcome's own cleanup result (issue #322 review finding 4), `None` for
    `--abandoned`, which never attempts cleanup at all."""

    selected: protocol.ActiveClaim
    agent: str
    role: str | None
    outcome: protocol.ReleaseOutcome
    landing: _ReleaseLanding | str | None
    storage: body.Storage
    worktree: checkout.WorktreeCleanupOutcome | None


def _print_release_result(release_report: _ReleaseReport, *, as_json: bool) -> None:
    selected, landing = release_report.selected, release_report.landing
    if as_json:
        _release_json(release_report)
        if isinstance(landing, str):
            board_read.print_board_read_hint(landing, as_json=True)
        return
    print(f"RELEASED {board.claim_subject(selected, release_report.storage)}: {selected.claim_id}")
    if isinstance(landing, str):
        board_read.print_board_read_hint(landing, as_json=False)
    elif landing is not None:
        print(board.release_freed_line(landing.freed, release_report.storage))
        print(_release_next_line(landing.next_item, release_report.storage))
        parent_line = board.parent_closable_line(landing.parent_closable, release_report.storage)
        if parent_line is not None:
            print(parent_line)
    if release_report.worktree is not None:
        print(f"worktree: {_worktree_cleanup_outcome_text(release_report.worktree)}")


def _release_branch_for(parsed: argparse.Namespace) -> str | None:
    if parsed.coordinator_override:
        protocol.require_coordinator_override(parsed.role)
    if parsed.branch is not None:
        # An explicit --branch selects the lane identity by name, exactly
        # like claim's own --branch, but never requires the checkout to be
        # on it (issue #250): a lane's worktree may be gone, or the release
        # may run from the coordinator's primary checkout.
        return parsed.branch
    if parsed.issue is not None and parsed.claim_id is not None:
        return None
    release_branch = checkout.current_branch()
    if release_branch:
        return release_branch
    if parsed.issue is None:
        raise protocol.ClaimUnavailableError(
            "lane release requires a non-empty current branch; "
            "check out the docs/ or fix/ lane branch, or pass "
            "an issue number"
        )
    raise protocol.ClaimUnavailableError(
        "release without --claim-id requires a non-empty current branch; pass --claim-id"
    )
