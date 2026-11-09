# Climate Alert System: Technical Specification

**Status:** final · **Date:** 2026-11-08 · **Implements:** [`PRD.md`](PRD.md)

This document says how the system is built. The schema and its diagrams are in [`ModeloDeDatos.md`](../ModeloDeDatos.md). The reasoning behind each choice is in [`Decisiones.md`](../Decisiones.md). Neither is repeated here.

## 1. Runtime

Four processes run from one image, against one PostgreSQL database.

| Process | Entry point | Lifetime |
|---------|-------------|----------|
| `migrate` | `alembic upgrade head` | One-shot |
| `seed` | `python -m climate_alerts.agrobot.seed` | One-shot |
| `api` | `uvicorn --factory climate_alerts.api.app:create_app` | Long-running |
| `worker` | `python -m climate_alerts.worker` | Long-running |

Docker Compose starts them in this order: `db` healthy (`pg_isready`), then `migrate`, then `seed`, then `api` and `worker`, using `service_healthy` and `service_completed_successfully`. All containers use exec-form commands so they receive `SIGTERM`.

## 2. Stack

| Component | Version | Notes |
|-----------|---------|-------|
| Python | 3.14 | 3.13 left bugfix support on 2026-10-01. asyncpg, SQLAlchemy and greenlet ship `cp314` wheels |
| FastAPI, uvicorn | 0.162, 0.54 | |
| SQLAlchemy | 2.1 with the `asyncio` extra | Since 2.1, `greenlet` is not installed by default |
| asyncpg | 0.32 | |
| Alembic | 1.20 | |
| APScheduler | `>=3.11,<4` | |
| PyJWT | 2.15 | |
| pydantic-settings | 2.16 | |
| structlog | 26 | |
| Tooling | uv, ruff, mypy (strict) | |
| Tests | pytest, pytest-asyncio 1.4, testcontainers 4.15, httpx, asgi-lifespan | httpx does not yet declare 3.14 support. It is test-only and confirmed by the suite running |

## 3. Repository layout

Organized by feature. There is no repository layer: services use `AsyncSession` directly, and every query is built with SQLAlchemy's 2.0-style expression language (section 5).

```text
src/climate_alerts/
  config.py              Settings
  db.py                  Engine and session factory, declarative base, naming convention
  logs.py                structlog setup
  auth.py                Token verification
  worker.py              Scheduler, signals, shutdown
  api/
    app.py               create_app, lifespan, routers
    dependencies.py      Session, current user, shared request types
    schemas.py           ErrorOut and the declared error responses
    health.py            Liveness and readiness
  agrobot/               Mock of the pre-existing system
    models.py            User, Field, ClimateEventType, WeatherForecast
    router.py  schemas.py  service.py  Event type catalog
    tokens.py            Token issuing, standing in for the real login system
    seed.py              One-shot seed command
  alerts/
    models.py  schemas.py  service.py  router.py
  notifications/
    models.py  schemas.py  service.py  router.py
    evaluation.py        Evaluation statement and chunk loop
    dispatch.py          Claim, send, record outcomes
    senders.py           Sender protocol, simulated senders, registry
migrations/              Alembic, async template
scripts/                 License audit and volume benchmark
collection/              Postman collection and environment, importable into Bruno
tests/
```

## 4. Configuration

`Settings` extends `BaseSettings`. Field names map to case-insensitive environment variables with no prefix.

**Required, no default.** The application refuses to start without them.

| Variable | Rule |
|----------|------|
| `DATABASE_URL` | `postgresql+asyncpg://...` |
| `JWT_SECRET` | At least 32 characters |

**Optional.**

