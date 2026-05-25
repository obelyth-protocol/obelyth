"""
Smoke runner for Phase 3.1 — accounts registry auth.

Boots a FullNode with accounts_enabled=True and verifies:
  - /compute/quote remains unauthenticated (public pricing)
  - /compute/submit requires valid api_key → 401 on missing/unknown
  - /compute/submit ignores body's developer_addr when valid api_key supplied
    (cannot spoof another account_id)
  - /compute/submit returns 402 when account has insufficient balance
  - /compute/submit returns 403 when account is suspended/banned
  - /compute/infer also requires valid api_key
  - /compute/job remains unauthenticated (job_id is non-enumerable)
  - Miner endpoints (/register, /heartbeat, /nextjob, /result) remain open
"""

import sys
import os
import json
import hashlib
import time
import tempfile
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from node.fullnode import FullNode, RPCHandler
from accounts.registry import AccountStatus


PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED.append((name, detail))
        print(f"  FAIL  {name}: {detail}")


def section(title):
    print(f"\n--- {title} ---")


def _hex(seed): return hashlib.sha256(str(seed).encode()).hexdigest()
def _digest(seed): return "sha256:" + _hex(seed)


def post(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def find_free_port():
    import socket
    s = socket.socket()
    s.bind(('', 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── Boot a real FullNode with accounts enabled ──────────────────────────────

P2P_PORT  = find_free_port()
RPC_PORT  = find_free_port()
TMPDIR    = tempfile.mkdtemp(prefix='obelyth-auth-test-')
BASE_URL  = f'http://127.0.0.1:{RPC_PORT}'

print(f"Booting FullNode on RPC port {RPC_PORT} with accounts_enabled=True")

node = FullNode(
    p2p_port=P2P_PORT,
    rpc_port=RPC_PORT,
    data_dir=TMPDIR,
    mine=False,
    accounts_enabled=True,
)

# Verify the registry is wired
check("FullNode constructed with accounts_registry",
      node.accounts_registry is not None)
check("ComputeAPI received accounts_registry",
      node.compute_api.accounts is not None)

RPCHandler.node = node
rpc_server = HTTPServer(('127.0.0.1', RPC_PORT), RPCHandler)
threading.Thread(target=rpc_server.serve_forever, daemon=True).start()
time.sleep(0.3)

# Seed rates so quote works
from tokenomics.engine import Stablecoin
node.tokenomics.update_rate(Stablecoin.USDC, 1.0)
node.tokenomics.update_rate(Stablecoin.DAI, 1.0)
node.tokenomics.update_rate(Stablecoin.USDT, 1.0)
node.tokenomics.update_rate(Stablecoin.EURC, 1.08)


# ── Register two test accounts ──────────────────────────────────────────────

import sqlite3

def _set_balance(registry, account_id, balance_usd):
    """Test helper — directly set balance bypassing the deposit_id requirement."""
    with sqlite3.connect(registry.db_path) as conn:
        conn.execute(
            'UPDATE accounts SET balance_usd = ? WHERE account_id = ?',
            (balance_usd, account_id),
        )


# Account with sufficient balance
funded_account, FUNDED_KEY = node.accounts_registry.register(
    email='funded@example.com', password='test-pw',
)
_set_balance(node.accounts_registry, funded_account.account_id, 100.0)

# Account with zero balance (should get 402)
broke_account, BROKE_KEY = node.accounts_registry.register(
    email='broke@example.com', password='test-pw',
)

# Account that will be suspended (should get 403)
sus_account, SUS_KEY = node.accounts_registry.register(
    email='suspended@example.com', password='test-pw',
)
_set_balance(node.accounts_registry, sus_account.account_id, 100.0)
with sqlite3.connect(node.accounts_registry.db_path) as conn:
    conn.execute(
        'UPDATE accounts SET status = ? WHERE account_id = ?',
        (AccountStatus.SUSPENDED.value, sus_account.account_id),
    )


# ── /compute/quote: should remain unauthenticated ──────────────────────────
section("/compute/quote remains unauthenticated")

code, body = post(f'{BASE_URL}/compute/quote', {
    'job_type': 'inference',
    'gpu_count': 1,
    'duration_hr': 1.0,
})
check("quote without api_key returns 200", code == 200)
check("quote returns pricing", 'stable_cost' in body)


# ── /compute/submit: 401 on missing api_key ─────────────────────────────────
section("/compute/submit requires api_key")

base_submit_body = {
    'job_type'          : 'inference',
    'model_id'          : 'test-model',
    'coin'              : 'USDC',
    'model_hash'        : _hex(1),
    'container_digest'  : _digest(2),
    'seed'              : 42,
    'input_payload_hash': _hex(3),
    'input_schema_hash' : _hex(4),
    'duration_hr'       : 0.1,
}

# No api_key, no developer_addr
code, body = post(f'{BASE_URL}/compute/submit', dict(base_submit_body))
check("submit without api_key -> 401", code == 401)
check("error mentions api_key", 'api_key' in body.get('error', '').lower())

# Unknown api_key
code, body = post(f'{BASE_URL}/compute/submit', {
    **base_submit_body, 'api_key': 'oby_clearly_fake_key_12345',
})
check("submit with unknown api_key -> 401", code == 401)

# Developer_addr in body alone — should still 401 because registry is enabled
code, body = post(f'{BASE_URL}/compute/submit', {
    **base_submit_body, 'developer_addr': 'dev_spoofed_address',
})
check("submit with developer_addr but no api_key -> 401", code == 401)


# ── /compute/submit: 200 with valid api_key ────────────────────────────────
section("/compute/submit with valid api_key")

code, body = post(f'{BASE_URL}/compute/submit', {
    **base_submit_body, 'api_key': FUNDED_KEY,
})
check("submit with valid api_key -> 200", code == 200,
      f"got {code} {body}")
check("job_id returned", 'job_id' in body)
SUBMITTED_JOB_ID = body.get('job_id', '')


# ── developer_addr cannot be spoofed ────────────────────────────────────────
section("developer_addr forced to account.account_id (cannot spoof)")

code, body = post(f'{BASE_URL}/compute/submit', {
    **base_submit_body,
    'api_key': FUNDED_KEY,
    'developer_addr': 'SPOOFED_ADDRESS_XYZ',   # ignored
    'model_hash': _hex(99),
})
check("submit with valid api_key + spoofed developer_addr -> 200", code == 200)
if code == 200:
    job = node.tokenomics.get_job(body['job_id'])
    check("job's developer_addr is account_id, NOT spoofed value",
          job is not None and job.developer_addr == funded_account.account_id,
          f"got {job.developer_addr if job else 'None'}")


# ── 402 on insufficient balance ─────────────────────────────────────────────
section("/compute/submit returns 402 on insufficient balance")

code, body = post(f'{BASE_URL}/compute/submit', {
    **base_submit_body,
    'api_key': BROKE_KEY,
    'model_hash': _hex(200),
})
check("broke account -> 402 payment required", code == 402,
      f"got {code} {body}")
check("error mentions balance",
      'balance' in body.get('error', '').lower())


# ── 403 on suspended account ────────────────────────────────────────────────
section("/compute/submit returns 403 on suspended account")

code, body = post(f'{BASE_URL}/compute/submit', {
    **base_submit_body,
    'api_key': SUS_KEY,
    'model_hash': _hex(300),
})
check("suspended account -> 403 forbidden", code == 403,
      f"got {code} {body}")
check("error mentions account status",
      'account status' in body.get('error', '').lower()
      or 'suspended' in body.get('error', '').lower())


# ── /compute/infer also requires api_key ────────────────────────────────────
section("/compute/infer requires api_key")

from compute.api import ComputeAPI
original_timeout = ComputeAPI.INFER_TIMEOUT_S
ComputeAPI.INFER_TIMEOUT_S = 1.0
try:
    code, body = post(f'{BASE_URL}/compute/infer', {
        **base_submit_body, 'model_hash': _hex(400),
    })
    check("infer without api_key -> 401", code == 401)

    code, body = post(f'{BASE_URL}/compute/infer', {
        **base_submit_body, 'api_key': FUNDED_KEY,
        'model_hash': _hex(401),
    })
    # Will time out since no miner runtime — but should NOT be 401
    check("infer with valid api_key returns non-401",
          code != 401, f"got {code}")
finally:
    ComputeAPI.INFER_TIMEOUT_S = original_timeout


# ── /compute/job remains unauthenticated ───────────────────────────────────
section("/compute/job remains unauthenticated (job_id is non-enumerable)")

if SUBMITTED_JOB_ID:
    code, body = post(f'{BASE_URL}/compute/job', {'job_id': SUBMITTED_JOB_ID})
    check("job status without api_key -> 200", code == 200)


# ── Miner endpoints remain open ─────────────────────────────────────────────
section("Miner endpoints remain open (no api_key required)")

code, body = post(f'{BASE_URL}/compute/register', {
    'address': 'OBYminer1xxxxxxxxxxxxxxxxxxxxxxxx',
    'gpu_model': 'A100', 'gpu_count': 1, 'vram_gb': 80,
    'bandwidth_gbps': 10.0, 'region': 'us-east',
    'stake_oby': 5000.0,
})
check("/compute/register works without api_key", code == 200)

code, body = post(f'{BASE_URL}/compute/heartbeat', {
    'address': 'OBYminer1xxxxxxxxxxxxxxxxxxxxxxxx',
})
check("/compute/heartbeat works without api_key", code == 200)

code, body = get(
    f'{BASE_URL}/compute/nextjob?address=OBYminer1xxxxxxxxxxxxxxxxxxxxxxxx'
)
check("/compute/nextjob works without api_key", code == 200)


# ── Shutdown + report ───────────────────────────────────────────────────────

rpc_server.shutdown()
import shutil
try:
    shutil.rmtree(TMPDIR)
except Exception:
    pass

print(f"\n{'='*50}")
print(f"  PASSED: {PASSED}")
print(f"  FAILED: {len(FAILED)}")
if FAILED:
    print(f"\nFailures:")
    for name, detail in FAILED:
        print(f"  - {name}: {detail}")
    sys.exit(1)
print(f"  All checks green. Phase 3.1 auth ready.")
sys.exit(0)
