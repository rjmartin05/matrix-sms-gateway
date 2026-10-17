# Deploy on the `matrix` VM

Run everything as root on the VM (`ssh root@matrix`). Commands are copy-paste ready except where marked
**PLACEHOLDER**. Nothing here texts anyone; the first SMS goes out only after a number texts JOIN.

## 0. Preconditions (check, don't skip)

- The group room exists, is **unencrypted**, invite-only. Note its alias (e.g. `#general:chat.ryanjmartin.net`).
- Synapse isn't forcing encryption on new rooms (ghost-created DM rooms must stay unencrypted).
  The playbook default is `"off"` (verified in source). This should print nothing:

  ```sh
  grep -n encryption_enabled_by_default /root/mdad/inventory/host_vars/matrix.chat.ryanjmartin.net/vars.yml
  ```

## 1. Repo access (read-only deploy key) and build

### 1a. Generate the key on the VM

```sh
install -d -m 700 /root/.ssh
ssh-keygen -t ed25519 -N "" -C "matrix VM read-only deploy key" -f /root/.ssh/matrix-sms-gateway_deploy
cat /root/.ssh/matrix-sms-gateway_deploy.pub
```

### 1b. Add it to the repo (browser)

1. Open https://github.com/rjmartin05/matrix-sms-gateway/settings/keys and click **Add deploy key**.
2. Title: `matrix VM (read-only)`.
3. Key: paste the single line printed by the `cat` above (starts with `ssh-ed25519`).
4. Leave **Allow write access unchecked**, then click **Add key**.

### 1c. Pin GitHub's host key and add an SSH alias

The `known_hosts` line is GitHub's published Ed25519 key. Verified against
docs.github.com, "GitHub's SSH key fingerprints", 2026-10-17; fingerprint
`SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`. Pinning it avoids trust-on-first-use.

```sh
cat >> /root/.ssh/known_hosts <<'EOF'
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
EOF
cat >> /root/.ssh/config <<'EOF'

Host github-matrix-sms-gateway
    HostName github.com
    User git
    IdentityFile /root/.ssh/matrix-sms-gateway_deploy
    IdentitiesOnly yes
EOF
chmod 600 /root/.ssh/config /root/.ssh/known_hosts
ssh -T github-matrix-sms-gateway
```

The last command should greet you and say GitHub does not provide shell access (exit code 1 is normal).
I expect the greeting to name the repo (`Hi rjmartin05/matrix-sms-gateway!`) because it's a deploy key.
That's inference; what matters is "successfully authenticated".

### 1d. Clone and build

The code currently lives on the work branch, not `main`. **PLACEHOLDER (pick one):** use the branch
below as-is, or merge it to `main` first and drop the `checkout` line.

```sh
git clone github-matrix-sms-gateway:rjmartin05/matrix-sms-gateway.git /root/matrix-sms-gateway
git -C /root/matrix-sms-gateway checkout claude/confident-planck-qdkdzn
cd /root/matrix-sms-gateway && docker build -t matrix-sms-gateway:latest .
```

## 2. Directories, registration, secrets

```sh
mkdir -p /matrix/sms-gateway/data
chown 10001:10001 /matrix/sms-gateway/data
python3 /root/matrix-sms-gateway/scripts/gen_registration.py --out /matrix/sms-gateway
chown matrix:matrix /matrix/sms-gateway/registration.yaml
chmod 0640 /matrix/sms-gateway/registration.yaml
chmod 0600 /matrix/sms-gateway/.env
```