| Variable | Default | Why |
|----------|---------|-----|
| `JWT_ALGORITHM` | `HS256` | Pinned by configuration. One of `HS256`, `HS384` or `HS512` |
| `EVALUATION_INTERVAL_SECONDS` | 300 | The measured worst case takes about 11 s |
| `EVALUATION_CHUNK_SIZE` | 5000 | Benchmark |
| `NOTIFICATION_CHANNELS` | `whatsapp` | Comma-separated. Keeps measured volumes valid |
| `DISPATCH_INTERVAL_SECONDS` | 5 | Upper bound on delivery latency when idle |
| `DISPATCH_BATCH_SIZE` | 500 | Benchmark |
| `DISPATCH_CONCURRENCY` | 40 | Reaches WhatsApp's default 80 messages per second if a send takes 500 ms. That latency is an assumption |
| `DISPATCH_SEND_TIMEOUT_SECONDS` | 10 | Every remote call needs a timeout |
| `DISPATCH_LEASE_SECONDS` | 300 | Must exceed the slowest batch: 500 / 40 × 10 s = 125 s |
| `DISPATCH_MAX_ATTEMPTS` | 8 | With the backoff below, retries span 1 to 2 hours, outlasting a provider outage of about an hour |
| `DISPATCH_BACKOFF_BASE_SECONDS` | 60 | |
| `DISPATCH_BACKOFF_CAP_SECONDS` | 3600 | |
| `DEFAULT_PAGE_SIZE`, `MAX_PAGE_SIZE` | 50, 200 | |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | 5, 5 | |
| `DB_COMMAND_TIMEOUT_SECONDS` | 60 | Without it, a query on a vanished host never returns, and `max_instances=1` then skips every later run |
| `WORKER_SHUTDOWN_TIMEOUT_SECONDS` | 25 | Below Compose's `stop_grace_period: 30s` |
| `SEED_TOKEN_TTL_HOURS` | 24 | Reviewer convenience. Production tokens would be short-lived |
| `LOG_LEVEL` | `INFO` | One of `CRITICAL`, `ERROR`, `WARNING`, `INFO` or `DEBUG` |

Startup also validates that every enabled channel has a registered sender. Counts, sizes, timeouts and intervals must be positive (`DB_MAX_OVERFLOW` may be 0), `DEFAULT_PAGE_SIZE` may not exceed `MAX_PAGE_SIZE`, and `DISPATCH_LEASE_SECONDS` must exceed `DISPATCH_BATCH_SIZE / DISPATCH_CONCURRENCY × DISPATCH_SEND_TIMEOUT_SECONDS`.

## 5. Data layer

- One `AsyncEngine` per process, created in the lifespan or worker startup and disposed on shutdown.
- `async_sessionmaker(engine, expire_on_commit=False)`.
- One `AsyncSession` per task, never shared between concurrent tasks. SQLAlchemy documents sharing as unsafe.
- All timestamps come from the database clock (`now()`), including retry times.
- `MetaData(naming_convention=...)` for `ix`, `uq`, `ck`, `fk` and `pk`.

### Queries

Application queries use SQLAlchemy's 2.0-style expression language: `select()`, `insert()`, `update()`, `session.execute()` and `session.scalars()`. Models use `DeclarativeBase`, `Mapped[]` and `mapped_column()`.

Not allowed in application code: `text()`, `session.query()`, `declarative_base()`, `select([...])` and any other legacy or deprecated API. Ruff bans the legacy imports through `TID251`, a unit test rejects `.query(` calls, and pytest turns deprecation warnings into errors. Migrations and tests may use `text()`.

PostgreSQL features come from the same language: `postgresql.insert(...).from_select(...).on_conflict_do_nothing(constraint=...)`, `with_for_update(skip_locked=True)`, `update(...).returning(...).cte(...)`, and `func.unnest(...).table_valued("channel").render_derived(name="c")`.

`ModeloDeDatos.md` shows these statements as SQL because they are easier to read that way.

### Migrations

One hand-written initial revision creates all six tables, inserts the event catalog, and has a complete downgrade.

Autogenerate cannot be trusted for this schema: since Alembic 1.19.2 it does not detect `CHECK` constraints by default, and it does not compare a changed `WHERE` on an existing partial index. `alembic check` still guards tables and columns. Behaviour tests guard the constraints it can't see (section 11).

Catalog: `frost` (Helada), `rain` (Lluvia), `hail` (Granizo), `snow` (Nieve), `thunderstorm` (Tormenta), `heat_wave` (Ola de calor), `strong_wind` (Viento fuerte).

## 6. Authentication

```python
CurrentUserId = Annotated[int, Depends(get_current_user_id)]
```

`get_current_user_id` uses `HTTPBearer`, which returns `401` when the header is missing, and calls:

