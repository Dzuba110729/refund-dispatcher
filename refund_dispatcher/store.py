"""Журнал заявок и аудит (SQLite). Отвечает за защиту от двойного нажатия и за дедупликацию."""
import json
import sqlite3
import threading
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY, order_id TEXT, status TEXT NOT NULL,
    card_message_id INTEGER, notified_at TEXT, reminded_at TEXT, escalated_at TEXT);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, at TEXT, event TEXT, data TEXT);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.db.executescript(SCHEMA)

    def _run(self, sql: str, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    def open_ticket(self, ticket_id: str, order_id: str | None, message_id: int, now: datetime) -> None:
        self._run("INSERT OR IGNORE INTO tickets VALUES (?,?,?,?,?,NULL,NULL)",
                  (ticket_id, order_id, "pending", message_id, now.isoformat()))

    def find_open_by_order(self, order_id: str) -> str | None:
        row = self._run("SELECT ticket_id FROM tickets WHERE order_id=? AND status IN ('pending','processing')",
                        (order_id,)).fetchone()
        return row["ticket_id"] if row else None

    def claim(self, ticket_id: str) -> bool:
        cur = self._run("UPDATE tickets SET status='processing' WHERE ticket_id=? AND status='pending'", (ticket_id,))
        return cur.rowcount == 1

    def release(self, ticket_id: str) -> None:
        self._run("UPDATE tickets SET status='pending' WHERE ticket_id=? AND status='processing'", (ticket_id,))

    def close(self, ticket_id: str) -> None:
        self._run("UPDATE tickets SET status='done' WHERE ticket_id=?", (ticket_id,))

    def status(self, ticket_id: str) -> str | None:
        row = self._run("SELECT status FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
        return row["status"] if row else None

    def pending(self) -> list[sqlite3.Row]:
        return self._run("SELECT * FROM tickets WHERE status='pending'").fetchall()

    def mark_reminded(self, ticket_id: str, now: datetime) -> None:
        self._run("UPDATE tickets SET reminded_at=? WHERE ticket_id=?", (now.isoformat(), ticket_id))

    def mark_escalated(self, ticket_id: str, now: datetime) -> None:
        self._run("UPDATE tickets SET escalated_at=? WHERE ticket_id=?", (now.isoformat(), ticket_id))

    def audit(self, ticket_id: str, event: str, data: dict, now: datetime) -> None:
        self._run("INSERT INTO audit (ticket_id, at, event, data) VALUES (?,?,?,?)",
                  (ticket_id, now.isoformat(), event, json.dumps(data, ensure_ascii=False, default=str)))

    def audit_events(self, ticket_id: str) -> list[tuple[str, dict]]:
        rows = self._run("SELECT event, data FROM audit WHERE ticket_id=? ORDER BY id", (ticket_id,)).fetchall()
        return [(r["event"], json.loads(r["data"])) for r in rows]
