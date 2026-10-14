# Deploy on the `matrix` VM

Run everything as root on the VM. Commands are copy-paste ready except where marked
**PLACEHOLDER**. Nothing here texts anyone; the first SMS goes out only after a number texts JOIN.

## 0. Preconditions (check, don't skip)

- The group room exists, is **unencrypted**, invite-only. Note its alias (e.g. `#general:example.com`).
- Synapse isn't forcing encryption on new rooms (ghost-created DM rooms must stay unencrypted).
  The playbook default is `"off"` (verified in source). This should print nothing:

  ```sh
  grep -n encryption_enabled_by_default /root/mdad/inventory/host_vars/matrix.example.com/vars.yml
  ```

## 1. Get the code and build the image

The repo is private, so the VM needs read access to GitHub. **PLACEHOLDER (pick one):** add a
read-only deploy key to the repo, or copy the tree over with `scp -r` from a machine that has it.

```sh
git clone git@github.com:rjmartin05/matrix-sms-gateway.git /root/matrix-sms-gateway
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
(**PLACEHOLDERS**: all three come from the Telnyx Mission Control portal of the account that owns the number):

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

Edit: `rooms[0].number` (**PLACEHOLDER** `+1XXXXXXXXXX` — the bridge refuses to start while it contains
`X`), `rooms[0].room` if the alias differs, and optionally `matrix.alerts_room`.

## 4. Register the appservice with Synapse (playbook-supported way)

First check these variables aren't already set (a duplicate YAML key would silently override):

```sh
grep -nE 'matrix_synapse_container_additional_volumes|matrix_synapse_app_service_config_files' \
  /root/mdad/inventory/host_vars/matrix.example.com/vars.yml
```

If that printed nothing:

```sh
cat >> /root/mdad/inventory/host_vars/matrix.example.com/vars.yml <<'EOF'

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

Expect `bot can't join #general... invite @smsbot:example.com` until step 7.

## 6. Public hostname (Cloudflare Tunnel)

Add a public hostname **without** a Cloudflare Access application:
`sms-hook.example.com` → `http://localhost:29341`.

- Dashboard-managed tunnel: Zero Trust → Networks → Tunnels → your tunnel → Public Hostname → Add.
- Locally-managed (`config.yml` ingress): add `- hostname: sms-hook.example.com` /
  `service: http://localhost:29341` above the catch-all rule and `systemctl restart cloudflared`.

I don't know which mode your tunnel uses. Check: if `/etc/cloudflared/config.yml` has an `ingress:` list
containing `example.com`, it's locally managed.

```sh
curl -s https://sms-hook.example.com/healthz          # -> ok
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://sms-hook.example.com/telnyx/webhook   # -> 401
```

## 7. Matrix room

In your Matrix client, in the group room:

1. Invite `@smsbot:example.com` (it auto-joins).
2. Give it power level 50 (moderator): it needs that to invite ghosts, `@room` announcements, and pin.

## 8. Telnyx profile

```sh
cd /root/matrix-sms-gateway
set -a; . /matrix/sms-gateway/.env; set +a
python3 scripts/telnyx_setup.py            # dry run: shows current state + planned changes
python3 scripts/telnyx_setup.py --apply    # webhook URL, AI opt-out OFF, STOP/START auto-replies
```

Optional: `--daily-spend-limit 5.00` caps spend per day (your call; see DECISIONS.md D7).

## 9. Live test (your phone only)

In the room: `!sms add +1XXXXXXXXXX Evan Parker` (**PLACEHOLDER**: your own mobile number).
Then from that phone, in order:

| Text | Expect |
|---|---|
| `JOIN` | registered opt-in text, then the "To leave, text one word…" line; your ghost joins the room |
| `hello` | appears in the room as "Evan Parker (SMS)" |
| post from Matrix | arrives as `Name: text` |
| `HELP` | **exactly one** reply. Two replies = Telnyx answers HELP too → re-run setup with `--help-autoresp` (see D5) |
| `yes` | posts "yes" to the room (not a command) |
| `WHO`, `VOTE 1` (after a Matrix poll), a photo both ways | as described in BRIEF.md §6 |
| `STOP` | exactly one reply (the registered opt-out text); ghost leaves the room |
| `JOIN` | no reply (Telnyx block holds until START) |
| `START` | registered opt-in text once; ghost rejoins |

Then check: `!sms status` in the room, and `journalctl -u matrix-sms-gateway | grep -i alert`.

## 10. Backups

`scripts/backup.sh DEST` takes a consistent SQLite snapshot (safe while running). Add it to the nightly job
once that exists (**PLACEHOLDER**: DEST = your backup share path). `.env` holds the Telnyx key — back it up
encrypted or not at all (it can be regenerated in the portal).

## Updating

```sh
cd /root/matrix-sms-gateway && git pull && docker build -t matrix-sms-gateway:latest . \
  && systemctl restart matrix-sms-gateway
```

The queue is persisted; a restart never re-sends (anything mid-send is marked `unknown`, not retried).
