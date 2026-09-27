# `aco protect`

`aco protect` is the `PreToolUse` hook entry point (issue #176, #238, #252,
#314): reading one hook payload from stdin, it judges a single mutating tool
call against this session's own live claim: an allow prints nothing, a denial
prints one JSON object and repeats its sentence on stderr. This file owns the
payload envelope, every denial reason and the order they are judged in, and
the allow/deny output and exit codes. `specs/claim-record.spec.md` owns a
claim's own identity, scope grammar and overlap; `specs/ref-store-cas.spec.md`
owns `refs/aco/state`'s own transport failures; `specs/storage-pin.spec.md`
owns the board-configuration precondition (PIN-01/PIN-32) every store command
shares -- this file cites those IDs rather than restating them. `aco rescope`
shares `protect`'s own checkout resolver and relative-path grammar (the
`relative payload path`, `not in a repository`, and `no commit on this
branch` sentences, and the sentences for a path no claim can ever cover,
PROT-14, PROT-42 and PROT-43) but is otherwise a different lane's own spec;
those sentences are documented here, where `not in a repository` is
`rescope`'s refusal alone -- `protect` allows such a path (PROT-32).
`<path>` is the payload's own absolute file path, lexically normalized;
`<remote>` is the canonical remote name; `<git-directory>` is a bare
repository or a checkout's own `.git` directory, symlink-resolved.

## Behavior table

| state \ trigger | generic mutating tool | `NotebookEdit` | `apply_patch` | `Bash`, `Monitor` | a read-effect tool |
|---|---|---|---|---|---|
| malformed or non-object payload | PROT-03 | PROT-03 | PROT-03 | PROT-03 | PROT-03 |
| no string tool name under either key | PROT-04 | PROT-04 | PROT-04 | PROT-04 | PROT-04 |
| tool name in neither table | — | — | — | — | PROT-06 (unknown) |
| tool name is read-only | — | — | — | — | PROT-05 |
| no resolvable path in the payload | PROT-07 | PROT-07 | PROT-07 | PROT-30 (no pattern) | — |
| payload path not absolute | PROT-09 | PROT-09 | PROT-09 (each path) | PROT-31 (allow) | — |
| path's directory outside every repository | PROT-32 (allow) | PROT-32 (allow) | PROT-32 (allow) | PROT-32 (allow) | — |
| path's directories do not exist yet | PROT-39 | PROT-39 | PROT-39 | PROT-39 | — |
| path inside a git directory itself | PROT-43 | PROT-43 | PROT-43 | PROT-43 | — |
| a write through a file symlink, or a directory symlink the path ends in, into another checkout | PROT-44 | PROT-44 | PROT-44 | PROT-44 | — |
| an ignored file under the checkout's `.claude/` | PROT-38 (allow) | PROT-38 (allow) | PROT-38 (allow) | PROT-38 (allow) | — |
| `ACO_PROTECT_UNGUARDED` names a malformed entry | PROT-41 | PROT-41 | PROT-41 | PROT-41 | — |
| the path's repository sits in an unguarded directory | PROT-40 (allow) | PROT-40 (allow) | PROT-40 (allow) | PROT-40 (allow) | — |
| checkout has no commit yet | PROT-11 | PROT-11 | PROT-11 | PROT-11 | — |
| shared main checkout, or on the default branch | PROT-12 | PROT-12 | PROT-12 | PROT-12 | — |
| default branch cannot be resolved | PROT-13 | PROT-13 | PROT-13 | PROT-13 | — |
| path below a file or a dangling symlink | PROT-42 | PROT-42 | PROT-42 | PROT-42 | — |
| path resolves to exactly the checkout root | PROT-14 | PROT-14 | PROT-14 | PROT-14 | — |
| board-configuration precondition fails | PROT-29 | PROT-29 | PROT-29 | PROT-29 | — |
| a store fetch failure | PROT-15 | PROT-15 | PROT-15 | PROT-15 | — |
| `refs/aco/state` missing | PROT-16 | PROT-16 | PROT-16 | PROT-16 | — |
| agent identity cannot be resolved | PROT-08 | PROT-08 | PROT-08 | PROT-08 | — |
| an unexpected crash | PROT-17 | PROT-17 | PROT-17 | PROT-17 | — |
| no live claim on this branch at all | PROT-18 | PROT-18 | PROT-18 | PROT-33 (names pattern) | — |
| a live claim whose scope misses the path | PROT-19 | PROT-19 | PROT-20 | PROT-33 (names pattern) | — |
| a live claim covering the path | PROT-21 | PROT-21 | PROT-23 | PROT-21 | — |
| a lane (issueless) claim covering the path | PROT-22 | PROT-22 | PROT-22 | PROT-22 | — |
| several paths, first one outside scope | — | — | PROT-24 | PROT-33 (each pair) | — |
| paths across two linked worktrees | — | — | PROT-25 | PROT-33 (own checkout each) | — |
| a `cd` changes the resolution directory | — | — | — | PROT-34 | — |
| a `cd` target cannot be resolved | — | — | — | PROT-35 (allow) | — |
| a decoy path key the tool never sends | — | PROT-27 | — | — | — |
| an OWNER/REPO `--repo`, or a non-GitHub canonical remote | PROT-28 | PROT-28 | PROT-28 | PROT-28 | PROT-28 |
| `--repo` not shaped OWNER/REPO | OUT-08 | OUT-08 | OUT-08 | OUT-08 | OUT-08 |

