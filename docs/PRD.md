# Climate Alert System: Product Requirements

> Working document, written before implementation. PRD and SPEC were updated while the system was built; continuous integration was added afterwards as the standard gate for a production repository (see [`operacion.md`](operacion.md#integración-continua)). Where this document and the code differ, the code and [`Decisiones.md`](Decisiones.md) are authoritative.

**Status:** final · **Date:** 2026-11-08 · **Source of truth:** [`statement.md`](../statement.md)

Producers configure alerts on their fields, choosing a climate event and a probability threshold. A background job compares stored forecasts against those thresholds, and the system notifies the owner when one is reached. The system must be small, production grade, and honest about what it leaves out.

Rationale, rejected alternatives and sources live in [`Decisiones.md`](Decisiones.md). The schema and its diagrams live in [`ModeloDeDatos.md`](ModeloDeDatos.md).

## 1. Context

Agrobot is a farm management system with a WhatsApp interface, built on Python, FastAPI and PostgreSQL. It already ingests weather forecasts, but nothing acts on them. Agrobot does not exist as an accessible system for this exercise, so the parts this feature depends on are minimal mocks.

## 2. Callers

| Caller | How it uses the system |
|--------|------------------------|
| Producer, through a hypothetical frontend | Calls the API directly with their own token |
| WhatsApp agent or internal tool | Calls the API on behalf of a producer, with a token for that producer |
| Reviewer | Runs `docker compose up` and uses `/docs` with a printed token |

## 3. Goals and non-goals

**Goals**

- Cover everything the statement asks for.
- Never lose a notification once a threshold has been reached and recorded.
- Stay correct with several workers running, and through crashes.
- Handle 24,000 farms with memory that does not grow with volume.
- Document every decision, edge case and simplification with its reasoning.

**Non-goals**

- WhatsApp integration (excluded by the statement).
- Real weather provider ingestion (the statement asks for mocked data).
- User and field management, login or token issuance (these belong to Agrobot).
- The items listed in "Simplificaciones frente a producción" in `Decisiones.md`.

## 4. Functional requirements

### Alerts

| ID | Requirement |
|----|-------------|
| FR-1 | A user can create an alert on a field they own, with an event type from the catalog and a threshold greater than 0 and at most 100 |
| FR-2 | A field can have one live alert per event type. A duplicate returns `409` |
| FR-3 | A user can list their alerts across all their fields, optionally filtered by field |
| FR-4 | A user can read a single alert |
| FR-5 | A user can change an alert's threshold. Nothing else is editable |
| FR-6 | Deleting an alert is a soft delete: evaluation stops, history stays, and notifications already created are still delivered |
| FR-7 | Any authenticated user can read the event type catalog |

### Evaluation

| ID | Requirement |
|----|-------------|
| FR-8 | A worker process, separate from the API, evaluates stored forecasts periodically |
| FR-9 | A notification is created when `probability >= threshold` for a forecast date that is today or later in the field's own timezone. Every stored future date is evaluated |
| FR-10 | There is at most one notification per alert, forecast date and channel, across re-runs, forecast revisions and concurrent workers |
| FR-11 | Each notification stores the probability and threshold observed when it was created |
| FR-12 | Evaluation runs inside PostgreSQL as one statement per chunk of alerts, each chunk in its own transaction, with no `NOT EXISTS` anti-join |

### Delivery

| ID | Requirement |
|----|-------------|
| FR-13 | A dispatcher claims due notifications in bounded batches using `FOR UPDATE SKIP LOCKED` and a lease held in `next_attempt_at` |
| FR-14 | Sending happens outside any database transaction, with bounded concurrency, through the sender registered for the notification's channel. All senders are simulated |
| FR-15 | Transient failures are retried with exponential backoff and jitter |
| FR-16 | Permanent failures, and notifications that exhaust their attempts, end as `failed`, remain visible and are never deleted |
| FR-17 | If a process dies while sending, the lease expires and another dispatcher can claim the notification |
| FR-18 | A user can list their notifications, filtered by status, channel and field, with server-validated cursor pagination |
| FR-26 | Evaluation creates one notification per enabled channel, each with its own status, attempts and retries |
| FR-27 | Enabled channels are configurable, defaulting to `whatsapp`. The application refuses to start if an enabled channel has no sender |
| FR-28 | A notification with no address for its channel ends as `failed` with the reason recorded. Users have a required `phone` and an optional `email` |

### Access

| ID | Requirement |
|----|-------------|
| FR-19 | Business endpoints require a bearer JWT whose signature, algorithm and expiry are verified. `sub` identifies the user. Otherwise the API returns `401` |
| FR-20 | Every query is scoped to the authenticated owner. Another user's resource returns `404` |
| FR-21 | Health endpoints report process liveness and database readiness, without authentication |

### Mock data and startup

| ID | Requirement |
|----|-------------|
| FR-22 | A one-shot, idempotent, deterministic command seeds users, fields in different timezones, forecasts relative to today, and a frost scenario that crosses a threshold |
| FR-23 | Re-running the command upserts forecasts, simulating a new ingestion cycle |
| FR-24 | The command prints each seeded user's id, field ids and a valid JWT |
| FR-25 | `docker compose up` applies migrations, seeds the data and starts the API and the worker, in that order |
| FR-29 | The repository includes an API collection and environment, usable in Postman and importable into Bruno, that walks the end-to-end flow with assertions |

## 5. Non-functional requirements

| Area | Requirement |
|------|-------------|
| Async | All I/O is non-blocking: async FastAPI endpoints, SQLAlchemy `AsyncSession` and an async driver. The worker runs on one event loop with APScheduler 3.x (`<4`) |
| Durability | A notification is committed before any send attempt. Delivery is at least once |
| Concurrency | Correctness never depends on running a single worker. Guarantees live in database constraints |
| Scale | Designed for 24,000 farms, 2.69M forecast rows and 72,000 alerts with one enabled channel, as measured in decision 10 |
| Memory | Evaluation brings no candidate rows into Python. The dispatcher holds at most one batch |
| Data integrity | Every rule listed in `ModeloDeDatos.md` is enforced by the database |
| Configuration | Operating values have defaults in code and environment overrides. Secrets have no default, and the app refuses to start without them |
| Security | JWT algorithm pinned by configuration. Signing secret of at least 32 characters |
| Observability | Structured logs for each run: rows created, sent, retried and failed, plus duration |
| Shutdown | The worker stops cleanly on `SIGTERM`. Any interrupted work is recovered by rollback or lease expiry |
| Design | Organized by feature. Abstractions exist only where several implementations are expected, which today means the per-channel sender |
| Queries | Built with SQLAlchemy's modern 2.0-style expression language. No raw SQL or legacy APIs in application code, enforced by lint and by failing on deprecation warnings |
| Language | Code and identifiers in English. `README.md`, `procedimiento.md`, `Decisiones.md`, `ModeloDeDatos.md` and `operacion.md` in Spanish |

## 6. Edge cases

The full table is in `Decisiones.md`. These must be covered by tests:

| Case | Expected behaviour |
|------|--------------------|
| Probability exactly equals the threshold | Notifies |
| The job runs again and the condition still holds | No new notification |
| A forecast is revised after notifying | No repeat for that date. The snapshot is unchanged |
| A date is past in one timezone and valid in another | Only the valid field notifies |
| Two evaluators run at once | No duplicates and no deadlocks |
| `notifications` was analyzed while empty, then a large first run | Completes without a pathological plan |
| Two dispatchers run at once | Each notification is claimed once per lease |
| A dispatcher dies while holding a lease | The notification is reclaimed after expiry |
| A send keeps failing | Ends as `failed` and stays visible |
| Alert deleted with notifications pending | Those are delivered, and no new ones are created |
| Deleted alert recreated | Allowed, and it is a new alert |
| Another user's field, alert or notification | `404` |
| Expired, tampered or wrong-algorithm token | `401` |
| Invalid timezone written to a field | Rejected on write |
| One channel delivers and another fails | Only the failed channel is retried |
| A user has no address for a channel | That notification ends as `failed` with the reason |
| A channel is enabled later | It only adds rows for that channel, on dates still above the threshold |

## 7. Deliverables

| Statement item | Deliverable |
|----------------|-------------|
| Repository with the code | This repository |
| README explaining how to run it and the decisions taken | `README.md` |
| Working migrations | Alembic, upgrade and downgrade from empty, with no model drift |
| Tests | Test suite on real PostgreSQL |
| Document anything left out | "Lo que faltó" section in `README.md` |
| Additional | `procedimiento.md`, `Decisiones.md`, `ModeloDeDatos.md`, and the API collection in `collection/` |

## 8. Definition of done

- [x] Every requirement in section 4 is implemented and covered by a test. FR-25 and FR-29 are covered by the CI `dast` job, which starts the stack and runs the collection, rather than by pytest.
- [x] Every edge case in section 6 has a test.
- [x] `docker compose up` on a clean machine produces notifications without manual steps. `scripts/smoke_test.sh` checks it in CI.
- [x] The API collection passes against that running system, and imports into Bruno. The Bruno import was checked with its official converter, not run inside the application.
- [x] Migrations upgrade and downgrade from an empty database, and `alembic check` reports no drift.
- [x] Lint, type checks and the test suite pass.
- [x] The README lets a reviewer run the system and find the main decisions.

## 9. Verifications carried into implementation

Status of the gaps recorded before implementation. Details are in `Decisiones.md`, decisions 2 and 10.

- **Resolved.** The final evaluation statement was measured at full scale: 1,152,000 rows in 15 chunks took 8.71 s, slowest chunk 0.64 s, 463 MiB of WAL, in line with the earlier 10.0 s.
- **Resolved.** The final claim was measured on 1,152,000 rows: no batch exceeded 500, and every row ended `sent`. The drain took 863 s against the earlier 34 to 40 s: the claim took 24.0 s and `record_outcomes` 836.9 s. The earlier spike recorded each batch with one `UPDATE`, while `record_outcomes` runs one per row. It is about 1,335 rows per second, still above the provider limit. An earlier run with the claim in a `MATERIALIZED` CTE took 917 s but did not split the time between the two steps.
- **Still open.** Sustained queue behaviour is unmeasured. Autovacuum ran 7 times on `notifications` during the final drain, but that is one 14-minute run.
- **Resolved.** All five Mermaid diagrams in `ModeloDeDatos.md` were rendered and checked. The state diagram had overlapping labels and now runs left to right with a shorter label.
- **Resolved.** The full dependency tree was audited: 52 packages, none without a declared license, no copyleft at runtime, and two MPL-2.0 packages used only in development (`certifi` and `pathspec`).
- **Resolved.** The four helper files are in `scripts/`: `claim_forms.sql`, `license_audit.py`, `smoke_test.sh` and `volume_benchmark.py`.

## 10. Values decided in the specification

Evaluation and dispatch intervals, lease duration, maximum attempts, backoff base and cap, send concurrency, page size limit, token lifetime in the seed, and the seeded event types.
