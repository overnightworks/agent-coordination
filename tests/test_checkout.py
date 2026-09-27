"""Direct `checkout.py` behavior: remote/worktree/branch validation, the
`_git_output` boundary and its `directory` (issue #314), `resolve_path_checkout`
(the path-based checkout resolver `protect` and `rescope` share), dirty-path
reading, `versioned_paths`, trunk landings, and remote-location parsing.
`_validate_checkout` -- the precondition every `claim` call site in `cli.py`
uses -- is exercised directly. Tests that drive these through
`issue_claim.main([...])` stay in `tests/test_cli.py` as CLI-wiring behavior."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from board_fixtures import BASE, request
from cli_fixtures import (
    _git_checkout,
    _push_repository_trunk,
    _real_git,
    _real_repository_with_bare_remote,
    _set_agent_identity_env,
    _stub_one_git_call,
    dangle_recorded_head,
)

from agent_coordination import board, checkout, process
from agent_coordination.protocol import ClaimError, ClaimRequest

_LIVE_VERSIONED_PATHS = checkout.versioned_paths
_LIVE_TRUNK_LANDINGS = checkout.trunk_landings


@pytest.mark.parametrize("remote", ["origin", "upstream"])
@pytest.mark.parametrize(
    "from_repository_cwd",
    [
        pytest.param(True, id="repository-cwd-without-directory"),
        pytest.param(False, id="directory-from-a-non-repository-cwd"),
    ],
)
def test_remote_url_reads_the_git_config_of_the_given_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    remote: str,
    from_repository_cwd: bool,
) -> None:
    """Issue #457 proof 7 at the checkout boundary: a remote's URL comes
    from the git configuration of the directory it is asked for, or of the
    calling process's cwd when none is given."""
    repo, origin = _real_repository_with_bare_remote(tmp_path)
    remote_urls = {"origin": str(origin), "upstream": "git@github.com:owner/repository.git"}
    _real_git(repo, "remote", "add", "upstream", remote_urls["upstream"])
    outside_every_repository = tmp_path / "elsewhere"
    outside_every_repository.mkdir()
    monkeypatch.chdir(repo if from_repository_cwd else outside_every_repository)
    where = {} if from_repository_cwd else {"directory": repo}

    assert checkout.remote_url(remote, **where) == remote_urls[remote]


@pytest.mark.parametrize(
    ("toplevel_readable", "expected"),
    [(True, ("scratch", "docs")), (False, ("docs",))],
    ids=["held-toplevel", "failed-toplevel-read"],
)
def test_scope_directories_finds_git_trees_and_untracked_directories_under_the_held_toplevel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    toplevel_readable: bool,
    expected: tuple[str, ...],
) -> None:
    """Issue #472: an entry that is no git tree is looked up under the
    caller's own held toplevel, never a second `rev-parse`; a failed read of
    it counts no untracked directory, and a later git tree still counts."""
    (tmp_path / "scratch").mkdir()
    (tmp_path / "file.py").write_text("x\n")

    def git(arguments: list[str], **_kwargs: object) -> str:
        if arguments == ["cat-file", "-t", "HEAD:docs"]:
            return "tree"
        raise ClaimError("not in HEAD")

    def toplevel() -> Path:
        if toplevel_readable:
            return tmp_path
        raise ClaimError("fatal: not a git repository")

    monkeypatch.setattr(checkout, "_git_output", git)

    directories = checkout._scope_directories(
        ("scratch", "file.py", "docs"), directory=None, toplevel=toplevel
    )

    assert directories == expected


def test_paths_under_scope_matches_prefix_or_exact_entry() -> None:
    paths = ("LICENSE", "src/a.py", "src/b.py", "docs/a.md")

    assert checkout.paths_under_scope(paths, ("src",)) == ("src/a.py", "src/b.py")
    assert checkout.paths_under_scope(paths, ("LICENSE",)) == ("LICENSE",)
    assert checkout.paths_under_scope(paths, ("src/a.py", "docs")) == ("src/a.py", "docs/a.md")
    assert checkout.paths_under_scope(paths, ("missing",)) == ()


def test_checkout_validation_binds_clean_head_and_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        ("rev-parse", "HEAD"): BASE,
        ("branch", "--show-current"): "codex/issue-71-claims",
        ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
        ("rev-parse", "--git-common-dir"): "/repo/.git",
        ("status", "--porcelain"): "",
    }
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: values[tuple(arguments)]
    )

    checkout._validate_checkout(request(), default_branch=lambda: "main")


@pytest.mark.parametrize(
    ("candidate", "values", "message"),
    [
        (
            request(),
            {
                ("rev-parse", "HEAD"): "b" * 40,
                ("branch", "--show-current"): "codex/issue-71-claims",
                ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
                ("rev-parse", "--git-common-dir"): "/repo/.git",
                ("status", "--porcelain"): "",
            },
            "does not match checkout HEAD",
        ),
        (
            request(),
            {
                ("rev-parse", "HEAD"): BASE,
                ("branch", "--show-current"): "other",
                ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
                ("rev-parse", "--git-common-dir"): "/repo/.git",
                ("status", "--porcelain"): "",
            },
            "does not match checkout branch",
        ),
        (
            request(),
            {
                ("rev-parse", "HEAD"): BASE,
                ("branch", "--show-current"): "codex/issue-71-claims",
                ("rev-parse", "--git-dir"): "/repo/.git",
                ("rev-parse", "--git-common-dir"): "/repo/.git",
                ("status", "--porcelain"): "",
            },
            "linked isolated worktree",
        ),
        (
            request(),
            {
                ("rev-parse", "HEAD"): BASE,
                ("branch", "--show-current"): "codex/issue-71-claims",
                ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
                ("rev-parse", "--git-common-dir"): "/repo/.git",
                ("status", "--porcelain"): " M file",
            },
            "before the first worktree edit",
        ),
    ],
)
def test_checkout_validation_rejects_false_or_late_claims(
    monkeypatch: pytest.MonkeyPatch,
    candidate: ClaimRequest,
    values: dict[tuple[str, ...], str],
    message: str,
) -> None:
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: values[tuple(arguments)]
    )

    with pytest.raises(ClaimError, match=message):
        checkout._validate_checkout(candidate, default_branch=lambda: "main")


def test_checkout_validation_names_the_base_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    """The base-mismatch refusal names both SHAs (unchanged) and the repair
    an agent reading it needs: omitting `--base` binds it to checkout HEAD."""
    values = {
        ("rev-parse", "HEAD"): "b" * 40,
        ("branch", "--show-current"): "codex/issue-71-claims",
        ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
        ("rev-parse", "--git-common-dir"): "/repo/.git",
        ("status", "--porcelain"): "",
    }
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: values[tuple(arguments)]
    )
    candidate = request()

    with pytest.raises(ClaimError) as error:
        checkout._validate_checkout(candidate, default_branch=lambda: "main")

    assert str(error.value) == (
        f"claim base {BASE} does not match checkout HEAD {'b' * 40}; "
        "omit --base to use checkout HEAD"
    )


@pytest.mark.parametrize(
    ("branch", "default_branch"),
    [
        pytest.param("main", None, id="guessed-main"),
        pytest.param("trunk", "trunk", id="repository-default-trunk"),
    ],
)
def test_checkout_validation_names_the_isolated_worktree_recipe_for_the_default_branch(
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
    default_branch: str | None,
) -> None:
    """Claiming from a checkout of the repository's default branch names the
    exact `git worktree add` recipe (#52), not just the rule it violates --
    whether that default is the guessed `main` or a recorded one (issue
    #238: a repository whose default is `trunk` refuses a claim from
    `trunk` the same way)."""
    values = {("rev-parse", "HEAD"): BASE}
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: values[tuple(arguments)]
    )
    candidate = request(branch=branch)

    with pytest.raises(ClaimError) as error:
        checkout._validate_checkout(candidate, default_branch=lambda: default_branch)

    assert str(error.value) == (
        "build claims require an isolated non-main worktree branch; "
        f"run {checkout.ISOLATED_WORKTREE_RECIPE}"
    )


