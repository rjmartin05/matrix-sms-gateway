"""Provider-neutral SMS interface. Telnyx is the only implementation today."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol


@dataclass(frozen=True)
class InboundMedia:
    url: str
    content_type: str
    size: int | None = None


@dataclass(frozen=True)
class InboundMessage:
    provider_id: str
    from_number: str
    to_number: str
    text: str
    media: tuple[InboundMedia, ...] = ()
    carrier: str | None = None
    # Set when the provider itself already handled an opt-in/out/help keyword and replied.
    autoresponse_type: str | None = None


@dataclass(frozen=True)
class DeliveryUpdate:
    provider_id: str
    to_number: str
    status: str  # provider's status string, e.g. delivered / delivery_failed / sending_failed
    failed: bool
    delivered: bool
    error: str | None = None


@dataclass(frozen=True)
class WebhookEvent:
    key: str  # dedupe key
    event_type: str
    message: InboundMessage | None = None
    delivery: DeliveryUpdate | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SendResult:
    provider_id: str
    parts: int
    carrier: str | None = None


class SendError(Exception):
    """kind: blocked | rate_limited | spend_limit | transient | permanent"""

    def __init__(self, kind: str, message: str, code: str | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.retry_after = retry_after


class SignatureError(Exception):
    pass


class Provider(Protocol):
    async def send(
        self, *, from_number: str, to_number: str, text: str, media_url: str | None = None
    ) -> SendResult: ...

    def verify(self, body: bytes, headers: Mapping[str, str]) -> None:
        """Raise SignatureError if the webhook isn't authentic."""

    def parse(self, body: bytes) -> WebhookEvent | None: ...

    async def fetch_media(self, url: str, max_bytes: int) -> tuple[bytes, str]: ...

    async def close(self) -> None: ...
