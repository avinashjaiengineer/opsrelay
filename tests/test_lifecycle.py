"""The incident state machine, and who may make each move."""

import pytest

from opsrelay.contracts import CONTRACTS, PLATFORM_TRANSITIONS, owners, transitions_of, unowned_transitions
from opsrelay.lifecycle import ALLOWED_TRANSITIONS, TERMINAL, IllegalTransition, Status, transition
from opsrelay.store import new_incident_id, now_iso

S = Status


def test_allowed_transitions_are_exactly_the_spec():
    assert ALLOWED_TRANSITIONS == {
        S.OPEN: {S.TRIAGING},
        S.TRIAGING: {S.INVESTIGATING, S.ESCALATED},
        S.INVESTIGATING: {S.AWAITING_APPROVAL, S.ESCALATED},
        S.AWAITING_APPROVAL: {S.REMEDIATING, S.ESCALATED},
        S.REMEDIATING: {S.VERIFYING, S.FAILED},
        S.VERIFYING: {S.RESOLVED, S.FAILED},
        S.FAILED: {S.ESCALATED},
    }
    assert TERMINAL == {S.RESOLVED, S.ESCALATED}


def test_every_legal_move_has_an_owner_and_every_owned_move_is_legal():
    assert unowned_transitions() == []
    for actor in ["platform", "coordinator", *CONTRACTS]:
        for current, target in transitions_of(actor):
            assert target in ALLOWED_TRANSITIONS[current], (actor, current, target)


def test_separation_of_duties():
    # Only the platform (after a human or the policy approves) starts remediation.
    assert owners(S.AWAITING_APPROVAL, S.REMEDIATING) == ["platform"]
    assert (S.REMEDIATING, S.VERIFYING) in PLATFORM_TRANSITIONS
    # Only communications resolves; the agent that proposed an action can't judge its outcome.
    assert owners(S.VERIFYING, S.RESOLVED) == ["communications"]
    assert CONTRACTS["remediation"].transitions == {(S.INVESTIGATING, S.AWAITING_APPROVAL)}
    assert CONTRACTS["diagnostics"].transitions == set()


@pytest.fixture
def incident(store):
    iid = new_incident_id()
    store.put_incident({"id": iid, "title": "t", "status": "open", "created_at": now_iso(), "updated_at": now_iso()})
    return iid


def test_legal_path_is_recorded(store, incident):
    transition(store, incident, S.TRIAGING, actor="coordinator")
    transition(store, incident, S.INVESTIGATING, actor="triage", severity="SEV2")
    assert store.get_incident(incident)["severity"] == "SEV2"
    moves = [e["data"] for e in store.list_events(incident) if e["kind"] == "status.changed"]
    assert moves == [{"from": "open", "to": "triaging"}, {"from": "triaging", "to": "investigating"}]


@pytest.mark.parametrize(
    ("path", "target", "actor", "message"),
    [
        ([], S.INVESTIGATING, "triage", "cannot move to investigating"),  # skipping triaging
        ([], S.ESCALATED, "coordinator", "cannot move to escalated"),  # open can only be triaged
        ([(S.TRIAGING, "coordinator")], S.INVESTIGATING, "diagnostics", "diagnostics may not move"),
        ([(S.TRIAGING, "coordinator"), (S.INVESTIGATING, "triage")], S.AWAITING_APPROVAL, "triage", "may not"),
        ([(S.TRIAGING, "coordinator"), (S.ESCALATED, "coordinator")], S.TRIAGING, "coordinator", "terminal"),
    ],
)
def test_illegal_moves_are_refused(store, incident, path, target, actor, message):
    for status, who in path:
        transition(store, incident, status, actor=who)
    before = store.get_incident(incident)["status"]
    with pytest.raises(IllegalTransition, match=message):
        transition(store, incident, target, actor=actor)
    assert store.get_incident(incident)["status"] == before


def test_status_cannot_be_changed_behind_the_lifecycle(store, incident):
    with pytest.raises(ValueError, match="lifecycle"):
        store.update_incident(incident, status="resolved")
