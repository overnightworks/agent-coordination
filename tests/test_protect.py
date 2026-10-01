"""Direct `protect` hook behavior: the pre-tool-use guard that denies a
mutating tool call outside a covering claim. Every test here drives
`issue_claim._protect`/`_protect_write` through `main(["protect"])` with the
hook's JSON payload on stdin (`_protect_main`) -- `protect` is the hook entry
point with its own doctrine, so this stays its owner file even though it
goes through `main`, unlike ordinary CLI-command tests in
`tests/test_cli.py`."""

from __future__ import annotations

import io
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from board_fixtures import BASE, REPOSITORY, _active_claim, complete_contract
from cli_fixtures import (
    RECORDED_ORIGIN_HEAD_READ,
    _forbid_forge_resolution,
    _forbid_github_construction,
    _forbid_protect_git_github_and_identity,
    _patch_command,
    _push_repository_trunk,
    _real_git,
    _real_repository_with_bare_remote,
    _set_agent_identity_env,
    stub_board_config_tracked,
)
from test_cli import FakeForge

from agent_coordination import (
    board,
    body,
    checkout,
    forge,
    github,
    hook_input,
    process,
    protect,
    protocol,
    store,
)
from agent_coordination import cli as issue_claim
from agent_coordination.protocol import ClaimError


@pytest.fixture(autouse=True)
def _stub_board_config_tracked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every `protect` test reads a tracked `board.toml` by default (issue
    #315): `_isolate_protect_home`'s `work` directory is never a real git
    checkout, so a real `git ls-files` check would otherwise always read
    "not tracked" here. A test proving the refusal itself overrides this."""
    stub_board_config_tracked(monkeypatch)


def _isolate_protect_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    return home, work


_PATH_CHECKOUT_ARGUMENTS = (
    "rev-parse",
    "--path-format=absolute",
    "--show-toplevel",
    "--git-dir",
    "--git-common-dir",
)


def _protect_git_values(
    work: Path,
    *,
    branch: str = "codex/issue-72-claims",
    git_directory: Path | None = None,
    common_directory: Path | None = None,
    origin_head: str | None = "refs/remotes/origin/main",
) -> dict[tuple[str, ...], str]:
    """The `_git_output` answers `checkout.resolve_path_checkout` needs for a
    write inside `work` (issue #314): a linked worktree by default, or a
    shared main checkout when `git_directory`/`common_directory` are pinned
    equal. Every payload path these tests use resolves to this one fake
    checkout regardless of the process's real cwd -- proving path-independence
    itself is the real-worktree proofs' job, below.

    `origin_head` is the resolved `origin/HEAD` symbolic ref gate G4 reads to
    determine the repository's own default branch; every fixture branch
    above but the explicit "not main" cases names a non-default branch, so a
    fixed `main` here never trips it by accident. `None` simulates a clone
    that never recorded one (gate G4's own unresolved case) -- omitted from
    this mapping entirely, so `_patch_protect_git` reads it as a denial
    rather than a missing fixture key."""
    resolved_git_directory = git_directory or (work / ".git" / "worktrees" / "issue-72")
    resolved_common_directory = common_directory or (work / ".git")
    origin_url = f"git@github.com:{REPOSITORY}.git"
    values = {
        ("branch", "--show-current"): branch,
        ("rev-parse", "--verify", "HEAD"): BASE,
        _PATH_CHECKOUT_ARGUMENTS: "\n".join(
            (str(work.resolve()), str(resolved_git_directory), str(resolved_common_directory))
        ),
        # The canonical-remote comparison (issue #176, Erwartung 6) reads this
        # to confirm the fake forge target (REPOSITORY) matches it.
        ("config", "--get", "remote.origin.url"): origin_url,
        # `checkout.unconfigured_remote_refusal` reads this to find the
        # canonical remote has a URL (issues #512, #516).
        ("config", "--get", "--default", "", "remote.origin.url"): origin_url,
    }
    if origin_head is not None:
        values[RECORDED_ORIGIN_HEAD_READ] = origin_head
    return values


def _patch_protect_git(
    monkeypatch: pytest.MonkeyPatch,
    work: Path,
    *,
    branch: str = "codex/issue-72-claims",
    git_directory: Path | None = None,
    common_directory: Path | None = None,
    origin_head: str | None = "refs/remotes/origin/main",
) -> None:
    """Fake `checkout._git_output` for exactly one checkout, rooted at
    `work` (issue #314 repeat gate, finding 2). The fake maps every queried
    `directory` to `work`'s own answers only when that directory *is* `work`
    or one of its descendants -- a payload path's parent, or `work` itself --
    an explicit `{directory: checkout values}` mapping in spirit even though
    every descendant of one root shares one git answer (real `git -C
    <any subdirectory>` does too). A directory outside that mapping (the
    hook process's own cwd, or any other stray location) fails the test
    loudly instead of silently being answered with `work`'s checkout, which
    is the one thing a directory-blind fake could never prove: that
    `protect` actually selects its checkout from the payload path, not from
    wherever it happened to ask."""
    values = _protect_git_values(
        work,
        branch=branch,
        git_directory=git_directory,
        common_directory=common_directory,
        origin_head=origin_head,
    )
    resolved_work = work.resolve()

    def git(arguments: list[str], *, directory: Path | None = None) -> str:
        if arguments == ["status", "--porcelain"]:
            pytest.fail("dirty tree is irrelevant to protect")
        if arguments == ["rev-parse", "HEAD"]:
            pytest.fail("protect must not bind HEAD to claim.base")
        if directory is None:
            # Issue #314 repeat gate, B5: `_patch_protect_git` must not answer
            # for a caller that never named a directory at all -- a resolver
            # that silently fell back to the calling process's own cwd (the
            # historical bug) must fail this fixture, not pass it by
            # accident. Which non-`None` directory is queried legitimately
            # varies per test (a payload path's own parent, or a resolved
            # checkout's own toplevel) -- `values` below, not this guard, is
            # what actually pins each one.
            pytest.fail(
                "protect read git with no directory at all (issue #314: every read must "
                "name the payload's own checkout, never the calling process's cwd)"
            )
        resolved_directory = directory.resolve()
        if resolved_directory != resolved_work and resolved_work not in resolved_directory.parents:
            pytest.fail(
                f"protect read git for {directory}, which this fixture does not map "
                f"(issue #314 repeat gate, finding 2): only {work} and its descendants "
                "are a known checkout here"
            )
        key = tuple(arguments)
        if key == RECORDED_ORIGIN_HEAD_READ and key not in values:
            # A real, unresolved `origin/HEAD` exits nonzero rather than
            # answering empty (measured locally, issue #238); `origin_head is
            # None` reproduces that shape rather than a missing-fixture-key
            # `KeyError`.
            raise ClaimError("unknown git failure")
        return values[key]

    monkeypatch.setattr(checkout, "_git_output", git)


def _protect_active_claim(
    agent: str,
    *,
    scope: tuple[str, ...] = ("src",),
    branch: str = "codex/issue-72-claims",
    lane: bool = False,
    issue: int = 72,
) -> protocol.ActiveClaim:
    return _active_claim(agent, scope=scope, branch=branch, lane=lane, issue=issue)


def _protect_state_with_claim(claim: protocol.ActiveClaim) -> protocol.ClaimState:
    key = protocol.claim_key(claim.identity, claim.branch)
    return protocol.ClaimState(tip=protocol.ObjectId(BASE), claims={key: claim})


def _patch_protect_claim(
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent: str = "Grok sess-1",
    scope: tuple[str, ...] = ("src",),
    branch: str = "codex/issue-72-claims",
    lane: bool = False,
) -> None:
    """Fake the store's fetched state with one live claim (issue #176):
    `protect` only ever reads `store.fetch_state`, so faking that boundary
    directly -- rather than a ledger comment `protect` no longer looks at --
    is the whole test double a `protect` test needs.
    """
    state = _protect_state_with_claim(
        _protect_active_claim(agent, scope=scope, branch=branch, lane=lane)
    )
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: state)


def _protect_main(monkeypatch: pytest.MonkeyPatch, payload: object) -> int:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    return issue_claim.main(["--repo", "example/agent-coordination", "protect"])


def _assert_protect_decision(
    capsys: pytest.CaptureFixture[str],
    *,
    decision: str,
    reason: str | None = None,
) -> None:
    captured = capsys.readouterr()
    if decision == "allow":
        assert (captured.out, captured.err) == ("", "")
        return
    assert (json.loads(captured.out), captured.err) == (
        {"decision": "deny", "reason": reason},
        f"{reason}\n",
    )


def test_protect_denied_checkout_validation_never_reads_the_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A write from the shared main checkout denies `not main` from
    checkout validation alone (gate G4), before any claim could possibly
    cover it -- a counting fake store, observed the same way gate G5's own
    `test_protect_apply_patch_fetches_store_state_once_per_repository` does
    (`len(fetch_calls) == 1` there), must stay at 0 reads here (issue #314
    repeat gate, finding 5): a resolver that read the store before checkout
    validation finished would pass every other test in this file by
    accident, since none of them assert the store was left untouched."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    git_directory = work / ".git"
    _patch_protect_git(
        monkeypatch, work, git_directory=git_directory, common_directory=git_directory
    )
    _forbid_github_construction(monkeypatch)
    state = _protect_state_with_claim(_protect_active_claim("Grok sess-1"))
    fetch_calls: list[Path] = []

    def counting_fetch_state(*, worktree: Path, remote: str) -> protocol.ClaimState:
        fetch_calls.append(worktree)
        return state

    monkeypatch.setattr(store, "fetch_state", counting_fetch_state)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason=checkout.PROTECT_NOT_MAIN_REASON)
    assert len(fetch_calls) == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"toolName": "Bash", "toolInput": {"command": "git diff"}},
        {"tool_name": "run_terminal_command", "tool_input": {"command": "git status"}},
        {"toolName": "Read", "toolInput": {"path": "src/secret.py"}},
        {"toolName": "read_file", "toolInput": {"path": "src/secret.py"}},
        {"tool_name": "grep", "tool_input": {"pattern": "secret"}},
        {"toolName": "list_dir", "toolInput": {"path": "src"}},
        {"tool_name": "spawn_subagent", "tool_input": {"prompt": "edit src"}},
        *(
            {"tool_name": name, "tool_input": {}}
            for name in (
                "Monitor",
                "ToolSearch",
                "SendMessage",
                "TaskStop",
                "TaskOutput",
                "StructuredOutput",
                "Skill",
                "AskUserQuestion",
                "ListAgents",
                "ScheduleWakeup",
                "SendFeedback",
                "Workflow",
                "Artifact",
            )
        ),
    ],
)
def test_protect_non_mutating_tools_allow_without_identity_git_or_github(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> None:
    home, work = _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch)
    _forbid_protect_git_github_and_identity(monkeypatch)

    assert _protect_main(monkeypatch, payload) == 0
    _assert_protect_decision(capsys, decision="allow")
    assert list(home.iterdir()) == []
    assert list(work.iterdir()) == []


@pytest.mark.parametrize(
    ("tool_name", "path_key"),
    [("write", "path"), ("search_replace", "filePath")],
)
def test_protect_grok_camelcase_allows_when_session_claim_covers_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    tool_name: str,
    path_key: str,
) -> None:
    home, work = _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": tool_name, "toolInput": {path_key: str(work / "src/widget.py")}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")
    assert list(home.iterdir()) == []


def test_protect_allows_a_lane_claim_covering_the_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Guardrail (Entschieden #6): `_protect_write` already authorizes purely via
    agent/branch/scope, so a lane claim (no GitHub issue at all) passes through it
    unchanged, with no code path change required."""
    home, work = _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work, branch="docs/lane-cleanup")
    _patch_protect_claim(monkeypatch, branch="docs/lane-cleanup", lane=True)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")
    assert list(home.iterdir()) == []


def test_protect_grok_camelcase_denies_write_without_this_session_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, work = _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, agent="Codex Sol")

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")
    assert list(home.iterdir()) == []


def test_protect_absolute_file_path_allows_when_claim_scope_covers_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)
    target = work / "src" / "agent_coordination" / "cli.py"

    assert (
        _protect_main(
            monkeypatch,
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(target.resolve())},
            },
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")


def test_protect_dirty_worktree_still_allows_covered_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    (work / "dirty.txt").write_text("edited\n", encoding="utf-8")
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")


def test_protect_no_matching_claim_denies_claim_first(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    state = protocol.ClaimState(tip=protocol.ObjectId(BASE))
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: state)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")


@pytest.mark.parametrize(
    "payload",
    ["not-json", "[]", "null", "1", '{"toolName": 1}', "{}"],
)
def test_protect_invalid_hook_payload_denies_without_raising(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload: str,
) -> None:
    home, work = _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch)
    _forbid_protect_git_github_and_identity(monkeypatch)

    assert _protect_main(monkeypatch, payload) == 2
    _assert_protect_decision(capsys, decision="deny", reason="invalid hook payload")
    assert list(home.iterdir()) == []
    assert list(work.iterdir()) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"toolName": "Write"},
        {"tool_name": "Edit", "tool_input": "src/widget.py"},
        {"toolName": "MultiEdit", "toolInput": {"contents": "x"}},
        {"toolName": "write", "toolInput": {"path": "", "file_path": ""}},
        {
            "toolName": "apply_patch",
            "toolInput": {"command": "*** Begin Patch\n*** End Patch"},
        },
        {"toolName": "apply_patch", "toolInput": {"command": "not a patch at all"}},
        {"toolName": "apply_patch", "toolInput": {}},
    ],
)
def test_protect_mutating_tool_without_path_denies_path_required(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch)
    _forbid_protect_git_github_and_identity(monkeypatch)

    assert _protect_main(monkeypatch, payload) == 2
    _assert_protect_decision(capsys, decision="deny", reason="path required")


