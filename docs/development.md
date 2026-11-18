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

Telegram and payment providers deliver updates by webhook, so the bot needs a public HTTPS
address. Locally a tunnel to `127.0.0.1:5055` works, for example:

```bash
cloudflared tunnel --url http://127.0.0.1:5055
```

Put the tunnel host (without `https://`) into `APP_DOMAIN`, then:

```bash
docker compose -f docker-compose.local.yml up
```
