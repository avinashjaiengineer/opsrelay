"""Changing the remediation policy: versioned, reviewed by a second person, and audited.

    propose (admin A) -> proposed -> review by admin B (not A) -> approved -> activate -> active
                                                               -> rejected
    activating a version supersedes the one in force

Each version is a record holding the full policy YAML, its author, reviewer and activation. Every
step is written in one commit with an audit event on the "policy" chain, so `opsrelay audit policy`
shows who changed what the platform may do, and when. The active version is what the policy
engine uses (opsrelay.policy.get_policy); until one is activated, the file policy applies.
"""

from .policy import parse_policy
from .store import Record, Store, now_iso
from .store.base import Commit, RecordMove, make_event, new_record

KIND = "policy"
CHAIN = "policy"


class PolicyChangeError(ValueError):
    pass


def history(store: Store) -> list[Record]:
    return sorted(store.list_records(KIND), key=lambda r: r["number"], reverse=True)


def _get(store: Store, version: str) -> Record:
    record = store.get_record(KIND, version)
    if record is None:
        raise PolicyChangeError(f"Unknown policy version {version}")
    return record


def propose(store: Store, text: str, author: str, note: str = "") -> Record:
    try:
        parsed = parse_policy(text)
    except Exception as e:  # noqa: BLE001 - any parse or validation problem is the proposer's to fix
        raise PolicyChangeError(f"Invalid policy: {e}") from e
    number = max((r["number"] for r in store.list_records(KIND)), default=0) + 1
    record = new_record(
        KIND,
        f"v{number}",
        "proposed",
        number=number,
        text=text,
        actions=sorted(parsed.actions),
        author=author,
        note=note,
        reviewed_by=None,
        activated_by=None,
    )
    event = make_event(
        CHAIN,
        author,
        "policy.proposed",
        f"{author} proposed policy v{number}" + (f": {note}" if note else ""),
        {"version": record["id"], "actions": record["actions"]},
        input=text,
    )
    if store.commit(Commit(new_records=[record], events=[event])) is None:
        raise PolicyChangeError("Another version was proposed at the same time; try again")
    return record


def review(store: Store, version: str, reviewer: str, approve: bool, note: str = "") -> Record:
    record = _get(store, version)
    if record["status"] != "proposed":
        raise PolicyChangeError(f"Policy {version} is {record['status']}, not proposed")
    if reviewer == record["author"]:
        raise PolicyChangeError("The author of a policy change can't review it; a second admin must")
    status = "approved" if approve else "rejected"
    move = RecordMove(
        KIND,
        version,
        record["rev"],
        {"status": status, "reviewed_by": reviewer, "reviewed_at": now_iso(), "review_note": note},
    )
    event = make_event(
        CHAIN,
        reviewer,
        f"policy.{status}",
        f"{reviewer} {status} policy {version}" + (f": {note}" if note else ""),
        {"version": version, "author": record["author"]},
    )
    done = store.commit(Commit(record_moves=[move], events=[event]))
    if done is None:
        raise PolicyChangeError(f"Policy {version} changed while reviewing; try again")
    return done.records[(KIND, version)]


def activate(store: Store, version: str, actor: str) -> Record:
    record = _get(store, version)
    if record["status"] != "approved":
        raise PolicyChangeError(f"Policy {version} is {record['status']}; only an approved version can be activated")
    moves = [
        RecordMove(KIND, version, record["rev"], {"status": "active", "activated_by": actor, "activated_at": now_iso()})
    ]
    for current in store.list_records(KIND, status="active"):
        moves.append(
            RecordMove(KIND, current["id"], current["rev"], {"status": "superseded", "superseded_by": version})
        )
    event = make_event(CHAIN, actor, "policy.activated", f"{actor} activated policy {version}", {"version": version})
    done = store.commit(Commit(record_moves=moves, events=[event]))
    if done is None:
        raise PolicyChangeError("The policy changed while activating; try again")
    return done.records[(KIND, version)]
