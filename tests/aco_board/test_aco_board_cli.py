"""`aco-board serve` driven through its real entry point in a child process."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
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

from aco_board import cli
from aco_board.server import LOOPBACK_HOST

_ENTRY_POINT = "import sys; from aco_board.cli import main; sys.exit(main())"


@pytest.fixture
def fake_aco_path(tmp_path: Path) -> str:
    install_fake_aco(tmp_path / "bin")
    return path_with(tmp_path / "bin")


def _repository(directory: Path, *rows: dict[str, object]) -> FakeAcoRepository:
    directory.mkdir()
    repository = FakeAcoRepository(directory)
    repository.set_rulings(*rows)
    return repository


@pytest.fixture
def served_from(fake_aco_path: str) -> Iterator[list[subprocess.Popen[str]]]:
    started: list[subprocess.Popen[str]] = []
    yield started
    for process in started:
        process.send_signal(signal.SIGINT)
        process.wait(timeout=30)


def _serve(started: list[subprocess.Popen[str]], directory: Path, path: str) -> BoardClient:
    process = subprocess.Popen(
        [sys.executable, "-c", _ENTRY_POINT, "serve", "--port", "0"],
        cwd=directory,
        env={**os.environ, "PATH": path},
        stdout=subprocess.PIPE,
        text=True,
    )
    started.append(process)
    assert process.stdout is not None
    return BoardClient.from_url(process.stdout.readline().strip())


def test_serve_prints_a_loopback_url_with_a_token_and_shows_only_its_own_repository(
    tmp_path: Path, fake_aco_path: str, served_from: list[subprocess.Popen[str]]
) -> None:
    here = _repository(tmp_path / "here", row(1, "Here", line(1, "Ours?")))
    _repository(tmp_path / "elsewhere", row(2, "Elsewhere", line(1, "Theirs?")))

    board = _serve(served_from, here.directory, fake_aco_path)

    assert board.host == LOOPBACK_HOST
    assert board.token
    assert BoardPage(board.get_page().body).card_keys() == [(1, 1)]
    assert board.get_page_without_token().status == 403


def test_serve_stops_cleanly_on_interrupt(
    tmp_path: Path, fake_aco_path: str, served_from: list[subprocess.Popen[str]]
) -> None:
    here = _repository(tmp_path / "here")
    _serve(served_from, here.directory, fake_aco_path)
    process = served_from.pop()

    process.send_signal(signal.SIGINT)

    assert process.wait(timeout=30) == 0


def test_serve_offers_no_way_to_listen_beyond_loopback(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        cli.main(["serve", "--host", "0.0.0.0"])

    assert refused.value.code == 2
    assert "--host" in capsys.readouterr().err


def test_serve_refuses_a_port_already_in_use(capsys: pytest.CaptureFixture[str]) -> None:
    with socket.socket() as occupant:
        occupant.bind((LOOPBACK_HOST, 0))
        occupant.listen()
        busy_port = occupant.getsockname()[1]

        assert cli.main(["serve", "--port", str(busy_port)]) == 1

    assert f"cannot listen on {LOOPBACK_HOST}:{busy_port}" in capsys.readouterr().err
