"""Entrypoint: python -m smsgw.login -c /data/login.yaml
    [link-account MXID (--email E | --phone P) | create-invite [--note TEXT]]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time

import aiohttp
from aiohttp import web

from ..config import ConfigError
from . import config as config_mod
from .clients import BridgeNotifier, Mailer, MasAdmin, MatrixClient
from .db import LoginDB
from .service import LoginService, ServiceError
from .web import build_internal_app, build_public_app

log = logging.getLogger("smsgw.login")


async def janitor(db: LoginDB) -> None:
    while True:
        try:
            await db.prune(time.time() - 86400)
        except Exception:
            log.exception("prune failed")
        await asyncio.sleep(3600)


async def run(cfg: config_mod.LoginConfig, command: argparse.Namespace) -> int:
    for name, val in (("SMSLOGIN_MAS_CLIENT_SECRET", cfg.mas_client_secret),
                      ("SMSLOGIN_CODE_SECRET", cfg.code_secret),
                      ("SMSGW_INTERNAL_SECRET", cfg.internal_secret)):
        if not val:
            raise ConfigError(f"missing required environment variable {name}")
    os.makedirs(os.path.dirname(cfg.database) or ".", exist_ok=True)
    db = LoginDB(cfg.database)
    await db.open()
    http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    mailer = (Mailer(cfg.smtp_host, cfg.smtp_port, cfg.smtp_starttls, cfg.smtp_username, cfg.smtp_password,
                     cfg.email_from) if cfg.smtp_host else None)
    bridge = BridgeNotifier(http, cfg.bridge_url, cfg.internal_secret) if cfg.bridge_url else None
    svc = LoginService(cfg, db, MasAdmin(http, cfg.mas_url, cfg.mas_client_id, cfg.mas_client_secret),
                       MatrixClient(http, cfg.client_url), mailer, bridge)
    try:
        if command.cmd == "link-account":
            ident = svc.identifier({"email": command.email, "phone": command.phone})
            await svc.admin_link(command.mxid, ident)
            print(f"{command.mxid} now signs in with codes sent via {ident.channel} ({ident.value})")
            return 0
        if command.cmd == "create-invite":
            inv = await svc.create_invite("cli", command.note)
            print(f"{inv['url']}\n(one use, expires {time.strftime('%Y-%m-%d', time.gmtime(inv['expires_at']))} UTC)")
            return 0
        return await serve(cfg, svc, db)
    except ServiceError as e:
        print(f"error: {e.message}", file=sys.stderr)
        return 1
    finally:
        await http.close()
        await db.close()


async def serve(cfg: config_mod.LoginConfig, svc: LoginService, db: LoginDB) -> int:
    runners = []
    for app, host, port in ((build_public_app(svc, cfg.client_ip_header), cfg.public_host, cfg.public_port),
                            (build_internal_app(svc, cfg.internal_secret), cfg.internal_host, cfg.internal_port)):
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        runners.append(runner)
    log.info("running: public %s:%d, internal %s:%d (email %s, phone %s)", cfg.public_host, cfg.public_port,
             cfg.internal_host, cfg.internal_port, "on" if cfg.smtp_host else "off",
             "on" if cfg.sms_number else "off")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    task = asyncio.create_task(janitor(db))
    await stop.wait()
    log.info("shutting down")
    task.cancel()
    for runner in runners:
        await runner.cleanup()
    return 0


def cli() -> None:
    ap = argparse.ArgumentParser(prog="smsgw-login")
    ap.add_argument("-c", "--config", default=os.environ.get("SMSLOGIN_CONFIG", "/data/login.yaml"))
    sub = ap.add_subparsers(dest="cmd")
    link = sub.add_parser("link-account", help="make an existing account sign in with codes")
    link.add_argument("mxid")
    grp = link.add_mutually_exclusive_group(required=True)
    grp.add_argument("--email")
    grp.add_argument("--phone")
    inv = sub.add_parser("create-invite", help="print a one-time sign-up link")
    inv.add_argument("--note", help="who it's for (kept with the invite)")
    args = ap.parse_args()
    cfg = config_mod.load(args.config)
    logging.basicConfig(level=cfg.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(run(cfg, args)))


if __name__ == "__main__":
    cli()
