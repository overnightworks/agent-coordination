"""The board's loopback HTTP transport.

Binds `127.0.0.1` only -- there is no host option -- and answers only a
request carrying the per-start access token: `GET /?t=TOKEN` renders the page
from a fresh read of the decision source, `POST /rule` submits one decision.
"""

from __future__ import annotations

import hmac
import json
import secrets
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TypeGuard, cast
from urllib.parse import parse_qs, urlsplit

from . import page
from .ports import (
    Decision,
    DecisionPort,
    DecisionResult,
    DecisionSourceUnavailableError,
    DecisionStatus,
    Outcome,
)

LOOPBACK_HOST = "127.0.0.1"
TOKEN_FIELD = "t"
PAGE_PATH = "/"
RULE_PATH = "/rule"
_MAX_REQUEST_BYTES = 16 * 1024
"""A decision is two numbers, an outcome, and a short note; anything larger
is refused before it is read."""


@dataclass(frozen=True)
class _DecisionRequest:
    token: str
    decision: Decision


class _BoardServer(ThreadingHTTPServer):
    def __init__(self, port: int, token: str, decisions: DecisionPort) -> None:
        super().__init__((LOOPBACK_HOST, port), _BoardRequestHandler)
        self.token = token
        self.decisions = decisions


@dataclass(frozen=True)
class RunningBoard:
    httpd: ThreadingHTTPServer
    url: str


def start_board(port: int, decisions: DecisionPort) -> RunningBoard:
    """Bind `127.0.0.1:port` (0 picks a free port) with a fresh token."""
    token = secrets.token_urlsafe(32)
    httpd = _BoardServer(port, token, decisions)
    bound_port = httpd.server_address[1]
    url = f"http://{LOOPBACK_HOST}:{bound_port}{PAGE_PATH}?{TOKEN_FIELD}={token}"
    return RunningBoard(httpd=httpd, url=url)


def submit_decision(decisions: DecisionPort, decision: Decision) -> DecisionResult:
    """Write `decision` only when a fresh read still shows its line open.

    The fresh read validates item and line against the source's own data and
    answers "already ruled" without a second write; the source's own refusal
    still covers a ruling that lands between that read and the write.
    """
    try:
        lines = decisions.expectation_lines()
    except DecisionSourceUnavailableError as error:
        return DecisionResult(DecisionStatus.FAILED, str(error))
    line = next(
        (line for line in lines if (line.item, line.index) == (decision.item, decision.index)),
        None,
    )
    if line is None:
        return DecisionResult(
            DecisionStatus.FAILED,
            f"#{decision.item} has no expectation line {decision.index} in this repository",
        )
    if not line.is_open:
        return DecisionResult(DecisionStatus.ALREADY_RULED)
    return decisions.rule(decision)


def _parsed_decision_request(raw: bytes) -> _DecisionRequest | None:
    try:
        fields: object = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(fields, dict):
        return None
    token, item, line = fields.get("token"), fields.get("item"), fields.get("line")
    outcome, note = fields.get("outcome"), fields.get("note")
    if not isinstance(token, str) or not _is_position(item) or not _is_position(line):
        return None
    if outcome not in {member.value for member in Outcome}:
        return None
    if note is not None and not isinstance(note, str):
        return None
    decision = Decision(
        item=item,
        index=line,
        outcome=Outcome(outcome),
        note=_note(note),
    )
    return _DecisionRequest(token=token, decision=decision)


def _note(raw: str | None) -> str | None:
    """An empty or blank note is no note."""
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def _is_position(value: object) -> TypeGuard[int]:
    """A positive item or line number; a JSON `true` is not one."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class _BoardRequestHandler(BaseHTTPRequestHandler):
    def _board(self) -> _BoardServer:
        # `_BoardServer.__init__` is this handler's only constructor.
        return cast(_BoardServer, self.server)

    def _authorized(self, candidate: str | None) -> bool:
        # Bytes, not str: `compare_digest` raises on a non-ASCII str, and a
        # query string can carry any character.
        return candidate is not None and hmac.compare_digest(
            candidate.encode("utf-8"), self._board().token.encode("utf-8")
        )

    def _respond(
        self, status: HTTPStatus, body: str, content_type: str, *, nonce: str | None = None
    ) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", page.content_security_policy(nonce))
        self.end_headers()
        self.wfile.write(encoded)

    def _respond_text(self, status: HTTPStatus, body: str) -> None:
        self._respond(status, body, "text/plain; charset=utf-8")

    def do_GET(self) -> None:
        split = urlsplit(self.path)
        if split.path != PAGE_PATH:
            self._respond_text(HTTPStatus.NOT_FOUND, "not found")
            return
        tokens = parse_qs(split.query).get(TOKEN_FIELD)
        if not self._authorized(tokens[0] if tokens else None):
            self._respond_text(HTTPStatus.FORBIDDEN, "forbidden: missing or wrong token")
            return
        nonce = secrets.token_urlsafe(16)
        try:
            lines = self._board().decisions.expectation_lines()
        except DecisionSourceUnavailableError as error:
            unavailable = page.render_unavailable(str(error), nonce)
            self._respond(HTTPStatus.BAD_GATEWAY, unavailable, page.HTML_CONTENT_TYPE, nonce=nonce)
            return
        open_lines = tuple(line for line in lines if line.is_open)
        self._respond(
            HTTPStatus.OK, page.render_board(open_lines, nonce), page.HTML_CONTENT_TYPE, nonce=nonce
        )

    def do_POST(self) -> None:
        if urlsplit(self.path).path != RULE_PATH:
            self._respond_text(HTTPStatus.NOT_FOUND, "not found")
            return
        length = self.headers.get("Content-Length", "")
        if not (length.isascii() and length.isdigit()) or int(length) > _MAX_REQUEST_BYTES:
            self._respond_text(HTTPStatus.BAD_REQUEST, "bad request: Content-Length")
            return
        request = _parsed_decision_request(self.rfile.read(int(length)))
        if request is not None and not self._authorized(request.token):
            self._respond_text(HTTPStatus.FORBIDDEN, "forbidden: missing or wrong token")
            return
        if request is None:
            self._respond_text(HTTPStatus.BAD_REQUEST, "bad request: decision")
            return
        result = submit_decision(self._board().decisions, request.decision)
        answer = json.dumps({"status": result.status.value, "message": result.message})
        self._respond(HTTPStatus.OK, answer, "application/json")

    def log_message(self, format: str, *_args: object) -> None:
        # The default request log would print the page URL, token included,
        # to the terminal; the printed start URL is the board's only output.
        return
