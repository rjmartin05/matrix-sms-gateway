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
- **D4 Reactions:** inbound only.
  - SMS -> Matrix: a phone's reaction text (Google Messages `👍 to “Name: text”`, iPhone `Loved “…”`) whose
    quote matches a group text sent to that number, or one of their own posted texts, in the last 7 days becomes an `m.reaction` from the
    ghost on the original event (the original, not the edit, if the quoted text was an edit). Removal texts
    redact it. No match = posted as a normal message. Google format verified from a live webhook; iPhone and
    removal formats from memory (see `smsgw/reactions.py`).
  - Matrix -> SMS: not relayed. It would cost one text per SMS member per reaction.
- **D5 Opt-in/out:**
  - Telnyx handles STOP/START blocking and replies (autoresp_configs with registered wording,
    `scripts/telnyx_setup.py`). HELP autoresp only if live test shows a double reply.
  - YES is not a Telnyx keyword, and bare "yes" is a normal group message.
  - Bridge owns JOIN (allowlist-checked) and the full HELP list, and logs every consent event,
    including Telnyx autoresponse webhooks.
  - START re-subscribes only numbers that previously JOINed and are still allowlisted.
  - AI opt-out detection is off.
  - The opt-out reminder goes out as a separate message right after the unmodified registered opt-in text.
- **D6 Repo:** `matrix-sms-gateway`, private.

## Decided (2026-10-17, v1 review)

- **D7 Reply quotes:** `Re Dave: "Saturday?..." ` (GSM-7 only). Default `sms.reply_quote_format`.
- **D8 Spend:** Telnyx daily spend limit **$15**, set on the messaging profile by `scripts/telnyx_setup.py`.
  The bridge posts one alert per UTC day to `matrix.alerts_room` when *estimated* spend reaches ~60%
  (`pricing.daily_spend_alert_fraction`). If Telnyx refuses a send for the spend limit (error 40333),
  the queue holds messages (they still expire at 12 h) and alerts.
- **D9 Encoding:** Telnyx `smart_encoding` stays **off**; the setup script sets it false explicitly.
  The bridge maps typographic characters to GSM-7 before counting segments and sending
  (`segments.normalize_typography`):
  - curly quotes → `'` `"`
  - dashes → `-`
  - `…` → `...`
  - bullets, odd spaces, zero-width characters

  Emoji and real non-Latin text are untouched. Outbound only; what members post into Matrix is not altered.
- **D10 Command syntax:**
  - **Member commands need a `#` prefix (`/` also works), any case:** `#vote n`, `#dm`, `#r`, `#who`,
    `#name`, `#photos on|off`, `#help`, `#yes`.
  - **Bare text is always a group message**, except these, when they are the whole message:
    - STOP family: STOP, STOPALL, STOP ALL, UNSUBSCRIBE, CANCEL, END, QUIT
    - START, UNSTOP, HELP, INFO, JOIN (INFO added by D12)
  - **An unknown `#word` is not posted.** The reply is "Unknown command, message not posted. Text it
    without the # to post it." (GSM-7, 1 segment; changed 2026-10-18).
  - **A prefixed command with bad arguments** gets a usage line and is not posted. Examples: `#vote two`,
    `#dm dave` with no message, `#photos maybe`, `#who is here`.
  - **`#` followed by a non-letter is a normal message** ("#1 fan", "# of people").
  - **System texts are GSM-7 basic with no brackets.** A test enforces this for every fixed system text.
- **D11 DM name resolution** (`names.resolve_dm`):
  - **Auto-send only on an exact match:**
    - full display name
    - a first name only one member has
    - username
    - WHO number

    Case-insensitive; the longest exact match wins the name/message split.
  - **Several exact matches** →
    `Which Dave? 4) Dave Miller 9) Dave Smith. Reply #dm 4 or #dm 9 with your message`. Nothing sent.
  - **Otherwise, suggestions only, never auto-sent.** That includes a unique partial name like "dav".
    - What counts as plausible: a prefix match, or Damerau-Levenshtein ≤1 for names up to 4 letters
      and ≤2 for longer names. Compared against first, last and full name and username.
    - **One suggestion** → `No exact match for "X". Did you mean 4) Dave Miller? Reply #yes to send`.
      The DM is held for 10 minutes (persisted, survives restarts).
    - **2–3 tied suggestions** → listed with `Reply #dm 4 or #dm 9 with your message`; nothing is held.
  - **`#yes`** sends the held DM. A new `#dm` replaces or clears it. With nothing held, `#yes` replies
    "Nothing to send." HELP shows the `#yes` line only while a DM is held.
  - **No plausible match** → `No member named "X". Text #who for the list`.
  - **@mentions use exact matching only.** Anything ambiguous or inexact stays plain text, with no prompt.
  - `!dm Name` (Matrix side) also requires an exact match.

## Implementation choices (approved 2026-10-17)

- Strict command shapes.
- DM numbers are WHO numbers everywhere.
- Allowlisted-but-not-joined and unknown numbers get responses only to JOIN/HELP/STOP/START.
- Ghosts leave the room on STOP/remove and rejoin on START/JOIN.
- Encrypted DMs to a ghost are refused with a notice; `!dm Name` opens an unencrypted one.
- Only group-room members can DM SMS members.
- Poll and announcement texts use the sender's first name; group messages use the full name.
- Matrix → SMS bodies are capped at 800 chars, ending "(more in the app)".
- Inbox failures retry in order (8 attempts, then park + alert).
- Queued messages expire after 12 h; a daily carrier cap holds and alerts; unknown carriers get the
  strictest per-minute limit.

## Implementation choices this round (low-stakes; say if you want any changed)

- **"#r" with no prior DM** replies "Nobody has sent you a DM yet. Text #dm name msg to start one."
  It no longer falls back to posting in the group, since the prefix makes intent explicit.
- **`#yes` help line text:** "#yes - send your pending DM", inserted right after the `#dm` line.
  HELP stays at 2 segments.
- **Post-JOIN follow-up:**
  "Messaging Chat: To leave, text one word: STOP, CANCEL, END or QUIT. Text START to rejoin. Commands start
  with #. Text HELP for the list." 136 chars, 1 segment.
- **STOPALL / STOP ALL stay opt-out keywords** (not in your list). Telnyx blocks on them regardless, so
  posting them to the group would contradict the block.
- **Spend estimate counts inbound too** and uses the configured rates. Telnyx's own accounting may
  differ. Which charges count toward Telnyx's limit, and when its day resets, are unverified;
  the bridge uses the UTC day.

## Decided (2026-10-18)

- **D12 INFO:** bare INFO (whole message) gets the same reply as HELP: the command list for members, the
  registered help text for everyone else. If live testing shows Telnyx auto-replying to HELP,
  `telnyx_setup.py --help-autoresp` now covers HELP and INFO together.
- **Unknown `#word` reply** changed (see D10).
- **Approved:** STOPALL/STOP ALL as opt-outs; `#r` with no DM; sender excluded from name ties; em-dash
  reading of the fuzzy-reply spec; typo thresholds; the new HELP / follow-up texts.
- **Deploy from `main`.** Alerts go to a private, unencrypted "Gateway Admin" room (DEPLOY.md §7a).

## Open

None.
