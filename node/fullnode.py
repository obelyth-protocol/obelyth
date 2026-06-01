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
from tokenomics.engine  import TokenomicsEngine
from compute.api        import ComputeAPI

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
        path_only = self.path.split('?')[0]
        query = self.path.split('?', 1)[1] if '?' in self.path else ''
        routes = {
            '/status'                    : self._status,
            '/peers'                     : self._peers,
            '/mempool'                   : self._mempool,
            '/vesting'                   : self._vesting,
            '/balance'                   : self._balance,
            '/utxos'                     : self._utxos_for,
            '/blocks'                    : self._blocks_list,
            '/tx'                        : self._tx_lookup,
            '/address'                   : self._address_history,
            '/compute/nextjob'           : lambda: self._compute_nextjob(query),
            '/compute/pending_challenges': lambda: self._compute_pending_challenges(query),
            '/faucet/status'             : self._faucet_status,
        }
        handler = routes.get(path_only)
        if handler:
            data = handler()
            if isinstance(data, tuple):
                code, body = data
                self._json(code, body)
            else:
                self._json(200, data)
        else:
            self._json(404, {'error': 'Not found'})

    def do_OPTIONS(self):
        # CORS preflight. Browsers send this before a cross-origin POST with a
        # JSON content-type. Without a proper response here, the browser blocks
        # the actual POST and the fetch() fails with "Failed to fetch".
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Max-Age', '86400')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length)
        try:
            req = json.loads(body) if body else {}
        except Exception:
            self._json(400, {'error': 'Invalid JSON'})
            return

        routes = {
            '/sendtx'                    : self._send_tx,
            '/mineblock'                 : self._mine_block,
            '/addpeer'                   : self._add_peer,
            '/getblock'                  : self._get_block,
            # ── Compute routes ──
            '/compute/quote'             : self._compute_quote,
            '/compute/submit'            : self._compute_submit,
            '/compute/job'               : self._compute_job,
            '/compute/infer'             : self._compute_infer,
            '/compute/register'          : self._compute_register,
            '/compute/heartbeat'         : self._compute_heartbeat,
            '/compute/result'            : self._compute_result,
            '/compute/challenge_resolve' : self._compute_challenge_resolve,
            # ── Faucet routes ──
            '/faucet/claim'              : self._faucet_claim,
        }
        handler = routes.get(self.path)
        if handler:
            data = handler(req)
            if isinstance(data, tuple):
                code, body = data
                self._json(code, body)
            else:
                self._json(200, data)
        else:
            self._json(404, {'error': 'Not found'})

    def _json(self, code: int, data: dict):
        body = json.dumps(data, indent=2).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
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

    def _utxos_for(self):
        """GET /utxos?addr=... — returns unspent UTXOs for a wallet to spend.
        The web wallet needs this to build transactions client-side."""
        addr = self.path.split('?addr=')[-1] if '?addr=' in self.path else ''
        if not addr:
            return {'error': 'Provide ?addr=<address>'}
        utxos = self.node.chain.utxos.unspent_for(addr)
        return {
            'address': addr,
            'count'  : len(utxos),
            'utxos'  : [u.to_dict() for u in utxos],
            'total'  : round(sum(u.amount for u in utxos), 8),
        }

    def _blocks_list(self):
        """GET /blocks?limit=N&before=H — paginated block list, newest first.

        `limit` (default 20, max 100) sets page size.
        `before` (optional) is a block hash; results start from blocks
        immediately before that one in height order. Used by the explorer
        to page back through history.

        Returns a lean per-block summary — full block details available via
        POST /getblock or GET /blocks?hash=H.
        """
        import urllib.parse
        q = urllib.parse.parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
        limit  = max(1, min(int(q.get('limit', ['20'])[0]), 100))
        before = q.get('before', [None])[0]

        all_blocks = self.node.chain.dag.all_blocks()
        # Sort newest first by height, tiebreak on hash for determinism
        all_blocks.sort(key=lambda b: (-b.header.height, b.hash))

        if before:
            # Drop everything up to and including 'before'
            idx = next((i for i, b in enumerate(all_blocks) if b.hash == before), None)
            if idx is None:
                return {'error': f'block {before} not found'}
            all_blocks = all_blocks[idx + 1:]

        page = all_blocks[:limit]
        return {
            'count'        : len(page),
            'total_blocks' : len(self.node.chain.dag),
            'chain_height' : self.node.chain.dag.height(),
            'blocks'       : [{
                'hash'         : b.hash,
                'height'       : b.header.height,
                'parent_hashes': b.header.parent_hashes,
                'timestamp'    : b.header.timestamp,
                'miner'        : b.header.miner_address,
                'consensus'    : b.header.consensus_type.value
                                  if hasattr(b.header.consensus_type, 'value')
                                  else b.header.consensus_type,
                'tx_count'     : len(b.transactions),
                'difficulty'   : b.header.difficulty,
            } for b in page],
            'next_before'  : page[-1].hash if len(page) == limit else None,
        }

    def _tx_lookup(self):
        """GET /tx?hash=H — find a transaction anywhere in the chain or mempool.

        Returns the tx body plus where we found it (which block, or 'mempool'),
        so the explorer can link from a tx to its containing block.
        """
        import urllib.parse
        q = urllib.parse.parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
        h = q.get('hash', [''])[0]
        if not h:
            return {'error': 'Provide ?hash=<tx_hash>'}

        # Search mempool first (cheap)
        for tx in self.node.chain.mempool:
            if tx.hash == h:
                return {'found': True, 'location': 'mempool', 'tx': tx.to_dict()}

        # Then search blocks. We walk newest-first because recent txs are the
        # most common lookup. For a large chain this should be replaced with
        # a tx_hash -> block_hash index; not worth building until size demands.
        blocks = self.node.chain.dag.all_blocks()
        blocks.sort(key=lambda b: -b.header.height)
        for block in blocks:
            for tx in block.transactions:
                if tx.hash == h:
                    return {
                        'found'      : True,
                        'location'   : 'block',
                        'block_hash' : block.hash,
                        'block_height': block.header.height,
                        'tx'         : tx.to_dict(),
                    }
        return {'found': False, 'error': 'tx not found'}

    def _address_history(self):
        """GET /address?addr=H&limit=N — balance plus tx history for an address.

        Returns balance, current UTXO count, and the list of transactions
        (in any block) where this address appears as an input source or
        output destination. Sorted newest-first.

        Like /tx, this walks every block. Acceptable on a small testnet;
        replace with an address-index for a real production chain.
        """
        import urllib.parse
        q = urllib.parse.parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
        addr  = q.get('addr', [''])[0]
        limit = max(1, min(int(q.get('limit', ['50'])[0]), 200))
        if not addr:
            return {'error': 'Provide ?addr=<address>'}

        # Walk blocks newest first, collect matching txs
        blocks = self.node.chain.dag.all_blocks()
        blocks.sort(key=lambda b: -b.header.height)

        history = []
        # Build a UTXO lookup so we can resolve input addresses (which the
        # tx itself doesn't carry — inputs reference a tx_hash:index)
        utxo_index = {f"{tx.hash}:{i}": out
                      for block in blocks
                      for tx in block.transactions
                      for i, out in enumerate(tx.outputs)}

        for block in blocks:
            for tx in block.transactions:
                # Is this address an output destination?
                received = sum(o.amount for o in tx.outputs if o.address == addr)
                # Or an input source? (look up referenced UTXO)
                sent = sum(
                    utxo_index.get(f"{i.utxo_tx_hash}:{i.utxo_index}").amount
                    for i in tx.inputs
                    if utxo_index.get(f"{i.utxo_tx_hash}:{i.utxo_index}")
                    and utxo_index[f"{i.utxo_tx_hash}:{i.utxo_index}"].address == addr
                )
                if received > 0 or sent > 0:
                    history.append({
                        'tx_hash'     : tx.hash,
                        'tx_type'     : tx.tx_type.value if hasattr(tx.tx_type, 'value') else tx.tx_type,
                        'block_hash'  : block.hash,
                        'block_height': block.header.height,
                        'timestamp'   : block.header.timestamp,
                        'received'    : round(received, 8),
                        'sent'        : round(sent, 8),
                        'net'         : round(received - sent, 8),
                        'fee'         : tx.fee if sent > 0 else 0.0,
                    })
                if len(history) >= limit:
                    break
            if len(history) >= limit:
                break

        return {
            'address'    : addr,
            'balance'    : self.node.chain.utxos.balance(addr),
            'utxo_count' : len(self.node.chain.utxos.unspent_for(addr)),
            'tx_count'   : len(history),
            'history'    : history,
        }

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

    # ── Compute routes ────────────────────────────────────────────────────────

    def _compute_quote(self, req):
        return self.node.compute_api.quote(req)

    def _compute_submit(self, req):
        return self.node.compute_api.submit(req)

    def _compute_job(self, req):
        return self.node.compute_api.job_status(req)

    def _compute_infer(self, req):
        return self.node.compute_api.infer(req)

    def _compute_register(self, req):
        return self.node.compute_api.register_miner(req)

    def _compute_heartbeat(self, req):
        return self.node.compute_api.heartbeat(req)

    def _compute_nextjob(self, query: str):
        return self.node.compute_api.next_job(query)

    def _compute_pending_challenges(self, query: str):
        return self.node.compute_api.pending_challenges(query)

    def _compute_result(self, req):
        return self.node.compute_api.submit_result(req)

    def _compute_challenge_resolve(self, req):
        return self.node.compute_api.resolve_challenge(req)

    # ── Faucet routes ────────────────────────────────────────────────────────

    def _faucet_status(self):
        if self.node.faucet is None:
            return (503, {'error': 'faucet disabled'})
        return self.node.faucet.status()

    def _faucet_claim(self, req):
        from faucet import FaucetError, FaucetMissingApiKey, FaucetUnknownAccount, \
            FaucetInvalidAddress, FaucetAlreadyClaimed, FaucetIPCooldown, \
            FaucetBudgetExhausted, FaucetReserveDry
        if self.node.faucet is None:
            return (503, {'error': 'faucet disabled'})
        # Source IP comes from the HTTP connection — never trust a client-
        # provided IP field. Use the first hop of the TCP socket.
        source_ip = self.client_address[0] if self.client_address else 'unknown'
        try:
            claim = self.node.faucet.claim(
                address   = req.get('address', ''),
                source_ip = source_ip,
                api_key   = req.get('api_key', ''),
            )
            return (200, {
                'tx_hash'    : claim.tx_hash,
                'amount_oby' : claim.amount_oby,
                'address'    : claim.address,
                'claim_id'   : claim.claim_id,
                'claim_at'   : claim.claim_at,
            })
        except FaucetInvalidAddress as e:
            return (400, {'error': str(e), 'code': e.code})
        except FaucetMissingApiKey as e:
            return (401, {'error': str(e), 'code': e.code})
        except FaucetUnknownAccount as e:
            return (401, {'error': str(e), 'code': e.code})
        except FaucetAlreadyClaimed as e:
            return (409, {'error': str(e), 'code': e.code})
        except FaucetIPCooldown as e:
            return (429, {'error': str(e), 'code': e.code})
        except FaucetBudgetExhausted as e:
            return (503, {'error': str(e), 'code': e.code})
        except FaucetReserveDry as e:
            return (503, {'error': str(e), 'code': e.code})
        except FaucetError as e:
            return (500, {'error': str(e), 'code': e.code})