@pytest.mark.parametrize(
    ("identity_variable", "claim_holder"),
    [
        pytest.param(checkout.ACO_AGENT_ENV, "sess-1", id="aco-agent"),
        pytest.param(checkout.GROK_SESSION_ID_ENV, "Grok sess-1", id="grok-session"),
        pytest.param(
            checkout.CLAUDE_CODE_SESSION_ID_ENV, "Claude sess-1", id="claude-code-session"
        ),
    ],
)
def test_protect_each_session_variable_alone_identifies_the_claim_holder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    identity_variable: str,
    claim_holder: str,
) -> None:
    """PROT-08 (issue #454): the session variable Claude Code actually sets,
    `CLAUDE_CODE_SESSION_ID`, identifies its session on its own, like the
    other two, so a covered write from the claimed worktree allows."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {identity_variable: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, agent=claim_holder)

    assert (
        _protect_main(
            monkeypatch,
            {"tool_name": "Write", "tool_input": {"file_path": str(work / "src/widget.py")}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")


@pytest.mark.parametrize(
    "environ",
    [
        pytest.param({}, id="no-variable"),
        pytest.param({"CLAUDE_SESSION_ID": "sess-1"}, id="retired-claude-session-id-only"),
    ],
)
def test_protect_missing_identity_denies_a_claimable_write_without_github(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    environ: dict[str, str],
) -> None:
    """PROT-08 (issue #448): identity resolves last, once the path's own
    linked worktree and its live state are in hand -- a write that reaches
    a claim check with no `ACO_AGENT`, `GROK_SESSION_ID`, or
    `CLAUDE_CODE_SESSION_ID` denies naming all three, never GitHub -- and never
    `--agent`, a flag the hook line does not have. The retired
    `CLAUDE_SESSION_ID` names no identity either (issue #454, no
    compatibility layer)."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, environ)
    _forbid_github_construction(monkeypatch)
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, scope=("src",))

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 2
    )
    _assert_protect_decision(
        capsys,
        decision="deny",
        reason=(
            "agent identity is required: set ACO_AGENT, GROK_SESSION_ID, or "
            "CLAUDE_CODE_SESSION_ID (ACO_AGENT can sit in the hook line)"
        ),
    )


