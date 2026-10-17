# Verification log (brief §11)

Checked 2026-10-14. "Verified" = read in current source/docs (link given). "Inference" = my reasoning,
not confirmed.

## Playbook: registering a custom appservice — VERIFIED

Source: `roles/custom/matrix-synapse/defaults/main.yml` and
`templates/synapse/homeserver.yaml.j2` in spantaleev/matrix-docker-ansible-deploy (HEAD b948855).

- `matrix_synapse_app_service_config_files: []` — user-overridable list of **in-container** paths.
  Merged with the playbook-managed `_auto` list into `_final`, rendered as `app_service_config_files:`.
- `matrix_synapse_container_additional_volumes: []` — list of `{"src": "/outside", "dst": "/inside", "options": "ro"}`,
  mounted via `--mount` in the Synapse systemd unit (main and worker units).

So the vars.yml addition will look like (exact paths decided at deploy time):

```yaml
matrix_synapse_container_additional_volumes:
  - {"src": "/matrix/sms-gateway/registration.yaml", "dst": "/sms-gateway-registration.yaml", "options": "ro"}
matrix_synapse_app_service_config_files:
  - /sms-gateway-registration.yaml
```

## MAS + appservices — VERIFIED (docs), runtime UNTESTED

`docs/configuring-playbook-matrix-authentication-service.md`: encrypted appservices don't work with MAS yet
(MSC4190 / Synapse PR 17705); unencrypted ones are fine. Our bridge is unencrypted by design.

## mautrix-python maintenance — VERIFIED

github.com/mautrix/python: latest tag v0.21.1 (2026-07-05); commits 2026-10-12. Maintained, low cadence.
The high-level `mautrix.bridge` framework is legacy (mautrix's Python bridges moved to Go/bridgev2 —
inference from project history, not re-checked today); the `mautrix.appservice` / client API layer is the
part we'd use.

## mautrix-go bridgev2 — VERIFIED active

github.com/mautrix/go: multiple bridgev2 commits on 2026-10-14.

## Telnyx STOP / START / HELP — VERIFIED (docs), behavior to confirm on live number

Sources: developers.telnyx.com/docs/messaging/messages/advanced-opt-in-out,
support.telnyx.com/en/articles/1270091.

- **Telnyx enforces opt-out itself by default.** `STOP, STOPALL, STOP ALL, UNSUBSCRIBE, CANCEL, END, QUIT`
  create a block rule; `START, UNSTOP` remove it. **JOIN does not remove a block.**
- Block rules are **per messaging profile**, not per number (matters for §8 multi-group).
- By default Telnyx sends a **generic** auto-response ("You have successfully been unsubscribed…"),
  which does **not** match our registered opt-out text. Fix: configure
  `POST /v2/messaging_profiles/{id}/autoresp_configs` for `stop` / `start` / `help` with the registered
  wording (min 20 chars). START/STOP/HELP are reserved and stay active.
- Inbound webhook still arrives, with `payload.autoresponse_type` (e.g. `"STOP"`) when Telnyx handled it.
  → Bridge records the event and **sends nothing** for those (no double-send).
- Sending to a blocked number returns error code **40300** "Blocked due to STOP message".
- **AI opt-out detection** exists, off unless enabled (`features.ai_opt_out_detection_enabled`). Recommend
  leaving it **off**: in a group chat "stop, I can't make it" could get someone blocked. (Inference.)

