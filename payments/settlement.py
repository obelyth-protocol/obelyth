"""
Obelyth Settlement Engine
==============================
Batches the 90/5/5 USDC fee split and executes on-chain transfers
to destination wallets on a schedule.

Flow:
  Developer deposits USDC → credited to internal ledger
  Developer submits jobs → costs allocated to buckets internally
  Settlement runs (daily/weekly) → sweeps funds to 3 wallets:
    90% → AMM liquidity pool address
     5% → Creator Share address
     5% → DAO multisig address

Why batch instead of settle per job:
  - On-chain ERC-20 transfers cost gas (~$0.50-$5 on Ethereum)
  - Settling per job at $0.10/job would cost more in gas than the job
  - Daily batching amortises gas across all jobs
  - Polygon/Base gas is cheap enough to settle more frequently

Internal ledger vs on-chain:
  - Internal ledger: instant, no gas, tracks every job allocation
  - On-chain settlement: periodic, gas cost, moves real USDC
  - Reconciliation: internal ledger must always match on-chain balance
"""

import time
import json
import uuid
import logging
import threading
import sqlite3
from pathlib     import Path
from dataclasses import dataclass, field, asdict
from typing      import Optional

log = logging.getLogger('obelyth.settlement')

# ── Settlement Configuration ───────────────────────────────────────────────────
SETTLEMENT_INTERVAL_SEC = 86_400    # settle once per day
MIN_SETTLEMENT_USD      = 10.0      # don't bother settling below this amount
GAS_RESERVE_PCT         = 0.005     # keep 0.5% for gas costs

# Fee split (must match tokenomics engine)
SPLIT_LIQUIDITY = 0.90
SPLIT_CREATOR   = 0.05
SPLIT_DAO       = 0.05


# ── Data Classes ───────────────────────────────────────────────────────────────

@dataclass
class JobAllocation:
    """Records how one completed job's fee is allocated."""
    allocation_id   : str
    job_id          : str
    account_id      : str
    coin            : str           # which stablecoin was used
    gross_usd       : float
    liquidity_usd   : float         # 90%
    creator_usd     : float         # 5%
    dao_usd         : float         # 5%
    settled         : bool  = False
    settlement_id   : str   = ''
    created_at      : int   = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SettlementBatch:
    """One settlement execution — sweeps all pending allocations."""
    settlement_id       : str
    period_start        : int
    period_end          : int
    jobs_included       : int
    # Amounts by destination
    liquidity_usd       : float
    creator_usd         : float
    dao_usd             : float
    total_usd           : float
    # Coin breakdown
    coin_breakdown      : dict      # {coin: {liquidity, creator, dao}}
    # On-chain tx hashes (filled after broadcast)
    liquidity_tx_hash   : str = ''
    creator_tx_hash     : str = ''
    dao_tx_hash         : str = ''
    status              : str = 'pending'   # pending|broadcast|confirmed|failed
    created_at          : int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DestinationWallets:
    """The three destination wallets for settlement."""
    liquidity_pool  : str   # AMM pool contract address
    creator_share   : str   # Founder's personal wallet (hardware wallet recommended)
    dao_multisig    : str   # DAO multisig address


# ── Ledger ─────────────────────────────────────────────────────────────────────

