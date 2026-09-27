# `aco start`

`aco start <item> [--scope PATH]... [--slug SLUG] [--whole REASON] [--out-of-order REASON]`
replaces the four-line dance a dispatcher used to type by hand -- `git fetch`, `git worktree
add ../<repo>-worktrees/issue-<n>-<slug> -b <prefix>/issue-<n>-<slug>`, `cd`, `aco claim <n>` --
with one command. This file owns the worktree/branch it builds or resumes, the slug and prefix
it derives, its own refusals, and how it mints or reuses the claim id underneath. It never
restates `aco claim`'s own preconditions, checks, or `CLAIMED ...`/cost-line grammar
(`specs/claim.spec.md`, `specs/body-block.spec.md`): `start` acquires that same claim, inside the
worktree, through the unchanged claim machinery, and cites those files' IDs rather than repeating
them. `<n>` is the claimed item number, a printed `#<n>` its `storage = "github"` form (PIN-30), `<slug>` the derived or given slug, `<prefix>` the derived
branch prefix, `<claim-id>` the acquired claim's own id.

## Behavior table

| state \ trigger | `aco start <n>` |
|---|---|
| target closed or missing | START-07 |
| no worktree yet at the computed path | START-01, START-03 |
| `--slug` given, matching the derived shape | START-01 |
| `--slug` given, not matching the derived shape | START-12 |
| no usable slug can be derived and `--slug` is omitted | START-02 |
| no identity signal resolves a prefix | START-03 |
| `--scope`/`--whole`/`--out-of-order` given or omitted | START-04 |
| a worktree already sits at the computed path, clean, same branch, a live claim already on it | START-06 |
| a worktree already sits at the computed path, clean, same branch, no live claim on it | START-11 |
| a worktree already sits at the computed path, dirty | START-10 |
| the computed branch name is already taken elsewhere | START-08 |
| a worktree at the computed path sits on a different branch | START-09 |
| a worktree at the computed path belongs to a foreign checkout | START-13 |
| the built branch name is unsafe as a git ref | START-14 |
| a non-worktree directory already sits at the computed path | START-15 |
| a live claim on the target is held by a different agent or branch | START-16 |
| a clean resume's own `--scope` differs from the live claim's stored scope | START-17 |
| the claim's own checks refuse | START-22 |
| the claim is refused after this call built the worktree | START-18 |
| the trunk moved after the checks, under a build or a gone-worktree rebuild | START-26 |
| the store cannot tell whether the claim's sent push landed | START-25 |
| git will not delete the branch a refused `start` built | START-21 |
| git will not remove the worktree a refused `start` built | START-23 |
| run from a linked worktree | START-19 |
| run from a linked worktree whose git directory names no checkout | START-24 |
| run inside the item's own lane worktree, clean or dirty | START-20 |
| every case | START-05 |

## Creating or resuming the worktree

The slug is `--slug` when given, else the item's own title lowercased with every run of
non-`[a-z0-9]` characters collapsed to one `-`, at most 40 characters, no leading or trailing
`-`. The branch prefix is the acting identity, read the same way the acting agent's own identity
is read elsewhere, but rendered short: the first word of `ACO_AGENT`, lowercased, when set; else
`grok` from a non-empty `GROK_SESSION_ID`; else `claude` from a non-empty `CLAUDE_CODE_SESSION_ID`.

The claim id is never derived from the item number: a fresh build always mints one exactly as a
bare `aco claim` would, and a clean resume looks the live claim up by identity and branch, the
same lookup `status`/`release` already use, and reprints that same id rather than minting or
computing a second one. A prior claim that ended -- released, abandoned, or the item's own
merge -- leaves no live claim behind, so the next `start` on that same clean worktree mints a
brand-new id, never a stale or deterministic per-item one.

`start` checks, then builds: target, slug, prefix, and the claim store are refused before any git
write. The item, the store, and every check the claim makes read the main checkout's own board
configuration, never that of a lane worktree `start` runs or claims in. Where no worktree stands
yet, `start` fetches the trunk and runs every check the claim itself makes -- scope, container,
body, a broken item, priority or `--out-of-order`, width, a claim already held -- against that one
fetched commit, then builds the worktree from the trunk: such a refusal builds nothing (START-22).
Only a refusal of the claim after the build -- another fetch moved the trunk so the worktree stands
on a commit the checks never saw, another claim landed after the checks, the store could not be
reached or saw every push rejected -- or between a live claim's gone-worktree rebuild and its
reprint, when another fetch moved the trunk under that rebuild (START-26), removes what this call
built (START-18). Once the claim's push was sent, only the store knows whether it was written: when
the store cannot tell, nothing is removed and the outcome is named uncertain (START-25). An
interrupt removes nothing.

