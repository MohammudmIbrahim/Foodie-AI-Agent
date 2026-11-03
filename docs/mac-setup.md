# Mac, BlueBubbles and Jev setup guide

Status: **written, not yet executed.** Nothing in this guide has been verified on real Apple
hardware with a live bot identity. Mark a step `blocked`, never
`passed`, if it fails.

Never disable SIP, never enable the BlueBubbles Private API, never add an SMS fallback, and never
let the bot send from your personal Apple Account.

## 0. Before you start: keep the project out of iCloud sync

This Mac's Desktop is synced to iCloud Drive ("Optimize Mac Storage" evicts files). That made
Python imports hang while iCloud re-downloaded `.venv` files. The repo works around it with
`.venv.nosync/` (a `.venv` symlink points at it) and `DATABASE_URL=sqlite:///./data.nosync/foodie.db`
in `.env`, because iCloud skips `*.nosync` names. The cleanest fix is to move the project out of
iCloud entirely, for example:

```bash
mkdir -p ~/code && mv "$HOME/Desktop/Certs/Foodie AI Agent/foodie-ai-github" ~/code/foodie-ai
```

A live SQLite WAL database inside an iCloud-synced folder risks corruption; keep `data/` local.

## (a) Dedicated macOS user and Apple Account

1. Create an Apple Account for the bot with a new email address (e.g. `foodie.bot.<you>@icloud.com`).
2. System Settings → Users & Groups → add a **Standard** user, e.g. `foodiebot`. This keeps the bot's
   Messages identity separate from yours. Fast User Switching keeps both sessions running.
3. Log in as `foodiebot`, open Messages, sign in with the bot Apple Account only. In Messages →
   Settings → iMessage, make sure "You can be reached at" lists only the bot email.
4. System Settings → Energy (or Battery → Options): prevent automatic sleep while plugged in. A
   sleeping Mac means no replies; the app expires stale work when it comes back.

## (b) Verify a real blue-bubble exchange manually

From your own iPhone, send an iMessage to the bot email. It must arrive **blue** in the bot's
Messages app, and a manual reply typed in the bot's Messages must arrive blue on your phone. Then
create a **test group** with one consenting friend and the bot email. Do not continue until a manual
group reply from the bot works.

## (c) Install BlueBubbles Server (public REST + webhooks only)

1. As `foodiebot`, download BlueBubbles Server from the official site / GitHub releases.
2. During setup, grant only what the setup screens require (Full Disk Access for reading the
   Messages database; Accessibility/Automation for AppleScript sends). **Skip the Private API.**
3. Set a strong server password. Keep the server bound to this Mac; the Foodie backend talks to it
   at `http://127.0.0.1:1234` (default port; check the server UI). No ngrok/Cloudflare tunnel is
   needed because both run on the same Mac.
