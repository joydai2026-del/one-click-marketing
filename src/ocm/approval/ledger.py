"""Single-use enforcement for approval tokens.

A signed token proves a human approved something. It does not, on its own, prove they
approved it ONCE. Without a ledger, a valid token is a reusable coupon: replay it and the
same approval authorizes a second publish or a second spend.

`NonceLedger.consume` is therefore the atomic test-and-set the whole gate depends on. The
in-memory implementation is here so the dry-run has no infrastructure requirement; the
SQLite implementation is here because that is the smallest thing that survives a restart
and gives a real uniqueness constraint. In production this is a row in the same
transactional store that records the spend, so an approval and the action it authorized
either both land or neither does.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Protocol

from .errors import ReplayError

__all__ = ["ReplayError", "NonceLedger", "InMemoryLedger", "SqliteLedger"]


class NonceLedger(Protocol):
    def consume(self, nonce: str) -> None:
        """Atomically mark `nonce` used. Raise ReplayError if it was already used."""

    def seen(self, nonce: str) -> bool: ...


class InMemoryLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._used: set[str] = set()

    def consume(self, nonce: str) -> None:
        with self._lock:
            if nonce in self._used:
                raise ReplayError(f"approval nonce already consumed: {nonce[:12]}...")
            self._used.add(nonce)

    def seen(self, nonce: str) -> bool:
        with self._lock:
            return nonce in self._used


class SqliteLedger:
    """Durable ledger. Uniqueness is enforced by the database, not by application code.

    The INSERT is the test-and-set: two concurrent workers presenting the same nonce means
    exactly one INSERT succeeds and the other raises IntegrityError, which becomes a
    ReplayError. Checking-then-inserting in Python would be a race.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS consumed_nonces ("
            "  nonce TEXT PRIMARY KEY,"
            "  consumed_at REAL NOT NULL DEFAULT (strftime('%s','now'))"
            ")"
        )
        self._conn.commit()

    def consume(self, nonce: str) -> None:
        try:
            with self._conn:
                self._conn.execute("INSERT INTO consumed_nonces (nonce) VALUES (?)", (nonce,))
        except sqlite3.IntegrityError as exc:
            raise ReplayError(f"approval nonce already consumed: {nonce[:12]}...") from exc

    def seen(self, nonce: str) -> bool:
        cur = self._conn.execute("SELECT 1 FROM consumed_nonces WHERE nonce = ?", (nonce,))
        return cur.fetchone() is not None

    def close(self) -> None:
        self._conn.close()