Every column resolves agent identity last (issue #448), only once a
checkout, its live state, and a repository-relative path are already in
hand: a write that never gets that far -- outside every repository, in the
main checkout, or a Bash command naming no pattern -- never needed an
identity at all, so a session without one is stopped only where a claim
could answer for it.

## The verdict's output

- [ ] [PROT-01] A write `protect` authorizes prints nothing to stdout or stderr, exit `0` (see E-PROT-01).
- [ ] [PROT-02] A write `protect` refuses prints exactly `{"decision": "deny", "reason": "<sentence>"}` to stdout, the same sentence, newline-terminated, on stderr, exit `2` (see E-PROT-02).

Who reads which channel: an allow is the one form all three hosts document as
"no objection" -- Claude Code ("Exit code 0 with no output means the hook has
no decision to report, so the tool call continues through the normal
permission flow", hooks reference), Codex ("Exit 0 with no output is treated
as success and Codex continues", hooks reference), and Grok (exit `0` is
"Success / allow", Hooks, "Exit Codes"). An allow object would not be:
Claude Code takes `decision` only as `approve` or `block` and shows any other
object as a hook error notice, while `approve` or `permissionDecision:
"allow"` would skip its permission prompt. A denial: Claude Code blocks on
exit `2` and hands its agent the stderr sentence, since the stdout object is
not its own hook schema (Claude Code hooks reference, "Exit code 2"); Codex
does the same ("You can also use exit code 2 and write the blocking reason to
stderr", Codex hooks reference, PreToolUse); Grok reads the stdout object.
All three see the same sentence, so no tool's agent is left without a reason.

## The hook payload

Claude Code's own session tools steer the session, a subagent, or a
workflow, or talk to the operator; none names a file to write, so the table
marks each read-only: `ToolSearch`, `SendMessage`, `TaskStop`,
`TaskOutput`, `StructuredOutput`, `Skill`, `AskUserQuestion`, `ListAgents`,
`ScheduleWakeup`, `SendFeedback`, `Workflow`, and `Artifact` (issue #448).
`Monitor` is the one session tool that runs a shell script, its own
`command`, so it is judged exactly like `Bash`.
The README's hook matcher names every tool the table gates -- each mutating
name plus the command-text tools `Bash` and `Monitor` -- so any other tool
(MCP tools, plan mode, task lists) never reaches `protect` and never stalls a
session; a new gated tool joins both the table and that matcher.

- [ ] [PROT-03] Unreadable stdin, invalid JSON, or a payload that is not a JSON object denies `invalid hook payload` (PROT-02's shape).
- [ ] [PROT-04] A payload naming no string tool name under either `toolName` or `tool_name` denies `invalid hook payload`.
- [ ] [PROT-05] A tool name this table marks read-only allows (PROT-01) without reading identity, git, the store, or GitHub (see E-PROT-05).
- [ ] [PROT-37] Each read-only session tool named above allows like PROT-05, `Monitor` is judged like `Bash`, and a tool outside the README's hook matcher never reaches `protect` (see E-PROT-05).
- [ ] [PROT-06] A tool name in neither the read nor the mutating table denies `'<name>' is not in aco's hook tool table`, fix `add it there as read-only or mutating before use` (see E-PROT-06).

## The payload path and its own checkout

- [ ] [PROT-07] A mutating tool call with no resolvable path -- a missing key, an empty string, or an `apply_patch` command matching no patch-file grammar -- denies `path required`.
- [ ] [PROT-08] Past its live state, no identity denies `agent identity is required: set ACO_AGENT, GROK_SESSION_ID, or CLAUDE_CODE_SESSION_ID (ACO_AGENT can sit in the hook line)`; a bad one, its own sentence.
- [ ] [PROT-09] A payload path that is not absolute denies `relative payload path`, never guessed against the hook process's own cwd (see E-PROT-07).
- [ ] [PROT-10] A path whose directory sits outside every git repository is `not in a repository`, the sentence `rescope` refuses with; `protect` allows it instead (PROT-32).
- [ ] [PROT-32] A write path outside every repository -- any tool's payload path or a recognized Bash pattern's -- allows before identity or the store is read, except a checkout's own root (PROT-14) (see E-PROT-11).
- [ ] [PROT-11] A checkout with no commit yet (an unborn branch) denies `no commit on this branch`.
- [ ] [PROT-12] The shared main checkout, or a linked worktree on the default branch its canonical remote's `HEAD` records, denies `not main` (see E-PROT-03); PROT-29 comes first for that worktree.
- [ ] [PROT-13] A linked worktree whose canonical remote records no `HEAD`, or one naming no branch that resolves, denies `default branch unknown`, never falling back to a `main`/`master` guess.
- [ ] [PROT-45] A canonical `<remote>` with no URL configured denies where PROT-13 would, in every command's sentence `cannot determine the trunk: canonical remote '<remote>' is not configured` (see E-PROT-15).
- [ ] [PROT-14] A payload path that resolves to exactly the checkout root denies `<path> is the checkout root itself`, the sentence `rescope` refuses it with (see E-PROT-14).
- [ ] [PROT-38] A path under the checkout's own `.claude/` that git ignores allows in any checkout, main included, before identity or the store is read (see E-PROT-12).
- [ ] [PROT-39] A path whose directories do not exist yet is judged by the checkout of its nearest existing ancestor, never allowed as outside every repository (PROT-32).
- [ ] [PROT-36] A payload path naming a nested checkout's own root is judged by that checkout, never by an outer one its parent directory sits inside, before PROT-14 denies it.
- [ ] [PROT-42] A path below a file denies `<path> cannot exist: <file> is a file`; below a dangling symlink, `<path> cannot exist: <link> is a dangling symlink` (see E-PROT-14).
- [ ] [PROT-43] A path inside a bare repository or a checkout's own `.git` directory denies `not a checkout: <git-directory> is a git directory`, never git's own error text (see E-PROT-14).
- [ ] [PROT-44] A write through a file symlink, or a directory symlink the path ends in, into another checkout is judged in both, store-free checks before any store or identity read; the target's wins when both deny.

## Unguarded repositories

`ACO_PROTECT_UNGUARDED` names directories, separated like `PATH`, whose
repositories a tester may write freely -- a scratchpad of throwaway
checkouts. The match is on the repository's own common git directory, so a
worktree's own location never exempts it; unset or empty, every repository
is judged. Any other value is read entry by entry, so an empty entry
between separators is malformed like any other (PROT-41).

- [ ] [PROT-40] A path whose repository's git directory, symlink-resolved, sits at or below an `ACO_PROTECT_UNGUARDED` directory allows after PROT-38, before PROT-11, reading no identity or store (see E-PROT-13).
- [ ] [PROT-41] An `ACO_PROTECT_UNGUARDED` entry not an existing absolute directory denies `ACO_PROTECT_UNGUARDED: <entry> is not an absolute directory` for a path in a checkout, after PROT-38 (see E-PROT-13).

## The live claim state

- [ ] [PROT-29] A board-configuration failure (PIN-01/PIN-32), reached resolving the canonical remote -- a linked worktree's at PROT-12, else past the root gates -- denies its own bare sentence, no `ERROR:` prefix.
- [ ] [PROT-15] A store fetch failure -- unreachable, malformed tree, or a lineage break -- denies `cannot reach refs/aco/state: <detail>`.
- [ ] [PROT-16] A fetched state with no `refs/aco/state` at all denies `cannot reach refs/aco/state: <sentence>`, `<sentence>` the one `specs/ref-store-cas.spec.md` CAS-03 already owns.
- [ ] [PROT-17] Any other uncaught exception denies `{"decision": "deny", "reason": "<message>"}`, that exception's own bare text, no traceback.
- [ ] [PROT-18] This session holding no live claim on the checkout's own branch at all denies `claim first`.
- [ ] [PROT-19] A live claim for this session and branch whose scope misses the path denies `claim first`, the same reason as no claim at all, for every tool but `apply_patch`.
- [ ] [PROT-20] The same scope miss under `apply_patch` denies `<path> outside claim scope`, naming the one path the payload's own grammar can name.
- [ ] [PROT-21] A live claim covering the path allows (PROT-01) (see E-PROT-01).
- [ ] [PROT-22] A lane (issueless) claim covering the path allows (PROT-01) exactly like an issue claim.

## `apply_patch`'s own multi-path payload

`apply_patch` (Codex) carries no path key: its `command` is a whole patch
text, parsed for every `*** Add File:`, `*** Delete File:`, and `*** Update
File:` header line. A `*** Move to:` line is recognized only immediately
after an `*** Update File:` header, before any hunk content; anywhere else
in the patch, that line is not a header the grammar admits, so the whole
patch fails to parse and `protect` denies `path required` (PROT-07)
fail-closed rather than guessing which paths it touches.

- [ ] [PROT-23] Every path an `apply_patch` command touches sitting inside the live claim's own scope allows (see E-PROT-04).
- [ ] [PROT-24] A command touching several paths denies naming the first one outside scope, in the patch's own order, not a generic `claim first` (see E-PROT-04).
- [ ] [PROT-25] Two paths in one command sitting in two different linked worktrees of the same repository are judged in their own checkout each; the first denial, `claim first` or otherwise, wins.
- [ ] [PROT-26] Same-repository `apply_patch` paths share one live snapshot: a claim change made mid-call cannot flip the second path's `claim first`/`outside claim scope`/allow verdict (see E-PROT-04).

## `NotebookEdit`'s own path key

- [ ] [PROT-27] `NotebookEdit` reads its target only from `notebook_path`, ignoring a decoy `path` key sitting beside it that this tool never actually sends.

## `Bash`'s own command-text payload

`Bash` -- and `Monitor`, whose `command` is a shell script too -- carries no
path key at all: its `command` text is scanned for a
short, fixed list of write patterns -- a real, unquoted `>`/`>>` redirection
(a heredoc target such as `cat > path <<EOF` included), `tee`'s own file
operands, `sed -i` (or `-i<suffix>`/`--in-place[=suffix]`, skipping
`-e`/`--expression`/`-f`/`--file`, `-l`/`--line-length`, and their own
values -- a lone remaining operand after a spelled-out suffix is judged as
the file it edits, since sed cannot otherwise write anywhere), `mv` (every
operand -- a source vanishes exactly like its destination is written), `cp`
(a `-t`/`--target-directory` value when given, attached or separate, e.g.
`-t/tmp`/`--target-directory=/tmp`, otherwise its last operand -- either
way, the only one it actually writes), `rm`, `git checkout`
(`-f`/`--ours`/`--theirs` before a literal `--`, then its path operands),
and `git restore` (skipping `-s`/`--source`, `--conflict`, and
`--pathspec-from-file` and their own values, and a literal `--`) -- each
occurrence naming a `(pattern, path)` pair, in the order the command names
them (issue #380). A literal `--` ends option parsing the same way Bash's
own coreutils do: every operand after it is a path regardless of a leading
`-` (`rm -- -f` judges `-f`). A quoted or backslash-escaped occurrence of a
character that would otherwise be an operator (`echo '>' > f`) is data,
never the operator it merely reads like -- but a quoted or backslash-escaped
*command name* (`'rm' f`, `r\m f`) still executes exactly as Bash runs it
and is recognized like the plain spelling. An unquoted `#` at the start of a
word is a comment to the end of its own physical line, never scanned for a
pattern of its own, exactly like Bash itself never runs what follows it on
that line; an unquoted or double-quoted, trailing backslash-newline joins
the next physical line first -- never inside single quotes, which keep it
literal -- so a command split that way is judged exactly like the one line
it forms. `git checkout <branch>` (no `--`) and a plain `sed` without
`-i`/`--in-place` name no path at all, since neither writes a file; neither
does a redirect whose own target is exactly `/dev/null`, or a
file-descriptor-duplication form (`2>&1`, `>&2`) -- its own "target" is
another operator, never a real file. Unlike every other tool's own
already-absolute payload path, a Bash pattern's own path is relative to the
shell's own working directory: it resolves against the payload's own `cwd`
field, updated by every literal, resolvable `cd` the command names first
(PROT-34), rather than the hook process's cwd, and PROT-09 does not apply to
it at all. That directory changes for the rest of the enclosing
`;`/`&&`/`||`/newline-separated list, never across a `|` -- a pipeline
segment is its own subshell, so a `cd` on either side of one changes nothing
outside it -- and a parenthesised `( ... )` group keeps its own copy that
reverts at its own closing `)`, exactly like Bash's own subshell scoping.
Every resolved path then runs the same Outside-Repository, Checkout,
Default-Branch, and Claim-Scope gates a mutating tool's own path runs
(PROT-32 outside every repository, PROT-38 an ignored `.claude/` setting,
PROT-11 no commit yet, PROT-12/PROT-13 not main, PROT-14 the checkout root
-- including a path that names a linked worktree's own root directory
exactly, judged by that
checkout rather than by its parent, the store's own
PROT-29/PROT-15/PROT-16/PROT-17, PROT-21/PROT-22 a covering claim), except a
scope miss denies naming both the recognized pattern and the path rather
than a bare `claim first`.

- [ ] [PROT-30] A `command` naming none of these patterns -- or no string `command` at all -- allows without resolving identity, git, or the store.
- [ ] [PROT-31] A recognized pattern's relative path resolves against the payload's own `cwd`, as PROT-34 updates it; with no known directory, that path allows outright, before identity resolves.
- [ ] [PROT-33] A recognized pattern's own path outside the live claim's scope denies `<pattern> <path> outside claim scope`, naming both (see E-PROT-08).
- [ ] [PROT-34] A literal, resolvable `cd` changes the directory every later path in its own `;`/`&&`/`||`/newline list resolves against -- never across a `|`, and only inside its own group.
- [ ] [PROT-35] An unresolvable `cd` target -- expandable, `-`, or no operand -- ends recognition for the rest of the command outright, allowing it (see E-PROT-10).

## Forge-free

- [ ] [PROT-28] `protect` never resolves an item forge: an OWNER/REPO `--repo` or a non-GitHub canonical remote leaves allow and deny alike unaffected; any other `--repo` refuses first (OUT-08).

## Never

- `protect` never reads the store for a verdict the checkout resolves alone: a "not main", "no commit on this branch", "relative payload path", or "path required" deny, a path no claim can ever cover (PROT-14, PROT-42, PROT-43), a path outside every repository, or one in an unguarded repository, touches `store.fetch_state` zero times.
- `protect` never exempts a guarded repository through an unguarded directory: its linked worktree placed there, a directory symlink into it, or a write through a file symlink from an unguarded repository into it is judged by the guarded checkout it lands in too, never by the link's own alone (PROT-40, PROT-44).
- `protect` never lets a claim in one checkout authorize a write through a symlink whose bytes land in another: a link in a claimed worktree into a main checkout, a nested one included, denies `not main`, and one into another repository's git directory PROT-43's sentence (PROT-44). A recognized `rm` or `mv` of a file link itself never touches its target and stays the link's checkout's; a directory link earlier in the path is followed by git, so only its target's checkout judges the write.
- `protect` never allows a write through a file symlink outside every repository as outside when its target lies in a checkout: that checkout judges the write (PROT-12 in a main checkout). A recognized `rm` or `mv` of the link itself never touches its target and stays outside (PROT-32).
- `protect` never reads a git failure as outside every repository: a path below a `.git` file or a `.git` directory denies when git cannot tell which checkout it is, with that failure's text (PROT-17); inside a git directory itself, PROT-43's sentence.
- `protect` never reads a git failure as an unguarded repository: PROT-40 weighs only a checkout git has resolved.
- `protect` never opens `.claude/` by its name alone: a tracked file there, or an untracked one git does not ignore, is judged like any other path (PROT-12 in the main checkout).
- The escape exists so a session can switch off a misconfigured hook in its own ignored `settings.local.json` without the operator; no claim can cover a file that never reaches a commit.
- `protect` never defaults an unrecognized tool name to allowed: PROT-06 fails closed instead.
- `protect` never trusts a relative payload path by joining it to the hook process's own cwd, even from the one cwd where that guess would happen to be correct.
- `protect` never accepts `--json`: its output is the hook protocol of PROT-01/PROT-02 (a silent exit `0`, or the deny object on stdout plus the sentence on stderr, exit `2`), not the `--json` envelope (`specs/output.spec.md`) (a malformed `--repo` refuses first, OUT-08).
- `protect` never writes a file: every denial and every allow leaves `$HOME` and the checkout untouched.
- `protect` never reads working-tree dirtiness: a dirty checkout still allows a covered write, unlike `claim`'s own precondition.
- `protect` never binds the resolved checkout's `HEAD` to a claim's own `base`: it judges the live claim's branch and scope alone.
- `shell` and other providers' equivalents never deny a missing path: the hook payload names no file path for those, so `protect` cannot gate what it cannot see (README, "PreToolUse write gate").
- `Bash` (issue #380) and `Monitor` (issue #448) are the one exception, judging only the fixed pattern list PROT-30 owns.
- A `python -c ...` one-liner or an opaque script invocation stays invisible on purpose: recognizing a pattern is a best-effort aid against forgetting the claim, never a security boundary.
- `protect` never guesses a Bash-recognized relative path's `cwd` from the hook process's own cwd: a payload naming no `cwd` allows that path outright (PROT-31).
- This is PROT-09's own "never guess a relative path" principle, applied as an allow instead of a deny since Bash's own path is expected to be relative.
- A Bash pattern never judges what only the shell could resolve: an operand with an unquoted (or double-quoted, still-substituting) `$name`, `` `command` ``, `~`, `*`, `?`, or `[` is never judged.
- Such an operand allows the same as naming no pattern at all: `protect` cannot know what a variable, glob, or substitution expands to without executing the command.
- Everything between an unquoted `<<WORD`/`<<-WORD`/`<<'WORD'` and its terminator line is heredoc body, never scanned for a write pattern of its own.
- Only the command line naming the heredoc is judged, so a body that merely reads like `rm docs/file` names nothing.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`, a
tracked `.agent-claim/board.toml` naming no `storage` key, and `ACO_AGENT`
set to `Ada`; `<worktree>` and `<main>` are its own linked-worktree and
shared-main directories. Every session pipes the hook's JSON payload on
stdin, exactly as a `PreToolUse` hook call does.

### E-PROT-01 -- a covered write allows

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, already `aco claim 42 --scope README.md`

```console
$ echo '{"toolName": "Write", "toolInput": {"file_path": "<worktree>/README.md"}}' | aco protect
exit 0
```

### E-PROT-02 -- no covering claim denies `claim first`

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, no live claim

```console
$ echo '{"toolName": "Write", "toolInput": {"file_path": "<worktree>/README.md"}}' | aco protect
{"decision": "deny", "reason": "claim first"}
2> claim first
exit 2
```

### E-PROT-03 -- the shared main checkout denies `not main`

Setup: bare-remote, bootstrapped, no live claim

```console
$ echo '{"toolName": "Write", "toolInput": {"file_path": "<main>/README.md"}}' | aco protect
{"decision": "deny", "reason": "not main"}
2> not main
exit 2
```

### E-PROT-04 -- `apply_patch` names the one path outside scope

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, already `aco claim 42 --scope src`

```console
$ echo '{"toolName": "apply_patch", "toolInput": {"command": "*** Begin Patch\n*** Update File: <worktree>/src/widget.py\n@@\n-old\n+new\n*** Add File: <worktree>/docs/widget.md\n+content\n*** End Patch"}}' | aco protect
{"decision": "deny", "reason": "docs/widget.md outside claim scope"}
2> docs/widget.md outside claim scope
exit 2
```

### E-PROT-05 -- a read-effect tool always allows

Setup: bare-remote, no live claim

```console
$ echo '{"toolName": "Read", "toolInput": {"path": "src/secret.py"}}' | aco protect
exit 0
$ echo '{"tool_name": "StructuredOutput", "tool_input": {"pr": 1}}' | aco protect
exit 0
```

### E-PROT-06 -- an unrecognized tool name fails closed

Setup: bare-remote, no live claim

```console
$ echo '{"toolName": "invented_tool"}' | aco protect
{"decision": "deny", "reason": "'invented_tool' is not in aco's hook tool table (HOOK_TOOL_EFFECTS, issue #238); add it there as read-only or mutating before use"}
2> 'invented_tool' is not in aco's hook tool table (HOOK_TOOL_EFFECTS, issue #238); add it there as read-only or mutating before use
exit 2
```

### E-PROT-07 -- a relative payload path denies outright

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, already `aco claim 42 --scope src`, cwd is `<worktree>`

```console
$ echo '{"toolName": "Write", "toolInput": {"path": "src/widget.py"}}' | aco protect
{"decision": "deny", "reason": "relative payload path"}
2> relative payload path
exit 2
```

### E-PROT-08 -- a Bash write pattern outside claim scope denies naming both

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, already `aco claim 42 --scope src`, cwd is `<worktree>`

```console
$ echo '{"toolName": "Bash", "toolInput": {"command": "sed -i \"s/a/b/\" docs/widget.md"}, "cwd": "<worktree>"}' | aco protect
{"decision": "deny", "reason": "sed -i docs/widget.md outside claim scope"}
2> sed -i docs/widget.md outside claim scope
exit 2
```

### E-PROT-09 -- a Bash command outside every repository, or naming no pattern, allows

Setup: bare-remote, no live claim

```console
$ echo '{"toolName": "Bash", "toolInput": {"command": "rm /tmp/scratch.txt"}}' | aco protect
exit 0
$ echo '{"toolName": "Bash", "toolInput": {"command": "git diff"}}' | aco protect
exit 0
```

### E-PROT-10 -- `cd` tracks the directory, an unresolvable one allows

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, already `aco claim 42 --scope src`, cwd is `<worktree>`

```console
$ echo '{"toolName": "Bash", "toolInput": {"command": "cd docs && rm widget.md"}, "cwd": "<worktree>"}' | aco protect
{"decision": "deny", "reason": "rm docs/widget.md outside claim scope"}
2> rm docs/widget.md outside claim scope
exit 2
$ echo '{"toolName": "Bash", "toolInput": {"command": "cd $SCRATCH && rm widget.md"}, "cwd": "<worktree>"}' | aco protect
exit 0
```

### E-PROT-11 -- a write outside every repository allows, without identity

Setup: bare-remote, no live claim, `ACO_AGENT` unset

```console
$ echo '{"tool_name": "Write", "tool_input": {"file_path": "/tmp/scratch/notes.md"}}' | aco protect
exit 0
```

### E-PROT-12 -- the session's own ignored `.claude/` settings stay writable

Setup: bare-remote, bootstrapped, a tracked `.claude/settings.json`, `.claude/settings.local.json` excluded by `.git/info/exclude`, no live claim, `ACO_AGENT` unset

```console
$ echo '{"tool_name": "Edit", "tool_input": {"file_path": "<main>/.claude/settings.local.json"}}' | aco protect
exit 0
$ echo '{"tool_name": "Edit", "tool_input": {"file_path": "<main>/.claude/settings.json"}}' | aco protect
{"decision": "deny", "reason": "not main"}
2> not main
exit 2
```

### E-PROT-13 -- an unguarded scratchpad repository writes freely, a malformed entry fails closed

Setup: a throwaway main checkout `/tmp/scratch/play` with one commit, no live claim, `ACO_AGENT` unset

```console
$ echo '{"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/scratch/play"}}' | ACO_PROTECT_UNGUARDED=/tmp/scratch aco protect
exit 0
$ echo '{"tool_name": "Write", "tool_input": {"file_path": "/tmp/scratch/play/notes.md"}}' | ACO_PROTECT_UNGUARDED=scratch aco protect
{"decision": "deny", "reason": "ACO_PROTECT_UNGUARDED: scratch is not an absolute directory"}
2> ACO_PROTECT_UNGUARDED: scratch is not an absolute directory
exit 2
```

### E-PROT-14 -- a path no claim can ever cover names why

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, a tracked `README.md`, a bare repository `/srv/served.git`

```console
$ echo '{"tool_name": "Write", "tool_input": {"file_path": "<worktree>/README.md/x.py"}}' | aco protect
{"decision": "deny", "reason": "<worktree>/README.md/x.py cannot exist: <worktree>/README.md is a file"}
2> <worktree>/README.md/x.py cannot exist: <worktree>/README.md is a file
exit 2
$ echo '{"tool_name": "Bash", "tool_input": {"command": "rm -rf <worktree>"}}' | aco protect
{"decision": "deny", "reason": "<worktree> is the checkout root itself"}
2> <worktree> is the checkout root itself
exit 2
$ echo '{"tool_name": "Write", "tool_input": {"file_path": "/srv/served.git/hooks/pre-receive"}}' | aco protect
{"decision": "deny", "reason": "not a checkout: /srv/served.git is a git directory"}
2> not a checkout: /srv/served.git is a git directory
exit 2
```

### E-PROT-15 -- a canonical remote with no URL configured is named

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, the tracked `.agent-claim/board.toml` naming `canonical_remote = "upstream"`, no remote `upstream` configured

```console
$ echo '{"tool_name": "Write", "tool_input": {"file_path": "<worktree>/README.md"}}' | aco protect
{"decision": "deny", "reason": "cannot determine the trunk: canonical remote 'upstream' is not configured"}
2> cannot determine the trunk: canonical remote 'upstream' is not configured
exit 2
```
