# Bridgerton

A self-contained Docker Compose stack that bridges **Signal** and **WhatsApp** together through a private [Matrix](https://matrix.org/) homeserver using [mautrix](https://github.com/mautrix) bridges.

```
Signal ←──→ mautrix-signal ←──→ Synapse (Matrix) ←──→ mautrix-whatsapp ←──→ WhatsApp
```

Synapse runs purely internal. No ports are exposed and no user other than the Bridge is allowed.


---
## How relay works

A Matrix account sits in both portal rooms. When a message arrives in either room the Bridgerton service copies it to the other room, where the receiving bridge delivers it to the bridget service.

### Supported message types

| Type | Behaviour |
|---|---|
| Text | Forwarded with a sender header (`Name:`) shown before the first message of each consecutive run; header re-appears after any message from the other side breaks the sequence |
| Images | Forwarded; sender header shown on context change |
| Video | Forwarded; sender header shown on context change |
| Audio / voice notes | Forwarded; sender header shown on context change |
| Files / documents | Forwarded; sender header shown on context change |
| Locations | Forwarded (geo_uri preserved); sender header shown on context change |
| Stickers | Forwarded with an always-present `Name sent a sticker:` prefix message |
| Reactions | Cannot be bridged (bot can only hold one reaction per event); sent as `Name reacted with 👍` text notice instead |
| Replies | Thread IDs translated across the relay pair |
| Deletes | Redactions propagated to the mirror event |
| Polls | Sent as a text summary attributed to the sender — polls cannot be bridged across networks |
| Contacts / calendar | Forwarded if the bridge exposes them as text or file; otherwise a `[Unsupported message type: …]` notice is sent so the recipient knows a message was missed |


---


## Requirements

- Docker Engine 24+ with the Compose plugin (`docker compose`)
- `openssl` (for generating secrets)

---

## Quick start

### 1. Copy and fill in `.env`

```bash
cp .env.example .env
```

Generate all blank secret fields:

```bash
# Synapse secrets
echo "SYNAPSE_REGISTRATION_SECRET=$(openssl rand -hex 32)"
echo "SYNAPSE_MACAROON_SECRET=$(openssl rand -hex 32)"
echo "SYNAPSE_FORM_SECRET=$(openssl rand -hex 32)"

# Database passwords
echo "SYNAPSE_DB_PASSWORD=$(openssl rand -hex 16)"
echo "SIGNAL_DB_PASSWORD=$(openssl rand -hex 16)"
echo "WHATSAPP_DB_PASSWORD=$(openssl rand -hex 16)"

# Bridge tokens (AS = bridge→Synapse, HS = Synapse→bridge)
echo "WHATSAPP_AS_TOKEN=$(openssl rand -hex 32)"
echo "WHATSAPP_HS_TOKEN=$(openssl rand -hex 32)"
echo "SIGNAL_AS_TOKEN=$(openssl rand -hex 32)"
echo "SIGNAL_HS_TOKEN=$(openssl rand -hex 32)"
```

Paste the output values into `.env`. Also set:

| Variable | Description |
|---|---|
| `MATRIX_SERVER_NAME` | Any hostname (e.g. `bridge.local`) — not DNS-resolved, just used as Matrix user IDs |
| `SYNAPSE_ADMIN_USER` | Username for the local admin account |
| `SYNAPSE_ADMIN_PASSWORD` | Password for the admin account |

### 2. Start the stack

```bash
docker compose up -d
```

Boot order is enforced:

1. `setup` writes bridge registration YAMLs (one-shot, exits 0)
2. `synapse` starts once `setup` completes and Postgres is healthy; creates the admin account on first boot
3. `mautrix-whatsapp` and `mautrix-signal` start once Synapse passes its health check
4. `setup-ui` starts and is available at **http://localhost:8080**

### 3. Link your accounts via the web UI

Open **http://localhost:8080** in a browser.

**WhatsApp**

1. Click "Link WhatsApp" — a QR code appears.
2. In WhatsApp on your phone: Settings → Linked Devices → Link a Device.
3. Scan the QR code.

**Signal**

1. Click "Link Signal" — a QR code appears.
2. In Signal on your phone: Settings → Linked Devices → tap "+".
3. Scan the QR code.

Both cards show "Linked" once connected.

### 4. Signal groups are synced automatically

When Signal is linked the bridge automatically runs a portal sync, creating Matrix rooms for all existing Signal groups. No manual steps are required.

WhatsApp groups also appear automatically once the account is linked.

### 5. Create a relay

1. In the web UI, select a WhatsApp group on the left and a Signal group on the right.
2. Click "Create Relay".

Messages are now forwarded in both directions.

---
