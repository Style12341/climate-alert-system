# Climate Alert System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the climate alert system described in the PRD: alert configuration API, periodic evaluation, durable multi-channel delivery, mock data and deliverables.

**Architecture:** One codebase and image run `api` and `worker` processes against one PostgreSQL database. Evaluation is one set-based statement per chunk of alerts. Delivery uses the `notifications` row as an outbox with a lease held in `next_attempt_at`. Correctness comes from database constraints, not worker coordination.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2.1 (async, asyncpg), Alembic, APScheduler 3.x, PyJWT, pydantic-settings, structlog, pytest with testcontainers.

**Spec:** [`SPEC.md`](SPEC.md), which implements [`PRD.md`](PRD.md). Schema and queries: [`ModeloDeDatos.md`](../ModeloDeDatos.md). Rationale: [`Decisiones.md`](../Decisiones.md). Executors read all of them.

## Global Constraints

- Python 3.14. Versions as in SPEC section 2. APScheduler pinned `>=3.11,<4`.
- Application queries use SQLAlchemy's 2.0-style expression language only. No `text()`, `session.query()`, `declarative_base()` or `select([...])`. Migrations and tests may use `text()`.
- All I/O is async. One `AsyncSession` per task, never shared between concurrent tasks.
- Every operating value comes from `Settings`, with the defaults in SPEC section 4. Secrets have no default.
- Code, identifiers and comments in English. `README.md`, `procedimiento.md`, `Decisiones.md` and `ModeloDeDatos.md` in Spanish.
- No phone numbers or emails in logs.
- No abstraction beyond `NotificationSender`. No repository layer.
- Integration tests run on real PostgreSQL 17. Nothing is mocked at the database boundary.
- Conventional commits with no `Co-Authored-By` or any AI attribution.
- Commands run from the repository root. Each task ends with `uv run ruff check . && uv run mypy src && uv run pytest` passing.

## Review Focus

1. A database outage or any exception during a scheduled run must not kill the worker. The next run recovers. Test in Task 8.
2. Integers beyond `bigint` in a token `sub`, path id or `cursor` must return `401` or `422`, never `500`. Tests in Tasks 3, 4 and 7.
3. Two simultaneous requests creating the same alert must return exactly one `201` and one `409`. Test in Task 4.
4. A sender that hangs must time out and be retried without stalling the rest of its batch. Test in Task 6.
5. `NOTIFICATION_CHANNELS` with spaces, repeats or only separators must be normalized or rejected at startup. Test in Task 1.

---

### Task 1: Project skeleton and settings

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/climate_alerts/__init__.py`, `src/climate_alerts/config.py`, `src/climate_alerts/logs.py`
- Test: `tests/unit/test_settings.py`, `tests/unit/test_query_style.py`

**Interfaces:**
- Produces: `Settings(BaseSettings)` with one field per variable in SPEC section 4, in lowercase. `Settings.enabled_channels -> tuple[str, ...]`. `get_settings() -> Settings` (cached). `configure_logging(level: str) -> None`.

- [ ] **Step 1: Initialize the repository and configuration**

Run `git init -b main`, then create `pyproject.toml` for uv with the `src` layout and the dependencies in SPEC section 2. Use `testcontainers[postgres]` for tests and import from `testcontainers.community.postgres`, because the older path emits a `DeprecationWarning`. Configure:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "session"
asyncio_default_test_loop_scope = "session"
filterwarnings = ["error"]
testpaths = ["tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "ASYNC", "S", "RUF", "TID251"]

[tool.ruff.lint.flake8-tidy-imports.banned-api]
"sqlalchemy.text".msg = "Use the SQLAlchemy expression language."
"sqlalchemy.orm.Query".msg = "Legacy API. Use select()."
"sqlalchemy.orm.declarative_base".msg = "Use DeclarativeBase."

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101", "TID251"]
"migrations/**" = ["TID251"]
```

Any third-party warning that must be ignored gets a narrow `filterwarnings` entry with a comment giving the reason.

- [ ] **Step 2: Write the failing tests**

