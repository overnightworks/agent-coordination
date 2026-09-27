"""Direct `protocol.py` behavior: body/slice claim identities, scope and
branch validation, claim conflict/overlap, and the wide-scope trip rule.
Tests that drive these through `issue_claim.main([...])` stay in
`tests/test_cli.py` as CLI-wiring behavior; `protocol.apply` and its TOML
codecs are `tests/test_store.py`'s own (the store's pure counterpart) --
except `LandingIntent` (issue #359), whose one job is exactly the atomic
close-and-release this module owns describing, so its own `apply` proof
lives here instead."""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from dataclasses import replace

import pytest
from board_fixtures import _active_claim, request
from test_store import _STATE_WITH_TIP, _claim_intent

from agent_coordination import board, protocol
from agent_coordination.body import Storage
from agent_coordination.protocol import (
    ClaimError,
    ClaimRequest,
    ClaimUnavailableError,
    InvalidClaimMarkerError,
    claims_conflict,
)


@pytest.mark.parametrize("bad_issue", [0, -1, True])
def test_issue_identity_requires_a_positive_integer(bad_issue: int) -> None:
    with pytest.raises(ClaimError, match="issue identity must be a positive integer"):
        protocol.IssueIdentity(bad_issue)


def test_outbound_resource_name_refuses_a_value_that_is_not_a_resource_name() -> None:
    with pytest.raises(ClaimError, match="resource is not a resource name"):
        protocol._outbound_resource_name("not a valid name!")


def test_merged_release_reason_names_the_pull_request() -> None:
    assert protocol.MergedRelease(12).reason == "merged #12"


@pytest.mark.parametrize(
    ("add", "drop", "match"),
    [
        pytest.param(
            ("new.py",), ("missing.py",), "cannot drop 'missing.py'", id="drop-not-present"
        ),
        pytest.param((), ("src",), "rescope must leave a non-empty scope", id="empty-after-drop"),
    ],
)
def test_combined_scope_refuses_an_invalid_rescope(
    add: tuple[str, ...], drop: tuple[str, ...], match: str
) -> None:
    with pytest.raises(ClaimUnavailableError, match=match):
        protocol._combined_scope(("src",), add, drop)


def test_outbound_text_refuses_a_non_string_field() -> None:
    with pytest.raises(ClaimError, match="agent must be text"):
        protocol._outbound_text(123, "agent", maximum=128)


@pytest.mark.parametrize(
    "invalid",
    ["Codex\nSol", "Codex\x1fSol", " ", "x" * 129],
)
def test_outbound_text_rejects_controlled_or_overlong_fields(invalid: str) -> None:
    """`_outbound_text` is the one owner of this validation since issue #176
    moved it out of the deleted ledger comment writers (`claim_comment` /
    `release_comment` / `supersede_comment`) and onto every real call site
    that builds an intent: `cli._request`'s agent/role, and `release`'s
    abandoned reason."""
    with pytest.raises(ClaimError, match="agent must be one bounded non-empty line"):
        protocol._outbound_text(invalid, "agent", maximum=128)


@pytest.mark.parametrize(
    ("branch", "match"),
    [
        pytest.param(5, "must be text", id="not-text"),
        pytest.param(" codex/issue-72", "must be one bounded non-empty line", id="padded"),
        pytest.param("", "must be one bounded non-empty line", id="empty"),
        pytest.param("codex\x1f/issue-72", "must be one bounded non-empty line", id="control"),
        pytest.param("-codex/issue-72", "not a safe Git ref", id="leading-dash"),
        pytest.param("codex/../issue-72", "not a safe Git ref", id="dot-dot"),
        pytest.param("codex//issue-72", "not a safe Git ref", id="double-slash"),
        pytest.param("codex/issue-72@{1}", "not a safe Git ref", id="reflog-syntax"),
        pytest.param("codex/issue-72.lock", "not a safe Git ref", id="lock-suffix"),
        pytest.param("codex/.hidden", "not a safe Git ref", id="dot-segment"),
    ],
)
def test_claim_branch_must_be_a_safe_git_ref(branch: object, match: str) -> None:
    """`_valid_branch` guards every branch that reaches a claim -- `cli._request`
    is its live caller, taking the value from `--branch` or the checked-out
    branch name, neither of which this repository controls."""
    with pytest.raises(InvalidClaimMarkerError, match=match):
        protocol._valid_branch({"branch": branch})