class SettlementLedger:
    """
    SQLite-backed ledger tracking every job allocation and settlement.
    Source of truth for what's owed to each destination wallet.
    """

    def __init__(self, db_path: str = './obelyth_data/settlement.db'):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS job_allocations (
                    allocation_id   TEXT PRIMARY KEY,
                    job_id          TEXT NOT NULL,
                    account_id      TEXT NOT NULL,
                    coin            TEXT NOT NULL,
                    gross_usd       REAL NOT NULL,
                    liquidity_usd   REAL NOT NULL,
                    creator_usd     REAL NOT NULL,
                    dao_usd         REAL NOT NULL,
                    settled         INTEGER NOT NULL DEFAULT 0,
                    settlement_id   TEXT NOT NULL DEFAULT '',
                    created_at      INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settlements (
                    settlement_id       TEXT PRIMARY KEY,
                    period_start        INTEGER NOT NULL,
                    period_end          INTEGER NOT NULL,
                    jobs_included       INTEGER NOT NULL,
                    liquidity_usd       REAL NOT NULL,
                    creator_usd         REAL NOT NULL,
                    dao_usd             REAL NOT NULL,
                    total_usd           REAL NOT NULL,
                    coin_breakdown      TEXT NOT NULL DEFAULT '{}',
                    liquidity_tx_hash   TEXT NOT NULL DEFAULT '',
                    creator_tx_hash     TEXT NOT NULL DEFAULT '',
                    dao_tx_hash         TEXT NOT NULL DEFAULT '',
                    status              TEXT NOT NULL DEFAULT 'pending',
                    created_at          INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reconciliation_log (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    checked_at      INTEGER NOT NULL,
                    internal_total  REAL NOT NULL,
                    onchain_total   REAL NOT NULL,
                    discrepancy     REAL NOT NULL,
                    status          TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_alloc_settled
                    ON job_allocations(settled, created_at);
                CREATE INDEX IF NOT EXISTS idx_alloc_job
                    ON job_allocations(job_id);
            ''')

    def record_allocation(self, job_id: str, account_id: str, coin: str,
                          gross_usd: float) -> JobAllocation:
        alloc = JobAllocation(
            allocation_id = str(uuid.uuid4()),
            job_id        = job_id,
            account_id    = account_id,
            coin          = coin,
            gross_usd     = gross_usd,
            liquidity_usd = round(gross_usd * SPLIT_LIQUIDITY, 8),
            creator_usd   = round(gross_usd * SPLIT_CREATOR,   8),
            dao_usd       = round(gross_usd * SPLIT_DAO,       8),
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT INTO job_allocations
                (allocation_id, job_id, account_id, coin, gross_usd,
                 liquidity_usd, creator_usd, dao_usd, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            ''', (
                alloc.allocation_id, alloc.job_id, alloc.account_id,
                alloc.coin, alloc.gross_usd, alloc.liquidity_usd,
                alloc.creator_usd, alloc.dao_usd, alloc.created_at,
            ))
        return alloc

    def pending_allocations(self) -> list[JobAllocation]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                'SELECT * FROM job_allocations WHERE settled = 0 '
                'ORDER BY created_at ASC'
            ).fetchall()
        return [self._row_to_alloc(r) for r in rows]

    def pending_totals(self) -> dict:
        """Sum of all unsettled allocations by bucket."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute('''
                SELECT
                    COUNT(*) as jobs,
                    SUM(gross_usd)     as gross,
                    SUM(liquidity_usd) as liquidity,
                    SUM(creator_usd)   as creator,
                    SUM(dao_usd)       as dao
                FROM job_allocations WHERE settled = 0
            ''').fetchone()
        return {
            'jobs'     : row[0] or 0,
            'gross'    : round(row[1] or 0, 6),
            'liquidity': round(row[2] or 0, 6),
            'creator'  : round(row[3] or 0, 6),
            'dao'      : round(row[4] or 0, 6),
        }

    def mark_settled(self, allocation_ids: list[str], settlement_id: str):
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.executemany('''
                    UPDATE job_allocations
                    SET settled = 1, settlement_id = ?
                    WHERE allocation_id = ?
                ''', [(settlement_id, aid) for aid in allocation_ids])

    def record_settlement(self, batch: SettlementBatch):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT OR REPLACE INTO settlements
                (settlement_id, period_start, period_end, jobs_included,
                 liquidity_usd, creator_usd, dao_usd, total_usd,
                 coin_breakdown, liquidity_tx_hash, creator_tx_hash,
                 dao_tx_hash, status, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                batch.settlement_id, batch.period_start, batch.period_end,
                batch.jobs_included, batch.liquidity_usd, batch.creator_usd,
                batch.dao_usd, batch.total_usd,
                json.dumps(batch.coin_breakdown),
                batch.liquidity_tx_hash, batch.creator_tx_hash,
                batch.dao_tx_hash, batch.status, batch.created_at,
            ))

    def settlement_history(self, limit: int = 50) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                'SELECT * FROM settlements ORDER BY created_at DESC LIMIT ?',
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def lifetime_totals(self) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute('''
                SELECT
                    COUNT(*) as settlements,
                    SUM(total_usd)     as total,
                    SUM(liquidity_usd) as liquidity,
                    SUM(creator_usd)   as creator,
                    SUM(dao_usd)       as dao
                FROM settlements WHERE status = 'confirmed'
            ''').fetchone()
        return {
            'settlements': row[0] or 0,
            'total_usd'  : round(row[1] or 0, 4),
            'liquidity'  : round(row[2] or 0, 4),
            'creator'    : round(row[3] or 0, 4),
            'dao'        : round(row[4] or 0, 4),
        }

    def _row_to_alloc(self, row) -> JobAllocation:
        return JobAllocation(
            allocation_id = row['allocation_id'],
            job_id        = row['job_id'],
            account_id    = row['account_id'],
            coin          = row['coin'],
            gross_usd     = row['gross_usd'],
            liquidity_usd = row['liquidity_usd'],
            creator_usd   = row['creator_usd'],
            dao_usd       = row['dao_usd'],
            settled       = bool(row['settled']),
            settlement_id = row['settlement_id'],
            created_at    = row['created_at'],
        )


# ── Settlement Engine ──────────────────────────────────────────────────────────

class SettlementEngine:
    """
    Executes periodic settlement of accumulated job fees to destination wallets.

    At settlement time:
      1. Pulls all unsettled job allocations from ledger
      2. Sums by coin and destination
      3. Executes on-chain ERC-20 transfers from custodial deposit wallets
         to the three destination addresses
      4. Marks allocations as settled
      5. Records the settlement batch for audit

    Production note:
      The actual ERC-20 transfers require a funded hot wallet with ETH/MATIC
      for gas. In production use a multi-sig or MPC wallet service
      (e.g. Fireblocks, Gnosis Safe) rather than a raw private key.
    """

    def __init__(
        self,
        ledger      : SettlementLedger,
        destinations: DestinationWallets,
        on_settlement_complete: callable = None,
    ):
        self.ledger       = ledger
        self.destinations = destinations
        self.on_complete  = on_settlement_complete
        self._running     = False
        self._last_settle = 0

    def start(self):
        self._running = True
        threading.Thread(
            target=self._settlement_loop,
            daemon=True, name='settlement'
        ).start()
        log.info(
            f"Settlement engine started | "
            f"interval={SETTLEMENT_INTERVAL_SEC//3600}h | "
            f"min=${MIN_SETTLEMENT_USD}"
        )

    def stop(self):
        self._running = False

    def _settlement_loop(self):
        while self._running:
            now = int(time.time())
            if now - self._last_settle >= SETTLEMENT_INTERVAL_SEC:
                try:
                    self.run_settlement()
                except Exception as e:
                    log.error(f"Settlement error: {e}")
                self._last_settle = now
            time.sleep(300)   # check every 5 minutes

    def run_settlement(self, force: bool = False) -> Optional[SettlementBatch]:
        """
        Execute one settlement cycle. Returns batch or None if nothing to settle.
        """
        totals = self.ledger.pending_totals()

        if totals['jobs'] == 0:
            log.info("Settlement: nothing to settle")
            return None

        if totals['gross'] < MIN_SETTLEMENT_USD and not force:
            log.info(
                f"Settlement: pending ${totals['gross']:.4f} below "
                f"minimum ${MIN_SETTLEMENT_USD} — skipping"
            )
            return None

        pending = self.ledger.pending_allocations()
        period_start = pending[0].created_at if pending else int(time.time())
        period_end   = int(time.time())

        # Build coin breakdown
        coin_breakdown: dict[str, dict] = {}
        for alloc in pending:
            if alloc.coin not in coin_breakdown:
                coin_breakdown[alloc.coin] = {
                    'liquidity': 0.0, 'creator': 0.0, 'dao': 0.0, 'gross': 0.0
                }
            coin_breakdown[alloc.coin]['liquidity'] += alloc.liquidity_usd
            coin_breakdown[alloc.coin]['creator']   += alloc.creator_usd
            coin_breakdown[alloc.coin]['dao']        += alloc.dao_usd
            coin_breakdown[alloc.coin]['gross']      += alloc.gross_usd

        batch = SettlementBatch(
            settlement_id   = str(uuid.uuid4()),
            period_start    = period_start,
            period_end      = period_end,
            jobs_included   = totals['jobs'],
            liquidity_usd   = totals['liquidity'],
            creator_usd     = totals['creator'],
            dao_usd         = totals['dao'],
            total_usd       = totals['gross'],
            coin_breakdown  = coin_breakdown,
        )

        log.info(
            f"Settlement #{batch.settlement_id[:8]} | "
            f"{batch.jobs_included} jobs | "
            f"total=${batch.total_usd:.4f} | "
            f"→ pool=${batch.liquidity_usd:.4f} "
            f"creator=${batch.creator_usd:.4f} "
            f"dao=${batch.dao_usd:.4f}"
        )

        # Execute on-chain transfers
        batch = self._execute_transfers(batch)

        # Mark allocations settled
        alloc_ids = [a.allocation_id for a in pending]
        self.ledger.mark_settled(alloc_ids, batch.settlement_id)
        self.ledger.record_settlement(batch)

        if self.on_complete:
            self.on_complete(batch)

        log.info(
            f"Settlement complete: {batch.settlement_id[:8]} "
            f"status={batch.status}"
        )
        return batch

    def _execute_transfers(self, batch: SettlementBatch) -> SettlementBatch:
        """
        Execute the three on-chain transfers.

        Production: use web3.py to sign and broadcast ERC-20 transfer()
        calls from the custodial hot wallet to each destination.

        This stub logs the intended transfers and marks status as
        'broadcast' for testing. Replace with real web3 calls.
        """
        log.info(
            f"Transfer intent:\n"
            f"  ${batch.liquidity_usd:.4f} → "
            f"Liquidity pool  {self.destinations.liquidity_pool[:16]}...\n"
            f"  ${batch.creator_usd:.4f} → "
            f"Creator Share   {self.destinations.creator_share[:16]}...\n"
            f"  ${batch.dao_usd:.4f} → "
            f"DAO multisig    {self.destinations.dao_multisig[:16]}..."
        )

        # Stub tx hashes — replace with actual web3.py calls:
        # from web3 import Web3
        # w3 = Web3(Web3.HTTPProvider(RPC_URL))
        # token = w3.eth.contract(address=USDC_ADDRESS, abi=ERC20_ABI)
        # tx = token.functions.transfer(dest, amount).build_transaction({...})
        # signed = w3.eth.account.sign_transaction(tx, private_key=HOT_WALLET_KEY)
        # tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)

        import random
        batch.liquidity_tx_hash = '0x' + '%064x' % random.randint(0, 2**256-1)
        batch.creator_tx_hash   = '0x' + '%064x' % random.randint(0, 2**256-1)
        batch.dao_tx_hash       = '0x' + '%064x' % random.randint(0, 2**256-1)
        batch.status            = 'broadcast'   # → 'confirmed' after block inclusion

        return batch

    def preview(self) -> dict:
        """Show what the next settlement would look like without executing it."""
        totals = self.ledger.pending_totals()
        return {
            'pending_jobs'    : totals['jobs'],
            'pending_gross'   : totals['gross'],
            'would_send': {
                'liquidity_pool' : {
                    'address': self.destinations.liquidity_pool,
                    'amount' : totals['liquidity'],
                },
                'creator_share'  : {
                    'address': self.destinations.creator_share,
                    'amount' : totals['creator'],
                },
                'dao_multisig'   : {
                    'address': self.destinations.dao_multisig,
                    'amount' : totals['dao'],
                },
            },
            'ready'           : totals['gross'] >= MIN_SETTLEMENT_USD,
            'next_auto_settle': 'daily at midnight UTC',
        }