```python
jwt.decode(token, secret, algorithms=[algorithm], options={"require": ["exp", "sub"]})
```

`sub` is a string in the JWT and must match `[1-9][0-9]*` and fit a `BIGINT`. Any `InvalidTokenError` or unparseable `sub` returns `401` with `WWW-Authenticate: Bearer`. The dependency does not query the database: a valid token for a user with no data simply sees nothing.

## 7. API

Errors use FastAPI's default `{"detail": ...}` format. A validation error is a `422` whose `detail` is a list of objects containing `type`, `loc` and `msg`, including the checks made at runtime (an unknown event, `limit` above the maximum). Other errors carry a string `detail` and are declared in the OpenAPI document as `ErrorOut`. Business routes are under `/api/v1` and require a token.

| Method and path | Success | Errors |
|-----------------|---------|--------|
| `POST /fields/{field_id}/alerts` | `201` `AlertOut` | `404` field, `409` duplicate, `422` |
| `GET /alerts?field_id=` | `200` `list[AlertOut]` | |
| `GET /alerts/{id}` | `200` `AlertOut` | `404` |
| `PATCH /alerts/{id}` | `200` `AlertOut` | `404`, `422` |
| `DELETE /alerts/{id}` | `204` | `404` |
| `GET /notifications` | `200` `NotificationPage` | `422` |
| `GET /climate-event-types` | `200` `list[EventTypeOut]` | |
| `GET /health/live` | `200` | |
| `GET /health/ready` | `200` | `503` |

**Schemas**

| Schema | Fields |
|--------|--------|
| `AlertCreate` | `event_type: str`, `threshold: Decimal` (greater than 0, at most 100, two decimals) |
| `AlertUpdate` | `threshold: Decimal`, same rules |
| `AlertOut` | `id`, `field_id`, `event_type`, `threshold`, `created_at`, `updated_at` |
| `NotificationOut` | `id`, `alert_id`, `field_id`, `event_type`, `forecast_date`, `channel`, `probability`, `threshold`, `status`, `attempts`, `last_error`, `created_at`, `sent_at` |
| `NotificationPage` | `items: list[NotificationOut]`, `next_cursor: int \| None` |

**Creating an alert.** Check the field belongs to the user (else `404`). Check the event exists (else `422`). Then `INSERT ... ON CONFLICT (field_id, event_type) WHERE deleted_at IS NULL DO NOTHING RETURNING *`. No row returned means `409`. This avoids parsing driver exceptions and is safe under concurrent requests.

**Ownership.** Every statement joins through `fields.owner_id`. Deleted alerts are invisible, so a second `DELETE` returns `404`.

**Pagination.** Newest first by `id`. `cursor` is the last `id` seen, and the query fetches `limit + 1` rows to compute `next_cursor`. `limit` above `MAX_PAGE_SIZE` returns `422`.

## 8. Evaluation

`notifications/evaluation.py` builds the statement described in `ModeloDeDatos.md`, with `channels` bound as `bindparam("channels", channels, type_=ARRAY(Text))`.

```text
after_id = 0
while not stopping:
    up_to_id = highest id of the next CHUNK_SIZE live alerts after after_id
    if none: stop
    in one transaction: run the statement for (after_id, up_to_id]
    on database error: log it, count a failed chunk, and continue
    after_id = up_to_id
```

`up_to_id` is read in its own step so a failing chunk can be skipped. A chunk that always fails cannot block the alerts after it. It is retried on the next run.

## 9. Dispatch

`notifications/dispatch.py` repeats this until nothing is due or the worker is stopping:

1. **Claim** a batch in one short transaction, using the statement in `ModeloDeDatos.md`.
2. **Send** concurrently, limited by a semaphore, with no transaction open. Each send has its own timeout.
3. **Record** all outcomes in one short transaction.

| Outcome | Update |
|---------|--------|
| Sent | `status = 'sent'`, `sent_at = now()`, `last_error = NULL` |
| `PermanentDeliveryError` | `status = 'failed'`, `last_error` set |
| Other error or timeout, attempts below the maximum | `next_attempt_at = now() + delay`, `last_error` set |
| Other error or timeout, attempts at the maximum | `status = 'failed'`, `last_error` set |
| No sender registered for the channel | Same as other error, with the same `last_error` |
| Claimed with attempts above the maximum | `status = 'failed'` without sending. Earlier leases expired without a result |

