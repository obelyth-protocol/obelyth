"""
Obelyth Full Node
======================
Wires together:
  - Blockchain engine
  - P2P networking
  - Wallet
  - HTTP JSON-RPC API (stdlib http.server — no external deps)
  - Mining loop (optional)

Usage:
  python -m node.fullnode --port 8333 --rpc-port 8334 [--mine] [--founder-key PATH]
"""

import json
import time
import logging
import threading
import argparse
import os
from http.server        import HTTPServer, BaseHTTPRequestHandler
from pathlib            import Path

from core.blockchain    import Blockchain
from core.structures    import Transaction, ConsensusType
from core.crypto        import generate_keypair, PrivateKey, VestingSchedule
from network.p2p        import NetworkNode
from wallet.wallet      import Wallet

logging.basicConfig(
    level   = logging.INFO,
    format  = '%(asctime)s [%(name)s] %(levelname)s %(message)s',
    datefmt = '%H:%M:%S',
)
log = logging.getLogger('obelyth.node')

DEFAULT_SEED_NODES = [
    # Add public seed node addresses here when launching mainnet
    # ('seed1.obelyth.io', 8333),
    # ('seed2.obelyth.io', 8333),
]


# ── RPC Handler ────────────────────────────────────────────────────────────────

class RPCHandler(BaseHTTPRequestHandler):
    """Minimal HTTP JSON-RPC server."""
    node : 'FullNode' = None     # set before server starts

    def log_message(self, fmt, *args):
        log.debug(f"RPC: {fmt % args}")

    def do_GET(self):
        routes = {
            '/status'   : self._status,
            '/peers'    : self._peers,
            '/mempool'  : self._mempool,
            '/vesting'  : self._vesting,
            '/balance'  : self._balance,
        }
        handler = routes.get(self.path.split('?')[0])
        if handler:
            data = handler()
            self._json(200, data)
        else:
            self._json(404, {'error': 'Not found'})

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length)
        try:
            req = json.loads(body)
        except Exception:
            self._json(400, {'error': 'Invalid JSON'})
            return

        routes = {
            '/sendtx'       : self._send_tx,
            '/mineblock'    : self._mine_block,
            '/addpeer'      : self._add_peer,
            '/getblock'     : self._get_block,
        }
        handler = routes.get(self.path)
        if handler:
            data = handler(req)
            self._json(200, data)
        else:
            self._json(404, {'error': 'Not found'})

    def _json(self, code: int, data: dict):
        body = json.dumps(data, indent=2).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    # ── Routes ────────────────────────────────────────────────────────────────

    def _status(self):
        s = self.node.chain.state_summary()
        s['network'] = self.node.network.status()
        s['uptime_s'] = int(time.time() - self.node.started_at)
        return s

    def _peers(self):
        return self.node.network.status()

    def _mempool(self):
        return {
            'count': len(self.node.chain.mempool),
            'txs'  : [tx.to_dict() for tx in self.node.chain.mempool[:50]],
        }

    def _vesting(self):
        return self.node.chain.vesting.to_dict() | {
            'vested_now': self.node.chain.vesting.vested_amount(int(time.time())),
            'locked_now': self.node.chain.vesting.locked_amount(int(time.time())),
        }

    def _balance(self):
        addr = self.path.split('?addr=')[-1] if '?addr=' in self.path else ''
        if not addr:
            return {'error': 'Provide ?addr=<address>'}
        return {'address': addr, 'balance': self.node.chain.utxos.balance(addr)}

    def _send_tx(self, req):
        tx_dict = req.get('tx')
        if not tx_dict:
            return {'error': 'Missing tx'}
        try:
            tx = Transaction.from_dict(tx_dict)
        except Exception as e:
            return {'error': str(e)}
        ok = self.node.chain.add_to_mempool(tx)
        if ok:
            self.node.network.broadcast_tx(tx.to_dict())
        return {'accepted': ok, 'hash': tx.hash}

    def _mine_block(self, req):
        addr      = req.get('address', self.node.wallet.primary_address)
        consensus = ConsensusType(req.get('consensus', 'pow'))
        block = self.node.chain.mine_block(addr, consensus=consensus)
        if block:
            self.node.network.broadcast_block(block.to_dict())
            return {'mined': True, 'hash': block.hash, 'height': block.height}
        return {'mined': False}

    def _add_peer(self, req):
        host = req.get('host')
        port = req.get('port', 8333)
        if not host:
            return {'error': 'Missing host'}
        self.node.network.connect(host, port)
        return {'connecting': True}

    def _get_block(self, req):
        h = req.get('hash')
        if not h:
            return {'error': 'Missing hash'}
        block = self.node.chain.dag.get(h)
        if not block:
            return {'error': 'Block not found'}
        return block.to_dict()


# ── Full Node ──────────────────────────────────────────────────────────────────

