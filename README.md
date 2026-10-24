# Telegram FAQ Bot

A small Hebrew-language Telegram bot using `pyTelegramBotAPI`. It responds to
keywords and keeps its editable replies in `commands.json`.

## Run it

1. Store the production credential in the `TELEGRAM_BOT_TOKEN` secret.
   Production accepts only @CinemixIL_bot; the legacy `BOT_TOKEN` is never used.
   Never put a token in source code or chat.
2. For workspace testing, create a **different bot** with
   [@BotFather](https://t.me/BotFather) and add its token as `DEV_BOT_TOKEN` in
   development Secrets. For local development, copy `.env.example` to `.env`
   and set only `DEV_BOT_TOKEN`. Never commit `.env` or share tokens.
3. The administrator can manage replies from Telegram with `/users` or by
   sending `הגדרות`.
4. Start **artifacts/telegram-bot: web**, or run
   `python main.py --runtime development`.

Without `DEV_BOT_TOKEN`, the workspace runs only an HTTP health endpoint. Its
response explicitly says **Development standby**. It does not connect to
Telegram, change webhooks, initialize storage, or start background jobs.
`python main.py` also defaults to this safe development behavior. Restart the
workflow after adding the development secret.

Development never falls back to `TELEGRAM_BOT_TOKEN` or `BOT_TOKEN`. Setting `DEV_BOT_TOKEN` to the
production token, or to a rotated token for the same bot, is rejected before
storage or Telegram access. Production mode is rejected outside a published
Replit deployment (`REPLIT_DEPLOYMENT=1`); development mode is rejected inside
one. Credentials are never included in runtime-selection messages.

## Publishing handoff

- The validated artifact configuration launches production with
  `.pythonlibs/bin/python -u main.py --runtime production`; the legacy
  `.replit` run command also explicitly selects production. Production uses
  **only `TELEGRAM_BOT_TOKEN` for @CinemixIL_bot**, never the legacy `BOT_TOKEN` or `DEV_BOT_TOKEN`. A missing production secret
  stops startup rather than falling back.
- These configuration changes do not change the currently published build.
  No automatic publish is performed. When you choose to publish, keep the
  production credential, production database and `BOT_STORAGE=postgres`.
  Change credentials only when explicitly requested. Do not migrate/clear
  production documents or copy development records over production.
  After changing the token, publish the updated code. If it belongs to a
  different bot, add that bot to the management group with the needed rights
  and re-upload Telegram media: file IDs are specific to the bot identity.
- Before publishing this update, stop any old workspace process running the
  previous entrypoint, which could still poll `BOT_TOKEN`. The new workspace
  service is safe to run alongside production in standby or with a separate
  development bot. Keep just one production deployment/process polling.
- Test the development bot in private chats and dedicated test groups; do not
  add it to production groups or reuse the production database for testing.
- After publishing, confirm production logs show runtime **production** and
  polling without Telegram conflict errors, and send `/start` to the published
  bot. HTTP health alone does not prove updates are being received.
- To verify workspace isolation, its logs should say **Development standby**
  or runtime **development**. Only the separate test bot should answer its
  test messages. A configuration error stops startup before any webhook change.

An active bot uses polling, so it does not need a public webhook URL. When it starts,
it removes any existing webhook and processes queued updates rather than
discarding them.

### Production polling ownership

Production must acquire a PostgreSQL session advisory lock before initializing
storage or contacting Telegram. It requires `DATABASE_URL`, even when file
storage is selected. All instances of the same bot must use the same database
and a direct or session-pooled connection, not a transaction-pooled URL. The
lock uses bot identity, so rotating a token does not permit a second poller.
It creates no tables and never changes stored JSON documents.

A competing instance exits with a clear lease error without changing webhooks
or starting workers. It does not wait for or automatically take over the lock;
restart the replacement after the previous process has stopped. Clean shutdown
holds ownership through worker cleanup and then closes the dedicated session.
PostgreSQL also releases ownership when that session dies.

The owner checks its session every second and before each Telegram API request.
If ownership or the connection is lost, it exits immediately with status 1,
stopping all workers rather than reconnecting. Database network failures have
bounded connection/query and TCP timeouts; a request already in flight cannot
be recalled. Development and health-only standby do not acquire a lease.
This protects updated instances sharing one database; it cannot stop an older
build without lease support or a process using a different database. Keep the
old-process publishing handoff above. No automatic publishing is performed.

## Polling health and monitoring

- `/` remains HTTP **200** for process liveness (or the development standby
  message). It is not proof of Telegram delivery.
- `/readyz` and `/health/polling` return anonymous JSON and HTTP **200** only
  after a successful `getUpdates` response, including an empty response.
  They return **503** while starting, after a polling failure, after polling
  stops, or when no successful poll has completed for **120 seconds**.
  This allows for the normal 20-second long poll and network delay.
- `/status` returns the same JSON with HTTP **200** for inspection, including
  runtime, state, readiness, monotonic ages of the last success/failure,
  failure counts and a fixed failure category. It never includes tokens,
  exception text, chat/user IDs, message content or Telegram request URLs.
  Responses are not cached. GET and HEAD use the same status codes.
- Missing `DEV_BOT_TOKEN` is deliberate **development / standby**, not a
  production outage. Its liveness is 200 but polling readiness is 503.
  A production monitor must require `runtime: production` and `ready: true`;
  never accept workspace standby as evidence the published bot is healthy.
- Monitor the **published** `/readyz`, not `/` or `/status`. Treat non-200,
  request timeout, and unreachable servers as unhealthy. Allow a short startup
  grace period; alert on failures persisting for roughly two minutes (for
  example four consecutive failed checks at 30-second intervals). A conflict,
  unauthorized, forbidden or repeated rate-limit failure needs investigation.
  Successful polling clears the active failure; historical failure counts,
  category and age remain until process restart and are not alone an outage.
- Fresh polling proves Telegram connectivity, not successful replies from every
  handler, correct bot identity, or database/background-job health. Verify
  `/start` with a real message after a manual publish; do not send production
  test messages or start a second poller as part of automated health checks.

These changes provide status only: they do not configure an external alert
service, automatically restart competing processes, or publish the app.

## Replies

- A message containing `היי` gets a greeting.
- A message containing `מחיר` asks which product or service the user means.
- A message containing `שעות` gets the configured hours response.
- A message containing `הכוכב הבא` gets “הכוכב הבא עונה 12” with a link button.
- `/start` and `/help` show a short welcome; unmatched messages get a fallback.

## Private users and groups

People who message the bot privately are recorded once in `users.json`. Group
and supergroup IDs are saved in `groups.json`, separately from private users.
The bot records membership changes and removes groups when it leaves or is
removed. Existing groups become known when the bot receives a message or a
membership update there; Telegram does not provide an API to enumerate all of
a bot's groups.

The administrator can open the statistics submenu from `/users`, or by sending
`משתמשים`. Each count message is deleted after ten seconds; the menu remains.

## Admin menu

Only the configured administrator can open `/users` or `הגדרות` and use its
callbacks. The Hebrew main menu contains:

- 📊 סטטיסטיקה ומשתמשים
- 💬 פקודות טקסט רגילות
- 🔘 פקודות עם כפתורים
- 📋 הצגת כל הפקודות הקיימות
- 📢 שליחת הודעה לכולם
- ⚙️ הודעות מערכת וברירת מחדל
- 🛡️ ניהול קבוצות
- 🔑 ניהול קוד גישה

Text replies and replies with buttons have separate add/edit/delete lists.
Button commands support multiple URL buttons, editing the reply, and replacing
or removing the buttons. Every URL button uses `InlineKeyboardButton`.
System settings include the `/start` text/buttons and the fallback reply.
All replies are saved in `commands.json` and take effect immediately.


### Support request tracking

In the system-settings submenu, **פניות תמיכה פתוחות** shows the current
count and a paginated list of request-header message IDs in the configured
support group. Refresh the list to see recent changes.

Each new request header has a button showing **פתוחה** or **טופלה**.
Only the sole bot administrator, in the currently configured support group,
can mark it handled or reopen it. The button edits the original message;
no new group message or private notification is sent. Handling a request
does not disable replies, including replies to copied media.

Statuses survive restarts in `support_threads.json`; only header IDs and
status are added, not names, usernames, or request content. Existing legacy
reply routes remain usable but are not retroactively counted: the old format
does not distinguish request headers from copied media. If a message edit
fails, the saved status remains authoritative and clicking again retries
the edit.

Broadcast sends the next text immediately to registered private users and
groups. Its prompt shows both recipient counts first. Send `ביטול` or `/cancel`
at any input step to cancel and return to the main menu. A pending broadcast
only accepts input from the chat where it was started. Deleting a command asks
for confirmation. Partial button-command and `/start` edits are not saved if
cancelled.

## Text formatting

Replies, `/start`, fallback messages, and broadcasts support Telegram's legacy
Markdown (for example `*bold*`, `_italic_`, and `[link](https://example.com)`)
or supported HTML tags such as `<b>bold</b>` and `<i>italic</i>`. The mode is
detected from the text. Formatting applied with Telegram's own text editor is
preserved as HTML. Plain messages and plain URLs remain unformatted.

## Group management

`group_settings.json` stores the welcome template and link-filter toggle.
These settings apply to all managed groups and can be changed only by the
configured bot administrator from the group-management submenu.

- New human members get a welcome containing their name. Use `{name}` in the
  template; without it, the name is appended. Welcomes disappear after 10 seconds.
- Link filtering is enabled initially. It checks URLs, Telegram links, hidden
  text links, media captions, and edited messages. Group administrators and
  owners, including anonymous group administrators, are exempt.
- A deleted link produces a short warning that disappears after 5 seconds.
- The bot must be a group administrator with permission to delete messages.
  If deletion fails, it reports the missing permission instead of claiming
  the message was removed. If sender permissions cannot be checked, it avoids
  deleting a possible administrator's message.
- Private chats are not moderated, and disabling the filter leaves links untouched.

## Maintenance, activity, and night locking

The system submenu includes a maintenance toggle and an editable notice.
The owner continues to use all features during maintenance. Each toggle asks
whether to send a custom, one-off update; choosing “yes” waits for text and sends
it only to registered private users. Normal broadcasts still include groups.

`activity.json` keeps anonymous daily hourly counters, using message timestamps
and `Asia/Jerusalem` time. Private requests and group commands/keywords are
counted after access checks; bot service messages, edits, and the owner's input
are excluded. The report shows the busiest hours and the largest single-hour
peak, then deletes itself after ten seconds. There is no invented historical data.

Night mode is initially off. Its global defaults are closing at 23:00 and
opening at 07:00, Israel time, and its schedule and announcement texts can be
edited from the group submenu. The bot needs group-admin permission to restrict
members. `night_lock_state.json` saves the original permissions before locking;
opening or disabling restores those exact permissions, even following a restart
or group-to-supergroup migration. Failed operations retain the snapshot and
retry, with a rate-limited owner warning.

The bot process must remain running for scheduled actions. If it stops overnight,
permissions are reconciled to the current schedule when it next starts.

## Clean admin chat and passcodes

Admin navigation edits one tracked menu; `admin_ui_state.json` remembers it across
restarts. In private administrator chats, all administrator input remains, as do
normal bot replies, confirmations, errors, warnings and statistics. Only admin
menu navigation can remove an old, known bot-authored menu. A central fail-closed
deletion guard prevents other cleanup paths, timers and bulk deletion from
removing private admin input or informational bot messages.
Temporary notices in groups and normal-user chats retain their configured expiry.
Administrative group input can still be deleted after processing; ordinary group
conversation is not deleted by this cleanup. Group cleanup requires permission.
Broadcast deliveries and configured content replies are not temporary admin notices.

Access is initially required, with **no preconfigured passcode**. Only the owner
can enter until setting a code in their private admin chat (or disabling the
requirement). Existing registered users are not automatically authorized.
`access_settings.json` stores a salted PBKDF2-SHA256 hash, never the plaintext
code, and authorized IDs. Codes can be entered only in private chats; attempts
are deleted for regular users, but every attempt receives a fresh access prompt,
without a local cooldown. Administrator inputs in private are never deleted.
Changing the code preserves existing approvals;
the owner can remove users individually. Disabling access temporarily opens the
bot without adding everyone to the authorization list.

## Live support

1. Add the bot to the chosen support group and, as the sole owner, send
   `/set_support_chat` there. The system submenu also offers a group-setting
   button that works only inside a group.
2. Authorized users see **💬 פנייה למנהל / תמיכה** in their private `/start`
   keyboard, alongside the existing URL buttons. Pressing it asks for a message.
3. Text, pictures, voice notes, and other supported media go only to the selected
   group with an escaped name, username, and user ID. Media is copied below a
   metadata header. The editable receipt is deleted after **10 seconds**.
4. Only the owner can reply to a routed header/media message inside the current
   support group to copy a response privately to its original user. Reply routing
   and delivered-response IDs survive restarts; unrelated replies never relay.
5. The system submenu includes **✏️ עריכת הודעת אישור תמיכה**.

`support_settings.json` holds the single support-group ID and receipt text.
`support_threads.json` holds pending user IDs and message-to-user routing, not
copies of user message content. Failed request delivery keeps the user pending
for retry and does not send a success receipt. Failed replies leave the owner's
source message available to retry. Switching the support group disables old
routes. After upgrading/replacing a support group, configure it again explicitly.
Access and maintenance checks apply to user support interactions; owner replies
are allowed during maintenance. Admin and support input states are mutually
exclusive so administrative codes cannot be captured as support requests.

## Tests

```bash
python -m unittest discover -s tests -v
```

To run only the isolated publishing-handoff checks:

```bash
python -m unittest discover -s tests -p 'test_polling_handoff.py' -v
```

These checks need `initdb` and `pg_ctl` (PostgreSQL) on `PATH`; they report a skip
if those tools are missing. They run the actual bot entrypoint and Telegram
worker threads in separate processes, with disposable Unix-socket PostgreSQL
databases, source-only temporary application copies, and a local fake Telegram
HTTP server. Child environments contain only synthetic credentials, disable
`.env` loading, and reject non-local network connections. No real Telegram
`getUpdates`, workspace database, or published data is used.

The scenarios cover shared-database lease contention, SIGTERM preview shutdown
before production starts, fatal preview exit on Telegram 409 across separate
databases, and fatal lease-session loss. They also check that queued `/start`
updates produce the real owner's admin menu without dropping pending updates.
Shutdown checks wait for actual OS process termination (including Telegram
daemon workers), verify application-service cleanup on graceful shutdown, and
ensure an exited preview makes no further requests or worker heartbeat writes.
