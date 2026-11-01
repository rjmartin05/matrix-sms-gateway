# App login service (D13)

A small HTTP service that lets a custom Matrix app sign people up and in with a code, entirely on the
app's own screens: no browser page, no password, no homeserver to type. It runs as its own process and
container (`python -m smsgw.login`), from the same image as the bridge.

- **Sign up:** name + a one-time **invite link** (or the shared join code, a MAS registration token, if
  still allowed) + email or phone, then verify.
- **Sign in:** email or phone, then verify.
- **Switch:** someone signed in with a password (in any client) can switch their account to codes.
  Their password stops working; codes are the way in from then on.
- **Switch back:** someone on codes can go to a password from the app, confirmed with a code. Codes stop
  working for them until they switch again. It's one or the other, per person, because every code login
  replaces the password.
- **Phone members move off SMS:** when someone verifies a phone number the bridge knows, the bridge
  stops texting it and their SMS ghost leaves the room. Texting START or JOIN turns texts back on.

Accounts that never switch keep their passwords and are untouched (for example, people on stock
FluffyChat on iPhone).

## How verification works

- **Email:** the service emails a 6-digit code; the person types it into the app.
- **Phone (inbound):** the app shows a code and a button that opens the phone's messaging app with
  `Sign-in code NNNNNN` addressed to the group number. The person taps Send. The bridge receives the
  text, forwards it to this service, and doesn't post it or reply. **No code is ever sent by SMS**, so
  this needs no change to the 10DLC campaign. It proves the person can send from that number.

Codes are 6 digits, valid 10 minutes, 5 wrong tries, hashed at rest (HMAC with `SMSLOGIN_CODE_SECRET`).
Requests are rate-limited per email/phone (5/hour) and per client IP (20/hour).

## How a verified person gets a session

