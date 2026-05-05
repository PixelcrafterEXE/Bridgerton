#!/bin/sh
# Generates bridge application-service registration YAMLs into /registrations.
# Runs once before Synapse starts so the files are present at homeserver boot.
# All tokens are supplied via environment variables — no generated randomness here.

set -eu

die() { echo "ERROR: $*" >&2; exit 1; }

[ -n "${MATRIX_SERVER_NAME:-}" ]  || die "MATRIX_SERVER_NAME is required"
[ -n "${WHATSAPP_AS_TOKEN:-}" ]   || die "WHATSAPP_AS_TOKEN is required"
[ -n "${WHATSAPP_HS_TOKEN:-}" ]   || die "WHATSAPP_HS_TOKEN is required"
[ -n "${SIGNAL_AS_TOKEN:-}" ]     || die "SIGNAL_AS_TOKEN is required"
[ -n "${SIGNAL_HS_TOKEN:-}" ]     || die "SIGNAL_HS_TOKEN is required"

mkdir -p /registrations

# ── WhatsApp registration ───────────────────────────────────────────────────
cat > /registrations/whatsapp-registration.yaml <<EOF
id: whatsapp
url: http://mautrix-whatsapp:29318
as_token: ${WHATSAPP_AS_TOKEN}
hs_token: ${WHATSAPP_HS_TOKEN}
sender_localpart: whatsappbot
rate_limited: false
namespaces:
  users:
    - exclusive: true
      regex: '@whatsapp_[0-9a-z_-]+:${MATRIX_SERVER_NAME}'
  aliases: []
  rooms: []
EOF

echo "Wrote /registrations/whatsapp-registration.yaml"

# ── Signal registration ─────────────────────────────────────────────────────
cat > /registrations/signal-registration.yaml <<EOF
id: signal
url: http://mautrix-signal:29328
as_token: ${SIGNAL_AS_TOKEN}
hs_token: ${SIGNAL_HS_TOKEN}
sender_localpart: signalbot
rate_limited: false
namespaces:
  users:
    - exclusive: true
      regex: '@signal_[0-9a-z_-]+:${MATRIX_SERVER_NAME}'
  aliases: []
  rooms: []
EOF

echo "Wrote /registrations/signal-registration.yaml"
echo "Setup complete."