- [ ] [START-01] No worktree yet at `../<repo>-worktrees/issue-<n>-<slug>`: fetch, create it on `<prefix>/issue-<n>-<slug>` from the trunk, claim it, print `worktree:`/`branch:` (see E-START-01).
- [ ] [START-02] A title with no usable slug, `--slug` omitted, refuses `no usable slug in this item's title: pass --slug explicitly`, exit 2.
- [ ] [START-03] No identity resolves a prefix: refuses `branch prefix is required: set ACO_AGENT, GROK_SESSION_ID, or CLAUDE_CODE_SESSION_ID`, exit 2; an unusable one, `claim`'s own sentence, before any worktree.
- [ ] [START-04] `--scope`/`--whole`/`--out-of-order` pass through verbatim to the claim `start` acquires, exactly as `aco claim <n>` reads them (CLM-06..CLM-18, CLAIM-53..CLAIM-55).
- [ ] [START-05] `start` never changes the caller's own working directory: it stands wherever it started once `start` returns, whatever worktree it just built or claimed in.
- [ ] [START-06] A worktree already at the computed path, clean, same branch, with a live claim already on it: looks it up by identity/branch and reprints it verbatim, never minting a second id (see E-START-02).
- [ ] [START-11] The same clean resume with no live claim (released, abandoned, or reopened after merge) mints a fresh id through the ordinary claim path, exactly as a first build would (see E-START-06).
- [ ] [START-12] An explicit `--slug` not matching the shape a derived slug would always produce refuses `--slug must be <rule>`, exit 2, before any worktree or branch is touched (see E-START-04).
- [ ] [START-19] `<repo>` is the caller's checkout if main; from a linked worktree, the one `core.worktree` or a `.git` common directory names; never nested under it (see E-START-12).
- [ ] [START-24] Run in a linked worktree whose git directory names no checkout refuses `main checkout unknown: git directory <dir> names no checkout; run start from the main checkout`, exit 2.
- [ ] [START-20] Run inside a linked worktree on the live claim's branch, `start` reprints that claim as START-06 does, clean or dirty, whatever slug the path carries; exit 0 (see E-START-13).

## Refusing a target, a collision, or a dirty resume

- [ ] [START-07] A closed target refuses `issue #<n> is closed`; a missing one refuses `issue #<n> does not exist here`; exit 2, before any worktree or branch (see E-START-03).
- [ ] [START-08] The branch name already taken elsewhere refuses `branch '<branch>' already exists and is not this item's worktree; remove it, or pass --slug to choose a different worktree`, exit 2.
- [ ] [START-09] A worktree at the computed path on a different branch refuses `worktree <path> exists on branch '<other>', not '<branch>'; remove it, or pass --slug to choose a different worktree`, exit 2.
- [ ] [START-10] A worktree at the computed path with uncommitted changes refuses `worktree <path> is dirty: <paths>; commit or clean it before resuming`, exit 2 (paths named as CLM-05 names them).
- [ ] [START-13] A worktree at the computed path that is not this repository's own -- a foreign root, this repository's own main checkout, or a different repository's worktree -- refuses by name (see E-START-05).
- [ ] [START-14] An unsafe branch prefix refuses `agent identity '<prefix>' is not usable in a branch name: '<branch>' is not a safe Git ref`, exit `2`, before any git write (see E-START-07).
- [ ] [START-15] Something other than a git worktree already sitting at the computed path refuses `path exists and is not a worktree of this repository`, exit `2` (see E-START-08).
- [ ] [START-16] A live claim on the target held by a different agent or branch is never silently resumed: it falls through to the ordinary claim path, refused by CLAIM-11's own sentence (see E-START-09).
- [ ] [START-17] A resume's own explicit `--scope` disagreeing with the live claim's stored scope refuses `live claim scope differs; release it first`, exit `2` (see E-START-10).
- [ ] [START-22] A refusal of the claim's own checks comes before the build: no `worktree:`/`branch:` line, no worktree, no branch; exit 2 (see E-START-11).
- [ ] [START-18] A claim refused after the build, a rejected push too, or a moved trunk (START-26), removes both, adding `removed worktree <path> and branch '<branch>' this start created`; exit 2 (see E-START-15).
- [ ] [START-26] A build or gone-worktree rebuild standing on a trunk moved after the checks refuses `the trunk moved after start checked it; run start again`, exit 2, then removes it as START-18 says.
- [ ] [START-25] A sent push the store cannot judge keeps both, adding `the claim's push was sent, its outcome unknown; worktree <path> and branch '<branch>' kept; run start again to resume it`; exit 2.
- [ ] [START-21] When git will not delete that branch, the line reads `removed worktree <path> this start created; branch '<branch>' kept: <reason>` instead (see E-START-14).
- [ ] [START-23] When git will not remove that worktree, the refusal and exit 2 stay and the line reads `worktree <path> and branch '<branch>' this start created kept: git failure: <reason>`.