| Test | Assertion |
|------|-----------|
| `test_requires_database_url_and_secret` | With both unset, `Settings(_env_file=None)` raises `ValidationError` |
| `test_rejects_short_secret` | `JWT_SECRET="x" * 31` raises `ValidationError` |
| `test_defaults_match_spec` | `evaluation_interval_seconds == 300`, `evaluation_chunk_size == 5000`, `dispatch_interval_seconds == 5`, `dispatch_batch_size == 500`, `dispatch_concurrency == 40`, `dispatch_send_timeout_seconds == 10`, `dispatch_lease_seconds == 300`, `dispatch_max_attempts == 8`, `dispatch_backoff_base_seconds == 60`, `dispatch_backoff_cap_seconds == 3600`, `default_page_size == 50`, `max_page_size == 200`, `jwt_algorithm == "HS256"`, `enabled_channels == ("whatsapp",)` |
| `test_environment_overrides_default` | `EVALUATION_CHUNK_SIZE=10` gives `evaluation_chunk_size == 10` |
| `test_channels_are_normalized` | `" whatsapp, email ,whatsapp"` gives `("whatsapp", "email")` |
| `test_empty_channels_rejected` | `" , "` raises `ValidationError` |
| `test_no_legacy_query_calls` | Walking `src/` with `ast` finds no attribute call named `query` |

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/unit -v`
Expected: FAIL with `ModuleNotFoundError: climate_alerts.config`

- [ ] **Step 4: Implement `Settings`, `get_settings` and `configure_logging`**

`notification_channels: str = "whatsapp"` stays a plain string, and `enabled_channels` parses it, since comma-separated lists are not the pydantic-settings default. `jwt_secret` is `SecretStr` with `min_length=32`. Logging renders JSON through structlog.

- [ ] **Step 5: Run tests to verify they pass, then commit**

Run: `uv run pytest tests/unit -v`
Expected: PASS

```bash
git add -A && git commit -m "chore: set up project and settings"
```

---

### Task 2: Schema, migration and test harness

**Files:**
- Create: `src/climate_alerts/db.py`, `src/climate_alerts/agrobot/models.py`, `src/climate_alerts/alerts/models.py`, `src/climate_alerts/notifications/models.py`, `alembic.ini`, `migrations/env.py` (async template), `migrations/versions/0001_initial_schema.py`
- Test: `tests/conftest.py`, `tests/integration/conftest.py`, `tests/integration/factories.py`, `tests/integration/test_migrations.py`, `tests/integration/test_constraints.py`

**Interfaces:**
- Consumes: `Settings`.
- Produces:
  - `Base(DeclarativeBase)` with the naming convention. `SessionFactory = async_sessionmaker[AsyncSession]`. `build_engine(settings) -> AsyncEngine`. `build_session_factory(engine) -> SessionFactory`.
  - Models with the columns in `ModeloDeDatos.md`: `User`, `Field`, `ClimateEventType`, `WeatherForecast`, `Alert`, `Notification`. `NotificationStatus(StrEnum)` with `PENDING`, `SENT`, `FAILED`.
  - Named constraints: `uq_notifications_alert_date_channel`, `uq_alerts_live_field_event` (partial), `ix_notifications_due` (partial), `ix_fields_owner_id`.
  - Fixtures: `postgres_url` (session, sync), `settings`, `sessions: SessionFactory`, autouse `clean_tables` (truncates everything except the catalog).
  - Factories taking a session: `create_user(s, phone=..., email=None)`, `create_field(s, owner, name=..., timezone="America/Argentina/Buenos_Aires")`, `create_alert(s, field, event_type="frost", threshold="70")`, `create_forecast(s, field, days_ahead, probability, event_type="frost")`, `create_notification(s, alert, days_ahead=1, channel="whatsapp", **overrides)`.

- [ ] **Step 1: Write the failing tests**

`test_constraints.py` is parametrized. Each case inserts or updates through the ORM and expects `DBAPIError`.

| Test | Assertion |
|------|-----------|
| `test_rejected_data[...]` | Cases: `threshold` 0 and 100.01, `probability` 101, unknown event on alert and on forecast, invalid timezone `Mars/Olympus`, unknown `status`, `sent` without `sent_at`, `sent_at` on a pending row, negative `attempts`, duplicate alert/date/channel, duplicate live alert, hard delete of an alert with notifications, hard delete of a field with alerts |
| `test_soft_deleted_alert_frees_the_slot` | With `deleted_at` set, a new alert on the same field and event succeeds |
| `test_two_channels_same_date_allowed` | `whatsapp` and `email` rows for one alert and date both insert |
| `test_upgrade_check_downgrade_cycle` | On a separate database: `upgrade head`, `check` passes, `downgrade base` leaves no tables, `upgrade head` works again |
| `test_migration_seeds_catalog` | Codes equal `{"frost", "rain", "hail", "snow", "thunderstorm", "heat_wave", "strong_wind"}` |

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration -v`
Expected: FAIL with `ModuleNotFoundError: climate_alerts.db`

