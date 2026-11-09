#!/usr/bin/env bash
# Smoke test: checks the normal flow end to end with one command.
#
# What it does:
#   1. Starts the Compose stack (`docker compose up -d --build --wait`).
#   2. Reads the tokens of users 1 and 2 from the seed output, without printing them.
#   3. Waits until user 1 has at least one notification, so the worker's first
#      evaluation has run.
#   4. Runs the Postman collection with Newman and exits with Newman's exit code.
#   The stack is left running so it can be explored afterwards.
#
# What it does NOT do:
#   - It is not a load test.
#   - It does not reset the database. The collection assumes the ids of a
#     freshly seeded database, so after manual changes run
#     `docker compose down -v` first.
#
# Usage, from anywhere: scripts/smoke_test.sh
set -euo pipefail

cd "$(dirname "$0")/.."

BASE_URL=http://127.0.0.1:8000
TIMEOUT_SECONDS=120

for tool in curl npx; do
  command -v "$tool" >/dev/null || { echo "error: '$tool' is required but not installed" >&2; exit 1; }
done
docker compose version >/dev/null 2>&1 || { echo "error: 'docker compose' is required but not available" >&2; exit 1; }

# --wait succeeds once the one-shot migrate and seed exit 0 and the api
# healthcheck (/health/ready) passes; it fails if any of them fails.
echo "Starting the stack..."
docker compose up -d --build --wait --wait-timeout "$TIMEOUT_SECONDS"

# The seed may have run more than once; the last line per user is the current token.
seed_token() {
  docker compose logs --no-log-prefix seed \
    | awk -v user="$1" '$1 == "user" && $2 == user && $5 == "token" { token = $6 } END { print token }'
}
token_user_1=$(seed_token 1)
token_user_2=$(seed_token 2)
if [[ -z "$token_user_1" || -z "$token_user_2" ]]; then
  echo "error: could not read the tokens of users 1 and 2 from 'docker compose logs seed'" >&2
  exit 1
fi

echo "Waiting for the worker's first evaluation..."
deadline=$((SECONDS + TIMEOUT_SECONDS))
until [[ "$(curl -fsS -H "Authorization: Bearer $token_user_1" "$BASE_URL/api/v1/notifications" || true)" == *'"items":[{'* ]]; do
  if ((SECONDS >= deadline)); then
    echo "error: user 1 has no notifications after ${TIMEOUT_SECONDS}s" >&2
    exit 1
  fi
  sleep 2
done

status=0
npx -y newman@6.2.3 run collection/climate-alerts.postman_collection.json \
  -e collection/local.postman_environment.json \
  --env-var "token_user_1=$token_user_1" \
  --env-var "token_user_2=$token_user_2" || status=$?

echo
echo "The stack is still running at $BASE_URL."
echo "Stop it:  docker compose down"
echo "Reset it: docker compose down -v"
exit "$status"
