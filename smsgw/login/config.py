"""Login service configuration: non-secret settings from YAML, secrets from environment (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import yaml

from ..config import ConfigError


@dataclass
class LoginConfig:
    domain: str
    client_url: str  # Matrix client API used for the password login and profile calls
    public_homeserver_url: str  # what the app is told to use as its homeserver
    mas_url: str  # MAS base URL including any path prefix, e.g. http://matrix-authentication-service:8080/auth
    mas_client_id: str
    mas_client_secret: str
    public_host: str
    public_port: int
    internal_host: str
    internal_port: int
    internal_secret: str
    bridge_url: str | None  # bridge internal API; None disables bridge notifications
    database: str
    code_secret: str
    code_ttl_seconds: int
    code_max_attempts: int
    starts_per_hour: int  # per email/phone
    starts_per_hour_ip: int
    sms_number: str | None  # number people text their code to; None disables phone verification
    smtp_host: str | None  # None disables email verification
    smtp_port: int
    smtp_starttls: bool
    smtp_username: str | None
    smtp_password: str | None
    email_from: str
    app_name: str
    device_name: str
    client_ip_header: str | None
    log_level: str = "INFO"


def _env(name: str, required: bool = True) -> str | None:
    val = os.environ.get(name)
    if required and not val:
        raise ConfigError(f"missing required environment variable {name}")
    return val


def load(path: str) -> LoginConfig:
    with open(path) as f:
        y = yaml.safe_load(f) or {}

    def get(section: str, key: str, default: Any = ...) -> Any:
        sec = y.get(section) or {}
        if key in sec:
            return sec[key]
        if default is ...:
            raise ConfigError(f"missing config {section}.{key}")
        return default

    sms_number = get("codes", "sms_number", None)
    if sms_number is not None and ("X" in str(sms_number) or not str(sms_number).startswith("+")):
        raise ConfigError(f"codes.sms_number {sms_number!r} is a placeholder or not E.164")
    smtp_host = get("email", "smtp_host", None) or None
    if sms_number is None and smtp_host is None:
        raise ConfigError("enable at least one of codes.sms_number or email.smtp_host")
    bridge_url = get("bridge", "url", None)
    client_url = str(get("matrix", "client_url")).rstrip("/")

    return LoginConfig(
        domain=get("matrix", "domain"),
        client_url=client_url,
        public_homeserver_url=str(get("matrix", "public_homeserver_url", client_url)).rstrip("/"),
        mas_url=str(get("mas", "url")).rstrip("/"),
        mas_client_id=get("mas", "client_id"),
        mas_client_secret=_env("SMSLOGIN_MAS_CLIENT_SECRET") or "",
        public_host=get("listen", "public_host", "0.0.0.0"),
        public_port=int(get("listen", "public_port", 29342)),
        internal_host=get("listen", "internal_host", "0.0.0.0"),
        internal_port=int(get("listen", "internal_port", 29343)),
        internal_secret=_env("SMSGW_INTERNAL_SECRET") or "",
        bridge_url=str(bridge_url).rstrip("/") if bridge_url else None,
        database=get("storage", "database", "/data/login.db"),
        code_secret=_env("SMSLOGIN_CODE_SECRET") or "",
        code_ttl_seconds=int(get("codes", "ttl_minutes", 10)) * 60,
        code_max_attempts=int(get("codes", "max_attempts", 5)),
        starts_per_hour=int(get("codes", "starts_per_hour", 5)),
        starts_per_hour_ip=int(get("codes", "starts_per_hour_ip", 20)),
        sms_number=str(sms_number) if sms_number else None,
        smtp_host=smtp_host,
        smtp_port=int(get("email", "smtp_port", 587)),
        smtp_starttls=bool(get("email", "starttls", True)),
        smtp_username=get("email", "smtp_username", None),
        smtp_password=_env("SMSLOGIN_SMTP_PASSWORD", required=False),
        email_from=get("email", "from", "noreply@example.com"),
        app_name=get("app", "name", "Example Chat"),
        device_name=get("app", "device_name", "Example Chat app"),
        client_ip_header=get("listen", "client_ip_header", "CF-Connecting-IP") or None,
        log_level=get("logging", "level", "INFO"),
    )
