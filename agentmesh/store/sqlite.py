import json
import sqlite3
import threading

from .base import Record, Store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, created_at TEXT, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT, doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_incident ON events (incident_id, seq);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, incident_id TEXT, status TEXT, created_at TEXT, doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS services (name TEXT PRIMARY KEY, doc TEXT NOT NULL);
"""


class SqliteStore(Store):
    """Single-file store for local development. Safe to share between threads in one process."""

    def __init__(self, path: str = "agentmesh.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)

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

    def put_incident(self, incident: Record) -> None:
        self._exec(
            "INSERT OR REPLACE INTO incidents (id, created_at, doc) VALUES (?, ?, ?)",
            (incident["id"], incident["created_at"], json.dumps(incident)),
        )

    def get_incident(self, incident_id: str) -> Record | None:
        return self._one("SELECT doc FROM incidents WHERE id = ?", (incident_id,))

    def list_incidents(self, limit: int = 50) -> list[Record]:
        return self._all("SELECT doc FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,))

    def append_event(self, event: Record) -> None:
        self._exec("INSERT INTO events (incident_id, doc) VALUES (?, ?)", (event["incident_id"], json.dumps(event)))

    def list_events(self, incident_id: str) -> list[Record]:
        return self._all("SELECT doc FROM events WHERE incident_id = ? ORDER BY seq", (incident_id,))

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
        with self._lock:
            row = self._conn.execute(
                "SELECT doc FROM approvals WHERE id = ? AND status = ?", (approval_id, from_status)
            ).fetchone()
            if row is None:
                return None
            approval = {**json.loads(row[0]), **updates}
            changed = self._conn.execute(
                "UPDATE approvals SET status = ?, doc = ? WHERE id = ? AND status = ?",
                (approval["status"], json.dumps(approval), approval_id, from_status),
            ).rowcount
        return approval if changed else None

    def put_service(self, service: Record) -> None:
        self._exec("INSERT OR REPLACE INTO services (name, doc) VALUES (?, ?)", (service["name"], json.dumps(service)))

    def get_service(self, name: str) -> Record | None:
        return self._one("SELECT doc FROM services WHERE name = ?", (name,))

    def list_services(self) -> list[Record]:
        return self._all("SELECT doc FROM services ORDER BY name")
