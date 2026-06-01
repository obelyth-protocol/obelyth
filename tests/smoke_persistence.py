"""
Smoke runner for chain persistence (Blockchain.save / load + node wiring).

Covers:
  - Empty/genesis chain round-trips
  - Mined blocks survive save/load (balances, height, DAO vault, burned)
  - DAG tips reconstruct correctly after load (children map rebuilt)
  - A real signed transaction's effect survives save/load
  - Validators survive save/load
  - Mempool (pending txs) survives save/load
  - Atomic write: a save over an existing file doesn't corrupt it
  - load() returns False for a missing snapshot (caller keeps genesis)
  - Full node restart simulation: boot -> mine -> save -> reboot -> state intact
  - Vesting genesis timestamp persists (vested/locked math stable across restart)
"""

import sys
import os
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.blockchain import Blockchain
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


def tmp_path(name):
    return os.path.join(tempfile.gettempdir(), name)


def signed_tx(bc, priv, pub, founder, recipient, amount, fee=0.001):
    """Build + sign a real transaction spending the founder's first UTXO."""
    u = bc.utxos.unspent_for(founder)[0]
    inp = TxInput(utxo_tx_hash=u.tx_hash, utxo_index=u.index,
                  signature='', public_key='')
    outs = [TxOutput(address=recipient, amount=amount)]
    change = round(u.amount - amount - fee, 8)
    if change > 1e-8:
        outs.append(TxOutput(address=founder, amount=change))
    tx = Transaction(tx_type=TxType.REGULAR, inputs=[inp], outputs=outs, fee=fee)
    sig = priv.sign(tx._signing_payload())
    inp.signature = sig.hex()
    inp.public_key = pub.to_bytes().hex()
    tx._hash = tx.compute_hash()
    return tx


# ── Genesis round-trip ──────────────────────────────────────────────────────
section("Genesis chain round-trips")

priv, pub, founder = generate_keypair()
bc = Blockchain(founder_address=founder)
p = tmp_path('oby-persist-genesis.json')
bc.save(p)
bc2 = Blockchain(founder_address=founder, genesis=False)
ok = bc2.load(p)
check("load returns True", ok)
check("genesis height preserved", bc2.dag.height() == bc.dag.height())
check("founder balance preserved",
      bc2.utxos.balance(founder) == bc.utxos.balance(founder))
check("founder balance is 630000",
      bc2.utxos.balance(founder) == 630000.0)
os.remove(p)


# ── Mined blocks round-trip ─────────────────────────────────────────────────
section("Mined blocks survive save/load")

bc = Blockchain(founder_address=founder)
bc.mine_block(founder)
bc.mine_block(founder)
bc.mine_block(founder)
bal = bc.utxos.balance(founder)
height = bc.dag.height()
vault = bc.dao_vault_oby
tips_before = sorted(t.hash for t in bc.dag.tips())

p = tmp_path('oby-persist-mined.json')
bc.save(p)
bc2 = Blockchain(founder_address=founder, genesis=False)
bc2.load(p)
check("height preserved", bc2.dag.height() == height, f"{bc2.dag.height()} vs {height}")
check("block count preserved", len(bc2.dag) == len(bc.dag))
check("founder balance preserved", bc2.utxos.balance(founder) == bal)
check("dao vault preserved", abs(bc2.dao_vault_oby - vault) < 1e-9)
check("tips reconstruct correctly",
      sorted(t.hash for t in bc2.dag.tips()) == tips_before)
os.remove(p)


# ── Transaction effect survives ─────────────────────────────────────────────
section("Signed transaction effect survives save/load")

bc = Blockchain(founder_address=founder)
bc.mine_block(founder)
_, _, recipient = generate_keypair()
tx = signed_tx(bc, priv, pub, founder, recipient, 100.0)
bc.add_to_mempool(tx)
bc.mine_block(founder)
recip_bal = bc.utxos.balance(recipient)
check("recipient received 100 pre-save", recip_bal == 100.0)

p = tmp_path('oby-persist-tx.json')
bc.save(p)
bc2 = Blockchain(founder_address=founder, genesis=False)
bc2.load(p)
check("recipient balance survives reload",
      bc2.utxos.balance(recipient) == recip_bal)
os.remove(p)


# ── Validators survive ──────────────────────────────────────────────────────
section("Validators survive save/load")

