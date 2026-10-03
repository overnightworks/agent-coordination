"""`DecisionPort` over the installed `aco` command line.

Reads with `aco rulings --json` (specs/rulings.spec.md) and writes with
`aco rule ITEM --line N --yes|--no [--note TEXT] --json` (specs/rule.spec.md).
Every call runs in the directory the board was started in, so the board only
ever sees that repository, and passes its arguments as a list, never through
a shell.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from .ports import (
    Decision,
    DecisionResult,
    DecisionSourceUnavailableError,
    DecisionStatus,
    ExpectationLine,
)

ACO_EXECUTABLE = "aco"
_TIMEOUT_SECONDS = 120
"""`aco rulings` builds the whole board projection, which can take tens of
seconds against a forge; a call still running after this is treated as hung."""
_ALREADY_RULED_REASON = "already_ruled"

_Field = TypeVar("_Field")


@dataclass(frozen=True)
class _Completed:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class AcoCli:
    directory: Path

    def expectation_lines(self) -> tuple[ExpectationLine, ...]:
        completed = self._run(["rulings", "--json"])
        envelope = _envelope(completed)
        if completed.exit_code != 0 or envelope.get("ok") is not True:
            raise DecisionSourceUnavailableError(
                _failure_sentence("aco rulings", completed, envelope)
            )
        return _lines_from_rulings(envelope.get("rulings"))

    def rule(self, decision: Decision) -> DecisionResult:
        arguments = [
            "rule",
            str(decision.item),
            "--line",
            str(decision.index),
            f"--{decision.outcome.value}",
        ]
        if decision.note is not None:
            arguments += ["--note", decision.note]
        try:
            completed = self._run([*arguments, "--json"])
        except DecisionSourceUnavailableError as error:
            return DecisionResult(DecisionStatus.FAILED, str(error))
        envelope = _envelope(completed)
        if completed.exit_code == 0 and envelope.get("ok") is True:
            return DecisionResult(DecisionStatus.RULED)
        if envelope.get("reason") == _ALREADY_RULED_REASON:
            return DecisionResult(DecisionStatus.ALREADY_RULED, _message(envelope))
        return DecisionResult(
            DecisionStatus.FAILED, _failure_sentence("aco rule", completed, envelope)
        )

    def _run(self, arguments: list[str]) -> _Completed:
        try:
            completed = subprocess.run(
                [ACO_EXECUTABLE, *arguments],
                cwd=self.directory,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError as error:
            raise DecisionSourceUnavailableError("aco is not installed on PATH") from error
        except subprocess.TimeoutExpired as error:
            raise DecisionSourceUnavailableError(
                f"aco {arguments[0]} did not answer within {_TIMEOUT_SECONDS} seconds"
            ) from error
        return _Completed(completed.returncode, completed.stdout, completed.stderr)


def _envelope(completed: _Completed) -> dict[str, object]:
    """aco's `--json` object, or an empty one when stdout carries none."""
    try:
        parsed: object = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}
    return _as_object(parsed) or {}


def _message(envelope: dict[str, object]) -> str | None:
    message = envelope.get("message")
    return message if isinstance(message, str) else None


def _failure_sentence(command: str, completed: _Completed, envelope: dict[str, object]) -> str:
    explanation = _message(envelope) or completed.stderr.strip()
    if not explanation:
        explanation = f"exit {completed.exit_code} without a message"
    return f"{command} failed: {explanation}"


def _lines_from_rulings(rows: object) -> tuple[ExpectationLine, ...]:
    if not isinstance(rows, list):
        raise _unreadable("rulings is not a list")
    return tuple(line for row in rows for line in _lines_from_row(row))


def _lines_from_row(row: object) -> tuple[ExpectationLine, ...]:
    fields = _as_object(row)
    if fields is None:
        raise _unreadable("a rulings row is not an object")
    item = _required(fields, "number", int)
    title = _required(fields, "title", str)
    lines = fields.get("lines")
    if not isinstance(lines, list):
        raise _unreadable(f"#{item} lines is not a list")
    return tuple(_line(item, title, line) for line in lines)


def _line(item: int, title: str, line: object) -> ExpectationLine:
    fields = _as_object(line)
    if fields is None:
        raise _unreadable(f"#{item} has a line that is not an object")
    return ExpectationLine(
        item=item,
        item_title=title,
        index=_required(fields, "index", int),
        text=_required(fields, "text", str),
        is_open=fields.get("ruling") is None,
        question=_optional_text(fields, "question"),
        example=_optional_text(fields, "example"),
        picture=_optional_text(fields, "picture"),
    )


def _as_object(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): field for key, field in value.items()}


def _required(fields: dict[str, object], key: str, kind: type[_Field]) -> _Field:
    value = fields.get(key)
    # bool is an int subclass; a JSON true is never an item or line number.
    if not isinstance(value, kind) or isinstance(value, bool):
        raise _unreadable(f"{key} is not a {kind.__name__}")
    return value


def _optional_text(fields: dict[str, object], key: str) -> str | None:
    value = fields.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _unreadable(f"{key} is not text")
    return value


def _unreadable(detail: str) -> DecisionSourceUnavailableError:
    return DecisionSourceUnavailableError(f"aco rulings --json is unreadable: {detail}")