## Never

- `start` never launches a second, competing way to run git: every worktree it builds, resumes, or refuses goes through the one git launcher every other command in this package already uses.
- `start` never overwrites, deletes, or reuses a worktree sitting on a different branch than the one it computed (START-09), or one that belongs to a foreign checkout (START-13): it refuses by name and leaves that worktree exactly as found.
- `start` never derives the claimed scope independently of `aco claim`'s own body-scope resolution: an explicit `--scope` that disagrees with the item's own body still refuses the same way (CLAIM-54).
- `start` never writes `refs/aco/state` itself: its one claim write is `aco claim`'s own, made after `aco claim`'s own checks passed.
- `start` never removes a worktree or branch it did not build in the same call: a refused resume leaves the worktree it found exactly as found.
- `start` never reuses a prior, now-terminal claim id for the same item: a clean resume with no live claim mints a fresh one (START-11), exactly as a first build would.
- `start` never gains a `--json` mode: its own parser defines no such flag, so every outcome, success or refusal, is `worktree:`/`branch:` text or a stderr sentence (own product decision).

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local bare repository with
`main` at one commit, a git identity, `origin/HEAD`, a tracked `.agent-claim/board.toml`, and
`ACO_AGENT` set to `Ada`. A session below also names a fixed, deterministic fake `gh` as a setup
precondition (the shape `specs/landing-grammar.spec.md` already uses) for reading the item's own
title and body.

### E-START-01 -- a fresh item gets a worktree, a branch, and a claim in one call

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` open, title `Fresh Slug`, body `scope = ["src/x.py"]`

```console
$ aco start 314
worktree: /work/agent-coordination-worktrees/issue-314-fresh-slug
branch: ada/issue-314-fresh-slug
CLAIMED issue #314: <claim-id>
1 of 6 versioned files (17%); overlaps no other open claims
exit 0
```

### E-START-02 -- a second call resumes the live claim by lookup, same id and all

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` as above, already `aco start 314`

```console
$ aco start 314
worktree: /work/agent-coordination-worktrees/issue-314-fresh-slug
branch: ada/issue-314-fresh-slug
CLAIMED issue #314: <claim-id>
1 of 6 versioned files (17%); overlaps no other open claims
exit 0
```

`<claim-id>` is the identical id E-START-01 minted: this call never mints a second one.

### E-START-03 -- a closed item refuses before anything is built

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` closed

```console
$ aco start 314
2> ERROR: issue #314 is closed
exit 2
```

### E-START-04 -- an explicit `--slug` failing the derived shape refuses before any worktree

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` open

```console
$ aco start 314 --slug Bad_Slug
2> ERROR: --slug must be lowercase letters, digits, and single '-' separators only, at most 40 characters, never leading or trailing '-'
exit 2
```

### E-START-05 -- a worktree at the computed path belongs to a different repository

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` open, `/work/agent-coordination-worktrees/issue-314-fresh-slug`
already a linked worktree of an unrelated repository, on branch `ada/issue-314-fresh-slug`

```console
$ aco start 314
2> ERROR: worktree /work/agent-coordination-worktrees/issue-314-fresh-slug belongs to a different repository; remove it, or pass --slug to choose a different worktree
exit 2
```

### E-START-06 -- a clean resume with no live claim mints a fresh id, never a stale one

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` as E-START-01, already `aco start 314` then
`aco release 314 --abandoned "stopped for the day"` -- the worktree stands, untouched, with no
live claim on it

