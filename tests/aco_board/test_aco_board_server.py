"""The decision board served in-process against a fake `aco` on PATH.

Each test drives the real server and the real `aco` adapter; only the `aco`
executable is a fake whose state the test sets (issue #620's ruled lines).
"""

from __future__ import annotations

import base64
import html
import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest
from aco_board_fixtures import (
    BoardClient,
    BoardPage,
    Card,
    FakeAcoRepository,
    install_fake_aco,
    line,
    path_with,
    row,
)

from aco_board.aco_cli import AcoCli
from aco_board.page import NOTE_MAX_LENGTH
from aco_board.ports import Decision, DecisionPort, DecisionResult, ExpectationLine
from aco_board.server import LINE_CHANGED, LOOPBACK_HOST, start_board


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeAcoRepository:
    install_fake_aco(tmp_path / "bin")
    monkeypatch.setenv("PATH", path_with(tmp_path / "bin"))
    directory = tmp_path / "repository"
    directory.mkdir()
    fake = FakeAcoRepository(directory)
    fake.set_rulings()
    return fake


@contextmanager
def serving(decisions: DecisionPort, **board_options: float) -> Iterator[BoardClient]:
    running = start_board(0, decisions, **board_options)
    server_thread = threading.Thread(
        target=running.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    server_thread.start()
    try:
        yield BoardClient.from_url(running.url)
    finally:
        running.httpd.shutdown()
        running.httpd.server_close()
        server_thread.join()


@pytest.fixture
def board(repository: FakeAcoRepository) -> Iterator[BoardClient]:
    with serving(AcoCli(directory=repository.directory)) as client:
        yield client


def _page(board: BoardClient) -> BoardPage:
    response = board.get_page()
    assert response.status == 200
    return BoardPage(response.body)


def test_an_open_line_shows_as_a_card_with_question_example_and_picture(
    repository: FakeAcoRepository, board: BoardClient
) -> None:
    picture = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="4" height="4"/></svg>'
    repository.set_rulings(
        row(
            42,
            "Share page",
            line(
                1,
                "Guests may comment.",
                question="Darf Ben kommentieren?",
                example="Ben schreibt",
                picture=picture,
            ),
        )
    )

    [card] = _page(board).cards

    assert (card.item, card.line) == (42, 1)
    assert card.texts["question"] == "Darf Ben kommentieren?"
    assert card.texts["text"] == "Guests may comment."
    assert card.texts["example"] == "Ben schreibt"
    assert len(card.image_sources) == 1


def test_a_line_without_card_fields_shows_its_text_as_the_question(
    repository: FakeAcoRepository, board: BoardClient
) -> None:
    repository.set_rulings(row(7, "Plain", line(1, "Ship it?")))

    [card] = _page(board).cards

    assert card.texts["question"] == "Ship it?"
    assert card.image_sources == []


def test_cards_follow_acos_order(repository: FakeAcoRepository, board: BoardClient) -> None:
    repository.set_rulings(
        row(60, "Higher priority", line(1, "First?"), line(3, "Third?")),
        row(5, "Lower priority", line(2, "Second?")),
    )

    assert _page(board).card_keys() == [(60, 1), (60, 3), (5, 2)]


@pytest.mark.parametrize(
    "rows",
    [
        (),
        (row(11, "Fully ruled", line(1, "Settled.", ruling="yes")),),
    ],
    ids=["no-expectation-lines", "only-ruled-lines"],
)
def test_nothing_open_shows_nichts_offen(
    repository: FakeAcoRepository, board: BoardClient, rows: tuple[dict[str, object], ...]
) -> None:
    repository.set_rulings(*rows)

    page = _page(board)

    assert page.cards == []
    assert page.empty_state_shown


def test_every_load_reads_aco_afresh(repository: FakeAcoRepository, board: BoardClient) -> None:
    repository.set_rulings(row(1, "First", line(1, "Before?")))
    assert _page(board).card_keys() == [(1, 1)]

    repository.set_rulings(row(2, "Second", line(4, "After?")))

    assert _page(board).card_keys() == [(2, 4)]


@pytest.mark.parametrize("outcome", ["yes", "no"])
def test_a_choice_is_written_through_aco_rule_and_its_card_leaves(
    repository: FakeAcoRepository, board: BoardClient, outcome: str
) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?"), line(2, "Links?")))

    answer = board.decide(_page(board).card(42, 2), outcome)

    assert answer.json() == {"status": "ruled", "message": None}
    assert repository.rule_calls() == [["rule", "42", "--line", "2", f"--{outcome}", "--json"]]
    assert repository.stored_line(42, 2)["ruling"] == outcome
    assert _page(board).card_keys() == [(42, 1)]


def test_the_board_keeps_no_copy_of_a_decision(
    repository: FakeAcoRepository, board: BoardClient
) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))
    board.decide(_page(board).card(42, 1), "yes")

    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))

    assert _page(board).card_keys() == [(42, 1)]


