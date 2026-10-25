# matrix-sms-gateway

Matrix application service that bridges a private Matrix group room to SMS/MMS-only members via Telnyx 10DLC.
Each phone number is a real ghost user (`@sms_<digits>`), so messages, poll votes, photos and DMs are attributed
to the person.

- Scope: [docs/BRIEF.md](docs/BRIEF.md)
- Decisions (and open ones): [docs/DECISIONS.md](docs/DECISIONS.md)
- What's verified vs. inferred: [docs/VERIFIED.md](docs/VERIFIED.md)
- Deploy runbook: [docs/DEPLOY.md](docs/DEPLOY.md)
- Optional app login service (email / phone codes): [docs/LOGIN.md](docs/LOGIN.md)

## Layout

| Path | What |
|---|---|
| `smsgw/bridge.py` | routing, SMS commands, polls, DMs, admin `!` commands |
| `smsgw/outbound.py` | persistent paced queue; compliance gate at send time |
| `smsgw/commands.py`, `names.py`, `segments.py` | pure parsing/matching/segment logic |
| `smsgw/provider/telnyx.py` | Telnyx adapter behind `provider/base.py` |
| `smsgw/matrix.py` | mautrix-python appservice implementation of `port.py` |
| `smsgw/web.py` | public listener: `/telnyx/webhook`, `/m/<token>`, `/healthz`; internal listener for the login service |
| `smsgw/login/` | app login service, its own process (`smsgw-login`); see docs/LOGIN.md |
| `scripts/` | registration generator, Telnyx profile setup (dry-run default), backup |

## Dev

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

Secrets live in `.env` only (gitignored).

## License

Copyright (C) 2026 Ryan Martin

GNU Affero General Public License v3.0 or later. See [LICENSE](LICENSE).
If you run a modified version as a network service, you must offer its users the modified source.
