# Architecture

StarterKit is a Telegram bot for selling VPN subscriptions on DobbyVPN. Each owner runs their
own instance on their own server. The bot is a copy of remnashop (`bot/`, see
`bot/UPSTREAM.md`) with the Remnawave panel replaced by a small DobbyVPN backend module.

## Overview

The bot needs no domain and accepts no inbound connections: it only makes outgoing requests
(Telegram, the payment provider, the config-delivery service).

```
                ┌──────────────── owner's server ─────────────────┐
 Telegram ◀─────┼─ getUpdates (long polling)                       │
                │ bot (aiogram + FastAPI)       taskiq worker      │
 YooKassa ◀─────┼─ create payment,              payment checks,    │
   API          │  check payment status         user export        │
                │   │                              │               │
                │   ▼                              ▼               │
                │ PostgreSQL (source of truth)   UserExporter ─────┼──▶ config-delivery
                │ Redis (queue, export state)    (file now,        │    service
                └─────────────────────────────────sftp later)──────┘    (keys, links)
                                                                          │
                                DobbyVPN client ◀── subscription link ────┘
```

1. A user buys, renews, takes a trial, gets a referral or promo bonus, or an admin edits the
   subscription. remnashop's use case calls the `Remnawave` protocol as before.
2. `DobbyBackend` (`bot/src/infrastructure/dobby/backend.py`) answers with what the panel used
   to return: the user's login, status, expiry and link. The use case stores them in the bot
   database as before. The backend then asks for an export.
3. `UserExportJob` (`export_job.py`) reads the current subscriptions, builds the list of logins
   to serve and hands it to a `UserExporter`. It runs 2 seconds after each change and every
   minute. It uploads only when the list changed, retries a failed upload on the next minute,
   and forces an upload once the last one is an hour old.
4. The config-delivery service owns server keys. It syncs to the file: logins missing from the
   list are removed, new ones are added. It does not check dates. The bot cuts a user off at
   expiry by leaving the login out; the per-minute run notices the date passing even when
   nothing changed in the database. While the bot is down nobody expires, so the bot has to
   be monitored.
5. The bot's only output to the user is the subscription link for their login, no configs.

## Telegram updates

`DOBBY_TELEGRAM_MODE=polling` (default): the bot fetches updates with `getUpdates`
(`telegram_polling.py`). A request hanging past its timeout is dropped and retried with
back-off. `DOBBY_TELEGRAM_MODE=webhook` is optional and needs `APP_DOMAIN` with HTTPS in front
of the bot.

## Payment confirmation

YooKassa does not need to reach the bot. Every trigger ends in `PaymentStatusChecker`
(`payment_check.py`): ask the YooKassa API for the payment status, then let remnashop's
`ProcessPayment` apply it.

| Trigger | When |
|---|---|
| Return to the bot | The payment page sends the user to `https://t.me/<bot>?start=pay`; the bot checks the user's payments right away |
| "I paid" button | On the payment message; checks the user's payments |
| Fast check | Each new payment: every 4 s for 3 minutes, every 15 s until 10 minutes |
| Sweep | Every minute: all pending payments. After 30 minutes an unpaid payment is given up (CANCELED) |
| Notification (optional) | Only with a domain. The body is not trusted: only the payment id is taken from it |

`ProcessPayment` moves the transaction with a conditional `UPDATE ... WHERE status IN (...)`,
so a payment activates or extends a subscription once however many triggers race. A payment
made after the give-up still completes when the user returns or presses "I paid" (CANCELED can
move to COMPLETED; the status always comes from the gateway API). The user's check looks 24
hours back. remnashop's blind 30-minute cleanup of pending transactions skips YooKassa.

Payment methods are the ones enabled in the owner's YooKassa shop: bank cards, YooMoney and,
if the owner turns it on in the shop settings, the Faster Payments System (pay by QR code from a
banking app). The bot passes no method, so whatever the shop enables appears on the page.