```text
delay = min(cap, base × 2^(attempts - 1)) × uniform(0.5, 1.0)
```

**Fencing.** Every outcome update includes `WHERE id = :id AND attempts = :attempts AND status = 'pending'`. A dispatcher whose lease expired cannot overwrite the result of the one that took over.

`last_error` is truncated to 500 characters and contains only messages we control.

**Reprocessing.** A `failed` notification can be requeued with one `UPDATE`, documented in the README. There is no endpoint.

### Senders

```python
@dataclass(frozen=True)
class Recipient:
    phone: str
    email: str | None


@dataclass(frozen=True)
class OutboundNotification:
    notification_id: int  # idempotency key for real providers
    channel: str
    recipient: Recipient
    field_name: str
    event_type: str
    forecast_date: date
    probability: Decimal
    threshold: Decimal


class NotificationSender(Protocol):
    channel: str

    async def send(self, notification: OutboundNotification) -> None: ...
```

Each sender chooses its own address. A missing address raises `PermanentDeliveryError`.

Two simulated senders are registered, `whatsapp` and `email`. They log a structured event with the recipient masked. Compose enables both, so a reviewer sees per-channel rows and a `failed` row for a user without email. The code default remains `whatsapp`.

## 10. Worker lifecycle

`AsyncIOScheduler` must start inside a running loop. Each job is added with `trigger="interval"`, `max_instances=1`, `coalesce=True` and an immediate first run. The jobs live in code, with no persistent store.

APScheduler's asyncio executor **cancels** running jobs on shutdown and cannot wait for them, even with `wait=True`. The worker therefore drains them itself:

1. On `SIGTERM` or `SIGINT` (`loop.add_signal_handler`), set a `stopping` event and pause the scheduler.
2. Evaluation and dispatch finish their current chunk or batch, then exit.
3. Wait for in-flight jobs up to `WORKER_SHUTDOWN_TIMEOUT_SECONDS`.
4. Call `scheduler.shutdown(wait=False)`, which cancels anything still running, and dispose the engine.

Anything interrupted is recovered: evaluation by rollback, dispatch by lease expiry.

## 11. Testing

Tests run on real PostgreSQL 17 through `testcontainers`. pytest-asyncio uses `asyncio_mode = "auto"` with session loop scope for tests and fixtures, so the engine stays on one loop. The schema is created by running the real migration once. Tables are truncated between tests, because concurrency tests need real commits.

| Layer | Covers |
|-------|--------|
| Unit | Backoff, token verification, settings validation, channel registry |
| API (httpx `ASGITransport`, `LifespanManager`) | Every endpoint, status code, ownership and pagination |
| Evaluation | Boundary, deduplication, revisions, timezones, chunks, soft delete, channels |
| Evaluation under concurrency | Several evaluators at once: no duplicates, no deadlocks |
| Plan regression | `EXPLAIN` of the evaluation statement contains no `Anti Join`, with the table analyzed while empty |
| Dispatch | All outcomes, retries, backoff, fencing, per-channel independence, missing address |
| Dispatch under concurrency | Several dispatchers: each row claimed once per lease |
| Crash recovery | A claimed row with an abandoned lease is reclaimed after expiry |
| Worker | `SIGTERM` stops a subprocess cleanly within the timeout |
| Migrations | Upgrade from empty, `alembic check`, downgrade to empty, upgrade again |
| Constraints | Each rule in `ModeloDeDatos.md`, including the ones autogenerate can't see |
| Seed | Idempotent: running twice leaves the same users, fields and alerts |
| End to end | Seed, evaluation, dispatch with the simulated senders, then the API history |

**Outside the test suite, run once and recorded in `Decisiones.md`:** the volume benchmark on the final statements at 24,000 farms, and the full-dependency license audit.

## 12. Seed

`python -m climate_alerts.agrobot.seed`, in one transaction, deterministic through `random.Random` with a fixed seed.