@pytest.mark.parametrize(
    ("scope", "match"),
    [
        pytest.param("src", "must be a non-empty list", id="not-a-list"),
        pytest.param([], "must be a non-empty list", id="empty-list"),
        pytest.param([5], "scope entries must be text", id="entry-not-text"),
        pytest.param([" src"], "canonical bounded paths", id="padded-entry"),
        pytest.param(
            [f"src/file{index}.py" for index in range(protocol.MAX_SCOPE_ENTRIES + 1)],
            "exceeds 256 entries",
            id="too-many-entries",
        ),
        pytest.param(["src\\widget.py"], "canonical bounded paths", id="backslash"),
        pytest.param(["src/\x1fwidget.py"], "canonical bounded paths", id="control-character"),
        pytest.param(["x" * (protocol.MAX_SCOPE_PATH_LENGTH + 1)], "canonical", id="overlong"),
        pytest.param(["/etc/passwd"], "must be repository-relative", id="absolute"),
        pytest.param(["../outside.py"], "must be repository-relative", id="escapes-upwards"),
        pytest.param(["~/secrets"], "must be repository-relative", id="home-relative"),
        pytest.param([".git/config"], "must be repository-relative", id="git-directory"),
        pytest.param(["./src"], "must be repository-relative", id="not-normalized"),
        pytest.param(["src", "src"], "duplicate paths", id="duplicate"),
    ],
)
def test_claim_scope_must_be_canonical_repository_relative_paths(scope: object, match: str) -> None:
    """`valid_scope` guards every path that reaches a claim -- `cli._request`,
    `cli._rescope`'s add/drop, the `--path` lookup, and `claims_holding_path`
    all hand it operator-supplied text."""
    with pytest.raises(InvalidClaimMarkerError, match=match):
        protocol.valid_scope(scope)


def test_claim_scope_is_recorded_and_serialized_in_canonical_order() -> None:
    """`valid_scope` is the one place a claim's scope order is decided --
    `cli._request` calls it at creation, `_combined_scope` calls it again at
    rescope -- so a claim recorded from paths given in caller order still
    lands in `claims/<key>.toml` sorted (issue #331 R1): the body-scope
    projection `board._canonical_scope` reuses this exact function, never a
    second sort, so a body's own `scope` and a live claim's `scope` stay
    comparable as tuples no matter which order either was typed in."""
    scope = protocol.valid_scope(["scripts/issue_claim.py", "docs/COORDINATION.md"])

    assert scope == ("docs/COORDINATION.md", "scripts/issue_claim.py")
    assert (
        'scope = ["docs/COORDINATION.md", "scripts/issue_claim.py"]'
        in protocol.serialize_claim_toml(_active_claim(scope=scope))
    )


def test_serialize_claim_toml_escapes_control_characters_the_reader_accepts_back() -> None:
    """`toml_string` (issue #378) escapes every control character TOML's
    basic-string grammar forbids literal, not only backslash and quote: a
    claim field carrying a tab or a newline still round-trips through
    `tomllib.loads` (the reader `claims/<key>.toml` is read back with)
    instead of producing TOML the reader refuses to parse."""
    claim = _active_claim(agent="Grok sess-1\twith a tab\nand a newline")

    decoded = tomllib.loads(protocol.serialize_claim_toml(claim))

    assert decoded["agent"] == "Grok sess-1\twith a tab\nand a newline"


def test_scope_overlap_is_repository_wide_and_path_aware() -> None:
    left = request(issue=71, scope=("frontend/src",))
    nested = request("claim-b", issue=72, scope=("frontend/src/lib/player.ts",))
    sibling = request("claim-c", issue=73, scope=("frontend/tests",))

    assert not claims_conflict(left, nested)
    assert protocol.claims_overlap(left, nested)
    assert not claims_conflict(left, sibling)
    assert not protocol.claims_overlap(left, sibling)


@pytest.mark.parametrize(
    ("right", "expected"),
    [
        pytest.param(
            request("claim-b", lane=True, branch="docs/lane-a", scope=("other",)),
            True,
            id="same-lane-disjoint-scope-still-conflicts",
        ),
        pytest.param(
            request("claim-b", lane=True, branch="docs/lane-b", scope=("shared/file.py",)),
            False,
            id="different-lanes-overlapping-scope-is-not-a-conflict",
        ),
        pytest.param(
            request("claim-b", lane=True, branch="docs/lane-b", scope=("other",)),
            False,
            id="different-lanes-disjoint-scope-no-conflict",
        ),
        pytest.param(
            request("claim-b", issue=72, scope=("shared/file.py",)),
            False,
            id="lane-and-issue-overlapping-scope-is-not-a-conflict",
        ),
        pytest.param(
            request("claim-b", issue=72, scope=("other",)),
            False,
            id="lane-and-issue-disjoint-scope-no-conflict",
        ),
    ],
)
def test_lane_and_issue_conflict_matrix(right: ClaimRequest, expected: bool) -> None:
    left = request(lane=True, branch="docs/lane-a", scope=("shared",))
    assert claims_conflict(left, right) == expected