4. Confirm the installed server version in its UI and open its API docs (Postman collection linked
   from https://docs.bluebubbles.app/server/developer-guides/rest-api-and-webhooks). Check that
   `POST /api/v1/message/text?password=...` accepts `chatGuid`, `tempGuid`, `message`, `method`, and
   that the `new-message` webhook payload contains `data.guid`, `data.text`, `data.isFromMe`,
   `data.handle.address`, `data.chats[0].guid`, `data.dateCreated`. If anything differs, update
   `app/messaging/bluebubbles.py` and `tests/test_webhook.py` before going live.

## (d) Point the webhook at the local FastAPI app

1. Generate secrets:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
   Use one value for `ID_HASH_PEPPER` and another for `BB_WEBHOOK_SECRET` in `.env`.
2. In BlueBubbles → API & Webhooks → add webhook URL
   `http://127.0.0.1:5050/webhooks/bluebubbles?token=<BB_WEBHOOK_SECRET>` subscribed to
   **New Messages** only.
3. The app rejects any webhook not from loopback or without the token.

## (e) Read the real chat GUID and sender identifiers

Keep `MESSAGE_PROVIDER=mock` for this step so nothing can be sent.

```bash
curl -s "http://127.0.0.1:1234/api/v1/chat/query?password=$BB_PASSWORD" \
  -H 'content-type: application/json' -d '{"limit": 50, "with": ["participants", "lastMessage"]}'
```

Find the test group's `guid` (looks like `iMessage;+;chat123...`) and the participants' handle
addresses. Do not paste these into Git, issues or logs.

## (f) Limit to the consenting test group

In `.env`:

```dotenv
APP_ENV=production
ALLOWED_CHAT_GUIDS=iMessage;+;chat...your-test-group...
SETUP_SENDERS=+1480XXXXXXX            # your own handle, as BlueBubbles reports it
MESSAGE_PROVIDER=bluebubbles
BB_BASE_URL=http://127.0.0.1:1234
BB_PASSWORD=...
BB_WEBHOOK_SECRET=...
ID_HASH_PEPPER=...
```

Startup refuses to run in production without the pepper and webhook secret.

## (g) Test wake → blue-bubble reply

```bash
source .venv/bin/activate
pytest -q                       # must be green first
uvicorn app.main:app --host 127.0.0.1 --port 5050
```

Manual acceptance checklist (record results in the implementation log):

| Step | Expected |
|---|---|
| Friend sends "hello" in the group | No reply. App log shows no processing. |
| You send `Foodie AI setup` | One intro, sent from the **bot** identity, blue. |
| Same again | "already set up" only, never a second intro. |
| Both send `Foodie AI agree`, vote names, `Foodie AI close vote` | Poll result message. |
| `Chef city Tempe, AZ`, `Chef confirm` | City confirmation then "All set". |
| `Chef find dinner near Tempe` then `halal for me` | Ack, then a shortlist with Maps links. |
| `Chef stop` mid-search | "Stopped." and no shortlist afterwards. |
| Bot's own messages | Never trigger a reply (no loop). |

## (h) Stop, revoke, roll back

- Stop: `Ctrl+C` uvicorn. In the group: `Foodie AI erase group` (organizer) deletes group data.
- Revoke: remove the webhook in BlueBubbles, quit BlueBubbles, remove its Full Disk Access /
  Accessibility grants, sign the bot account out of Messages, and remove the bot from the group.
- Roll back to offline mode: set `MESSAGE_PROVIDER=mock` and clear `BB_*` values.
- Local data: `rm -rf data.nosync data` deletes the SQLite DB (all groups, preferences, logs of
  actions). Messages/BlueBubbles keep their own copies of chats; that storage is Apple's/BlueBubbles',
  not Foodie's.

BlueBubbles is unofficial. macOS updates can break it. If it cannot be made to work on this Intel
Mac, keep using `MESSAGE_PROVIDER=mock` and record the blocker.

## Jev credentials

1. Decide which provider your account is with, from its own docs/dashboard:
   - TypeSafe (`https://api.typesafe.ai/v1/systemone`, body has `model`) → `JEV_SCHEMA=typesafe_systemone`,
     `JEV_BASE_URL=https://api.typesafe.ai`, `JEV_ENDPOINT=/v1/systemone`, `JEV_MODEL=jev-latest` (or a pinned version).
   - jevtypesafe.org (`https://jevtypesafe.org/api/v1/decide`, no `model`) → `JEV_SCHEMA=jevtypesafe_decide`,
     `JEV_BASE_URL=https://jevtypesafe.org/api`, `JEV_ENDPOINT=/v1/decide`.
   The two are not interchangeable; the app refuses to start with Jev enabled but no schema.
2. Compare one real response with `tests/test_jev.py::answer()`. If field names differ (e.g.
   confidence location), update `parse_response` and the fixture together.
3. Set spend limits before enabling: `JEV_MAX_CALLS_PER_SESSION` (default 4) and
   `JEV_MAX_CALLS_PER_DAY` (default 200). No automatic retries are made.
4. Enable: `DECISION_PROVIDER=jev`, `JEV_ENABLED=true`, `JEV_API_KEY=...`. A 401/403 disables Jev
   for the process and falls back to rules (audit event `jev_auth_failed`).
5. Jev only sees: session status, whether an area is set, requested cuisine names, whether a
   shortlist exists, and one redacted message (phones, emails, links removed). Never history or names.

## Live OpenStreetMap search

1. Set `OSM_USER_AGENT` to something identifying with a contact you own, e.g.
   `FoodieAI-local/0.1 (https://github.com/<you>/foodie-ai)`.
2. Check connectivity once: `python scripts/live_osm_check.py "Tempe, AZ" burger`.
3. Enable: `PLACES_PROVIDER=osm`, `ENABLE_LIVE_SEARCH=true`. Each area (at least
   `DEFAULT_RADIUS_MILES` around the search point) is fetched once and reused for
   `PLACES_AREA_TTL_MINUTES` (default a day); Nominatim is limited to 1 request/second. Public
   Overpass instances are frequently at capacity: the bot reports "lookup unavailable" and waits
   for `Chef go` instead of retrying.
4. Faster: create a free Geoapify key (https://myprojects.geoapify.com) and set
   `PLACES_PROVIDER=geoapify`, `GEOAPIFY_API_KEY=...`. An area fetch costs about 1 credit per 20
   places (`GEOAPIFY_MAX_PLACES=1000` caps it near 51 of the free plan's 3,000 daily credits). If
   Geoapify fails or the key is rejected, the bot falls back to public Overpass. Verified live
   on 2026-11-03: `live_osm_check.py "Tempe, AZ" burger` fetched the area in ~7 s and returned
   OSM-linked places with opening hours.
