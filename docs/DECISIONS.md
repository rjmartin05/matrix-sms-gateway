# Decisions

Status: **OPEN** = awaiting Ryan. Leans are recommendations, not decisions.

## D1. Language / framework — OPEN

| | mautrix-go bridgev2 | Python, lean appservice on `mautrix.appservice` |
|---|---|---|
| Maintenance | Very active (commits today) | Maintained, slow (v0.21.1 Jul 2026, commits Oct 2026) |
| Model fit | Built for "Matrix user logs into a remote account, each remote chat → portal room the bridge creates". Our case is a fan-out relay into an existing room + SMS command UX + compliance state machine. Relay mode and ghosts help; login/portal/room-creation model is friction. (Inference — not prototyped.) | Nothing assumed; we write the room/ghost logic ourselves. The appservice protocol surface we need is small. |
| What we get free | Ghost mgmt, DB migrations, relay formatting, media helpers, admin command plumbing | Intent API (masquerading as ghosts), transaction handling, state store, typed events |
| What we write anyway | Telnyx adapter, paced queue, opt-in state, command registry, name matcher, polls→VOTE, DM routing | Same, plus room/ghost glue |
| Your maintenance | Go | Your daily language |

**Lean: Python.** The framework would save the least on the parts that are actually hard here (compliance,
pacing, SMS UX), and you'll be the one debugging it at 11pm when the NAS reboots.

## D2. State store — OPEN

- **SQLite (WAL)** on a bind-mounted dir. Zero ops, one file, `sqlite3 .backup` into the nightly job.
- **Playbook Postgres.** Already in the planned nightly dump; more moving parts and couples the bridge to
  playbook DB management.

**Lean: SQLite.** Volume is tiny; single writer.

## D3. Webhook / media ingress — OPEN

- **Separate hostname** (e.g. `sms-hook.ryanjmartin.net → http://localhost:<port>`), served by the bridge
  directly, bypassing Traefik. Hosts `/telnyx/webhook` and `/m/<token>` (MMS media for Telnyx to fetch).
  Must be public (Telnyx can't pass Cloudflare Access) — signature-verified and unguessable tokens.
- **Path on chat.ryanjmartin.net** via Traefik. Ties it to playbook Traefik config and to whatever Access
  policy you put on that host.

**Lean: separate hostname.**

## D4. Reactions in v1 — OPEN

Skip (lean: each reaction is a paid SMS and noise) vs. minimal `Dave reacted 👍 to "Saturday?…"`.

## D5. STOP/START/HELP ownership — OPEN (see VERIFIED.md)

Telnyx blocks on STOP itself. Lean: Telnyx owns STOP/START blocking + replies (configured with the
registered wording via `autoresp_configs`); bridge owns JOIN (allowlist-gated) and the extended HELP list,
records every opt-in/out from the webhook, and never replies to messages carrying `autoresponse_type`.
HELP double-send behavior to be tested on Ryan's phone.
