"""Forge adapter over `refs/aco/state`'s `items/` tree (issues #248, #283).

`StateRefBoard` sits beside `github.GitHubForge` behind the same `forge`
port: both talk to a different backend for the same board data, and
neither imports the other. Unlike `GitHubForge`, this adapter performs no
IO of its own -- the Layers contract puts `store` above this module, so
`cli._state_ref_forge` reads `items/`'s raw bytes and blob oids through
`store.read_item_files`/`ClaimState.items` and hands them to the
constructor; every read method below is a pure projection over that
already-fetched data. Every write (`create_item`, `create_child`,
`update_item_body`, `close_item`, `link_child`) instead calls the injected `ItemWriter`
port: one
compare-and-swap write to `items/<id>.md`, implemented in `cli.py` over
`store` (hash-object once, then one `commit_transition` with an
`ItemWriteIntent`, issue #279; `close_item` through the port's own
`close_item` and an `ItemCloseIntent`, issue #459) -- so this module still
never imports `store` itself.

`LANDING` and the two pull-request listings answer `Capability.UNSUPPORTED`:
this adapter has no data for any of them. The two pull-request listings
still return an empty tuple rather than raising: `cli._board` calls them
unconditionally for every board read, and "no pull requests exist here" is
this adapter's honest answer, not a refusal. `Board.recovery` -- purely
pull-request-body-declared -- stays empty from that same emptiness.
`Stage.CODE_LANDED` and the board's own Landungen view do not: both read
`checkout.trunk_landings`'s trailer block straight from local git history
instead (issues #304, #371), independent of this adapter's own missing
pull-request data.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Protocol, cast

from . import board, forge, items
from .body import (
    RECORD_KEY,
    BodyReadState,
    ContractDefect,
    ItemKind,
    Storage,
    UnreadParent,
    body_defect_text,
    locate_agent_claim_block,
    parse_body,
    readable_record_parent,
    readable_record_title,
    replace_agent_claim_block,
)
from .protocol import (
    ClaimUnavailableError,
    MalformedStateTreeError,
    ObjectId,
    item_id_of_filename,
)

STATE_REF_CAPABILITIES: Mapping[forge.ForgeOperation, forge.Capability] = MappingProxyType(
    {
        forge.ForgeOperation.ITEM_REFERENCE: forge.Capability.READ_ONLY,
        forge.ForgeOperation.ITEM_REFERENCES: forge.Capability.READ_ONLY,
        forge.ForgeOperation.PARENT_ISSUE: forge.Capability.READ_ONLY,
        forge.ForgeOperation.PARENT_NUMBER: forge.Capability.READ_ONLY,
        forge.ForgeOperation.LIST_CHILDREN: forge.Capability.READ_ONLY,
        forge.ForgeOperation.DEFAULT_BRANCH: forge.Capability.READ_ONLY,
        forge.ForgeOperation.LIST_OPEN_BOARD_ISSUES: forge.Capability.READ_ONLY,
        forge.ForgeOperation.LIST_BOARD_DEPENDENCIES: forge.Capability.READ_ONLY,
        forge.ForgeOperation.LANDING: forge.Capability.UNSUPPORTED,
        forge.ForgeOperation.LIST_OPEN_BOARD_PULL_REQUESTS: forge.Capability.UNSUPPORTED,
        forge.ForgeOperation.LIST_RECENT_MERGED_BOARD_PULL_REQUESTS: forge.Capability.UNSUPPORTED,
        forge.ForgeOperation.LIST_RECENTLY_CLOSED_ISSUES: forge.Capability.READ_ONLY,
        forge.ForgeOperation.LINK_CHILD: forge.Capability.READ_WRITE,
        # `item new` creates a state-ref item through `compose_item` and `create_item`, which
        # mint its id and record its parent and origin in one write.
        forge.ForgeOperation.CREATE_ISSUE: forge.Capability.UNSUPPORTED,
        forge.ForgeOperation.CREATE_CHILD: forge.Capability.READ_WRITE,
        forge.ForgeOperation.UPDATE_ITEM_BODY: forge.Capability.READ_WRITE,
        forge.ForgeOperation.SET_ITEM_KIND: forge.Capability.READ_WRITE,
    }
)


class ItemWriter(Protocol):
    """The write port every mutating `StateRefBoard` operation composes onto
    (issue #283): one CAS write to `items/<item_id>.md` -- `expected` the
    oid the caller's own already-read snapshot carries (`None` for "must not
    exist yet"), `content` the item's finished new bytes -- returning the
    freshly written blob's oid, so the caller can update its own in-memory
    state without a re-fetch. Implemented in `cli.py`, over `store`
    (hash-object once, then one `commit_transition` with an
    `ItemWriteIntent`, issue #279, or for a close an `ItemCloseIntent`,
    issue #459): this module may not import `store` itself (Layers
    contract), so every actual git call for an item write stays behind
    this port's two methods.
    """

    def write_item(
        self,
        item_id: str,
        *,
        expected: ObjectId | None,
        content: bytes,
        store_expected: Mapping[str, ObjectId] | None,
    ) -> ObjectId: ...

    def close_item(
        self,
        item_id: str,
        *,
        number: int,
        expected: ObjectId,
        content: bytes,
        store_expected: Mapping[str, ObjectId] | None,
    ) -> ObjectId:
        """`write_item`'s own CAS for `item close` (issue #459), refused on
        every attempt while item `number` still carries a live claim."""
        ...


NO_LANDINGS_YET = (
    "landings are not yet derived from the state ref; "
    "#230 slice 6 adds merge-commit-derived landings"
)
NO_BARE_ISSUE = "a state-ref item is created by aco item new, never as a bare forge issue"
_BLOCKER_ITSELF = "is listed as its own blocker"


def _unknown_item_sentence(item_id: str) -> str:
    return f"item {item_id} does not exist"


def _missing_parent_sentence(parent_id: str) -> str:
    """PIN-16's sentence."""
    return f"item {parent_id} is referenced as a parent but does not exist"


def _missing_blocker_sentence(item_id: str, blocker_id: str) -> str:
    """PIN-17's sentence, naming the item that lists the missing blocker
    as ITEM-43's names the one repeating it, so the repair needs no
    search (issue #548)."""
    return f"item {item_id} lists blocker {blocker_id}, which does not exist"


def _repeated_blocker_sentence(item_id: str, blocker_id: str) -> str:
    """ITEM-43's sentence, shared by the write that refuses a delivered
    repeat and the read that refuses a stored one (issue #546)."""
    return f"item {item_id} lists blocker {blocker_id} more than once"


@dataclass(frozen=True)
class _DecodedItem:
    record: items.ItemRecord
    body: str
    oid: ObjectId


@dataclass(frozen=True)
class _MalformedItem:
    """An item file whose bytes decode to no valid `agent-claim` block with
    a `[record]` table (issue #447): kept aside rather than refusing the
    store at decode, so a read of any other item still answers while this
    item's own read refuses, and `board`/`next` list it by `defect` rather
    than refusing the whole store (issue #517). `problem` completes the
    sentence `item <id> ...`; `oid` is the CAS `expected` a repairing
    `update_item_body` writes over; `text` is whatever of the file still
    decodes; `title` is the record's title when it alone still reads, for
    the twin search and the board's row; `parent` is the record's parent
    when it alone still reads, so that container counts this item as an
    open child, `None` for a top-level record, and `UnreadParent.UNREAD`
    when the record or its parent does not read."""

    problem: str
    defect: ContractDefect
    oid: ObjectId
    text: str = ""
    title: str | None = None
    parent: str | UnreadParent | None = UnreadParent.UNREAD

    def may_be_child_of(self, item_id: str, kind: ItemKind | None) -> bool:
        """Whether this item may be `item_id`'s child, `kind` being
        `item_id`'s own: its readable parent names it, or its parent does not
        read and `item_id` is a Container, the one kind that takes children
        (ITEM-45, ITEM-54). The one answer the board's child count and a
        close or retype's refusal both ask (issue #550)."""
        if self.parent is UnreadParent.UNREAD:
            return kind is ItemKind.CONTAINER
        return self.parent == item_id


# The defects an item file the block grammar never reached is named by
# (issue #517): its bytes are no UTF-8, or its valid block has no record.
_NOT_UTF8 = ContractDefect("item", "item file is not valid UTF-8")
_NO_RECORD = ContractDefect(RECORD_KEY, "no [record] table")


@dataclass(frozen=True)
class LandingWrite:
    """One item's own close write, composed but not yet applied (issue
    #359): `item_id`/`expected`/`content` are exactly the CAS write
    `close_item` performs immediately today, staged instead so `release
    --merged <sha|empty>` can hash `content` into a blob itself and fold
    the result into one atomic `protocol.LandingIntent` alongside the claim
    it releases. `record` is the item's own already-closed record, carried
    along so `mark_landed` can fold the committed write back into this
    instance's in-memory view without re-decoding `content`."""

    item_id: str
    expected: ObjectId
    content: bytes
    record: items.ItemRecord


@dataclass(frozen=True)
class NewItemWrite:
    """A fresh item's write, composed and checked readable but not yet
    applied (issue #517): `compose_item` builds it, `create_item` writes
    it."""

    item_id: str
    record: items.ItemRecord
    body: str


def _valid_record(text: str) -> Mapping[str, object] | None:
    """`text`'s own `[record]` table when its `agent-claim` block is VALID
    under `Storage.STATE_REF` -- the same block grammar `body.py` already
    reads, gated open to `record` only there -- else `None`."""
    parsed = parse_body(text, storage=Storage.STATE_REF)
    return parsed.record if parsed.read_state is BodyReadState.VALID else None


def _readable_content(text: str) -> bytes:
    """`text`, a body `_with_record` composed for a write, as the bytes to
    store -- refused before any write when the read would set it aside
    (issue #517): the composed `[record]` is always present, so the parse's
    own first defect is the whole reason. A text holding a lone surrogate
    (an argv byte that was no UTF-8) has no UTF-8 bytes at all, so it is
    refused with the read's own not-UTF-8 defect."""
    try:
        content = text.encode("utf-8")
    except UnicodeEncodeError:
        raise _unreadable_body_refusal(_NOT_UTF8) from None
    defects = parse_body(text, storage=Storage.STATE_REF).contract.defects
    if defects:
        raise _unreadable_body_refusal(defects[0])
    return content


def _unreadable_body_refusal(defect: ContractDefect) -> ClaimUnavailableError:
    """ITEM-52's refusal of a write whose stored body the read would set
    aside for `defect`."""
    return ClaimUnavailableError(
        f"{body_defect_text(defect)}; stored, that body would not read back, so nothing was written"
    )


def _decode_item(item_id: str, content: bytes, oid: ObjectId) -> _DecodedItem | _MalformedItem:
    """`content` turned into a `_DecodedItem`, or set aside as a
    `_MalformedItem` (issue #447): every item file must be UTF-8 text whose
    block parses VALID with a `[record]` table; one that does not refuses
    its own read, never another item's."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return _MalformedItem(problem="is not valid UTF-8", defect=_NOT_UTF8, oid=oid)
    parsed = parse_body(text, storage=Storage.STATE_REF)
    if parsed.read_state is not BodyReadState.VALID or parsed.record is None:
        return _MalformedItem(
            problem="has a malformed agent-claim block",
            defect=(parsed.contract.defects or (_NO_RECORD,))[0],
            oid=oid,
            text=text,
            title=readable_record_title(text),
            parent=readable_record_parent(text),
        )
    return _DecodedItem(record=items.parse_item_record(item_id, parsed.record), body=text, oid=oid)


def _malformed_item_refusal(item_id: str, malformed: _MalformedItem) -> MalformedStateTreeError:
    return MalformedStateTreeError(
        f"item {item_id} {malformed.problem}; repair it with aco item edit {item_id} "
        "and a body whose agent-claim block carries a valid [record]"
    )


def _with_record(body: str, record: items.ItemRecord) -> str:
    """`body`'s `agent-claim` block, its `[record]` table replaced by
    `record`'s own fields, every other byte untouched -- the one place a
    write composes a fresh `[record]` table, shared by `create_child` (a
    brand new one), `update_item_body` (an existing one with `updated_at`
    refreshed), and `close_item` (an existing one moved to `CLOSED`)."""
    located = locate_agent_claim_block(body)
    new_data = {**located.data, RECORD_KEY: items.record_table(record)}
    return replace_agent_claim_block(body, located, new_data)


def _item_kind(kind: str | None) -> ItemKind | None:
    return ItemKind(kind) if kind is not None else None


def _delivered_content_fields(
    body: str, stored: items.ItemRecord
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """`update_item_body`'s own owner split for `title`, `labels`,
    `blocked_by` (issue #287, `aco item edit`'s ruled record merge): taken
    from `body`'s own `[record]` table when the delivered body carries one
    valid -- read off the table's raw TOML presence, never
    `items.parse_item_record`'s own `.get(key, [])` default, which would
    turn an omitted `labels`/`blocked_by` into an emptied one rather than
    `stored`'s own value. A body with no `[record]` at all -- every
    `rule`/`ask`/`cut` write already composes one -- changes nothing here,
    exactly `update_item_body`'s behaviour before this issue. Every other
    record field (`parent`, `state`, `origin`, `kind`, the three
    timestamps) stays `stored`'s own regardless of what a delivered record
    names for it -- `update_item_body` itself, never this helper, owns
    that half of the split."""
    parsed = parse_body(body, storage=Storage.STATE_REF)
    delivered = parsed.record
    if parsed.read_state is not BodyReadState.VALID or delivered is None:
        return stored.title, stored.labels, stored.blocked_by
    title = cast(str, delivered["title"]).strip() if "title" in delivered else stored.title
    labels = (
        tuple(cast("list[str]", delivered["labels"])) if "labels" in delivered else stored.labels
    )
    blocked_by = (
        tuple(cast("list[str]", delivered["blocked_by"]))
        if "blocked_by" in delivered
        else stored.blocked_by
    )
    return title, labels, blocked_by


class StateRefBoard:
    """The `state-ref` storage pin's `BoardSource`/`ForgeReader`/`ForgeWriter`
    adapter."""

    def __init__(
        self,
        *,
        repository: forge.RepositoryId,
        default_branch: str,
        item_files: Mapping[str, bytes],
        item_oids: Mapping[str, ObjectId],
        writer: ItemWriter,
    ) -> None:
        self.repository = repository
        self._default_branch = default_branch
        self._writer = writer
        self._items: dict[str, _DecodedItem] = {}
        self._malformed: dict[str, _MalformedItem] = {}
        self._holds_items = False
        self._foreign_filenames = sorted(
            filename for filename in item_files if item_id_of_filename(filename) is None
        )
        for filename, content in item_files.items():
            item_id = item_id_of_filename(filename)
            if item_id is None:
                continue
            decoded = _decode_item(item_id, content, item_oids[item_id])
            if isinstance(decoded, _MalformedItem):
                self._malformed[item_id] = decoded
            else:
                self._items[item_id] = decoded
        self._by_number = {
            items.item_number(item_id): item_id for item_id in (*self._items, *self._malformed)
        }

    @property
    def requests(self) -> int:
        """Always zero: every byte this adapter reads was already fetched
        by `cli._state_ref_forge` before construction (issue #248) -- no
        method below costs a further round trip."""
        return 0

    def capability(self, operation: forge.ForgeOperation) -> forge.Capability:
        return STATE_REF_CAPABILITIES[operation]

    def holds(self, number: int) -> bool:
        """Whether `items/` carries `number` at all, malformed or not: the
        one existence check `aco item edit` needs, since it is also the
        repair path for a malformed item every other read refuses."""
        return number in self._by_number

    def _refuse_a_foreign_entry(self) -> None:
        """PIN-13's refusal by the lowest name while `items/` holds an entry
        whose file name names no item (issue #565): a read of the whole store
        cannot say what such an entry is, so leaving it out would let the
        board lie. A write to one item never reads it, and the store carries
        it byte for byte (CAS-61), so such a write goes past it."""
        if self._foreign_filenames:
            items.item_id_from_filename(self._foreign_filenames[0])

    def hold_well_formed(self) -> None:
        """Refuses with the lowest malformed item's own sentence and repair
        while `items/` holds any, then `hold_items` through every later write
        of this instance (issue #447): a malformed item's parent, state, and
        blockers are unknown, so `board --serve`'s ruling click would guess
        past it -- the one whole-board write that keeps PIN-29 "before any
        write". Reads that only project the board list it instead (issue
        #517). An entry that names no item refuses the click as it refuses
        every whole-store read (PIN-13, issue #565)."""
        self._refuse_a_foreign_entry()
        if self._malformed:
            item_id = min(self._malformed)
            raise _malformed_item_refusal(item_id, self._malformed[item_id])
        self.hold_items()

    def hold_items(self) -> None:
        """Every later write of this instance commits only onto the very
        `items/` this instance read (issue #447), so an item written or gone
        bad after the caller's check refuses the write instead of landing
        beside it."""
        self._holds_items = True

    def _write_item(self, item_id: str, *, expected: ObjectId | None, body: str) -> ObjectId:
        return self._writer.write_item(
            item_id,
            expected=expected,
            content=_readable_content(body),
            store_expected=self._store_expected(),
        )

    def _store_expected(self) -> Mapping[str, ObjectId] | None:
        """The whole `items/` map every write of a `hold_items`
        instance commits onto (issue #447), else `None`."""
        if not self._holds_items:
            return None
        return {
            **{held_id: held.oid for held_id, held in self._malformed.items()},
            **{held_id: held.oid for held_id, held in self._items.items()},
        }

    def _decoded(self, number: int) -> _DecodedItem | None:
        item_id = self._by_number.get(number)
        return (
            None
            if item_id is None
            else self._related(item_id, missing=_unknown_item_sentence(item_id))
        )

    def _related(self, item_id: str, *, missing: str) -> _DecodedItem:
        """`item_id`'s decoded item, or a refusal by name: a malformed one
        names its repair (issue #447), an unknown one refuses the sentence
        `missing`."""
        if not self._carries(item_id):
            raise MalformedStateTreeError(missing)
        malformed = self._malformed.get(item_id)
        if malformed is not None:
            raise _malformed_item_refusal(item_id, malformed)
        return self._items[item_id]

    def _carries(self, item_id: str) -> bool:
        """Whether `items/` holds `item_id` at all, malformed or not: the one
        existence test behind PIN-16/PIN-17's "does not exist"."""
        return item_id in self._items or item_id in self._malformed

    def _issue(self, item_id: str) -> board.Issue:
        decoded = self._items[item_id]
        record = decoded.record
        kind = _item_kind(record.kind)
        children_closed = children_total = None
        if kind is ItemKind.CONTAINER:
            children = self._children(item_id)
            children_total = len(children)
            children_closed = sum(1 for child in children if child.state is board.ChildState.CLOSED)
        return board.Issue(
            record.number,
            record.title,
            record.labels,
            decoded.body,
            record.created_at,
            record.updated_at,
            kind,
            children_closed,
            children_total,
            len(record.blocked_by),
        )

    def item_reference(self, number: int) -> forge.ItemReference:
        decoded = self._decoded(number)
        if decoded is None:
            return forge.ItemReference(forge.ItemState.MISSING)
        state = (
            forge.ItemState.OPEN
            if decoded.record.state is items.RecordState.OPEN
            else forge.ItemState.CLOSED
        )
        return forge.ItemReference(
            state, decoded.record.title, decoded.body, False, decoded.record.origin
        )

    def item_references(self, numbers: Iterable[int]) -> Mapping[int, forge.ItemReference]:
        """Every one of `numbers`, read straight from the already-fetched
        `items/` tree (issue #440): this adapter performs no IO of its own at
        all (module docstring), so there is no round trip here to batch --
        `item_reference` per number costs the same as this already does."""
        return {number: self.item_reference(number) for number in numbers}

    def landing(self, number: int) -> forge.Landing:
        raise forge.ForgeUnsupportedError(NO_LANDINGS_YET)

    def parent_number(self, number: int) -> int | None:
        """`number`'s recorded parent, off its own record alone: never
        decoding that parent, so `item show` still names a malformed one
        (issue #447), while PIN-16 still refuses one `items/` lacks."""
        decoded = self._decoded(number)
        if decoded is None or decoded.record.parent is None:
            return None
        parent_id = decoded.record.parent
        if not self._carries(parent_id):
            raise MalformedStateTreeError(_missing_parent_sentence(parent_id))
        return items.item_number(parent_id)

    def parent_issue(self, number: int) -> board.ParentIssue | None:
        """`number`'s parent as the board reads it; an unreadable parent
        (issue #517) is answered by its reference and whatever of its text
        still decodes, its kind unknown, so the child still reads as nested
        rather than refusing the whole board."""
        parent_number = self.parent_number(number)
        if parent_number is None:
            return None
        parent_id = self._by_number[parent_number]
        reference = board.IssueReference(self.repository.path, parent_number)
        malformed = self._malformed.get(parent_id)
        if malformed is not None:
            return board.ParentIssue(reference, malformed.text, None)
        parent = self._items[parent_id]
        return board.ParentIssue(reference, parent.body, _item_kind(parent.record.kind))

    def list_children(self, number: int) -> tuple[board.ChildItem, ...]:
        item_id = self._by_number.get(number)
        return () if item_id is None else self._children(item_id)

    def _children(self, item_id: str) -> tuple[board.ChildItem, ...]:
        """`item_id`'s children, every unreadable item that may be one
        counted open (issues #517, #550): its state may not read, so its
        container never reads as childless past it, and the board offers no
        close that `item close` would refuse by it (ITEM-54)."""
        decoded = self._items.get(item_id)
        kind = None if decoded is None else _item_kind(decoded.record.kind)
        return (
            *(
                board.ChildItem(child.record.number, board.ChildState(child.record.state.value))
                for child in self._items.values()
                if child.record.parent == item_id
            ),
            *(
                board.ChildItem(items.item_number(child_id), board.ChildState.OPEN)
                for child_id, child in self._malformed.items()
                if child.may_be_child_of(item_id, kind)
            ),
        )

    def default_branch(self) -> str:
        return self._default_branch

    def list_open_board_issues(self) -> tuple[board.Issue, ...]:
        """Every open item, plus every unreadable one as a row the board
        names by its defect (issue #517): its state may not read, so it
        counts as open, and one such item never hides the others."""
        self._refuse_a_foreign_entry()
        return (
            *(
                self._issue(item_id)
                for item_id, decoded in self._items.items()
                if decoded.record.state is items.RecordState.OPEN
            ),
            *(
                self._unreadable_issue(item_id, malformed)
                for item_id, malformed in self._malformed.items()
            ),
        )

    @staticmethod
    def _unreadable_issue(item_id: str, malformed: _MalformedItem) -> board.Issue:
        """`malformed`'s board row: only its id, its title when that alone
        still reads, and its defect are known -- its record's timestamps
        are not, so its age counts from this read."""
        read_at = items.format_record_timestamp(datetime.now(UTC))
        return board.Issue(
            items.item_number(item_id),
            malformed.title or item_id,
            (),
            malformed.text,
            read_at,
            read_at,
            unreadable=malformed.defect,
        )

    def open_issue(self, number: int) -> board.Issue | None:
        """`number`'s item as the board reads it while it is open, else
        `None` -- `item new --parent`'s one look at its parent's kind (issue
        #503), which reads that one item alone, so another malformed item
        never refuses it (ITEM-37)."""
        decoded = self._decoded(number)
        if decoded is None or decoded.record.state is not items.RecordState.OPEN:
            return None
        return self._issue(self._by_number[number])

    def open_item_titles(self) -> tuple[tuple[int, str], ...]:
        """Every open item's number and title, the open half of `item new`'s
        twin search: `item new` still runs beside a malformed item (issue
        #447), and one whose title still reads counts as open, since its
        state may not."""
        self._refuse_a_foreign_entry()
        return (
            *(
                (decoded.record.number, decoded.record.title)
                for decoded in self._items.values()
                if decoded.record.state is items.RecordState.OPEN
            ),
            *(
                (items.item_number(item_id), malformed.title)
                for item_id, malformed in self._malformed.items()
                if malformed.title is not None
            ),
        )

    def list_board_dependencies(self, number: int) -> tuple[board.IssueDependency, ...]:
        decoded = self._decoded(number)
        if decoded is None:
            return ()
        item_id = items.format_item_id(number)
        dependencies: list[board.IssueDependency] = []
        named: set[str] = set()
        for blocker_id in decoded.record.blocked_by:
            if blocker_id in named:
                raise MalformedStateTreeError(_repeated_blocker_sentence(item_id, blocker_id))
            named.add(blocker_id)
            if blocker_id in self._malformed:
                dependencies.append(self._unreadable_blocker(blocker_id))
                continue
            blocker = self._related(
                blocker_id, missing=_missing_blocker_sentence(item_id, blocker_id)
            )
            closed_at = None
            if blocker.record.closed_at is not None:
                closed_at = datetime.fromisoformat(blocker.record.closed_at).astimezone(UTC)
            dependencies.append(
                board.IssueDependency(
                    board.IssueReference(self.repository.path, blocker.record.number),
                    board.BlockerState(blocker.record.state.value),
                    False,
                    closed_at,
                )
            )
        return tuple(dependencies)

    def _unreadable_blocker(self, blocker_id: str) -> board.IssueDependency:
        """An unreadable blocker (issue #517): its state does not read, so it
        keeps blocking until repaired, rather than refusing the whole board."""
        return board.IssueDependency(
            board.IssueReference(self.repository.path, items.item_number(blocker_id)),
            board.BlockerState.OPEN,
            False,
            None,
        )

    def list_open_board_pull_requests(self) -> tuple[board.PullRequest, ...]:
        return ()

    def list_recently_closed_issues(self, since: datetime) -> tuple[forge.ClosedIssue, ...]:
        self._refuse_a_foreign_entry()
        cutoff = since.astimezone(UTC)
        return tuple(
            forge.ClosedIssue(decoded.record.number, decoded.record.title)
            for decoded in self._items.values()
            if decoded.record.closed_at is not None
            and datetime.fromisoformat(decoded.record.closed_at) >= cutoff
        )

    def list_recent_merged_board_pull_requests(
        self, since: datetime
    ) -> tuple[board.PullRequest, ...]:
        # `since` stays the `BoardSource` protocol's own parameter name
        # (positional identity matters for structural conformance) even
        # though this adapter never filters by it: `state-ref` cannot list
        # merged pull requests at all, so this empty tuple is this
        # adapter's own honest answer -- `Board.recovery` (purely
        # pull-request-declared) stays empty from it, while `Stage.CODE_LANDED`
        # and the board's own Landungen view read the trunk walk instead
        # (issue #371) and never depend on this listing at all.
        del since
        return ()

    def link_child(self, parent: int, child: int) -> None:
        """A no-op (issue #283): parentage has exactly one owner here,
        `record.parent`, already set by `create_child`'s own single write.
        GitHub's `link_child` recovers an orphan its own two-write
        `create_child` could leave behind (a created issue with no recorded
        sub-issue relation); a state-ref item write is one CAS write, so
        that half-finished state can never occur and there is nothing left
        to link."""
        del parent, child

    def compose_item(
        self,
        *,
        title: str,
        body: str,
        kind: ItemKind,
        parent: int | None,
        origin: str | None = None,
    ) -> NewItemWrite:
        """A fresh state-ref item's one write (issues #283, #285, #316),
        composed -- its id minted, its `[record]` filled -- and refused
        before any write when the read would set it aside (issue #517), but
        not yet applied. `origin` binds the item to a foreign forge issue
        (`--origin FORGE#N`, already grammar-checked by `items.parse_origin`)
        without aco governing that forge at all -- #230's own concept, "the
        forge is pulled, never governed." `item new` composes before it
        retypes a Task parent, so a refused item leaves the store untouched.
        Minting an id decides over the whole store, so it refuses beside an
        entry that names no item (PIN-13), `cut` included."""
        self._refuse_a_foreign_entry()
        new_id = items.mint_item_id(self._by_number.values())
        now = items.format_record_timestamp(datetime.now(UTC))
        record = items.ItemRecord(
            number=items.item_number(new_id),
            title=title,
            state=items.RecordState.OPEN,
            kind=kind.value,
            labels=(),
            blocked_by=(),
            parent=None if parent is None else self._by_number[parent],
            origin=origin,
            created_at=now,
            updated_at=now,
            closed_at=None,
        )
        new_body = _with_record(body, record)
        _readable_content(new_body)
        return NewItemWrite(item_id=new_id, record=record, body=new_body)

    def create_item(self, write: NewItemWrite) -> str:
        """`write` applied in one CAS write, then folded into this
        instance's own view -- the one write every fresh state-ref item goes
        through, `item new`'s and `cut`'s alike, so `cli.py` never grows a
        second way to create one. Returns the freshly minted item id."""
        new_oid = self._write_item(write.item_id, expected=None, body=write.body)
        self._items[write.item_id] = _DecodedItem(record=write.record, body=write.body, oid=new_oid)
        self._by_number[write.record.number] = write.item_id
        return write.item_id

    def create_issue(self, *, title: str, body: str, kind: ItemKind) -> int:
        """Unsupported (`STATE_REF_CAPABILITIES`): `compose_item` mints a
        state-ref item's id and records its parent and origin in one write."""
        raise forge.ForgeUnsupportedError(NO_BARE_ISSUE)

    def set_item_kind(self, number: int, kind: ItemKind) -> None:
        """`number`'s `record.kind` moved to `kind` (issue #503) -- `item new
        --parent` retyping a Task it gives a first child, `item edit --kind`
        -- in one CAS write over this instance's own already-read oid, as
        `update_item_body` writes; `updated_at` moves to now, every other
        byte stays."""
        item_id = self._by_number[number]
        current = self._related(item_id, missing=_unknown_item_sentence(item_id))
        updated_record = replace(
            current.record,
            kind=kind.value,
            updated_at=items.format_record_timestamp(datetime.now(UTC)),
        )
        new_body = _with_record(current.body, updated_record)
        new_oid = self._write_item(item_id, expected=current.oid, body=new_body)
        self._items[item_id] = _DecodedItem(record=updated_record, body=new_body, oid=new_oid)

    def create_child(self, *, parent: int, title: str, body: str, kind: ItemKind) -> int:
        item_id = self.create_item(
            self.compose_item(title=title, body=body, kind=kind, parent=parent)
        )
        return items.item_number(item_id)

    def update_item_body(self, number: int, body: str) -> None:
        """`body`, written back to `number`'s item file (issues #283, #287):
        `title`, `labels`, `blocked_by` come from `body`'s own `[record]`
        table when it carries one (`_delivered_content_fields`'s own owner
        split -- `aco item edit`'s ruled record merge); every other record
        field stays this item's own already-read `current.record`, and
        `updated_at` always moves to now. The CAS write's `expected` is
        `current.oid`, this instance's own already-read snapshot -- never a
        re-read -- so a second writer holding the same stale oid refuses
        with issue #279's own sentence rather than merging or overwriting.
        Every blocker the write adds must resolve to another readable item,
        and none may repeat, before the write (issue #450): a missing one
        would stop every later `board`/`next` (PIN-17), a repeated one trip
        `cli._validated_dependencies`' malformed blocked-by list refusal; a
        write that carries the stored record through never re-judges it.
        A malformed item (issue #447) has no stored record to merge into:
        `body`'s own complete `[record]` repairs it once its relations
        resolve (`_refuse_unresolved_repair`), else it refuses by name."""
        item_id = self._by_number[number]
        now = items.format_record_timestamp(datetime.now(UTC))
        malformed = self._malformed.get(item_id)
        if malformed is None:
            current = self._items[item_id]
            title, labels, blocked_by = _delivered_content_fields(body, current.record)
            self._refuse_unresolved_blockers(item_id, blocked_by, stored=current.record.blocked_by)
            updated_record = replace(
                current.record, title=title, labels=labels, blocked_by=blocked_by, updated_at=now
            )
            expected = current.oid
        else:
            delivered = _valid_record(body)
            if delivered is None:
                raise _malformed_item_refusal(item_id, malformed)
            updated_record = replace(items.parse_item_record(item_id, delivered), updated_at=now)
            self._refuse_unresolved_repair(updated_record)
            expected = malformed.oid
        new_body = _with_record(body, updated_record)
        new_oid = self._write_item(item_id, expected=expected, body=new_body)
        self._malformed.pop(item_id, None)
        self._items[item_id] = _DecodedItem(record=updated_record, body=new_body, oid=new_oid)

    def _refuse_unresolved_repair(self, record: items.ItemRecord) -> None:
        """A repair's own `[record]` is written whole (issue #447), so its
        relations must resolve first: its parent and every blocker name a
        readable item -- never a missing one (PIN-16/PIN-17's sentences), a
        malformed one, or itself -- and it stays open, since only `item close`
        guards a close against a live claim."""
        item_id = items.format_item_id(record.number)
        if record.state is items.RecordState.CLOSED:
            raise ClaimUnavailableError(
                f'a repair records state = "open"; close {item_id} afterwards '
                f"with aco item close {item_id}"
            )
        if record.parent is not None:
            self._related(record.parent, missing=_missing_parent_sentence(record.parent))
        self._refuse_unresolved_blockers(item_id, record.blocked_by)

    def _refuse_unresolved_blockers(
        self, item_id: str, blocked_by: tuple[str, ...], *, stored: tuple[str, ...] = ()
    ) -> None:
        """`blocked_by` names each blocker once, and every one not in
        `stored` names a readable item other than `item_id` (issues #447,
        #450): a repeated one refuses by name, since a board read refuses
        a repeated dependency; a missing one PIN-17's sentence, a malformed
        one its repair, `item_id` itself by name. Carrying `stored` through
        unchanged is never re-judged."""
        if blocked_by == stored:
            return
        named: set[str] = set()
        for blocker_id in blocked_by:
            if blocker_id in named:
                raise ClaimUnavailableError(_repeated_blocker_sentence(item_id, blocker_id))
            named.add(blocker_id)
            if blocker_id in stored:
                continue
            self._related(blocker_id, missing=_missing_blocker_sentence(item_id, blocker_id))
            if blocker_id == item_id:
                raise ClaimUnavailableError(f"item {item_id} {_BLOCKER_ITSELF}")

    def _closing_write(self, number: int) -> LandingWrite:
        """`number`'s own close write, composed but not written (issues
        #289, #359): `state` moves to `CLOSED`, `closed_at` and `updated_at`
        both move to now, and every other field -- the body included --
        stays byte-identical. Refuses loud, before any write, when the
        record already carries `state = CLOSED`: a second close on the same
        item is a stale caller, not an idempotent no-op, so it gets its own
        named date rather than a generic conflict. Shared by `close_item`
        (which writes this immediately, on its own) and `prepare_landing`
        (which hands it to `release --merged <sha>`'s own atomic
        `protocol.LandingIntent` instead, issue #359)."""
        item_id = self._by_number[number]
        current = self._items[item_id]
        if current.record.state is items.RecordState.CLOSED:
            raise ClaimUnavailableError(
                f"{item_id} is already closed (closed on {current.record.closed_at})"
            )
        now = items.format_record_timestamp(datetime.now(UTC))
        updated_record = replace(
            current.record, state=items.RecordState.CLOSED, closed_at=now, updated_at=now
        )
        new_body = _with_record(current.body, updated_record)
        return LandingWrite(
            item_id=item_id,
            expected=current.oid,
            content=_readable_content(new_body),
            record=updated_record,
        )

    def close_item(self, number: int) -> str:
        """Closes `number`'s item record (issue #289) in one CAS write over
        this instance's own already-read `current.oid` (the same oid
        discipline `update_item_body` uses, issue #279), through the
        writer's own `close_item`, which also refuses a live claim on every
        attempt (issue #459) -- the one place
        `state`/`closed_at` are ever composed for an immediate close;
        `cli._cmd_item_close` delegates the whole write here rather than
        building its own record. Returns the fresh `closed_at` for the
        CLI's own report line."""
        write = self._closing_write(number)
        new_oid = self._writer.close_item(
            write.item_id,
            number=number,
            expected=write.expected,
            content=write.content,
            store_expected=self._store_expected(),
        )
        self._items[write.item_id] = _DecodedItem(
            record=write.record, body=write.content.decode("utf-8"), oid=new_oid
        )
        assert write.record.closed_at is not None  # `_closing_write` just set it
        return write.record.closed_at

    def prepare_landing(self, number: int) -> LandingWrite:
        """`number`'s own close write, staged but not applied (issue #359):
        `release --merged <sha|empty>` under `storage = "state-ref"` hashes
        `content` into a blob itself (`store.hash_blob` -- this module may
        not import `store`, the Layers contract) and folds the resulting
        oid into one atomic `protocol.LandingIntent` alongside the claim it
        releases, instead of `close_item`'s own immediate, separately
        committed write. Call `mark_landed` with the result once that
        transition actually commits, to fold it into this instance's own
        in-memory view -- this method itself writes nothing. Unlike
        `close_item`, it refuses beside an entry that names no item, before
        any write (PIN-13; head ruling 01.10.2026, issue #565)."""
        self._refuse_a_foreign_entry()
        return self._closing_write(number)

    def mark_landed(self, write: LandingWrite, oid: ObjectId) -> None:
        """Folds an atomic landing's already-committed close (issue #359)
        into this instance's own in-memory view, the same update
        `close_item` performs after its own separate write -- but writes
        nothing itself: `release --merged <sha>`'s own transition already
        committed `write.content` at `oid`, atomically with the claim
        release."""
        self._items[write.item_id] = _DecodedItem(
            record=write.record, body=write.content.decode("utf-8"), oid=oid
        )

    def item_oid(self, number: int) -> ObjectId:
        """This item's own current blob oid, straight off this adapter's
        already-read state (issue #287): `aco item edit --json`'s own
        output is the one caller that needs it -- never part of the generic
        `ForgeWriter` port, since no other forge in this repository stores
        one blob per item."""
        item_id = self._by_number[number]
        return self._items[item_id].oid
