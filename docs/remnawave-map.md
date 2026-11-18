# Remnawave calls in remnashop

Every place where remnashop talks to the Remnawave panel, and what StarterKit does with it.
Upstream commit: see `bot/UPSTREAM.md`. Paths are relative to `bot/src/`.

remnashop reaches the panel in three ways:

1. The `Remnawave` protocol (`application/common/remnawave.py`), implemented by `RemnawaveImpl`
   (`infrastructure/services/remnawave.py`) on top of the `remnapy` SDK. StarterKit replaces the
   implementation with `DobbyBackend` (`infrastructure/dobby/backend.py`).
2. Direct `RemnawaveSDK` calls that bypass the protocol.
3. Webhooks from the panel (`web/endpoints/remnawave.py` → `application/services/remnawave.py`).

## 1. `Remnawave` protocol

| Method | Called from | What remnashop does | What it reads back | Needed | DobbyBackend |
|---|---|---|---|---|---|
| `try_connection` | `lifespan.py` | Checks panel version at startup; on error sends an admin alert, bot keeps running | `Version` | No | Returns min supported version |
| `create_user` | `subscription/commands/purchase.py` (trial, new purchase), `subscription/commands/set_plan.py` (admin sets plan), `promocode/commands/activate.py` (promo gives a subscription), `subscription/commands/sync.py`, `remnawave/commands/synchronization.py` | Creates a panel user before the bot stores the subscription | `uuid`, `status`, `expire_at`, `subscription_url` → saved as `Subscription.user_remna_id`, `status`, `expire_at`, `url` | **Yes** | New UUID (or the subscription's), expiry from plan, link from template; triggers export |
| `update_user` | `purchase.py` (renew, change plan, trial → paid), `set_plan.py`, `promocode/.../activate.py` (days, traffic, devices), `subscription/commands/management.py` (admin: add days, traffic and device limits, squads) , `sync.py` | Pushes new expiry, limits and enabled/disabled status | `uuid`, `status`, `expire_at`, `subscription_url` | **Yes** | Echoes the data, keeps the UUID; triggers export |
| `enable_user` / `disable_user` | `management.py` `ToggleSubscriptionStatus` (admin) | Enables or disables access | — | **Yes** | Triggers export (disabled users are left out of the file) |
| `delete_user` | `management.py` `DeleteSubscription` (admin), `sync.py` | Deletes the panel user | `bool` | **Yes** | Triggers export |
| `get_user_by_uuid` | `sync.py` (consistency check), `user/queries/profile.py` (admin subscription screen; raises if `None`), `web/endpoints/public/subscription.py` (web cabinet) | Reads the panel's view of a subscription | `uuid`, `status`, `expire_at`, `subscription_url`, limits, `used_traffic_bytes`, `online_at`, `last_connected_node_uuid`, squads | Yes (admin screens) | Built from the bot database; traffic, node and squad fields empty |
| `get_users_by_telegram_id` | `sync.py` | Finds panel users for a Telegram account | list of the above | Yes (sync screens) | From the bot database |
| `get_users_by_email` | not called | — | — | No | From the bot database |
| `get_all_users` | `remnawave/commands/synchronization.py` | Imports all panel users into the bot | list | No | `[]` |
| `get_devices` | `user/queries/profile.py`, `telegram/routers/menu/getters.py` (user's "devices" screen), `web/.../subscription.py` | Lists HWID devices | `HwidDeviceDto` list | No | `[]` |
| `delete_device`, `delete_all_devices` | `remnawave/commands/management.py` (user resets devices) | Removes HWID devices | remaining count | No | No-op |
| `drop_connections` | same | Kicks live connections | — | No | No-op |
| `reset_traffic` | `remnawave/commands/management.py` (admin), `update_user(reset_traffic=True)` on purchase | Resets used traffic | — | No | No-op |
| `revoke_subscription` | `remnawave/commands/management.py` (user or admin "reissue link") | Rotates the subscription link | — | Later | Logs a warning; needs a link-rotation call in the config-delivery service |
| `get_squads_available`, `get_internal_squads`, `get_external_squads` | `plan/commands/squads.py`, `plan/queries/squads.py`, `remnawave/queries/squads.py`, dashboard plan getters | Lets an admin bind plans to server groups (squads) | `SquadInfoDto` list | No | `False` / `[]` |
| `apply_sync` | `remnawave/commands/synchronization.py` | Copies fields from a panel user into the bot subscription | — | No | Same field copy, local only |

## 2. Direct `RemnawaveSDK` calls

These still go to `REMNAWAVE_HOST` and fail without a panel. Each caller is listed with how it
behaves today.

| Call | File | Feature | Without a panel | Needed |
|---|---|---|---|---|
| `users.disable_user` / `users.enable_user` | `subscription/commands/management.py` `DisableTrialSubscription`, `EnableTrialSubscription` | Trial is paused when the user leaves a required channel | Fixed in StarterKit: goes through the `Remnawave` protocol, so `DobbyBackend` handles it | Yes |
| `nodes.get_one_node`, `external_squads.get_external_squad_by_uuid` | `user/queries/profile.py` | Admin subscription screen: last node, squad | Not called: the adapter returns no node or squad | No |
| `users.create_user` | `taskiq/tasks/importer.py` | Import users from other panels into Remnawave | Fails | No |
| `internal_squads.get_internal_squads` | `telegram/routers/dashboard/importer/*` | Importer screens | Fails | No |
| `users.get_users_by_telegram_id`, `internal_squads.get_internal_squads` | `telegram/routers/dashboard/users/user/getters.py` `sync_getter` | Admin "sync with panel" screen | Fails | No |
| `system.get_stats`, `system.get_metadata`, `hosts.get_all_hosts`, `nodes.get_all_nodes`, `inbounds.get_all_inbounds` | `telegram/routers/dashboard/remnawave/getters.py` | Admin "Remnawave" dashboard section | Fails | No (later: our own server status screen) |

## 3. Webhooks from the panel

`POST /api/v1/remnawave`, verified with `REMNAWAVE_WEBHOOK_SECRET`. Without a panel nothing is sent.

| Event | What remnashop does | Needed | Replacement |
|---|---|---|---|
| `user.expires_in_72/48/24_hours` | Renewal reminder to the user | **Yes** | Own scheduled task over `Subscription.expire_at` (not done yet) |
| `user.expired`, `user.expired_24_hours_ago` | "Subscription expired" message | **Yes** | Same scheduled task (not done yet) |
| `user.created`, `user.modified`, `user.deleted`, `user.enabled`, `user.disabled`, `user.revoked`, `user.limited` | Syncs panel changes back into the bot | No | The bot is the source of truth |
| `user.first_connected`, `user.not_connected` | Admin event; nudges a user who never connected | Later | Needs connection data from the config-delivery service |
| `user_hwid_devices.*` | Device added/removed notifications | No | — |
| `node.connection_lost/restored`, `node.traffic_notify` | Admin alerts about servers | Later | StarterKit's own server monitoring (see plan) |
| `torrent_blocker.report` | Torrent block notice | No | — |

## What remnashop expects back

- **A stable id per subscription** (`uuid`). It is stored in `subscriptions.user_remna_id` and
  used to find the subscription again. `DobbyBackend` issues it with `uuid4()` and keeps it
  across renewals and plan changes. This is the id exported to the config-delivery service.
- **`subscription_url`**, stored in `subscriptions.url` and shown behind the "Connect" button.
- **`expire_at`**: for a plan of N days the panel returned now + N days (0 days = year 2099,
  "unlimited"). Renewal computes the new date in the bot and sends it to the panel.
- **`status`**: `ACTIVE` or `DISABLED`.
- Errors: `update_user` raises if the user does not exist; `delete_user` returns `False`.
  `DobbyBackend` never raises for these because the bot database is authoritative.
