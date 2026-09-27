# The `--json` envelope

Every migrated command's `--json` object is built and printed through one
shared, nameless envelope (issue #396). This file owns the envelope's own
shape -- key order, when `ok` is `true`, and what `reason` and `message`
may and may not carry -- and applies to every command whose own spec cites
`OUT-nn`; `ask`, `rule`, and `brief` were its first three
(`specs/ask.spec.md`, `specs/rule.spec.md`, `specs/brief.spec.md`), and
`release`, `cut`, and `item` are its last (issue #425,
`specs/release.spec.md`, `specs/cut.spec.md`, `specs/item.spec.md`), each
naming its own `reason` vocabulary with examples. Every `--json` command's
own spec now cites this file (issue #425 finished the migration ask/rule/
brief started; issue #435 brought `check <sha>`, the last printer that
still built an object of its own, in), including the refusals that fire
before the
named command starts (OUT-05) and the ones the argument parser itself
raises on the way into a command that declares `--json` (OUT-06,
issue #432).

## Behavior table

| state \ trigger | `--json` |
|---|---|
| a migrated command's own success | OUT-01, OUT-02 |
| a migrated command's own refusal | OUT-01, OUT-03 |
| a refusal before the named command starts | OUT-01, OUT-05 |
| a refusal the argument parser itself raises | OUT-01, OUT-06 |
| `--repo` given a value not shaped OWNER/REPO | OUT-08 |
| a long option spelled short of its full name | OUT-09 |

## The envelope

- [ ] [OUT-01] Every migrated command's `--json` object prints `ok` first and `reason` second, in that order, before any of the command's own payload keys.
- [ ] [OUT-02] `ok` is `true` only for that command's own success outcome; `reason` is always one stable token from that command's own enum, documented by its own spec, never a free sentence.
- [ ] [OUT-03] A refusal's `reason` names its enum member, never an `error` object; an optional `message` -- the refusal's own sentence, as its text form prints it -- is always the last key.
- [ ] [OUT-05] A refusal raised before the named command starts -- a missing identity, `release`'s branch checks -- prints this envelope, `reason` `precondition_failed`, its sentence as `message`.
- [ ] [OUT-06] A parser refusal on a command declaring `--json` -- an unknown flag, a missing required one, an unreadable positional -- prints this envelope, `invalid_usage`, exit `2` (see E-OUT-04).
- [ ] [OUT-07] The exit code answers before the object does: a refusal is never exit `0`, so a caller reads the code, then `ok` and `reason`, then the payload keys.
- [ ] [OUT-08] On every command, `--repo` not shaped OWNER/REPO refuses `repository must be OWNER/REPO, not '<value>'`, exit `2`, before any git or forge call; with `--json` as OUT-06 (see E-OUT-05).
- [ ] [OUT-09] On every command, a long option spelled short of its full name is never read as that option: it is a parser refusal, exit `2`, with `--json` as OUT-06 (see E-OUT-06).
- OUT-04 (retired 20.09.2026, issue #425): the `{"ok": false, "error": "<sentence>"}` fallback it kept for a command whose own spec cited no `OUT-nn` no longer exists; every `--json` command cites this file now.

## Never

- This file never lists a command's own vocabulary: `ask`, `rule`, and `brief` each document their own `reason` members, with examples, in their own spec.
- `ok`/`reason` never reorder around a command's payload: `reason` is always the second key, never last, never interleaved with structured detail keys.
- `message` never carries structured data: every structured detail (`item`, `index`, `claim`, `checks`, and the like) is its own sibling key, never packed into the prose.
- `message` never promises a stderr line beside the object: `ask`'s refusal prints `ERROR: <sentence>` there (E-OUT-02), `check <sha> --json` prints the object alone (`specs/check.spec.md`).
- A parser refusal without `--json` never changes shape (issue #432): stdout stays empty and stderr carries argparse's own usage block and sentence, exactly as it did before the envelope reached this refusal at all.
- A command that declares no `--json` never answers in this envelope (issue #432): `aco bootstrap --json` stays argparse's own text, and so does `aco release 42 --merged --jso`, an abbreviation OUT-09 refuses.
- An abbreviation never stands for a destructive flag (issue #502): `aco reset --conf --f` refuses before any read, never acting as `--confirm --force-unreadable`.
- A `--repo` path, bare owner, third segment, or `.`/`..` name is never dropped for the checkout's own remote: it refuses by name (OUT-08, issue #465).
- A non-zero exit never means a refusal on its own: a command may name a further code for an answer it did give, and its own spec owns that code.
- `protect` never joins this envelope, migrated or not: its hook protocol (a silent exit `0`, or the deny object on exit `2`; PROT-01/PROT-02) is a permanent exception (`specs/protect.spec.md`).
- `board --serve` never prints this file's own `--json` envelope either: its own request/response wire contract is permanently `specs/board.spec.md`'s own, not this file's.
- `bootstrap`, `reset`, `start`, `register`, `run`, and `login` never gain a `--json` mode of their own (each command's own product decision, not a pending migration): each names it in its own `## Never` (`specs/bootstrap.spec.md`, `specs/reset.spec.md`, `specs/start.spec.md`, `specs/workspace.spec.md`).

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`,
and `ACO_AGENT` set to `Ada`; `<item-id>` is the state-ref id a session
itself minted, `<n>` that same item's own bare number.

### E-OUT-01 -- a success envelope, `ok`/`reason` first

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, `<item-id>` open (`aco item new --title "Decide something"`)

```console
$ aco ask <item-id> --text "New question?" --json
{"ok": true, "reason": "asked", "item": <n>, "index": 1, "text": "New question?", "default": "yes"}
exit 0
```

### E-OUT-02 -- a refusal envelope, `reason` and `message`

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, `items/aco-000001.md` hand-written with no `agent-claim` block

```console
$ aco ask aco-000001 --text "New question?" --json
2> ERROR: aco-000001 body malformed: agent-claim: no agent-claim block; ask needs a valid agent-claim block
{"ok": false, "reason": "invalid_item", "message": "aco-000001 body malformed: agent-claim: no agent-claim block; ask needs a valid agent-claim block"}
exit 2
```

### E-OUT-03 -- a refusal before the named command starts

Setup: bare-remote, bootstrapped, `ACO_AGENT`, `GROK_SESSION_ID` and `CLAUDE_CODE_SESSION_ID` all unset

```console
$ aco claim 42 --scope src --json
2> ERROR: agent identity is required: pass --agent or set ACO_AGENT, GROK_SESSION_ID, or CLAUDE_CODE_SESSION_ID
{"ok": false, "reason": "precondition_failed", "message": "agent identity is required: pass --agent or set ACO_AGENT, GROK_SESSION_ID, or CLAUDE_CODE_SESSION_ID"}
exit 2
```

### E-OUT-04 -- a usage error the parser itself raises

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked

```console
$ aco release --json
2> ERROR: one of the arguments --merged --abandoned is required
{"ok": false, "reason": "invalid_usage", "message": "one of the arguments --merged --abandoned is required"}
exit 2
```

Without `--json` the same invocation prints no object at all, only argparse's
own usage block and `aco release: error: one of the arguments --merged
--abandoned is required` on stderr, exit `2`.

### E-OUT-05 -- a `--repo` that names no OWNER/REPO

Setup: bare-remote, `storage = "state-ref"` tracked, no `refs/aco/state` yet

```console
$ aco --repo /tmp/x bootstrap
2> ERROR: repository must be OWNER/REPO, not '/tmp/x'
exit 2
$ aco --repo a/b/c next --json
2> ERROR: repository must be OWNER/REPO, not 'a/b/c'
{"ok": false, "reason": "invalid_usage", "message": "repository must be OWNER/REPO, not 'a/b/c'"}
exit 2
```

Neither call reaches git: `origin` still carries no `refs/aco/state`.

### E-OUT-06 -- an abbreviated option

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked

```console
$ aco reset --conf --f
2> usage: aco [-h] [--version] [--repo REPO] ...
2> aco: error: unrecognized arguments: --conf --f
exit 2
$ aco status --js --json
2> ERROR: unrecognized arguments: --js
{"ok": false, "reason": "invalid_usage", "message": "unrecognized arguments: --js"}
exit 2
```

`refs/aco/state` is unchanged after the first call: no export, no delete.