@pytest.mark.parametrize(
    "payload_for_work",
    [
        lambda work: {
            "toolName": "apply_patch",
            "toolInput": {
                "command": _patch_command(
                    f"*** Update File: {work / 'src/widget.py'}", "@@", "-old", "+new"
                )
            },
        },
        lambda work: {
            "tool_name": "NotebookEdit",
            "tool_input": {
                "notebook_path": str(work / "notebook.ipynb"),
                "new_source": "print(1)",
                "cell_type": "code",
                "edit_mode": "replace",
            },
        },
    ],
    ids=["apply_patch", "notebook_edit"],
)
def test_protect_extended_mutating_tools_deny_on_main_without_a_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload_for_work: Callable[[Path], dict[str, object]],
) -> None:
    """`apply_patch` (Codex) and `NotebookEdit` (Claude Code) joined the
    mutating table (issue #238): both are gated exactly like `Write`, denied
    from the shared main checkout before a claim is even looked up. The
    payload's own path must be absolute (issue #314 delta, finding R2), so
    each case builds it from `work` -- not known until the test itself picks
    a fresh `tmp_path` -- rather than at collection time."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(
        monkeypatch, work, git_directory=work / ".git", common_directory=work / ".git"
    )
    _forbid_github_construction(monkeypatch)

    assert _protect_main(monkeypatch, payload_for_work(work)) == 2
    _assert_protect_decision(capsys, decision="deny", reason="not main")


@pytest.mark.parametrize(
    ("notebook_path", "decision", "reason"),
    [
        ("src/widget.ipynb", "allow", None),
        ("docs/widget.ipynb", "deny", "claim first"),
    ],
)
def test_protect_notebook_edit_reads_notebook_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    notebook_path: str,
    decision: str,
    reason: str | None,
) -> None:
    """Claude Code's `NotebookEdit` carries its target under `notebook_path`,
    not `path`/`file_path`/`filePath` (issue #252) -- the real payload shape,
    checked against the claim scope exactly like any other mutating tool."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)

    exit_code = _protect_main(
        monkeypatch,
        {
            "tool_name": "NotebookEdit",
            "tool_input": {
                "notebook_path": str(work / notebook_path),
                "new_source": "print(1)",
                "cell_type": "code",
                "edit_mode": "replace",
            },
        },
    )

    assert exit_code == (0 if decision == "allow" else 2)
    _assert_protect_decision(capsys, decision=decision, reason=reason)


def test_protect_notebook_edit_ignores_a_decoy_path_key_it_never_sends(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`NotebookEdit` reads only `notebook_path` (issue #252): an in-scope
    `path` sitting next to an out-of-scope `notebook_path` -- a key this tool
    never actually sends -- must not smuggle the real target past the claim
    check the way a first-wins generic key list would."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)

    exit_code = _protect_main(
        monkeypatch,
        {
            "tool_name": "NotebookEdit",
            "tool_input": {
                "path": str(work / "src/widget.py"),
                "notebook_path": str(work / "docs/widget.ipynb"),
                "new_source": "print(1)",
                "cell_type": "code",
                "edit_mode": "replace",
            },
        },
    )

    assert exit_code == 2
    _assert_protect_decision(capsys, decision="deny", reason="claim first")


def test_protect_apply_patch_allows_when_every_touched_path_is_in_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Codex's `apply_patch` carries a patch-text `command`, not a path key
    (issue #252), and can touch several files in one call: every one of them
    must be in scope, not just the first."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)
    command = _patch_command(
        f"*** Update File: {work / 'src/widget.py'}",
        "@@",
        "-old",
        "+new",
        f"*** Add File: {work / 'src/new_module.py'}",
        "+content",
    )

    assert (
        _protect_main(monkeypatch, {"toolName": "apply_patch", "toolInput": {"command": command}})
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")


@pytest.mark.parametrize(
    ("line_templates", "outside_path"),
    [
        (
            (
                "*** Update File: {work}/src/widget.py",
                "@@",
                "-old",
                "+new",
                "*** Add File: {work}/docs/widget.md",
                "+content",
            ),
            "docs/widget.md",
        ),
        (
            (
                "*** Update File: {work}/src/widget.py",
                "*** Move to: {work}/docs/widget.py",
                "@@",
                "-old",
                "+new",
            ),
            "docs/widget.py",
        ),
    ],
)
def test_protect_apply_patch_denies_naming_the_first_path_outside_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    line_templates: tuple[str, ...],
    outside_path: str,
) -> None:
    """A multi-file `apply_patch` call names the specific file outside the
    claim scope -- unlike a single-path write's generic `claim first` -- since
    the hook payload doesn't otherwise say which of several files was the
    problem (issue #252). Covers both a plain outside path and a `Move to:`
    rename landing outside scope. `line_templates` hold `{work}` -- the
    checkout, not known until the test picks its own `tmp_path` -- rather
    than a path already absolute (issue #314 delta, finding R2)."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)
    lines = (line.format(work=work) for line in line_templates)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "apply_patch", "toolInput": {"command": _patch_command(*lines)}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason=f"{outside_path} outside claim scope")


def test_protect_apply_patch_denies_an_indented_header_smuggled_after_add_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Outside an Update hunk, Codex recognises a header after trimming both
    ends of the line (issue #252's Grok finding): an indented
    `*** Update File:` line right after an in-scope `Add File` block still
    names a real file Codex will write, so the naive `startswith` scan that
    missed it -- letting it slip past the claim check -- is the vulnerability
    this pins shut."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, scope=("README.md",))
    command = _patch_command(
        f"*** Add File: {work / 'README.md'}",
        "+content",
        f"  *** Update File: {work / 'docs/evil.md'}",
        "@@",
        "-old",
        "+new",
    )

    assert (
        _protect_main(monkeypatch, {"toolName": "apply_patch", "toolInput": {"command": command}})
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="docs/evil.md outside claim scope")


def test_protect_apply_patch_denies_with_claim_first_when_no_session_claim_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no live claim for this session at all -- as opposed to a live
    claim whose scope simply misses one of the patch's paths -- the repair
    sentence is the same `claim first` a single-path write gets (issue #252):
    naming a path as 'outside claim scope' would be false when there is no
    claim to be outside of."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, agent="Codex Sol")
    command = _patch_command(f"*** Update File: {work / 'src/widget.py'}", "@@", "-old", "+new")

    assert (
        _protect_main(monkeypatch, {"toolName": "apply_patch", "toolInput": {"command": command}})
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")


_BASH_WRITE_COMMAND_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("cat > {path} <<EOF\ncontent\nEOF", hook_input.PATTERN_REDIRECT_OVERWRITE),
    ("echo hi >> {path}", hook_input.PATTERN_REDIRECT_APPEND),
    ("tee {path}", hook_input.PATTERN_TEE),
    ("sed -i 's/a/b/' {path}", hook_input.PATTERN_SED_IN_PLACE),
    ("mv {path} src/renamed.py", hook_input.PATTERN_MOVE),
    ("cp src/source.py {path}", hook_input.PATTERN_COPY),
    ("rm {path}", hook_input.PATTERN_REMOVE),
    ("git checkout -- {path}", hook_input.PATTERN_GIT_CHECKOUT),
    ("git restore {path}", hook_input.PATTERN_GIT_RESTORE),
)


def _write_target_payload(target: Path) -> dict[str, object]:
    return {"toolName": "write", "toolInput": {"path": str(target)}}


def _bash_rm_target_payload(target: Path) -> dict[str, object]:
    return {"toolName": "Bash", "toolInput": {"command": f"rm {target}"}}


_TARGET_PATH_PAYLOAD_BUILDERS = (_write_target_payload, _bash_rm_target_payload)


def _monitor_rm_target_payload(target: Path) -> dict[str, object]:
    return {"tool_name": "Monitor", "tool_input": {"command": f"rm {target}"}}


_BASH_OUTSIDE_SCOPE_PATH = "docs/widget.md"
_BASH_INSIDE_SCOPE_PATH = "src/widget.py"


def _bash_scope_case(
    template: str, pattern: str, *, path: str, denied: bool
) -> tuple[str, int, str | None]:
    command = template.format(path=path)
    if not denied:
        return command, 0, None
    return command, 2, f"{pattern} {path} outside claim scope"


@pytest.mark.parametrize(
    ("command", "status", "reason"),
    [
        *(
            _bash_scope_case(template, pattern, path=_BASH_OUTSIDE_SCOPE_PATH, denied=True)
            for template, pattern in _BASH_WRITE_COMMAND_TEMPLATES
        ),
        *(
            _bash_scope_case(template, pattern, path=_BASH_INSIDE_SCOPE_PATH, denied=False)
            for template, pattern in _BASH_WRITE_COMMAND_TEMPLATES
        ),
    ],
)
def test_protect_bash_judges_a_recognized_pattern_path_by_claim_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    status: int,
    reason: str | None,
) -> None:
    """Issue #380: every write pattern `hook_input.hook_command_paths`
    recognizes runs the same Checkout/Default-Branch/Claim-Scope chain as
    an `Edit` path, resolved against the payload's own `cwd` since Bash's
    own paths are relative (PROT-31). A path inside the live claim's scope
    allows exactly like a covered `Edit` path; one outside it denies naming
    both the pattern and the path (PROT-33) -- for `mv` reporting its own
    (untouched) source rather than its in-scope destination, since `mv`
    judges every operand; `cp` here is tested on its destination alone
    (issue #380 delta), the only operand it actually writes."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, scope=("src",))

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "Bash", "toolInput": {"command": command}, "cwd": str(work)},
        )
        == status
    )
    _assert_protect_decision(capsys, decision="deny" if reason else "allow", reason=reason)


def test_protect_bash_allows_a_relative_path_when_the_payload_carries_no_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PROT-31's second half: a relative Bash-recognized path with no `cwd`
    in the payload at all cannot be resolved without guessing, and a wrong
    guess would deny legitimate work `protect` has no way to tell from a
    real out-of-scope write -- so it allows rather than risk a false deny
    (`specs/protect.spec.md`'s own `## Never`). No claim is set up at all:
    a resolver that fell back to guessing a cwd would deny `claim first`
    here instead of allowing. `_forbid_protect_git_github_and_identity`'s
    own `session_agent` stub is left in place, unlike the sibling tests
    below: this path must never resolve identity at all (issue #380 delta,
    review finding: resolving it eagerly, before this allow, used to turn an
    unresolvable identity into a wrongful PROT-08 deny here)."""
    _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _forbid_protect_git_github_and_identity(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "Bash", "toolInput": {"command": "rm docs/widget.md"}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")


def test_protect_bash_denies_a_relative_path_resolved_against_the_payloads_own_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The companion to the no-`cwd` allow above: once a `cwd` is given, a
    relative path resolves against it and is judged exactly like an
    already-absolute one, denying when it sits outside the live claim."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, scope=("src",))

    assert (
        _protect_main(
            monkeypatch,
            {
                "toolName": "Bash",
                "toolInput": {"command": "rm docs/widget.md"},
                "cwd": str(work),
            },
        )
        == 2
    )
    _assert_protect_decision(
        capsys, decision="deny", reason="rm docs/widget.md outside claim scope"
    )


def test_protect_bash_cd_changes_the_directory_for_the_rest_of_the_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #380 delta, decision 4: a literal `cd <path> &&` changes the
    directory a later relative path resolves against -- even into a
    different checkout than the payload's own `cwd`, judged there exactly
    as an absolute path in that checkout would be. Were `cd` not tracked,
    `docs/widget.md` would resolve under the payload's own `cwd`
    (`elsewhere`, no checkout at all) and allow outright instead."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, scope=("src",))

    assert (
        _protect_main(
            monkeypatch,
            {
                "toolName": "Bash",
                "toolInput": {"command": f"cd {work} && rm docs/widget.md"},
                "cwd": str(elsewhere),
            },
        )
        == 2
    )
    _assert_protect_decision(
        capsys, decision="deny", reason="rm docs/widget.md outside claim scope"
    )


def test_protect_bash_allows_a_command_with_no_recognized_pattern_without_identity_or_git(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PROT-30: `grep`, `ls`, `pytest`, and `git diff` name no recognized
    write pattern at all, so `protect` never even resolves identity, git,
    or the store for them -- the same "cannot judge what it cannot see"
    limit README documents for the read-only tools (issue #380)."""
    _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch)
    _forbid_protect_git_github_and_identity(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "Bash", "toolInput": {"command": "grep foo bar.py && ls && pytest"}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")


@pytest.mark.parametrize(
    "tool_input",
    [{}, {"command": 1}],
    ids=["missing-command", "non-string-command"],
)
def test_protect_bash_allows_a_missing_or_non_string_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    tool_input: dict[str, object],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch)
    _forbid_protect_git_github_and_identity(monkeypatch)

    assert _protect_main(monkeypatch, {"toolName": "Bash", "toolInput": tool_input}) == 0
    _assert_protect_decision(capsys, decision="allow")


def test_protect_unknown_tool_name_denies_with_a_repair_sentence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A tool name in neither the read nor the mutating table fails closed
    (issue #238) instead of the old default-allow, and the refusal names the
    table to extend rather than a bare 'unknown tool'."""
    _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch)
    _forbid_protect_git_github_and_identity(monkeypatch)

    assert _protect_main(monkeypatch, {"toolName": "invented_tool"}) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["decision"] == "deny"
    assert captured.err == f"{payload['reason']}\n"
    assert "invented_tool" in payload["reason"]
    assert "HOOK_TOOL_EFFECTS" in payload["reason"]
    assert "238" in payload["reason"]


def _documented_hook_matcher() -> str:
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    write_gate_section = readme.split("## PreToolUse write gate", 1)[1]
    hook_json = write_gate_section.split("```json", 1)[1].split("```", 1)[0]
    (entry,) = json.loads(hook_json)["hooks"]["PreToolUse"]
    return entry["matcher"]


def test_documented_hook_matcher_names_exactly_the_tools_the_table_gates() -> None:
    """PROT-37 (issue #448): the README's matcher keeps every other tool --
    MCP tools, plan mode, task lists -- from reaching `protect`'s fail-closed
    PROT-06, so it must name every tool the table does not clear as read-only,
    and nothing else, or a new gated tool slips past the hook."""
    writing_tools = {
        name
        for name, effect in protect.HOOK_TOOL_EFFECTS.items()
        if effect is not protect.HookToolEffect.READ
    }

    assert set(_documented_hook_matcher().split("|")) == writing_tools


def test_protect_primary_checkout_denies_not_main_without_github(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A payload path resolved into the shared main checkout denies `not
    main` (issue #314) regardless of what branch that checkout happens to be
    on -- `git-dir == git-common-dir` is the whole test, not a branch name."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    git_directory = work / ".git"
    _patch_protect_git(
        monkeypatch, work, git_directory=git_directory, common_directory=git_directory
    )
    _forbid_github_construction(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="not main")


@pytest.mark.parametrize(
    ("branch", "origin_head", "expected_reason"),
    [
        ("main", "refs/remotes/origin/main", "not main"),
        ("master", "refs/remotes/origin/master", "not main"),
        ("trunk", "refs/remotes/origin/trunk", "not main"),
        ("codex/issue-72-claims", None, checkout.DEFAULT_BRANCH_UNKNOWN_REASON),
    ],
    ids=["main", "master", "trunk", "unresolved-origin-head"],
)
def test_protect_denies_not_main_for_a_custom_or_unresolved_default_branch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    branch: str,
    origin_head: str | None,
    expected_reason: str,
) -> None:
    """Gate G4's own retained matrix (issue #238, restored by the #314
    repeat gate and delta review): `protect` reads the repository's default
    branch the same way `claim` does -- a repository whose `origin/HEAD`
    names `trunk` denies a write from `trunk`, not just from the hardcoded
    `main`/`master` -- and a checkout whose `origin/HEAD` cannot be resolved
    at all denies outright, never falling back to that hardcoded guess the
    way `claim`'s own precondition does."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work, branch=branch, origin_head=origin_head)
    _forbid_github_construction(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason=expected_reason)


def _checkout_root_reason(root: Path) -> str:
    return f"{root} is the checkout root itself"


@pytest.mark.parametrize("payload_for", _TARGET_PATH_PAYLOAD_BUILDERS, ids=["write", "bash-rm"])
def test_protect_path_resolving_to_the_checkout_root_denies_naming_the_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload_for: Callable[[Path], dict[str, object]],
) -> None:
    """PROT-14 fires when the payload path's own checkout resolves
    (issue #314 repeat gate, finding 2 fallout: the pre-fix fake answered
    any directory, including one genuinely outside `work`, with `work`'s own
    checkout -- masking that this scenario needs a *real* descendant of the
    checkout, not an outside path, to reach this denial at all) but the path
    itself resolves to exactly the checkout root: `work/subdir/..` queries
    git from the real descendant `work/subdir`, so the checkout resolves
    fine, while the full path resolves to `work` itself, named in the
    sentence `rescope` refuses it with too (issue #483). A Bash-recognized
    path runs the identical gate (issue #380)."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _forbid_github_construction(monkeypatch)

    assert _protect_main(monkeypatch, payload_for(work / "subdir" / "..")) == 2
    _assert_protect_decision(capsys, decision="deny", reason=_checkout_root_reason(work))


@pytest.mark.parametrize(
    ("branch", "scope", "decision", "reason"),
    [
        pytest.param("codex/issue-72-claims", ("src",), "allow", None, id="matching-claim"),
        pytest.param("other/issue-72", ("src",), "deny", "claim first", id="wrong-branch"),
        pytest.param(
            "codex/issue-72-claims", ("docs",), "deny", "claim first", id="non-overlapping-scope"
        ),
    ],
)
def test_protect_write_decision_reflects_the_live_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    branch: str,
    scope: tuple[str, ...],
    decision: str,
    reason: str | None,
) -> None:
    """One live claim, three shapes (issue #314 repeat gate, finding 5): the
    default claim covers the write (`allow`); a claim on a different branch,
    or one whose scope excludes the payload path, each deny `claim first` --
    `protect` cannot tell an agent with no claim at all from one holding a
    claim that plainly does not cover this write, so both share one reason.
    This folds a former standalone allow-path test (which pinned identity,
    then git, then store as an exact call sequence -- not a contract either
    `protect` or its caller promises) together with its two sibling
    `claim first` denials, which already differed from each other only in
    which claim field misses: the same shape repeated for a fourth verdict
    is exactly the near-identical-copy case a parametrized table replaces,
    not three more functions. The companion proof below,
    `test_protect_denied_checkout_validation_never_reads_the_store`, covers
    the one part of that removed ordering that *is* observable behavior: a
    denial before the checkout resolves must never touch the store at
    all."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, branch=branch, scope=scope)

    status = _protect_main(
        monkeypatch,
        {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
    )

    assert status == (0 if decision == "allow" else 2)
    _assert_protect_decision(capsys, decision=decision, reason=reason)


def test_protect_claim_error_from_write_path_denies_json_without_error_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A `ClaimError` raised before the store is ever reached (here, reading
    the repository's board configuration -- `protect` is forge-free, issue
    #245, so it never resolves a forge target at all) denies with its own
    bare text -- only a failure inside `store.fetch_state` itself gets the
    'cannot reach refs/aco/state' wrapping (see the dedicated store-refusal
    tests below)."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)

    def failed(*_args: object, **_kwargs: object) -> board.BoardConfig:
        raise ClaimError("adapter failed")

    monkeypatch.setattr(board, "load_config", failed)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="adapter failed")


def test_protect_non_claim_error_from_write_path_denies_json_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)

    def crashed(*_args: object, **_kwargs: object) -> board.BoardConfig:
        raise RuntimeError("write path crashed")

    monkeypatch.setattr(board, "load_config", crashed)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="write path crashed")


@pytest.mark.parametrize("payload_for", _TARGET_PATH_PAYLOAD_BUILDERS, ids=["write", "bash-rm"])
@pytest.mark.parametrize(
    ("failure", "match"),
    [
        pytest.param(
            protocol.ClaimError("auth or transport failure"),
            "auth or transport failure",
            id="unreachable",
        ),
        pytest.param(protocol.MalformedStateTreeError("bad tree"), "bad tree", id="malformed"),
        pytest.param(protocol.StateLineageError("rewritten"), "rewritten", id="lineage"),
        pytest.param(protocol.ClaimError("cannot fetch"), "cannot fetch", id="fetch-failure"),
    ],
)
def test_protect_maps_every_store_error_to_cannot_reach_the_state_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failure: protocol.ClaimError,
    match: str,
    payload_for: Callable[[Path], dict[str, object]],
) -> None:
    """Issue #380: a Bash-recognized path shares this same store-failure
    mapping, since it reads the identical `_protect_cached_claim_state_or_denial`."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)

    def fake_fetch_state(*, worktree: Path, remote: str) -> protocol.ClaimState:
        raise failure

    monkeypatch.setattr(store, "fetch_state", fake_fetch_state)

    assert _protect_main(monkeypatch, payload_for(work / "src/widget.py")) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["decision"] == "deny"
    assert payload["reason"].startswith(f"cannot reach {store.STATE_REF}: ")
    assert match in payload["reason"]


def test_protect_apply_patch_maps_a_store_error_to_cannot_reach_the_state_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`apply_patch`'s multi-path store check (issue #252) fails closed on a
    store read error exactly like the single-path check above -- it shares
    `_protect_fetch_claim_state` rather than re-deciding this on its own."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)

    def fake_fetch_state(*, worktree: Path, remote: str) -> protocol.ClaimState:
        raise ClaimError("cannot fetch")

    monkeypatch.setattr(store, "fetch_state", fake_fetch_state)
    command = _patch_command(f"*** Update File: {work / 'src/widget.py'}", "@@", "-old", "+new")

    assert (
        _protect_main(monkeypatch, {"toolName": "apply_patch", "toolInput": {"command": command}})
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "decision": "deny",
        "reason": f"cannot reach {store.STATE_REF}: cannot fetch",
    }


def test_protect_missing_state_ref_denies_cannot_reach(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    monkeypatch.setattr(store, "fetch_state", lambda **_k: protocol.EMPTY_STATE)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "decision": "deny",
        "reason": f"cannot reach {store.STATE_REF}: {protocol.MISSING_STATE_REF}",
    }


def test_protect_allow_is_forge_free_against_a_non_github_remote(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, work = _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)
    _forbid_forge_resolution(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")


def test_protect_deny_is_forge_free_against_a_non_github_remote(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, work = _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, agent="Codex Sol")
    _forbid_forge_resolution(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")


# Issue #314's own proofs: real linked worktrees (`git worktree add`), never
# `_git_output` mocking, since the whole point is that `protect`/`rescope`
# judge the payload's own checkout -- proving that requires a resolver that
# actually looks at different real directories, which a fake indifferent to
# `directory` cannot exercise.


_REAL_PATH_IS_TRACKED = checkout.path_is_tracked


def _use_real_path_is_tracked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo this module's autouse tracked-`board.toml` stub (issue #314
    gate B3): a real-worktree test builds an actual fixture repository, so
    `path_is_tracked` must read that repository's real git index -- via the
    payload checkout's own resolved directory -- rather than the stub every
    other (non-git) protect test relies on."""
    monkeypatch.setattr(checkout, "path_is_tracked", _REAL_PATH_IS_TRACKED)


def _protect_real_repo_with_worktree(
    tmp_path: Path, *, slug: str = "issue-72-widget"
) -> tuple[Path, Path]:
    """A real repository (`main`, reused across worktrees) with one linked,
    isolated worktree on a feature branch -- the same `git worktree add`
    recipe `checkout.ISOLATED_WORKTREE_RECIPE` documents. `main` carries a
    real, tracked (`git add -f`) empty `.agent-claim/board.toml` (issue #314
    gate B3): every worktree shares `main`'s history, so `path_is_tracked`
    reads a real "tracked" answer for it from any of them, via
    `_use_real_path_is_tracked`. `main` also carries a real, resolvable
    `origin/HEAD` (gate G4): every worktree's own feature branch reads a
    real default-branch name through it, rather than denying "default
    branch unknown" -- a repository with no recorded `origin/HEAD` at all is
    its own dedicated proof, `_real_worktree_on_default_branch_target`."""
    main = tmp_path / "repo"
    if not main.exists():
        main.mkdir()
        _real_git(main, "init", "-q", "-b", "main")
        _real_git(main, "config", "user.name", "Test")
        _real_git(main, "config", "user.email", "test@example.com")
        (main / "README.md").write_text("hello\n")
        (main / ".agent-claim").mkdir()
        (main / ".agent-claim" / "board.toml").write_text("")
        _real_git(main, "add", "-f", "README.md", ".agent-claim/board.toml")
        _real_git(main, "commit", "-q", "-m", "initial")
        _real_git(main, "remote", "add", "origin", "https://example.invalid/example/repo.git")
        _real_git(main, "update-ref", "refs/remotes/origin/main", "HEAD")
        _real_git(main, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    worktree = tmp_path / "repo-worktrees" / slug
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _real_git(main, "worktree", "add", "-q", str(worktree), "-b", f"codex/{slug}")
    (worktree / "src").mkdir()
    (worktree / "docs").mkdir()
    return main, worktree


def test_protect_bash_denies_deleting_a_linked_worktrees_own_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PROT-14 reused for a Bash-recognized path (issue #380 delta, gate
    finding): a path naming a linked worktree's own root directory exactly
    -- `rm -rf ../<repo>-worktrees/issue-1-x` -- has a *parent*
    (`<repo>-worktrees/`) that is never itself a git checkout, so resolving
    the checkout from only the parent finds nothing. Trying the path itself
    too still finds that checkout and denies it as that checkout's own root
    (PROT-14), rather than silently allowing the whole checkout's deletion
    through PROT-32's "outside every repository" allow."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _use_real_path_is_tracked(monkeypatch)
    main, worktree = _protect_real_repo_with_worktree(tmp_path)
    monkeypatch.chdir(main)

    assert (
        _protect_main(
            monkeypatch,
            {
                "toolName": "Bash",
                "toolInput": {"command": f"rm -rf {worktree}"},
                "cwd": str(main),
            },
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason=_checkout_root_reason(worktree))


def test_protect_bash_judges_an_ordinary_directory_by_its_own_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A real directory that is *not* itself a checkout root -- `rm -rf
    <worktree>/docs` -- still reaches the ordinary claim-scope gate rather
    than PROT-14's checkout-root denial: `_protect_checkout_denial`'s own
    directory branch finds it is not the root it names, so it falls back to
    the cheaper parent-first lookup exactly like a non-directory path
    would (issue #380 delta, gate finding: `resolve_path_checkout(path)`
    only decides the root question, never replaces the parent lookup for
    every other directory)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _use_real_path_is_tracked(monkeypatch)
    main, worktree = _protect_real_repo_with_worktree(tmp_path)
    monkeypatch.chdir(main)
    _patch_protect_claim(monkeypatch, branch="codex/issue-72-widget", scope=("src",))

    assert (
        _protect_main(
            monkeypatch,
            {
                "toolName": "Bash",
                "toolInput": {"command": f"rm -rf {worktree / 'docs'}"},
                "cwd": str(main),
            },
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="rm docs outside claim scope")


def _protect_real_repo_with_nested_worktree(
    tmp_path: Path, *, slug: str = "issue-1-x"
) -> tuple[Path, Path]:
    """A real repository (`outer`) with one linked worktree nested *inside*
    its own working tree (`outer/nested-worktrees/<slug>`), unlike
    `_protect_real_repo_with_worktree`'s sibling layout: the nested root's
    own parent directory sits inside `outer`'s checkout, so `git -C parent`
    resolves to `outer` rather than to nothing -- the shape PROT-36 (issue
    #380 round 4, gate finding) exists for, since a parent-first lookup
    would otherwise let `outer`'s own checkout silently answer for a path
    that is itself a different, nested checkout's own root."""
    outer = tmp_path / "outer-repo"
    outer.mkdir()
    _real_git(outer, "init", "-q", "-b", "main")
    _real_git(outer, "config", "user.name", "Test")
    _real_git(outer, "config", "user.email", "test@example.com")
    (outer / "README.md").write_text("hello\n")
    (outer / ".agent-claim").mkdir()
    (outer / ".agent-claim" / "board.toml").write_text("")
    _real_git(outer, "add", "-f", "README.md", ".agent-claim/board.toml")
    _real_git(outer, "commit", "-q", "-m", "initial")
    _real_git(outer, "remote", "add", "origin", "https://example.invalid/example/repo.git")
    _real_git(outer, "update-ref", "refs/remotes/origin/main", "HEAD")
    _real_git(outer, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    nested = outer / "nested-worktrees" / slug
    nested.parent.mkdir(parents=True, exist_ok=True)
    _real_git(outer, "worktree", "add", "-q", str(nested), "-b", f"codex/{slug}")
    return outer, nested


@pytest.mark.parametrize("payload_for", _TARGET_PATH_PAYLOAD_BUILDERS, ids=["write", "bash-rm"])
@pytest.mark.parametrize(
    "root_form",
    [lambda root: root, lambda root: root / ".." / root.name, lambda root: root / "."],
    ids=["exact", "dot-dot", "dot"],
)
def test_protect_denies_deleting_a_nested_worktrees_own_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload_for: Callable[[Path], dict[str, object]],
    root_form: Callable[[Path], Path],
) -> None:
    """PROT-36 (issue #380 round 4, gate finding): a linked worktree whose
    own root's *parent* directory sits inside another, outer git checkout
    must still be judged by its own checkout -- resolved directly from the
    root itself, before the outer checkout's parent-first lookup ever gets
    a say -- so deleting it still denies naming that root (PROT-14) rather
    than the outer checkout's own scope silently authorizing it. Proven for
    both a payload path (`Write`, the accepted Edit-family behaviour change
    this round) and a Bash-recognized one (`rm`), since both share the same
    checkout-resolution chain, and for a lexically equivalent but
    unnormalized spelling of the same root (`nested/../nested`, `nested/.`)
    -- a covering outer claim must not stand in for the nested checkout
    root's own PROT-14 gate just because the payload path never collapsed
    its own `..`/`.` segments (issue #380 round 4 delta, gate finding 7)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _use_real_path_is_tracked(monkeypatch)
    outer, nested = _protect_real_repo_with_nested_worktree(tmp_path)
    monkeypatch.chdir(outer)
    payload = payload_for(root_form(nested))
    if payload["toolName"] == "Bash":
        payload["cwd"] = str(outer)

    assert _protect_main(monkeypatch, payload) == 2
    _assert_protect_decision(capsys, decision="deny", reason=_checkout_root_reason(nested))


@pytest.mark.parametrize(
    "cwd_kind",
    ["main_default_branch", "main_other_branch", "outside_any_repository", "another_worktree"],
)
def test_protect_allows_the_same_payload_path_from_every_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    cwd_kind: str,
) -> None:
    """Issue #314's repro, proof 1: the same payload path, claimed in its own
    linked worktree, must allow from all four measured cwds -- the main
    checkout on its default branch, the main checkout on another branch, a
    directory outside every repository, and an unrelated worktree. The old
    cwd-implicit `_git_output` calls allowed only from the fourth."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _use_real_path_is_tracked(monkeypatch)
    main, worktree = _protect_real_repo_with_worktree(tmp_path)
    _main2, other_worktree = _protect_real_repo_with_worktree(tmp_path, slug="issue-90-other")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    if cwd_kind == "main_other_branch":
        _real_git(main, "checkout", "-q", "-b", "some-other-branch")
    cwd_by_kind = {
        "main_default_branch": main,
        "main_other_branch": main,
        "outside_any_repository": outside,
        "another_worktree": other_worktree,
    }
    monkeypatch.chdir(cwd_by_kind[cwd_kind])
    state = _protect_state_with_claim(
        _protect_active_claim("Grok sess-1", branch="codex/issue-72-widget")
    )
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: state)
    target = worktree / "src" / "widget.py"

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(target)}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")


def test_protect_denies_a_path_outside_every_claim_scope_still(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Path-based resolution allows only the claimed worktree's own scope --
    a write inside a still-unclaimed area of the same worktree still denies,
    proving the fix does not simply allow everything real."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _use_real_path_is_tracked(monkeypatch)
    _main, worktree = _protect_real_repo_with_worktree(tmp_path)
    monkeypatch.chdir(worktree)
    state = _protect_state_with_claim(
        _protect_active_claim("Grok sess-1", branch="codex/issue-72-widget")
    )
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: state)
    target = worktree / "docs" / "widget.md"

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(target)}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")


@pytest.mark.parametrize(
    ("claimed_branch", "written", "decision", "reason"),
    [
        pytest.param(
            "codex/issue-72-widget", "scripts/registry.txt", "allow", None, id="claimed-shared"
        ),
        pytest.param(
            "codex/issue-99-other", "scripts/registry.txt", "deny", "claim first", id="unclaimed"
        ),
        pytest.param(
            "codex/issue-72-widget", "docs/widget.md", "deny", "claim first", id="not-shared"
        ),
    ],
)
def test_protect_lets_any_live_claim_write_a_lane_shared_registry_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    claimed_branch: str,
    written: str,
    decision: str,
    reason: str | None,
) -> None:
    """Issue #575 line 2 (PROT-46): a `lane_shared` file is writable by any
    live claim this session holds in the checkout, though its scope (`src`)
    never names it; without a claim on the branch it still denies, and a
    file the configuration does not share stays bound to the scope."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _use_real_path_is_tracked(monkeypatch)
    _main, worktree = _protect_real_repo_with_worktree(tmp_path)
    (worktree / board.CONFIG_PATH).write_text('lane_shared = ["scripts/registry.txt"]\n')
    state = _protect_state_with_claim(_protect_active_claim("Grok sess-1", branch=claimed_branch))
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: state)
    payload = {"toolName": "write", "toolInput": {"path": str(worktree / written)}}

    exit_code = _protect_main(monkeypatch, payload)

    assert exit_code == (0 if decision == "allow" else 2)
    _assert_protect_decision(capsys, decision=decision, reason=reason)


@pytest.mark.parametrize("payload_for", _TARGET_PATH_PAYLOAD_BUILDERS, ids=["write", "bash-rm"])
def test_protect_allows_a_path_outside_every_repository_without_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload_for: Callable[[Path], dict[str, object]],
) -> None:
    """PROT-32 (issue #448): a write outside every git checkout -- the
    session's memory, its scratchpad, `/tmp` -- is not aco's to judge, so an
    `Edit` path and a Bash-recognized one alike allow before identity or the
    store is ever read (neither is set up here, and both would fail)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch)
    outside = tmp_path / "not-a-repository"
    outside.mkdir()
    monkeypatch.chdir(outside)

    assert _protect_main(monkeypatch, payload_for(outside / "widget.py")) == 0
    _assert_protect_decision(capsys, decision="allow")


