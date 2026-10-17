"""Coordinate coding-agent claims through this repository's own state ref."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, NoReturn

from . import (
    __version__,
    board,
    body,
    checkout,
    cmd_ask,
    cmd_board,
    cmd_body,
    cmd_bootstrap,
    cmd_brief,
    cmd_check,
    cmd_claim,
    cmd_cut,
    cmd_item,
    cmd_land,
    cmd_next,
    cmd_protect,
    cmd_release,
    cmd_rescope,
    cmd_reset,
    cmd_rule,
    cmd_rulings,
    cmd_start,
    cmd_status,
    cmd_workspace,
    github,
    items,
    metrics,
    protocol,
    providers,
    report,
    session,
)
from .session import (
    RunContext,
)

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


LANE_ISSUE_HELP = "omit for lane mode, derived from a docs/ or fix/ checkout branch"
LANE_LOOKUP_HELP = "omit for lane mode: a docs/ or fix/ branch, or one holding a live lane claim"
LONG_OPTION_PREFIX = "--"
JSON_HELP = "print the result as JSON instead of the human lines"
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


def _add_bootstrap_parser(commands: _Subcommands, name: str) -> None:
    commands.add_parser(name, help="create refs/aco/state if it does not exist yet")


def _add_reset_parser(commands: _Subcommands, name: str) -> None:
    reset = commands.add_parser(
        name,
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


def _add_json_flag(container: argparse.ArgumentParser) -> None:
    """The one place `--json` is declared (issue #432): a command that never
    calls this has no JSON mode at all, and `main` matches this same
    spelling when argparse refuses before any namespace is filled, so the
    flag needs a single owner."""
    container.add_argument(report.JSON_FLAG, action="store_true", help=JSON_HELP)


def _add_status_parser(commands: _Subcommands, name: str) -> None:
    status = commands.add_parser(name, help="show repository-wide build claims")
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


def _add_board_parser(commands: _Subcommands, name: str) -> None:
    board_command = commands.add_parser(name, help="project the open work board, read-only")
    _add_json_flag(board_command)
    board_command.add_argument(
        "--serve",
        action="store_true",
        help=argparse.SUPPRESS,
    )


def _add_rulings_parser(commands: _Subcommands, name: str) -> None:
    rulings_command = commands.add_parser(
        name, help="list expectation lines, open and ruled, without writes"
    )
    _add_json_flag(rulings_command)


def _add_next_parser(commands: _Subcommands, name: str) -> None:
    next_command = commands.add_parser(
        name,
        help="name the board's top-priority item to pull",
        description=NEXT_PULL_DESCRIPTION,
    )
    _add_json_flag(next_command)


def _add_start_parser(commands: _Subcommands, name: str) -> None:
    start = commands.add_parser(
        name,
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


def _add_claim_parser(commands: _Subcommands, name: str) -> None:
    claim = commands.add_parser(
        name,
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
        default=cmd_claim.DEFAULT_CLAIM_ROLE,
        help=f"the claiming role; default {cmd_claim.DEFAULT_CLAIM_ROLE}",
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


def _add_release_parser(commands: _Subcommands, name: str) -> None:
    release = commands.add_parser(name, help="release a landed or abandoned claim")
    release.add_argument(
        "issue",
        type=board.parse_item_reference,
        nargs="?",
        help=LANE_LOOKUP_HELP,
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
        cmd_release.KEEP_WORKTREE_FLAG,
        action="store_true",
        help=(
            "keep the lane's local worktree and branch after a merged landing; by default a "
            "clean worktree whose branch is already merged is removed"
        ),
    )
    _add_json_flag(release)


def _add_land_parser(commands: _Subcommands, name: str) -> None:
    land = commands.add_parser(
        name,
        help="merge a green pull request with its pinned head sha and run its release path",
    )
    land.add_argument("pull_request", type=int, help="the pull request number to land")
    land.add_argument(
        "--head",
        type=_reviewed_head,
        help=(
            "the head sha the reviewers saw, in full or as a prefix of at least 7 hex digits; "
            "land refuses before any write when the pull request's head is another commit"
        ),
    )
    land.add_argument("--agent", help=AGENT_HELP)
    land.add_argument("--role", help=ROLE_ON_LIVE_CLAIM_HELP)
    land.add_argument(
        "--coordinator-override",
        action="store_true",
        help="land another agent's claim as the coordinator; requires --role coordinator",
    )
    land.add_argument(
        cmd_release.KEEP_WORKTREE_FLAG,
        action="store_true",
        help=(
            "keep the lane's local worktree and branch after landing; by default a clean "
            "worktree whose branch is already merged is removed"
        ),
    )


REVIEWED_HEAD_PATTERN = re.compile(r"[0-9a-fA-F]{7,40}")


def _reviewed_head(value: str) -> str:
    """`land --head`'s argparse `type=` (issue #590): the sha its reviewers
    saw, lowercased as git prints it, refused before anything is read."""
    if REVIEWED_HEAD_PATTERN.fullmatch(value) is None:
        raise protocol.ClaimUnavailableError("--head must be 7 to 40 hex digits")
    return value.lower()


def _add_rescope_parser(commands: _Subcommands, name: str) -> None:
    rescope = commands.add_parser(name, help="add or drop paths on a live claim without releasing")
    rescope.add_argument(
        "issue",
        type=board.parse_item_reference,
        nargs="?",
        help=LANE_LOOKUP_HELP,
    )
    rescope.add_argument("--agent", help=AGENT_HELP)
    rescope.add_argument(
        "--add",
        action="append",
        help=(
            "an absolute or repository-relative path to add; repeat --add for more than one path"
        ),
    )
    rescope.add_argument(
        "--drop",
        action="append",
        help=(
            "an absolute or repository-relative path to drop; repeat --drop for more than one path"
        ),
    )
    rescope.add_argument("--claim-id", help=EXPECTED_CLAIM_ID_HELP)
    rescope.add_argument(
        "--whole",
        metavar="REASON",
        help=WHOLE_HELP,
    )
    _add_json_flag(rescope)


def _add_cut_parser(commands: _Subcommands, name: str) -> None:
    cut = commands.add_parser(name, help="create a container's next slice as a fresh child issue")
    cut.add_argument("issue", type=board.parse_item_reference, help="the container to cut")
    cut.add_argument(
        "--title",
        type=_nonblank_title,
        help=(
            "the fresh child issue's title; default is the linked slice row's own title, "
            "which a given --title must match; required when no row is linked"
        ),
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


def _add_ask_parser(commands: _Subcommands, name: str) -> None:
    ask = commands.add_parser(name, help="append one proposed expectation line to an item's block")
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


def _add_rule_parser(commands: _Subcommands, name: str) -> None:
    rule = commands.add_parser(
        name, help="rule one proposed expectation line, transcribing the operator's word"
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


def _add_check_parser(commands: _Subcommands, name: str) -> None:
    check = commands.add_parser(
        name,
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


def _add_body_parser(commands: _Subcommands, name: str) -> None:
    body_parser = commands.add_parser(
        name,
        help=(
            f"check a piped body's fenced {body.BLOCK_FENCE_INFO} block for defects "
            "before the forge"
        ),
    )
    body_parser.add_argument(
        "--check",
        action="store_true",
        required=True,
        help="read a body from stdin and report its defects",
    )
    _add_json_flag(body_parser)


def _add_brief_parser(commands: _Subcommands, name: str) -> None:
    brief = commands.add_parser(
        name,
        help="print one item's body, live claim, lane tip and touched files for a dispatch",
    )
    brief.add_argument("item", type=board.parse_item_reference, help="the work item to brief")
    brief.add_argument(
        "--step",
        choices=[step.value for step in board.BriefStep],
        default=None,
        help=f"also print this lane step's own rules and checks from {board.BRIEF_CONFIG_PATH}",
    )
    _add_json_flag(brief)


def _nonblank_title(value: str) -> str:
    """`item new`'s and `cut`'s `--title` argparse `type=` (issue #447):
    `value` unchanged unless `body.is_valid_title` -- the rule `record.title`
    is read by -- refuses it, before anything is read, created, or minted."""
    if not body.is_valid_title(value):
        raise protocol.ClaimUnavailableError("--title must be a non-empty string")
    return value


def _add_item_parser(commands: _Subcommands, name: str) -> None:
    item = commands.add_parser(name, help="create, show, edit, or close one work item")
    item_commands = _add_subcommands(item, "item_command")
    new = item_commands.add_parser(
        "new",
        help=(
            "create a fresh item and print its id; its body is read from stdin, a piped "
            f"body without an {body.BLOCK_FENCE_INFO} block getting one built from the flags"
        ),
    )
    for key, meaning in cmd_item.ITEM_NEW_PROJECTION_FLAGS:
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
        help="bind this lane to a foreign forge issue, e.g. gitlab#42",
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
    refuses_stdin = "reads no stdin; a piped or redirected body is refused"
    one_field.add_argument(
        "--size",
        choices=tuple(metrics.Size),
        help=f"set only this item's size class (any storage); {refuses_stdin}",
    )
    one_field.add_argument(
        "--whole",
        metavar="REASON",
        help=f"set only this item's whole reason (any storage); {refuses_stdin}",
    )
    one_field.add_argument(
        "--kind",
        choices=ITEM_EDIT_KINDS,
        help=f"set only this item's kind (any storage); {refuses_stdin}",
    )
    _add_json_flag(edit)
    close = item_commands.add_parser(
        "close", help="close a state-ref item; the file stays, next and board let it go"
    )
    close.add_argument(
        "item", type=board.parse_item_reference, help=f"the item to close, {ITEM_REF_HELP}"
    )
    _add_json_flag(close)


def _add_protect_parser(commands: _Subcommands, name: str) -> None:
    commands.add_parser(name, help="deny PreToolUse writes without this session's live claim")


def _add_register_parser(commands: _Subcommands, name: str) -> None:
    register = commands.add_parser(
        name,
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


def _add_run_parser(commands: _Subcommands, name: str) -> None:
    run = commands.add_parser(name, help="open the registered provider workspace consoles")
    run.add_argument("project", metavar="PROJECT", nargs="?", help="one registered project")


def _add_login_parser(commands: _Subcommands, name: str) -> None:
    login = commands.add_parser(name, help="manage configured workspace recovery at desktop login")
    login_commands = _add_subcommands(login, "login_command")
    login_commands.add_parser("enable", help="install the owned desktop login launcher")
    login_commands.add_parser("disable", help="remove the owned desktop login launcher")
    login_commands.add_parser(
        "status", help="show launcher, configuration, and latest attempt state"
    )


def _add_run_at_login_parser(commands: _Subcommands, name: str) -> None:
    commands.add_parser(name, help=argparse.SUPPRESS)


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


class _UsageErrorParser(argparse.ArgumentParser):
    """Every `aco` parser and subparser -- argparse hands this class down to
    each subparser it builds. Its own refusal path prints usage and exits
    inside the parse, before `--json` was ever read off a namespace, so this
    raises instead and lets one place decide the refusal's shape.

    No parser reads an abbreviated long option (issue #502, OUT-09): a
    prefix unique today targets another option once one is added, and
    `reset --conf --f` must never stand for a destructive flag pair.

    It also remembers what a refused parse would otherwise lose (issue #432):
    Python parses a subcommand into a throwaway namespace and copies the
    values back only once that parse succeeds, yet `--json` is exactly the
    flag the refusal's own shape depends on. So each parser keeps the tokens
    it was handed and the arguments declared on it, and `subcommands` names
    the parsers one level down."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.declared_actions: list[argparse.Action] = []
        self.received_tokens: tuple[str, ...] | None = None
        self.subcommands: _Subcommands | None = None
        super().__init__(*args, allow_abbrev=False, **kwargs)

    def add_argument(self, *args: Any, **kwargs: Any) -> argparse.Action:
        action = super().add_argument(*args, **kwargs)
        self.declared_actions.append(action)
        return action

    def parse_known_args(
        self, args: Iterable[str] | None = None, *rest: Any, **keywords: Any
    ) -> tuple[Any, list[str]]:
        if self.subcommands:
            self.subcommands.forget_received_tokens()
        self.received_tokens = tuple(sys.argv[1:] if args is None else args)
        return super().parse_known_args(self.received_tokens, *rest, **keywords)

    def error(self, message: str) -> NoReturn:
        raise _UsageError(self, message)


class _Subcommands:
    """The subcommand level of one `aco` parser: builders add their command
    through it, and a refused parse can ask which command was chosen (the
    one that was handed tokens)."""

    def __init__(self, parent: _UsageErrorParser, dest: str) -> None:
        self._action = parent.add_subparsers(dest=dest, required=True)

    def add_parser(self, name: str, **kwargs: Any) -> _UsageErrorParser:
        return self._action.add_parser(name, **kwargs)

    def chosen(self) -> tuple[_UsageErrorParser, tuple[str, ...]] | None:
        return next(
            (
                (parser, parser.received_tokens)
                for parser in self._action.choices.values()
                if parser.received_tokens is not None
            ),
            None,
        )

    def forget_received_tokens(self) -> None:
        """A reused parser must not report the command an earlier parse chose."""
        for parser in self._action.choices.values():
            parser.received_tokens = None
            if parser.subcommands:
                parser.subcommands.forget_received_tokens()


def _add_subcommands(parser: _UsageErrorParser, dest: str) -> _Subcommands:
    parser.subcommands = _Subcommands(parser, dest)
    return parser.subcommands


def _parser() -> _UsageErrorParser:
    parser = _UsageErrorParser(prog="aco", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--repo", help="GitHub repository as OWNER/REPO")
    commands = _add_subcommands(parser, "command")
    for name, command in _COMMANDS.items():
        command.add_parser(commands, name)
    return parser


BODY_TEMPLATE_KINDS = ("task", "feature", "container")
# `item edit --kind`'s targets: an item turns container with its first
# child and back to task once none is open (issue #503).
ITEM_EDIT_KINDS = (body.ItemKind.TASK.value, body.ItemKind.CONTAINER.value)
DEFAULT_BODY_TEMPLATE_KIND = "task"


STATE_REF_ITEM_PAST_THE_ID_SPACE = "{number} " + board.NAMES_NO_ITEM


class PreDispatchReason(StrEnum):
    """Reasons owned before command dispatch: agent identity resolution
    reports `precondition_failed`; parser usage and out-of-range state-ref
    item numbers report `invalid_usage`. Release owns its branch checks."""

    INVALID_USAGE = "invalid_usage"
    PRECONDITION_FAILED = "precondition_failed"


def _refuse_usage(error: _UsageError, *, as_json: bool) -> int:
    """The argument parser's own refusal, in the shape this invocation asked
    for (issue #432): without `--json` argparse's own usage block and
    sentence, printed by the implementation `_UsageErrorParser` overrode, so
    the bytes stay exactly what they were; with `--json` the shared envelope
    instead, `invalid_usage`, argparse's own sentence as `message`."""
    if not as_json:
        argparse.ArgumentParser.error(error.parser, str(error))
    return report.refuse(PreDispatchReason.INVALID_USAGE, error, as_json=True)


@dataclass(frozen=True)
class _RepositoryCommand:
    """A context-taking type keeps repository setup out of local and hook commands."""

    add_parser: Callable[[_Subcommands, str], None]
    run: Callable[[argparse.Namespace, RunContext], int]
    resolves_agent: bool = False


@dataclass(frozen=True)
class _LocalCommand:
    """A namespace-only type makes local execution independent of repository setup."""

    add_parser: Callable[[_Subcommands, str], None]
    run: Callable[[argparse.Namespace], int]


@dataclass(frozen=True)
class _NoArgumentsCommand:
    """A distinct type keeps `protect` inside its own exception boundary before
    any setup and lets the hidden login runner execute without reading a namespace."""

    add_parser: Callable[[_Subcommands, str], None]
    run: Callable[[], int]


_Command = _RepositoryCommand | _LocalCommand | _NoArgumentsCommand


_COMMANDS: dict[str, _Command] = {
    "bootstrap": _RepositoryCommand(
        _add_bootstrap_parser, lambda _parsed, context: cmd_bootstrap.run(context)
    ),
    "reset": _RepositoryCommand(_add_reset_parser, cmd_reset.run),
    "status": _RepositoryCommand(_add_status_parser, cmd_status.run),
    "board": _RepositoryCommand(_add_board_parser, cmd_board.run),
    "rulings": _RepositoryCommand(_add_rulings_parser, cmd_rulings.run),
    "next": _RepositoryCommand(_add_next_parser, cmd_next.run),
    "start": _RepositoryCommand(_add_start_parser, cmd_start.run),
    "claim": _RepositoryCommand(_add_claim_parser, cmd_claim.run, resolves_agent=True),
    "release": _RepositoryCommand(_add_release_parser, cmd_release.run, resolves_agent=True),
    "land": _RepositoryCommand(_add_land_parser, cmd_land.run, resolves_agent=True),
    "rescope": _RepositoryCommand(_add_rescope_parser, cmd_rescope.run, resolves_agent=True),
    "cut": _RepositoryCommand(_add_cut_parser, cmd_cut.run),
    "ask": _RepositoryCommand(_add_ask_parser, cmd_ask.run),
    "rule": _RepositoryCommand(_add_rule_parser, cmd_rule.run),
    "check": _RepositoryCommand(_add_check_parser, cmd_check.run),
    "body": _RepositoryCommand(_add_body_parser, cmd_body.run),
    "brief": _RepositoryCommand(_add_brief_parser, cmd_brief.run),
    "item": _RepositoryCommand(_add_item_parser, cmd_item.run),
    "protect": _NoArgumentsCommand(_add_protect_parser, cmd_protect.run),
    "register": _LocalCommand(_add_register_parser, cmd_workspace.register_workspace),
    "run": _LocalCommand(_add_run_parser, cmd_workspace.run_workspace),
    "login": _LocalCommand(_add_login_parser, cmd_workspace.manage_login),
    "_run-at-login": _NoArgumentsCommand(_add_run_at_login_parser, cmd_workspace.run_at_login),
}


def _resolve_agent_or_refuse(parsed: argparse.Namespace) -> int | None:
    """Only identity failures own precondition_failed; handler failures do not."""
    try:
        parsed.agent = checkout.resolved_agent(parsed.agent)
    except protocol.ClaimError as error:
        return report.refuse(
            PreDispatchReason.PRECONDITION_FAILED,
            error,
            as_json=bool(getattr(parsed, "json", False)),
        )
    return None


def _spells_json_flag(token: str, parser: _UsageErrorParser) -> bool:
    """Whether `parser` itself would read `token` as its own `--json`: the
    exact spelling on a parser that declares it, never an abbreviation, since
    no `aco` parser reads one (OUT-09)."""
    spelling = token.split("=", 1)[0]
    return spelling == report.JSON_FLAG and any(
        report.JSON_FLAG in action.option_strings for action in parser.declared_actions
    )


def _level_options(tokens: tuple[str, ...]) -> tuple[str, ...]:
    """The tokens one parser level still reads as options. A bare `--` ends
    that level's options and nothing else's: argparse marks the rest of this
    level's tokens as non-options, then hands what follows the command name
    down untouched, and the next level scans it for a `--` of its own."""
    return tokens[: tokens.index(LONG_OPTION_PREFIX)] if LONG_OPTION_PREFIX in tokens else tokens


def _asked_for_json(root: _UsageErrorParser, given: list[str]) -> bool:
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
    root: _UsageErrorParser, given: list[str]
) -> Iterator[tuple[_UsageErrorParser, tuple[str, ...]]]:
    """Each parser this invocation's parse reached, root first, with the
    tokens argparse handed that level (issue #432)."""
    parser, tokens = root, tuple(given)
    while True:
        yield parser, tokens
        level = parser.subcommands.chosen() if parser.subcommands else None
        if level is None:
            return
        parser, tokens = level


# Every argparse `type=` that reads an item reference. `check`'s subject also
# reads a trunk commit id, which parses to a string and so names no item.
_ITEM_ARGUMENT_TYPES = frozenset({board.parse_item_reference, _parse_check_subject})


def _item_arguments(
    root: _UsageErrorParser, given: list[str], parsed: argparse.Namespace
) -> Iterator[int]:
    """Every item number this invocation named: the parsed integer of each
    argument an `_ITEM_ARGUMENT_TYPES` parser types, on every parser the
    parse reached."""
    for parser, _tokens in _reached_levels(root, given):
        for action in parser.declared_actions:
            value = getattr(parsed, action.dest, None)
            if action.type in _ITEM_ARGUMENT_TYPES and isinstance(value, int):
                yield value


def _refuse_item_past_the_id_space(
    root: _UsageErrorParser,
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
    return report.refuse(
        PreDispatchReason.INVALID_USAGE, error, as_json=_asked_for_json(root, given)
    )


def _pins_state_ref(context: RunContext) -> bool:
    """Whether this checkout's readable storage pin says `state-ref`; an
    unreadable pin says nothing here, so its refusal stays the command's."""
    try:
        return context.config.storage is body.Storage.STATE_REF
    except protocol.ClaimError:
        return False


def _run_repository_command(
    command: _RepositoryCommand,
    parser: _UsageErrorParser,
    given: list[str],
    parsed: argparse.Namespace,
) -> int:
    context = session.run_context(parsed.repo)
    refusal = _refuse_item_past_the_id_space(parser, given, parsed, context)
    if refusal is None and command.resolves_agent:
        refusal = _resolve_agent_or_refuse(parsed)
    return refusal if refusal is not None else command.run(parsed, context)


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
        return report.refuse(
            PreDispatchReason.INVALID_USAGE, error, as_json=_asked_for_json(parser, given)
        )
    try:
        command = _COMMANDS[parsed.command]
        match command:
            case _NoArgumentsCommand():
                return command.run()
            case _LocalCommand():
                return command.run(parsed)
        return _run_repository_command(command, parser, given, parsed)
    except protocol.ClaimError as error:
        return report.render_error(error)


if __name__ == "__main__":
    raise SystemExit(main())
