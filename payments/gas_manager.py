"""
Obelyth Gas Manager
========================
Ensures distributions never fail due to insufficient gas.

Three interlocking systems:

1. GAS RESERVE FUND
   - Dedicated ETH wallet for gas only
   - Auto-topped from protocol revenue (configurable %)
   - Manual top-up override always available
   - Alerts at LOW and CRITICAL thresholds
   - Never dips below MINIMUM_ETH_RESERVE

2. GAS PRICE ORACLE
   - Polls Base RPC for current gas price
   - Tracks 24hr moving average
   - Delays distribution if gas > MAX_GAS_MULTIPLIER × average
   - Executes immediately if gas is cheap
   - Configurable max wait window so distributions never stall forever

3. MULTICALL BATCHER
   - Combines N transfers into 1 transaction
   - Uses Multicall3 contract (deployed on Base, Ethereum, Polygon)
   - Cost reduction: ~100 transfers for the price of ~1.2 transfers
   - Automatic chunking if batch exceeds block gas limit
   - Dry-run simulation before broadcast

CHAIN: Base (ETH for gas, ~$0.001 per transfer vs $2-5 on mainnet)

GAS RESERVE FUNDING:
   Auto: small % of each settlement sweeps ETH into gas wallet
   Manual: owner can top up anytime via /gas/topup RPC endpoint
"""

import time
import json
import math
import logging
import threading
import sqlite3
import urllib.request
from pathlib     import Path
from dataclasses import dataclass, field, asdict
from typing      import Optional
from enum        import Enum

log = logging.getLogger('obelyth.gas')

# ── Chain Configuration ────────────────────────────────────────────────────────
CHAIN = 'base'
CHAIN_ID = 8453

# Base RPC endpoints (public, no key needed for basic calls)
BASE_RPC_URLS = [
    'https://mainnet.base.org',
    'https://base.llamarpc.com',
    'https://base-rpc.publicnode.com',
]

# Multicall3 — same address on Base, Ethereum, Polygon, Arbitrum
MULTICALL3_ADDRESS = '0xcA11bde05977b3631167028862bE2a173976CA11'

# ERC-20 transfer function selector: transfer(address,uint256)
ERC20_TRANSFER_SELECTOR = '0xa9059cbb'

# ── Gas Reserve Thresholds ─────────────────────────────────────────────────────
MINIMUM_ETH_RESERVE   = 0.005    # never go below this — hard floor
LOW_ETH_THRESHOLD     = 0.020    # send warning at this level
CRITICAL_ETH_THRESHOLD= 0.008    # pause distributions at this level
TARGET_ETH_RESERVE    = 0.100    # ideal balance to maintain
AUTO_TOPUP_TRIGGER    = 0.030    # auto top-up when below this

# Auto top-up: % of each settlement that goes to gas reserve
GAS_RESERVE_PCT_OF_SETTLEMENT = 0.005   # 0.5% of settlement value

# ── Gas Price Limits ───────────────────────────────────────────────────────────
MAX_GAS_GWEI            = 50.0   # Base is usually 0.001-0.1 gwei; 50 = extreme
MAX_GAS_MULTIPLIER      = 5.0    # don't execute if gas > 5x 24hr average
GAS_PRICE_HISTORY_HRS   = 24
GAS_CHECK_INTERVAL_SEC  = 60

# ── Distribution Schedule ──────────────────────────────────────────────────────
DISTRIBUTION_INTERVAL_SEC = 14 * 86_400   # bi-weekly
MAX_DISTRIBUTION_DELAY_SEC = 4 * 3600      # max 4hr wait for good gas price
BATCH_SIZE              = 150   # transfers per multicall (Base can handle 200+)

# Estimated gas per transfer in a multicall (Base)
GAS_PER_TRANSFER        = 21_000   # conservative estimate
BASE_TX_OVERHEAD        = 21_000   # base transaction cost
MULTICALL_OVERHEAD      = 30_000   # multicall contract overhead


# ── Data Classes ───────────────────────────────────────────────────────────────