bc = Blockchain(founder_address=founder)
# register() enforces minimum stake; inject directly to avoid coupling to that
bc.validators._validators['OBYvalidator_test_addr'] = 50000.0
p = tmp_path('oby-persist-val.json')
bc.save(p)
bc2 = Blockchain(founder_address=founder, genesis=False)
bc2.load(p)
check("validator preserved",
      bc2.validators.all().get('OBYvalidator_test_addr') == 50000.0)
os.remove(p)


# ── Mempool survives ────────────────────────────────────────────────────────
section("Mempool (pending txs) survives save/load")

bc = Blockchain(founder_address=founder)
bc.mine_block(founder)
tx = signed_tx(bc, priv, pub, founder, recipient, 50.0)
bc.add_to_mempool(tx)
mempool_before = len(bc.mempool)
check("mempool has the tx", mempool_before == 1)

p = tmp_path('oby-persist-mempool.json')
bc.save(p)
bc2 = Blockchain(founder_address=founder, genesis=False)
bc2.load(p)
check("mempool size preserved", len(bc2.mempool) == mempool_before)
check("mempool tx hash preserved",
      bc2.mempool[0].hash == tx.hash)
os.remove(p)


# ── Atomic overwrite doesn't corrupt ────────────────────────────────────────
section("Saving over an existing snapshot is safe")

bc = Blockchain(founder_address=founder)
bc.mine_block(founder)
p = tmp_path('oby-persist-overwrite.json')
bc.save(p)
bc.mine_block(founder)
bc.save(p)   # overwrite
bc2 = Blockchain(founder_address=founder, genesis=False)
ok = bc2.load(p)
check("reload after overwrite works", ok)
check("overwrite has latest height", bc2.dag.height() == bc.dag.height())
os.remove(p)


# ── Missing snapshot ────────────────────────────────────────────────────────
section("load() returns False for missing file")

bc = Blockchain(founder_address=founder, genesis=False)
ok = bc.load(tmp_path('oby-does-not-exist-xyz.json'))
check("missing file returns False", ok is False)


# ── Vesting genesis timestamp persists ──────────────────────────────────────
section("Vesting genesis timestamp survives restart")

bc = Blockchain(founder_address=founder)
original_gts = bc.vesting.genesis_timestamp
p = tmp_path('oby-persist-vesting.json')
bc.save(p)
bc2 = Blockchain(founder_address=founder, genesis=False)
bc2.load(p)
check("vesting genesis ts preserved",
      bc2.vesting.genesis_timestamp == original_gts)
os.remove(p)


# ── Full node restart simulation ────────────────────────────────────────────
section("Node restart simulation: boot -> mine -> save -> reboot")

import shutil
restart_dir = os.path.join(tempfile.gettempdir(), 'oby-restart-sim')
shutil.rmtree(restart_dir, ignore_errors=True)
os.makedirs(restart_dir, exist_ok=True)
_cwd = os.getcwd()
os.chdir(restart_dir)
try:
    from node.fullnode import FullNode

    n1 = FullNode(p2p_port=24500, rpc_port=24501,
                  data_dir='./obelyth_data', mine=False)
    f1 = n1.wallet.primary_address
    n1.chain.mine_block(f1)
    n1.chain.mine_block(f1)
    bal1 = n1.chain.utxos.balance(f1)
    h1 = n1.chain.dag.height()
    n1.save_state()

    n2 = FullNode(p2p_port=24502, rpc_port=24503,
                  data_dir='./obelyth_data', mine=False)
    f2 = n2.wallet.primary_address
    check("founder stable across restart", f1 == f2)
    check("balance survives restart", n2.chain.utxos.balance(f2) == bal1,
          f"{n2.chain.utxos.balance(f2)} vs {bal1}")
    check("height survives restart", n2.chain.dag.height() == h1)
finally:
    os.chdir(_cwd)
    shutil.rmtree(restart_dir, ignore_errors=True)


# ── Report ──────────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  PASSED: {PASSED}")
print(f"  FAILED: {len(FAILED)}")
if FAILED:
    print("\nFailures:")
    for n, d in FAILED:
        print(f"  - {n}: {d}")
    sys.exit(1)
print("  All checks green. Chain persistence ready.")
sys.exit(0)
