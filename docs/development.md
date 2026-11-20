# Development

Everything runs in Docker from `bot/`.

```bash
cd bot
cp .env.example .env && make setup-env   # then fill in BOT_TOKEN, BOT_OWNER_ID, ...
docker compose -f docker-compose.local.yml build
```

## Tests

Tests and one-off commands run in their own stack (`docker-compose.test.yml`): a throwaway
Postgres and Redis and the dummy settings in `.env.test`. They never read the real `.env`, so
they never reach Telegram, YooKassa or the running bot's database.

```bash
docker compose -f docker-compose.test.yml -p starterkit-test run --rm tests
docker compose -f docker-compose.test.yml -p starterkit-test down -v
```

Any other one-off command the same way, for example lint:

```bash
docker compose -f docker-compose.test.yml -p starterkit-test run --rm --no-deps tests \
  ruff check src tests
```

Only the long-running bot (`docker-compose.local.yml`) uses the real `.env`. Keep
`UVICORN_RELOAD_ENABLED=false` there: every reload restarts the bot, and each start and stop
sends the owner a notification.

## Running the bot locally

No domain and no tunnel are needed: by default the bot polls Telegram
(`DOBBY_TELEGRAM_MODE=polling`) and confirms YooKassa payments by asking its API. Leave
`APP_DOMAIN` empty and run:

```bash
docker compose -f docker-compose.local.yml up
```

Webhook mode (`DOBBY_TELEGRAM_MODE=webhook`) needs `APP_DOMAIN` and HTTPS in front of
`127.0.0.1:5055`.

To deliver a YooKassa notification by hand (for example to test the optional webhook path),
post the payment id; the bot reads the status from the API, not from the body:

```bash
curl -X POST http://127.0.0.1:5055/api/v1/payments/yookassa \
  -H 'Content-Type: application/json' \
  -d '{"type":"notification","event":"payment.succeeded","object":{"id":"<payment id>"}}'
```
