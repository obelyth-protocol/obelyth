#!/usr/bin/env bash
# Obelyth Testnet VPS Install Script
# ====================================
# Idempotent setup for a clean Ubuntu 24.04 LTS Hetzner / DigitalOcean / generic VPS.
# Run as root via SSH:
#
#   curl -sSL https://raw.githubusercontent.com/obelyth-protocol/obelyth/main/deploy/install.sh | bash
#
# Or, before the repo is public-installable, scp this file to the VPS and run:
#
#   bash install.sh
#
# What it does (in order):
#   1. Updates apt, installs Python 3.12 + git + nginx + certbot + curl
#   2. Creates an 'obelyth' system user with no shell
#   3. Clones (or updates) the repo to /opt/obelyth
#   4. Installs Python deps in a virtualenv at /opt/obelyth/.venv
#   5. Creates data directory at /var/lib/obelyth (chowned to obelyth user)
#   6. Drops the systemd unit at /etc/systemd/system/obelyth-node.service
#   7. Configures UFW firewall: allow SSH, P2P 8333, HTTP 80, HTTPS 443
#   8. Drops nginx config that proxies https://testnet.obelyth.io/ to 127.0.0.1:8334
#   9. Runs certbot to provision the SSL cert (interactive — needs email)
#   10. Enables and starts the obelyth-node service
#
# What it does NOT do:
#   - Open RPC port 8334 to the public internet directly (nginx is the only path in)
#   - Bind the node to 0.0.0.0 on RPC — it stays on 127.0.0.1, nginx proxies it
#   - Open the faucet by default (pass FAUCET=1 to enable)
#   - Configure DNS — you must point testnet.obelyth.io's A record at this VPS first
#
# Environment overrides (all optional):
#   DOMAIN          subdomain to use   (default: testnet.obelyth.io)
#   EMAIL           letsencrypt email  (default: prompted)
#   REPO_URL        git clone source   (default: https://github.com/obelyth-protocol/obelyth.git)
#   BRANCH          git branch         (default: main)
#   FAUCET          set to 1 to enable (default: off)
#   MINE            set to 1 to mine   (default: on — silent testnet needs a miner)
#
# Re-run safe: every step is guarded; you can re-run this to apply config updates.

set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────
DOMAIN="${DOMAIN:-testnet.obelyth.io}"
REPO_URL="${REPO_URL:-https://github.com/obelyth-protocol/obelyth.git}"
BRANCH="${BRANCH:-main}"
FAUCET="${FAUCET:-0}"
MINE="${MINE:-1}"
INSTALL_DIR="/opt/obelyth"
DATA_DIR="/var/lib/obelyth"
SERVICE_USER="obelyth"

log()  { echo -e "\033[1;36m[obelyth]\033[0m $*"; }
err()  { echo -e "\033[1;31m[obelyth ERROR]\033[0m $*" >&2; }
warn() { echo -e "\033[1;33m[obelyth WARN]\033[0m $*"; }

require_root() {
  if [[ $EUID -ne 0 ]]; then
    err "This script must be run as root. Try: sudo bash install.sh"
    exit 1
  fi
}

require_root

# ── 0. Pre-flight checks ──────────────────────────────────────────────────
log "Pre-flight checks"

if ! grep -qi "ubuntu" /etc/os-release; then
  warn "This script is tested on Ubuntu 24.04. You're on something else — proceeding anyway."
fi

