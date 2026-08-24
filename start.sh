#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

CERT_DIR="$ROOT/Rec/adapter/certs"
SETTINGS_FILE="$ROOT/Rec/data/config/settings.json"

echo "==> Preparing Stripchat adapter certificates..."

mkdir -p "$CERT_DIR"
mkdir -p "$(dirname "$SETTINGS_FILE")"

if ! command -v openssl >/dev/null 2>&1; then
    echo "ERROR: openssl is required."
    exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "ERROR: jq is required."
    exit 1
fi

if [[ ! -f "$CERT_DIR/ca.crt" || ! -f "$CERT_DIR/ca.key" ]]; then
    echo "Generating local Stripchat CA..."

    openssl req \
        -x509 \
        -newkey rsa:2048 \
        -keyout "$CERT_DIR/ca.key" \
        -out "$CERT_DIR/ca.crt" \
        -days 3650 \
        -nodes \
        -subj '/CN=Stripchat Local CA'
else
    echo "Local Stripchat CA already exists."
fi

if [[ ! -f "$CERT_DIR/server.cnf" ]]; then
    cat > "$CERT_DIR/server.cnf" <<'EOF'
[req]
distinguished_name = dn
prompt = no
req_extensions = req_ext

[dn]
CN = stripchat-adapter

[req_ext]
subjectAltName = @alt_names

[alt_names]
DNS.1 = stripchat-adapter
EOF
fi

if [[ ! -f "$CERT_DIR/server.crt" || ! -f "$CERT_DIR/server.key" ]]; then
    echo "Generating stripchat-adapter TLS certificate..."

    openssl genrsa \
        -out "$CERT_DIR/server.key" \
        2048

    openssl req \
        -new \
        -key "$CERT_DIR/server.key" \
        -out "$CERT_DIR/server.csr" \
        -config "$CERT_DIR/server.cnf"

    openssl x509 \
        -req \
        -in "$CERT_DIR/server.csr" \
        -CA "$CERT_DIR/ca.crt" \
        -CAkey "$CERT_DIR/ca.key" \
        -CAcreateserial \
        -out "$CERT_DIR/server.crt" \
        -days 825 \
        -extensions req_ext \
        -extfile "$CERT_DIR/server.cnf"
else
    echo "stripchat-adapter TLS certificate already exists."
fi

chmod 644 \
    "$CERT_DIR/ca.crt" \
    "$CERT_DIR/server.crt"

chmod 600 \
    "$CERT_DIR/ca.key" \
    "$CERT_DIR/server.key"

echo "==> Starting Docker monitor services..."
docker compose \
    --env-file .env \
    -f monitor/docker-compose.yml \
    up -d

echo "==> Starting Podman media services..."
podman-compose \
    --env-file .env \
    -f media/compose.yml \
    up -d

echo "==> Starting Immich services..."
docker compose \
    --env-file .env \
    -f immich-app/docker-compose.yml \
    up -d

echo "==> Starting Stripchat recorder + adapter..."
podman-compose \
    --env-file .env \
    -f Rec/docker-compose.yaml \
    up -d --build

echo "==> Waiting for recorder config..."

for i in {1..30}; do
    if [[ -s "$SETTINGS_FILE" ]]; then
        break
    fi

    sleep 1
done

if [[ ! -s "$SETTINGS_FILE" ]]; then
    echo "ERROR: Stripchat settings.json was not created."
    exit 1
fi

echo "==> Configuring Stripchat mirror..."

tmp="$(mktemp)"

jq '
  .api_proxy_url = null |
  .cdn_proxy_url = null |
  .sc_mirror_url = "stripchat-adapter:8080"
' "$SETTINGS_FILE" > "$tmp"

cat "$tmp" > "$SETTINGS_FILE"
rm -f "$tmp"

echo "==> Restarting Stripchat recorder with updated settings..."
podman restart stripchat-recorder >/dev/null

echo
echo "==> Stripchat configuration:"
jq '{
  api_proxy_url,
  cdn_proxy_url,
  sc_mirror_url
}' "$SETTINGS_FILE"

echo
echo "==> Services started."

podman ps \
    --filter name=stripchat-recorder \
    --filter name=stripchat-adapter \
    --format 'table {{.Names}}\t{{.Status}}\t{{.Networks}}'