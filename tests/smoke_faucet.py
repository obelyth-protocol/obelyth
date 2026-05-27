"""
Smoke runner for Phase 4.1 — testnet faucet.

Tests the FaucetService and its HTTP routes:
  - /faucet/status returns expected shape when enabled
  - /faucet/status returns 503 when disabled
  - /faucet/claim with valid api_key + address -> 200 with tx_hash
  - Second claim from same account -> 409 (already_claimed)
  - Same IP, different account, within cooldown -> 429 (ip_cooldown)
  - Invalid address -> 400
  - Missing api_key (with require_api_key=True) -> 401
  - Unknown api_key -> 401
  - Anonymous mode (require_api_key=False) accepts no api_key
  - Daily budget exhaustion -> 503 (budget_exhausted)
  - Reserve below floor -> 503 (reserve_dry)
  - Claim record persists to SQLite
  - Status counts increment after each successful claim
  - Submitted tx lands in mempool
"""

import sys
import os
import json
import time
import tempfile
import threading
import sqlite3
import urllib.request
import urllib.error
from http.server import HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from node.fullnode import FullNode, RPCHandler


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


def post(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={'Content-Type': 'application/json'},
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


# ── Generate a syntactically-valid OBY address (just for the dedupe tests) ──
def fake_address(seed: str) -> str:
    """20-80 char alphanumeric. The faucet only checks format, not whether
    the address is valid on-chain — for testnet we just need something that
    passes the cheap validator."""
    import hashlib as h
    return 'OBY' + h.sha256(seed.encode()).hexdigest()[:32]


# ── Boot a node with faucet AND accounts enabled ────────────────────────────

P2P_PORT = find_free_port()
RPC_PORT = find_free_port()
TMPDIR   = tempfile.mkdtemp(prefix='obelyth-faucet-test-')
BASE_URL = f'http://127.0.0.1:{RPC_PORT}'

print(f"Booting FullNode on RPC port {RPC_PORT} (faucet + accounts enabled)")
node = FullNode(
    p2p_port=P2P_PORT,
    rpc_port=RPC_PORT,
    data_dir=TMPDIR,
    mine=False,
    accounts_enabled=True,
    faucet_enabled=True,
)

# Verify wiring
check("FullNode has faucet attribute", node.faucet is not None)
check("FullNode has accounts_registry", node.accounts_registry is not None)
check("Faucet's accounts_registry is wired",
      node.faucet.accounts is not None)
check("Faucet reserve is funded (founder grant)",
      node.faucet.reserve_balance() > 0)

# Start the HTTP server
RPCHandler.node = node
rpc_server = HTTPServer(('127.0.0.1', RPC_PORT), RPCHandler)
threading.Thread(target=rpc_server.serve_forever, daemon=True).start()
time.sleep(0.3)


# ── Register some test accounts ─────────────────────────────────────────────

alice_account, ALICE_KEY = node.accounts_registry.register(
    email='alice@example.com', password='pw',
)
bob_account, BOB_KEY = node.accounts_registry.register(
    email='bob@example.com', password='pw',
)
carol_account, CAROL_KEY = node.accounts_registry.register(
    email='carol@example.com', password='pw',
)


# ── /faucet/status ───────────────────────────────────────────────────────────
section("/faucet/status")

code, body = get(f'{BASE_URL}/faucet/status')
check("status returns 200", code == 200)
expected_keys = {
    'reserve_oby', 'payout_per_claim_oby', 'total_paid_oby',
    'total_claims', 'paid_today_oby', 'claims_today',
    'daily_budget_oby', 'budget_remaining_oby', 'wallet_address',
}
check("status has all expected keys", expected_keys.issubset(body.keys()),
      f"missing: {expected_keys - set(body.keys())}")
check("reserve_oby > 0", body.get('reserve_oby', 0) > 0)
check("total_claims == 0 initially", body.get('total_claims') == 0)
check("paid_today_oby == 0 initially", body.get('paid_today_oby') == 0)


# ── /faucet/claim happy path ────────────────────────────────────────────────
section("/faucet/claim — happy path")

addr_alice = fake_address('alice-1')
code, body = post(f'{BASE_URL}/faucet/claim', {
    'api_key': ALICE_KEY,
    'address': addr_alice,
})
check("first claim returns 200", code == 200, f"got {code} {body}")
check("response includes tx_hash",
      body.get('tx_hash') and len(body['tx_hash']) > 0)
check("response includes amount_oby == default payout",
      body.get('amount_oby') == 1500.0)
check("response includes claim_id", body.get('claim_id'))
ALICE_TX_HASH = body.get('tx_hash', '')


# ── Tx landed in mempool ────────────────────────────────────────────────────
section("Submitted tx lands in mempool")

mempool_size = len(node.chain.mempool)
check("mempool grew by at least 1", mempool_size >= 1)
mempool_hashes = [tx.hash for tx in node.chain.mempool]
check("tx_hash from claim is in mempool",
      ALICE_TX_HASH in mempool_hashes,
      f"mempool: {mempool_hashes[:3]}")


# ── Status counts increment ─────────────────────────────────────────────────
section("Status reflects the claim")

code, body = get(f'{BASE_URL}/faucet/status')
check("total_claims == 1 after one claim", body.get('total_claims') == 1)
check("paid_today_oby == 1500", body.get('paid_today_oby') == 1500.0)
check("budget_remaining decreased by payout",
      abs(body.get('budget_remaining_oby', 0) - (750_000 - 1500)) < 0.01)


# ── Per-account dedupe (409) ────────────────────────────────────────────────
section("Per-account dedupe: same account, different address — blocked")

addr_alice2 = fake_address('alice-2')
code, body = post(f'{BASE_URL}/faucet/claim', {
    'api_key': ALICE_KEY,
    'address': addr_alice2,
})
check("second claim same account -> 409", code == 409)
check("error code = already_claimed",
      body.get('code') == 'already_claimed')


# ── Per-IP cooldown (429) — different account, same IP ──────────────────────
section("Per-IP cooldown: different account, same IP — blocked")

addr_bob = fake_address('bob-1')
code, body = post(f'{BASE_URL}/faucet/claim', {
    'api_key': BOB_KEY,
    'address': addr_bob,
})
check("different account, same IP -> 429", code == 429)
check("error code = ip_cooldown",
      body.get('code') == 'ip_cooldown')

# Bob can claim after we wipe his IP cooldown manually (test-only)
# This simulates Bob using a different network
with sqlite3.connect(node.faucet.db_path) as conn:
    conn.execute("DELETE FROM faucet_claims")  # wipe all to reset IP cooldown
# This also wipes Alice's claim — but that's fine for the rest of these tests


# ── Invalid address (400) ───────────────────────────────────────────────────
section("Invalid address rejected")

code, body = post(f'{BASE_URL}/faucet/claim', {
    'api_key': ALICE_KEY,
    'address': 'x',  # too short
})
check("3-char address -> 400", code == 400)
check("error code = invalid_address",
      body.get('code') == 'invalid_address')

code, body = post(f'{BASE_URL}/faucet/claim', {
    'api_key': ALICE_KEY,
    'address': 'has@invalid#chars!!!!!!!!!!!!!!!!!!!',
})
check("special-char address -> 400", code == 400)


# ── Missing api_key (401) ───────────────────────────────────────────────────
section("Missing api_key rejected when require_api_key=True")

code, body = post(f'{BASE_URL}/faucet/claim', {
    'address': fake_address('no-key'),
})
check("no api_key -> 401", code == 401)
check("error code = missing_api_key",
      body.get('code') == 'missing_api_key')


# ── Unknown api_key (401) ───────────────────────────────────────────────────
section("Unknown api_key rejected")

code, body = post(f'{BASE_URL}/faucet/claim', {
    'api_key': 'oby_clearly_not_real_xxxxxxxxxx',
    'address': fake_address('unknown-key'),
})
check("unknown api_key -> 401", code == 401)
check("error code = unknown_account",
      body.get('code') == 'unknown_account')


# ── Successful Carol claim after the DB wipe ────────────────────────────────
section("Fresh state: Carol claims successfully")

code, body = post(f'{BASE_URL}/faucet/claim', {
    'api_key': CAROL_KEY,
    'address': fake_address('carol-fresh'),
})
check("carol's claim succeeds after reset", code == 200,
      f"got {code} {body}")


# ── Claim record persisted to SQLite ────────────────────────────────────────
section("Claim record persisted")

with sqlite3.connect(node.faucet.db_path) as conn:
    rows = conn.execute(
        "SELECT account_id, address, amount_oby, status FROM faucet_claims"
    ).fetchall()
check("at least 1 row in faucet_claims table", len(rows) >= 1)
if rows:
    check("persisted account_id matches Carol's",
          rows[0][0] == carol_account.account_id)
    check("persisted amount == payout", rows[0][2] == 1500.0)
    check("persisted status == 'pending'", rows[0][3] == 'pending')


# ── Reserve floor enforcement ───────────────────────────────────────────────
section("Reserve floor enforcement — temporarily drop floor below balance")

# Bump floor higher than current reserve
node.faucet.min_reserve_oby = 999_999_999.0
# Wipe claims so the dedupe filters don't fire first
with sqlite3.connect(node.faucet.db_path) as conn:
    conn.execute("DELETE FROM faucet_claims")

code, body = post(f'{BASE_URL}/faucet/claim', {
    'api_key': ALICE_KEY,
    'address': fake_address('reserve-test'),
})
check("reserve below floor -> 503", code == 503)
check("error code = reserve_dry",
      body.get('code') == 'reserve_dry')

# Restore for next test
node.faucet.min_reserve_oby = 10_000.0


# ── Daily budget exhaustion ─────────────────────────────────────────────────
section("Daily budget exhaustion")

# Wipe and shrink budget to 1 OBY (less than payout 1500) so first claim blocks
with sqlite3.connect(node.faucet.db_path) as conn:
    conn.execute("DELETE FROM faucet_claims")
original_budget = node.faucet.daily_budget_oby
node.faucet.daily_budget_oby = 100.0   # below 1500 payout

code, body = post(f'{BASE_URL}/faucet/claim', {
    'api_key': ALICE_KEY,
    'address': fake_address('budget-test'),
})
check("payout > budget -> 503", code == 503)
check("error code = budget_exhausted",
      body.get('code') == 'budget_exhausted')

# Restore
node.faucet.daily_budget_oby = original_budget


# ── Anonymous mode (require_api_key=False) ──────────────────────────────────
section("Anonymous mode (require_api_key=False)")

# Boot a second node WITHOUT accounts_enabled to test anonymous flow
TMPDIR2   = tempfile.mkdtemp(prefix='obelyth-faucet-anon-')
P2P_PORT2 = find_free_port()
RPC_PORT2 = find_free_port()
BASE_URL2 = f'http://127.0.0.1:{RPC_PORT2}'

print(f"  booting second node on port {RPC_PORT2} (anonymous faucet)")
node2 = FullNode(
    p2p_port=P2P_PORT2, rpc_port=RPC_PORT2, data_dir=TMPDIR2,
    mine=False, faucet_enabled=True,  # accounts NOT enabled
)
check("anon node: accounts disabled", node2.accounts_registry is None)
check("anon node: faucet in anonymous mode",
      node2.faucet.require_api_key is False)

# Start the second RPC server on a SEPARATE handler subclass so we don't
# clobber the first node's binding
class _Handler2(RPCHandler):
    pass
_Handler2.node = node2
rpc2 = HTTPServer(('127.0.0.1', RPC_PORT2), _Handler2)
threading.Thread(target=rpc2.serve_forever, daemon=True).start()
time.sleep(0.3)

# Claim with NO api_key should succeed in anon mode
code, body = post(f'{BASE_URL2}/faucet/claim', {
    'address': fake_address('anon-1'),
})
check("anonymous claim succeeds (no api_key)", code == 200,
      f"got {code} {body}")

# Second claim same address still dedupes via per-address account_id derivation
code, body = post(f'{BASE_URL2}/faucet/claim', {
    'address': fake_address('anon-1'),  # SAME address
})
check("anonymous: same address re-claim -> 409",
      code == 409)


# ── Faucet disabled (503) ───────────────────────────────────────────────────
section("Faucet disabled returns 503")

TMPDIR3   = tempfile.mkdtemp(prefix='obelyth-faucet-off-')
P2P_PORT3 = find_free_port()
RPC_PORT3 = find_free_port()
BASE_URL3 = f'http://127.0.0.1:{RPC_PORT3}'

node3 = FullNode(
    p2p_port=P2P_PORT3, rpc_port=RPC_PORT3, data_dir=TMPDIR3,
    mine=False, faucet_enabled=False,
)
check("faucet disabled: node.faucet is None", node3.faucet is None)

class _Handler3(RPCHandler):
    pass
_Handler3.node = node3
rpc3 = HTTPServer(('127.0.0.1', RPC_PORT3), _Handler3)
threading.Thread(target=rpc3.serve_forever, daemon=True).start()
time.sleep(0.3)

code, body = get(f'{BASE_URL3}/faucet/status')
check("disabled faucet /status -> 503", code == 503)

code, body = post(f'{BASE_URL3}/faucet/claim', {
    'api_key': 'anything',
    'address': fake_address('disabled'),
})
check("disabled faucet /claim -> 503", code == 503)


# ── Shutdown ────────────────────────────────────────────────────────────────
rpc_server.shutdown()
rpc2.shutdown()
rpc3.shutdown()
import shutil
for d in (TMPDIR, TMPDIR2, TMPDIR3):
    try:
        shutil.rmtree(d)
    except Exception:
        pass


# ── Report ─────────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  PASSED: {PASSED}")
print(f"  FAILED: {len(FAILED)}")
if FAILED:
    print(f"\nFailures:")
    for name, detail in FAILED:
        print(f"  - {name}: {detail}")
    sys.exit(1)
print(f"  All checks green. Phase 4.1 faucet ready.")
sys.exit(0)