def test_checkout_validation_names_the_isolated_worktree_recipe_for_a_shared_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkout whose git-dir is the shared common dir (not a linked
    worktree) names the same recipe as the trunk-branch refusal above."""
    values = {
        ("rev-parse", "HEAD"): BASE,
        ("branch", "--show-current"): "codex/issue-71-claims",
        ("rev-parse", "--git-dir"): "/repo/.git",
        ("rev-parse", "--git-common-dir"): "/repo/.git",
        ("status", "--porcelain"): "",
    }
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: values[tuple(arguments)]
    )
    candidate = request()

    with pytest.raises(ClaimError) as error:
        checkout._validate_checkout(candidate, default_branch=lambda: "main")

    assert str(error.value) == (
        "build claims require a linked isolated worktree checkout; "
        f"run {checkout.ISOLATED_WORKTREE_RECIPE}"
    )


def test_checkout_validation_names_the_first_three_dirty_paths_and_the_rest_as_a_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dirty-tree refusal used to discard `git status --porcelain`'s
    list entirely; it now names the first three changed paths and how many
    more there are (#52)."""
    porcelain = "\n".join(
        [" M src/a.py", " M src/b.py", "?? src/c.py", " M src/d.py", " M src/e.py"]
    )
    values = {
        ("rev-parse", "HEAD"): BASE,
        ("branch", "--show-current"): "codex/issue-71-claims",
        ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
        ("rev-parse", "--git-common-dir"): "/repo/.git",
        ("status", "--porcelain"): porcelain,
    }
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: values[tuple(arguments)]
    )
    candidate = request()

    with pytest.raises(ClaimError) as error:
        checkout._validate_checkout(candidate, default_branch=lambda: "main")

    assert str(error.value) == (
        "claim must be acquired before the first worktree edit: "
        "src/a.py, src/b.py, src/c.py, and 2 more"
    )


def test_checkout_validation_names_every_dirty_path_when_three_or_fewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No trailing count when every changed path already fits in the first
    three named."""
    values = {
        ("rev-parse", "HEAD"): BASE,
        ("branch", "--show-current"): "codex/issue-71-claims",
        ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
        ("rev-parse", "--git-common-dir"): "/repo/.git",
        ("status", "--porcelain"): " M src/a.py",
    }
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: values[tuple(arguments)]
    )
    candidate = request()

    with pytest.raises(ClaimError) as error:
        checkout._validate_checkout(candidate, default_branch=lambda: "main")

    assert str(error.value) == "claim must be acquired before the first worktree edit: src/a.py"


def _scratch_git_repository(tmp_path: Path, *init_options: str) -> Path:
    """An initialized repository with one committed, tracked file -- for
    tests that drive `_dirty_paths` against real `git status --porcelain`
    output instead of a fake standing in for `_git_output` itself.
    `init_options` shape its git directory's layout."""
    repository = tmp_path / "repo"
    repository.mkdir()
    _real_git(repository, "init", "-q", "-b", "main", *init_options)
    _real_git(repository, "config", "user.name", "Test")
    _real_git(repository, "config", "user.email", "test@example.com")
    (repository / "README.md").write_text("hello\n")
    _real_git(repository, "add", "README.md")
    _real_git(repository, "commit", "-q", "-m", "initial")
    return repository