class FullNode:
    def __init__(
        self,
        p2p_port     : int  = 8333,
        rpc_port     : int  = 8334,
        seed_nodes   : list = None,
        mine         : bool = False,
        mine_interval: int  = 30,      # seconds between auto-mine attempts
        data_dir     : str  = './obelyth_data',
        founder_key  : str  = None,    # path to founder WIF key file
    ):
        self.p2p_port      = p2p_port
        self.rpc_port      = rpc_port
        self.mine_enabled  = mine
        self.mine_interval = mine_interval
        self.data_dir      = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.started_at    = time.time()

        # ── Wallet ──
        wallet_path = self.data_dir / 'wallet.json'
        if wallet_path.exists():
            self.wallet = Wallet.load(str(wallet_path))
            log.info(f"Loaded wallet: {self.wallet.primary_address}")
        else:
            self.wallet = Wallet.new('node-wallet')
            self.wallet.save(str(wallet_path))
            log.info(f"New wallet: {self.wallet.primary_address}")

        # ── Founder key ──
        self.founder_privkey = None
        if founder_key and Path(founder_key).exists():
            wif = Path(founder_key).read_text().strip()
            self.founder_privkey = PrivateKey.from_wif(wif)
            founder_pub  = self.founder_privkey.public_key()
            founder_addr = founder_pub.to_address()
            log.info(f"Founder key loaded: {founder_addr}")
        else:
            # Use wallet primary as founder in dev mode
            founder_addr = self.wallet.primary_address
            log.warning(f"No founder key — using node wallet as founder: {founder_addr}")

        # ── Blockchain ──
        self.chain = Blockchain(founder_address=founder_addr, genesis=True)

        # ── Network ──
        self.network = NetworkNode(
            port       = p2p_port,
            seed_nodes = seed_nodes or DEFAULT_SEED_NODES,
        )
        self._wire_network_callbacks()

    # ── Network Callbacks ──────────────────────────────────────────────────────

    def _wire_network_callbacks(self):
        chain = self.chain
        dag   = self.chain.dag

        def on_block(block_dict: dict) -> bool:
            from core.structures import Block
            try:
                block = Block.from_dict(block_dict)
                return chain.add_block(block)
            except Exception as e:
                log.warning(f"Block parse error: {e}")
                return False

        def on_tx(tx_dict: dict) -> bool:
            try:
                tx = Transaction.from_dict(tx_dict)
                return chain.add_to_mempool(tx)
            except Exception as e:
                log.warning(f"TX parse error: {e}")
                return False

        def get_hashes(from_height: int) -> list[str]:
            return [
                b.hash for b in dag.all_blocks()
                if b.height > from_height
            ]

        self.network.on_block_received = on_block
        self.network.on_tx_received    = on_tx
        self.network.get_chain_height  = lambda: dag.height()
        self.network.get_block_hashes  = get_hashes

    # ── Start ──────────────────────────────────────────────────────────────────

    def start(self):
        # P2P
        self.network.start()

        # RPC
        RPCHandler.node = self
        rpc = HTTPServer(('0.0.0.0', self.rpc_port), RPCHandler)
        threading.Thread(target=rpc.serve_forever, daemon=True, name='rpc').start()
        log.info(f"RPC listening on http://0.0.0.0:{self.rpc_port}")

        # Mining loop
        if self.mine_enabled:
            threading.Thread(target=self._mine_loop, daemon=True, name='miner').start()
            log.info(f"Auto-miner started (interval={self.mine_interval}s)")

        log.info("=== Obelyth node running ===")
        self._print_status()

    def _mine_loop(self):
        time.sleep(5)  # let network settle
        while True:
            try:
                block = self.chain.mine_block(
                    self.wallet.primary_address,
                    consensus=ConsensusType.POW,
                )
                if block:
                    self.network.broadcast_block(block.to_dict())
            except Exception as e:
                log.error(f"Mining error: {e}")
            time.sleep(self.mine_interval)

    def _print_status(self):
        s = self.chain.state_summary()
        v = self.chain.vesting
        print()
        print("  ┌─────────────────────────────────────────┐")
        print("  │         OBELYTH NODE STARTED          │")
        print("  ├─────────────────────────────────────────┤")
        print(f"  │  P2P port  : {self.p2p_port:<27} │")
        print(f"  │  RPC port  : {self.rpc_port:<27} │")
        print(f"  │  Address   : {self.wallet.primary_address[:27]:<27} │")
        print(f"  │  Chain ht  : {s['height']:<27} │")
        print(f"  │  Difficulty: {s['difficulty']:<27} │")
        print(f"  │  Founder   : {v.founder_address[:27]:<27} │")
        print(f"  │  Vested    : {v.vested_amount(int(time.time())):>12.2f} OBY (of {v.total_oby:,.0f})   │")
        print(f"  │  Locked    : {v.locked_amount(int(time.time())):>12.2f} OBY              │")
        print("  └─────────────────────────────────────────┘")
        print()

    def run_forever(self):
        self.start()
        try:
            while True:
                time.sleep(60)
                s = self.chain.state_summary()
                log.info(
                    f"Height={s['height']} Mempool={s['mempool']} "
                    f"Peers={self.network.status()['peers']} "
                    f"Burned={s['total_burned']:.4f} OBY"
                )
        except KeyboardInterrupt:
            log.info("Shutting down...")
            self.network.stop()


# ── CLI Entry Point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Obelyth Full Node')
    parser.add_argument('--port',         type=int, default=8333,          help='P2P port')
    parser.add_argument('--rpc-port',     type=int, default=8334,          help='RPC port')
    parser.add_argument('--seed',         type=str, action='append',       help='host:port seed node')
    parser.add_argument('--mine',         action='store_true',             help='Enable auto-mining')
    parser.add_argument('--mine-interval',type=int, default=30,            help='Seconds between mine attempts')
    parser.add_argument('--data-dir',     type=str, default='./obelyth_data',help='Data directory')
    parser.add_argument('--founder-key',  type=str, default=None,          help='Path to founder WIF key file')
    args = parser.parse_args()

    seeds = []
    for s in (args.seed or []):
        host, port = s.split(':')
        seeds.append((host, int(port)))

    node = FullNode(
        p2p_port      = args.port,
        rpc_port      = args.rpc_port,
        seed_nodes    = seeds,
        mine          = args.mine,
        mine_interval = args.mine_interval,
        data_dir      = args.data_dir,
        founder_key   = args.founder_key,
    )
    node.run_forever()


if __name__ == '__main__':
    main()
