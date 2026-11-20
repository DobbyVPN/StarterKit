# StarterKit

A Telegram bot and backend for selling VPN subscriptions on DobbyVPN.

- `bot/` — the bot, based on remnashop (MIT, see `bot/UPSTREAM.md`), with the Remnawave panel
  replaced by the DobbyVPN backend module in `bot/src/infrastructure/dobby/`.
- `docs/architecture.md` — how it fits together and what is not decided yet.
- `docs/remnawave-map.md` — every Remnawave call in remnashop and what replaced it.
- `docs/development.md` — build, tests, local run.

## License

The repository is licensed under Apache-2.0 (`LICENSE`). The code in `bot/` is a copy of
remnashop and stays under its MIT license (`bot/LICENSE`, source and commit in
`bot/UPSTREAM.md`).
