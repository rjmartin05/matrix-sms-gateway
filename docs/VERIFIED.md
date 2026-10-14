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

Design consequences (inference, to confirm with the operator's own phone during rollout):

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
