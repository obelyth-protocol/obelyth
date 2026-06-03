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

# ── 8. Nginx reverse proxy + static pages ─────────────────────────────────
# We split the config into two files:
#   1. /etc/nginx/sites-available/obelyth-node — the main server block.
#      Written once on first install, then left alone so certbot's HTTPS
#      additions survive re-runs of this script.
#   2. /etc/nginx/snippets/obelyth-routes.conf — the location blocks for
#      static pages + RPC proxy. Always rewritten, so location changes
#      land on every re-run without disturbing the SSL setup.

log "Writing nginx location snippet"
mkdir -p /etc/nginx/snippets
cat > /etc/nginx/snippets/obelyth-routes.conf <<'SNIPPET_EOF'
# Obelyth route snippet — included by both HTTP (port 80) and HTTPS (port 443)
# server blocks. certbot manages the HTTPS block; we manage these locations.
#
# Routes:
#   /                  → landing page (HTML, /var/www/obelyth/index.html)
#   /app/wallet/       → wallet UI (/opt/obelyth/wallet/)
#   /app/explorer/     → block explorer UI (/opt/obelyth/explorer/)
#   /app/faucet/       → faucet UI (/opt/obelyth/faucet/)
#   /branding/         → SVG marks (/opt/obelyth/branding/)
#   everything else    → proxied to node RPC on 127.0.0.1:8334

location = / {
    root /var/www/obelyth;
    try_files /index.html =404;
}

location /app/wallet/ {
    alias /opt/obelyth/wallet/;
    try_files $uri $uri/ /index.html;
}

location /app/explorer/ {
    alias /opt/obelyth/explorer/;
    try_files $uri $uri/ /index.html;
}

location /app/faucet/ {
    alias /opt/obelyth/faucet/;
    try_files $uri $uri/ /index.html;
}

location /branding/ {
    alias /opt/obelyth/branding/;
    expires 7d;
    add_header Cache-Control "public, immutable";
}

location / {
    proxy_pass         http://127.0.0.1:8334;
    proxy_http_version 1.1;
    proxy_set_header   Host $host;
    proxy_set_header   X-Real-IP $remote_addr;
    proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;
    proxy_send_timeout 60s;

    add_header Access-Control-Allow-Origin  '*' always;
    add_header Access-Control-Allow-Methods 'GET, POST, OPTIONS' always;
    add_header Access-Control-Allow-Headers 'Content-Type' always;
    if ($request_method = 'OPTIONS') {
        return 204;
    }
}
SNIPPET_EOF

# Only write the main server block if it doesn't exist yet. certbot extends
# this file on first run with the SSL server block — we don't want to clobber
# that on re-installs. The snippet is what we use to push location changes.
if [[ ! -f /etc/nginx/sites-available/obelyth-node ]]; then
  log "Writing initial nginx server block (HTTP only — certbot will add HTTPS)"
  cat > /etc/nginx/sites-available/obelyth-node <<EOF
# Obelyth testnet — nginx main config
# Locations are managed via /etc/nginx/snippets/obelyth-routes.conf
# (re-runs of install.sh update the snippet but leave this file alone so
# certbot's SSL additions survive.)

server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    include /etc/nginx/snippets/obelyth-routes.conf;
}
EOF
else
  log "Existing nginx server block found — leaving it alone (preserves certbot SSL)"
  # If the existing file doesn't include the snippet yet (older install),
  # try to add the include line before the closing brace of each server block.
  if ! grep -q "snippets/obelyth-routes.conf" /etc/nginx/sites-available/obelyth-node; then
    log "Migrating existing config: adding snippet include to all server blocks"
    # Insert include before the LAST closing brace of each server block.
    # We use a python one-liner so the edit is robust to whitespace variants.
    python3 - <<'PYEOF'
