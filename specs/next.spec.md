# `aco next`

`aco next` names the one action the board recommends pulling right now,
plus a fixed tail of three further lines every run prints regardless of
which action it recommends: `parallel:`, `scope unknown:`, and `close:`
(issue #348). This file owns every one of those lines' own shape, the
three `NextAction` variants' own text and `--json`, the `RECOVERY`
preamble and `SKIPPED` tail, and exit `0`/`3`. It cites rather than
restates: the forge-resolution precondition (`specs/board.spec.md`,
BOARD-01/BOARD-02/BOARD-42), `RECOVERY`'s own content and its always-first
placement (`specs/landing-grammar.spec.md`, LAND-53), the kind
exclusion a container without a recognized `kind` gets from every
container-shaped view (`specs/board.spec.md`, BOARD-08), and the scope
overlap grammar `aco claim`'s own cost line already defines
(`specs/claim-record.spec.md`, CLAIM-31/CLAIM-32); `specs/output.spec.md`
owns the `--json` envelope itself (OUT-nn: key order, `ok`, `message`)
that wraps NEXT-11..13's own action fields. `<n>` is an
item number, `<label>` an item as `specs/landing-grammar.spec.md` prints
it, `<s>` an integer score. A *display control* -- the one set this file
owns, which `next` escapes (NEXT-37), `aco land`'s refusal escapes
(`specs/land.spec.md`), `release`'s `next:` line, `rulings` text, `claim`'s
out-of-order warning, the board and brief configurations' unknown-key
refusals and, keeping their line feeds, the `brief` and `item show` bodies
escape as NEXT-37 does, a slice title refuses (`specs/body-block.spec.md`,
BODY-63) and a scope path refuses (`specs/claim-record.spec.md`, CLAIM-20)
-- is every control character but TAB (C0, DEL and C1), the line and paragraph
separators U+2028/U+2029, the bidi controls U+200E, U+200F, U+061C,
U+202A..U+202E and U+2066..U+2069, the zero-width characters U+200B..U+200D,
U+2060 and U+FEFF, and the tag characters U+E0000..U+E007F; NBSP and every
other printable space are text.

## Behavior table

| state \ trigger | text | `--json` |
|---|---|---|
| unsupported forge host / untracked pin | BOARD-01/02 (cited) | BOARD-01/02 (cited) |
| `--repo` under `storage = "state-ref"` | BOARD-42 (cited) | BOARD-42 (cited), NEXT-24 |
| no actionable item at all | NEXT-01 | NEXT-01 |
| a work item is the top action | NEXT-02, NEXT-03 | NEXT-11 |
| that action, run outside a linked non-default worktree | NEXT-41 | NEXT-11 |
| that action's scope is wide and the body names no `whole` | NEXT-42 | NEXT-11 |
| that item's expectations are proposed or old-ruled | NEXT-04 | NEXT-11 |
| a container qualifies to be cut | NEXT-05, NEXT-27, NEXT-31 | NEXT-12 |
| a title, `Next`, slice title or `SKIPPED` reason holds a display control | NEXT-37 | NEXT-11..14 |
| its first uncut row's title holds a line break or control character | NEXT-32 | NEXT-14 |
| a state-ref item the store cannot read | NEXT-36 | NEXT-14 |
| a container whose every open child is such an item | NEXT-38 | NEXT-14 |
| a second cuttable container behind the top action | NEXT-34 | NEXT-14 |
| that container is itself a child of another item | NEXT-25, NEXT-29, NEXT-33 | NEXT-14 |
| a childless container has no further `Next` work | NEXT-07 | NEXT-13 |
| that same container still names further work | NEXT-06 | NEXT-13 |
| a landed-but-open item exists | LAND-53 (cited) | NEXT-14 |
| an unworkable item exists beside the top action | NEXT-08 | NEXT-14 |
| an item carries the label `needs-operator` | NEXT-39, NEXT-40 | NEXT-14 |
| further free items exist, disjoint from the first action | NEXT-09, NEXT-21, NEXT-22 | NEXT-15 |
| more than three such candidates | NEXT-10 | NEXT-15 |
| the first action itself names no scope | NEXT-16 | NEXT-15 |
| a candidate names no scope of its own | NEXT-17 | NEXT-15 |
| a closable container or recovery item, any rank | NEXT-18, NEXT-19, NEXT-28 | NEXT-14 |
| neither exists | NEXT-20 | NEXT-14 |
| any item under `storage = "state-ref"` | its id, as labels print it | NEXT-35 |

## No actionable item

- [ ] [NEXT-01] With nothing to pull, `next` prints `No actionable item.`, still followed by `parallel:`/`scope unknown:`/`close:`, exit `3`; `--json` is `{"ok": false, "reason": "nothing_actionable", ...}`.

## A work item action

- [ ] [NEXT-02] The top-ranked item prints `<label> score <s>: <title>`, then `Next: <item's own Next>`, then `Run: aco claim <n>` from a linked worktree on a non-default branch, exit `0` (see E-NEXT-01).
- [ ] [NEXT-41] From any other checkout, where `claim` refuses, `Run:` is `aco start <n> --slug=<slug>`, the slug `start` derives from the title, else the id's: `!!! ???` gives `--slug=aco-1234ab` (E-NEXT-14).
- [ ] [NEXT-42] A scope of more than three paths and no `whole` in the body ends `Run:` with `--whole <reason>`, which `claim` and `start` then need; with `whole` set, it is left out (E-NEXT-14).
- [ ] [NEXT-37] Text shows each display control in a title, `Next`, slice title or `SKIPPED` reason as its escape: `a\x1b[2J` prints as typed; TAB, NBSP and `Größe` as is; `--json` unchanged.
- [ ] [NEXT-03] An item naming its own top-level `scope` drops `--scope` from `Run:`; a scopeless item's `Run:` ends `--scope <paths>` and gains a further `scope unknown` line (see E-NEXT-02).
- [ ] [NEXT-30] A scopeless item whose only `[[slice]]` row names paths -- a retyped nested container (NEXT-29) -- instead prints `--scope=<path>` per path of that row, with no `scope unknown`.
- [ ] [NEXT-04] A still-proposed item adds `expectations unruled: refine before the pull`; a stale ruling adds `ruled <n> landings ago: refine again at the pull` -- never both (see E-NEXT-02).
- [ ] [NEXT-23] A scopeless, sliceless item still qualifies as this action (`scope unknown`, NEXT-03); `claim`/`start`'s own precedence check reads a narrower rule (`specs/claim.spec.md` CLM-23).

## A container's own action

- [ ] [NEXT-05] A childless container with an undispatched `[[slice]]` row prints `cut_slice <label>: <next>`, then `Next: aco cut <n> --title=<cut title>`, exit `0` (see E-NEXT-03).
- [ ] [NEXT-27] Every argument an advice line prints inside a command is quoted for a POSIX shell, so it runs as printed: title `Say "hi" to $HOME` prints `--title='Say "hi" to $HOME'`.
- [ ] [NEXT-31] An advice command attaches each option's value as `--title=<quoted>` or `--scope=<quoted>`, so a value starting with `-` still reaches it: title `-draft` prints `--title=-draft`.
- [ ] [NEXT-32] A first uncut row whose title NEXT-37 escapes gets no `cut`; `SKIPPED` reads `slice row <i> title holds a line break or control character; make it one printable line` (E-NEXT-10).
- [ ] [NEXT-36] An item PIN-14/PIN-15 refuses is named under `SKIPPED` as `<item-id>: body malformed: <field>: <message>`; every other item still reads, one it blocks stays blocked (see E-NEXT-11).
- [ ] [NEXT-38] A container whose every open child, ITEM-54's included, is such an item reads `container; its open children do not read: <item-ids>` under `SKIPPED`, never `close:` (see E-NEXT-12).
- [ ] [NEXT-06] A childless container with no slice row whose `Next` names work prints `check_container <label>: no open children; check done_when`, then `Next: <next>`; never close or cut (E-NEXT-04).
- [ ] [NEXT-07] The same container with no further `Next` work prints `close_container <label>: <closed>/<total> children closed, no Next work` (see E-NEXT-04).
- [ ] [NEXT-25] A childless container with an uncut row that is a child of any item -- any type, open or closed, any repository -- never gets `cut_slice` (CUT-03); `SKIPPED` names its repair (E-NEXT-09).
- [ ] [NEXT-29] That repair for one row claims as the retyped item's `Run:` will (NEXT-03): bare with a top-level `scope`, else `--scope=<path>` per row path, else `--scope <paths>` (E-NEXT-09).
- [ ] [NEXT-33] Every claim a `SKIPPED` reason names ends `--out-of-order <reason>`, since a skipped item is never the first action; `<reason>`, like `<paths>`, is for the agent to fill in.

## `RECOVERY` and `SKIPPED`

- [ ] [NEXT-08] Every other unworkable item is named once under a trailing `SKIPPED` block, `<label>: <reason>`; a container `next` itself recommends cutting or closing is left out of that list.
- [ ] [NEXT-26] A NEXT-06 container that is not the first action reads `no open children; check done_when` under `SKIPPED`, never `container; claim a child` and never `close:`.
- [ ] [NEXT-34] A NEXT-05 container not the first action reads `cut slice "<title>"; run <its cut command>` under `SKIPPED`, `\` and `"` in `<title>` escaped in text and `--json`; never `container; claim a child`.
- [ ] [NEXT-28] An item `close:` lists is never also named under `SKIPPED` or `waiting on operator:`, in text or `--json` (see E-NEXT-06).
- [ ] [NEXT-39] An open, unclaimed, unfrozen item labelled `needs-operator` is never pulled; a line `waiting on operator: <labels>` after `close:` names it instead of `SKIPPED` (E-NEXT-13).
- [ ] [NEXT-40] `aco next --help` names that label and how to set or remove it: `gh issue edit <n> --add-label/--remove-label`, or `aco item edit <item-id>` under `storage = "state-ref"`.

## `parallel:`

- [ ] [NEXT-09] `parallel:` lists every further free item the walk placed, `<label> (<n> path[s])`, comma-joined, in board order, alongside the first action (see E-NEXT-05).
- [ ] [NEXT-21] `parallel:` occupies live claims' scopes and the first action's scope, then walks further qualifying actions in board order, skipping a close, check or recovery item outright (see E-NEXT-05).
- [ ] [NEXT-22] A candidate whose `scope` stays disjoint (CLAIM-31/CLAIM-32's grammar) from everything occupied is placed, its `scope` then joining what's occupied for the rest of the walk (see E-NEXT-05).
- [ ] [NEXT-10] Beyond three placed candidates, only the first three are named, followed by `, and <n> more`; `--json`'s own `candidates` array still carries every one (see E-NEXT-05).
- [ ] [NEXT-16] Once the first action names no scope, the tail collapses to `parallel: unknown (first action names no scope)`; `scope unknown:` is skipped, never printed as `none` (see E-NEXT-02).
- [ ] [NEXT-17] `scope unknown:` lists, in board order, every candidate the walk could not place for lacking a scope of its own, or `none`; printed whenever the first action does name a scope.

## `close:`

- [ ] [NEXT-18] `close:` unions closable containers (kind, no open child, no uncut `[[slice]]` row, no `Next` work) then recovery items, in board order, first-seen, whatever the first action (E-NEXT-06).
- [ ] [NEXT-19] `close:` never names a container the forge reports no recognized `kind` for (BOARD-08's own exclusion, #309): such an item is read as ordinary, not as one with nothing left to do.
- [ ] [NEXT-20] `close:` prints `none` when neither a closable container nor a recovery item exists.

## `--json`

- [ ] [NEXT-11] A work-item action's `reason` is `work_item` (`ok: true`), adding `number`, `score`, `title`, `next`, `command`, `ruling_landings`/`ruling_old`/`ruling_hint` per NEXT-04 (E-NEXT-07).
- [ ] [NEXT-12] A cut proposal's `reason` is `"cut_slice"` (`ok: true`), the object adding `number`, `title`, `slice`, `cut_title`, `command` (see E-NEXT-07).
- [ ] [NEXT-13] A close or check proposal's `reason` is `"close_container"` or `"check_container"` (`ok: true`), adding `number`, `closed`, `total`, `next_step` (`null` on close), never `command`.
- [ ] [NEXT-14] The object always carries `recovery` (`{number, title, step}` each), `skipped` (`{number, reason}` each), `close` and `waiting_on_operator` (bare number arrays), independent of `reason`.
- [ ] [NEXT-15] `parallel` always carries `first_scope_unknown`, `candidates` (`{number, scope}` each, uncapped), and `scope_unknown` (a bare number array).
- [ ] [NEXT-35] Under `storage = "state-ref"` each item these fields name, `number` included, is the `aco-xxxxxx` id the text prints, a string in place of the bare number.
- [ ] [NEXT-24] `--json` on a dispatched refusal (BOARD-02, BOARD-42) prints OUT-nn's envelope with the sentence as `message` and `reason` from the table below, exit `2` (see E-NEXT-08).

`reason`, by which refusal fired:

| refusal | `reason` |
|---|---|
| PIN-04 (`--repo` under `storage = state-ref`) | `invalid_usage` |
| BOARD-02 (no forge adapter for host), PIN-05 (no resolvable default branch) | `unavailable` |

## Never

- `aco next` never writes anything: it is a pure projection over the same read `board` performs.
- `close:` never fires the cut proposal's own `Next:`/`Run:` lines: a closable container's own action, when it is also the top action, never carries a command -- NEXT-06/NEXT-07 already refuse to invent one.
- `parallel:`/`scope unknown:` never occupy or place a `board.recovery` item: a landed-but-open item is `close:`'s domain alone, never a `parallel:` candidate or an occupant that could crowd out a real free item behind it.
- The first action's own row is never repeated inside `parallel:`'s candidate list, whatever its own scope is.
- Exit `3` never carries any action-specific line: `No actionable item.` alone stands where `Next:`/`Run:` would.
- Exit `3` is never returned for a refusal (NEXT-24's `invalid_usage`/`unavailable`): those exit `2`, matching every other emitter refusal; `3` is `nothing_actionable`'s alone.
- `next --json` never carries an `action` key; the action type is the `reason` token.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`, a
tracked `.agent-claim/board.toml`, and `ACO_AGENT` set to `Ada`. Every
session below also names a fixed, deterministic fake `gh` as a setup
precondition (the shape `specs/landing-grammar.spec.md` already uses).
Every session runs from a linked worktree on branch `ada/lane` unless its
setup names the main checkout.

### E-NEXT-01 — the top-ranked work item, scoped

Setup: bare-remote, fake `gh`, issue `#10` open, complete, `scope = ["README.md"]`

```console
$ aco next
#10 score -10: Work
Next: Claim #10.
Run: aco claim 10
parallel: none
scope unknown: none
close: none
exit 0
```

### E-NEXT-02 — a scopeless item, proposed expectations, `--scope <paths>`

Setup: bare-remote, fake `gh`, issue `#10` open, no `scope`, one proposed `[[expectation]]`

```console
$ aco next
#10 score -10: Work
Next: Claim #10.
Run: aco claim 10 --scope <paths>
scope unknown
expectations unruled: refine before the pull
parallel: unknown (first action names no scope)
close: none
exit 0
```

### E-NEXT-03 — a container with an uncut slice row

Setup: bare-remote, fake `gh`, container `#181`, no open child, one `[[slice]]` row titled `Scheibe C`

```console
$ aco next
cut_slice #181: Scheibe C
Next: aco cut 181 --title='Scheibe C'
parallel: none
scope unknown: none
close: none
exit 0
```

### E-NEXT-04 — a closable container, with and without further `Next` work

Setup: bare-remote, fake `gh`, container `#182`, no open child, no `[[slice]]` row, `Next` line `keiner`

```console
$ aco next
close_container #182: 3/3 children closed, no Next work
parallel: none
scope unknown: none
close: #182
exit 0
```

The same container after a slice landed, its `Next` line still naming the next slice (issue #503):

```console
$ aco next
check_container #182: no open children; check done_when
Next: Scheibe D schneiden.
parallel: none
scope unknown: none
close: none
exit 0
```

### E-NEXT-05 — five disjoint candidates, capped at three

Setup: bare-remote, fake `gh`, six open items each naming its own disjoint scope

```console
$ aco next
#60 score -10: Alpha
Next: Ship Alpha.
Run: aco claim 60
parallel: #61 (1 path), #62 (1 path), #63 (1 path), and 2 more
scope unknown: none
close: none
exit 0
```

### E-NEXT-06 — `close:` unions a closable container and a recovery item

Setup: bare-remote, fake `gh`, a top-ranked item `#70`, a closable container `#71`, a
recovery item `#72` whose merged pull request typed a `Work-Item:` line for it while it stayed open

```console
$ aco next
RECOVERY
#72: close or re-project

#70 score 40: Top ranked work
Next: Ship it.
Run: aco claim 70
parallel: none
scope unknown: none
close: #71, #72
exit 0
```

### E-NEXT-07 — `--json`, the three action shapes

Setup: bare-remote, fake `gh`, container `#181` as in E-NEXT-03

```console
$ aco next --json
{"ok": true, "reason": "cut_slice", "recovery": [], "skipped": [], "parallel": {"first_scope_unknown": false, "candidates": [], "scope_unknown": []}, "close": [], "waiting_on_operator": [], "number": 181, "title": "Epic", "slice": "Scheibe C", "cut_title": "Scheibe C", "command": "aco cut 181 --title='Scheibe C'"}
exit 0
```

### E-NEXT-08 — `--json` refusal envelope, `--repo` under `storage = state-ref`

Setup: bare-remote, `storage = "state-ref"` tracked

```console
$ aco --repo acme/items next --json
2> ERROR: --repo is meaningless under storage = state-ref
{"ok": false, "reason": "invalid_usage", "message": "--repo is meaningless under storage = state-ref"}
exit 2
```

### E-NEXT-09 — a nested container's uncut row, never a `cut` that `cut` refuses

Setup: bare-remote, fake `gh`, container `#298` with one open child, the container `#299`; `#299` has no open child and one `[[slice]]` row titled `Scheibe Z` with `scope = ["docs/z.md"]`

```console
$ aco next
No actionable item.
parallel: none
scope unknown: none
close: none

SKIPPED
#298: container; claim a child
#299: nested container, which cut refuses; run aco item edit 299 --kind task and claim it with aco claim 299 --scope=docs/z.md --out-of-order <reason>
exit 3
$ aco cut 299 --title "Scheibe Z"
2> ERROR: #299 is itself a child of example/agent-coordination#298; nested containers are not supported
exit 2
```

The named `item edit --kind task` runs under either storage (ITEM-47), `#299` printed as the id the storage takes back; the named claim then claims the row's paths past any higher-ranked free item (NEXT-33), and once retyped `next`'s own `Run:` line names that same claim without `--out-of-order <reason>` (NEXT-30). A container naming its own top-level `scope` ends the reason `claim it with aco claim 299 --out-of-order <reason>` instead, the claim that derives that scope (NEXT-03); a row without a `scope` of its own, under a container without one, ends it `claim it with aco claim 299 --scope <paths> --out-of-order <reason>`. With two or more uncut rows, the `SKIPPED` reason reads `nested container, which cut refuses; move its slice rows to example/agent-coordination#298`, the parent named as `cut`'s refusal names it. The same holds when `#298` is a Feature or Task, closed, or in another repository: `next` reads the parent relation `cut` refuses on.

### E-NEXT-10 — a second cuttable container, and a row title holding a line break

Setup: bare-remote, fake `gh`, containers `#181`, `#183` and `#184`, each with no open child; `#181`'s row is `Scheibe C` and `#183`'s `-draft`, both with `scope = ["docs/c.md"]`; `#184`'s row 2 is titled `Zeile eins`, a line break, `Zeile zwei`

```console
$ aco next
cut_slice #181: Scheibe C
Next: aco cut 181 --title='Scheibe C'
parallel: none
scope unknown: none
close: none

SKIPPED
#183: cut slice "-draft"; run aco cut 183 --title=-draft
#184: slice row 2 title holds a line break or control character; make it one printable line
exit 0
```

### E-NEXT-11 — an unreadable item beside a readable one

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked; `aco-4470c5`'s file was stored before BODY-63 with a raw U+000B in a slice title, `aco-2620d6` is an unfilled Task

```console
$ aco next
No actionable item.
parallel: none
scope unknown: none
close: none

SKIPPED
aco-2620d6: body incomplete: Now, Next, Done when
aco-4470c5: body malformed: agent-claim: agent-claim block is not valid TOML: Illegal character '\x0b' (at line 8, column 18)
exit 3
$ aco item show aco-4470c5
2> ERROR: item aco-4470c5 has a malformed agent-claim block; repair it with aco item edit aco-4470c5 and a body whose agent-claim block carries a valid [record]
exit 2
```

### E-NEXT-12 — a container whose one open child may be an unreadable item

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked; `aco-000001` is a filled Container whose one readable child is closed, `aco-3e26d9`'s file was hand-written with no `agent-claim` block

```console
$ aco next
No actionable item.
parallel: none
scope unknown: none
close: none

SKIPPED
aco-3e26d9: body malformed: agent-claim: no agent-claim block
aco-000001: container; its open children do not read: aco-3e26d9
exit 3
$ aco item close aco-000001
2> ERROR: item aco-3e26d9 has a malformed agent-claim block; repair it with aco item edit aco-3e26d9 and a body whose agent-claim block carries a valid [record]
exit 2
```

### E-NEXT-13 — the top item waits on the operator

Setup: bare-remote, fake `gh`; `#230` is labelled `security` and `needs-operator`, `#11` is a scopeless open item

```console
$ aco next
#11 score -10: Top work
Next: Claim #11.
Run: aco claim 11 --scope <paths>
scope unknown
parallel: unknown (first action names no scope)
close: none
waiting on operator: #230
exit 0
$ aco next --json
{"ok": true, "reason": "work_item", "recovery": [], "skipped": [], "parallel": {"first_scope_unknown": true, "candidates": [], "scope_unknown": []}, "close": [], "waiting_on_operator": [230], "number": 11, "score": -10, "title": "Top work", "next": "Claim #11.", "command": "aco claim 11 --scope <paths>", "ruling_landings": null, "ruling_old": null}
exit 0
```

### E-NEXT-14 — the top-ranked work item, seen from the main checkout

Setup: bare-remote, run from the main checkout on `main`, fake `gh`, issue `#10` titled `Work` open, complete, `scope = ["README.md"]`

```console
$ aco next
#10 score -10: Work
Next: Claim #10.
Run: aco start 10 --slug=work
parallel: none
scope unknown: none
close: none
exit 0
```

`aco start 10 --slug=work` then builds `issue-10-work` beside the checkout and claims `#10` there, as E-NEXT-01's `claim` would from that worktree. A title yielding no slug, such as `!!! ???`, prints `Run: aco start 10 --slug=10`; under `storage = "state-ref"` the id's own, `Run: aco start aco-1234ab --slug=aco-1234ab`.

The same item with `scope` naming five paths and no `whole` prints `Run: aco start 10 --slug=work --whole <reason>`; with the reason filled in, `aco start 10 --slug=work --whole 'One sweep over five files.'` claims it.
