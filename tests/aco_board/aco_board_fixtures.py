"""A fake `aco` on PATH and a client for the decision board.

The fake parses its arguments with argparse the way aco does, answers
`aco rulings --json` from `rulings.json` in its working directory, and applies
`aco rule` to that same file with aco's own refusals (specs/rule.spec.md:
already ruled, out of range, unknown item), so a test drives the board's real
adapter and server against state it controls. Every invocation is appended to
`aco-calls.jsonl`.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import stat
import struct
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

_FAKE_ACO = """\
import argparse, json, pathlib, sys, time

repository = pathlib.Path.cwd()
with (repository / "aco-calls.jsonl").open("a", encoding="utf-8") as calls:
    calls.write(json.dumps(sys.argv[1:]) + "\\n")
rulings = repository / "rulings.json"

parser = argparse.ArgumentParser(prog="aco")
commands = parser.add_subparsers(dest="command", required=True)
listing = commands.add_parser("rulings")
listing.add_argument("--json", action="store_true")
rule = commands.add_parser("rule")
rule.add_argument("item")
rule.add_argument("--line", type=int, required=True)
outcomes = rule.add_mutually_exclusive_group(required=True)
for outcome in ("yes", "no", "later"):
    outcomes.add_argument("--" + outcome, dest="ruling", action="store_const", const=outcome)
rule.add_argument("--note")
rule.add_argument("--json", action="store_true")
arguments = parser.parse_args()


def refuse(reason, message):
    print("ERROR: " + message, file=sys.stderr)
    print(json.dumps({"ok": False, "reason": reason, "message": message}))
    sys.exit(2)


def wait_if_asked(name):
    delay = repository / name
    if delay.exists():
        time.sleep(float(delay.read_text(encoding="utf-8")))


if arguments.command == "rulings":
    wait_if_asked("rulings-delay-seconds")
    failure = repository / "rulings-failure.txt"
    if failure.exists():
        print(failure.read_text(encoding="utf-8"), file=sys.stderr)
        sys.exit(2)
    stdout = repository / "rulings-stdout.txt"
    if stdout.exists():
        print(stdout.read_text(encoding="utf-8"))
        sys.exit(0)
    rows = json.loads(rulings.read_text(encoding="utf-8"))
    print(json.dumps({"ok": True, "reason": "listed", "rulings": rows}))
    sys.exit(0)

refusal = repository / "rule-refusal.json"
if refusal.exists():
    envelope = json.loads(refusal.read_text(encoding="utf-8"))
    refuse(envelope["reason"], envelope["message"])
rows = json.loads(rulings.read_text(encoding="utf-8"))
wait_if_asked("rule-delay-seconds")
row = next((row for row in rows if str(row["number"]) == arguments.item), None)
if row is None:
    refuse("invalid_item", "#" + arguments.item + " does not exist")
line = next((line for line in row["lines"] if line["index"] == arguments.line), None)
if line is None:
    refuse("line_out_of_range", "line %d out of range: this item has %d expectation line(s)"
           % (arguments.line, len(row["lines"])))
if line["ruling"] is not None:
    refuse("already_ruled",
           "line %d is already ruled; a changed ruling is a new line" % arguments.line)
line["ruling"], line["ruled_on"] = arguments.ruling, "2026-10-03"
if arguments.note is not None:
    line["text"] += " Anmerkung: " + arguments.note
rulings.write_text(json.dumps(rows), encoding="utf-8")
still_open = sum(1 for entry in row["lines"] if entry["ruling"] is None)
print(json.dumps({"ok": True, "reason": "ruled", "item": row["number"],
                  "index": arguments.line, "ruling": arguments.ruling,
                  "ruled_on": "2026-10-03", "open": still_open}))
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

    def answer_rulings_with(self, stdout: str) -> None:
        (self.directory / "rulings-stdout.txt").write_text(stdout, encoding="utf-8")

    def delay(self, command: str, seconds: float) -> None:
        """Hold every later `aco <command>` for `seconds` (rule: between read and write)."""
        (self.directory / f"{command}-delay-seconds").write_text(str(seconds), encoding="utf-8")

    def rulings_state(self) -> bytes:
        return (self.directory / "rulings.json").read_bytes()

    def stored_line(self, item: int, index: int) -> dict[str, object]:
        rows = json.loads(self.rulings_state())
        return next(
            line
            for row in rows
            if row["number"] == item
            for line in row["lines"]
            if line["index"] == index
        )

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


def _received_until_closed(connection: socket.socket) -> bytes:
    received = b""
    while chunk := connection.recv(65536):
        received += chunk
    return received


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
        card: Card,
        outcome: str,
        *,
        note: str | None = None,
        token: str | None = None,
    ) -> Response:
        """Click Ja or Nein on `card` the way the page script posts it."""
        payload = {
            "token": self.token if token is None else token,
            "item": card.item,
            "line": card.line,
            "fingerprint": card.fingerprint,
            "outcome": outcome,
            "note": note,
        }
        return self.post_raw(json.dumps(payload))

    def send_raw(self, request: bytes) -> bytes:
        """Send bytes as they are, beneath any HTTP client's own checks."""
        with self._connection() as connection:
            connection.sendall(request)
            connection.shutdown(socket.SHUT_WR)
            return _received_until_closed(connection)

    def send_unfinished(self, request: bytes) -> bytes:
        """Send the start of a request, keep the connection open, and return
        whatever the board answers before it closes the connection."""
        with self._connection() as connection:
            connection.sendall(request)
            return _received_until_closed(connection)

    @contextmanager
    def holding_unfinished(self, request: bytes) -> Iterator[None]:
        """Keep a connection open on the start of a request while inside."""
        with self._connection() as connection:
            connection.sendall(request)
            yield

    @contextmanager
    def holding_unread(self, request: bytes) -> Iterator[None]:
        """Send a whole request, then read none of the answer while inside."""
        with socket.socket() as connection:
            # A small receive window set before connecting, so the board's
            # write blocks instead of filling the kernel's buffers.
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            connection.connect((self.host, self.port))
            connection.sendall(request)
            yield

    def send_and_reset(self, request: bytes) -> None:
        """Send a whole request, then hang up with a reset before any answer."""
        with self._connection() as connection:
            connection.sendall(request)
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))

    def _connection(self) -> socket.socket:
        return socket.create_connection((self.host, self.port), timeout=30)

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
    fingerprint: str
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
            fingerprint = attributes.get("data-fingerprint") or ""
            self.cards.append(Card(int(item or 0), int(line or 0), fingerprint))
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

    def card(self, item: int, line: int) -> Card:
        return next(card for card in self.cards if (card.item, card.line) == (item, line))