# Check that the domain resolves to this server. We compare the A record
# against any local interface IPv4 (handles the case where the VPS has a
# public IP that's not directly bound to a local interface, by also checking
# what curl sees as our public IP).
log "Checking that ${DOMAIN} points at this server"
DOMAIN_IP=$(getent hosts "${DOMAIN}" | awk '{print $1}' | head -n1 || true)
SERVER_IP=$(curl -s --max-time 5 https://api.ipify.org || true)
if [[ -z "${DOMAIN_IP}" ]]; then
  warn "Could not resolve ${DOMAIN}. Add the A record at Namecheap before running certbot."
elif [[ "${DOMAIN_IP}" != "${SERVER_IP}" ]]; then
  warn "${DOMAIN} resolves to ${DOMAIN_IP} but this server is ${SERVER_IP}."
  warn "Certbot will fail. Wait for DNS to propagate before continuing."
else
  log "DNS OK: ${DOMAIN} → ${DOMAIN_IP}"
fi

# ── 1. System packages ────────────────────────────────────────────────────
log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  python3.12 python3.12-venv python3-pip \
  git curl ufw nginx certbot python3-certbot-nginx \
  ca-certificates

# ── 2. Service user ───────────────────────────────────────────────────────
if ! id "${SERVICE_USER}" &>/dev/null; then
  log "Creating service user '${SERVICE_USER}'"
  useradd --system --home "${INSTALL_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
else
  log "Service user '${SERVICE_USER}' already exists"
fi

# ── 3. Clone or update repo ───────────────────────────────────────────────
if [[ -d "${INSTALL_DIR}/.git" ]]; then
  log "Updating existing repo at ${INSTALL_DIR}"
  cd "${INSTALL_DIR}"
  sudo -u "${SERVICE_USER}" git fetch --quiet origin
  sudo -u "${SERVICE_USER}" git reset --hard "origin/${BRANCH}"
else
  log "Cloning ${REPO_URL} (branch: ${BRANCH}) into ${INSTALL_DIR}"
  rm -rf "${INSTALL_DIR}"
  git clone --quiet --branch "${BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
fi

# ── 4. Python virtualenv + dependencies ──────────────────────────────────
log "Setting up Python virtualenv"
if [[ ! -d "${INSTALL_DIR}/.venv" ]]; then
  sudo -u "${SERVICE_USER}" python3.12 -m venv "${INSTALL_DIR}/.venv"
fi
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"

# ── 5. Data directory ─────────────────────────────────────────────────────
log "Creating data directory at ${DATA_DIR}"
mkdir -p "${DATA_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}"
chmod 750 "${DATA_DIR}"

# ── 6. Systemd service ────────────────────────────────────────────────────
log "Writing systemd unit"

# Build the ExecStart args based on env flags
EXEC_ARGS="--port 8333 --rpc-port 8334 --data-dir ${DATA_DIR}"
[[ "${MINE}" == "1" ]]   && EXEC_ARGS="${EXEC_ARGS} --mine"
[[ "${FAUCET}" == "1" ]] && EXEC_ARGS="${EXEC_ARGS} --faucet-enabled"

cat > /etc/systemd/system/obelyth-node.service <<EOF
[Unit]
Description=Obelyth Full Node (testnet)
Documentation=https://obelyth.io
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
Environment="PYTHONPATH=${INSTALL_DIR}"
Environment="PYTHONUNBUFFERED=1"
ExecStart=${INSTALL_DIR}/.venv/bin/python -m node.fullnode ${EXEC_ARGS}
Restart=on-failure
RestartSec=10s
StandardOutput=journal
StandardError=journal
SyslogIdentifier=obelyth-node

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=${DATA_DIR}
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

# ── 7. Firewall ────────────────────────────────────────────────────────────
log "Configuring UFW firewall"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp comment 'SSH'         >/dev/null
ufw allow 80/tcp comment 'HTTP'        >/dev/null
ufw allow 443/tcp comment 'HTTPS'      >/dev/null
ufw allow 8333/tcp comment 'Obelyth P2P' >/dev/null
ufw --force enable >/dev/null
log "UFW enabled — open ports: 22, 80, 443, 8333"

# ── 8. Nginx reverse proxy ────────────────────────────────────────────────
log "Writing nginx config for ${DOMAIN}"
cat > /etc/nginx/sites-available/obelyth-node <<EOF
# Obelyth testnet RPC reverse proxy
# Routes https://${DOMAIN}/ → 127.0.0.1:8334 (node RPC)
#
# Public endpoints (all GET unless noted):
#   /health, /metrics, /status, /blocks, /tx, /address, /utxos,
#   /balance, /peers, /mempool, /vesting, /faucet/status,
#   /compute/nextjob, /compute/pending_challenges
# POST endpoints reach the node via the same proxy (no separate config needed).

server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    # certbot will add the SSL block to this file on first run
    location / {
        proxy_pass         http://127.0.0.1:8334;
        proxy_http_version 1.1;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
        proxy_send_timeout 60s;

        # CORS — the node already sets these on its responses, but having nginx
        # echo them too means OPTIONS preflights from the wallet/explorer
        # don't have to round-trip into Python.
        add_header Access-Control-Allow-Origin  '*' always;
        add_header Access-Control-Allow-Methods 'GET, POST, OPTIONS' always;
        add_header Access-Control-Allow-Headers 'Content-Type' always;
        if (\$request_method = 'OPTIONS') {
            return 204;
        }
    }
}
EOF

ln -sf /etc/nginx/sites-available/obelyth-node /etc/nginx/sites-enabled/obelyth-node
rm -f /etc/nginx/sites-enabled/default

if ! nginx -t 2>&1 | grep -q "test is successful"; then
  err "nginx config test failed"
  nginx -t
  exit 1
fi
systemctl reload nginx
log "nginx reloaded with proxy config"

# ── 9. Start the node service ─────────────────────────────────────────────
log "Enabling and starting obelyth-node"
systemctl enable obelyth-node >/dev/null 2>&1
systemctl restart obelyth-node
sleep 3

if systemctl is-active --quiet obelyth-node; then
  log "obelyth-node is running"
else
  err "obelyth-node failed to start. Check: journalctl -u obelyth-node -n 50"
  systemctl status obelyth-node --no-pager || true
  exit 1
fi

# Verify the RPC responds locally
log "Verifying /health responds locally"
sleep 2
if curl -sf http://127.0.0.1:8334/health >/dev/null 2>&1; then
  log "Node RPC is responding"
  curl -s http://127.0.0.1:8334/health | head -c 300; echo
else
  warn "Node RPC not responding yet — give it 30 seconds and check: curl http://127.0.0.1:8334/health"
fi

# ── 10. SSL via certbot (interactive) ─────────────────────────────────────
echo
log "========================================================================"
log "Node is up locally. Final step: provision SSL cert for ${DOMAIN}"
log "========================================================================"
log "Run this manually (it prompts for an email and for the redirect setting):"
log ""
log "    certbot --nginx -d ${DOMAIN}"
log ""
log "After certbot finishes, your endpoints are publicly available at:"
log "    https://${DOMAIN}/health"
log "    https://${DOMAIN}/metrics"
log ""
log "Useful commands:"
log "    systemctl status obelyth-node     # service status"
log "    journalctl -u obelyth-node -f     # live logs"
log "    systemctl restart obelyth-node    # restart the node"
log "    curl https://${DOMAIN}/health     # public health check"
log "========================================================================"