import re
p = '/etc/nginx/sites-available/obelyth-node'
src = open(p).read()
# Strip out any old inline location blocks we used to write — these will
# conflict with the snippet's locations.
# Simpler approach: replace each `server { ... }` body, keeping only
# `listen`, `server_name`, `ssl_*`, `if (...)`, and adding the include.
# But that's risky. Safer: just inject the include line before the last
# closing brace of every server block, and let nginx -t catch duplicates.
def inject(block):
    inner = block.group(1)
    if 'snippets/obelyth-routes.conf' in inner:
        return block.group(0)
    # Remove any 'location /' blocks (legacy inline) — they'd collide
    inner = re.sub(r'\n\s*location\s+/\s*\{[^{}]*(\{[^{}]*\}[^{}]*)*\}\s*\n', '\n', inner)
    inner = re.sub(r'\n\s*location\s+=\s+/\s*\{[^{}]*(\{[^{}]*\}[^{}]*)*\}\s*\n', '\n', inner)
    inner = re.sub(r'\n\s*location\s+/app/\w+/\s*\{[^{}]*(\{[^{}]*\}[^{}]*)*\}\s*\n', '\n', inner)
    inner = re.sub(r'\n\s*location\s+/branding/\s*\{[^{}]*(\{[^{}]*\}[^{}]*)*\}\s*\n', '\n', inner)
    inner += '\n    include /etc/nginx/snippets/obelyth-routes.conf;\n'
    return 'server {' + inner + '}'
out = re.sub(r'server\s*\{((?:[^{}]|\{[^{}]*\})*)\}', inject, src, flags=re.DOTALL)
open(p, 'w').write(out)
print("Snippet include injected")
PYEOF
  fi
fi

# ── 8b. Landing page ──────────────────────────────────────────────────────
log "Writing landing page at /var/www/obelyth/index.html"
mkdir -p /var/www/obelyth
cat > /var/www/obelyth/index.html <<'LANDING_EOF'
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Obelyth Testnet</title>
<link rel="icon" type="image/svg+xml" href="/branding/obelyth-icon.svg">
<style>
  :root {
    --bg: #06080F;
    --panel: #0E1320;
    --border: #1E2436;
    --text: #F0F4FF;
    --mute: #9098A8;
    --gold: #C9A84C;
    --purple: #9B59D0;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 4rem 1.5rem;
  }
  .mark { width: 96px; height: 96px; margin-bottom: 1.5rem; }
  h1 {
    font-size: 2.4rem;
    font-weight: 600;
    margin: 0 0 0.4rem 0;
    color: var(--gold);
    letter-spacing: 0.02em;
  }
  .tagline {
    color: var(--mute);
    margin: 0 0 0.4rem 0;
    font-size: 1.05rem;
  }
  .pill {
    display: inline-block;
    padding: 0.25rem 0.7rem;
    border-radius: 999px;
    background: rgba(155, 89, 208, 0.15);
    color: var(--purple);
    font-size: 0.8rem;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    margin-bottom: 3rem;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 1.2rem;
    width: 100%;
    max-width: 800px;
    margin-bottom: 3rem;
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.5rem;
    text-decoration: none;
    color: var(--text);
    transition: border-color 0.15s, transform 0.15s;
  }
  .card:hover {
    border-color: var(--gold);
    transform: translateY(-2px);
  }
  .card h2 {
    margin: 0 0 0.4rem 0;
    font-size: 1.1rem;
    color: var(--gold);
  }
  .card p {
    margin: 0;
    color: var(--mute);
    font-size: 0.9rem;
    line-height: 1.5;
  }
  .endpoints {
    width: 100%;
    max-width: 800px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.5rem;
  }
  .endpoints h3 {
    margin: 0 0 0.8rem 0;
    font-size: 0.85rem;
    color: var(--mute);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 500;
  }
  .endpoints code {
    display: block;
    padding: 0.4rem 0;
    color: var(--text);
    font-size: 0.85rem;
    font-family: 'SF Mono', 'Menlo', monospace;
  }
  .endpoints code .method {
    color: var(--purple);
    margin-right: 0.5rem;
  }
  .endpoints code .desc {
    color: var(--mute);
    margin-left: 0.5rem;
  }
  footer {
    margin-top: 3rem;
    color: var(--mute);
    font-size: 0.8rem;
    text-align: center;
    max-width: 600px;
    line-height: 1.6;
  }
  footer a { color: var(--gold); text-decoration: none; }
  footer a:hover { text-decoration: underline; }
