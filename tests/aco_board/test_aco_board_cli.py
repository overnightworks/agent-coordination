"""`aco-board serve` driven in-process through its real entry point, `cli.main`.

`main` serves until interrupted, so a helper thread drives the printed URL and
then interrupts the main thread the way Ctrl-C does.
"""

from __future__ import annotations

import _thread
import io
import socket
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
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

from aco_board import cli
from aco_board.server import LOOPBACK_HOST

_URL_WAIT_SECONDS = 30


class _PrintedLines(io.StringIO):
    """Standard output that tells a waiting thread when the first line is out."""

    def __init__(self) -> None:
        super().__init__()
        self.first_line_printed = threading.Event()

    def write(self, text: str) -> int:
        written = super().write(text)
        if "\n" in self.getvalue():
            self.first_line_printed.set()
        return written

    def first_line(self) -> str:
        return self.getvalue().splitlines()[0]


def _serve_until_done(
    monkeypatch: pytest.MonkeyPatch, directory: Path, scenario: Callable[[BoardClient], None]
) -> int:
    """Run `aco-board serve` in `directory`, drive `scenario`, then press Ctrl-C."""
    printed = _PrintedLines()
    monkeypatch.chdir(directory)
    monkeypatch.setattr(sys, "stdout", printed)
    failures: list[BaseException] = []

    def drive() -> None:
        try:
            assert printed.first_line_printed.wait(_URL_WAIT_SECONDS)
            # Every scenario requests at least once, so serving has begun
            # before the interrupt arrives.
            scenario(BoardClient.from_url(printed.first_line()))
        except BaseException as failure:
            failures.append(failure)
        finally:
            _thread.interrupt_main()

    driver = threading.Thread(target=drive)
    driver.start()
    exit_code = cli.main(["serve", "--port", "0"])
    driver.join()
    if failures:
        raise failures[0]
    return exit_code


@pytest.fixture
def fake_aco_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_aco(tmp_path / "bin")
    monkeypatch.setenv("PATH", path_with(tmp_path / "bin"))


def _repository(directory: Path, *rows: dict[str, object]) -> FakeAcoRepository:
    directory.mkdir()
    repository = FakeAcoRepository(directory)
    repository.set_rulings(*rows)
    return repository


@pytest.mark.usefixtures("fake_aco_on_path")
def test_serve_shows_its_own_repository_on_a_tokened_loopback_url_until_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = _repository(tmp_path / "here", row(1, "Here", line(1, "Ours?")))
    _repository(tmp_path / "elsewhere", row(2, "Elsewhere", line(1, "Theirs?")))
    seen: list[BoardClient] = []

    def look(board: BoardClient) -> None:
        seen.append(board)
        assert BoardPage(board.get_page().body).card_keys() == [(1, 1)]
        assert board.get_page_without_token().status == 403

    assert _serve_until_done(monkeypatch, here.directory, look) == 0
    [board] = seen
    assert board.host == LOOPBACK_HOST
    assert board.token


@pytest.mark.usefixtures("fake_aco_on_path")
def test_interrupting_serve_during_a_ruling_waits_until_the_ruling_is_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path / "here", row(42, "Share page", line(1, "Guests?")))
    repository.delay("rule", 0.5)

    with ThreadPoolExecutor(max_workers=1) as clicks:

        def click_then_interrupt(board: BoardClient) -> None:
            card = BoardPage(board.get_page().body).card(42, 1)
            clicks.submit(board.decide, card, "yes")
            _wait_until(lambda: repository.rule_calls() != [])

        assert _serve_until_done(monkeypatch, repository.directory, click_then_interrupt) == 0
        # Checked before the click's own answer is awaited: `main` returning
        # must already mean the ruling is stored.
        assert repository.stored_line(42, 1)["ruling"] == "yes"


def _wait_until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + _URL_WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_serve_offers_no_way_to_listen_beyond_loopback(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        cli.main(["serve", "--host", "0.0.0.0"])

    assert refused.value.code == 2
    assert "--host" in capsys.readouterr().err


@pytest.mark.parametrize("port", ["65536", "-1", "eighty"])
def test_serve_refuses_a_port_outside_0_to_65535(
    capsys: pytest.CaptureFixture[str], port: str
) -> None:
    with pytest.raises(SystemExit) as refused:
        cli.main(["serve", "--port", port])

    assert refused.value.code == 2
    assert "is not a port between 0 and 65535" in capsys.readouterr().err


def test_serve_refuses_a_port_already_in_use(capsys: pytest.CaptureFixture[str]) -> None:
    with socket.socket() as occupant:
        occupant.bind((LOOPBACK_HOST, 0))
        occupant.listen()
        busy_port = occupant.getsockname()[1]

        assert cli.main(["serve", "--port", str(busy_port)]) == 1

    assert f"cannot listen on {LOOPBACK_HOST}:{busy_port}" in capsys.readouterr().err
