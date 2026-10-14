"""Entrypoint: python -m smsgw -c /data/config.yaml"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal

from aiohttp import web

from . import config as config_mod
from .bridge import Bridge
from .db import Database
from .matrix import MautrixPort
from .media import MediaStore
from .outbound import OutboundQueue
from .provider.telnyx import TelnyxProvider
from .web import build_app

log = logging.getLogger("smsgw")


async def media_janitor(media: MediaStore) -> None:
    while True:
        try:
            n = await media.cleanup()
            if n:
                log.info("removed %d expired media file(s)", n)
        except Exception:
            log.exception("media cleanup failed")
        await asyncio.sleep(3600)


async def main(config_path: str) -> None:
    cfg = config_mod.load(config_path)
    logging.basicConfig(
        level=cfg.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    os.makedirs(os.path.dirname(cfg.database) or ".", exist_ok=True)
    db = Database(cfg.database)
    await db.open()

    provider = TelnyxProvider(
        cfg.telnyx_api_key,
        cfg.telnyx_public_key,
        cfg.telnyx_messaging_profile_id,
        cfg.webhook_tolerance_seconds,
    )
    queue = OutboundQueue(cfg, db, provider)
    media = MediaStore(db, cfg.media_dir, cfg.public_base_url)
    port = MautrixPort(cfg)
    bridge = Bridge(cfg, db, port, provider, queue, media)

    async def on_matrix_event(evt: dict) -> None:
        if not bridge.wants_matrix_event(evt):
            return
        if await db.add_matrix_event(evt["event_id"], evt.get("room_id", ""), evt):
            bridge.matrix_wakeup.set()

    await port.start(on_matrix_event)
    await bridge.start()

    public = web.AppRunner(build_app(db, provider, bridge.inbound_wakeup.set))
    await public.setup()
    await web.TCPSite(public, cfg.public_host, cfg.public_port).start()
    log.info(
        "running: appservice %s:%d, public %s:%d (%s)",
        cfg.appservice_host, cfg.appservice_port, cfg.public_host, cfg.public_port, cfg.public_base_url,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    tasks = [
        asyncio.create_task(queue.run(), name="outbound"),
        asyncio.create_task(bridge.run_inbound(), name="inbound"),
        asyncio.create_task(bridge.run_matrix(), name="matrix"),
        asyncio.create_task(media_janitor(media), name="media"),
    ]
    await stop.wait()
    log.info("shutting down")
    # Stop intake first, then let workers finish their current item.
    await public.cleanup()
    await port.stop()
    queue.stop()
    bridge.stop()
    done, pending = await asyncio.wait(tasks, timeout=15)
    for t in pending:
        t.cancel()
    await provider.close()
    await db.close()


def cli() -> None:
    ap = argparse.ArgumentParser(prog="smsgw")
    ap.add_argument("-c", "--config", default=os.environ.get("SMSGW_CONFIG", "/data/config.yaml"))
    args = ap.parse_args()
    asyncio.run(main(args.config))


if __name__ == "__main__":
    cli()
