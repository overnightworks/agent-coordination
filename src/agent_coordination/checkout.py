"""Local git checkout validation and agent identity."""

from __future__ import annotations

import functools
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from . import board, process
from .protocol import (
    ClaimError,
    ClaimRequest,
    InvalidClaimMarkerError,
    _outbound_text,
    is_safe_branch_name,
    named_with_overflow_count,
    valid_scope,
)

ACO_AGENT_ENV = "ACO_AGENT"
GROK_SESSION_ID_ENV = "GROK_SESSION_ID"
CLAUDE_CODE_SESSION_ID_ENV = "CLAUDE_CODE_SESSION_ID"
# `session_agent`'s own order, for the sentences built from it: `claim`'s and
# `start`'s identity refusals, `protect`'s PROT-08 denial, and `--agent`'s help.
IDENTITY_ENVIRONMENT_ORDER = (
    f"{ACO_AGENT_ENV}, {GROK_SESSION_ID_ENV}, or {CLAUDE_CODE_SESSION_ID_ENV}"
)


# One owner for every git-subprocess failure sentence: `_git_run` launches
# every `git` subprocess this module runs and translates the same three
# launch-failure shapes -- a missing executable, a timeout, and any other
# OS-level launch failure -- to the same `ClaimError` text (issue #315 Sonar
# S1192; issue #314 gate G's follow-up folds `versioned_paths` and
# `path_is_tracked` into this one owner too, F1). `process.run_git` and
# `process.git_failure_detail` (issue #372) own the argv shape and the
# fallback reading `store.py` needs too; the `ClaimError` translation stays
# here since `checkout`/`store` share one import-linter layer. `_git_output`,
# `versioned_paths`, and `path_is_tracked` each interpret a successful
# launch's exit status their own way.
_GIT_MISSING_EXECUTABLE_ERROR = "git is required for issue claims"
_GIT_TIMED_OUT_ERROR = "git timed out while validating the build checkout"


def _git_run(arguments: list[str], *, directory: Path | None = None) -> process.CapturedResult:
    """Launch `git arguments`, in `directory` when given via `-C` (issue
    #314) -- every caller that must judge a specific checkout rather than
    the calling process's own cwd names `directory` explicitly, so the
    checkout a security decision reads is never an accident of where the
    process happens to run.

    Every OS-level launch failure -- a missing executable, a timeout, or
    anything else (permission denied, out of file descriptors, `-C` naming a
    non-directory, ...) -- fails closed as a `ClaimError`, never an
    uncaught traceback out of `protect`'s hook boundary. Interpreting a
    successful launch's exit status is each caller's own job.
    """
    try:
        return process.run_git(arguments, directory=directory)
    except process.ExecutableMissingError as error:
        raise ClaimError(_GIT_MISSING_EXECUTABLE_ERROR) from error
    except process.ProcessTimedOutError as error:
        raise ClaimError(_GIT_TIMED_OUT_ERROR) from error
    except OSError as error:
        raise ClaimError(f"git failed to launch: {error}") from error


def _git_output(arguments: list[str], *, directory: Path | None = None) -> str:
    """`git arguments`'s stdout, in `directory` when given via `-C` (issue
    #314) or the calling process's own cwd otherwise; a nonzero exit fails
    closed."""
    result = _git_run(arguments, directory=directory)
    if result.exit_status != 0:
        raise ClaimError(process.git_failure_detail(result))
    # Trailing-only: every caller wants the one newline `git` appends after its
    # output trimmed, but `git status --porcelain`'s short format is
    # significant in its *leading* column (` M path` names a modified file by
    # a leading space before the path) -- a leading strip silently turned that
    # into `M path` and `_dirty_paths` then sliced into the filename itself.
    return result.stdout.decode().rstrip("\n")


def current_branch(*, directory: Path | None = None) -> str:
    """The branch checked out in `directory` (or the calling process's own
    cwd), empty on a detached HEAD."""
    return _git_output(["branch", "--show-current"], directory=directory)


# `git rev-parse --verify --quiet <ref>` (git(1)): exit 1 is the one
# documented "does not resolve to a single object" outcome under `--quiet`
# -- the same single-defined-exit contract `path_is_tracked` already reads
# from `ls-files --error-unmatch` above. Any other nonzero exit (128 for a
# broken checkout, a corrupted ref, ...) is a real git failure, not an
# absent ref, and must fail loud with its own detail instead of being read
# the same way `_lane_tip` used to (issue #390 finding 9b).
_REV_PARSE_VERIFY_EXIT_UNRESOLVED = 1


def resolved_commit(ref: str) -> str | None:
    """`ref`'s current commit, or `None` when it does not resolve to a
    single object. A git failure that keeps the read from answering either
    way -- a missing executable, a timeout, or any exit but the documented
    "unresolved" one -- raises `ClaimError` with git's own detail instead."""
    result = _git_run(["rev-parse", "--verify", "--quiet", ref])
    if result.exit_status == _REV_PARSE_VERIFY_EXIT_UNRESOLVED:
        return None
    if result.exit_status != 0:
        raise ClaimError(process.git_failure_detail(result))
    return result.stdout.decode().rstrip("\n")


def lane_changed_paths(tip: str, *, trunk: str, directory: Path) -> tuple[str, ...]:
    """The paths `tip` changes since its merge base with `trunk` (issue
    #468), read in `directory`: the lane's own change only. A diff from the
    claim's base would also list every path a trunk pull brought in from
    other lanes."""
    diff = _git_output(["diff", "--name-only", f"{trunk}...{tip}"], directory=directory)
    return tuple(diff.splitlines())


def remote_url(remote: str, *, directory: Path | None = None) -> str:
    """One named remote's URL, read from `directory` via `-C` when given
    (issue #457: a `RunContext` for another checkout) or the calling
    process's own cwd otherwise."""
    return _git_output(["config", "--get", f"remote.{remote}.url"], directory=directory)


@dataclass(frozen=True)
class RemoteLocation:
    """A git remote URL's host and repository path, independent of any forge
    adapter's own URL syntax (issue #245).

    The one owner comparing a forge target against the checkout's canonical
    remote (Erwartung 6, issue #176 §2): a GitHub adapter target and a
    `RemoteLocation` agree exactly when their `host` and `path` do, whether
    the canonical remote is GitHub, another forge host entirely, or a local
    `file://` path.
    """

    host: str
    path: str


_GIT_SUFFIX = ".git"
# A scheme-form remote: `ssh://[user@]host[:port]/path`, `https://host/path`,
# `file:///path` -- the one shape every non-scp remote URL shares.
_SCHEME_REMOTE_PATTERN = re.compile(
    r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://(?:[^@/]*@)?(?P<rest>.+)$"
)
# The scp-like shorthand `ssh` alone accepts: `[user@]host:path`, no scheme.
_SCP_REMOTE_PATTERN = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>.+)$")


def _without_git_suffix(path: str) -> str:
    # Trailing only: a `file://` path's leading `/` is significant (it is
    # what makes the path absolute), while a trailing one is never part of a
    # repository's name.
    return path.rstrip("/").removesuffix(_GIT_SUFFIX)


