# SMS ↔ Matrix Gateway — Build Brief

Source of truth for scope. Verification results for §11 live in [VERIFIED.md](VERIFIED.md);
decisions and their outcomes in [DECISIONS.md](DECISIONS.md).

Placeholders (the operator fills/confirms; secrets go in `.env` only, never in git):

- `+1XXXXXXXXXX` — the Telnyx 10DLC number.
- Telnyx API key, Telnyx webhook public key, appservice tokens.
- Repo name: `matrix-sms-gateway` (the repo exists under this name).

## 1. What we're building

A Matrix application service (bridge) that connects a private Matrix group chat to members who only use
plain SMS/MMS, via Telnyx. Replaces GroupMe's SMS mode (discontinued Aug 2026) and should be clearly better.

- ~30–40 members; fewer than ~20 SMS-only. Everyone else uses a Matrix app.
- SMS members are real participants: each phone number gets its own ghost user, so messages, poll votes,
  photos and DMs are attributed to them, not to a bot.
- Personal project. Low volume.

## 2. Working style

- Evan makes decisions. On real choices (language, data model, UX), lay out tradeoffs + lean and ask.
- Flag every placeholder in any command; prefer copy-paste-ready commands with no placeholders.
- Label fact vs. inference; verify version-sensitive facts against current docs/source.
- Direct and technical.

## 3. As-built environment (facts)

**Host**

- A small Linux VM (Debian) on a home server. No inbound LAN access; it may reboot without notice.
- Admin over a private VPN, SSH key-only.
- Public ingress: Cloudflare Tunnel (`cloudflared` systemd service on the VM).
  `example.com → http://localhost:81`. TLS terminates at Cloudflare.

**Matrix stack** — spantaleev/matrix-docker-ansible-deploy at `/root/mdad`, `ansible_connection=local`.
Inventory host `matrix.example.com`; vars at
`/root/mdad/inventory/host_vars/matrix.example.com/vars.yml`.

- `matrix_domain: example.com`, `matrix_server_fqn_matrix: example.com`
  → user IDs `@dave.miller:example.com` (permanent).
- Synapse. Federation disabled (`matrix_homeserver_federation_enabled: false`).
- Traefik on `127.0.0.1:81`, no TLS, behind the tunnel.
- MAS enabled (syn2mas; image seen 1.26.0). Password registration on, registration token required,
  email not required, `minimum_complexity: 2`.
- Ketesa admin UI at `/ketesa/` (MAS mode); Synapse admin API exposed, intended behind Cloudflare Access.
- `matrix_client_element_enabled: false`.
- `devture_systemd_service_manager_service_restart_mode: clean-stop-start`.
- `matrix_synapse_presence_enabled: false`.
- Docker network `matrix-homeserver`. Synapse internal URL `http://matrix-synapse:8008`.
- Re-apply: `cd /root/mdad && ansible-playbook -i inventory/hosts setup.yml --tags=setup-all,start`.

**Clients** — Element X frustrated members; FluffyChat under evaluation. Bridge must not depend on a client.

**Room (planned)** — one group room, unencrypted (required), invite-only, "block users from other servers"
on, alias e.g. `#general:example.com`.

## 4. Telnyx / 10DLC (compliance-critical)

- A dedicated Telnyx account for this project.
- Sole Proprietor 10DLC brand "Example Chat", campaign approved (Customer Care / Account
  Notification).
- One campaign, one number. Sole-prop brands cannot add a second number (see §8).
- Throughput: AT&T ~15 msg/min, T-Mobile 1,000 segments/day (brand-level), ~1 MPS overall
  → outbound through a paced queue.
- Pricing: SMS $0.004/part + carrier fee (~$0.0025–0.005); MMS $0.015 out / $0.005 in + carrier fee
  (~$0.007–0.01). Photos are the main cost.
- Keywords: opt-in `JOIN,START,YES`; opt-out `STOP,UNSUBSCRIBE,CANCEL,END,QUIT`; help `HELP,INFO`.
- Registered messages (real behavior must match):
  - Opt-in: `Example Chat: You've joined the group text relay. Group messages will be sent to this number; reply to post to the group. Message frequency varies. Msg&data rates may apply. Reply HELP for help, STOP to opt out.`
  - Opt-out: `Example Chat: You are unsubscribed and will receive no further messages. Reply START to rejoin.`
  - Help: `Example Chat: Group text relay. Reply STOP to opt out, START to rejoin. Help: help@example.com`
    (the §6 HELP list extends this; keep brand name, STOP wording and the email).
- Opt-in flow: admin invites in person → member texts JOIN → confirmation. Store opt-in records
  (number, timestamp, keyword).
- Campaign declares embedded links: yes; embedded phone numbers: yes.

**Hard rules**

- Never send the first message to a number that hasn't texted JOIN (or START after opt-out).
- STOP takes effect immediately; nothing else is sent except the opt-out confirmation.
- Log opt-in/opt-out events permanently.
- Handle Telnyx's own STOP behavior; don't double-send.

## 5. Architecture