`gen_registration.py` wrote the two appservice tokens into `.env`. Add the Telnyx values
(**PLACEHOLDERS**: all three come from the *personal* Telnyx account's Mission Control portal):

```sh
cat >> /matrix/sms-gateway/.env <<'EOF'
TELNYX_API_KEY=PLACEHOLDER_API_KEY
TELNYX_PUBLIC_KEY=PLACEHOLDER_BASE64_PUBLIC_KEY
TELNYX_MESSAGING_PROFILE_ID=PLACEHOLDER_PROFILE_ID
EOF
nano /matrix/sms-gateway/.env    # replace the three PLACEHOLDER values
```

## 3. Config

```sh
cp /root/matrix-sms-gateway/config.example.yaml /matrix/sms-gateway/data/config.yaml
chown 10001:10001 /matrix/sms-gateway/data/config.yaml
nano /matrix/sms-gateway/data/config.yaml
```

Edit these:

- `rooms[0].number`: **PLACEHOLDER** `+1717XXXXXXX`. The bridge refuses to start while it contains `X`.
- `rooms[0].room`, if the alias differs.
- `matrix.alerts_room`: the Matrix admin room for alerts. **PLACEHOLDER**: its alias or room ID.
  Without it, the 60% spend alert (D8) only goes to the log.

`pricing.daily_spend_limit: 15.00` must match the Telnyx limit set in step 8.

## 4. Register the appservice with Synapse (playbook-supported way)

First check these variables aren't already set (a duplicate YAML key would silently override):

```sh
grep -nE 'matrix_synapse_container_additional_volumes|matrix_synapse_app_service_config_files' \
  /root/mdad/inventory/host_vars/matrix.chat.ryanjmartin.net/vars.yml
```

If that printed nothing:

```sh
cat >> /root/mdad/inventory/host_vars/matrix.chat.ryanjmartin.net/vars.yml <<'EOF'

# SMS gateway appservice (github.com/rjmartin05/matrix-sms-gateway)
matrix_synapse_container_additional_volumes:
  - {"src": "/matrix/sms-gateway/registration.yaml", "dst": "/sms-gateway-registration.yaml", "options": "ro"}
matrix_synapse_app_service_config_files:
  - /sms-gateway-registration.yaml
EOF
cd /root/mdad && ansible-playbook -i inventory/hosts setup.yml --tags=setup-all,start
```

If it printed something, merge the entries into the existing lists by hand instead.

## 5. Run the bridge

```sh
cp /root/matrix-sms-gateway/deploy/matrix-sms-gateway.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now matrix-sms-gateway
journalctl -fu matrix-sms-gateway
```

Expect `bot can't join #general... invite @smsbot:chat.ryanjmartin.net` until step 7.

## 6. Public hostname (Cloudflare Tunnel, dashboard-managed)

Your tunnel was installed with `cloudflared service install <token>`, so routes are managed in the
dashboard. No change on the VM.

In the Cloudflare Zero Trust dashboard: Networks → Tunnels → your tunnel → **Published application routes**
→ add a route:

| Field | Value |
|---|---|
| Subdomain | `sms-hook` |
| Domain | `ryanjmartin.net` |
| Path | empty |
| Service type | `HTTP` |
| URL | `localhost:29341` |

The field labels are from memory of the current dashboard (inference). The values are what matter.

Do **not** put a Cloudflare Access application on this hostname. Telnyx can't authenticate to Access;
webhook signatures are the protection (D3). Check that no Access app covers it by wildcard: Access →
Applications. An app on `*.ryanjmartin.net` would catch it, while one scoped to `chat.ryanjmartin.net/ketesa`
won't.

```sh
curl -s https://sms-hook.ryanjmartin.net/healthz          # -> ok
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://sms-hook.ryanjmartin.net/telnyx/webhook   # -> 401
```

A 302 or a Cloudflare login page instead of `ok`/`401` means an Access policy is in the way.

~~Locally-managed tunnel (`config.yml` ingress)~~: **N/A** for this host.

## 7. Matrix room

In your Matrix client, in the group room:

1. Invite `@smsbot:chat.ryanjmartin.net` (it auto-joins).
2. Give it power level 50 (moderator): it needs that to invite ghosts, `@room` announcements, and pin.
3. Also invite it to the admin room you set as `matrix.alerts_room`.

## 8. Telnyx profile

```sh
cd /root/matrix-sms-gateway
set -a; . /matrix/sms-gateway/.env; set +a
python3 scripts/telnyx_setup.py            # dry run: shows current state + planned changes
python3 scripts/telnyx_setup.py --apply
```

`--apply` sets:

- the webhook URL
- AI opt-out detection off
- `smart_encoding` off (D9)
- daily spend limit $15 (D8)
- STOP/START auto-replies with the registered wording

## 9. Live test (your phone only)

In the room: `!sms add +1717XXXXXXX Ryan Martin` (**PLACEHOLDER**: your own mobile number).
Then from that phone, in order:

| Text | Expect |
|---|---|
| `JOIN` | registered opt-in text, then "…STOP, CANCEL, END or QUIT. Text START to rejoin. Commands start with #. Text HELP for the list."; your ghost joins the room |
| `hello` | appears in the room as "Ryan Martin (SMS)" |
| post from Matrix with an iPhone-style apostrophe | arrives as `Name: text` with a plain `'` |
| `HELP` | **exactly one** reply: the `#vote 1 …` list. Two replies = Telnyx answers HELP too → re-run setup with `--help-autoresp` (D5) |
| `who` / `yes` | posted to the room as normal messages (not commands) |
| `#who` | numbered member list |
| `#blah` | "Unknown command. Text HELP for the list." |
| `#dm <misspelled name> test` | "No exact match … Reply #yes to send"; then `#yes` → "Sent to …" |
| `#vote 1` (after a Matrix poll), a photo both ways | as described in BRIEF.md §6 |
| `STOP` | exactly one reply (the registered opt-out text); ghost leaves the room |
| `JOIN` | no reply (Telnyx block holds until START) |
| `START` | registered opt-in text once; ghost rejoins |

Then check:

- `!sms status` in the room shows "Today (UTC): est. $… of $15.00 daily limit".
- `journalctl -u matrix-sms-gateway | grep -i alert` shows nothing unexpected.

## 10. Backups

`scripts/backup.sh DEST` takes a consistent SQLite snapshot (safe while running). Add it to the nightly job
once that exists (**PLACEHOLDER**: DEST = the NAS share path). `.env` holds the Telnyx key — back it up
encrypted or not at all (it can be regenerated in the portal).

## Updating

```sh
cd /root/matrix-sms-gateway && git pull && docker build -t matrix-sms-gateway:latest . \
  && systemctl restart matrix-sms-gateway
```

The queue is persisted; a restart never re-sends (anything mid-send is marked `unknown`, not retried).