def _claude_code_write(target: Path) -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "claude-session",
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": "x"},
    }


def _codex_apply_patch(target: Path) -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "turn_id": "codex-turn",
        "tool_name": "apply_patch",
        "tool_input": {"command": f"*** Begin Patch\n*** Add File: {target}\n+x\n*** End Patch"},
    }


def _grok_write(target: Path) -> dict[str, object]:
    return {
        "hookEventName": "pre_tool_use",
        "hook_event_name": "PreToolUse",
        "sessionId": "grok-session",
        "toolName": "write",
        "toolInput": {"path": str(target)},
    }


@pytest.mark.parametrize(
    "host_payload_for",
    [_claude_code_write, _codex_apply_patch, _grok_write],
    ids=["claude-code", "codex", "grok"],
)
def test_protect_allow_is_silent_exit_zero_for_every_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    host_payload_for: Callable[[Path], dict[str, object]],
) -> None:
    """PROT-01 (issue #454): each host's own documented allow is exit 0 with
    nothing on stdout -- Claude Code rejects a `decision` outside
    approve/block as a hook error notice, and an `approve` or
    `permissionDecision: allow` would skip its permission prompt."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch)
    outside = tmp_path / "not-a-repository"
    outside.mkdir()
    monkeypatch.chdir(outside)

    assert _protect_main(monkeypatch, host_payload_for(outside / "widget.py")) == 0
    assert capsys.readouterr() == ("", "")


def test_protect_apply_patch_judges_two_worktrees_separately_and_one_deny_wins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #314's own third proof: an `apply_patch` call touching two real
    linked worktrees of the same repository judges each path in its own
    checkout -- the first worktree holds a covering claim, the second holds
    none at all, and the second's denial wins."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _use_real_path_is_tracked(monkeypatch)
    _main, claimed_worktree = _protect_real_repo_with_worktree(tmp_path)
    _main2, unclaimed_worktree = _protect_real_repo_with_worktree(tmp_path, slug="issue-90-other")
    monkeypatch.chdir(tmp_path)
    state = _protect_state_with_claim(
        _protect_active_claim("Grok sess-1", branch="codex/issue-72-widget")
    )
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: state)
    command = _patch_command(
        f"*** Update File: {claimed_worktree / 'src' / 'widget.py'}",
        "@@",
        "-old",
        "+new",
        f"*** Update File: {unclaimed_worktree / 'src' / 'other.py'}",
        "@@",
        "-old",
        "+new",
    )

    assert (
        _protect_main(monkeypatch, {"toolName": "apply_patch", "toolInput": {"command": command}})
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")


def _serve_item_72_body(monkeypatch: pytest.MonkeyPatch) -> FakeForge:
    """Issue #72's own body, with the `agent-claim` block a rescope keeps in
    step with its claim (issue #554), on a GitHub fake the checkout's
    canonical remote names -- so no rescope here ever reaches a real forge."""
    client = FakeForge()
    client.issue_references[72] = forge.ItemReference(
        forge.ItemState.OPEN, "Widget", complete_contract("Build it.")
    )
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(
        checkout, "remote_url", lambda remote, **_kwargs: f"https://github.com/{REPOSITORY}.git"
    )
    return client


@pytest.mark.parametrize(
    "cwd_kind", ["claimed_worktree", "foreign_tmp_dir", "foreign_main_checkout"]
)
def test_rescope_succeeds_from_every_cwd_when_the_add_path_is_absolute(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    cwd_kind: str,
) -> None:
    """Issue #314's own fourth proof: the same absolute `--add` path
    locates the claimed worktree's own checkout from the worktree itself,
    an unrelated tmp directory outside every repository, and the shared
    main checkout alike -- the one location signal a dispatcher in the
    head's own shared environment (editing a linked worktree through a
    subagent) can give without knowing its cwd. The item body's scope moves
    with the claim (issue #554)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})
    _use_real_path_is_tracked(monkeypatch)
    main, worktree = _protect_real_repo_with_worktree(tmp_path)
    add = str(worktree / "docs" / "widget.md")
    if cwd_kind == "claimed_worktree":
        cwd = worktree
    elif cwd_kind == "foreign_main_checkout":
        cwd = main
    else:
        cwd = tmp_path / "elsewhere"
        cwd.mkdir()
    monkeypatch.chdir(cwd)
    client = _serve_item_72_body(monkeypatch)
    claimed = _protect_active_claim(
        "Codex Sol", scope=("src/widget.py",), branch="codex/issue-72-widget"
    )
    state = _protect_state_with_claim(claimed)
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: state)
    monkeypatch.setattr(
        store,
        "commit_transition",
        lambda *, observed, subject, intent: protocol.apply(observed.state, intent),
    )

    status = issue_claim.main(["rescope", "72", "--add", add])

    assert status == 0
    assert capsys.readouterr().out == f"RESCOPED issue #72: {claimed.claim_id}\n"
    assert body.parse_body(client.item_bodies[72]).scope == ("docs/widget.md", "src/widget.py")


