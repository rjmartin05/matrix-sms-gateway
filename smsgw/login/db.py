"""Login service persistence. Its own SQLite file; never shared with the bridge."""

from __future__ import annotations

import time
from typing import Any, Iterable

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- Accounts that sign in with codes. code_managed=1 means the MAS password is random and unknown
-- to anyone; it is replaced at every code login.
CREATE TABLE IF NOT EXISTS accounts (
    mxid TEXT PRIMARY KEY,
    mas_user_id TEXT NOT NULL,
    username TEXT NOT NULL,
    email TEXT UNIQUE,
    phone TEXT UNIQUE,
    code_managed INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- purpose: signup | login | switch; channel: email | sms
-- status: pending | verified | used | failed
CREATE TABLE IF NOT EXISTS verifications (
    id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL,
    channel TEXT NOT NULL,
    identifier TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    mxid TEXT,
    display_name TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS verifications_identifier ON verifications (identifier, status);

-- Code requests, for rate limiting. key is "id:<identifier>" or "ip:<address>".
CREATE TABLE IF NOT EXISTS starts (key TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS starts_key ON starts (key, ts);
"""


class LoginDB:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Cursor:
        assert self.conn
        cur = await self.conn.execute(sql, tuple(params))
        await self.conn.commit()
        return cur

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        assert self.conn
        async with self.conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()

    # ---- accounts --------------------------------------------------------------------

    async def account_by(self, field: str, value: str) -> aiosqlite.Row | None:
        assert field in {"mxid", "email", "phone"}
        return await self.fetchone(f"SELECT * FROM accounts WHERE {field}=?", (value,))

    async def upsert_account(self, mxid: str, mas_user_id: str, username: str, **fields: Any) -> None:
        assert set(fields) <= {"email", "phone", "code_managed"}
        now = time.time()
        await self.execute(
            "INSERT INTO accounts (mxid, mas_user_id, username, created_at, updated_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (mxid) DO NOTHING",
            (mxid, mas_user_id, username, now, now),
        )
        for k, v in fields.items():
            await self.execute(f"UPDATE accounts SET {k}=?, updated_at=? WHERE mxid=?", (v, now, mxid))

    # ---- verifications ---------------------------------------------------------------

    async def add_verification(self, vid: str, purpose: str, channel: str, identifier: str, code_hash: str,
                               expires_at: float, *, mxid: str | None = None, display_name: str | None = None) -> None:
        # A new request replaces any earlier pending one for the same identifier.
        await self.execute(
            "UPDATE verifications SET status='failed' WHERE identifier=? AND status IN ('pending', 'verified')",
            (identifier,),
        )
        await self.execute(
            "INSERT INTO verifications (id, purpose, channel, identifier, code_hash, mxid, display_name,"
            " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (vid, purpose, channel, identifier, code_hash, mxid, display_name, time.time(), expires_at),
        )

    async def get_verification(self, vid: str) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM verifications WHERE id=?", (vid,))

    async def pending_for(self, identifier: str, channel: str) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT * FROM verifications WHERE identifier=? AND channel=? AND status='pending'"
            " ORDER BY created_at DESC LIMIT 1",
            (identifier, channel),
        )

    async def claim_attempt(self, vid: str, limit: int) -> int | None:
        """Reserve one guess before comparing, so concurrent guesses can't exceed ``limit``.
        Returns the attempt number, or None if the request isn't pending or is used up."""
        cur = await self.execute(
            "UPDATE verifications SET attempts=attempts+1 WHERE id=? AND status='pending' AND attempts<?",
            (vid, limit),
        )
        if cur.rowcount != 1:
            return None
        row = await self.get_verification(vid)
        return row["attempts"] if row else None

    async def transition(self, vid: str, old: str, new: str) -> bool:
        """Atomic status change; False if another request got there first."""
        cur = await self.execute("UPDATE verifications SET status=? WHERE id=? AND status=?", (new, vid, old))
        return cur.rowcount == 1

    # ---- rate limiting ---------------------------------------------------------------

    async def count_starts(self, key: str, since: float) -> int:
        row = await self.fetchone("SELECT COUNT(*) AS n FROM starts WHERE key=? AND ts>=?", (key, since))
        return row["n"] if row else 0

    async def add_start(self, key: str, ts: float) -> None:
        await self.execute("INSERT INTO starts (key, ts) VALUES (?, ?)", (key, ts))

    async def prune(self, before: float) -> None:
        await self.execute("DELETE FROM starts WHERE ts<?", (before,))
        await self.execute("DELETE FROM verifications WHERE expires_at<?", (before,))
