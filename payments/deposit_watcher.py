"""
Obelyth Deposit Watcher
============================
Monitors all developer deposit addresses on EVM chains for incoming
stablecoin deposits. When a deposit is detected and confirmed:
  1. Records the deposit in the ledger
  2. Credits the developer's account balance
  3. Triggers the 90/5/5 settlement allocation
  4. Sends deposit notification to developer

Architecture:
  - Polls each supported network every POLL_INTERVAL seconds
  - Uses public RPC endpoints (no API key needed for basic polling)
  - Production upgrade: replace polling with Alchemy/Moralis webhooks
  - Requires CONFIRMATIONS blocks before crediting (prevents double-spend)

In production: replace urllib RPC calls with web3.py or ethers.js.
This implementation stubs the chain calls and works fully offline
for testing — swap _fetch_transfers() for real RPC calls.
"""

import time
import json
import uuid
import logging
import threading
import urllib.request
import urllib.error
from typing    import Optional, Callable
from pathlib   import Path

from accounts.registry import (
    AccountRegistry, DepositRecord, NotifyEvent,
    SUPPORTED_COINS
)

log = logging.getLogger('obelyth.deposit_watcher')

# ── Configuration ──────────────────────────────────────────────────────────────
POLL_INTERVAL_SEC   = 60       # check for new deposits every 60 seconds
CONFIRMATIONS       = 6        # blocks before crediting (prevents reorg attacks)
MAX_BLOCKS_PER_POLL = 1000     # don't look back more than this many blocks

# ERC-20 Transfer event topic (keccak256 of Transfer(address,address,uint256))
TRANSFER_TOPIC = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'

# Stablecoin contract addresses (mainnet)
TOKEN_CONTRACTS = {
    'USDC': {
        'ethereum': '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48',
        'polygon' : '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174',
        'base'    : '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913',
        'arbitrum': '0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8',
    },
    'DAI': {
        'ethereum': '0x6B175474E89094C44Da98b954EedeAC495271d0F',
        'polygon' : '0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063',
        'base'    : '0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb',
        'arbitrum': '0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1',
    },
    'USDT': {
        'ethereum': '0xdAC17F958D2ee523a2206206994597C13D831ec7',
        'polygon' : '0xc2132D05D31c914a87C6611C10748AEb04B58e8F',
        'base'    : '0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2',
        'arbitrum': '0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9',
    },
    'EURC': {
        'ethereum': '0x1aBaEA1f7C830bD89Acc67eC4af516284b1bC33c',
        'base'    : '0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42',
    },
}

# Token decimals
TOKEN_DECIMALS = {
    'USDC': 6, 'USDT': 6,   # 6 decimal places
    'DAI' : 18, 'EURC': 6,
}

# Public RPC endpoints (free tier — upgrade to Alchemy/Infura in production)
RPC_ENDPOINTS = {
    'ethereum': 'https://eth.llamarpc.com',
    'polygon' : 'https://polygon-rpc.com',
    'base'    : 'https://mainnet.base.org',
    'arbitrum': 'https://arb1.arbitrum.io/rpc',
}

# USD rates — updated by oracle (stubs at 1.0 for USD coins)
COIN_RATES_USD = {
    'USDC': 1.000,
    'DAI' : 1.000,
    'USDT': 1.000,
    'EURC': 1.085,   # EUR/USD approximate
}


# ── RPC Helpers ────────────────────────────────────────────────────────────────

def _rpc_call(network: str, method: str, params: list) -> Optional[dict]:
    """Make a JSON-RPC call to the network endpoint."""
    endpoint = RPC_ENDPOINTS.get(network)
    if not endpoint:
        return None
    body = json.dumps({
        'jsonrpc': '2.0',
        'method' : method,
        'params' : params,
        'id'     : 1,
    }).encode()
    try:
        req = urllib.request.Request(
            endpoint, data=body,
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()).get('result')
    except Exception as e:
        log.debug(f"RPC {network} {method}: {e}")
        return None

