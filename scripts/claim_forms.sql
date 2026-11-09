-- Compares the forms of the dispatch claim (decision 14 in docs/Decisiones.md).
--
-- What it does: on an empty, throwaway database it creates a minimal copy of
-- the schema, then for each claim form prints the plan and how many rows one
-- claim with LIMIT 5 takes when statistics are stale. Each claim runs in a
-- transaction that is rolled back. It ends by timing, with EXPLAIN ANALYZE,
-- the old MATERIALIZED CTE form against the current one on 100,000 due rows
-- with healthy statistics and a batch of 500.
--
-- What it does NOT do: it is not a benchmark of the full drain (that is
-- scripts/volume_benchmark.py), and it does not use the application's tables,
-- migrations or code. The tables it creates stay in the database: only the
-- claims are rolled back. Timings vary between runs and machines.
--
-- Never run it against a database with real data. It refuses to start if the
-- database already has any table outside the system schemas.
--
-- Run it on a throwaway postgres:17 container, then remove the container:
--
--   docker run -d --rm --name claim-forms -e POSTGRES_PASSWORD=throwaway postgres:17
--   until docker exec claim-forms pg_isready -U postgres -q; do sleep 1; done
--   docker exec -i claim-forms psql -U postgres -X -q < scripts/claim_forms.sql
--   docker stop claim-forms

\set ON_ERROR_STOP on
\pset footer off

DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_tables
             WHERE schemaname NOT IN ('pg_catalog', 'information_schema')) THEN
    RAISE EXCEPTION 'refusing to run: this database already has tables; use an empty throwaway database';
  END IF;
END
$$;

