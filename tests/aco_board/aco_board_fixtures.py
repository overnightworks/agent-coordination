"""A fake `aco` on PATH and a client for the decision board.

The fake answers `aco rulings --json` from `rulings.json` in its working
directory and applies `aco rule` to that same file, the way aco itself moves
a line to ruled, so a test drives the board's real adapter and server against
state it controls. Every invocation is appended to `aco-calls.jsonl`.
"""

from __future__ import annotations

import http.client
import json
import os
import stat
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

_FAKE_ACO = """\
import json, pathlib, sys

repository = pathlib.Path.cwd()
arguments = sys.argv[1:]
with (repository / "aco-calls.jsonl").open("a", encoding="utf-8") as calls:
    calls.write(json.dumps(arguments) + "\\n")
rulings = repository / "rulings.json"

if arguments == ["rulings", "--json"]:
    failure = repository / "rulings-failure.txt"
    if failure.exists():
        print(failure.read_text(encoding="utf-8"), file=sys.stderr)
        sys.exit(2)
    rows = json.loads(rulings.read_text(encoding="utf-8"))
    print(json.dumps({"ok": True, "reason": "listed", "rulings": rows}))
    sys.exit(0)

if arguments[0] == "rule":
    refusal = repository / "rule-refusal.json"
    if refusal.exists():
        print(refusal.read_text(encoding="utf-8"))
        sys.exit(2)
    item, index, ruling = int(arguments[1]), int(arguments[3]), arguments[4][2:]
    note = arguments[arguments.index("--note") + 1] if "--note" in arguments else None
    rows = json.loads(rulings.read_text(encoding="utf-8"))
    for row in rows:
        for line in row["lines"]:
            if row["number"] == item and line["index"] == index:
                line["ruling"], line["ruled_on"] = ruling, "2026-10-03"
                if note is not None:
                    line["text"] += " Anmerkung: " + note
    rulings.write_text(json.dumps(rows), encoding="utf-8")
    envelope = {"ok": True, "reason": "ruled", "item": item, "index": index,
                "ruling": ruling, "ruled_on": "2026-10-03", "open": 0}
    print(json.dumps(envelope))
    sys.exit(0)

sys.exit(2)
"""


def install_fake_aco(bin_directory: Path) -> None:
    bin_directory.mkdir(parents=True, exist_ok=True)
    executable = bin_directory / "aco"
    executable.write_text(f"#!{sys.executable}\n{_FAKE_ACO}", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)


def path_with(bin_directory: Path) -> str:
    return f"{bin_directory}{os.pathsep}{os.environ.get('PATH', '')}"


def line(index: int, text: str, *, ruling: str | None = None, **card: str) -> dict[str, object]:
    ruled_on = "2026-10-01" if ruling is not None else None
    return {"index": index, "text": text, "ruling": ruling, "ruled_on": ruled_on, **card}


def row(number: int, title: str, *lines: dict[str, object]) -> dict[str, object]:
    open_count = sum(1 for entry in lines if entry["ruling"] is None)
    return {
        "number": number,
        "title": title,
        "open": open_count,
        "total": len(lines),
        "lines": list(lines),
    }


@dataclass(frozen=True)
class FakeAcoRepository:
    directory: Path

    def set_rulings(self, *rows: dict[str, object]) -> None:
        (self.directory / "rulings.json").write_text(json.dumps(list(rows)), encoding="utf-8")

    def refuse_rule(self, reason: str, message: str) -> None:
        envelope = {"ok": False, "reason": reason, "message": message}
        (self.directory / "rule-refusal.json").write_text(json.dumps(envelope), encoding="utf-8")

    def fail_rulings(self, stderr: str) -> None:
        (self.directory / "rulings-failure.txt").write_text(stderr, encoding="utf-8")

    def calls(self) -> list[list[str]]:
        log = self.directory / "aco-calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(entry) for entry in log.read_text(encoding="utf-8").splitlines()]

    def rule_calls(self) -> list[list[str]]:
        return [call for call in self.calls() if call[0] == "rule"]


@dataclass(frozen=True)
class Response:
    status: int
    body: str
    content_security_policy: str | None

    def json(self) -> dict[str, object]:
        parsed: dict[str, object] = json.loads(self.body)
        return parsed


@dataclass(frozen=True)
class BoardClient:
    host: str
    port: int
    token: str

    @classmethod
    def from_url(cls, url: str) -> BoardClient:
        split = urlsplit(url)
        assert split.hostname is not None
        assert split.port is not None
        return cls(split.hostname, split.port, parse_qs(split.query)["t"][0])

    def get_page(self, token: str | None = None) -> Response:
        presented = self.token if token is None else token
        return self._request("GET", f"/?t={presented}")

    def get_page_without_token(self) -> Response:
        return self._request("GET", "/")

    def decide(
        self,
        item: int,
        line: int,
        outcome: str,
        *,
        note: str | None = None,
        token: str | None = None,
    ) -> Response:
        payload = {
            "token": self.token if token is None else token,
            "item": item,
            "line": line,
            "outcome": outcome,
            "note": note,
        }
        return self.post_raw(json.dumps(payload))

    def post_raw(self, body: str) -> Response:
        return self._request("POST", "/rule", body)

    def _request(self, method: str, path: str, body: str | None = None) -> Response:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=30)
        try:
            connection.request(method, path, body=body)
            response = connection.getresponse()
            return Response(
                status=response.status,
                body=response.read().decode("utf-8"),
                content_security_policy=response.getheader("Content-Security-Policy"),
            )
        finally:
            connection.close()


@dataclass
class Card:
    item: int
    line: int
    texts: dict[str, str] = field(default_factory=dict)
    image_sources: list[str] = field(default_factory=list)


class BoardPage(HTMLParser):
    """The page as a browser would build it: cards, elements, visible text."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.cards: list[Card] = []
        self.element_names: list[str] = []
        self.script_count = 0
        self.empty_state_shown = False
        self._text_class: str | None = None
        self._in_card = False
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.element_names.append(tag)
        css_class = attributes.get("class")
        if tag == "script":
            self.script_count += 1
        if tag == "article" and css_class == "card":
            item, line = attributes.get("data-item"), attributes.get("data-line")
            self.cards.append(Card(int(item or 0), int(line or 0)))
            self._in_card = True
        if tag == "img" and self._in_card:
            self.cards[-1].image_sources.append(attributes.get("src") or "")
        if tag == "p" and css_class == "empty" and "hidden" not in attributes:
            self.empty_state_shown = True
        self._text_class = css_class if tag == "p" and self._in_card else None

    def handle_data(self, data: str) -> None:
        if self._text_class is not None:
            texts = self.cards[-1].texts
            texts[self._text_class] = texts.get(self._text_class, "") + data

    def handle_endtag(self, tag: str) -> None:
        if tag == "p":
            self._text_class = None
        if tag == "article":
            self._in_card = False

    def card_keys(self) -> list[tuple[int, int]]:
        return [(card.item, card.line) for card in self.cards]