@pytest.mark.parametrize("note", ["nur mit Konto", "--später", "-x"])
def test_a_note_is_stored_with_the_ruling(
    repository: FakeAcoRepository, board: BoardClient, note: str
) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))

    answer = board.decide(_page(board).card(42, 1), "no", note=f"  {note}  ")

    assert answer.json()["status"] == "ruled"
    assert repository.stored_line(42, 1)["text"] == f"Guests? Anmerkung: {note}"


def test_a_blank_note_is_no_note(repository: FakeAcoRepository, board: BoardClient) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))

    board.decide(_page(board).card(42, 1), "yes", note="   ")

    assert repository.rule_calls() == [["rule", "42", "--line", "1", "--yes", "--json"]]
    assert repository.stored_line(42, 1)["text"] == "Guests?"


def test_a_failing_aco_rule_keeps_the_card_with_acos_sentence(
    repository: FakeAcoRepository, board: BoardClient
) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))
    repository.refuse_rule("unavailable", "the forge rejected the body write")

    answer = board.decide(_page(board).card(42, 1), "yes")

    assert answer.json() == {
        "status": "failed",
        "message": "aco rule failed: the forge rejected the body write",
    }
    assert _page(board).card_keys() == [(42, 1)]


def test_aco_missing_during_a_decision_keeps_the_card_undecided(
    repository: FakeAcoRepository,
    board: BoardClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))
    card = _page(board).card(42, 1)
    monkeypatch.setenv("PATH", str(tmp_path / "no-aco-here"))

    answer = board.decide(card, "yes")

    assert answer.json() == {"status": "failed", "message": "aco is not installed on PATH"}
    assert repository.rule_calls() == []


def test_an_unreadable_aco_during_a_decision_writes_nothing(
    repository: FakeAcoRepository, board: BoardClient
) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))
    card = _page(board).card(42, 1)
    repository.fail_rulings("ERROR: no forge adapter for host")

    answer = board.decide(card, "yes")

    assert answer.json() == {
        "status": "failed",
        "message": "aco rulings failed: ERROR: no forge adapter for host",
    }
    assert repository.rule_calls() == []


def test_an_unreadable_aco_shows_its_reason_instead_of_cards(
    repository: FakeAcoRepository, board: BoardClient
) -> None:
    repository.fail_rulings("ERROR: no forge adapter for host")

    response = board.get_page()

    assert response.status == 502
    assert "ERROR: no forge adapter for host" in response.body
    assert BoardPage(response.body).cards == []


@pytest.mark.parametrize(
    ("stdout", "reason"),
    [
        ("not json", "aco rulings failed: exit 0 without a message"),
        ('{"ok": true, "rulings": {}}', "rulings is not a list"),
        ('{"ok": true, "rulings": [1]}', "a rulings row is not an object"),
        (
            '{"ok": true, "rulings": [{"number": true, "title": "T", "lines": []}]}',
            "number is not a int",
        ),
        ('{"ok": true, "rulings": [{"number": 1, "title": "T"}]}', "#1 lines is not a list"),
        (
            '{"ok": true, "rulings": [{"number": 1, "title": "T", "lines": [2]}]}',
            "#1 has a line that is not an object",
        ),
        (
            '{"ok": true, "rulings": [{"number": 1, "title": "T", "lines": '
            '[{"index": 1, "text": "Q?", "ruling": null, "question": 3}]}]}',
            "question is not text",
        ),
    ],
    ids=[
        "not-json",
        "rulings-not-a-list",
        "row-not-an-object",
        "number-not-an-int",
        "lines-not-a-list",
        "line-not-an-object",
        "question-not-text",
    ],
)
def test_unreadable_rulings_json_shows_why_instead_of_cards(
    repository: FakeAcoRepository, board: BoardClient, stdout: str, reason: str
) -> None:
    repository.answer_rulings_with(stdout)

    response = board.get_page()

    assert response.status == 502
    assert reason in html.unescape(response.body)
    assert BoardPage(response.body).cards == []