- [ ] **Step 3: Implement models, engine setup and the migration**

Write the migration by hand from `ModeloDeDatos.md`, including every `CHECK`, the partial indexes, the catalog insert and a full downgrade. Autogenerate does not detect `CHECK` constraints. `fields.timezone` uses `CHECK ((TIMESTAMP '2000-01-01 00:00' AT TIME ZONE timezone) IS NOT NULL)`. `env.py` takes the URL from `sqlalchemy.url` or, if unset, from `DATABASE_URL`. Apply migrations from a sync, session-scoped fixture, because Alembic's async template calls `asyncio.run`.

- [ ] **Step 4: Run tests to verify they pass, then commit**

Run: `uv run pytest tests/integration -v`
Expected: PASS

```bash
git add -A && git commit -m "feat: add schema, migration and test harness"
```

---

### Task 3: API foundation, authentication, health and catalog

**Files:**
- Create: `src/climate_alerts/auth.py`, `src/climate_alerts/agrobot/tokens.py`, `src/climate_alerts/agrobot/schemas.py`, `src/climate_alerts/agrobot/router.py`, `src/climate_alerts/api/app.py`, `src/climate_alerts/api/dependencies.py`, `src/climate_alerts/api/health.py`
- Test: `tests/unit/test_auth.py`, `tests/integration/test_api_foundation.py`

**Interfaces:**
- Consumes: `Settings`, `build_engine`, `build_session_factory`, `ClimateEventType`.
- Produces:
  - `InvalidCredentials(Exception)`. `verify_token(token: str, settings: Settings) -> int`.
  - `issue_token(user_id: int, settings: Settings, ttl: timedelta) -> str`.
  - `create_app(settings: Settings | None = None) -> FastAPI`, whose lifespan stores `app.state.settings` and `app.state.sessions`.
  - `SessionDep`, `SettingsDep`, `CurrentUserId` annotated dependencies. `MAX_BIGINT = 2**63 - 1`.
  - Fixtures: `client` (httpx `ASGITransport` inside `LifespanManager`), `auth_headers(user_id) -> dict`.

- [ ] **Step 1: Write the failing tests**

| Test | Assertion |
|------|-----------|
| `test_valid_token_returns_user_id` | `verify_token(issue_token(7, s, timedelta(minutes=5)), s) == 7` |
| `test_rejected_tokens[...]` | Raises `InvalidCredentials` for: expired, no `exp`, no `sub`, `sub="abc"`, `sub="0"`, `sub="-1"`, `sub="99999999999999999999"`, tampered signature, wrong secret, signed with `HS512` |
| `test_no_token_returns_401` | `GET /api/v1/climate-event-types` returns `401` with `WWW-Authenticate: Bearer` |
| `test_invalid_token_returns_401` | Same route with `Bearer abc` returns `401` |
| `test_catalog_lists_events` | `200`, 7 items with `code` and `label` |
| `test_live_needs_no_token` | `GET /health/live` returns `200` |
| `test_ready_ok` | `GET /health/ready` returns `200` |
| `test_ready_when_database_is_down` | With `app.state.sessions` pointing at a closed port, returns `503` |

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_auth.py tests/integration/test_api_foundation.py -v`
Expected: FAIL with `ModuleNotFoundError: climate_alerts.auth`

- [ ] **Step 3: Implement as specified in SPEC section 6**

Use `HTTPBearer`. Decode with `algorithms=[settings.jwt_algorithm]` and `options={"require": ["exp", "sub"]}`. `sub` must parse to an integer between 1 and `MAX_BIGINT`. Readiness runs `select(1)`.

- [ ] **Step 4: Run tests to verify they pass, then commit**

Run: `uv run pytest tests/unit/test_auth.py tests/integration/test_api_foundation.py -v`
Expected: PASS

```bash
git add -A && git commit -m "feat: add app factory, JWT auth, health and catalog"
```

---

### Task 4: Alerts API

**Files:**
- Create: `src/climate_alerts/alerts/schemas.py`, `src/climate_alerts/alerts/service.py`, `src/climate_alerts/alerts/router.py`
- Modify: `src/climate_alerts/api/app.py`
- Test: `tests/integration/test_alerts_api.py`

**Interfaces:**
- Consumes: `SessionDep`, `CurrentUserId`, `Alert`, `Field`, `ClimateEventType`.
- Produces in `alerts/service.py`, each taking `session` and `user_id` first:
  - `create_alert(session, user_id, field_id, event_type, threshold) -> Alert`
  - `list_alerts(session, user_id, field_id: int | None) -> list[Alert]`
  - `get_alert(session, user_id, alert_id) -> Alert`
  - `update_threshold(session, user_id, alert_id, threshold) -> Alert`
  - `delete_alert(session, user_id, alert_id) -> None`
  - Exceptions `FieldNotFound`, `AlertNotFound`, `UnknownEventType`, `AlertAlreadyExists`.

- [ ] **Step 1: Write the failing tests**

| Test | Assertion |
|------|-----------|
| `test_create_alert` | `201`, body has `id`, `field_id`, `event_type`, `threshold`, `created_at`, `updated_at` |
| `test_threshold_bounds[...]` | `100` gives `201`. `0`, `100.01`, `70.123` give `422` |
| `test_unknown_event_returns_422` | `event_type="tornado"` gives `422` |
| `test_duplicate_returns_409` | Second create gives `409` |
| `test_concurrent_creates_conflict_once` | Two simultaneous creates: `sorted(statuses) == [201, 409]` |
| `test_create_on_foreign_field_returns_404` | Another user's field gives `404` |
| `test_list_scoped_and_filtered` | Only own alerts. `?field_id=` narrows. Deleted alerts are absent |
| `test_read_foreign_alert_returns_404` | Another user's alert gives `404` |
| `test_patch_changes_threshold_only` | `200` with the new threshold and a later `updated_at`. A body with `event_type` gives `422` |
| `test_delete_is_soft_and_not_repeatable` | `204`, row keeps `deleted_at`, second delete gives `404` |
| `test_recreate_after_delete` | `201` with a new `id` |
| `test_oversized_path_id_returns_422` | `GET /api/v1/alerts/99999999999999999999` gives `422` |

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_alerts_api.py -v`
Expected: FAIL with `404` on the routes

