"""Behavioral tests for `scripts/migrate_protocol_names.py` (issue #587).

Every test drives the script's `main` against `FakeGitHub`, an in-memory stand-in for the
issues REST API that answers in `gh api --include` form, and a `FakeClock` that records each
wait instead of sleeping. No test reaches GitHub.
"""

from __future__ import annotations

import json
import runpy
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "migrate_protocol_names.py"
migrate = SimpleNamespace(**runpy.run_path(str(_SCRIPT_PATH), run_name="migrate_protocol_names"))

PROTOCOL_BODY = 'Why\n\n```agent-claim\nversion = 1\nsize = "S"\n```\n'
MIGRATED_BODY = 'Why\n\n```aco\nversion = 1\nsize = "S"\n```\n'
EDITED_BODY = PROTOCOL_BODY + "edited after the dry run\n"
REPOSITORY = "owner/repo"


def _included(status: int, payload: object, headers: dict[str, str] | None = None) -> str:
    header_lines = "".join(f"{name}: {value}\r\n" for name, value in (headers or {}).items())
    return f"HTTP/2.0 {status} Status\n{header_lines}\r\n{json.dumps(payload)}"


@dataclass
class FakeGitHub:
    """Issues by (repository, number). A PATCH merges its JSON payload into the issue, so a
    payload carrying anything beyond the body would visibly change the stored issue.
    `patch_answers` scripts the next PATCH answers; None stores the PATCH as GitHub would."""

    issues: dict[tuple[str, int], dict[str, object]] = field(default_factory=dict)
    patch_answers: list[str | None] = field(default_factory=list)
    stored_suffix: str = ""

    def add(
        self, number: int, body: str | None, *, repository: str = REPOSITORY, **fields: object
    ) -> None:
        self.issues[(repository, number)] = {"number": number, "body": body, **fields}

    def body(self, number: int) -> object:
        return self.issues[(REPOSITORY, number)]["body"]

    def __call__(self, arguments: list[str], *, input_data: bytes | None = None) -> str:
        method, target = arguments[arguments.index("--method") + 1 :][:2]
        url = urlsplit(target)
        _, owner, name, _, *number = url.path.split("/")
        repository = f"{owner}/{name}"
        if not number:
            return self._page(repository, parse_qs(url.query))
        issue = self.issues[(repository, int(number[0]))]
        scripted = self.patch_answers.pop(0) if method == "PATCH" and self.patch_answers else None
        if scripted is not None:
            return scripted
        if method == "PATCH":
            payload = json.loads(input_data or b"{}")
            issue.update(payload, body=payload["body"] + self.stored_suffix)
        return _included(200, issue)

    def _page(self, repository: str, query: dict[str, list[str]]) -> str:
        assert query["state"] == ["all"]
        size, page = int(query["per_page"][0]), int(query["page"][0])
        items = [
            issue for (held_by, _), issue in sorted(self.issues.items()) if held_by == repository
        ]
        return _included(200, items[(page - 1) * size : page * size])


@dataclass
class FakeClock:
    """Records each wait instead of sleeping; `during_wait` stands for whatever else
    happens on GitHub while the script waits."""

    waits: list[float] = field(default_factory=list)
    time: float = 1_000_000.0
    during_wait: Callable[[], None] = lambda: None

    def now(self) -> float:
        return self.time

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.time += seconds
        self.during_wait()


@dataclass
class Migration:
    github: FakeGitHub
    clock: FakeClock
    manifest: Path

    def dry_run(self, *repositories: str) -> int:
        repository_arguments = [part for repo in repositories for part in ("--repo", repo)]
        arguments = ["--dry-run", *repository_arguments, "--manifest", str(self.manifest)]
        return migrate.main(arguments, run=self.github, clock=self.clock)

    def apply(self, *options: str) -> int:
        arguments = ["--apply", "--manifest", str(self.manifest), *options]
        return migrate.main(arguments, run=self.github, clock=self.clock)

    def manifest_rows(self) -> list[tuple[str, int]]:
        return [(row["repository"], row["number"]) for row in json.loads(self.manifest.read_text())]


