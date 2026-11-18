# Architecture

StarterKit is a Telegram bot for selling VPN subscriptions on DobbyVPN. Each owner runs their
own instance on their own server. The bot is a copy of remnashop (`bot/`, see
`bot/UPSTREAM.md`) with the Remnawave panel replaced by a small DobbyVPN backend module.

## Overview

```
 Telegram ──webhook──▶ ┌──────────────── owner's server ────────────────┐
                       │ bot (FastAPI + aiogram)      taskiq worker     │
 Payment provider ───▶ │   │  remnashop use cases       │ export job    │
   (webhook)           │   ▼                            ▼               │
                       │ DobbyBackend ──trigger──▶ export_users_*_task  │
                       │   │ reads/writes               │ reads         │
                       │   ▼                            ▼               │
                       │ PostgreSQL (source of truth)   UserExporter ───┼──▶ config-delivery
                       │ Redis (queue, export state)    (file now,      │    service
                       └────────────────────────────────sftp later)────┘    (keys, links)
                                                                              │
                                    DobbyVPN client ◀── subscription link ────┘
```

1. A user buys, renews, takes a trial, gets a referral or promo bonus, or an admin edits the
   subscription. remnashop's use case calls the `Remnawave` protocol as before.
2. `DobbyBackend` (`bot/src/infrastructure/dobby/backend.py`) answers with what the panel used
   to return: a subscription id, status, expiry and link. The use case stores them in the bot
   database as before. The backend then asks for an export.
3. `UserExportJob` (`export_job.py`) reads the current subscriptions, builds the list of users
   to serve and hands it to a `UserExporter`. It runs 2 seconds after each change and every
   minute. It uploads only when the list changed, retries a failed upload on the next minute,
   and forces an upload once the last one is an hour old.
4. The config-delivery service owns server keys and subscription links. It reads the file and
   stops serving a user when their date passes, even if the bot is down.

## What is stored where

| Data | Where |
|---|---|
| Users, roles, blocks, referrals, promo codes | Bot database, remnashop tables (`users`, `referrals`, `promocodes`, ...) |
| Subscriptions: id, status, expiry, plan snapshot, link | Bot database, `subscriptions`. `user_remna_id` is the permanent subscription id sent outside |
| Plans and prices | Bot database, `plans` |
| Payments | Bot database, `transactions`; gateway keys encrypted in `payment_gateways` |
| Export state (last digest and time, lock) | Redis keys `dobby:export:*` |
| Server keys, subscription links, devices, traffic | Config-delivery service |

No new tables are needed: remnashop already keeps every subscription in its own database.

## Exported file (placeholder)

Only current subscriptions that are `ACTIVE` and whose user is not blocked are listed.
Disabled, deleted and blocked users are left out. Expired ones stay with their past date.

```json
{
  "version": 1,
  "generated_at": "2026-11-19T12:00:00Z",
  "users": [
    {"id": "65fd48d2-92a4-4bc1-9088-9e31f6c97aff", "expires_at": "2026-12-19T12:00:00Z"}
  ]
}
```

The format lives in `exporter.py` (`serialize_users`), the transport in `UserExporter`.
Today `FileUserExporter` writes the file atomically (temp file in the same folder, fsync,
rename) to `DOBBY_EXPORT_PATH`. An sftp exporter will implement the same `export(content)`
method: upload to a temp name, then rename on the server.

## Subscription links (placeholder)

`TemplateLinkProvider` builds the link from `DOBBY_SUBSCRIPTION_URL_TEMPLATE`, replacing `{id}`
with the subscription id. It will be replaced once the config-delivery service defines links.

## What the owner configures

**`.env`, once at install** (`bot/.env.example` for remnashop's own settings):

| Variable | Meaning |
|---|---|
| `APP_DOMAIN`, `BOT_TOKEN`, `BOT_OWNER_ID`, `BOT_SUPPORT_USERNAME`, `DATABASE_PASSWORD`, `APP_CRYPT_KEY`, `BOT_SECRET_TOKEN` | As in remnashop |
| `DOBBY_EXPORT_PATH` | Where the user file is written. Default `/opt/remnashop/data/users.json` |
| `DOBBY_SUBSCRIPTION_URL_TEMPLATE` | Link template with `{id}` |
| `DOBBY_FORCE_EXPORT_INTERVAL_SECONDS` | Forced export period, default 3600 |
| `REMNAWAVE_TOKEN`, `REMNAWAVE_WEBHOOK_SECRET` | Still required by remnashop's config validation; any value works. To be removed |

**Admin menu in the bot** (remnashop as is): plans, prices, trial, payment gateways and their
keys, texts, support contact, referral program, users (expiry, block, delete).

## Removed or idle remnashop features

Device (HWID) limits, traffic limits and resets, squads, the panel dashboard section, the
importer from other panels and panel webhooks have no effect: the config-delivery service owns
servers and connections. The code stays in place to keep the diff from upstream small; see
`docs/remnawave-map.md` for each call.

## Not decided yet

- Export format, sftp address, folder and user, and whether the service re-reads the file
  right away.
- How the service turns a subscription id into a link, and whether links can be rotated
  ("reissue link" in the bot is a no-op for now).
- Who creates keys on the servers from the file.
- Whose config-delivery service other StarterKit owners use.
- Renewal reminders (72/48/24 hours, expired) came from panel webhooks. They need a scheduled
  task over `subscriptions.expire_at` in the bot.
- Whether to remove the panel-only screens and the `remnapy` dependency, or keep the diff small.