def test_rescope_admits_a_file_in_a_new_directory_that_protect_then_allows_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #474: `rescope --add` of a file whose directories do not exist
    yet resolves the worktree from their nearest existing ancestor, the way
    `protect` judges the same path (RESC-18, PROT-39), so the claim can grow
    before the write the hook would otherwise deny with `claim first`."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})
    _use_real_path_is_tracked(monkeypatch)
    _main, worktree = _protect_real_repo_with_worktree(tmp_path)
    new_file = worktree / "neu" / "tief" / "x.py"
    _serve_item_72_body(monkeypatch)
    claimed = _protect_active_claim(
        "Codex Sol", scope=("src/widget.py",), branch="codex/issue-72-widget"
    )
    states = [_protect_state_with_claim(claimed)]
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: states[-1])

    def commit(*, observed, subject, intent):
        states.append(protocol.apply(observed.state, intent))
        return states[-1]

    monkeypatch.setattr(store, "commit_transition", commit)

    status = issue_claim.main(["rescope", "72", "--add", str(new_file)])

    assert status == 0
    assert capsys.readouterr().out == f"RESCOPED issue #72: {claimed.claim_id}\n"
    assert _protect_main(monkeypatch, _write_target_payload(new_file)) == 0
    _assert_protect_decision(capsys, decision="allow", reason=None)


def _rescope_args_mixed_absolute_and_relative(tmp_path: Path) -> list[str]:
    _main, worktree = _protect_real_repo_with_worktree(tmp_path)
    return [
        "rescope",
        "72",
        "--add",
        str(worktree / "docs" / "widget.md"),
        "--drop",
        "src/widget.py",
    ]


def test_rescope_rejects_a_wide_scope_from_a_foreign_cwd_via_the_resolved_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #314 repeat gate B4: the width guard's own directory classifier
    (`checkout._scope_directories`) must read the resolved checkout's own
    directory, never the calling process's cwd. From a foreign cwd -- which
    has no `docs` directory at all, and is not even a git checkout -- an
    absolute `--add` naming the claimed worktree's own `docs` directory
    still trips the same `--whole` refusal it would from inside that
    worktree, because `docs` is classified against the worktree via `-C`,
    not against the foreign cwd."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})
    _use_real_path_is_tracked(monkeypatch)
    _main, worktree = _protect_real_repo_with_worktree(tmp_path)
    foreign_cwd = tmp_path / "elsewhere"
    foreign_cwd.mkdir()
    monkeypatch.chdir(foreign_cwd)
    _serve_item_72_body(monkeypatch)
    claimed = _protect_active_claim(
        "Codex Sol", scope=("src/widget.py",), branch="codex/issue-72-widget"
    )
    state = _protect_state_with_claim(claimed)
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: state)

    status = issue_claim.main(["rescope", "72", "--add", str(worktree / "docs")])

    assert status == 2
    err = capsys.readouterr().err
    assert "scope is wide" in err
    assert "--whole" in err


def test_protect_denies_a_relative_payload_path_even_from_the_claimed_worktree_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Finding R2: a relative payload path must never be guessed by joining
    it to the hook process's own cwd -- not even in the one case where doing
    so would happen to land on the right file (cwd already the claimed
    worktree, the historical vulnerable shape this whole item closes).
    Every provider `aco` supports sends an already-absolute `file_path`, so
    a relative one is untrustworthy on its own and denies outright,
    regardless of a live covering claim."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _main, worktree = _protect_real_repo_with_worktree(tmp_path)
    monkeypatch.chdir(worktree)
    state = _protect_state_with_claim(
        _protect_active_claim("Grok sess-1", branch="codex/issue-72-widget")
    )
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: state)

    assert (
        _protect_main(monkeypatch, {"toolName": "write", "toolInput": {"path": "src/widget.py"}})
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason=checkout.RELATIVE_PAYLOAD_PATH_DENIAL)


def _real_main_checkout_target(tmp_path: Path) -> Path:
    """Finding R3's own proof target: a real main checkout's own file, no
    fake `_git_output` involved."""
    main, _worktree = _protect_real_repo_with_worktree(tmp_path)
    return main / "README.md"