- [ ] **Step 3: Implement as specified in SPEC section 7**

Create with `postgresql.insert(Alert).on_conflict_do_nothing(index_elements=["field_id", "event_type"], index_where=Alert.deleted_at.is_(None)).returning(Alert)`. No row returned means `AlertAlreadyExists`. Decimals use `max_digits=5, decimal_places=2`. Update and create bodies set `extra="forbid"`. Path ids are bounded by `MAX_BIGINT`.

- [ ] **Step 4: Run tests to verify they pass, then commit**

Run: `uv run pytest tests/integration/test_alerts_api.py -v`
Expected: PASS

```bash
git add -A && git commit -m "feat: add alerts API"
```

---

### Task 5: Evaluation

**Files:**
- Create: `src/climate_alerts/notifications/evaluation.py`
- Test: `tests/integration/test_evaluation.py`

**Interfaces:**
- Consumes: `Settings`, `SessionFactory`, models.
- Produces:
  - `build_evaluation_statement(after_id: int, up_to_id: int, channels: Sequence[str]) -> Insert`
  - `next_chunk_end(session, after_id: int, chunk_size: int) -> int | None`
  - `EvaluationResult(created: int, chunks: int, failed_chunks: int)`
  - `async evaluate_alerts(sessions, settings, stopping: asyncio.Event) -> EvaluationResult`

- [ ] **Step 1: Write the failing tests**