def test_dirty_paths_reads_a_modified_tracked_file_from_real_git_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the truncated-name bug (#52 follow-up): `_git_output`
    used to `.strip()` its whole decoded output, which ate the leading space
    of a modified file's ` M path` porcelain line before `_dirty_paths`
    sliced off the fixed three-character status prefix -- `README.md` came
    back as `EADME.md`. A fake that hands `_dirty_paths` a hand-typed string
    with the leading space intact cannot catch this; only the real reader
    against real git output can."""
    repository = _scratch_git_repository(tmp_path)
    (repository / "README.md").write_text("hello\nmodified\n")
    monkeypatch.chdir(repository)

    assert checkout._dirty_paths(checkout._git_output(["status", "--porcelain"])) == ("README.md",)


def test_dirty_paths_reads_an_untracked_file_from_real_git_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The untracked `?? path` line has no leading space to lose, which is
    why the truncation bug above went unnoticed."""
    repository = _scratch_git_repository(tmp_path)
    (repository / "extra.txt").write_text("new\n")
    monkeypatch.chdir(repository)

    assert checkout._dirty_paths(checkout._git_output(["status", "--porcelain"])) == ("extra.txt",)


def test_dirty_paths_reads_a_rename_from_real_git_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_dirty_paths`'s docstring claims a rename's `old -> new` line is
    handled; prove it against a real rename rather than a hand-typed line
    that could not tell us whether the code actually handles it."""
    repository = _scratch_git_repository(tmp_path)
    _real_git(repository, "mv", "README.md", "RENAMED.md")
    monkeypatch.chdir(repository)

    assert checkout._dirty_paths(checkout._git_output(["status", "--porcelain"])) == (
        "README.md -> RENAMED.md",
    )


def test_git_output_directory_runs_git_dash_c_in_that_directory(tmp_path: Path) -> None:
    """`directory` (issue #314) must reach the real `git` process as `-C`,
    not merely be accepted and ignored -- the one fact every path-based
    resolution downstream of `_git_output` depends on."""
    repository = _scratch_git_repository(tmp_path)

    assert checkout._git_output(["rev-parse", "--show-toplevel"], directory=repository) == str(
        repository.resolve()
    )


def test_git_output_denies_loud_on_a_non_standard_os_error_launching_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_captured` translates a missing executable and a timeout to their
    own typed errors, but leaves every other OS-level launch failure --
    permission denied, out of file descriptors, `-C` naming a non-directory
    -- as a raw `OSError` (issue #314 gate G's follow-up). Every
    `_git_output` caller judges a checkout for a security decision, so this
    must fail closed with `ClaimError` too, never an uncaught traceback out
    of `protect`'s hook boundary."""

    def raises_os_error(command: list[str], **_kwargs: object) -> object:
        raise PermissionError("denied")

    monkeypatch.setattr(process, "run_captured", raises_os_error)

    with pytest.raises(ClaimError, match="git failed to launch: denied"):
        checkout._git_output(["rev-parse", "--verify", "HEAD"])


def _repo_with_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A real repository with a linked, isolated worktree on a feature
    branch (issue #314) -- `main` is the shared checkout, `worktree` is what
    a claim actually builds in, via the same `git worktree add` recipe
    `checkout.ISOLATED_WORKTREE_RECIPE` documents."""
    main = _scratch_git_repository(tmp_path)
    worktree = tmp_path / "repo-worktrees" / "issue-1-widget"
    worktree.parent.mkdir(parents=True)
    _real_git(main, "worktree", "add", "-q", str(worktree), "-b", "codex/issue-1-widget")
    return main, worktree


def test_resolve_path_checkout_reads_the_linked_worktree_owning_a_directory(
    tmp_path: Path,
) -> None:
    _main, worktree = _repo_with_linked_worktree(tmp_path)
    (worktree / "src").mkdir()

    resolved = checkout.resolve_path_checkout(worktree / "src")

    assert resolved == checkout.PathCheckout(
        toplevel=worktree.resolve(),
        branch="codex/issue-1-widget",
        kind=checkout.CheckoutKind.LINKED_WORKTREE,
        common_directory=(tmp_path / "repo" / ".git").resolve(),
        has_commit=True,
    )


def test_resolve_path_checkout_reads_the_main_checkout_owning_a_directory(
    tmp_path: Path,
) -> None:
    main, _worktree = _repo_with_linked_worktree(tmp_path)

    resolved = checkout.resolve_path_checkout(main)

    assert resolved == checkout.PathCheckout(
        toplevel=main.resolve(),
        branch="main",
        kind=checkout.CheckoutKind.MAIN,
        common_directory=(main / ".git").resolve(),
        has_commit=True,
    )


def test_resolve_path_checkout_is_independent_of_the_calling_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of issue #314: resolving from `directory` rather than
    an implicit process cwd means the same directory resolves the same way
    regardless of where the test process itself is standing."""
    _main, worktree = _repo_with_linked_worktree(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    resolved = checkout.resolve_path_checkout(worktree)

    assert resolved is not None
    assert resolved.kind is checkout.CheckoutKind.LINKED_WORKTREE
    assert resolved.branch == "codex/issue-1-widget"


def test_resolve_path_checkout_denies_a_directory_outside_every_repository(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "not-a-repo"
    outside.mkdir()

    assert checkout.resolve_path_checkout(outside) is None


_ISOLATED_NON_MAIN_BRANCH_SENTENCE = (
    "build claims require an isolated non-main worktree branch; "
    "run this command from this claim's own worktree, not the primary checkout"
)


@pytest.mark.parametrize(
    ("branch", "kind", "default_branch", "expected"),
    [
        pytest.param(
            "main",
            checkout.CheckoutKind.LINKED_WORKTREE,
            "main",
            _ISOLATED_NON_MAIN_BRANCH_SENTENCE,
            id="trunk-branch-names-none",
        ),
        pytest.param(
            "codex/issue-211-worktree-repair-sentence",
            checkout.CheckoutKind.MAIN,
            "main",
            "build claims require a linked isolated worktree checkout; "
            "run this command from this claim's own worktree on "
            "'codex/issue-211-worktree-repair-sentence', not the primary checkout",
            id="known-branch-named",
        ),
        pytest.param(
            "master",
            checkout.CheckoutKind.LINKED_WORKTREE,
            "master",
            _ISOLATED_NON_MAIN_BRANCH_SENTENCE,
            id="master-default-branch",
        ),
        pytest.param(
            "trunk",
            checkout.CheckoutKind.LINKED_WORKTREE,
            "trunk",
            _ISOLATED_NON_MAIN_BRANCH_SENTENCE,
            id="trunk-default-branch",
        ),
        pytest.param(
            "main",
            checkout.CheckoutKind.LINKED_WORKTREE,
            None,
            checkout.DEFAULT_BRANCH_UNKNOWN_REASON,
            id="unrecorded-default-branch-never-guessed",
        ),
    ],
)
def test_refuse_shared_checkout_matrix(
    tmp_path: Path,
    branch: str,
    kind: checkout.CheckoutKind,
    default_branch: str | None,
    expected: str,
) -> None:
    """`rescope`'s own worktree-isolation refusal (issue #314 repeat gate,
    finding 3) judges the repository's recorded default branch -- not just
    the hardcoded `main`/`master` fallback, but any a real clone can record
    (`master`, `trunk`) -- and never falls back to that guess when none is
    recorded at all, not even for `main`.

    `RETURN_TO_CLAIM` is used throughout: `rescope` acts on a claim whose
    worktree already exists, so recommending the `git worktree add` recipe
    would build a second, foreign one. On the trunk branch no other branch
    is known here to name, so `RETURN_TO_CLAIM` points back at the claim's
    own worktree without inventing one; checked out directly on a real
    branch inside the shared (non-linked) checkout, that branch is already
    known -- it is the same branch the caller resolved its identity from --
    so `RETURN_TO_CLAIM` names it instead of leaving the sentence
    branch-less."""
    repository, _remote = _real_repository_with_bare_remote(tmp_path)
    path_checkout = checkout.PathCheckout(
        toplevel=repository,
        branch=branch,
        kind=kind,
        common_directory=repository / ".git",
        has_commit=True,
    )
    repair = checkout.WorktreeRepair.RETURN_TO_CLAIM

    with pytest.raises(ClaimError) as error:
        checkout._refuse_shared_checkout(
            path_checkout, default_branch=default_branch, canonical_remote="origin", repair=repair
        )

    assert str(error.value) == expected


@pytest.mark.parametrize(
    ("branch", "denied"),
    [("main", True), ("master", True), ("trunk", False)],
)
def test_claim_default_branch_fallback_denies_only_main_and_master(
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
    denied: bool,
) -> None:
    """When no default branch is recorded, `claim`'s fallback (issue #238,
    Grok review) still denies exactly the historical `{"main", "master"}`
    guess and nothing else -- `trunk` is not treated as default without a
    recorded one, so deleting `DEFAULT_BRANCH_FALLBACK` would fail this
    test by letting `main`/`master` through instead."""
    values = _git_checkout(branch=branch)
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: values[tuple(arguments)]
    )
    candidate = request(branch=branch)

    if not denied:
        checkout._validate_checkout(candidate, default_branch=lambda: None)
        return

    with pytest.raises(ClaimError, match="isolated non-main worktree branch"):
        checkout._validate_checkout(candidate, default_branch=lambda: None)


def test_versioned_paths_reads_nul_terminated_ls_files_without_stripping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[list[str]] = []

    def run(arguments, **kwargs):
        observed.append(arguments)
        return subprocess.CompletedProcess(
            arguments, 0, stdout=b" foo.py\0bar.py\0 foo.py\0", stderr=b""
        )

    monkeypatch.setattr(subprocess, "run", run)

    assert _LIVE_VERSIONED_PATHS() == (" foo.py", "bar.py")
    assert observed == [["git", "ls-files", "-z", "--full-name"]]


@pytest.mark.parametrize(
    "git_call",
    [
        pytest.param(_LIVE_VERSIONED_PATHS, id="versioned-paths"),
        pytest.param(lambda: checkout.remote_url("origin"), id="remote-url"),
        pytest.param(
            lambda: checkout.path_is_tracked(board.CONFIG_PATH.as_posix()), id="path-is-tracked"
        ),
    ],
)
@pytest.mark.parametrize(
    ("raised", "match"),
    [
        pytest.param(
            FileNotFoundError("git"), "git is required for issue claims", id="missing-executable"
        ),
        pytest.param(
            subprocess.TimeoutExpired(["git"], process.DEFAULT_TIMEOUT_SECONDS),
            "git timed out while validating the build checkout",
            id="timed-out",
        ),
    ],
)
def test_checkout_git_calls_fail_loud_when_git_is_missing_or_times_out(
    monkeypatch: pytest.MonkeyPatch,
    git_call: Callable[[], object],
    raised: Exception,
    match: str,
) -> None:
    """`versioned_paths`, `remote_url`, and `path_is_tracked` -- all
    direct `subprocess.run` callers (`_git_output` backs `remote_url`)
    -- must translate a missing executable or a timeout to the same
    `ClaimError` text."""

    def fails(*_arguments, **_kwargs):
        raise raised

    monkeypatch.setattr(subprocess, "run", fails)
    with pytest.raises(ClaimError, match=match):
        git_call()


@pytest.mark.parametrize(
    "git_call",
    [
        pytest.param(_LIVE_VERSIONED_PATHS, id="versioned-paths"),
        pytest.param(
            lambda: checkout.path_is_tracked(board.CONFIG_PATH.as_posix()), id="path-is-tracked"
        ),
    ],
)
def test_checkout_git_calls_fail_loud_on_a_nonzero_git_exit(
    monkeypatch: pytest.MonkeyPatch, git_call: Callable[[], object]
) -> None:
    """`versioned_paths` and `path_is_tracked` both translate a real git
    failure exit -- 128 here, outside a git repository -- to the same
    `ClaimError` detail (issue #315 review): `path_is_tracked`'s own exit-1
    "not tracked" reading is proven separately by
    `test_path_is_tracked_reads_the_git_ls_files_exit_status`."""

    def failed(arguments, **_kwargs):
        return subprocess.CompletedProcess(
            arguments, 128, stdout=b"", stderr=b"fatal: not a git repository\n"
        )

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(ClaimError, match="fatal: not a git repository"):
        git_call()


@pytest.mark.parametrize(
    ("exit_status", "expected"),
    [
        pytest.param(0, True, id="tracked"),
        pytest.param(1, False, id="untracked-or-ignored"),
    ],
)
def test_path_is_tracked_reads_the_git_ls_files_exit_status(
    monkeypatch: pytest.MonkeyPatch, exit_status: int, expected: bool
) -> None:
    """`git ls-files --error-unmatch` exits 0 for a path git tracks and 1
    for any path it does not -- absent, merely untracked, and ignored alike
    (issue #315): the caller never has to tell those apart."""
    board_config_path = board.CONFIG_PATH.as_posix()
    observed: list[list[str]] = []

    def run(arguments, **_kwargs):
        observed.append(arguments)
        return subprocess.CompletedProcess(arguments, exit_status, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", run)

    assert checkout.path_is_tracked(board_config_path) is expected
    assert observed == [["git", "ls-files", "--error-unmatch", "--", board_config_path]]


def _untracked_board_config(repository: Path) -> None:
    config = repository / board.CONFIG_PATH.as_posix()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("version = 1\n")


def _write_gitignore_for_dot_directories(repository: Path) -> None:
    (repository / ".gitignore").write_text(".*/\n")
    _real_git(repository, "add", ".gitignore")
    _real_git(repository, "commit", "-q", "-m", "ignore dot directories")


def _ignored_board_config(repository: Path) -> None:
    _write_gitignore_for_dot_directories(repository)
    _untracked_board_config(repository)


def _tracked_board_config(repository: Path) -> None:
    _untracked_board_config(repository)
    _real_git(repository, "add", board.CONFIG_PATH.as_posix())
    _real_git(repository, "commit", "-q", "-m", "add board config")


def _tracked_but_ignored_board_config(repository: Path) -> None:
    _write_gitignore_for_dot_directories(repository)
    _untracked_board_config(repository)
    _real_git(repository, "add", "-f", board.CONFIG_PATH.as_posix())
    _real_git(repository, "commit", "-q", "-m", "add board config despite ignore")


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        pytest.param(lambda _repository: None, False, id="absent"),
        pytest.param(_untracked_board_config, False, id="untracked"),
        pytest.param(_ignored_board_config, False, id="ignored-via-gitignore"),
        pytest.param(_tracked_board_config, True, id="tracked"),
        pytest.param(_tracked_but_ignored_board_config, True, id="tracked-but-ignored"),
    ],
)
def test_path_is_tracked_reads_real_git_index_and_ignore_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup: Callable[[Path], None],
    expected: bool,
) -> None:
    """`path_is_tracked` against real git filesystem/index state, not a
    hand-typed exit code (issue #315 review): absent, merely untracked, and
    `.gitignore`-ignored (`.*/`, the pattern that hid `.agent-claim/` in the
    field checkout the issue reports) all read `False`; a tracked file reads
    `True` even when a later `.gitignore` pattern would also match it, since
    `git ls-files --error-unmatch` answers from the index, not the ignore
    rules -- the `git add -f` repair this issue's refusal names must keep
    working after it runs."""
    repository = _scratch_git_repository(tmp_path)
    setup(repository)
    monkeypatch.chdir(repository)

    assert checkout.path_is_tracked(board.CONFIG_PATH.as_posix()) is expected


def _fake_trunk_log_record(*fields: str) -> str:
    """One fake `git log -z` trunk-landing record: `fields` joined by
    `checkout._TRUNK_LANDING_FIELD_SEPARATOR`, terminated by that same
    separator -- real `git log -z` framing, where the record terminator and
    the field separator are the same NUL byte."""
    separator = checkout._TRUNK_LANDING_FIELD_SEPARATOR
    return separator.join(fields) + separator


def test_trunk_landings_walk_the_trunk_ref_they_are_given_not_the_work_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def git_output(arguments: list[str], **_kwargs: object) -> str:
        assert arguments[0] == "log"
        assert arguments[-3:] == ["-n", "20", "refs/remotes/hub/main"]
        return _fake_trunk_log_record(
            "sha1", "2026-08-29T00:00:00+00:00", "", ""
        ) + _fake_trunk_log_record("sha2", "2026-08-30T00:00:00Z", "#10", "")

    monkeypatch.setattr(checkout, "_git_output", git_output)
    landings = _LIVE_TRUNK_LANDINGS("refs/remotes/hub/main", 20, directory=tmp_path)

    assert landings == (
        checkout.TrunkLanding("sha1", datetime(2026, 8, 29, tzinfo=UTC), None, ()),
        checkout.TrunkLanding(
            "sha2",
            datetime(2026, 8, 30, tzinfo=UTC),
            board.TrunkWorkItemClassification((10,)),
            ("#10",),
        ),
    )


@pytest.mark.parametrize(
    ("is_repository", "failure"),
    [
        pytest.param(True, "cannot determine the trunk: none of ", id="no-candidate"),
        pytest.param(False, "^fatal: ", id="no-repository"),
    ],
)
def test_trunk_ref_after_fails_loud_when_no_candidate_branch_resolves(
    tmp_path: Path, is_repository: bool, failure: str
) -> None:
    """Neither a recorded `HEAD` nor any of the default-branch-name
    candidates resolving must fail loud rather than silently ruling every
    candidate's age as unknown (BRIEF-20); a git failure other than a
    missing ref is git's own, never read as one (issue #492)."""
    if is_repository:
        _real_git(tmp_path, "init", "-q", "-b", "trunk")
        _add_branchless_hub(tmp_path)

    with pytest.raises(ClaimError, match=failure):
        checkout.trunk_ref_after("hub", None, directory=tmp_path)


def _add_branchless_hub(repository: Path) -> None:
    """`hub` configured, but never fetched: it has no branch here yet."""
    _real_git(repository, "remote", "add", "hub", str(repository.parent / "hub.git"))


def _add_only_origin(repository: Path) -> None:
    """The board names `hub`, but this clone only ever added `origin`."""
    _real_git(repository, "remote", "add", "origin", str(repository.parent / "origin.git"))


def _trunk_or_refusal(remote: str, *, directory: Path) -> str:
    try:
        return checkout.trunk_ref_after(
            remote, checkout.recorded_head_ref(remote, directory=directory), directory=directory
        )
    except ClaimError as error:
        return str(error)


@pytest.mark.parametrize(
    ("add_remote", "expected"),
    [
        pytest.param(_add_branchless_hub, "main", id="configured-without-branches"),
        pytest.param(
            _add_only_origin,
            "cannot determine the trunk: canonical remote 'hub' is not configured",
            id="not-configured",
        ),
    ],
)
def test_trunk_ref_after_guesses_the_local_branch_only_for_a_configured_remote_without_branches(
    tmp_path: Path, add_remote: Callable[[Path], None], expected: str
) -> None:
    """A clone that never recorded its remote's `HEAD` still resolves
    through the historical `{main, master}` guess (issue #238), generalized
    to the caller's own remote name rather than `origin` alone (issue
    #304) -- but only for a remote it configured: one it never added is
    named instead, since its local `main` would report an unpushed commit
    as landed (issue #508)."""
    repository = _scratch_git_repository(tmp_path)
    add_remote(repository)

    assert _trunk_or_refusal("hub", directory=repository) == expected


def _record_origin_head(repo: Path, _remote: Path) -> None:
    _real_git(repo, "push", "-q", "origin", "main")
    _real_git(repo, "remote", "set-head", "origin", "main")


def _leave_origin_head_unrecorded(repo: Path, _remote: Path) -> None:
    _real_git(repo, "push", "-q", "origin", "main")


def _record_origin_head_as_a_plain_ref(repo: Path, _remote: Path) -> None:
    _real_git(repo, "push", "-q", "origin", "main")
    _real_git(repo, "update-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")


def _dangle_origin_head_after_a_rename(repo: Path, _remote: Path) -> None:
    dangle_recorded_head(repo, "origin")


@pytest.mark.parametrize(
    ("record", "expected_default_branch"),
    [
        pytest.param(_record_origin_head, "main", id="recorded"),
        pytest.param(_leave_origin_head_unrecorded, None, id="never-recorded"),
        pytest.param(_dangle_origin_head_after_a_rename, None, id="dangling"),
        pytest.param(_record_origin_head_as_a_plain_ref, None, id="plain-ref"),
    ],
)
def test_a_dangling_or_plain_recorded_head_counts_as_unrecorded_and_the_trunk_guesses_on(
    tmp_path: Path, record: Callable[[Path, Path], None], expected_default_branch: str | None
) -> None:
    """Issue #490: a remote `HEAD` that names no resolvable branch counts
    as never recorded -- no default branch, and the trunk guesses on with
    `origin/main` instead of handing git a ref it cannot resolve."""
    repo, remote = _real_repository_with_bare_remote(tmp_path)
    _real_git(repo, "commit", "-q", "--allow-empty", "-m", "initial")
    record(repo, remote)
    recorded_head = checkout.recorded_head_ref("origin", directory=repo)

    assert (
        checkout.recorded_default_branch("origin", directory=repo),
        checkout.trunk_ref_after("origin", recorded_head, directory=repo),
    ) == (expected_default_branch, "refs/remotes/origin/main")


def test_trunk_landings_is_empty_when_trunk_has_no_first_parent_landings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: "")
    assert _LIVE_TRUNK_LANDINGS("refs/remotes/origin/main", 20, directory=tmp_path) == ()


@pytest.mark.parametrize(
    "raw_commit_time",
    [
        pytest.param("not-a-timestamp", id="unparsable"),
        pytest.param("2026-08-29T00:00:00", id="missing-offset"),
    ],
)
def test_trunk_landings_fails_loud_on_a_malformed_commit_timestamp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw_commit_time: str
) -> None:
    """Neither an unparsable `%cI` line nor one git left offset-naive (both
    would only occur if git itself misbehaved) may silently produce a wrong
    ruling age; both fail loud with the same diagnostic."""

    monkeypatch.setattr(
        checkout,
        "_git_output",
        lambda _arguments, **_kwargs: _fake_trunk_log_record("sha1", raw_commit_time, "", ""),
    )
    with pytest.raises(ClaimError, match="git returned a malformed trunk landing timestamp"):
        _LIVE_TRUNK_LANDINGS("refs/remotes/origin/main", 20, directory=tmp_path)


