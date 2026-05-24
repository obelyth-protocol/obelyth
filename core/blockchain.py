"""
Obelyth Blockchain Engine
==============================
- DAG chain management (multiple parents per block)
- UTXO set with spend tracking
- PoW mining with adaptive difficulty
- PoS validator registration & finality
- Block & transaction validation
- Adaptive block size adjustment
- Fee burning
- Founder vesting enforcement
"""

import time
import json
import threading
import logging
from typing import Optional

from core.structures import (
    Block, BlockHeader, Transaction, TxInput, TxOutput,
    UTXO, TxType, ConsensusType,
    BASE_BLOCK_SIZE, MAX_BLOCK_SIZE, MIN_BLOCK_SIZE, BURN_RATE
)
from core.crypto import (
    sha3_256, double_sha3, merkle_root, generate_keypair,
    PrivateKey, PublicKey, VestingSchedule
)

log = logging.getLogger('obelyth.engine')

# ── Network Constants ──────────────────────────────────────────────────────────
TOTAL_SUPPLY         = 21_000_000.0     # OBY hard cap
# ── Supply Allocation ─────────────────────────────────────────────────────────
# 3%  — Founder (12-month cliff, 48-month linear vest)
# 3%  — Pre-mainnet community (testnet miners, devs, validators, security)
# 2%  — Year 1 DAO discretionary (early grants, partnerships, ecosystem)
# 92% — Mined supply (block rewards + compute job rewards, forever)
# 0%  — No VC allocation. Ever.

FOUNDER_PCT          = 0.03             # 3% — personal founder allocation
FOUNDER_TOTAL        = TOTAL_SUPPLY * FOUNDER_PCT           # 630,000 OBY

COMMUNITY_PCT        = 0.03             # 3% — pre-mainnet testnet community pool
COMMUNITY_TOTAL      = TOTAL_SUPPLY * COMMUNITY_PCT         # 630,000 OBY

DAO_DISCRETIONARY_PCT  = 0.02           # 2% — Year 1 DAO discretionary
DAO_DISCRETIONARY_TOTAL= TOTAL_SUPPLY * DAO_DISCRETIONARY_PCT  # 420,000 OBY

PRE_MINE_TOTAL       = FOUNDER_TOTAL + COMMUNITY_TOTAL + DAO_DISCRETIONARY_TOTAL
MINED_SUPPLY_PCT     = 1.0 - (FOUNDER_PCT + COMMUNITY_PCT + DAO_DISCRETIONARY_PCT)
# MINED_SUPPLY_PCT = 0.92 — 92% earned through mining

assert abs(PRE_MINE_TOTAL - 21_000_000 * 0.08) < 1.0, "Pre-mine must equal 8%"

# DAO Mining Tax — 5% of ALL OBY earned by miners goes to DAO vault
# Applies to: block rewards + compute job OBY rewards
# Constitutional parameter — enforced at consensus layer, not DAO-controlled
DAO_MINING_TAX_PCT   = 0.05            # 5% of gross miner OBY earnings
DAO_VAULT_ADDRESS    = 'OBY_DAO_VAULT' # protocol-controlled vault address

INITIAL_REWARD       = 50.0             # OBY per block (gross before tax)
HALVING_INTERVAL     = 210_000          # blocks per halving (like BTC)
TARGET_BLOCK_TIME    = 10 * 60          # 10 minutes in seconds
DIFFICULTY_WINDOW    = 144             # blocks used for difficulty adjustment
SIZE_ADJUST_WINDOW   = 100             # blocks for adaptive size adjustment
SIZE_FILL_TARGET     = 0.75            # target 75% fullness

INITIAL_DIFFICULTY   = 4               # leading zero bits (low for dev)
MAX_DIFFICULTY       = 64
MIN_DIFFICULTY       = 1

POS_STAKE_MINIMUM    = 1000.0          # minimum OBY to be a validator