def test_a_hung_aco_shows_that_it_did_not_answer(repository: FakeAcoRepository) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))
    repository.delay("rulings", 30)

    with serving(AcoCli(directory=repository.directory, timeout_seconds=5)) as board:
        response = board.get_page()

    assert response.status == 502
    assert "aco rulings did not answer within 5 seconds" in response.body


def test_a_hung_aco_rule_keeps_the_card_undecided(repository: FakeAcoRepository) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))
    repository.delay("rule", 30)

    with serving(AcoCli(directory=repository.directory, timeout_seconds=5)) as board:
        answer = board.decide(_page(board).card(42, 1), "yes")
        page_after = _page(board)

    assert answer.json() == {
        "status": "failed",
        "message": "aco rule did not answer within 5 seconds",
    }
    assert page_after.card_keys() == [(42, 1)]


@pytest.mark.parametrize(
    "ruled_text", ["Guests?", "Guests? Anmerkung: später"], ids=["without-note", "with-note"]
)
def test_a_line_ruled_elsewhere_answers_already_decided_without_writing(
    repository: FakeAcoRepository, board: BoardClient, ruled_text: str
) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))
    card = _page(board).card(42, 1)
    repository.set_rulings(row(42, "Share page", line(1, ruled_text, ruling="no")))
    state_before = repository.rulings_state()

    answer = board.decide(card, "yes")

    assert answer.json() == {
        "status": "already_ruled",
        "message": "line 1 is already ruled; a changed ruling is a new line",
    }
    assert repository.rulings_state() == state_before


@pytest.mark.parametrize(
    ("item", "index", "sentence"),
    [
        (42, 9, "line 9 out of range: this item has 1 expectation line(s)"),
        (99, 1, "#99 does not exist"),
    ],
    ids=["unknown-line", "unknown-item"],
)
def test_a_line_aco_does_not_know_is_refused_with_acos_sentence(
    repository: FakeAcoRepository, board: BoardClient, item: int, index: int, sentence: str
) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))
    state_before = repository.rulings_state()

    answer = board.decide(Card(item, index, fingerprint="never rendered"), "yes")

    assert answer.json() == {"status": "failed", "message": f"aco rule failed: {sentence}"}
    assert repository.rulings_state() == state_before


_SHOWN_FIELDS = {
    "title": "Share page",
    "text": "Guests may comment.",
    "question": "Darf Ben kommentieren?",
    "example": "Ben schreibt",
    "picture": '<svg xmlns="http://www.w3.org/2000/svg"><rect width="4" height="4"/></svg>',
}


def _shown_row(**changes: str) -> dict[str, object]:
    card = {**_SHOWN_FIELDS, **changes}
    title, text = card.pop("title"), card.pop("text")
    return row(42, title, line(1, text, **card))


@pytest.mark.parametrize(
    "change",
    [
        {"title": "Share link"},
        {"text": "Guests may delete."},
        {"question": "Darf Ben löschen?"},
        {"example": "Ben löscht"},
        {"picture": '<svg xmlns="http://www.w3.org/2000/svg"><circle r="4"/></svg>'},
    ],
    ids=["title", "text", "question", "example", "picture"],
)
def test_a_card_whose_line_changed_since_rendering_is_refused_without_writing(
    repository: FakeAcoRepository, board: BoardClient, change: dict[str, str]
) -> None:
    repository.set_rulings(_shown_row())
    stale = _page(board).card(42, 1)
    repository.set_rulings(_shown_row(**change))
    state_before = repository.rulings_state()

    answer = board.decide(stale, "yes")

    assert answer.json() == {"status": "failed", "message": LINE_CHANGED}
    assert repository.rule_calls() == []
    assert repository.rulings_state() == state_before


