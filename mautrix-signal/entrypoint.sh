#!/bin/sh
set -eu

: "${MATRIX_SERVER_NAME:?}"
: "${SIGNAL_DB_PASSWORD:?}"
: "${SIGNAL_AS_TOKEN:?}"
: "${SIGNAL_HS_TOKEN:?}"
: "${SIGNAL_ADMIN_USER:?}"

mkdir -p /data

if [ ! -f /data/config.yaml ]; then
    /usr/bin/mautrix-signal -c /data/config.yaml -e
fi

# ── homeserver ──────────────────────────────────────────────────────────────
yq -i ".homeserver.address = \"http://synapse:8008\""   /data/config.yaml
yq -i ".homeserver.domain  = \"$MATRIX_SERVER_NAME\""   /data/config.yaml

# ── appservice (listener + tokens) ─────────────────────────────────────────
yq -i ".appservice.address  = \"http://mautrix-signal:29328\"" /data/config.yaml
yq -i ".appservice.hostname = \"0.0.0.0\""                     /data/config.yaml
yq -i ".appservice.port     = 29328"                           /data/config.yaml
yq -i ".appservice.as_token = \"$SIGNAL_AS_TOKEN\""            /data/config.yaml
yq -i ".appservice.hs_token = \"$SIGNAL_HS_TOKEN\""            /data/config.yaml

# ── database (top-level in megabridge format) ───────────────────────────────
yq -i ".database.type = \"postgres\""                                                                                /data/config.yaml
yq -i ".database.uri  = \"postgres://signal:${SIGNAL_DB_PASSWORD}@postgres-signal/signal?sslmode=disable\""        /data/config.yaml

# ── permissions ─────────────────────────────────────────────────────────────
yq -i ".bridge.permissions = {\"*\": \"relay\", \"$MATRIX_SERVER_NAME\": \"user\", \"$SIGNAL_ADMIN_USER\": \"admin\"}" /data/config.yaml

if [ ! -f /data/registration.yaml ]; then
    /usr/bin/mautrix-signal -g -c /data/config.yaml -r /data/registration.yaml
fi

echo "Starting mautrix-signal…"
exec /usr/bin/mautrix-signal -c /data/config.yaml