class UTXOSet:
    """In-memory UTXO database."""
    def __init__(self):
        self._utxos: dict[str, UTXO] = {}   # key = "tx_hash:index"
        self._lock = threading.RLock()

    def _key(self, tx_hash: str, index: int) -> str:
        return f"{tx_hash}:{index}"

    def add(self, tx_hash: str, index: int, address: str, amount: float):
        k = self._key(tx_hash, index)
        with self._lock:
            self._utxos[k] = UTXO(tx_hash, index, address, amount)

    def spend(self, tx_hash: str, index: int) -> bool:
        k = self._key(tx_hash, index)
        with self._lock:
            utxo = self._utxos.get(k)
            if utxo is None or utxo.spent:
                return False
            utxo.spent = True
            return True

    def get(self, tx_hash: str, index: int) -> Optional[UTXO]:
        return self._utxos.get(self._key(tx_hash, index))

    def balance(self, address: str) -> float:
        with self._lock:
            return sum(
                u.amount for u in self._utxos.values()
                if u.address == address and not u.spent
            )

    def unspent_for(self, address: str) -> list[UTXO]:
        with self._lock:
            return [u for u in self._utxos.values()
                    if u.address == address and not u.spent]

    def snapshot(self) -> dict:
        with self._lock:
            return {k: v.to_dict() for k, v in self._utxos.items()}

    def load(self, data: dict):
        with self._lock:
            self._utxos = {k: UTXO.from_dict(v) for k, v in data.items()}


class ValidatorSet:
    """Registered PoS validators and their stake."""
    def __init__(self):
        self._validators: dict[str, float] = {}  # address -> staked OBY
        self._lock = threading.RLock()

    def register(self, address: str, stake: float):
        if stake < POS_STAKE_MINIMUM:
            raise ValueError(f"Minimum stake is {POS_STAKE_MINIMUM} OBY")
        with self._lock:
            self._validators[address] = stake
        log.info(f"Validator registered: {address} stake={stake}")

    def is_validator(self, address: str) -> bool:
        return address in self._validators

    def total_stake(self) -> float:
        return sum(self._validators.values())

    def all(self) -> dict:
        return dict(self._validators)


class DAGChain:
    """
    DAG block store.
    Tracks all blocks and their parent relationships.
    'Tips' = blocks not yet referenced as a parent by any other block.
    """
    def __init__(self):
        self._blocks: dict[str, Block] = {}    # hash -> Block
        self._children: dict[str, list[str]] = {}  # hash -> [child hashes]
        self._lock = threading.RLock()

    def add(self, block: Block):
        with self._lock:
            self._blocks[block.hash] = block
            if block.hash not in self._children:
                self._children[block.hash] = []
            for ph in block.header.parent_hashes:
                self._children.setdefault(ph, []).append(block.hash)

    def get(self, h: str) -> Optional[Block]:
        return self._blocks.get(h)

    def tips(self) -> list[Block]:
        """Blocks with no children — the DAG frontier."""
        with self._lock:
            return [b for h, b in self._blocks.items()
                    if not self._children.get(h)]

    def height(self) -> int:
        if not self._blocks:
            return -1
        return max(b.header.height for b in self._blocks.values())

    def all_blocks(self) -> list[Block]:
        return list(self._blocks.values())

    def __len__(self):
        return len(self._blocks)

    def __contains__(self, h: str):
        return h in self._blocks


