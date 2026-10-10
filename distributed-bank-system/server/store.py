"""SQLite storage for one bank server: accounts, prepared_txns (2PC state) and local_log (history)."""

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from common.timeutil import now_ist, now_ist_dt


class Store:
    """Thread-safe SQLite store for one bank server."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Initialize schema on the main thread
        self._init_schema(self._conn())

    def _conn(self) -> sqlite3.Connection:
        """Return a per-thread connection (with WAL mode and busy timeout)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY,
                owner      TEXT NOT NULL,
                balance    INTEGER NOT NULL DEFAULT 0,
                held       INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS prepared_txns (
                txn_id     TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                op         TEXT NOT NULL CHECK(op IN ('debit', 'credit')),
                amount     INTEGER NOT NULL,
                state      TEXT NOT NULL CHECK(state IN ('PREPARED', 'COMMITTED', 'ABORTED')),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS local_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                txn_id        TEXT NOT NULL,
                account_id    TEXT NOT NULL,
                type          TEXT NOT NULL,
                amount        INTEGER NOT NULL,
                balance_after INTEGER,
                timestamp     TEXT NOT NULL,
                status        TEXT NOT NULL,
                reason        TEXT
            );
        """)

    # Account CRUD

    def create_account(self, account_id: str, owner: str, balance: int) -> bool:
        """Create an account. Returns True on success, False if it already exists."""
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO accounts (account_id, owner, balance, held, created_at) "
                "VALUES (?, ?, ?, 0, ?)",
                (account_id, owner, balance, now_ist()),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_account(self, account_id: str) -> Optional[dict]:
        """Return account dict or None."""
        row = self._conn().execute(
            "SELECT account_id, owner, balance, held, created_at FROM accounts WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        return dict(row) if row else None

    def update_balance(self, account_id: str, delta: int) -> int:
        """Atomically change balance by delta. Returns new balance."""
        conn = self._conn()
        conn.execute(
            "UPDATE accounts SET balance = balance + ? WHERE account_id = ?",
            (delta, account_id),
        )
        conn.commit()
        new_bal = conn.execute(
            "SELECT balance FROM accounts WHERE account_id = ?",
            (account_id,),
        ).fetchone()["balance"]
        return new_bal

    # 2PC. Each method below is one SQLite transaction that starts with a write,
    # so concurrent calls for the same txn are serialized by SQLite.

    def prepare_debit(self, txn_id: str, account_id: str, amount: int) -> tuple[bool, str]:
        """Prepare a debit: check available balance, place a hold."""
        return self._prepare(txn_id, account_id, "debit", amount)

    def prepare_credit(self, txn_id: str, account_id: str, amount: int) -> tuple[bool, str]:
        """Prepare a credit: just check the account exists."""
        return self._prepare(txn_id, account_id, "credit", amount)

    def _prepare(self, txn_id: str, account_id: str, op: str, amount: int) -> tuple[bool, str]:
        """Store PREPARED (and the hold, for a debit) in one transaction.
        A repeated PREPARE is a no-op; a PREPARE that arrives after its ABORT votes no.
        """
        conn = self._conn()
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO prepared_txns (txn_id, account_id, op, amount, state, created_at) "
                "VALUES (?, ?, ?, ?, 'PREPARED', ?)",
                (txn_id, account_id, op, amount, now_ist()),
            )
            if cur.rowcount == 0:
                state = conn.execute(
                    "SELECT state FROM prepared_txns WHERE txn_id = ?", (txn_id,)
                ).fetchone()["state"]
                conn.commit()
                if state == "ABORTED":
                    return False, "Transaction already aborted"
                return True, ""

            acct = conn.execute(
                "SELECT balance, held FROM accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
            if acct is None:
                conn.rollback()
                return False, "Account not found"
            if op == "debit":
                if acct["balance"] - acct["held"] < amount:
                    conn.rollback()
                    return False, "Insufficient balance"
                conn.execute(
                    "UPDATE accounts SET held = held + ? WHERE account_id = ?",
                    (amount, account_id),
                )
            conn.commit()
            return True, ""
        except Exception:
            conn.rollback()
            raise

    def commit_txn(self, txn_id: str) -> bool:
        """Apply a prepared txn. Safe to call more than once, even concurrently:
        only the call that moves the row out of PREPARED changes the balance.
        """
        conn = self._conn()
        try:
            cur = conn.execute(
                "UPDATE prepared_txns SET state = 'COMMITTED' WHERE txn_id = ? AND state = 'PREPARED'",
                (txn_id,),
            )
            if cur.rowcount == 0:
                row = conn.execute(
                    "SELECT state FROM prepared_txns WHERE txn_id = ?", (txn_id,)
                ).fetchone()
                conn.commit()
                return row is not None and row["state"] == "COMMITTED"

            row = conn.execute(
                "SELECT account_id, op, amount FROM prepared_txns WHERE txn_id = ?", (txn_id,)
            ).fetchone()
            account_id, op, amount = row["account_id"], row["op"], row["amount"]
            if op == "debit":
                # Debit: reduce balance and release hold
                conn.execute(
                    "UPDATE accounts SET balance = balance - ?, held = held - ? WHERE account_id = ?",
                    (amount, amount, account_id),
                )
            else:
                # Credit: increase balance
                conn.execute(
                    "UPDATE accounts SET balance = balance + ? WHERE account_id = ?",
                    (amount, account_id),
                )
            new_bal = conn.execute(
                "SELECT balance FROM accounts WHERE account_id = ?", (account_id,)
            ).fetchone()["balance"]
            self._insert_log(conn, txn_id, account_id, f"2PC_{op.upper()}", amount, new_bal, "SUCCESS")
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise

    def abort_txn(self, txn_id: str) -> bool:
        """Abort a prepared txn and release its hold. Safe to call more than once.
        For an unknown txn an ABORTED row is stored, so a late PREPARE votes no.
        """
        conn = self._conn()
        try:
            cur = conn.execute(
                "UPDATE prepared_txns SET state = 'ABORTED' WHERE txn_id = ? AND state = 'PREPARED'",
                (txn_id,),
            )
            if cur.rowcount == 1:
                row = conn.execute(
                    "SELECT account_id, op, amount FROM prepared_txns WHERE txn_id = ?", (txn_id,)
                ).fetchone()
                if row["op"] == "debit":
                    # Release the hold
                    conn.execute(
                        "UPDATE accounts SET held = held - ? WHERE account_id = ?",
                        (row["amount"], row["account_id"]),
                    )
                conn.commit()
                return True

            conn.execute(
                "INSERT OR IGNORE INTO prepared_txns (txn_id, account_id, op, amount, state, created_at) "
                "VALUES (?, '', 'credit', 0, 'ABORTED', ?)",
                (txn_id, now_ist()),
            )
            state = conn.execute(
                "SELECT state FROM prepared_txns WHERE txn_id = ?", (txn_id,)
            ).fetchone()["state"]
            conn.commit()
            return state == "ABORTED"
        except Exception:
            conn.rollback()
            raise

    def get_prepared_txns(self, min_age_s: float = 0.0) -> list[dict]:
        """Return transactions still in PREPARED state at least min_age_s old (for recovery)."""
        rows = self._conn().execute(
            "SELECT * FROM prepared_txns WHERE state = 'PREPARED'"
        ).fetchall()
        now = now_ist_dt()
        return [
            dict(r) for r in rows
            if (now - datetime.fromisoformat(r["created_at"])).total_seconds() >= min_age_s
        ]

    # Local Transfer (same server, atomic)

    def local_transfer(self, txn_id: str, source: str, dest: str, amount: int) -> tuple[bool, str, dict]:
        """Transfer between two accounts on this server. Caller holds both locks."""
        conn = self._conn()
        src = self.get_account(source)
        if src is None:
            return False, "Source account not found", {}
        dst = self.get_account(dest)
        if dst is None:
            return False, "Destination account not found", {}

        avail = src["balance"] - src["held"]
        if avail < amount:
            self.append_log(txn_id, source, "TRANSFER_OUT", amount, src["balance"],
                            "FAILED", "Insufficient balance")
            return False, "Insufficient balance", {}

        # Debit source, credit dest and log both sides in one transaction
        try:
            conn.execute(
                "UPDATE accounts SET balance = balance - ? WHERE account_id = ?",
                (amount, source),
            )
            conn.execute(
                "UPDATE accounts SET balance = balance + ? WHERE account_id = ?",
                (amount, dest),
            )
            src_bal = conn.execute(
                "SELECT balance FROM accounts WHERE account_id = ?", (source,)
            ).fetchone()["balance"]
            dst_bal = conn.execute(
                "SELECT balance FROM accounts WHERE account_id = ?", (dest,)
            ).fetchone()["balance"]
            self._insert_log(conn, txn_id, source, "TRANSFER_OUT", amount, src_bal, "SUCCESS")
            self._insert_log(conn, txn_id, dest, "TRANSFER_IN", amount, dst_bal, "SUCCESS")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return True, "", {source: src_bal, dest: dst_bal}

    # Transaction Log

    def _insert_log(
        self,
        conn: sqlite3.Connection,
        txn_id: str,
        account_id: str,
        txn_type: str,
        amount: int,
        balance_after: Optional[int],
        status: str,
        reason: str = "",
    ) -> None:
        """Insert a local log row inside the caller's open transaction."""
        conn.execute(
            "INSERT INTO local_log (txn_id, account_id, type, amount, balance_after, timestamp, status, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (txn_id, account_id, txn_type, amount, balance_after, now_ist(), status, reason),
        )

    def append_log(
        self,
        txn_id: str,
        account_id: str,
        txn_type: str,
        amount: int,
        balance_after: Optional[int],
        status: str,
        reason: str = "",
    ) -> None:
        """Append a record to the local transaction log."""
        conn = self._conn()
        self._insert_log(conn, txn_id, account_id, txn_type, amount, balance_after, status, reason)
        conn.commit()

    def list_accounts(self) -> list[dict]:
        rows = self._conn().execute(
            "SELECT account_id, owner, balance, held FROM accounts ORDER BY account_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_history(self, account_id: str) -> list[dict]:
        """Return the transaction history for an account, ordered by time."""
        rows = self._conn().execute(
            "SELECT * FROM local_log WHERE account_id = ? ORDER BY id ASC",
            (account_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # Admin

    def reset(self) -> None:
        """Drop all data."""
        conn = self._conn()
        conn.executescript("""
            DELETE FROM local_log;
            DELETE FROM prepared_txns;
            DELETE FROM accounts;
        """)

    def seed(self, accounts: list[dict]) -> None:
        """Insert seed accounts."""
        for a in accounts:
            self.create_account(a["account_id"], a["owner"], a["balance"])