CREATE TABLE users (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, phone text NOT NULL, email text);
CREATE TABLE fields (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, owner_id bigint NOT NULL REFERENCES users, name text NOT NULL);
CREATE TABLE alerts (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, field_id bigint NOT NULL REFERENCES fields, event_type text NOT NULL);
CREATE TABLE notifications (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  alert_id bigint NOT NULL REFERENCES alerts,
  status text NOT NULL DEFAULT 'pending',
  attempts int NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_notifications_due ON notifications (next_attempt_at, id) WHERE status = 'pending';
INSERT INTO users (phone) VALUES ('+1');
INSERT INTO fields (owner_id, name) VALUES (1, 'f');
INSERT INTO alerts (field_id, event_type) VALUES (1, 'frost');

-- Stale statistics: analyzed at 1 row, then 20 more rows on the same heap page.
INSERT INTO notifications (alert_id, next_attempt_at) VALUES (1, now() - interval '1 hour');
ANALYZE notifications;
INSERT INTO notifications (alert_id, next_attempt_at)
  SELECT 1, now() - interval '1 hour' FROM generate_series(1, 20);

\echo '=== A: UPDATE ... WHERE id IN (subquery LIMIT 5)'
EXPLAIN (COSTS OFF)
UPDATE notifications SET attempts = attempts + 1, next_attempt_at = now() + interval '5 minutes'
WHERE id IN (SELECT id FROM notifications WHERE status = 'pending' AND next_attempt_at <= now()
             ORDER BY next_attempt_at, id LIMIT 5 FOR UPDATE SKIP LOCKED)
RETURNING id;
BEGIN;
\o /dev/null
UPDATE notifications SET attempts = attempts + 1, next_attempt_at = now() + interval '5 minutes'
WHERE id IN (SELECT id FROM notifications WHERE status = 'pending' AND next_attempt_at <= now()
             ORDER BY next_attempt_at, id LIMIT 5 FOR UPDATE SKIP LOCKED)
RETURNING id;
\o
\echo 'A claimed' :ROW_COUNT
ROLLBACK;

\echo '=== B: CTE, not materialized'
EXPLAIN (COSTS OFF)
WITH due AS (SELECT id FROM notifications WHERE status = 'pending' AND next_attempt_at <= now()
             ORDER BY next_attempt_at, id LIMIT 5 FOR UPDATE SKIP LOCKED)
UPDATE notifications SET attempts = attempts + 1, next_attempt_at = now() + interval '5 minutes'
WHERE id IN (SELECT id FROM due) RETURNING id;
BEGIN;
\o /dev/null
WITH due AS (SELECT id FROM notifications WHERE status = 'pending' AND next_attempt_at <= now()
             ORDER BY next_attempt_at, id LIMIT 5 FOR UPDATE SKIP LOCKED)
UPDATE notifications SET attempts = attempts + 1, next_attempt_at = now() + interval '5 minutes'
WHERE id IN (SELECT id FROM due) RETURNING id;
\o
\echo 'B claimed' :ROW_COUNT
ROLLBACK;

\echo '=== C: CTE MATERIALIZED (the previous form)'
EXPLAIN (COSTS OFF)
WITH due AS MATERIALIZED (SELECT id FROM notifications WHERE status = 'pending' AND next_attempt_at <= now()
             ORDER BY next_attempt_at, id LIMIT 5 FOR UPDATE SKIP LOCKED)
UPDATE notifications SET attempts = attempts + 1, next_attempt_at = now() + interval '5 minutes'
WHERE id IN (SELECT id FROM due) RETURNING id;
BEGIN;
\o /dev/null
WITH due AS MATERIALIZED (SELECT id FROM notifications WHERE status = 'pending' AND next_attempt_at <= now()
             ORDER BY next_attempt_at, id LIMIT 5 FOR UPDATE SKIP LOCKED)
UPDATE notifications SET attempts = attempts + 1, next_attempt_at = now() + interval '5 minutes'
WHERE id IN (SELECT id FROM due) RETURNING id;
\o
\echo 'C claimed' :ROW_COUNT
ROLLBACK;

\echo '=== D: id = ANY(ARRAY(subquery))'
EXPLAIN (COSTS OFF)
UPDATE notifications SET attempts = attempts + 1, next_attempt_at = now() + interval '5 minutes'
WHERE id = ANY (ARRAY(SELECT id FROM notifications WHERE status = 'pending' AND next_attempt_at <= now()
                      ORDER BY next_attempt_at, id LIMIT 5 FOR UPDATE SKIP LOCKED))
RETURNING id;
BEGIN;
\o /dev/null
UPDATE notifications SET attempts = attempts + 1, next_attempt_at = now() + interval '5 minutes'
WHERE id = ANY (ARRAY(SELECT id FROM notifications WHERE status = 'pending' AND next_attempt_at <= now()
                      ORDER BY next_attempt_at, id LIMIT 5 FOR UPDATE SKIP LOCKED))
RETURNING id;
\o
\echo 'D claimed' :ROW_COUNT
ROLLBACK;

\echo '=== E: single UPDATE ... FROM with recipient, ANY(ARRAY) (the current form)'
EXPLAIN (COSTS OFF)
UPDATE notifications n SET attempts = n.attempts + 1, next_attempt_at = now() + interval '5 minutes'
FROM alerts a JOIN fields f ON f.id = a.field_id JOIN users u ON u.id = f.owner_id
WHERE n.id = ANY (ARRAY(SELECT id FROM notifications WHERE status = 'pending' AND next_attempt_at <= now()
                        ORDER BY next_attempt_at, id LIMIT 5 FOR UPDATE SKIP LOCKED))
  AND a.id = n.alert_id
RETURNING n.id, n.attempts, a.event_type, f.name, u.phone, u.email;
BEGIN;
\o /dev/null
UPDATE notifications n SET attempts = n.attempts + 1, next_attempt_at = now() + interval '5 minutes'
FROM alerts a JOIN fields f ON f.id = a.field_id JOIN users u ON u.id = f.owner_id
WHERE n.id = ANY (ARRAY(SELECT id FROM notifications WHERE status = 'pending' AND next_attempt_at <= now()
                        ORDER BY next_attempt_at, id LIMIT 5 FOR UPDATE SKIP LOCKED))
  AND a.id = n.alert_id
RETURNING n.id, n.attempts, a.event_type, f.name, u.phone, u.email;
\o
\echo 'E claimed' :ROW_COUNT
ROLLBACK;

\echo '=== G: CTE not materialized, UPDATE ... FROM due (common queue-library form)'
EXPLAIN (COSTS OFF)
WITH due AS (SELECT id FROM notifications WHERE status = 'pending' AND next_attempt_at <= now()
             ORDER BY next_attempt_at, id LIMIT 5 FOR UPDATE SKIP LOCKED)
UPDATE notifications n SET attempts = n.attempts + 1, next_attempt_at = now() + interval '5 minutes'
FROM due WHERE n.id = due.id RETURNING n.id;
BEGIN;
\o /dev/null
WITH due AS (SELECT id FROM notifications WHERE status = 'pending' AND next_attempt_at <= now()
             ORDER BY next_attempt_at, id LIMIT 5 FOR UPDATE SKIP LOCKED)
UPDATE notifications n SET attempts = n.attempts + 1, next_attempt_at = now() + interval '5 minutes'
FROM due WHERE n.id = due.id RETURNING n.id;
\o
\echo 'G claimed' :ROW_COUNT
ROLLBACK;

\echo '=== E with a generic plan (asyncpg prepares statements)'
SET plan_cache_mode = force_generic_plan;
PREPARE claim_e(int) AS
UPDATE notifications n SET attempts = n.attempts + 1, next_attempt_at = now() + interval '5 minutes'
FROM alerts a JOIN fields f ON f.id = a.field_id JOIN users u ON u.id = f.owner_id
WHERE n.id = ANY (ARRAY(SELECT id FROM notifications WHERE status = 'pending' AND next_attempt_at <= now()
                        ORDER BY next_attempt_at, id LIMIT $1 FOR UPDATE SKIP LOCKED))
  AND a.id = n.alert_id
RETURNING n.id;
BEGIN;
\o /dev/null
EXECUTE claim_e(5);
\o
\echo 'E generic claimed' :ROW_COUNT
ROLLBACK;
RESET plan_cache_mode;

\echo '=== Healthy statistics, 100k due rows, batch 500: MATERIALIZED CTE (previous) vs E (current)'
TRUNCATE notifications;
INSERT INTO notifications (alert_id, next_attempt_at)
  SELECT 1, now() - interval '1 hour' * random() FROM generate_series(1, 100000);
ANALYZE notifications;
BEGIN;
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, SUMMARY ON)
WITH due AS MATERIALIZED (SELECT id FROM notifications WHERE status = 'pending' AND next_attempt_at <= now()
             ORDER BY next_attempt_at, id LIMIT 500 FOR UPDATE SKIP LOCKED),
claimed AS (UPDATE notifications SET attempts = attempts + 1, next_attempt_at = now() + interval '5 minutes'
            WHERE id IN (SELECT id FROM due) RETURNING id, alert_id, attempts)
SELECT c.id, c.attempts, a.event_type, f.name, u.phone, u.email
FROM claimed c JOIN alerts a ON a.id = c.alert_id JOIN fields f ON f.id = a.field_id JOIN users u ON u.id = f.owner_id;
ROLLBACK;
BEGIN;
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, SUMMARY ON)
UPDATE notifications n SET attempts = n.attempts + 1, next_attempt_at = now() + interval '5 minutes'
FROM alerts a JOIN fields f ON f.id = a.field_id JOIN users u ON u.id = f.owner_id
WHERE n.id = ANY (ARRAY(SELECT id FROM notifications WHERE status = 'pending' AND next_attempt_at <= now()
                        ORDER BY next_attempt_at, id LIMIT 500 FOR UPDATE SKIP LOCKED))
  AND a.id = n.alert_id
RETURNING n.id, n.attempts, a.event_type, f.name, u.phone, u.email;
ROLLBACK;
