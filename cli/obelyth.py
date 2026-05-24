"""
Obelyth CLI
================
Interact with a running Obelyth node via RPC.

Commands:
  status            Node status & chain summary
  balance <addr>    Get balance of an address
  send <to> <amt>   Send OBY from wallet
  mine              Mine one block
  vesting           Founder vesting status
  peers             Connected peers
  mempool           Pending transactions
  wallet            Show wallet addresses
  newaddress        Derive a new receiving address
  keygen            Generate a new key pair (for founder setup)
"""

import sys
import json
import argparse
import urllib.request
import urllib.error
import os
from pathlib import Path

from core.crypto  import generate_keypair
from wallet.wallet import Wallet


RPC_DEFAULT = 'http://127.0.0.1:8334'


def rpc_get(endpoint: str, rpc: str = RPC_DEFAULT) -> dict:
    try:
        with urllib.request.urlopen(rpc + endpoint, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        print(f"[error] Cannot reach node at {rpc}: {e.reason}")
        sys.exit(1)


def rpc_post(endpoint: str, data: dict, rpc: str = RPC_DEFAULT) -> dict:
    body = json.dumps(data).encode()
    req  = urllib.request.Request(
        rpc + endpoint, data=body,
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        print(f"[error] Cannot reach node at {rpc}: {e.reason}")
        sys.exit(1)


def cmd_status(args):
    s = rpc_get('/status', args.rpc)
    print()
    print("  Obelyth Node Status")
    print("  " + "─" * 40)
    print(f"  Chain height : {s['height']}")
    print(f"  Block count  : {s['blocks']}")
    print(f"  Mempool      : {s['mempool']} txs")
    print(f"  Difficulty   : {s['difficulty']}")
    print(f"  Block size   : {s['block_size_kb']} KB")
    print(f"  Total burned : {s['total_burned']:.6f} OBY")
    print(f"  Validators   : {s['validators']}")
    print(f"  DAG tips     : {', '.join(t[:12]+'...' for t in s['tips'])}")
    n = s.get('network', {})
    print(f"  Peers        : {n.get('peers',0)} ({n.get('inbound',0)} in / {n.get('outbound',0)} out)")
    print(f"  Uptime       : {s.get('uptime_s',0)//60} min")
    print()


def cmd_balance(args):
    if not args.address:
        print("[error] Provide --address <OBY_ADDRESS>")
        sys.exit(1)
    r = rpc_get(f'/balance?addr={args.address}', args.rpc)
    if 'error' in r:
        print(f"[error] {r['error']}")
    else:
        print(f"  Balance: {r['balance']:.8f} OBY  ({r['address']})")


def cmd_send(args):
    if not args.to or not args.amount:
        print("[error] Provide --to <ADDRESS> --amount <OBY>")
        sys.exit(1)

    wallet_path = args.wallet or './obelyth_data/wallet.json'
    if not Path(wallet_path).exists():
        print(f"[error] Wallet not found: {wallet_path}")
        sys.exit(1)

    # Load wallet & query UTXOs via node
    wallet = Wallet.load(wallet_path)
    print(f"  From: {wallet.primary_address}")
    print(f"  To  : {args.to}")
    print(f"  Amt : {args.amount} OBY  fee={args.fee}")

    # We need the UTXO set from the node — in production, the node exposes
    # /utxos?addr=... ; for now we build a lightweight local UTXO set
    print("[info] Fetching UTXOs from node...")
    # (In a full implementation, GET /utxos?addr=... returns the UTXO set)
    # For this CLI demo, we call mine a tx via RPC directly
    r = rpc_post('/sendtx', {
        'tx': {
            # Simplified: in full impl, build & sign locally then post raw tx
            'note': 'Use the Python wallet.build_transaction() + post to /sendtx'
        }
    }, args.rpc)
    print(f"  Result: {r}")


def cmd_mine(args):
    r = rpc_post('/mineblock', {'consensus': args.consensus or 'pow'}, args.rpc)
    if r.get('mined'):
        print(f"  ✓ Mined block #{r['height']}  hash={r['hash'][:24]}...")
    else:
        print("  ✗ Mining failed (check node logs)")


def cmd_vesting(args):
    v = rpc_get('/vesting', args.rpc)
    total   = v['total_oby']
    vested  = v['vested_now']
    locked  = v['locked_now']
    pct     = round(vested / total * 100, 2) if total else 0
    bar_len = 30
    filled  = int(bar_len * pct / 100)
    bar     = '█' * filled + '░' * (bar_len - filled)
    print()
    print("  Founder Vesting Schedule")
    print("  " + "─" * 40)
    print(f"  Address  : {v['founder_address'][:32]}...")
    print(f"  Total    : {total:>14,.2f} OBY")
    print(f"  Vested   : {vested:>14,.2f} OBY  ({pct:.1f}%)")
    print(f"  Locked   : {locked:>14,.2f} OBY")
    print(f"  Progress : [{bar}]")
    print(f"  Cliff    : {v['cliff_months']} months")
    print(f"  Duration : {v['total_months']} months")
    print()


def cmd_peers(args):
    p = rpc_get('/peers', args.rpc)
    print(f"  Peers: {p['peers']} total  ({p['inbound']} inbound, {p['outbound']} outbound)")
    print(f"  Listening: {p['listening']}")


def cmd_mempool(args):
    m = rpc_get('/mempool', args.rpc)
    print(f"  Mempool: {m['count']} transactions")
    for tx in m['txs'][:10]:
        print(f"    {tx['hash'][:16]}...  fee={tx['fee']:.4f}  type={tx['tx_type']}")
    if m['count'] > 10:
        print(f"    ... and {m['count']-10} more")


def cmd_wallet(args):
    wallet_path = args.wallet or './obelyth_data/wallet.json'
    if not Path(wallet_path).exists():
        print(f"[error] Wallet not found at {wallet_path}. Start the node first.")
        sys.exit(1)
    w = Wallet.load(wallet_path)
    print(f"  Wallet: {w.label}")
    print(f"  Addresses ({len(w.all_addresses)}):")
    for addr in w.all_addresses:
        print(f"    {addr}")


def cmd_newaddress(args):
    wallet_path = args.wallet or './obelyth_data/wallet.json'
    if not Path(wallet_path).exists():
        print(f"[error] Wallet not found")
        sys.exit(1)
    w = Wallet.load(wallet_path)
    addr = w.new_address(label=args.label or '')
    w.save(wallet_path)
    print(f"  New address: {addr}")


def cmd_keygen(args):
    """Generate a key pair — useful for setting up the founder key."""
    priv, pub, addr = generate_keypair()
    out_path = args.output or './founder_key.wif'
    Path(out_path).write_text(priv.to_wif())
    print()
    print("  ┌── Founder Key Generated ──────────────────┐")
    print(f"  │  Address : {addr}")
    print(f"  │  WIF     : {priv.to_wif()[:24]}...  (saved)")
    print(f"  │  Saved to: {out_path}")
    print("  │")
    print("  │  ⚠  BACK THIS UP.  NEVER SHARE THE WIF KEY.")
    print("  │     Use --founder-key when starting node.")
    print("  └───────────────────────────────────────────┘")
    print()
    print(f"  Start node with:")
    print(f"    python -m node.fullnode --founder-key {out_path} --mine")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Obelyth CLI',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--rpc',    default=RPC_DEFAULT, help='Node RPC URL')
    parser.add_argument('--wallet', default=None,        help='Wallet file path')
    sub = parser.add_subparsers(dest='command')

    sub.add_parser('status')
    sub.add_parser('vesting')
    sub.add_parser('peers')
    sub.add_parser('mempool')
    sub.add_parser('wallet')

    bal = sub.add_parser('balance')
    bal.add_argument('--address', '-a', required=True)

    snd = sub.add_parser('send')
    snd.add_argument('--to',     '-t', required=True)
    snd.add_argument('--amount', '-v', type=float, required=True)
    snd.add_argument('--fee',          type=float, default=0.001)
    snd.add_argument('--zk',           action='store_true')

    mn = sub.add_parser('mine')
    mn.add_argument('--consensus', default='pow', choices=['pow','pos','dag'])

    na = sub.add_parser('newaddress')
    na.add_argument('--label', default='')

    kg = sub.add_parser('keygen')
    kg.add_argument('--output', '-o', default='./founder_key.wif')

    args = parser.parse_args()

    commands = {
        'status'    : cmd_status,
        'balance'   : cmd_balance,
        'send'      : cmd_send,
        'mine'      : cmd_mine,
        'vesting'   : cmd_vesting,
        'peers'     : cmd_peers,
        'mempool'   : cmd_mempool,
        'wallet'    : cmd_wallet,
        'newaddress': cmd_newaddress,
        'keygen'    : cmd_keygen,
    }

    fn = commands.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
