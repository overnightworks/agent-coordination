# `aco land`

`aco land <pr>` merges one green pull request under `storage = "github"`
with a pinned head sha and its own composed commit message, then runs the
existing `release --merged <pr>` path (`specs/release.spec.md`,
`specs/landing-grammar.spec.md` LAND-29..39, 49, 50, 55, 62/63/64) unchanged
-- one command instead of "merge, delete the branch, fetch, fast-forward,
release" by hand. This file owns the command's own preflight order and
sentences, the commit message it composes, its idempotent post-merge steps,
and its recovery line; it never restates what `release --merged`'s own
classification, claim, parent, closing, or claimant rules require (cited by
ID) or what a successful release prints (`freed:`/`next:`, LAND-49). `<n>`
is the pull request number as given, `<sha>` its merge commit, `<state>`
GitHub's own `mergeable_state`, `<name>`/`<conclusion>` one check's own name
and conclusion, `<path>` the board configuration `.aco/board.toml`,
`<setting>` one of the two settings in it a head may not change, `storage`
and `canonical_remote` (`priority_labels`, `idea_label`, `body_contract`, and
`merge_method` may change), `<title>` the pull request's own title, a "head" the pull request's own head commit read during
preflight, `<actual>` that head's full sha, and `<reviewed>` the head its reviewers saw, the `--head` value lowercased: its
full sha or a prefix of at least 7 hex digits. "Checks" is every check run GitHub reports for the head sha
(every page of `check-runs`) plus every combined-status context
(`commits/<actual>/status`; an external context such as SonarCloud counts);
"no checks" means both are empty. GitHub owns a check's own name -- no
length this tool controls -- so each name is truncated to 40 characters
with `…` before a name list past three entries prints only the first three,
then `and N more` (`protocol.named_with_overflow_count`); the assembled
sentence is then capped again so no refusal line ever prints past 200
characters, even past that truncation; a display control in it (the set
`specs/next.spec.md` owns, NEXT-37) prints escaped exactly as `next` shows
it (a newline as `\n`, ESC as `\x1b`, U+202E by its code point), while TAB
and NBSP stay as they are, so a capped refusal is always one line. A refusal before any write
prints `ERROR: <sentence>` on stderr, exit `2`, exactly as
`specs/ref-store-cas.spec.md`'s own preamble documents; `aco land` has no
`--json` mode. `<branch>` is the forge's default branch, `<m>` the `merge_method`
pinned in `<path>`, and `<list>` GitHub's allowed methods (LANDCMD-39) comma-joined in the
order `merge`, `squash`, `rebase`.

## Behavior table

| state \ trigger | `aco land <n>` |
|---|---|
| `--head` not 7 to 40 hex digits | LANDCMD-33 |
| `<path>` absent from this checkout | PIN-32 (cited) |
| `<path>` present but untracked or ignored | PIN-01 (cited) |
| `storage = "state-ref"` | LANDCMD-01 |
| `--coordinator-override` without `--role coordinator` | LANDCMD-19 |
| the head no longer starts with `<reviewed>` | LANDCMD-31 |
| no `--head` given | LANDCMD-32 |
| pull request not open | LANDCMD-02 |
| not mergeable | LANDCMD-03 |
| no CI checks at all | LANDCMD-04 |
| a check still running | LANDCMD-05 |
| a check finished without success | LANDCMD-06 |
| pull request's own shape (classification line, cross-repository head, target branch) invalid | LANDCMD-07 (LAND-06..13, 32, cited) |
| the head removes `<path>`, changes `storage` or `canonical_remote` in it, or carries an invalid one | LANDCMD-22, LANDCMD-23, LANDCMD-24 |
| named work item not open | LANDCMD-08 |
| classification's own claim/parent/closing defect | LANDCMD-09 (LAND-14..28, cited) |
| claim held by another agent or role | LANDCMD-10 |
| checkout unclean or off the default branch | LANDCMD-11 |
| checkout without a git identity | LANDCMD-25 |
| the repository's own settings allow neither a merge commit nor a squash merge | LANDCMD-28 |
| the `merge_method` pin in `<path>` is not among GitHub's allowed methods | LANDCMD-40 |
| the default branch's rules leave neither a merge commit nor a squash merge | LANDCMD-41 |
| every precondition holds | LANDCMD-12, LANDCMD-13, LANDCMD-27, LANDCMD-39, LANDCMD-42, LANDCMD-29, LANDCMD-34, LANDCMD-35, LANDCMD-36 |
| the pull request changed since it was read | LANDCMD-14 |
| GitHub refuses the merge | LANDCMD-37 |
| a step after the merge fails | LANDCMD-15, LANDCMD-16 |
| this repository's own pull request | LANDCMD-17 |
| a pull request already merged (rerun) | LANDCMD-18 |
| a rerun whose release already succeeded | LANDCMD-38 |

