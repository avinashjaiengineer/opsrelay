import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from .base import GENESIS_HASH, Record, Store, seal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, created_at TEXT, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT, doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_incident ON events (incident_id, seq);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, incident_id TEXT, status TEXT, created_at TEXT, doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS executions (key TEXT PRIMARY KEY, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dead_letters (id TEXT PRIMARY KEY, created_at TEXT, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS services (name TEXT PRIMARY KEY, doc TEXT NOT NULL);
"""


class SqliteStore(Store):
    """Single-file store for local development. Safe across threads, and across processes for the
    compare-and-set operations (they run in BEGIN IMMEDIATE transactions)."""

    def __init__(self, path: str = "opsrelay.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """A write transaction that also holds the database write lock across processes."""
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

    def transition_incident(self, incident_id: str, from_status: str, updates: Record) -> Record | None:
        with self._transaction() as conn:
            row = conn.execute("SELECT doc FROM incidents WHERE id = ?", (incident_id,)).fetchone()
            if row is None:
                return None
            incident = json.loads(row[0])
            if incident["status"] != from_status:
                return None
            incident.update(updates)
            conn.execute("UPDATE incidents SET doc = ? WHERE id = ?", (json.dumps(incident), incident_id))
        return incident

    # Audit log
    def append_event(self, event: Record) -> Record:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT doc FROM events WHERE incident_id = ? ORDER BY seq DESC LIMIT 1", (event["incident_id"],)
            ).fetchone()
            prev_hash = json.loads(row[0]).get("hash", GENESIS_HASH) if row else GENESIS_HASH
            sealed = seal(event, prev_hash)
            conn.execute(
                "INSERT INTO events (incident_id, doc) VALUES (?, ?)", (event["incident_id"], json.dumps(sealed))
            )
        return sealed

    def list_events(self, incident_id: str) -> list[Record]:
        return self._all("SELECT doc FROM events WHERE incident_id = ? ORDER BY seq", (incident_id,))

    # Approvals
    def put_approval(self, approval: Record) -> None:
        self._exec(
            "INSERT OR REPLACE INTO approvals (id, incident_id, status, created_at, doc) VALUES (?, ?, ?, ?, ?)",
            (
                approval["id"],
                approval["incident_id"],
                approval["status"],
                approval["created_at"],
                json.dumps(approval),
            ),
        )

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

    def transition_approval(self, approval_id: str, from_status: str, updates: Record) -> Record | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT doc FROM approvals WHERE id = ? AND status = ?", (approval_id, from_status)
            ).fetchone()
            if row is None:
                return None
            approval = {**json.loads(row[0]), **updates}
            conn.execute(
                "UPDATE approvals SET status = ?, doc = ? WHERE id = ?",
                (approval["status"], json.dumps(approval), approval_id),
            )
        return approval

    # Executions
    def claim_execution(self, execution: Record) -> bool:
        return (
            self._exec(
                "INSERT OR IGNORE INTO executions (key, doc) VALUES (?, ?)", (execution["key"], json.dumps(execution))
            )
            == 1
        )

    def get_execution(self, key: str) -> Record | None:
        return self._one("SELECT doc FROM executions WHERE key = ?", (key,))

    def finish_execution(self, key: str, updates: Record) -> Record:
        with self._transaction() as conn:
            row = conn.execute("SELECT doc FROM executions WHERE key = ?", (key,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown execution {key}")
            execution = {**json.loads(row[0]), **updates}
            conn.execute("UPDATE executions SET doc = ? WHERE key = ?", (json.dumps(execution), key))
        return execution

    # Dead letters
    def put_dead_letter(self, dead_letter: Record) -> None:
        self._exec(
            "INSERT INTO dead_letters (id, created_at, doc) VALUES (?, ?, ?)",
            (dead_letter["id"], dead_letter["created_at"], json.dumps(dead_letter)),
        )

    def list_dead_letters(self, limit: int = 50) -> list[Record]:
        return self._all("SELECT doc FROM dead_letters ORDER BY created_at DESC LIMIT ?", (limit,))

    # Services
    def put_service(self, service: Record) -> None:
        self._exec("INSERT OR REPLACE INTO services (name, doc) VALUES (?, ?)", (service["name"], json.dumps(service)))

    def get_service(self, name: str) -> Record | None:
        return self._one("SELECT doc FROM services WHERE name = ?", (name,))

    def list_services(self) -> list[Record]:
        return self._all("SELECT doc FROM services ORDER BY name")