def parse_remote_location(url: str) -> RemoteLocation:
    """`url`'s host and repository path (issue #245): every remote shape
    `aco` accepts -- SSH scp-like (`git@host:o/r.git`), SSH URL
    (`ssh://git@host/o/r`), HTTPS (`https://host/o/r(.git)`), and
    `file:///...` (host `"file"`, its filesystem path) -- normalizes to one
    shape here, so no caller keeps its own copy of this parsing.
    """
    scheme_match = _SCHEME_REMOTE_PATTERN.match(url)
    if scheme_match is not None:
        scheme = scheme_match.group("scheme").lower()
        rest = scheme_match.group("rest")
        if scheme == "file":
            return RemoteLocation("file", _without_git_suffix(rest))
        host, _, path = rest.partition("/")
        host = host.partition(":")[0]  # drop an explicit port, e.g. `host:2222`
        return RemoteLocation(host, _without_git_suffix(path))
    scp_match = _SCP_REMOTE_PATTERN.match(url)
    if scp_match is not None:
        return RemoteLocation(scp_match.group("host"), _without_git_suffix(scp_match.group("path")))
    raise ClaimError(f"remote url {url!r} names no recognized host")


def versioned_paths(
    *, directory: Path | None = None, revision: str | None = None
) -> tuple[str, ...]:
    """Every versioned path git tracks, from `directory` via `-C` when given
    (issue #314: `rescope`'s own resolved checkout, never the calling
    process's cwd) or the process's own checkout otherwise (`claim`'s own
    precondition, unaffected by #314) -- or, given `revision`, every path
    that commit's tree holds (issue #479: `start` measures a scope against
    the trunk it has not checked out yet)."""
    listing = (
        ["ls-files", "-z", "--full-name"]
        if revision is None
        else ["ls-tree", "-r", "-z", "--full-tree", "--name-only", revision]
    )
    result = _git_run(listing, directory=directory)
    if result.exit_status != 0:
        raise ClaimError(process.git_failure_detail(result))
    return tuple(dict.fromkeys(path for path in result.stdout.decode().split("\0") if path))


def path_is_tracked(path: str, *, directory: Path | None = None) -> bool:
    """Whether `path` (repo-relative, forward slashes) is tracked in git's
    index right now, read from `directory` via `-C` when given (issue #314:
    `session.board_config`'s own resolved checkout, never the calling process's
    cwd) or the process's own checkout otherwise (issue #315) -- absent,
    untracked, and ignored all read as `False`, since
    `git ls-files --error-unmatch` exits 1, and only 1, for a path it does
    not track. A dedicated call, not `path in versioned_paths()`: that
    listing's exact membership and count are a different concern
    (scope-width math over every tracked file), so a test fixing one axis
    never has to carry the other."""
    return _git_yes_or_no(["ls-files", "--error-unmatch", "--", path], directory=directory)


def path_is_ignored(path: str, *, directory: Path) -> bool:
    """Whether git's own exclude rules (`.gitignore`, `.git/info/exclude`,
    the global excludes file) ignore `path` (repo-relative) in the checkout
    at `directory` -- for a path that need not exist yet, and never for a
    tracked one, which no exclude rule can ignore (issue #448: `protect`'s
    escape for a session's own ignored `.claude/` settings)."""
    return _git_yes_or_no(["check-ignore", "--quiet", "--", path], directory=directory)


def _git_yes_or_no(arguments: list[str], *, directory: Path | None) -> bool:
    """A git question answered by exit status alone: `0` yes, `1` no -- the
    one "no" both `ls-files --error-unmatch` and `check-ignore` define. Any
    other nonzero exit (e.g. 128 outside a git repository) is a real git
    failure, matching `versioned_paths`'s handling in this module -- it must
    never read as a plain "no" instead of a git error."""
    result = _git_run(arguments, directory=directory)
    if result.exit_status == 0:
        return True
    if result.exit_status == 1:
        return False
    raise ClaimError(process.git_failure_detail(result))


def paths_under_scope(paths: tuple[str, ...], scope: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            path
            for path in paths
            if any(path == entry or path.startswith(f"{entry}/") for entry in scope)
        )
    )


def _scope_directories(
    paths: tuple[str, ...],
    *,
    directory: Path | None,
    toplevel: Callable[[], Path],
    revision: str | None = None,
) -> tuple[str, ...]:
    """Return the scope entries that name a git tree or on-disk directory,
    read from `directory` via `-C` when given (issue #314 gate B4:
    `rescope`'s own resolved checkout, never the calling process's cwd) or
    the process's own checkout otherwise (`claim`'s own precondition,
    unaffected by #314). `toplevel` is the caller's own held toplevel
    (issue #472: its run context's, never a second git read), asked once
    and only for an entry that is no git tree. Given `revision`, only that
    commit's own trees count (issue #479): it has no checkout on disk yet."""
    directories: list[str] = []
    checkout_root = functools.cache(lambda: _toplevel_or_none(toplevel))
    for path in paths:
        try:
            kind = _git_output(
                ["cat-file", "-t", f"{revision or 'HEAD'}:{path}"], directory=directory
            )
        except ClaimError:
            kind = ""
        if kind == "tree":
            directories.append(path)
            continue
        if revision is not None:
            continue
        root = checkout_root()
        if root is not None and (root / path).is_dir():
            directories.append(path)
    return tuple(directories)


def _toplevel_or_none(toplevel: Callable[[], Path]) -> Path | None:
    """`toplevel()`, or `None` when it fails: the width gate then counts no
    untracked directory, and the command's own toplevel refusal, which runs
    after it, reports the failure (issue #472)."""
    try:
        return toplevel()
    except ClaimError:
        return None


ISOLATED_WORKTREE_RECIPE = (
    "git worktree add ../<repo>-worktrees/issue-<n>-<slug> -b <agent>/issue-<n>-<slug>"
)

# One owner for both worktree-isolation refusal sentences (issue #314 gate
# B6, Sonar S1192): `claim`'s own cwd-based precondition
# (`_validate_worktree_branch`) and `rescope`'s path-resolved one
# (`_refuse_shared_checkout`) raise the identical two sentences, so each is
# spelled once here instead of twice across the two functions.
ISOLATED_NON_MAIN_BRANCH_REFUSAL = "build claims require an isolated non-main worktree branch; "
LINKED_ISOLATED_WORKTREE_REFUSAL = "build claims require a linked isolated worktree checkout; "


class WorktreeRepair(StrEnum):
    """Which repair a worktree-isolation refusal should name.

    `claim` has no worktree yet, so it needs one built (`CREATE`, the `git
    worktree add` recipe). `rescope` and `release` act on a claim that was
    taken from a worktree that therefore already exists, so naming the same
    create recipe sends an agent to build a second, foreign one -- exactly
    the worktree the state then sees as an unrelated lane. Their honest
    repair is `RETURN_TO_CLAIM`: go back to the worktree this claim already
    has.
    """

    CREATE = "create"
    RETURN_TO_CLAIM = "return_to_claim"


def _worktree_repair_instruction(repair: WorktreeRepair, *, branch: str | None) -> str:
    """The actionable clause a worktree-isolation refusal ends with.

    `branch` is the checkout's own already-known branch, never one looked up
    for the occasion: at the trunk-branch check the checkout is on `main` or
    `master`, so no other branch is available to name without guessing, and
    `None` says so; at the shared-checkout check the checkout's branch is
    already known (it is `branch` below), so `RETURN_TO_CLAIM` names it.
    """
    if repair is WorktreeRepair.CREATE:
        return f"run {ISOLATED_WORKTREE_RECIPE}"
    if branch is None:
        return "run this command from this claim's own worktree, not the primary checkout"
    return (
        f"run this command from this claim's own worktree on {branch!r}, not the primary checkout"
    )