| Data | Content |
|------|---------|
| Users | 3 users. Two have an email and one does not |
| Fields | 5, across Buenos Aires, Montevideo, Bogotá, Mexico City and São Paulo. One user owns several |
| Forecasts | 16 days from today, for each field and event, between 0 and 40 |
| Alerts | A frost alert at 70 on each field, and a rain alert at 80 on one |
| Scenario | Frost at 85 tomorrow on a field whose owner has an email, and at 90 in two days on the field of the user without one |

Upserts use natural keys: `users.phone`, the owner and name of a field, the alert's partial unique index, and the forecast primary key. It prints each user's id, field ids and JWT.

## 13. Observability

JSON logs through structlog, with no phone numbers or emails. Standard library loggers, including `uvicorn*` and `apscheduler`, go through the same pipeline. `apscheduler` is limited to warnings, because at `INFO` it writes two lines per job per interval.

| Event | Fields |
|-------|--------|
| `evaluation.completed` | `created`, `chunks`, `failed_chunks`, `duration_ms` |
| `dispatch.completed` | `claimed`, `sent`, `retried`, `failed`, `duration_ms` |
| `worker.stopping`, `worker.stopped` | `drained` |

## 14. Container

Multi-stage build with uv on `python:3.14-slim`, running as a non-root user. Compose sets `stop_grace_period: 30s` on the worker, `restart: unless-stopped` on `api` and `worker`, a healthcheck on `api` through `/health/ready`, publishes the API on `127.0.0.1` only because `JWT_SECRET` in that file is public, and sets clearly labelled development-only values for `JWT_SECRET` and the database password directly in `compose.yaml`, so `docker compose up` needs no setup. The application code still has no default for either.

## 15. Delivery of the work

- Initialize git at the start of implementation. Conventional commits only, with no AI attribution.
- Implementation order: project skeleton, schema and migration, auth and API, evaluation, dispatch, worker, seed, Compose, API collection, verification, documentation.
- Move the license audit and benchmark scripts from the scratchpad into `scripts/`.
- Update `README.md`, `procedimiento.md` and `Decisiones.md` as each part lands.

Out of scope, not needed by the statement: CI pipelines and a published container image.

## 16. Manual API collection

`collection/climate-alerts.postman_collection.json` (Collection v2.1) and `collection/local.postman_environment.json`. It is the one format both tools open: Postman natively, and Bruno through its importer, which translates `pm.test` and `pm.environment`. Scripts stay simple to survive that translation, and environment values are compared as strings because Bruno imports them that way.

Environment variables: `base_url`, `token_user_1` and `token_user_2` (pasted from the seed output), `field_id`, `other_user_field_id`, and `alert_id` (captured when the alert is created).

Requests run in order, in numbered folders, each asserting its status code:

| Folder | Requests |
|--------|----------|
| 1. Health | Liveness, readiness |
| 2. Authentication | No token returns `401`, invalid token returns `401` |
| 3. Catalog | List event types |
| 4. Alerts | Create, duplicate returns `409`, unknown event returns `422`, read, list, change threshold |
| 5. Ownership | Another user reading the alert or creating on the field returns `404` |
| 6. Notifications | List, filter by channel, filter `failed` as the user without email, paginate |
| 7. Cleanup | Delete returns `204`, deleting again returns `404` |

Verification: Newman runs the collection against a clean `docker compose up`, and `@usebruno/converters` confirms the collection converts without skipped items.

## 17. Requirement traceability

| Requirements | Module | Verified in |
|--------------|--------|-------------|
| FR-1 to FR-7 | `alerts/`, `agrobot/models.py` | API tests |
| FR-8 to FR-12, FR-26 | `notifications/evaluation.py`, `worker.py` | Evaluation tests, plan regression |
| FR-13 to FR-17, FR-27, FR-28 | `notifications/dispatch.py`, `senders.py` | Dispatch tests, crash recovery |
| FR-18 | `notifications/router.py`, `notifications/service.py` | API tests |
| FR-19, FR-20 | `auth.py`, every router | API tests |
| FR-21 | `api/health.py` | API tests |
| FR-22 to FR-24 | `agrobot/seed.py` | Seed tests |
| FR-25 | `compose.yaml`, `Dockerfile` | Manual run on a clean checkout |
| FR-29 | `collection/` | Newman run, Bruno conversion |