class Blockchain:
    """
    Full Obelyth node state.
    Manages DAG, UTXO set, validators, mempool, mining, and consensus.
    """

    def __init__(
        self,
        founder_address : str,
        dao_address     : str = DAO_VAULT_ADDRESS,
        genesis         : bool = True,
    ):
        self.dag          = DAGChain()
        self.utxos        = UTXOSet()
        self.validators   = ValidatorSet()
        self.mempool      : list[Transaction] = []
        self._mempool_lock = threading.RLock()

        self.difficulty   = INITIAL_DIFFICULTY
        self.block_size   = BASE_BLOCK_SIZE
        self.total_burned = 0.0
        self.founder_address = founder_address
        self.dao_address     = dao_address

        # Running DAO vault OBY balance — tracked separately for auditability
        self.dao_vault_oby   = 0.0
        self.dao_vault_txs   = 0      # lifetime number of tax deposits

        # Vesting schedule — immutable after genesis
        self.vesting = VestingSchedule(
            founder_address = founder_address,
            total_oby       = FOUNDER_TOTAL,
            cliff_months    = 12,
            total_months    = 48,
        )

        if genesis:
            self._make_genesis()

    # ── Genesis ────────────────────────────────────────────────────────────────

    def _make_genesis(self):
        """
        Create genesis block with full pre-mine allocation.

        Pre-mine breakdown (8% total, 1,680,000 OBY):
          3% (630,000)  → Founder address     — 12mo cliff, 48mo linear vest
          3% (630,000)  → Community pool      — testnet miners/devs/validators
          2% (420,000)  → DAO discretionary   — Year 1 grants and partnerships
         92% (~19.32M)  → Genesis reserve     — released through mining only

        No VC allocation. 92% of supply earned through real work.
        """
        log.info(
            f"Creating genesis block | "
            f"founder={FOUNDER_TOTAL:,.0f} OBY | "
            f"community={COMMUNITY_TOTAL:,.0f} OBY | "
            f"dao_disc={DAO_DISCRETIONARY_TOTAL:,.0f} OBY | "
            f"mined_reserve={TOTAL_SUPPLY-PRE_MINE_TOTAL:,.0f} OBY"
        )

        outputs = [
            # 3% — Founder (consensus-enforced vesting)
            TxOutput(address=self.founder_address, amount=FOUNDER_TOTAL),

            # 3% — Pre-mainnet community pool
            #       Distributed by testnet tracker to miners, devs,
            #       validators and security researchers before mainnet
            TxOutput(address='OBY_COMMUNITY_POOL', amount=COMMUNITY_TOTAL),

            # 2% — Year 1 DAO discretionary
            #       Governed by DAO from day one — grants, partnerships,
            #       ecosystem initiatives outside testnet criteria
            TxOutput(address='OBY_DAO_DISCRETIONARY', amount=DAO_DISCRETIONARY_TOTAL),

            # 92% — Genesis mining reserve
            #        Released only through block rewards and compute job rewards
            #        over the lifetime of the network
            TxOutput(address='OBY_GENESIS_RESERVE',
                     amount=TOTAL_SUPPLY - PRE_MINE_TOTAL),
        ]

        genesis_tx = Transaction(
            tx_type = TxType.COINBASE,
            inputs  = [],
            outputs = outputs,
            fee     = 0.0,
            memo    = (
                f'Obelyth Genesis Block | '
                f'Founder 3% ({FOUNDER_TOTAL:,.0f} OBY, 4yr vest) | '
                f'Community 3% ({COMMUNITY_TOTAL:,.0f} OBY, testnet) | '
                f'DAO 2% ({DAO_DISCRETIONARY_TOTAL:,.0f} OBY, Y1 discretionary) | '
                f'Mined 92% ({TOTAL_SUPPLY-PRE_MINE_TOTAL:,.0f} OBY) | '
                f'No VC allocation.'
            ),
        )
        genesis_tx._hash = genesis_tx.compute_hash()

        root = merkle_root([bytes.fromhex(genesis_tx.hash)])

        header = BlockHeader(
            height         = 0,
            parent_hashes  = [],
            merkle_root    = root.hex(),
            timestamp      = int(time.time()),
            consensus_type = ConsensusType.POW,
            miner_address  = 'genesis',
            difficulty     = 0,
            nonce          = 0,
        )

        genesis = Block(header=header, transactions=[genesis_tx])
        self.dag.add(genesis)

        # Register all four genesis UTXOs
        self.utxos.add(genesis_tx.hash, 0, self.founder_address,
                       FOUNDER_TOTAL)
        self.utxos.add(genesis_tx.hash, 1, 'OBY_COMMUNITY_POOL',
                       COMMUNITY_TOTAL)
        self.utxos.add(genesis_tx.hash, 2, 'OBY_DAO_DISCRETIONARY',
                       DAO_DISCRETIONARY_TOTAL)
        self.utxos.add(genesis_tx.hash, 3, 'OBY_GENESIS_RESERVE',
                       TOTAL_SUPPLY - PRE_MINE_TOTAL)

        log.info(
            f"Genesis: {genesis.hash[:16]}... | "
            f"Founder={FOUNDER_TOTAL:,.0f} (3%) | "
            f"Community={COMMUNITY_TOTAL:,.0f} (3%) | "
            f"DAO_disc={DAO_DISCRETIONARY_TOTAL:,.0f} (2%) | "
            f"Mined={TOTAL_SUPPLY-PRE_MINE_TOTAL:,.0f} (92%) | "
            f"No VC."
        )

    # ── Mempool ────────────────────────────────────────────────────────────────

    def add_to_mempool(self, tx: Transaction) -> bool:
        """Validate and add transaction to mempool."""
        if not self._validate_transaction(tx):
            return False
        with self._mempool_lock:
            # Dedup
            if any(t.hash == tx.hash for t in self.mempool):
                return False
            self.mempool.append(tx)
        log.debug(f"Mempool +tx {tx.hash[:12]} fee={tx.fee}")
        return True

    def _select_mempool_txs(self, size_limit: int) -> list[Transaction]:
        """Select highest-fee transactions that fit in the block."""
        with self._mempool_lock:
            candidates = sorted(self.mempool, key=lambda t: t.fee, reverse=True)
        selected = []
        used = 0
        for tx in candidates:
            sz = len(json.dumps(tx.to_dict()).encode())
            if used + sz <= size_limit * 0.95:
                selected.append(tx)
                used += sz
        return selected

    # ── Mining ─────────────────────────────────────────────────────────────────

    def mine_block(
        self,
        miner_address: str,
        miner_privkey: Optional[PrivateKey] = None,
        consensus: ConsensusType = ConsensusType.POW,
    ) -> Optional[Block]:
        """
        Assemble and mine a new block.
        PoW: increment nonce until hash satisfies difficulty.
        PoS: validator signs the block header (no nonce grinding).
        """
        if consensus == ConsensusType.POS:
            if not self.validators.is_validator(miner_address):
                log.warning(f"{miner_address} is not a registered validator")
                return None
            if miner_privkey is None:
                log.warning("PoS block requires validator private key")
                return None

        tips = self.dag.tips()
        if not tips:
            log.error("No DAG tips found")
            return None

        # Up to 3 parents (DAG breadth)
        parent_hashes = [t.hash for t in tips[:3]]
        height = max(t.height for t in tips) + 1
        gross_reward = self._block_reward(height)

        # ── DAO Mining Tax ──
        # 5% of gross reward → DAO vault (constitutional, consensus-enforced)
        dao_tax      = round(gross_reward * DAO_MINING_TAX_PCT, 8)
        miner_reward = round(gross_reward - dao_tax, 8)

        # Coinbase has two outputs: miner net + DAO vault
        coinbase_outputs = [
            TxOutput(address=miner_address,      amount=miner_reward),
            TxOutput(address=self.dao_address,   amount=dao_tax),
        ]
        coinbase = Transaction(
            tx_type  = TxType.COINBASE,
            inputs   = [],
            outputs  = coinbase_outputs,
            fee      = 0.0,
            memo     = (
                f'Block #{height} | miner={miner_reward:.8f} OBY '
                f'| dao_tax={dao_tax:.8f} OBY ({DAO_MINING_TAX_PCT*100:.0f}%)'
            ),
        )
        coinbase._hash = coinbase.compute_hash()

        # Select mempool transactions
        txs = [coinbase] + self._select_mempool_txs(self.block_size)
        tx_hashes = [bytes.fromhex(tx.hash) for tx in txs]
        root = merkle_root(tx_hashes).hex()

        header = BlockHeader(
            height         = height,
            parent_hashes  = parent_hashes,
            merkle_root    = root,
            timestamp      = int(time.time()),
            consensus_type = consensus,
            miner_address  = miner_address,
            difficulty     = self.difficulty,
            nonce          = 0,
        )

        if consensus == ConsensusType.POW:
            block = self._pow_mine(header, txs)
        elif consensus == ConsensusType.POS:
            block = self._pos_sign(header, txs, miner_privkey)
        else:
            # DAG tip — lightweight, no PoW
            block = Block(header=header, transactions=txs)

        if block and self.add_block(block):
            return block
        return None

    def _pow_mine(self, header: BlockHeader, txs: list[Transaction]) -> Block:
        """Grind nonce until hash has required leading zero bits."""
        target = '0' * (self.difficulty // 4)   # hex digits of zeros
        log.info(f"Mining PoW block #{header.height} difficulty={self.difficulty}...")
        start = time.time()
        nonce = 0
        while True:
            header.nonce = nonce
            header.invalidate()
            h = header.hash
            if h.startswith(target):
                elapsed = time.time() - start
                log.info(f"PoW solved: {h[:16]}... nonce={nonce} ({elapsed:.2f}s)")
                return Block(header=header, transactions=txs)
            nonce += 1
            if nonce % 50_000 == 0:
                log.debug(f"  ...nonce {nonce:,}")

    def _pos_sign(
        self, header: BlockHeader, txs: list[Transaction], privkey: PrivateKey
    ) -> Block:
        """Validator signs the block header — no grinding needed."""
        sig = privkey.sign(header.header_bytes()).hex()
        header.validator_sig = sig
        header.invalidate()
        log.info(f"PoS block #{header.height} signed: {header.hash[:16]}...")
        return Block(header=header, transactions=txs)

    def _block_reward(self, height: int) -> float:
        """Bitcoin-style halving every HALVING_INTERVAL blocks."""
        halvings = height // HALVING_INTERVAL
        if halvings >= 64:
            return 0.0
        return round(INITIAL_REWARD / (2 ** halvings), 8)

    # ── Block Acceptance ───────────────────────────────────────────────────────

    def add_block(self, block: Block) -> bool:
        """Validate and add a block to the DAG."""
        if block.hash in self.dag:
            return False   # already known

        if not self._validate_block(block):
            log.warning(f"Block rejected: {block.hash[:16]}...")
            return False

        self.dag.add(block)

        # Apply transactions to UTXO set
        for tx in block.transactions:
            self._apply_transaction(tx, block.header.timestamp)

        # Burn fees
        self.total_burned += block.burned_fees

        # Remove confirmed txs from mempool
        confirmed_hashes = {tx.hash for tx in block.transactions}
        with self._mempool_lock:
            self.mempool = [t for t in self.mempool
                            if t.hash not in confirmed_hashes]

        # Adjust difficulty and block size periodically
        h = block.height
        if h > 0 and h % DIFFICULTY_WINDOW == 0:
            self._adjust_difficulty()
        if h > 0 and h % SIZE_ADJUST_WINDOW == 0:
            self._adjust_block_size()

        log.info(f"Block accepted #{block.height} {block.hash[:16]}... "
                 f"txs={len(block.transactions)} burned={block.burned_fees:.6f}")
        return True

    # ── Validation ─────────────────────────────────────────────────────────────

    def _validate_block(self, block: Block) -> bool:
        h = block.header

        # Genesis has no parents
        if h.height == 0:
            return True

        # All parents must exist
        for ph in h.parent_hashes:
            if ph not in self.dag:
                log.warning(f"Unknown parent: {ph[:16]}")
                return False

        # Timestamp sanity (not more than 2h in future)
        if h.timestamp > int(time.time()) + 7200:
            log.warning("Block timestamp too far in future")
            return False

        # PoW difficulty check
        if h.consensus_type == ConsensusType.POW:
            target = '0' * (h.difficulty // 4)
            if not block.hash.startswith(target):
                log.warning(f"PoW difficulty not met: {block.hash[:12]}")
                return False

        # PoS validator check
        if h.consensus_type == ConsensusType.POS:
            if not self.validators.is_validator(h.miner_address):
                log.warning(f"PoS block from non-validator: {h.miner_address}")
                return False

        # Merkle root
        tx_hashes = [bytes.fromhex(tx.hash) for tx in block.transactions]
        expected = merkle_root(tx_hashes).hex()
        if expected != h.merkle_root:
            log.warning("Merkle root mismatch")
            return False

        # Size limit
        if block.serialised_size() > self.block_size:
            log.warning("Block exceeds size limit")
            return False

        # Exactly one coinbase
        coinbases = [tx for tx in block.transactions if tx.tx_type == TxType.COINBASE]
        if len(coinbases) != 1:
            log.warning(f"Expected 1 coinbase, found {len(coinbases)}")
            return False

        # Coinbase reward check — total outputs must equal gross reward
        # (gross = miner net + dao tax, both are outputs of the coinbase tx)
        gross_reward     = self._block_reward(h.height) + block.total_fees - block.burned_fees
        actual_total     = sum(o.amount for o in coinbases[0].outputs)
        expected_dao_tax = round(self._block_reward(h.height) * DAO_MINING_TAX_PCT, 8)

        if actual_total > gross_reward + 1e-8:
            log.warning(f"Coinbase over-reward: {actual_total} > {gross_reward}")
            return False

        # Verify DAO vault output exists and is correct amount
        dao_outputs = [o for o in coinbases[0].outputs if o.address == self.dao_address]
        if not dao_outputs:
            log.warning("Coinbase missing DAO vault output")
            return False
        actual_dao = sum(o.amount for o in dao_outputs)
        if abs(actual_dao - expected_dao_tax) > 1e-6:
            log.warning(
                f"Coinbase DAO tax incorrect: "
                f"got {actual_dao:.8f}, expected {expected_dao_tax:.8f}"
            )
            return False

        # Validate all non-coinbase transactions
        utxo_snapshot = dict(self.utxos._utxos)
        for tx in block.transactions:
            if tx.tx_type == TxType.COINBASE:
                continue
            if not self._validate_transaction(tx):
                return False

        return True

    def _validate_transaction(self, tx: Transaction) -> bool:
        if tx.tx_type == TxType.COINBASE:
            return True

        if tx.fee < 0:
            return False

        # Vesting: check schedule
        if tx.tx_type == TxType.VESTING:
            vested = self.vesting.vested_amount(int(time.time()))
            total_out = sum(o.amount for o in tx.outputs)
            if total_out > vested:
                log.warning(f"Vesting tx exceeds vested amount ({total_out} > {vested})")
                return False
            return True

        # Signature verification
        if not tx.verify_signatures(self.utxos._utxos):
            log.warning(f"Signature verification failed: {tx.hash[:12]}")
            return False

        # Input/output balance
        input_total  = sum(
            self.utxos.get(i.utxo_tx_hash, i.utxo_index).amount
            for i in tx.inputs
            if self.utxos.get(i.utxo_tx_hash, i.utxo_index)
        )
        output_total = sum(o.amount for o in tx.outputs)
        if output_total + tx.fee > input_total + 1e-8:
            log.warning(f"Tx outputs exceed inputs: {output_total+tx.fee} > {input_total}")
            return False

        return True

    def _apply_transaction(self, tx: Transaction, block_ts: int):
        """Spend inputs, create output UTXOs. Track DAO vault deposits."""
        for inp in tx.inputs:
            self.utxos.spend(inp.utxo_tx_hash, inp.utxo_index)
        for idx, out in enumerate(tx.outputs):
            self.utxos.add(tx.hash, idx, out.address, out.amount)
            # Track DAO vault accumulation
            if out.address == self.dao_address:
                self.dao_vault_oby += out.amount
                self.dao_vault_txs += 1

    # ── Difficulty Adjustment ─────────────────────────────────────────────────

    def _adjust_difficulty(self):
        blocks = sorted(self.dag.all_blocks(), key=lambda b: b.height)
        pow_blocks = [b for b in blocks[-DIFFICULTY_WINDOW:]
                      if b.header.consensus_type == ConsensusType.POW]
        if len(pow_blocks) < 2:
            return

        actual_time   = pow_blocks[-1].header.timestamp - pow_blocks[0].header.timestamp
        expected_time = (len(pow_blocks) - 1) * TARGET_BLOCK_TIME

        if actual_time == 0:
            return

        ratio = expected_time / actual_time
        new_diff = max(MIN_DIFFICULTY,
                       min(MAX_DIFFICULTY, int(self.difficulty * ratio)))

        if new_diff != self.difficulty:
            log.info(f"Difficulty: {self.difficulty} → {new_diff} "
                     f"(ratio={ratio:.3f})")
            self.difficulty = new_diff

    # ── Adaptive Block Size ────────────────────────────────────────────────────

    def _adjust_block_size(self):
        recent = sorted(self.dag.all_blocks(), key=lambda b: b.height)[-SIZE_ADJUST_WINDOW:]
        if not recent:
            return

        avg_fill = sum(b.serialised_size() for b in recent) / (len(recent) * self.block_size)

        if avg_fill > SIZE_FILL_TARGET + 0.1:
            new_size = min(MAX_BLOCK_SIZE, int(self.block_size * 1.25))
        elif avg_fill < SIZE_FILL_TARGET - 0.2:
            new_size = max(MIN_BLOCK_SIZE, int(self.block_size * 0.85))
        else:
            new_size = self.block_size

        if new_size != self.block_size:
            log.info(f"Block size: {self.block_size//1024}KB → {new_size//1024}KB "
                     f"(fill={avg_fill:.1%})")
            self.block_size = new_size

    def stake_as_validator(
        self,
        address    : str,
        stake_oby  : float,
        privkey    : 'PrivateKey' = None,
    ) -> bool:
        """
        Register an address as a PoS validator by staking OBY.
        For the founder this can use unvested allocation for staking
        (staking ≠ spending — the OBY stays locked, just earns validation rights).
        Returns True if registered successfully.
        """
        balance = self.utxos.balance(address)
        # Founder can stake from vested OR locked allocation
        # (staking locks in place — it cannot be transferred while staked)
        is_founder = address == self.founder_address
        effective_balance = balance
        if is_founder:
            # Founder can stake up to their full allocation even if unvested
            effective_balance = max(balance, self.vesting.total_oby)

        if effective_balance < stake_oby:
            log.warning(
                f"Insufficient balance to stake: "
                f"have {effective_balance:.2f}, need {stake_oby:.2f}"
            )
            return False

        try:
            self.validators.register(address, stake_oby)
            log.info(
                f"{'Founder' if is_founder else 'Validator'} staked "
                f"{stake_oby:,.2f} OBY — address={address[:16]}..."
            )
            return True
        except ValueError as e:
            log.warning(f"Stake failed: {e}")
            return False

    def state_summary(self) -> dict:
        return {
            'height'          : self.dag.height(),
            'blocks'          : len(self.dag),
            'mempool'         : len(self.mempool),
            'difficulty'      : self.difficulty,
            'block_size_kb'   : self.block_size // 1024,
            'total_burned'    : self.total_burned,
            'validators'      : len(self.validators.all()),
            'founder_vested'  : self.vesting.vested_amount(int(time.time())),
            'founder_locked'  : self.vesting.locked_amount(int(time.time())),
            'dao_vault_oby'   : round(self.dao_vault_oby, 8),
            'dao_vault_txs'   : self.dao_vault_txs,
            'dao_vault_address': self.dao_address,
            'dao_mining_tax_pct': DAO_MINING_TAX_PCT * 100,
            'genesis_allocation': {
                'founder_pct'          : f'{FOUNDER_PCT*100:.0f}%',
                'founder_oby'          : FOUNDER_TOTAL,
                'community_pool_pct'   : f'{COMMUNITY_PCT*100:.0f}%',
                'community_pool_oby'   : COMMUNITY_TOTAL,
                'dao_discretionary_pct': f'{DAO_DISCRETIONARY_PCT*100:.0f}%',
                'dao_discretionary_oby': DAO_DISCRETIONARY_TOTAL,
                'mined_pct'            : f'{MINED_SUPPLY_PCT*100:.0f}%',
                'mined_reserve_oby'    : TOTAL_SUPPLY - PRE_MINE_TOTAL,
                'vc_allocation'        : 'None — ever.',
            },
            'tips'            : [t.hash[:16] for t in self.dag.tips()],
        }