Design consequences (inference, to confirm with Ryan's phone during rollout):

- A non-allowlisted number texting START will get Telnyx's opt-in auto-reply. Registered opt-in text says
  "You've joined…" — misleading for a stranger. Options: keep START's autoresp generic-but-compliant, or
  let the bridge own START and configure no custom start autoresp (needs testing whether Telnyx's default
  START reply can be suppressed).
- JOIN should be bridge-owned (allowlist gate), not a Telnyx autoresp keyword.
- HELP: if Telnyx autoresponds to HELP *and* the bridge sends the command list, that's a double-send.
  Unverified whether Telnyx sends a default HELP reply with no config. Test, then pick one owner.

## Telnyx webhook signatures — VERIFIED

Source: developers.telnyx.com/docs/development/api-fundamentals/webhooks/receiving-webhooks.
Headers `telnyx-signature-ed25519` (base64) and `telnyx-timestamp` (unix). Signed string is
`f"{timestamp}|{raw_body}"`; verify against the account public key (Mission Control → API keys → public key).
Verify on raw bytes, enforce a replay window. Envelope: `data.event_type`, `data.id`, `data.payload`,
`meta.attempt`.

## Still unverified

- `auto_join_rooms` for MAS-registered users.
- MMS size limits (Telnyx/carrier) and exact rate-limit / daily-cap error codes.
- Element X / FluffyChat poll rendering (MSC3381 stable vs unstable event types).
- Authenticated-media download path for appservices (`/_matrix/client/v1/media/download/...` with as_token
  is the expected path — inference until tested against this Synapse).

## Round 2 (2026-10-14)

### Telnyx keyword matching — VERIFIED (docs)

developers.telnyx.com/docs/messaging/messages/configuration-and-usage: a keyword triggers only when the
message contains **only** that word/phrase, **case-insensitive, surrounding whitespace ignored**. So
"stop" / " STOP " trigger, "please stop" / "stop it" don't. **Unverified:** whether trailing punctuation
("Stop!") counts. The bridge mirrors the documented rule exactly, so the two can't disagree on documented
input. Watch the consent log for STOP-like messages without `autoresponse_type` during rollout.

- YES is not in Telnyx's default keyword sets (defaults: STOP/STOPALL/STOP ALL/UNSUBSCRIBE/CANCEL/END/QUIT;
  START/UNSTOP). It only becomes one if added to an autoresp config, which we don't do.
- JOIN is not a Telnyx keyword either: it never lifts a block.
- Discrepancy: one Telnyx page says sending to a blocked number gets HTTP **409**, the error-code
  reference says code **40300**. The adapter treats either as "blocked".

### Messaging profile API — VERIFIED (docs)

`PATCH /v2/messaging_profiles/{id}` accepts `webhook_url`, `webhook_api_version: "2"`,
`features.ai_opt_out_detection_enabled`, `daily_spend_limit(_enabled)`, `smart_encoding`.
The docs' example response shows `ai_opt_out_detection_enabled: true`, so the setup script sets it to
false explicitly rather than assuming the default.

### bridgev2 `bridge` command — VERIFIED (source)

mautrix/go `bridgev2/commands/managechat.go` (commit e7cf128, 2026-03-22; first tag v0.27.0, 2026-04-16).
Plumbs an existing room to one remote chat ID, sets a default relay. See DECISIONS.md D1 for why it
doesn't change the framework choice.

### Synapse default encryption — VERIFIED (playbook source)

`matrix_synapse_encryption_enabled_by_default_for_room_type: "off"` by default. Ghost-created DM rooms
rely on this; DEPLOY.md step 0 checks it isn't overridden.

### Still unverified (needs live traffic)

- Whether Telnyx auto-replies to HELP with no HELP autoresp configured (test in DEPLOY.md §9).
- `from.carrier` on inbound webhooks and `to[].carrier` on send responses (used for pacing classes;
  falls back to strict "unknown" limits if absent).
- Whether inbound MMS media URLs need auth (adapter tries without, then with the API key).
- Poll event compatibility in FluffyChat (bot-created polls use unstable MSC3381 types by default;
  the bridge reads both stable and unstable).
- Docker image build: not built here (no Docker daemon in the build sandbox); `pip install .` in a clean
  venv was verified.

## Round 3 (2026-10-17)

- **GitHub SSH host key — VERIFIED** (docs.github.com, "GitHub's SSH key fingerprints"): Ed25519
  `SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`; the `known_hosts` line in DEPLOY.md §1c is copied
  from that page.
- **Telnyx spend limit error — VERIFIED (docs excerpt):** the messaging error-code reference lists
  `40333` "Spend limit reached". The adapter maps it to a hold, not a retry storm.
- **Telnyx daily spend limit semantics — UNVERIFIED:** when the day resets (the bridge assumes UTC) and
  whether inbound and carrier fees count toward it. The bridge's 60% alert is an estimate from configured
  rates.
- **Cloudflare dashboard field labels — INFERENCE:** "Published application routes" is your term; the
  field names in DEPLOY.md §6 are from memory.
- **Damerau-Levenshtein thresholds** (≤1 for ≤4-letter names, ≤2 otherwise) are my choice, tuned by the
  tests, not taken from any source.