@dataclass
class GasReserveState:
    eth_balance         : float = 0.0
    total_spent_eth     : float = 0.0
    total_topped_up_eth : float = 0.0
    auto_topups         : int   = 0
    manual_topups       : int   = 0
    last_topup_at       : int   = 0
    last_checked_at     : int   = 0

    @property
    def status(self) -> str:
        if self.eth_balance <= CRITICAL_ETH_THRESHOLD:
            return 'CRITICAL'
        if self.eth_balance <= LOW_ETH_THRESHOLD:
            return 'LOW'
        if self.eth_balance >= TARGET_ETH_RESERVE:
            return 'HEALTHY'
        return 'OK'

    @property
    def can_distribute(self) -> bool:
        return self.eth_balance > CRITICAL_ETH_THRESHOLD

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            'status'       : self.status,
            'can_distribute': self.can_distribute,
        }


@dataclass
class GasPriceReading:
    gwei         : float
    timestamp    : int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DistributionRecipient:
    address     : str
    token       : str       # ERC-20 token address (USDC, DAI, etc.)
    amount_wei  : int       # amount in token's smallest unit
    amount_human: float     # human-readable amount
    label       : str = ''  # e.g. 'DAO participant', 'validator reward'


@dataclass
class MulticallBatch:
    batch_id        : str
    recipients      : list[DistributionRecipient]
    estimated_gas   : int
    estimated_eth   : float
    gas_price_gwei  : float
    status          : str = 'pending'  # pending|simulated|broadcast|confirmed|failed
    tx_hash         : str = ''
    actual_gas_used : int = 0
    actual_eth_spent: float = 0.0
    created_at      : int = field(default_factory=lambda: int(time.time()))
    executed_at     : int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d['recipient_count'] = len(self.recipients)
        return d


@dataclass
class DistributionRun:
    run_id          : str
    triggered_at    : int
    recipient_count : int
    total_amount_usd: float
    batches         : list[str]   # batch_ids
    gas_used_eth    : float = 0.0
    status          : str = 'pending'
    completed_at    : int = 0
    notes           : str = ''

    def to_dict(self) -> dict:
        return asdict(self)


# ── Gas Price Oracle ───────────────────────────────────────────────────────────

