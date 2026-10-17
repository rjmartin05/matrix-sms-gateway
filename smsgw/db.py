"""SQLite persistence. One file, WAL mode, single process."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterable

import aiosqlite

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- One row per (room, phone number). status: allowlisted | opted_in | opted_out | removed
CREATE TABLE IF NOT EXISTS members (
    room_id TEXT NOT NULL,
    number TEXT NOT NULL,
    mxid TEXT NOT NULL,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL,
    photos_on INTEGER NOT NULL DEFAULT 1,
    carrier TEXT,
    ever_joined INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_dm_from TEXT,          -- mxid of whoever last DMed this number
    added_by TEXT,
    added_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (room_id, number)
);

-- Permanent, append-only consent record. Never delete rows.
CREATE TABLE IF NOT EXISTS consent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    number TEXT NOT NULL,
    room_id TEXT,
    event TEXT NOT NULL,        -- opt_in | opt_out | resubscribe | join_rejected | start_ignored | ...
    keyword TEXT,
    source TEXT NOT NULL,       -- bridge | telnyx_autoresponse | send_error | admin
    provider_message_id TEXT,
    detail TEXT
);

-- Durable inbox for provider webhooks. key dedupes retries.
CREATE TABLE IF NOT EXISTS inbound (
    key TEXT PRIMARY KEY,
    received_at REAL NOT NULL,
    event_type TEXT NOT NULL,
    raw TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0
);

-- Durable inbox for Matrix events.
CREATE TABLE IF NOT EXISTS matrix_inbox (
    event_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    received_at REAL NOT NULL,
    raw TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0
);

-- Outbound SMS/MMS queue. status: pending | sending | sent | failed | blocked | expired | unknown | skipped
CREATE TABLE IF NOT EXISTS outbound (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT NOT NULL,
    room_id TEXT,
    from_number TEXT NOT NULL,
    to_number TEXT NOT NULL,
    kind TEXT NOT NULL,         -- group | dm | system | reply
    text TEXT NOT NULL,
    media_url TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    created_at REAL NOT NULL,
    sent_at REAL,
    provider_id TEXT,
    parts INTEGER,
    carrier TEXT,
    error TEXT,
    delivery_status TEXT,
    UNIQUE (source_key, to_number)
);
CREATE INDEX IF NOT EXISTS outbound_pending ON outbound (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS outbound_provider ON outbound (provider_id);

-- SMS -> Matrix mapping (for reply lookups / debugging).
CREATE TABLE IF NOT EXISTS message_map (
    provider_id TEXT PRIMARY KEY,
    matrix_event_id TEXT,
    room_id TEXT,
    number TEXT,
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS polls (
    event_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    question TEXT NOT NULL,
    answers TEXT NOT NULL,      -- JSON [[id, text], ...]
    style TEXT NOT NULL,        -- stable | unstable
    max_selections INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS poll_votes (
    poll_event_id TEXT NOT NULL,
    voter TEXT NOT NULL,
    answer_ids TEXT NOT NULL,   -- JSON list
    ts REAL NOT NULL,
    PRIMARY KEY (poll_event_id, voter)
);

-- Unencrypted DM rooms between a ghost and a Matrix user.
CREATE TABLE IF NOT EXISTS dm_rooms (
    ghost_mxid TEXT NOT NULL,
    user_mxid TEXT NOT NULL,
    room_id TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    PRIMARY KEY (ghost_mxid, user_mxid)
);

CREATE TABLE IF NOT EXISTS media_tokens (
    token TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    content_type TEXT NOT NULL,
    expires_at REAL NOT NULL
);

-- Monthly usage for cost estimates. kind: sms_out | mms_out | sms_in | mms_in
CREATE TABLE IF NOT EXISTS usage (
    month TEXT NOT NULL,
    kind TEXT NOT NULL,
    messages INTEGER NOT NULL DEFAULT 0,
    segments INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (month, kind)
);

-- A #dm held for confirmation after a non-exact name match. One per (room, number); #yes sends it.
CREATE TABLE IF NOT EXISTS pending_dms (
    room_id TEXT NOT NULL,
    number TEXT NOT NULL,
    target_mxid TEXT NOT NULL,
    target_name TEXT NOT NULL,
    message TEXT NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (room_id, number)
);

-- Matrix reactions posted for SMS reaction texts ("👍 to “…”"), so a removal can redact them.
CREATE TABLE IF NOT EXISTS sms_reactions (
    room_id TEXT NOT NULL,
    number TEXT NOT NULL,
    target_event_id TEXT NOT NULL,
    reaction_key TEXT NOT NULL,
    reaction_event_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (room_id, number, target_event_id, reaction_key)
);

-- Daily per-carrier-class segment counts (for caps like T-Mobile's).
CREATE TABLE IF NOT EXISTS daily_segments (
    day TEXT NOT NULL,
    carrier_class TEXT NOT NULL,
    segments INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, carrier_class)
);
"""