| Test | Assertion |
|------|-----------|
| `test_probability_equal_to_threshold_notifies` | 70.00 against 70 gives `created == 1` |
| `test_below_threshold_does_not_notify` | 69.99 against 70 gives `created == 0` |
| `test_rerun_creates_nothing` | Second run gives `created == 0` |
| `test_revision_keeps_snapshot` | After raising the forecast to 95, the row still has `probability == 70` |
| `test_new_date_after_notifying` | A second qualifying date adds exactly one row |
| `test_past_local_date_is_skipped` | A date that is today in `Pacific/Pago_Pago` and past in `Pacific/Kiritimati` notifies only the first field |
| `test_every_stored_future_date_is_evaluated` | 16 qualifying days give `created == 16` |
| `test_deleted_alert_is_skipped` | `created == 0` |
| `test_one_row_per_enabled_channel` | Channels `whatsapp,email` give 2 rows |
| `test_enabling_a_channel_adds_only_that_channel` | `whatsapp` then `whatsapp,email` adds 1 row |
| `test_chunks_cover_all_alerts` | 5 alerts with `evaluation_chunk_size=2` give `chunks == 3`, `created == 5` |
| `test_failing_chunk_does_not_block_later_alerts` | Chunk size 1, first chunk raises `OperationalError`: `failed_chunks == 1`, `created == 1` |
| `test_stopping_prevents_new_chunks` | With `stopping` set, `chunks == 0` |
| `test_concurrent_evaluators_create_no_duplicates` | 4 concurrent runs leave one row per key and raise no deadlock |
| `test_statement_has_no_anti_join` | With `notifications` analyzed while empty, `EXPLAIN` of the statement does not contain `Anti Join` |

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_evaluation.py -v`
Expected: FAIL with `ModuleNotFoundError: climate_alerts.notifications.evaluation`

- [ ] **Step 3: Implement as specified in SPEC section 8**

This form has been executed on PostgreSQL. `render_derived` is required, because PostgreSQL needs the alias `c(channel)`:

```python
channel = (
    func.unnest(bindparam("channels", list(channels), type_=ARRAY(Text)))
    .table_valued("channel")
    .render_derived(name="c")
)
local_today = cast(func.timezone(Field.timezone, func.now()), Date)
# select(...).join(Field, ...).join(WeatherForecast, ...).join(channel, true())
# .order_by(Alert.id, WeatherForecast.forecast_date, channel.c.channel)
# postgresql.insert(Notification).from_select([...], candidates)
#   .on_conflict_do_nothing(constraint="uq_notifications_alert_date_channel")
```

Catch `SQLAlchemyError` per chunk. Do not catch `CancelledError`. Log `evaluation.completed` as in SPEC section 13.

- [ ] **Step 4: Run tests to verify they pass, then commit**

Run: `uv run pytest tests/integration/test_evaluation.py -v`
Expected: PASS

```bash
git add -A && git commit -m "feat: evaluate forecasts against alerts in chunks"
```

---

### Task 6: Senders and dispatch

**Files:**
- Create: `src/climate_alerts/notifications/senders.py`, `src/climate_alerts/notifications/dispatch.py`
- Test: `tests/unit/test_senders.py`, `tests/unit/test_backoff.py`, `tests/integration/test_dispatch.py`

**Interfaces:**
- Consumes: `Settings`, `SessionFactory`, `Notification`, `NotificationStatus`.
- Produces in `senders.py`: `Recipient`, `OutboundNotification`, `NotificationSender` (SPEC section 9), `PermanentDeliveryError`, `SimulatedWhatsAppSender`, `SimulatedEmailSender`, `build_sender_registry(channels: Sequence[str]) -> dict[str, NotificationSender]`, which raises `ValueError` for a channel with no sender.
- Produces in `dispatch.py`:
  - `Claimed(notification: OutboundNotification, attempts: int)`
  - `Outcome(notification_id: int, attempts: int, kind: Literal["sent", "retry", "failed"], error: str | None, delay_seconds: float | None)`
  - `backoff_seconds(attempts: int, base: float, cap: float, rng: random.Random) -> float`
  - `build_claim_statement(batch_size: int, lease_seconds: int) -> Select`
  - `async claim_batch(sessions, batch_size, lease_seconds) -> list[Claimed]`
  - `async deliver(claimed, senders, settings, rng) -> Outcome`
  - `async record_outcomes(sessions, outcomes: Sequence[Outcome]) -> None`
  - `DispatchResult(claimed: int, sent: int, retried: int, failed: int)`
  - `async dispatch_due(sessions, senders, settings, stopping, rng=None) -> DispatchResult`

- [ ] **Step 1: Write the failing tests**

| Test | Assertion |
|------|-----------|
| `test_backoff_range` | `backoff_seconds(1, 60, 3600, Random(0))` is between 30 and 60 |
| `test_backoff_is_capped` | `backoff_seconds(20, 60, 3600, Random(0)) <= 3600` |
| `test_registry_rejects_unknown_channel` | `build_sender_registry(["sms"])` raises `ValueError` |
| `test_email_without_address_is_permanent` | `SimulatedEmailSender.send` with `email=None` raises `PermanentDeliveryError` |
| `test_simulated_sender_masks_recipient` | Captured log does not contain the phone or email |
| `test_success_marks_sent` | `status == "sent"`, `sent_at` set, `last_error` null |
| `test_permanent_error_marks_failed` | `status == "failed"`, reason in `last_error`, sender called once |
| `test_transient_error_schedules_retry` | `status == "pending"`, `next_attempt_at > now()`, `last_error` set |
| `test_last_attempt_marks_failed` | Failing at `attempts == 8` gives `failed` |
| `test_expired_leases_exhaust_attempts` | Claimed with `attempts == 9`: `failed`, sender not called |
| `test_hanging_sender_times_out` | With a 0.05 s timeout: row retried, other rows in the batch `sent` |
| `test_row_for_disabled_channel_fails_visibly` | `channel="sms"` ends `failed` with a reason |
| `test_missing_address_marks_failed` | No email: `failed` with a reason |
| `test_channels_fail_independently` | `whatsapp` sent, `email` retried, and a later run sends only `email` |
| `test_claim_respects_batch_size_and_order` | Returns `batch_size` rows, oldest due first |
| `test_leased_rows_are_invisible` | Second claim returns none |
| `test_expired_lease_is_reclaimed` | After expiry, reclaimed with `attempts == 2` |
| `test_stale_dispatcher_cannot_overwrite` | `record_outcomes` with an old `attempts` changes nothing |
| `test_concurrent_dispatchers_send_once` | 4 concurrent `dispatch_due` over 50 rows: each id sent exactly once |
| `test_concurrency_is_bounded` | Peak simultaneous sends `<= dispatch_concurrency` |
| `test_error_is_truncated` | `len(last_error) <= 500` |

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_senders.py tests/unit/test_backoff.py tests/integration/test_dispatch.py -v`
Expected: FAIL with `ModuleNotFoundError: climate_alerts.notifications.senders`

