"""Coordinate coding-agent claims through this repository's own state ref."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import threading
import tomllib
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, NoReturn, TypeVar, cast

from . import (
    __version__,
    board,
    board_html,
    board_serve,
    body,
    checkout,
    forge,
    github,
    items,
    metrics,
    protect,
    protocol,
    providers,
    state_board,
    store,
    terminal,
    workspace,
)
from .session import RepoMeaninglessUnderStateRefError, RunContext, board_config

CLI_ERROR_PREFIX = "ERROR: "

DEFAULT_CLAIM_ROLE = "builder"
NEXT_PULL_DESCRIPTION = (
    "Pulling is not dispatching: an item whose expectations are still unruled is "
    "named here with refining as its first step, while dispatching a builder onto "
    "it waits for the operator's ruling. An item labelled "
    f"{board.NEEDS_OPERATOR_LABEL} waits on the operator and is never pulled; set or "
    f"remove the label with gh issue edit <n> --add-label/--remove-label "
    f"{board.NEEDS_OPERATOR_LABEL}, or under storage = state-ref by piping a body whose "
    "[record] labels add or drop it to aco item edit <item-id>."
)
CLAIM_DESCRIPTION = (
    "Refuses before the first edit unless the checkout is a linked, isolated "
    "worktree on a non-main branch, the tree is clean, and every --scope value is "
    "a repository-relative path."
)
WHOLE_HELP = (
    "one sentence why this wide scope does not split; required for more than "
    "three paths, any directory, or, once the repository has at least twelve "
    "versioned files, more than a quarter of them"
)
ITEM_WHOLE_HELP = (
    "one sentence justifying this item's own wide scope, stored in its body; "
    "claim/start read it as --whole's own fallback when the call itself names none"
)
# `item new`'s own projection flags (issue #555), keyed by the block key
# each one writes.
ITEM_NEW_PROJECTION_FLAGS = (
    ("now", "the block's now: where the item stands"),
    ("next", "the block's next: the one concrete next step"),
    ("done_when", "the block's done_when: the checkable finish"),
)
NOT_A_TWIN_HELP = "create even though an open or recently closed issue carries a similar title"
START_DESCRIPTION = (
    "Creates the item's linked worktree and branch from the canonical remote's own trunk "
    "when neither exists yet, then claims it exactly as aco claim would; a second call "
    "against the same worktree only claims, or reports the live claim."
)
# Shared by `claim` and `start` (issue #322 review finding): CLM-09's own contract, spelled
# once so `start`'s pass-through help text can never drift from what `claim` itself does with
# the flag -- specs/claim.spec.md:62's "the reason is never stored" holds for both.
OUT_OF_ORDER_HELP = (
    "refuses a claim without a reason when a higher-priority actionable item is free or "
    "an open blocker remains; the reason is never stored, only downgrading the check"
)


def _resolved_identity(issue: int | None, branch: str) -> protocol.ClaimIdentity:
    """Resolve the CLI's discriminated identity: an explicit issue, or a lane.

    Omitting the positional issue number means lane mode, derived from `branch`
    (the same checkout branch `--base`/`--branch` auto-fill and the release
    branch-matching fallback already use). Lane mode is refused outright unless
    `branch` follows the issueless-lane convention, so a builder who simply forgot
    the issue number never gets a silent, unlabeled, non-projected lane claim.
    """
    if issue is not None:
        return protocol.IssueIdentity(issue)
    if not branch.startswith(protocol.ISSUELESS_LANE_BRANCH_PREFIXES):
        prefixes = " or ".join(repr(prefix) for prefix in protocol.ISSUELESS_LANE_BRANCH_PREFIXES)
        raise protocol.ClaimError(
            f"branch {branch!r} is not an issueless lane; pass an issue number, or "
            f"check out a branch prefixed {prefixes}"
        )
    return protocol.LaneIdentity()


def _claim_subject(claim: protocol.ScopedClaim, storage: body.Storage) -> str:
    return (
        f"lane {claim.branch}"
        if isinstance(claim.identity, protocol.LaneIdentity)
        else f"issue {board.item_label(claim.identity.issue, storage)}"
    )


def _claim_age_fields(opened_at: datetime, now: datetime) -> tuple[str, bool]:
    """The rendered age and old-ness of a claim opened at `opened_at` (a
    commit's committer date -- issue #176, §1 -- not a ledger comment
    timestamp)."""
    age = now.astimezone(UTC) - opened_at.astimezone(UTC)
    return board.format_claim_age(age), board.claim_is_old(age)


def _claim_age_suffix(opened_at: datetime, now: datetime) -> str:
    rendered, old = _claim_age_fields(opened_at, now)
    return f" {rendered} old" if old else f" {rendered}"


def _scope_cost(versioned: tuple[str, ...], scope: tuple[str, ...]) -> tuple[int, int, float]:
    n = len(checkout.paths_under_scope(versioned, scope))
    total = len(versioned)
    share = 0.0 if total == 0 else n / total
    return n, total, share


def _optional_whole_reason(arguments: argparse.Namespace) -> str | None:
    raw = getattr(arguments, "whole", None)
    if raw is None:
        return None
    return protocol._outbound_text(raw, _WHOLE_REASON_LABEL, maximum=512)


def _wide_scope_condition(trip: protocol.WideScopeTrip) -> str:
    """The tripped condition in words, with the numbers it was judged
    against -- what the refusal names instead of restating the whole rule."""
    if trip.reason is protocol.WideScopeReason.PATH_COUNT:
        return f"{trip.path_count} paths exceeds three"
    if trip.reason is protocol.WideScopeReason.DIRECTORY:
        noun = "directory" if len(trip.directories) == 1 else "directories"
        return f"{len(trip.directories)} {noun} in scope ({', '.join(trip.directories)})"
    covered, total = trip.covered_file_count, trip.versioned_file_count
    percent = round(100 * covered / total)
    path_word = "path" if covered == 1 else "paths"
    return f"{covered} {path_word} of {total} versioned files ({percent} %) exceeds a quarter"


_WHOLE_REASON_LABEL = "whole reason"
"""`protocol._outbound_text`'s own field name for a `--whole`/body `whole` value
(issue #399), named once here beside the width-gate refusal it justifies --
`_optional_whole_reason`, `_cmd_item_edit_whole` and `_whole_from_item_body`
all bound the same reason text and must refuse by the same name."""


def _wide_scope_refusal(trip: protocol.WideScopeTrip, *, names_body_fallback: bool) -> str:
    suffix = " or set whole in the body" if names_body_fallback else ""
    return f"scope is wide: {_wide_scope_condition(trip)}; pass --whole REASON{suffix}"


def _reject_wide_scope(
    scope: tuple[str, ...],
    versioned: tuple[str, ...],
    whole_reason: str | None,
    *,
    directories: tuple[str, ...],
    whole_from_body: Callable[[], str | None] | None = None,
) -> tuple[int, int, float, str | None]:
    """`scope`'s own width gate (issue #326), `whole_reason`'s own text
    admitting a wide one exactly as before -- plus, for `claim`/`start`
    (issue #399), the effective reason actually used: `whole_reason` itself
    when given, else `whole_from_body()`, read only once the gate actually
    trips and the caller named none, so a narrow scope or an explicit
    `--whole` never costs the body read `whole_from_body` performs.
    `rescope` passes no `whole_from_body` at all, and keeps the plain
    refusal: it never reads an item's own body for this. `directories` are
    the scope entries that name a directory in the tree `versioned` lists
    (`checkout._scope_directories`)."""
    n, total, share = _scope_cost(versioned, scope)
    trip = protocol.wide_scope_trip(
        scope, directories=directories, covered_file_count=n, versioned_file_count=total
    )
    if trip is None:
        return n, total, share, whole_reason
    effective = whole_reason
    if effective is None and whole_from_body is not None:
        effective = whole_from_body()
    if effective is None:
        raise protocol.ClaimError(
            _wide_scope_refusal(trip, names_body_fallback=whole_from_body is not None)
        )
    return n, total, share, effective


def _reject_ungrounded_comma_scope(
    scope: tuple[str, ...], versioned: tuple[str, ...], *, flag: str
) -> None:
    """Refuse a scope entry that contains a comma and matches no versioned
    file -- the shape `{flag} a.py,b.py` takes when passed as one flag from
    habit instead of one path per flag (issue #207). Stored verbatim, that
    single entry names a path nothing tracks, so the claim protects nothing:
    the lane's real files stay uncovered and no overlap check can ever fire
    for them.

    Never call this over `--drop`: a value the live claim already holds is a
    fact about the claim, not a typo about the checkout, and dropping a value
    the claim does not hold is already refused by `_combined_scope` with a
    truer sentence naming the claim rather than the checkout -- adding this
    check there would either block the very repair this refusal exists to
    leave open (dropping an already-claimed ungrounded value), or never fire
    at all (a not-yet-dropped value the claim lacks is refused first).

    Splitting on the comma would be the old, wrong fix (issue #201): it made
    a real comma-bearing filename unrepresentable. So a real comma-bearing
    path that names a versioned file, or a directory holding one, still
    passes here -- `paths_under_scope` matches either. So does a comma-free
    path that does not exist yet, since a lane routinely claims files it is
    about to create; only a comma with no match among versioned files is the
    signature this refuses.
    """
    for entry in scope:
        if "," in entry and not checkout.paths_under_scope(versioned, (entry,)):
            raise protocol.ClaimError(
                f"{entry!r} matches no versioned file; one {flag} path per flag, so its comma "
                f"is read literally -- repeat {flag} for a second path"
            )


def _touch_json(claim: protocol.ScopedClaim) -> dict[str, object]:
    return {
        **_identity_json(claim.identity),
        "claim_id": claim.claim_id,
        "agent": claim.agent,
        "scope": list(claim.scope),
    }


def _touch_line(
    own_scope: tuple[str, ...], claim: protocol.ScopedClaim, storage: body.Storage
) -> str:
    """One overlapping claim, named with the paths where its scope meets
    `own_scope` -- the fact a claimant needs to know they hold both scopes
    at once, not only the other item's name (issue #206)."""
    meeting = protocol.scope_overlap_paths(own_scope, claim.scope)
    return f"{_claim_subject(claim, storage)} on {protocol.named_with_overflow_count(meeting)}"


def _touch_summary(
    own_scope: tuple[str, ...], touches: tuple[protocol.ScopedClaim, ...], storage: body.Storage
) -> str:
    if not touches:
        return "overlaps no other open claims"
    return "overlaps " + ", ".join(_touch_line(own_scope, claim, storage) for claim in touches)


def _claim_cost_line(
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
    `_cmd_claim` (issue #337, which needs it before `_request` builds a full
    request) both bind to, so it stays a single owner rather than two copies
    of the same git call and validation."""
    branch = (
        checkout.attached_branch(checkout.current_branch(directory=directory))
        if arguments.branch is None
        else arguments.branch
    )
    return protocol._valid_branch({"branch": branch})


def _request(
    arguments: argparse.Namespace,
    *,
    default_branch: Callable[[], str | None],
    directory: Path | None = None,
) -> protocol.ClaimRequest:
    """`_claim_request`, checked against the checkout it is made in:
    `claim` stands in the worktree it claims, judged against the canonical
    remote's recorded default branch `default_branch` answers (issue
    #490)."""
    request = _claim_request(arguments, directory=directory)
    checkout._validate_checkout(request, default_branch=default_branch, directory=directory)
    return request


def _claim_request(
    arguments: argparse.Namespace, *, directory: Path | None = None
) -> protocol.ClaimRequest:
    """The `ClaimRequest` `claim` submits, `arguments.scope` bound as-is
    when given. Omitted -- issue mode only (issue #337); lane mode still
    refuses it, `_checked_claim`'s own first check -- it binds the empty
    tuple instead of raising: `_checked_claim` replaces it with the item's
    own body scope, or a live claim's stored scope on replay, before this
    request's scope ever reaches a wide-scope check or a write. With
    `--base` and `--branch` given it reads no checkout at all (issue #479:
    `start` checks its claim before the worktree exists)."""
    agent = protocol._outbound_text(checkout.resolved_agent(arguments.agent), "agent", maximum=128)
    role = protocol._outbound_text(arguments.role, "role", maximum=64)
    base = (
        checkout._git_output(["rev-parse", "HEAD"], directory=directory)
        if arguments.base is None
        else arguments.base
    )
    if protocol.COMMIT_PATTERN.fullmatch(base) is None:
        raise protocol.ClaimError("base must be a full lowercase commit SHA")
    branch = _resolved_claim_branch(arguments, directory=directory)
    issue = _optional_issue_number(arguments.issue)
    identity = _resolved_identity(issue, branch)
    claim_id = arguments.claim_id or uuid.uuid4().hex
    protocol.ClaimId(claim_id)
    whole_reason = _optional_whole_reason(arguments)
    resource = getattr(arguments, "resource", None)
    if resource is not None:
        resource = protocol._outbound_resource_name(resource)
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


LANE_ISSUE_HELP = "omit for lane mode, derived from a docs/ or fix/ checkout branch"
JSON_FLAG = "--json"
LONG_OPTION_PREFIX = "--"
JSON_HELP = "print the result as JSON instead of the human lines"
# `--html` with no value: `argparse`'s `nargs="?"` const, distinct from the
# `None` default (flag absent) -- `_cmd_board_html` treats it as "stdout".
STDOUT_HTML_PATH = ""
AGENT_HELP = (
    f"the acting agent's name; filled from a non-empty {checkout.IDENTITY_ENVIRONMENT_ORDER} "
    "when omitted"
)
EXPECTED_CLAIM_ID_HELP = (
    "assert which claim you are acting on; the issue number or lane branch selects it, and "
    "a differing id is refused rather than redirected"
)
ROLE_ON_LIVE_CLAIM_HELP = (
    "the acting role; the selected claim's own role when omitted, and required to be "
    "coordinator with --coordinator-override"
)
ITEM_REF_HELP = "an item, as aco-xxxxxx, #n, or the bare number n"


def _add_bootstrap_parser(commands: argparse._SubParsersAction) -> None:
    commands.add_parser("bootstrap", help="create refs/aco/state if it does not exist yet")


def _add_reset_parser(commands: argparse._SubParsersAction) -> None:
    reset = commands.add_parser(
        "reset",
        help="export refs/aco/state, delete it remotely and locally, and bootstrap fresh",
    )
    reset.add_argument(
        "--confirm", action="store_true", help="perform the reset; omit for a dry run"
    )
    reset.add_argument(
        "--no-export", action="store_true", help="skip the otherwise-mandatory bundle export"
    )
    reset.add_argument(
        "--export-dir",
        type=Path,
        metavar="DIR",
        help="directory for the export bundle (default: the repository's parent directory)",
    )
    reset.add_argument(
        "--force-unreadable",
        action="store_true",
        help="with --confirm, also reset a state whose schema this aco cannot read",
    )


def _add_json_flag(container: argparse._ActionsContainer) -> None:
    """The one place `--json` is declared (issue #432): a command that never
    calls this has no JSON mode at all, and `main` matches this same
    spelling when argparse refuses before any namespace is filled, so the
    flag needs a single owner."""
    container.add_argument(JSON_FLAG, action="store_true", help=JSON_HELP)


def _add_status_parser(commands: argparse._SubParsersAction) -> None:
    status = commands.add_parser("status", help="show repository-wide build claims")
    status.add_argument(
        "issue",
        type=board.parse_item_reference,
        nargs="?",
        help="show only this issue's claims and the ones they overlap",
    )
    status.add_argument(
        "--path", metavar="PATH", help="list holders of this path instead of by issue"
    )
    _add_json_flag(status)


def _add_board_parser(commands: argparse._SubParsersAction) -> None:
    board_command = commands.add_parser(
        "board", help="project the open work board; only --serve writes"
    )
    output = board_command.add_mutually_exclusive_group()
    _add_json_flag(output)
    output.add_argument(
        "--html",
        nargs="?",
        const=STDOUT_HTML_PATH,
        default=None,
        metavar="PATH",
        help="write a static HTML board page (stdout when PATH is omitted)",
    )
    output.add_argument(
        "--serve",
        action="store_true",
        help=(
            "serve the board page on 127.0.0.1 with a one-click aco-rule form per "
            "expectation line (issue #280); a write command, so it needs the writer"
        ),
    )
    board_command.add_argument(
        "--port",
        type=int,
        default=0,
        metavar="PORT",
        help="loopback port for --serve; 0 (default) picks an ephemeral one",
    )
    board_command.add_argument(
        "--new-token",
        action="store_true",
        help=(
            "mint a fresh persistent loopback token for --serve (issue #388), "
            "replacing this board's own at "
            "${XDG_CONFIG_HOME:-~/.config}/aco/boards/<board>/token"
        ),
    )


def _add_rulings_parser(commands: argparse._SubParsersAction) -> None:
    rulings_command = commands.add_parser(
        "rulings", help="list expectation lines, open and ruled, without writes"
    )
    _add_json_flag(rulings_command)


def _add_next_parser(commands: argparse._SubParsersAction) -> None:
    next_command = commands.add_parser(
        "next",
        help="name the board's top-priority item to pull",
        description=NEXT_PULL_DESCRIPTION,
    )
    _add_json_flag(next_command)


def _add_start_parser(commands: argparse._SubParsersAction) -> None:
    start = commands.add_parser(
        "start",
        help="create an item's linked worktree and branch, then claim it",
        description=START_DESCRIPTION,
    )
    start.add_argument("item", type=board.parse_item_reference, help=ITEM_REF_HELP)
    start.add_argument(
        "--scope",
        action="append",
        help=(
            "a repository-relative path; repeat --scope for more than one path; the item's "
            "own body scope when omitted"
        ),
    )
    start.add_argument(
        "--slug",
        help=(
            "the worktree/branch slug; derived from the item's title (lowercase, at most "
            "40 characters) when omitted"
        ),
    )
    start.add_argument("--whole", metavar="REASON", help=WHOLE_HELP)
    start.add_argument("--out-of-order", metavar="REASON", help=OUT_OF_ORDER_HELP)


def _add_claim_parser(commands: argparse._SubParsersAction) -> None:
    claim = commands.add_parser(
        "claim",
        help="claim an issue and scope before editing",
        description=CLAIM_DESCRIPTION,
    )
    claim.add_argument(
        "issue",
        type=board.parse_item_reference,
        nargs="?",
        help=LANE_ISSUE_HELP,
    )
    claim.add_argument("--agent", help=AGENT_HELP)
    claim.add_argument(
        "--role",
        default=DEFAULT_CLAIM_ROLE,
        help=f"the claiming role; default {DEFAULT_CLAIM_ROLE}",
    )
    claim.add_argument(
        "--base",
        help="the full commit SHA this lane starts from; the current HEAD when omitted",
    )
    claim.add_argument(
        "--branch", help="the lane's branch; the current checkout branch when omitted"
    )
    claim.add_argument(
        "--scope",
        action="append",
        help=(
            "a repository-relative path; repeat --scope for more than one path; issue mode "
            "takes it from the item's own body when omitted, and refuses a value whose set "
            "differs from it; lane mode always requires it"
        ),
    )
    claim.add_argument(
        "--claim-id",
        help=(
            "this claim's own id; generated when omitted, and repeating an identical claim "
            "with it returns the active claim instead of writing a second one"
        ),
    )
    claim.add_argument("--out-of-order", metavar="REASON", help=OUT_OF_ORDER_HELP)
    claim.add_argument(
        "--whole",
        metavar="REASON",
        help=WHOLE_HELP,
    )
    claim.add_argument(
        "--resource",
        metavar="NAME",
        help="allocate the next free value of this named scarce resource and hold it",
    )
    _add_json_flag(claim)


def _add_release_parser(commands: argparse._SubParsersAction) -> None:
    release = commands.add_parser("release", help="release a landed or abandoned claim")
    release.add_argument(
        "issue",
        type=board.parse_item_reference,
        nargs="?",
        help=LANE_ISSUE_HELP,
    )
    release.add_argument("--agent", help=AGENT_HELP)
    release.add_argument("--role", help=ROLE_ON_LIVE_CLAIM_HELP)
    release.add_argument(
        "--branch",
        help=(
            "the claim's lane branch; selects it without checking out that branch, unlike "
            "claim's --branch, and defaults to the current checkout branch when omitted "
            "together with --claim-id"
        ),
    )
    outcome = release.add_mutually_exclusive_group(required=True)
    outcome.add_argument(
        "--merged",
        nargs="?",
        const="",
        metavar="PULL_REQUEST_OR_SHA",
        help=(
            "the pull request that landed this claim's item on the default branch "
            "(storage = github), or the trunk commit that did (storage = state-ref; "
            "bare --merged picks the newest trunk commit naming this item)"
        ),
    )
    outcome.add_argument(
        "--abandoned",
        metavar="REASON",
        help="why this claim ends without a landing",
    )
    release.add_argument("--claim-id", help=EXPECTED_CLAIM_ID_HELP)
    release.add_argument(
        "--coordinator-override",
        action="store_true",
        help="release another agent's claim as the coordinator; requires --role coordinator",
    )
    release.add_argument(
        "--keep-worktree",
        action="store_true",
        help=(
            "keep the lane's local worktree and branch after a merged landing; by default a "
            "clean worktree whose branch is already merged is removed"
        ),
    )
    _add_json_flag(release)


def _add_land_parser(commands: argparse._SubParsersAction) -> None:
    land = commands.add_parser(
        "land",
        help="merge a green pull request with its pinned head sha and run its release path",
    )
    land.add_argument("pull_request", type=int, help="the pull request number to land")
    land.add_argument("--agent", help=AGENT_HELP)
    land.add_argument("--role", help=ROLE_ON_LIVE_CLAIM_HELP)
    land.add_argument(
        "--coordinator-override",
        action="store_true",
        help="land another agent's claim as the coordinator; requires --role coordinator",
    )
    land.add_argument(
        "--keep-worktree",
        action="store_true",
        help=(
            "keep the lane's local worktree and branch after landing; by default a clean "
            "worktree whose branch is already merged is removed"
        ),
    )


def _add_rescope_parser(commands: argparse._SubParsersAction) -> None:
    rescope = commands.add_parser(
        "rescope", help="add or drop paths on a live claim without releasing"
    )
    rescope.add_argument(
        "issue",
        type=board.parse_item_reference,
        nargs="?",
        help=LANE_ISSUE_HELP,
    )
    rescope.add_argument("--agent", help=AGENT_HELP)
    rescope.add_argument(
        "--add",
        action="append",
        help="an absolute path to add; repeat --add for more than one path",
    )
    rescope.add_argument(
        "--drop",
        action="append",
        help="an absolute path to drop; repeat --drop for more than one path",
    )
    rescope.add_argument("--claim-id", help=EXPECTED_CLAIM_ID_HELP)
    rescope.add_argument(
        "--whole",
        metavar="REASON",
        help=WHOLE_HELP,
    )
    _add_json_flag(rescope)


def _add_cut_parser(commands: argparse._SubParsersAction) -> None:
    cut = commands.add_parser("cut", help="create a container's next slice as a fresh child issue")
    cut.add_argument("issue", type=board.parse_item_reference, help="the container to cut")
    cut.add_argument(
        "--title", required=True, type=_nonblank_title, help="the fresh child issue's title"
    )
    cut.add_argument(
        "--row",
        type=int,
        metavar="N",
        help="the slice table's # column value to cut; default is the first cuttable row",
    )
    cut.add_argument(
        "--scope",
        action="append",
        help=(
            "a repository-relative path; repeat --scope for more than one path; fills the "
            "cut slice's own row scope when it has none, and becomes the child's scope"
        ),
    )
    cut.add_argument("--not-a-twin", action="store_true", help=NOT_A_TWIN_HELP)
    _add_json_flag(cut)


def _add_ask_parser(commands: argparse._SubParsersAction) -> None:
    ask = commands.add_parser("ask", help="append one proposed expectation line to an item's block")
    ask.add_argument(
        "item", type=board.parse_item_reference, help="the item to append the expectation line to"
    )
    ask.add_argument("--text", required=True, help="the expectation line's prose")
    ask.add_argument(
        "--default",
        choices=sorted(body.BLOCK_EXPECTATION_DEFAULTS),
        default="yes",
        help="the proposer's suggested outcome; default yes",
    )
    ask.add_argument(
        "--question",
        help=(
            "one operator-language sentence the card shows as its heading "
            f"instead of --text; at most {body.EXPECTATION_QUESTION_MAXIMUM_CHARACTERS} characters"
        ),
    )
    ask.add_argument("--example", help="one operator-language sentence illustrating the question")
    ask.add_argument(
        "--picture",
        metavar="FILE.svg",
        help=(
            "a path to an inline-SVG file (root <svg>, no <script>, no external "
            f"href, at most {body.EXPECTATION_PICTURE_MAXIMUM_BYTES} bytes) the card shows"
        ),
    )
    _add_json_flag(ask)


def _add_rule_parser(commands: argparse._SubParsersAction) -> None:
    rule = commands.add_parser(
        "rule", help="rule one proposed expectation line, transcribing the operator's word"
    )
    rule.add_argument(
        "item", type=board.parse_item_reference, help="the item whose expectation line is ruled"
    )
    rule.add_argument(
        "--line",
        type=int,
        required=True,
        metavar="N",
        help="the 1-based expectation line index, as rulings prints it",
    )
    outcome = rule.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--yes", action="store_const", dest="ruling", const="yes")
    outcome.add_argument("--no", action="store_const", dest="ruling", const="no")
    outcome.add_argument("--later", action="store_const", dest="ruling", const="later")
    rule.add_argument("--note", help="appended to the line's own text as ' Anmerkung: TEXT'")
    _add_json_flag(rule)


def _parse_check_subject(value: str) -> int | str:
    """`check`'s one positional: a pull/issue reference (`board.parse_item_reference`),
    or -- issue #359, LAND-48 -- a full 40-character commit id, read as a trunk
    commit instead. The two grammars never collide: an item reference
    (`aco-xxxxxx`, `#n`, or the bare `n`) is always far shorter than a full
    git object id."""
    if protocol.COMMIT_PATTERN.fullmatch(value) is not None:
        return value
    return board.parse_item_reference(value)


def _add_check_parser(commands: argparse._SubParsersAction) -> None:
    check = commands.add_parser(
        "check",
        help=(
            "read one number or trunk commit -- a pull request's work-item "
            "classification, a trunk commit's (LAND-48), or an issue's body "
            "contract; claims, labels and writes nothing"
        ),
    )
    check.add_argument(
        "number",
        type=_parse_check_subject,
        help="the pull request, issue, or trunk commit to read",
    )
    _add_json_flag(check)


def _add_body_parser(commands: argparse._SubParsersAction) -> None:
    body = commands.add_parser(
        "body", help="check a piped body's fenced agent-claim block for defects before the forge"
    )
    body.add_argument(
        "--check",
        action="store_true",
        required=True,
        help="read a body from stdin and report its defects",
    )
    _add_json_flag(body)


def _add_brief_parser(commands: argparse._SubParsersAction) -> None:
    brief = commands.add_parser(
        "brief",
        help="print one item's body, live claim, lane tip and touched files for a dispatch",
    )
    brief.add_argument("item", type=board.parse_item_reference, help="the work item to brief")
    brief.add_argument(
        "--step",
        choices=[step.value for step in board.BriefStep],
        default=None,
        help="also print this lane step's own rules and checks from .agent-claim/brief.toml",
    )
    _add_json_flag(brief)


def _nonblank_title(value: str) -> str:
    """`item new`'s and `cut`'s `--title` argparse `type=` (issue #447):
    `value` unchanged unless `body.is_valid_title` -- the rule `record.title`
    is read by -- refuses it, before anything is read, created, or minted."""
    if not body.is_valid_title(value):
        raise protocol.ClaimUnavailableError("--title must be a non-empty string")
    return value


def _add_item_parser(commands: argparse._SubParsersAction) -> None:
    item = commands.add_parser("item", help="create, show, edit, or close one work item")
    item_commands = _add_subcommands(item, "item_command")
    new = item_commands.add_parser(
        "new",
        help=(
            "create a fresh item and print its id; its body is read from stdin, a piped "
            "body without an agent-claim block getting one built from the flags"
        ),
    )
    for key, meaning in ITEM_NEW_PROJECTION_FLAGS:
        new.add_argument(f"--{key.replace('_', '-')}", dest=key, help=meaning)
    new.add_argument("--title", required=True, type=_nonblank_title, help="the fresh item's title")
    new.add_argument(
        "--kind",
        choices=BODY_TEMPLATE_KINDS,
        default=DEFAULT_BODY_TEMPLATE_KIND,
        help=f"the fresh item's kind; default {DEFAULT_BODY_TEMPLATE_KIND}",
    )
    new.add_argument(
        "--parent",
        type=board.parse_item_reference,
        metavar="ITEM",
        help=f"the fresh item's parent, {ITEM_REF_HELP}",
    )
    new.add_argument(
        "--origin",
        type=items.parse_origin,
        metavar="FORGE#N",
        help="bind this lane to a foreign forge issue, e.g. gitlab#514",
    )
    new.add_argument(
        "--scope",
        action="append",
        help="a repository-relative path; repeat --scope for more than one path",
    )
    new.add_argument(
        "--size",
        choices=tuple(metrics.Size),
        help="this item's size class, for the board's own measured estimate; default none",
    )
    new.add_argument("--whole", metavar="REASON", help=ITEM_WHOLE_HELP)
    new.add_argument("--not-a-twin", action="store_true", help=NOT_A_TWIN_HELP)
    _add_json_flag(new)
    show = item_commands.add_parser(
        "show",
        help="print one item's header and its stored body, display controls escaped"
        " (--json keeps the body as stored)",
    )
    show.add_argument(
        "item", type=board.parse_item_reference, help=f"the item to show, {ITEM_REF_HELP}"
    )
    _add_json_flag(show)
    edit = item_commands.add_parser(
        "edit", help="replace one item's body from stdin, aco keeping its own record fields"
    )
    edit.add_argument(
        "item", type=board.parse_item_reference, help=f"the item to edit, {ITEM_REF_HELP}"
    )
    one_field = edit.add_mutually_exclusive_group()
    one_field.add_argument(
        "--size",
        choices=tuple(metrics.Size),
        help="set only this item's size class (any storage); skips the stdin body read",
    )
    one_field.add_argument(
        "--whole",
        metavar="REASON",
        help="set only this item's whole reason (any storage); skips the stdin body read",
    )
    one_field.add_argument(
        "--kind",
        choices=ITEM_EDIT_KINDS,
        help="set only this item's kind (any storage); skips the stdin body read",
    )
    _add_json_flag(edit)
    close = item_commands.add_parser(
        "close", help="close a state-ref item; the file stays, next and board let it go"
    )
    close.add_argument(
        "item", type=board.parse_item_reference, help=f"the item to close, {ITEM_REF_HELP}"
    )
    _add_json_flag(close)


def _add_protect_parser(commands: argparse._SubParsersAction) -> None:
    commands.add_parser("protect", help="deny PreToolUse writes without this session's live claim")


def _add_register_parser(commands: argparse._SubParsersAction) -> None:
    register = commands.add_parser(
        "register",
        help="record one stopped or validated live provider session for workspace recovery",
    )
    register.add_argument("project", metavar="PROJECT", help="a stable local project key")
    register.add_argument(
        "--path", required=True, type=Path, help="the project's canonical directory"
    )
    register.add_argument("--session-id", required=True, help="the exact provider session UUID")
    register.add_argument("--agent", required=True, help="the inherited logical claim identity")
    register.add_argument(
        "--provider",
        choices=tuple(provider.value for provider in providers.Provider),
        default=providers.Provider.CODEX.value,
        help="the conversation provider (default: codex)",
    )
    register.add_argument("--model", help="optional provider model override")
    handover = register.add_mutually_exclusive_group(required=True)
    handover.add_argument(
        "--stopped",
        action="store_true",
        help="acknowledge that the existing provider session was checkpointed and stopped",
    )
    handover.add_argument(
        "--live-pid", type=int, help="the exact running native Codex or Claude process ID"
    )


def _add_run_parser(commands: argparse._SubParsersAction) -> None:
    run = commands.add_parser("run", help="open the registered provider workspace consoles")
    run.add_argument("project", metavar="PROJECT", nargs="?", help="one registered project")


def _add_login_parser(commands: argparse._SubParsersAction) -> None:
    login = commands.add_parser(
        "login", help="manage configured workspace recovery at desktop login"
    )
    login_commands = _add_subcommands(login, "login_command")
    login_commands.add_parser("enable", help="install the owned desktop login launcher")
    login_commands.add_parser("disable", help="remove the owned desktop login launcher")
    login_commands.add_parser(
        "status", help="show launcher, configuration, and latest attempt state"
    )


def _add_run_at_login_parser(commands: argparse._SubParsersAction) -> None:
    commands.add_parser("_run-at-login", help=argparse.SUPPRESS)


# `item`, `protect`, `register`, `run`, `login`, and `_run-at-login` never
# own a `_CommandEntry` (issue #372): `item` fans out to its own four
# subcommands ahead of `_COMMAND_TABLE`, `protect` and the workspace
# commands are forge-free and never reach `_dispatch` at all. `status` and
# `body` are the same story (`_read_status_body_or_dispatch`, ahead of
# `_dispatch`) but `aco --help` must still list all
# eight in their original places, so `_subparser_build_order` below weaves
# them back into position rather than appending them after every table
# entry.
_OTHER_SUBPARSER_BUILDERS: tuple[Callable[[argparse._SubParsersAction], None], ...] = (
    _add_item_parser,
    _add_protect_parser,
    _add_register_parser,
    _add_run_parser,
    _add_login_parser,
    _add_run_at_login_parser,
)


def _subparser_build_order() -> tuple[Callable[[argparse._SubParsersAction], None], ...]:
    """Every subcommand's parser builder, in the exact order `aco --help`
    has always listed them (issue #372 R3): read from `_COMMAND_TABLE` only
    here, at `_parser()`'s own call time, since that table is defined
    further below in the file."""
    table = _COMMAND_TABLE
    return (
        table["bootstrap"].add_parser,
        table["reset"].add_parser,
        _add_status_parser,
        table["board"].add_parser,
        table["rulings"].add_parser,
        table["next"].add_parser,
        table["start"].add_parser,
        table["claim"].add_parser,
        table["release"].add_parser,
        table["land"].add_parser,
        table["rescope"].add_parser,
        table["cut"].add_parser,
        table["ask"].add_parser,
        table["rule"].add_parser,
        table["check"].add_parser,
        _add_body_parser,
        table["brief"].add_parser,
        *_OTHER_SUBPARSER_BUILDERS,
    )


class _UsageError(protocol.ClaimError):
    """One refusal the argument parser itself raises -- an unknown flag, a
    missing required option, a mutually exclusive pair -- carried out of the
    parse instead of exiting inside it (issue #432), so a `--json` caller
    still gets the shared envelope. `parser` is the one that refused, a
    subcommand's own rather than the root's, because only it can print the
    usage block the plain-text path still prints byte for byte."""

    def __init__(self, parser: argparse.ArgumentParser, message: str) -> None:
        super().__init__(message)
        self.parser = parser


class _RecordingSubParsersAction(argparse._SubParsersAction):
    """argparse's own subcommand action, remembering which subparser it
    selected (issue #432). Python parses a subcommand into a throwaway
    namespace and copies the values back only once that parse succeeds, so
    a refused parse otherwise leaves no trace of the command whose flags
    were in play -- and `--json` is exactly the flag the refusal's own shape
    depends on."""

    chosen: argparse.ArgumentParser | None = None
    handed_down: tuple[str, ...] = ()

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        name, *handed_down = cast(Sequence[str], values)
        self.chosen = self._name_parser_map.get(name)
        self.handed_down = tuple(handed_down)
        super().__call__(parser, namespace, values, option_string)


class _UsageErrorParser(argparse.ArgumentParser):
    """Every `aco` parser and subparser -- argparse hands this class down to
    each subparser it builds. Its own refusal path prints usage and exits
    inside the parse, before `--json` was ever read off a namespace, so this
    raises instead and lets one place decide the refusal's shape.

    No parser reads an abbreviated long option (issue #502, OUT-09): a
    prefix unique today targets another option once one is added, and
    `reset --conf --f` must never stand for a destructive flag pair."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, allow_abbrev=False, **kwargs)

    def error(self, message: str) -> NoReturn:
        raise _UsageError(self, message)


def _add_subcommands(parser: argparse.ArgumentParser, dest: str) -> argparse._SubParsersAction:
    """Every subcommand level of `aco`, recorded while it is chosen so a
    refused parse still names the command whose flags were in play."""
    return parser.add_subparsers(dest=dest, required=True, action=_RecordingSubParsersAction)


def _parser() -> argparse.ArgumentParser:
    parser = _UsageErrorParser(prog="aco", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--repo", help="GitHub repository as OWNER/REPO")
    commands = _add_subcommands(parser, "command")
    for add_subparser in _subparser_build_order():
        add_subparser(commands)
    return parser


def _identity_json(identity: protocol.ClaimIdentity) -> dict[str, object]:
    """`issue`/`lane` pair for one claim's discriminated identity, for JSON output.

    A lane claim's name lives in the sibling `branch` field of the same JSON
    object, so `lane` stays a bare marker instead of duplicating it.
    """
    if isinstance(identity, protocol.LaneIdentity):
        return {"issue": None, "lane": True}
    return {"issue": identity.issue, "lane": None}


def _status_claims(
    claims: tuple[protocol.ActiveClaim, ...], issue: int | None
) -> tuple[tuple[protocol.ActiveClaim, ...], protocol.ClaimConflictIndex]:
    selected = tuple(
        claim
        for claim in claims
        if issue is None
        or (isinstance(claim.identity, protocol.IssueIdentity) and claim.identity.issue == issue)
    )
    index = protocol._claim_conflict_index(claims)
    if not selected:
        return (), index
    related_ids = (
        {claim.claim_id for claim in claims}
        if issue is None
        else protocol._related_claim_ids(index, selected)
    )
    related = tuple(claim for claim in claims if claim.claim_id in related_ids)
    return related, index


def _resource_fields(resource: protocol.ResourceHold | None) -> dict[str, object]:
    if resource is None:
        return {"resource": None, "resource_value": None}
    return {"resource": resource.name, "resource_value": resource.value}


def _claim_identity_fields(claim: protocol.ActiveClaim) -> dict[str, object]:
    """The one claim-identity dict every `--json` view of a live claim
    shares -- `status`, `status --path`, `claim`, and `rescope` (issue #406,
    #390 finding 6) each spread this instead of typing the same seven
    fields out by hand a fifth time with their own drifting order. A view
    still projects its own remaining fields explicitly beside it: `resource`/
    `resource_value` (every view but `rescope`, which names no `--resource`
    flag of its own), `whole` (`status` and `status --path` alone), and
    whatever else is that view's own (`overlaps`/`age`/`old` for `status`,
    `versioned_files`/`touches`/`checks` for `claim`)."""
    return {
        **_identity_json(claim.identity),
        "claim_id": claim.claim_id,
        "agent": claim.agent,
        "role": claim.role,
        "base": claim.base,
        "branch": claim.branch,
        "scope": list(claim.scope),
    }


def _overlap_subjects(
    claims_by_id: Mapping[str, protocol.ActiveClaim], peer_ids: set[str]
) -> list[dict[str, object]]:
    return [
        {
            **_identity_json(peer.identity),
            "claim_id": peer.claim_id,
            "agent": peer.agent,
        }
        for claim_id in sorted(peer_ids)
        if (peer := claims_by_id.get(claim_id)) is not None
    ]


def _overlap_note(
    claims_by_id: Mapping[str, protocol.ActiveClaim], peer_ids: set[str], storage: body.Storage
) -> str | None:
    peers = [claims_by_id[claim_id] for claim_id in sorted(peer_ids) if claim_id in claims_by_id]
    if not peers:
        return None
    return "overlaps " + ", ".join(
        f"{_claim_subject(claim, storage)} ({claim.claim_id})" for claim in peers
    )


@dataclass(frozen=True)
class _ClaimReportContext:
    """`index` and `storage` together (issue #292): the two facts every
    claim in one `status` read shares -- bundled so adding `storage`
    beside the pre-existing `index` never pushes a caller past PLR0913's
    five-argument ceiling."""

    index: protocol.ClaimConflictIndex
    storage: body.Storage


def _print_claim_status_lines(
    claim: protocol.ActiveClaim,
    claims_by_id: Mapping[str, protocol.ActiveClaim],
    context: _ClaimReportContext,
    opened_at: datetime,
    observed_at: datetime,
) -> None:
    state = "CONFLICT" if claim.claim_id in context.index.conflict_ids else "CLAIMED"
    print(
        f"{state} {_claim_subject(claim, context.storage)}: {claim.agent} ({claim.role}) "
        f"base={claim.base} branch={claim.branch} claim={claim.claim_id}"
        f"{_claim_age_suffix(opened_at, observed_at)}"
    )
    for path in claim.scope:
        print(f"  {path}")
    if claim.resource is not None:
        print(f"  resource {claim.resource.name}={claim.resource.value}")
    if claim.whole_reason is not None:
        print(f"  whole: {claim.whole_reason}")
    note = _overlap_note(
        claims_by_id, protocol._overlap_peer_ids(context.index, claim), context.storage
    )
    if note is not None:
        print(f"  {note}")


def _print_related_claims(
    claims: tuple[protocol.ActiveClaim, ...],
    related: tuple[protocol.ActiveClaim, ...],
    context: _ClaimReportContext,
    ages: Mapping[str, datetime],
    observed_at: datetime,
) -> int:
    claims_by_id: dict[str, protocol.ActiveClaim] = {claim.claim_id: claim for claim in claims}
    for claim in related:
        _print_claim_status_lines(claim, claims_by_id, context, ages[claim.claim_id], observed_at)
    return 2 if any(claim.claim_id in context.index.conflict_ids for claim in related) else 0


def _status(
    claims: tuple[protocol.ActiveClaim, ...],
    issue: int | None,
    ages: Mapping[str, datetime],
    storage: body.Storage,
    now: datetime | None = None,
) -> int:
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    related, index = _status_claims(claims, issue)
    if related:
        context = _ClaimReportContext(index, storage)
        return _print_related_claims(claims, related, context, ages, observed_at)
    subject = "repository" if issue is None else f"issue {board.item_label(issue, storage)}"
    print(f"UNCLAIMED {subject}")
    return 0


class StatusReason(StrEnum):
    """`aco status`'s own `--json` `reason` vocabulary (issue #406,
    `specs/status.spec.md` STAT-03/STAT-05): `claimed`, `unclaimed`, and
    `conflict` are its own three read states. `status` is forge-free
    (STAT-16) and never resolves a repository target, so it names no
    `invalid_usage` of its own -- only `unavailable`, reached when
    `store.fetch_state` refuses a rewritten or malformed state ref."""

    CLAIMED = "claimed"
    UNCLAIMED = "unclaimed"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


def _status_claim_json(
    claim: protocol.ActiveClaim,
    claims_by_id: Mapping[str, protocol.ActiveClaim],
    index: protocol.ClaimConflictIndex,
    ages: Mapping[str, datetime],
    observed_at: datetime,
) -> dict[str, object]:
    age, old = _claim_age_fields(ages[claim.claim_id], observed_at)
    return {
        **_claim_identity_fields(claim),
        **_resource_fields(claim.resource),
        **({"whole": claim.whole_reason} if claim.whole_reason is not None else {}),
        "overlaps": _overlap_subjects(claims_by_id, protocol._overlap_peer_ids(index, claim)),
        "state": "CONFLICT" if claim.claim_id in index.conflict_ids else "CLAIMED",
        "age": age,
        "old": old,
    }


def _status_json(
    claims: tuple[protocol.ActiveClaim, ...],
    issue: int | None,
    ages: Mapping[str, datetime],
    tip: protocol.ObjectId | None,
    now: datetime | None = None,
) -> int:
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    related, index = _status_claims(claims, issue)
    if not related:
        reason = StatusReason.UNCLAIMED
    elif any(claim.claim_id in index.conflict_ids for claim in related):
        reason = StatusReason.CONFLICT
    else:
        reason = StatusReason.CLAIMED
    claims_by_id: dict[str, protocol.ActiveClaim] = {claim.claim_id: claim for claim in claims}
    claims_payload = [
        _status_claim_json(claim, claims_by_id, index, ages, observed_at) for claim in related
    ]
    _emit_json(
        reason is not StatusReason.CONFLICT, reason, issue=issue, tip=tip, claims=claims_payload
    )
    return 2 if reason is StatusReason.CONFLICT else 0


def _status_path(
    claims: tuple[protocol.ActiveClaim, ...], path: str, storage: body.Storage
) -> None:
    holders = protocol.claims_holding_path(claims, path)
    if not holders:
        print(f"UNCLAIMED {path}")
        return
    for claim in holders:
        print(
            f"CLAIMED {path} {_claim_subject(claim, storage)}: {claim.agent} ({claim.role}) "
            f"claim={claim.claim_id}"
        )
        if claim.whole_reason is not None:
            print(f"  whole: {claim.whole_reason}")
    if len(holders) > 1:
        print(
            "overlap: "
            + ", ".join(f"{_claim_subject(claim, storage)} ({claim.claim_id})" for claim in holders)
        )


def _status_path_json(claims: tuple[protocol.ActiveClaim, ...], path: str) -> int:
    holders = protocol.claims_holding_path(claims, path)
    reason = StatusReason.UNCLAIMED if not holders else StatusReason.CLAIMED
    claims_payload = [
        {
            **_claim_identity_fields(claim),
            **_resource_fields(claim.resource),
            **({"whole": claim.whole_reason} if claim.whole_reason is not None else {}),
            "state": "CLAIMED",
        }
        for claim in holders
    ]
    _emit_json(True, reason, path=path, claims=claims_payload)
    return 0


class RescopeReason(StrEnum):
    """`aco rescope`'s own `--json` `reason` vocabulary (issue #406,
    `specs/rescope.spec.md`): `rescoped` the only success. `invalid_usage`
    covers a malformed `--add`/`--drop` value (a relative entry, one outside
    the checkout, a comma-bearing entry matching no versioned file, or a
    combination `protocol._combined_scope` refuses); `precondition_failed`
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
    _emit_json(True, RescopeReason.RESCOPED, **_claim_identity_fields(claimed))


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
    covers the shared `checks` array `_refuse_claim` reports (out-of-order,
    blocked, container, closed/missing, body-incomplete, missing-parent --
    CLM-08..14). `target_invalid` and `body_invalid` are the two failures a
    scope actually being *derived* from an item's own body can hit before
    those checks ever run: the item itself is unusable (missing, a pull
    request), or its body cannot supply or confirm a scope (a malformed
    `agent-claim` block, no `scope` field, or one differing from an explicit
    `--scope`). `claim_conflict` is `apply()`'s own single failure surface
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
    checks: tuple[SliceCheck, ...],
) -> int:
    _emit_json(
        True,
        ClaimReason.CLAIMED,
        **_claim_identity_fields(claimed),
        **_resource_fields(claimed.resource),
        versioned_files=versioning.versioned_files,
        versioned_files_total=versioning.versioned_files_total,
        share=versioning.share,
        touches=[_touch_json(claim) for claim in touches],
        checks=[check.as_json() for check in checks],
    )
    return 0


class ReleaseReason(StrEnum):
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


def _release_reason(outcome: protocol.ReleaseOutcome) -> ReleaseReason:
    return (
        ReleaseReason.ABANDONED
        if isinstance(outcome, protocol.AbandonedRelease)
        else ReleaseReason.MERGED
    )


def _release_json(report: ReleaseReport) -> None:
    released, landing = report.selected, report.landing
    payload: dict[str, object] = {
        "outcome": report.outcome.reason,
        **_identity_json(released.identity),
        "branch": released.branch,
        "claim_id": released.claim_id,
        "agent": report.agent,
        "role": report.role if report.role is not None else released.role,
    }
    if isinstance(landing, ReleaseLanding):
        payload["freed"] = list(landing.freed)
        payload["next"] = None if landing.next_item is None else landing.next_item.number
        payload["parent_closable"] = landing.parent_closable
    if report.worktree is not None:
        payload["worktree"] = worktree_cleanup_outcome_text(report.worktree)
    _emit_json(True, _release_reason(report.outcome), **payload)


@dataclass(frozen=True)
class ReleaseLanding:
    """What a merged release's lazy board read (issue #256) adds to its
    report once the release itself has already succeeded: every open item
    this landing fully freed, the same next pick `aco next` would recommend
    right after it, and the parent container this landing's own child just
    made closable, if any (issue #348)."""

    freed: tuple[int, ...]
    next_item: board.BoardItem | None
    parent_closable: int | None


def _next_action_item(action: board.NextAction) -> board.BoardItem:
    """The `BoardItem` `action` targets, whichever action kind it is -- a
    plain claim target for `WorkItemAction`, the container itself for
    `CutSliceAction`/`CheckContainerAction`/`CloseContainerAction` (issue #256)."""
    return action.item if isinstance(action, board.WorkItemAction) else action.container


def _freed_item_numbers(
    dependencies: Mapping[int, tuple[board.IssueDependency, ...]],
    landed: board.IssueReference,
) -> tuple[int, ...]:
    """Open items whose last open blocker was `landed` (issue #256): every
    local `blocked_by` dependency of the item is now closed, `landed` was
    one of them, and nothing else -- local or foreign -- still blocks it.
    Reuses `board.open_dependency_blockers`/`board._dependency_freed_on`,
    the same per-item blocker facts `_issue_check`'s own refusal already
    reads, rather than re-deriving them from `Board`'s aggregate fields:
    those only ever describe the *currently* open issues, so a landed
    item's own `blocked_by` reference disappears from them the moment it
    closes -- exactly the fact this needs to name who it freed. `dependencies`
    is `_release_landing`'s one fetch, shared with `_board` (issue #256
    review), rather than a second `list_board_dependencies` round trip over
    the same candidates.
    """
    repository = landed.repository
    freed: list[int] = []
    for number, local in dependencies.items():
        if board._dependency_freed_on(local, repository) is None:
            continue
        if board.open_dependency_blockers(local, repository):
            continue
        if landed in {dependency.reference for dependency in local}:
            freed.append(number)
    return tuple(sorted(freed))


def _parent_closable_number(
    client: forge.ForgeReader, closed_child: int, storage: body.Storage
) -> int | None:
    """The container `closed_child`'s own landing may just have completed
    (issue #348, the parent hint `release --merged`/`item close` share): its
    parent, read fresh, when every other child is already closed too and no
    undispatched `[[slice]]` row is left to cut -- `board.closable_container_number`
    is the one owner for that decision, reused rather than re-derived board-wide
    for one relation. `None` covers every non-container parent, one still
    holding another open child, an already-closed parent (a second close
    would only refuse), or no parent at all. The container decision runs
    before the parent's own state read, so an unreadable parent (issue
    #517, never a container by kind) is answered `None` rather than
    refusing a close that already stood."""
    parent = client.parent_issue(closed_child)
    if parent is None:
        return None
    children = client.list_children(parent.reference.number)
    closable = board.closable_container_number(parent, children, storage)
    if closable is None:
        return None
    parent_is_open = client.item_reference(closable).state is forge.ItemState.OPEN
    return closable if parent_is_open else None


def _release_landing(
    context: RunContext,
    new_state: protocol.ClaimState,
    landed: board.IssueReference | None,
    storage: body.Storage,
    trunk_ref: str,
) -> ReleaseLanding:
    """A merged release's own board read (issue #256) of `new_state`, the
    claims its release transition left: fetched once, lazily, only after
    that transition already committed -- the caller wraps this in one broad
    `forge.ForgeError` catch, so a forge hiccup here can never undo or fail
    a release that already stood. The dependency fetch below is the one
    round trip both `_freed_item_numbers` and `_board` need for this same
    candidate set; passing it into `_board` keeps this a single fetch rather
    than two (issue #256 review). The board walks `trunk_ref`, the ref the
    release judged its landing on (LANDCMD-21, issue #492)."""
    client = context.forge
    issues = client.list_open_board_issues()
    candidates = tuple(issue.number for issue in issues if issue.blocked_by_count > 0)
    dependencies = _validated_dependencies(issues, _fetch_dependencies(client, candidates))
    freed = () if landed is None else _freed_item_numbers(dependencies, landed)
    projected = _board(
        context,
        tuple(new_state.claims.values()),
        issues=issues,
        history=_ClaimHistory(ages=_claim_ages(context.toplevel, new_state)),
        landing=_LandingReads(dependencies, trunk_ref),
    )
    action = board.next_action(projected)
    parent_closable = (
        None if landed is None else _parent_closable_number(client, landed.number, storage)
    )
    return ReleaseLanding(
        freed, None if action is None else _next_action_item(action), parent_closable
    )


def _release_freed_line(freed: tuple[int, ...], storage: body.Storage) -> str:
    return "freed: " + (
        ", ".join(board.item_label(number, storage) for number in freed) if freed else "none"
    )


def _parent_closable_line(number: int | None, storage: body.Storage) -> str | None:
    if number is None:
        return None
    return f"parent {board.item_label(number, storage)}: no open children — close it"


def _release_next_line(item: board.BoardItem | None, storage: body.Storage) -> str:
    if item is None:
        return "next: none"
    label = board.item_label(item.number, storage)
    return f"next: {label} score {item.score}: {board.terminal_text(item.title)}"


def _merged_pull_request_floor(issues: tuple[board.Issue, ...], now: datetime) -> datetime:
    """The earliest merge that could still matter to a currently open issue.

    A pull request can only touch or close an issue that already exists, so
    nothing merged before the oldest still-open issue was filed can ever
    change any open item's stage. Anchoring the query here — instead of an
    arbitrary fixed window — is what lets a slice's "Refs #N"/"Part of #N"
    landing keep crediting its still-open epic for as long as the epic stays
    open, rather than for a fixed number of days after which the credit
    silently reverts. Residual: the underlying query is still capped (see
    `GitHubForge.list_recent_merged_board_pull_requests`), so an epic
    old enough to have more merges than that cap between its filing and now
    can still lose credit for an early slice; this floor removes the
    fortnight-sized version of that gap, not every version of it.
    """
    if not issues:
        return now
    return min(board._timestamp(issue.created_at) for issue in issues)


# A container's children are their own `gh list_children` subprocess call;
# an unbounded pool would spawn one worker per container on a large board.
# This caps that fan-out -- a stable invariant of this executor, not
# something an operator tunes. `_fetch_children` gives it a dedicated
# executor sized to exactly this constant, so the cap holds regardless of
# whether the three base board reads below have already finished.
BOARD_CHILD_FETCH_CONCURRENCY = 4

# A stable invariant of the board's own ruling-freshness read (issue #304),
# not something an operator tunes: `RULING_OLD_AFTER_LANDINGS` (10) is the
# most any ruling ever needs counted, so this bounds `git log`'s walk deep
# enough that no realistic ruling window is ever truncated.
TRUNK_LANDING_DEPTH = 5000


def _fetch_children(
    client: forge.BoardSource, container_numbers: tuple[int, ...]
) -> dict[int, tuple[board.ChildItem, ...]]:
    """Every container's children, at most `BOARD_CHILD_FETCH_CONCURRENCY`
    `gh` subprocesses at a time; excess containers queue behind it."""
    if not container_numbers:
        return {}
    workers = min(len(container_numbers), BOARD_CHILD_FETCH_CONCURRENCY)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            number: pool.submit(client.list_children, number) for number in container_numbers
        }
        return {number: future.result() for number, future in futures.items()}


def _fetch_dependencies(
    client: forge.BoardSource, issue_numbers: tuple[int, ...]
) -> dict[int, tuple[board.IssueDependency, ...]]:
    """Every named issue's `blocked_by` dependencies (#150), at most
    `BOARD_CHILD_FETCH_CONCURRENCY` `gh` subprocesses at a time -- run only
    after `_board`'s base reads and children wave have already finished, so
    peak concurrent `gh` subprocesses never exceeds today's 3+4."""
    if not issue_numbers:
        return {}
    workers = min(len(issue_numbers), BOARD_CHILD_FETCH_CONCURRENCY)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            number: pool.submit(client.list_board_dependencies, number) for number in issue_numbers
        }
        return {number: future.result() for number, future in futures.items()}


def _validated_dependencies(
    issues: tuple[board.Issue, ...], fetched: dict[int, tuple[board.IssueDependency, ...]]
) -> dict[int, tuple[board.IssueDependency, ...]]:
    """`fetched`, keyed by exactly the positive-`blocked_by_count` issues,
    each list checked against its own listing count (#150 §6): a length
    mismatch or a duplicated dependency is the same class of malformed
    forge response as a disagreeing container summary -- named loud rather
    than guessed through."""
    validated: dict[int, tuple[board.IssueDependency, ...]] = {}
    for issue in issues:
        if issue.blocked_by_count <= 0:
            continue
        dependencies = fetched.get(issue.number, ())
        distinct = {dependency.reference for dependency in dependencies}
        length = len(dependencies)
        if length != issue.blocked_by_count or len(distinct) != length:
            raise forge.ForgeMalformedResponseError(
                f"GitHub returned a malformed board blocked-by list for #{issue.number}: "
                f"listing total_blocked_by={issue.blocked_by_count}, detail length={length}"
            )
        validated[issue.number] = dependencies
    return validated


def _load_board_config(client: forge.BoardSource, context: RunContext) -> board.BoardConfig:
    """`context`'s board configuration, validated against what `client`
    can actually do (#150 §3): reading a body's dependencies requires
    `list_board_dependencies` at read-only or better, and the typed block is
    the one body grammar, so every repository needs it."""
    config = context.config
    if (
        client.capability(forge.ForgeOperation.LIST_BOARD_DEPENDENCIES)
        is forge.Capability.UNSUPPORTED
    ):
        raise protocol.ClaimError(
            "reading work-item bodies requires forge operation list_board_dependencies"
        )
    return config


@dataclass(frozen=True)
class _ClaimHistory:
    """The store's own git-history reads one board build needs (issue #357):
    bundled into one parameter so `_board` stays under the five-argument
    ceiling instead of growing a `claim_ages`-shaped parameter every time
    another history read joins it. `ages`, `lane_events`, and
    `unparsed_lifecycle_commits` are always read from the same
    already-fetched `ClaimState` (`_claim_history` below), never from two
    different observations."""

    ages: Mapping[str, datetime] = field(default_factory=dict)
    lane_events: tuple[metrics.LaneEvent, ...] = ()
    # Claim-shaped `refs/aco/state` commits `store.claim_lifecycle` could not
    # parse (issue #357 R1) -- older history predating the `item:` trailer,
    # or a foreign commit merely shaped like one -- counted rather than
    # silently dropped.
    unparsed_lifecycle_commits: int = 0


@dataclass(frozen=True)
class _LandingReads:
    """What a merged release already read before its board report (issues
    #256, #492), bundled so `_board` stays under the five-argument ceiling:
    the dependency wave `_freed_item_numbers` needed for the same candidate
    set, and the trunk ref the release judged its landing on, which the
    board walks instead of the trunk as the last fetch left it."""

    dependencies: dict[int, tuple[board.IssueDependency, ...]]
    trunk_ref: str


def _closed_item_numbers(
    lane_events: tuple[metrics.LaneEvent, ...], open_numbers: frozenset[int]
) -> frozenset[int]:
    """Every numbered item `lane_events` names that is not among this board
    build's own open issues (issue #357 R2) -- a completed lane whose item
    has since closed, or one this walk can no longer resolve to a live
    issue at all. A `docs/`/`fix/` lane claim's own `item` is never numeric
    and is silently excluded, matching `board._item_number_or_none`."""
    numbers = {int(event.item) for event in lane_events if event.item.isdigit()}
    return frozenset(numbers - open_numbers)


def _closed_item_sizes(
    client: forge.ForgeReader, numbers: frozenset[int], storage: body.Storage
) -> dict[int, metrics.Size | None]:
    """Each closed (or vanished) item's own current size, read through one
    batched forge/state fetch (issue #357 R2, issue #440): a completed lane
    for a since-closed item still belongs in its size class's own measured
    lanes -- most lanes close their item on landing, so without this the
    size classes `aco board` estimates from would stay almost always empty.
    `ItemState.MISSING` (a deleted or renumbered item) and a bodyless
    reference both read as no size, exactly like an open item that never
    carried a `size` key. `client.item_references` -- not one `item_
    reference` call per number -- is what turned this from the board's own
    dominant cost (32 serial round trips, 12.8 of an 18s build) into a
    single round trip that stays flat as the closed-item history grows."""
    references = client.item_references(numbers)
    return {
        number: (
            None
            if (raw_body := references[number].body) is None
            else body.parse_body(raw_body, storage=storage).size
        )
        for number in numbers
    }


def _board(
    context: RunContext,
    claims: tuple[protocol.ActiveClaim, ...],
    *,
    issues: tuple[board.Issue, ...] | None = None,
    history: _ClaimHistory | None = None,
    landing: _LandingReads | None = None,
) -> board.Board:
    history = history or _ClaimHistory()
    now = datetime.now(UTC)
    client = context.forge
    config = _load_board_config(client, context)
    if issues is None:
        issues = client.list_open_board_issues()
    since = _merged_pull_request_floor(issues, now)
    # A container whose own summary already says 0 (or carries no summary at
    # all, `children_total is None`) can never own an open child either way:
    # `_container_progress` returns no progress at all without both numbers,
    # and returns an empty open-children list when they're both 0 -- exactly
    # what an absent `children` entry already defaults to. Fetching its
    # detail list would cost a request `board` never needed (issue #168).
    container_numbers = tuple(
        issue.number
        for issue in issues
        if issue.kind is body.ItemKind.CONTAINER and issue.children_total
    )
    # Open and recently-merged pull requests, the closed-item size batch
    # (issue #440: one `item_references` round trip rather than the 32
    # serial single-item reads this used to pay before the pool ever
    # started), and each container's children are independent reads once
    # `since` is known, so fetching them on separate threads instead of one
    # after another overlaps their `gh` subprocess wait time. Children get
    # their own executor (`_fetch_children`) so their concurrency stays
    # capped at `BOARD_CHILD_FETCH_CONCURRENCY` even once these three base
    # reads finish and free their own pool's workers. The dependency wave
    # runs afterward instead (below), so peak concurrent `gh` subprocesses
    # stays at 3+4, then at most 4.
    with ThreadPoolExecutor(max_workers=3) as pool:
        open_pull_requests = pool.submit(client.list_open_board_pull_requests)
        merged_pull_requests = pool.submit(client.list_recent_merged_board_pull_requests, since)
        closed_item_sizes_future = pool.submit(
            _closed_item_sizes,
            client,
            _closed_item_numbers(history.lane_events, frozenset(issue.number for issue in issues)),
            config.storage,
        )
        children = _fetch_children(client, container_numbers)
        pull_requests = (open_pull_requests.result(), merged_pull_requests.result())
        closed_item_sizes = closed_item_sizes_future.result()
    # The same forge relation `_cut_target` refuses a nested container on
    # (issue #503), so `next` never proposes a `cut` that `cut` refuses.
    nesting_parents = {
        number: parent.reference
        for number in board.childless_containers_with_uncut_rows(issues, config.storage)
        if (parent := client.parent_issue(number)) is not None
    }
    if landing is None:
        dependencies = _validated_dependencies(
            issues,
            _fetch_dependencies(
                client, tuple(issue.number for issue in issues if issue.blocked_by_count > 0)
            ),
        )
        trunk_ref = context.trunk_ref
    else:
        # A merged release already fetched and validated this exact
        # candidate set (`_release_landing`'s own `list_board_dependencies`
        # wave, issue #256 review) and judged its landing on its own trunk
        # ref (issue #492), so neither is read a second time here.
        dependencies, trunk_ref = landing.dependencies, landing.trunk_ref
    trunk_landings = checkout.trunk_landings(
        trunk_ref, TRUNK_LANDING_DEPTH, directory=context.toplevel
    )
    # One walk of `trunk_landings` feeds three views `board.py` keeps
    # separate (issue #371): `trunk_landing_items` (sha and all) drives the
    # Landungen view itself; `landed_at_by_item`/`trunk_landed_work_items`
    # are its own narrower rollups for `Stage.CODE_LANDED` and the
    # measurements' landing dates.
    trunk_landing_items = tuple(
        board.TrunkLandingItem(number, landing.sha, landing.committed_at)
        for landing in trunk_landings
        if isinstance(landing.classification, board.TrunkWorkItemClassification)
        for number in landing.classification.numbers
    )
    landed_at_by_item = {entry.item: entry.committed_at for entry in trunk_landing_items}
    return board.build_board(
        board.BoardBuildInputs(
            issues=issues,
            open_pull_requests=pull_requests[0],
            recent_merged_pull_requests=pull_requests[1],
            claims=claims,
            config=config,
            repository=client.repository.path,
            now=now,
            trunk_landings=tuple(landing.committed_at for landing in trunk_landings),
            trunk_landed_work_items=frozenset(entry.item for entry in trunk_landing_items),
            trunk_landing_items=trunk_landing_items,
            children=children,
            nesting_parents=nesting_parents,
            dependencies=dependencies,
            requests=client.requests,
            claim_ages=history.ages,
            lane_events=history.lane_events,
            unparsed_lifecycle_commits=history.unparsed_lifecycle_commits,
            closed_item_sizes=closed_item_sizes,
            landed_at_by_item=landed_at_by_item,
            open_pull_requests_supported=(
                client.capability(forge.ForgeOperation.LIST_OPEN_BOARD_PULL_REQUESTS)
                is not forge.Capability.UNSUPPORTED
            ),
        )
    )


@dataclass(frozen=True)
class _RulingsRow:
    """One `rulings` row: the item, its open/total counts, and every one of
    its `[[expectation]]` lines (open and already-ruled alike) in block
    order -- the detail `rulings` prints beneath the item's own summary."""

    item: board.BoardItem
    progress: body.ExpectationProgress
    lines: tuple[body.ExpectationLine, ...]


def _rulings_rows(
    projected: board.Board, bodies: Mapping[int, str], *, storage: body.Storage
) -> tuple[_RulingsRow, ...]:
    """Every open board item that carries at least one `[[expectation]]`
    line, fully ruled ones included (issue #379) -- items with an open line
    first, board-ranked then by fewer open lines then issue number
    (unchanged from before #240), then the fully ruled items in that same
    order. Each row is paired with its lines read fresh from `bodies` --
    `projected.items` itself carries only the open/total counters. `storage`
    is forwarded to `expectation_lines` unchanged (issue #248): a state-ref
    body's `[record]` table must read as a known key, not a malformed one."""
    ranked = sorted(
        (
            (item, item.expectation_progress)
            for item in projected.items
            if item.expectation_progress.total > 0
        ),
        key=lambda entry: (
            0 if entry[1].open > 0 else 1,
            *board.board_rank(entry[0])[:2],
            entry[1].open,
            entry[0].number,
        ),
    )
    return tuple(
        _RulingsRow(
            item, progress, body.expectation_lines(bodies.get(item.number, ""), storage=storage)
        )
        for item, progress in ranked
    )


def _rulings_line_json(line: body.ExpectationLine) -> dict[str, object]:
    payload: dict[str, object] = {
        "index": line.index,
        "text": line.text,
        "ruling": line.ruling,
        "ruled_on": line.ruled_on.isoformat() if line.ruled_on is not None else None,
    }
    payload.update(
        (key, value)
        for key, value in (
            ("question", line.question),
            ("example", line.example),
            ("picture", line.picture),
        )
        if value is not None
    )
    return payload


def _rulings_line_text(line: body.ExpectationLine) -> str:
    state = body.expectation_line_state(line)
    summary = board.terminal_text(body.expectation_line_summary(line))
    return f"  {line.index} {state}: {summary}"


def _rulings_row_text(row: _RulingsRow, storage: body.Storage) -> str:
    label = board.item_label(row.item.number, storage)
    title = board.terminal_text(row.item.title)
    header = f"{label} {row.progress.open}/{row.progress.total}: {title}"
    return "\n".join((header, *(_rulings_line_text(line) for line in row.lines)))


def _rulings(
    projected: board.Board, bodies: Mapping[int, str], *, as_json: bool, storage: body.Storage
) -> None:
    rows = _rulings_rows(projected, bodies, storage=storage)
    if as_json:
        _emit_json(
            True,
            RulingsReason.LISTED,
            rulings=[
                {
                    "number": row.item.number,
                    "title": row.item.title,
                    "open": row.progress.open,
                    "total": row.progress.total,
                    "lines": [_rulings_line_json(line) for line in row.lines],
                }
                for row in rows
            ],
        )
        return
    if not rows:
        print("No expectation lines.")
        return
    print("\n".join(_rulings_row_text(row, storage) for row in rows))


def _ruling_pull_hint(item: board.BoardItem) -> str | None:
    if item.expectation_state is body.ExpectationState.PROPOSED:
        return "expectations unruled: refine before the pull"
    if not item.ruling_old:
        return None
    return f"ruled {item.ruling_landings} landings ago: refine again at the pull"


def _next_action_command(
    action: board.WorkItemAction | board.CutSliceAction,
    storage: body.Storage,
    *,
    claims_in_place: bool,
) -> str:
    """The exact `aco` invocation `_next` prints and `_next --json` carries
    as `command` -- one owner so text and JSON never name a different
    command for the same action. `close_container` has none: there is no
    command to run, and neither grammar invents one.

    A `WorkItemAction` is `claim` where `next` runs in a checkout `claim`
    accepts (`claims_in_place`), else `start` with the slug `start` itself
    derives from the title (issue #562), so the advice runs as printed from
    the default branch's checkout too. Either drops `--scope` entirely for
    an item carrying its own top-level `scope` (issue #348, #337's own
    derivation); an item whose one `[[slice]]` row names paths claims
    exactly those; only an item naming neither still prints the
    placeholder, alongside `SCOPE_UNKNOWN_NOTE`. A scope wider than `claim`'s
    path limit with no `whole` in the body adds `--whole <reason>` (issue
    #566). All render through `board.advice_command`, so the line runs as
    printed (issue #510).
    """
    if isinstance(action, board.CutSliceAction):
        return board.cut_command(action.container.number, storage, action.cut_title)
    item = action.item
    pull = board.PullScope(item.scope, action.scope, item.whole)
    if claims_in_place:
        return board.work_item_claim_command(item.number, storage, pull)
    slug = _advised_slug(item.number, item.title, storage)
    return board.work_item_start_command(item.number, storage, slug, pull)


def _advised_slug(number: int, title: str, storage: body.Storage) -> str:
    """The slug `start` derives from `title`, else, for a title with no
    usable slug (issue #566), the one item `number`'s own id yields -- a
    value `start --slug` accepts, so the advice runs as printed."""
    try:
        return checkout.slug_from_title(title)
    except protocol.ClaimError:
        return checkout.slug_from_title(board.item_argument(number, storage))


def _claims_in_place(context: RunContext) -> bool:
    """Whether `claim` accepts a build claim in this run's checkout: a
    linked worktree on an attached branch other than the default one, the
    isolation `claim` itself requires (CLAIM_DESCRIPTION)."""
    here = checkout.resolve_path_checkout(context.toplevel)
    return (
        here is not None
        and here.kind is checkout.CheckoutKind.LINKED_WORKTREE
        and not checkout.is_detached_head(here.branch)
        and not checkout.is_default_branch(here.branch, context.recorded_default_branch)
    )


class NextReason(StrEnum):
    """`aco next`'s own `--json` `reason` vocabulary (issue #412,
    `specs/next.spec.md`): the action type -- `work_item`, `cut_slice`,
    `check_container`, `close_container` -- names a success (`ok: true`) exactly as it did
    when carried under the dropped `"action"` key; `nothing_actionable`
    is the one `ok: false` outcome that exits `3` in text (NEXT-01) and
    under `--json` alike, the sole reason exit `3` is ever used.
    `invalid_usage` covers `--repo` under `storage = state-ref`
    (PIN-04); every other refusal -- an unsupported forge host (BOARD-02),
    a state-ref checkout with no resolvable default branch (PIN-05) --
    falls to `unavailable`, matching `ask`/`rule`/`brief`'s own catch-all."""

    WORK_ITEM = "work_item"
    CUT_SLICE = "cut_slice"
    CHECK_CONTAINER = "check_container"
    CLOSE_CONTAINER = "close_container"
    NOTHING_ACTIONABLE = "nothing_actionable"
    INVALID_USAGE = "invalid_usage"
    UNAVAILABLE = "unavailable"


def _next_action_reason(action: board.NextAction) -> NextReason:
    if isinstance(action, board.WorkItemAction):
        return NextReason.WORK_ITEM
    if isinstance(action, board.CutSliceAction):
        return NextReason.CUT_SLICE
    if isinstance(action, board.CheckContainerAction):
        return NextReason.CHECK_CONTAINER
    return NextReason.CLOSE_CONTAINER


def _next_action_payload(
    action: board.NextAction, storage: body.Storage, *, claims_in_place: bool
) -> dict[str, object]:
    """The action-specific fields `_next_json` adds beyond `recovery`/`skipped`
    -- `_next_action_reason` now carries what an `"action"` key used to."""
    number = board.item_json_reference(_next_action_item(action).number, storage)
    if isinstance(action, board.WorkItemAction):
        item = action.item
        payload: dict[str, object] = {
            "number": number,
            "score": item.score,
            "title": item.title,
            "next": item.next_step,
            "command": _next_action_command(action, storage, claims_in_place=claims_in_place),
            "ruling_landings": item.ruling_landings,
            "ruling_old": item.ruling_old,
        }
        hint = _ruling_pull_hint(item)
        if hint is not None:
            payload["ruling_hint"] = hint
        return payload
    if isinstance(action, board.CutSliceAction):
        return {
            "number": number,
            "title": action.container.title,
            "slice": action.next_step,
            "cut_title": action.cut_title,
            "command": _next_action_command(action, storage, claims_in_place=claims_in_place),
        }
    return {
        "number": number,
        "closed": action.container_progress.closed,
        "total": action.container_progress.total,
        "next_step": (action.next_step if isinstance(action, board.CheckContainerAction) else None),
    }


# The three sentences issue #348 adds to `next`'s own text form, each named
# once so text and JSON (`_parallel_json`) never restate them (Sonar S1192).
SCOPE_UNKNOWN_NOTE = "scope unknown"
PARALLEL_UNKNOWN_LINE = "parallel: unknown (first action names no scope)"
# "Text zeigt höchstens drei Kandidaten plus 'and N more'" (issue #348 Form):
# a stable display cap, not something an operator tunes.
PARALLEL_TEXT_LIMIT = 3


def _parallel_candidate_label(candidate: board.ParallelCandidate, storage: body.Storage) -> str:
    label = board.item_label(candidate.number, storage)
    count = len(candidate.scope)
    unit = "path" if count == 1 else "paths"
    return f"{label} ({count} {unit})"


def _parallel_line(parallel: board.ParallelSet, storage: body.Storage) -> str:
    """`next`'s own `parallel:` line: the unknown sentence when the first
    action itself names no scope, `none` when the walk placed nothing,
    otherwise every placed candidate capped at `PARALLEL_TEXT_LIMIT` --
    reusing `protocol.named_with_overflow_count`, the one "and N more"
    owner, rather than a second overflow renderer here."""
    if parallel.first_scope_unknown:
        return PARALLEL_UNKNOWN_LINE
    if not parallel.candidates:
        return "parallel: none"
    labels = tuple(
        _parallel_candidate_label(candidate, storage) for candidate in parallel.candidates
    )
    return f"parallel: {protocol.named_with_overflow_count(labels, limit=PARALLEL_TEXT_LIMIT)}"


def _scope_unknown_line(parallel: board.ParallelSet, storage: body.Storage) -> str:
    if not parallel.scope_unknown:
        return "scope unknown: none"
    named = ", ".join(board.item_label(number, storage) for number in parallel.scope_unknown)
    return f"scope unknown: {named}"


def _close_line(close: tuple[int, ...], storage: body.Storage) -> str:
    if not close:
        return "close: none"
    return "close: " + ", ".join(board.item_label(number, storage) for number in close)


def _next_json_numbers(numbers: Iterable[int], storage: body.Storage) -> list[int | str]:
    return [board.item_json_reference(number, storage) for number in numbers]


def _parallel_json(parallel: board.ParallelSet, storage: body.Storage) -> dict[str, object]:
    return {
        "first_scope_unknown": parallel.first_scope_unknown,
        "candidates": [
            {
                "number": board.item_json_reference(candidate.number, storage),
                "scope": list(candidate.scope),
            }
            for candidate in parallel.candidates
        ],
        "scope_unknown": _next_json_numbers(parallel.scope_unknown, storage),
    }


@dataclass(frozen=True)
class _NextReport:
    """Everything `_next`/`_next_json` print for one `next` run, bundled so
    each printer takes one argument instead of PLR0913's five-scalar
    ceiling (issue #348): the board's own first action (`None` when nothing
    qualifies), the unworkable rows `SKIPPED` names, the landed-but-open
    `RECOVERY` rows, the parallel-capacity projection, the zero-cost
    `close:` list, the items waiting on the operator (issue #553), and
    whether `claim` accepts this checkout, which picks `claim` or `start`
    as the advice (issue #562)."""

    action: board.NextAction | None
    skipped: tuple[board.BoardItem, ...]
    recovery: tuple[board.BoardItem, ...]
    parallel: board.ParallelSet
    close: tuple[int, ...]
    waiting: tuple[int, ...]
    claims_in_place: bool


def _next_json(report: _NextReport, storage: body.Storage) -> None:
    reason = (
        _next_action_reason(report.action)
        if report.action is not None
        else NextReason.NOTHING_ACTIONABLE
    )
    payload: dict[str, object] = {
        "recovery": [
            {
                "number": board.item_json_reference(recovery_item.number, storage),
                "title": recovery_item.title,
                "step": board.RECOVERY_STEP,
            }
            for recovery_item in report.recovery
        ],
        "skipped": [
            {
                "number": board.item_json_reference(skipped_item.number, storage),
                "reason": skipped_item.actionable_reason,
            }
            for skipped_item in report.skipped
        ],
        "parallel": _parallel_json(report.parallel, storage),
        "close": _next_json_numbers(report.close, storage),
        "waiting_on_operator": _next_json_numbers(report.waiting, storage),
    }
    if report.action is not None:
        payload.update(
            _next_action_payload(report.action, storage, claims_in_place=report.claims_in_place)
        )
    _emit_json(report.action is not None, reason, **payload)


def _next_action_lines(
    action: board.NextAction, storage: body.Storage, *, claims_in_place: bool
) -> list[str]:
    """The action-specific lines `_next` prints before `parallel:`/`close:`."""
    if isinstance(action, board.WorkItemAction):
        item = action.item
        label = board.item_label(item.number, storage)
        lines = [
            f"{label} score {item.score}: {board.terminal_text(item.title)}",
            f"Next: {board.terminal_text(str(item.next_step))}",
            f"Run: {_next_action_command(action, storage, claims_in_place=claims_in_place)}",
        ]
        if action.scope is None:
            lines.append(SCOPE_UNKNOWN_NOTE)
        hint = _ruling_pull_hint(item)
        if hint is not None:
            lines.append(hint)
        return lines
    container_label = board.item_label(action.container.number, storage)
    if isinstance(action, board.CutSliceAction):
        return [
            f"cut_slice {container_label}: {board.terminal_text(action.next_step)}",
            f"Next: {_next_action_command(action, storage, claims_in_place=claims_in_place)}",
        ]
    if isinstance(action, board.CheckContainerAction):
        return [
            f"check_container {container_label}: {board.CHECK_DONE_WHEN}",
            f"Next: {board.terminal_text(action.next_step)}",
        ]
    progress = action.container_progress
    return [
        f"close_container {container_label}: "
        f"{progress.closed}/{progress.total} children closed, no Next work"
    ]


def _next(report: _NextReport, storage: body.Storage) -> None:
    """A landed-but-open item is named before anything new is pulled; the
    parallel set and the zero-cost `close:` list are named right after the
    first action, unconditionally (issue #348) -- regardless of that
    action's own rank, so a closable container or a recovery item never goes
    unmentioned merely because something else pulled ahead of it."""
    lines: list[str] = []
    if report.recovery:
        lines.append("RECOVERY")
        lines.extend(
            f"{board.item_label(recovery_item.number, storage)}: {board.RECOVERY_STEP}"
            for recovery_item in report.recovery
        )
        lines.append("")
    lines.extend(
        _next_action_lines(report.action, storage, claims_in_place=report.claims_in_place)
        if report.action is not None
        else ["No actionable item."]
    )
    lines.append(_parallel_line(report.parallel, storage))
    if not report.parallel.first_scope_unknown:
        lines.append(_scope_unknown_line(report.parallel, storage))
    lines.append(_close_line(report.close, storage))
    if report.waiting:
        named = ", ".join(board.item_label(number, storage) for number in report.waiting)
        lines.append(f"{board.WAITING_ON_OPERATOR}: {named}")
    if report.skipped:
        skipped_lines = (
            f"{board.item_label(skipped_item.number, storage)}: "
            f"{skipped_item.terminal_actionable_reason}"
            for skipped_item in report.skipped
        )
        lines.extend(("", "SKIPPED", *skipped_lines))
    print("\n".join(lines))


def _unworkable(projected: board.Board) -> tuple[board.BoardItem, ...]:
    return tuple(item for item in projected.items if not item.actionable)


@dataclass(frozen=True)
class SliceCheck:
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


def _fetch_issue_reference(client: forge.ForgeReader, number: int) -> forge.ItemReference:
    """The live state, title, and body of issue `number` from `client`.

    Called only for a claim target that the already-fetched open board
    didn't resolve as OPEN — a closed or missing issue never appears in
    `list_open_board_issues`, so those two states need their own targeted
    lookup; this is that lookup, kept to one issue at a time rather than a
    repository-wide query.
    """
    return client.item_reference(number)


def _issue_reference_state(
    client: forge.ForgeReader,
    open_by_number: dict[int, board.Issue],
    number: int,
) -> tuple[forge.ItemState, str | None, str | None]:
    open_issue = open_by_number.get(number)
    if open_issue is not None:
        return forge.ItemState.OPEN, open_issue.title, open_issue.body
    reference = _fetch_issue_reference(client, number)
    return reference.state, reference.title, reference.body


def _out_of_order_check(
    projected: board.Board,
    issue: int | None,
    out_of_order_reason: str | None,
    storage: body.Storage,
) -> SliceCheck | None:
    highest = board.highest_scored_actionable(projected)
    if highest is None or issue is None:
        return None
    claimed_item = next((item for item in projected.items if item.number == issue), None)
    if claimed_item is None or board.board_rank(highest) >= board.board_rank(claimed_item):
        return None
    return SliceCheck(
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
) -> SliceCheck | None:
    if item is None or not item.open_blockers:
        return None
    blockers = ", ".join(
        board.open_blocker_label(reference, repository, storage) for reference in item.open_blockers
    )
    return SliceCheck(
        "warning" if out_of_order_reason is not None else "error",
        "blocked",
        f"{board.item_label(item.number, storage)} is blocked by {blockers} (open); "
        "pass --out-of-order REASON to claim it anyway",
        issue=item.number,
    )


def _parent_checks(
    client: forge.ForgeReader, repository: str, issue: int, title: str, storage: body.Storage
) -> SliceCheck | None:
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
    return SliceCheck(
        "warning",
        "missing-parent",
        f"looks like slice {slice_number} of {parent_label} but is no sub-issue "
        f"of {parent_label}; the parent inherits nothing",
        slice=slice_number,
        issue=parent_issue,
    )


def _malformed_checks(item: board.BoardItem) -> tuple[SliceCheck, ...] | None:
    """The one refusal a malformed block body gets (#150) -- every other
    body-contract check (blocker state, completeness) never runs, since
    neither the parsed projections nor the blocker set can be trusted once
    the body itself failed to read."""
    if item.read_state is body.BodyReadState.MALFORMED:
        return tuple(
            SliceCheck("error", "body-contract", body.body_defect_text(defect))
            for defect in item.contract.defects
        )
    return None


def _body_contract_checks(item: board.BoardItem, storage: body.Storage) -> tuple[SliceCheck, ...]:
    malformed = _malformed_checks(item)
    if malformed is not None:
        return malformed
    contract = item.contract
    checks = [SliceCheck("error", "body-contract", defect.message) for defect in contract.defects]
    # Read the two atomic facts directly rather than `item.actionable_reason`:
    # that reason is the *first* one `_actionable_reason` finds (frozen,
    # claimed, blocked, then incomplete), so an item that is both blocked and
    # incomplete would report only "blocked" there -- masking the incomplete
    # body this check exists to name. A freshly `cut` child (an incomplete
    # but defect-free skeleton) is refused here exactly as it is invisible to
    # `next`, regardless of what else may also be true of it.
    if not item.contract_complete and not item.projectionless_idea:
        missing = ", ".join(body.missing_or_empty_sections(contract))
        checks.append(
            SliceCheck(
                "error",
                "body-incomplete",
                f"{board.item_label(item.number, storage)} body incomplete: {missing}",
                issue=item.number,
            )
        )
    return tuple(checks)


@dataclass(frozen=True)
class BoardReferenceLookup:
    """The board client, its repository, and the currently open issues it can
    resolve `#reference`s against — what every cross-issue slice/parent check
    below needs to look a referenced issue up."""

    client: forge.ForgeReader
    repository: str
    open_by_number: dict[int, board.Issue]


def _slice_rule_checks(
    lookup: BoardReferenceLookup,
    issue: int,
    projected: board.Board,
    out_of_order_reason: str | None,
    storage: body.Storage,
) -> tuple[SliceCheck, ...]:
    checks: list[SliceCheck] = []
    out_of_order = _out_of_order_check(projected, issue, out_of_order_reason, storage)
    if out_of_order is not None:
        checks.append(out_of_order)
    label = board.item_label(issue, storage)
    item = next((item for item in projected.items if item.number == issue), None)
    if item is not None and item.kind is body.ItemKind.CONTAINER:
        checks.append(
            SliceCheck("error", "container", f"{label} is a container; claim a child", issue=issue)
        )
    blocked = _blocked_check(item, out_of_order_reason, lookup.repository, storage)
    if blocked is not None:
        checks.append(blocked)
    state, title, _body = _issue_reference_state(lookup.client, lookup.open_by_number, issue)
    if state is forge.ItemState.CLOSED:
        checks.append(SliceCheck("error", "closed-issue", f"issue {label} is closed", issue=issue))
    elif state is forge.ItemState.MISSING:
        checks.append(
            SliceCheck("error", "missing-issue", f"issue {label} does not exist here", issue=issue)
        )
    if item is not None:
        checks.extend(_body_contract_checks(item, storage))
    if title is not None:
        parent_check = _parent_checks(lookup.client, lookup.repository, issue, title, storage)
        if parent_check is not None:
            checks.append(parent_check)
    return tuple(checks)


def _refuse_claim(as_json: bool, issue: int | None, checks: tuple[SliceCheck, ...]) -> None:
    if as_json:
        _emit_json(
            False,
            ClaimReason.PRECONDITION_FAILED,
            issue=issue,
            checks=[c.as_json() for c in checks],
        )
        return
    for check in checks:
        print(check.render(), file=sys.stderr)


def _claim_defect(
    claims: tuple[protocol.ActiveClaim, ...],
    detail: forge.Landing,
    identity: protocol.ClaimIdentity,
) -> board.ClassificationDefect | None:
    """A landing declares only what its own head branch holds a live store claim on."""
    matching = next(
        (
            claim
            for claim in claims
            if claim.identity == identity and claim.branch == detail.source_branch
        ),
        None,
    )
    if matching is None:
        subject = (
            f"claim for #{identity.issue}"
            if isinstance(identity, protocol.IssueIdentity)
            else "issue-less lane claim"
        )
        return board.ClassificationDefect(
            f"has no active {subject} on branch {detail.source_branch!r}"
        )
    return None


def _no_item_defect(
    claims: tuple[protocol.ActiveClaim, ...],
    repository: str,
    detail: forge.Landing,
) -> board.ClassificationDefect | None:
    """Why this repository does not accept an issue-less landing as declared.

    A `No-Item` lane owns no issue, so it needs its own lane claim and may
    retire nothing: a closing reference here would close an item no claim and
    no `Work-Item:` line ever named.
    """
    claim_defect = _claim_defect(claims, detail, protocol.LaneIdentity())
    if claim_defect is not None:
        return claim_defect
    closing = board.closing_references(detail.body, repository)
    if closing:
        named = ", ".join(str(reference) for reference in sorted(closing, key=str))
        return board.ClassificationDefect(
            f"declares no work item but closes {named}; name it as the work item"
        )
    return None


@dataclass(frozen=True)
class _LandingCheckContext:
    """What every landing-classification helper needs beyond the pull
    request's own detail and the store's live claims: which forge to read
    and which repository owns the check -- grouped so the cluster of helpers
    below stays under the five-argument limit as claims moved from a
    `client`-read ledger walk to a separately threaded store snapshot."""

    run: RunContext
    repository: str
    storage: body.Storage

    @property
    def client(self) -> forge.ForgeReader:
        return self.run.forge


@dataclass(frozen=True)
class _ParentRequirement:
    """What an item's parent demands of the pull request that lands the item.

    `last_child` says closing the parent is *permitted* -- this landing
    closes the parent's one remaining open child; `closing_required` narrows
    that to *required*, which holds only when the parent's own `Next` line
    names no further work.
    """

    reference: board.IssueReference
    closing_required: bool
    last_child: bool


def _parent_body_finding(reference: board.IssueReference, parsed: body.ParsedBody) -> str:
    """Why a malformed parent body refuses the last-child rule before its
    `Next` line is ever consulted (#150)."""
    defect = parsed.contract.defects[0]
    return f"has parent {reference} with a {body.body_defect_text(defect)}"


def _parent_reference_defect(
    parent: board.ParentIssue, repository: str
) -> board.ClassificationDefect | None:
    """Whether the parent itself is even a same-repository container --
    checked before its body is read at all."""
    if parent.reference.repository != repository:
        return board.ClassificationDefect(
            f"has parent {parent.reference} in another repository, "
            "whose children this check cannot read"
        )
    if parent.kind is not body.ItemKind.CONTAINER:
        kind_text = parent.kind.value if parent.kind is not None else "unknown"
        return board.ClassificationDefect(
            f"has parent {parent.reference} of kind {kind_text}, which is not a "
            "container; only a container holds children"
        )
    return None


def _parent_requirement(
    context: _LandingCheckContext,
    item: board.IssueReference,
) -> _ParentRequirement | board.ClassificationDefect | None:
    """The parent's demand, read from GitHub's sub-issue relation and its
    body under the repository's own pin (#150).

    Closing a parent's last open child completes the parent only when the
    parent's own `Next` line names no further work; otherwise the container
    keeps dispatching slices, and the landing may close the parent (its one
    remaining child) but need not. A parent keeping other open children
    stays open, and must say what happens next.
    """
    parent = context.client.parent_issue(item.number)
    if parent is None:
        return None
    reference_defect = _parent_reference_defect(parent, context.repository)
    if reference_defect is not None:
        return reference_defect
    parsed_parent = body.parse_body(parent.body, storage=context.storage)
    if parsed_parent.read_state is not body.BodyReadState.VALID:
        return board.ClassificationDefect(_parent_body_finding(parent.reference, parsed_parent))
    remaining = tuple(
        child
        for child in context.client.list_children(parent.reference.number)
        if child.state is board.ChildState.OPEN and child.number != item.number
    )
    if not remaining:
        return _ParentRequirement(
            parent.reference,
            not board.has_further_work(parsed_parent.contract.next),
            True,
        )
    if not parsed_parent.contract.next:
        children = "child" if len(remaining) == 1 else "children"
        return board.ClassificationDefect(
            f"leaves parent {parent.reference} open with {len(remaining)} other open "
            f"{children}, whose body carries no Next line"
        )
    return _ParentRequirement(parent.reference, False, False)


def _closing_defect(
    detail: forge.Landing,
    repository: str,
    item: board.IssueReference,
    requirement: _ParentRequirement | None,
) -> board.ClassificationDefect | None:
    """Which issues this landing must close, and that it closes nothing else."""
    closing = board.closing_references(detail.body, repository)
    if item not in closing:
        return board.ClassificationDefect(f"carries no closing reference for its work item {item}")
    if (
        requirement is not None
        and requirement.closing_required
        and requirement.reference not in closing
    ):
        return board.ClassificationDefect(
            f"closes the last open child of parent {requirement.reference}; close the parent too"
        )
    permitted_parent = (
        {requirement.reference} if requirement is not None and requirement.last_child else set()
    )
    besides = tuple(sorted(closing - {item} - permitted_parent, key=str))
    if besides:
        named = ", ".join(str(reference) for reference in besides)
        return board.ClassificationDefect(
            f"closes {named} besides its work item {item}; a pull request lands one item"
        )
    return None


def _work_item_defect(
    context: _LandingCheckContext,
    claims: tuple[protocol.ActiveClaim, ...],
    detail: forge.Landing,
    item: board.IssueReference,
) -> board.ClassificationDefect | None:
    """Why this repository does not accept `item` as the landing pull request's work item."""
    if item.repository != context.repository:
        return board.ClassificationDefect(
            f"names work item {item} of another repository, which holds no claim here"
        )
    claim_defect = _claim_defect(claims, detail, protocol.IssueIdentity(item.number))
    if claim_defect is not None:
        return claim_defect
    requirement = _parent_requirement(context, item)
    if isinstance(requirement, board.ClassificationDefect):
        return requirement
    return _closing_defect(detail, context.repository, item, requirement)


def _structural_classification(
    context: _LandingCheckContext, detail: forge.Landing
) -> board.Classification | board.ClassificationDefect:
    """The classification `check <pr>` and `land` both start from (issue
    #405): whether this pull request's own shape -- its source repository,
    its `Work-Item:`/`No-Item:` line, its target branch -- names one item at
    all. Never touches a claim, a parent, or a closing reference: `land`'s
    own preflight checks the named item's live open state between this and
    `_classification_defect` (LANDCMD-08 before claim validation, issue #405
    review/gate finding)."""
    if detail.source_repository.path != context.repository:
        return board.ClassificationDefect(
            f"proposes a branch of {detail.source_repository}; cross-repository pull "
            "requests are not classified"
        )
    classification = board.parse_pull_request_classification(detail.body, context.repository)
    if isinstance(classification, board.ClassificationDefect):
        return classification
    default_branch = context.run.default_branch
    if detail.target_branch != default_branch:
        return board.ClassificationDefect(
            f"targets {detail.target_branch!r}, not the default branch {default_branch!r}"
        )
    return classification


def _classification_defect(
    context: _LandingCheckContext,
    claims: tuple[protocol.ActiveClaim, ...],
    detail: forge.Landing,
    classification: board.Classification,
) -> board.ClassificationDefect | None:
    """Whether `classification`'s own claim, parent, and closing rules hold
    (issue #405): the second half of `_checked_classification`, split out so
    a caller needing a live-store read only for this half (`land`'s own
    preflight, which fetches store state no earlier than this) never pays
    for one just to read the pull request's own shape."""
    if isinstance(classification, board.NoItemClassification):
        return _no_item_defect(claims, context.repository, detail)
    return _work_item_defect(context, claims, detail, classification.item)


def _checked_classification(
    context: _LandingCheckContext,
    claims: tuple[protocol.ActiveClaim, ...],
    detail: forge.Landing,
) -> board.Classification | board.ClassificationDefect:
    classification = _structural_classification(context, detail)
    if isinstance(classification, board.ClassificationDefect):
        return classification
    defect = _classification_defect(context, claims, detail, classification)
    return classification if defect is None else defect


class CheckKind(StrEnum):
    """Which subject one `check` run read -- the `--json` discriminator.

    The number space holds three of them, because that is what the one
    dispatch request actually distinguishes: GitHub gives issues and pull
    requests a single number space, so a number that is not there was never
    proven to be either. A trunk commit is the fourth: named by its `sha`,
    read from local history instead of that number space (issue #435).
    """

    PULL_REQUEST = "pull_request"
    ISSUE = "issue"
    MISSING = "missing"
    TRUNK_COMMIT = "trunk_commit"


@dataclass(frozen=True)
class NumberSubject:
    """One run's subject in the forge's number space, printed under
    `number`. Its `kind` cannot be `TRUNK_COMMIT`: the key travels with the
    value that determines it, so no answer can name a sha under `number`
    (issue #435)."""

    kind: Literal[CheckKind.PULL_REQUEST, CheckKind.ISSUE, CheckKind.MISSING]
    number: int

    def payload(self) -> dict[str, object]:
        return {"kind": self.kind.value, "number": self.number}


@dataclass(frozen=True)
class TrunkSubject:
    """One run's subject in local trunk history, printed under `sha`. It
    carries no `kind` field, because a commit is never anything but
    `TRUNK_COMMIT` (issue #435)."""

    sha: str

    def payload(self) -> dict[str, object]:
        return {"kind": CheckKind.TRUNK_COMMIT.value, "sha": self.sha}


CheckSubject = NumberSubject | TrunkSubject


class CheckReason(StrEnum):
    """`aco check`'s own `--json` `reason` vocabulary (`specs/check.spec.md`,
    issue #404): `valid` the only exit `0`; `blocked` the only exit `3`
    (a valid, complete issue with an open dependency); every other member
    exits `2`. `valid`/`malformed`/`incomplete` mirror
    `body.BodyShapeVerdict`'s own three values -- the body-shape decision
    `check` and `body --check` both read, never re-derived from a defect
    sentence's own prefix. `not_on_trunk` is the trunk form's own member: a
    commit the first-parent walk does not hold may well exist in this
    repository, so it is never `missing`."""

    VALID = body.BodyShapeVerdict.VALID.value
    BLOCKED = "blocked"
    MALFORMED = body.BodyShapeVerdict.MALFORMED.value
    INCOMPLETE = body.BodyShapeVerdict.INCOMPLETE.value
    INVALID_CLASSIFICATION = "invalid_classification"
    MISSING = "missing"
    NOT_ON_TRUNK = "not_on_trunk"
    INVALID_USAGE = "invalid_usage"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CheckOutcome:
    """One `check` answer: the line a human reads, and the `--json` `reason`
    a caller acts on. `subject` is the number or the trunk commit the run
    was asked about, printing itself under its own key; `message` mirrors
    the line's own finding without its `ISSUE #<n> `/`REFUSED: #<n> `/
    `REFUSED: <sha> ` prefix; `blocked_by` is only ever set together with
    `CheckReason.BLOCKED` (issue #404)."""

    subject: CheckSubject
    line: str
    reason: CheckReason
    message: str | None = None
    blocked_by: tuple[str, ...] = ()

    def exit_code(self) -> int:
        if self.reason is CheckReason.VALID:
            return 0
        if self.reason is CheckReason.BLOCKED:
            return 3
        return 2

    def report(self, *, as_json: bool) -> int:
        if as_json:
            payload = self.subject.payload()
            if self.blocked_by:
                payload["blocked_by"] = list(self.blocked_by)
            if self.message is not None:
                payload["message"] = self.message
            _emit_json(self.reason is CheckReason.VALID, self.reason, **payload)
        else:
            print(self.line, file=sys.stderr if self.message is not None else sys.stdout)
        return self.exit_code()


def _pull_request_check(
    run: RunContext,
    claims: tuple[protocol.ActiveClaim, ...],
    repository: str,
    number: int,
    storage: body.Storage,
) -> CheckOutcome:
    detail = run.forge.landing(number)
    context = _LandingCheckContext(run, repository, storage)
    checked = _checked_classification(context, claims, detail)
    if isinstance(checked, board.ClassificationDefect):
        return CheckOutcome(
            NumberSubject(CheckKind.PULL_REQUEST, detail.number),
            f"REFUSED: pull request #{detail.number} {checked.message}",
            CheckReason.INVALID_CLASSIFICATION,
            checked.message,
        )
    return CheckOutcome(
        NumberSubject(CheckKind.PULL_REQUEST, detail.number),
        f"PR #{detail.number} by {detail.author} declares {checked}",
        CheckReason.VALID,
    )


def _missing_number(repository: str, number: int, storage: body.Storage) -> CheckOutcome:
    """A number neither mode can read, named without claiming which of the
    two it would have been."""
    finding = f"does not exist in {repository}"
    return CheckOutcome(
        NumberSubject(CheckKind.MISSING, number),
        f"REFUSED: {board.item_label(number, storage)} {finding}",
        CheckReason.MISSING,
        finding,
    )


def _issue_line(number: int, finding: str, storage: body.Storage) -> str:
    """The one shape every issue-mode answer takes."""
    return f"ISSUE {board.item_label(number, storage)} {finding}"


def _refused_issue(
    number: int,
    finding: str,
    reason: CheckReason,
    storage: body.Storage,
    *,
    blocked_by: tuple[str, ...] = (),
) -> CheckOutcome:
    return CheckOutcome(
        NumberSubject(CheckKind.ISSUE, number),
        _issue_line(number, finding, storage),
        reason,
        finding,
        blocked_by,
    )


def _body_shape_defects(
    raw_body: str, *, storage: body.Storage = body.Storage.GITHUB
) -> tuple[str, ...]:
    """`item edit`'s own accessor (issue #287) onto `body.body_shape_check`
    (issue #404): only the defect sentences, never the verdict `check
    <item>` and `body --check` read for their own `--json` `reason`."""
    return body.body_shape_check(raw_body, storage=storage).defects


def _issue_check(
    client: forge.ForgeReader,
    repository: str,
    raw_body: str,
    number: int,
    *,
    storage: body.Storage,
) -> CheckOutcome:
    """Whether this issue's body is the contract a builder can start from:
    readable, complete, and unblocked. Its dependencies come from GitHub's
    own `blocked_by` relation, or the state-ref item's own `[record]` table
    under `storage = "state-ref"` -- a body never states them itself."""
    shape = body.body_shape_check(raw_body, storage=storage)
    if shape.defects:
        return _refused_issue(number, shape.defects[0], CheckReason(shape.verdict.value), storage)
    blockers = board.open_dependency_blockers(client.list_board_dependencies(number), repository)
    if blockers:
        labels = tuple(
            board.open_blocker_label(blocker, repository, storage) for blocker in blockers
        )
        return _refused_issue(
            number,
            f"blocked by {', '.join(labels)}",
            CheckReason.BLOCKED,
            storage,
            blocked_by=labels,
        )
    return CheckOutcome(
        NumberSubject(CheckKind.ISSUE, number),
        _issue_line(number, "body ok", storage),
        CheckReason.VALID,
    )


BODY_TEMPLATE_KINDS = ("task", "feature", "container")
# `item edit --kind`'s targets: an item turns container with its first
# child and back to task once none is open (issue #503).
ITEM_EDIT_KINDS = (body.ItemKind.TASK.value, body.ItemKind.CONTAINER.value)
DEFAULT_BODY_TEMPLATE_KIND = "task"


def _read_body_check_input() -> str:
    """`body --check`'s body text, read from stdin only (issue #262 Sonar
    S8707): an agent pipes the file in (`aco body --check < body.md`)
    rather than naming a path the CLI would have to trust."""
    try:
        return sys.stdin.read()
    except UnicodeDecodeError as error:
        raise protocol.ClaimError(
            f"stdin is not valid UTF-8: {error}; pipe the body as UTF-8 text"
        ) from error


def _stdin_file_mode() -> int | None:
    """The type bits of the descriptor on stdin, told without reading, since
    an idle pipe or socket a harness holds open would block a read forever;
    `None` for a closed stdin."""
    try:
        return os.fstat(sys.stdin.fileno()).st_mode
    except (AttributeError, OSError, ValueError):
        return None


def _stdin_is_a_regular_file() -> bool:
    """Whether a file a shell redirected (`< body.md`) stands on stdin. Only
    a regular file counts: an agent harness hands a command a pipe or a
    socket even when it redirected nothing, so a pipe, socket, terminal,
    `/dev/null` or a closed stdin passes, and a body piped in
    (`cat body.md |`) goes unread."""
    mode = _stdin_file_mode()
    return mode is not None and stat.S_ISREG(mode)


def _stdin_carries_a_body() -> bool:
    """Whether stdin carries a body a command reads (head ruling of
    01.10.2026 on issue #555): a redirected file (`< body.md`) or a pipe
    (`printf ... |`). A socket -- what Claude Code's Bash tool hands a
    command -- a terminal, `/dev/null` or a closed stdin carry none, so the
    command never waits on an idle harness stdin."""
    mode = _stdin_file_mode()
    return mode is not None and (stat.S_ISREG(mode) or stat.S_ISFIFO(mode))


class BodyCheckReason(StrEnum):
    """`aco body --check`'s own `--json` `reason` vocabulary
    (`specs/body.spec.md`, issue #404): `valid`/`malformed`/`incomplete`
    mirror `body.BodyShapeVerdict`'s own three values -- the same
    body-shape decision `check <item>`'s own `CheckReason` reads."""

    VALID = body.BodyShapeVerdict.VALID.value
    MALFORMED = body.BodyShapeVerdict.MALFORMED.value
    INCOMPLETE = body.BodyShapeVerdict.INCOMPLETE.value
    UNAVAILABLE = "unavailable"


def _body_check_report(check: body.BodyShapeCheck, *, as_json: bool) -> int:
    """The one rendering of a `body --check` answer: `body.body_shape_check`'s
    own sentences, never truncated to the first, since there is no live
    item here to refuse a single verdict about."""
    ok = check.verdict is body.BodyShapeVerdict.VALID
    if as_json:
        _emit_json(ok, BodyCheckReason(check.verdict.value), defects=list(check.defects))
    elif check.defects:
        for defect in check.defects:
            print(defect, file=sys.stderr)
    else:
        print("body ok")
    return 0 if ok else 2


def _cmd_body(parsed: argparse.Namespace, context: RunContext) -> int:
    """`body --check` is forge-free (issue #262), the same way `status` and
    `bootstrap` are (issue #245): it reads stdin only, never an issue, a
    live claim, a filesystem path, or the forge's own `blocked_by`
    relation, so it never asks the `RunContext` for its forge (`main` dispatches
    it outside `_dispatch`, exactly like `status`). It still reads the
    repository's own storage pin (issue #287): `[record]` is a known key
    under `storage = "state-ref"` and an unknown one under
    `storage = "github"`, the same gate `_state_ref_forge`'s own items
    already read through `_decode_item`."""
    as_json = parsed.json
    try:
        config = context.config
        raw_body = _read_body_check_input()
    except protocol.ClaimError as error:
        return _refuse(BodyCheckReason.UNAVAILABLE, error, as_json=as_json)
    check = body.body_shape_check(raw_body, storage=config.storage)
    return _body_check_report(check, as_json=as_json)


def _github_pull_request_number(value: str) -> int:
    """`--merged`'s value under `storage = "github"` (issue #359): a bare
    decimal pull request number -- the sha grammar and the empty "newest
    trunk landing" sentinel both belong to `storage = "state-ref"` alone,
    since GitHub has no trunk walk to read either from."""
    if not value or not value.isdigit():
        raise protocol.ClaimUnavailableError(
            "--merged requires a pull request number under storage = github"
        )
    return int(value)


def _release_outcome(merged: int | None, abandoned: str | None) -> protocol.ReleaseOutcome:
    if merged is not None:
        return protocol.MergedRelease(merged)
    return protocol.AbandonedRelease(
        protocol._outbound_text(abandoned, "abandoned reason", maximum=512)
    )


@dataclass(frozen=True)
class _MergedLandingClose:
    """The still-open issue `_verify_merged_release` found and the pull
    request that landed it (issue #359 R1): naming both, rather than
    closing on the spot, so `_cmd_release` can call `close_landed_item`
    itself once the claim is resolved and the claimant already authorized --
    an unauthorized or mismatched-claim `--merged` release then never
    reaches a forge write, or even this read, at all."""

    issue: int
    pull_request: int


def _trunk_no_item_landing_defect(
    landings: tuple[checkout.TrunkLanding, ...], sha: str, pull_request: int
) -> str | None:
    """Why `sha` does not authorize an issue-less lane's own `--merged`
    release (issue #405, #397 gate follow-up): mirrors `_trunk_landing_defect`'s
    own walk of the first-parent trunk, for the one classification shape
    that names no work item at all -- read from the merge commit's own
    trailer block, never the pull request's mutable `body` (issue #397,
    Befund 41)."""
    landing = next((entry for entry in landings if entry.sha == sha), None)
    if landing is None:
        return SHA_NOT_ON_TRUNK_DEFECT
    if landing.work_item_values:
        return _lane_work_item_trailer_defect(landing.work_item_values, pull_request)
    classification = landing.classification
    if classification is None:
        return "carries no `Work-Item:` or `No-Item:` trailer"
    if isinstance(classification, board.ClassificationDefect):
        return classification.message
    return None


def _lane_work_item_trailer_defect(work_item_values: tuple[str, ...], pull_request: int) -> str:
    """The lane rule a `Work-Item:` trailer breaks, valid or malformed alike
    (issue #427), ending in the only way out: a landed trailer can no longer
    change, so the lane is released as abandoned with the landing named."""
    values = ", ".join(work_item_values)
    return (
        f"carries `Work-Item: {values}`; an issue-less lane needs a `No-Item: <docs|fix>` "
        f'trailer; release it with --abandoned "landed as PR #{pull_request} '
        'with a malformed trailer"'
    )


def _verify_merged_release(
    context: RunContext,
    client: github.GitHubForge,
    identity: protocol.ClaimIdentity,
    merged: protocol.MergedRelease,
) -> _MergedLandingClose | None:
    """Refuse a `--merged` release the landing itself does not support, and
    report -- without yet closing anything -- whether the named work item is
    still open and needs to be (issue #359 Card 1/R1): `_cmd_release` calls
    this only after the claim is already resolved and the claimant already
    authorized, and performs the actual close itself afterward, so a defect
    or a transient forge failure there never runs ahead of authorization
    and never lands on an unauthorized attempt. `state_board.py`'s own
    `LandingIntent` path is `storage = state-ref`'s equivalent, so this only
    ever runs under `storage = github` (see `_cmd_release`).

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
        context.fetched_default_branch_ref(), TRUNK_LANDING_DEPTH, directory=context.toplevel
    )
    if isinstance(identity, protocol.LaneIdentity):
        defect = _trunk_no_item_landing_defect(landings, detail.merge_commit, detail.number)
        if defect is not None:
            raise protocol.ClaimUnavailableError(
                f"merge commit {detail.merge_commit} of pull request #{detail.number} {defect}"
            )
        return None
    _verify_merge_commit_authority(
        landings, detail.number, identity.issue, detail.merge_commit, context.config.storage
    )
    reference = _fetch_issue_reference(client, identity.issue)
    if reference.state is forge.ItemState.OPEN:
        return _MergedLandingClose(identity.issue, detail.number)
    if reference.state is not forge.ItemState.CLOSED:
        raise protocol.ClaimUnavailableError(
            f"work item #{identity.issue} is {reference.state.value}, not closed"
        )
    return None


def _canonical_remote_name(toplevel: Path) -> str:
    """The configured `canonical_remote` of the checkout at `toplevel`, for
    `protect` (handed into `protect.judge` as `canonical_remote_for`), which
    judges from its own payload's resolved checkout and never builds a
    `RunContext` (issue #457)."""
    return board_config(toplevel).canonical_remote


@dataclass(frozen=True)
class _StoreItemWriter:
    """`state_board.ItemWriter`, implemented over `store` (issue #283): the
    one place this tool hashes an item's finished bytes into a blob and
    writes it through one CAS transition -- an `ItemWriteIntent` (issue
    #279), or for a close an `ItemCloseIntent` (issue #459).
    `state_board.py` itself may not import `store` (Layers contract), so
    this class owns both git steps of a state-ref item write: `_item_write`
    hashes the blob, `_commit` publishes it through the CAS transition.
    """

    context: RunContext

    def write_item(
        self,
        item_id: str,
        *,
        expected: protocol.ObjectId | None,
        content: bytes,
        store_expected: Mapping[str, protocol.ObjectId] | None,
    ) -> protocol.ObjectId:
        return self._commit(self._item_write(item_id, expected, content, store_expected))

    def close_item(
        self,
        item_id: str,
        *,
        number: int,
        expected: protocol.ObjectId,
        content: bytes,
        store_expected: Mapping[str, protocol.ObjectId] | None,
    ) -> protocol.ObjectId:
        write = self._item_write(item_id, expected, content, store_expected)
        return self._commit(protocol.ItemCloseIntent(write, protocol.IssueIdentity(number)))

    def _item_write(
        self,
        item_id: str,
        expected: protocol.ObjectId | None,
        content: bytes,
        store_expected: Mapping[str, protocol.ObjectId] | None,
    ) -> protocol.ItemWriteIntent:
        return protocol.ItemWriteIntent(
            item_id=item_id,
            expected=expected,
            new_oid=store.hash_blob(self.context.toplevel, content),
            operation_id=uuid.uuid4().hex,
            store_expected=store_expected,
        )

    def _commit(
        self, intent: protocol.ItemWriteIntent | protocol.ItemCloseIntent
    ) -> protocol.ObjectId:
        new_state = self.context.transition(
            store.TransitionSubject(f"write item {intent.item_id}"), intent
        )
        return new_state.items[intent.item_id]


def _state_ref_forge(context: RunContext) -> state_board.StateRefBoard:
    """The `state-ref` storage pin's forge (issues #248, #283): repository
    identity and default branch from `context` (host-neutral, the canonical
    remote's recorded `HEAD`),
    item content and blob oids read once through
    `store.read_item_files`/`ClaimState.items` of `context.observation` --
    the very snapshot the command's own checks read (issue #477) -- and a
    write port over that same `store` (`_StoreItemWriter`) -- this is the
    one place `state_board.StateRefBoard` is ever handed live data or a way
    to write it, since the Layers contract keeps that module from reaching
    `store` itself. It reads and writes through `context`'s own worktree:
    `start` (issue #322 review finding 2) hands it a context for the freshly
    created worktree, so the worktree-scoped claim never reads a
    caller-checkout snapshot observed before that worktree existed."""
    repository = context.repository_id
    default_branch = context.default_branch
    state = context.observation
    item_files = {} if state.tip is None else store.read_item_files(context.toplevel, state.tip)
    return state_board.StateRefBoard(
        repository=repository,
        default_branch=default_branch,
        item_files=item_files,
        item_oids=state.items,
        writer=_StoreItemWriter(context),
    )


def _build_forge(context: RunContext) -> forge.ForgeReader:
    """The forge adapter for `context`'s own `storage` pin (issue #248),
    never chosen by the canonical remote's host: `github` (the default)
    builds `github.GitHubForge`; `state-ref` builds
    `state_board.StateRefBoard` from the state ref's own `items/` tree."""
    if context.config.storage is body.Storage.STATE_REF:
        return _state_ref_forge(context)
    return github.GitHubForge(context.repository_id)


def _state_ref_board(context: RunContext) -> state_board.StateRefBoard:
    """`context`'s own forge as the state-ref board, for an item command
    that has already refused every other storage pin: under `state-ref`
    `_build_forge` builds exactly `_state_ref_forge`'s board, so the cast is
    honest, not a suppression -- the same narrowing `RunContext.forge_writer`
    performs for the generic writing surface."""
    return cast(state_board.StateRefBoard, context.forge)


def _run_context(repo: forge.RepositoryId | None) -> RunContext:
    """This run's root context, built once the command is known to need a
    repository at all (after the workspace and `protect` dispatch)."""
    return RunContext(repo, build_forge=_build_forge)


ITEM_NEW_ORIGIN_ON_GITHUB_REFUSAL = '--origin needs storage = "state-ref"'


class ItemReason(StrEnum):
    """`aco item`'s own `--json` `reason` vocabulary, shared across its four
    subcommands (issue #425, `specs/item.spec.md`): `created`/`edited`/
    `closed`/`shown` name each subcommand's own success. `body_invalid`
    covers only `item new`'s and `item edit`'s own piped-body shape check
    (`_body_shape_defects`, the same check `body --check` runs), carrying
    `defects` the same way (`BodyCheckReason`, issue #404); `partial_write`
    only a GitHub issue `item new` created but could not type or record under
    its `--parent` (issue #444), `cut`'s own shape. Every other refusal -- a
    `storage = "github"` command, a missing item or parent, a possible twin,
    a pull request target, a forge write capability, or a live claim still
    on the item -- is `precondition_failed`, this command family's single
    generic bucket."""

    CREATED = "created"
    EDITED = "edited"
    CLOSED = "closed"
    SHOWN = "shown"
    PRECONDITION_FAILED = "precondition_failed"
    BODY_INVALID = "body_invalid"
    PARTIAL_WRITE = "partial_write"


def _refuse_item_body_invalid(defects: tuple[str, ...], *, as_json: bool) -> int:
    print(f"{CLI_ERROR_PREFIX}{defects[0]}", file=sys.stderr)
    if as_json:
        _emit_json(False, ItemReason.BODY_INVALID, defects=list(defects), message=defects[0])
    return 2


def _cmd_item_new(parsed: argparse.Namespace, context: RunContext) -> int:
    """`aco item new` (issues #285, #316, #444): one fresh item under
    either storage pin, each through its own adapter's write
    (`_item_new_on_github`, `_item_new_on_state_ref`), both behind the same
    twin search `cut` runs (`_refuse_possible_twin`). Every refusal but a
    malformed piped body or a partial GitHub write reports through the
    shared envelope as `precondition_failed` (issue #425)."""
    as_json = parsed.json
    try:
        if context.config.storage is body.Storage.GITHUB:
            return _item_new_on_github(parsed, context)
        return _item_new_on_state_ref(parsed, context)
    except _ItemBodyInvalidError as error:
        return _refuse_item_body_invalid(error.defects, as_json=as_json)
    except _PartialWriteError as error:
        return _refuse_partial_write(error, ItemReason.PARTIAL_WRITE, as_json=as_json)
    except protocol.ClaimError as error:
        return _refuse(ItemReason.PRECONDITION_FAILED, error, as_json=as_json)


class _ItemBodyInvalidError(protocol.ClaimError):
    """`item new`'s piped body refused before anything is created, carrying
    every defect sentence for `body_invalid`'s own `defects` list."""

    def __init__(self, defects: tuple[str, ...]) -> None:
        self.defects = defects
        super().__init__(defects[0])


def _read_item_new_input() -> str:
    """`item new`'s piped body under either storage, or `""` when stdin
    carries none (`_stdin_carries_a_body`)."""
    return _read_body_check_input() if _stdin_carries_a_body() else ""


def _item_new_block_fields(parsed: argparse.Namespace) -> dict[str, object]:
    """Every block field `item new`'s own flags name, keyed as the block
    writes it -- only the flags given."""
    scope = _requested_body_scope(parsed.scope)
    requested: dict[str, object | None] = {
        **{key: getattr(parsed, key) for key, _meaning in ITEM_NEW_PROJECTION_FLAGS},
        "scope": None if scope is None else list(scope),
        "size": parsed.size,
        "whole": _requested_whole_reason(parsed.whole),
    }
    return {key: value for key, value in requested.items() if value is not None}


def _item_new_body(parsed: argparse.Namespace, storage: body.Storage) -> str:
    """The body `item new` stores under either storage (issue #555): the
    piped one, its block given each flag it lacks and refused when a flag
    contradicts it; or, piped without a block, the prose above a block the
    flags build -- a container's own skeleton prose when nothing was piped."""
    piped = _read_item_new_input()
    fields = _item_new_block_fields(parsed)
    if not body.carries_agent_claim_block(piped):
        default_prose = (
            body.CONTAINER_SKELETON_PROSE if parsed.kind == body.ItemKind.CONTAINER else ""
        )
        return body.prose_above_fresh_block(piped.rstrip() or default_prose, fields)
    shape = body.body_shape_check(piped, storage=storage)
    if shape.verdict is body.BodyShapeVerdict.MALFORMED:
        raise _ItemBodyInvalidError(shape.defects)
    conflicts = body.block_field_conflicts(piped, fields)
    if conflicts:
        raise _ItemBodyInvalidError(conflicts)
    return body.body_with_block_fields(piped, fields)


def _item_new_on_github(parsed: argparse.Namespace, context: RunContext) -> int:
    """`item new` under `storage = "github"` (issue #444): the body it
    stores (`_item_new_body`) passes the same shape check `aco check <n>`
    applies before
    anything else is read or written, so an invalid body creates nothing;
    `--parent` must name an open container or Task (a Task is retyped once
    the twin search passes, `_retype_task_parent`); then the twin search, the only
    guard against a second run: the issue an earlier run created carries
    the same title, so the search names it. Then one issue of the
    organization's type for `--kind`, recorded under `--parent` when given
    (`create_child`, else `create_issue`); a type or relation write that
    fails after the create refuses naming the issue and what is left to do
    by hand, never a re-run. `--origin` binds a state-ref item only: a
    GitHub issue binds to no foreign one."""
    if parsed.origin is not None:
        raise protocol.ClaimUnavailableError(ITEM_NEW_ORIGIN_ON_GITHUB_REFUSAL)
    new_body = _item_new_body(parsed, body.Storage.GITHUB)
    defects = _body_shape_defects(new_body)
    if defects:
        raise _ItemBodyInvalidError(defects)
    client = context.forge_writer
    open_issues = client.list_open_board_issues()
    storage = context.config.storage
    parent = (
        None
        if parsed.parent is None
        else _open_container(open_issues, parsed.parent, storage, ITEM_PARENT_KINDS)
    )
    if not parsed.not_a_twin:
        _refuse_possible_twin(
            client,
            parsed.title,
            _numbered_titles(open_issues),
            parent=parsed.parent,
            storage=storage,
        )
    _retype_task_parent(client, parent, storage)
    kind = body.ItemKind(parsed.kind)
    try:
        number = (
            client.create_issue(title=parsed.title, body=new_body, kind=kind)
            if parsed.parent is None
            else client.create_child(
                parent=parsed.parent, title=parsed.title, body=new_body, kind=kind
            )
        )
    except forge.ForgeIssueTypeNotSetError as error:
        relation = "" if parsed.parent is None else f" and record it under #{parsed.parent}"
        raise _PartialWriteError(
            error, recovery=f"set that type{relation} on the forge by hand"
        ) from error
    except forge.ForgePartialChildCreationError as error:
        raise _PartialWriteError(
            error, recovery="record that sub-issue relation on the forge by hand"
        ) from error
    _print_item_new_result(
        board.item_label(number, body.Storage.GITHUB), number, as_json=parsed.json
    )
    return 0


def _retype_task_parent(
    client: forge.ForgeWriter, parent: board.Issue | None, storage: body.Storage
) -> None:
    """Retype an open Task `--parent` to Container before it gets its first
    child (issue #503), under either storage, and say so on stderr, leaving
    stdout and `--json` the created item's alone; any other parent keeps
    its kind."""
    if parent is None or parent.kind is not body.ItemKind.TASK:
        return
    client.set_item_kind(parent.number, body.ItemKind.CONTAINER)
    label = board.item_label(parent.number, storage)
    print(f"retyped {label} to Container for its first child", file=sys.stderr)


def _item_new_on_state_ref(parsed: argparse.Namespace, context: RunContext) -> int:
    """`item new` under `storage = "state-ref"` (issues #285, #316): the one
    write path for a fresh state-ref item -- `StateRefBoard.compose_item`
    then `create_item`, the same CAS write `cut`'s own `create_child`
    performs, generalized to an optional parent and origin -- so this
    module never grows a second way to create one. `--origin` binds the
    fresh item to a foreign forge issue (`items.parse_origin`'s own grammar,
    refused by `argparse` before this ever runs) without aco governing that
    forge at all. An open Task parent turns Container
    (`_retype_task_parent`) once the item is composed and before it is
    written, so an item the read would refuse retypes nothing. Narrows the
    context's forge to the state-ref board (`_state_ref_board`), since
    `create_item` is not part of the generic `ForgeWriter` port every other
    write command narrows to. The body is `_item_new_body`'s, the piped one
    stored as under github; one left incomplete is still written, each
    missing section named on stderr (issue #555)."""
    client = _state_ref_board(context)
    parent = parsed.parent
    storage = context.config.storage
    new_body = _item_new_body(parsed, storage)
    if parent is not None and client.item_reference(parent).state is forge.ItemState.MISSING:
        raise protocol.ClaimUnavailableError(f"{board.item_label(parent, storage)} does not exist")
    if not parsed.not_a_twin:
        _refuse_possible_twin(
            client, parsed.title, client.open_item_titles(), parent=parsed.parent, storage=storage
        )
    new_item = client.compose_item(
        title=parsed.title,
        body=new_body,
        kind=body.ItemKind(parsed.kind),
        parent=parsed.parent,
        origin=parsed.origin,
    )
    if parent is not None:
        _retype_task_parent(client, client.open_issue(parent), storage)
    item_id = client.create_item(new_item)
    _print_item_new_result(item_id, items.item_number(item_id), as_json=parsed.json)
    for section in body.missing_or_empty_sections(body.parse_body(new_body).contract):
        print(f"{item_id} misses {section}; aco item edit {item_id} fills it", file=sys.stderr)
    return 0


def _print_item_new_result(item_id: str, number: int, *, as_json: bool) -> None:
    if as_json:
        _emit_json(True, ItemReason.CREATED, item=item_id, number=number)
    else:
        print(item_id)


ITEM_EDIT_GITHUB_REFUSAL = "forge issues are edited on the forge; aco never governs them"


def _cmd_item_edit(parsed: argparse.Namespace, context: RunContext) -> int:
    """`aco item edit ITEM` (issue #287; `--size`, issue #357; `--whole`,
    issue #399; `--kind`, issue #503): with `--size`, `--whole`, or `--kind`,
    a narrow write of only that one field (`_cmd_item_edit_size`/
    `_cmd_item_edit_whole`/`_cmd_item_edit_kind`, both storages); with none,
    the state-ref item's own body, replaced from
    stdin only -- refused before any write when the piped body carries no
    valid `agent-claim` block (`body --check`'s own sentences,
    `_body_shape_defects`). The CAS `expected` oid is this process's own
    already-read snapshot (`StateRefBoard.update_item_body`'s
    `current.oid`, set once at `_state_ref_forge` construction): a second
    process writing from that same snapshot refuses with issue #279's own
    sentence, never merged, never silently overwritten. `parent`, `state`,
    `origin`, `created_at`, and `closed_at` stay this item's own stored
    values regardless of what the piped body's `[record]` names for them --
    `update_item_body`'s own owner rule; `updated_at` always moves to now;
    `title`, `labels`, `blocked_by` come from the piped record when it
    carries one -- except a malformed item (ITEM-39), which takes the piped
    record whole and refuses a body without one; a delivered `blocked_by`
    naming one blocker twice, or a blocker the item does not already carry
    that names no item, a malformed item or the item itself, refuses before
    any write (ITEM-43, ITEM-44). Refuses under
    `storage = "github"`: forge issues are edited on the forge, never
    governed by aco. Narrows the context's forge to the state-ref board,
    as `item new`'s state-ref path does. A malformed piped body reports through
    the shared envelope as `body_invalid`, with `body --check`'s own
    `defects`; every other refusal is `precondition_failed` (issue #425)."""
    if parsed.size is not None:
        return _cmd_item_edit_size(parsed, context)
    if parsed.whole is not None:
        return _cmd_item_edit_whole(parsed, context)
    if parsed.kind is not None:
        return _cmd_item_edit_kind(parsed, context)
    as_json = parsed.json
    try:
        if context.config.storage is not body.Storage.STATE_REF:
            raise protocol.ClaimUnavailableError(ITEM_EDIT_GITHUB_REFUSAL)
        new_body = _read_body_check_input()
        defects = _body_shape_defects(new_body, storage=body.Storage.STATE_REF)
        if defects:
            return _refuse_item_body_invalid(defects, as_json=as_json)
        client = _state_ref_board(context)
        number = parsed.item
        if not client.holds(number):
            raise protocol.ClaimUnavailableError(
                _missing_item_refusal(number, client, context.config.storage)
            )
        client.update_item_body(number, new_body)
        _print_item_edit_result(
            items.format_item_id(number), number, client.item_oid(number), as_json=as_json
        )
        return 0
    except protocol.ClaimError as error:
        return _refuse(ItemReason.PRECONDITION_FAILED, error, as_json=as_json)


ITEM_EDIT_SIZE_COMMAND = "item edit --size"


def _cmd_item_edit_size(parsed: argparse.Namespace, context: RunContext) -> int:
    """`aco item edit ITEM --size S|M|L` (issue #357): the one item-size
    write, over the generic `ForgeWriter.update_item_body` both `github`
    and `state-ref` already implement -- unlike the whole-body `item edit`
    above, this works under `storage = "github"` too, since it patches only
    the block's own top-level `size` key (never `[record]`, a
    `state-ref`-only table BODY-15 refuses under `github`) and leaves every
    other byte untouched. Every refusal reports through the shared envelope
    as `precondition_failed` (issue #425)."""
    as_json = parsed.json
    try:
        client = context.forge_writer
        _require_update_item_body(client, command=ITEM_EDIT_SIZE_COMMAND)
        number = parsed.item
        storage = context.config.storage
        current_body = _item_body_or_refuse(
            client, number, command=ITEM_EDIT_SIZE_COMMAND, storage=storage
        )
        located = _located_block_or_refuse(
            number, current_body, command=ITEM_EDIT_SIZE_COMMAND, storage=storage
        )
        new_data = {**located.data, "size": parsed.size}
        client.update_item_body(
            number, body.replace_agent_claim_block(current_body, located, new_data)
        )
        _print_item_edit_size_result(number, parsed.size, storage, as_json=as_json)
        return 0
    except protocol.ClaimError as error:
        return _refuse(ItemReason.PRECONDITION_FAILED, error, as_json=as_json)


def _print_item_edit_size_result(
    number: int, size: str, storage: body.Storage, *, as_json: bool
) -> None:
    if as_json:
        _emit_json(True, ItemReason.EDITED, item=number, size=size)
    else:
        print(f"EDITED {board.item_label(number, storage)} size={size}")


ITEM_EDIT_WHOLE_COMMAND = "item edit --whole"


def _cmd_item_edit_whole(parsed: argparse.Namespace, context: RunContext) -> int:
    """`aco item edit ITEM --whole REASON` (issue #399): the one item-whole
    write, mirroring `_cmd_item_edit_size` over the same generic
    `ForgeWriter.update_item_body` -- works under `storage = "github"` too,
    since it patches only the block's own top-level `whole` key and leaves
    every other byte untouched. Every refusal reports through the shared
    envelope as `precondition_failed` (issue #425)."""
    as_json = parsed.json
    try:
        client = context.forge_writer
        _require_update_item_body(client, command=ITEM_EDIT_WHOLE_COMMAND)
        number = parsed.item
        storage = context.config.storage
        current_body = _item_body_or_refuse(
            client, number, command=ITEM_EDIT_WHOLE_COMMAND, storage=storage
        )
        located = _located_block_or_refuse(
            number, current_body, command=ITEM_EDIT_WHOLE_COMMAND, storage=storage
        )
        reason = protocol._outbound_text(parsed.whole, _WHOLE_REASON_LABEL, maximum=512)
        new_data = {**located.data, "whole": reason}
        client.update_item_body(
            number, body.replace_agent_claim_block(current_body, located, new_data)
        )
        _print_item_edit_whole_result(number, reason, storage, as_json=as_json)
        return 0
    except protocol.ClaimError as error:
        return _refuse(ItemReason.PRECONDITION_FAILED, error, as_json=as_json)


def _print_item_edit_whole_result(
    number: int, reason: str, storage: body.Storage, *, as_json: bool
) -> None:
    if as_json:
        _emit_json(True, ItemReason.EDITED, item=number, whole=reason)
    else:
        print(f"EDITED {board.item_label(number, storage)} whole={reason}")


ITEM_EDIT_KIND_STDIN_REFUSAL = "item edit --kind reads no stdin; drop the redirect"


def _cmd_item_edit_kind(parsed: argparse.Namespace, context: RunContext) -> int:
    """`aco item edit ITEM --kind task|container` (issue #503): the one
    retype a person runs, over the `ForgeWriter.set_item_kind` both storages
    implement -- the repair `next` names for a nested container with one
    uncut row. Reads no stdin, and refuses a file redirected there before
    any write, so a body redirected from a file is never silently dropped.
    A container with an open child stays one, since a Task never has
    children to claim through. Every refusal reports through the shared
    envelope as `precondition_failed`."""
    as_json = parsed.json
    try:
        if _stdin_is_a_regular_file():
            raise protocol.ClaimUnavailableError(ITEM_EDIT_KIND_STDIN_REFUSAL)
        client = context.forge_writer
        storage = context.config.storage
        number = parsed.item
        kind = body.ItemKind(parsed.kind)
        # Under state-ref the retype reads its item alone (ITEM-37/38, issue
        # #536); the forge answers only through its open-issue list.
        if isinstance(client, state_board.StateRefBoard):
            target = client.open_issue(number)
        else:
            target = next(
                (issue for issue in client.list_open_board_issues() if issue.number == number),
                None,
            )
        label = board.item_label(number, storage)
        if target is None:
            raise protocol.ClaimUnavailableError(f"{label} is not an open item")
        # The retype decides with the item and its children alone (issue
        # #536), and holds the store it read through its write, so a child
        # written since refuses it rather than being guessed past (#447).
        if isinstance(client, state_board.StateRefBoard):
            _refuse_an_unreadable_relative(client, number, with_parent=False)
            client.hold_items()
        if kind is body.ItemKind.TASK and target.has_open_child:
            raise protocol.ClaimUnavailableError(
                f"{label} has an open child; a container with open children stays a container"
            )
        client.set_item_kind(number, kind)
        _print_item_edit_kind_result(number, kind, storage, as_json=as_json)
        return 0
    except protocol.ClaimError as error:
        return _refuse(ItemReason.PRECONDITION_FAILED, error, as_json=as_json)


def _print_item_edit_kind_result(
    number: int, kind: body.ItemKind, storage: body.Storage, *, as_json: bool
) -> None:
    label = board.item_label(number, storage)
    if as_json:
        _emit_json(True, ItemReason.EDITED, item=label, number=number, kind=kind.value)
    else:
        print(f"EDITED {label} kind={kind.value}")


def _print_item_edit_result(
    item_id: str, number: int, oid: protocol.ObjectId, *, as_json: bool
) -> None:
    if as_json:
        _emit_json(True, ItemReason.EDITED, item=item_id, number=number, oid=oid)
    else:
        print(f"EDITED {item_id}")


ITEM_CLOSE_GITHUB_REFUSAL = "the forge closes its issues; aco never governs them"


def _cmd_item_close(parsed: argparse.Namespace, context: RunContext) -> int:
    """`aco item close ITEM` (issue #289): the state-ref item's own record,
    closed -- `state` to `CLOSED`, `closed_at`/`updated_at` to now, the item
    file and every other byte untouched (`StateRefBoard.close_item`'s one
    CAS write over this process's own already-read oid, extending #287's
    record-owner rule by one field rather than composing a record here).
    Refuses under `storage = "github"` by name -- the forge closes its own
    issues, aco never governs them -- and, once the state ref is readable,
    refuses a live claim on the item before every item-state refusal
    (missing, malformed, already closed), then again on every write
    attempt, retries included (`protocol.ItemCloseIntent`, issue #459), so
    a concurrent claim cannot slip past the first check.
    Existence is checked through the ordinary
    `item_reference` read before `close_item` is ever called, so an unknown
    id gets this command's own "does not exist" sentence rather than
    `close_item`'s internal `_by_number` lookup failing with the wrong
    shape; `close_item` itself refuses a second close on an already-closed
    item, naming its date. An unreadable item refuses at its own read; an
    unreadable parent or child, or a parent `items/` lacks, refuses before
    the write (`_refuse_an_unreadable_relative`); any other malformed item only goes
    unfreed, and one failing the `freed:` read after the write turns that line
    into a hint (issue #541). Prints one line, `CLOSED aco-xxxxxx` (`--json`:
    `{"item", "number", "closed_at", "parent_closable"}`), then `release
    --merged`'s own `freed:` line -- open items whose only open local
    blocker was this one (`_freed_item_numbers`, issue #256; nothing new) --
    and, when this close was its parent's last open child, `release
    --merged`'s own parent hint (issue #348). Every refusal reports through
    the shared envelope as `precondition_failed` (issue #425)."""
    as_json = parsed.json
    try:
        if context.config.storage is not body.Storage.STATE_REF:
            raise protocol.ClaimUnavailableError(ITEM_CLOSE_GITHUB_REFUSAL)
        number = parsed.item
        observed = context.observation
        _require_state_ref(observed)
        protocol.require_no_live_claim(observed, protocol.IssueIdentity(number))
        client = _state_ref_board(context)
        if client.item_reference(number).state is forge.ItemState.MISSING:
            raise protocol.ClaimUnavailableError(
                _missing_item_refusal(number, client, context.config.storage)
            )
        _refuse_an_unreadable_relative(client, number, with_parent=True)
        closed_at = client.close_item(number)
        result = _ItemCloseResult(
            item_id=items.format_item_id(number),
            number=number,
            closed_at=closed_at,
            freed=_board_read_after_write(lambda: _item_close_freed(client, number)),
            parent_closable=_closable_parent_of_a_closed_item(client, number),
        )
        _print_item_close_result(result, as_json=as_json)
        return 0
    except protocol.ClaimError as error:
        named = _named_refusal(error, body.Storage.STATE_REF)
        return _refuse(ItemReason.PRECONDITION_FAILED, named, as_json=as_json)


def _refuse_an_unreadable_relative(
    client: state_board.StateRefBoard, number: int, *, with_parent: bool
) -> None:
    """Refuses by the lowest id while one of `number`'s children -- the same
    children the board counts, an unreadable item that may be one included
    (issue #550, ITEM-54) -- or, `with_parent`, its parent does not read
    (issue #536, ITEM-53): a single-item write decides with those alone, so
    each is read through the one narrow `item_references` read, and an
    unrelated malformed item never blocks the write. `parent_number` refuses
    a parent `items/` lacks (PIN-16) here too, before the write rather than
    after it."""
    relatives = {child.number for child in client.list_children(number)}
    parent = client.parent_number(number) if with_parent else None
    if parent is not None:
        relatives.add(parent)
    client.item_references(sorted(relatives))


def _closable_parent_of_a_closed_item(
    client: state_board.StateRefBoard, closed_item: int
) -> int | None:
    """`_parent_closable_number`'s parent hint, withheld while the parent's
    own `item close` would refuse by an unreadable relative (issue #536,
    ITEM-54): the hint recommends that close, so it asks the very check
    that close runs rather than contradicting it."""
    closable = _parent_closable_number(client, closed_item, body.Storage.STATE_REF)
    if closable is None:
        return None
    try:
        _refuse_an_unreadable_relative(client, closable, with_parent=True)
    except protocol.MalformedStateTreeError:
        return None
    return closable


def _item_close_freed(client: forge.ForgeReader, number: int) -> tuple[int, ...]:
    """Every open item `number`'s own close just freed -- issue #256's own
    derivation, reused rather than reinvented, the same base-issues-plus-
    dependency-fetch wave `_release_landing` runs for a merged release, read
    fresh off `client`'s own already-closed view so `number` itself never
    appears among its own candidates."""
    issues = client.list_open_board_issues()
    candidates = tuple(issue.number for issue in issues if issue.blocked_by_count > 0)
    dependencies = _validated_dependencies(issues, _fetch_dependencies(client, candidates))
    landed = board.IssueReference(client.repository.path, number)
    return _freed_item_numbers(dependencies, landed)


@dataclass(frozen=True)
class _ItemCloseResult:
    """Everything `_print_item_close_result` needs for one `item close`
    (issue #348), bundled so the printer itself takes one argument instead
    of PLR0913's five-scalar ceiling. `freed` is the freed numbers, or
    the hint that replaced them when that read refused (issue #541)."""

    item_id: str
    number: int
    closed_at: str
    freed: tuple[int, ...] | str
    parent_closable: int | None


def _print_item_close_result(result: _ItemCloseResult, *, as_json: bool) -> None:
    if as_json:
        _emit_json(
            True,
            ItemReason.CLOSED,
            item=result.item_id,
            number=result.number,
            closed_at=result.closed_at,
            parent_closable=result.parent_closable,
        )
        if isinstance(result.freed, str):
            _print_board_read_hint(result.freed, as_json=True)
        return
    print(f"CLOSED {result.item_id}")
    if isinstance(result.freed, str):
        _print_board_read_hint(result.freed, as_json=False)
    else:
        # `item close` only ever runs under `storage = "state-ref"`
        # (`_cmd_item_close`'s own refusal otherwise), so `freed:`'s own id
        # chooser is fixed here rather than threaded as a further field.
        print(_release_freed_line(result.freed, body.Storage.STATE_REF))
    parent_line = _parent_closable_line(result.parent_closable, body.Storage.STATE_REF)
    if parent_line is not None:
        print(parent_line)


def _item_state_text(state: forge.ItemState) -> str:
    return "open" if state is forge.ItemState.OPEN else "closed"


def _item_parent_id(parent: int | None) -> str | None:
    return None if parent is None else items.format_item_id(parent)


def _item_header(number: int, reference: forge.ItemReference, parent: int | None) -> str:
    """`item show`'s one header line: id, number, state, parent, and origin
    -- the same shape regardless of which forge answered the reads, since an
    id (`items.format_item_id`) is a pure encoding of `number`, never a
    per-adapter fact. `reference.origin` (issue #316) stays `None` for a
    GitHub-stored item, so this prints `origin none` there exactly like an
    unset parent prints `parent none`."""
    return (
        f"{items.format_item_id(number)} · #{number} · "
        f"{_item_state_text(reference.state)} · parent {_item_parent_id(parent) or 'none'} · "
        f"origin {reference.origin or 'none'}"
    )


def _cmd_item_show(parsed: argparse.Namespace, context: RunContext) -> int:
    """`aco item show` (issue #285): the stored body behind one header
    line, its display controls escaped in text (issue #544), byte-exact
    under `--json` -- read through the ordinary forge port, so it works
    identically under `storage = "github"` (the forge's own issue body) and
    `storage = "state-ref"` (the item file's own body); closing an item
    never deletes it, so a closed item is shown exactly like an open one.
    A missing id -- or a forge that fails either read this header needs --
    reports through the shared envelope as `precondition_failed` (issue
    #425)."""
    as_json = parsed.json
    try:
        client = context.forge
        number = parsed.item
        reference = client.item_reference(number)
        if reference.state is forge.ItemState.MISSING:
            storage = context.config.storage
            raise protocol.ClaimUnavailableError(_missing_item_refusal(number, client, storage))
        parent = client.parent_number(number)
    except protocol.ClaimError as error:
        return _refuse(ItemReason.PRECONDITION_FAILED, error, as_json=as_json)
    body = reference.body or ""
    if as_json:
        _emit_json(
            True,
            ItemReason.SHOWN,
            item=items.format_item_id(number),
            number=number,
            state=_item_state_text(reference.state),
            parent=_item_parent_id(parent),
            origin=reference.origin,
            body=body,
        )
        return 0
    print(_item_header(number, reference, parent))
    print(board.terminal_block(body), end="")
    return 0


def _claim_ages(worktree: Path, state: protocol.ClaimState) -> dict[str, datetime]:
    """Each live claim's age in an already-fetched state -- one batched read
    of `state.tip`'s history (`store.claim_ages`, issue #242), never a git
    call per claim.
    """
    if state.tip is None:
        return {}
    return store.claim_ages(worktree=worktree, tip=state.tip, claims=state.claims.values())


def _claim_lifecycle(worktree: Path, state: protocol.ClaimState) -> store.ClaimLifecycle:
    """Every claim's own ref-history lifecycle in an already-fetched state
    (issue #357) -- one batched `store.claim_lifecycle` walk of `state.tip`'s
    history, mirroring `_claim_ages`'s own no-refetch seam: a fresh fetch of
    `refs/aco/state` never happens twice for the one board build that
    already read it."""
    if state.tip is None:
        return store.ClaimLifecycle(events=(), unparsed=0)
    return store.claim_lifecycle(worktree=worktree, tip=state.tip)


def _claim_history(worktree: Path, state: protocol.ClaimState) -> _ClaimHistory:
    """`_board`'s own bundle of the two store history reads a full board
    build needs, from one already-fetched state -- `board`, `rulings`,
    `next`, and `board --html`/`--serve` all read this rather than
    `_claim_ages` and `_claim_lifecycle` separately."""
    lifecycle = _claim_lifecycle(worktree, state)
    return _ClaimHistory(
        ages=_claim_ages(worktree, state),
        lane_events=lifecycle.events,
        unparsed_lifecycle_commits=lifecycle.unparsed,
    )


def _require_state_ref(state: protocol.ClaimState) -> None:
    if state.tip is None:
        raise protocol.ClaimError(protocol.MISSING_STATE_REF)


def _transition_subject(
    action: str, identity: protocol.ClaimIdentity, branch: str
) -> store.ClaimTransitionSubject:
    """One claim-shaped transition's own `store.ClaimTransitionSubject` (§1
    "Commit message"; issue #357 R2): `text` is `claim issue 42`, `rescope
    lane docs/lane-cleanup`, and so on -- `action` plus `issue`/`lane` plus
    `protocol.transition_item_identifier`'s own bare identifier; `item` is
    that same identifier again, carried separately into the commit's own
    mandatory `item:` trailer rather than reconstructed from `text` by
    `claim_lifecycle`'s reader."""
    kind = "lane" if isinstance(identity, protocol.LaneIdentity) else "issue"
    item = protocol.transition_item_identifier(identity, branch)
    return store.ClaimTransitionSubject(f"{action} {kind} {item}", item=item)


def _claim_intent_from_request(
    request: protocol.ClaimRequest, operation_id: str, item_pin: protocol.ItemPin | None
) -> protocol.ClaimIntent:
    """`_request`'s validated `ClaimRequest`, converted to the store's own
    intent (issue #176, §1: `ClaimRequest` stays the CLI-facing input,
    `ClaimIntent` is what `apply` actually consumes), pinned to `item_pin`
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
    claim id -- `apply` itself only replays on an exact claim-id match
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


def _selected_store_claim(
    observed: protocol.ClaimState,
    identity: protocol.ClaimIdentity,
    branch: str | None,
    claim_id: str | None,
    storage: body.Storage,
) -> protocol.ActiveClaim:
    """The one live store claim `rescope`/`release` names: at most one claim
    is ever live per identity (the store's own invariant), so this is a
    direct key lookup, never the ledger's filter-then-disambiguate walk.
    `claim_id`, when given, is a safety check against that one claim, not a
    selector among several -- there are never several. A refusal names the
    item in `storage`'s form (issue #471).
    """
    if isinstance(identity, protocol.LaneIdentity) and not branch:
        raise protocol.ClaimUnavailableError(
            "lane release requires a non-empty current branch; check out the "
            "docs/ or fix/ lane branch, or pass an issue number"
        )
    selected = observed.claims.get(protocol.claim_key(identity, branch or ""))
    if selected is None or (claim_id is not None and selected.claim_id != claim_id):
        subject = protocol.identity_summary(identity, branch or "", board.item_labeller(storage))
        raise protocol.ClaimUnavailableError(f"{subject} has no active build claim")
    return selected


def _hook_payload() -> dict[str, object] | None:
    try:
        payload = json.loads(sys.stdin.read())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _protect() -> int:
    # Grok fail-opens on crash or non-JSON hook output; deny instead of raising.
    try:
        payload = _hook_payload()
        verdict = protect.judge(payload, canonical_remote_for=_canonical_remote_name)
    except Exception as error:
        verdict = protect.Verdict.deny(str(error))
    if verdict.stdout_text is not None:
        print(verdict.stdout_text)
    if verdict.stderr_text is not None:
        print(verdict.stderr_text, file=sys.stderr)
    return verdict.exit_code


def _optional_issue_number(value: int | None) -> int | None:
    return None if value is None else int(value)


def _rescope_location(add: list[str] | None, drop: list[str] | None) -> Path:
    """The path `rescope`'s checkout is resolved from (issue #314
    repeat gate, finding R1): every `--add`/`--drop` entry must itself be an
    absolute path -- the one location signal a dispatcher running in a
    foreign cwd (the head's own shared environment, editing a linked
    worktree through a subagent) can give without knowing that cwd. A
    relative entry carries no location of its own and is never joined to the
    process's cwd to guess one -- finding R2's same principle, applied here:
    any relative entry, anywhere in either list, denies outright with the
    same sentence `protect`'s own relative-payload-path gate uses, never
    falling back to cwd to interpret it. `rescope` falls back to its own
    process cwd only when neither flag names a single path at all -- its
    other legitimate location signal, unchanged from before this fix for
    that ordinary, undispatched case, and a distinct usage error
    (`_combined_scope`'s own "does not change the claim scope") handles it
    from there."""
    entries = (*(add or ()), *(drop or ()))
    if any(not Path(raw_path).is_absolute() for raw_path in entries):
        raise _RescopeInvalidUsageError(checkout.RELATIVE_PAYLOAD_PATH_DENIAL)
    if entries:
        return Path(entries[0])
    return Path.cwd()


def _rescope_scope_entries(
    raw_paths: list[str] | None, *, toplevel: Path, flag: str
) -> tuple[str, ...]:
    """One `--add`/`--drop` list, canonicalized to repository-relative scope
    entries against `toplevel`. Every entry here is already absolute:
    `_rescope_location` (issue #314 repeat gate, finding R1) denies outright
    before this ever runs if any entry in either list is relative, so there
    is no repository-relative form left to accept as-is. A path no claim
    could ever cover refuses with the sentence `protect` denies it with
    (issue #483)."""
    if not raw_paths:
        return ()
    canonical: list[str] = []
    for raw_path in raw_paths:
        unscopable = checkout.unscopable_path_reason(raw_path, toplevel=toplevel)
        if unscopable is not None:
            raise _RescopeInvalidUsageError(unscopable)
        relative = checkout.relative_scope_entry(raw_path, toplevel=toplevel)
        if relative is None:
            raise _RescopeInvalidUsageError(
                f"{flag} path {raw_path!r} is outside the resolved checkout {toplevel}"
            )
        canonical.append(relative)
    return protocol.valid_scope(canonical)


def _rescope_checkout(parsed: argparse.Namespace) -> checkout.PathCheckout:
    """`rescope`'s checkout, resolved from a path it is given whenever one
    names a location (issue #314 delta, finding R1), read through the same
    path-based resolver `protect` uses rather than the ad hoc
    `git branch --show-current` this replaces, so it fails the same way
    regardless of where else in the tree a bare cwd fallback might have
    looked. A checkout with no commit yet denies here too (gate G3), the
    same precondition `protect` enforces on its own resolved checkout. The
    path resolves exactly as `protect` resolves a payload path -- a
    checkout root as its own checkout, a file in a directory not created
    yet from its nearest existing ancestor (issues #474, #483)."""
    path_checkout = checkout.resolve_named_path_checkout(_rescope_location(parsed.add, parsed.drop))
    if path_checkout is None:
        raise protocol.ClaimUnavailableError(checkout.NOT_IN_A_REPOSITORY_REASON)
    if not path_checkout.has_commit:
        raise protocol.ClaimUnavailableError(checkout.NO_COMMIT_CHECKOUT_REASON)
    return path_checkout


def _rescope_command(
    parsed: argparse.Namespace, path_checkout: checkout.PathCheckout, checkout_context: RunContext
) -> protocol.RescopeRequest:
    """The rescope `parsed` asks for in `path_checkout`, judged against the
    resolved checkout's own recorded default branch (issue #490)."""
    branch = path_checkout.branch
    if not branch:
        raise protocol.ClaimUnavailableError(
            "rescope requires a non-empty current branch; "
            "check out the claim branch, or pass an issue number"
        )
    checkout._refuse_shared_checkout(
        path_checkout,
        default_branch=checkout_context.recorded_default_branch,
        canonical_remote=checkout_context.canonical_remote,
        repair=checkout.WorktreeRepair.RETURN_TO_CLAIM,
    )
    identity = _resolved_identity(_optional_issue_number(parsed.issue), branch)
    return protocol.RescopeRequest(
        identity=identity,
        agent=parsed.agent,
        add=_rescope_scope_entries(parsed.add, toplevel=path_checkout.toplevel, flag="--add"),
        drop=_rescope_scope_entries(parsed.drop, toplevel=path_checkout.toplevel, flag="--drop"),
        claim_id=parsed.claim_id,
        branch=branch,
        whole_reason=_optional_whole_reason(parsed),
    )


def _cmd_check(parsed: argparse.Namespace, context: RunContext) -> int:
    """One number or trunk commit, one dispatch request into one of four
    answers: a pull request to classify, an issue whose body contract to
    read, a trunk commit to classify from its own trailer block (issue
    #359, LAND-48), or a number that is in neither number space. Only the
    pull-request side needs the live claims, so the issue side never
    fetches the state ref."""
    as_json = parsed.json
    try:
        if isinstance(parsed.number, str):
            return _check_trunk_commit(parsed, context)
        number = int(parsed.number)
        client = context.forge
        # Read for its refusals only: a repository pinned to a grammar this
        # tool no longer reads, or a forge that cannot answer `blocked_by`,
        # must fail here rather than hand back a half-read answer. Every
        # further forge read (the reference itself, the PR/issue body it
        # dispatches to) stays inside this handler too, so a forge failure
        # anywhere on the check path reports `unavailable` through the
        # shared envelope rather than the plain sentence alone.
        config = _load_board_config(client, context)
        repository = client.repository.path
        reference = client.item_reference(number)
        if reference.state is forge.ItemState.MISSING:
            outcome = _missing_number(repository, number, config.storage)
        elif reference.is_landing:
            outcome = _pull_request_check(
                context,
                tuple(context.observation.claims.values()),
                repository,
                number,
                config.storage,
            )
        else:
            outcome = _issue_check(
                client, repository, reference.body or "", number, storage=config.storage
            )
    except RepoMeaninglessUnderStateRefError as error:
        return _refuse(CheckReason.INVALID_USAGE, error, as_json=as_json)
    except protocol.ClaimError as error:
        return _refuse(CheckReason.UNAVAILABLE, error, as_json=as_json)
    return outcome.report(as_json=as_json)


def _trunk_classification_text(
    classification: board.TrunkClassification, storage: body.Storage
) -> str:
    """`classification`'s own display line, the trunk-trailer counterpart of
    `WorkItemClassification`/`NoItemClassification`'s `__str__` (issue
    #359): a trunk `Work-Item:` trailer names bare, repository-local
    numbers (`TrunkWorkItemClassification.numbers`), never a qualified
    `IssueReference` -- a commit's own trailer is always local -- so this
    lives here rather than reusing either PR-body type's `__str__`."""
    if isinstance(classification, board.NoItemClassification):
        return f"No-Item: {classification.kind.value}"
    return "Work-Item: " + ", ".join(
        board.item_label(number, storage) for number in classification.numbers
    )


def _refused_trunk_commit(sha: str, finding: str, reason: CheckReason) -> CheckOutcome:
    return CheckOutcome(TrunkSubject(sha), f"REFUSED: {sha} {finding}", reason, finding)


STATE_REF_TRAILER_PAST_THE_ID_SPACE = "carries `Work-Item:` {number}, which " + board.NAMES_NO_ITEM


def _trailer_number_past_the_id_space(
    classification: board.TrunkClassification, storage: body.Storage
) -> int | None:
    """LAND-68 (issue #467, #469 review): under `storage = "state-ref"` a
    trailer number past `aco-ffffff` names no item, and its label would be
    an id `board.parse_item_reference` refuses back -- the first such
    number, or `None` when every number names an item."""
    if not isinstance(classification, board.TrunkWorkItemClassification):
        return None
    return next(
        (number for number in classification.numbers if board.names_no_item(number, storage)),
        None,
    )


def _trunk_commit_outcome(
    sha: str, landing: checkout.TrunkLanding | None, storage: body.Storage
) -> CheckOutcome:
    """`sha`'s own answer (issue #359, LAND-48): the same three answers
    `check <pr>` reads from a pull request body's classification
    (LAND-04/LAND-06), read instead from the trailer block of `landing`, the
    walked first-parent trunk's own entry for `sha` -- `None` when that walk
    holds no such commit at all."""
    if landing is None:
        return _refused_trunk_commit(sha, SHA_NOT_ON_TRUNK_DEFECT, CheckReason.NOT_ON_TRUNK)
    classification = landing.classification
    if classification is None:
        return _refused_trunk_commit(
            sha,
            "carries no `Work-Item:` or `No-Item:` trailer",
            CheckReason.INVALID_CLASSIFICATION,
        )
    if isinstance(classification, board.ClassificationDefect):
        return _refused_trunk_commit(
            sha, classification.message, CheckReason.INVALID_CLASSIFICATION
        )
    past = _trailer_number_past_the_id_space(classification, storage)
    if past is not None:
        return _refused_trunk_commit(
            sha,
            STATE_REF_TRAILER_PAST_THE_ID_SPACE.format(number=past),
            CheckReason.INVALID_CLASSIFICATION,
        )
    return CheckOutcome(
        TrunkSubject(sha),
        f"{sha} declares {_trunk_classification_text(classification, storage)}",
        CheckReason.VALID,
    )


def _check_trunk_commit(parsed: argparse.Namespace, context: RunContext) -> int:
    """`check <sha>`: the trunk form of the one check, reporting through the
    same envelope and the same exit codes the number forms use (issue #435).
    Its walk is the one `release --merged <sha>` verifies against under
    `storage = "state-ref"` (LAND-47/LAND-52), reused rather than re-derived
    here. Needs no forge at all: a trunk commit's trailer is local history."""
    sha = cast(str, parsed.number)
    landings = checkout.trunk_landings(
        context.trunk_ref, TRUNK_LANDING_DEPTH, directory=context.toplevel
    )
    landing = next((entry for entry in landings if entry.sha == sha), None)
    return _trunk_commit_outcome(sha, landing, context.config.storage).report(as_json=parsed.json)


def _brief_claim(
    claims: tuple[protocol.ActiveClaim, ...], item: int
) -> protocol.ActiveClaim | None:
    """This item's own live claim -- one exclusive build claim per issue
    before the first edit (README), so at most one is ever live; the first
    match is it. `None` when the item carries no live claim at all."""
    for claim in claims:
        if isinstance(claim.identity, protocol.IssueIdentity) and claim.identity.issue == item:
            return claim
    return None


@dataclass(frozen=True)
class _BriefClaim:
    """`brief`'s own live-claim reading: the claim paired with the one age it
    needs -- never every live claim's age like `_claim_ages`, so a lineage
    break in an unrelated claim can never stop this item's brief."""

    claim: protocol.ActiveClaim
    opened_at: datetime


def _brief_live_claim(worktree: Path, state: protocol.ClaimState, item: int) -> _BriefClaim | None:
    claim = _brief_claim(tuple(state.claims.values()), item)
    if claim is None:
        return None
    # A live claim cannot exist without the ref it was read from.
    tip = cast(protocol.ObjectId, state.tip)
    opened_at = store.claim_ages(worktree=worktree, tip=tip, claims=(claim,))[claim.claim_id]
    return _BriefClaim(claim, opened_at)


def _lane_tip(branch: str) -> str | None:
    """`branch`'s current commit -- local first, then `origin/` -- or `None`
    when neither ref resolves (a deleted or not-yet-pushed lane branch). A
    git failure that keeps either read from answering (issue #390 finding
    9b) raises instead of being read the same way as an absent ref."""
    for ref in (branch, f"origin/{branch}"):
        commit = checkout.resolved_commit(ref)
        if commit is not None:
            return commit
    return None


def _print_brief_claim(
    claim: protocol.ActiveClaim, opened_at: datetime, observed_at: datetime
) -> None:
    print(
        f"{claim.agent} ({claim.role}) branch={claim.branch} base={claim.base}"
        f"{_claim_age_suffix(opened_at, observed_at)}"
    )
    for path in claim.scope:
        print(f"  {path}")
    if claim.whole_reason is not None:
        print(f"  whole: {claim.whole_reason}")


def _print_brief_step_rules(step_rules: board.BriefStepRules) -> None:
    """`RULES` and `CHECKS` (issue #324): two more sections after the four
    `_print_brief` always prints, only under `--step` -- the repository's own
    `.agent-claim/brief.toml` entries for that lane step, one per line,
    each as `terminal_text` shows it so no entry can break into a forged
    line or section (issue #548)."""
    print()
    print("RULES")
    for rule in step_rules.rules:
        print(board.terminal_text(rule))
    print()
    print("CHECKS")
    for check in step_rules.checks:
        print(board.terminal_text(check))


@dataclass(frozen=True)
class _BriefComposition:
    """One `aco brief`'s full composed reads (issue #324): the four sections
    `_print_brief` always prints, plus, only under `--step`, the repository's
    own rules and checks -- gathered once so the text and `--json` renderers
    print the same reads with no chance to drift apart."""

    body: str
    live: _BriefClaim | None
    observed_at: datetime
    tip: str | None
    touched: tuple[str, ...]
    step_rules: board.BriefStepRules | None


def _print_brief(composition: _BriefComposition) -> None:
    print(board.terminal_block(composition.body))
    print()
    print("CLAIM")
    if composition.live is None:
        print("no active claim")
    else:
        _print_brief_claim(
            composition.live.claim, composition.live.opened_at, composition.observed_at
        )
    print()
    print("TIP")
    if composition.live is not None:
        print(composition.tip if composition.tip is not None else "branch not found")
    print()
    print("TOUCHED")
    for path in composition.touched:
        print(path)
    if composition.step_rules is not None:
        _print_brief_step_rules(composition.step_rules)


def _emit_json(ok: bool, reason: StrEnum, **payload: object) -> None:
    """The one `--json` envelope owner (issue #396, `specs/output.spec.md`):
    `ok` and `reason` always print first, in that order -- `reason` a
    stable enum token, never an `error` object -- then the payload in the
    order given, with an optional `message` (free prose) moved last so a
    reader can stop before it. `ask`, `rule`, and `brief` are its first
    callers, on success and on every refusal alike. `reason` is typed
    `StrEnum` -- the shared base every command's own reason vocabulary
    (`AskReason`, `RuleReason`, `BriefReason`) already subclasses -- so a
    loose string can never reach this envelope."""
    message = payload.pop("message", None)
    envelope: dict[str, object] = {"ok": ok, "reason": reason}
    envelope.update(payload)
    if message is not None:
        envelope["message"] = message
    print(json.dumps(envelope))


STATE_REF_ITEM_PAST_THE_ID_SPACE = "{number} " + board.NAMES_NO_ITEM


class PreDispatchReason(StrEnum):
    """The one `reason` every refusal raised before the chosen command starts
    reports under `--json` (issue #425): agent identity resolution, and
    `release`'s own branch and override checks, refuse before any command
    runs, so `_dispatch` owns their envelope here instead of each command
    carrying a second `precondition_failed` member for a refusal it never
    sees itself. `invalid_usage` is the parser's own refusal (issue #432),
    raised before any command is even chosen, and PIN-31's state-ref item
    number past the id space (issue #467), an argument no command can use."""

    INVALID_USAGE = "invalid_usage"
    PRECONDITION_FAILED = "precondition_failed"


def _refuse(reason: StrEnum, error: protocol.ClaimError, *, as_json: bool) -> int:
    """One shared refusal report for every `--json` command (issue #396) and
    for `_dispatch`'s own pre-start checks (issue #425): `ERROR: <sentence>`
    on stderr exactly as `main`'s own generic sink always printed it, then
    -- only under `--json` -- the envelope naming this call's own reason
    instead of the dropped `error` key. Exit `2`, the one exit every refusal
    past the parser still uses."""
    print(f"{CLI_ERROR_PREFIX}{error}", file=sys.stderr)
    if as_json:
        _emit_json(False, reason, message=str(error))
    return 2


def _refuse_usage(error: _UsageError, *, as_json: bool) -> int:
    """The argument parser's own refusal, in the shape this invocation asked
    for (issue #432): without `--json` argparse's own usage block and
    sentence, printed by the implementation `_UsageErrorParser` overrode, so
    the bytes stay exactly what they were; with `--json` the shared envelope
    instead, `invalid_usage`, argparse's own sentence as `message`."""
    if not as_json:
        argparse.ArgumentParser.error(error.parser, str(error))
    return _refuse(PreDispatchReason.INVALID_USAGE, error, as_json=True)


class BriefReason(StrEnum):
    """`aco brief`'s own `--json` `reason` vocabulary (`specs/brief.spec.md`,
    issue #396): `composed` the only success. `invalid_item` names no
    reachable refusal today -- brief never refuses for an item nothing
    carries (BRIEF-08's own Never clause) -- so it stays out until a future
    slice gives it a caller."""

    COMPOSED = "composed"
    INVALID_USAGE = "invalid_usage"
    UNAVAILABLE = "unavailable"


def _brief_claim_json(live: _BriefClaim, observed_at: datetime) -> dict[str, object]:
    claim = live.claim
    return {
        "agent": claim.agent,
        "role": claim.role,
        "branch": claim.branch,
        "base": claim.base,
        "scope": list(claim.scope),
        "whole": claim.whole_reason,
        "age": _claim_age_fields(live.opened_at, observed_at)[0],
    }


def _brief_json(composition: _BriefComposition) -> int:
    live = composition.live
    payload: dict[str, object] = {
        "body": composition.body,
        "claim": None if live is None else _brief_claim_json(live, composition.observed_at),
        "tip": composition.tip,
        "touched": list(composition.touched),
    }
    step_rules = composition.step_rules
    if step_rules is not None:
        payload["rules"] = list(step_rules.rules)
        payload["checks"] = list(step_rules.checks)
    _emit_json(True, BriefReason.COMPOSED, **payload)
    return 0


def _brief_config(toplevel: Path) -> board.BriefConfig | None:
    """`.agent-claim/brief.toml`'s own content, read only when the file is
    actually tracked by git (issue #324) -- the same tracked-file
    requirement `board_config` enforces for `board.toml`'s storage pin, so
    an ignored or not-yet-added file never quietly answers for the
    repository. `None` either when it is untracked or when `load_brief_config`
    finds no such file at all."""
    if not checkout.path_is_tracked(board.BRIEF_CONFIG_PATH.as_posix(), directory=toplevel):
        return None
    return board.load_brief_config(toplevel / board.BRIEF_CONFIG_PATH)


def _brief_step_rules_or_refusal(
    step: str | None, context: RunContext
) -> board.BriefStepRules | None:
    """`--step`'s own rules and checks, or `None` when the brief carries no
    `--step` at all -- the one branch that must stay untouched by
    `.agent-claim/brief.toml`'s presence or content (BRIEF-16)."""
    if step is None:
        return None
    config = _brief_config(context.toplevel)
    if config is None:
        raise protocol.ClaimError(f"no {board.BRIEF_CONFIG_PATH} in the repository")
    return config.for_step(board.BriefStep(step))


def _cmd_brief(parsed: argparse.Namespace, context: RunContext) -> int:
    """Compose one item's own reads into the one dispatch brief a lane step's
    body otherwise gets assembled from by hand (AGENTS.md "the next brief
    names the body, the lane tip ... and the commands"): the item's body from
    the forge, its live claim from the store, the claim branch's current tip,
    and the files the lane itself changes since its merge base with trunk.
    Never a new data source, and never a write. Every refusal on this path --
    BRIEF-07, BRIEF-09's PIN-04/PIN-05, BRIEF-15, BRIEF-18's lane-tip read and
    trunk diff, BRIEF-20's trunk read, and the item read itself, which is a
    forge call like any other (issue #432) -- reports through `_refuse` under
    this command's own vocabulary."""
    as_json = parsed.json
    try:
        return _brief_report(parsed, context)
    except RepoMeaninglessUnderStateRefError as error:
        return _refuse(BriefReason.INVALID_USAGE, error, as_json=as_json)
    except protocol.ClaimError as error:
        return _refuse(BriefReason.UNAVAILABLE, error, as_json=as_json)


def _brief_report(parsed: argparse.Namespace, context: RunContext) -> int:
    """`brief`'s own composition and printing, every refusal raised by name
    for `_cmd_brief` to report."""
    step_rules = _brief_step_rules_or_refusal(parsed.step, context)
    item = int(parsed.item)
    client = context.forge
    item_body = client.item_reference(item).body or ""
    live = _brief_live_claim(context.toplevel, context.observation, item)
    if live is None:
        tip: str | None = None
        touched: tuple[str, ...] = ()
    else:
        tip = _lane_tip(live.claim.branch)
        touched = (
            checkout.lane_changed_paths(tip, trunk=context.trunk_ref, directory=context.toplevel)
            if tip is not None
            else ()
        )
    observed_at = datetime.now(UTC)
    composition = _BriefComposition(item_body, live, observed_at, tip, touched, step_rules)
    if parsed.json:
        return _brief_json(composition)
    _print_brief(composition)
    return 0


def _cmd_status(parsed: argparse.Namespace, context: RunContext) -> int:
    """`status` reads live claims from the store directly (issue #176),
    dispatched straight from `main` -- it never needs `_dispatch`'s ledger
    resolution (a cut-over repository may have no ledger issue left at all).
    It is forge-free (issue #245): `--repo` is meaningless here and unused.
    Every `protocol.ClaimError` `_status_read` can raise -- a rewritten or
    malformed state ref -- reports through `_refuse` as `unavailable` (issue
    #406): `status` names no `invalid_usage` of its own (see `StatusReason`).
    """
    try:
        return _status_read(parsed, context)
    except protocol.ClaimError as error:
        return _refuse(StatusReason.UNAVAILABLE, error, as_json=parsed.json)


def _status_read(parsed: argparse.Namespace, context: RunContext) -> int:
    worktree = context.toplevel
    state = context.observation
    claims = tuple(state.claims.values())
    if parsed.path is not None:
        # `--path` prints no age, so it never reads a claim's ancestry: a
        # lineage break in one unrelated claim must not stop this answer
        # (README "status --path").
        if parsed.json:
            return _status_path_json(claims, parsed.path)
        _status_path(claims, parsed.path, context.config.storage)
        return 0
    ages = _claim_ages(worktree, state)
    issue = _optional_issue_number(parsed.issue)
    now = datetime.now(UTC)
    if parsed.json:
        return _status_json(claims, issue, ages, state.tip, now=now)
    return _status(claims, issue, ages, context.config.storage, now=now)


@dataclass(frozen=True)
class _ObservedBoard:
    """`_observed_board`'s own result: the projected board, plus every live
    claim it was built from (issue #348) -- `next`'s own `parallel_set`
    needs each claim's scope for its occupied-paths accounting, and this is
    the one fetch that already read them, never a second store observation
    for the same run."""

    board: board.Board
    live_claims: tuple[protocol.ScopedClaim, ...]


@dataclass(frozen=True)
class _StoreAndIssues:
    """`_store_observation_and_issues`'s own result: the context's
    observation of the claim state and the open-issue list, read
    concurrently (issue #440) rather than one after the other -- neither
    reads the other's result. Under `github` both are round trips a board
    build pays; under `state-ref` the forge already holds the observation
    and the item files before the threads start (issue #477), so neither
    thread pays a round trip."""

    observed: protocol.ClaimState
    issues: tuple[board.Issue, ...]


def _store_observation_and_issues(context: RunContext) -> _StoreAndIssues:
    """The claim-state fetch (`ls-remote` + `fetch`) and the open-issue list
    overlapped on separate threads (issue #440): a board build that needs
    both -- every one that has not already been handed a pre-fetched
    `issues` tuple -- used to pay their wait times back to back. The forge
    is resolved first, on this thread, so its refusals keep their order and
    the context's facts are read once, before either thread asks for them;
    the state-ref board already reads the context's one observation there,
    which the observing thread then finds held (issue #477)."""
    client = context.forge
    with ThreadPoolExecutor(max_workers=2) as pool:
        observation = pool.submit(lambda: context.observation)
        issues = pool.submit(client.list_open_board_issues)
        return _StoreAndIssues(observation.result(), issues.result())


def _observed_board(
    context: RunContext,
    *,
    issues: tuple[board.Issue, ...] | None = None,
) -> _ObservedBoard:
    """`board`/`rulings`/`next` share this: the store's live claims, projected
    onto forge board data (issue #176 -- claims no longer come from the
    ledger; the forge is still the board's own data source)."""
    if issues is None:
        fetched = _store_observation_and_issues(context)
        observed, issues = fetched.observed, fetched.issues
    else:
        # A caller that already fetched `issues` itself (`rulings`) has
        # nothing left to overlap the state fetch with.
        observed = context.observation
    live_claims = tuple(observed.claims.values())
    projected = _board(
        context,
        live_claims,
        issues=issues,
        history=_claim_history(context.toplevel, observed),
    )
    return _ObservedBoard(projected, live_claims)


def _lane_claimants(observed: protocol.ClaimState) -> dict[int, board_html.LaneClaimant]:
    """Every live claim's agent, role, and branch, keyed by the issue it
    holds -- the one field (`branch`) `board.BoardItem.active_claim` never
    carries, since `board.py` joins it into a display string instead."""
    return {
        claim.identity.issue: board_html.LaneClaimant(claim.agent, claim.role, claim.branch)
        for claim in observed.claims.values()
        if isinstance(claim.identity, protocol.IssueIdentity)
    }


def _board_page(context: RunContext) -> board_html.BoardPage:
    """The one board build both `board --html` (issue #276) and `board
    --serve` (issue #280) render -- every `gh`/local-git read `board`
    already performs, including `_board`'s own `checkout.trunk_landings`
    read, which now also serves the Landungen section's rows directly
    through `projected.landings` (issue #371). `--html` calls this fresh
    every time; `--serve` (issue #440) holds its own result between `GET`s
    instead, rebuilding only on a ruling or an explicit reload."""
    fetched = _store_observation_and_issues(context)
    projected = _board(
        context,
        tuple(fetched.observed.claims.values()),
        issues=fetched.issues,
        history=_claim_history(context.toplevel, fetched.observed),
    )
    bodies = {issue.number: issue.body for issue in fetched.issues}
    toplevel = context.toplevel
    config = context.config
    sources = board_html.BoardSources(
        bodies=bodies,
        claimants=_lane_claimants(fetched.observed),
        state_tip="" if fetched.observed.tip is None else str(fetched.observed.tip),
        checkout=toplevel,
        storage=config.storage,
    )
    return board_html.build_page(projected, sources)


def _board_html_page(
    context: RunContext, *, served: board_html.ServedRuleForm | None = None
) -> str:
    """`board --html`'s own static rendering (issue #276): `_board_page`,
    rendered fresh every call so a written page never shows stale state."""
    return board_html.render(_board_page(context), served=served)


def _cmd_board_html(parsed: argparse.Namespace, context: RunContext) -> None:
    """`board --html` (issue #276): writes `_board_html_page`'s static
    rendering to `PATH`, or to stdout when `PATH` is omitted."""
    rendered = _board_html_page(context)
    if parsed.html:
        Path(parsed.html).write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


class BoardReason(StrEnum):
    """`aco board`'s own `--json` `reason` vocabulary (issue #412,
    `specs/board.spec.md`): `projected` the only success -- `--html` and
    `--serve` never reach it, since neither ever sets `--json`.
    `invalid_usage` covers `--new-token` without `--serve` (BOARD-39), no
    `--json`/`--html`/`--serve` given at all (BOARD-44), and `--repo` under
    `storage = state-ref` (PIN-04); every other refusal -- an unsupported
    forge host (BOARD-02), a state-ref checkout with no resolvable default
    branch (PIN-05) -- falls to `unavailable`, matching `ask`/`rule`/
    `brief`'s own catch-all."""

    PROJECTED = "projected"
    INVALID_USAGE = "invalid_usage"
    UNAVAILABLE = "unavailable"


class _BoardNewTokenUsageError(protocol.ClaimError):
    """`--new-token` without `--serve` (BOARD-39): named so `--json` can
    choose `invalid_usage` over the broad `unavailable` catch-all every
    other `board` refusal falls to."""


class _BoardNoModeUsageError(protocol.ClaimError):
    """Neither `--json` nor `--html` given, and `--serve` already diverted
    before `_cmd_board` ever runs (BOARD-44): the retired text table (issue
    #420, #390 Befund 13) leaves `board` with no default mode of its own
    any more, so a bare `aco board` must name one instead of guessing."""


BOARD_NO_MODE_MESSAGE = "aco board requires --json, --html, or --serve"


def _cmd_board(parsed: argparse.Namespace, context: RunContext) -> int:
    as_json = parsed.json
    try:
        if parsed.new_token:
            # `--new-token` only ever mints through `--serve`'s own writer
            # session (issue #388): reaching here means `--serve` was not
            # given, so minting here would be a silent no-op the caller
            # cannot observe rather than a refusal by name.
            raise _BoardNewTokenUsageError("--new-token requires --serve")
        if parsed.html is not None:
            _cmd_board_html(parsed, context)
            return 0
        if not as_json:
            raise _BoardNoModeUsageError(BOARD_NO_MODE_MESSAGE)
        projected = _observed_board(context).board
    except (_BoardNewTokenUsageError, _BoardNoModeUsageError) as error:
        return _refuse(BoardReason.INVALID_USAGE, error, as_json=as_json)
    except RepoMeaninglessUnderStateRefError as error:
        return _refuse(BoardReason.INVALID_USAGE, error, as_json=as_json)
    except protocol.ClaimError as error:
        return _refuse(BoardReason.UNAVAILABLE, error, as_json=as_json)
    _emit_json(True, BoardReason.PROJECTED, **board.board_payload(projected))
    return 0


class RulingsReason(StrEnum):
    """`aco rulings`' own `--json` `reason` vocabulary (issue #412,
    `specs/rulings.spec.md`): `listed` the only success. `invalid_usage`
    covers `--repo` under `storage = state-ref` (PIN-04); every other
    refusal -- an unsupported forge host (BOARD-02), a state-ref checkout
    with no resolvable default branch (PIN-05) -- falls to `unavailable`,
    matching `ask`/`rule`/`brief`'s own catch-all."""

    LISTED = "listed"
    INVALID_USAGE = "invalid_usage"
    UNAVAILABLE = "unavailable"


def _cmd_rulings(parsed: argparse.Namespace, context: RunContext) -> int:
    as_json = parsed.json
    try:
        issues = context.forge.list_open_board_issues()
        projected = _observed_board(context, issues=issues).board
        bodies = {issue.number: issue.body for issue in issues}
        storage = context.config.storage
    except RepoMeaninglessUnderStateRefError as error:
        return _refuse(RulingsReason.INVALID_USAGE, error, as_json=as_json)
    except protocol.ClaimError as error:
        return _refuse(RulingsReason.UNAVAILABLE, error, as_json=as_json)
    _rulings(projected, bodies, as_json=as_json, storage=storage)
    return 0


def _next_action_container_number(action: board.NextAction | None) -> int | None:
    """The container `action` targets, when it targets one -- excluded from
    `SKIPPED` below since a container is always non-actionable itself."""
    if action is None or isinstance(action, board.WorkItemAction):
        return None
    return action.container.number


def _cmd_next(parsed: argparse.Namespace, context: RunContext) -> int:
    as_json = parsed.json
    try:
        observed = _observed_board(context)
        storage = context.config.storage
        claims_in_place = _claims_in_place(context)
    except RepoMeaninglessUnderStateRefError as error:
        return _refuse(NextReason.INVALID_USAGE, error, as_json=as_json)
    except protocol.ClaimError as error:
        return _refuse(NextReason.UNAVAILABLE, error, as_json=as_json)
    projected = observed.board
    action = board.next_action(projected)
    close = board.zero_cost_closes(projected)
    waiting = board.waiting_on_operator(projected)
    already_named = {_next_action_container_number(action), *close, *waiting}
    skipped = tuple(item for item in _unworkable(projected) if item.number not in already_named)
    report = _NextReport(
        action=action,
        skipped=skipped,
        recovery=projected.recovery,
        parallel=board.parallel_set(projected, observed.live_claims, action),
        close=close,
        waiting=waiting,
        claims_in_place=claims_in_place,
    )
    if as_json:
        _next_json(report, storage)
    else:
        _next(report, storage)
    return 0 if action is not None else 3


class _RescopeInvalidUsageError(protocol.ClaimError):
    """A rescope `--add`/`--drop` value itself is malformed or contradictory
    -- relative, outside the checkout, a comma-bearing entry matching no
    versioned file, or a combination `protocol._combined_scope` refuses
    (issue #406) -- named so `--json` can choose `invalid_usage` over the
    broad `unavailable` every checkout- or store-level refusal falls to."""


class _RescopePreconditionError(protocol.ClaimError):
    """The rescope cannot proceed given this claim's own current state: no
    live claim on the target identity/branch, a different agent than the
    one holding it, or a scope too wide for the width gate without
    `--whole` (issue #406) -- named so `--json` can choose
    `precondition_failed`."""


def _rescope_write(parsed: argparse.Namespace, run_context: RunContext) -> int:
    path_checkout = _rescope_checkout(parsed)
    checkout_context = run_context.for_directory(path_checkout.toplevel, is_toplevel=True)
    requested = _rescope_command(parsed, path_checkout, checkout_context)
    worktree = checkout_context.toplevel
    observed = checkout_context.observation
    _require_state_ref(observed)
    try:
        selected = _selected_store_claim(
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
            f"(holder={protocol._claimant_text(selected.agent, selected.role)!r}, "
            f"this session={protocol._claimant_text(requested.agent, selected.role)!r})"
        )
    versioned = checkout.versioned_paths(directory=worktree)
    try:
        _reject_ungrounded_comma_scope(requested.add, versioned, flag="--add")
        combined = protocol._combined_scope(selected.scope, requested.add, requested.drop)
    except protocol.ClaimError as error:
        raise _RescopeInvalidUsageError(str(error)) from error
    try:
        _reject_wide_scope(
            combined,
            versioned,
            requested.whole_reason or selected.whole_reason,
            directories=checkout._scope_directories(
                combined,
                directory=checkout_context.directory,
                toplevel=lambda: checkout_context.toplevel,
            ),
        )
    except protocol.ClaimError as error:
        raise _RescopePreconditionError(str(error)) from error
    intent = protocol.RescopeIntent(
        claim_id=selected.claim_id,
        agent=requested.agent,
        role=selected.role,
        scope=combined,
        operation_id=uuid.uuid4().hex,
        whole_reason=requested.whole_reason,
    )
    new_state = checkout_context.transition(
        _transition_subject("rescope", selected.identity, selected.branch), intent
    )
    rescoped = new_state.claims[protocol.claim_key(selected.identity, selected.branch)]
    if parsed.json:
        _rescope_json(rescoped)
        return 0
    print(
        f"RESCOPED {_claim_subject(rescoped, checkout_context.config.storage)}: {rescoped.claim_id}"
    )
    return 0


def _cmd_rescope(parsed: argparse.Namespace, context: RunContext) -> int:
    """`rescope`'s own `--json` refusals (issue #406, `RescopeReason`):
    `_rescope_write`'s two typed exceptions choose `invalid_usage`/
    `precondition_failed`; every other `protocol.ClaimError` -- an
    unresolved checkout, a missing state ref, a corrupted claim record --
    falls to `unavailable`, matching `ask`/`rule`/`brief`'s own catch-all."""
    as_json = parsed.json
    try:
        return _rescope_write(parsed, context)
    except _RescopeInvalidUsageError as error:
        return _refuse(RescopeReason.INVALID_USAGE, error, as_json=as_json)
    except _RescopePreconditionError as error:
        return _refuse(RescopeReason.PRECONDITION_FAILED, error, as_json=as_json)
    except protocol.ClaimError as error:
        return _refuse(RescopeReason.UNAVAILABLE, error, as_json=as_json)


LANE_CLAIM_SCOPE_REQUIRED = "lane claim requires --scope; a lane names no item to derive it from"
CLAIM_SCOPE_MISSING = "item names no scope; pass --scope"
CLAIM_SCOPE_MISMATCH = "claim scope differs from the item's scope; correct the item first"


class _ClaimTargetInvalidError(protocol.ClaimError):
    """The item a claim's scope is being *derived* from is unusable --
    missing, or a pull request -- discovered while reading its body, before
    any slice-rule check ever runs (issue #406). Named so `--json` can
    choose `target_invalid` over the broad `unavailable` every other claim
    refusal falls to."""


class _ClaimBodyInvalidError(protocol.ClaimError):
    """The item a claim's scope is being *derived* from has a body that
    cannot supply or confirm one -- a malformed `agent-claim` block (issue
    #310 finding 43: named *before* "item names no scope", reusing the same
    block-defect reader `body --check` uses), no `scope` field at all, or
    one differing from an explicit `--scope` (issue #406). Named so
    `--json` can choose `body_invalid`."""


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
    `_item_body_or_refuse`'s own `ClaimUnavailableError` -- missing or a
    pull request, CLM-24's own two conditions -- becomes
    `_ClaimTargetInvalidError` by name (issue #406); a forge outage or
    malformed response (`forge.ForgeError`, raised by `client.item_reference`
    itself) is a different failure and passes through unwrapped to
    `_cmd_claim`'s `unavailable` catch-all (CLM-27)."""
    issue = open_by_number.get(number)
    if issue is not None:
        return issue.body
    try:
        return _item_body_or_refuse(client, number, command="claim", storage=storage)
    except protocol.ClaimUnavailableError as error:
        raise _ClaimTargetInvalidError(str(error)) from error


def _item_scope(
    client: forge.ForgeReader,
    open_by_number: Mapping[int, board.Issue],
    number: int,
    *,
    storage: body.Storage,
) -> tuple[str, ...] | None:
    """The item's own top-level `scope = [...]` (issue #337). A malformed
    block refuses `_ClaimBodyInvalidError` here (issue #310 finding 43) --
    before this function's caller ever gets to name the less specific "item
    names no scope" -- reading `read_state`/`contract.defects` the same way
    `body --check` and `_located_block_or_refuse` already do, the one
    block-defect reader every by-name body refusal in this file shares.
    `_item_whole`'s own read stays tolerant of a malformed body instead: its
    caller is only ever an optional width-gate fallback, never a hard
    requirement the way a claim's own scope is."""
    raw_body = _item_target_body(client, open_by_number, number, storage)
    parsed = body.parse_body(raw_body, storage=storage)
    if parsed.read_state is body.BodyReadState.MALFORMED:
        defect = parsed.contract.defects[0]
        raise _ClaimBodyInvalidError(
            f"{board.item_label(number, storage)} {body.body_defect_text(defect)}"
        )
    return parsed.scope


def _item_whole(
    client: forge.ForgeReader,
    open_by_number: Mapping[int, board.Issue],
    number: int,
    *,
    storage: body.Storage,
) -> str | None:
    """The item's own top-level `whole = "<reason>"` (issue #399), read the
    same way `_item_scope` reads `scope` -- except a malformed body is never
    a hard refusal here (see `_item_scope`'s own docstring): `parse_body`
    simply carries no `whole` for one, exactly as it did before issue #406,
    so the width gate's own refusal still fires instead of an unrelated
    defect message."""
    raw_body = _item_target_body(client, open_by_number, number, storage)
    return body.parse_body(raw_body, storage=storage).whole


def _whole_from_item_body(
    context: RunContext,
    identity: protocol.ClaimIdentity,
    *,
    open_by_number: Mapping[int, board.Issue] | None,
) -> Callable[[], str | None] | None:
    """The wide-scope gate's own fallback for `claim`/`start` (issue #399):
    `None` for a lane claim, which names no item to read one from; otherwise
    a lazy resolver `_reject_wide_scope` calls only once the scope actually
    trips the gate and neither call names `--whole` -- so a narrow scope, or
    an explicit `--whole`, never costs this read. Reuses `open_by_number`
    when the caller already fetched it (a derived scope); otherwise reads
    the one target item alone, never the whole board, so a replay (CLM-15)
    or a live-claim resume (START-06) never reads past its own item (issue
    #447). Resolves the repository's own storage pin
    itself, since a trip's resolver runs before `_cmd_claim`'s own branch
    has necessarily done so."""
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
    context: RunContext,
    storage: body.Storage,
) -> tuple[protocol.ClaimRequest, dict[int, board.Issue] | None]:
    """`requested` with its scope filled in for an issue-mode claim that
    omitted `--scope` (issue #337), paired with the open-board listing that
    filling it cost -- `None` when it cost nothing, so `_cmd_claim` never
    fetches that board a second time for its own slice-rule checks.

    A live claim already on this identity and branch is a replayed,
    interrupted request even when the retry drops `--scope`: its own stored
    scope is taken outright, with no forge call and no body read at all, so
    a retry never refuses merely because the body changed, or lost its
    scope, since the original claim was opened -- and `_matching_store_claim`
    is guaranteed `None` for an identity with no live claim at all, so this
    is the only place that needs to look. Otherwise the item's own body
    `scope = [...]` is the source of truth, fetched from the same open board
    `_cmd_claim` needs anyway for its slice-rule checks, so the caller reuses
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
        raise _ClaimBodyInvalidError(CLAIM_SCOPE_MISSING)
    return replace(requested, scope=item_scope), open_by_number


def _reject_scope_mismatch(
    open_by_number: Mapping[int, board.Issue],
    target_issue: int,
    scope: tuple[str, ...],
    storage: body.Storage,
) -> None:
    """Refuses an explicit `--scope` whose canonical set differs from the
    target item's own body scope (issue #337) -- reusing the open board
    `_cmd_claim` fetches once, whether for a derived scope or for its
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
        raise _ClaimBodyInvalidError(CLAIM_SCOPE_MISMATCH)


def _scope_versioning(
    scope: tuple[str, ...],
    whole_reason: str | None,
    *,
    context: RunContext,
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
    _reject_ungrounded_comma_scope(scope, versioned, flag="--scope")
    directories = checkout._scope_directories(
        scope, directory=context.directory, toplevel=lambda: context.toplevel, revision=revision
    )
    n, total, share, effective_whole_reason = _reject_wide_scope(
        scope, versioned, whole_reason, directories=directories, whole_from_body=whole_from_body
    )
    return ScopeVersioning(n, total, share), effective_whole_reason


@dataclass(frozen=True)
class _ClaimTargetContext:
    """The board-environment scalars `_claim_target_checks` needs beyond
    the claim request itself, bundled once instead of PLR0913's
    five-scalar ceiling (issue #337): `storage` and `worktree` `_cmd_claim`
    already resolved before this point, and `open_by_number` -- the open
    board, when deriving the scope already fetched it, `None` otherwise --
    so a fresh issue claim that did not derive its scope still fetches the
    board exactly once, inside `_claim_target_checks` itself."""

    storage: body.Storage
    worktree: Path
    open_by_number: dict[int, board.Issue] | None


def _claim_target_checks(
    context: RunContext,
    requested: protocol.ClaimRequest,
    observed: protocol.ClaimState,
    target: _ClaimTargetContext,
) -> tuple[tuple[SliceCheck, ...], int | None, protocol.ActiveClaim | None]:
    """`_cmd_claim`'s slice-rule checks against its own target issue,
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
    projected = _board(
        context,
        tuple(observed.claims.values()),
        issues=tuple(open_by_number.values()),
        history=_ClaimHistory(ages=_claim_ages(target.worktree, observed)),
    )
    checks = _slice_rule_checks(
        BoardReferenceLookup(client, client.repository.path, open_by_number),
        target_issue,
        projected,
        requested.out_of_order_reason,
        target.storage,
    )
    return checks, target_issue, replayed


def _cmd_claim(parsed: argparse.Namespace, context: RunContext) -> int:
    try:
        return _claim_write(parsed, context)
    except protocol.ClaimError as error:
        return _refuse_claim_error(error, context, as_json=parsed.json)


def _refuse_claim_error(error: protocol.ClaimError, context: RunContext, *, as_json: bool) -> int:
    """`claim`'s own `--json` refusals (issue #406, `ClaimReason`):
    `_claim_write`'s typed exceptions choose `target_invalid`/
    `body_invalid`/`claim_conflict`; `RepoMeaninglessUnderStateRefError`
    chooses `invalid_usage`; every other `protocol.ClaimError` -- a checkout
    precondition, scope grammar, an unsafe branch or claim id -- falls to
    `unavailable`, matching `ask`/`rule`/`brief`'s own catch-all. Only a
    conflict reads `context`'s board configuration, to name its item."""
    match error:
        case RepoMeaninglessUnderStateRefError():
            reason = ClaimReason.INVALID_USAGE
        case _ClaimTargetInvalidError():
            reason = ClaimReason.TARGET_INVALID
        case _ClaimBodyInvalidError():
            reason = ClaimReason.BODY_INVALID
        case protocol.ClaimConflictError():
            # `apply()`'s own conflict, met on the observed state or by the
            # claim's write (CLM-25), whether or not its push was sent; a
            # transport, git, lineage, or retry-exhaustion failure is another
            # `ClaimError` and falls to `unavailable` below (CLM-27).
            return _refuse(
                ClaimReason.CLAIM_CONFLICT,
                _named_refusal(error, context.config.storage),
                as_json=as_json,
            )
        case _:
            reason = ClaimReason.UNAVAILABLE
    return _refuse(reason, error, as_json=as_json)


def _named_refusal(error: protocol.ClaimError, storage: body.Storage) -> protocol.ClaimError:
    """`error` as a report prints it: naming its item the way `storage`'s
    board does (issue #471)."""
    return protocol.ClaimError(error.named(board.item_labeller(storage)))


def _claim_write(
    parsed: argparse.Namespace, context: RunContext, *, worktree: Path | None = None
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
        plan = _checked_claim(requested, context)
        writer = context
    else:
        requested = _request(parsed, default_branch=default_branch, directory=worktree)
        plan = _checked_claim(requested, context, revision=requested.base)
        writer = context.for_lane_worktree(worktree)
    if plan.refused:
        _refuse_claim(parsed.json, plan.target_issue, plan.checks)
        return 2
    _print_claim_checks(plan, as_json=parsed.json)
    claimed, live = _committed_claim(plan, writer)
    return _report_claim(plan, claimed, live, as_json=parsed.json)


@dataclass(frozen=True)
class _ClaimPlan:
    """What `claim`'s check phase accepted, and everything its commit phase
    writes and reports (issue #479): `start` runs the two phases around the
    worktree it builds, so a refusal comes before the build."""

    requested: protocol.ClaimRequest
    observed: protocol.ClaimState
    storage: body.Storage
    versioning: ScopeVersioning
    checks: tuple[SliceCheck, ...]
    target_issue: int | None
    replayed: protocol.ActiveClaim | None
    intent: protocol.ClaimIntent

    @property
    def refused(self) -> bool:
        return any(check.level == "error" for check in self.checks)


def _checked_claim(
    requested: protocol.ClaimRequest, context: RunContext, *, revision: str | None = None
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
        raise protocol.ClaimUnavailableError(LANE_CLAIM_SCOPE_REQUIRED)
    open_by_number: dict[int, board.Issue] | None = None
    if requested.scope or not isinstance(requested.identity, protocol.IssueIdentity):
        # Scope already final (given, or a lane's own required value): the
        # local shape checks run first, exactly as before this field
        # existed, so a comma or width refusal never touches the store or
        # resolves the repository toplevel -- unless the scope is actually
        # wide and names no `--whole` of its own, in which case the width
        # gate's own lazy fallback (issue #399) resolves the toplevel itself
        # to read the item's body, never this branch.
        versioning, effective_whole = _scope_versioning(
            requested.scope,
            requested.whole_reason,
            context=context,
            whole_from_body=_whole_from_item_body(context, requested.identity, open_by_number=None),
            revision=revision,
        )
        requested = replace(requested, whole_reason=effective_whole)
        observed = context.observation
        _require_state_ref(observed)
        storage = context.config.storage
    else:
        # `--scope` was omitted in issue mode: the item's own scope has to
        # come from the store, and usually the forge, before it can even be
        # shape-checked -- both observed exactly once, here, so an omitted-
        # scope claim never fetches either a second time (issue #337).
        observed = context.observation
        _require_state_ref(observed)
        storage = context.config.storage
        requested, open_by_number = _resolved_claim_request(requested, observed, context, storage)
        versioning, effective_whole = _scope_versioning(
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


def _print_claim_checks(plan: _ClaimPlan, *, as_json: bool) -> None:
    for check in plan.checks:
        print(check.render(), file=sys.stderr if as_json else sys.stdout)


def _committed_claim(
    plan: _ClaimPlan, writer: RunContext
) -> tuple[protocol.ActiveClaim, tuple[protocol.ActiveClaim, ...]]:
    """`claim`'s commit phase: the plan's one ledger write, made from the
    checkout `writer` stands for, or the replayed claim it already names,
    with every live claim after it."""
    if plan.replayed is not None:
        return plan.replayed, tuple(plan.observed.claims.values())
    requested = plan.requested
    new_state = writer.transition(
        _transition_subject("claim", requested.identity, requested.branch), plan.intent
    )
    claimed = new_state.claims[protocol.claim_key(requested.identity, requested.branch)]
    return claimed, tuple(new_state.claims.values())


def _report_claim(
    plan: _ClaimPlan,
    claimed: protocol.ActiveClaim,
    live: tuple[protocol.ActiveClaim, ...],
    *,
    as_json: bool,
) -> int:
    touches = protocol.conflicting_claims(live, claimed)
    if as_json:
        return _claim_json(claimed, versioning=plan.versioning, touches=touches, checks=plan.checks)
    print(f"CLAIMED {_claim_subject(claimed, plan.storage)}: {claimed.claim_id}")
    print(
        _claim_cost_line(
            plan.versioning.versioned_files,
            plan.versioning.versioned_files_total,
            plan.requested.scope,
            touches,
            plan.storage,
        )
    )
    return 0


def _start_worktree_path(main_checkout: Path, *, number: int, slug: str) -> Path:
    return main_checkout.parent / f"{main_checkout.name}-worktrees" / f"issue-{number}-{slug}"


@dataclass(frozen=True)
class _StartTarget:
    """The worktree `start` claims in and the branch it stands on."""

    path: Path
    branch: str


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
    `claim_key` alone is an issue-only key for an `IssueIdentity` (it never
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
        and live.role == DEFAULT_CLAIM_ROLE
        and live.branch == branch
    ):
        return live
    return None


def _print_start_target(target: _StartTarget) -> None:
    print(f"worktree: {target.path}")
    print(f"branch: {target.branch}")


def _remove_refused_start_worktree(target: _StartTarget) -> None:
    """Undo the build of a `start` whose claim was refused after the check
    phase passed (issue #479), saying so: a refusal must leave no worktree
    or branch behind. When git will not remove the worktree, that is said
    too, beside the refusal rather than in its place."""
    try:
        outcome = checkout.remove_linked_worktree(target.path, branch=target.branch)
    except protocol.ClaimError as error:
        print(
            f"worktree {target.path} and branch '{target.branch}' this start created "
            f"kept: git failure: {error}",
            file=sys.stderr,
        )
        return
    if outcome.branch.removed:
        print(
            f"removed worktree {target.path} and branch '{target.branch}' this start created",
            file=sys.stderr,
        )
        return
    print(
        f"removed worktree {target.path} this start created; "
        f"branch '{target.branch}' kept: {outcome.branch.reason}",
        file=sys.stderr,
    )


RESUME_SCOPE_MISMATCH = "live claim scope differs; release it first"


def _refuse_resume_scope_mismatch(live: protocol.ActiveClaim, parsed: argparse.Namespace) -> None:
    if parsed.scope is not None and protocol.valid_scope(parsed.scope) != live.scope:
        raise protocol.ClaimUnavailableError(RESUME_SCOPE_MISMATCH)


def _checked_start_resume(
    live: protocol.ActiveClaim,
    parsed: argparse.Namespace,
    *,
    context: RunContext,
    revision: str | None = None,
) -> ScopeVersioning:
    """Every refusal `start`'s resume of `live` can meet, run before anything
    is printed or built (issue #479): an explicit `--scope` that disagrees
    with the live claim's own stored scope (review/gate finding: resume must
    not silently ignore it), then the stored scope's own shape and width
    against `context`'s checkout -- or `revision`, the fetched trunk a gone
    worktree is rebuilt from. `--whole` is never required here even when the
    live scope is wide, since the stored claim's own `whole_reason` already
    justified it once."""
    _refuse_resume_scope_mismatch(live, parsed)
    versioning, _effective_whole = _scope_versioning(
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
    versioning: ScopeVersioning,
) -> None:
    """`start`'s own resume path (issue #322 review finding 1): prints the
    same `CLAIMED ...`/cost-line grammar a fresh claim prints, for the live
    claim `_cmd_start` already found in the store -- the same
    `observed.claims` lookup `status`/`release` use -- rather than minting a
    second, fresh id for an item that already has one."""
    print(f"CLAIMED {_claim_subject(live, storage)}: {live.claim_id}")
    touches = protocol.conflicting_claims(tuple(observed.claims.values()), live)
    print(
        _claim_cost_line(
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


def _cmd_start(parsed: argparse.Namespace, context: RunContext) -> int:
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
        checkout.main_checkout_root(toplevel=caller.toplevel), is_toplevel=True
    )
    branch, slug = _start_branch_and_slug(parsed, main_context)
    observed = main_context.observation
    _require_state_ref(observed)
    live = observed.claims.get(protocol.claim_key(_resolved_identity(parsed.item, branch), branch))
    if live is not None and _runs_in_lane_worktree(caller, live.branch):
        own_lane = _StartTarget(caller.toplevel, live.branch)
        return _claim_in_start_worktree(parsed, main_context, own_lane, observed, live)
    target = _StartTarget(
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
        role=DEFAULT_CLAIM_ROLE,
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
    target: _StartTarget,
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
        _print_start_target(target)
        _print_start_resume(resumed, observed, context.config.storage, versioning)
        return 0
    _print_start_target(target)
    # The main checkout's one observation, the same snapshot the lookup above
    # read (CAS-53): nothing fetched since.
    try:
        return _claim_write(_start_claim_arguments(parsed), context, worktree=target.path)
    except protocol.UncertainWriteError as error:
        named = _named_refusal(error, context.config.storage)
        return _report_uncertain_start_claim(named, target)
    except protocol.ClaimError as error:
        return _refuse_claim_error(error, context, as_json=False)


def _rebuild_and_resume(
    parsed: argparse.Namespace,
    context: RunContext,
    target: _StartTarget,
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
    trunk = checkout.trunk_commit(trunk_ref, directory=context.toplevel)
    versioning = _checked_start_resume(resumed, parsed, context=context, revision=trunk)
    checked = _claim_request(
        _start_claim_arguments(parsed, base=trunk, branch=target.branch, claim_id=resumed.claim_id)
    )
    checkout.create_linked_worktree(
        target.path, branch=target.branch, trunk=trunk_ref, directory=context.toplevel
    )
    _print_start_target(target)
    try:
        _validate_built_worktree(checked, target, context)
    except protocol.ClaimError as error:
        return _refuse_built_start(error, target)
    _print_start_resume(resumed, observed, context.config.storage, versioning)
    return 0


def _check_build_and_claim(
    parsed: argparse.Namespace, context: RunContext, target: _StartTarget
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
    trunk = checkout.trunk_commit(trunk_ref, directory=context.toplevel)
    # The main checkout observed afresh, never the observation the
    # item-existence read already holds: the fetch above may take a while,
    # and the claim must read the item as it stands once the fetch is done,
    # not the snapshot that read took (issue #322 review finding 2, CAS-55).
    check_context = context.observed_afresh()
    requested = _claim_request(_start_claim_arguments(parsed, base=trunk, branch=target.branch))
    storage = check_context.config.storage
    try:
        plan = _checked_claim(requested, check_context, revision=trunk)
    except protocol.ClaimConflictError as error:
        raise _named_refusal(error, storage) from error
    if plan.refused:
        _refuse_claim(False, plan.target_issue, plan.checks)
        return 2
    checkout.create_linked_worktree(
        target.path, branch=target.branch, trunk=trunk_ref, directory=context.toplevel
    )
    _print_start_target(target)
    _print_claim_checks(plan, as_json=False)
    try:
        _validate_built_worktree(requested, target, check_context)
        # Written from the built worktree, so the lane's own lineage stamp
        # and fetch anchor start at its claim (CAS-09).
        claimed, claims = _committed_claim(plan, check_context.for_lane_worktree(target.path))
    except protocol.UncertainWriteError as error:
        return _report_uncertain_start_claim(_named_refusal(error, storage), target)
    except protocol.ClaimError as error:
        return _refuse_built_start(_named_refusal(error, storage), target)
    return _report_claim(plan, claimed, claims, as_json=False)


def _validate_built_worktree(
    request: protocol.ClaimRequest, target: _StartTarget, context: RunContext
) -> None:
    """`claim`'s own checkout preconditions against the worktree `start`
    built, judged against the main checkout `context`'s recorded default
    branch. Its base is the trunk `start` checked, so a base mismatch means
    another fetch moved the trunk between the checks and the build, and
    rerunning `start` checks the trunk as it stands now (START-26)."""
    try:
        checkout._validate_checkout(
            request,
            default_branch=lambda: context.recorded_default_branch,
            directory=target.path,
        )
    except checkout.CheckoutBaseMismatchError as error:
        raise protocol.ClaimUnavailableError(
            "the trunk moved after start checked it; run start again"
        ) from error


def _report_uncertain_start_claim(error: protocol.ClaimError, target: _StartTarget) -> int:
    """A claim write whose outcome the store cannot tell after its push was
    sent: the claim may have landed, so the worktree it may name stays, and
    the next `start` resumes whichever it finds (START-25)."""
    status = _refuse(ClaimReason.UNAVAILABLE, error, as_json=False)
    print(
        f"the claim's push was sent, its outcome unknown; worktree {target.path} and "
        f"branch '{target.branch}' kept; run start again to resume it",
        file=sys.stderr,
    )
    return status


def _refuse_built_start(error: protocol.ClaimError, target: _StartTarget) -> int:
    try:
        return _refuse(ClaimReason.UNAVAILABLE, error, as_json=False)
    finally:
        # A refusal that cannot be written (a closed stderr) must still
        # leave nothing behind (START-18).
        _remove_refused_start_worktree(target)


@dataclass(frozen=True)
class _ResolvedRelease:
    """The live claim `release` targets and the role it releases under
    (issue #359): factored out of `_cmd_release` so `_cmd_release_landed`
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
    selected = _selected_store_claim(observed, identity, release_branch, parsed.claim_id, storage)
    if (
        parsed.branch is not None
        and parsed.claim_id is not None
        and selected.branch != parsed.branch
    ):
        raise protocol.ClaimUnavailableError(
            f"--branch {parsed.branch!r} and --claim-id {parsed.claim_id!r} disagree: the "
            f"claim's own branch is {selected.branch!r}; drop --branch or pass its own value"
        )
    role = parsed.role
    if not parsed.coordinator_override:
        if role is None:
            role = selected.role
        if (parsed.agent, role) != (selected.agent, selected.role):
            raise protocol.ClaimUnavailableError(
                "only the original claimant may release; use an explicit coordinator override "
                f"(holder={protocol._claimant_text(selected.agent, selected.role)!r}, "
                f"this session={protocol._claimant_text(parsed.agent, role)!r})"
            )
    resolved_role = role if role is not None else selected.role
    return _ResolvedRelease(selected, resolved_role)


def _cmd_release(
    parsed: argparse.Namespace, context: RunContext, release_branch: str | None
) -> int:
    """`release`'s own `--json` envelope (issue #425): every refusal this
    function or `_cmd_release_landed` raises -- REL-01's own usage errors
    excepted, which the parser reports as `invalid_usage` before either ever
    runs (issue #432) -- is caught here and reported as
    `precondition_failed`, rather than escaping the envelope entirely."""
    as_json = parsed.json
    try:
        return _release_transition(parsed, context, release_branch)
    except protocol.ClaimError as error:
        return _refuse(ReleaseReason.PRECONDITION_FAILED, error, as_json=as_json)


def _release_transition(
    parsed: argparse.Namespace, context: RunContext, release_branch: str | None
) -> int:
    issue = _optional_issue_number(parsed.issue)
    identity = _resolved_identity(issue, release_branch or "")
    storage = context.config.storage
    if parsed.merged is not None and storage is body.Storage.STATE_REF:
        return _cmd_release_landed(parsed, context, identity, storage, release_branch)
    merged = None if parsed.merged is None else _github_pull_request_number(parsed.merged)
    outcome = _release_outcome(merged, parsed.abandoned)
    observed = context.observation
    _require_state_ref(observed)
    resolved = _resolve_release_claimant(parsed, observed, identity, release_branch, storage)
    client: github.GitHubForge | None = None
    if isinstance(outcome, protocol.MergedRelease):
        # Authorization above gates every forge read and write here (issue
        # #359 R1): an unauthorized or mismatched-claim `--merged` release
        # never reaches the forge at all, so it can neither verify, comment
        # on, nor close a pull request's issue. `storage` is already proven
        # `github` here (the `state-ref` branch above returned), so this
        # cast is honest, not a suppression: `_build_forge` builds
        # exactly a `github.GitHubForge` for every other storage pin.
        client = cast(github.GitHubForge, context.forge)
        pending_close = _verify_merged_release(context, client, identity, outcome)
        if pending_close is not None:
            # Runs before the release transition below (issue #359 R1): a
            # close failure here -- a transient forge error, most often --
            # leaves this function raising before the release transition
            # ever runs, so the claim it would have released stays exactly
            # as live as it was, and `main`'s own `ClaimError` handler
            # prints the failure as the one sentence the operator sees.
            client.close_landed_item(pending_close.issue, pull_request=pending_close.pull_request)
    intent = protocol.ReleaseIntent(
        claim_id=resolved.selected.claim_id,
        agent=parsed.agent,
        role=resolved.resolved_role,
        outcome=outcome,
        operation_id=uuid.uuid4().hex,
        coordinator_override=parsed.coordinator_override,
    )
    new_state = context.transition(
        _transition_subject("release", resolved.selected.identity, resolved.selected.branch),
        intent,
    )
    # The run already fetched the canonical remote and asked the forge's
    # default branch to verify the merge, so this ref costs no read.
    landing = (
        None
        if client is None
        else _landing_report(
            context, identity, new_state, storage, context.fetched_default_branch_ref()
        )
    )
    worktree_cleanup = (
        _cleanup_landed_worktree(
            parsed, resolved.selected.branch, context, context.fetched_default_branch_ref
        )
        if isinstance(outcome, protocol.MergedRelease)
        else None
    )
    _print_release_result(
        ReleaseReport(
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
    return 0


WORKTREE_KEPT_FLAG_REASON = "--keep-worktree was given"
WORKTREE_KEPT_RAN_FROM_INSIDE_REASON = "release ran from inside it"
WORKTREE_KEPT_NO_WORKTREE_REASON = "no linked worktree found"


def worktree_cleanup_outcome_text(outcome: checkout.WorktreeCleanupOutcome) -> str:
    """The tail `release` prints after `worktree: ` in text, and the exact
    `--json` value of its `worktree` field (issue #322 review/gate finding
    4): one owner, so the two shapes can never drift apart. A branch-deletion
    failure after the worktree is already gone names both halves -- never a
    bare `kept`, which would hide that the worktree itself is gone."""
    if outcome.worktree.removed and outcome.branch.removed:
        return "removed"
    if outcome.worktree.removed:
        return f"removed; branch kept -- {outcome.branch.reason}"
    return f"kept -- {outcome.worktree.reason}"


def _cleanup_landed_worktree(
    parsed: argparse.Namespace,
    branch: str,
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
    so a failure to resolve it reads as `kept` too. The remote branch stays
    the forge merge's own business either way."""
    if parsed.keep_worktree:
        return checkout.worktree_cleanup_kept(WORKTREE_KEPT_FLAG_REASON)
    try:
        toplevel = context.toplevel
        others = tuple(path for path in store.list_worktrees(toplevel) if path != toplevel)
        if checkout.current_branch(directory=toplevel) == branch:
            return checkout.worktree_cleanup_kept(WORKTREE_KEPT_RAN_FROM_INSIDE_REASON)
        matching = checkout.worktree_on_branch(others, branch)
        if matching is None:
            return checkout.worktree_cleanup_kept(WORKTREE_KEPT_NO_WORKTREE_REASON)
        return checkout.cleanup_landed_worktree(
            matching, branch, trunk=fetched_trunk_ref(), directory=toplevel
        )
    except protocol.ClaimError as error:
        return checkout.worktree_cleanup_kept(f"git failure: {error}")


def _newest_landed_commit(
    landings: tuple[checkout.TrunkLanding, ...], number: int, storage: body.Storage
) -> str:
    """The most recent first-parent trunk commit whose own trailer names
    `number` (issue #359, LAND-47): the empty-value form of `release
    --merged`'s own sha argument under `storage = "state-ref"`."""
    for landing in reversed(landings):
        classification = landing.classification
        if (
            isinstance(classification, board.TrunkWorkItemClassification)
            and number in classification.numbers
        ):
            return landing.sha
    raise protocol.ClaimUnavailableError(
        f"no trunk commit carries a Work-Item: trailer naming {board.item_label(number, storage)}"
    )


SHA_NOT_ON_TRUNK_DEFECT = "is not on the first-parent trunk"


def _trunk_landing_defect(
    landings: tuple[checkout.TrunkLanding, ...], number: int, sha: str, storage: body.Storage
) -> str | None:
    """Why `sha` does not authorize closing work item `number` from the
    walked first-parent trunk (issue #359 LAND-52), or `None` when it does:
    `sha` must sit on that trunk and carry a `Work-Item:` trailer naming
    exactly `number`. One reader (`checkout.trunk_landings`) and one grammar
    (`board.trunk_commit_classification`) for both `release --merged
    <sha|empty>` under `storage = state-ref` and, since issue #397 (Befund
    41), `release --merged <pr>` under `storage = github`'s own merge-commit
    verification -- neither trusts a different notion of "landed"."""
    landing = next((entry for entry in landings if entry.sha == sha), None)
    if landing is None:
        return SHA_NOT_ON_TRUNK_DEFECT
    classification = landing.classification
    if classification is None:
        return "carries no `Work-Item:` trailer"
    if isinstance(classification, board.ClassificationDefect):
        return classification.message
    if (
        not isinstance(classification, board.TrunkWorkItemClassification)
        or number not in classification.numbers
    ):
        return f"does not name work item {board.item_label(number, storage)}"
    return None


def _landed_commit_by_sha(
    landings: tuple[checkout.TrunkLanding, ...], number: int, sha: str, storage: body.Storage
) -> str:
    """`sha`, verified as `number`'s own landing commit (issue #359,
    LAND-52): refused by name before anything is written otherwise."""
    defect = _trunk_landing_defect(landings, number, sha, storage)
    if defect is not None:
        raise protocol.ClaimUnavailableError(f"{sha} {defect}")
    return sha


def _verify_merge_commit_authority(
    landings: tuple[checkout.TrunkLanding, ...],
    pull_request: int,
    number: int,
    sha: str,
    storage: body.Storage,
) -> None:
    """Refuse a github `release --merged <pr>` whose merge commit does not
    authorize closing `number` (issue #397, Befund 41): `number` is the
    claim's own issue, resolved before `_verify_merged_release` ever calls
    this -- never the pull request's own mutable body -- and this merge
    commit trailer is the actual authority, the same `_trunk_landing_defect`
    reads for `storage = state-ref`."""
    defect = _trunk_landing_defect(landings, number, sha, storage)
    if defect is not None:
        raise protocol.ClaimUnavailableError(
            f"merge commit {sha} of pull request #{pull_request} {defect}"
        )


LAND_GITHUB_ONLY_REFUSAL = (
    "aco land is a github command; storage = state-ref has no pull requests to land"
)
LAND_SELF_PACKAGE_NAME = "agent-coordination"
LAND_REINSTALL_LINE = "reinstall: uv tool install --force --from . agent-coordination"
# A check name is GitHub's own external vocabulary -- a workflow job or a
# third-party status context's own title -- with no length limit this tool
# controls, unlike the internal path names `protocol.named_with_overflow_count`
# was written for (issue #405 review/gate finding): each name is truncated
# here before that same three-then-`and N more` grammar ever sees it, and the
# assembled sentence is capped again as a final backstop -- short enough that
# `main`'s own `CLI_ERROR_PREFIX` still fits under the same 200-character
# printed-line budget (issue #405 round-4 finding) -- so several long check
# names can never print a refusal line past 200 characters.
LAND_CHECK_NAME_LENGTH_LIMIT = 40
LAND_REFUSAL_LINE_LENGTH_LIMIT = 200
LAND_REFUSAL_SENTENCE_LENGTH_LIMIT = LAND_REFUSAL_LINE_LENGTH_LIMIT - len(CLI_ERROR_PREFIX)


def _land_not_open_refusal(number: int) -> str:
    return f"pull request #{number} is not open; it cannot be landed"


def _land_not_mergeable_refusal(number: int, state: str) -> str:
    return f"pull request #{number} is not mergeable ({state})"


def _land_truncated_check_name(name: str) -> str:
    if len(name) <= LAND_CHECK_NAME_LENGTH_LIMIT:
        return name
    return name[: LAND_CHECK_NAME_LENGTH_LIMIT - 1] + "…"


def _land_bounded_refusal(sentence: str) -> str:
    """`sentence` as one bounded refusal line: a pull request's own text
    (a check name, a head's board configuration key) may carry a newline or
    a terminal escape, so it is shown as `next` shows foreign text (`\\n`,
    `\\x1b`) before the length cap applies (issues #505, #538)."""
    printable = board.terminal_text(sentence)
    if len(printable) <= LAND_REFUSAL_SENTENCE_LENGTH_LIMIT:
        return printable
    return printable[: LAND_REFUSAL_SENTENCE_LENGTH_LIMIT - 1] + "…"


def _land_pending_check_names(checks: tuple[forge.CheckRun, ...]) -> tuple[str, ...]:
    return tuple(
        _land_truncated_check_name(check.name) for check in checks if check.conclusion is None
    )


def _land_failed_check(checks: tuple[forge.CheckRun, ...]) -> forge.CheckRun | None:
    return next(
        (
            check
            for check in checks
            if check.conclusion is not None and check.conclusion != forge.CHECK_CONCLUSION_SUCCESS
        ),
        None,
    )


def _land_checks_refusal(number: int, checks: tuple[forge.CheckRun, ...]) -> str | None:
    """Why `aco land`'s preflight refuses on `checks` alone, or `None` when
    every one of them proves green (issue #405): no checks at all, a check
    still running, then a check that finished without success -- in that
    order, since a still-running check is not yet a failure."""
    if not checks:
        return f"pull request #{number} exposes no CI checks; cannot verify green CI"
    pending = _land_pending_check_names(checks)
    if pending:
        # Capped the same way `next`'s own `parallel:` line is (issue #348,
        # `protocol.named_with_overflow_count`): a pull request with many
        # still-running checks must never print a refusal past 200
        # characters (issue #405 review/gate finding) -- each name already
        # truncated by `_land_pending_check_names` before this grammar sees
        # it, `_land_bounded_refusal` a final backstop on the whole line.
        return _land_bounded_refusal(
            f"pull request #{number} has checks still running: "
            f"{protocol.named_with_overflow_count(pending)}; wait for every check to succeed"
        )
    failed = _land_failed_check(checks)
    if failed is not None:
        conclusion = failed.conclusion
        assert conclusion is not None  # `_land_failed_check` only returns a completed check.
        return _land_bounded_refusal(
            f"pull request #{number} has non-successful checks: "
            f"{_land_truncated_check_name(failed.name)} ({conclusion}); "
            "land only after every check succeeds"
        )
    return None


def _refuse_land_readiness(readiness: forge.LandingReadiness) -> None:
    if not readiness.open:
        raise protocol.ClaimUnavailableError(_land_not_open_refusal(readiness.number))
    if readiness.mergeable_state != forge.MERGEABLE_STATE_CLEAN:
        raise protocol.ClaimUnavailableError(
            _land_not_mergeable_refusal(readiness.number, readiness.mergeable_state)
        )
    checks_refusal = _land_checks_refusal(readiness.number, readiness.checks)
    if checks_refusal is not None:
        raise protocol.ClaimUnavailableError(checks_refusal)


def _land_governing_settings(config: board.BoardConfig) -> dict[str, object]:
    """The settings that decide where `aco land`'s own release half writes
    (issue #505): a pull request may change any other setting and still
    land, but never one of these under its own landing."""
    return {"storage": config.storage, "canonical_remote": config.canonical_remote}


def _land_head_config_refusal(
    number: int, head_text: str | None, landed: board.BoardConfig
) -> str | None:
    """Why the pull request head's board configuration `head_text` refuses
    `aco land` against the default branch's own `landed` one (LANDCMD-22..24),
    or `None` when it keeps every governing setting. The head's copy is
    only checked, never obeyed: `landed` alone governs this run (issue
    #505 R1)."""
    path = board.CONFIG_PATH
    cannot_release = "aco land cannot release its claim across that change"
    if head_text is None:
        return f"pull request #{number} removes {path}; {cannot_release}"
    try:
        head = board.parse_config(head_text, path)
    except protocol.ClaimError as error:
        return _land_bounded_refusal(f"pull request #{number} carries an invalid {path}: {error}")
    landed_settings = _land_governing_settings(landed)
    changed = next(
        (
            setting
            for setting, value in _land_governing_settings(head).items()
            if value != landed_settings[setting]
        ),
        None,
    )
    if changed is None:
        return None
    return f"pull request #{number} changes {changed} in {path}; {cannot_release}"


def _refuse_land_head_config(
    client: github.GitHubForge, readiness: forge.LandingReadiness, landed: board.BoardConfig
) -> None:
    head_text = client.file_at_commit(board.CONFIG_PATH, readiness.head_sha)
    refusal = _land_head_config_refusal(readiness.number, head_text, landed)
    if refusal is not None:
        raise protocol.ClaimUnavailableError(refusal)


def _land_preflight(
    client: github.GitHubForge,
    claims_provider: Callable[[], protocol.ClaimState],
    context: _LandingCheckContext,
    number: int,
    parsed: argparse.Namespace,
) -> tuple[forge.Landing, board.Classification, forge.LandingReadiness]:
    """Every read-only precondition `aco land` proves before its first write
    (issue #405): a green, open, mergeable pull request classifying exactly
    one open item, or declaring itself issue-less -- reusing `check <pr>`'s
    own classification/claim/parent/closing rules (`_structural_classification`/
    `_classification_defect`) rather than a second copy of them.

    In order: readiness (no local git read at all), the pull request's own
    shape, its head's board configuration against this checkout's own
    (LANDCMD-22..24, issue #505), then the named item's live open state --
    LANDCMD-08 before claim validation (issue #405 review/gate finding) --
    then `claims_provider`,
    the one step that reads `refs/aco/state` locally, so a pull request this
    preflight would refuse on GitHub's own answers alone never pays for that
    read at all, and finally this session's own authorization against the
    exact claim just proven to exist (`_resolve_release_claimant`, `release`'s
    own claimant/coordinator-override check, issue #405 review finding): a
    claim held by another agent or role refuses here, before the merge,
    rather than only once the delegated `release --merged` step runs after
    it. `_cmd_land`'s own entry already validated `--coordinator-override`'s
    role (issue #405 round-4 finding 1) -- before this preflight, and before
    the fresh/rerun split that skips it entirely on a rerun -- so this
    function itself never repeats that check.
    """
    readiness = client.landing_readiness(number)
    _refuse_land_readiness(readiness)
    detail = client.landing(number)
    structural = _structural_classification(context, detail)
    if isinstance(structural, board.ClassificationDefect):
        raise protocol.ClaimUnavailableError(f"pull request #{number} {structural.message}")
    _refuse_land_head_config(client, readiness, context.run.config)
    if isinstance(structural, board.WorkItemClassification):
        reference = _fetch_issue_reference(client, structural.item.number)
        if reference.state is not forge.ItemState.OPEN:
            raise protocol.ClaimUnavailableError(
                f"work item #{structural.item.number} is not open; it cannot be landed"
            )
    observed = claims_provider()
    defect = _classification_defect(context, tuple(observed.claims.values()), detail, structural)
    if defect is not None:
        raise protocol.ClaimUnavailableError(f"pull request #{number} {defect.message}")
    identity: protocol.ClaimIdentity = (
        protocol.IssueIdentity(structural.item.number)
        if isinstance(structural, board.WorkItemClassification)
        else protocol.LaneIdentity()
    )
    _resolve_release_claimant(
        argparse.Namespace(
            agent=parsed.agent,
            role=parsed.role,
            coordinator_override=parsed.coordinator_override,
            branch=None,
            claim_id=None,
        ),
        observed,
        identity,
        detail.source_branch,
        context.storage,
    )
    return detail, structural, readiness


def _land_trunk_trailer(classification: board.Classification) -> str:
    """`classification` rendered as a trunk commit's own trailer grammar
    (`board.trunk_commit_classification`'s counterpart), never the pull
    request body's qualified `owner/repo#n` form `WorkItemClassification.__str__`
    prints: a git trailer is always local to the repository whose history
    it lands on (issue #405; see `board.parse_item_reference`'s own
    docstring)."""
    if isinstance(classification, board.WorkItemClassification):
        return f"Work-Item: #{classification.item.number}"
    return f"No-Item: {classification.kind.value}"


def _land_merge_body(body: str, classification: board.Classification) -> str:
    """The merge commit message `aco land` composes itself (issue #405,
    Befund 42 on #310): the pull request's own body with its classification
    line removed, then that classification, in the trunk's own trailer
    grammar, as the message's own final paragraph -- so the trailer a later
    trunk walk reads through git's own trailer parsing is never wherever
    the pull request body happened to put it, always the message's own last
    block."""
    without_classification = board.CLASSIFICATION_LINE_PATTERN.sub("", body).strip()
    trailer = _land_trunk_trailer(classification)
    if not without_classification:
        return f"{trailer}\n"
    return f"{without_classification}\n\n{trailer}\n"


def _land_merge(
    client: github.GitHubForge,
    detail: forge.Landing,
    readiness: forge.LandingReadiness,
    classification: board.Classification,
) -> str:
    title = f"Merge pull request #{detail.number}"
    body = _land_merge_body(detail.body, classification)
    try:
        return client.merge_landing(
            detail.number, head_sha=readiness.head_sha, title=title, body=body
        )
    except forge.ForgeMergeConflictError as error:
        raise protocol.ClaimUnavailableError(
            f"pull request #{detail.number} changed while it was checked; re-run land"
        ) from error


def _land_step(number: int, sha: str, step: str, action: Callable[[], None]) -> None:
    """Every step `aco land` runs once its merge already landed (issue
    #405): a failure here never means "not merged" -- the merge already
    happened -- so it reports the one ruled recovery line instead of the
    generic refusal an earlier precondition would print. A rerun starts
    `_cmd_land` over from the top, finds the pull request already merged,
    and resumes here without a second merge."""
    try:
        action()
    except protocol.ClaimError as error:
        raise protocol.ClaimUnavailableError(
            f"MERGED pull request #{number} as {sha}; follow-up incomplete: {step}; "
            f"re-run aco land {number}"
        ) from error


def _land_release_routing(
    classification: board.Classification | None, merge_sha: str, context: RunContext
) -> int | None:
    """The issue `aco land`'s own delegated `release --merged` call routes
    to (issue #405 point 4): a fresh merge reuses the classification this
    same run's own preflight already verified, never a re-read of the pull
    request's own mutable body. `classification` is `None` only for a rerun
    against a pull request `_cmd_land` found already merged -- preflight
    never ran this time -- so this reads the merge commit's own trailer
    instead, exactly as `_verify_merged_release` does (issue #397, Befund
    41): the same authority a lane release itself checks, never the pull
    request's `body`, which stays mutable long after the merge. Runs inside
    the `release` step's own `_land_step` (issue #405 review finding), so a
    defect here -- an edited body's classification long gone from the merge
    commit's own history -- prints the ruled `MERGED ... follow-up
    incomplete: release; re-run aco land <n>` line, never a bare refusal."""
    if classification is not None:
        if isinstance(classification, board.WorkItemClassification):
            return classification.item.number
        return None
    landings = checkout.trunk_landings(
        context.fetched_default_branch_ref(), TRUNK_LANDING_DEPTH, directory=context.toplevel
    )
    landing = next((entry for entry in landings if entry.sha == merge_sha), None)
    trunk_classification = None if landing is None else landing.classification
    if isinstance(trunk_classification, board.TrunkWorkItemClassification):
        return trunk_classification.numbers[0]
    if isinstance(trunk_classification, board.NoItemClassification):
        return None
    raise protocol.ClaimUnavailableError(
        f"merge commit {merge_sha} carries no `Work-Item:` or `No-Item:` trailer"
    )


def _land_release(
    parsed: argparse.Namespace, context: RunContext, issue: int | None, branch: str
) -> None:
    """`aco land`'s own delegated call into the existing `release --merged`
    path (issue #405): never a second copy of its close/release/report/
    cleanup -- `--branch` selects the lane by name without requiring this
    checkout to be on it (`_release_branch_for`'s own documented case: "the
    release may run from the coordinator's primary checkout"), exactly
    where `land` runs from. Calls `_release_transition` directly, never
    `_cmd_release` (issue #425): `land` needs a raised `ClaimError` here so
    `_land_step` can convert it into its own ruled `MERGED ... follow-up
    incomplete: release` line, not `_cmd_release`'s own caught-and-printed
    `--json` refusal, which `land` never uses (`json=False` above) and
    would otherwise print its sentence a second time. The release reads
    through a fresh `context` (issue #457 proof 6): the fast-forward just
    wrote the landed trunk into this very checkout, so the configuration
    and forge read before it no longer answer for it."""
    release_parsed = argparse.Namespace(
        issue=issue,
        agent=parsed.agent,
        role=parsed.role,
        branch=branch,
        merged=str(parsed.pull_request),
        abandoned=None,
        claim_id=None,
        coordinator_override=parsed.coordinator_override,
        keep_worktree=parsed.keep_worktree,
        # `land` has no `--json` mode of its own (specs/land.spec.md): the
        # delegated release path always reports in text, exactly as every
        # other line `land` itself prints does.
        json=False,
        repo=parsed.repo,
    )
    _release_transition(release_parsed, context.fresh(), branch)


def _land_is_own_repository(toplevel: Path) -> bool:
    """Whether `toplevel` is this very package's own repository (issue
    #405): `land`'s own reinstall reminder applies only there -- landing in
    any other repository this tool coordinates leaves nothing to
    reinstall."""
    try:
        with (toplevel / "pyproject.toml").open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    project = data.get("project")
    return isinstance(project, dict) and project.get("name") == LAND_SELF_PACKAGE_NAME


def _cmd_land(parsed: argparse.Namespace, context: RunContext) -> None:
    """`aco land <pr>` (issue #405): preflight every ruled precondition
    read-only, merge with a pinned head sha and a commit message this tool
    composes itself, delete the merged branch, fast-forward this checkout's
    own default branch, then run the existing `release --merged` path
    unchanged. A pull request `landing` already finds merged -- a resumed
    run after an earlier step failed -- skips preflight and the merge
    itself entirely: `merge_landing` never runs twice for the same pull
    request. `--coordinator-override`'s own role is validated here, at
    entry, before that fresh/rerun split and before any read (issue #405
    round-4 finding 1): a rerun skips `_land_preflight` entirely, so a
    check placed only there left a rerun free to delete the branch and
    fast-forward -- and `_cmd_release` free to close the item -- on a bare
    `--coordinator-override` with no coordinator role behind it."""
    toplevel = context.toplevel
    config = context.config
    if config.storage is not body.Storage.GITHUB:
        raise protocol.ClaimUnavailableError(LAND_GITHUB_ONLY_REFUSAL)
    if parsed.coordinator_override:
        protocol._require_coordinator_override(parsed.role)
    client = cast(github.GitHubForge, context.forge)
    number = parsed.pull_request
    repository = client.repository.path
    detail = client.landing(number)
    classification: board.Classification | None
    if detail.merged:
        assert detail.merge_commit is not None  # `merged` is true; github.py guarantees this.
        merge_sha = detail.merge_commit
        checkout.refuse_unclean_default_branch_checkout(context.default_branch, directory=toplevel)
        # A rerun: this run's own preflight never ran, so it never verified a
        # classification -- `_land_release_routing` reads the merge commit's
        # own trailer instead (issue #405 point 4).
        classification = None
    else:

        def claims_provider() -> protocol.ClaimState:
            # `store.peek_state`, never `RunContext.observation`'s
            # `fetch_state` (issue #405 review/gate finding): a pull
            # request this preflight goes on to refuse must anchor no ref
            # and stamp no lineage -- the same read-only requirement
            # `_reset_observation` already carries for `reset`.
            observed = store.peek_state(worktree=context.toplevel, remote=context.canonical_remote)
            _require_state_ref(observed)
            return observed

        check_context = _LandingCheckContext(context, repository, config.storage)
        detail, classification, readiness = _land_preflight(
            client, claims_provider, check_context, number, parsed
        )
        checkout.refuse_unclean_default_branch_checkout(context.default_branch, directory=toplevel)
        merge_sha = _land_merge(client, detail, readiness, classification)
    _land_step(
        number, merge_sha, "delete-branch", lambda: client.delete_branch(detail.source_branch)
    )
    _land_step(
        number,
        merge_sha,
        "fast-forward",
        lambda: checkout.fast_forward_default_branch(
            context.fetched_default_branch_ref(), directory=toplevel
        ),
    )
    _land_step(
        number,
        merge_sha,
        "release",
        lambda: _land_release(
            parsed,
            context,
            _land_release_routing(classification, merge_sha, context),
            detail.source_branch,
        ),
    )
    if _land_is_own_repository(toplevel):
        print(LAND_REINSTALL_LINE)


def _landed_commit(
    landings: tuple[checkout.TrunkLanding, ...],
    number: int,
    requested: str,
    storage: body.Storage,
) -> str:
    """The trunk-landing commit `release --merged <sha|empty>` closes
    `number` from, under `storage = "state-ref"` (issue #359, LAND-47/
    LAND-52): `requested` empty picks the newest such commit
    (`_newest_landed_commit`); a given sha is verified instead
    (`_landed_commit_by_sha`)."""
    if requested:
        return _landed_commit_by_sha(landings, number, requested, storage)
    return _newest_landed_commit(landings, number, storage)


def _cmd_release_landed(
    parsed: argparse.Namespace,
    context: RunContext,
    identity: protocol.ClaimIdentity,
    storage: body.Storage,
    release_branch: str | None,
) -> int:
    """`release --merged <sha|empty>` under `storage = "state-ref"` (issue
    #359, LAND-47/LAND-52): the trunk walk (`checkout.trunk_landings`,
    issue #304) names, or verifies, the landing commit; one
    `protocol.LandingIntent` then closes the item and releases the claim in
    one commit, one CAS -- `_cmd_release`'s own `ReleaseIntent` path never
    runs for this storage pin's `--merged`.
    """
    if not isinstance(identity, protocol.IssueIdentity):
        raise protocol.ClaimUnavailableError(
            "--merged under storage = state-ref requires an issue number; "
            "an issue-less lane has no item to close"
        )
    trunk_ref = context.trunk_ref
    landings = checkout.trunk_landings(trunk_ref, TRUNK_LANDING_DEPTH, directory=context.toplevel)
    commit = _landed_commit(
        landings, identity.issue, cast(str, parsed.merged), context.config.storage
    )
    # `storage` is already proven `state-ref` by `_release_transition`, so
    # this cast is honest: `_build_forge` builds exactly a `StateRefBoard`.
    client = cast(state_board.StateRefBoard, context.forge)
    if client.item_reference(identity.issue).state is forge.ItemState.MISSING:
        raise protocol.ClaimUnavailableError(
            _missing_item_refusal(identity.issue, client, context.config.storage)
        )
    write = client.prepare_landing(identity.issue)
    worktree = context.toplevel
    observed = context.observation
    _require_state_ref(observed)
    resolved = _resolve_release_claimant(parsed, observed, identity, release_branch, storage)
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
        _transition_subject("release", resolved.selected.identity, resolved.selected.branch),
        intent,
    )
    client.mark_landed(write, new_oid)
    landing = _landing_report(context, identity, new_state, storage, trunk_ref)
    worktree_cleanup = _cleanup_landed_worktree(
        parsed, resolved.selected.branch, context, context.fetched_trunk_ref
    )
    _print_release_result(
        ReleaseReport(
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
    return 0


_BoardRead = TypeVar("_BoardRead")


def _board_read_after_write(read: Callable[[], _BoardRead]) -> _BoardRead | str:
    """`read`'s result, or the one hint that replaces it, for a board read a
    command runs once its own write already committed (`release --merged`,
    issue #256; `item close`, issue #541): a forge hiccup (LAND-38) or a
    board read refusal (LAND-65) can only downgrade that report, never undo
    or fail the write, so this is the one owner of which refusals become a
    hint and what it says."""
    try:
        return read()
    except forge.ForgeError as error:
        return _board_read_hint(error, "once the forge is reachable")
    except protocol.MalformedStateTreeError as error:
        return _board_read_hint(error, "once it is repaired")


def _board_read_hint(refusal: protocol.ClaimError, until: str) -> str:
    """The one wording every post-write command shares (issue #546): it
    names the write rather than a landing, since `item close` lands
    nothing, and advises the one board read that runs as printed."""
    return (
        f"hint: could not read the board to report what this write freed ({refusal}); "
        f"run `aco board --json` {until}"
    )


def _print_board_read_hint(hint: str, *, as_json: bool) -> None:
    """Print `_board_read_after_write`'s hint on the channel both of its
    callers share: stderr under `--json`, so stdout stays one JSON document,
    and stdout in text mode, in place of the lines the read would have
    printed."""
    print(hint, file=sys.stderr if as_json else sys.stdout)


def _landing_report(
    context: RunContext,
    identity: protocol.ClaimIdentity,
    new_state: store.ClaimState,
    storage: body.Storage,
    trunk_ref: str,
) -> ReleaseLanding | str:
    """The landing `_cmd_release` prints once its release transition
    already committed (issue #256), or the hint that replaces it: a forge
    hiccup here, or a state-ref store the board read refuses (issue #447),
    can only ever downgrade the report, never undo or fail that release."""
    landed = (
        board.IssueReference(context.forge.repository.path, identity.issue)
        if isinstance(identity, protocol.IssueIdentity)
        else None
    )
    return _board_read_after_write(
        lambda: _release_landing(context, new_state, landed, storage, trunk_ref)
    )


@dataclass(frozen=True)
class ReleaseReport:
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
    landing: ReleaseLanding | str | None
    storage: body.Storage
    worktree: checkout.WorktreeCleanupOutcome | None


def _print_release_result(report: ReleaseReport, *, as_json: bool) -> None:
    selected, landing = report.selected, report.landing
    if as_json:
        _release_json(report)
        if isinstance(landing, str):
            _print_board_read_hint(landing, as_json=True)
        return
    print(f"RELEASED {_claim_subject(selected, report.storage)}: {selected.claim_id}")
    if isinstance(landing, str):
        _print_board_read_hint(landing, as_json=False)
    elif landing is not None:
        print(_release_freed_line(landing.freed, report.storage))
        print(_release_next_line(landing.next_item, report.storage))
        parent_line = _parent_closable_line(landing.parent_closable, report.storage)
        if parent_line is not None:
            print(parent_line)
    if report.worktree is not None:
        print(f"worktree: {worktree_cleanup_outcome_text(report.worktree)}")


CONTAINER_KINDS = frozenset({body.ItemKind.CONTAINER})
# `item new --parent` also takes a Task: an item becomes a container exactly
# when it gets its first child, so that write retypes it (issue #503).
ITEM_PARENT_KINDS = frozenset({body.ItemKind.CONTAINER, body.ItemKind.TASK})


def _open_container(
    open_issues: Iterable[board.Issue],
    number: int,
    storage: body.Storage,
    accepted_kinds: frozenset[body.ItemKind] = CONTAINER_KINDS,
) -> board.Issue:
    """`number`'s open issue when its type is one of `accepted_kinds` --
    `cut`'s own target and `item new --parent` (issue #444) -- or why it is
    not."""
    target = next((issue for issue in open_issues if issue.number == number), None)
    if target is None:
        raise protocol.ClaimUnavailableError(
            f"{board.item_label(number, storage)} is not an open container"
        )
    if target.unreadable is not None:
        raise protocol.ClaimUnavailableError(body.body_defect_text(target.unreadable))
    if target.kind not in accepted_kinds:
        raise protocol.ClaimUnavailableError(
            f"{board.item_label(number, storage)} is not a container"
        )
    return target


def _cut_target(
    client: forge.ForgeWriter,
    open_issues: Iterable[board.Issue],
    number: int,
    storage: body.Storage,
) -> board.Issue:
    """The open container `cut` targets, or why it refuses before any write."""
    target = _open_container(open_issues, number, storage)
    parent = client.parent_issue(number)
    if parent is not None:
        parent_label = board.relation_label(parent.reference, storage)
        raise protocol.ClaimUnavailableError(
            f"{board.item_label(number, storage)} is itself a child of {parent_label}; "
            "nested containers are not supported"
        )
    return target


@dataclass(frozen=True)
class _SliceRowRemoval:
    """`container`'s rewritten body, the cut row already removed from its
    `agent-claim` block, and `step`, the words naming that write."""

    container: int
    new_body: str
    step: str


def _link_created_child(
    client: forge.ForgeWriter, removal: _SliceRowRemoval, child: int, storage: body.Storage
) -> None:
    """Write `removal.new_body` back to `removal.container`.

    Not atomic with `create_child` -- GitHub has no transaction across the
    two writes. A failure here still leaves the created child behind, so it
    raises the same `forge.ForgePartialChildCreationError` a failed relation
    write inside `create_child` itself would -- one type, so `_cmd_cut`
    renders one recovery message for either, `step` naming what an
    identical re-run finishes.
    """
    try:
        client.update_item_body(removal.container, removal.new_body)
    except protocol.ClaimError as error:
        raise forge.ForgePartialChildCreationError(
            child=child,
            parent=removal.container,
            step=removal.step,
            cause=error,
            storage=storage,
        ) from error


class CutReason(StrEnum):
    """`aco cut`'s own `--json` `reason` vocabulary (issue #425,
    `specs/cut.spec.md`): `cut`/`adopted` mirror the text form's own
    `CUT`/`ADOPTED` verb -- whether `child` was freshly created or an
    already-open child GitHub already recorded (#260). Every refusal
    before any write is `precondition_failed`; a write that created the
    child but failed to finish recording it
    (`forge.ForgePartialChildCreationError`) is `partial_write` instead,
    carrying `written`/`failed` as structured siblings rather than only
    the prose sentence stderr already printed."""

    CUT = "cut"
    ADOPTED = "adopted"
    PRECONDITION_FAILED = "precondition_failed"
    PARTIAL_WRITE = "partial_write"


@dataclass(frozen=True)
class _CutOutcome:
    """What one `cut` did: the container, the row it linked (if any), the
    child it created or adopted, and whether it adopted."""

    container: int
    row_index: int | None
    child: int
    adopted: bool


def _print_cut_result(outcome: _CutOutcome, storage: body.Storage, *, as_json: bool) -> None:
    """Print `cut`'s result: the text form's `CUT`/`ADOPTED` verb becomes
    `--json`'s own `reason` (issue #425) -- `adopted` no longer needs its
    own boolean sibling once the envelope's `reason` already names it."""
    if as_json:
        _emit_json(
            True,
            CutReason.ADOPTED if outcome.adopted else CutReason.CUT,
            container=outcome.container,
            row=outcome.row_index,
            child=outcome.child,
        )
        return
    suffix = "" if outcome.row_index is None else f" row {outcome.row_index}"
    verb = "ADOPTED" if outcome.adopted else "CUT"
    container_label = board.item_label(outcome.container, storage)
    print(f"{verb} {container_label}{suffix} -> {board.item_label(outcome.child, storage)}")


class _PartialWriteError(protocol.ClaimError):
    """Wraps `forge.ForgePartialCreationError` so `cut` and `item new`
    can choose `partial_write`'s own structured `--json` shape (`written`,
    `failed`) without parsing the wrapped error's prose (mirrors `ask`/
    `rule`'s own `_TargetUnavailableError`/`_InvalidTargetError`, issue
    #396); `recovery` is the caller's own way to finish the write."""

    def __init__(self, error: forge.ForgePartialCreationError, *, recovery: str) -> None:
        self.written = error.created
        self.failed = error.step
        super().__init__(f"{error}; {recovery}")


def _parent_line(container: int, storage: body.Storage) -> str:
    """The `Parent: <label>` line `cut` writes as a fresh child's first line
    -- the same wording issue bodies already use for this fact, naming
    `container` as `board.item_label` prints it (issue #467) -- and reads
    back to recognise its own orphan."""
    return f"Parent: {board.item_label(container, storage)}"


def _requested_body_scope(raw: list[str] | None) -> tuple[str, ...] | None:
    """A repeated `--scope` flag's canonical value, or `None` when it was
    never given -- `item new` and `cut` (issue #337) both take an optional
    `--scope`, so this is the one place either turns the raw flag list into
    the same canonical form `protocol.valid_scope` produces for `claim`."""
    return None if raw is None else protocol.valid_scope(raw)


def _requested_whole_reason(raw: str | None) -> str | None:
    """A `--whole` flag's own bounded text (issue #399), or `None` when it
    was never given -- `item new`'s own entry point into the same
    `protocol._outbound_text` bound `claim`'s own `--whole` already enforces
    (`_optional_whole_reason`), so the two never drift on what a legal
    reason looks like."""
    return None if raw is None else protocol._outbound_text(raw, _WHOLE_REASON_LABEL, maximum=512)


def _cut_child_body(
    container: int, storage: body.Storage, scope: tuple[str, ...] | None = None
) -> str:
    """The body `cut` writes for a fresh child: `_parent_line` ahead of
    `body.BLOCK_CHILD_SKELETON`, plus the cut slice's own
    top-level `scope = [...]` (issue #337) when the cut carries one -- the
    linked row's own scope, or a filled `--scope`. A repeat `cut` after a
    partial failure reads the parent line back (`_orphan_names_container`)
    to tell `container`'s own orphan apart from an unrelated open issue that
    merely shares the row's title (#260)."""
    skeleton = f"{_parent_line(container, storage)}\n\n{body.BLOCK_CHILD_SKELETON}"
    if scope is None:
        return skeleton
    return body.body_with_block_fields(skeleton, {"scope": list(scope)})


def _orphan_names_container(raw_body: str, container: int, storage: body.Storage) -> bool:
    """Whether `raw_body`'s first line is the `_parent_line`
    `_cut_child_body` writes -- the one signal that tells `container`'s own
    orphan apart from another open issue, another container's own failed
    cut, or a human-filed issue that happens to share the row's title."""
    return body.first_line(raw_body) == _parent_line(container, storage)


def _adoptable_child(
    client: forge.ForgeWriter,
    container: int,
    title: str,
    config: board.BoardConfig,
    open_issues: Iterable[board.Issue],
) -> board.ChildItem | None:
    """`container`'s already-open child titled exactly `title`, so a repeat
    `cut` after a partial failure (`forge.ForgePartialChildCreationError`)
    adopts the child GitHub already recorded instead of risking a second one
    (#260). Two sources can carry that child: already linked under
    `container` (`list_children`), or an orphan -- one of the caller's own
    `open_issues` snapshot with no recorded parent at all, exactly the shape
    a failed `link_child` POST leaves behind. A title match alone is too
    weak to adopt an orphan: any unrelated open issue anywhere in the
    repository -- including one a human filed -- could share it. An orphan
    is adoptable only when it is also a `TASK` (never the container itself,
    never an idea-labelled item) and its body still names `container` as the
    parent `_cut_child_body` wrote for it; a recovery orphan is always
    exactly that shape, and nothing else can fake it. An untyped orphan
    (`forge.ForgeIssueTypeNotSetError`, issue #444) is never adopted: the
    twin search names it instead. An orphan match is linked under
    `container` right here before it is returned, so the caller's remaining steps treat it exactly
    like an already-linked child; the container's own issue is never created
    twice for it. More than one open match refuses by name rather than guess
    which one the failed cut actually created. A closed linked child with
    that title refuses too -- adoption finishes an interrupted cut, it does
    not reopen a closed one. `None` when nothing matches, so the caller
    falls through to `create_child`.
    """
    linked = [
        child
        for child in client.list_children(container)
        if client.item_reference(child.number).title == title
    ]
    open_linked = [child.number for child in linked if child.state is board.ChildState.OPEN]
    orphans = [
        issue.number
        for issue in open_issues
        if issue.title == title
        and issue.number != container
        and issue.kind is body.ItemKind.TASK
        and not board.has_label(issue.labels, config.idea_label)
        and _orphan_names_container(issue.body, container, config.storage)
        and client.parent_issue(issue.number) is None
    ]
    open_matches = open_linked + orphans
    container_label = board.item_label(container, config.storage)
    if len(open_matches) > 1:
        named = ", ".join(board.item_label(number, config.storage) for number in open_matches)
        raise protocol.ClaimUnavailableError(
            f"{container_label}'s row {title!r} matches more than one open issue ({named}); "
            "adopt the right one by hand and remove the row"
        )
    if open_matches:
        [number] = open_matches
        if number in orphans:
            client.link_child(container, number)
        return board.ChildItem(number, board.ChildState.OPEN)
    closed = next((child for child in linked if child.state is board.ChildState.CLOSED), None)
    if closed is None:
        return None
    raise protocol.ClaimUnavailableError(
        f"{container_label} already has a closed child "
        f"{board.item_label(closed.number, config.storage)} titled {title!r}; "
        "reopen it or remove the row by hand"
    )


# How far back a closed issue still counts for the twin search (issue #444):
# a duplicate usually repeats an item closed weeks, not months, ago.
TWIN_SEARCH_CLOSED_WINDOW = timedelta(days=30)
# Two titles are possible twins when at least this share of their combined
# distinct casefolded words appears in both (issue #444).
TWIN_TITLE_WORD_OVERLAP = 0.6
_TITLE_WORD = re.compile(r"\w+")


def _title_words(title: str) -> frozenset[str]:
    return frozenset(_TITLE_WORD.findall(title.casefold()))


def _title_overlap(title: str, other: str) -> float:
    """The share of both titles' combined distinct words they have in
    common; an identical title overlaps fully even without a single word,
    so a re-run is always named as its own earlier run's twin."""
    if other == title:
        return 1.0
    first, second = _title_words(title), _title_words(other)
    combined = first | second
    return len(first & second) / len(combined) if combined else 0.0


def _numbered_titles(issues: Iterable[board.Issue]) -> tuple[tuple[int, str], ...]:
    return tuple((issue.number, issue.title) for issue in issues)


def _possible_twin(title: str, candidates: Iterable[tuple[int, str]]) -> int | None:
    """The candidate whose title overlaps `title` most, at least
    `TWIN_TITLE_WORD_OVERLAP`, the lower number on a tie; `None` when no
    candidate reaches it."""
    hits = [
        (overlap, number)
        for number, other in candidates
        if (overlap := _title_overlap(title, other)) >= TWIN_TITLE_WORD_OVERLAP
    ]
    return min(hits, key=lambda hit: (-hit[0], hit[1]))[1] if hits else None


def _refuse_possible_twin(
    client: forge.ForgeReader,
    title: str,
    open_titles: Iterable[tuple[int, str]],
    *,
    parent: int | None,
    storage: body.Storage,
) -> None:
    """The one twin search `item new` and `cut` run before they create an
    issue (issue #444): `open_titles` -- every open issue's number and title
    -- and the titles of every issue closed within `TWIN_SEARCH_CLOSED_WINDOW`,
    never the new issue's own `parent`. A hit refuses by number;
    `--not-a-twin` is the caller's way past it."""
    since = datetime.now(UTC) - TWIN_SEARCH_CLOSED_WINDOW
    candidates = [
        *open_titles,
        *((issue.number, issue.title) for issue in client.list_recently_closed_issues(since)),
    ]
    twin = _possible_twin(title, (entry for entry in candidates if entry[0] != parent))
    if twin is not None:
        raise protocol.ClaimUnavailableError(
            f"possible twin {board.item_label(twin, storage)}; pass --not-a-twin"
        )


def _block_slice_entries(data: Mapping[str, object]) -> list[dict[str, object]]:
    value = data.get("slice")
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _slice_row(entry: dict[str, object]) -> body.SliceRow:
    """One `[[slice]]` entry as `cut` sees it. `entry`'s own `scope` (issue
    #337), when it carries one, already passed `protocol.valid_scope` at
    `_located_block_or_refuse`'s own `parse_body` gate -- a body that failed
    that check never reaches here -- so this is the one canonicalizing pass,
    not a second validation of an already-checked value."""
    scope = protocol.valid_scope(entry["scope"]) if "scope" in entry else None
    return body.SliceRow(cast(int, entry["index"]), cast(str, entry["title"]), scope)


def _cut_link(
    label: str, data: Mapping[str, object], row_number: int | None
) -> body.SliceRow | None:
    """Which `[[slice]]` entry `cut` links its fresh child to (#150 §7):
    without `--row`, the first entry when the block carries one; with `--row
    N`, the entry `N` names, refusing by name when the block has no `slice`
    key at all or no such row left (every remaining entry is cuttable: a
    linked entry is removed from `data["slice"]` at the moment it is cut)."""
    entries = _block_slice_entries(data)
    if row_number is None:
        return _slice_row(entries[0]) if entries else None
    if "slice" not in data:
        raise protocol.ClaimUnavailableError(
            f"{label} has no slice table; --row needs one to select a row from"
        )
    match = next((entry for entry in entries if entry["index"] == row_number), None)
    if match is None:
        cuttable = ", ".join(str(entry["index"]) for entry in entries) or "none"
        raise protocol.ClaimUnavailableError(
            f"{label} has no row {row_number}; cuttable rows: {cuttable}"
        )
    return _slice_row(match)


def _require_matching_title(label: str, link: body.SliceRow, title: str) -> None:
    if title != link.title:
        raise protocol.ClaimUnavailableError(
            f"{label}'s slice {link.index} is titled {link.title!r}; --title must match it exactly"
        )


def _located_block_or_refuse(
    number: int, raw_body: str, *, command: str, storage: body.Storage = body.Storage.GITHUB
) -> body.LocatedBlock:
    """`raw_body`'s located `agent-claim` block, or a by-name refusal before
    any write: `cut`, `rule`, and `ask` all need a body `parse_body` reads as
    VALID before they touch it, and share this one gate so the message is
    the same shape for all three. `storage` is forwarded to `parse_body`
    unchanged (issue #283): a state-ref item's own `[record]` table must
    read as a known key, not a malformed one."""
    parsed = body.parse_body(raw_body, storage=storage)
    if parsed.read_state is body.BodyReadState.MALFORMED:
        defect = parsed.contract.defects[0]
        raise protocol.ClaimUnavailableError(
            f"{board.item_label(number, storage)} {body.body_defect_text(defect)}; "
            f"{command} needs a valid agent-claim block"
        )
    return body.locate_agent_claim_block(raw_body)


CUT_ROW_SCOPE_ALREADY_SET = "slice {index} already names a scope; edit the container instead"


def _cut_row_scope(
    link: body.SliceRow | None, requested: tuple[str, ...] | None
) -> tuple[str, ...] | None:
    """The scope `cut`'s fresh child inherits (issue #337): the linked
    row's own scope when it already has one -- `--scope` then refuses by
    name, since the row is the one place to change it -- else `--scope`
    fills the (still empty) row and becomes the child's scope; with no
    linked row at all, `--scope` becomes the child's scope directly."""
    if link is not None and link.scope is not None:
        if requested is not None:
            raise protocol.ClaimUnavailableError(CUT_ROW_SCOPE_ALREADY_SET.format(index=link.index))
        return link.scope
    return requested


CUT_RERUN_RECOVERY = "re-run the same cut -- it adopts the child"
CUT_TYPE_RECOVERY = f"set that type on the forge by hand, then {CUT_RERUN_RECOVERY}"


def _cut_slice(
    client: forge.ForgeWriter,
    target: board.Issue,
    open_issues: Iterable[board.Issue],
    parsed: argparse.Namespace,
    config: board.BoardConfig,
) -> int:
    number = target.number
    storage = config.storage
    label = board.item_label(number, storage)
    located = _located_block_or_refuse(number, target.body, command="cut", storage=storage)
    link = _cut_link(label, located.data, parsed.row)
    if link is not None:
        _require_matching_title(label, link, parsed.title)
    child_scope = _cut_row_scope(link, _requested_body_scope(parsed.scope))
    adopted = _adoptable_child(client, number, parsed.title, config, open_issues)
    if adopted is None and not parsed.not_a_twin:
        _refuse_possible_twin(
            client, parsed.title, _numbered_titles(open_issues), parent=number, storage=storage
        )
    try:
        child = (
            adopted.number
            if adopted is not None
            else client.create_child(
                parent=number,
                title=parsed.title,
                body=_cut_child_body(number, storage, child_scope),
                kind=body.ItemKind.TASK,
            )
        )
        if link is not None:
            remaining = [
                entry
                for entry in _block_slice_entries(located.data)
                if entry["index"] != link.index
            ]
            new_data = {**located.data, "slice": remaining}
            removal = _SliceRowRemoval(
                container=number,
                new_body=body.replace_agent_claim_block(target.body, located, new_data),
                step=f"remove row {link.index} from {label}'s agent-claim block",
            )
            _link_created_child(client, removal, child, storage)
    except forge.ForgeIssueTypeNotSetError as error:
        raise _PartialWriteError(error, recovery=CUT_TYPE_RECOVERY) from error
    except forge.ForgePartialChildCreationError as error:
        raise _PartialWriteError(error, recovery=CUT_RERUN_RECOVERY) from error
    outcome = _CutOutcome(
        container=number,
        row_index=None if link is None else link.index,
        child=child,
        adopted=adopted is not None,
    )
    _print_cut_result(outcome, storage, as_json=parsed.json)
    return 0


def _refuse_partial_write(
    error: _PartialWriteError, reason: CutReason | ItemReason, *, as_json: bool
) -> int:
    print(f"{CLI_ERROR_PREFIX}{error}", file=sys.stderr)
    if as_json:
        _emit_json(
            False,
            reason,
            written=error.written,
            failed=error.failed,
            message=str(error),
        )
    return 2


def _cmd_cut(parsed: argparse.Namespace, context: RunContext) -> int:
    """`cut`'s own `--json` envelope (issue #425): a partial write reports
    through `_refuse_partial_write`'s own structured shape; every other
    refusal past the parser is `precondition_failed`, matching this
    command's single generic refusal bucket."""
    as_json = parsed.json
    try:
        client = context.forge_writer
        number = int(parsed.issue)
        for operation in (
            forge.ForgeOperation.CREATE_CHILD,
            forge.ForgeOperation.LINK_CHILD,
            forge.ForgeOperation.UPDATE_ITEM_BODY,
        ):
            if client.capability(operation) is not forge.Capability.READ_WRITE:
                raise protocol.ClaimUnavailableError(
                    f"this forge cannot {operation.value}; cut the slice by hand"
                )
        config = _load_board_config(client, context)
        open_issues = client.list_open_board_issues()
        target = _cut_target(client, open_issues, number, config.storage)
        return _cut_slice(client, target, open_issues, parsed, config)
    except _PartialWriteError as error:
        return _refuse_partial_write(error, CutReason.PARTIAL_WRITE, as_json=as_json)
    except protocol.ClaimError as error:
        return _refuse(CutReason.PRECONDITION_FAILED, error, as_json=as_json)


def _missing_item_refusal(number: int, client: forge.ForgeReader, storage: body.Storage) -> str:
    """The refusal sentence for a missing item number -- every forge-backed
    command that checks `client.item_reference(number).state is
    forge.ItemState.MISSING` before acting shares this one sentence rather
    than typing it out again. The repository slot is the canonical remote's
    own path: `owner/repo` on a forge host, the bare remote's local path
    for a forge-less remote under `state-ref` (PIN-28)."""
    return f"{board.item_label(number, storage)} does not exist in {client.repository.path}"


def _item_body_or_refuse(
    client: forge.ForgeReader, number: int, *, command: str, storage: body.Storage
) -> str:
    """The live body of issue `number`, or a by-name refusal before any
    write: `rule` and `ask` both target one existing issue, never a pull
    request -- a pull request exists only on a forge, so that refusal
    keeps the forge's own `#n`."""
    reference = client.item_reference(number)
    if reference.state is forge.ItemState.MISSING:
        raise protocol.ClaimUnavailableError(f"{board.item_label(number, storage)} does not exist")
    if reference.is_landing:
        raise protocol.ClaimUnavailableError(
            f"#{number} is a pull request, not an issue; {command} needs an issue"
        )
    return reference.body or ""


def _require_update_item_body(client: forge.ForgeWriter, *, command: str) -> None:
    if client.capability(forge.ForgeOperation.UPDATE_ITEM_BODY) is not forge.Capability.READ_WRITE:
        raise protocol.ClaimUnavailableError(
            f"this forge cannot update_item_body; {command} by hand"
        )


class _TargetUnavailableError(protocol.ClaimError):
    """`_require_writable_target`'s own "forge cannot write" refusal (issue
    #396) -- a distinct type from `_InvalidTargetError` so `ask`/`rule` can
    each choose their own `unavailable` without parsing prose."""


class _InvalidTargetError(protocol.ClaimError):
    """`_require_writable_target`'s own "no such writable item" refusal
    (issue #396) -- missing, a pull request, or a malformed body -- so
    `ask`/`rule` can each choose their own `invalid_item` without parsing
    prose."""


def _require_writable_target(
    client: forge.ForgeWriter, context: RunContext, number: int, *, command: str
) -> tuple[str, board.BoardConfig]:
    """`ask` and `rule`'s shared target gate (RULE-06..08, cited verbatim by
    `specs/ask.spec.md`): the forge must accept a body write, the item must
    exist and not be a pull request, and its body must parse -- in that
    order, unchanged from before issue #396 factored it out of both
    commands. Returns the live body and the board configuration `ask`/
    `rule` still need for their own write."""
    try:
        _require_update_item_body(client, command=command)
    except protocol.ClaimError as error:
        raise _TargetUnavailableError(str(error)) from error
    config = _load_board_config(client, context)
    try:
        body = _item_body_or_refuse(client, number, command=command, storage=config.storage)
        _located_block_or_refuse(number, body, command=command, storage=config.storage)
    except protocol.ClaimError as error:
        raise _InvalidTargetError(str(error)) from error
    return body, config


def _rule_remaining_open(new_body: str, *, storage: body.Storage) -> int:
    return sum(
        1 for line in body.expectation_lines(new_body, storage=storage) if line.ruling is None
    )


class RuleReason(StrEnum):
    """`aco rule`'s own `--json` `reason` vocabulary (`specs/rule.spec.md`,
    issue #396): `ruled` the only success."""

    RULED = "ruled"
    ALREADY_RULED = "already_ruled"
    LINE_OUT_OF_RANGE = "line_out_of_range"
    INVALID_ITEM = "invalid_item"
    INVALID_USAGE = "invalid_usage"
    UNAVAILABLE = "unavailable"


def _emit_rule_result(
    number: int,
    line: body.ExpectationLine,
    open_remaining: int,
    storage: body.Storage,
    *,
    as_json: bool,
) -> None:
    ruling = cast(str, line.ruling)
    ruled_on = cast(date, line.ruled_on)
    if as_json:
        _emit_json(
            True,
            RuleReason.RULED,
            item=number,
            index=line.index,
            ruling=ruling,
            ruled_on=ruled_on.isoformat(),
            open=open_remaining,
        )
        return
    print(
        f"RULED {board.item_label(number, storage)} line {line.index} {ruling}; "
        f"{open_remaining} line(s) still open"
    )


def rule_item(
    context: RunContext, number: int, line: int, ruling: str, note: str | None
) -> tuple[body.ExpectationLine, int]:
    """`_cmd_rule`'s own write, extracted (issue #280) so `board --serve`'s
    `POST /rule` calls the exact same path a CLI `aco rule` invocation does
    -- one owner for "click -> ruled line", never a second one behind the
    loopback server. Returns the newly ruled line and how many the item
    still has open; raises `protocol.ClaimError` by name for every refusal
    (already ruled, out of range, a bad outcome, a malformed or missing
    item), which both callers turn into their own by-name response."""
    client = context.forge_writer
    current_body, config = _require_writable_target(client, context, number, command="rule")
    ruled_on = datetime.now(UTC).date()
    new_body = body.rule_expectation(current_body, line, ruling, ruled_on, note=note)
    client.update_item_body(number, new_body)
    ruled_line = body.expectation_lines(new_body, storage=config.storage)[line - 1]
    return ruled_line, _rule_remaining_open(new_body, storage=config.storage)


_RuleItemError = (
    _TargetUnavailableError
    | _InvalidTargetError
    | body.ExpectationAlreadyRuledError
    | body.ExpectationOutOfRangeError
)


def _rule_item_reason(error: _RuleItemError) -> RuleReason:
    """`_cmd_rule`'s own mapping from `rule_item`'s four refusals to their
    `--json` `reason` (issue #396): a bad target names `unavailable`
    (forge cannot write) or `invalid_item` (missing item, pull request);
    a bad line names `already_ruled` or `line_out_of_range`."""
    if isinstance(error, _TargetUnavailableError):
        return RuleReason.UNAVAILABLE
    if isinstance(error, _InvalidTargetError):
        return RuleReason.INVALID_ITEM
    if isinstance(error, body.ExpectationAlreadyRuledError):
        return RuleReason.ALREADY_RULED
    return RuleReason.LINE_OUT_OF_RANGE


def _rule_expectation_line(parsed: argparse.Namespace, context: RunContext) -> int:
    """`rule`'s own work, every refusal raised by name for `_cmd_rule` to
    report."""
    number = int(parsed.item)
    ruled_line, open_remaining = rule_item(context, number, parsed.line, parsed.ruling, parsed.note)
    storage = context.config.storage
    _emit_rule_result(number, ruled_line, open_remaining, storage, as_json=parsed.json)
    return 0


def _cmd_rule(parsed: argparse.Namespace, context: RunContext) -> int:
    """`rule`'s own `--json` refusals (issue #396), for the whole command and
    not only its first steps (issue #432): the body write inside the shared
    ruling path can fail like any other forge call, and a failure there names
    this command's own `unavailable` rather than escaping the envelope."""
    as_json = parsed.json
    try:
        return _rule_expectation_line(parsed, context)
    except (
        _TargetUnavailableError,
        _InvalidTargetError,
        body.ExpectationAlreadyRuledError,
        body.ExpectationOutOfRangeError,
    ) as error:
        return _refuse(_rule_item_reason(error), error, as_json=as_json)
    except RepoMeaninglessUnderStateRefError as error:
        return _refuse(RuleReason.INVALID_USAGE, error, as_json=as_json)
    except protocol.ClaimError as error:
        return _refuse(RuleReason.UNAVAILABLE, error, as_json=as_json)


def _board_token_location(repository: forge.RepositoryId) -> workspace.BoardTokenLocation:
    """`board --serve`'s token file for the repository this command already
    resolved (issue #431) -- one board, one token, so the URL an operator
    opens can only ever reach this repository's own served board."""
    return workspace.default_board_token_location(repository.host, repository.path, os.environ)


@dataclass
class _ServedBoardCache:
    """`board --serve`'s own held page (issue #440): built once by the first
    `GET` (or the first after a rebuild) and reused by every one after it,
    instead of paying `_board_page`'s full forge fetch again on every
    request -- the fix for the operator's own report of a 19s-per-load
    board. Every ruling `POST /rule` -- written, refused, or raised (BOARD-48)
    -- marks `built` stale, so the next `GET` rebuilds it; an explicit
    `?reload=1` rebuilds on that very `GET`. `lock` serializes a rebuild
    against a concurrent request: `ThreadingHTTPServer` runs each one on its
    own thread. A rebuild reads through the asking request's own context
    (issues #447, #457), never one held by the cache: a state-ref forge is a
    snapshot of the store at resolution, so one held for the server's
    lifetime would never show a later write."""

    built: tuple[board_html.BoardPage, datetime] | None = None
    refusal: str | None = None
    stale: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def held(
        self, context: RunContext, *, reload: bool
    ) -> tuple[board_html.BoardPage, datetime, str | None]:
        """The held page, when it was built, and the refusal the last rebuild
        met, if any (issues #447, #481): rebuilt through `context` first when
        nothing is held yet, it is stale, or `reload` asks -- a request the
        held page answers never reads `context` at all. A refused rebuild --
        an item PIN-16 names missing or an unreachable remote alike -- keeps the
        last page built and its refusal until a later rebuild succeeds, so
        every request in between, the reload's own redirect target
        included, shows that page with the sentence beside its age instead
        of failing; only a first build has no page to keep, and raises."""
        with self.lock:
            built = self.built
            if built is None or self.stale or reload:
                built = self._rebuild(context)
            return (*built, self.refusal)

    def _rebuild(self, context: RunContext) -> tuple[board_html.BoardPage, datetime]:
        try:
            self.built = (_board_page(context), datetime.now(UTC))
        except protocol.ClaimError as refusal:
            if self.built is None:
                raise
            self.refusal = str(refusal)
            return self.built
        self.refusal = None
        self.stale = False
        return self.built

    def discard(self) -> None:
        with self.lock:
            self.stale = True


def _board_server(parsed: argparse.Namespace, context: RunContext) -> board_serve.BoardServer:
    """`board --serve`'s bound, listening server (issue #280), built but not
    yet run: a loopback page built through `_board_page` and held in
    `_ServedBoardCache` between requests (issue #440), and writes through
    `rule_item`, exactly like `aco rule` does apart -- `board_serve.py` is
    transport only, so this function is still the one place that resolves
    the forge, the persistent token (issue #388: `workspace.board_token`,
    `--new-token` mints a fresh one), renders a page, and rules a line.
    Every HTTP request reads through its own fresh child of the run's
    context (issue #457), never one memoised across requests.
    `resolve_token` is only called by `start` itself once the socket is
    already bound, so `render_page`'s own closure reads the resolved value
    back out of `token_holder` -- never a token read before the busy-port
    check that could bind. Split from `_cmd_board_serve`'s own
    `serve_forever` loop so a test can bind a real ephemeral port and drive
    it without blocking."""
    client = context.forge_writer
    token_holder: list[str] = []
    cache = _ServedBoardCache()

    def resolve_token() -> str:
        token = workspace.board_token(
            _board_token_location(client.repository), mint_new=parsed.new_token
        )
        token_holder.append(token)
        return token

    def render_page(refused: str | None, reload: bool) -> str:
        page, built_at, store_refusal = cache.held(context.fresh(), reload=reload)
        notices = dict.fromkeys(
            sentence for sentence in (refused, store_refusal) if sentence is not None
        )
        served = board_html.ServedRuleForm(
            token=token_holder[0],
            refused=" ".join(notices) or None,
            age=datetime.now(UTC) - built_at,
        )
        return board_html.render(page, served=served)

    def post_rule(item: int, line: int, ruling: str, note: str | None) -> board_serve.RuleOutcome:
        # A refused click (a line already ruled elsewhere) means the held
        # page is stale too, so every click rebuilds, not only a write.
        # Discarding only after `rule_item` returns or raises (issue #440
        # review) keeps a concurrent GET that races the write from rebuilding
        # and holding a pre-ruling page: discarding first left a window where
        # such a GET restored exactly the staleness this cache exists to
        # remove. The click writes through a store read afresh and holds
        # PIN-29 through its write (issue #447): the server's startup
        # snapshot cannot see an item that went bad while it ran, and a
        # preflight alone could not see one going bad before the write.
        try:
            request_context = context.fresh()
            clicked = request_context.forge_writer
            if isinstance(clicked, state_board.StateRefBoard):
                clicked.hold_well_formed()
            rule_item(request_context, item, line, ruling, note)
        except protocol.ClaimError as error:
            return board_serve.RuleOutcome(refusal=str(error))
        finally:
            cache.discard()
        return board_serve.RuleOutcome(refusal=None)

    # Building the first page before `start` makes a store the board read
    # refuses (issue #447) stop the server before any token write or ruling click;
    # the first `GET` then serves this very page instead of building again.
    # No request exists yet, so it reads through the run's own context.
    cache.held(context, reload=False)
    return board_serve.start(
        port=parsed.port, resolve_token=resolve_token, render_page=render_page, rule_item=post_rule
    )


def _cmd_board_serve(parsed: argparse.Namespace, context: RunContext) -> int:
    server = _board_server(parsed, context)
    print(server.url, flush=True)
    try:
        server.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.httpd.server_close()
    return 0


@dataclass(frozen=True)
class _AskedLine:
    """One `aco ask` write's result (issue #295): the item, the fresh
    line's 1-based index, its `text`/`default`, and whichever card fields
    were given -- bundled so `_print_ask_result` takes one value instead of
    five loose ones."""

    item: int
    index: int
    text: str
    default: str
    card: body.ExpectationCardFields


class AskReason(StrEnum):
    """`aco ask`'s own `--json` `reason` vocabulary (`specs/ask.spec.md`,
    issue #396): `asked` the only success."""

    ASKED = "asked"
    INVALID_ITEM = "invalid_item"
    INVALID_EXPECTATION = "invalid_expectation"
    INVALID_PICTURE = "invalid_picture"
    INVALID_USAGE = "invalid_usage"
    UNAVAILABLE = "unavailable"


def _emit_ask_result(asked: _AskedLine, storage: body.Storage, *, as_json: bool) -> None:
    if as_json:
        card_fields = {key: value for key, value in asdict(asked.card).items() if value is not None}
        _emit_json(
            True,
            AskReason.ASKED,
            item=asked.item,
            index=asked.index,
            text=asked.text,
            default=asked.default,
            **card_fields,
        )
        return
    print(f"ASKED {board.item_label(asked.item, storage)} line {asked.index}: {asked.text}")


class _PictureFileError(protocol.ClaimError):
    """`_read_picture_file`'s own unreadable-file refusal (issue #396),
    typed so `aco ask`'s `--json` can choose `invalid_picture` without
    parsing prose."""


def _read_picture_file(path: str) -> str:
    """`--picture FILE.svg`'s own filesystem boundary (issue #295): read
    before any forge call, so a missing file refuses before the item body
    is even fetched. Content validation (size, `<svg>` root, no `<script>`,
    no external `href`) is `body.append_expectation`'s -- one owner, shared
    with the body parser's own defects."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise _PictureFileError(f"--picture {path} could not be read: {error}") from error


def _ask_target_reason(error: _TargetUnavailableError | _InvalidTargetError) -> AskReason:
    """`_cmd_ask`'s own mapping from `_require_writable_target`'s two
    target refusals to their `--json` `reason` (issue #396)."""
    if isinstance(error, _TargetUnavailableError):
        return AskReason.UNAVAILABLE
    return AskReason.INVALID_ITEM


def _ask_expectation_reason(
    error: body.ExpectationTextError | body.ExpectationFieldError,
) -> AskReason:
    """`_cmd_ask`'s own mapping from `append_expectation`'s two card-content
    refusals (issue #396) to their `--json` `reason`: a blank `--text`
    (`ExpectationTextError`) and a `--question`/`--example` failing its own
    rule both name `invalid_expectation`; only a refused `--picture`
    (`ExpectationFieldError` whose `field` is `picture`) names
    `invalid_picture`, per `specs/ask.spec.md`'s ASK-07/ASK-10 split."""
    if isinstance(error, body.ExpectationFieldError) and error.field == "picture":
        return AskReason.INVALID_PICTURE
    return AskReason.INVALID_EXPECTATION


def _append_expectation_card(parsed: argparse.Namespace, context: RunContext) -> int:
    """`ask`'s own work, every refusal raised by name for `_cmd_ask` to
    report: the picture file is read before the forge is ever resolved, so a
    missing one refuses before the item body is fetched."""
    picture = _read_picture_file(parsed.picture) if parsed.picture else None
    card = body.ExpectationCardFields(
        question=parsed.question, example=parsed.example, picture=picture
    )
    client = context.forge_writer
    number = int(parsed.item)
    current_body, config = _require_writable_target(client, context, number, command="ask")
    new_body = body.append_expectation(current_body, parsed.text, parsed.default, card=card)
    index = len(body.expectation_lines(new_body, storage=config.storage))
    client.update_item_body(number, new_body)
    asked = _AskedLine(
        item=number, index=index, text=parsed.text, default=parsed.default, card=card
    )
    _emit_ask_result(asked, config.storage, as_json=parsed.json)
    return 0


def _cmd_ask(parsed: argparse.Namespace, context: RunContext) -> int:
    """`ask`'s own `--json` refusals (issue #396), for the whole command and
    not only its first steps (issue #432): the body write this command ends
    with can fail like any other forge call, and a failure there names this
    command's own `unavailable` rather than escaping the envelope."""
    as_json = parsed.json
    try:
        return _append_expectation_card(parsed, context)
    except _PictureFileError as error:
        return _refuse(AskReason.INVALID_PICTURE, error, as_json=as_json)
    except (_TargetUnavailableError, _InvalidTargetError) as error:
        return _refuse(_ask_target_reason(error), error, as_json=as_json)
    except (body.ExpectationTextError, body.ExpectationFieldError) as error:
        return _refuse(_ask_expectation_reason(error), error, as_json=as_json)
    except RepoMeaninglessUnderStateRefError as error:
        return _refuse(AskReason.INVALID_USAGE, error, as_json=as_json)
    except protocol.ClaimError as error:
        return _refuse(AskReason.UNAVAILABLE, error, as_json=as_json)


def _release_branch_for(parsed: argparse.Namespace) -> str | None:
    if parsed.coordinator_override:
        protocol._require_coordinator_override(parsed.role)
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


def _bootstrap_state(context: RunContext) -> int:
    """Create `refs/aco/state` if proven absent; report the existing tip
    untouched when it is already there. Forge-free (issue #245): `--repo` is
    meaningless here and unused. The context's configuration is the tracked
    one (#315), so an untracked `board.toml` refuses here too, before this
    command's own first write, as a canonical remote with no URL does
    (issue #516)."""
    canonical_remote = context.configured_canonical_remote
    print(store.bootstrap(worktree=context.toplevel, remote=canonical_remote))
    return 0


# A short, still-practically-unique prefix of an `ObjectId`'s 40 hex
# characters (git's own `--short` abbreviation depends on the repository's
# object count, which the bundle filename has no reason to vary with).
_RESET_BUNDLE_SHA_LENGTH = 12


class ResetStep(StrEnum):
    """The five lines `reset` prints, in the one order the dry run and the
    real run share (plan review, 19.09.2026): export, delete the remote
    ref, delete the local one if present, clear every worktree's lineage
    stamp and fetch anchor, bootstrap fresh."""

    EXPORT = "export"
    DELETE_REMOTE = "delete_remote"
    DELETE_LOCAL = "delete_local"
    CLEAR_STAMPS = "clear_stamps"
    BOOTSTRAP = "bootstrap"


RESET_STEP_ORDER: tuple[ResetStep, ...] = (
    ResetStep.EXPORT,
    ResetStep.DELETE_REMOTE,
    ResetStep.DELETE_LOCAL,
    ResetStep.CLEAR_STAMPS,
    ResetStep.BOOTSTRAP,
)


@dataclass(frozen=True)
class ResetExportTarget:
    tip: protocol.ObjectId
    destination: Path


@dataclass(frozen=True)
class ResetPlan:
    """Every fact `reset`'s five lines are built from, read once before
    anything is exported or deleted (plan review, 19.09.2026). The dry run
    prints this verbatim with a `would: ` prefix; the real run prints the
    same per-step text as each action completes -- the two share the exact
    same line-building functions below, so they cannot say different things
    about the same reset."""

    remote: str
    remote_tip: protocol.ObjectId | None
    export_target: ResetExportTarget | None
    local_ref_present: bool
    worktree_count: int
    unreadable_schema_version: int | None


def _reset_bundle_name(repository: str, today: date, tip: protocol.ObjectId) -> str:
    return f"aco-state-{repository}-{today.isoformat()}-{tip[:_RESET_BUNDLE_SHA_LENGTH]}.bundle"


def _reset_restore_command(destination: Path) -> str:
    """`export_state_bundle` bundles `store.EXPORT_BUNDLE_REF`, never the
    shared `store.STATE_REF` (issue #298, 19.09.2026 REVISE findings 1+2)
    -- the bundle's own head therefore carries that name (`git bundle
    list-heads` shows it), and the fetch renames it to `STATE_REF` on the
    way in.
    """
    return f"git fetch {destination} {store.EXPORT_BUNDLE_REF}:{store.STATE_REF}"


@dataclass(frozen=True)
class ResetExportConfig:
    """`--no-export`/`--export-dir`, resolved once (issue #298): keeps
    `_build_reset_plan` under the five-argument ceiling without folding an
    unrelated pair of facts into `worktree` or `remote`."""

    enabled: bool
    directory: Path


def _resolved_reset_export_config(parsed: argparse.Namespace, toplevel: Path) -> ResetExportConfig:
    directory = parsed.export_dir if parsed.export_dir is not None else toplevel.parent
    return ResetExportConfig(enabled=not parsed.no_export, directory=directory)


def _build_reset_plan(
    *,
    worktree: Path,
    remote: str,
    state: protocol.ClaimState | protocol.UnreadableState,
    export: ResetExportConfig,
    today: date,
) -> ResetPlan:
    export_target = None
    if export.enabled and state.tip is not None:
        destination = export.directory / _reset_bundle_name(worktree.name, today, state.tip)
        export_target = ResetExportTarget(tip=state.tip, destination=destination)
    return ResetPlan(
        remote=remote,
        remote_tip=state.tip,
        export_target=export_target,
        local_ref_present=store.local_state_ref_exists(worktree),
        worktree_count=len(store.list_worktrees(worktree)),
        unreadable_schema_version=(
            state.schema_version if isinstance(state, protocol.UnreadableState) else None
        ),
    )


def _reset_unreadable_line(schema_version: int) -> str:
    return (
        f"schema {schema_version} not readable by this aco; live claims unknown "
        "(--confirm needs --force-unreadable)"
    )


def _reset_export_line(plan: ResetPlan, *, done: bool) -> str:
    if plan.export_target is None:
        if plan.remote_tip is None:
            return f"nothing to export: {store.STATE_REF} does not exist on {plan.remote}"
        return f"skipped export (--no-export): {store.STATE_REF} at {plan.remote_tip} not saved"
    verb = "exported" if done else "export"
    restore = _reset_restore_command(plan.export_target.destination)
    return (
        f"{verb} {store.STATE_REF} at {plan.export_target.tip} to "
        f"{plan.export_target.destination} (restore with: {restore})"
    )


def _reset_delete_remote_line(plan: ResetPlan, *, done: bool) -> str:
    if plan.remote_tip is None:
        return f"nothing to delete on {plan.remote}: {store.STATE_REF} does not exist"
    verb = "deleted" if done else "delete"
    return f"{verb} {store.STATE_REF} on {plan.remote} (lease {plan.remote_tip})"


def _reset_delete_local_line(*, present: bool, done: bool) -> str:
    if not present:
        return f"no local {store.STATE_REF} to delete"
    verb = "deleted" if done else "delete"
    return f"{verb} local {store.STATE_REF}"


def _reset_clear_stamps_line(*, worktree_count: int, done: bool) -> str:
    verb = "cleared" if done else "clear"
    plural = "" if worktree_count == 1 else "s"
    return f"{verb} lineage stamps and fetch anchors in {worktree_count} worktree{plural}"


def _reset_bootstrap_line(*, tip: protocol.ObjectId | None) -> str:
    if tip is None:
        return "bootstrap a fresh empty state"
    return f"bootstrapped a fresh empty state at {tip}"


def _print_reset_dry_run(plan: ResetPlan) -> None:
    lines: dict[ResetStep, str] = {
        ResetStep.EXPORT: _reset_export_line(plan, done=False),
        ResetStep.DELETE_REMOTE: _reset_delete_remote_line(plan, done=False),
        ResetStep.DELETE_LOCAL: _reset_delete_local_line(
            present=plan.local_ref_present, done=False
        ),
        ResetStep.CLEAR_STAMPS: _reset_clear_stamps_line(
            worktree_count=plan.worktree_count, done=False
        ),
        ResetStep.BOOTSTRAP: _reset_bootstrap_line(tip=None),
    }
    if plan.unreadable_schema_version is not None:
        print(_reset_unreadable_line(plan.unreadable_schema_version))
    for step in RESET_STEP_ORDER:
        print(f"would: {lines[step]}")


def _execute_reset(*, worktree: Path, remote: str, plan: ResetPlan) -> None:
    """Export -> delete remote -> delete local -> clear stamps -> bootstrap
    (issue #298), each step printed the moment it completes. An export
    failure raises before anything else runs; a remote-deletion failure
    (rejected, or the lease gone stale) raises with the local ref left
    exactly as it was, and whatever export ran left on disk."""
    if plan.export_target is not None:
        store.export_state_bundle(
            worktree=worktree,
            tip=plan.export_target.tip,
            destination=plan.export_target.destination,
        )
    print(_reset_export_line(plan, done=True))
    local_deleted = store.delete_state_ref(
        worktree=worktree, remote=remote, expected_remote_tip=plan.remote_tip
    )
    print(_reset_delete_remote_line(plan, done=True))
    print(_reset_delete_local_line(present=local_deleted, done=True))
    cleared_worktrees = store.clear_lineage_stamps(worktree=worktree)
    print(_reset_clear_stamps_line(worktree_count=len(cleared_worktrees), done=True))
    fresh_tip = store.bootstrap(worktree=worktree, remote=remote)
    print(_reset_bootstrap_line(tip=fresh_tip))


def _reset_observation(
    context: RunContext,
) -> tuple[Path, str, protocol.ClaimState | protocol.UnreadableState]:
    """`reset`'s own state read (issue #298, 19.09.2026 gate finding 1):
    `store.peek_state_for_reset` instead of `RunContext.observation`'s ordinary
    `fetch_state`, so a broken lineage -- exactly what `reset` exists to
    recover from -- never blocks it, and so a dry run, a live-claim
    refusal, or a failed export writes no per-worktree stamp or anchor
    (finding 2). A canonical remote with no URL refuses before that read
    (issue #516)."""
    canonical_remote = context.configured_canonical_remote
    worktree = context.toplevel
    state = store.peek_state_for_reset(worktree=worktree, remote=canonical_remote)
    return worktree, canonical_remote, state


def _reset_state(parsed: argparse.Namespace, context: RunContext) -> int:
    """`reset` (issue #298): exports `STATE_REF`, deletes it on the remote
    with a lease and locally if present, clears every worktree's lineage
    stamp and fetch anchor, and bootstraps a fresh empty state. Forge-free,
    like `bootstrap`. A live claim always refuses -- `--confirm` or not --
    printing its claim lines instead of touching anything: a reset over live
    work is data loss with no owner. A state whose schema this aco cannot
    read has unknown live claims, so executing over it additionally needs
    `--force-unreadable` (issue #341); its bundle is still exported.
    """
    worktree, remote, state = _reset_observation(context)
    if isinstance(state, protocol.ClaimState) and state.claims:
        ages = _claim_ages(worktree, state)
        _status(tuple(state.claims.values()), None, ages, context.config.storage)
        return 2
    export = _resolved_reset_export_config(parsed, context.toplevel)
    plan = _build_reset_plan(
        worktree=worktree,
        remote=remote,
        state=state,
        export=export,
        today=datetime.now(UTC).date(),
    )
    if not parsed.confirm:
        _print_reset_dry_run(plan)
        return 0
    if plan.unreadable_schema_version is not None and not parsed.force_unreadable:
        raise protocol.ClaimError(_reset_unreadable_line(plan.unreadable_schema_version))
    _execute_reset(worktree=worktree, remote=remote, plan=plan)
    return 0


@dataclass(frozen=True)
class _CommandEntry:
    """One command's parser builder and handler (issue #372: one table
    instead of a `_SUBPARSER_BUILDERS`/`_READ_HANDLERS`/`_WRITE_HANDLERS`/
    `_FORGE_FREE_COMMANDS` quartet keyed by the same names). Every handler
    takes the parsed arguments and the run's `RunContext`; `release` also
    takes the branch `_dispatch` read before it started."""

    add_parser: Callable[[argparse._SubParsersAction], None]
    handler: Callable[..., int | None]


_COMMAND_TABLE: dict[str, _CommandEntry] = {
    "bootstrap": _CommandEntry(
        _add_bootstrap_parser, lambda _parsed, context: _bootstrap_state(context)
    ),
    "reset": _CommandEntry(_add_reset_parser, _reset_state),
    "board": _CommandEntry(_add_board_parser, _cmd_board),
    "rulings": _CommandEntry(_add_rulings_parser, _cmd_rulings),
    "next": _CommandEntry(_add_next_parser, _cmd_next),
    "start": _CommandEntry(_add_start_parser, _cmd_start),
    "claim": _CommandEntry(_add_claim_parser, _cmd_claim),
    "release": _CommandEntry(_add_release_parser, _cmd_release),
    "land": _CommandEntry(_add_land_parser, _cmd_land),
    "rescope": _CommandEntry(_add_rescope_parser, _cmd_rescope),
    "cut": _CommandEntry(_add_cut_parser, _cmd_cut),
    "ask": _CommandEntry(_add_ask_parser, _cmd_ask),
    "rule": _CommandEntry(_add_rule_parser, _cmd_rule),
    "check": _CommandEntry(_add_check_parser, _cmd_check),
    "brief": _CommandEntry(_add_brief_parser, _cmd_brief),
}


def _dispatch_item(parsed: argparse.Namespace, context: RunContext) -> int:
    """`item`'s own four subcommands, pulled out of `_dispatch` (issue #372
    S1) so their nesting stops counting against every other command's
    cognitive complexity."""
    if parsed.item_command == "new":
        return _cmd_item_new(parsed, context)
    if parsed.item_command == "edit":
        return _cmd_item_edit(parsed, context)
    if parsed.item_command == "close":
        return _cmd_item_close(parsed, context)
    return _cmd_item_show(parsed, context)


def _resolved_before_the_command_starts(parsed: argparse.Namespace) -> str | None:
    """Everything a writer needs settled before its own command starts
    (issue #425): the agent identity it claims under, and `release`'s own
    branch and override checks, which read the checkout rather than the
    claim record. Returns `release`'s branch, `None` for every other
    command."""
    if parsed.command in {"claim", "release", "rescope", "land"}:
        parsed.agent = checkout.resolved_agent(parsed.agent)
    return _release_branch_for(parsed) if parsed.command == "release" else None


def _dispatch(parsed: argparse.Namespace, context: RunContext) -> int:
    # Only the pre-start checks report through the shared envelope (issue
    # #425); a refusal a command raises itself owns its own `reason`, so the
    # dispatch below stays outside this boundary.
    try:
        release_branch = _resolved_before_the_command_starts(parsed)
    except protocol.ClaimError as error:
        return _refuse(
            PreDispatchReason.PRECONDITION_FAILED,
            error,
            as_json=bool(getattr(parsed, "json", False)),
        )
    if parsed.command == "item":
        return _dispatch_item(parsed, context)
    entry = _COMMAND_TABLE[parsed.command]
    if parsed.command == "release":
        # The release branch is dispatch data, read before the command
        # starts, never a repository fact the context would hold (#418).
        result = entry.handler(parsed, context, release_branch)
    elif parsed.command == "board" and parsed.serve:
        # `board --serve` writes through a click (issue #280), so it runs
        # its own handler rather than `board`'s read-only output modes.
        result = _cmd_board_serve(parsed, context)
    else:
        result = entry.handler(parsed, context)
    return 0 if result is None else result


def _workspace_config_path() -> Path:
    return workspace.default_config_path(os.environ)


def _register_workspace(parsed: argparse.Namespace) -> int:
    handoff = workspace.WorkspaceRegistration(
        parsed.project,
        parsed.path,
        parsed.session_id,
        parsed.agent,
        parsed.model,
        provider=providers.Provider(parsed.provider),
        live_pid=parsed.live_pid,
    )
    created = workspace.register_project(handoff, _workspace_config_path())
    status = "registered" if created else "already registered"
    print(f"{parsed.project}: {status}")
    return 0


def _run_workspace(parsed: argparse.Namespace) -> int:
    outcomes = workspace.run_projects(_workspace_config_path(), parsed.project)
    for outcome in outcomes:
        suffix = f": {outcome.detail}" if outcome.detail else ""
        print(f"{outcome.project}: {outcome.state}{suffix}")
    return (
        2
        if any(
            outcome.state in {workspace.RunState.FAILED, workspace.RunState.UNKNOWN}
            for outcome in outcomes
        )
        else 0
    )


def _login_summary(result: workspace.LoginRunResult) -> str:
    if result.attempt.failure is not None:
        return "Workspace recovery failed."
    outcomes = result.attempt.outcomes
    failed_states = {workspace.RunState.FAILED, workspace.RunState.UNKNOWN}
    failed = sum(state in failed_states for _, state in outcomes)
    already_live = sum(state is workspace.RunState.EXTERNAL for _, state in outcomes)
    recovered = len(outcomes) - failed - already_live
    summary = f"Workspace recovery completed for {recovered} project(s)."
    if already_live:
        summary += f" {already_live} project(s) already had live owners; no console was opened."
    if failed:
        summary += f" {failed} project(s) failed recovery."
    return summary


def _run_at_login() -> int:
    try:
        result = workspace.run_login_recovery(
            _workspace_config_path(), workspace.login_attempt_path(os.environ)
        )
    except workspace.WorkspaceError:
        terminal.notify_login_recovery("Workspace recovery could not record its attempt.")
        return 2
    terminal.notify_login_recovery(_login_summary(result))
    return result.exit_status


def _login_status() -> int:
    launcher = workspace.login_launcher_state(os.environ, Path(sys.executable))
    configuration = workspace.login_configuration_state(_workspace_config_path())
    print(f"launcher: {launcher}")
    print(f"configuration: {configuration}")
    try:
        attempt = workspace.load_login_attempt(workspace.login_attempt_path(os.environ))
    except FileNotFoundError:
        print("attempt: no login attempt recorded")
        return 0
    except workspace.WorkspaceError:
        print("attempt: malformed")
        return 2
    print(f"attempt: {attempt.attempt_id} {attempt.started_at} {attempt.state}")
    for project, outcome in attempt.outcomes:
        print(f"{project}: {outcome}")
    if attempt.failure is not None:
        print(f"workspace: {attempt.failure}")
    if attempt.completed_at is not None:
        print(f"completed: {attempt.completed_at}")
    return 0


def _login_operation(parsed: argparse.Namespace) -> int:
    if parsed.repo is not None:
        raise protocol.ClaimError("--repo is meaningless for login recovery operations")
    if parsed.login_command == "enable":
        changed = workspace.enable_login(_workspace_config_path(), os.environ, Path(sys.executable))
        print("login launcher enabled" if changed else "login launcher already enabled")
        return 0
    if parsed.login_command == "disable":
        changed = workspace.disable_login(os.environ)
        print("login launcher disabled" if changed else "login launcher already disabled")
        return 0
    return _login_status()


def _local_operation(parsed: argparse.Namespace) -> int:
    if parsed.command == "_run-at-login":
        return _run_at_login()
    if parsed.command == "login":
        return _login_operation(parsed)
    if parsed.repo is not None:
        raise protocol.ClaimError("--repo is meaningless for workspace operations")
    return _register_workspace(parsed) if parsed.command == "register" else _run_workspace(parsed)


def _read_status_body_or_dispatch(parsed: argparse.Namespace, context: RunContext) -> int:
    """`status` and `body` are forge-free (issue #245, #262): both are
    resolved here, ahead of `_dispatch`, and neither ever asks the
    `RunContext` for its forge."""
    if parsed.command == "status":
        return _cmd_status(parsed, context)
    if parsed.command == "body":
        return _cmd_body(parsed, context)
    return _dispatch(parsed, context)


def _subcommands(parser: argparse.ArgumentParser) -> _RecordingSubParsersAction | None:
    """`parser`'s own subcommand action, or `None` for a leaf command."""
    return next(
        (action for action in parser._actions if isinstance(action, _RecordingSubParsersAction)),
        None,
    )


def _spells_json_flag(token: str, parser: argparse.ArgumentParser) -> bool:
    """Whether `parser` itself would read `token` as its own `--json`: the
    exact spelling on a parser that declares it, never an abbreviation, since
    no `aco` parser reads one (OUT-09)."""
    spelling = token.split("=", 1)[0]
    return spelling == JSON_FLAG and JSON_FLAG in parser._option_string_actions


def _level_options(tokens: tuple[str, ...]) -> tuple[str, ...]:
    """The tokens one parser level still reads as options. A bare `--` ends
    that level's options and nothing else's: argparse marks the rest of this
    level's tokens as non-options, then hands what follows the command name
    down untouched, and the next level scans it for a `--` of its own."""
    return tokens[: tokens.index(LONG_OPTION_PREFIX)] if LONG_OPTION_PREFIX in tokens else tokens


def _asked_for_json(root: argparse.ArgumentParser, given: list[str]) -> bool:
    """Whether this invocation asked for JSON, answered by the parsers it
    reached rather than by the raw tokens alone (issue #432): only a command
    declaring `--json` can answer in the envelope, so `aco bootstrap --json`
    stays argparse's own text, and so does `aco release --jso`, an
    abbreviation no parser reads (OUT-09). Each level is asked about its own tokens,
    the ones argparse handed it, so a `--` cuts that level alone."""
    return any(
        _spells_json_flag(token, parser)
        for parser, tokens in _reached_levels(root, given)
        for token in _level_options(tokens)
    )


def _reached_levels(
    root: argparse.ArgumentParser, given: list[str]
) -> Iterator[tuple[argparse.ArgumentParser, tuple[str, ...]]]:
    """Each parser this invocation's parse reached, root first, with the
    tokens argparse handed that level (issue #432)."""
    parser, tokens = root, tuple(given)
    while True:
        yield parser, tokens
        subcommands = _subcommands(parser)
        if subcommands is None or subcommands.chosen is None:
            return
        parser, tokens = subcommands.chosen, subcommands.handed_down


# Every argparse `type=` that reads an item reference. `check`'s subject also
# reads a trunk commit id, which parses to a string and so names no item.
_ITEM_ARGUMENT_TYPES = frozenset({board.parse_item_reference, _parse_check_subject})


def _item_arguments(
    root: argparse.ArgumentParser, given: list[str], parsed: argparse.Namespace
) -> Iterator[int]:
    """Every item number this invocation named: the parsed integer of each
    argument an `_ITEM_ARGUMENT_TYPES` parser types, on every parser the
    parse reached."""
    for parser, _tokens in _reached_levels(root, given):
        for action in parser._actions:
            value = getattr(parsed, action.dest, None)
            if action.type in _ITEM_ARGUMENT_TYPES and isinstance(value, int):
                yield value


def _refuse_item_past_the_id_space(
    root: argparse.ArgumentParser,
    given: list[str],
    parsed: argparse.Namespace,
    context: RunContext,
) -> int | None:
    """PIN-31 (issue #467, #469 review): under `storage = "state-ref"` a
    number past `aco-ffffff` names no item, and `items.format_item_id` would
    print it as an id `board.parse_item_reference` refuses back -- so it
    refuses here, before any command looks it up or names it. The storage
    pin is read only for such a number; a pin that cannot be read is left to
    the command, which meets the same refusal and reports it in its own
    envelope."""
    past = next(
        (
            number
            for number in _item_arguments(root, given, parsed)
            if board.names_no_item(number, body.Storage.STATE_REF)
        ),
        None,
    )
    if past is None or not _pins_state_ref(context):
        return None
    error = protocol.ClaimUnavailableError(STATE_REF_ITEM_PAST_THE_ID_SPACE.format(number=past))
    return _refuse(PreDispatchReason.INVALID_USAGE, error, as_json=_asked_for_json(root, given))


def _pins_state_ref(context: RunContext) -> bool:
    """Whether this checkout's readable storage pin says `state-ref`; an
    unreadable pin says nothing here, so its refusal stays the command's."""
    try:
        return context.config.storage is body.Storage.STATE_REF
    except protocol.ClaimError:
        return False


def main(arguments: list[str] | None = None) -> int:
    # Every refusal the parse itself raises -- argparse's own usage errors,
    # and `board.parse_item_reference`, an argparse `type=` whose refusal is
    # a `ClaimError` -- fires before any namespace carries `--json`, so the
    # mode is read back off the parsers this parse reached (issue #432).
    # Both report `invalid_usage` through the one envelope. Past the parse
    # the plain sentence alone still stands: a refusal a command raises
    # outside its own reported vocabulary must not be dressed as one
    # (issue #425).
    given = sys.argv[1:] if arguments is None else arguments
    parser = _parser()
    try:
        parsed = parser.parse_args(given)
        # Judged after the parse, not as an argparse `type=`: `--repo`
        # precedes the subcommand, so a refusal raised while argparse reads
        # it would fire before the parse reached the command that decides
        # whether this run asked for JSON (issue #465).
        parsed.repo = None if parsed.repo is None else github.repository_id(parsed.repo)
    except _UsageError as error:
        return _refuse_usage(error, as_json=_asked_for_json(parser, given))
    except protocol.ClaimError as error:
        return _refuse(
            PreDispatchReason.INVALID_USAGE, error, as_json=_asked_for_json(parser, given)
        )
    try:
        if parsed.command in {"_run-at-login", "register", "run", "login"}:
            return _local_operation(parsed)
        if parsed.command == "protect":
            return _protect()
        context = _run_context(parsed.repo)
        refusal = _refuse_item_past_the_id_space(parser, given, parsed, context)
        return refusal if refusal is not None else _read_status_body_or_dispatch(parsed, context)
    except protocol.ClaimError as error:
        print(f"{CLI_ERROR_PREFIX}{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
