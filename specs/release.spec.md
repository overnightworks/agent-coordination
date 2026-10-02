# Release

`aco release` ends one live claim, `--merged <pr|sha|empty>` or `--abandoned
REASON`, and reports what that ending changed. This file owns the command's
own flags, its identity/branch resolution, which live claim it selects, its
`RELEASED ...` line and `--json` shape, and when the shared `freed`/`next`/
`hint` facts appear at all. It never restates what a `--merged` release
verifies against the pull request or, under `storage = "state-ref"`, the
trunk walk (`specs/landing-grammar.spec.md`, `## What release --merged
requires`), the exact `freed`/`next` line and `--json` shapes
(`specs/landing-grammar.spec.md` LAND-49), the missing-state-ref sentence
(`specs/ref-store-cas.spec.md` CAS-03), or a claimant refusal
(`specs/claim-record.spec.md` CLAIM-16, CLAIM-17, CLAIM-38..CLAIM-40, beyond REL-12's repeat) --
each is cited by ID.

`<claim-id>` is the released claim's own id. `<subject>` is the same unquoted
grammar `CLAIMED`/`RESCOPED` already print: `issue <label>` or `lane <branch>`
(`<label>` is
`specs/landing-grammar.spec.md`'s own convention -- `#<n>` under
`storage = "github"`, `aco-xxxxxx` under `storage = "state-ref"`).
`<identity>`, printed only by the claim-selection refusals below (REL-09, REL-48, REL-53), differs only
in quoting the lane's branch: `issue <label>` or `lane '<branch>'`. A refusal reaching the shared collection point prints
`ERROR: <sentence>` on stderr and exits `2`, exactly as
`specs/claim-record.spec.md` already documents.

## Behavior table

| state \ trigger | `aco release` (either outcome) | `--merged <pr\|sha\|empty>` | `--abandoned REASON` |
|---|---|---|---|
| neither or both outcome flags given | REL-01 | REL-01 | REL-01 |
| `REASON` blank, padded, multiline, or over 512 characters | — | — | REL-02 |
| lane mode, branch not `docs/`/`fix/` | REL-03 | REL-03 | REL-03 |
| explicit `--branch` | REL-04 | REL-04 | REL-04 |
| `--branch` omitted, issue + `--claim-id` both given | REL-05 | REL-05 | REL-05 |
| `--branch` omitted, no issue, empty checkout branch | REL-06 | REL-06 | REL-06 |
| `--branch` omitted, issue without `--claim-id`, empty checkout branch | REL-07 | REL-07 | REL-07 |
| `--coordinator-override` without `--role coordinator` | REL-08 | REL-08 | REL-08 |
| identity/branch resolve to no live claim | REL-09 | REL-09, REL-54 | REL-09 |
| no live claim on the identity, no `--claim-id`, the landing verified | — | REL-47, REL-49..REL-51 | — |
| a live claim off the landing's own branch | — | REL-48, REL-56 | — |
| a live claim on that branch opened from a trunk already holding the landing | — | REL-53 | — |
| `--claim-id` mismatches the resolved claim | REL-10 | REL-10 | REL-10 |
| `--branch` and `--claim-id` disagree | REL-11 | REL-11 | REL-11 |
| wrong claimant, no override | REL-12 (CLAIM-38) | REL-12 | REL-12 |
| `--coordinator-override --role coordinator` | REL-13 (CLAIM-40) | REL-13 | REL-13 |
| `--role` omitted | REL-14 | REL-14 | REL-14 |
| `refs/aco/state` not yet bootstrapped | REL-15 (CAS-03) | REL-15 | REL-15 |
| pull request verification | — | LAND-29..39, 49, 50, 55, 62/63/64 | — |
| `storage = "state-ref"` trunk verification | — | REL-17 (LAND-47, LAND-52, LAND-56, LAND-59) | — |
| released item's own body contract | REL-23 | REL-23 | REL-23 |
| successful release, text output | REL-18 | REL-18 | REL-18 |
| successful release, `--json` | REL-19, REL-36 | REL-19, REL-35, REL-36 | REL-19, REL-36 |
| landing board read resolves | — | REL-20 | — |
| the `next:` item's title holds a display control | — | REL-40 | — |
| landing board read hits an unreachable forge | — | REL-22 | — |
| no landing to report | — | — | REL-21 |
| any refusal past the parser, with `--json` | REL-24 | REL-24 | REL-24 |
| a successful outcome's own worktree/branch cleanup | — | REL-25..REL-32, REL-34, REL-42..REL-46, REL-52, REL-55 | REL-33 |

## Flags and outcome

- [ ] [REL-01] `aco release` with neither `--merged PULL_REQUEST` nor `--abandoned REASON`, or with both, is refused by the parser itself before anything runs, exit `2`; with `--json`, through OUT-06.
- [ ] [REL-02] An `--abandoned` value blank, padded, with a control character, multiline, or over 512 characters refuses `abandoned reason must be one bounded non-empty line`, exit `2`.

## Identity and branch resolution

Omitting the positional issue number and an explicit `--branch` both select
lane mode the same way `aco claim`'s own lane mode does; a future `claim`
spec would cite REL-03 rather than restate it.

- [ ] [REL-03] A lane-mode branch not prefixed `docs/` or `fix/` refuses `branch '<branch>' is not an issueless lane; pass an issue number, or check out a branch prefixed 'docs/' or 'fix/'`, exit `2`.
- [ ] [REL-04] An explicit `--branch <branch>` selects that claim by name and never reads the checkout's current branch at all (see E-REL-01).
- [ ] [REL-05] `--branch` omitted together with an issue number and `--claim-id` skips reading the checkout branch entirely, the same as an explicit `--branch` would.
- [ ] [REL-06] `--branch` omitted, no issue, empty branch refuses `lane release requires a non-empty current branch; check out the docs/ or fix/ lane branch, or pass an issue number`, exit `2`.
- [ ] [REL-07] `--branch` omitted, issue given without `--claim-id`, empty current branch refuses `release without --claim-id requires a non-empty current branch; pass --claim-id`, exit `2`.
- [ ] [REL-08] `--coordinator-override` without `--role coordinator` refuses (CLAIM-39's sentence) before the checkout branch, git, or the forge are ever read.

## Claim selection

- [ ] [REL-09] An identity/branch pair with no matching live claim refuses `<identity> has no active build claim`, exit `2`, unless REL-47 applies.
- [ ] [REL-10] A `--claim-id` mismatching the one claim already resolved refuses that same `has no active build claim` sentence: never a second selector among several claims.
- [ ] [REL-11] `--branch` and `--claim-id` naming different branches refuses, quoting both and the claim's own branch, exit `2` (see E-REL-04).
- [ ] [REL-12] A `release` by the wrong agent/role, no coordinator override, refuses before any write, naming the holder's `<repeat>` (REL-41) before the override (see E-REL-19).
- [ ] [REL-41] `<repeat>` is `aco release`, the item, then as given `--branch`, `--claim-id`, the outcome flag, `--keep-worktree`, `--json`, then `--agent <holder>`, `--role <holder role>` if the roles differ.
- [ ] [REL-13] `--coordinator-override --role coordinator` releases a foreign claim with no agent/role match (CLAIM-40's outcome, for release specifically).
- [ ] [REL-14] Omitting `--role` -- unlike `claim`'s own default `builder` -- reports the claim's own stored role, in text and in `--json` alike.
- [ ] [REL-15] A release before `aco bootstrap` has created `refs/aco/state` refuses (CAS-03's sentence), before any transition is attempted.

## A `--merged` release with nothing left to release

A rerun of a landing whose release already succeeded -- `aco land`'s own
rerun, or REL-52's `<rerun>` from the checkout that holds the lane -- finds
no live claim. Its trigger is that missing claim, never a closed item:
GitHub closes the item at merge time through `Closes #<n>`.

- [ ] [REL-47] A `--merged` release without `--claim-id` whose identity has no live claim, once its landing verifies (REL-16, REL-17), closes nothing, releases nothing, and exits `0` (see E-REL-24).
- [ ] [REL-54] An issue-less lane's pull request merged from a branch other than the release's own is no such rerun: REL-09 refuses.
- [ ] [REL-49] Its text is `LANDED <landing> already; nothing left to release`, `<landing>` `pull request #<n>` or, under `storage = "state-ref"`, `commit <sha>`, then its `worktree:` line.
- [ ] [REL-50] Its cleanup (REL-25..REL-34) acts on the pull request's own source branch, or under `storage = "state-ref"` on the release's own branch (REL-04, REL-06).
- [ ] [REL-51] Its `--json` prints `specs/output.spec.md`'s envelope, `reason` `merged`, then `outcome` `"nothing left to release"`, `issue`, `lane`, `branch`, `worktree`.
- [ ] [REL-55] It reads the claim state afresh right before that cleanup; a claim on the identity found then keeps both: `worktree: kept -- claimed again while this release ran`.

Accepted residual: a claim taken between that read and the removal is not seen; the worktree
removed is clean and merged into the default branch, so nothing is lost, and `aco start` builds it
again for that claim.

- [ ] [REL-48] A live claim off the landing's branch refuses `<identity> is claimed on '<branch>', not on <landing>'s branch '<source>'; release that claim by itself`, exit `2` (see E-REL-25, E-REL-26).
- [ ] [REL-56] `<landing>` is REL-49's; `<source>` is the pull request's source branch or, under `storage = "state-ref"`, the release's own branch (REL-04, REL-07), with REL-05 the claim's own.
- [ ] [REL-53] A claim on that branch based on the landing commit or a later trunk commit (START-01) refuses `<identity> was claimed on '<branch>' after <landing> landed; release that claim by itself`.

Both storages judge REL-48 and REL-53 alike, before anything is written, closed or removed.

Residual, owned by #310 finding 356: a same-branch claim based off the trunk, such as START-11 or
`aco claim` in a lane worktree still standing, passes both REL-48 and REL-53; a rerun releases that
claim and closes its reopened item.

## What a `--merged` release verifies and never checks

- [ ] [REL-16] `release --merged <pr>`'s forge verification runs before any write; it is `specs/landing-grammar.spec.md`'s grammar (LAND-29..39, LAND-49/50, LAND-55, LAND-62..64, LAND-66/67), nothing added.
- [ ] [REL-37] Under `storage = "github"`, that verification walks `<remote>/<branch>` for the forge's default branch `<branch>` once the canonical remote `<remote>` is fetched, as `aco land` does.
- [ ] [REL-38] Under `storage = "github"`, the release's board report and its worktree cleanup (REL-25, REL-29) walk that same `<remote>/<branch>`.
- [ ] [REL-39] A canonical `<remote>` with no URL configured refuses before any write: `cannot determine the trunk: canonical remote '<remote>' is not configured`, exit `2` (see E-REL-18).
- [ ] [REL-17] `release --merged` under `storage = "state-ref"` reads `<sha|empty>` against the local trunk walk, never a pull request or forge (LAND-47, LAND-52, LAND-56, LAND-59, `specs/landing-grammar.spec.md`).
- [ ] [REL-23] `release` never reads the released item's own body contract, unlike issue-mode `claim` (`specs/body-block.spec.md` BODY-52).

## The `RELEASED` line and `--json`

`specs/landing-grammar.spec.md` LAND-49 owns `freed`/`next`'s exact shape;
this file owns only when they appear at all. A release that releases a claim
follows this section; one with nothing left to release (REL-47) prints no
`RELEASED` line, no `claim_id`, `agent` or `role`, and REL-49..REL-51's shape
instead.

- [ ] [REL-18] Without `--json`, a successful release of a claim always prints `RELEASED <subject>: <claim-id>` first; `--abandoned`, that line is the whole output (see E-REL-01).
- [ ] [REL-19] `--json` on a success prints `specs/output.spec.md`'s envelope, `reason` `merged`/`abandoned`, then `outcome`, `issue`, `lane`, `branch`, `claim_id`, `agent`, `role` (see E-REL-05).
- [ ] [REL-36] `outcome` is `"merged #<n>"`, `"abandoned: <explanation>"`, `"landed <sha>"` (`storage = "state-ref"`), or REL-51's -- `reason` still reads `merged` for the last two.
- [ ] [REL-35] A `--merged` release's `--json` object also carries `worktree`, the identical text its printed `worktree:` line shows (REL-25..REL-34), present only for `--merged` (see E-REL-17).
- [ ] [REL-20] A resolved `--merged` landing adds LAND-49's `freed:`/`next:` lines after `RELEASED` in text, or its keys to `--json`, present only then (see E-REL-02).
- [ ] [REL-40] The text `next:` line shows `<title>` with each display control escaped (NEXT-37): `a\u202eb` prints as typed, `Größe` and TAB as they are.
- [ ] [REL-21] `--abandoned` never resolves the forge, reads the board, or prints `freed`/`next`/`hint` (LAND-39); its `--json` object carries neither key.
- [ ] [REL-22] A `--merged` release whose post-commit board read fails prints LAND-38's `hint:` line, on stdout in text or stderr with `--json`; `freed`/`next` omitted (LAND-50) (see E-REL-06).
- [ ] [REL-24] A release refusal past the parser (every ID but REL-01) prints `specs/output.spec.md`'s envelope, `reason` `precondition_failed`, exit `2`; REL-06..08 through OUT-05 (see E-REL-07).

## `--merged`'s own worktree/branch cleanup

A successful `--merged` outcome (issue #322) removes the lane's own linked worktree and local
branch when both are safe to remove -- after the release's own store transition already
committed, so a cleanup problem never turns a released claim back into a live one. The remote
branch stays the forge merge's own business: only the local worktree and the local branch move
here, never anything on `remote`. Every outcome is loud: exactly one `worktree: <outcome>` line
follows the report in text, and the same text becomes `--json`'s own `worktree` value -- `removed`
when both are gone, `kept -- <reason>` when neither moves, `removed; branch kept -- <reason>`
when the worktree is gone but the branch delete itself failed (REL-34),
`removed; branch unknown -- <reason>` when that delete timed out (REL-45), or
`removed; branch.<name> section kept -- <reason>` when both are gone but a squashed branch's own
section stayed (REL-42) -- one owner for all five shapes so they can never drift apart. Accepted residual of REL-42: a same-name branch another process
creates between its compare-and-delete and the section removal can lose its upstream setting,
never a commit; `git branch -u` restores it.

- [ ] [REL-25] A clean linked worktree whose branch is in the canonical remote's trunk, or whose tip is the merged pull request's recorded head, goes with that local branch: `worktree: removed` (see E-REL-08).
- [ ] [REL-42] A squashed branch goes by compare-and-delete on its recorded head, a moved one stays (REL-34); its section goes only while no such branch exists, and a refused removal reads `section kept`.
- [ ] [REL-43] A rerun of `aco land` or `release --merged` after a squash judges the lane by that recorded head too: a clean lane on it reads `worktree: removed`, never `kept` (see E-REL-20).
- [ ] [REL-44] A value under `branch.*` in git config that is no UTF-8 never stops the squashed branch's section removal: only key names are read (see E-REL-21).
- [ ] [REL-26] `--keep-worktree` skips that removal outright: `worktree: kept -- --keep-worktree was given`, worktree and branch both left exactly as found (see E-REL-09).
- [ ] [REL-27] A release run from inside the lane's own worktree cannot remove its own cwd: `worktree: kept -- release ran from inside it`, and keeps both (see E-REL-10).
- [ ] [REL-28] A dirty worktree keeps it: `worktree: kept -- dirty`, exit code unaffected (see E-REL-11).
- [ ] [REL-29] A branch not yet provably merged into the default branch keeps it: `worktree: kept -- not merged into the default branch` (see E-REL-12).
- [ ] [REL-30] No linked worktree on that branch here reads `worktree: kept -- no linked worktree on <branch> in this checkout; run <rerun> in the checkout that holds it` (see E-REL-13).
- [ ] [REL-52] `<rerun>` is `aco release`, the item (none for a lane), `--merged` with the pull request or the verified `<sha>`, `--branch <branch>`; there it meets REL-47 (see E-REL-24).
- [ ] [REL-31] The branch checked out on this repository's own shared main checkout, not a linked worktree, keeps it: `worktree: kept -- branch checked out elsewhere` (see E-REL-14).
- [ ] [REL-32] A git failure resolving which worktree matches the lane's branch keeps both and reports it: `worktree: kept -- git failure: <detail>`, the release itself stays committed regardless (see E-REL-15).
- [ ] [REL-33] `--abandoned` never attempts this cleanup at all, the same as it never resolves a forge target (LAND-39).
- [ ] [REL-34] A git failure deleting the branch after the worktree is removed, by exit or launch, merged or squashed, names both halves: `worktree: removed; branch kept -- git failure: <detail>` (see E-REL-16).
- [ ] [REL-45] A delete git that timed out after the worktree is removed may have deleted it: `worktree: removed; branch unknown -- git <subcommand> timed out; check git branch --list <branch>` (see E-REL-22).
- [ ] [REL-46] A git step that timed out names itself wherever its `<detail>` stands: `git <subcommand> timed out`, never a step it did not run (see E-REL-23).

## Never

- `release` never reads or checks the released item's own body contract (REL-23).
- `--abandoned` never resolves a forge target, reads the board, or prints `freed`/`next`/`hint`, ever (LAND-39); nor does it ever remove a worktree or branch (REL-33).
- `--claim-id` is never a second selector among several live claims: at most one claim is ever live per identity (`specs/claim-record.spec.md`), so it only ever confirms or refuses the one claim identity/branch resolution already found.
- A `--branch`/`--claim-id` disagreement is never silently resolved by preferring one: REL-11 refuses instead.
- A forge outage discovered after the release's own store transition already committed never undoes or fails that transition (LAND-50): the claim stays released regardless of whether `freed`/`next` could be reported.
- A worktree/branch cleanup problem after that same commit never undoes or fails it either (REL-28..REL-32, REL-34): the claim stays released regardless of whether cleanup removed anything.
- Cleanup never touches the remote branch a forge merge already owns: only the local worktree and local branch are ever removed.
- A release with nothing left to release never closes an item or writes the claim state (REL-47); a claim REL-48 or REL-53 names is never released and never skipped.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`,
and `ACO_AGENT` set to `Ada`; `<remote>`, `<tmp>` and `<home>` are the
runner's own paths. A session for `--merged` also names a fixed,
deterministic fake `gh` as a setup precondition (the shape
`specs/landing-grammar.spec.md` already uses); an `--abandoned` session
needs no fake `gh` at all (REL-21).

### E-REL-01 — an abandoned lane release, no forge in sight

Setup: bare-remote, bootstrapped, a linked worktree on `docs/tidy-readme`

```console
$ aco claim --scope README.md
CLAIMED lane docs/tidy-readme: <claim-id>
1 of 6 versioned files (17%); overlaps no other open claims
exit 0
$ aco release --abandoned "stopped for the day"
RELEASED lane docs/tidy-readme: <claim-id>
exit 0
```

### E-REL-02 — a merged issue release, closed and unblocking nothing else

Setup: bare-remote, fake `gh`, a linked worktree on `ada/issue-42`, pull
request `#57` merged into `main`, body `Work-Item: #42\n\nCloses #42`, its
own merge commit's trailer also naming `Work-Item: #42`, issue `#42` claimed
and closed on the forge, no other open board items, run from inside that
same worktree

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: kept -- release ran from inside it
exit 0
```

### E-REL-03 — no live claim to release

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, no live claim on issue `#42`

```console
$ aco release 42 --abandoned stopped
2> ERROR: issue #42 has no active build claim
exit 2
```

### E-REL-04 — `--branch` and `--claim-id` disagree

Setup: bare-remote, bootstrapped, a live claim on issue `#42`, claim id
`mine`, branch `ada/issue-42`

```console
$ aco release 42 --branch ada/issue-99 --claim-id mine --abandoned stopped
2> ERROR: --branch 'ada/issue-99' and --claim-id 'mine' disagree: the claim's own branch is 'ada/issue-42'; drop --branch or pass its own value
exit 2
```

### E-REL-05 — `--json` on an abandoned release

Setup: bare-remote, bootstrapped, a live claim on issue `#42`, claim id
`mine`, agent `Ada`, role `builder`, branch `ada/issue-42`

```console
$ aco release 42 --claim-id mine --abandoned "stopped for the day" --json
{"ok": true, "reason": "abandoned", "outcome": "abandoned: stopped for the day", "issue": 42, "lane": null, "branch": "ada/issue-42", "claim_id": "mine", "agent": "Ada", "role": "builder"}
exit 0
```

### E-REL-06 — a merged release whose landing board read cannot reach the forge

Setup: bare-remote, a fake `gh` that returns the merged pull request, its
own merge commit's trailer naming `Work-Item: #42`, but fails the
landing-board read that follows it, a linked worktree on `ada/issue-42`,
already merged into `main`, run from the main checkout, issue `#42` claimed

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
hint: could not read the board to report what this write freed (<error>); run `aco board --json` once the forge is reachable
worktree: removed
exit 0
```

### E-REL-07 — `--json` on a refused release

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, no
live claim on issue `#42`

```console
$ aco release 42 --abandoned stopped --json
{"ok": false, "reason": "precondition_failed", "message": "issue #42 has no active build claim"}
2> ERROR: issue #42 has no active build claim
exit 2
```

### E-REL-08 — a merged release removes its own clean, merged lane worktree

Setup: bare-remote, fake `gh`, its merge commit's trailer naming `Work-Item: #42`, a linked
worktree `/work/agent-coordination-worktrees/issue-42-widget` on `ada/issue-42`, already merged into
`main`, run from the main checkout, issue `#42` claimed

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: removed
exit 0
```

`/work/agent-coordination-worktrees/issue-42-widget` and branch `ada/issue-42` are both gone afterward.

### E-REL-09 — `--keep-worktree` skips cleanup outright

Setup: bare-remote, fake `gh`, its merge commit's trailer naming `Work-Item: #42`, a linked
worktree `/work/agent-coordination-worktrees/issue-42-widget` on `ada/issue-42`, already merged into
`main`, run from the main checkout, issue `#42` claimed

```console
$ aco release 42 --merged 57 --keep-worktree
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: kept -- --keep-worktree was given
exit 0
```

`/work/agent-coordination-worktrees/issue-42-widget` and branch `ada/issue-42` both remain.

### E-REL-10 — running from inside the lane worktree keeps it, one line and all

Setup: bare-remote, fake `gh`, its merge commit's trailer naming `Work-Item: #42`, a linked
worktree on `ada/issue-42`, already merged into `main`, issue `#42` claimed, run from inside that
same worktree

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: kept -- release ran from inside it
exit 0
```

### E-REL-11 — a dirty worktree keeps it

Setup: bare-remote, fake `gh`, its merge commit's trailer naming `Work-Item: #42`, a linked
worktree on `ada/issue-42`, already merged into `main`, run from the main checkout, an
uncommitted file sitting in that worktree, issue `#42` claimed

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: kept -- dirty
exit 0
```

### E-REL-12 — a branch not yet locally merged keeps it

Setup: bare-remote, fake `gh` reporting pull request `#57` merged, its merge commit's trailer
naming `Work-Item: #42`, a linked worktree on `ada/issue-42` whose branch this checkout has not
itself merged into `main` yet and whose tip is not `#57`'s recorded head, run from the main
checkout, issue `#42` claimed

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: kept -- not merged into the default branch
exit 0
```

### E-REL-13 — no linked worktree ever built for the branch

Setup: bare-remote, fake `gh`, its merge commit's trailer naming `Work-Item: #42`, branch
`ada/issue-42` merged into `main` with no linked worktree of its own (the claim came from a
plain checkout), issue `#42` claimed

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: kept -- no linked worktree on ada/issue-42 in this checkout; run aco release 42 --merged 57 --branch ada/issue-42 in the checkout that holds it
exit 0
```

### E-REL-14 — the branch sits on this repository's own main checkout, not a linked worktree

Setup: bare-remote, fake `gh`, its merge commit's trailer naming `Work-Item: #42`, branch
`ada/issue-42` checked out on this repository's own main checkout rather than a linked
worktree, run from an unrelated linked worktree, issue `#42` claimed

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: kept -- branch checked out elsewhere
exit 0
```

### E-REL-15 — a git failure resolving the lane's own worktree keeps both

Setup: bare-remote, fake `gh`, its merge commit's trailer naming `Work-Item: #42`, a linked
worktree on `ada/issue-42`, already merged into `main`, run from the main checkout, issue `#42`
claimed, a git failure while resolving which of the repository's worktrees sits on
`ada/issue-42`

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: kept -- git failure: <detail>
exit 0
```

### E-REL-16 — a branch-deletion failure after the worktree is already gone names both halves

Setup: bare-remote, fake `gh`, its merge commit's trailer naming `Work-Item: #42`, a linked
worktree on `ada/issue-42`, already merged into `main`, run from the main checkout, issue `#42`
claimed, deleting the local branch fails once the worktree itself is already removed

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: removed; branch kept -- git failure: <detail>
exit 0
```

### E-REL-17 — `--json` on a merged release carries the same `worktree` text

Setup: bare-remote, fake `gh`, its merge commit's trailer naming `Work-Item: #42`, a linked
worktree on `ada/issue-42`, already merged into `main`, run from the main checkout, issue `#42`
claimed

```console
$ aco release 42 --merged 57 --json
{"ok": true, "reason": "merged", "outcome": "merged #42", "issue": 42, "lane": null, "branch": "ada/issue-42", "claim_id": "<claim-id>", "agent": "Ada", "role": "builder", "freed": [], "next": null, "parent_closable": null, "worktree": "removed"}
exit 0
```

### E-REL-18 — a canonical remote with no URL configured

Setup: as E-REL-17, but `.aco/board.toml` names `canonical_remote = "hub"`, which this
clone never added beside its `origin`; the same holds when `hub` has only a URL-less line such
as a local `remote.hub.fetch`

```console
$ aco release 42 --merged 57
2> ERROR: cannot determine the trunk: canonical remote 'hub' is not configured
exit 2
```

`git branch --list` reads afterwards exactly as before the call: `ada/issue-42` stands, the
claim on #42 still stands, and no landing comment closed #42.

### E-REL-19 — a claim taken under an explicit `--agent` names its repeat

Setup: bare-remote, fake `gh`, a live claim on issue `#42` taken with `--agent claude-head`, role `builder`; this session's `ACO_AGENT` is unset and its session id falls back to `Claude s-1`

```console
$ aco release 42 --merged 57
2> ERROR: only the original claimant may release; repeat as the holder with `aco release 42 --merged 57 --agent claude-head`, or use an explicit coordinator override (holder='claude-head (builder)', this session='Claude s-1 (builder)')
exit 2
```

### E-REL-20 — a rerun after a squash removes the lane on the recorded head

Setup: bare-remote, fake `gh`, pull request `#57` squashed into `main` with recorded head
`0a5d32f`, its first release failing after the merge, a clean linked worktree on `ada/issue-42`
at `0a5d32f`, run from the main checkout, issue `#42` claimed

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: removed
exit 0
```

### E-REL-21 — a non-UTF-8 branch description never stops the squash cleanup

Setup: E-REL-20's, plus `branch.other.description` set to the bytes `caf\xe9` in this
checkout's git config

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: removed
exit 0
```

### E-REL-22 — a branch delete that timed out reads unknown, never kept

Setup: bare-remote, fake `gh`, its merge commit's trailer naming `Work-Item: #42`, a linked
worktree on `ada/issue-42`, already merged into `main`, run from the main checkout, issue `#42`
claimed, `git branch -d` timing out once the worktree itself is already removed

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: removed; branch unknown -- git branch timed out; check git branch --list ada/issue-42
exit 0
```

### E-REL-23 — a timed-out git step names itself

Setup: E-REL-20's, the squashed branch deleted, then its section listing `git config` timing out

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: removed; branch.ada/issue-42 section kept -- git failure: git config timed out
exit 0
```

### E-REL-24 — a squashed, fully released pull request re-released where its lane lives

Setup: bare-remote, fake `gh`, pull request `#57` from `ada/issue-42` squashed into `main` with
recorded head `0a5d32f`, released already from a landing clone, so no live claim on issue `#42`;
this checkout holds a clean linked worktree on `ada/issue-42` at `0a5d32f`, run from its main
checkout

```console
$ aco release 42 --merged 57 --branch ada/issue-42
LANDED pull request #57 already; nothing left to release
worktree: removed
exit 0
```

Issue `#42` gets no second close and `refs/aco/state` does not move.

### E-REL-25 — a newer lane's claim on the same issue is never released by an old pull request

Setup: bare-remote, fake `gh`, pull request `#57` from `ada/issue-42` merged, its merge commit's
trailer naming `Work-Item: #42`, the live claim on issue `#42` now on `ada/issue-42-again`

```console
$ aco release 42 --merged 57 --branch ada/issue-42
2> ERROR: issue #42 is claimed on 'ada/issue-42-again', not on pull request #57's branch 'ada/issue-42'; release that claim by itself
exit 2
```

The claim on `ada/issue-42-again` still stands.

### E-REL-26 — an abandoned landing's rerun never ends the lane that claimed its item again

Setup: `storage = "state-ref"`, commit `4c1e9a0` on `main` trailer-naming `Work-Item: aco-00002a`,
landed from `ada/issue-42`; the claim released `--abandoned`, so `aco-00002a` stayed open; then
claimed again on `ada/issue-42-again`, whose clean linked worktree stands in this checkout

```console
$ aco release aco-00002a --merged 4c1e9a0 --branch ada/issue-42
2> ERROR: issue aco-00002a is claimed on 'ada/issue-42-again', not on commit 4c1e9a0's branch 'ada/issue-42'; release that claim by itself
exit 2
```

The claim on `ada/issue-42-again`, the open item and its worktree all stand; `refs/aco/state`
does not move. With no live claim on `aco-00002a` the same command meets REL-47 instead.