- [ ] **Step 3: Implement as specified in SPEC section 9**

The claim is one statement, executed on PostgreSQL in this form:

```python
due = aliased(Notification)  # without the alias the subquery loses its FROM
due_ids = (
    select(due.id)
    .where(due.status == "pending", due.next_attempt_at <= func.now())
    .order_by(due.next_attempt_at, due.id)
    .limit(batch_size)
    .with_for_update(skip_locked=True)
)
claimed = (
    update(Notification)
    .where(
        Notification.id == any_(func.array(due_ids.scalar_subquery())),
        Alert.id == Notification.alert_id,
        Field.id == Alert.field_id,
        User.id == Field.owner_id,
    )
    .values(
        attempts=Notification.attempts + 1,
        next_attempt_at=func.now() + timedelta(seconds=lease_seconds),
    )
    .returning(
        ..., Alert.event_type, Field.name.label("field_name"), User.phone, User.email
    )
)
```

Note: an earlier version selected the ids in a `MATERIALIZED` CTE. It kept the batch bounded but scanned the whole table on every claim; `ARRAY(...)` is evaluated once as an InitPlan and the update uses the primary key. See `Decisiones.md`, decision 14.

Send with `asyncio.timeout` under a semaphore, outside any transaction. Each outcome update filters on `id`, `attempts` and `status == "pending"`. Log `dispatch.completed`.

- [ ] **Step 4: Run tests to verify they pass, then commit**

Run: `uv run pytest tests/unit/test_senders.py tests/unit/test_backoff.py tests/integration/test_dispatch.py -v`
Expected: PASS

```bash
git add -A && git commit -m "feat: dispatch notifications per channel with leases and retries"
```

---

### Task 7: Notification history API

**Files:**
- Create: `src/climate_alerts/notifications/schemas.py`, `src/climate_alerts/notifications/router.py`
- Modify: `src/climate_alerts/api/app.py`
- Test: `tests/integration/test_notifications_api.py`

**Interfaces:**
- Consumes: `SessionDep`, `SettingsDep`, `CurrentUserId`, models.
- Produces: `GET /api/v1/notifications` with `NotificationOut` and `NotificationPage` from SPEC section 7.

- [ ] **Step 1: Write the failing tests**