def _validate_worktree_branch(
    branch: str,
    *,
    default_branch: str | None,
    repair: WorktreeRepair = WorktreeRepair.CREATE,
    directory: Path | None = None,
) -> None:
    """Require an isolated non-main worktree checked out on `branch`, read
    from `directory` via `-C` when given (issue #322: `start`'s own resolved
    worktree, never a process-wide `os.chdir`) or the calling process's own
    cwd otherwise -- `claim`'s own precondition, since a fresh claim is
    created by literally standing in the worktree it claims. `default_branch`
    is the canonical remote's recorded default branch, or `None` when none
    is recorded (issue #490). `rescope` no longer shares this (issue #314):
    it judges an already-resolved `PathCheckout` instead, via
    `_refuse_shared_checkout` below, so a rescope invoked from a foreign cwd
    is not silently judged by the wrong checkout.
    """
    if is_default_branch(branch, default_branch):
        raise ClaimError(
            f"{ISOLATED_NON_MAIN_BRANCH_REFUSAL}{_worktree_repair_instruction(repair, branch=None)}"
        )
    current = current_branch(directory=directory)
    git_directory = Path(_git_output(["rev-parse", "--git-dir"], directory=directory)).resolve()
    common_directory = Path(
        _git_output(["rev-parse", "--git-common-dir"], directory=directory)
    ).resolve()
    if current != branch:
        raise ClaimError(f"claim branch {branch!r} does not match checkout branch {current!r}")
    if git_directory == common_directory:
        raise ClaimError(
            f"{LINKED_ISOLATED_WORKTREE_REFUSAL}"
            f"{_worktree_repair_instruction(repair, branch=branch)}"
        )


class CheckoutKind(StrEnum):
    """Whether a resolved checkout is the shared main checkout or a linked,
    isolated worktree (issue #314) -- the one structural fact `protect` and
    `rescope` judge a write or a rescope by, read from the checkout itself
    rather than from a branch name."""

    MAIN = "main"
    LINKED_WORKTREE = "linked_worktree"


@dataclass(frozen=True)
class PathCheckout:
    """The git checkout that owns a directory, resolved directly from that
    directory (issue #314) -- never from the calling process's own cwd, so
    the same directory yields the same checkout regardless of where the
    process runs. `protect` resolves this from a hook payload path's own
    parent directory; `rescope` resolves it from the first absolute path it
    is given, falling back to its own process cwd when none is (its one
    other legitimate location signal). `common_directory` is the one fact
    shared by every worktree of the same repository -- the key a caller
    fetching store state once per repository, not once per worktree, caches
    on (issue #314 gate G5). `has_commit` is `False` for an unborn branch
    (a symbolic `HEAD` naming a branch with no commit yet): a resolved
    checkout with no commit must never itself authorize a write (issue #314
    gate G3), since its branch name can coincidentally match a still-live
    claim's."""

    toplevel: Path
    branch: str
    kind: CheckoutKind
    common_directory: Path
    has_commit: bool


NO_COMMIT_CHECKOUT_REASON = "no commit on this branch"
NOT_IN_A_REPOSITORY_REASON = "not in a repository"
RELATIVE_PAYLOAD_PATH_DENIAL = "relative payload path"


def relative_scope_entry(absolute_path: str, *, toplevel: Path) -> str | None:
    """`absolute_path` (already an absolute filesystem path -- a hook
    payload path, or a `rescope --add`/`--drop` entry given that way) as a
    canonical, repository-relative scope entry under `toplevel`, or `None`
    when it resolves outside `toplevel` or is otherwise not a valid scope
    entry. Shared by `protect.judge` (issue #314) and `cli`'s own
    `rescope` absolute-path handling (issue #314 delta, finding R1)."""
    try:
        relative = Path(absolute_path).resolve().relative_to(toplevel).as_posix()
        return valid_scope([relative])[0]
    except (InvalidClaimMarkerError, OSError, ValueError):
        return None


def resolve_path_checkout(directory: Path) -> PathCheckout | None:
    """The checkout owning `directory`, or `None` when `directory` sits
    outside every git repository ("not in a repository", issue #314).

    Every git read runs `git -C directory`, so the result is the same
    regardless of the calling process's own cwd -- unlike the ad hoc,
    cwd-implicit `_git_output` calls this replaces in `protect` and
    `rescope`, which silently read the *process's* checkout instead of the
    one the caller actually means. `--path-format=absolute` makes the
    toplevel/git-dir/common-dir comparison below meaningful: git's default,
    relative-to-`-C`-directory paths would otherwise have to be re-resolved
    against `directory` itself, not the caller's own cwd.

    A git failure on an existing `directory` is "outside every repository"
    only when no repository marker sits in it or any of its ancestors
    either; below one, the failure is raised instead (issue #448 review
    finding: `protect` allows a `None` path unjudged, so a missing git must
    never read as "no repository here"). Inside a git directory itself -- a
    checkout's `.git/` or a bare repository -- the failure is raised as
    `inside_git_directory_reason`'s sentence rather than git's own
    localized text (issue #483). A `directory` that does not exist yet is
    never inside a repository -- `git -C` cannot even enter it -- so it
    stays `None` whatever sits above it, even an outer checkout's `.git`.
    """
    try:
        return _resolve_checkout(directory)
    except ClaimError as error:
        if directory.is_dir():
            _refuse_a_failure_inside_a_repository(directory, error)
        return None


def _refuse_a_failure_inside_a_repository(directory: Path, error: ClaimError) -> None:
    """Raises `error` -- or, inside a git directory itself, its own sentence
    -- when `directory` sits inside a repository, so the git failure on it
    can never read as "outside every repository"."""
    git_directory = _enclosing_git_directory(directory)
    if git_directory is not None:
        raise ClaimError(inside_git_directory_reason(git_directory)) from error
    if _has_repository_marker_above(directory):
        raise error


def inside_git_directory_reason(git_directory: Path) -> str:
    return f"not a checkout: {git_directory} is a git directory"


def resolve_named_path_checkout(path: Path) -> PathCheckout | None:
    """The checkout an absolute path a caller names belongs to -- the one
    resolver `protect` and `rescope` share for a payload path or an
    `--add`/`--drop` entry (issue #483). A directory that is itself a
    checkout root resolves as that checkout (PROT-36): a nested checkout's
    own root, whose parent sits inside an outer repository, must not have
    the outer checkout answer for it, and a root whose parent sits outside
    every repository is still judged as that checkout's own root (PROT-14).
    Any other directory resolves from its parent first and from itself only
    as a fallback; a file -- existing or not yet written -- from its
    nearest existing ancestor directory (PROT-39). `path` is normalized
    lexically first (`os.path.normpath`, no symlink resolution), so
    `nested/../nested` or `nested/.` reach the comparison the way `nested`
    does."""
    path = Path(os.path.normpath(path))
    if not path.is_dir():
        return resolve_nearest_existing_checkout(path.parent)
    self_checkout = resolve_path_checkout(path)
    if self_checkout is not None and self_checkout.toplevel == path:
        return self_checkout
    return resolve_path_checkout(path.parent) or self_checkout


