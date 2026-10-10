"""Global transaction log: SQLite, with each change also appended to a JSONL file."""

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from common.timeutil import now_ist


class TxnLog:
    """Durable global transaction log."""

    def __init__(self, db_path: Path, jsonl_path: Path):
        self.db_path = db_path
        self.jsonl_path = jsonl_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._jsonl_lock = threading.Lock()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def _init_schema(self):
        self._conn().executescript("""
            CREATE TABLE IF NOT EXISTS transactions (
                txn_id        TEXT PRIMARY KEY,
                type          TEXT NOT NULL,
                source        TEXT,
                destination   TEXT,
                amount        INTEGER NOT NULL,
                timestamp     TEXT NOT NULL,
                status        TEXT NOT NULL,
                failure_reason TEXT,
                decision      TEXT,
                participants  TEXT,
                updated_at    TEXT
            );
        """)

    def log(
        self,
        txn_id: str,
        txn_type: str,
        amount: int,
        status: str,
        source: str = "",
        destination: str = "",
        failure_reason: str = "",
        decision: str = "",
        participants: str = "",
    ) -> None:
        """Insert or update a transaction record."""
        ts = now_ist()
        conn = self._conn()
        conn.execute(
            """INSERT INTO transactions
               (txn_id, type, source, destination, amount, timestamp, status,
                failure_reason, decision, participants, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(txn_id) DO UPDATE SET
                 status=excluded.status,
                 failure_reason=excluded.failure_reason,
                 decision=excluded.decision,
                 updated_at=excluded.updated_at
            """,
            (txn_id, txn_type, source, destination, amount, ts, status,
             failure_reason, decision, participants, ts),
        )
        conn.commit()

        # Mirror to JSONL
        record = {
            "txn_id": txn_id, "type": txn_type, "source": source,
            "destination": destination, "amount": amount, "timestamp": ts,
            "status": status, "failure_reason": failure_reason,
            "decision": decision,
        }
        with self._jsonl_lock:
            with open(self.jsonl_path, "a") as f:
                f.write(json.dumps(record) + "\n")

    def update_status(self, txn_id: str, status: str, failure_reason: str = "", decision: str = "") -> None:
        """Update the status of an existing transaction."""
        ts = now_ist()
        conn = self._conn()
        if decision:
            conn.execute(
                "UPDATE transactions SET status=?, failure_reason=?, decision=?, updated_at=? WHERE txn_id=?",
                (status, failure_reason, decision, ts, txn_id),
            )
        else:
            conn.execute(
                "UPDATE transactions SET status=?, failure_reason=?, updated_at=? WHERE txn_id=?",
                (status, failure_reason, ts, txn_id),
            )
        conn.commit()

        # Mirror update to JSONL
        record = {"txn_id": txn_id, "status": status, "failure_reason": failure_reason,
                   "decision": decision, "timestamp": ts, "update": True}
        with self._jsonl_lock:
            with open(self.jsonl_path, "a") as f:
                f.write(json.dumps(record) + "\n")

    def get(self, txn_id: str) -> Optional[dict]:
        """Get a single transaction by ID."""
        row = self._conn().execute(
            "SELECT * FROM transactions WHERE txn_id = ?", (txn_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_decision(self, txn_id: str) -> Optional[str]:
        """Get the decision for a 2PC transaction (for participant recovery)."""
        row = self._conn().execute(
            "SELECT decision FROM transactions WHERE txn_id = ?", (txn_id,)
        ).fetchone()
        if row is None:
            return None
        return row["decision"]

    def list_transactions(self, status: str = "", limit: int = 100) -> list[dict]:
        """List transactions, optionally filtered by status."""
        conn = self._conn()
        if status:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE status = ? ORDER BY timestamp DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM transactions ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_pending_commits(self) -> list[dict]:
        """Return all PENDING transactions that have a COMMIT decision (for gateway recovery)."""
        rows = self._conn().execute(
            "SELECT * FROM transactions WHERE status = 'PENDING' AND decision = 'COMMIT'"
        ).fetchall()
        return [dict(r) for r in rows]

    def abort_undecided(self) -> int:
        """At startup: a transfer still PENDING with no decision was cut off by a
        coordinator crash in phase 1, so nobody committed it. Mark it ABORT.
        """
        rows = self._conn().execute(
            "SELECT txn_id FROM transactions "
            "WHERE status = 'PENDING' AND (decision IS NULL OR decision = '')"
        ).fetchall()
        for r in rows:
            self.update_status(r["txn_id"], "FAILED",
                               failure_reason="Coordinator restarted before a decision was made",
                               decision="ABORT")
        return len(rows)

    def reset(self) -> None:
        """Clear all records."""
        self._conn().execute("DELETE FROM transactions")
        self._conn().commit()
        # Clear JSONL
        with self._jsonl_lock:
            open(self.jsonl_path, "w").close()