| Test | Assertion |
|------|-----------|
| `test_lists_only_own_notifications` | Another user's rows are absent |
| `test_body_has_spec_fields` | Keys equal the `NotificationOut` fields |
| `test_filters[...]` | `status`, `channel` and `field_id` each narrow the results |
| `test_pagination` | 3 rows with `limit=2`: first page 2 items, newest first, `next_cursor` set. Next page 1 item, `next_cursor` null |
| `test_limit_above_maximum_returns_422` | `limit=201` gives `422` |
| `test_oversized_cursor_returns_422` | `cursor=99999999999999999999` gives `422` |
| `test_deleted_alert_keeps_history` | A soft-deleted alert's notifications are still listed |

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_notifications_api.py -v`
Expected: FAIL with `404`

- [ ] **Step 3: Implement as specified in SPEC section 7**

Join `Notification`, `Alert` and `Field`, filter on `Field.owner_id`, order by `Notification.id.desc()`, and fetch `limit + 1`.

- [ ] **Step 4: Run tests to verify they pass, then commit**

Run: `uv run pytest tests/integration/test_notifications_api.py -v`
Expected: PASS

```bash
git add -A && git commit -m "feat: add notification history API"
```

---

### Task 8: Worker lifecycle

**Files:**
- Create: `src/climate_alerts/worker.py`
- Test: `tests/integration/test_worker.py`

**Interfaces:**
- Consumes: `evaluate_alerts`, `dispatch_due`, `build_sender_registry`, `SessionFactory`, `Settings`.
- Produces: `Worker(settings, sessions, senders)` with `async run() -> None` and `request_stop() -> None`. `main() -> None`, runnable as `python -m climate_alerts.worker`.

- [ ] **Step 1: Write the failing tests**

| Test | Assertion |
|------|-----------|
| `test_jobs_run_immediately_on_start` | Both jobs ran before the first interval elapses |
| `test_job_failure_does_not_stop_the_worker` | `evaluate_alerts` raises `OperationalError` once: it is called again and `run()` is still active |
| `test_stop_waits_for_in_flight_job` | `request_stop()` during a job: the job completes and `run()` returns |
| `test_stop_gives_up_after_timeout` | A job that ignores `stopping`: `run()` returns within `worker_shutdown_timeout_seconds` plus 1 s |
| `test_sigterm_stops_subprocess_cleanly` | `python -m climate_alerts.worker`, then `SIGTERM`: exit code 0, and `worker.stopped` is logged |

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_worker.py -v`
Expected: FAIL with `ModuleNotFoundError: climate_alerts.worker`

- [ ] **Step 3: Implement as specified in SPEC section 10**

Add jobs with `trigger="interval"`, `max_instances=1`, `coalesce=True` and `next_run_time` set to now. Track each running job's task in a set, because APScheduler cancels jobs on shutdown and cannot wait for them. On stop: set `stopping`, pause the scheduler, `asyncio.wait` the tasks with the timeout, call `scheduler.shutdown(wait=False)`, then dispose the engine. Job wrappers log and swallow `Exception`, never `CancelledError`. `main` validates the registry before starting.

- [ ] **Step 4: Run tests to verify they pass, then commit**

Run: `uv run pytest tests/integration/test_worker.py -v`
Expected: PASS

```bash
git add -A && git commit -m "feat: add worker with graceful shutdown"
```

---

### Task 9: Seed command

**Files:**
- Create: `src/climate_alerts/agrobot/seed.py`
- Test: `tests/integration/test_seed.py`

**Interfaces:**
- Consumes: `issue_token`, models, `SessionFactory`, `evaluate_alerts` (test only).
- Produces: `SeededUser(id: int, field_ids: list[int], token: str, has_email: bool)`. `async seed(sessions, settings, today: date | None = None) -> list[SeededUser]`. `main() -> None`, runnable as `python -m climate_alerts.agrobot.seed`.

- [ ] **Step 1: Write the failing tests**

| Test | Assertion |
|------|-----------|
| `test_creates_expected_data` | 3 users, 5 fields, 560 forecasts (5 × 16 × 7), exactly one user without email |
| `test_is_idempotent` | Running twice leaves the same counts and the same ids |
| `test_is_deterministic` | Probabilities match between two runs |
| `test_background_stays_below_thresholds` | Excluding the scenario rows, every probability is at most 40 |
| `test_scenario_produces_notifications` | After seeding, `evaluate_alerts` creates 2 rows per enabled channel |
| `test_user_without_email_is_in_the_scenario` | One qualifying field belongs to the user without email |
| `test_tokens_verify` | `verify_token(user.token, settings) == user.id` for each user |

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_seed.py -v`
Expected: FAIL with `ModuleNotFoundError: climate_alerts.agrobot.seed`

- [ ] **Step 3: Implement as specified in SPEC section 12**

One transaction. Forecasts upsert with `on_conflict_do_update` on the primary key, updating `probability` and `fetched_at`. Probabilities come from `random.Random` seeded by a fixed value. Dates use each field's local today. Print each user's id, field ids and token.

- [ ] **Step 4: Run tests to verify they pass, then commit**

Run: `uv run pytest tests/integration/test_seed.py -v`
Expected: PASS

```bash
git add -A && git commit -m "feat: add deterministic seed command"
```

---

### Task 10: Containers

**Files:**
- Create: `Dockerfile`, `compose.yaml`, `.dockerignore`

**Interfaces:**
- Consumes: the entry points in SPEC section 1.
- Produces: `docker compose up` as described in SPEC sections 1 and 14, with `NOTIFICATION_CHANNELS=whatsapp,email`.

- [ ] **Step 1: Write the image and Compose file**

Multi-stage uv build on `python:3.14-slim`, non-root user, exec-form commands. Services `db`, `migrate`, `seed`, `api`, `worker`. Use `service_healthy` and `service_completed_successfully`, and `stop_grace_period: 30s` on the worker.

- [ ] **Step 2: Verify the flow on a clean start**

Run: `docker compose down -v && docker compose up -d --build`
Then, with a token from `docker compose logs seed`: `curl -H "Authorization: Bearer $TOKEN" localhost:8000/api/v1/notifications`
Expected: `200` with `sent` rows, and a `failed` email row for the user without email.

- [ ] **Step 3: Verify shutdown**

Run: `docker compose stop worker && docker compose ps -a worker`
Expected: exit code 0, in under 30 s.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "build: add container image and compose stack"
```

