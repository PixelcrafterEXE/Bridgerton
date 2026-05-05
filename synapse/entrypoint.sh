#!/bin/bash
set -euo pipefail

: "${MATRIX_SERVER_NAME:?}"
: "${SYNAPSE_DB_PASSWORD:?}"
: "${SYNAPSE_REGISTRATION_SECRET:?}"
: "${SYNAPSE_MACAROON_SECRET:?}"
: "${SYNAPSE_FORM_SECRET:?}"
: "${SYNAPSE_ADMIN_USER:?}"
: "${SYNAPSE_ADMIN_PASSWORD:?}"
: "${SYNAPSE_REPORT_STATS:=no}"

# ── Render config with Python (envsubst not present in this image) ──────────
python3 - <<'PYEOF'
import os, re

with open('/homeserver.yaml.tpl') as f:
    tpl = f.read()

result = re.sub(
    r'\$\{([A-Z_][A-Z0-9_]*)\}',
    lambda m: os.environ.get(m.group(1), m.group(0)),
    tpl
)

with open('/data/homeserver.yaml', 'w') as f:
    f.write(result)

with open('/log.config.tpl') as f:
    tpl = f.read()

with open('/data/log.config', 'w') as f:
    f.write(tpl)
PYEOF

# ── Generate signing key once ───────────────────────────────────────────────
if [ ! -f /data/homeserver.signing.key ]; then
    python3 -c "
from signedjson.key import generate_signing_key, write_signing_keys
key = generate_signing_key('auto')
write_signing_keys(open('/data/homeserver.signing.key', 'w'), [key])
print('Generated signing key.')
"
fi

# ── Create admin user on first boot ────────────────────────────────────────
FIRST_BOOT_FLAG=/data/.admin_created
if [ ! -f "$FIRST_BOOT_FLAG" ]; then
    echo "First boot: starting Synapse temporarily to create admin account…"

    python3 -m synapse.app.homeserver --config-path /data/homeserver.yaml --daemonize

    for i in $(seq 1 30); do
        curl -fsSL http://localhost:8008/_matrix/client/versions >/dev/null 2>&1 && break
        sleep 2
    done

    register_new_matrix_user \
        -u "${SYNAPSE_ADMIN_USER}" \
        -p "${SYNAPSE_ADMIN_PASSWORD}" \
        -a \
        -k "${SYNAPSE_REGISTRATION_SECRET}" \
        http://localhost:8008 \
      && touch "$FIRST_BOOT_FLAG" \
      || echo "Warning: admin user creation failed (may already exist)"

    pid=$(cat /data/homeserver.pid 2>/dev/null || true)
    [ -n "$pid" ] && kill "$pid" && sleep 3
fi

echo "Starting Synapse…"
exec python3 -m synapse.app.homeserver --config-path /data/homeserver.yaml
