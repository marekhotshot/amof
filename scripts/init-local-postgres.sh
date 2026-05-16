#!/usr/bin/env bash
# Ensure a local Postgres container exists and is running for AMOF dev.
# Idempotent behavior:
# - If running: do nothing
# - If stopped: start it
# - If missing: create and start it
set -euo pipefail

CONTAINER_NAME="${AMOF_PG_CONTAINER_NAME:-amof-local-postgres}"
IMAGE="${AMOF_PG_IMAGE:-postgres:15-alpine}"
PORT="${AMOF_PG_PORT:-5432}"
DB_USER="${AMOF_PG_USER:-amof}"
DB_PASSWORD="${AMOF_PG_PASSWORD:-amof}"
DB_NAME="${AMOF_PG_DB:-amof}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required for local postgres bootstrap" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "docker daemon is not running or unavailable" >&2
  exit 1
fi

if docker ps --format '{{.Names}}' | awk -v n="$CONTAINER_NAME" '$0 == n { found=1 } END { exit !found }'; then
  echo "Local Postgres already running: ${CONTAINER_NAME}"
else
  if docker ps -a --format '{{.Names}}' | awk -v n="$CONTAINER_NAME" '$0 == n { found=1 } END { exit !found }'; then
    echo "Starting existing Postgres container: ${CONTAINER_NAME}"
    docker start "$CONTAINER_NAME" >/dev/null
  else
    echo "Creating Postgres container: ${CONTAINER_NAME}"
    docker run -d \
      --name "$CONTAINER_NAME" \
      -e POSTGRES_USER="$DB_USER" \
      -e POSTGRES_PASSWORD="$DB_PASSWORD" \
      -e POSTGRES_DB="$DB_NAME" \
      -p "${PORT}:5432" \
      "$IMAGE" >/dev/null
  fi
fi

echo "Waiting for Postgres readiness..."
for _ in $(seq 1 30); do
  if docker exec "$CONTAINER_NAME" pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
    echo "Local Postgres is ready on 127.0.0.1:${PORT}"
    exit 0
  fi
  sleep 1
done

echo "Postgres did not become ready in time" >&2
exit 1