def unscopable_path_reason(absolute_path: str, *, toplevel: Path) -> str | None:
    """Why `absolute_path`, inside the checkout at `toplevel`, can never be a
    scope entry, or `None` when it can -- the one sentence `protect` denies
    with and `rescope` refuses with (issue #483). Below a file or a
    dangling symlink the path can never exist; the checkout root itself is
    the whole checkout, not a path in it."""
    path = Path(os.path.normpath(absolute_path))
    directory = _nearest_existing_directory(path.parent)
    if directory != path.parent:
        blocker = directory / path.parent.relative_to(directory).parts[0]
        if os.path.lexists(blocker):
            kind = "file" if blocker.exists() else "dangling symlink"
            return f"{path} cannot exist: {blocker} is a {kind}"
    if path.resolve() == toplevel:
        return f"{path} is the checkout root itself"
    return None


def resolve_nearest_existing_checkout(directory: Path) -> PathCheckout | None:
    """The checkout owning `directory`, judged from its nearest existing
    ancestor when it does not exist yet (PROT-39): a file about to be
    written may sit in directories not created yet either, and resolving a
    missing directory would read as "outside every repository". The one
    resolver `protect` and `rescope` share for a path's directory (issue
    #474: `rescope --add` of a file in a new directory refused `not in a
    repository` while `protect` judged the same path by its checkout).
    `directory` is normalized lexically first, as `protect` normalizes its
    payload: a `..` through a directory not created yet must not walk up to
    an ancestor the normalized path has already left (issue #474 drive
    finding)."""
    return resolve_path_checkout(_nearest_existing_directory(Path(os.path.normpath(directory))))


def _nearest_existing_directory(directory: Path) -> Path:
    """`directory` itself when it exists, else its closest existing ancestor
    -- the filesystem root at the latest, which always exists."""
    while not directory.is_dir():
        directory = directory.parent
    return directory


def _enclosing_git_directory(directory: Path) -> Path | None:
    """`directory` or its closest ancestor that is a git directory itself --
    a bare repository, or a checkout's own `.git/` -- or `None`. Judged on
    the symlink-resolved path, the one git's own discovery walks."""
    resolved = directory.resolve()
    return next(
        (candidate for candidate in (resolved, *resolved.parents) if _is_git_directory(candidate)),
        None,
    )


def _has_repository_marker_above(directory: Path) -> bool:
    """Whether `directory` or any ancestor holds a `.git` marker: a `.git`
    file (a linked worktree's) or a `.git` git directory (a main
    checkout's) -- a stray empty `.git` directory is no repository to git
    either. Judged on the symlink-resolved path, the one git's own
    discovery walks."""
    resolved = directory.resolve()
    return any(
        _is_repository_marker(candidate / ".git") for candidate in (resolved, *resolved.parents)
    )


def _is_repository_marker(dot_git: Path) -> bool:
    return dot_git.is_file() or _is_git_directory(dot_git)


def _is_git_directory(candidate: Path) -> bool:
    """The layout git's own discovery takes for a repository directory:
    `HEAD` beside `objects/` and `refs/`."""
    return (
        (candidate / "HEAD").is_file()
        and (candidate / "objects").is_dir()
        and (candidate / "refs").is_dir()
    )


def _resolve_checkout(directory: Path) -> PathCheckout:
    """`resolve_path_checkout`'s own git reads, without swallowing a failure
    into `None` (issue #322 review/gate finding: `worktree_on_branch` below
    resolves paths git's own worktree registry already vouches for as
    worktrees of this repository, so any failure resolving one of them is
    real -- a moved or deleted worktree directory, most often -- never a
    legitimate "nothing here"; `resolve_path_checkout` is the one caller
    that still wants that bare `None` reading)."""
    combined = _git_output(
        [
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-dir",
            "--git-common-dir",
        ],
        directory=directory,
    )
    try:
        toplevel, git_directory, common_directory = combined.splitlines()
    except ValueError as error:
        raise ClaimError(f"git returned a malformed checkout description: {combined!r}") from error
    branch = current_branch(directory=directory)
    kind = CheckoutKind.MAIN if git_directory == common_directory else CheckoutKind.LINKED_WORKTREE
    try:
        _git_output(["rev-parse", "--verify", "HEAD"], directory=directory)
        has_commit = True
    except ClaimError:
        has_commit = False
    return PathCheckout(
        toplevel=Path(toplevel),
        branch=branch,
        kind=kind,
        common_directory=Path(common_directory),
        has_commit=has_commit,
    )


def _refuse_shared_checkout(
    path_checkout: PathCheckout, *, default_branch: str | None, repair: WorktreeRepair
) -> None:
    """`rescope`'s own worktree-isolation refusal (issue #314): the same
    invariant `_validate_worktree_branch` enforces for `claim`, judged from
    an already path-resolved checkout and `default_branch`, the canonical
    remote's recorded default branch in that checkout (issue #490), instead
    of a fresh git read in the calling process's own cwd (gate G4).

    Unlike `claim`'s own `is_default_branch`, which falls back to guessing
    `{main, master}` when no default branch is recorded, this denies
    outright: `rescope` judges an attacker-reachable payload location, so a
    repository whose default branch is `trunk`, read from a checkout with no
    recorded `HEAD` yet, must never slip through unnoticed as "not the
    default branch".
    """
    if default_branch is None:
        raise ClaimError(DEFAULT_BRANCH_UNKNOWN_REASON)
    if path_checkout.branch == default_branch:
        raise ClaimError(
            f"{ISOLATED_NON_MAIN_BRANCH_REFUSAL}{_worktree_repair_instruction(repair, branch=None)}"
        )
    if path_checkout.kind is CheckoutKind.MAIN:
        raise ClaimError(
            f"{LINKED_ISOLATED_WORKTREE_REFUSAL}"
            f"{_worktree_repair_instruction(repair, branch=path_checkout.branch)}"
        )


def _dirty_paths(status: str) -> tuple[str, ...]:
    """The changed paths named by `git status --porcelain`'s short format:
    each line is two status characters, a space, then the path (or, for a
    rename, `old -> new`), so dropping the first three characters leaves the
    path a dirty-tree refusal names."""
    return tuple(line[3:] for line in status.splitlines() if line)


class CheckoutBaseMismatchError(ClaimError):
    """The checkout stands on another commit than the claim's base (CLM-04):
    its own type so `start`, whose base is the trunk it checked, can name a
    trunk that moved after its checks in its own sentence (START-26)."""


def _validate_checkout(
    request: ClaimRequest,
    *,
    default_branch: Callable[[], str | None],
    directory: Path | None = None,
) -> None:
    """`claim`'s own preconditions against `directory` via `-C` when given
    (issue #322: `start`'s own resolved worktree, never a process-wide
    `os.chdir`) or the calling process's own cwd otherwise, judging the
    recorded default branch `default_branch` answers as
    `_validate_worktree_branch` does -- asked only once the base matches,
    so a refusal before it reads no configuration (issue #490)."""
    head = _git_output(["rev-parse", "HEAD"], directory=directory)
    if head != request.base:
        raise CheckoutBaseMismatchError(
            f"claim base {request.base} does not match checkout HEAD {head}; "
            "omit --base to use checkout HEAD"
        )
    _validate_worktree_branch(request.branch, default_branch=default_branch(), directory=directory)
    dirty = _git_output(["status", "--porcelain"], directory=directory)
    if dirty:
        named = named_with_overflow_count(_dirty_paths(dirty))
        raise ClaimError(f"claim must be acquired before the first worktree edit: {named}")


DEFAULT_BRANCH_FALLBACK = frozenset({"main", "master"})

