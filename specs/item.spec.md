# Item: new, show, edit, close

`aco item new`/`show`/`edit`/`close`: the one straight-to-`refs/aco/state`
item lifecycle (issues #285, #287, #289, #316, #337, #357), plus `item new`
under `storage = "github"`, which opens the GitHub issue itself (issue #444).
This file owns each command's own flags (`--title`, `--kind`, `--parent`,
`--origin`, `--scope`, `--size`, `--whole`, `--now`, `--next`, `--done-when`,
`--not-a-twin`), the body `item new` builds from them (issue #555), its printed and
`--json` shapes, and `item edit`'s record-merge rule; `specs/cut.spec.md`
owns the twin search `item new` shares with `cut` (CUT-29..CUT-31). `specs/body-block.spec.md` also owns the stored
`size` field's own schema (BODY-57..BODY-59), cited by ITEM-20 rather than
restated, and the stored `whole` field's own schema (BODY-60..BODY-62),
cited by ITEM-23/24; `specs/claim.spec.md` owns what `claim`/`start` do
with a stored `whole` (CLM-21/22).
`specs/storage-pin.spec.md` already owns the storage-pin refusals under
`storage = "github"` (PIN-10, PIN-11), the id-argument grammar (PIN-08), the
fresh-id mint and its collision refusal (PIN-06, PIN-07, PIN-19), the unknown-
parent and unknown-id refusals (PIN-18, PIN-23, PIN-28), the `--origin` write
and its header read (PIN-20), and `item close`'s live-claim and already-
closed refusals (PIN-25..PIN-28); `specs/body-block.spec.md` owns a stored
`scope` field's own schema (BODY-53..BODY-56) and `item edit`'s malformed-
body refusal (BODY-01..BODY-50, cited by PIN-24); `specs/claim-record.spec.md`
owns the scope canonicalization grammar (CLAIM-19..CLAIM-23) `item new
--scope` reuses -- never CLAIM-18's comma-versioned-file check, which only
`aco claim`/`rescope` apply; `specs/ref-store-cas.spec.md` owns the stale-oid refusal
(CAS-20) a second `item edit`/`item close` from the same snapshot meets,
and `item close`'s live-claim re-check on every write attempt (CAS-52);
`specs/output.spec.md` owns the `--json` envelope itself (key order, `ok`,
`message`); this file names only its own `reason` vocabulary (ITEM-17,
ITEM-25, ITEM-32) and cites the others by ID. A refusal prints `ERROR: <sentence>`
on stderr, exit `2`, and with `--json` also that envelope.
`<item-id>` is a minted `aco-xxxxxx` id, `<n>` its number, `<oid>`/`<sha>` the
runner's own git object ids. A printed `#<n>` outside `item show`'s header
is its `storage = "github"` form; under `storage = "state-ref"` it prints
`<item-id>` (PIN-30).

## Behavior table

| state \ trigger | `item new` | `item show` | `item edit` | `item close` |
|---|---|---|---|---|
| state-ref pin, default `--kind` | ITEM-01 | — | — | — |
| state-ref pin, `--kind container` | ITEM-02 | — | — | — |
| state-ref pin, `--parent` given | ITEM-03 | — | — | — |
| state-ref pin, `--scope` given | ITEM-04 | — | — | — |
| state-ref pin, `--scope` invalid | ITEM-05 | — | — | — |
| state-ref pin, `--origin` given | ITEM-19 | — | — | — |
| `--origin` malformed | ITEM-06 | — | — | — |
| `--title` empty or whitespace only, either storage | ITEM-36 | — | — | — |
| `--size` given, valid or invalid | ITEM-20 | — | ITEM-21, ITEM-22, ITEM-49, ITEM-51 | — |
| `--whole` given, valid or invalid | ITEM-23 | — | ITEM-24, ITEM-49, ITEM-51 | — |
| `--size`, `--whole` or `--kind` naming the value already set | — | — | ITEM-64 | — |
| prose piped without a block, or nothing piped, either storage | ITEM-58, ITEM-60 | — | — | — |
| a piped block a flag agrees with or contradicts, either storage | ITEM-59, ITEM-62 | — | — | — |
| state-ref pin, a stored body leaving a section empty | ITEM-61 | — | — | — |
| `--parent` an open Task, or `--kind` given, either storage | ITEM-45, ITEM-46 | — | ITEM-47..ITEM-51 | — |
| an item, open or closed | — | ITEM-07, ITEM-08, ITEM-56 | — | — |
| an unknown id | PIN-18 | ITEM-10 | PIN-23 | PIN-28 |
| `storage = "github"` | ITEM-26..ITEM-31 | ITEM-11 | PIN-10 | PIN-11 |
| `storage = "github"`, the `--parent` relation write fails | ITEM-32 | — | — | — |
| `storage = "github"`, GitHub drops the issue type | ITEM-34 | — | — | — |
| an open or recently closed look-alike title, either storage | ITEM-33 | — | — | — |
| `storage = "github"`, a re-run or an exact duplicate | ITEM-35 | — | — | — |
| a delivered `[record]`, present or absent | — | — | ITEM-12..ITEM-14 | — |
| no flag, and nothing piped or redirected | — | — | ITEM-63 | — |
| a delivered `blocked_by` the item does not carry, naming no item, a malformed item or the item itself, or one blocker twice | — | — | ITEM-43, ITEM-44 | — |
| a title or body whose stored bytes the read would refuse | ITEM-52 | — | ITEM-52, BODY-63 | — |
| `item show`/`edit`/`close --json` | — | ITEM-09 | ITEM-15 | ITEM-16 |
| a malformed piped body | ITEM-27 | — | ITEM-25 | — |
| another item malformed | ITEM-37, ITEM-42 | ITEM-37 | ITEM-53, ITEM-54, PIN-29 | ITEM-53, ITEM-54, PIN-29 |
| the item itself malformed | — | ITEM-38 | ITEM-39..ITEM-41 | ITEM-38 |
| another item naming a blocker `items/` lacks | — | — | — | ITEM-55 |
| a refusal reached with `--json` | ITEM-17, ITEM-18 | ITEM-17, ITEM-18 | ITEM-17, ITEM-18 | ITEM-17, ITEM-18 |

## `item new`

- [ ] [ITEM-01] `aco item new --title TITLE` with no `--kind` and nothing piped mints the id, then writes the skeleton body once, `kind = "task"` in its `[record]`; PIN-06/PIN-07 own id and `--json` (see E-ITEM-01).
- [ ] [ITEM-02] `--kind container` with nothing piped writes `Blocked by: nichts` ahead of the block, and `kind = "container"` in the stored `[record]` (see E-ITEM-01).
- [ ] [ITEM-03] `--parent PARENT` sets `record.parent` to `PARENT`'s id, an open Task retyped first (ITEM-45); unlike `cut`'s child body, it never writes a `Parent: #<n>` line.
- [ ] [ITEM-04] Repeated `--scope` values write a sorted, deduplicated top-level `scope = [...]` ahead of the `[record]` table, CLAIM-19..CLAIM-23's own canonical form (see E-ITEM-01).
- [ ] [ITEM-05] A `--scope` value that is absolute, `..`, or `~`-prefixed refuses with CLAIM-19's own sentence; a duplicate refuses with CLAIM-21's `claim scope contains duplicate paths`, before any write.
- [ ] [ITEM-06] `--origin FORGE#N` failing its grammar refuses `'<value>' is not an origin; use forge#n or host/owner/repo#n, e.g. gitlab#514`, exit `2`, before `item new`'s own body ever runs.
- [ ] [ITEM-36] Under either storage, an empty or whitespace-only `--title` refuses `--title must be a non-empty string`, exit `2`, before anything is read, created, or minted (see E-ITEM-09).
- [ ] [ITEM-19] `--origin`'s grammar is ASCII-only, case-insensitive `forge#n`/`host/owner/repo#n` tokens; an accepted value is stored in `record.origin` with its original case (PIN-20).
- [ ] [ITEM-20] `--size S|M|L` writes the item's own top-level `size` (BODY-57..BODY-59); argparse refuses an invalid value first. `id`/`--json` stay ITEM-01's shape; `size` is never printed.

  ```
  $ aco item new --title "Ship it" --size M
  writes size = "M" at the block's top level, ahead of [record]
  ```
- [ ] [ITEM-23] `--whole REASON` writes the item's own top-level `whole` (BODY-60..BODY-62), the same bound `claim`'s own `--whole` enforces; `claim`/`start` read it back when their own call names none (CLM-21).

## The body `item new` stores (issue #555)

- [ ] [ITEM-58] Either storage keeps prose piped without a block byte for byte, CRLF and trailing blanks too, above a block the flags build in its line ending; a key no flag names stays `""` (see E-ITEM-16).
- [ ] [ITEM-59] A piped body keeps its prose and fence lines byte for byte; a flag naming another value refuses `--<flag> <value> contradicts the piped block's <key> = <value>`, exit `2` (see E-ITEM-16).
- [ ] [ITEM-60] Under either storage `item new` reads a body from a file or pipe on stdin, never dropping it; a socket, terminal, `/dev/null` or closed stdin is never read.
- [ ] [ITEM-61] Under `storage = "state-ref"`, each section the stored body leaves empty prints `<item-id> misses <Section>; aco item edit <item-id> fills it` on stderr, one line each (see E-ITEM-01).
- [ ] [ITEM-62] A piped block is stored verbatim when the flags add nothing to it under `storage = "github"`, otherwise in its canonical rendering, a comment typed inside it not kept.

## `item new` under `storage = "github"` (issue #444)

- [ ] [ITEM-26] Under `storage = "github"`, `aco item new --title T < BODY` opens one GitHub issue titled `T` whose body is the piped one, its block built or completed from the flags (ITEM-58, ITEM-59).
- [ ] [ITEM-27] The body to store passes `aco check <n>`'s own body check first; a failing body refuses exactly like ITEM-25, and nothing is created (see E-ITEM-08).
- [ ] [ITEM-28] `--kind task|feature|container` sets the organization's own issue type `Task`, `Feature`, or `Container`, by name.
- [ ] [ITEM-29] `--parent N` records the issue as `#N`'s sub-issue; `#N` not open refuses `#N is not an open container`, neither Container nor Task `#N is not a container`, exit `2` (see E-ITEM-08).
- [ ] [ITEM-45] Under either storage, `--parent N` on an open Task retypes it Container after the twin search, before the create; stderr: `retyped #N to Container for its first child` (see E-ITEM-12).
- [ ] [ITEM-46] A retype GitHub drops refuses `GitHub did not set #N's type Container; set that type on the forge by hand`, exit `2`, before anything is created (see E-ITEM-12).
- [ ] [ITEM-30] Success prints `#<n>`, exit `0`; `--json` prints the envelope, `reason: "created"`, then `item` (`#<n>`) and `number`, the state-ref shape (see E-ITEM-07).
- [ ] [ITEM-31] `--origin` under `storage = "github"` refuses `--origin needs storage = "state-ref"`, exit `2`, before stdin is read.
- [ ] [ITEM-32] A failed `--parent` relation write refuses CUT-17's `created` sentence ending `; record that sub-issue relation on the forge by hand`, `--json` shaped as CUT-28.
- [ ] [ITEM-34] An issue GitHub creates untyped refuses `created #<n> but GitHub did not set its type <Type>; set that type[ and record it under #<N>] on the forge by hand`, `--json` as CUT-28.

## The twin search

- [ ] [ITEM-33] Under either storage, `item new` runs `cut`'s twin search (CUT-29..CUT-31) before it creates, `--parent` excepted as `cut` excepts its container (see E-ITEM-07).
- [ ] [ITEM-35] A re-run of the same GitHub `item new`, after a success, ITEM-32, or ITEM-34, meets its own issue as `possible twin #<n>`; `--not-a-twin` creates anyway, even an exact duplicate.

## `item show`

- [ ] [ITEM-07] `aco item show ITEM` prints one header, `<id> · #<n> · <state> · parent <parent-or-none> · origin <origin-or-none>`, then the stored body as ITEM-56 shows it, exit `0` (see E-ITEM-02).
- [ ] [ITEM-56] Text shows the body with each display control but the line feed escaped (NEXT-37): `a\x1b[2J` prints as typed; line breaks, TAB and `Größe` as is; `--json`'s `body` keeps it as stored.
- [ ] [ITEM-08] A closed item prints ITEM-07's same header, `state closed`; closing rewrites the body's `[record]` with `state = "closed"`, `closed_at`, and `updated_at`, the rest byte-identical.
- [ ] [ITEM-09] `aco item show ITEM --json` prints the envelope, `reason: "shown"`, then `item`, `number`, `state`, `parent`, `origin`, `body` (`parent`/`origin` `null` when unset) (see E-ITEM-02).
- [ ] [ITEM-10] `aco item show ITEM` against an unknown id refuses `#<n> does not exist in <owner/repo>`, exit `2`.
- [ ] [ITEM-11] `aco item show ITEM` under `storage = "github"` reads the forge issue's own body through ITEM-07/ITEM-09's same header and `--json` shape.

## `item edit`

- [ ] [ITEM-12] `aco item edit ITEM < BODY` takes `title`, `labels`, `blocked_by` from a delivered `[record]` when the piped body carries one valid (see E-ITEM-03).
- [ ] [ITEM-57] A delivered `[record]` whose `labels` add or drop `needs-operator` marks the item waiting on the operator or frees it (`specs/next.spec.md` NEXT-39).
- [ ] [ITEM-13] `item edit`'s every other field — `parent`, `state`, `origin`, `kind`, `created_at`, `closed_at` — stays stored, except a malformed item (ITEM-39) or `--kind` (ITEM-47); `updated_at` moves to now.
- [ ] [ITEM-14] A delivered body carrying no `[record]` table at all leaves `title`, `labels`, `blocked_by` unchanged too, exactly `item edit`'s own pre-#287 behaviour, except a malformed item (ITEM-39).
- [ ] [ITEM-43] A delivered `blocked_by` refuses before any write when it names one blocker twice, `item <item-id> lists blocker <blocker-id> more than once`, or a new one naming no item, PIN-17's (see E-ITEM-11).
- [ ] [ITEM-44] So does a new blocker naming a malformed item, PIN-14/PIN-15's then ITEM-38's, or the item itself, `item <item-id> is listed as its own blocker`; a stored list delivered unchanged is never re-judged.
- [ ] [ITEM-52] A state-ref write whose stored body PIN-14/PIN-15 would refuse prints `<defect>; stored, that body would not read back, so nothing was written`, exit `2`, before any write (see E-ITEM-14).
- [ ] [ITEM-63] With no flag, a socket, terminal, `/dev/null` or closed stdin refuses `item edit <item-id> needs the new body on stdin: aco item edit <item-id> < body.md`, exit `2`, unread, before any write.

  ```console
  $ aco item edit aco-00013a
  2> ERROR: item edit aco-00013a needs the new body on stdin: aco item edit aco-00013a < body.md
  exit 2
  ```
- [ ] [ITEM-15] `aco item edit ITEM --json` prints the envelope, `reason: "edited"`, then `item`, `number`, `oid` (the freshly written blob's own oid) (see E-ITEM-03).
- [ ] [ITEM-21] `item edit --size S|M|L` patches only the top-level `size`, reads no stdin, works under both storages; state-ref also bumps `record.updated_at`.

  ```
  $ aco item edit <item-id> --size L
  patches size = "L" only; every other field, including the body outside it, is untouched
  ```
- [ ] [ITEM-22] `item edit --size` prints `EDITED #<n> size=<S|M|L>` (`--json`: the envelope, `reason: "edited"`, then `item`, `size`); an invalid value is refused by argparse before any write.
- [ ] [ITEM-24] `item edit --whole REASON` patches only the top-level `whole`, reads no stdin, works under both storages, prints `EDITED #<n> whole=<reason>` (`--json`: `reason: "edited"`, `item`, `whole`).
- [ ] [ITEM-47] `item edit --kind task|container` sets only an open item's type (else `#<n> is not an open item`), both storages; prints `EDITED #<n> kind=<kind>` (`--json`: `item`, `number`, `kind`) (see E-ITEM-13).
- [ ] [ITEM-48] `--kind task` on an item with an open child refuses `#<n> has an open child; a container with open children stays a container`, exit `2`, before any write (see E-ITEM-13).
- [ ] [ITEM-49] A file or pipe on the stdin of `--kind`, `--size` or `--whole` refuses `item edit --<flag> reads no stdin; drop the redirect`, exit `2`, before any write.

  ```console
  $ printf 'body\n' | aco item edit 484 --size S
  2> ERROR: item edit --size reads no stdin; drop the redirect
  exit 2
  ```
- [ ] [ITEM-64] `--size`, `--whole` or `--kind` naming the value already set prints `UNCHANGED #<n> <field>=<value>` (`--json`: `reason: "unchanged"`, ITEM-22/24/47's keys), exit `0`, and writes nothing.

  ```console
  $ aco item edit 484 --size S
  EDITED #484 size=S
  $ aco item edit 484 --size S
  UNCHANGED #484 size=S
  exit 0
  ```
- [ ] [ITEM-50] `--size`, `--whole` and `--kind` exclude one another: a second one refuses at argparse, `argument <second>: not allowed with argument <first>`, exit `2`, before any write.
- [ ] [ITEM-51] A socket, terminal, `/dev/null` or closed stdin on `--kind`, `--size` or `--whole` passes unread, as an agent harness hands a socket when nothing was piped.

## `item close`

- [ ] [ITEM-16] `aco item close ITEM --json` prints `reason: "closed"`, then `item`, `number`, `closed_at`, `parent_closable` (issue #348's parent hint); overlaps ITEM-09's `item`/`number` (see E-ITEM-04).
- [ ] [ITEM-55] After the write, only hints: a `freed:` read another item refuses (PIN-16/17/34) prints LAND-38's or LAND-65's `hint:` line instead (`--json`: on stderr), exit `0` (see E-ITEM-15).

## One malformed item (issue #447)

- [ ] [ITEM-37] Under `storage = "state-ref"`, an item whose file PIN-14/PIN-15 refuse never stops `item new`, nor `item show` of any other item.
- [ ] [ITEM-42] That item's `record.title`, while it still reads as a non-empty string, joins ITEM-33's twin search as an open item's title.
- [ ] [ITEM-53] Beside a malformed item, `item close` refuses only if it is its item, its parent, or a child, and `item edit --kind` only if it is its item or a child; any other malformed item stops neither (PIN-29).
- [ ] [ITEM-54] For ITEM-53 and the board's child counts alike, a malformed item whose `[record]`, or its `parent`, does not read is an open child of every Container; one read without a parent is no child.
- [ ] [ITEM-38] Reading that item itself (`item show`, `item close`, `item edit --size`/`--whole`/`--kind`) refuses PIN-14/PIN-15's sentence, then the repair clause of E-ITEM-10.
- [ ] [ITEM-39] `aco item edit <id> < BODY` on that item takes BODY's complete `[record]` as the item's own, `updated_at` moved to now; BODY without a `[record]` refuses as ITEM-38 (see E-ITEM-10).
- [ ] [ITEM-40] That `[record]`'s `parent` or `blocked_by` naming a missing item refuses PIN-16/PIN-17's sentence, a malformed one or the item itself ITEM-38's, before any write.
- [ ] [ITEM-41] That `[record]` naming `state = "closed"` refuses `a repair records state = "open"; close <id> afterwards with aco item close <id>`, before any write.

## `--json` and the shared envelope

- [ ] [ITEM-17] Every other runtime refusal from `item new`/`show`/`edit`/`close`, reached with `--json`, prints `specs/output.spec.md`'s envelope, `reason: "precondition_failed"`, exit `2`.
- [ ] [ITEM-18] An argparse-level refusal — a malformed `--origin`, a blank `--title`, or an item argument PIN-08 refuses — prints the `ERROR:` line, exit `2`, and under `--json` OUT-06's envelope.
- [ ] [ITEM-25] A malformed piped body (`item edit`'s PIN-24, `item new`'s ITEM-27) reports `reason: "body_invalid"` instead, `defects` the same list `body --check`'s own `--json` carries, exit `2`.

## Never

- `aco item new --scope` never applies CLAIM-25/CLAIM-26's wide-scope gate: a bare directory or four-plus paths write cleanly into the item's own `scope`; only a later `aco claim` on that item enforces width.
- `aco item new` under `storage = "github"` never adds a `Parent: #<n>` line or a `[record]` table to the piped body: the forge's own sub-issue relation and issue type carry both.
- `aco item show` never refuses merely for its storage value itself (ITEM-11 covers both); it does resolve the state-ref forge like `item new`/`edit`/`close`, so `--repo` there refuses same as those (PIN-04, PIN-05).
- `aco item edit`/`close` never reach ITEM-17's refusal object ahead of PIN-08's own argparse-level check: the item argument is parsed before either command body ever runs.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local bare
repository with `main` at one commit, a git identity, `origin/HEAD`, and
`ACO_AGENT` set to `Ada`; `<remote>`, `<tmp>`, and `<home>` are the runner's
own paths, `<item-id>` an id the session itself minted. Sessions whose stdin
carries a fenced block use a four-backtick console fence.

### E-ITEM-01 — a fresh task, then a scoped container

Setup: bare-remote, `storage = "state-ref"` tracked, bootstrapped

```console
$ aco item new --title "Ship it"
<item-id>
2> <item-id> misses Now; aco item edit <item-id> fills it
2> <item-id> misses Next; aco item edit <item-id> fills it
2> <item-id> misses Done when; aco item edit <item-id> fills it
exit 0
$ aco item new --title "Docs pass" --kind container --scope docs/README.md --scope docs/PRODUCT.md --now "Cut." --next "Cut slice 1." --done-when "All slices landed." --json
{"ok": true, "reason": "created", "item": "<item-id-2>", "number": <n2>}
exit 0
```

### E-ITEM-02 — item show, text and `--json`

Setup: bare-remote, `storage = "state-ref"` tracked, bootstrapped, `<item-id>`
already `aco item new --title "Ship it"`

````console
$ aco item show <item-id>
<item-id> · #<n> · open · parent none · origin none
```agent-claim
version = 1
now = ""
next = ""
done_when = ""

[record]
title = "Ship it"
state = "open"
kind = "task"
labels = []
blocked_by = []
created_at = "<created_at>"
updated_at = "<updated_at>"
```
exit 0
$ aco item show <item-id> --json
{"ok": true, "reason": "shown", "item": "<item-id>", "number": <n>, "state": "open", "parent": null, "origin": null, "body": "```agent-claim\nversion = 1\nnow = \"\"\nnext = \"\"\ndone_when = \"\"\n\n[record]\ntitle = \"Ship it\"\nstate = \"open\"\nkind = \"task\"\nlabels = []\nblocked_by = []\ncreated_at = \"<created_at>\"\nupdated_at = \"<updated_at>\"\n```\n"}
exit 0
````

### E-ITEM-03 — item edit, text and `--json`

Setup: bare-remote, `storage = "state-ref"` tracked, bootstrapped, `<item-id>`
already open

````console
$ aco item edit <item-id> <<'BODY'
```agent-claim
version = 1
now = "Cut on 19.09.2026."
next = "Build it."
done_when = "It is built."
```
BODY
EDITED <item-id>
exit 0
$ aco item edit <item-id> --json <<'BODY'
```agent-claim
version = 1
now = "Cut on 19.09.2026."
next = "Ship it."
done_when = "It is built."
```
BODY
{"ok": true, "reason": "edited", "item": "<item-id>", "number": <n>, "oid": "<oid>"}
exit 0
````

### E-ITEM-04 — item close, text and `--json`

Setup: bare-remote, `storage = "state-ref"` tracked, bootstrapped, `<item-a>`
open with no live claim, `<item-b>` open and blocked only by `<item-a>`,
`<item-c>` open with no live claim

```console
$ aco item close <item-a>
CLOSED <item-a>
freed: <item-b>
exit 0
$ aco item close <item-c> --json
{"ok": true, "reason": "closed", "item": "<item-c>", "number": <n-c>, "closed_at": "<closed_at>", "parent_closable": null}
exit 0
```

### E-ITEM-05 — the `storage = "github"` refusals

Setup: bare-remote, `.agent-claim/board.toml` tracked with no `storage` key
(the default)

```console
$ aco item edit 42
2> ERROR: forge issues are edited on the forge; aco never governs them
exit 2
$ aco item close 42
2> ERROR: the forge closes its issues; aco never governs them
exit 2
```

### E-ITEM-06 — `item edit --json` on a malformed piped body

Setup: bare-remote, `storage = "state-ref"` tracked, bootstrapped, `<item-id>` already open

```console
$ aco item edit <item-id> --json <<'BODY'
no block
BODY
{"ok": false, "reason": "body_invalid", "defects": ["body malformed: agent-claim: no agent-claim block"], "message": "body malformed: agent-claim: no agent-claim block"}
2> ERROR: body malformed: agent-claim: no agent-claim block
exit 2
```

### E-ITEM-07 — a GitHub issue under a container, then a twin

Setup: bare-remote, `.agent-claim/board.toml` tracked with no `storage` key,
fake `gh`, open container `#90`, open task `#91` titled `Ship the importer`,
`#92` the next free number; `body.md` a complete `agent-claim` block

```console
$ aco item new --title "Write the importer docs" --kind feature --parent 90 < body.md
#92
exit 0
$ aco item new --title "Ship importer" --json < body.md
{"ok": false, "reason": "precondition_failed", "message": "possible twin #91; pass --not-a-twin"}
2> ERROR: possible twin #91; pass --not-a-twin
exit 2
$ aco item new --title "Ship importer" --not-a-twin --json < body.md
{"ok": true, "reason": "created", "item": "#93", "number": 93}
exit 0
```

### E-ITEM-08 — refused before anything is created

Setup: as E-ITEM-07, `#85` closed, `#94` an open feature

```console
$ aco item new --title "Another slice" --parent 94 < body.md
2> ERROR: #94 is not a container
exit 2
$ aco item new --title "Another slice" --parent 85 < body.md
2> ERROR: #85 is not an open container
exit 2
$ aco item new --title "Another slice" --json <<'BODY'
no block
BODY
{"ok": false, "reason": "body_invalid", "defects": ["body incomplete: Now, Next, Done when"], "message": "body incomplete: Now, Next, Done when"}
2> ERROR: body incomplete: Now, Next, Done when
exit 2
```

### E-ITEM-09 — a blank title, refused before anything is minted

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked

```console
$ aco item new --title "   "
2> ERROR: --title must be a non-empty string
exit 2
$ aco item new --title "" --json
{"ok": false, "reason": "invalid_usage", "message": "--title must be a non-empty string"}
2> ERROR: --title must be a non-empty string
exit 2
```

### E-ITEM-10 — one malformed item, refused alone and repaired

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, `items/aco-3e26d9.md` hand-written with `title = ""` in its `[record]`, `<item-id>` another open item, `repaired.md` a body whose block carries a complete `[record]`

```console
$ aco item show aco-3e26d9
2> ERROR: item aco-3e26d9 has a malformed agent-claim block; repair it with aco item edit aco-3e26d9 and a body whose agent-claim block carries a valid [record]
exit 2
$ aco item new --title "Fresh item"
<item-id>
2> <item-id> misses Now; aco item edit <item-id> fills it
2> <item-id> misses Next; aco item edit <item-id> fills it
2> <item-id> misses Done when; aco item edit <item-id> fills it
exit 0
$ aco item edit aco-3e26d9 < repaired.md
EDITED aco-3e26d9
exit 0
```

### E-ITEM-11 — item edit refuses a blocker that does not resolve

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, `<item-id>`
open, `unknown.md` a body whose `[record]` names `blocked_by = ["aco-ffffff"]`
and no `items/aco-ffffff.md`, `itself.md` one naming `blocked_by = ["<item-id>"]`,
`twice.md` one naming `blocked_by = ["<blocker-id>", "<blocker-id>"]` of an open item

```console
$ aco item edit <item-id> < unknown.md
2> ERROR: item <item-id> lists blocker aco-ffffff, which does not exist
exit 2
$ aco item edit <item-id> < itself.md
2> ERROR: item <item-id> is listed as its own blocker
exit 2
$ aco item edit <item-id> < twice.md
2> ERROR: item <item-id> lists blocker <blocker-id> more than once
exit 2
```

### E-ITEM-12 — a Task parent becomes a Container with its first child

Setup: as E-ITEM-07, `#91` still the open task `Ship the importer`; in the second session GitHub drops the type, since the caller lacks push access

```console
$ aco item new --title "Document the importer" --parent 91 < body.md
2> retyped #91 to Container for its first child
#92
exit 0
$ aco item new --title "Document the importer" --parent 91 < body.md
2> ERROR: GitHub did not set #91's type Container; set that type on the forge by hand
exit 2
```

### E-ITEM-13 — a childless container turns Task, one with an open child stays

Setup: `storage = "state-ref"`, `<item-id>` a nested container with one uncut row and no open child, `<container-id>` a container with an open child

```console
$ aco item edit <item-id> --kind task
EDITED <item-id> kind=task
exit 0
$ aco item edit <container-id> --kind task
2> ERROR: <container-id> has an open child; a container with open children stays a container
exit 2
```

Under `storage = "state-ref"` `record.kind` and `updated_at` move and every other byte stays; under `storage = "github"` the issue's organization type moves, and a type GitHub drops refuses ITEM-46's sentence naming that type.

### E-ITEM-14 — a write the read would refuse lands nothing

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, `<item-id>` open, `vt.md` a body whose `[[slice]]` title is `"Line one\u000bLine two"`

```console
$ aco item edit <item-id> < vt.md
2> ERROR: body malformed: slice[0].title: slice[0].title of row 1 holds U+000B; a slice title stays on one line
exit 2
$ aco item new --title "$(printf 'a\377b')"
2> ERROR: body malformed: item: item file is not valid UTF-8; stored, that body would not read back, so nothing was written
exit 2
```

`refs/aco/state` keeps its tip after both refusals.

### E-ITEM-15 — a close stands although `freed:` does not read

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, `<item-a>` open with no live claim, `<item-m>` open with `blocked_by = ["aco-ffffff"]`, an id `items/` lacks

```console
$ aco item close <item-a>
CLOSED <item-a>
hint: could not read the board to report what this write freed (item <item-m> lists blocker aco-ffffff, which does not exist); run `aco board --json` once it is repaired
exit 0
$ aco item close <item-a>
2> ERROR: <item-a> is already closed (closed on <closed_at>)
exit 2
```

`refs/aco/state` holds the first close; the second refuses before any write.

### E-ITEM-16 — prose above a block the flags build, then a contradicting block

Setup: bare-remote, `.agent-claim/board.toml` tracked with no `storage` key,
fake `gh`, `#95` the next free number; `body.md` a complete `agent-claim`
block with `size = "S"`

```console
$ printf 'Ship the importer.\n' | aco item new --title "Import the feed" --now "Ready." --next "Build it." --done-when "Merged." --size S
#95
exit 0
$ aco item new --title "Import the archive" --size M < body.md
2> ERROR: --size "M" contradicts the piped block's size = "S"
exit 2
```

`#95`'s body is `Ship the importer.`, a blank line, then the block holding
`now`, `next`, `done_when` and `size = "S"`; the refused run creates nothing.