```console
$ aco start 314
worktree: /work/agent-coordination-worktrees/issue-314-fresh-slug
branch: ada/issue-314-fresh-slug
CLAIMED issue #314: <fresh-claim-id>
1 of 6 versioned files (17%); overlaps no other open claims
exit 0
```

`<fresh-claim-id>` differs from any id minted for #314 before it.

### E-START-07 -- an unsafe identity prefix refuses before any worktree

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` open, title `Fresh Slug`, `ACO_AGENT` set to `-bad`

```console
$ aco start 314
2> ERROR: agent identity '-bad' is not usable in a branch name: '-bad/issue-314-fresh-slug' is not a safe Git ref
exit 2
```

### E-START-08 -- something other than a worktree already sits at the computed path

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` open, title `Fresh Slug`,
`/work/agent-coordination-worktrees/issue-314-fresh-slug` already a plain directory, not a git worktree

```console
$ aco start 314
2> ERROR: path exists and is not a worktree of this repository
exit 2
```

### E-START-09 -- a live claim held by a different agent is never silently resumed

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` as E-START-01, a live claim on `#314`
held by `Grok sess-9` on branch `grok/issue-314-other`

```console
$ aco start 314
2> ERROR: issue #314 is claimed by Grok sess-9 (builder) on issue #314 branch grok/issue-314-other
exit 2
```

### E-START-10 -- resuming with a `--scope` that disagrees with the live claim refuses

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` as E-START-01, already `aco start 314`

```console
$ aco start 314 --scope src/other.py
2> ERROR: live claim scope differs; release it first
exit 2
```

### E-START-11 -- a claim the checks refuse builds nothing

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` open, title `Fresh Slug`, a body naming no
`scope`

```console
$ aco start 314
2> ERROR: item names no scope; pass --scope
exit 2
```

`git worktree list` and `git branch --list` read afterwards exactly as before the call.

### E-START-12 -- a call from a linked worktree builds beside the main checkout

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` as E-START-01, run from the linked
worktree `/work/agent-coordination-worktrees/issue-9-other` of the main checkout
`/work/agent-coordination`

```console
$ aco start 314
worktree: /work/agent-coordination-worktrees/issue-314-fresh-slug
branch: ada/issue-314-fresh-slug
CLAIMED issue #314: <claim-id>
1 of 6 versioned files (17%); overlaps no other open claims
exit 0
```

### E-START-13 -- a call inside the item's own lane worktree reprints its live claim

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` as E-START-01, already
`aco start 314 --slug own-lane`, run from `/work/agent-coordination-worktrees/issue-314-own-lane`
with uncommitted work in it

```console
$ aco start 314
worktree: /work/agent-coordination-worktrees/issue-314-own-lane
branch: ada/issue-314-own-lane
CLAIMED issue #314: <claim-id>
1 of 6 versioned files (17%); overlaps no other open claims
exit 0
```

`<claim-id>` is the id the first call minted; no second worktree or branch is built.

### E-START-14 -- git keeps the branch a refused call built

Setup: as E-START-15, and `git branch -d ada/issue-314-fresh-slug` refuses `error: branch not fully merged`

```console
$ aco start 314
worktree: /work/agent-coordination-worktrees/issue-314-fresh-slug
branch: ada/issue-314-fresh-slug
2> ERROR: issue #314 is claimed by Grok sess-9 (builder) on issue #314 branch grok/issue-314-other
2> removed worktree /work/agent-coordination-worktrees/issue-314-fresh-slug this start created; branch 'ada/issue-314-fresh-slug' kept: git failure: error: branch not fully merged
exit 2
```

### E-START-15 -- a claim that lands after the checks makes the call remove its build

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` as E-START-01; `Grok sess-9` claims `#314`
on branch `grok/issue-314-other` after this call's checks passed and before its claim's push is sent

```console
$ aco start 314
worktree: /work/agent-coordination-worktrees/issue-314-fresh-slug
branch: ada/issue-314-fresh-slug
2> ERROR: issue #314 is claimed by Grok sess-9 (builder) on issue #314 branch grok/issue-314-other
2> removed worktree /work/agent-coordination-worktrees/issue-314-fresh-slug and branch 'ada/issue-314-fresh-slug' this start created
exit 2
```
