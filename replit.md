# Telegram FAQ Bot

A Python Telegram bot with PostgreSQL-backed JSON documents and a Hebrew admin dashboard.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- `python main.py --runtime development` — run the separate test bot with `DEV_BOT_TOKEN`; without it, serve only standby health
- `python -m unittest discover -s tests -v` — run the Python unit tests
- Required env: `DATABASE_URL` — Postgres connection string
- Production uses only `TELEGRAM_BOT_TOKEN` for @CinemixIL_bot; legacy `BOT_TOKEN` is disabled. Optional `DEV_BOT_TOKEN` must belong to a different test bot.
- Explicit temporary Cinemix testing: development-only `BOT_WORKSPACE_PREVIEW=1` selects `TELEGRAM_BOT_TOKEN`, verifies Cinemix identity, and acquires the exclusive polling lease. Turn this OFF and stop the workspace poller before publishing; never run both against Cinemix.

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- Python 3.12, `pyTelegramBotAPI`, and `python-dotenv` for the Telegram bot
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `main.py` — Telegram bot entry point and message handlers
- `admin_dashboard.py` — admin-only inline menus and guided editing flows
- `admin_menus.py` — Hebrew inline menus, command summaries, and ten-second statistics messages
- `bot_storage.py` — validated JSON storage, formatting, and private-user/group registration
- `commands.json` — dynamic keyword replies, /start text/buttons, and fallback reply
- `users.json` — unique private Telegram user IDs recorded by the bot
- `groups.json` — active known group/supergroup IDs
- `group_management.py` — join welcomes and link moderation middleware, including edited messages and media captions
- `group_settings.py` and `group_settings.json` — validated, atomic shared group settings
- `system_features.py`, `system_settings.json`, `activity.json` — maintenance and anonymous Israel-time hourly counters
- `advanced_admin.py` — maintenance-update and night-setting conversations
- `night_lock.py`, `night_lock_state.json` — scheduled locks and restart-safe original permission snapshots
- `access_control.py`, `access_admin.py`, `access_settings.json` — hashed private-chat passcodes and approved-user management
- `admin_ui.py`, `admin_ui_state.json` — single edited admin menu, temporary notices, and administrative input cleanup
- `support_storage.py`, `support_relay.py`, `support_admin.py` — single-group support and persistent owner-only reply routing
- `support_settings.json`, `support_threads.json` — selected support group, receipt text, pending IDs, and routes
- `telegram_faq_bot/faq.py` and `data/faqs.json` — optional FAQ matching utilities
- `tests/` — Python tests for response matching and FAQ utilities

## Architecture decisions

- Keyword replies are loaded from `commands.json` for each incoming text, so admin edits apply immediately.
- Only `ADMIN_USER_ID` can access the `/users` or `הגדרות` admin menu and its callbacks.
- Broadcast sends the next admin text to registered private users and groups; it accepts input only from the chat where the workflow began.
- `ביטול` or `/cancel` cancels any input workflow and returns to the main menu. Partial start/button-command edits are saved only on completion.
- Text-only commands and commands with inline buttons have separate management submenus.
- Group membership updates remove groups the bot leaves. Telegram cannot enumerate pre-existing groups; they are discovered through messages or membership events.
- Configured messages support legacy Markdown, supported HTML, and native Telegram formatting.
- Group-management settings are shared across groups; only the bot administrator can edit them. Link deletion exempts the group's administrators/owner, not the bot administrator automatically.
- Welcomes contain `{name}` and expire after 10 seconds. Link warnings expire after 5 seconds. The bot needs group-admin delete permissions.
- The bot uses Telegram polling, removes any active webhook on startup, and keeps queued updates.

## Product

- Users receive simple Hebrew responses for greeting, price, and hours keywords.

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

- Never commit `.env`; store bot tokens in Replit Secrets, not logs or chat.
- Do not log user/group IDs or codes. IDs are displayed only in owner-authorized support/user-management views. Keep all persistent JSON stores valid; they are validated at startup and written atomically.

## Persistent storage and publishing

- `BOT_STORAGE=postgres` makes PostgreSQL authoritative for settings, users,
  standard/VIP media, reports, analytics, and live-chat queues. Local JSON files
  are atomic mirrors retained for existing backup/download features.
- Startup seeds only missing documents, then restores database records over local
  mirrors. It never replaces existing database data with bundled JSON.
- Database failures must be explicit: do not silently fall back to stale JSON
  while PostgreSQL mode is enabled. Temporary test paths remain file-backed.
- The registered Telegram Bot Server artifact runs the existing Python
  `main.py` directly. Its HTTP health endpoint binds to the injected `PORT`.
  The Run button starts this managed service; there is no second polling workflow.
- Publish as a Reserved VM. Publish applies the development database schema to
  production; do not add startup DDL or deploy-build schema migrations.
- Runtime selection is explicit in managed commands: development uses only
  `DEV_BOT_TOKEN`; production uses only `TELEGRAM_BOT_TOKEN`, verifies @CinemixIL_bot, and requires Replit's
  published-deployment marker. Plain `python main.py` defaults to development.
- Missing `DEV_BOT_TOKEN` means health-only standby, with no Telegram calls,
  storage initialization or background jobs. Same-bot development credentials
  are rejected. Stop any pre-isolation workspace process before publishing;
  never run another production poller with the same token.
- See README.md for the publishing handoff. Do not publish automatically,
  rotate the existing production secret, or replace production data for testing.
- `/` is liveness only. Monitor published `/readyz` for polling readiness;
  `/status` exposes anonymous diagnostics. Development standby is intentionally
  not polling-ready. See README.md for thresholds and monitoring limitations.
- Do not overwrite production data from development on later publishes.
- Access is required initially, but no initial code is provided. Only the owner can configure one, in private; authorization is separate from user registration, and code changes preserve approvals.
- Private administrator retention follows `.agents/memory/private-admin-messages.md`; do not apply normal notice timers there. Normal-user support receipts expire after 10 seconds and group notices after 5 seconds.
- Keep support and admin input states exclusive. Opening admin controls cancels pending support input, and opening support cancels/hides the admin prompt.
- Support reply routing is limited to the owner and the current configured group. Never infer a recipient from forwarded metadata or route replies from other groups.
- Night locking restores saved original permissions, not a blanket allow-all. Never discard a snapshot after an API failure. The worker must be running for scheduled changes.
- Count activity only after access checks; exclude administrative/service traffic. Use Israel local time and actual message timestamps rather than polling time.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
