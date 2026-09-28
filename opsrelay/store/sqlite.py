import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from .base import GENESIS_HASH, Commit, Committed, Record, Store, now_iso, seal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, created_at TEXT, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT, doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_incident ON events (incident_id, seq);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, incident_id TEXT, status TEXT, created_at TEXT, doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
    kind TEXT NOT NULL, id TEXT NOT NULL, status TEXT, rev INTEGER NOT NULL, created_at TEXT,
    doc TEXT NOT NULL, PRIMARY KEY (kind, id)
);
CREATE INDEX IF NOT EXISTS records_kind ON records (kind, status, created_at);
CREATE TABLE IF NOT EXISTS services (name TEXT PRIMARY KEY, doc TEXT NOT NULL);
"""


class _Conflict(Exception):
    pass


class SqliteStore(Store):
    """Single-file store for local development. Every commit runs in one BEGIN IMMEDIATE
    transaction, so it is atomic and serialized across threads and processes."""

    def __init__(self, path: str = "opsrelay.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _one(self, sql: str, args: tuple) -> Record | None:
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return json.loads(row[0]) if row else None

    def _all(self, sql: str, args: tuple = ()) -> list[Record]:
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [json.loads(r[0]) for r in rows]

    def _exec(self, sql: str, args: tuple) -> int:
        with self._lock:
            return self._conn.execute(sql, args).rowcount

    # Atomic writes
    def commit(self, change: Commit) -> Committed | None:
        try:
            with self._transaction() as conn:
                return self._apply(conn, change)
        except _Conflict:
            return None

    def _apply(self, conn: sqlite3.Connection, change: Commit) -> Committed:
        incident = None
        if change.incident:
            c = change.incident
            row = conn.execute("SELECT doc FROM incidents WHERE id = ?", (c.incident_id,)).fetchone()
            if row is None:
                raise _Conflict
            incident = json.loads(row[0])
            if incident["status"] != c.expected_status:
                raise _Conflict
            incident.update(c.updates)
            conn.execute("UPDATE incidents SET doc = ? WHERE id = ?", (json.dumps(incident), c.incident_id))

        approvals: dict[str, Record] = {}
        for a in change.new_approvals:
            if conn.execute("SELECT 1 FROM approvals WHERE id = ?", (a["id"],)).fetchone():
                raise _Conflict
            conn.execute(
                "INSERT INTO approvals (id, incident_id, status, created_at, doc) VALUES (?, ?, ?, ?, ?)",
                (a["id"], a["incident_id"], a["status"], a["created_at"], json.dumps(a)),
            )
            approvals[a["id"]] = a
        for m in change.approval_moves:
            row = conn.execute("SELECT doc FROM approvals WHERE id = ?", (m.approval_id,)).fetchone()
            if row is None or json.loads(row[0])["status"] != m.expected_status:
                raise _Conflict
            approval = {**json.loads(row[0]), **m.updates}
            conn.execute(
                "UPDATE approvals SET status = ?, doc = ? WHERE id = ?",
                (approval["status"], json.dumps(approval), m.approval_id),
            )
            approvals[m.approval_id] = approval

        records: dict[tuple[str, str], Record] = {}
        for r in change.new_records:
            if conn.execute("SELECT 1 FROM records WHERE kind = ? AND id = ?", (r["kind"], r["id"])).fetchone():
                raise _Conflict
            conn.execute(
                "INSERT INTO records (kind, id, status, rev, created_at, doc) VALUES (?, ?, ?, ?, ?, ?)",
                (r["kind"], r["id"], r.get("status"), r.get("rev", 0), r["created_at"], json.dumps(r)),
            )
            records[(r["kind"], r["id"])] = r
        for m in change.record_moves:
            row = conn.execute("SELECT doc FROM records WHERE kind = ? AND id = ?", (m.kind, m.record_id)).fetchone()
            if row is None or json.loads(row[0])["rev"] != m.expected_rev:
                raise _Conflict
            record = {**json.loads(row[0]), **m.updates, "rev": m.expected_rev + 1, "updated_at": now_iso()}
            conn.execute(
                "UPDATE records SET status = ?, rev = ?, doc = ? WHERE kind = ? AND id = ?",
                (record.get("status"), record["rev"], json.dumps(record), m.kind, m.record_id),
            )
            records[(m.kind, m.record_id)] = record

        heads: dict[str, str] = {}
        sealed_events = []
        for event in change.events:
            iid = event["incident_id"]
            if iid not in heads:
                row = conn.execute(
                    "SELECT doc FROM events WHERE incident_id = ? ORDER BY seq DESC LIMIT 1", (iid,)
                ).fetchone()
                heads[iid] = json.loads(row[0]).get("hash", GENESIS_HASH) if row else GENESIS_HASH
            sealed = seal(event, heads[iid])
            heads[iid] = sealed["hash"]
            conn.execute("INSERT INTO events (incident_id, doc) VALUES (?, ?)", (iid, json.dumps(sealed)))
            sealed_events.append(sealed)
        return Committed(incident=incident, approvals=approvals, records=records, events=sealed_events)

    # Incidents
    def put_incident(self, incident: Record) -> None:
        self._exec(
            "INSERT INTO incidents (id, created_at, doc) VALUES (?, ?, ?)",
            (incident["id"], incident["created_at"], json.dumps(incident)),
        )

    def get_incident(self, incident_id: str) -> Record | None:
        return self._one("SELECT doc FROM incidents WHERE id = ?", (incident_id,))

    def list_incidents(self, limit: int = 50) -> list[Record]:
        return self._all("SELECT doc FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,))

    # Audit log
    def list_events(self, incident_id: str) -> list[Record]:
        return self._all("SELECT doc FROM events WHERE incident_id = ? ORDER BY seq", (incident_id,))

    def event_count(self, incident_id: str) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM events WHERE incident_id = ?", (incident_id,)).fetchone()[0]

    # Approvals
    def get_approval(self, approval_id: str) -> Record | None:
        return self._one("SELECT doc FROM approvals WHERE id = ?", (approval_id,))

    def list_approvals(self, status: str | None = None, incident_id: str | None = None) -> list[Record]:
        sql, args = "SELECT doc FROM approvals WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if incident_id:
            sql += " AND incident_id = ?"
            args.append(incident_id)
        return self._all(sql + " ORDER BY created_at", tuple(args))

    # Records
    def get_record(self, kind: str, record_id: str) -> Record | None:
        return self._one("SELECT doc FROM records WHERE kind = ? AND id = ?", (kind, record_id))

    def list_records(self, kind: str, status: str | None = None, limit: int = 200) -> list[Record]:
        if status:
            return self._all(
                "SELECT doc FROM records WHERE kind = ? AND status = ? ORDER BY created_at DESC LIMIT ?",
                (kind, status, limit),
            )
        return self._all("SELECT doc FROM records WHERE kind = ? ORDER BY created_at DESC LIMIT ?", (kind, limit))

    # Services
    def put_service(self, service: Record) -> None:
        self._exec("INSERT OR REPLACE INTO services (name, doc) VALUES (?, ?)", (service["name"], json.dumps(service)))

    def get_service(self, name: str) -> Record | None:
        return self._one("SELECT doc FROM services WHERE name = ?", (name,))

    def list_services(self) -> list[Record]:
        return self._all("SELECT doc FROM services ORDER BY name")