## Preflight, read-only, in order

Every step here reads only: the one local `refs/aco/state` read LANDCMD-10's
claim check needs is `store.peek_state` (CAS-49), never `fetch_state` --
this worktree's own fetch anchor and lineage stamp stay untouched by a
preflight, refused or not, exactly as `reset`'s own read does.

- [ ] [LANDCMD-33] A `--head` that is not 7 to 40 hex digits refuses `--head must be 7 to 40 hex digits`, exit `2`, at the argument parse: before any read, PIN-32, or LANDCMD-01, on a rerun too (E-LANDCMD-33).
- [ ] [LANDCMD-01] Under `storage = "state-ref"`, `aco land <n>` refuses `aco land is a github command; storage = state-ref has no pull requests to land`, exit `2`, before any read.
- [ ] [LANDCMD-19] `--coordinator-override` without `--role coordinator` refuses (CLAIM-39's sentence), exit `2`, at `aco land`'s own entry -- before any read, so also before a rerun skips the rest of preflight.
- [ ] [LANDCMD-31] `--head <reviewed>` refuses `pull request #<n> head is <actual>, not the reviewed <reviewed>; review the new head before landing`, exit `2`, unless the head starts with `<reviewed>` (E-LANDCMD-31).
- [ ] [LANDCMD-32] Without `--head`, `aco land` pins whatever head preflight reads (LANDCMD-12); a rerun (LANDCMD-18) never compares `--head`, since its merge already happened.
- [ ] [LANDCMD-02] A pull request that is not open refuses `pull request #<n> is not open; it cannot be landed`, exit `2`.
- [ ] [LANDCMD-03] A pull request whose own `mergeable_state` is not `clean` refuses `pull request #<n> is not mergeable (<state>)`, exit `2`.
- [ ] [LANDCMD-04] A pull request exposing no CI checks against its own head commit refuses `pull request #<n> exposes no CI checks; cannot verify green CI`, exit `2`.
- [ ] [LANDCMD-05] A pull request with one or more checks not yet completed refuses `pull request #<n> has checks still running: <name>, <name>; wait for every check to succeed`, exit `2`.
- [ ] [LANDCMD-06] A pull request whose checks all completed, at least one without success, refuses `pull request #<n> has non-successful checks: <name> (<conclusion>); land only after every check succeeds`, exit `2`.
- [ ] [LANDCMD-07] This pull request's own shape decides its classification, exactly as `check <pr>` reads it (LAND-06..13, 32): a shape defect refuses `pull request #<n> <that same defect sentence>`, exit `2`.
- [ ] [LANDCMD-22] After LANDCMD-07, a head without `<path>` refuses `pull request #<n> removes <path>; aco land cannot release its claim across that change`, exit `2` (E-LANDCMD-22).
- [ ] [LANDCMD-23] A head changing `storage` or `canonical_remote` in `<path>` refuses `pull request #<n> changes <setting> in <path>; aco land cannot release its claim across that change`, exit `2`.
- [ ] [LANDCMD-24] A head `<path>` the pin's own validator refuses prints `pull request #<n> carries an invalid <path>: <detail>`, exit `2`.
- [ ] [LANDCMD-08] A classified work item that is not open refuses `work item #<n> is not open; it cannot be landed`, exit `2`; an issue-less pull request skips this check.
- [ ] [LANDCMD-09] The classification's own claim, parent, and closing rules then apply (LAND-14..28): a defect refuses `pull request #<n> <that same defect sentence>`, exit `2`.
- [ ] [LANDCMD-10] A claim held by another agent or role, no coordinator override, refuses (REL-12's sentence), exit `2`, before the merge; `<repeat>`: `aco land <n>`, any `--head`/`--keep-worktree`, REL-41's identity.
- [ ] [LANDCMD-11] This checkout must sit on the forge's default branch with nothing uncommitted, or `aco land` refuses `land must run from a clean checkout of the default branch '<branch>'`, exit `2`.
- [ ] [LANDCMD-25] A checkout without a git identity refuses `land must run from a checkout with a git identity; set user.name and user.email there so its release can commit to the claim state`, exit `2`.
- [ ] [LANDCMD-20] The forge names `<branch>` even where the canonical remote records no `HEAD`; LANDCMD-11 never reads one.

## Merge, composed by `aco land`

- [ ] [LANDCMD-12] `aco land` merges pinned to the head sha read during preflight, never an unpinned re-read, with the method LANDCMD-27 picks: a merge commit or one squash commit, never a rebase.
- [ ] [LANDCMD-27] `merge_method` `"merge"`/`"squash"` in `<path>` picks the method; else a merge commit if GitHub's allowed methods (LANDCMD-39) include one, else a squash.
- [ ] [LANDCMD-39] GitHub's allowed methods are `allow_merge_commit`/`allow_squash_merge`/`allow_rebase_merge` (all three where withheld), narrowed by every rule on `<branch>` (LANDCMD-42).
- [ ] [LANDCMD-42] A `pull_request` rule narrows them to its `allowed_merge_methods` (not at all without that list), a `required_linear_history` rule drops `merge`, and a plan without rulesets narrows nothing.
- [ ] [LANDCMD-40] A pin non-empty allowed methods exclude refuses `board.toml merge_method <m> is not allowed on <branch>: GitHub allows <list>`, exit `2`, before any write, after LANDCMD-28 (E-LANDCMD-40).
- [ ] [LANDCMD-41] Rules narrowing the settings to no method, or an unpinned land's to only a rebase, refuse `GitHub allows no merge method aco can use on <branch>`, exit `2`, before any write (E-LANDCMD-41).
- [ ] [LANDCMD-28] Settings allowing neither refuse `pull request #<n> cannot land: this repository allows neither a merge commit nor a squash merge`, exit `2`, before any write (E-LANDCMD-28).
- [ ] [LANDCMD-30] Any other `merge_method` refuses `board configuration <path> merge_method must be 'merge' or 'squash'`, exit `2`; a head carrying one refuses as LANDCMD-24.
- [ ] [LANDCMD-29] The landed commit's title is `Merge pull request #<n>` for a merge commit and `<title> (#<n>)` for a squash commit.
- [ ] [LANDCMD-13] Its message is the pull request body with its classification line removed, then that classification as the message's last line, nothing after it, for a merge and a squash commit alike (E-LANDCMD-13).
- [ ] [LANDCMD-34] When that body ends in a trailer paragraph as git's trailer parsing reads it, the classification joins it as its last line, no blank line between, where git then reads both.
- [ ] [LANDCMD-36] Any other body takes a blank line, then the classification alone as its last paragraph.
- [ ] [LANDCMD-35] Removing the classification line leaves no run of blank lines where it stood: a paragraph it alone made goes with it, and a whitespace-only line counts as blank.
- [ ] [LANDCMD-14] A pull request whose head sha changed since preflight (HTTP 409) refuses the pinned merge with `pull request #<n> changed while it was checked; re-run land`, exit `2`; nothing merges.
- [ ] [LANDCMD-37] A merge GitHub refuses to perform (HTTP 405) refuses `GitHub refused the merge of pull request #<n>: <forge message>`, exit `2`; nothing merges (see E-LANDCMD-37).

## After the merge

- [ ] [LANDCMD-15] A failed branch delete, fast-forward, or delegated `release --merged` prints `MERGED pull request #<n> as <sha>; follow-up incomplete: <step> (<error>); re-run aco land <n>`, exit `2`.
- [ ] [LANDCMD-26] LANDCMD-15's `<error>` is the failed step's own sentence (for `release`, the one `release --merged` would print, without `ERROR: `), display controls escaped as NEXT-37 shows them.
- [ ] [LANDCMD-21] The fast-forward fetches the canonical remote `<remote>` once per run and moves `<branch>` to `<remote>/<branch>`; the delegated release walks that same ref.
- [ ] [LANDCMD-16] Deleting the merged branch is idempotent: a forge already reporting it absent is success, not a refusal.
- [ ] [LANDCMD-17] In this package's own repository, a successful landing's last line is `reinstall: uv tool install --force --from . agent-coordination`; any other repository prints nothing further.
- [ ] [LANDCMD-18] A rerun skips every preflight check but LANDCMD-11, LANDCMD-19, LANDCMD-25, and LANDCMD-33, verifies the trailer as `release --merged` does (LAND-62, LAND-64), and resumes -- never a second merge.
- [ ] [LANDCMD-38] A rerun whose delegated release meets REL-47 exits `0` with REL-49's lines, never LANDCMD-15, and no LANDCMD-17 line follows (see E-LANDCMD-18a).

Accepted residual: a squash-landed local branch behind or ahead of the pull request's recorded
head is no clean lane on it (REL-42, REL-43), so its worktree still reads `kept -- not merged into
the default branch` on every rerun; it is removed by hand.

## Never

- `aco land` never calls `gh pr merge`: every merge is a pinned REST call, never a re-read of the pull request's current head at merge time.
- `aco land` never runs `release`'s own close, store transition, `freed`/`next` report, or worktree cleanup a second time; it delegates to the one existing `release --merged` path (`specs/release.spec.md`).
- A step after the merge never re-merges: recovery always resumes from the pull request's own already-merged state, read fresh on every rerun.
- Once merged, the delegated release never reads the pull request's own mutable body for routing: a fixer editing it away afterward changes nothing this pull request already landed (LAND-64).
- `aco land` never writes when any preflight check (LANDCMD-01..11, LANDCMD-22..25, LANDCMD-28, LANDCMD-31, LANDCMD-33, LANDCMD-40, LANDCMD-41) refuses.
- `aco land` never takes its storage, canonical remote, forge, or claim store from a head's `<path>`: this checkout's own tracked copy governs, and the head's copy is only checked (LANDCMD-22..24).

## Examples

`Setup: bare-remote` is `specs/landing-grammar.spec.md`'s own fixture: a
fresh work repository whose `origin` is a local bare repository with `main`
at one commit, a git identity, `origin/HEAD`, and `ACO_AGENT` set to `Ada`,
plus a fixed, deterministic fake `gh`.

### E-LANDCMD-13 — a body ending in a trailer block keeps one trailer paragraph

Setup: bare-remote, fake `gh`, pull request `#57` landed as a merge commit by `aco land 57`, its body `Fixes it.`, a blank line, `Work-Item: #42`, `Co-Authored-By: A <a@x>`

```console
$ git log -1 --format=%B main
Merge pull request #57

Fixes it.

Co-Authored-By: A <a@x>
Work-Item: #42
$ git log -1 --format=%B main | git interpret-trailers --parse
Co-Authored-By: A <a@x>
Work-Item: #42
```

### E-LANDCMD-33 — a `--head` that is no sha refuses before any read

Setup: bare-remote, fake `gh`

```console
$ aco land 57 --head main
2> ERROR: --head must be 7 to 40 hex digits
exit 2
```

### E-LANDCMD-31 — a head that moved past the reviewed one refuses before any write

Setup: bare-remote, fake `gh`, pull request `#57` open, its head `1f23527c0ffee0ddba11ab1e5eed5ca1ab1edeed`, `<reviewed>` `9a6383f`

```console
$ aco land 57 --head 9a6383f
2> ERROR: pull request #57 head is 1f23527c0ffee0ddba11ab1e5eed5ca1ab1edeed, not the reviewed 9a6383f; review the new head before landing
exit 2
```

### E-LANDCMD-02 — a closed pull request refuses

Setup: bare-remote, fake `gh`, pull request `#57` closed without merging

```console
$ aco land 57
2> ERROR: pull request #57 is not open; it cannot be landed
exit 2
```

### E-LANDCMD-05 — a pull request with a check still running refuses

Setup: bare-remote, fake `gh`, pull request `#57` open, `mergeable_state` `clean`, one check `build` still running

```console
$ aco land 57
2> ERROR: pull request #57 has checks still running: build; wait for every check to succeed
exit 2
```

### E-LANDCMD-05a — a name list past three checks is capped

Setup: bare-remote, fake `gh`, pull request `#57` open, `mergeable_state` `clean`, five checks still running: `check-0` through `check-4`

```console
$ aco land 57
2> ERROR: pull request #57 has checks still running: check-0, check-1, check-2, and 2 more; wait for every check to succeed
exit 2
```

### E-LANDCMD-22 — a head that removes the board configuration refuses before any write

Setup: bare-remote, fake `gh`, pull request `#57` open, mergeable, every check green, its head deleting `.aco/board.toml`

```console
$ aco land 57
2> ERROR: pull request #57 removes .aco/board.toml; aco land cannot release its claim across that change
exit 2
```

### E-LANDCMD-23 — a head that re-pins the storage refuses before any write

Setup: bare-remote, fake `gh`, pull request `#57` open, mergeable, every check green, its head setting `storage = "state-ref"` in `.aco/board.toml`

```console
$ aco land 57
2> ERROR: pull request #57 changes storage in .aco/board.toml; aco land cannot release its claim across that change
exit 2
```

### E-LANDCMD-24 — a head carrying an invalid board configuration refuses before any write

Setup: bare-remote, fake `gh`, pull request `#57` open, mergeable, every check green, its head setting `storage = "gitlab"` in `.aco/board.toml`

```console
$ aco land 57
2> ERROR: pull request #57 carries an invalid .aco/board.toml: board configuration .aco/board.toml storage must be 'github' or 'state-ref'
exit 2
```

### E-LANDCMD-10 — a foreign claim refuses before the merge

Setup: bare-remote, fake `gh`, pull request `#57` open, mergeable, every check green, its own head branch's live claim held by `Grok`, not `Ada`

```console
$ aco land 57
2> ERROR: only the original claimant may release; repeat as the holder with `aco land 57 --agent Grok`, or use an explicit coordinator override (holder='Grok (builder)', this session='Ada (builder)')
exit 2
```

### E-LANDCMD-11 — an unclean checkout refuses before any write

Setup: bare-remote, fake `gh`, pull request `#57` open, mergeable, every check green, this checkout on `main` with an uncommitted change

```console
$ aco land 57
2> ERROR: land must run from a clean checkout of the default branch 'main'
exit 2
```

### E-LANDCMD-25 — a landing clone without a git identity refuses before any write

Setup: bare-remote, fake `gh`, pull request `#57` open, mergeable, every check green, `aco land` running from a second clean clone on `main` with no user.name or user.email

```console
$ aco land 57
2> ERROR: land must run from a checkout with a git identity; set user.name and user.email there so its release can commit to the claim state
exit 2
```

### E-LANDCMD-28 — a repository allowing neither a merge commit nor a squash refuses before any write

Setup: bare-remote, fake `gh`, pull request `#57` open, mergeable, every check green, no `merge_method` in `<path>`, the repository allowing only rebase merges

```console
$ aco land 57
2> ERROR: pull request #57 cannot land: this repository allows neither a merge commit nor a squash merge
exit 2
```

### E-LANDCMD-39 — a ruleset narrowing the settings picks a squash

Setup: bare-remote, fake `gh`, pull request `#57` open, mergeable, every check green, no `merge_method` in `<path>`, the repository's settings allowing all three methods, a ruleset on `main` allowing only squash and rebase

```console
$ aco land 57
RELEASED issue #42: <claim-id>
freed: none
next: none
exit 0
```

`main` gains one squash commit titled `<title> (#57)` (LANDCMD-29), never a merge commit the ruleset forbids.

### E-LANDCMD-40 — a pin the default branch's rules exclude refuses before any write

Setup: bare-remote, fake `gh`, pull request `#57` open, mergeable, every check green, `merge_method = "merge"` in `<path>`, a ruleset on `main` allowing only squash and rebase

```console
$ aco land 57
2> ERROR: board.toml merge_method merge is not allowed on main: GitHub allows squash, rebase
exit 2
```

### E-LANDCMD-41 — rules leaving no usable method refuse before any write

Setup: bare-remote, fake `gh`, pull request `#57` open, mergeable, every check green, no `merge_method` in `<path>`, the repository's settings allowing only merge commits, a ruleset on `main` allowing only squash and rebase

```console
$ aco land 57
2> ERROR: GitHub allows no merge method aco can use on main
exit 2
```

### E-LANDCMD-14 — the pull request changed since preflight refuses the merge

Setup: bare-remote, fake `gh`, pull request `#57` open and green during preflight, its head moved before the pinned merge request lands

```console
$ aco land 57
2> ERROR: pull request #57 changed while it was checked; re-run land
exit 2
```

### E-LANDCMD-37 — a merge GitHub refuses names the forge's own reason

Setup: bare-remote, fake `gh`, pull request `#57` open and green during preflight, the pinned merge request answered `Repository rule violations found (HTTP 405)`

```console
$ aco land 57
2> ERROR: GitHub refused the merge of pull request #57: gh: Repository rule violations found (HTTP 405)
exit 2
```

### E-LANDCMD-15 — a failed post-merge step names its own recovery line

Setup: bare-remote, fake `gh`, pull request `#57` merges cleanly, the delegated `release --merged` path then fails

```console
$ aco land 57
2> ERROR: MERGED pull request #57 as <sha>; follow-up incomplete: release (forge unreachable); re-run aco land 57
exit 2
```

### E-LANDCMD-18 — a rerun resumes without a second merge

Setup: bare-remote, fake `gh`, pull request `#57` already merged (LANDCMD-15's own scenario, resumed)

```console
$ aco land 57
RELEASED issue #42: <claim-id>
freed: none
next: none
exit 0
```

### E-LANDCMD-18a — a rerun after a completed release finishes, naming where the lane lives

Setup: bare-remote, fake `gh`, pull request `#57` from `ada/issue-42` merged and released by an
earlier `aco land 57` from this landing clone, which holds no worktree; the lane worktree on
`ada/issue-42` lives in the original checkout

```console
$ aco land 57
LANDED pull request #57 already; nothing left to release
worktree: kept -- no linked worktree on ada/issue-42 in this checkout; run aco release 42 --merged 57 --branch ada/issue-42 in the checkout that holds it
exit 0
```

That `aco release` line, run in the original checkout, prints REL-49's line and `worktree: removed`
(E-REL-24).