---

### Task 11: API collection

**Files:**
- Create: `collection/climate-alerts.postman_collection.json`, `collection/local.postman_environment.json`

**Interfaces:**
- Consumes: the running stack from Task 10, and the seed output.
- Produces: the folders and variables in SPEC section 16.

- [ ] **Step 1: Write the collection and environment as in SPEC section 16**

Use `pm.test` with `pm.response.to.have.status(...)`, and `pm.environment.set("alert_id", ...)` after creating the alert. Keep scripts simple so Bruno's translator handles them.

- [ ] **Step 2: Verify it passes against a clean stack**

Run: `npx newman run collection/climate-alerts.postman_collection.json -e collection/local.postman_environment.json --env-var token_user_1=$T1 --env-var token_user_2=$T2`
Expected: all assertions pass.

- [ ] **Step 3: Verify it converts for Bruno**

Run `postmanToBruno` from `@usebruno/converters` on the collection with a short script in the scratchpad.
Expected: no skipped items, and the converted status checks do not contain untranslated `pm.`.

If `npx` is blocked by the permission system, stop and report it to the user rather than working around it.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "docs: add API collection for manual testing"
```

---

### Task 12: Verification, scripts and final documentation

**Files:**
- Create: `scripts/license_audit.py`, `scripts/volume_benchmark.py`
- Modify: `README.md`, `Decisiones.md`, `ModeloDeDatos.md`, `procedimiento.md`

**Interfaces:**
- Consumes: `build_evaluation_statement`, `build_claim_statement`, and the scratchpad audit and benchmark work.

- [ ] **Step 1: Promote the license audit and run it on the full dependency tree**

Move the audit into `scripts/license_audit.py`. Say in its docstring what it does not do: PyPI metadata is self-declared and is not legal review.
Run: `uv run python scripts/license_audit.py`
Expected: every package has a license, and any copyleft entry is explained in `Decisiones.md`.

- [ ] **Step 2: Promote the benchmark and measure the final statements**

`scripts/volume_benchmark.py` must use `build_evaluation_statement` and `build_claim_statement`, and refuse to run unless the database URL host is `localhost` or `127.0.0.1`, because it truncates tables. Run it at 24,000 farms, in the analyzed-while-empty state.
Expected: worst-case evaluation stays near the 10 s measured earlier. Record the numbers in decision 12 of `Decisiones.md`.

- [ ] **Step 3: Check the diagrams**

Render the Mermaid blocks in `ModeloDeDatos.md`. Automated rendering was blocked by the permission system earlier, so if it is still blocked, ask the user to confirm them in a Mermaid preview.

- [ ] **Step 4: Finish the documents**

In `README.md`: how to run it, how to get a token, how to use `collection/`, how to requeue a `failed` notification, and "Lo que faltó". Update section 9 items of the PRD that are now resolved, `Decisiones.md`, and `procedimiento.md`. Remove the "borrador" notices.

- [ ] **Step 5: Run all gates on a clean checkout**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest`, then repeat the clean start from Task 10.
Expected: everything passes.

- [ ] **Step 6: Run the four lens reviews and fix findings**

Run `review-risk`, `review-resilience`, `review-readability` and `review-reliability` over the full diff. Fix Critical and Important findings test-first, then rerun Step 5.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "docs: finalize documentation and verification results"
```