# `protect`'s and `rescope`'s own denial when a resolved checkout's default
# branch cannot be determined at all (issue #314 gate G4): unlike `claim`'s
# `is_default_branch` fallback below, they never guess -- see
# `_refuse_shared_checkout`'s and `_protect_not_main_denial`'s own
# docstrings for why the callers of the same `recorded_default_branch`
# reader accept different risk here.
DEFAULT_BRANCH_UNKNOWN_REASON = "default branch unknown"

# One owner for `protect`'s "not main" denial (issue #314 repeat gate,
# finding 4, Sonar S1192): `_protect_not_main_denial` in `cli.py` returns
# this for both a shared main checkout and a linked worktree that sits on
# the resolved default branch, so the one production spelling lives here
# instead of twice in that function.
PROTECT_NOT_MAIN_REASON = "not main"


def recorded_head_ref(remote: str, *, directory: Path | None = None) -> str | None:
    """`remote`'s recorded `HEAD` symbolic ref (e.g.
    `refs/remotes/origin/trunk`), read from `directory` via `-C` when given
    (issue #314: a resolved checkout's own lookup, never the calling
    process's cwd) or the process's own checkout otherwise -- `None` when a
    clone, a fetch, or `git remote set-head` never recorded one, or when
    the branch it names no longer resolves (issue #490: the remote renamed
    it and `fetch --prune` removed the old one). The one reader of a
    remote's `HEAD`; each caller decides its own fallback (issue #238),
    since `claim`, `protect`, and the trunk word or guess differently.

    One git read resolves the symbolic ref and proves its target exists; a
    plain, non-symbolic `HEAD` ref names itself and so no branch."""
    head = f"refs/remotes/{remote}/HEAD"
    try:
        symbolic = _git_output(
            ["rev-parse", "--verify", "--quiet", "--symbolic-full-name", head],
            directory=directory,
        )
    except ClaimError:
        return None
    return None if symbolic == head else symbolic


def recorded_default_branch(remote: str, *, directory: Path | None = None) -> str | None:
    """The default branch name `remote`'s recorded `HEAD` names in
    `directory` (issue #490), or `None` when `recorded_head_ref` finds none
    -- the offline checks' default branch: `claim`, `rescope`, `protect`,
    and `land` ask it of the canonical remote."""
    recorded_head = recorded_head_ref(remote, directory=directory)
    if recorded_head is None:
        return None
    return recorded_head.removeprefix(f"refs/remotes/{remote}/")


def is_default_branch(branch: str, default_branch: str | None) -> bool:
    """Whether `branch` is the repository's default branch (issue #238):
    `default_branch` -- `recorded_default_branch`'s answer -- or the
    historical `{"main", "master"}` guess when none is recorded.

    `claim`'s own worktree precondition (`_validate_worktree_branch`) alone,
    since a fresh claim is created by literally standing in the worktree it
    claims -- there is no attacker-reachable payload location to spoof
    here, so the historical guess stays an accepted risk (issue #238).
    `protect` and `rescope` deny outright when no default branch is
    recorded (issue #314 gate G4), rather than share this guess.
    """
    if default_branch is not None:
        return branch == default_branch
    return branch in DEFAULT_BRANCH_FALLBACK


def refuse_unclean_default_branch_checkout(
    branch: str | None, *, directory: Path | None = None
) -> None:
    """`land`'s own precondition (issue #405): the checkout at `directory`
    (or the calling process's own cwd) must already sit on the repository's
    default branch `branch` with nothing uncommitted, since `land`
    fast-forwards that exact branch in place once its merge succeeds --
    raises the ruled refusal otherwise, and `default branch unknown` when
    `branch` is `None`."""
    if branch is None:
        raise ClaimError(DEFAULT_BRANCH_UNKNOWN_REASON)
    current = current_branch(directory=directory)
    dirty = _git_output(["status", "--porcelain"], directory=directory)
    if current != branch or dirty:
        raise ClaimError(f"land must run from a clean checkout of the default branch {branch!r}")


def trunk_ref_after(remote: str, recorded_head: str | None, *, directory: Path) -> str:
    """`remote`'s trunk ref in `directory`: `recorded_head` -- `remote`'s
    recorded `HEAD` as `recorded_head_ref` read it -- or, when `remote`
    never recorded one or it dangles, the historical `{main, master}` guess, `remote`'s
    own before the local branch (issues #238, #304). A `RunContext` asks
    this once per directory, and again only after its run's fetch (issue
    #488), so a trunk is never resolved from a `HEAD` read before it."""
    if recorded_head is not None:
        return recorded_head
    for candidate in (
        f"refs/remotes/{remote}/main",
        f"refs/remotes/{remote}/master",
        "main",
        "master",
    ):
        try:
            _git_output(["rev-parse", "--verify", candidate], directory=directory)
            return candidate
        except ClaimError:
            continue
    raise ClaimError(
        f"cannot determine the trunk: none of {remote}/HEAD, {remote}/main, "
        f"{remote}/master, main or master resolves"
    )


def _git_hex_placeholder(character: str) -> str:
    """`character`, as the git `--format=`/`%(trailers:...)` `%xHH` escape
    that makes git itself emit the raw byte at run time -- the one owner for
    every separator `_TRUNK_LANDING_LOG_FORMAT` embeds, so each byte is
    spelled once in Python and turned into git's own placeholder text here,
    never typed a second time as a literal `%x..` string. (The raw byte
    itself can never sit directly in the `--format=` argument: an argv
    string is a C string, so a literal NUL there is illegal.)"""
    return f"%x{ord(character):02x}"


# Field separator between a trunk-landing record's `sha`/`committed_at`/
# `Work-Item` trailer values/`No-Item` trailer values. NUL is also `git log
# -z`'s own record terminator, so splitting the whole raw stream on it
# (`trunk_landings`, below) reads every record's four fields *and* the
# boundary between records through the one byte git forbids inside a commit
# message -- unlike the historical `\x1f` separator this replaces, which git
# never escapes inside a trailer *value* (issue #304 review, finding B1: a
# probed value `#12\x1f#13` survived verbatim and silently split into two
# fabricated work items).
#
# Repeated trailer values within one field join on a real newline instead:
# git's own `unfold` guarantees one physical line per logical trailer value
# (a folded/wrapped continuation line is joined back into it) before that
# separator ever runs, so a value can never itself contain the byte the
# split relies on.
_TRUNK_LANDING_FIELD_SEPARATOR = "\x00"
_TRUNK_LANDING_TRAILER_VALUE_SEPARATOR = "\n"
_TRUNK_LANDING_LOG_FORMAT = (
    "%H"
    f"{_git_hex_placeholder(_TRUNK_LANDING_FIELD_SEPARATOR)}%cI"
    f"{_git_hex_placeholder(_TRUNK_LANDING_FIELD_SEPARATOR)}"
    "%(trailers:key=Work-Item,valueonly,"
    f"separator={_git_hex_placeholder(_TRUNK_LANDING_TRAILER_VALUE_SEPARATOR)},unfold)"
    f"{_git_hex_placeholder(_TRUNK_LANDING_FIELD_SEPARATOR)}"
    "%(trailers:key=No-Item,valueonly,"
    f"separator={_git_hex_placeholder(_TRUNK_LANDING_TRAILER_VALUE_SEPARATOR)},unfold)"
)


