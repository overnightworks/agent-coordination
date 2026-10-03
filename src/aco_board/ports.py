"""The board's one narrow interface to its decision source.

The board reads expectation lines and writes rulings through `DecisionPort`
only; `aco_cli.AcoCli` is its one implementation. Truth stays with the
source: the board keeps no copy of a line or a ruling.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class Outcome(StrEnum):
    YES = "yes"
    NO = "no"


@dataclass(frozen=True)
class ExpectationLine:
    item: int
    item_title: str
    index: int
    text: str
    is_open: bool
    question: str | None
    example: str | None
    picture: str | None

    @property
    def fingerprint(self) -> str:
        """Names exactly what a card shows the operator: every field it renders."""
        shown = json.dumps(
            [
                self.item,
                self.item_title,
                self.index,
                self.text,
                self.question,
                self.example,
                self.picture,
            ],
            ensure_ascii=False,
        )
        return hashlib.sha256(shown.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True)
class Decision:
    item: int
    index: int
    outcome: Outcome
    note: str | None


class DecisionStatus(StrEnum):
    RULED = "ruled"
    ALREADY_RULED = "already_ruled"
    FAILED = "failed"


@dataclass(frozen=True)
class DecisionResult:
    status: DecisionStatus
    message: str | None = None


class DecisionSourceUnavailableError(Exception):
    """The source could not be read; the message says why in one sentence."""


class DecisionPort(Protocol):
    def expectation_lines(self) -> tuple[ExpectationLine, ...]:
        """Every expectation line, open and ruled, in the source's own order.

        Raises `DecisionSourceUnavailableError` when the source cannot answer.
        """
        ...

    def rule(self, decision: Decision) -> DecisionResult:
        """Write one decision; the source itself refuses an already-ruled line,
        an unknown item, or a line the item does not have."""
        ...
