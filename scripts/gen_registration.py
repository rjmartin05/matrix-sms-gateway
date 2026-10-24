#!/usr/bin/env python3
"""Generate the Synapse appservice registration + matching tokens for .env.

Stdlib only, so it runs on the VM without installing anything:

    python3 gen_registration.py --out /matrix/sms-gateway --domain example.com

Writes <out>/registration.yaml (for Synapse) and appends SMSGW_AS_TOKEN / SMSGW_HS_TOKEN to
<out>/.env. Refuses to overwrite an existing registration (rotating tokens needs both sides
updated together; delete the file first if that's what you want).
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--domain", required=True, help="your Matrix server name (matrix_domain), e.g. example.com")
    ap.add_argument("--id", default="sms-gateway")
    ap.add_argument("--url", default="http://matrix-sms-gateway:29340")
    ap.add_argument("--bot", default="smsbot")
    ap.add_argument("--ghost-prefix", default="sms_")
    args = ap.parse_args()

    reg_path = os.path.join(args.out, "registration.yaml")
    env_path = os.path.join(args.out, ".env")
    if os.path.exists(reg_path):
        print(f"{reg_path} already exists; not overwriting.", file=sys.stderr)
        return 1

    as_token = secrets.token_urlsafe(48)
    hs_token = secrets.token_urlsafe(48)
    dom = re.escape(args.domain)
    # Exclusive namespaces: only the bot and phone-number ghosts. No alias/room namespaces.
    reg = f"""id: {args.id}
url: {args.url}
as_token: {as_token}
hs_token: {hs_token}
sender_localpart: {args.bot}
rate_limited: false
receive_ephemeral: false
namespaces:
  users:
    - exclusive: true
      regex: '@{re.escape(args.bot)}:{dom}'
    - exclusive: true
      regex: '@{re.escape(args.ghost_prefix)}[0-9]{{8,15}}:{dom}'
  aliases: []
  rooms: []
"""
    os.makedirs(args.out, exist_ok=True)
    old_umask = os.umask(0o077)
    try:
        with open(reg_path, "w") as f:
            f.write(reg)
        with open(env_path, "a") as f:
            f.write(f"SMSGW_AS_TOKEN={as_token}\nSMSGW_HS_TOKEN={hs_token}\n")
    finally:
        os.umask(old_umask)
    print(f"wrote {reg_path} and appended tokens to {env_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
