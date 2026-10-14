# Decisions

## Decided (2026-10-14)

- **D1 Language:** Python on mautrix-python's appservice layer (`mautrix.appservice`), not bridgev2.
  The bridgev2 `bridge` command (added 2026-03-22, shipped in mautrix-go v0.27.0 on 2026-04-16) does
  plumb a remote chat into an *existing* room with relay. It doesn't change the call: a Matrix
  user who invites a ghost to a DM is rejected unless that user has their own bridge login
  (`bridgev2/matrixinvite.go`, `handleGhostDMInvite`: "You're not logged in"). Our members don't log in
  to anything. Fan-out, the SMS command set, and compliance would still be custom code.
- **D2 State:** SQLite (WAL), `scripts/backup.sh` snapshot in the nightly job.
- **D3 Ingress:** `sms-hook.ryanjmartin.net` → bridge directly (`127.0.0.1:29341`); MMS media served there.
  Not behind Cloudflare Access; webhook signature verification is the protection.
- **D4 Reactions:** skipped in v1.
- **D5 Opt-in/out:**
  - Telnyx handles STOP/START blocking and replies (autoresp_configs with registered wording,
    `scripts/telnyx_setup.py`). HELP autoresp only if live test shows a double reply.
  - YES is not a Telnyx keyword and not a bridge command.
  - Bridge owns JOIN (allowlist-checked) and the full HELP list, and logs every consent event,
    including Telnyx autoresponse webhooks.
  - START re-subscribes only numbers that previously JOINed and are still allowlisted.
  - AI opt-out detection is off.
  - The opt-out reminder ("text one word: STOP, CANCEL, END or QUIT…") goes out as a separate
    message right after the unmodified registered opt-in text.
- **D6 Repo:** `matrix-sms-gateway`, private.

## Implementation choices made without asking (low-stakes; say if you want any changed)

- **Command shapes are strict.** Text that starts like a command but doesn't fit is a normal group message:
  "Who is driving?" isn't WHO; "vote for pizza" isn't VOTE.
  Known ambiguities that remain:
  - `NAME <2–4 words>` is always NAME ("name the place" renames you; the reply says so).
  - `R <text>` goes to your last DM sender once someone has DMed you ("r u coming" included).
    Before that, it posts to the group.
- **DM ambiguity numbers are the WHO numbers**, not 1..n, so `DM 4 msg` means the same thing everywhere
  (brief's example showed 1/2).
- **Allowlisted-but-not-joined and unknown numbers:** only JOIN/HELP/STOP/START get a response; anything
  else is ignored and logged (most conservative compliance-wise).
- **Ghosts leave the room on STOP/remove and rejoin on START/JOIN**, so Matrix members can see who's reachable.
- **Encrypted DMs to a ghost** (FluffyChat/Element X may default to E2EE) are refused with a notice.
  Workaround command `!dm Name` makes the ghost open an unencrypted DM with you.
- **Poll/announcement SMS use the sender's first name** ("poll from Dave"), group messages use the full
  display name ("Dave Miller: …"), matching the brief's examples.
- **Matrix → SMS bodies are capped at 800 chars** (~6 segments) with "(more in the app)".
- **Only members of the group room** can DM SMS members (by inviting a ghost or with `!dm`).
- **Inbox failures retry in order with backoff** (e.g. while Synapse restarts during a playbook run); after
  8 attempts the event is parked and an alert is raised.
- **Queue:** messages older than 12 h expire instead of arriving late. A daily cap (T-Mobile) holds
  messages and alerts. Carrier is learned from Telnyx; unknown carriers get the strictest
  (AT&T-like) per-minute limit.

## Open (need you)

- **D7 Reply quote encoding.** The brief's `↩ Dave: Saturday?… ` contains two non-GSM-7 characters
  (`↩`, `…`), which forces every reply onto UCS-2: 70-char segments, roughly 2× cost for replies.
  - Option: keep it as specified (current default; configurable in `sms.reply_quote_format`).
  - Option: a GSM-7 form like `Re Dave: "Saturday?..." ` (stays 160-char).
  - Lean: GSM-7 form.
- **D8 Telnyx daily spend limit** on the messaging profile. Lean: yes, ~$5/day, as a runaway guard.
  `telnyx_setup.py --daily-spend-limit 5.00`.
- **D9 Telnyx `smart_encoding`** (profile field, verified to exist; that it swaps look-alike Unicode for GSM-7 is my inference, not verified). It would
  partly fix D7, but it rewrites user content. Lean: leave off, decide D7 directly.