MAS logs people in through a browser page by design, so the service doesn't ask MAS for a login. For
an account it manages, it sets a fresh random password through the MAS admin API, performs an ordinary
`m.login.password` login itself (MAS's compatibility layer), and returns only the resulting tokens.
Nobody ever sees or knows the password, and it changes at every code login. That's why code login is
only for accounts marked code-managed (created here, switched, or linked by the admin).

MAS personal sessions (admin API) would be simpler, but sending messages with them fails on Synapse
at the time of writing (element-hq/synapse#19606).

## App API

Base URL: the service's public hostname. JSON in and out. Errors: HTTP status +
`{"error": "<code>", "message": "<text to show the person>"}`.

| Request | Body | Result |
|---|---|---|
| `POST /v1/signup` | `display_name`, `invite` (or `join_token`), and `email` or `phone` | a verification |
| `POST /v1/login` | `email` or `phone` | a verification |
| `POST /v1/switch` | header `Authorization: Bearer <the app's current access token>`; `email` or `phone` | a verification |
| `POST /v1/verify` | `verification_id`, `code` (email codes) | a session |
| `GET /v1/verify/{verification_id}` | (phone codes: poll every 2–3 s) | `{"status": "pending"}` or a session |
| `POST /v1/account/use-password/start` | header as above | a verification (the account's phone, else its email) |
| `POST /v1/account/use-password` | header as above; `verification_id`, `new` | `{"ok": true}` |

A verification: `{"verification_id", "channel": "email"|"sms", "expires_in"}`, plus for phone codes
`"sms_to"` (E.164) and `"sms_body"`. Open `sms:<sms_to>?body=<sms_body, URL-encoded>`.

Verifying a `use-password` code gives `{"status": "verified"}` instead of a session.

A session: `{"status": "ok", "homeserver", "user_id", "device_id", "access_token", "refresh_token",
"expires_in_ms"}`. Give these to the Matrix SDK as an existing login; refresh as usual.

Adding people by text from the app (`/v1/sms-members`): see [SMS_MEMBERS.md](SMS_MEMBERS.md).

Error codes worth handling: `bad_invite`, `bad_join_token`, `already_registered`, `no_account`, `wrong_code`,
`too_many_attempts`, `expired`, `already_used`, `rate_limited`, `try_again` (retry the same request),
`email_failed`, `uses_password`, `no_contact`, `not_confirmed`, `weak_password`.

## Invite links

An admin sends `!invite Sue` to the bridge bot in a private chat (or the admin room). The bot replies with
a one-time link, `https://<this service>/join/<code>`. In the group room it refuses, since anyone who sees
a link could use it. Without Matrix: `docker exec matrix-sms-login smsgw-login -c /data/login.yaml
create-invite --note Sue`.

- **One use, 7 days** (`invites.ttl_days`). The code has 128 random bits and only its hash is stored.
  It's checked when sign-up starts and spent only when the account is created, so a mistyped code or
  an abandoned verification doesn't burn it. Two people racing on one link: the first to finish wins.
- **Opens in the app.** The app declares a verified Android App Link for `https://<this host>/join/`, and
  this service publishes `/.well-known/assetlinks.json`. With the app installed, Android opens the
  sign-up screen with the code filled in; no browser. Without the app, the link lands on a tiny page that
  jumps straight to Google Play. It passes `referrer=invite=<code>` to Play, and the app picks that up
  on first open. The page doesn't check the code.
- **Turning off the shared code:** once everyone joins by invite, set `invites.allow_join_token: false`.
  Sign-up then needs an invite. Accounts and password sign-up on MAS's own page are unaffected; MAS's
  registration token still gates that.

Setup (`login.yaml`, then restart `matrix-sms-login`):

```yaml
app:
  public_url: https://login.example.com          # this service's public URL (PLACEHOLDER)
  android_package: com.example.groupchat         # the app's applicationId (PLACEHOLDER)
  android_cert_sha256:                           # PLACEHOLDER, see below
    - "AA:BB:...:FF"
```

`android_cert_sha256` is the **app signing key** certificate's SHA-256 from Play Console → your app →
Test and release → App integrity → App signing. That's the key Google signs Play installs with, not your
upload key. Add your upload key's SHA-256 too if you also install builds from CI artifacts directly.
Check `https://<this host>/.well-known/assetlinks.json` loads with no redirect or challenge; Cloudflare
bot protection on this host would break Android's verification.

## Deploy

All commands run on the VM as root. **PLACEHOLDER** values are marked.

### 1. A MAS admin client for the service

Generate a client ID (a ULID) and a secret:

```sh
python3 -c 'import os,time;A="0123456789ABCDEFGHJKMNPQRSTVWXYZ";n=(int(time.time()*1000)<<80)|int.from_bytes(os.urandom(10),"big");print("".join(A[(n>>(5*i))&31] for i in reversed(range(26))))'
openssl rand -hex 32
```

Add to the playbook's `vars.yml` (both IDs are the ULID above), then re-apply the playbook:

```yaml
matrix_authentication_service_config_clients_custom:
  - client_id: PLACEHOLDER_ULID
    client_auth_method: client_secret_basic
    client_secret: PLACEHOLDER_SECRET

matrix_authentication_service_configuration_extension_yaml: |
  policy:
    data:
      admin_clients:
        - PLACEHOLDER_ULID
```

If `vars.yml` already sets either variable, merge into it instead of adding a second copy.

### 2. Data, secrets, config

```sh
install -d -m 700 -o 10001 /matrix/sms-login/data
cp /root/matrix-sms-gateway/login.example.yaml /matrix/sms-login/data/login.yaml   # then edit it
INTERNAL=$(openssl rand -hex 32)
cat > /matrix/sms-login/.env <<EOF
SMSLOGIN_MAS_CLIENT_SECRET=PLACEHOLDER_SECRET
SMSLOGIN_CODE_SECRET=$(openssl rand -hex 32)
SMSLOGIN_SMTP_PASSWORD=
SMSGW_INTERNAL_SECRET=$INTERNAL
EOF
chmod 600 /matrix/sms-login/.env
echo "SMSGW_INTERNAL_SECRET=$INTERNAL" >> /matrix/sms-gateway/.env
```

In the bridge's `config.yaml`, enable the link (see `config.example.yaml`):

```yaml
login:
  service_url: http://matrix-sms-login:29343
```

Email needs a real SMTP service: mail sent directly from a home connection is usually blocked or
filtered as spam. Leave `email.smtp_host` empty to offer phone codes only.

### 3. Run it

```sh
cp /root/matrix-sms-gateway/deploy/matrix-sms-login.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now matrix-sms-login && systemctl restart matrix-sms-gateway
journalctl -u matrix-sms-login -n 20 --no-pager
```

### 4. Public hostname

Cloudflare dashboard → the tunnel → Public Hostname → add `login.example.com` (**PLACEHOLDER**) →
`http://localhost:29342`. No Cloudflare Access: the app calls it before anyone is signed in. Then:

```sh
curl -s https://login.example.com/healthz   # PLACEHOLDER hostname; expect: ok
```

### Admin: make an existing account sign in with codes

```sh
docker exec matrix-sms-login smsgw-login -c /data/login.yaml link-account @dave.miller:example.com --email dave@example.net
```

The password keeps working until their first code login.

## Known limits

- MAS doesn't count sign-ups made here against the registration token's usage limit (its admin API
  can't increment it). Revoke or rotate the join token in MAS when you want to stop sign-ups.
- An SMS sender ID can in principle be spoofed. The join code or invite still gates sign-up, but phone
  sign-in to an existing account relies on the sender ID alone.
- Every code login reaches MAS from the service's address, so MAS's per-IP login rate limit applies
  to all of them together. Fine for a small group; raise `rate_limiting.login.per_ip` in MAS if a
  burst of sign-ups hits `try_again`.
- Sessions issued this way are normal MAS compatibility sessions. To sign someone out everywhere:
  `mas-cli manage kill-sessions <username>`.