def test_wide_scope_trip_for_paths_directory_or_share_above_the_limits() -> None:
    three = ("a.py", "b.py", "c.py")
    four = (*three, "d.py")
    assert (
        protocol.wide_scope_trip(
            three, directories=(), covered_file_count=3, versioned_file_count=20
        )
        is None
    )
    assert (
        protocol.wide_scope_trip(
            four, directories=(), covered_file_count=4, versioned_file_count=20
        )
        is not None
    )
    assert (
        protocol.wide_scope_trip(
            ("docs",), directories=("docs",), covered_file_count=1, versioned_file_count=20
        )
        is not None
    )
    assert (
        protocol.wide_scope_trip(
            ("a.py",), directories=(), covered_file_count=1, versioned_file_count=4
        )
        is None
    )
    assert (
        protocol.wide_scope_trip(
            ("a.py", "b.py", "c.py"),
            directories=(),
            covered_file_count=3,
            versioned_file_count=protocol.WIDE_SCOPE_SHARE_FLOOR - 1,
        )
        is None
    ), "below the share floor, a share over a quarter still does not trip"
    assert (
        protocol.wide_scope_trip(
            ("a.py", "b.py", "c.py"),
            directories=(),
            covered_file_count=4,
            versioned_file_count=protocol.WIDE_SCOPE_SHARE_FLOOR,
        )
        is not None
    ), "at the share floor, a share over a quarter trips"
    assert (
        protocol.wide_scope_trip(
            ("a.py",), directories=(), covered_file_count=0, versioned_file_count=0
        )
        is None
    )


def test_wide_scope_trip_names_the_condition_in_the_rule_s_priority_order() -> None:
    """`wide_scope_trip(...) is not None` is the one rule owner -- a
    path-count trip outranks a directory trip that would also fire."""
    four = ("a.py", "b.py", "c.py", "d.py")
    assert protocol.wide_scope_trip(
        four, directories=("a.py",), covered_file_count=4, versioned_file_count=20
    ) == protocol.WideScopeTrip(protocol.WideScopeReason.PATH_COUNT, 4, ("a.py",), 4, 20)
    assert protocol.wide_scope_trip(
        ("docs",), directories=("docs",), covered_file_count=1, versioned_file_count=20
    ) == protocol.WideScopeTrip(protocol.WideScopeReason.DIRECTORY, 1, ("docs",), 1, 20)
    assert protocol.wide_scope_trip(
        ("a.py", "b.py", "c.py"),
        directories=(),
        covered_file_count=4,
        versioned_file_count=protocol.WIDE_SCOPE_SHARE_FLOOR,
    ) == protocol.WideScopeTrip(
        protocol.WideScopeReason.SHARE, 3, (), 4, protocol.WIDE_SCOPE_SHARE_FLOOR
    )
    assert (
        protocol.wide_scope_trip(
            ("a.py",), directories=(), covered_file_count=1, versioned_file_count=4
        )
        is None
    )


# --- `LandingIntent`: the atomic close-and-release (issue #359) ------------

_LANDING_ITEM_ID = "aco-000001"
_LANDING_ITEM_OID = protocol.ObjectId("d" * 40)
_LANDING_ITEM_NEW_OID = protocol.ObjectId("e" * 40)
_LANDING_COMMIT = protocol.ObjectId("1" * 40)


def test_landed_release_reason_names_the_commit() -> None:
    assert protocol.LandedRelease(_LANDING_COMMIT).reason == f"landed {_LANDING_COMMIT}"


def _landing_intent(
    *,
    item_id: str = _LANDING_ITEM_ID,
    item_expected: protocol.ObjectId = _LANDING_ITEM_OID,
    item_new_oid: protocol.ObjectId = _LANDING_ITEM_NEW_OID,
    claim_id: str = "a1",
    agent: str = "Ada",
    role: str = "builder",
    outcome: protocol.LandedRelease | None = None,
    operation_id: str = "op-2",
    coordinator_override: bool = False,
) -> protocol.LandingIntent:
    return protocol.LandingIntent(
        item_id=item_id,
        item_expected=item_expected,
        item_new_oid=item_new_oid,
        claim_id=protocol.ClaimId(claim_id),
        agent=agent,
        role=role,
        outcome=outcome if outcome is not None else protocol.LandedRelease(_LANDING_COMMIT),
        operation_id=operation_id,
        coordinator_override=coordinator_override,
    )