@pytest.mark.parametrize("twin", [(43, 1), (42, 2)], ids=["item", "index"])
def test_a_card_shown_for_another_line_with_the_same_words_is_refused_without_writing(
    repository: FakeAcoRepository, board: BoardClient, twin: tuple[int, int]
) -> None:
    repository.set_rulings(
        row(42, "Share page", line(1, "Guests?"), line(2, "Guests?")),
        row(43, "Share page", line(1, "Guests?")),
    )
    twin_card = _page(board).card(*twin)
    state_before = repository.rulings_state()

    answer = board.decide(Card(42, 1, fingerprint=twin_card.fingerprint), "yes")

    assert answer.json() == {"status": "failed", "message": LINE_CHANGED}
    assert repository.rule_calls() == []
    assert repository.rulings_state() == state_before


def test_simultaneous_decisions_on_one_item_are_all_stored(
    repository: FakeAcoRepository, board: BoardClient
) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?"), line(2, "Links?")))
    cards = _page(board).cards
    # Each `aco rule` waits between reading and rewriting the body, so two
    # unserialized writes would overlap and the later one would drop the first.
    repository.delay("rule", 0.3)

    with ThreadPoolExecutor(max_workers=2) as clicks:
        answers = list(clicks.map(lambda card: board.decide(card, "yes"), cards))

    assert [answer.json()["status"] for answer in answers] == ["ruled", "ruled"]
    assert repository.stored_line(42, 1)["ruling"] == "yes"
    assert repository.stored_line(42, 2)["ruling"] == "yes"


def test_a_page_without_or_with_a_wrong_token_is_refused_without_reading_aco(
    repository: FakeAcoRepository, board: BoardClient
) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))

    assert board.get_page_without_token().status == 403
    assert board.get_page(token="wrong").status == 403
    assert board.get_page(token="%C3%A4").status == 403
    assert repository.calls() == []


def test_a_decision_with_a_wrong_token_is_refused_without_reading_aco(
    repository: FakeAcoRepository, board: BoardClient
) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))

    assert board.decide(Card(42, 1, "any"), "yes", token="wrong").status == 403
    assert board.decide(Card(42, 1, "any"), "yes", token="\ud800").status == 403
    assert board.decide(Card(42, 1, "any"), "yes", token="ä" * 43).status == 403
    assert repository.calls() == []


@pytest.mark.parametrize(
    "body", ["not json", '{"item": 42}', "[]"], ids=["not-json", "missing-fields", "not-an-object"]
)
def test_a_malformed_decision_is_refused_without_writing(
    repository: FakeAcoRepository, board: BoardClient, body: str
) -> None:
    assert board.post_raw(body).status == 400
    assert repository.calls() == []


def _decision_body(token: str, **overrides: object) -> str:
    fields: dict[str, object] = {
        "token": token,
        "item": 42,
        "line": 1,
        "fingerprint": "any",
        "outcome": "yes",
        "note": None,
    }
    return json.dumps({**fields, **overrides})


@pytest.mark.parametrize(
    "overrides",
    [
        {"outcome": "later"},
        {"outcome": ["yes"]},
        {"item": True},
        {"line": 0},
        {"fingerprint": None},
        {"note": 7},
        {"note": "\ud800"},
        {"note": "nul\u0000byte"},
        {"note": "x" * (NOTE_MAX_LENGTH + 1)},
    ],
    ids=[
        "outcome-later",
        "outcome-not-text",
        "item-true",
        "line-zero",
        "no-fingerprint",
        "note-not-text",
        "note-lone-surrogate",
        "note-nul",
        "note-too-long",
    ],
)
def test_a_decision_outside_yes_or_no_on_a_real_line_is_refused(
    repository: FakeAcoRepository, board: BoardClient, overrides: dict[str, object]
) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))

    assert board.post_raw(_decision_body(board.token, **overrides)).status == 400
    assert repository.calls() == []


def _post(body: bytes, length: bytes | None = None) -> bytes:
    declared = str(len(body)).encode() if length is None else length
    return b"POST /rule HTTP/1.1\r\nHost: x\r\nContent-Length: " + declared + b"\r\n\r\n" + body


