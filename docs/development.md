# Development

Everything runs in Docker from `bot/`.

```bash
cd bot
cp .env.example .env && make setup-env   # then fill in BOT_TOKEN, BOT_OWNER_ID, ...
docker compose -f docker-compose.local.yml build
```

## Tests

Unit tests, no services needed:

```bash
docker compose -f docker-compose.local.yml run --rm --no-deps \
  -v ./tests:/opt/remnashop/tests remnashop pytest -q tests
```

End-to-end test on a real Postgres (trial, add days, disable, block, delete → exported file).
It uses fake bot settings, so it does not need a real token:

```bash
docker compose -f docker-compose.local.yml up -d remnashop-db remnashop-redis
docker compose -f docker-compose.local.yml run --rm \
  -e DOBBY_DB_TESTS=1 -e BOT_TOKEN=123456:TESTnotarealtokenTESTnotarealtoken \
  -e BOT_OWNER_ID=1 -e BOT_SUPPORT_USERNAME=support_test -e APP_DOMAIN=example.invalid \
  -v ./tests:/opt/remnashop/tests remnashop sh -c \
  'alembic -c src/infrastructure/database/alembic.ini upgrade head && pytest -q tests'
```

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
