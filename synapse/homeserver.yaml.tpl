server_name: "${MATRIX_SERVER_NAME}"
pid_file: /data/homeserver.pid

# No public_baseurl — this Synapse instance is bridge-internal only.
# Federation is disabled; bridges reach it at http://synapse:8008.

listeners:
  - port: 8008
    bind_addresses: ["0.0.0.0"]
    tls: false
    type: http
    x_forwarded: false
    resources:
      - names: [client, media]
        compress: false

database:
  name: psycopg2
  args:
    user: synapse
    password: "${SYNAPSE_DB_PASSWORD}"
    database: synapse
    host: postgres-synapse
    port: 5432
    cp_min: 5
    cp_max: 10

log_config: "/data/log.config"

media_store_path: /data/media_store

registration_shared_secret: "${SYNAPSE_REGISTRATION_SECRET}"

report_stats: ${SYNAPSE_REPORT_STATS}

macaroon_secret_key: "${SYNAPSE_MACAROON_SECRET}"
form_secret: "${SYNAPSE_FORM_SECRET}"

signing_key_path: "/data/homeserver.signing.key"

# No federation — this server is only used to glue the bridges together.
federation_domain_whitelist: []
trusted_key_servers: []
suppress_key_server_warning: true

# Bridge application services
app_service_config_files:
  - /registrations/whatsapp-registration.yaml
  - /registrations/signal-registration.yaml

# Disable open registration — use the shared secret for bot accounts
enable_registration: false

# Relaxed rate limits — this server is on a private Docker network,
# only accessed by the bridges and the setup-ui service.
rc_login:
  address:
    per_second: 10
    burst_count: 50
  account:
    per_second: 10
    burst_count: 50
  failed_attempts:
    per_second: 10
    burst_count: 50

rc_message:
  per_second: 50
  burst_count: 200