@pytest.mark.parametrize(
    ("request_bytes", "status"),
    [
        (b"GET /?t=%ED%A0%80 HTTP/1.1\r\nHost: x\r\n\r\n", b"403"),
        (b"GET /?t=\xff\xfe HTTP/1.1\r\nHost: x\r\n\r\n", b"403"),
        (b"GET /elsewhere HTTP/1.1\r\nHost: x\r\n\r\n", b"404"),
        (_post(b"{}").replace(b"/rule", b"/elsewhere"), b"404"),
        (b"POST /rule HTTP/1.1\r\nHost: x\r\n\r\n", b"400"),
        (_post(b"{}", b"abc"), b"400"),
        (_post(b"{}", b"-1"), b"400"),
        (_post(b"{}", b"\xd9\xa3"), b"400"),
        (_post(b"{}", b"9" * 5000), b"400"),
        (_post(b"{}", b"16385"), b"400"),
        (_post(b"x" * 16385), b"400"),
        (_post(b"[" * 16000), b"400"),
        (_post(b'{"item": 1' + b"0" * 5000 + b"}"), b"400"),
        (_post(b'{"token": "\xed\xa0\x80"}'), b"400"),
        (
            _post(
                b'{"token": "\\ud800", "item": 42, "line": 1, "fingerprint": "f", "outcome": "yes"}'
            ),
            b"403",
        ),
    ],
    ids=[
        "query-token-lone-surrogate",
        "query-token-raw-bytes",
        "get-unknown-path",
        "post-unknown-path",
        "no-content-length",
        "content-length-not-numeric",
        "content-length-negative",
        "content-length-non-ascii-digit",
        "content-length-past-int-limit",
        "content-length-over-limit",
        "body-over-limit",
        "body-nested-too-deep",
        "body-integer-past-int-limit",
        "body-not-utf8",
        "body-token-lone-surrogate",
    ],
)
def test_a_malformed_request_gets_a_fixed_answer_without_a_traceback(
    repository: FakeAcoRepository,
    board: BoardClient,
    capsys: pytest.CaptureFixture[str],
    request_bytes: bytes,
    status: bytes,
) -> None:
    answer = board.send_raw(request_bytes)

    status_line, _, rest = answer.partition(b"\r\n")
    assert status_line.split(b" ")[1] == status
    assert b"Traceback" not in rest
    assert str(Path(__file__).parents[2]).encode() not in rest
    assert "Traceback" not in capsys.readouterr().err
    assert repository.rule_calls() == []


_SHORT_DEADLINE_SECONDS = 0.2


@pytest.mark.parametrize(
    ("unfinished", "status"),
    [
        (b"POST /rule HTTP/1.1\r\nHost: x\r\n", None),
        (_post(b'{"item": 42}', b"40"), b"408"),
    ],
    ids=["headers-unfinished", "body-unfinished"],
)
def test_a_stalled_request_is_dropped_after_the_deadline_without_a_traceback(
    repository: FakeAcoRepository,
    capsys: pytest.CaptureFixture[str],
    unfinished: bytes,
    status: bytes | None,
) -> None:
    decisions = AcoCli(directory=repository.directory)
    with serving(decisions, request_deadline_seconds=_SHORT_DEADLINE_SECONDS) as board:
        answer = board.send_unfinished(unfinished)

    assert (answer.split(b" ")[1] if answer else None) == status
    assert "Traceback" not in capsys.readouterr().err
    assert repository.calls() == []


def test_a_stalled_request_does_not_hold_up_shutdown(repository: FakeAcoRepository) -> None:
    def serve_and_stop_beneath(stalled_clients: ExitStack) -> None:
        with serving(AcoCli(directory=repository.directory)) as board:
            stalled_clients.enter_context(board.holding_unfinished(b"GET /?t="))
            # Connections are accepted in order, so this answer means the
            # stalled one already holds a handler.
            board.send_raw(b"GET /elsewhere HTTP/1.1\r\nHost: x\r\n\r\n")

    with ExitStack() as stalled_clients:
        stopping = threading.Thread(
            target=serve_and_stop_beneath, args=(stalled_clients,), daemon=True
        )
        stopping.start()
        stopping.join(timeout=5)

        assert not stopping.is_alive()