@dataclass(frozen=True)
class TrunkLanding:
    """One first-parent commit on the trunk (issue #304): its identity, when
    it landed, and -- when its own trailer block names one -- what it
    landed. `classification` is read solely from the trailer block git's own
    parsing recognizes; a `Work-Item:`/`No-Item:` line anywhere else in the
    body is prose, not evidence, so most trunk commits (not every landing is
    a dispatched slice's own merge or squash) carry `None`.
    `work_item_values` keeps the trailer's own `Work-Item:` values verbatim,
    so a refusal can quote what landed even when the grammar rejects it
    (issue #427)."""

    sha: str
    committed_at: datetime
    classification: board.TrunkClassification | board.ClassificationDefect | None
    work_item_values: tuple[str, ...]


def _trailer_values(field: str) -> tuple[str, ...]:
    return tuple(field.split(_TRUNK_LANDING_TRAILER_VALUE_SEPARATOR)) if field else ()


def _parsed_trunk_landing(fields: tuple[str, str, str, str]) -> TrunkLanding:
    sha, raw_committed_at, work_item_field, no_item_field = fields
    try:
        committed_at = datetime.fromisoformat(raw_committed_at)
    except ValueError as error:
        raise ClaimError("git returned a malformed trunk landing timestamp") from error
    if committed_at.tzinfo is None:
        raise ClaimError("git returned a malformed trunk landing timestamp")
    work_item_values = _trailer_values(work_item_field)
    classification = board.trunk_commit_classification(
        work_item_values, _trailer_values(no_item_field)
    )
    return TrunkLanding(sha, committed_at.astimezone(UTC), classification, work_item_values)


def trunk_landings(trunk: str, depth: int, *, directory: Path) -> tuple[TrunkLanding, ...]:
    """The most recent `depth` first-parent landings on `trunk`, read in
    `directory`, oldest first, each classified from its own trailer block
    alone (issue #304).

    A merge counts once. Reading the trunk — never the work branch — is the
    contract: a ruling ages with trunk, not with local commits, and `trunk`
    is the caller's own canonical remote's (its `RunContext`'s), never a
    hardcoded `origin`'s, so a repository configured with a different
    canonical remote ages rulings against the trunk it actually lands on.
    A caller that must see a commit the forge just reported merged passes
    the trunk its context fetched first (issue #397).
    """
    raw = _git_output(
        [
            "log",
            "-z",
            "--first-parent",
            "--reverse",
            f"--format={_TRUNK_LANDING_LOG_FORMAT}",
            "-n",
            str(depth),
            trunk,
        ],
        directory=directory,
    )
    if not raw:
        return ()
    # `-z` terminates every record -- including the last -- with the same
    # byte that separates that record's own four fields, so splitting the
    # whole stream on it leaves exactly one trailing empty token; `sha` and
    # `committed_at` are never empty, so any other shape is git misbehaving.
    fields = raw.split(_TRUNK_LANDING_FIELD_SEPARATOR)
    if fields[-1] != "" or len(fields) % 4 != 1:
        raise ClaimError("git returned a malformed trunk landing log")
    fields = fields[:-1]
    return tuple(
        _parsed_trunk_landing(
            (fields[index], fields[index + 1], fields[index + 2], fields[index + 3])
        )
        for index in range(0, len(fields), 4)
    )


def fast_forward_default_branch(trunk: str, *, directory: Path | None = None) -> None:
    """`land`'s own step once its merge succeeds (issue #405): fast-forward
    the checkout's local default branch to `trunk`, the ref its run fetched
    (`RunContext.fetched_trunk_ref`, issue #488). `--ff-only` refuses loud
    rather than rewriting history if the local branch somehow diverged --
    never true in the ordinary case, since
    `refuse_unclean_default_branch_checkout` already proved this exact
    checkout clean and on the default branch before the merge ever ran."""
    result = _git_run(["merge", "--ff-only", trunk], directory=directory)
    if result.exit_status != 0:
        raise ClaimError(process.git_failure_detail(result))


def resolved_agent(explicit: str | None) -> str:
    if explicit is not None:
        return _outbound_text(explicit, "agent", maximum=128)
    agent = session_agent()
    if agent is None:
        raise ClaimError(
            f"agent identity is required: pass --agent or set {IDENTITY_ENVIRONMENT_ORDER}"
        )
    return agent


def session_agent() -> str | None:
    """This session's own agent identity from its environment, or `None`
    when it names none -- each caller says how to supply one, since only
    the CLI commands have an `--agent` flag (`protect`'s hook line does
    not, issue #448)."""
    configured = os.environ.get(ACO_AGENT_ENV)
    if configured:
        return _outbound_text(configured, "agent", maximum=128)
    grok_session = os.environ.get(GROK_SESSION_ID_ENV)
    if grok_session:
        return _outbound_text(f"Grok {grok_session}", "agent", maximum=128)
    claude_session = os.environ.get(CLAUDE_CODE_SESSION_ID_ENV)
    if claude_session:
        return _outbound_text(f"Claude {claude_session}", "agent", maximum=128)
    return None


# One owner for `start`'s own path/branch naming scheme (issue #322): the
# same `<repo>-worktrees/issue-<n>-<slug>` / `<prefix>/issue-<n>-<slug>`
# shape `ISOLATED_WORKTREE_RECIPE` already spells out by hand, so a lane
# directory `start` creates looks exactly like one a person typed.
_SLUG_MAX_LENGTH = 40
_SLUG_COLLAPSE_PATTERN = re.compile(r"[^a-z0-9]+")


def slug_from_title(title: str) -> str:
    """`start`'s own worktree/branch slug, derived from an item's title when
    `--slug` is not given (issue #322): lowercased, every run of characters
    outside `[a-z0-9]` collapsed to one hyphen, at most 40 characters, with
    no leading or trailing hyphen. Refuses when nothing usable survives (a
    title that is empty or pure punctuation), rather than emitting a
    trailing-bare `issue-<n>-` path silently."""
    collapsed = _SLUG_COLLAPSE_PATTERN.sub("-", title.strip().lower()).strip("-")
    slug = collapsed[:_SLUG_MAX_LENGTH].rstrip("-")
    if not slug:
        raise ClaimError("no usable slug in this item's title: pass --slug explicitly")
    return slug


# The shape `slug_from_title` always produces (issue #322 review finding 3):
# an explicit `--slug` value is held to the identical rule rather than used
# verbatim, since it lands unescaped in a worktree path and a branch name.
_SLUG_SHAPE_RULE = (
    "lowercase letters, digits, and single '-' separators only, at most 40 characters, "
    "never leading or trailing '-'"
)
_SLUG_SHAPE_PATTERN = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


def validate_slug(slug: str) -> str:
    """`slug`, refused by name when it does not match the shape
    `slug_from_title` itself always produces (issue #322 review finding 3):
    `--slug` is never sanitized the way a derived slug is, so a value with
    uppercase letters, punctuation, or a leading/trailing/doubled `-` is
    refused outright rather than silently accepted into a worktree path and
    branch name a derived slug could never produce."""
    if len(slug) > _SLUG_MAX_LENGTH or _SLUG_SHAPE_PATTERN.fullmatch(slug) is None:
        raise ClaimError(f"--slug must be {_SLUG_SHAPE_RULE}")
    return slug


