"""The decision board served in-process against a fake `aco` on PATH.

Each test drives the real server and the real `aco` adapter; only the `aco`
executable is a fake whose state the test sets (issue #620's ruled lines).
"""

from __future__ import annotations

import base64
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from aco_board_fixtures import (
    BoardClient,
    BoardPage,
    FakeAcoRepository,
    install_fake_aco,
    line,
    path_with,
    row,
)

from aco_board.aco_cli import AcoCli
from aco_board.server import LOOPBACK_HOST, start_board


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeAcoRepository:
    install_fake_aco(tmp_path / "bin")
    monkeypatch.setenv("PATH", path_with(tmp_path / "bin"))
    directory = tmp_path / "repository"
    directory.mkdir()
    fake = FakeAcoRepository(directory)
    fake.set_rulings()
    return fake


@pytest.fixture
def board(repository: FakeAcoRepository) -> Iterator[BoardClient]:
    running = start_board(0, AcoCli(directory=repository.directory))
    serving = threading.Thread(
        target=running.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    serving.start()
    try:
        yield BoardClient.from_url(running.url)
    finally:
        running.httpd.shutdown()
        running.httpd.server_close()
        serving.join()


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

    answer = board.decide(42, 2, outcome)

    assert answer.json() == {"status": "ruled", "message": None}
    assert repository.rule_calls() == [["rule", "42", "--line", "2", f"--{outcome}", "--json"]]
    assert _page(board).card_keys() == [(42, 1)]


def test_the_board_keeps_no_copy_of_a_decision(
    repository: FakeAcoRepository, board: BoardClient
) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))
    board.decide(42, 1, "yes")

    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))

    assert _page(board).card_keys() == [(42, 1)]


def test_a_note_is_stored_with_the_ruling(
    repository: FakeAcoRepository, board: BoardClient
) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))

    board.decide(42, 1, "no", note="  nur mit Konto  ")

    assert repository.rule_calls() == [
        ["rule", "42", "--line", "1", "--no", "--note", "nur mit Konto", "--json"]
    ]


def test_a_failing_aco_rule_keeps_the_card_with_acos_sentence(
    repository: FakeAcoRepository, board: BoardClient
) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))
    repository.refuse_rule("unavailable", "the forge rejected the body write")

    answer = board.decide(42, 1, "yes")

    assert answer.json() == {
        "status": "failed",
        "message": "aco rule failed: the forge rejected the body write",
    }
    assert _page(board).card_keys() == [(42, 1)]


def test_an_unreadable_aco_shows_its_reason_instead_of_cards(
    repository: FakeAcoRepository, board: BoardClient
) -> None:
    repository.fail_rulings("ERROR: no forge adapter for host")

    response = board.get_page()

    assert response.status == 502
    assert "ERROR: no forge adapter for host" in response.body
    assert BoardPage(response.body).cards == []


def test_a_line_ruled_elsewhere_answers_already_decided_without_writing(
    repository: FakeAcoRepository, board: BoardClient
) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))
    page_before = _page(board)
    repository.set_rulings(row(42, "Share page", line(1, "Guests?", ruling="no")))

    answer = board.decide(*page_before.card_keys()[0], "yes")

    assert answer.json()["status"] == "already_ruled"
    assert repository.rule_calls() == []


def test_acos_already_ruled_refusal_answers_already_decided(
    repository: FakeAcoRepository, board: BoardClient
) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))
    repository.refuse_rule(
        "already_ruled", "line 1 is already ruled; a changed ruling is a new line"
    )

    answer = board.decide(42, 1, "yes")

    assert answer.json()["status"] == "already_ruled"
    assert len(repository.rule_calls()) == 1


@pytest.mark.parametrize(
    ("item", "index"), [(42, 9), (99, 1)], ids=["unknown-line", "unknown-item"]
)
def test_a_line_aco_does_not_know_is_refused_without_writing(
    repository: FakeAcoRepository, board: BoardClient, item: int, index: int
) -> None:
    repository.set_rulings(row(42, "Share page", line(1, "Guests?")))

    answer = board.decide(item, index, "yes")

    assert answer.json()["status"] == "failed"
    assert repository.rule_calls() == []


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

    assert board.decide(42, 1, "yes", token="wrong").status == 403
    assert repository.calls() == []


@pytest.mark.parametrize(
    "body", ["not json", '{"item": 42}', "[]"], ids=["not-json", "missing-fields", "not-an-object"]
)
def test_a_malformed_decision_is_refused_without_writing(
    repository: FakeAcoRepository, board: BoardClient, body: str
) -> None:
    assert board.post_raw(body).status == 400
    assert repository.calls() == []


@pytest.mark.parametrize(
    ("item", "index", "outcome"), [(42, 1, "later"), (True, 1, "yes"), (42, 0, "yes")]
)
def test_a_decision_outside_yes_or_no_on_a_real_line_is_refused(
    repository: FakeAcoRepository, board: BoardClient, item: object, index: int, outcome: str
) -> None:
    body = (
        f'{{"token": "{board.token}", "item": {str(item).lower()}, "line": {index}, '
        f'"outcome": "{outcome}", "note": null}}'
    )

    assert board.post_raw(body).status == 400
    assert repository.rule_calls() == []


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
