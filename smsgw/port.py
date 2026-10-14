"""The Matrix operations the bridge core needs. Implemented by matrix.py (mautrix) and a fake in tests."""

from __future__ import annotations

from typing import Protocol


class MatrixPort(Protocol):
    bot_mxid: str

    def ghost_mxid(self, number: str) -> str: ...

    def number_for_ghost(self, mxid: str) -> str | None: ...

    async def resolve_room(self, room: str) -> str: ...

    async def ensure_ghost(self, number: str, displayname: str) -> None: ...

    async def join(self, room_id: str, mxid: str) -> None:
        """Make ``mxid`` (bot or ghost) a joined member; the bot invites ghosts if needed."""

    async def leave(self, room_id: str, mxid: str) -> None: ...

    async def send_text(
        self,
        room_id: str,
        sender: str,
        body: str,
        *,
        html: str | None = None,
        mention_user_ids: list[str] | None = None,
        mention_room: bool = False,
        notice: bool = False,
        txn_id: str | None = None,
    ) -> str: ...

    async def send_media(
        self,
        room_id: str,
        sender: str,
        data: bytes,
        mimetype: str,
        filename: str,
        *,
        txn_id: str | None = None,
    ) -> str: ...

    async def send_poll_start(
        self, room_id: str, sender: str, question: str, answers: list[tuple[str, str]], style: str
    ) -> str: ...

    async def send_poll_response(
        self, room_id: str, sender: str, poll_event_id: str, answer_id: str, style: str, *, txn_id: str | None = None
    ) -> str: ...

    async def joined_members(self, room_id: str) -> dict[str, str]:
        """mxid -> display name (falls back to localpart)."""

    async def display_name(self, room_id: str, mxid: str) -> str: ...

    async def power_level(self, room_id: str, mxid: str) -> int: ...

    async def get_event(self, room_id: str, event_id: str) -> dict | None: ...

    async def download(self, mxc: str) -> bytes: ...

    async def create_dm(self, creator: str, invitee: str) -> str: ...

    async def pin(self, room_id: str, event_id: str) -> None: ...

    async def set_displayname(self, mxid: str, name: str) -> None: ...
