#!/usr/bin/env python3
"""One-time rewrite of GitHub item bodies from the ```agent-claim fence to ```aco (issue #587).

``--dry-run --repo OWNER/REPO ... --manifest FILE`` reads every issue of the named
repositories (open and closed, no pull requests) and writes one manifest row per body to
change; it writes nothing on GitHub. ``--apply --manifest FILE`` patches each row whose fresh
body still has the row's old hash, reads the body back, and stops at the first drift. Rows
whose body already has the new hash count as done, so a stopped apply resumes from the same
manifest.

Only one shape is rewritten: exactly one opening line reading exactly ```agent-claim, which
becomes ```aco. Every other shape is refused and named, never handled.

The shape check and the rewrite are pure. GitHub sits behind the injected ``run`` callable
(gh arguments in, ``gh api --include`` output out) and time behind the injected ``Clock``,
so tests drive both with fakes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from enum import StrEnum
from functools import partial
from http import HTTPStatus
from pathlib import Path
from typing import Protocol

OLD_FENCE_LINE = "```agent-claim"
NEW_FENCE_INFO = "aco"
NEW_FENCE_LINE = f"```{NEW_FENCE_INFO}"
PACE_SECONDS = 8.0
# GitHub's advice when a rate-limited answer names neither retry-after nor a reset time.
FALLBACK_RATE_LIMIT_WAIT_SECONDS = 60.0
MAX_RATE_LIMIT_WAITS = 5
PAGE_SIZE = 100
GH_TIMEOUT_SECONDS = 60
# The shell's code for a run ended by SIGINT (128 + 2).
INTERRUPTED_EXIT_CODE = 130

_FENCE = re.compile(r"^(?P<indent> {0,3})(?P<marker>`{3,}|~{3,})(?P<info>.*)$")
# A fence opening anywhere a reader would see one: after a byte-order mark, any indent and
# any blockquote or list-item markers. A backtick run followed by another backtick on its line
# is inline code, which CommonMark never reads as a fence.
_MENTION_PREFIX = r"^\ufeff?(?:\s|>|[-*+]\s|\d{1,9}[.)]\s)*(?:`{3,}(?=[^`]*$)|~{3,})\s*"
_PROTOCOL_MENTION = re.compile(_MENTION_PREFIX + "agent-claim")
_HEADER_END = re.compile(r"\r?\n\r?\n")
# aco's own OWNER/REPO judge (`github.repository_id`), repeated here because #587 line 6
# keeps this script free of aco imports.
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}")
_RESERVED_REPOSITORY_NAMES = frozenset({".", ".."})
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_RATE_LIMIT_STATUSES = frozenset({HTTPStatus.FORBIDDEN, HTTPStatus.TOO_MANY_REQUESTS})


class Refusal(StrEnum):
    FOREIGN_LINE_BREAK = (
        "the body has line breaks other than \\n"
        " (CR, CRLF, VT, FF, FS, GS, RS, NEL, U+2028 or U+2029)"
    )
    BYTE_ORDER_MARK = "the agent-claim opening line starts with a byte-order mark (U+FEFF)"
    TILDE_FENCE = "the agent-claim fence uses tildes"
    INEXACT_OPENING_LINE = "the agent-claim opening line does not read exactly ```agent-claim"
    INSIDE_ANOTHER_FENCE = "an agent-claim fence sits inside another fenced block"
    UNCLOSED_FENCE = "the agent-claim fence is never closed"
    SEVERAL_FENCES = "the body has more than one agent-claim fence"
    MIXED_FENCES = "the body already has an aco fence next to the agent-claim fence"


@dataclass(frozen=True)
class Rewrite:
    body: str


@dataclass(frozen=True)
class FencedBlock:
    opening_index: int
    closing_index: int | None


@dataclass(frozen=True)
class Issue:
    repository: str
    number: int
    body: str


@dataclass(frozen=True)
class ManifestRow:
    repository: str
    number: int
    old_hash: str
    new_hash: str


@dataclass(frozen=True)
class ApiResponse:
    status: int
    headers: Mapping[str, str]
    body: str


class MigrationStoppedError(Exception):
    """The run cannot go on without risking a wrong write; the message names where and why."""


class _ResendDeclinedError(Exception):
    """The caller declined to resend a request after a rate-limit wait."""


class GhRun(Protocol):
    def __call__(self, arguments: list[str], *, input_data: bytes | None = None) -> str: ...


class Clock(Protocol):
    def now(self) -> float:
        """Epoch seconds, the scale of GitHub's rate-limit reset times."""
        ...

    def monotonic(self) -> float:
        """Seconds that only move forward, the scale for measuring waits."""
        ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def body_hash(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def item_reference(repository: str, number: int) -> str:
    return f"{repository}#{number}"


def report_progress(line: str) -> None:
    # Flushed at once, so the operator watching a live run sees each step as it happens and a
    # killed run leaves its record of what was patched.
    print(line, flush=True)


def classify(body: str) -> Rewrite | Refusal | None:
    """The rewrite of a body, the reason it is refused, or None when it names no fence."""
    lines = body.split("\n")
    # aco reads lines with `str.splitlines`, which also breaks at CR, VT, FF, FS-RS, NEL and
    # U+2028/9. A mention on either view counts, so one behind such a break is refused, not
    # skipped; any such break is refused, because there the two readers see different blocks.
    reader_lines = body.splitlines()
    if not any(_PROTOCOL_MENTION.match(line) for line in (*lines, *reader_lines)):
        return None
    if reader_lines != body.removesuffix("\n").split("\n"):
        return Refusal.FOREIGN_LINE_BREAK
    mentions = [index for index, line in enumerate(lines) if _PROTOCOL_MENTION.match(line)]
    blocks = {block.opening_index: block for block in fenced_blocks(lines)}
    for index in mentions:
        refusal = _mention_refusal(lines[index], blocks.get(index))
        if refusal is not None:
            return refusal
    if len(mentions) > 1:
        return Refusal.SEVERAL_FENCES
    # The rewrite would leave two aco blocks, a body the aco contract refuses.
    if any(_is_aco_opening(lines[index]) for index in blocks):
        return Refusal.MIXED_FENCES
    (opening_index,) = mentions
    lines[opening_index] = NEW_FENCE_LINE
    return Rewrite("\n".join(lines))


def _is_aco_opening(line: str) -> bool:
    """aco reads a fenced block as its own when the info string, stripped of spaces and tabs,
    is exactly its name (`body._agent_claim_fence_matches`)."""
    opening = _FENCE.match(line)
    return opening is not None and opening["info"].strip(" \t") == NEW_FENCE_INFO


def _mention_refusal(line: str, block: FencedBlock | None) -> Refusal | None:
    if line.startswith("\ufeff"):
        return Refusal.BYTE_ORDER_MARK
    if block is None:
        return Refusal.INSIDE_ANOTHER_FENCE if _FENCE.match(line) else Refusal.INEXACT_OPENING_LINE
    if line.lstrip().startswith("~"):
        return Refusal.TILDE_FENCE
    if line != OLD_FENCE_LINE:
        return Refusal.INEXACT_OPENING_LINE
    if block.closing_index is None:
        return Refusal.UNCLOSED_FENCE
    return None


def fenced_blocks(lines: Sequence[str]) -> list[FencedBlock]:
    """CommonMark fenced code blocks: a fence closes on a line of its own marker character,
    at least as long as its opening marker, followed by nothing but spaces or tabs.

    The rule is aco's own (`body.FENCE_CLOSING_PATTERN`), repeated here because #587 line 6
    keeps this script free of aco imports."""
    blocks: list[FencedBlock] = []
    index = 0
    while index < len(lines):
        opening = _FENCE.match(lines[index])
        if opening is None:
            index += 1
            continue
        closing_index = _closing_index(lines, index, opening["marker"])
        blocks.append(FencedBlock(index, closing_index))
        if closing_index is None:
            break
        index = closing_index + 1
    return blocks


def _closing_index(lines: Sequence[str], opening_index: int, marker: str) -> int | None:
    for index in range(opening_index + 1, len(lines)):
        closing = _FENCE.match(lines[index])
        if (
            closing is not None
            and closing["marker"][0] == marker[0]
            and len(closing["marker"]) >= len(marker)
            and not closing["info"].strip(" \t")
        ):
            return index
    return None


def parse_included_response(output: str) -> ApiResponse:
    """Split `gh api --include` output into status line, headers and body."""
    if not output.startswith("HTTP/"):
        raise MigrationStoppedError(f"gh gave no HTTP response: {output[:200]}")
    head, body = _HEADER_END.split(output, maxsplit=1)
    status_line, *header_lines = head.splitlines()
    headers = {
        name.strip().lower(): value.strip()
        for name, _, value in (line.partition(":") for line in header_lines)
    }
    return ApiResponse(int(status_line.split()[1]), headers, body)


def rate_limit_wait(response: ApiResponse, now: float) -> float | None:
    """Seconds to wait before retrying a rate-limited answer, or None when it is not one.

    Follows GitHub's REST guidance: retry-after first, then the primary limit's reset time,
    then a minute for a 429 or for a 403 whose message names the secondary rate limit; any
    other 403 is a refused permission, not a rate limit."""
    if response.status not in _RATE_LIMIT_STATUSES:
        return None
    retry_after = response.headers.get("retry-after")
    if retry_after is not None:
        return float(retry_after)
    if response.headers.get("x-ratelimit-remaining") == "0":
        return max(float(response.headers["x-ratelimit-reset"]) - now, 0.0)
    if response.status == HTTPStatus.TOO_MANY_REQUESTS or _names_secondary_limit(response):
        return FALLBACK_RATE_LIMIT_WAIT_SECONDS
    return None


def _names_secondary_limit(response: ApiResponse) -> bool:
    return "secondary rate limit" in response.body.lower()


def run_gh(arguments: list[str], *, input_data: bytes | None = None) -> str:
    """Run gh and return its stdout even on a non-zero exit: with --include the HTTP status
    of a refused request is printed there, and the caller decides what it means."""
    try:
        completed = subprocess.run(
            ["gh", *arguments],
            input=input_data,
            capture_output=True,
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as timeout:
        raise MigrationStoppedError(
            f"gh {' '.join(arguments)} did not answer within {GH_TIMEOUT_SECONDS} s"
        ) from timeout
    if not completed.stdout:
        raise MigrationStoppedError(f"gh {' '.join(arguments)} failed: {completed.stderr.decode()}")
    return completed.stdout.decode()


class GitHubApi:
    def __init__(self, run: GhRun, clock: Clock) -> None:
        self._run = run
        self._clock = clock

    def issues(self, repository: str) -> Iterator[Issue]:
        page = 1
        while True:
            path = f"repos/{repository}/issues?state=all&per_page={PAGE_SIZE}&page={page}"
            items = json.loads(self._request("GET", path).body)
            for item in items:
                if "pull_request" not in item:
                    yield Issue(repository, item["number"], item["body"] or "")
            if len(items) < PAGE_SIZE:
                return
            page += 1

    def issue_body(self, repository: str, number: int) -> str:
        item = json.loads(self._request("GET", f"repos/{repository}/issues/{number}").body)
        return item["body"] or ""

    def update_body(
        self, repository: str, number: int, body: str, *, resend_wanted: Callable[[], bool]
    ) -> bool:
        """PATCH the body of an issue; False when a rate-limit wait ended with `resend_wanted`
        declining the resend, so the PATCH was not sent again.

        GitHub's issue update takes no precondition, so the caller's check cannot travel with
        the write; `resend_wanted` lets the caller look again after every rate-limit wait."""
        # Only the body travels, so labels, type, assignees, state and comments stay as they are.
        payload = json.dumps({"body": body}).encode()
        path = f"repos/{repository}/issues/{number}"
        try:
            self._request("PATCH", path, payload, resend_wanted=resend_wanted)
        except _ResendDeclinedError:
            return False
        return True

    def _request(
        self,
        method: str,
        path: str,
        payload: bytes | None = None,
        *,
        resend_wanted: Callable[[], bool] = lambda: True,
    ) -> ApiResponse:
        arguments = ["api", "--include", "--method", method, path]
        if payload is not None:
            arguments += ["--input", "-"]
        for waits_done in range(MAX_RATE_LIMIT_WAITS + 1):
            response = parse_included_response(self._run(arguments, input_data=payload))
            wait = rate_limit_wait(response, self._clock.now())
            if wait is None:
                return _successful(response, method, path)
            if waits_done < MAX_RATE_LIMIT_WAITS:
                report_progress(f"waiting {wait:g}s: rate limited on {method} {path}")
                self._clock.sleep(wait)
                if not resend_wanted():
                    raise _ResendDeclinedError
        raise MigrationStoppedError(
            f"{method} {path} still rate-limited after {MAX_RATE_LIMIT_WAITS} waits"
        )


def _successful(response: ApiResponse, method: str, path: str) -> ApiResponse:
    if response.status != HTTPStatus.OK:
        raise MigrationStoppedError(
            f"GitHub answered {response.status} to {method} {path}: {_github_message(response)}"
        )
    return response


def _github_message(response: ApiResponse) -> str:
    """GitHub's own explanation of a refused request, or the start of its raw answer."""
    try:
        answer = json.loads(response.body)
    except ValueError:
        answer = None
    if isinstance(answer, dict) and isinstance(answer.get("message"), str):
        return answer["message"]
    return response.body[:200]


def dry_run(api: GitHubApi, repositories: Sequence[str], manifest: Path) -> None:
    rows: list[ManifestRow] = []
    refused = 0
    for repository in repositories:
        for issue in api.issues(repository):
            outcome = classify(issue.body)
            if isinstance(outcome, Refusal):
                refused += 1
                report_progress(f"refused {item_reference(repository, issue.number)}: {outcome}")
            elif isinstance(outcome, Rewrite):
                rows.append(
                    ManifestRow(
                        repository, issue.number, body_hash(issue.body), body_hash(outcome.body)
                    )
                )
    try:
        manifest.write_text(json.dumps([asdict(row) for row in rows], indent=2) + "\n")
    except OSError as error:
        raise MigrationStoppedError(f"cannot write the manifest {manifest}: {error}") from error
    report_progress(
        f"{len(rows)} bodies to change, {refused} refused; manifest written to {manifest}"
    )


def read_manifest(manifest: Path) -> list[ManifestRow]:
    try:
        entries = json.loads(manifest.read_text())
    except (OSError, ValueError) as error:
        raise MigrationStoppedError(f"cannot read the manifest {manifest}: {error}") from error
    if not isinstance(entries, list):
        raise MigrationStoppedError(f"the manifest {manifest} is not a list of rows")
    return [_manifest_row(position, entry) for position, entry in enumerate(entries, start=1)]


def _manifest_row(position: int, entry: object) -> ManifestRow:
    """The manifest alone names what --apply patches, so a damaged row stops the run."""
    if not (
        isinstance(entry, dict)
        and entry.keys() == {field.name for field in fields(ManifestRow)}
        and _is_repository(entry["repository"])
        and isinstance(entry["number"], int)
        and not isinstance(entry["number"], bool)
        and entry["number"] > 0
        and all(
            isinstance(entry[key], str) and _SHA256_HEX.fullmatch(entry[key])
            for key in ("old_hash", "new_hash")
        )
    ):
        raise MigrationStoppedError(
            f"manifest row {position} is not a repository, number and two hashes: {entry!r}"
        )
    return ManifestRow(**entry)


class RowState(StrEnum):
    PENDING = "pending"
    MIGRATED = "migrated"


def row_state(row: ManifestRow, body: str) -> RowState:
    """Where a manifest row stands, given its freshly read body; a body with neither of the
    row's hashes changed since the dry run, and the run stops there."""
    current_hash = body_hash(body)
    if current_hash == row.new_hash:
        return RowState.MIGRATED
    if current_hash != row.old_hash:
        raise MigrationStoppedError(
            f"{item_reference(row.repository, row.number)}: the body changed since the dry run"
        )
    return RowState.PENDING


def apply(api: GitHubApi, clock: Clock, rows: Sequence[ManifestRow], pace_seconds: float) -> None:
    migrated = 0
    last_patch_at: float | None = None
    for row in rows:
        reference = item_reference(row.repository, row.number)
        # The pace runs out before the fresh read, so the read stays right before the PATCH.
        if last_patch_at is not None:
            _wait_out_pace(clock, last_patch_at + pace_seconds)
        body = api.issue_body(row.repository, row.number)
        patched = False
        if row_state(row, body) is RowState.PENDING:
            patched = api.update_body(
                row.repository,
                row.number,
                _migrated_body(reference, body),
                resend_wanted=partial(_still_pending, api, row),
            )
            last_patch_at = clock.monotonic()
        if not patched:
            report_progress(f"already migrated {reference}")
            continue
        migrated += 1
        if body_hash(api.issue_body(row.repository, row.number)) != row.new_hash:
            raise MigrationStoppedError(
                f"{reference}: the body read back does not have the new hash"
            )
        report_progress(f"migrated {reference}")
    report_progress(f"{migrated} migrated, {len(rows) - migrated} already migrated")


def _still_pending(api: GitHubApi, row: ManifestRow) -> bool:
    return row_state(row, api.issue_body(row.repository, row.number)) is RowState.PENDING


def _wait_out_pace(clock: Clock, next_patch_at: float) -> None:
    remaining = next_patch_at - clock.monotonic()
    if remaining > 0:
        clock.sleep(remaining)


def _migrated_body(reference: str, body: str) -> str:
    outcome = classify(body)
    if not isinstance(outcome, Rewrite):
        raise MigrationStoppedError(f"{reference}: the body no longer has the one migratable shape")
    return outcome.body


def _repository(value: str) -> str:
    if not _is_repository(value):
        raise argparse.ArgumentTypeError(f"{value!r} is not OWNER/REPO")
    return value


def _is_repository(value: object) -> bool:
    if not isinstance(value, str) or _REPOSITORY.fullmatch(value) is None:
        return False
    _, _, name = value.partition("/")
    return name not in _RESERVED_REPOSITORY_NAMES


def _pace_seconds(value: str) -> float:
    refusal = argparse.ArgumentTypeError(f"{value!r} is not a positive number of seconds")
    try:
        seconds = float(value)
    except ValueError:
        raise refusal from None
    if not (math.isfinite(seconds) and seconds > 0):
        raise refusal
    return seconds


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rewrite the ```agent-claim fence in GitHub issue bodies to ```aco."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="read and write the manifest")
    mode.add_argument("--apply", action="store_true", help="patch the manifest's rows")
    parser.add_argument(
        "--repo",
        action="append",
        type=_repository,
        default=[],
        help="a repository to read (--dry-run only; repeat for several)",
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--pace-seconds",
        type=_pace_seconds,
        default=PACE_SECONDS,
        help=f"wait between two PATCHes (default {PACE_SECONDS:g})",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    run: GhRun = run_gh,
    clock: Clock | None = None,
) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.dry_run and not arguments.repo:
        parser.error("--dry-run needs at least one --repo OWNER/REPO")
    # GitHub reads OWNER/REPO without regard to case, so Owner/Repo and owner/repo are one.
    if len({repository.casefold() for repository in arguments.repo}) < len(arguments.repo):
        parser.error("--repo names the same repository more than once")
    if arguments.apply and arguments.repo:
        parser.error("--apply takes its repositories from the manifest; --repo is for --dry-run")
    active_clock = clock if clock is not None else SystemClock()
    api = GitHubApi(run, active_clock)
    try:
        if arguments.dry_run:
            dry_run(api, arguments.repo, arguments.manifest)
        else:
            apply(api, active_clock, read_manifest(arguments.manifest), arguments.pace_seconds)
    except MigrationStoppedError as stopped:
        print(f"stopped: {stopped}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("stopped: interrupted", file=sys.stderr)
        return INTERRUPTED_EXIT_CODE
    return 0


if __name__ == "__main__":
    sys.exit(main())