def _claimed_state_with_item() -> protocol.ClaimState:
    claimed = protocol.apply(_STATE_WITH_TIP, _claim_intent())
    return replace(claimed, items={_LANDING_ITEM_ID: _LANDING_ITEM_OID})


def test_apply_landing_intent_closes_the_item_and_releases_the_claim_in_one_transition() -> None:
    """Issue #359: one `apply` call both moves the item's blob oid and
    removes the claim -- the atomicity a landing needs (never one without
    the other) is exactly this being a single `ClaimState` transition, not
    two intents applied in sequence."""
    with_item = _claimed_state_with_item()

    landed = protocol.apply(with_item, _landing_intent())

    assert "issue-42" not in landed.claims
    assert landed.items == {_LANDING_ITEM_ID: _LANDING_ITEM_NEW_OID}
    assert protocol.ClaimId("a1") in landed.consumed_ids


def test_apply_landing_intent_allows_a_coordinator_override() -> None:
    with_item = _claimed_state_with_item()

    landed = protocol.apply(
        with_item,
        _landing_intent(agent="Coordinator", role="coordinator", coordinator_override=True),
    )

    assert "issue-42" not in landed.claims
    assert landed.items == {_LANDING_ITEM_ID: _LANDING_ITEM_NEW_OID}


@pytest.mark.parametrize(
    ("build_intent", "match"),
    [
        pytest.param(
            lambda: _landing_intent(claim_id="nonexistent"),
            "no active claim to release",
            id="no-such-claim",
        ),
        pytest.param(
            lambda: _landing_intent(agent="Grace"),
            "only the original claimant may release",
            id="wrong-claimant-no-override",
        ),
        pytest.param(
            lambda: _landing_intent(coordinator_override=True),
            "requires --role coordinator",
            id="override-without-coordinator-role",
        ),
        pytest.param(
            lambda: _landing_intent(item_expected=protocol.ObjectId("f" * 40)),
            "written since it was read",
            id="stale-item-oid",
        ),
    ],
)
def test_apply_landing_intent_refuses(
    build_intent: Callable[[], protocol.LandingIntent], match: str
) -> None:
    """Every way a landing refuses before it ever touches `refs/aco/state`
    (issue #359): a claim id that names no live claim, a non-claimant with
    no coordinator override, an override without the coordinator role --
    all three `ReleaseIntent`'s own sentences, proven exactly in
    `test_store.py` (`test_apply_release_intent_refuses_releasing_a_claim_that_does_not_exist`,
    `test_apply_release_intent_refuses_a_non_claimant_without_override`,
    `test_apply_release_intent_refuses_a_coordinator_override_without_coordinator_role`)
    -- and, the one check `LandingIntent` adds beyond `ReleaseIntent`, a
    stale item oid, which leaves the claim live rather than releasing it
    anyway."""
    with_item = _claimed_state_with_item()
    intent = build_intent()

    with pytest.raises(protocol.ClaimUnavailableError, match=match):
        protocol.apply(with_item, intent)

    # A refused landing changes nothing: still claimed, item still at its
    # original oid.
    assert "issue-42" in with_item.claims
    assert with_item.items == {_LANDING_ITEM_ID: _LANDING_ITEM_OID}


def test_apply_landing_intent_refuses_against_a_missing_state_ref() -> None:
    """A `LandingIntent` needs a real state-ref tip to close its item and
    release its claim against -- `protocol.EMPTY_STATE` (`tip is None`, no
    bootstrap yet) refuses before either half runs, the same precondition
    every other transition shape enforces."""
    assert protocol.EMPTY_STATE.tip is None
    intent = _landing_intent()

    with pytest.raises(protocol.ClaimError, match="does not exist yet"):
        protocol.apply(protocol.EMPTY_STATE, intent)


# --- `ItemCloseIntent`: a close refused while its item is claimed (#459) ---


def test_apply_item_close_intent_refuses_while_the_item_is_claimed() -> None:
    """Issue #459: the live-claim check is part of the transition itself, so
    every attempt `store.commit_transition` applies it to re-checks it; the
    unclaimed close is `item close`'s own CLI proofs (PIN-25, ITEM-16)."""
    claimed = _claimed_state_with_item()
    write = protocol.ItemWriteIntent(
        item_id=_LANDING_ITEM_ID,
        expected=_LANDING_ITEM_OID,
        new_oid=_LANDING_ITEM_NEW_OID,
        operation_id="close-op",
    )
    intent = protocol.ItemCloseIntent(write, protocol.IssueIdentity(42))

    with pytest.raises(
        protocol.ClaimUnavailableError,
        match=r"^#42 has a live claim \(Ada \(builder\)\); release the claim first$",
    ):
        protocol.apply(claimed, intent)