def test_trunk_landings_fails_loud_on_a_log_stream_that_is_not_nul_framed_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A raw `git log -z` stream always ends in the same NUL that separates
    each record's own four fields (`_fake_trunk_log_record`); anything else
    -- here, a caller that fed back plain newline-joined text -- is git (or
    the fake) misbehaving, not a shape this reads silently."""

    monkeypatch.setattr(
        checkout, "_git_output", lambda _arguments, **_kwargs: "sha1\x00not-nul-terminated"
    )
    with pytest.raises(ClaimError, match="git returned a malformed trunk landing log"):
        _LIVE_TRUNK_LANDINGS("refs/remotes/origin/main", 20, directory=tmp_path)


def _minimal_pushed_repository(tmp_path: Path) -> Path:
    """A `hub`-remote worktree with one `main`, empty of any commit -- the
    common setup every real-`git` trunk-landing test that doesn't need the
    shared five-proof history (`_trunk_history_repository`) builds on."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _real_git(remote, "init", "-q", "--bare", "-b", "main")
    repo = tmp_path / "repo"
    repo.mkdir()
    _real_git(repo, "init", "-q", "-b", "main")
    _real_git(repo, "config", "user.name", "Test")
    _real_git(repo, "config", "user.email", "test@example.com")
    _real_git(repo, "config", "commit.gpgsign", "false")
    _real_git(repo, "remote", "add", "hub", str(remote))
    return repo


