"""Configuration: non-secret settings from YAML, secrets from environment (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class RoomConfig:
    room: str  # room ID or alias; resolved to an ID at startup
    number: str  # E.164 sending number for this room


@dataclass(frozen=True)
class CarrierClass:
    name: str
    match: tuple[str, ...]  # case-insensitive substrings of the carrier name
    per_minute: int | None = None
    daily_segments: int | None = None


@dataclass(frozen=True)
class Pricing:
    sms_part: float = 0.004
    sms_carrier_fee: float = 0.004
    mms_out: float = 0.015
    mms_in: float = 0.005
    mms_carrier_fee: float = 0.01
    daily_spend_limit: float = 15.0  # mirror of the Telnyx profile limit (D8); 0 disables the alert
    daily_spend_alert_fraction: float = 0.6


@dataclass
class Config:
    homeserver_url: str
    domain: str
    as_id: str
    as_token: str
    hs_token: str
    bot_localpart: str
    bot_displayname: str
    ghost_prefix: str
    ghost_name_suffix: str
    appservice_host: str
    appservice_port: int
    public_base_url: str
    public_host: str
    public_port: int
    database: str
    media_dir: str
    media_ttl_hours: float
    link_ttl_days: float
    telnyx_api_key: str
    telnyx_public_key: str
    telnyx_messaging_profile_id: str | None
    webhook_tolerance_seconds: int
    brand: str
    contact_email: str
    rooms: list[RoomConfig]
    admin_power_level: int
    admins: list[str]
    alerts_room: str | None
    global_per_second: float
    unknown_carrier_class: str
    carrier_classes: list[CarrierClass]
    max_attempts: int
    max_queue_age_hours: float
    failure_alert_threshold: int
    pricing: Pricing
    poll_style: str  # unstable | stable — event types used for polls the bot creates
    reply_quote_format: str
    reply_quote_chars: int
    image_max_px: int
    image_max_bytes: int
    inbound_media_max_bytes: int
    log_level: str = "INFO"
    extra: dict[str, Any] = field(default_factory=dict)
    # App login service (D13). Unset = no sign-in code forwarding and no internal API.
    login_url: str | None = None
    internal_host: str = "0.0.0.0"
    internal_port: int = 29344
    internal_secret: str | None = None
    # D14: SMS members are real accounts (created by the login service) instead of @<prefix><digits>
    # ghosts. Needs the login service and a registration whose user namespace covers real users.
    sms_accounts: bool = False
    # D14: one text sent when someone is added, before they JOIN. None = send nothing until they JOIN.
    # Must match the 10DLC campaign; {brand}, {adder} and {number} are filled in.
    invite_text: str | None = None
    members_can_add: bool = True
    adds_per_day: int = 20  # per person adding, so nobody can text-bomb numbers through the bridge


def _env(name: str, required: bool = True) -> str | None:
    val = os.environ.get(name)
    if required and not val:
        raise ConfigError(f"missing required environment variable {name}")
    return val


def load(path: str) -> Config:
    with open(path) as f:
        y = yaml.safe_load(f) or {}

    def get(section: str, key: str, default: Any = ...) -> Any:
        sec = y.get(section) or {}
        if key in sec:
            return sec[key]
        if default is ...:
            raise ConfigError(f"missing config {section}.{key}")
        return default

    rooms = [RoomConfig(str(r["room"]), str(r["number"])) for r in (y.get("rooms") or [])]
    if not rooms:
        raise ConfigError("at least one entry under rooms: is required")
    for r in rooms:
        if "X" in r.number or not r.number.startswith("+"):
            raise ConfigError(f"room {r.room}: number {r.number!r} is a placeholder or not E.164")
    numbers = [r.number for r in rooms]
    if len(set(numbers)) != len(numbers):
        raise ConfigError("each room needs its own number (one room <-> one number)")

    pacing = y.get("pacing") or {}
    classes = [
        CarrierClass(
            name=c["name"],
            match=tuple(m.lower() for m in c.get("match", [])),
            per_minute=c.get("per_minute"),
            daily_segments=c.get("daily_segments"),
        )
        for c in pacing.get("carriers", [])
    ]
    pricing = Pricing(**(y.get("pricing") or {}))

    login_url = get("login", "service_url", None)
    internal_secret = _env("SMSGW_INTERNAL_SECRET") if y.get("login") else None

    sms_accounts = bool(get("appservice", "sms_accounts", False))
    if sms_accounts and not login_url:
        raise ConfigError("appservice.sms_accounts needs login.service_url (accounts are created there)")
    invite_text = get("compliance", "invite_text", None) or None
    if invite_text and "STOP" not in invite_text:
        raise ConfigError("compliance.invite_text must tell people how to opt out (include STOP)")
    if invite_text:
        try:
            invite_text.format(brand="", adder="", number="")
        except (KeyError, IndexError, ValueError) as e:
            raise ConfigError(f"compliance.invite_text: only {{brand}}, {{adder}} and {{number}} can be filled in ({e!r})")

    poll_style = get("matrix", "poll_style", "unstable")
    if poll_style not in ("stable", "unstable"):
        raise ConfigError("matrix.poll_style must be stable or unstable")

    return Config(
        homeserver_url=get("homeserver", "address"),
        domain=get("homeserver", "domain"),
        as_id=get("appservice", "id", "sms-gateway"),
        as_token=_env("SMSGW_AS_TOKEN") or "",
        hs_token=_env("SMSGW_HS_TOKEN") or "",
        bot_localpart=get("appservice", "bot_localpart", "smsbot"),
        bot_displayname=get("appservice", "bot_displayname", "SMS Bridge"),
        ghost_prefix=get("appservice", "ghost_prefix", "sms_"),
        ghost_name_suffix=get("appservice", "ghost_name_suffix", " (SMS)"),
        appservice_host=get("appservice", "listen_host", "0.0.0.0"),
        appservice_port=int(get("appservice", "listen_port", 29340)),
        public_base_url=str(get("public", "base_url")).rstrip("/"),
        public_host=get("public", "listen_host", "0.0.0.0"),
        public_port=int(get("public", "listen_port", 29341)),
        database=get("storage", "database", "/data/smsgw.db"),
        media_dir=get("storage", "media_dir", "/data/media"),
        media_ttl_hours=float(get("public", "media_ttl_hours", 24)),
        link_ttl_days=float(get("public", "link_ttl_days", 7)),
        telnyx_api_key=_env("TELNYX_API_KEY") or "",
        telnyx_public_key=_env("TELNYX_PUBLIC_KEY") or "",
        telnyx_messaging_profile_id=os.environ.get("TELNYX_MESSAGING_PROFILE_ID") or None,
        webhook_tolerance_seconds=int(get("telnyx", "webhook_tolerance_seconds", 300)),
        brand=get("compliance", "brand"),  # required: must match the 10DLC registration
        contact_email=get("compliance", "contact_email"),
        rooms=rooms,
        admin_power_level=int(get("matrix", "admin_power_level", 50)),
        admins=list(get("matrix", "admins", []) or []),
        alerts_room=get("matrix", "alerts_room", None),
        global_per_second=float(pacing.get("global_per_second", 1)),
        unknown_carrier_class=pacing.get("unknown_carrier_class", "unknown"),
        carrier_classes=classes,
        max_attempts=int(pacing.get("max_attempts", 6)),
        max_queue_age_hours=float(pacing.get("max_queue_age_hours", 12)),
        failure_alert_threshold=int(get("matrix", "failure_alert_threshold", 3)),
        pricing=pricing,
        poll_style=poll_style,
        reply_quote_format=get("sms", "reply_quote_format", 'Re {name}: "{quote}" '),
        reply_quote_chars=int(get("sms", "reply_quote_chars", 20)),
        image_max_px=int(get("sms", "image_max_px", 1200)),
        image_max_bytes=int(get("sms", "image_max_bytes", 600_000)),
        inbound_media_max_bytes=int(get("sms", "inbound_media_max_bytes", 5_000_000)),
        log_level=get("logging", "level", "INFO"),
        login_url=str(login_url).rstrip("/") if login_url else None,
        internal_host=get("login", "internal_listen_host", "0.0.0.0"),
        internal_port=int(get("login", "internal_listen_port", 29344)),
        internal_secret=internal_secret,
        sms_accounts=sms_accounts,
        invite_text=invite_text,
        members_can_add=bool(get("matrix", "members_can_add", True)),
        adds_per_day=int(get("matrix", "adds_per_day", 20)),
    )