@dataclass
class Member:
    room_id: str
    number: str
    mxid: str
    display_name: str
    status: str
    photos_on: bool
    carrier: str | None
    ever_joined: bool
    consecutive_failures: int
    last_dm_from: str | None

    @classmethod
    def from_row(cls, r: aiosqlite.Row) -> "Member":
        return cls(
            room_id=r["room_id"],
            number=r["number"],
            mxid=r["mxid"],
            display_name=r["display_name"],
            status=r["status"],
            photos_on=bool(r["photos_on"]),
            carrier=r["carrier"],
            ever_joined=bool(r["ever_joined"]),
            consecutive_failures=r["consecutive_failures"],
            last_dm_from=r["last_dm_from"],
        )


def month_key(ts: float | None = None) -> str:
    return time.strftime("%Y-%m", time.gmtime(ts or time.time()))


def day_key(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts or time.time()))


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.executescript(SCHEMA)
        await self.conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # ---- low-level helpers -------------------------------------------------------------

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Cursor:
        assert self.conn
        cur = await self.conn.execute(sql, tuple(params))
        await self.conn.commit()
        return cur

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        assert self.conn
        async with self.conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        assert self.conn
        async with self.conn.execute(sql, tuple(params)) as cur:
            return list(await cur.fetchall())

    # ---- members ---------------------------------------------------------------------

    async def get_member(self, room_id: str, number: str) -> Member | None:
        r = await self.fetchone(
            "SELECT * FROM members WHERE room_id=? AND number=?", (room_id, number)
        )
        return Member.from_row(r) if r else None

    async def get_member_by_mxid(self, room_id: str, mxid: str) -> Member | None:
        r = await self.fetchone("SELECT * FROM members WHERE room_id=? AND mxid=?", (room_id, mxid))
        return Member.from_row(r) if r else None

    async def members(self, room_id: str, status: str | None = None) -> list[Member]:
        if status:
            rows = await self.fetchall(
                "SELECT * FROM members WHERE room_id=? AND status=? ORDER BY display_name",
                (room_id, status),
            )
        else:
            rows = await self.fetchall(
                "SELECT * FROM members WHERE room_id=? ORDER BY display_name", (room_id,)
            )
        return [Member.from_row(r) for r in rows]

    async def upsert_member(
        self, room_id: str, number: str, mxid: str, name: str, added_by: str
    ) -> Member:
        now = time.time()
        existing = await self.get_member(room_id, number)
        if existing is None:
            await self.execute(
                "INSERT INTO members (room_id, number, mxid, display_name, status, added_by,"
                " added_at, updated_at) VALUES (?, ?, ?, ?, 'allowlisted', ?, ?, ?)",
                (room_id, number, mxid, name, added_by, now, now),
            )
        else:
            new_status = "allowlisted" if existing.status == "removed" else existing.status
            await self.execute(
                "UPDATE members SET display_name=?, status=?, updated_at=? WHERE room_id=? AND number=?",
                (name, new_status, now, room_id, number),
            )
        m = await self.get_member(room_id, number)
        assert m
        return m

    async def set_member_status(self, room_id: str, number: str, status: str) -> None:
        extra = ", ever_joined=1" if status == "opted_in" else ""
        await self.execute(
            f"UPDATE members SET status=?, updated_at=?{extra} WHERE room_id=? AND number=?",
            (status, time.time(), room_id, number),
        )

    async def set_member_field(self, room_id: str, number: str, field: str, value: Any) -> None:
        assert field in {"display_name", "photos_on", "carrier", "last_dm_from", "consecutive_failures"}
        await self.execute(
            f"UPDATE members SET {field}=?, updated_at=? WHERE room_id=? AND number=?",
            (value, time.time(), room_id, number),
        )

    async def set_number_status_all_rooms(self, number: str, status: str) -> None:
        """Telnyx opt-out blocks are per messaging profile, so they apply across rooms."""
        await self.execute(
            "UPDATE members SET status=?, updated_at=? WHERE number=? AND status != 'removed'",
            (status, time.time(), number),
        )

    # ---- consent ---------------------------------------------------------------------

    async def log_consent(
        self,
        number: str,
        event: str,
        source: str,
        *,
        room_id: str | None = None,
        keyword: str | None = None,
        provider_message_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        await self.execute(
            "INSERT INTO consent_log (ts, number, room_id, event, keyword, source,"
            " provider_message_id, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), number, room_id, event, keyword, source, provider_message_id, detail),
        )

    # ---- inboxes ---------------------------------------------------------------------

    async def add_inbound(self, key: str, event_type: str, raw: dict) -> bool:
        cur = await self.execute(
            "INSERT OR IGNORE INTO inbound (key, received_at, event_type, raw) VALUES (?, ?, ?, ?)",
            (key, time.time(), event_type, json.dumps(raw)),
        )
        return cur.rowcount > 0

    async def pending_inbound(self, limit: int = 50) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM inbound WHERE processed=0 ORDER BY received_at LIMIT ?", (limit,)
        )

    async def mark_inbound(self, key: str, state: int = 1) -> None:
        await self.execute("UPDATE inbound SET processed=? WHERE key=?", (state, key))

    async def add_matrix_event(self, event_id: str, room_id: str, raw: dict) -> bool:
        cur = await self.execute(
            "INSERT OR IGNORE INTO matrix_inbox (event_id, room_id, received_at, raw) VALUES (?, ?, ?, ?)",
            (event_id, room_id, time.time(), json.dumps(raw)),
        )
        return cur.rowcount > 0

    async def pending_matrix(self, limit: int = 50) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM matrix_inbox WHERE processed=0 ORDER BY received_at LIMIT ?", (limit,)
        )

    async def mark_matrix(self, event_id: str, state: int = 1) -> None:
        await self.execute("UPDATE matrix_inbox SET processed=? WHERE event_id=?", (state, event_id))

    # ---- outbound --------------------------------------------------------------------

    async def enqueue(
        self,
        *,
        source_key: str,
        room_id: str | None,
        from_number: str,
        to_number: str,
        kind: str,
        text: str,
        media_url: str | None = None,
        carrier: str | None = None,
    ) -> bool:
        now = time.time()
        cur = await self.execute(
            "INSERT OR IGNORE INTO outbound (source_key, room_id, from_number, to_number, kind, text,"
            " media_url, next_attempt_at, created_at, carrier) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source_key, room_id, from_number, to_number, kind, text, media_url, now, now, carrier),
        )
        return cur.rowcount > 0

    async def due_outbound(self, now: float, limit: int = 200) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM outbound WHERE status='pending' ORDER BY id LIMIT ?", (limit,)
        )

    async def recover_interrupted_sends(self) -> int:
        """Rows left in 'sending' by a crash may or may not have gone out. Never resend them."""
        cur = await self.execute(
            "UPDATE outbound SET status='unknown', error='interrupted during send (not retried)'"
            " WHERE status='sending'"
        )
        return cur.rowcount

    async def update_outbound(self, row_id: int, **fields: Any) -> None:
        allowed = {
            "status", "attempts", "next_attempt_at", "sent_at", "provider_id", "parts",
            "carrier", "error", "delivery_status",
        }
        assert set(fields) <= allowed
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.execute(f"UPDATE outbound SET {cols} WHERE id=?", (*fields.values(), row_id))

    async def outbound_by_provider_id(self, provider_id: str) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM outbound WHERE provider_id=?", (provider_id,))

    async def skip_pending_for(self, number: str, reason: str) -> int:
        cur = await self.execute(
            "UPDATE outbound SET status='skipped', error=? WHERE to_number=? AND status='pending'",
            (reason, number),
        )
        return cur.rowcount

    # ---- usage -----------------------------------------------------------------------

    async def add_usage(self, kind: str, segments: int, ts: float | None = None) -> None:
        await self.execute(
            "INSERT INTO usage (month, kind, messages, segments) VALUES (?, ?, 1, ?)"
            " ON CONFLICT(month, kind) DO UPDATE SET messages=messages+1, segments=segments+excluded.segments",
            (month_key(ts), kind, segments),
        )

    async def month_usage(self, month: str | None = None) -> dict[str, tuple[int, int]]:
        rows = await self.fetchall(
            "SELECT kind, messages, segments FROM usage WHERE month=?", (month or month_key(),)
        )
        return {r["kind"]: (r["messages"], r["segments"]) for r in rows}

    async def add_daily_segments(self, carrier_class: str, segments: int) -> None:
        await self.execute(
            "INSERT INTO daily_segments (day, carrier_class, segments) VALUES (?, ?, ?)"
            " ON CONFLICT(day, carrier_class) DO UPDATE SET segments=segments+excluded.segments",
            (day_key(), carrier_class, segments),
        )

    async def daily_segments(self, carrier_class: str) -> int:
        r = await self.fetchone(
            "SELECT segments FROM daily_segments WHERE day=? AND carrier_class=?",
            (day_key(), carrier_class),
        )
        return r["segments"] if r else 0

    # ---- polls -----------------------------------------------------------------------

    async def add_poll(
        self,
        event_id: str,
        room_id: str,
        sender: str,
        question: str,
        answers: list[tuple[str, str]],
        style: str,
        max_selections: int,
    ) -> None:
        await self.execute(
            "INSERT OR IGNORE INTO polls (event_id, room_id, sender, question, answers, style,"
            " max_selections, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, room_id, sender, question, json.dumps(answers), style, max_selections, time.time()),
        )

    async def latest_open_poll(self, room_id: str) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT * FROM polls WHERE room_id=? AND closed=0 ORDER BY created_at DESC LIMIT 1",
            (room_id,),
        )

    async def get_poll(self, event_id: str) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM polls WHERE event_id=?", (event_id,))

    async def close_poll(self, event_id: str) -> bool:
        cur = await self.execute("UPDATE polls SET closed=1 WHERE event_id=? AND closed=0", (event_id,))
        return cur.rowcount > 0

    async def record_vote(self, poll_event_id: str, voter: str, answer_ids: list[str]) -> None:
        await self.execute(
            "INSERT INTO poll_votes (poll_event_id, voter, answer_ids, ts) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(poll_event_id, voter) DO UPDATE SET answer_ids=excluded.answer_ids, ts=excluded.ts",
            (poll_event_id, voter, json.dumps(answer_ids), time.time()),
        )

    async def poll_votes(self, poll_event_id: str) -> list[list[str]]:
        rows = await self.fetchall(
            "SELECT answer_ids FROM poll_votes WHERE poll_event_id=?", (poll_event_id,)
        )
        return [json.loads(r["answer_ids"]) for r in rows]

    # ---- DM rooms --------------------------------------------------------------------

    async def get_dm_room(self, ghost: str, user: str) -> str | None:
        r = await self.fetchone(
            "SELECT room_id FROM dm_rooms WHERE ghost_mxid=? AND user_mxid=?", (ghost, user)
        )
        return r["room_id"] if r else None

    async def dm_room_info(self, room_id: str) -> tuple[str, str] | None:
        r = await self.fetchone("SELECT ghost_mxid, user_mxid FROM dm_rooms WHERE room_id=?", (room_id,))
        return (r["ghost_mxid"], r["user_mxid"]) if r else None

    async def set_dm_room(self, ghost: str, user: str, room_id: str) -> None:
        await self.execute(
            "INSERT INTO dm_rooms (ghost_mxid, user_mxid, room_id, created_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(ghost_mxid, user_mxid) DO UPDATE SET room_id=excluded.room_id",
            (ghost, user, room_id, time.time()),
        )

    async def delete_dm_room(self, room_id: str) -> None:
        await self.execute("DELETE FROM dm_rooms WHERE room_id=?", (room_id,))

    # ---- media -----------------------------------------------------------------------

    async def add_media_token(self, token: str, path: str, content_type: str, expires_at: float) -> None:
        await self.execute(
            "INSERT INTO media_tokens (token, path, content_type, expires_at) VALUES (?, ?, ?, ?)",
            (token, path, content_type, expires_at),
        )

    async def get_media_token(self, token: str) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT * FROM media_tokens WHERE token=? AND expires_at > ?", (token, time.time())
        )

    async def expired_media(self) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM media_tokens WHERE expires_at <= ?", (time.time(),))

    async def delete_media_token(self, token: str) -> None:
        await self.execute("DELETE FROM media_tokens WHERE token=?", (token,))

    # ---- pending DMs -----------------------------------------------------------------

    async def set_pending_dm(self, room_id: str, number: str, target_mxid: str, target_name: str,
                             message: str, ttl: float) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO pending_dms (room_id, number, target_mxid, target_name, message, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (room_id, number, target_mxid, target_name, message, time.time() + ttl),
        )

    async def get_pending_dm(self, room_id: str, number: str) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT * FROM pending_dms WHERE room_id=? AND number=? AND expires_at > ?",
            (room_id, number, time.time()),
        )

    async def clear_pending_dm(self, room_id: str, number: str) -> None:
        await self.execute("DELETE FROM pending_dms WHERE room_id=? AND number=?", (room_id, number))

    # ---- daily spend -----------------------------------------------------------------

    async def day_traffic(self, since: float) -> dict[str, int]:
        """Today's billable traffic for spend estimates: outbound SMS segments / MMS count, inbound counts."""
        out = await self.fetchone(
            "SELECT COALESCE(SUM(CASE WHEN media_url IS NULL THEN parts ELSE 0 END), 0) AS sms_seg,"
            " COALESCE(SUM(CASE WHEN media_url IS NOT NULL THEN 1 ELSE 0 END), 0) AS mms"
            " FROM outbound WHERE status='sent' AND sent_at >= ?",
            (since,),
        )
        inn = await self.fetchall(
            "SELECT raw FROM inbound WHERE event_type='message.received' AND received_at >= ?", (since,)
        )
        sms_in = mms_in = 0
        for r in inn:
            payload = (json.loads(r["raw"]).get("data") or {}).get("payload") or {}
            if payload.get("media"):
                mms_in += 1
            else:
                sms_in += int(payload.get("parts") or 1)
        return {"sms_seg": out["sms_seg"], "mms": out["mms"], "sms_in_seg": sms_in, "mms_in": mms_in}

    # ---- message map -----------------------------------------------------------------

    async def map_message(self, provider_id: str, event_id: str | None, room_id: str, number: str) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO message_map (provider_id, matrix_event_id, room_id, number, ts)"
            " VALUES (?, ?, ?, ?, ?)",
            (provider_id, event_id, room_id, number, time.time()),
        )

    # ---- reactions -------------------------------------------------------------------

    async def recent_group_texts(self, room_id: str, number: str, since: float, limit: int = 200) -> list[aiosqlite.Row]:
        """Group texts sent to ``number``, newest first: (source_key, text)."""
        return await self.fetchall(
            "SELECT source_key, text FROM outbound WHERE room_id=? AND to_number=? AND kind='group'"
            " AND source_key LIKE 'mx:%' AND status='sent' AND created_at >= ? ORDER BY id DESC LIMIT ?",
            (room_id, number, since, limit),
        )

    async def recent_own_posts(self, room_id: str, number: str, since: float, limit: int = 200) -> list[tuple[str, str]]:
        """Texts ``number`` posted to the group, newest first: (matrix_event_id, original SMS text)."""
        rows = await self.fetchall(
            "SELECT m.matrix_event_id, json_extract(i.raw, '$.data.payload.text') AS text FROM message_map m"
            " JOIN inbound i ON json_extract(i.raw, '$.data.payload.id') = m.provider_id"
            " WHERE m.room_id=? AND m.number=? AND m.ts >= ? AND m.matrix_event_id IS NOT NULL"
            " ORDER BY m.ts DESC LIMIT ?",
            (room_id, number, since, limit),
        )
        return [(r["matrix_event_id"], r["text"] or "") for r in rows]

    async def save_reaction(self, room_id: str, number: str, target: str, key: str, reaction_event_id: str) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO sms_reactions (room_id, number, target_event_id, reaction_key,"
            " reaction_event_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (room_id, number, target, key, reaction_event_id, time.time()),
        )

    async def pop_reaction(self, room_id: str, number: str, target: str, key: str) -> str | None:
        r = await self.fetchone(
            "SELECT reaction_event_id FROM sms_reactions WHERE room_id=? AND number=? AND target_event_id=?"
            " AND reaction_key=?", (room_id, number, target, key),
        )
        if r is None:
            return None
        await self.execute(
            "DELETE FROM sms_reactions WHERE room_id=? AND number=? AND target_event_id=? AND reaction_key=?",
            (room_id, number, target, key),
        )
        return r["reaction_event_id"]