@pytest.fixture
def migration(tmp_path: Path) -> Migration:
    return Migration(FakeGitHub(), FakeClock(), tmp_path / "manifest.json")


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        pytest.param(PROTOCOL_BODY + PROTOCOL_BODY, "more than one", id="several-fences"),
        pytest.param(
            MIGRATED_BODY + PROTOCOL_BODY, "already has an aco fence", id="mixed-new-then-old"
        ),
        pytest.param(
            PROTOCOL_BODY + MIGRATED_BODY, "already has an aco fence", id="mixed-old-then-new"
        ),
        pytest.param("Why\n```agent-claim\nversion = 1\n", "never closed", id="open-fence"),
        pytest.param("Why\n~~~agent-claim\nversion = 1\n~~~\n", "tildes", id="tilde"),
        pytest.param(PROTOCOL_BODY.replace("\n", "\r\n"), "CRLF", id="crlf"),
        pytest.param(
            "Example:\n````markdown\n" + PROTOCOL_BODY + "````\n",
            "inside another fenced block",
            id="fence-inside-documentation",
        ),
        pytest.param(
            PROTOCOL_BODY.replace("```agent-claim", "```agent-claim toml"),
            "does not read exactly",
            id="info-after-the-name",
        ),
    ],
)
def test_dry_run_refuses_and_names_every_shape_but_the_one_exact_fence(
    migration: Migration, capsys: pytest.CaptureFixture[str], body: str, reason: str
) -> None:
    migration.github.add(7, body)

    exit_code = migration.dry_run(REPOSITORY)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert f"refused {REPOSITORY}#7: " in output
    assert reason in output
    assert migration.manifest_rows() == []
    assert migration.github.body(7) == body


def test_dry_run_lists_every_issue_body_to_change_across_pages_and_writes_nothing(
    migration: Migration, capsys: pytest.CaptureFixture[str]
) -> None:
    github = migration.github
    for number in range(1, migrate.PAGE_SIZE + 2):
        github.add(number, "no protocol here")
    github.add(200, PROTOCOL_BODY, state="closed")
    github.add(201, PROTOCOL_BODY, pull_request={"url": "a pull request"})
    github.add(202, None)
    github.add(1, PROTOCOL_BODY, repository="owner/other")

    exit_code = migration.dry_run(REPOSITORY, "owner/other")

    assert exit_code == 0
    assert migration.manifest_rows() == [(REPOSITORY, 200), ("owner/other", 1)]
    assert "2 bodies to change, 0 refused" in capsys.readouterr().out
    assert github.body(200) == PROTOCOL_BODY


def test_apply_migrates_only_the_body_and_a_second_dry_run_reports_nothing_left(
    migration: Migration, capsys: pytest.CaptureFixture[str]
) -> None:
    github = migration.github
    github.add(1, PROTOCOL_BODY, state="closed", labels=["ready"], assignees=["someone"])
    github.add(2, PROTOCOL_BODY)
    migration.dry_run(REPOSITORY)

    exit_code = migration.apply()

    assert exit_code == 0
    assert github.issues[(REPOSITORY, 1)] == {
        "number": 1,
        "body": MIGRATED_BODY,
        "state": "closed",
        "labels": ["ready"],
        "assignees": ["someone"],
    }
    assert github.body(2) == MIGRATED_BODY
    migration.dry_run(REPOSITORY)
    assert "0 bodies to change, 0 refused" in capsys.readouterr().out


def _edit_issue_two(migration: Migration) -> None:
    migration.github.add(2, EDITED_BODY)


def _edit_issue_two_during_the_pace(migration: Migration) -> None:
    migration.clock.during_wait = lambda: migration.github.add(2, EDITED_BODY)


def _edit_issue_one_during_a_rate_limit_wait(migration: Migration) -> None:
    rate_limited = _included(429, {"message": "slow down"}, {"Retry-After": "1"})
    migration.github.patch_answers.append(rate_limited)
    migration.clock.during_wait = lambda: migration.github.add(1, EDITED_BODY)


def _store_bodies_altered(migration: Migration) -> None:
    migration.github.stored_suffix = "\n"


def _refuse_permission(migration: Migration) -> None:
    migration.github.patch_answers.append(_included(403, {"message": "Resource not accessible"}))


def _rate_limit_forever(migration: Migration) -> None:
    rate_limited = _included(429, {"message": "slow down"}, {"Retry-After": "1"})
    migration.github.patch_answers.extend([rate_limited] * (migrate.MAX_RATE_LIMIT_WAITS + 1))