Opening the payment screen again within 15 minutes (going back and forth, or "Buy" after "I
paid") reuses the user's unpaid payment for the same plan, duration and amount: the bot asks
YooKassa for that payment's page instead of creating a new payment. A payment that can no
longer be paid is not reused.

## Renewal reminders

The Remnawave panel used to trigger them by webhooks. `RenewalReminders` (`reminders.py`) runs
every minute over `subscriptions.expire_at` and publishes remnashop's own events, so its texts,
the renew/buy button and the per-type switches in the admin notification settings apply.
Three reminders by default:

| Reminder | When |
|---|---|
| Expires in 3 days | From 72 h before expiry |
| Expires in 1 day | From 24 h before expiry |
| Expired | At expiry, until 24 h after: the same cut-off (`is_expired`) and the same minute cadence as the export, so it arrives when the user drops out of the file |

The hours before expiry are set in one place: `DEFAULT_REMINDER_HOURS_BEFORE` in
`bot/src/infrastructure/dobby/config.py` (72 and 24), overridable per owner with
`DOBBY_REMINDER_HOURS_BEFORE` (a JSON list, e.g. `[72, 24]`). The text says "in N days",
rounded from the hours.

Only current, active subscriptions of users who are not blocked (by an admin or by blocking the
bot) get reminders; trials get them with the trial wording, as in remnashop. Each reminder is
sent once per subscription period: what was sent is kept in Redis (persistent) under the
subscription id, its expiry and the reminder, so a restart does not repeat it and a renewal,
which moves the expiry, starts the reminders over. After downtime only the latest due reminder
is sent.

## What is stored where

| Data | Where |
|---|---|
| Users, roles, blocks, referrals, promo codes | Bot database, remnashop tables (`users`, `referrals`, `promocodes`, ...) |
| Subscriptions: login, status, expiry, plan snapshot, link | Bot database, `subscriptions`. `user_remna_id` is the user's login, sent outside |
| Plans and prices | Bot database, `plans` |
| Payments | Bot database, `transactions`; gateway keys encrypted in `payment_gateways` |
| Export state (last digest and time, lock) | Redis keys `dobby:export:*` |
| Payment check schedule | taskiq queue in Redis; nothing extra in the database |
| Server keys, subscription links, devices, traffic | Config-delivery service |

No new tables are needed: remnashop already keeps every subscription in its own database.

## Login

Each user has one login for life: the id of their first subscription. Every later subscription
of the user reuses it (`DobbyBackend._login_for`): a new purchase after expiry, a purchase after
an admin deleted the subscription, a promo or an admin-assigned plan. Renewals and plan changes
keep it anyway. So the subscription link, built from the login, never changes. The login is a
random UUID, so links cannot be guessed. It is lost only if the user record itself is deleted.

## Exported file

The full list of logins to serve, nothing else. A login is listed when the user's current
subscription is `ACTIVE`, not expired, and the user is not blocked. Disabled, deleted, expired
and blocked users are left out. The content is agreed; the JSON wrapper is a placeholder until
the exact syntax is.

```json
{
  "version": 2,
  "generated_at": "2026-11-20T12:00:00Z",
  "logins": ["65fd48d2-92a4-4bc1-9088-9e31f6c97aff"]
}
```

The format lives in `exporter.py` (`serialize_users`), the transport in `UserExporter`.
Today `FileUserExporter` writes the file atomically (temp file in the same folder, fsync,
rename) to `DOBBY_EXPORT_PATH`. An sftp exporter will implement the same `export(content)`
method: upload to a temp name, then rename on the server.

## Subscription links (placeholder)

`TemplateLinkProvider` builds the link from `DOBBY_SUBSCRIPTION_URL_TEMPLATE`, replacing `{id}`
with the login. It stays as the stub until it is decided how links reach the bot: built from
the login like this, or read from a file the config-delivery service sends back.

## What the owner configures

**`.env`, once at install** (`bot/.env.example` for remnashop's own settings):

| Variable | Meaning |
|---|---|
| `BOT_TOKEN`, `BOT_OWNER_ID`, `BOT_SUPPORT_USERNAME`, `DATABASE_PASSWORD`, `APP_CRYPT_KEY`, `BOT_SECRET_TOKEN` | As in remnashop |
| `APP_DOMAIN` | Optional. Only for webhook mode or payment notifications |
| `DOBBY_TELEGRAM_MODE` | `polling` (default) or `webhook` |
| `DOBBY_EXPORT_PATH` | Where the user file is written. Default `/opt/remnashop/data/users.json` |
| `DOBBY_SUBSCRIPTION_URL_TEMPLATE` | Link template with `{id}` |
| `DOBBY_FORCE_EXPORT_INTERVAL_SECONDS` | Forced export period, default 3600 |
| `DOBBY_REMINDER_HOURS_BEFORE` | Renewal reminders before expiry, hours, default `[72, 24]` |
| `DOBBY_DOWNLOAD_URL_IOS`, `_ANDROID`, `_WINDOWS`, `_MACOS` | Download links on "Download DobbyVPN" |
| `DOBBY_CONNECT_GUIDE_URL` | Optional video or page on how to connect; adds a button to "Connect DobbyVPN" |
| `DOBBY_REFERRAL_FRIEND_BONUS_DAYS` | Days the invited friend gets on their first payment, default 30 (0 = off) |
| `DOBBY_PURCHASE_WIDGET` | Choose and pay in one message widget (Bot API 10.4), default `true`. Used when exactly one payment gateway is active; otherwise, or when `false`, the usual screens |
| `REMNAWAVE_TOKEN`, `REMNAWAVE_WEBHOOK_SECRET` | Still required by remnashop's config validation; any value works. To be removed |

**Admin menu in the bot** (remnashop as is): plans, prices, trial, payment gateways and their
keys, texts, support contact, referral program, users (expiry, block, delete).

## Removed or idle remnashop features

Device (HWID) limits, traffic limits and resets, squads, the panel dashboard section, the
importer from other panels and panel webhooks have no effect: the config-delivery service owns
servers and connections. The code stays in place to keep the diff from upstream small; see
`docs/remnawave-map.md` for each call.

## Risks

- Telegram's bot terms (section 6.2) require Telegram Stars for digital goods sold in bots. A
  public statement from Telegram (17–18.11.2026) exempts VPN bots in authoritarian countries,
  but this is not in the official terms. Users, subscriptions and links therefore live in the
  owner's database and the config-delivery service, not in Telegram: losing the bot does not
  lose the subscriptions.
- During testing the YooKassa API did not answer through a VPN exit abroad and did answer
  without the VPN. To be checked from the owner's server; fallback: route only the payment
  client through a proxy in the payment provider's country.
- Payment providers other than YooKassa still rely on notifications and need a domain.

## Not decided yet

- Exact file syntax (the content, a list of logins, is agreed), sftp address, folder and user,
  and whether the service re-reads the file right away.
- How links reach the bot: a template from the login, or a file back from the
  config-delivery service.
- Whether links can be rotated. With one login for life, "reissue link" needs a separate
  token in the link; it is a no-op for now.
- Whose config-delivery service other StarterKit owners use.
- Whether to remove the panel-only screens and the `remnapy` dependency, or keep the diff small.
