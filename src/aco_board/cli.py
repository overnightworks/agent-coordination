"""`aco-board serve [--port N]`: start the decision board for this repository."""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Sequence
from pathlib import Path

from .aco_cli import AcoCli
from .server import LOOPBACK_HOST, start_board

_HIGHEST_PORT = 65535


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        port = -1
    if not 0 <= port <= _HIGHEST_PORT:
        raise argparse.ArgumentTypeError(f"{text!r} is not a port between 0 and {_HIGHEST_PORT}")
    return port


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aco-board",
        description="Decide this repository's open expectation lines in the browser.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser(
        "serve",
        help=f"serve the board on {LOOPBACK_HOST} with a per-start access token",
        allow_abbrev=False,
    )
    serve.add_argument(
        "--port",
        type=_port,
        default=0,
        metavar="N",
        help=f"{LOOPBACK_HOST} port to listen on; 0 (default) picks a free one",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    decisions = AcoCli(directory=Path.cwd())
    try:
        board = start_board(arguments.port, decisions)
    except OSError as error:
        print(
            f"ERROR: cannot listen on {LOOPBACK_HOST}:{arguments.port}: {error.strerror}",
            file=sys.stderr,
        )
        return 1
    print(board.url, flush=True)
    try:
        with contextlib.suppress(KeyboardInterrupt):
            board.httpd.serve_forever()
    finally:
        board.httpd.server_close()
    return 0