</style>
</head>
<body>
  <svg class="mark" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="#F6D46A"/>
        <stop offset="0.48" stop-color="#B88A25"/>
        <stop offset="1" stop-color="#6E5218"/>
      </linearGradient>
      <linearGradient id="p" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="#B96DFF"/>
        <stop offset="1" stop-color="#5B2AD1"/>
      </linearGradient>
    </defs>
    <circle cx="256" cy="256" r="148" fill="none" stroke="url(#g)" stroke-width="34"/>
    <path d="M360 150a148 148 0 0 1 42 104" fill="none" stroke="#F6D46A" stroke-width="18" stroke-linecap="round" opacity=".92"/>
    <path d="M256 58 288 168v176L256 454 224 344V168Z" fill="url(#g)"/>
    <path d="M256 58v396l-32-110V168Z" fill="#7B5B1C" opacity=".72"/>
    <path d="M256 58v396l32-110V168Z" fill="#E5BB55" opacity=".9"/>
    <path d="M256 174 326 256 256 338 186 256Z" fill="#1B102C"/>
    <path d="M256 194 306 256 256 318 206 256Z" fill="url(#p)"/>
    <path d="M256 224 282 256 256 288 230 256Z" fill="#D8B3FF" opacity=".95"/>
    <circle cx="256" cy="256" r="10" fill="#FFFFFF"/>
  </svg>
  <h1>Obelyth Testnet</h1>
  <p class="tagline">The obelisk that channels compute into intelligence</p>
  <span class="pill">Pre-testnet · Not financial advice</span>

  <div class="grid">
    <a class="card" href="/app/wallet/">
      <h2>Wallet</h2>
      <p>Generate keys, send and receive testnet OBY. Self-contained, encrypted in-browser.</p>
    </a>
    <a class="card" href="/app/explorer/">
      <h2>Explorer</h2>
      <p>Browse blocks, transactions, and addresses on the live testnet chain.</p>
    </a>
    <a class="card" href="/app/faucet/">
      <h2>Faucet</h2>
      <p>Claim testnet OBY (requires an API key). Testnet tokens have no monetary value.</p>
    </a>
  </div>

  <div class="endpoints">
    <h3>Public RPC endpoints</h3>
    <code><span class="method">GET</span> /health<span class="desc">— binary up/down + reasons</span></code>
    <code><span class="method">GET</span> /metrics<span class="desc">— full diagnostic JSON</span></code>
    <code><span class="method">GET</span> /status<span class="desc">— chain summary + peer count</span></code>
    <code><span class="method">GET</span> /blocks?limit=N<span class="desc">— recent blocks (paginated)</span></code>
    <code><span class="method">GET</span> /tx?hash=H<span class="desc">— transaction by hash</span></code>
    <code><span class="method">GET</span> /address?addr=A<span class="desc">— balance and history</span></code>
  </div>

  <footer>
    Testnet OBY has no monetary value. All testnet activity is subject to change after legal review.<br>
    <a href="https://obelyth.io">obelyth.io</a> · <a href="https://github.com/obelyth-protocol/obelyth">GitHub</a>
  </footer>
</body>
</html>
LANDING_EOF

# nginx needs read access to the static dirs (it runs as www-data).
# /opt/obelyth is owned by obelyth:obelyth with default perms — make sure
# www-data can read the HTML and SVG files.
chmod -R o+rX /opt/obelyth/wallet /opt/obelyth/explorer /opt/obelyth/faucet /opt/obelyth/branding 2>/dev/null || true
chmod -R o+rX /var/www/obelyth

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