def _get_block_number(network: str) -> Optional[int]:
    result = _rpc_call(network, 'eth_blockNumber', [])
    return int(result, 16) if result else None

def _get_logs(
    network       : str,
    contract_addr : str,
    from_block    : int,
    to_block      : int,
    to_addresses  : list[str],
) -> list[dict]:
    """
    Fetch ERC-20 Transfer events where `to` is one of our deposit addresses.
    Uses eth_getLogs — one call covers all addresses at once.
    """
    # Pad addresses to 32 bytes for topic matching
    topics_to = [
        '0x' + addr.lower().replace('0x', '').zfill(64)
        for addr in to_addresses
    ]
    result = _rpc_call(network, 'eth_getLogs', [{
        'fromBlock': hex(from_block),
        'toBlock'  : hex(to_block),
        'address'  : contract_addr,
        'topics'   : [TRANSFER_TOPIC, None, topics_to],
    }])
    return result if isinstance(result, list) else []

def _parse_transfer_log(log_entry: dict, coin: str) -> Optional[dict]:
    """Parse an ERC-20 Transfer log into a transfer dict."""
    try:
        topics = log_entry['topics']
        # topics[1] = from address (padded), topics[2] = to address (padded)
        to_addr = '0x' + topics[2][-40:]
        # data = amount (hex, padded to 32 bytes)
        amount_raw = int(log_entry['data'], 16)
        decimals   = TOKEN_DECIMALS.get(coin, 18)
        amount     = amount_raw / (10 ** decimals)
        return {
            'to'          : to_addr,
            'amount'      : amount,
            'tx_hash'     : log_entry['transactionHash'],
            'block_number': int(log_entry['blockNumber'], 16),
        }
    except Exception:
        return None


# ── Deposit Watcher ────────────────────────────────────────────────────────────

