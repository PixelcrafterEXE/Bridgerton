#!/bin/sh
set -eu

: "${MATRIX_SERVER_NAME:?}"
: "${WHATSAPP_DB_PASSWORD:?}"
: "${WHATSAPP_AS_TOKEN:?}"
: "${WHATSAPP_HS_TOKEN:?}"
: "${WHATSAPP_ADMIN_USER:?}"

mkdir -p /data

# On first start, let the bridge write its default (megabridge) config.yaml
if [ ! -f /data/config.yaml ]; then
    /usr/bin/mautrix-whatsapp -c /data/config.yaml -e
fi

# ── homeserver ──────────────────────────────────────────────────────────────
yq -i ".homeserver.address = \"http://synapse:8008\""   /data/config.yaml
yq -i ".homeserver.domain  = \"$MATRIX_SERVER_NAME\""   /data/config.yaml

# ── appservice (listener + tokens) ─────────────────────────────────────────
yq -i ".appservice.address  = \"http://mautrix-whatsapp:29318\"" /data/config.yaml
yq -i ".appservice.hostname = \"0.0.0.0\""                       /data/config.yaml
yq -i ".appservice.port     = 29318"                             /data/config.yaml
yq -i ".appservice.as_token = \"$WHATSAPP_AS_TOKEN\""            /data/config.yaml
yq -i ".appservice.hs_token = \"$WHATSAPP_HS_TOKEN\""            /data/config.yaml

# ── database (top-level in megabridge format) ───────────────────────────────
yq -i ".database.type = \"postgres\""                                                                                      /data/config.yaml
yq -i ".database.uri  = \"postgres://whatsapp:${WHATSAPP_DB_PASSWORD}@postgres-whatsapp/whatsapp?sslmode=disable\""       /data/config.yaml

# ── permissions ─────────────────────────────────────────────────────────────
yq -i ".bridge.permissions = {\"*\": \"relay\", \"$MATRIX_SERVER_NAME\": \"user\", \"$WHATSAPP_ADMIN_USER\": \"admin\"}" /data/config.yaml

# Generate registration.yaml once (tokens come from config, so they match setup.sh)
if [ ! -f /data/registration.yaml ]; then
    /usr/bin/mautrix-whatsapp -g -c /data/config.yaml -r /data/registration.yaml
fi

echo "Starting mautrix-whatsapp…"
exec /usr/bin/mautrix-whatsapp -c /data/config.yaml