# ── Full Node ──────────────────────────────────────────────────────────────────

class FullNode:
    def __init__(
        self,
        p2p_port         : int  = 8333,
        rpc_port         : int  = 8334,
        seed_nodes       : list = None,
        mine             : bool = False,
        mine_interval    : int  = 30,      # seconds between auto-mine attempts
        data_dir         : str  = './obelyth_data',
        founder_key      : str  = None,    # path to founder WIF key file
        accounts_enabled : bool = False,   # require API key auth on /compute/submit
        faucet_enabled   : bool = False,   # enable /faucet/* endpoints
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
        # Create with genesis, then overlay a saved snapshot if one exists.
        # On a fresh node the genesis allocation stands; on a restart the
        # snapshot replaces it with the persisted chain (blocks, UTXOs,
        # validators, mempool). This is what lets the silent testnet survive
        # VPS reboots without losing balances or mined history.
        self.chain = Blockchain(founder_address=founder_addr, genesis=True)
        self.chain_state_path = self.data_dir / 'chain_state.json'
        if self.chain_state_path.exists():
            try:
                if self.chain.load(str(self.chain_state_path)):
                    log.info(f"Restored chain from {self.chain_state_path} "
                             f"(height={self.chain.dag.height()})")
            except Exception as e:
                log.error(f"Failed to load chain snapshot ({e}); "
                          f"continuing from genesis")

        # ── Tokenomics + Compute API ──
        # The engine takes providers so verification can use real chain state
        # for deterministic challenge/assignment decisions. We pass closures
        # over the chain so the engine sees fresh values on each call.
        def _block_height_provider() -> int:
            return self.chain.dag.height()

        def _block_hash_provider() -> bytes:
            tips = self.chain.dag.tips()
            if not tips:
                return b'\x00' * 32
            # Canonical: highest-height tip; tie-break on lexically smallest hash
            tips.sort(key=lambda b: (-b.header.height, b.hash))
            tip = tips[0]
            try:
                return bytes.fromhex(tip.hash)
            except (ValueError, AttributeError):
                import hashlib as _h
                return _h.sha3_256(str(tip.hash).encode()).digest()

        self.tokenomics = TokenomicsEngine(
            creator_address       = founder_addr,
            dao_address           = founder_addr,    # placeholder until DAO multisig
            block_height_provider = _block_height_provider,
            block_hash_provider   = _block_hash_provider,
        )

        # Optional persistence — load on start if a snapshot exists, save on
        # shutdown. Accounts registry is left optional for testnet/dev.
        self.tokenomics_state_path = self.data_dir / 'tokenomics_state.json'
        if self.tokenomics_state_path.exists():
            try:
                self.tokenomics.load(str(self.tokenomics_state_path))
            except Exception as e:
                log.warning(f"Could not load tokenomics state: {e}")

        self.accounts_registry = None
        if accounts_enabled:
            from accounts.registry import AccountRegistry
            self.accounts_registry = AccountRegistry(
                db_path=str(self.data_dir / 'accounts.db'),
            )
            log.info(f"Accounts registry enabled at {self.data_dir / 'accounts.db'}")
        else:
            log.warning(
                "Accounts registry DISABLED — /compute/submit accepts raw "
                "developer_addr (testnet/dev mode). Pass accounts_enabled=True "
                "in production."
            )
        self.compute_api = ComputeAPI(
            engine            = self.tokenomics,
            accounts_registry = self.accounts_registry,
        )

        # ── Faucet (optional) ──
        # The faucet uses the node's own wallet as its OBY reserve. In
        # production a dedicated wallet would be loaded here. The faucet
        # requires accounts_registry when require_api_key=True (default);
        # if accounts are disabled, the faucet falls back to anonymous mode
        # with per-address dedupe instead of per-account.
        self.faucet = None
        if faucet_enabled:
            from faucet import FaucetService
            self.faucet = FaucetService(
                chain             = self.chain,
                wallet            = self.wallet,
                accounts_registry = self.accounts_registry,
                db_path           = str(self.data_dir / 'faucet.db'),
                require_api_key   = (self.accounts_registry is not None),
            )
            log.info(
                f"Faucet enabled at /faucet/* "
                f"(payout={self.faucet.payout_oby:.0f} OBY, "
                f"daily_budget={self.faucet.daily_budget_oby:.0f} OBY)"
            )

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

        # Refund sweep loop — periodically converts faulted job refunds from OBY
        # to dev's stablecoin via AMM and credits their account balance.
        # Runs every REFUND_SWEEP_INTERVAL_S regardless of accounts_enabled (the
        # engine handles the no-registry path gracefully).
        threading.Thread(target=self._refund_sweep_loop, daemon=True,
                         name='refund-sweep').start()
        log.info(
            f"Refund sweep started (interval={self.REFUND_SWEEP_INTERVAL_S}s)"
        )

        # Consensus sweep loop — finalizes redundant-tier jobs that hit their
        # deadline without receiving all N submissions. The 30s interval is
        # tighter than the refund sweep because timeouts affect dev-visible
        # job status (refunds are background settlement).
        threading.Thread(target=self._consensus_sweep_loop, daemon=True,
                         name='consensus-sweep').start()
        log.info(
            f"Consensus sweep started (interval={self.CONSENSUS_SWEEP_INTERVAL_S}s)"
        )

        # Chain persistence loop — periodically snapshot chain + tokenomics
        # state so a restart (VPS reboot, crash, update) resumes where it left
        # off instead of resetting to genesis. Also saves on clean shutdown.
        threading.Thread(target=self._persist_loop, daemon=True,
                         name='persist').start()
        log.info(
            f"Chain persistence started (interval={self.PERSIST_INTERVAL_S}s "
            f"→ {self.chain_state_path})"
        )

        log.info("=== Obelyth node running ===")
        self._print_status()

    REFUND_SWEEP_INTERVAL_S    = 60.0   # how often to sweep faulted-job refunds
    CONSENSUS_SWEEP_INTERVAL_S = 30.0   # how often to finalize timed-out redundant jobs
    PERSIST_INTERVAL_S         = 30.0   # how often to snapshot chain + tokenomics state

    def _persist_loop(self):
        time.sleep(self.PERSIST_INTERVAL_S)
        while True:
            try:
                self.save_state()
            except Exception as e:
                log.error(f"Persist loop error: {e}")
            time.sleep(self.PERSIST_INTERVAL_S)

    def save_state(self):
        """Snapshot chain + tokenomics to disk. Safe to call anytime."""
        self.chain.save(str(self.chain_state_path))
        try:
            self.tokenomics.save(str(self.tokenomics_state_path))
        except Exception as e:
            log.error(f"Tokenomics save failed: {e}")

    def _refund_sweep_loop(self):
        # Let the rest of the node settle before the first sweep
        time.sleep(10)
        while True:
            try:
                summary = self.tokenomics.process_pending_refunds(
                    accounts_registry=self.accounts_registry,
                )
                if summary['settled'] > 0:
                    log.info(
                        f"Refund sweep: settled={summary['settled']} "
                        f"oby={summary['total_oby_swept']:.4f} "
                        f"usd_credited=${summary['total_usd_credited']:.4f} "
                        f"errors={summary['errors']}"
                    )
            except Exception as e:
                log.error(f"Refund sweep error: {e}")
            time.sleep(self.REFUND_SWEEP_INTERVAL_S)

    def _consensus_sweep_loop(self):
        """Finalize redundant-tier jobs past their consensus_deadline.

        A redundant job naturally finalizes when its Nth miner submits a
        result (in complete_job_with_verification). This loop catches the
        timeout case: if some miners go silent, the dev shouldn't be left
        with a permanently 'assigned' job.
        """
        time.sleep(10)
        while True:
            try:
                finalized = self.tokenomics.finalize_due_redundant_jobs()
                if finalized:
                    by_status = {}
                    for o in finalized:
                        by_status[o.status] = by_status.get(o.status, 0) + 1
                    log.info(
                        f"Consensus sweep: finalized {len(finalized)} "
                        f"{by_status}"
                    )
            except Exception as e:
                log.error(f"Consensus sweep error: {e}")
            time.sleep(self.CONSENSUS_SWEEP_INTERVAL_S)

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
            log.info("Shutting down — saving chain + tokenomics state...")
            try:
                self.save_state()
                log.info(f"State saved to {self.data_dir}")
            except Exception as e:
                log.warning(f"Could not save state on shutdown: {e}")
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
    parser.add_argument('--accounts-enabled', action='store_true',
                        help='Require valid API key on /compute/submit (production mode)')
    parser.add_argument('--faucet-enabled', action='store_true',
                        help='Enable /faucet/claim and /faucet/status endpoints '
                             '(testnet only; uses node wallet as reserve)')
    parser.add_argument('--challenger', action='store_true',
                        help='Run a challenger daemon in-process (polls own RPC '
                             'for pending challenges, reruns work, posts verdicts)')
    parser.add_argument('--challenger-address', type=str, default=None,
                        help='Address to advertise as challenger (defaults to node wallet)')
    args = parser.parse_args()

    seeds = []
    for s in (args.seed or []):
        host, port = s.split(':')
        seeds.append((host, int(port)))

    node = FullNode(
        p2p_port         = args.port,
        rpc_port         = args.rpc_port,
        seed_nodes       = seeds,
        mine             = args.mine,
        mine_interval    = args.mine_interval,
        data_dir         = args.data_dir,
        founder_key      = args.founder_key,
        accounts_enabled = args.accounts_enabled,
        faucet_enabled   = args.faucet_enabled,
    )

    # Optional: start in-process challenger daemon
    if args.challenger:
        from compute.challenger import ChallengerDaemon
        challenger_addr = args.challenger_address or node.wallet.primary_address
        challenger = ChallengerDaemon(
            node_url        = f'http://127.0.0.1:{args.rpc_port}',
            challenger_addr = challenger_addr,
        )
        challenger.start()
        log.info(f"Challenger daemon attached: addr={challenger_addr[:16]}")

    node.run_forever()


if __name__ == '__main__':
    main()