def _real_worktree_on_default_branch_target(tmp_path: Path) -> Path:
    """Gate G4's own proof target: a real linked worktree checked out on the
    repository's own default branch -- possible once the main checkout
    moves off it first (git refuses the same branch checked out twice), then
    a worktree attaches the now-free branch directly rather than creating a
    new one, the shape a repository's default-branch change can leave
    behind."""
    main = tmp_path / "repo"
    main.mkdir()
    _real_git(main, "init", "-q", "-b", "main")
    _real_git(main, "config", "user.name", "Test")
    _real_git(main, "config", "user.email", "test@example.com")
    (main / "README.md").write_text("hello\n")
    _real_git(main, "add", "README.md")
    _real_git(main, "commit", "-q", "-m", "initial")
    _real_git(main, "remote", "add", "origin", "https://example.invalid/example/repo.git")
    _real_git(main, "update-ref", "refs/remotes/origin/main", "HEAD")
    _real_git(main, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    _real_git(main, "checkout", "-q", "-b", "codex/elsewhere")
    worktree = tmp_path / "repo-worktrees" / "issue-1-on-default"
    worktree.parent.mkdir(parents=True)
    _real_git(main, "worktree", "add", "-q", str(worktree), "main")
    return worktree / "README.md"


def _symlink_outside_every_repository_into_main_checkout(tmp_path: Path) -> Path:
    """Issue #448 review finding: a file symlink sitting outside every
    repository (the shape of `~/.claude/CLAUDE.md`) whose target is a
    tracked file in a real main checkout -- the write lands in that
    checkout, so it must never pass as outside every repository."""
    link = tmp_path / "not-a-repository" / "linked-readme.md"
    link.parent.mkdir()
    link.symlink_to(_real_main_checkout_target(tmp_path))
    return link


@pytest.mark.parametrize(
    "payload_for",
    [*_TARGET_PATH_PAYLOAD_BUILDERS, _monitor_rm_target_payload],
    ids=["write", "bash-rm", "monitor-rm"],
)
@pytest.mark.parametrize(
    "build_target",
    [_real_main_checkout_target, _real_worktree_on_default_branch_target],
    ids=["main-checkout", "linked-worktree-on-default-branch"],
)
def test_protect_denies_not_main_for_a_real_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    build_target: Callable[[Path], Path],
    payload_for: Callable[[Path], dict[str, object]],
) -> None:
    """Finding R3 and gate G4, against real checkouts rather than
    `_patch_protect_git`'s mock: a payload path in the real shared main
    checkout denies `not main` (R3 -- the previously required proof was
    mocked, never exercising `git worktree add`/`git -C`); so does one in a
    real linked worktree sitting on the repository's own default branch (G4
    -- `resolve_path_checkout`'s structural MAIN/LINKED_WORKTREE split alone
    stopped catching this once issue #314 dropped the old branch-name check,
    so a live claim matching that worktree's agent/branch/scope would
    otherwise be honoured there exactly as if it were a real lane). A
    Bash-recognized path runs the identical gate (issue #380), and so does
    one in a `Monitor` script, which the shell runs just the same (issue
    #448)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    target = build_target(tmp_path)
    monkeypatch.chdir(tmp_path)
    _forbid_github_construction(monkeypatch)

    assert _protect_main(monkeypatch, payload_for(target)) == 2
    _assert_protect_decision(capsys, decision="deny", reason="not main")


def _hook_in_a_bare_repository(tmp_path: Path) -> Path:
    """The shape of a forge-free canonical remote (README, "A workflow
    without a forge"): a bare repository has no `.git` entry at all, yet a
    write into its hooks is a write into a repository."""
    served = tmp_path / "served.git"
    served.mkdir()
    _real_git(served, "init", "-q", "--bare")
    return served / "hooks" / "pre-receive"


def _bash_payload(command: str) -> dict[str, object]:
    return {"toolName": "Bash", "toolInput": {"command": command}}


@pytest.mark.parametrize(
    ("payload_for", "decision", "exit_code"),
    [
        (_write_target_payload, "deny", 2),
        (lambda link: _bash_payload(f"echo hi >> {link}"), "deny", 2),
        (_bash_rm_target_payload, "allow", 0),
        (_monitor_rm_target_payload, "allow", 0),
        (lambda link: _bash_payload(f"mv {link} {link}.old"), "allow", 0),
        (lambda link: _bash_payload(f"mv {link.parent / 'src'} {link}"), "allow", 0),
    ],
    ids=["write", "bash-append", "bash-rm", "monitor-rm", "bash-mv", "bash-mv-onto-link"],
)
def test_protect_judges_a_symlink_outside_every_repository_by_what_the_operation_touches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload_for: Callable[[Path], dict[str, object]],
    decision: str,
    exit_code: int,
) -> None:
    """Issue #448 review findings: a write through a file symlink outside
    every repository lands in its target's main checkout and denies `not
    main`; removing, renaming, or renaming a file onto the link itself never
    touches that target, so it stays outside every repository and allows
    (PROT-32)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    link = _symlink_outside_every_repository_into_main_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    _forbid_github_construction(monkeypatch)

    assert _protect_main(monkeypatch, payload_for(link)) == exit_code
    _assert_protect_decision(
        capsys, decision=decision, reason=None if decision == "allow" else "not main"
    )


def _file_outside_every_repository(tmp_path: Path) -> Path:
    outside = tmp_path / "not-a-repository"
    outside.mkdir()
    return outside / "widget.py"


@pytest.mark.parametrize("payload_for", _TARGET_PATH_PAYLOAD_BUILDERS, ids=["write", "bash-rm"])
@pytest.mark.parametrize(
    ("build_target", "exit_code"),
    [
        (_real_main_checkout_target, 2),
        (_hook_in_a_bare_repository, 2),
        (_file_outside_every_repository, 0),
    ],
    ids=[
        "inside-main-checkout-denies",
        "inside-bare-repository-denies",
        "outside-every-repository-allows",
    ],
)
def test_protect_never_reads_a_git_failure_as_outside_every_repository(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload_for: Callable[[Path], dict[str, object]],
    build_target: Callable[[Path], Path],
    exit_code: int,
) -> None:
    """Issue #448 review finding: with git unavailable, a path below a
    `.git` entry or inside a bare repository still denies (PROT-17, the
    failure's own text) instead of passing as outside every repository
    (PROT-32); a path with no repository above it is outside and allows all
    the same."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch)
    target = build_target(tmp_path)
    monkeypatch.chdir(tmp_path)

    def git_missing(*_args: object, **_kwargs: object) -> process.CapturedResult:
        raise process.ExecutableMissingError("git")

    monkeypatch.setattr(process, "run_git", git_missing)

    assert _protect_main(monkeypatch, payload_for(target)) == exit_code


@pytest.mark.parametrize("payload_for", _TARGET_PATH_PAYLOAD_BUILDERS, ids=["write", "bash-rm"])
def test_protect_denies_a_checkout_with_no_commit_yet(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload_for: Callable[[Path], dict[str, object]],
) -> None:
    """Gate G3: a freshly `git init`ed checkout with no commit yet -- an
    unborn branch -- still names a real branch (`git branch --show-current`
    reads the symbolic ref's target regardless of whether it resolves to a
    commit), so without a successful-HEAD requirement its name could
    coincidentally match a still-live claim's and be authorized despite
    naming no real history. A Bash-recognized path runs the identical gate
    (issue #380)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _real_git(unborn, "init", "-q", "-b", "codex/issue-72-widget")
    monkeypatch.chdir(tmp_path)
    _forbid_github_construction(monkeypatch)
    target = unborn / "widget.py"

    assert _protect_main(monkeypatch, payload_for(target)) == 2
    _assert_protect_decision(capsys, decision="deny", reason=checkout.NO_COMMIT_CHECKOUT_REASON)


def test_protect_apply_patch_fetches_store_state_once_per_repository(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Gate G5: two payload paths in one `apply_patch` call, each in its own
    linked worktree of the same repository, are judged against one state
    snapshot -- fetched once for the repository (observed via a counting
    fake store, never by asserting mock call order), not once per path -- so
    a rescope that narrows coverage between two per-path fetches can never
    let each path pass against a different snapshot though no single live
    claim ever covered the whole patch."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _use_real_path_is_tracked(monkeypatch)
    _main, claimed_worktree = _protect_real_repo_with_worktree(tmp_path)
    _main2, unclaimed_worktree = _protect_real_repo_with_worktree(tmp_path, slug="issue-90-other")
    monkeypatch.chdir(tmp_path)
    state = _protect_state_with_claim(
        _protect_active_claim("Grok sess-1", branch="codex/issue-72-widget")
    )
    fetch_calls: list[Path] = []

    def counting_fetch_state(*, worktree: Path, remote: str) -> protocol.ClaimState:
        fetch_calls.append(worktree)
        return state

    monkeypatch.setattr(store, "fetch_state", counting_fetch_state)
    command = _patch_command(
        f"*** Update File: {claimed_worktree / 'src' / 'widget.py'}",
        "@@",
        "-old",
        "+new",
        f"*** Update File: {unclaimed_worktree / 'src' / 'other.py'}",
        "@@",
        "-old",
        "+new",
    )

    assert (
        _protect_main(monkeypatch, {"toolName": "apply_patch", "toolInput": {"command": command}})
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")
    assert len(fetch_calls) == 1


def _rescope_args_add_path_outside_the_first_paths_checkout(tmp_path: Path) -> list[str]:
    _main, worktree = _protect_real_repo_with_worktree(tmp_path)
    _main2, other_worktree = _protect_real_repo_with_worktree(tmp_path, slug="issue-90-other")
    return [
        "rescope",
        "72",
        "--add",
        str(worktree / "docs" / "widget.md"),
        "--add",
        str(other_worktree / "src" / "other.py"),
    ]


def _rescope_args_add_path_outside_any_repository(tmp_path: Path) -> list[str]:
    outside = tmp_path / "outside"
    outside.mkdir()
    return ["rescope", "72", "--add", str(outside / "file.py")]


def _rescope_args_add_path_in_a_new_directory_outside_any_repository(
    tmp_path: Path,
) -> list[str]:
    return ["rescope", "72", "--add", str(tmp_path / "outside" / "neu" / "x.py")]


def _rescope_args_add_dotdot_path_through_a_missing_directory_out_of_the_worktree(
    tmp_path: Path,
) -> list[str]:
    _main, worktree = _protect_real_repo_with_worktree(tmp_path)
    escape_to_outside = os.path.relpath(tmp_path / "outside", worktree)
    return ["rescope", "72", "--add", f"{worktree}/missing/../{escape_to_outside}/new/q.py"]


def _rescope_args_add_path_in_an_unborn_checkout(tmp_path: Path) -> list[str]:
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _real_git(unborn, "init", "-q", "-b", "codex/issue-72-widget")
    return ["rescope", "72", "--add", str(unborn / "widget.py")]


@pytest.mark.parametrize(
    ("build_args", "expected_error_fragment"),
    [
        (
            _rescope_args_add_path_outside_the_first_paths_checkout,
            "is outside the resolved checkout",
        ),
        (_rescope_args_add_path_outside_any_repository, "not in a repository"),
        (
            _rescope_args_add_path_in_a_new_directory_outside_any_repository,
            "not in a repository",
        ),
        (
            _rescope_args_add_dotdot_path_through_a_missing_directory_out_of_the_worktree,
            "not in a repository",
        ),
        (_rescope_args_add_path_in_an_unborn_checkout, checkout.NO_COMMIT_CHECKOUT_REASON),
        (
            _rescope_args_mixed_absolute_and_relative,
            "--drop path 'src/widget.py' is relative and {tmp_path} is not in a repository; "
            "pass it as an absolute path",
        ),
    ],
    ids=[
        "second-add-path-outside-checkout",
        "outside-any-repository",
        "new-directory-outside-any-repository",
        "dotdot-through-missing-directory-outside-any-repository",
        "checkout-has-no-commit",
        "mixed-absolute-and-relative",
    ],
)
def test_rescope_denies_before_touching_the_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    build_args: Callable[[Path], list[str]],
    expected_error_fragment: str,
) -> None:
    """`rescope`'s own new checkout-resolution paths (issue #314 delta and
    repeat gate, finding R1): a second `--add`/`--drop` path outside the
    first path's own checkout (`_rescope_scope_entries`) refuses rather than
    silently mis-scoping; a location outside every repository, and gate
    G3's no-commit checkout (`_rescope_checkout`); and a relative
    `--drop` entry beside an absolute `--add` that names a real checkout,
    run from a cwd outside every repository, which has no checkout to read
    it against (RESC-01) -- all refuse before the store is ever touched."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})
    monkeypatch.setattr(store, "fetch_state", _store_must_not_be_read)
    args = build_args(tmp_path)

    status = issue_claim.main(args)

    assert status == 2
    assert expected_error_fragment.format(tmp_path=tmp_path) in capsys.readouterr().err


def test_rescope_json_reports_a_dotdot_path_through_a_missing_directory_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """RESC-17: a `..` path that leaves the worktree through a missing
    directory is an unresolved checkout, so `--json` reports `unavailable`
    with the sentence stderr printed."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})
    args = _rescope_args_add_dotdot_path_through_a_missing_directory_out_of_the_worktree(tmp_path)

    status = issue_claim.main([*args, "--json"])

    captured = capsys.readouterr()
    refusal = json.loads(captured.out)
    assert status == 2
    assert (refusal["ok"], refusal["reason"]) == (False, "unavailable")
    assert "not in a repository" in refusal["message"]
    assert captured.err == f"ERROR: {refusal['message']}\n"


# `protect.judge`'s own direct proofs (issue #394): a real bare-remote
# repository with a real linked worktree, driven through `judge` itself --
# never `main(["protect"])` -- so none of these needs `sys.stdin` or the
# process cwd stubbed at all; `judge` takes its payload and its one
# dependency (`board_config_for`) as plain arguments.


def _judge_worktree(tmp_path: Path, *, branch: str) -> Path:
    """A real bare-remote repository (`Setup: bare-remote`,
    `specs/protect.spec.md`) with one linked, isolated worktree on `branch`
    -- built entirely from its own explicit repository path, never a
    process-cwd stub, since `judge`'s own tests must prove the same thing
    `judge` itself proves: the verdict depends only on the payload's
    absolute path, never on the process's cwd."""
    repo, _remote = _real_repository_with_bare_remote(tmp_path)
    (repo / "README.md").write_text("hello\n")
    _real_git(repo, "add", "README.md")
    _real_git(repo, "commit", "-q", "-m", "initial")
    _push_repository_trunk(repo, "origin")
    worktree = tmp_path / "repo-worktrees" / branch.replace("/", "-")
    checkout.create_linked_worktree(
        worktree, branch=branch, trunk="refs/remotes/origin/main", directory=repo
    )
    return worktree


def _judge_decision_and_reason(verdict: protect.Verdict) -> tuple[protect.Decision, str | None]:
    return verdict.decision, verdict.reason


def test_judge_denies_an_apply_patch_path_outside_the_live_claims_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Checkout/Default-Branch/Claim-Scope chain, driven directly: a
    live claim on this branch whose scope misses the patched file denies
    `<path> outside claim scope` (PROT-20), `apply_patch`'s own distinct
    text for a path a live claim exists for but does not cover."""
    branch = "codex/issue-9-widget"
    worktree = _judge_worktree(tmp_path, branch=branch)
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Ada"})
    claim = _protect_active_claim("Ada", scope=("docs",), branch=branch)
    monkeypatch.setattr(
        store, "fetch_state", lambda *, worktree, remote: _protect_state_with_claim(claim)
    )
    target = worktree / "README.md"
    payload = {
        "toolName": "apply_patch",
        "toolInput": {
            "command": _patch_command(f"*** Update File: {target}", "@@", "-hello", "+hi")
        },
    }

    verdict = protect.judge(payload, board_config_for=lambda _toplevel: board.BoardConfig())

    assert _judge_decision_and_reason(verdict) == (
        protect.Decision.DENY,
        "README.md outside claim scope",
    )


def test_judge_denies_a_bash_recognized_pattern_path_outside_the_live_claims_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Bash-recognized write pattern runs the identical chain (issue
    #380): a live claim whose scope misses the removed path denies naming
    both the recognized pattern and the path (PROT-33)."""
    branch = "codex/issue-9-widget"
    worktree = _judge_worktree(tmp_path, branch=branch)
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Ada"})
    claim = _protect_active_claim("Ada", scope=("docs",), branch=branch)
    monkeypatch.setattr(
        store, "fetch_state", lambda *, worktree, remote: _protect_state_with_claim(claim)
    )
    payload = {
        "toolName": "Bash",
        "toolInput": {"command": f"rm {worktree / 'README.md'}"},
        "cwd": str(worktree),
    }

    verdict = protect.judge(payload, board_config_for=lambda _toplevel: board.BoardConfig())

    assert _judge_decision_and_reason(verdict) == (
        protect.Decision.DENY,
        "rm README.md outside claim scope",
    )


def test_judge_denies_a_path_resolving_to_the_checkout_root_before_reading_the_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PROT-14 fires from the checkout gate alone, before the store is ever
    read (`_store_must_not_be_read` fails the test if it is): a payload path
    that resolves to exactly the checkout root denies naming that root. The
    canonical remote is asked only for its recorded default branch (issue
    #490)."""
    branch = "codex/issue-9-widget"
    worktree = _judge_worktree(tmp_path, branch=branch)
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Ada"})
    monkeypatch.setattr(store, "fetch_state", _store_must_not_be_read)
    payload = {"toolName": "Edit", "toolInput": {"path": str(worktree)}}

    verdict = protect.judge(payload, board_config_for=lambda _toplevel: board.BoardConfig())

    assert _judge_decision_and_reason(verdict) == (
        protect.Decision.DENY,
        _checkout_root_reason(worktree),
    )


def _real_main_checkout_with_session_settings(tmp_path: Path) -> Path:
    """A real repository's own main checkout (`Setup: bare-remote`) with a
    tracked `.claude/settings.json` and git excluding
    `.claude/settings.local.json` -- the global excludes file switched off,
    so only this repository's own rules decide what is ignored."""
    repo, _remote = _real_repository_with_bare_remote(tmp_path)
    _real_git(repo, "config", "core.excludesFile", "/dev/null")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text("{}\n")
    (repo / "README.md").write_text("hello\n")
    _real_git(repo, "add", "README.md", ".claude/settings.json")
    _real_git(repo, "commit", "-q", "-m", "initial")
    _push_repository_trunk(repo, "origin")
    # The sealed helper initializes from an empty template, which carries no `.git/info/`.
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("/.claude/settings.local.json\n")
    return repo


@pytest.mark.parametrize("payload_for", _TARGET_PATH_PAYLOAD_BUILDERS, ids=["write", "bash-rm"])
@pytest.mark.parametrize(
    ("relative", "status", "reason"),
    [
        (".claude/settings.local.json", 0, None),
        (".claude/settings.json", 2, checkout.PROTECT_NOT_MAIN_REASON),
        (".claude/notes.md", 2, checkout.PROTECT_NOT_MAIN_REASON),
        ("README.md", 2, checkout.PROTECT_NOT_MAIN_REASON),
    ],
    ids=["ignored-session-setting", "tracked-setting", "untracked-unignored", "tracked-file"],
)
def test_protect_lets_the_main_checkout_write_only_its_ignored_session_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload_for: Callable[[Path], dict[str, object]],
    relative: str,
    status: int,
    reason: str | None,
) -> None:
    """PROT-38 (issue #448): the escape a misconfigured hook needs -- the
    session may rewrite its own ignored `.claude/` settings in the main
    checkout, with no identity and no claim, while a tracked `.claude/` file,
    an untracked one git does not ignore, and any other file there still
    deny `not main` (PROT-12)."""
    main_checkout = _real_main_checkout_with_session_settings(tmp_path)
    _set_agent_identity_env(monkeypatch)

    assert _protect_main(monkeypatch, payload_for(main_checkout / relative)) == status
    _assert_protect_decision(capsys, decision="deny" if reason else "allow", reason=reason)


@pytest.mark.parametrize(
    ("payload_for", "relative", "status", "reason"),
    [
        (_write_target_payload, "src/new/package/module.py", 0, None),
        (_write_target_payload, "docs/new/page.md", 2, "claim first"),
        (_bash_rm_target_payload, "src/new/package/module.py", 0, None),
        (_bash_rm_target_payload, "docs/new/page.md", 2, "rm docs/new/page.md outside claim scope"),
    ],
    ids=["write-inside-scope", "write-outside-scope", "bash-inside-scope", "bash-outside-scope"],
)
def test_protect_judges_a_file_in_a_not_yet_existing_directory_by_its_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload_for: Callable[[Path], dict[str, object]],
    relative: str,
    status: int,
    reason: str | None,
) -> None:
    """Issue #448 drive finding: a new file whose directories do not exist
    yet is judged by the checkout its nearest existing ancestor belongs to,
    never allowed as a path outside every repository (PROT-32)."""
    branch = "codex/issue-72-claims"
    worktree = _judge_worktree(tmp_path, branch=branch)
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Ada"})
    claim = _protect_active_claim("Ada", scope=("src",), branch=branch)
    monkeypatch.setattr(
        store, "fetch_state", lambda *, worktree, remote: _protect_state_with_claim(claim)
    )

    assert _protect_main(monkeypatch, payload_for(worktree / relative)) == status
    _assert_protect_decision(capsys, decision="deny" if reason else "allow", reason=reason)


def _bash_rm_rf_target_payload(target: Path) -> dict[str, object]:
    return {"toolName": "Bash", "toolInput": {"command": f"rm -rf {target}"}}


def _unguarded_scratchpad(tmp_path: Path) -> Path:
    """A tester's scratchpad (issue #483): a throwaway main checkout with
    its own bare remote (`repo`, `remote.git`) and a linked worktree of it
    on a feature branch (`lane`), a linked worktree of a guarded
    repository placed inside it (`guarded-worktree`), a directory symlink
    into that guarded repository's main checkout (`guarded-link`), file
    symlinks from the throwaway checkout and its worktree into that main
    checkout (`repo/into-guarded.md`, `lane/into-guarded.md`), one from
    the throwaway checkout into the guarded worktree
    (`repo/into-guarded-worktree.md`), one from the throwaway checkout into
    its own git directory (`repo/into-own-git`), into its own README
    (`repo/into-own-readme.md`), and to a file outside every repository
    (`repo/into-outside.md`), one from the guarded worktree into the
    throwaway checkout (`guarded-worktree/into-throwaway.md`), a directory
    symlink from the throwaway checkout into a directory of the guarded main checkout
    (`repo/into-guarded-directory`), and a dangling directory
    symlink outside every repository into a directory of that main checkout
    not created yet (`dangling`)."""
    guarded, _worktree = _protect_real_repo_with_worktree(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    throwaway = _real_main_checkout_with_session_settings(scratch)
    _real_git(
        guarded,
        "worktree",
        "add",
        "-q",
        str(scratch / "guarded-worktree"),
        "-b",
        "codex/issue-9-guarded",
    )
    _real_git(throwaway, "worktree", "add", "-q", str(scratch / "lane"), "-b", "codex/issue-9-lane")
    (scratch / "guarded-link").symlink_to(guarded, target_is_directory=True)
    for link in (throwaway / "into-guarded.md", scratch / "lane" / "into-guarded.md"):
        link.symlink_to(guarded / "README.md")
    (throwaway / "into-guarded-worktree.md").symlink_to(scratch / "guarded-worktree" / "README.md")
    (throwaway / "into-own-git").symlink_to(throwaway / ".git" / "description")
    (throwaway / "into-own-readme.md").symlink_to(throwaway / "README.md")
    outside = _file_outside_every_repository(tmp_path)
    outside.write_text("outside\n")
    (throwaway / "into-outside.md").symlink_to(outside)
    (scratch / "guarded-worktree" / "into-throwaway.md").symlink_to(throwaway / "README.md")
    (throwaway / "into-guarded-directory").symlink_to(
        guarded / ".agent-claim", target_is_directory=True
    )
    (scratch / "dangling").symlink_to(guarded / "newdir", target_is_directory=True)
    return scratch


def _bash_copy_into_payload(destination: Path) -> dict[str, object]:
    return _bash_payload(f"cp {destination.parent / 'note.txt'} {destination}")


def _bash_move_into_payload(destination: Path) -> dict[str, object]:
    return _bash_payload(f"mv {destination.parent / 'note.txt'} {destination}")


def _bash_rm_rf_contents_payload(directory: Path) -> dict[str, object]:
    return _bash_payload(f"rm -rf {directory}/")


_MALFORMED_ENTRY_REASON = "ACO_PROTECT_UNGUARDED: {entry} is not an absolute directory"


@pytest.mark.parametrize(
    ("unguarded", "payload_for", "target", "status", "reason", "store_reads"),
    [
        (None, _write_target_payload, "repo/README.md", 2, "not main", 0),
        (None, _write_target_payload, "dangling/file.md", 2, "not main", 0),
        (None, _write_target_payload, "repo/into-own-git", 2, "not main", 0),
        ("{scratch}", _write_target_payload, "repo/README.md", 0, None, 0),
        ("{scratch}", _bash_rm_target_payload, "repo/README.md", 0, None, 0),
        ("{scratch}", _bash_rm_rf_target_payload, "repo", 0, None, 0),
        ("{scratch}", _write_target_payload, "guarded-worktree/src/x.py", 2, "claim first", 1),
        ("{scratch}", _write_target_payload, "guarded-link/README.md", 2, "not main", 0),
        ("{scratch}", _write_target_payload, "repo/into-guarded.md", 2, "not main", 0),
        ("{scratch}", _write_target_payload, "lane/into-guarded.md", 2, "not main", 0),
        ("{scratch}", _write_target_payload, "repo/into-guarded-worktree.md", 2, "claim first", 1),
        ("{scratch}", _bash_copy_into_payload, "repo/into-guarded-directory", 2, "not main", 0),
        ("{scratch}", _bash_move_into_payload, "repo/into-guarded-directory", 2, "not main", 0),
        ("{scratch}", _bash_rm_target_payload, "repo/into-guarded-directory", 2, "not main", 0),
        (
            "{scratch}",
            _bash_rm_rf_contents_payload,
            "repo/into-guarded-directory",
            2,
            "not main",
            0,
        ),
        ("{scratch}", _write_target_payload, "repo/into-own-readme.md", 0, None, 0),
        ("{scratch}", _write_target_payload, "repo/into-outside.md", 0, None, 0),
        (
            "{scratch}",
            _write_target_payload,
            "guarded-worktree/into-throwaway.md",
            2,
            "path required",
            0,
        ),
        (
            "scratch",
            _write_target_payload,
            "repo/README.md",
            2,
            _MALFORMED_ENTRY_REASON.format(entry="scratch"),
            0,
        ),
        (
            f"{{scratch}}/missing{os.pathsep}{{scratch}}",
            _write_target_payload,
            "repo/README.md",
            2,
            _MALFORMED_ENTRY_REASON.format(entry="{scratch}/missing"),
            0,
        ),
        (
            f"{{scratch}}{os.pathsep}",
            _write_target_payload,
            "repo/README.md",
            2,
            _MALFORMED_ENTRY_REASON.format(entry=""),
            0,
        ),
        (
            os.pathsep,
            _write_target_payload,
            "repo/README.md",
            2,
            _MALFORMED_ENTRY_REASON.format(entry=""),
            0,
        ),
        ("scratch", _write_target_payload, "repo/.claude/settings.local.json", 0, None, 0),
        ("scratch", _write_target_payload, "notes.md", 0, None, 0),
    ],
    ids=[
        "unset-guards-the-throwaway-checkout",
        "unset-judges-a-write-below-a-dangling-directory-symlink-by-its-target",
        "unset-judges-a-file-symlink-into-its-own-git-directory-by-its-checkout",
        "write-allows",
        "bash-rm-allows",
        "bash-rm-rf-of-the-root-allows",
        "guarded-worktree-inside-still-needs-a-claim",
        "directory-symlink-into-a-guarded-checkout",
        "file-symlink-into-a-guarded-checkout",
        "file-symlink-from-a-throwaway-worktree-into-a-guarded-checkout",
        "file-symlink-into-a-guarded-worktree",
        "bash-cp-into-a-directory-symlink-into-a-guarded-checkout",
        "bash-mv-into-a-directory-symlink-into-a-guarded-checkout",
        "bash-rm-of-a-directory-symlink-into-a-guarded-checkout",
        "bash-rm-rf-through-a-directory-symlink-into-a-guarded-checkout",
        "file-symlink-into-its-own-checkout-allows",
        "file-symlink-outside-every-repository-allows",
        "file-symlink-from-a-guarded-worktree-into-the-throwaway-checkout",
        "relative-entry-fails-closed",
        "missing-entry-fails-closed",
        "empty-trailing-entry-fails-closed",
        "only-empty-entries-fail-closed",
        "malformed-variable-keeps-session-settings-writable",
        "malformed-variable-leaves-outside-paths-alone",
    ],
)
def test_protect_unguarded_directories_exempt_only_the_repositories_they_hold(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    unguarded: str | None,
    payload_for: Callable[[Path], dict[str, object]],
    target: str,
    status: int,
    reason: str | None,
    store_reads: int,
) -> None:
    """PROT-40/PROT-41 (issue #483): a repository whose common git directory
    sits in an `ACO_PROTECT_UNGUARDED` directory allows every write, root
    deletion included, without reading the store; a guarded repository
    reached from inside it -- its linked worktree, a directory symlink, a
    file symlink, from the throwaway checkout or its worktree -- is still
    judged by that guarded checkout; and an entry that is not an existing
    absolute directory denies every path in a checkout except the session's
    own ignored settings. The session has an identity only where the verdict
    reads the store, so every other row proves it decides without one."""
    scratch = _unguarded_scratchpad(tmp_path)
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Ada"} if store_reads else None)
    if unguarded is not None:
        monkeypatch.setenv(protect.PROTECT_UNGUARDED_ENV, unguarded.format(scratch=scratch))
    fetches: list[Path] = []

    def fetch_state(*, worktree: Path, remote: str) -> protocol.ClaimState:
        fetches.append(worktree)
        return protocol.ClaimState(tip=protocol.ObjectId(BASE), claims={})

    monkeypatch.setattr(store, "fetch_state", fetch_state)

    assert _protect_main(monkeypatch, payload_for(scratch / target)) == status
    _assert_protect_decision(
        capsys,
        decision="deny" if reason else "allow",
        reason=reason.format(scratch=scratch) if reason else None,
    )
    assert len(fetches) == store_reads


_CLAIMED_WORKTREE = "claimed/repo-worktrees/issue-72-widget"


def _symlinks_across_checkouts(tmp_path: Path) -> None:
    """Issue #486's arrangement under `tmp_path`: a linked worktree the
    tests claim with scope `src` (`_CLAIMED_WORKTREE`) of a repository whose
    main checkout is `claimed/repo`, another repository's main checkout
    (`other/repo`), a main checkout nested inside the worktree's scope
    (`src/nested/repo`), a file outside every repository (`outside.md`),
    and file symlinks from the worktree into the other main checkout
    (`src/into-other.md`), into the nested one (`src/into-nested.md`)
    and outside the claimed scope of its linked worktree
    (`src/into-nested-worktree.md`),
    within its own scope (`src/into-own.md`), and outside every repository
    (`src/into-outside.md`), and from `claimed/repo` into the worktree
    (`into-worktree.md`); a link from each of the two into the nested
    checkout's git directory (`into-nested-git`)."""
    for name in ("claimed", "other"):
        (tmp_path / name).mkdir()
    main, claimed_worktree = _protect_real_repo_with_worktree(tmp_path / "claimed")
    other, _other_worktree = _protect_real_repo_with_worktree(tmp_path / "other")
    source = claimed_worktree / "src"
    (source / "nested").mkdir()
    nested, nested_worktree = _protect_real_repo_with_worktree(source / "nested")
    (source / "x.py").write_text("x = 1\n")
    (tmp_path / "outside.md").write_text("outside\n")
    links = {
        source / "into-other.md": other / "README.md",
        source / "into-nested.md": nested / "README.md",
        source / "into-nested-worktree.md": nested_worktree / "README.md",
        source / "into-nested-git": nested / ".git" / "description",
        source / "into-own.md": source / "x.py",
        source / "into-outside.md": tmp_path / "outside.md",
        main / "into-worktree.md": source / "x.py",
        main / "into-nested-git": nested / ".git" / "description",
    }
    for link, target in links.items():
        link.symlink_to(target)


@pytest.mark.parametrize(
    ("payload_for", "link", "agent", "status", "reason", "store_reads"),
    [
        (
            _write_target_payload,
            f"{_CLAIMED_WORKTREE}/src/into-other.md",
            None,
            2,
            "not main",
            0,
        ),
        (
            _write_target_payload,
            f"{_CLAIMED_WORKTREE}/src/into-nested.md",
            None,
            2,
            "not main",
            0,
        ),
        (_write_target_payload, "claimed/repo/into-worktree.md", "Ada", 2, "not main", 0),
        (_write_target_payload, "claimed/repo/into-worktree.md", None, 2, "not main", 0),
        (
            _write_target_payload,
            f"{_CLAIMED_WORKTREE}/src/into-nested-git",
            None,
            2,
            "not a checkout: {tmp_path}/" + _CLAIMED_WORKTREE + "/src/nested/repo/.git"
            " is a git directory",
            0,
        ),
        (
            _write_target_payload,
            "claimed/repo/into-nested-git",
            None,
            2,
            "not a checkout: {tmp_path}/" + _CLAIMED_WORKTREE + "/src/nested/repo/.git"
            " is a git directory",
            0,
        ),
        (_write_target_payload, f"{_CLAIMED_WORKTREE}/src/into-own.md", "Ada", 0, None, 1),
        (
            _write_target_payload,
            f"{_CLAIMED_WORKTREE}/src/into-nested-worktree.md",
            "Ada",
            2,
            "claim first",
            2,
        ),
        (
            _write_target_payload,
            f"{_CLAIMED_WORKTREE}/src/into-outside.md",
            None,
            2,
            "path required",
            0,
        ),
        (
            _bash_rm_target_payload,
            f"{_CLAIMED_WORKTREE}/src/into-other.md",
            None,
            2,
            "path required",
            0,
        ),
    ],
    ids=[
        "claimed-worktree-into-another-repositorys-main-checkout",
        "claimed-worktree-into-a-nested-main-checkout-its-scope-covers",
        "main-checkout-into-the-claimed-worktree",
        "main-checkout-into-the-claimed-worktree-without-an-identity",
        "claimed-worktree-into-a-git-directory-no-checkout-resolves",
        "main-checkout-into-a-git-directory-no-checkout-resolves",
        "within-the-claimed-worktrees-scope",
        "claimed-worktree-into-a-nested-worktree-outside-its-claim-reads-both-stores",
        "claimed-worktree-outside-every-repository",
        "bash-rm-of-the-link-itself",
    ],
)
def test_protect_judges_a_write_through_a_symlink_by_the_link_and_the_target_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload_for: Callable[[Path], dict[str, object]],
    link: str,
    agent: str | None,
    status: int,
    reason: str | None,
    store_reads: int,
) -> None:
    """PROT-44 (issue #486): a write through a file symlink is judged by the link's
    checkout and by the one its bytes land in, and the stricter verdict
    wins -- a claim covering the link never authorizes a write into
    another repository's, or a nested, main checkout, and a claim covering
    the target never opens a link in a main checkout. A target outside
    every repository, and `rm` of the link itself, stay the link's
    checkout's alone. Both checkouts' store-free checks run before either
    store is read, so a store-free denial in either reads no store; every
    row without an identity proves its verdict needs none."""
    _symlinks_across_checkouts(tmp_path)
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: agent} if agent else None)
    claim = _protect_active_claim("Ada", scope=("src",), branch="codex/issue-72-widget")
    fetches: list[Path] = []

    def fetch_state(*, worktree: Path, remote: str) -> protocol.ClaimState:
        fetches.append(worktree)
        return _protect_state_with_claim(claim)

    monkeypatch.setattr(store, "fetch_state", fetch_state)

    assert _protect_main(monkeypatch, payload_for(tmp_path / link)) == status
    _assert_protect_decision(
        capsys,
        decision="deny" if reason else "allow",
        reason=reason.format(tmp_path=tmp_path) if reason else None,
    )
    assert len(fetches) == store_reads


@pytest.mark.parametrize(
    ("failing_store", "reason"),
    [
        ("src/nested/repo-worktrees/issue-72-widget", "store unreadable"),
        ("", "claim first"),
    ],
    ids=[
        "target-store-fails-and-the-link-store-is-still-read",
        "target-denial-wins-over-a-link-failure",
    ],
)
def test_protect_runs_both_symlink_claim_checks_when_one_store_read_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failing_store: str,
    reason: str,
) -> None:
    """PROT-44 (issue #486): a failure reading one checkout's store is that
    checkout's denial, never a reason to skip the other's claim check, and
    on a double denial the target's is reported -- here a link inside the
    claimed worktree's scope into a nested worktree no claim covers."""
    _symlinks_across_checkouts(tmp_path)
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Ada"})
    claim = _protect_active_claim("Ada", scope=("src",), branch="codex/issue-72-widget")
    unreadable = tmp_path / _CLAIMED_WORKTREE / failing_store
    fetches: list[Path] = []

    def fetch_state(*, worktree: Path, remote: str) -> protocol.ClaimState:
        fetches.append(worktree)
        if worktree == unreadable:
            raise RuntimeError("store unreadable")
        return _protect_state_with_claim(claim)

    monkeypatch.setattr(store, "fetch_state", fetch_state)

    link = tmp_path / _CLAIMED_WORKTREE / "src" / "into-nested-worktree.md"
    assert _protect_main(monkeypatch, _write_target_payload(link)) == 2
    _assert_protect_decision(capsys, decision="deny", reason=reason)
    assert len(fetches) == 2


@pytest.mark.parametrize(
    ("link", "reason", "store_reads"),
    [
        (f"{_CLAIMED_WORKTREE}/src/into-other.md", "not main", 0),
        (f"{_CLAIMED_WORKTREE}/src/into-nested-worktree.md", "claim first", 1),
        (
            "claimed/repo/into-worktree.md",
            ".agent-claim/board.toml is not tracked in this checkout, so its "
            "storage pin cannot be trusted: git add -f .agent-claim/board.toml",
            0,
        ),
    ],
    ids=[
        "link-fails-target-main-checkout-denies-not-main",
        "link-fails-target-claim-denial-wins",
        "target-fails-over-link-main-checkout",
    ],
)
def test_protect_lets_the_targets_verdict_win_when_one_checkouts_board_is_untracked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    link: str,
    reason: str,
    store_reads: int,
) -> None:
    """PROT-44, PROT-29 (issues #486, #490): when a write through a symlink
    lands in another checkout and one of the two checkouts' board
    configuration is untracked, the target's verdict is the one reported --
    its store-free or claim denial over a link that failed, and its own
    failure over the link's "not main"."""
    _symlinks_across_checkouts(tmp_path)
    _use_real_path_is_tracked(monkeypatch)
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Ada"})
    claimed_worktree = tmp_path / _CLAIMED_WORKTREE
    _real_git(claimed_worktree, "rm", "-q", "--cached", ".agent-claim/board.toml")
    claim = _protect_active_claim("Ada", scope=("src",), branch="codex/issue-72-widget")
    fetches: list[Path] = []

    def fetch_state(*, worktree: Path, remote: str) -> protocol.ClaimState:
        fetches.append(worktree)
        return _protect_state_with_claim(claim)

    monkeypatch.setattr(store, "fetch_state", fetch_state)

    payload = _write_target_payload(tmp_path / link)
    assert _protect_main(monkeypatch, payload) == 2
    _assert_protect_decision(capsys, decision="deny", reason=reason)
    assert len(fetches) == store_reads


def test_protect_denies_path_required_for_a_worktree_symlink_leading_outside_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A write through a file symlink inside the worktree whose target lies
    outside it names no scope entry of that checkout, so it denies `path
    required` before the store is read -- unlike the paths no claim can ever
    cover, which name their own sentence (issue #483)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Ada"})
    _main, worktree = _protect_real_repo_with_worktree(tmp_path)
    link = worktree / "docs" / "outside.md"
    link.symlink_to(_file_outside_every_repository(tmp_path))
    monkeypatch.setattr(store, "fetch_state", _store_must_not_be_read)

    assert _protect_main(monkeypatch, _write_target_payload(link)) == 2
    _assert_protect_decision(capsys, decision="deny", reason=protect.PATH_REQUIRED)


def _path_below_a_file(_main: Path, worktree: Path) -> tuple[Path, str]:
    path = worktree / "README.md" / "x.py"
    return path, f"{path} cannot exist: {worktree / 'README.md'} is a file"


def _path_below_a_dangling_symlink(_main: Path, worktree: Path) -> tuple[Path, str]:
    link = worktree / "dangling"
    link.symlink_to(worktree.parent / "nowhere", target_is_directory=True)
    path = link / "deep" / "x.py"
    return path, f"{path} cannot exist: {link} is a dangling symlink"


def _the_checkout_root(_main: Path, worktree: Path) -> tuple[Path, str]:
    return worktree, _checkout_root_reason(worktree)


def _path_in_a_bare_repository(main: Path, _worktree: Path) -> tuple[Path, str]:
    hook = _hook_in_a_bare_repository(main.parent)
    return hook, f"not a checkout: {hook.parent.parent.resolve()} is a git directory"


def _path_in_a_checkouts_git_directory(main: Path, _worktree: Path) -> tuple[Path, str]:
    git_directory = main / ".git"
    return (
        git_directory / "info" / "x",
        f"not a checkout: {git_directory.resolve()} is a git directory",
    )


def _protect_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], path: Path
) -> tuple[int, str]:
    status = _protect_main(monkeypatch, _write_target_payload(path))
    captured = capsys.readouterr()
    assert captured.err == f"{json.loads(captured.out)['reason']}\n"
    return status, captured.err.removesuffix("\n")


def _rescope_refusal(
    _monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], path: Path
) -> tuple[int, str]:
    status = issue_claim.main(["rescope", "72", "--add", str(path)])
    return status, capsys.readouterr().err.removeprefix("ERROR: ").removesuffix("\n")


def _store_must_not_be_read(*, worktree: Path, remote: str) -> protocol.ClaimState:
    raise AssertionError(f"store read for {worktree} on {remote}")


@pytest.mark.parametrize(
    "refusal_of", [_protect_refusal, _rescope_refusal], ids=["protect", "rescope"]
)
@pytest.mark.parametrize(
    "build_case",
    [
        _path_below_a_file,
        _path_below_a_dangling_symlink,
        _the_checkout_root,
        _path_in_a_bare_repository,
        _path_in_a_checkouts_git_directory,
    ],
    ids=["below-a-file", "below-a-dangling-symlink", "checkout-root", "bare-repository", "dot-git"],
)
def test_protect_and_rescope_refuse_a_path_no_claim_can_cover_with_one_sentence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    refusal_of: Callable[[pytest.MonkeyPatch, pytest.CaptureFixture[str], Path], tuple[int, str]],
    build_case: Callable[[Path, Path], tuple[Path, str]],
) -> None:
    """PROT-14, PROT-42, PROT-43 and RESC-19 (issue #483, #310 findings 110,
    111, 112, 115): a path below a file or a dangling symlink, the checkout
    root itself, and a path inside a git directory are refused by `protect`
    and `rescope` with the same sentence, before the store is read."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})
    _use_real_path_is_tracked(monkeypatch)
    main, worktree = _protect_real_repo_with_worktree(tmp_path)
    path, sentence = build_case(main, worktree)
    monkeypatch.setattr(store, "fetch_state", _store_must_not_be_read)

    assert refusal_of(monkeypatch, capsys, path) == (2, sentence)


def _hub_canonical_worktree_file(
    tmp_path: Path, *, canonical: str, canonical_head: str | None, branch: str
) -> Path:
    """A file in a linked worktree on `branch` of a repository with the
    remotes `origin` and `hub` whose tracked `board.toml` makes `canonical`
    canonical, with `origin/HEAD` naming `main` and `<canonical>/HEAD`
    naming `canonical_head` -- or never recorded (issue #490). A recorded
    `HEAD` of a canonical remote the repository never configured is one a
    removed remote left behind (issue #492)."""
    main = tmp_path / "repo"
    main.mkdir()
    _real_git(main, "init", "-q", "-b", "main")
    _real_git(main, "config", "user.name", "Test")
    _real_git(main, "config", "user.email", "test@example.com")
    (main / ".agent-claim").mkdir()
    (main / ".agent-claim" / "board.toml").write_text(f'canonical_remote = "{canonical}"\n')
    _real_git(main, "add", "-f", ".agent-claim/board.toml")
    _real_git(main, "commit", "-q", "-m", "initial")
    _real_git(main, "branch", "trunk")
    for remote in ("origin", "hub"):
        _real_git(main, "remote", "add", remote, f"https://example.invalid/{remote}/repo.git")
        _real_git(main, "update-ref", f"refs/remotes/{remote}/main", "HEAD")
        _real_git(main, "update-ref", f"refs/remotes/{remote}/trunk", "HEAD")
    _real_git(main, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    if canonical_head is not None:
        recorded = f"refs/remotes/{canonical}/{canonical_head}"
        _real_git(main, "update-ref", recorded, "HEAD")
        _real_git(main, "symbolic-ref", f"refs/remotes/{canonical}/HEAD", recorded)
    worktree = tmp_path / "repo-worktrees" / "lane"
    worktree.parent.mkdir()
    _real_git(main, "worktree", "add", "-q", str(worktree), "-B", branch)
    return worktree / "widget.py"


def _claim_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], path: Path
) -> tuple[int, str]:
    """`claim` standing in `path`'s worktree: its toplevel read asks that
    worktree, past the autouse toplevel isolation."""
    isolated_git_output = checkout._git_output

    def git_output(arguments: list[str], *, directory: Path | None = None) -> str:
        return isolated_git_output(arguments, directory=directory or path.parent)

    monkeypatch.setattr(checkout, "_git_output", git_output)
    monkeypatch.chdir(path.parent)
    status = issue_claim.main(["claim", "72", "--scope", path.name])
    return status, capsys.readouterr().err.removeprefix("ERROR: ").removesuffix("\n")


_CLAIM_ON_THE_DEFAULT_BRANCH = (
    "build claims require an isolated non-main worktree branch; run git worktree add "
    "../<repo>-worktrees/issue-<n>-<slug> -b <agent>/issue-<n>-<slug>"
)


_UNCONFIGURED_UPSTREAM = "cannot determine the trunk: canonical remote 'upstream' is not configured"


@pytest.mark.usefixtures("isolated_global_git_config")
@pytest.mark.parametrize(
    ("refusal_of", "canonical", "canonical_head", "branch", "sentence"),
    [
        pytest.param(
            _claim_refusal,
            "hub",
            "trunk",
            "trunk",
            _CLAIM_ON_THE_DEFAULT_BRANCH,
            id="claim-hub-default",
        ),
        pytest.param(
            _claim_refusal,
            "hub",
            None,
            "master",
            _CLAIM_ON_THE_DEFAULT_BRANCH,
            id="claim-hub-unrecorded-guesses",
        ),
        pytest.param(
            _protect_refusal, "hub", "trunk", "trunk", "not main", id="protect-hub-default"
        ),
        pytest.param(
            _rescope_refusal,
            "hub",
            "trunk",
            "trunk",
            "build claims require an isolated non-main worktree branch; "
            "run this command from this claim's own worktree, not the primary checkout",
            id="rescope-hub-default",
        ),
        pytest.param(
            _protect_refusal,
            "hub",
            None,
            "codex/issue-72-widget",
            checkout.DEFAULT_BRANCH_UNKNOWN_REASON,
            id="protect-hub-unrecorded",
        ),
        pytest.param(
            _rescope_refusal,
            "hub",
            None,
            "codex/issue-72-widget",
            checkout.DEFAULT_BRANCH_UNKNOWN_REASON,
            id="rescope-hub-unrecorded",
        ),
        pytest.param(
            _protect_refusal,
            "upstream",
            None,
            "codex/issue-72-widget",
            _UNCONFIGURED_UPSTREAM,
            id="protect-canonical-remote-not-configured",
        ),
        pytest.param(
            _rescope_refusal,
            "upstream",
            None,
            "codex/issue-72-widget",
            _UNCONFIGURED_UPSTREAM,
            id="rescope-canonical-remote-not-configured",
        ),
        pytest.param(
            _protect_refusal,
            "upstream",
            "trunk",
            "codex/issue-72-widget",
            _UNCONFIGURED_UPSTREAM,
            id="protect-unconfigured-remote-left-a-recorded-head",
        ),
        pytest.param(
            _rescope_refusal,
            "upstream",
            "trunk",
            "codex/issue-72-widget",
            _UNCONFIGURED_UPSTREAM,
            id="rescope-unconfigured-remote-left-a-recorded-head",
        ),
    ],
)
def test_protect_and_rescope_judge_the_canonical_remotes_recorded_default_branch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    refusal_of: Callable[[pytest.MonkeyPatch, pytest.CaptureFixture[str], Path], tuple[int, str]],
    canonical: str,
    canonical_head: str | None,
    branch: str,
    sentence: str,
) -> None:
    """Issue #490 proof 1 (PROT-12, PROT-13, RESC-20, CLM-29): with `hub`
    canonical and `origin/HEAD` naming `main`, a worktree on `hub`'s
    default branch is refused; without a recorded `hub/HEAD`, `protect` and
    `rescope` refuse `default branch unknown` while `claim` guesses
    `main`/`master` -- `origin/HEAD` never answers for `hub`. A canonical
    remote the clone never added is named instead, in every command's own
    sentence, even where a `HEAD` it left behind still resolves (issues
    #492, #516)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})
    _use_real_path_is_tracked(monkeypatch)
    path = _hub_canonical_worktree_file(
        tmp_path, canonical=canonical, canonical_head=canonical_head, branch=branch
    )
    monkeypatch.setattr(store, "fetch_state", _store_must_not_be_read)

    assert refusal_of(monkeypatch, capsys, path) == (2, sentence)


@pytest.mark.usefixtures("isolated_global_git_config")
@pytest.mark.parametrize(
    "refusal_of", [_protect_refusal, _rescope_refusal], ids=["protect", "rescope"]
)
def test_protect_and_rescope_name_a_canonical_remote_that_has_config_lines_but_no_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    refusal_of: Callable[[pytest.MonkeyPatch, pytest.CaptureFixture[str], Path], tuple[int, str]],
) -> None:
    """Issues #512 line 1 and #516 line 2 (PROT-45, RESC-21): a
    `remote.upstream.fetch` line without a URL, beside a `HEAD` the remote
    left behind, is still no configured canonical remote -- `protect` and
    `rescope` name it in every command's own sentence rather than judging
    its branch."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})
    _use_real_path_is_tracked(monkeypatch)
    path = _hub_canonical_worktree_file(
        tmp_path, canonical="upstream", canonical_head="trunk", branch="codex/issue-72-widget"
    )
    _real_git(
        path.parent, "config", "remote.upstream.fetch", "+refs/heads/*:refs/remotes/upstream/*"
    )
    monkeypatch.setattr(store, "fetch_state", _store_must_not_be_read)

    assert refusal_of(monkeypatch, capsys, path) == (2, _UNCONFIGURED_UPSTREAM)