def _push_to_hub(repo: Path) -> None:
    _real_git(repo, "push", "-q", "hub", "main")
    _real_git(repo, "remote", "set-head", "hub", "main")


@pytest.mark.parametrize(
    "byte",
    ["\x1f", "\x1e", "\x01"],
    ids=["unit-separator", "record-separator", "start-of-heading"],
)
def test_trunk_landings_classifies_a_control_byte_inside_a_trailer_value_as_one_literal_defect(
    tmp_path: Path, byte: str
) -> None:
    """Issue #304 review, finding B1: git never escapes `\\x1f`, `\\x1e`, or
    `\\x01` inside a trailer value -- exactly the bytes the historical
    `\\x1f`-separated framing used to split repeated trailer values -- so a
    value that happens to contain one of them must read back as the one
    literal value git actually recorded, never as two fabricated work items.
    The NUL/newline framing reads it as data: `trunk_commit_classification`
    then reports that single literal value as a `ClassificationDefect`
    (LAND-03) rather than raising and aborting the whole read -- the commit
    lands neither #12 nor #13."""
    repo = _minimal_pushed_repository(tmp_path)
    (repo / "f.txt").write_text("content\n")
    _real_git(repo, "add", "f.txt")
    value = f"#12{byte}#13"
    _real_git(repo, "commit", "-q", "-m", "change", "-m", f"Work-Item: {value}")
    _push_to_hub(repo)

    [landing] = checkout.trunk_landings("refs/remotes/hub/main", 20, directory=repo)

    assert landing.classification == board.ClassificationDefect(
        f"carries `Work-Item: {value}`; a trunk trailer names #n, aco-xxxxxx, or the bare number n"
    )


def _trunk_history_repository(tmp_path: Path) -> Path:
    """A worktree pushed to a `hub` remote (never `origin`, issue #304
    proof 4) whose `main` carries, in first-parent order: a plain initial
    commit, a merge commit trailer-classified `Work-Item: #10`, a squash
    commit whose trailer block repeats `Work-Item:` twice, a commit landed
    through a real `git rebase` and trailer-classified `No-Item: docs`, and
    a commit whose `Work-Item:` line sits in prose, never its own trailer
    block. A `sidebranch` ref never joins that first-parent line. One
    history serves every one of the five proofs at once, since building a
    real git repository per proof would only repeat the same setup."""
    repo, _remote = _real_repository_with_bare_remote(tmp_path, remote_name="hub")
    (repo / "base.txt").write_text("base\n")
    _real_git(repo, "add", "base.txt")
    _real_git(repo, "commit", "-q", "-m", "initial")

    # A merge commit whose own message carries the trailer block.
    _real_git(repo, "checkout", "-q", "-b", "feature")
    (repo / "feature.txt").write_text("feature\n")
    _real_git(repo, "add", "feature.txt")
    _real_git(repo, "commit", "-q", "-m", "feature work")
    _real_git(repo, "checkout", "-q", "main")
    _real_git(
        repo, "merge", "-q", "--no-ff", "-m", "Merge feature", "-m", "Work-Item: #10", "feature"
    )

    # A squash commit whose trailer block repeats `Work-Item:` -- every
    # named item lands (issue #304).
    _real_git(repo, "checkout", "-q", "-b", "squashed")
    (repo / "squash.txt").write_text("one\n")
    _real_git(repo, "add", "squash.txt")
    _real_git(repo, "commit", "-q", "-m", "squash step 1")
    (repo / "squash.txt").write_text("one\ntwo\n")
    _real_git(repo, "add", "squash.txt")
    _real_git(repo, "commit", "-q", "-m", "squash step 2")
    _real_git(repo, "checkout", "-q", "main")
    _real_git(repo, "merge", "-q", "--squash", "squashed")
    _real_git(repo, "commit", "-q", "-m", "Squash landing", "-m", "Work-Item: #11\nWork-Item: #12")

    # A commit landed through a real rebase, its trailer block preserved.
    _real_git(repo, "checkout", "-q", "-b", "docslane", "feature")
    (repo / "docs.txt").write_text("docs\n")
    _real_git(repo, "add", "docs.txt")
    _real_git(repo, "commit", "-q", "-m", "docs change", "-m", "No-Item: docs")
    _real_git(repo, "rebase", "-q", "main")
    _real_git(repo, "checkout", "-q", "main")
    _real_git(repo, "merge", "-q", "--ff-only", "docslane")

    # A `Work-Item:` line in prose, never its own trailer block -- not a
    # landing (issue #304 proof 2).
    (repo / "prose.txt").write_text("prose\n")
    _real_git(repo, "add", "prose.txt")
    _real_git(repo, "commit", "-q", "-m", "Prose change", "-m", "Explanation prose.\nWork-Item: #7")

    # A side branch that never joins the trunk's first-parent line
    # (issue #304 proof 3).
    _real_git(repo, "checkout", "-q", "-b", "sidebranch")
    (repo / "side.txt").write_text("side\n")
    _real_git(repo, "add", "side.txt")
    _real_git(repo, "commit", "-q", "-m", "side change", "-m", "Work-Item: #99")
    _real_git(repo, "checkout", "-q", "main")

    _push_repository_trunk(repo, "hub")
    return repo