def branch_prefix_for_identity() -> str:
    """`start`'s own branch prefix (issue #322): `session_agent`'s own
    identity rendered as a short git-branch-safe token -- its first word,
    lowercased (`"Claude head (coordinator)"` -> `"claude"`, `"Grok <id>"`
    -> `"grok"`); refused when none resolves, since a worktree/branch
    scheme needs a real name, never a guess."""
    agent = session_agent()
    if agent is None:
        raise ClaimError(f"branch prefix is required: set {IDENTITY_ENVIRONMENT_ORDER}")
    return agent.split()[0].lower()


def refuse_unsafe_start_branch(branch: str, *, prefix: str) -> None:
    """Refuse `start`'s own built branch name before any git write (issue
    #322 review/gate: an unsafe `ACO_AGENT` first word used to reach `git
    worktree add` before the claim machinery ever validated the resulting
    branch, so an invalid name could leave an orphan worktree and branch
    behind). `slug_from_title`/`validate_slug` already bind the slug's own
    shape and `number` is always an int, so the one unsanitized ingredient
    left in `branch` is `prefix` -- the identity word this names as the
    refusal's cause.
    """
    if not is_safe_branch_name(branch):
        raise ClaimError(
            f"agent identity {prefix!r} is not usable in a branch name: "
            f"{branch!r} is not a safe Git ref"
        )


