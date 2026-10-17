#!/usr/bin/env python3
"""Configure the Telnyx messaging profile for the gateway. Dry-run unless --apply.

Reads TELNYX_API_KEY and TELNYX_MESSAGING_PROFILE_ID from the environment (e.g. `set -a; . .env`).
Stdlib only.

What it does (per decisions in docs/DECISIONS.md, D5):
  - webhook_url -> https://sms-hook.example.com/telnyx/webhook, webhook_api_version "2"
  - features.ai_opt_out_detection_enabled = false
  - daily_spend_limit 15.00 enabled (D8; the bridge also alerts at ~60%), smart_encoding = false (D9)
  - autoresp_configs: STOP (registered opt-out text) and START (registered opt-in text), US only.
    YES/JOIN are NOT Telnyx keywords: the bridge owns JOIN, and "yes" is a normal group reply.
  - HELP autoresp only with --help-autoresp (use if live testing shows Telnyx double-replies to HELP).

Verified against developers.telnyx.com 2026-10-14: PATCH /v2/messaging_profiles/{id} accepts
webhook_url / webhook_api_version / features.ai_opt_out_detection_enabled; POST
/v2/messaging_profiles/{id}/autoresp_configs takes {op, keywords, resp_text, country_code}.
Unverified: the exact GET listing shape for autoresp_configs (printed raw so you can check).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.telnyx.com/v2"
BRAND = "Example Chat"
EMAIL = "help@example.com"

OPT_IN = (
    f"{BRAND}: You've joined the group text relay. Group messages will be sent to this number; "
    "reply to post to the group. Message frequency varies. Msg&data rates may apply. "
    "Reply HELP for help, STOP to opt out."
)
OPT_OUT = f"{BRAND}: You are unsubscribed and will receive no further messages. Reply START to rejoin."
HELP = f"{BRAND}: Group text relay. Reply STOP to opt out, START to rejoin. Help: {EMAIL}"


def call(method: str, path: str, key: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        print(f"{method} {path} -> HTTP {e.code}: {e.read().decode()[:500]}", file=sys.stderr)
        raise SystemExit(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually make changes (default: dry run)")
    ap.add_argument("--webhook-url", default="https://sms-hook.example.com/telnyx/webhook")
    ap.add_argument("--help-autoresp", action="store_true")
    ap.add_argument("--daily-spend-limit", default="15.00",
                    help="USD per day (D8, default 15.00). Keep pricing.daily_spend_limit in config.yaml equal.")
    args = ap.parse_args()

    key = os.environ.get("TELNYX_API_KEY")
    profile = os.environ.get("TELNYX_MESSAGING_PROFILE_ID")
    if not key or not profile:
        print("Set TELNYX_API_KEY and TELNYX_MESSAGING_PROFILE_ID (e.g. set -a; . /matrix/sms-gateway/.env)", file=sys.stderr)
        return 1

    current = call("GET", f"/messaging_profiles/{profile}", key).get("data", {})
    print("Current profile:", json.dumps({k: current.get(k) for k in (
        "name", "webhook_url", "webhook_api_version", "features", "daily_spend_limit", "daily_spend_limit_enabled",
        "smart_encoding", "mms_transcoding")}, indent=2))
    existing = call("GET", f"/messaging_profiles/{profile}/autoresp_configs", key)
    print("Existing autoresp_configs (raw):", json.dumps(existing, indent=2))

    patch: dict = {
        "webhook_url": args.webhook_url,
        "webhook_api_version": "2",
        "features": {"ai_opt_out_detection_enabled": False},
        "smart_encoding": False,  # D9: the bridge normalizes typography itself; never rewrite member text
        "daily_spend_limit": args.daily_spend_limit,
        "daily_spend_limit_enabled": True,
    }

    configs = [
        {"op": "stop", "keywords": ["STOP"], "resp_text": OPT_OUT, "country_code": "US"},
        {"op": "start", "keywords": ["START"], "resp_text": OPT_IN, "country_code": "US"},
    ]
    if args.help_autoresp:
        configs.append({"op": "help", "keywords": ["HELP"], "resp_text": HELP, "country_code": "US"})

    have_ops = {
        (c.get("op"), c.get("country_code"))
        for c in (existing.get("data") or [])
        if isinstance(c, dict)
    }
    print("\nPlanned PATCH:", json.dumps(patch, indent=2))
    for c in configs:
        tag = " (already exists for this op/country: update it in the portal or delete first)" if (c["op"], "US") in have_ops else ""
        print(f"Planned autoresp {c['op']}:{tag}\n  {c['resp_text']}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to make these changes.")
        return 0

    call("PATCH", f"/messaging_profiles/{profile}", key, patch)
    for c in configs:
        if (c["op"], "US") in have_ops:
            print(f"skipped {c['op']}: already configured")
            continue
        call("POST", f"/messaging_profiles/{profile}/autoresp_configs", key, c)
        print(f"created autoresp {c['op']}")
    print("Done. Re-run without --apply to review the result.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