def test_trunk_landings_classify_merge_squash_and_rebase_commits_from_their_trailer_block_alone(
    tmp_path: Path,
) -> None:
    """Issue #304 proofs 1-3, against a real `file://`-reachable remote with
    real merge, squash, and rebase history."""
    repo = _trunk_history_repository(tmp_path)
    trunk = "refs/remotes/hub/main"

    landings = checkout.trunk_landings(trunk, 20, directory=repo)

    assert [landing.classification for landing in landings] == [
        None,  # the plain initial commit
        board.TrunkWorkItemClassification((10,)),  # the merge commit
        board.TrunkWorkItemClassification((11, 12)),  # the squash commit
        board.NoItemClassification(board.NoItemKind.DOCS),  # the rebased commit
        None,  # `Work-Item:` in prose, not a trailer (proof 2)
    ]
    side_sha = _real_git(repo, "rev-parse", "sidebranch").stdout.strip()
    assert side_sha not in {landing.sha for landing in landings}  # proof 3

    # `depth` bounds the walk to the most recent commits, oldest of those first.
    walked = checkout.trunk_landings(trunk, 2, directory=repo)
    assert [landing.classification for landing in walked] == [
        board.NoItemClassification(board.NoItemKind.DOCS),
        None,
    ]


@pytest.mark.parametrize(
    ("url", "location"),
    [
        pytest.param(
            "git@github.com:owner/repo.git",
            checkout.RemoteLocation("github.com", "owner/repo"),
            id="ssh-scp",
        ),
        pytest.param(
            "ssh://git@github.com/owner/repo.git",
            checkout.RemoteLocation("github.com", "owner/repo"),
            id="ssh-url",
        ),
        pytest.param(
            "ssh://git@github.com:2222/owner/repo",
            checkout.RemoteLocation("github.com", "owner/repo"),
            id="ssh-url-with-port",
        ),
        pytest.param(
            "https://github.com/owner/repo.git",
            checkout.RemoteLocation("github.com", "owner/repo"),
            id="https",
        ),
        pytest.param(
            "https://github.com/owner/repo",
            checkout.RemoteLocation("github.com", "owner/repo"),
            id="https-no-suffix",
        ),
        pytest.param(
            "file:///srv/git/repo.git",
            checkout.RemoteLocation("file", "/srv/git/repo"),
            id="file",
        ),
        pytest.param(
            "file:///srv/git/repo",
            checkout.RemoteLocation("file", "/srv/git/repo"),
            id="file-no-suffix",
        ),
    ],
)
def test_parse_remote_location_normalizes_every_remote_shape(
    url: str, location: checkout.RemoteLocation
) -> None:
    assert checkout.parse_remote_location(url) == location


def test_parse_remote_location_refuses_an_unrecognized_shape() -> None:
    with pytest.raises(ClaimError, match="names no recognized host"):
        checkout.parse_remote_location("not-a-remote-url")


# `start`'s own worktree/branch naming and git plumbing (issue #322): a
# slug/prefix pure-function pair, plus the create/resume/remove/ancestry
# helpers `_cmd_start`/`release --merged`'s own cleanup share, each proven
# directly against real git rather than through the full CLI (that wiring
# lives in `tests/test_cli.py`).


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        pytest.param("  Fix the Login Bug!! ", "fix-the-login-bug", id="punctuation-collapses"),
        pytest.param("Add __init__.py", "add-init-py", id="underscores-and-dots-collapse"),
    ],
)
def test_slug_from_title_lowercases_and_collapses_punctuation(title: str, slug: str) -> None:
    assert checkout.slug_from_title(title) == slug


def test_slug_from_title_truncates_to_forty_characters_with_no_trailing_hyphen() -> None:
    title = "a" * 45 + " tail words that overflow the forty character limit"

    assert checkout.slug_from_title(title) == "a" * 40


def test_slug_from_title_refuses_when_nothing_survives() -> None:
    with pytest.raises(ClaimError, match="no usable slug"):
        checkout.slug_from_title("!!! ??? ...")


def test_validate_slug_accepts_a_value_matching_the_derived_shape() -> None:
    assert checkout.validate_slug("fresh-slug-42") == "fresh-slug-42"


@pytest.mark.parametrize(
    "slug",
    [
        pytest.param("Fresh-Slug", id="uppercase"),
        pytest.param("-fresh-slug", id="leading-hyphen"),
        pytest.param("fresh-slug-", id="trailing-hyphen"),
        pytest.param("fresh--slug", id="doubled-hyphen"),
        pytest.param("a" * 41, id="too-long"),
        pytest.param("", id="empty"),
    ],
)
def test_validate_slug_refuses_a_value_the_derived_rule_would_never_produce(slug: str) -> None:
    with pytest.raises(ClaimError, match="--slug must be"):
        checkout.validate_slug(slug)


@pytest.mark.parametrize(
    ("environ", "prefix"),
    [
        pytest.param(
            {"ACO_AGENT": "Claude head (coordinator)"}, "claude", id="aco-agent-first-word"
        ),
        pytest.param({"GROK_SESSION_ID": "sess-1"}, "grok", id="grok-session"),
        pytest.param({"CLAUDE_CODE_SESSION_ID": "sess-1"}, "claude", id="claude-session"),
        pytest.param(
            {"ACO_AGENT": "Grok", "CLAUDE_CODE_SESSION_ID": "sess-1"},
            "grok",
            id="aco-agent-wins-over-a-session-id",
        ),
    ],
)
def test_branch_prefix_for_identity_reads_the_same_precedence_as_resolved_agent(
    monkeypatch: pytest.MonkeyPatch, environ: dict[str, str], prefix: str
) -> None:
    _set_agent_identity_env(monkeypatch, environ)

    assert checkout.branch_prefix_for_identity() == prefix


@pytest.mark.parametrize(
    ("environ", "refusal"),
    [
        pytest.param({}, "branch prefix is required", id="no-identity-signal"),
        pytest.param(
            {"ACO_AGENT": " Ada "},
            "agent must be one bounded non-empty line",
            id="aco-agent-with-surrounding-space",
        ),
    ],
)
def test_branch_prefix_for_identity_refuses_an_identity_claim_would_refuse(
    monkeypatch: pytest.MonkeyPatch, environ: dict[str, str], refusal: str
) -> None:
    """START-03: `start` refuses before any worktree exists whatever the
    `claim` inside it would refuse, with the same sentence."""
    _set_agent_identity_env(monkeypatch, environ)

    with pytest.raises(ClaimError, match=refusal):
        checkout.branch_prefix_for_identity()


def test_refuse_unsafe_start_branch_names_the_identity_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ClaimError, match="agent identity '-bad' is not usable"):
        checkout.refuse_unsafe_start_branch("-bad/issue-1-widget", prefix="-bad")


def test_refuse_unsafe_start_branch_accepts_a_safe_branch() -> None:
    checkout.refuse_unsafe_start_branch("codex/issue-1-widget", prefix="codex")


def test_refuse_unsafe_start_branch_refuses_an_overlong_identity_prefix() -> None:
    """Issue #322 review/gate finding 1: a syntactically safe but overlong
    `ACO_AGENT` first word must still be refused before any git write,
    exactly as the claim machinery's own `BRANCH_NAME_MAX_LENGTH` bound on a
    stored claim marker refuses it -- one predicate, not a shape check that
    happens to cap length only by coincidence of an unrelated regex
    quantifier."""
    prefix = "a" * 300
    branch = f"{prefix}/issue-1-widget"

    with pytest.raises(ClaimError, match=f"agent identity {prefix!r} is not usable"):
        checkout.refuse_unsafe_start_branch(branch, prefix=prefix)