- Unencrypted Matrix appservice registered with Synapse (encrypted appservices don't work with MAS yet).
- Bot `@smsbot:example.com` (name TBD). Ghosts `@sms_<e164 digits>:example.com`,
  display name from `!sms add` / `NAME`.
- Registration wired in via the playbook's supported mechanism (no hand-editing `homeserver.yaml`).
- Docker container on the VM, on `matrix-homeserver`, systemd-managed. Config via `.env` + config file.
- Telnyx inbound: webhook via the tunnel; ed25519 signature check; idempotent by message ID; ack fast,
  process async.
- Telnyx outbound: persistent queue, per-carrier pacing, retries with backoff, rate-limit/daily-cap
  handling, delivery-status webhooks.
- Provider adapter interface (send SMS, send MMS, parse inbound, verify signature).
- State: members, opt-in log, message map, polls, DM threads / last-DM-sender, delivery failures,
  monthly usage counters.
- Per-room number config from day one.

## 6. Features — v1

**Message flow**

- Matrix → SMS: `Dave Miller: text` to every opted-in SMS member except the sender. System prefixes
  GSM-7 only; user content as-is (emoji → UCS-2, 70-char segments).
- SMS → Matrix: posted by the sender's ghost.
- Replies: `Re Dave: "Saturday?..." ` quote prefix on SMS (D7); SMS replies arrive as normal messages.
- Edits: `(edited) new text`. Redactions: ignore on SMS. Reactions: inbound only (DECISIONS D4).
- Mentions: Matrix mention of a ghost → deliver normally. SMS `@Name` → real Matrix mention via name matcher.

**Photos**

- Matrix image → download via authenticated media, resize ~1200px JPEG < ~600 KB, serve at short-lived
  random public URL, send MMS.
- Inbound MMS → download, upload to Matrix media, post `m.image` from ghost.
- Video: link, not MMS. `#photos off` → `Dave sent a photo`.

**Polls**

- Matrix poll → `Example Chat poll from Dave: Saturday or Sunday? Reply #vote 1 (Saturday), #vote 2 (Sunday).`
- `#vote n` → real poll response from the ghost; applies to latest open poll in the room.
- Poll end → results text.

**SMS commands** (revised 2026-10-17, see DECISIONS.md D10/D11)

Bare text is always a group message, except these carrier/registered keywords when they are the whole
message: STOP-family (STOP, STOPALL, STOP ALL, UNSUBSCRIBE, CANCEL, END, QUIT), START, UNSTOP, HELP, INFO, JOIN.
Member commands need a `#` prefix (`/` also works), any case.

| Command | Effect |
|---|---|
| `JOIN` | opt in (allowlist only; else "Ask the group admin to add your number.") |
| `STOP` / `START` | opt out / back in |
| `HELP`, `INFO`, `#help` | command list |
| `#vote n` | answer latest poll |
| `#dm <name> <msg>` / `#dm <n> <msg>` | private message (exact name or WHO number) |
| `#yes` | send a DM held after a non-exact name match |
| `#r <msg>` | reply to last DM sender |
| `#who` | numbered member list |
| `#name <Full Name>` | set display name |
| `#photos off` / `#photos on` | MMS toggle |
| unknown `#word` | not posted; `Unknown command, message not posted. Text it without the # to post it.` |
| anything else | posted to the group |

HELP reply:

```
Example Chat commands:
#vote 1 - answer latest poll
#dm name msg - private message
#r msg - reply to last DM
#who - list members
#name Dave Miller - set your name
#photos off / #photos on
STOP - leave, START - rejoin
Questions: help@example.com
```

**Name matching** — auto-send only on an exact full name, unique first name, username or WHO number;
otherwise suggest (typo-tolerant) and wait for `#yes`. Details in DECISIONS.md D11.

**DMs** — unencrypted DM rooms between ghost and target. Matrix → ghost DM delivered as `DM from Name: text`;
reply with `#r`.

**Admin** (power-level checked): `!sms add|remove|list|status`, `!announce <text>` (formatted, @room,
pinned, texted as `ANNOUNCEMENT from Evan: …`, optional `| yes | no` RSVP poll), `!help`. Alerts on
repeated failures / Telnyx unreachable.

**Nice-to-have**: auto-invite MAS-registered users; read-only Announcements room in a Space.

## 7. Non-goals

No E2EE in bridged rooms/DMs. No federation. No mute/digest/quiet hours. No custom app. No Element Web.

## 8. Multi-group (design for, don't build)

Room ↔ number (ideally member+room ↔ number) as configuration. Options later: one number + routing,
another sole-prop brand, or EIN → low-volume standard brand → multiple numbers.

## 9. Security

Secrets only in `.env`. Repo private until secrets handling is proven. Verify webhook signatures; reject
unknown numbers; power-level checks; sanitize inbound text; short-lived unguessable media URLs;
least-privilege namespaces.

## 10. Testing & rollout

Unit tests (command parsing, name matcher, segment counting, pacing queue); signed webhook fixtures against
a local instance; live test on the operator's own phone; one friendly member; then everyone. Watch costs.

## 12. Ops

- Updates: `cd /root/mdad && just update && ansible-playbook -i inventory/hosts setup.yml --tags=setup-all,start`.
- Backups: nightly Postgres dump + `/matrix` → backup share + off-site; include the bridge DB.
- Host reboots: bridge must recover cleanly (persisted queue, no duplicate sends).
