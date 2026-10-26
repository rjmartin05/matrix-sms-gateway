# SMS members as real accounts (D14)

By default every SMS member is a bridge "ghost", `@sms_<digits>:<domain>`. That user ID shows the phone
number to everyone, and someone who later installs the app gets a second, different account.

With `appservice.sms_accounts: true` an SMS member is an ordinary account instead:

- When someone is added, the login service creates a normal account named from their display name
  (`@dave.miller:<domain>`) with the phone number attached, the same way app sign-ups get one. The bridge
  posts their texts *as* that account.
- Display names stay plain ("Dave Miller", no " (SMS)"). Who is on SMS is published in the group room as a
  state event, `io.github.rjmartin05.smsgw.sms_members` (`{"users": [mxid, ...]}`, opted-in members),
  so the app can tag them. Other clients show nothing extra.
- If they install the app and sign in (or "sign up") with their number, they get the same account, with
  their history and DMs. The bridge stops texting them (status `app`) and they stay in the room.
- Name and avatar are set when they're added or renamed (`#name`, `#avatar`), never otherwise, so what
  they later set in the app isn't overwritten.

## Who can add and remove

- Any member of the group room can add someone (`matrix.members_can_add`, default true), from the app or
  with `!sms add +1XXXXXXXXXX Full Name`. Non-admins can add `matrix.adds_per_day` people a day (20).
- Admins can remove anyone; others only the people they added (`!sms remove +1XXXXXXXXXX`, or the app).
  A removed member who never used the app can no longer sign in with their number; re-adding them
  reuses the same account.
- Numbers are shown only to admins and to whoever added that person.

## The invite text

`compliance.invite_text` is one text sent when someone is added, before they JOIN, e.g.

```yaml
compliance:
  invite_text: "{brand}: {adder} added you to the group text. Reply JOIN to get group messages, STOP to opt out."
```

`{brand}`, `{adder}` (first name) and `{number}` (the group number) are filled in. It must include STOP;
startup refuses it otherwise. **Only turn it on once the 10DLC campaign covers it** (opt-in flow and
sample message). Left unset, nothing is sent until they text JOIN, as before.

It is sent once: not when someone is re-added after opting out at any time, not after they've ever
joined, and not if they're removed before it goes out. Everything else still needs JOIN.

## App API (login service)

All need `Authorization: Bearer <the app's access token>`; the caller must be in the room.

| Request | Body / query | Result |
|---|---|---|
| `GET /v1/sms-members?room_id=` | | `{"ok", "room_id", "members": [{"mxid", "name", "status", "can_remove", "number"?}]}` |
| `POST /v1/sms-members` | `room_id`, `display_name`, `phone` | `{"ok", "mxid", "status", "invite_sent", "message"}` |
| `POST /v1/sms-members/remove` | `room_id`, `phone` | `{"ok", "message"}` |

`room_id` may be empty for the first bridged room. Errors: `forbidden` (403), `rate_limited` (429),
`uses_app` (409: that number already has an app account), `bad_phone`, `bad_name`, `not_found`.

## Turning it on

1. The registration must let the bridge act as real users. Add to `registration.yaml`'s `users:` list
   (or regenerate with `gen_registration.py --real-users`):

   ```yaml
       - exclusive: false
         regex: '@.*:example\.com'      # your domain, dots escaped
   ```

   Synapse then also sends the bridge every event on the server; the bridge keeps only the group rooms,
   SMS members' DM rooms and `!` commands in rooms it's in, and drops the rest unread. This gives the
   bridge's token the power to act as any local user, which is what bridges' "double puppeting" uses.
   Restart Synapse after the change.
2. `config.yaml`: `appservice.sms_accounts: true` (needs `login.service_url`). Optionally
   `compliance.invite_text` once the campaign covers it.
3. Restart the bridge and the login service (the login service adds its new column itself).

Not verified yet: that Synapse with MAS lets an appservice act as MAS-created users this way. It's how
mautrix bridges double-puppet under MAS, so it's expected to work; test with one number first.

Existing `@sms_<digits>` ghosts aren't converted. Turn this on before adding SMS members.
