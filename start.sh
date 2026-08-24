#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

echo "Starting Docker monitor services..."
docker compose \
  --env-file .env \
  -f monitor/docker-compose.yml up -d

echo "Starting Podman media services..."
podman-compose \
  --env-file .env \
  -f media/compose.yml up -d

echo "Starting Immich services..."
docker compose \
  --env-file .env \
  -f immich-app/docker-compose.yml up -d

echo "All services started."