class DepositWatcher:
    """
    Polls EVM networks for incoming stablecoin deposits.
    Calls on_deposit when a new confirmed deposit is found.
    """

    def __init__(
        self,
        registry      : AccountRegistry,
        on_deposit     : Callable,     # called with (DepositRecord, account_id)
        last_blocks    : dict = None,  # {network: last_scanned_block}
        state_path     : str = './nexus_data/watcher_state.json',
    ):
        self.registry   = registry
        self.on_deposit = on_deposit
        self.state_path = Path(state_path)
        self._running   = False
        self._lock      = threading.RLock()
        # Load or init last-scanned block per network
        self._last_blocks = self._load_state() if self.state_path.exists() \
                            else (last_blocks or {})
        self._seen_txs  : set[str] = set()   # dedup processed tx hashes

    def start(self):
        self._running = True
        threading.Thread(
            target=self._watch_loop,
            daemon=True, name='deposit-watcher'
        ).start()
        log.info(
            f"Deposit watcher started | "
            f"polling every {POLL_INTERVAL_SEC}s | "
            f"networks: {list(RPC_ENDPOINTS.keys())}"
        )

    def stop(self):
        self._running = False

    def _watch_loop(self):
        while self._running:
            try:
                self._scan_all_networks()
            except Exception as e:
                log.error(f"Watcher error: {e}")
            time.sleep(POLL_INTERVAL_SEC)

    def _scan_all_networks(self):
        """Scan all networks for deposits to any registered address."""
        # Get all deposit addresses across all accounts
        all_addresses = self.registry.get_all_deposit_addresses()
        if not all_addresses:
            return

        for network in RPC_ENDPOINTS:
            try:
                self._scan_network(network, all_addresses)
            except Exception as e:
                log.warning(f"Network scan failed {network}: {e}")

    def _scan_network(self, network: str, all_addresses: dict[str, str]):
        current_block = _get_block_number(network)
        if not current_block:
            log.debug(f"Could not get block number for {network}")
            return

        # Confirmed block = current - CONFIRMATIONS
        safe_block = current_block - CONFIRMATIONS
        last_block = self._last_blocks.get(network, safe_block - 100)

        if safe_block <= last_block:
            return   # nothing new

        from_block = last_block + 1
        to_block   = min(safe_block, from_block + MAX_BLOCKS_PER_POLL - 1)

        # Get deposit addresses relevant to this network
        network_addrs = {
            addr: acct_id
            for addr, acct_id in all_addresses.items()
        }
        if not network_addrs:
            return

        # Scan each supported coin on this network
        for coin, networks in SUPPORTED_COINS.items():
            if network not in networks:
                continue
            contract = TOKEN_CONTRACTS.get(coin, {}).get(network)
            if not contract:
                continue

            logs = _get_logs(
                network, contract, from_block, to_block,
                list(network_addrs.keys())
            )

            for entry in logs:
                transfer = _parse_transfer_log(entry, coin)
                if not transfer:
                    continue

                tx_key = f"{transfer['tx_hash']}:{coin}"
                if tx_key in self._seen_txs:
                    continue

                to_addr  = transfer['to'].lower()
                acct_id  = network_addrs.get(to_addr)
                if not acct_id:
                    continue

                if transfer['amount'] < MIN_DEPOSIT_USD / COIN_RATES_USD.get(coin, 1.0):
                    log.info(
                        f"Deposit below minimum: "
                        f"{transfer['amount']:.4f} {coin} on {network}"
                    )
                    continue

                amount_usd = transfer['amount'] * COIN_RATES_USD.get(coin, 1.0)
                deposit    = DepositRecord(
                    deposit_id      = str(uuid.uuid4()),
                    account_id      = acct_id,
                    coin            = coin,
                    network         = network,
                    amount_coin     = transfer['amount'],
                    amount_usd      = round(amount_usd, 6),
                    tx_hash         = transfer['tx_hash'],
                    block_number    = transfer['block_number'],
                    deposit_address = to_addr,
                    status          = 'confirmed',
                )
                self.registry.record_deposit(deposit)
                self._seen_txs.add(tx_key)
                log.info(
                    f"Deposit detected: {transfer['amount']:.4f} {coin} "
                    f"on {network} → account {acct_id[:8]} "
                    f"(${amount_usd:.4f}) tx={transfer['tx_hash'][:16]}"
                )
                self.on_deposit(deposit, acct_id)

        # Update last scanned block
        with self._lock:
            self._last_blocks[network] = to_block
        self._save_state()

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except Exception:
            return {}

    def _save_state(self):
        try:
            with self._lock:
                self.state_path.write_text(
                    json.dumps(self._last_blocks, indent=2)
                )
        except Exception as e:
            log.warning(f"Could not save watcher state: {e}")

    # ── Stub for testing (no live network needed) ──────────────────────────────

    def simulate_deposit(
        self,
        account_id   : str,
        coin         : str,
        network      : str,
        amount_coin  : float,
    ) -> DepositRecord:
        """
        Simulate an incoming deposit for testing without a live network.
        Calls on_deposit as if a real transfer was detected.
        """
        import random
        amount_usd = amount_coin * COIN_RATES_USD.get(coin, 1.0)
        deposit = DepositRecord(
            deposit_id      = str(uuid.uuid4()),
            account_id      = account_id,
            coin            = coin,
            network         = network,
            amount_coin     = amount_coin,
            amount_usd      = round(amount_usd, 6),
            tx_hash         = '0x' + '%064x' % random.randint(0, 2**256-1),
            block_number    = random.randint(19_000_000, 20_000_000),
            deposit_address = self.registry.deriver.derive_address(
                account_id, coin, network
            ),
            status          = 'confirmed',
        )
        self.registry.record_deposit(deposit)
        self.on_deposit(deposit, account_id)
        return deposit


MIN_DEPOSIT_USD = 1.0
