"""
Smoke runner for the explorer-supporting RPC endpoints:
  - GET /blocks?limit=N&before=H — paginated block list
  - GET /tx?hash=H              — tx lookup (mempool + blocks)
  - GET /address?addr=A         — balance + tx history

These are read-only routes added to support the block explorer frontend.
Tests confirm correctness of the responses, pagination, and error handling.
"""

import sys
import os
import json
import time
import tempfile
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from node.fullnode import FullNode, RPCHandler
from core.crypto import generate_keypair
from core.structures import Transaction, TxInput, TxOutput, TxType


PASSED = 0
FAILED = []


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED.append((name, detail))
        print(f"  FAIL  {name}: {detail}")


def section(t):
    print(f"\n--- {t} ---")


def find_free_port():
    import socket
    s = socket.socket(); s.bind(('', 0)); p = s.getsockname()[1]; s.close()
    return p


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ── Boot a node ─────────────────────────────────────────────────────────────
P2P  = find_free_port()
RPC  = find_free_port()
TMP  = tempfile.mkdtemp(prefix='oby-explorer-')
BASE = f'http://127.0.0.1:{RPC}'

print(f"Booting FullNode on RPC port {RPC}")
node = FullNode(p2p_port=P2P, rpc_port=RPC, data_dir=TMP, mine=False)
RPCHandler.node = node
srv = HTTPServer(('127.0.0.1', RPC), RPCHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)

# Mine a few blocks
founder = node.wallet.primary_address
node.chain.mine_block(founder)
node.chain.mine_block(founder)
node.chain.mine_block(founder)

# Send a real tx so we have something with inputs/outputs to inspect
priv = node.wallet._keypairs[0].private
pub  = node.wallet._keypairs[0].public
_, _, RECIPIENT = generate_keypair()
u = node.chain.utxos.unspent_for(founder)[0]
tx = Transaction(
    tx_type=TxType.REGULAR,
    inputs=[TxInput(utxo_tx_hash=u.tx_hash, utxo_index=u.index,
                    signature='', public_key='')],
    outputs=[TxOutput(address=RECIPIENT, amount=100.0),
             TxOutput(address=founder, amount=round(u.amount - 100.0 - 0.001, 8))],
    fee=0.001,
)
sig = priv.sign(tx._signing_payload())
tx.inputs[0].signature = sig.hex()
tx.inputs[0].public_key = pub.to_bytes().hex()
tx._hash = tx.compute_hash()
node.chain.add_to_mempool(tx)

# Mine the first tx in BEFORE queueing the mempool tx, so the second tx
# stays pending and we can verify mempool lookup.
node.chain.mine_block(founder)
TX_HASH = tx.hash

# Now add a second tx that stays in mempool (not mined) to test mempool lookups
u2 = next((x for x in node.chain.utxos.unspent_for(founder)
           if x.tx_hash != u.tx_hash), None)
mempool_tx = None
if u2:
    mp_tx = Transaction(
        tx_type=TxType.REGULAR,
        inputs=[TxInput(utxo_tx_hash=u2.tx_hash, utxo_index=u2.index,
                        signature='', public_key='')],
        outputs=[TxOutput(address=RECIPIENT, amount=5.0),
                 TxOutput(address=founder, amount=round(u2.amount - 5.0 - 0.001, 8))],
        fee=0.001,
    )
    sig2 = priv.sign(mp_tx._signing_payload())
    mp_tx.inputs[0].signature = sig2.hex()
    mp_tx.inputs[0].public_key = pub.to_bytes().hex()
    mp_tx._hash = mp_tx.compute_hash()
    node.chain.add_to_mempool(mp_tx)
    mempool_tx = mp_tx


# ── /blocks ─────────────────────────────────────────────────────────────────
section("/blocks — paginated block list")

code, data = get(f'{BASE}/blocks')
check("/blocks default returns 200", code == 200)
check("response has blocks array", isinstance(data.get('blocks'), list))
check("response has total_blocks", isinstance(data.get('total_blocks'), int))
check("response has chain_height", isinstance(data.get('chain_height'), int))
check("blocks sorted newest-first",
      all(data['blocks'][i]['height'] >= data['blocks'][i+1]['height']
          for i in range(len(data['blocks']) - 1)))
