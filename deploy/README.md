# Obelyth Testnet VPS Deployment Guide

End-to-end walkthrough for taking a fresh Ubuntu 24.04 VPS to a running
Obelyth node behind nginx + Let's Encrypt SSL, with UptimeRobot monitoring
and chain-state backups.

Expected time: **45 minutes**, most of which is waiting for DNS propagation
and certbot.

## Before you start

You need:

- A Hetzner / DigitalOcean / any cloud VPS account
- Access to Namecheap (or wherever obelyth.io DNS is managed)
- An UptimeRobot account (free tier is fine — sign up at uptimerobot.com)
- A Discord channel where you want alerts to land

## Step 1 — Provision the VPS

**Recommended: Hetzner CX22** ($4/mo, 2 vCPU, 4 GB RAM, 40 GB SSD)

1. Sign in at console.hetzner.cloud
2. New project → name it "Obelyth"
3. Add server:
   - **Location**: Ashburn, VA (us-east) for North America, or Falkenstein for EU
   - **Image**: Ubuntu 24.04
   - **Type**: CX22 (Shared vCPU, x86)
   - **Networking**: Public IPv4 + IPv6
   - **SSH keys**: add your public key (paste contents of `~/.ssh/id_rsa.pub` from your local machine, or generate one if you don't have one)
   - **Name**: `obelyth-testnet-01`
4. Click "Create & Buy now"
5. Note the public IPv4 address that appears (call this `$VPS_IP`)

**Alternative: DigitalOcean basic droplet** ($6/mo, 1 GB RAM)

Pick Ubuntu 24.04, basic shared CPU, NYC1 or SFO3, add SSH key, create.

## Step 2 — Point DNS at the VPS

In Namecheap:

1. Domain List → obelyth.io → Manage → Advanced DNS
2. Add A record:
   - **Type**: A Record
   - **Host**: `testnet`
   - **Value**: `$VPS_IP` (the address from Step 1)
   - **TTL**: 1 min
3. Save

Verify from your local machine after 1-2 minutes:

```bash
dig +short testnet.obelyth.io
# Should print $VPS_IP
```

If it doesn't resolve after 5 minutes, double-check the A record and wait.
Don't proceed until DNS resolves correctly — certbot will fail otherwise.

## Step 3 — SSH into the VPS and run the install

From your local machine:

```bash
ssh root@$VPS_IP
```

You should land at a root shell. From there:

```bash
# Download the install script directly from the repo
curl -sSL -o install.sh https://raw.githubusercontent.com/obelyth-protocol/obelyth/main/deploy/install.sh
chmod +x install.sh
bash install.sh
```

This takes about 3-5 minutes. It will:

- Install Python, nginx, certbot, ufw
- Create the `obelyth` system user
- Clone the repo to `/opt/obelyth`
- Install Python deps in a venv
- Set up systemd unit, firewall, nginx reverse proxy
- Start the node service

You should see green `[obelyth]` lines stepping through each phase. The
script ends by printing the `certbot` command you should run next.

## Step 4 — Provision the SSL certificate

Still on the VPS as root:

```bash
certbot --nginx -d testnet.obelyth.io
```

It will ask for:

- **Your email** — for cert expiry warnings; use a real address you check
- **TOS agreement** — type `Y`
- **Newsletter** — `N` (your call)
- **HTTPS redirect** — pick `2` (redirect all HTTP to HTTPS)

About 30 seconds later you'll have a working SSL cert. Verify from your local machine:

```bash
curl https://testnet.obelyth.io/health
```

Should return JSON: `{"status":"ok","uptime_s":N,"height":N}`.

## Step 5 — Set up UptimeRobot monitoring

1. Sign in at uptimerobot.com
2. New Monitor:
   - **Type**: HTTP(s)
   - **Friendly name**: `Obelyth Testnet — Health`
   - **URL**: `https://testnet.obelyth.io/health`
   - **Monitoring Interval**: 5 minutes (free tier max frequency)
   - **HTTP method**: GET
   - **Alert when**: Status code is not 200
3. Save
4. **My Settings → Add Alert Contact** → Discord webhook
   - Get a webhook from your Discord server: Server Settings → Integrations → Webhooks → New Webhook → copy URL
   - Paste into UptimeRobot
   - Test the alert from UptimeRobot's interface
5. Edit the monitor → assign the Discord contact

You can repeat the process for `https://testnet.obelyth.io/status` if you
want a second canary (catches node-down even when /health would still
return 200 from cached nginx).

## Step 6 — Enable chain state backups

Still on the VPS:

```bash
cp /opt/obelyth/deploy/backup.sh /usr/local/bin/obelyth-backup.sh
chmod +x /usr/local/bin/obelyth-backup.sh

# Add to root's crontab — every 6 hours
(crontab -l 2>/dev/null; echo "0 */6 * * * /usr/local/bin/obelyth-backup.sh >> /var/log/obelyth-backup.log 2>&1") | crontab -

# Verify
crontab -l
```

Backups land at `/var/lib/obelyth/backups/chain_state_TIMESTAMP.json`,
rotated after 14 days. If the chain ever corrupts, you can stop the
service, restore a backup over `/var/lib/obelyth/chain_state.json`, and
restart.

## Step 7 — Verify and stress-test

Run these from your local machine (not the VPS):

```bash
# Health and metrics
curl https://testnet.obelyth.io/health
curl https://testnet.obelyth.io/metrics

# Chain state
curl https://testnet.obelyth.io/status

# Blocks
curl 'https://testnet.obelyth.io/blocks?limit=5'

# Should all return 200 with sensible JSON
```

Then walk away for 4 hours. When you come back, run:

```bash
curl -s https://testnet.obelyth.io/metrics | python3 -m json.tool
```

Verify:

- `chain.height` > 0 (node is mining blocks)
- `persistence.save_count` > 0 (persist loop is working)
- `persistence.last_save_duration_ms` is small (< 100ms is fine)
- `counters.persist_save_failures` == 0
- `node.uptime_s` is large (no restart loops)

If all of those check out, the node is healthy.

## Operating the node

### Live logs
```bash
ssh root@$VPS_IP
journalctl -u obelyth-node -f
```

### Restart
```bash
systemctl restart obelyth-node
```

### Status
```bash
systemctl status obelyth-node
```

### Update to latest code
```bash
bash /opt/obelyth/deploy/install.sh   # idempotent — pulls latest from main and restarts
```

### Stop the node
```bash
systemctl stop obelyth-node
```

### Inspect chain state on disk
```bash
ls -la /var/lib/obelyth/
du -sh /var/lib/obelyth/chain_state.json
```

### Manual backup right now
```bash
/usr/local/bin/obelyth-backup.sh
```

## Troubleshooting

### `certbot` failed with "Connection refused"
Your DNS hasn't propagated yet. Check `dig +short testnet.obelyth.io` from
your local machine; it should match `$VPS_IP`. If not, wait and retry.

### Node service won't start
```bash
journalctl -u obelyth-node -n 100 --no-pager
```
Read the traceback. Common causes:
- Missing dependency (re-run `install.sh`)
- Port already in use (something else on 8333 or 8334)
- Data directory permissions (`chown -R obelyth:obelyth /var/lib/obelyth`)

### `/health` returns 503
The node is unhealthy. Check `/metrics` for the `persistence` block and
`counters` to see what's failing. Usually means the persist loop hasn't
fired yet (during boot) or has been failing (disk full, permission issue).

### nginx returns 502 Bad Gateway
The node isn't running or isn't listening on 127.0.0.1:8334.
```bash
systemctl status obelyth-node
ss -tlnp | grep 8334
```

### Out of disk
Backups + chain growth can fill the disk over months.
```bash
df -h /var/lib/obelyth
du -sh /var/lib/obelyth/*
```
If backups are eating space, shorten retention by editing `KEEP_DAYS`
in `/usr/local/bin/obelyth-backup.sh`.

## Security notes

- **RPC port 8334 is NOT exposed to the public internet.** Only nginx
  (on 443) talks to it, over localhost.
- **The `obelyth` user has no shell.** Can't be SSH'd into directly.
- **Systemd hardening is enabled** — the service can only write to
  `/var/lib/obelyth`. Even a code-execution bug in the node can't
  trash the rest of the filesystem.
- **The node wallet** is at `/var/lib/obelyth/wallet.json`. **This is a
  testnet wallet only.** No real value, but back it up if you care about
  the address.
- **No founder key is loaded** — the node uses its auto-generated wallet
  as the founder during silent testnet. When you mint a proper mainnet
  founder key, mount it at `/var/lib/obelyth/founder.wif` and add
  `--founder-key /var/lib/obelyth/founder.wif` to the systemd ExecStart.

## What's next after the node is live

1. **Update wallet/explorer/faucet to point at the public node.** Default
   URL goes from `http://127.0.0.1:8334` to `https://testnet.obelyth.io`.
   Three small file edits.
2. **Phase 5.5b** (structured logging) — drops in cleanly without touching
   deployment. Just re-run `install.sh` after the commit lands to pull
   the update.
3. **Watch the node** for 24-72 hours before any wider sharing. The
   silent phase exists specifically so you find weird bugs (memory
   leaks, slow drift, disk usage) before anyone else does.
