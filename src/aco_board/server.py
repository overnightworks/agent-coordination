"""The board's loopback HTTP transport.

Binds `127.0.0.1` only -- there is no host option -- and answers only a
request carrying the per-start access token: `GET /?t=TOKEN` renders the page
from a fresh read of the decision source, `POST /rule` submits one decision.
A malformed or stalled request gets a fixed 4xx answer, never a traceback.
"""

from __future__ import annotations

import hmac
import json
import secrets
import socket
import sys
import threading
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
"""A decision is two numbers, an outcome, a fingerprint, and a short note;
anything larger is refused before it is read."""
REQUEST_DEADLINE_SECONDS = 10.0
"""How long one socket read or write on a connection may wait; a browser
sends its small request at once, so a client still silent after this is
dropped rather than holding a handler thread."""
LINE_CHANGED = "Diese Zeile hat sich geändert, bitte neu laden"


@dataclass(frozen=True)
class _DecisionRequest:
    token: str
    fingerprint: str
    decision: Decision


class _ClientDisconnectedError(Exception):
    """Raised only by `_BoardRequestHandler._respond`'s own socket write: the
    one place a broken or reset connection is honestly the client hanging up
    rather than a server defect raising the same exception elsewhere."""


class _BoardServer(ThreadingHTTPServer):
    # `ThreadingHTTPServer` runs handlers as daemon threads, so `server_close`
    # never waits for one a stalled client holds -- only for a ruling write.

    def __init__(
        self, port: int, token: str, decisions: DecisionPort, request_deadline_seconds: float
    ) -> None:
        # `aco rule` rewrites the whole item body, so two writes at once could
        # each drop the other's ruling; this server writes one at a time. Made
        # before binding: a failed bind already calls `server_close`.
        self.write_lock = threading.Lock()
        super().__init__((LOOPBACK_HOST, port), _BoardRequestHandler)
        self.token = token
        self.decisions = decisions
        self.request_deadline_seconds = request_deadline_seconds

    def handle_error(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: tuple[str, int]
    ) -> None:
        """A browser that leaves mid-response is no defect: stay quiet for it
        and keep the stdlib's traceback for everything else."""
        if isinstance(sys.exc_info()[1], _ClientDisconnectedError):
            return
        super().handle_error(request, client_address)

    def server_close(self) -> None:
        """Stop listening, then wait for a ruling already being written.

        Handler threads are daemons, so the process may exit while one is
        inside `aco rule`, leaving the ruling's outcome unknown. Taking the
        write lock waits for that write -- as long as the source's own timeout
        lets it run -- and is never released, so no later write starts.
        """
        super().server_close()
        self.write_lock.acquire()


@dataclass(frozen=True)
class RunningBoard:
    httpd: ThreadingHTTPServer
    url: str


def start_board(
    port: int,
    decisions: DecisionPort,
    *,
    request_deadline_seconds: float = REQUEST_DEADLINE_SECONDS,
) -> RunningBoard:
    """Bind `127.0.0.1:port` (0 picks a free port) with a fresh token."""
    token = secrets.token_urlsafe(32)
    httpd = _BoardServer(port, token, decisions, request_deadline_seconds)
    bound_port = httpd.server_address[1]
    url = f"http://{LOOPBACK_HOST}:{bound_port}{PAGE_PATH}?{TOKEN_FIELD}={token}"
    return RunningBoard(httpd=httpd, url=url)


def submit_decision(
    decisions: DecisionPort, decision: Decision, fingerprint: str
) -> DecisionResult:
    """Write `decision` unless the open line it names no longer reads as shown.

    The fingerprint comparison is the board's only own check: the card was
    rendered from an earlier read, and an open line whose item, title,
    index, text, question, example, or picture changed since must not be
    ruled blind. A line that is no longer open skips the comparison on
    purpose: whether it is already ruled, out of range, or its item unknown
    is the source's own refusal, which `rule` reports.
    """
    try:
        lines = decisions.expectation_lines()
    except DecisionSourceUnavailableError as error:
        return DecisionResult(DecisionStatus.FAILED, str(error))
    named = (decision.item, decision.index)
    if any(
        (line.item, line.index) == named and line.is_open and line.fingerprint != fingerprint
        for line in lines
    ):
        return DecisionResult(DecisionStatus.FAILED, LINE_CHANGED)
    return decisions.rule(decision)