check("each block has hash, height, miner, tx_count, consensus",
      all(set(b.keys()) >= {'hash','height','miner','tx_count','consensus','timestamp','parent_hashes','difficulty'}
          for b in data['blocks']))

# Limit
code, data = get(f'{BASE}/blocks?limit=2')
check("limit=2 returns 2 blocks", code == 200 and len(data['blocks']) == 2)
check("limit=2 sets next_before when more exist",
      data['next_before'] is not None or len(node.chain.dag) <= 2)

# Pagination: page back
if data.get('next_before'):
    code2, data2 = get(f'{BASE}/blocks?limit=2&before={data["next_before"]}')
    check("/blocks?before=H returns 200", code2 == 200)
    check("pagination returns DIFFERENT blocks",
          set(b['hash'] for b in data2['blocks']) !=
          set(b['hash'] for b in data['blocks']))

# limit cap
code, data = get(f'{BASE}/blocks?limit=999')
check("/blocks?limit=999 caps at 100", code == 200 and len(data['blocks']) <= 100)


# ── /tx ─────────────────────────────────────────────────────────────────────
section("/tx — transaction lookup")

# Tx that's in a block
code, data = get(f'{BASE}/tx?hash={TX_HASH}')
check("/tx for mined tx returns 200", code == 200)
check("found is true", data.get('found') is True)
check("location = 'block'", data.get('location') == 'block')
check("includes block_hash and block_height",
      'block_hash' in data and 'block_height' in data)
check("includes the full tx body",
      data.get('tx', {}).get('hash') == TX_HASH)

# Tx still in mempool
if mempool_tx:
    code, data = get(f'{BASE}/tx?hash={mempool_tx.hash}')
    check("/tx for mempool tx found",
          data.get('found') and data.get('location') == 'mempool')

# Missing tx
code, data = get(f'{BASE}/tx?hash={"0" * 64}')
check("/tx for missing hash returns found=False",
      data.get('found') is False)

# Missing hash param
code, data = get(f'{BASE}/tx')
check("/tx with no hash returns error", 'error' in data)


# ── /address ────────────────────────────────────────────────────────────────
section("/address — balance + history")

# Recipient address — received 100
code, data = get(f'{BASE}/address?addr={RECIPIENT}')
check("/address returns 200", code == 200)
check("includes address, balance, utxo_count, tx_count, history",
      set(data.keys()) >= {'address', 'balance', 'utxo_count', 'tx_count', 'history'})
check("recipient balance = 100", data['balance'] == 100.0)
check("recipient utxo_count = 1", data['utxo_count'] == 1)
check("recipient has 1 history entry", data['tx_count'] == 1)
if data['history']:
    h0 = data['history'][0]
    check("history entry net = +100", h0['net'] == 100.0)
    check("history entry received = 100", h0['received'] == 100.0)
    check("history entry sent = 0", h0['sent'] == 0.0)
    check("history entry includes block_hash + block_height",
          'block_hash' in h0 and 'block_height' in h0)
    check("history entry includes tx_hash", h0.get('tx_hash') == TX_HASH)

# Founder address — coinbase rewards + 1 send
code, data = get(f'{BASE}/address?addr={founder}')
check("founder has multiple history entries", data['tx_count'] >= 2)
sends = sum(1 for h in data['history'] if h['sent'] > 0)
check("founder has at least 1 send", sends >= 1)

# Missing addr
code, data = get(f'{BASE}/address')
check("/address with no addr returns error", 'error' in data)

# Unknown address
_, _, UNKNOWN = generate_keypair()
code, data = get(f'{BASE}/address?addr={UNKNOWN}')
check("unknown address returns 0 balance, empty history",
      data['balance'] == 0 and data['tx_count'] == 0)


# ── Cleanup ─────────────────────────────────────────────────────────────────
srv.shutdown()
import shutil
shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'='*50}")
print(f"  PASSED: {PASSED}")
print(f"  FAILED: {len(FAILED)}")
if FAILED:
    print("\nFailures:")
    for n, d in FAILED:
        print(f"  - {n}: {d}")
    sys.exit(1)
print("  All checks green. Explorer endpoints ready.")
sys.exit(0)