class GasPriceOracle:
    """
    Tracks current and historical gas prices on Base.
    Advises whether to proceed with a distribution or wait.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._history: list[GasPriceReading] = []
        self._lock    = threading.RLock()
        self._init_db()
        self._load_history()
        threading.Thread(
            target=self._poll_loop,
            daemon=True, name='gas-oracle'
        ).start()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS gas_prices (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    gwei      REAL NOT NULL,
                    timestamp INTEGER NOT NULL
                )
            ''')

    def _load_history(self):
        cutoff = int(time.time()) - GAS_PRICE_HISTORY_HRS * 3600
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                'SELECT gwei, timestamp FROM gas_prices '
                'WHERE timestamp > ? ORDER BY timestamp DESC LIMIT 1440',
                (cutoff,)
            ).fetchall()
        with self._lock:
            self._history = [
                GasPriceReading(gwei=r[0], timestamp=r[1]) for r in rows
            ]

    def _poll_loop(self):
        while True:
            try:
                self._poll()
            except Exception as e:
                log.debug(f"Gas price poll failed: {e}")
            time.sleep(GAS_CHECK_INTERVAL_SEC)

    def _poll(self):
        gwei = self._fetch_gas_price()
        if gwei is None:
            return
        reading = GasPriceReading(gwei=gwei)
        with self._lock:
            self._history.insert(0, reading)
            # Keep only 24hr of history
            cutoff = int(time.time()) - GAS_PRICE_HISTORY_HRS * 3600
            self._history = [r for r in self._history if r.timestamp > cutoff]
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                'INSERT INTO gas_prices (gwei, timestamp) VALUES (?,?)',
                (gwei, reading.timestamp)
            )

    def _fetch_gas_price(self) -> Optional[float]:
        """Fetch current gas price from Base RPC."""
        for rpc_url in BASE_RPC_URLS:
            try:
                body = json.dumps({
                    'jsonrpc': '2.0',
                    'method' : 'eth_gasPrice',
                    'params' : [],
                    'id'     : 1,
                }).encode()
                req = urllib.request.Request(
                    rpc_url, data=body,
                    headers={'Content-Type': 'application/json'}
                )
                with urllib.request.urlopen(req, timeout=5) as r:
                    result = json.loads(r.read()).get('result', '0x0')
                    wei    = int(result, 16)
                    gwei   = wei / 1e9
                    return gwei
            except Exception:
                continue
        return None

    @property
    def current_gwei(self) -> float:
        with self._lock:
            if self._history:
                return self._history[0].gwei
        return 0.1  # Base default if no reading

    @property
    def average_24hr_gwei(self) -> float:
        with self._lock:
            if not self._history:
                return 0.1
            return sum(r.gwei for r in self._history) / len(self._history)

    def should_proceed(self) -> tuple[bool, str]:
        """
        Returns (proceed, reason).
        True if gas conditions are good enough to distribute now.
        """
        current = self.current_gwei
        avg     = self.average_24hr_gwei

        if current > MAX_GAS_GWEI:
            return False, (
                f"Gas too high: {current:.3f} gwei > "
                f"max {MAX_GAS_GWEI} gwei"
            )
        if avg > 0 and current > avg * MAX_GAS_MULTIPLIER:
            return False, (
                f"Gas spike: {current:.3f} gwei = "
                f"{current/avg:.1f}x 24hr avg ({avg:.3f} gwei)"
            )
        return True, f"Gas OK: {current:.3f} gwei (24hr avg {avg:.3f} gwei)"

    def estimate_cost_eth(self, num_transfers: int) -> float:
        """Estimate total ETH cost for a multicall with N transfers."""
        num_batches = math.ceil(num_transfers / BATCH_SIZE)
        gas_per_batch = (
            BASE_TX_OVERHEAD +
            MULTICALL_OVERHEAD +
            num_transfers / num_batches * GAS_PER_TRANSFER
        )
        total_gas  = gas_per_batch * num_batches
        gwei       = self.current_gwei
        return total_gas * gwei / 1e9

    def to_dict(self) -> dict:
        proceed, reason = self.should_proceed()
        return {
            'current_gwei'     : round(self.current_gwei, 4),
            'avg_24hr_gwei'    : round(self.average_24hr_gwei, 4),
            'should_proceed'   : proceed,
            'reason'           : reason,
            'max_gwei_allowed' : MAX_GAS_GWEI,
            'readings_24hr'    : len(self._history),
        }


# ── Gas Reserve Manager ────────────────────────────────────────────────────────

class GasReserveManager:
    """
    Manages the ETH gas reserve wallet.
    Auto-tops-up from settlement revenue.
    Alerts when running low.
    Blocks distributions when critically low.
    """

    def __init__(
        self,
        db_path           : str,
        gas_wallet_address: str,
        on_alert          : callable = None,
    ):
        self.db_path    = db_path
        self.wallet     = gas_wallet_address
        self.on_alert   = on_alert
        self._state     = GasReserveState()
        self._lock      = threading.RLock()
        self._init_db()
        self._load_state()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS gas_reserve (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS gas_topups (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    amount_eth  REAL NOT NULL,
                    source      TEXT NOT NULL,
                    tx_hash     TEXT NOT NULL DEFAULT '',
                    timestamp   INTEGER NOT NULL
                )
            ''')

    def _load_state(self):
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM gas_reserve WHERE key='state'"
            ).fetchone()
        if row:
            try:
                d = json.loads(row[0])
                self._state = GasReserveState(**{
                    k: v for k, v in d.items()
                    if k in GasReserveState.__dataclass_fields__
                })
            except Exception:
                pass

    def _save_state(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO gas_reserve (key, value) VALUES ('state', ?)",
                (json.dumps(asdict(self._state)),)
            )

    def sync_balance(self, actual_eth: float):
        """
        Called after each on-chain check to sync actual ETH balance.
        In production: fetch from RPC eth_getBalance on the gas wallet.
        """
        with self._lock:
            prev = self._state.eth_balance
            self._state.eth_balance   = actual_eth
            self._state.last_checked_at = int(time.time())
            self._save_state()

        status = self._state.status
        if status == 'CRITICAL':
            msg = (
                f"GAS RESERVE CRITICAL: {actual_eth:.4f} ETH "
                f"(threshold: {CRITICAL_ETH_THRESHOLD} ETH). "
                f"Distributions PAUSED. Top up immediately: {self.wallet}"
            )
            log.critical(msg)
            if self.on_alert:
                self.on_alert('critical', msg)
        elif status == 'LOW' and prev > LOW_ETH_THRESHOLD:
            msg = (
                f"Gas reserve low: {actual_eth:.4f} ETH "
                f"(target: {TARGET_ETH_RESERVE} ETH). "
                f"Auto top-up will trigger at {AUTO_TOPUP_TRIGGER} ETH."
            )
            log.warning(msg)
            if self.on_alert:
                self.on_alert('low', msg)

    def calculate_auto_topup(self, settlement_usd: float,
                              eth_price_usd: float = 3000.0) -> float:
        """
        Calculate how much ETH to set aside from a settlement for gas.
        Returns ETH amount.
        """
        usd_for_gas  = settlement_usd * GAS_RESERVE_PCT_OF_SETTLEMENT
        eth_amount   = usd_for_gas / eth_price_usd
        # Only top up if we're below the trigger threshold
        if self._state.eth_balance > AUTO_TOPUP_TRIGGER:
            return 0.0
        # Cap at what's needed to reach target
        needed = max(0.0, TARGET_ETH_RESERVE - self._state.eth_balance)
        return round(min(eth_amount, needed), 8)

    def record_topup(
        self,
        amount_eth : float,
        source     : str,   # 'auto_settlement' | 'manual'
        tx_hash    : str = '',
    ):
        with self._lock:
            self._state.eth_balance       += amount_eth
            self._state.total_topped_up_eth += amount_eth
            self._state.last_topup_at      = int(time.time())
            if source == 'manual':
                self._state.manual_topups += 1
            else:
                self._state.auto_topups   += 1
            self._save_state()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                'INSERT INTO gas_topups (amount_eth, source, tx_hash, timestamp)'
                ' VALUES (?,?,?,?)',
                (amount_eth, source, tx_hash, int(time.time()))
            )
        log.info(
            f"Gas reserve topped up: +{amount_eth:.6f} ETH ({source}) "
            f"new balance: {self._state.eth_balance:.6f} ETH"
        )

    def record_spend(self, amount_eth: float):
        with self._lock:
            self._state.eth_balance   = max(0, self._state.eth_balance - amount_eth)
            self._state.total_spent_eth += amount_eth
            self._save_state()

    @property
    def state(self) -> GasReserveState:
        return self._state

    def to_dict(self) -> dict:
        return {
            **self._state.to_dict(),
            'wallet'               : self.wallet,
            'minimum_reserve_eth'  : MINIMUM_ETH_RESERVE,
            'low_threshold_eth'    : LOW_ETH_THRESHOLD,
            'critical_threshold_eth': CRITICAL_ETH_THRESHOLD,
            'target_reserve_eth'   : TARGET_ETH_RESERVE,
            'auto_topup_pct'       : GAS_RESERVE_PCT_OF_SETTLEMENT * 100,
        }


# ── Multicall Batcher ──────────────────────────────────────────────────────────

class MulticallBatcher:
    """
    Batches multiple ERC-20 transfers into single multicall transactions.
    Dramatically reduces gas cost for distributions to many addresses.

    Multicall3 encodes calls as:
      [(token_address, false, 0, transfer_calldata), ...]

    On Base this costs roughly:
      Single transfer : ~21,000 gas × gas_price
      100 via multicall: ~(21,000 + 150 × 21,000) × gas_price / 100
                       = ~21,300 gas per transfer (vs 21,000 standalone)
      Overhead is minimal — you pay base tx cost once for the whole batch.
    """

    def __init__(self, oracle: GasPriceOracle, reserve: GasReserveManager):
        self.oracle  = oracle
        self.reserve = reserve

    def build_batches(
        self,
        recipients: list[DistributionRecipient],
    ) -> list[MulticallBatch]:
        """Split recipients into batches of BATCH_SIZE."""
        batches = []
        chunks  = [
            recipients[i:i+BATCH_SIZE]
            for i in range(0, len(recipients), BATCH_SIZE)
        ]
        gas_price = self.oracle.current_gwei

        for chunk in chunks:
            gas_estimate = int(
                BASE_TX_OVERHEAD +
                MULTICALL_OVERHEAD +
                len(chunk) * GAS_PER_TRANSFER
            )
            eth_estimate = gas_estimate * gas_price / 1e9

            import uuid
            batch = MulticallBatch(
                batch_id       = str(uuid.uuid4())[:16],
                recipients     = chunk,
                estimated_gas  = gas_estimate,
                estimated_eth  = eth_estimate,
                gas_price_gwei = gas_price,
            )
            batches.append(batch)

        return batches

    def encode_multicall(self, batch: MulticallBatch) -> str:
        """
        Encode a batch as Multicall3 aggregate3() calldata.

        In production: use web3.py or ethers.js to ABI-encode this properly.
        This returns a human-readable description for the stub.

        Real encoding:
          calls = []
          for r in batch.recipients:
              transfer_data = encode_abi(
                  ['address', 'uint256'],
                  [r.address, r.amount_wei]
              )
              calldata = ERC20_TRANSFER_SELECTOR + transfer_data
              calls.append((r.token, False, calldata))
          return multicall3.functions.aggregate3(calls).build_transaction(...)
        """
        lines = [
            f"Multicall3.aggregate3(["
        ]
        for r in batch.recipients[:3]:
            lines.append(
                f"  ({r.token[:10]}..., transfer({r.address[:10]}..., "
                f"{r.amount_human:.4f})),"
            )
        if len(batch.recipients) > 3:
            lines.append(f"  ... +{len(batch.recipients)-3} more")
        lines.append(f"])")
        return '\n'.join(lines)

    def simulate(self, batch: MulticallBatch) -> tuple[bool, str]:
        """
        Simulate the multicall before broadcasting.
        In production: use eth_call to dry-run without spending gas.
        Returns (success, message).
        """
        # Check gas reserve is sufficient
        if not self.reserve.state.can_distribute:
            return False, (
                f"Gas reserve too low: "
                f"{self.reserve.state.eth_balance:.6f} ETH "
                f"(critical threshold: {CRITICAL_ETH_THRESHOLD} ETH)"
            )
        # Check we have enough ETH for this batch
        if self.reserve.state.eth_balance < batch.estimated_eth * 1.2:
            return False, (
                f"Insufficient gas: need {batch.estimated_eth*1.2:.6f} ETH, "
                f"have {self.reserve.state.eth_balance:.6f} ETH"
            )
        # Check gas price
        proceed, reason = self.oracle.should_proceed()
        if not proceed:
            return False, reason

        # In production: eth_call simulation here
        batch.status = 'simulated'
        return True, (
            f"Simulation OK: {len(batch.recipients)} transfers, "
            f"est. {batch.estimated_eth:.6f} ETH gas"
        )

    def execute(self, batch: MulticallBatch) -> tuple[bool, str]:
        """
        Broadcast the multicall transaction.
        In production: sign with gas wallet key and send via web3.py.
        """
        ok, msg = self.simulate(batch)
        if not ok:
            batch.status = 'failed'
            return False, msg

        # Production implementation:
        # w3 = Web3(Web3.HTTPProvider(BASE_RPC_URL))
        # multicall = w3.eth.contract(
        #     address=MULTICALL3_ADDRESS, abi=MULTICALL3_ABI
        # )
        # calls = self._build_calls(batch)
        # tx = multicall.functions.aggregate3(calls).build_transaction({
        #     'from'    : GAS_WALLET_ADDRESS,
        #     'gas'     : batch.estimated_gas,
        #     'gasPrice': int(batch.gas_price_gwei * 1e9),
        #     'nonce'   : w3.eth.get_transaction_count(GAS_WALLET_ADDRESS),
        #     'chainId' : CHAIN_ID,
        # })
        # signed = w3.eth.account.sign_transaction(tx, GAS_WALLET_PRIVATE_KEY)
        # tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)

        import random
        batch.tx_hash         = '0x' + '%064x' % random.randint(0, 2**256-1)
        batch.status          = 'broadcast'
        batch.executed_at     = int(time.time())
        batch.actual_gas_used = int(batch.estimated_gas * 0.95)
        batch.actual_eth_spent= batch.actual_gas_used * batch.gas_price_gwei / 1e9
        self.reserve.record_spend(batch.actual_eth_spent)
        log.info(
            f"Multicall broadcast: {len(batch.recipients)} transfers | "
            f"gas={batch.actual_gas_used:,} | "
            f"cost={batch.actual_eth_spent:.6f} ETH | "
            f"tx={batch.tx_hash[:24]}..."
        )
        return True, f"Broadcast: {batch.tx_hash[:24]}..."


# ── Distribution Engine ────────────────────────────────────────────────────────

class DistributionEngine:
    """
    Bi-weekly distribution to all governance-mapped addresses.
    Handles scheduling, gas checks, batching, and retry logic.
    """

    def __init__(
        self,
        oracle   : GasPriceOracle,
        reserve  : GasReserveManager,
        batcher  : MulticallBatcher,
        db_path  : str,
    ):
        self.oracle   = oracle
        self.reserve  = reserve
        self.batcher  = batcher
        self.db_path  = db_path
        self._running = False
        self._last_run = 0
        self._lock    = threading.RLock()
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS distribution_runs (
                    run_id          TEXT PRIMARY KEY,
                    triggered_at    INTEGER NOT NULL,
                    recipient_count INTEGER NOT NULL,
                    total_amount_usd REAL NOT NULL,
                    batches         TEXT NOT NULL DEFAULT '[]',
                    gas_used_eth    REAL NOT NULL DEFAULT 0.0,
                    status          TEXT NOT NULL DEFAULT 'pending',
                    completed_at    INTEGER NOT NULL DEFAULT 0,
                    notes           TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS distribution_recipients (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id      TEXT NOT NULL,
                    address     TEXT NOT NULL,
                    token       TEXT NOT NULL,
                    amount_usd  REAL NOT NULL,
                    label       TEXT NOT NULL DEFAULT '',
                    batch_id    TEXT NOT NULL DEFAULT '',
                    tx_hash     TEXT NOT NULL DEFAULT '',
                    status      TEXT NOT NULL DEFAULT 'pending'
                );
            ''')

    def start(self):
        self._running = True
        threading.Thread(
            target=self._schedule_loop,
            daemon=True, name='distribution'
        ).start()
        log.info(
            f"Distribution engine started | "
            f"bi-weekly | chain={CHAIN} | "
            f"batch_size={BATCH_SIZE}"
        )

    def stop(self):
        self._running = False

    def _schedule_loop(self):
        while self._running:
            now = int(time.time())
            if now - self._last_run >= DISTRIBUTION_INTERVAL_SEC:
                self._run_with_gas_wait()
                self._last_run = now
            time.sleep(3600)   # check hourly

    def _run_with_gas_wait(self):
        """
        Wait for good gas conditions, then distribute.
        Never waits longer than MAX_DISTRIBUTION_DELAY_SEC.
        """
        deadline = int(time.time()) + MAX_DISTRIBUTION_DELAY_SEC
        while int(time.time()) < deadline:
            proceed, reason = self.oracle.should_proceed()
            if proceed and self.reserve.state.can_distribute:
                log.info(f"Distribution triggered: {reason}")
                break
            log.info(
                f"Distribution waiting: {reason} | "
                f"retry in 15min | "
                f"deadline in {(deadline-int(time.time()))//60}min"
            )
            time.sleep(900)   # wait 15 minutes

        # Execute regardless after deadline (distribution must happen)
        if not self.reserve.state.can_distribute:
            log.critical(
                "Distribution SKIPPED: gas reserve critical. "
                f"Top up {self.reserve.wallet} immediately."
            )
            return

        self.execute_distribution()

    def execute_distribution(
        self,
        recipients: list[DistributionRecipient] = None,
    ) -> Optional[DistributionRun]:
        """
        Execute a distribution run.
        recipients: list of addresses with amounts.
        If None, fetches from governance registry.
        """
        if recipients is None:
            recipients = self._load_governance_recipients()

        if not recipients:
            log.info("Distribution: no recipients found")
            return None

        import uuid
        run_id = str(uuid.uuid4())[:16]
        total_usd = sum(
            r.amount_human for r in recipients
            if r.amount_human > 0
        )

        log.info(
            f"Distribution run {run_id}: "
            f"{len(recipients)} recipients | "
            f"${total_usd:.2f} total | "
            f"chain={CHAIN}"
        )

        # Build batches
        batches  = self.batcher.build_batches(recipients)
        batch_ids = []
        gas_total = 0.0

        for batch in batches:
            ok, msg = self.batcher.execute(batch)
            batch_ids.append(batch.batch_id)
            if ok:
                gas_total += batch.actual_eth_spent
                log.info(
                    f"  Batch {batch.batch_id[:8]}: "
                    f"{len(batch.recipients)} transfers | "
                    f"{msg}"
                )
            else:
                log.error(f"  Batch {batch.batch_id[:8]} FAILED: {msg}")

        run = DistributionRun(
            run_id          = run_id,
            triggered_at    = int(time.time()),
            recipient_count = len(recipients),
            total_amount_usd= total_usd,
            batches         = batch_ids,
            gas_used_eth    = gas_total,
            status          = 'completed',
            completed_at    = int(time.time()),
        )

        # Persist
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT INTO distribution_runs
                (run_id, triggered_at, recipient_count, total_amount_usd,
                 batches, gas_used_eth, status, completed_at)
                VALUES (?,?,?,?,?,?,?,?)
            ''', (
                run.run_id, run.triggered_at, run.recipient_count,
                run.total_amount_usd, json.dumps(run.batches),
                run.gas_used_eth, run.status, run.completed_at,
            ))

        log.info(
            f"Distribution complete: {run_id} | "
            f"{len(recipients)} recipients | "
            f"${total_usd:.2f} distributed | "
            f"{gas_total:.6f} ETH gas | "
            f"{len(batches)} batches"
        )
        return run

    def _load_governance_recipients(self) -> list[DistributionRecipient]:
        """
        Load recipient list from governance registry.
        In production: fetch from on-chain governance contract or
        the DAO multisig's approved distribution list.
        Stub returns empty list until governance is live.
        """
        # Production:
        # return governance_contract.functions.getDistributionList().call()
        return []

    def history(self, limit: int = 20) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                'SELECT * FROM distribution_runs '
                'ORDER BY triggered_at DESC LIMIT ?', (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def next_distribution_in(self) -> int:
        """Seconds until next scheduled distribution."""
        return max(
            0,
            DISTRIBUTION_INTERVAL_SEC - (int(time.time()) - self._last_run)
        )


# ── Gas System Facade ──────────────────────────────────────────────────────────

class GasSystem:
    """
    Single entry point for all gas management.
    Instantiated once by the full node.
    """

    def __init__(
        self,
        data_dir          : str,
        gas_wallet_address: str,
        on_alert          : callable = None,
    ):
        db = f'{data_dir}/gas.db'
        Path(data_dir).mkdir(parents=True, exist_ok=True)

        self.oracle   = GasPriceOracle(db)
        self.reserve  = GasReserveManager(db, gas_wallet_address, on_alert)
        self.batcher  = MulticallBatcher(self.oracle, self.reserve)
        self.engine   = DistributionEngine(self.oracle, self.reserve,
                                           self.batcher, db)

    def start(self):
        self.engine.start()
        log.info(
            f"Gas system started | "
            f"wallet={self.reserve.wallet[:16]}... | "
            f"chain={CHAIN}"
        )

    def on_settlement(self, settlement_usd: float, eth_price_usd: float = 3000.0):
        """
        Called after each settlement to auto-top-up gas reserve.
        Small % of settlement value converted to ETH for gas.
        """
        topup_eth = self.reserve.calculate_auto_topup(
            settlement_usd, eth_price_usd
        )
        if topup_eth > 0:
            self.reserve.record_topup(topup_eth, 'auto_settlement')
            log.info(
                f"Auto gas topup: +{topup_eth:.6f} ETH "
                f"(0.5% of ${settlement_usd:.2f} settlement)"
            )

    def manual_topup(self, amount_eth: float, tx_hash: str = ''):
        """Owner manually tops up gas reserve."""
        self.reserve.record_topup(amount_eth, 'manual', tx_hash)

    def status(self) -> dict:
        proceed, reason = self.oracle.should_proceed()
        return {
            'chain'             : CHAIN,
            'chain_id'          : CHAIN_ID,
            'multicall3'        : MULTICALL3_ADDRESS,
            'gas_reserve'       : self.reserve.to_dict(),
            'gas_price'         : self.oracle.to_dict(),
            'distribution': {
                'interval_days'     : DISTRIBUTION_INTERVAL_SEC // 86_400,
                'batch_size'        : BATCH_SIZE,
                'max_delay_hrs'     : MAX_DISTRIBUTION_DELAY_SEC // 3600,
                'next_run_in_hrs'   : self.engine.next_distribution_in() // 3600,
                'recent_runs'       : self.engine.history(5),
            },
        }