@pytest.mark.parametrize(
    ("perturb", "stop", "kept"),
    [
        pytest.param(
            _edit_issue_two,
            "owner/repo#2: the body changed since the dry run",
            {2: EDITED_BODY, 3: PROTOCOL_BODY},
            id="drift",
        ),
        pytest.param(
            _edit_issue_two_during_the_pace,
            "owner/repo#2: the body changed since the dry run",
            {2: EDITED_BODY, 3: PROTOCOL_BODY},
            id="drift-during-the-pace",
        ),
        pytest.param(
            _edit_issue_one_during_a_rate_limit_wait,
            "owner/repo#1: the body changed since the dry run",
            {1: EDITED_BODY, 3: PROTOCOL_BODY},
            id="drift-during-a-rate-limit-wait",
        ),
        pytest.param(
            _store_bodies_altered,
            "owner/repo#1: the body read back does not have the new hash",
            {3: PROTOCOL_BODY},
            id="read-back-mismatch",
        ),
        pytest.param(
            _refuse_permission,
            "GitHub answered 403 to PATCH repos/owner/repo/issues/1",
            {3: PROTOCOL_BODY},
            id="refused-permission",
        ),
        pytest.param(
            _rate_limit_forever,
            f"PATCH repos/owner/repo/issues/1 still rate-limited after "
            f"{migrate.MAX_RATE_LIMIT_WAITS} waits",
            {3: PROTOCOL_BODY},
            id="rate-limit-never-lifts",
        ),
    ],
)
def test_apply_stops_at_the_first_unsafe_row_and_names_it(
    migration: Migration,
    capsys: pytest.CaptureFixture[str],
    perturb: Callable[[Migration], None],
    stop: str,
    kept: dict[int, str],
) -> None:
    github = migration.github
    for number in (1, 2, 3):
        github.add(number, PROTOCOL_BODY)
    migration.dry_run(REPOSITORY)
    perturb(migration)

    exit_code = migration.apply()

    assert exit_code == 1
    assert f"stopped: {stop}" in capsys.readouterr().err
    assert {number: github.body(number) for number in kept} == kept


@pytest.mark.parametrize(
    ("status", "headers", "wait", "pace_arguments", "pace"),
    [
        pytest.param(
            429, {"Retry-After": "30"}, 30.0, [], migrate.PACE_SECONDS, id="429-retry-after"
        ),
        pytest.param(
            403, {"Retry-After": "45"}, 45.0, ["--pace-seconds", "2.5"], 2.5, id="given-pace"
        ),
        pytest.param(
            403,
            {"X-Ratelimit-Remaining": "0", "X-Ratelimit-Reset": "1000090"},
            90.0,
            [],
            migrate.PACE_SECONDS,
            id="403-primary-limit-reset",
        ),
        pytest.param(
            429,
            {},
            migrate.FALLBACK_RATE_LIMIT_WAIT_SECONDS,
            [],
            migrate.PACE_SECONDS,
            id="429-without-headers",
        ),
    ],
)
def test_apply_paces_its_patches_and_waits_out_a_rate_limit(
    migration: Migration,
    status: int,
    headers: dict[str, str],
    wait: float,
    pace_arguments: list[str],
    pace: float,
) -> None:
    github = migration.github
    for number in (1, 2, 3):
        github.add(number, PROTOCOL_BODY)
    migration.dry_run(REPOSITORY)
    github.patch_answers.append(_included(status, {"message": "slow down"}, headers))

    exit_code = migration.apply(*pace_arguments)

    assert exit_code == 0
    assert migration.clock.waits == [wait, pace, pace]
    assert [github.body(number) for number in (1, 2, 3)] == [MIGRATED_BODY] * 3


def test_apply_resumes_from_its_manifest_after_a_stopped_run(
    migration: Migration, capsys: pytest.CaptureFixture[str]
) -> None:
    github = migration.github
    for number in (1, 2):
        github.add(number, PROTOCOL_BODY)
    migration.dry_run(REPOSITORY)
    github.patch_answers.extend([None, _included(502, {"message": "Bad Gateway"})])
    stopped_exit_code = migration.apply()

    exit_code = migration.apply()

    output = capsys.readouterr().out
    assert (stopped_exit_code, exit_code) == (1, 0)
    assert f"already migrated {REPOSITORY}#1" in output
    assert f"migrated {REPOSITORY}#2" in output
    assert [github.body(number) for number in (1, 2)] == [MIGRATED_BODY] * 2