def test_a_browser_leaving_mid_answer_prints_no_traceback(
    repository: FakeAcoRepository, capsys: pytest.CaptureFixture[str]
) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))
    repository.delay("rulings", 0.3)

    with serving(AcoCli(directory=repository.directory)) as board:
        board.send_and_reset(f"GET /?t={board.token} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        _wait_for_request_handlers()

    assert repository.calls() == [["rulings", "--json"]]
    assert "Traceback" not in capsys.readouterr().err


class _HugeLineSource:
    """One open line far larger than the socket buffers between board and client."""

    def expectation_lines(self) -> tuple[ExpectationLine, ...]:
        huge_text = "x" * (32 * 1024 * 1024)
        return (ExpectationLine(42, "Share page", 1, huge_text, True, None, None, None),)

    def rule(self, decision: Decision) -> DecisionResult:
        raise AssertionError("loading the page must not write")


def test_a_browser_that_stops_reading_is_dropped_without_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        serving(_HugeLineSource(), request_deadline_seconds=_SHORT_DEADLINE_SECONDS) as board,
        board.holding_unread(f"GET /?t={board.token} HTTP/1.1\r\nHost: x\r\n\r\n".encode()),
    ):
        # Connections are accepted in order, so this answer means the
        # unread one already holds a handler.
        board.send_raw(b"GET /elsewhere HTTP/1.1\r\nHost: x\r\n\r\n")
        _wait_for_request_handlers()

    assert "Traceback" not in capsys.readouterr().err


class _BrokenPipeSource:
    """A decision source whose own pipe breaks: a server defect, not a client leaving."""

    def expectation_lines(self) -> tuple[ExpectationLine, ...]:
        raise BrokenPipeError("aco's pipe broke")

    def rule(self, decision: Decision) -> DecisionResult:
        raise AssertionError("the board must not write after a failed read")


def test_a_broken_pipe_inside_the_board_still_prints_its_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with serving(_BrokenPipeSource()) as board:
        board.send_raw(f"GET /?t={board.token} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        _wait_for_request_handlers()

    assert "BrokenPipeError: aco's pipe broke" in capsys.readouterr().err


def _wait_for_request_handlers() -> None:
    # `socketserver.ThreadingMixIn` names every handler thread after its target.
    for thread in threading.enumerate():
        if thread.name.endswith("(process_request_thread)"):
            thread.join(timeout=10)


def test_the_board_listens_on_loopback_only(board: BoardClient) -> None:
    assert board.host == LOOPBACK_HOST


def test_loading_the_page_never_writes(repository: FakeAcoRepository, board: BoardClient) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))

    _page(board)
    _page(board)

    assert repository.rule_calls() == []


def test_a_picture_is_embedded_only_as_an_image_never_as_markup(
    repository: FakeAcoRepository, board: BoardClient
) -> None:
    hostile = (
        '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)">'
        "<script>alert(2)</script><foreignObject><iframe/></foreignObject></svg>"
    )
    repository.set_rulings(row(42, "Share page", line(1, "Guests?", picture=hostile)))

    response = board.get_page()
    page = BoardPage(response.body)

    [source] = page.cards[0].image_sources
    prefix = "data:image/svg+xml;base64,"
    assert source.startswith(prefix)
    assert base64.b64decode(source.removeprefix(prefix)).decode("utf-8") == hostile
    assert not {"svg", "foreignobject", "iframe"} & set(page.element_names)
    assert page.script_count == 1
    assert response.content_security_policy is not None
    assert "img-src data:" in response.content_security_policy


def test_text_from_aco_is_shown_as_text_never_as_markup(
    repository: FakeAcoRepository, board: BoardClient
) -> None:
    repository.set_rulings(
        row(
            42,
            "<b>Title</b>",
            line(1, '<img src=x onerror="alert(1)">', example="<script>x</script>"),
        )
    )

    page = BoardPage(board.get_page().body)

    [card] = page.cards
    assert card.texts["question"] == '<img src=x onerror="alert(1)">'
    assert card.image_sources == []
    assert page.script_count == 1