def _parsed_decision_request(raw: bytes) -> _DecisionRequest | None:
    fields = _json_object(raw)
    if fields is None:
        return None
    token, fingerprint = fields.get("token"), fields.get("fingerprint")
    item, line = fields.get("item"), fields.get("line")
    outcome, note = fields.get("outcome"), fields.get("note")
    if (
        isinstance(token, str)
        and isinstance(fingerprint, str)
        and _is_position(item)
        and _is_position(line)
        and isinstance(outcome, str)
        and outcome in {member.value for member in Outcome}
        and (note is None or _is_note_text(note))
    ):
        decision = Decision(item=item, index=line, outcome=Outcome(outcome), note=_note(note))
        return _DecisionRequest(token=token, fingerprint=fingerprint, decision=decision)
    return None


def _json_object(raw: bytes) -> dict[str, object] | None:
    try:
        fields: object = json.loads(raw)
    # ValueError covers undecodable bytes, invalid JSON, and an integer past
    # Python's digit limit; RecursionError a body nested deeper than the stack.
    except (ValueError, RecursionError):
        return None
    if not isinstance(fields, dict):
        return None
    return cast(dict[str, object], fields)


def _is_note_text(note: object) -> TypeGuard[str]:
    """Short text a command-line argument can carry: no NUL, no lone surrogate."""
    if not isinstance(note, str) or len(note.strip()) > page.NOTE_MAX_LENGTH or "\0" in note:
        return False
    try:
        note.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _note(raw: str | None) -> str | None:
    """An empty or blank note is no note."""
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def _content_length(header: str) -> int | None:
    """The declared body size, or None unless it is a decimal within the limit."""
    # The length check comes first: `int()` refuses a digit string past
    # Python's conversion limit with a ValueError.
    if len(header) > len(str(_MAX_REQUEST_BYTES)) or not (header.isascii() and header.isdigit()):
        return None
    length = int(header)
    return length if length <= _MAX_REQUEST_BYTES else None


def _is_position(value: object) -> TypeGuard[int]:
    """A positive item or line number; a JSON `true` is not one."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class _BoardRequestHandler(BaseHTTPRequestHandler):
    def _board(self) -> _BoardServer:
        # `_BoardServer.__init__` is this handler's only constructor.
        return cast(_BoardServer, self.server)

    def setup(self) -> None:
        # The per-server form of `StreamRequestHandler.timeout`, whose class
        # attribute every board would share; set before the request is read.
        super().setup()
        self.connection.settimeout(self._board().request_deadline_seconds)

    def _authorized(self, candidate: str | None) -> bool:
        # The token is ASCII, so a non-ASCII candidate (a lone surrogate
        # included) is simply wrong; `compare_digest` raises on one.
        return (
            candidate is not None
            and candidate.isascii()
            and hmac.compare_digest(candidate, self._board().token)
        )

    def _respond(
        self, status: HTTPStatus, body: str, content_type: str, *, nonce: str | None = None
    ) -> None:
        encoded = body.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", page.content_security_policy(nonce))
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as error:
            raise _ClientDisconnectedError from error

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
        length = _content_length(self.headers.get("Content-Length", ""))
        if length is None:
            self._respond_text(HTTPStatus.BAD_REQUEST, "bad request: Content-Length")
            return
        try:
            raw = self.rfile.read(length)
        except TimeoutError:
            self._respond_text(HTTPStatus.REQUEST_TIMEOUT, "request timeout: body incomplete")
            return
        request = _parsed_decision_request(raw)
        if request is not None and not self._authorized(request.token):
            self._respond_text(HTTPStatus.FORBIDDEN, "forbidden: missing or wrong token")
            return
        if request is None:
            self._respond_text(HTTPStatus.BAD_REQUEST, "bad request: decision")
            return
        board = self._board()
        with board.write_lock:
            result = submit_decision(board.decisions, request.decision, request.fingerprint)
        answer = json.dumps({"status": result.status.value, "message": result.message})
        self._respond(HTTPStatus.OK, answer, "application/json")

    def log_message(self, format: str, *_args: object) -> None:
        # The default request log would print the page URL, token included,
        # to the terminal; the printed start URL is the board's only output.
        return