def branch_exists(branch: str) -> bool:
    """Whether the calling process's own checkout already has a local
    branch named `branch` (issue #322) -- `start`'s naming-collision guard,
    read the same `-C`-free way `_validate_worktree_branch` reads the
    checkout's own current branch, since `start` always runs from the
    repository whose sibling worktree it is about to create, never from an
    arbitrary resolved directory."""
    result = _git_run(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
    if result.exit_status == 0:
        return True
    if result.exit_status == 1:
        return False
    raise ClaimError(process.git_failure_detail(result))


def fetch_remote(remote: str, *, directory: Path | None = None) -> None:
    """Refresh `remote`'s remote-tracking refs in `directory` via `-C` when
    given or the calling process's own cwd otherwise, failing loud with
    git's own detail."""
    fetch = _git_run(["fetch", remote], directory=directory)
    if fetch.exit_status != 0:
        raise ClaimError(process.git_failure_detail(fetch))


def trunk_commit(trunk: str, *, directory: Path) -> str:
    """The commit `trunk` names now in `directory` (issue #479): `start`
    checks its claim against this one commit -- the claim's base and the
    tree its scope is measured against -- before it builds."""
    return _git_output(["rev-parse", "--verify", f"{trunk}^{{commit}}"], directory=directory)


def create_linked_worktree(
    path: Path, *, branch: str, trunk: str, directory: Path | None = None
) -> None:
    """Create a linked worktree at `path` on a fresh `branch` from `trunk`,
    the canonical remote's trunk ref as the caller's fetch left it (issue
    #322; issue #479): the `git worktree add` step
    `ISOLATED_WORKTREE_RECIPE` used to spell out for a person to type by
    hand, run through this module's own `_git_run` chokepoint so `start`
    opens no new subprocess call site. Built from the trunk's ref, so the
    branch tracks it wherever git's own `branch.autoSetupMerge` says so.
    Reads and writes `directory`'s own checkout via `-C` when given (issue
    #394: `protect.judge`'s own direct tests build a worktree fixture from
    an explicit repository path, never the test process's cwd) or the
    calling process's own checkout otherwise."""
    result = _git_run(["worktree", "add", str(path), "-b", branch, trunk], directory=directory)
    if result.exit_status != 0:
        raise ClaimError(process.git_failure_detail(result))


# One owner for `existing_start_worktree`'s two naming-collision repair
# clauses (Sonar S1192): a stray branch with no worktree of its own and an
# existing worktree checked out on the wrong branch share the identical fix.
_CHOOSE_A_DIFFERENT_WORKTREE_REPAIR = "remove it, or pass --slug to choose a different worktree"


def _own_common_directory() -> Path:
    """The calling process's own common git directory: the one fact every
    worktree of this repository shares, whichever of them the process runs
    in."""
    return Path(_git_output(["rev-parse", "--path-format=absolute", "--git-common-dir"])).resolve()


def main_checkout_root(*, toplevel: Path) -> Path:
    """The repository's main checkout, whichever of its worktrees the
    caller stands in (issue #479): `start` places a lane's worktree beside
    it, so a call from inside a linked worktree never nests the new one
    under that worktree. `toplevel` is the caller's own, held by its run
    context. A main checkout is its own answer, whatever layout its git
    directory has. A linked worktree finds it through the common
    directory's `core.worktree` when that names one (a submodule), else as
    the checkout holding a common directory called `.git`. A bare or
    `--separate-git-dir` common directory records no checkout at all -- git's
    own `worktree list` names the git directory itself there -- so this
    refuses rather than build beside, or read the board configuration of,
    a linked worktree (START-24)."""
    caller = _resolve_checkout(toplevel)
    if caller.kind is CheckoutKind.MAIN:
        return caller.toplevel.resolve()
    configured = _configured_worktree(directory=toplevel)
    if configured is not None:
        return (caller.common_directory / configured).resolve()
    common_directory = caller.common_directory.resolve()
    if common_directory.name == ".git":
        return common_directory.parent
    raise ClaimError(
        f"main checkout unknown: git directory {common_directory} names no checkout; "
        "run start from the main checkout"
    )


def _configured_worktree(*, directory: Path) -> str | None:
    """The common directory's own `core.worktree`, or `None` when unset:
    git exits `1` for an unset key and nothing else."""
    result = _git_run(["config", "--get", "core.worktree"], directory=directory)
    if result.exit_status == 1:
        return None
    if result.exit_status != 0:
        raise ClaimError(process.git_failure_detail(result))
    return result.stdout.decode().strip()


def _refuse_foreign_worktree(path: Path, existing: PathCheckout) -> None:
    """Refuse to resume `existing` -- already resolved at `path` -- unless
    it is a linked worktree of this same repository (issue #322 review
    finding 2): the caller's own common git directory, read from its own
    cwd since `existing_start_worktree` always runs from the repository
    it is building a sibling worktree for, must equal `existing`'s; a clean
    linked worktree from a different repository that merely happens to sit
    on the same branch name must never be adopted as this item's own.
    """
    if existing.toplevel != path.resolve():
        raise ClaimError(
            f"worktree {path} is not a checkout root by itself (its own toplevel is "
            f"{existing.toplevel}); {_CHOOSE_A_DIFFERENT_WORKTREE_REPAIR}"
        )
    if existing.kind is not CheckoutKind.LINKED_WORKTREE:
        raise ClaimError(
            f"worktree {path} is a repository's own main checkout, not a linked worktree; "
            f"{_CHOOSE_A_DIFFERENT_WORKTREE_REPAIR}"
        )
    if existing.common_directory != _own_common_directory():
        raise ClaimError(
            f"worktree {path} belongs to a different repository; "
            f"{_CHOOSE_A_DIFFERENT_WORKTREE_REPAIR}"
        )


NOT_A_WORKTREE_REFUSAL = "path exists and is not a worktree of this repository"


def existing_start_worktree(path: Path, branch: str) -> bool:
    """Whether a prior `start`'s own worktree already stands at `path`,
    validated for resume (issue #322), or nothing does and `start` may
    build there (issue #479: it checks its claim first, so this only
    reads). Refuses by name when `branch` is already taken by something
    that is not this worktree, when `path` resolves to a checkout this
    repository does not own (`_refuse_foreign_worktree`), when a worktree
    already at `path` is dirty, or when something -- empty or not --
    already sits at `path` without being a worktree of this repository at
    all (issue #322 review/gate finding: `git worktree add` must never be
    left to adopt, and potentially remove, an existing directory nobody
    offered up for this)."""
    if not path.exists():
        if branch_exists(branch):
            raise ClaimError(
                f"branch {branch!r} already exists and is not this item's worktree; "
                f"{_CHOOSE_A_DIFFERENT_WORKTREE_REPAIR}"
            )
        return False
    existing = resolve_path_checkout(path)
    if existing is None:
        raise ClaimError(NOT_A_WORKTREE_REFUSAL)
    _refuse_foreign_worktree(path, existing)
    if existing.branch != branch:
        raise ClaimError(
            f"worktree {path} exists on branch {existing.branch!r}, not {branch!r}; "
            f"{_CHOOSE_A_DIFFERENT_WORKTREE_REPAIR}"
        )
    dirty = _git_output(["status", "--porcelain"], directory=path)
    if dirty:
        named = named_with_overflow_count(_dirty_paths(dirty))
        raise ClaimError(f"worktree {path} is dirty: {named}; commit or clean it before resuming")
    return True


def worktree_on_branch(paths: tuple[Path, ...], branch: str) -> Path | None:
    """The one path among `paths` (`store.list_worktrees`'s own listing)
    whose own checked-out branch is `branch`, or `None` when none matches
    (issue #322) -- `release --merged`'s own cleanup finds the lane's
    linked worktree by the branch its claim already names, not by `start`'s
    naming scheme, since a claim's worktree may predate `start` or have
    been resumed under a different `--slug`. `paths` are entries git's own
    worktree registry already vouches for, so a failure resolving one of
    them (issue #322 review/gate finding: a moved or deleted worktree
    directory, most often) is a real refusal, via `_resolve_checkout`,
    never a silently skipped "not on this branch"."""
    for candidate in paths:
        found = _resolve_checkout(candidate)
        if found.branch == branch:
            return candidate
    return None


@dataclass(frozen=True)
class WorktreeRemoval:
    """Whether `release --merged`'s own cleanup removed the lane's linked
    worktree, and why not when it did not (issue #322 review/gate finding
    4)."""

    removed: bool
    reason: str | None


@dataclass(frozen=True)
class BranchRemoval:
    """Whether the same cleanup also removed the lane's own local branch,
    tracked apart from `WorktreeRemoval` (issue #322 review/gate finding 4):
    `git worktree remove` and `git branch -d` are two separate git writes,
    so the first can succeed while the second fails, and that must never
    read as a bare `kept` that hides the worktree's own removal."""

    removed: bool
    reason: str | None


@dataclass(frozen=True)
class WorktreeCleanupOutcome:
    """`release --merged`'s own worktree/branch cleanup result, in two parts
    (issue #322 review/gate finding 4): this module owns the policy that
    decides it, `cli` only renders it."""

    worktree: WorktreeRemoval
    branch: BranchRemoval


_WORKTREE_REMOVED = WorktreeRemoval(removed=True, reason=None)
_BRANCH_REMOVED = BranchRemoval(removed=True, reason=None)


def worktree_cleanup_kept(reason: str) -> WorktreeCleanupOutcome:
    """The outcome for every reason cleanup declines before ever touching
    disk: a flag, a dirty tree, an unmerged or elsewhere-checked-out branch,
    no matching worktree, running from inside it, or a git failure resolving
    any of these -- `branch` is never separately attempted once `worktree`
    itself is kept."""
    return WorktreeCleanupOutcome(
        worktree=WorktreeRemoval(removed=False, reason=reason),
        branch=BranchRemoval(removed=False, reason=None),
    )


WORKTREE_KEPT_NOT_MERGED_REASON = "not merged into the default branch"
WORKTREE_KEPT_DIRTY_REASON = "dirty"
WORKTREE_KEPT_ELSEWHERE_REASON = "branch checked out elsewhere"


def cleanup_landed_worktree(
    matching: Path, branch: str, *, trunk: str, directory: Path
) -> WorktreeCleanupOutcome:
    """`release --merged`'s own cleanup policy once a lane's linked worktree
    is already found (issue #322 review/gate finding 4): merged check, then
    checked-out-elsewhere check, then a dirty check, then the worktree
    removal itself, then the branch deletion -- in that order, since
    removing a dirty or still-needed worktree is unsafe and the two git
    writes below it are each worth reporting apart. `release`'s own
    cwd-equality guard and its "no worktree matches this branch" decision
    run before this and stay the caller's own job (they need the process's
    own cwd and worktree listing, neither of which this function reads)."""
    if not branch_merged_into_default(branch, trunk=trunk, directory=directory):
        return worktree_cleanup_kept(WORKTREE_KEPT_NOT_MERGED_REASON)
    matching_checkout = resolve_path_checkout(matching)
    if matching_checkout is not None and matching_checkout.kind is CheckoutKind.MAIN:
        return worktree_cleanup_kept(WORKTREE_KEPT_ELSEWHERE_REASON)
    dirty = _git_output(["status", "--porcelain"], directory=matching)
    if dirty:
        return worktree_cleanup_kept(WORKTREE_KEPT_DIRTY_REASON)
    return remove_linked_worktree(matching, branch=branch)


def remove_linked_worktree(path: Path, *, branch: str) -> WorktreeCleanupOutcome:
    """Remove a landed lane's linked worktree and its own local branch
    (issue #322), or the pair a refused `start` had just created (issue
    #479): `git worktree remove` first -- git refuses to delete a
    branch still checked out anywhere -- then `git branch -d`, both through
    this module's own `_git_run` chokepoint. Never called on the calling
    process's own checkout: `release`'s own cwd-equality guard runs first,
    since a worktree cannot remove its own cwd, and `start` removes only a
    worktree it created, never the one it runs in. A worktree-removal failure
    still raises loud (nothing on disk has changed yet); a branch-deletion
    failure once the worktree is already gone returns a typed outcome
    instead (issue #322 review/gate finding 4), so that success is never
    lost behind a bare `kept`."""
    result = _git_run(["worktree", "remove", str(path)])
    if result.exit_status != 0:
        raise ClaimError(process.git_failure_detail(result))
    result = _git_run(["branch", "-d", branch])
    if result.exit_status != 0:
        return WorktreeCleanupOutcome(
            worktree=_WORKTREE_REMOVED,
            branch=BranchRemoval(
                removed=False, reason=f"git failure: {process.git_failure_detail(result)}"
            ),
        )
    return WorktreeCleanupOutcome(worktree=_WORKTREE_REMOVED, branch=_BRANCH_REMOVED)


def branch_merged_into_default(branch: str, *, trunk: str, directory: Path) -> bool:
    """Whether `branch`'s tip is already an ancestor of `trunk` in
    `directory` (issue #322): `release --merged`'s own cleanup
    precondition, against the trunk its context fetched so a landing this
    same process just verified through the forge is visible locally even
    when nothing else in this checkout has fetched since. A squash or rebase
    landing's trunk commit is never a literal descendant of the lane
    branch's own tip, so this reads `False` for one -- a safe, conservative
    "not merged" that only ever skips cleanup, never removes a branch git
    cannot itself prove is in."""
    result = _git_run(["merge-base", "--is-ancestor", branch, trunk], directory=directory)
    if result.exit_status == 0:
        return True
    if result.exit_status == 1:
        return False
    raise ClaimError(process.git_failure_detail(result))