def test_branch_exists_reads_real_local_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _scratch_git_repository(tmp_path)
    _real_git(repository, "branch", "feature")
    monkeypatch.chdir(repository)

    assert checkout.branch_exists("feature") is True
    assert checkout.branch_exists("no-such-branch") is False


def _bare_remote_repository_with_one_commit(tmp_path: Path) -> Path:
    repo, _remote = _real_repository_with_bare_remote(tmp_path)
    (repo / "base.txt").write_text("base\n")
    _real_git(repo, "add", "base.txt")
    _real_git(repo, "commit", "-q", "-m", "initial")
    _push_repository_trunk(repo, "origin")
    return repo


def _build_start_worktree(worktree: Path, branch: str) -> None:
    checkout.create_linked_worktree(worktree, branch=branch, trunk="refs/remotes/origin/main")


def test_create_linked_worktree_builds_from_the_trunk_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    worktree = tmp_path / "repo-worktrees" / "issue-9-widget"
    monkeypatch.chdir(repo)

    _build_start_worktree(worktree, "codex/issue-9-widget")

    assert (worktree / "base.txt").read_text() == "base\n"
    assert _real_git(worktree, "branch", "--show-current").stdout.strip() == "codex/issue-9-widget"


def test_existing_start_worktree_is_absent_before_the_build_and_standing_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    worktree = tmp_path / "repo-worktrees" / "issue-9-widget"
    branch = "codex/issue-9-widget"
    monkeypatch.chdir(repo)

    before = checkout.existing_start_worktree(worktree, branch)
    _build_start_worktree(worktree, branch)
    after = checkout.existing_start_worktree(worktree, branch)

    assert (before, after, worktree.exists()) == (False, True, True)


def test_create_linked_worktree_builds_a_repository_nested_in_an_outer_working_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #448 review finding (START-01): an outer checkout's `.git` above
    the not-yet-created worktree path -- a home directory that is itself a
    git repository -- never turns git's "cannot change to" into a refusal."""
    _real_git(tmp_path, "init", "-q")
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    worktree = tmp_path / "repo-worktrees" / "issue-9-widget"
    branch = "codex/issue-9-widget"
    monkeypatch.chdir(repo)

    assert checkout.existing_start_worktree(worktree, branch) is False
    _build_start_worktree(worktree, branch)

    assert _real_git(worktree, "branch", "--show-current").stdout.strip() == branch


def test_existing_start_worktree_refuses_a_dirty_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    worktree = tmp_path / "repo-worktrees" / "issue-9-widget"
    branch = "codex/issue-9-widget"
    monkeypatch.chdir(repo)
    _build_start_worktree(worktree, branch)
    (worktree / "scratch.txt").write_text("uncommitted\n")

    with pytest.raises(ClaimError, match="is dirty"):
        checkout.existing_start_worktree(worktree, branch)


def test_existing_start_worktree_refuses_a_branch_taken_by_no_worktree_of_this_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    branch = "codex/issue-9-widget"
    _real_git(repo, "branch", branch)
    worktree = tmp_path / "repo-worktrees" / "issue-9-widget"
    monkeypatch.chdir(repo)

    with pytest.raises(ClaimError, match="already exists and is not this item's worktree"):
        checkout.existing_start_worktree(worktree, branch)


def test_existing_start_worktree_refuses_an_existing_non_worktree_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #322 review/gate finding: an ordinary directory already sitting
    at the target path -- empty or not -- is refused outright rather than
    left for `git worktree add` to adopt."""
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    branch = "codex/issue-9-widget"
    worktree = tmp_path / "repo-worktrees" / "issue-9-widget"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(repo)

    with pytest.raises(ClaimError, match=re.escape(checkout.NOT_A_WORKTREE_REFUSAL)):
        checkout.existing_start_worktree(worktree, branch)


def test_existing_start_worktree_refuses_a_worktree_on_a_different_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    worktree = tmp_path / "repo-worktrees" / "issue-9-widget"
    worktree.parent.mkdir(parents=True)
    _real_git(repo, "worktree", "add", "-q", str(worktree), "-b", "codex/issue-9-old-slug")
    monkeypatch.chdir(repo)

    with pytest.raises(ClaimError, match="exists on branch 'codex/issue-9-old-slug'"):
        checkout.existing_start_worktree(worktree, "codex/issue-9-widget")


def test_existing_start_worktree_refuses_a_worktree_from_a_different_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #322 review finding 2: a clean linked worktree on the exact
    same branch name, but belonging to an entirely different repository,
    must never be adopted as this item's own -- only its own repository's
    common git directory earns reuse."""
    caller = _scratch_git_repository(tmp_path)
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    _foreign_main, foreign_worktree = _repo_with_linked_worktree(foreign_root)
    monkeypatch.chdir(caller)

    with pytest.raises(ClaimError, match="belongs to a different repository"):
        checkout.existing_start_worktree(foreign_worktree, "codex/issue-1-widget")


def test_existing_start_worktree_refuses_a_path_that_is_not_a_checkout_root_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #322 review finding 2: a path resolving into the middle of some
    repository's own working tree -- rather than a checkout root by itself --
    must never be adopted as `start`'s own worktree, even when a branch of
    the same name happens to exist elsewhere in that repository."""
    caller = _scratch_git_repository(tmp_path)
    nested = caller / "nested"
    nested.mkdir()
    monkeypatch.chdir(caller)

    with pytest.raises(ClaimError, match="is not a checkout root by itself"):
        checkout.existing_start_worktree(nested, "codex/issue-1-widget")


def test_existing_start_worktree_refuses_the_repositorys_own_main_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #322 review finding 2: `path` resolving to this repository's own
    main checkout, not a linked worktree, must never be adopted -- only a
    linked worktree is ever safe to treat as one of `start`'s own lanes."""
    caller = _scratch_git_repository(tmp_path)
    monkeypatch.chdir(caller)

    with pytest.raises(
        ClaimError, match="is a repository's own main checkout, not a linked worktree"
    ):
        checkout.existing_start_worktree(caller, "codex/issue-1-widget")


def _conventional_checkout(tmp_path: Path) -> Path:
    return _scratch_git_repository(tmp_path)


def _checkout_whose_git_directory_names_it(tmp_path: Path) -> Path:
    """A submodule's layout: the git directory lives elsewhere and records
    its checkout as `core.worktree`."""
    (tmp_path / "modules").mkdir()
    repository = _scratch_git_repository(
        tmp_path, f"--separate-git-dir={tmp_path / 'modules' / 'repo'}"
    )
    _real_git(repository, "config", "core.worktree", "../../repo")
    return repository


def _linked_lane_of(main: Path, tmp_path: Path) -> Path:
    lane = tmp_path / "lane"
    _real_git(main, "worktree", "add", "-q", str(lane), "-b", "codex/issue-1-widget")
    return lane


def _git_directory_kept_elsewhere(tmp_path: Path) -> Path:
    """A `--separate-git-dir` repository with no `core.worktree`: its git
    directory records no way back to its main checkout."""
    return _scratch_git_repository(tmp_path, f"--separate-git-dir={tmp_path / 'store'}")


@pytest.mark.parametrize(
    ("build_checkout", "runs_in_linked_lane"),
    [
        pytest.param(_conventional_checkout, False, id="conventional-main"),
        pytest.param(_conventional_checkout, True, id="conventional-linked"),
        pytest.param(_checkout_whose_git_directory_names_it, False, id="core-worktree-main"),
        pytest.param(_checkout_whose_git_directory_names_it, True, id="core-worktree-linked"),
        pytest.param(_git_directory_kept_elsewhere, False, id="separate-git-dir-main"),
    ],
)
def test_main_checkout_root_is_the_main_checkout_from_any_of_its_worktrees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_checkout: Callable[[Path], Path],
    runs_in_linked_lane: bool,
) -> None:
    """Issue #479 (START-19): `start` builds beside the main checkout, never
    nested under a linked caller's, whatever layout its git directory has."""
    main = build_checkout(tmp_path)
    caller = _linked_lane_of(main, tmp_path) if runs_in_linked_lane else main
    monkeypatch.chdir(caller)

    assert checkout.main_checkout_root(toplevel=caller) == main.resolve()