@pytest.mark.parametrize(
    "items_after_the_claim",
    [
        pytest.param({_LANDING_ITEM_ID: _LANDING_ITEM_NEW_OID}, id="edited"),
        pytest.param({}, id="gone"),
    ],
)
def test_apply_claim_intent_replaying_its_own_claim_refuses_once_its_pinned_item_changed(
    items_after_the_claim: dict[str, protocol.ObjectId],
) -> None:
    """Issue #496 / CAS-59: the item pin is checked on every attempt, the
    retry of a claim whose own earlier push already landed included -- its
    consumed claim id does not let it past an item written since."""
    pinned = replace(
        _claim_intent(), item_pin=protocol.ItemPin(_LANDING_ITEM_ID, _LANDING_ITEM_OID)
    )
    claimed = protocol.apply(
        replace(_STATE_WITH_TIP, items={_LANDING_ITEM_ID: _LANDING_ITEM_OID}), pinned
    )

    with pytest.raises(
        protocol.ClaimUnavailableError,
        match=rf"^item '{_LANDING_ITEM_ID}' was written since it was read ",
    ):
        protocol.apply(replace(claimed, items=items_after_the_claim), pinned)


@pytest.mark.parametrize(
    ("held", "conflicting_identity", "forge_sentence", "named_sentence"),
    [
        pytest.param(
            _claim_intent(identity=protocol.IssueIdentity(42), branch="ada/issue-42"),
            protocol.IssueIdentity(42),
            "issue #42 is claimed by Ada (builder) on issue #42 branch ada/issue-42",
            "issue aco-00002a is claimed by Ada (builder) on issue aco-00002a branch ada/issue-42",
            id="issue",
        ),
        pytest.param(
            _claim_intent(identity=protocol.LaneIdentity(), branch="docs/guide"),
            protocol.LaneIdentity(),
            "lane 'docs/guide' is claimed by Ada (builder) on lane 'docs/guide' branch docs/guide",
            "lane 'docs/guide' is claimed by Ada (builder) on lane 'docs/guide' branch docs/guide",
            id="lane",
        ),
        pytest.param(
            _claim_intent(
                identity=protocol.IssueIdentity(42), resource_name="display", resource_value=2
            ),
            protocol.IssueIdentity(43),
            "display 2 is held by Ada (builder) on issue #42",
            "display 2 is held by Ada (builder) on issue aco-00002a",
            id="resource",
        ),
        pytest.param(
            _claim_intent(
                identity=protocol.IssueIdentity(16777216), resource_name="display", resource_value=2
            ),
            protocol.IssueIdentity(43),
            "display 2 is held by Ada (builder) on issue #16777216",
            "display 2 is held by Ada (builder) on issue #16777216",
            id="resource-held-by-a-stored-number-past-aco-ffffff",
        ),
    ],
)
def test_a_claim_conflict_names_its_item_in_the_state_ref_form_the_cli_asks_for(
    held: protocol.ClaimIntent,
    conflicting_identity: protocol.ClaimIdentity,
    forge_sentence: str,
    named_sentence: str,
) -> None:
    """Issue #471: a claim-ledger refusal's own text names an item `#<n>`,
    and `named` with the renderer the CLI hands it under `storage =
    "state-ref"` names it `aco-xxxxxx`; a lane names no item, so both read
    alike. A resource conflict (CLAIM-42, #476 review finding 3) names its
    holder's item the same way, and a stored holder past `aco-ffffff` keeps
    `#<n>` (PIN-30's second exception, #476 review finding 2) rather than an
    `aco-1000000` no command takes back. The CLI offers no explicit resource
    value, so CLAIM-42 is driven at the store's own transition."""
    claimed = protocol.apply(_STATE_WITH_TIP, held)
    second = replace(
        held,
        identity=conflicting_identity,
        claim_id=protocol.ClaimId("a2"),
        operation_id="op-2",
    )

    with pytest.raises(protocol.ClaimConflictError) as refused:
        protocol.apply(claimed, second)

    assert (str(refused.value), refused.value.named(board.item_labeller(Storage.STATE_REF))) == (
        forge_sentence,
        named_sentence,
    )