def _linked_to_a_git_directory_kept_elsewhere(
    tmp_path: Path, _monkeypatch: pytest.MonkeyPatch
) -> Path:
    return _git_directory_kept_elsewhere(tmp_path)


def _core_worktree_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A conventional repository whose `core.worktree` read fails outright."""
    _stub_one_git_call(
        monkeypatch,
        ["config", "--get", "core.worktree"],
        exit_status=128,
        stderr="fatal: bad config line 1",
    )
    return _conventional_checkout(tmp_path)


@pytest.mark.parametrize(
    ("build_checkout", "refusal"),
    [
        pytest.param(
            _linked_to_a_git_directory_kept_elsewhere,
            r"main checkout unknown: git directory .* names no checkout; "
            r"run start from the main checkout",
            id="names-no-checkout",
        ),
        pytest.param(_core_worktree_unreadable, "fatal: bad config line 1", id="config-fails"),
    ],
)
def test_main_checkout_root_refuses_a_linked_worktree_it_cannot_trace_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_checkout: Callable[[Path, pytest.MonkeyPatch], Path],
    refusal: str,
) -> None:
    """Issue #479 (START-19, START-24): from a linked worktree whose git
    directory records no checkout, or whose `core.worktree` git cannot read,
    `start` refuses rather than build beside, or read the board
    configuration of, the linked worktree itself."""
    lane = _linked_lane_of(build_checkout(tmp_path, monkeypatch), tmp_path)
    monkeypatch.chdir(lane)

    with pytest.raises(ClaimError, match=refusal):
        checkout.main_checkout_root(toplevel=lane)


def test_worktree_on_branch_finds_the_one_matching_path(tmp_path: Path) -> None:
    main, worktree = _repo_with_linked_worktree(tmp_path)

    assert checkout.worktree_on_branch((main, worktree), "codex/issue-1-widget") == worktree
    assert checkout.worktree_on_branch((main,), "codex/issue-1-widget") is None


def test_worktree_on_branch_surfaces_a_git_failure_resolving_a_registered_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #322 review/gate finding: `paths` names worktrees git's own
    registry already vouches for, so a failure resolving one of them (a
    moved or deleted directory, most often) must surface as a refusal --
    never a silently skipped "not on this branch"."""
    main, worktree = _repo_with_linked_worktree(tmp_path)
    monkeypatch.chdir(main)
    _stub_one_git_call(
        monkeypatch,
        [
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-dir",
            "--git-common-dir",
        ],
        exit_status=128,
        stderr="fatal: cannot change to 'gone': No such file or directory",
    )

    with pytest.raises(ClaimError, match="cannot change to"):
        checkout.worktree_on_branch((worktree,), "codex/issue-1-widget")


def test_resolve_path_checkout_fails_loud_on_a_malformed_rev_parse_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #322 review/gate finding: a successful `git rev-parse` exit
    whose combined toplevel/git-dir/git-common-dir reply does not split into
    exactly three lines is a real git misbehavior -- `worktree_on_branch`'s
    own callers must see it as a refusal, never as a silently swallowed
    `None`."""
    repository = _scratch_git_repository(tmp_path)
    monkeypatch.chdir(repository)
    _stub_one_git_call(
        monkeypatch,
        [
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-dir",
            "--git-common-dir",
        ],
        exit_status=0,
        stderr="",
    )

    with pytest.raises(ClaimError, match="git returned a malformed checkout description"):
        checkout.worktree_on_branch((repository,), "main")


def test_remove_linked_worktree_deletes_the_directory_and_the_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, worktree = _repo_with_linked_worktree(tmp_path)
    monkeypatch.chdir(main)

    outcome = checkout.remove_linked_worktree(worktree, branch="codex/issue-1-widget")

    assert not worktree.exists()
    assert checkout.branch_exists("codex/issue-1-widget") is False
    assert outcome.worktree.removed is True
    assert outcome.branch.removed is True


def test_branch_merged_into_default_is_true_only_after_a_real_merge(tmp_path: Path) -> None:
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    _real_git(repo, "checkout", "-q", "-b", "feature")
    (repo / "feature.txt").write_text("feature\n")
    _real_git(repo, "add", "feature.txt")
    _real_git(repo, "commit", "-q", "-m", "feature work")
    _real_git(repo, "checkout", "-q", "main")
    trunk = "refs/remotes/origin/main"
    before = checkout.branch_merged_into_default("feature", trunk=trunk, directory=repo)

    _real_git(repo, "merge", "-q", "--no-ff", "-m", "Merge feature", "feature")
    _push_repository_trunk(repo, "origin")
    after = checkout.branch_merged_into_default("feature", trunk=trunk, directory=repo)

    assert (before, after) == (False, True)


def test_branch_exists_fails_loud_on_an_unexpected_git_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _scratch_git_repository(tmp_path)
    monkeypatch.chdir(repository)
    _stub_one_git_call(
        monkeypatch,
        ["show-ref", "--verify", "--quiet", "refs/heads/x"],
        exit_status=128,
        stderr="fatal: bad object refs/heads/x",
    )

    with pytest.raises(ClaimError, match="fatal: bad object"):
        checkout.branch_exists("x")


def test_create_linked_worktree_fails_loud_when_worktree_add_itself_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    monkeypatch.chdir(repo)
    worktree = tmp_path / "repo-worktrees" / "issue-9-widget"
    branch, trunk = "codex/issue-9-widget", "refs/remotes/origin/main"
    _stub_one_git_call(
        monkeypatch,
        ["worktree", "add", str(worktree), "-b", branch, trunk],
        exit_status=128,
        stderr="fatal: already exists",
    )

    with pytest.raises(ClaimError, match="fatal: already exists"):
        checkout.create_linked_worktree(worktree, branch=branch, trunk=trunk)


def test_remove_linked_worktree_fails_loud_when_worktree_remove_itself_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, worktree = _repo_with_linked_worktree(tmp_path)
    monkeypatch.chdir(main)
    _stub_one_git_call(
        monkeypatch,
        ["worktree", "remove", str(worktree)],
        exit_status=1,
        stderr="fatal: contains modified or untracked files",
    )

    with pytest.raises(ClaimError, match="contains modified or untracked files"):
        checkout.remove_linked_worktree(worktree, branch="codex/issue-1-widget")


def test_remove_linked_worktree_reports_the_worktree_removed_and_the_branch_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #322 review/gate finding 4: a branch-deletion failure after the
    worktree is already gone must never read as a bare `kept` -- the typed
    outcome names both halves apart."""
    main, worktree = _repo_with_linked_worktree(tmp_path)
    monkeypatch.chdir(main)
    _stub_one_git_call(
        monkeypatch,
        ["branch", "-d", "codex/issue-1-widget"],
        exit_status=1,
        stderr="error: branch not fully merged",
    )

    outcome = checkout.remove_linked_worktree(worktree, branch="codex/issue-1-widget")

    assert not worktree.exists()
    assert outcome.worktree.removed is True
    assert outcome.branch.removed is False
    assert outcome.branch.reason is not None
    assert "not fully merged" in outcome.branch.reason


def test_branch_merged_into_default_fails_loud_on_an_unexpected_merge_base_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    trunk = "refs/remotes/origin/main"
    _stub_one_git_call(
        monkeypatch,
        ["merge-base", "--is-ancestor", "no-such-branch", "refs/remotes/origin/main"],
        exit_status=128,
        stderr="fatal: not a valid object name no-such-branch",
    )

    with pytest.raises(ClaimError, match="not a valid object name"):
        checkout.branch_merged_into_default("no-such-branch", trunk=trunk, directory=repo)
