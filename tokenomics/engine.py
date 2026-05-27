"""
Obelyth Tokenomics Engine v4
=================================

MULTI-STABLECOIN RESERVE:
  Accepts : USDC, DAI, USDT, EURC
  Basket target weights:
    40% USDC  — most liquid, US-regulated
    35% DAI   — decentralized, censorship-resistant
    15% USDT  — highest global liquidity
    10% EURC  — Euro-denominated hedge vs USD volatility

  All incoming stablecoins are normalised to USD using oracle rates.
  Reserve rebalances toward target weights passively over time.
  Any single stablecoin depeg only affects its share of the basket.

FEE SPLIT (accepts any supported stablecoin):
  90% → Liquidity Reserve  (diversified basket — hard locked)
   5% → Creator Share      (constitutional — not DAO-controlled)
   5% → DAO Multisig       (governance-controlled)

AMM:
  - Denominated in basket-USD (weighted average of all stablecoins)
  - Users buy/sell OBY with any supported stablecoin
  - AMM converts incoming stablecoin to basket-USD internally
  - constant-product: basket_usd_reserve × oby_reserve = k

BURN: OFF by default. DAO vote required to activate.
DAO : Founder multisig → on-chain governance at Month 12.
"""

import time
import json
import threading
import logging
from dataclasses import dataclass, field, asdict
from typing      import Optional
from enum        import Enum

log = logging.getLogger('obelyth.tokenomics')


# ── Supply ─────────────────────────────────────────────────────────────────────
TOTAL_OBY_SUPPLY     = 21_000_000.0
FOUNDER_OBY          = TOTAL_OBY_SUPPLY * 0.03

# ── Fee Split (constitutional) ─────────────────────────────────────────────────
FEE_TO_LIQUIDITY     = 0.90
FEE_TO_CREATOR       = 0.05
FEE_TO_DAO           = 0.05
assert abs(FEE_TO_LIQUIDITY + FEE_TO_CREATOR + FEE_TO_DAO - 1.0) < 1e-9

# ── AMM ────────────────────────────────────────────────────────────────────────
AMM_FEE_PCT          = 0.003
MIN_LIQUIDITY        = 1.0

# ── Block Rewards ──────────────────────────────────────────────────────────────
INITIAL_BLOCK_REWARD = 50.0
HALVING_INTERVAL     = 210_000
BOOTSTRAP_BLOCKS     = 52_560
BOOTSTRAP_BONUS      = 2.0
UPTIME_BONUS_OBY     = 0.5

# ── DAO Mining Tax ─────────────────────────────────────────────────────────────
# 5% of ALL OBY earned by miners (block rewards + compute job rewards)
# Constitutional — enforced at consensus layer, not DAO-controlled
# Sits in DAO vault as OBY; governance decides deployment
DAO_MINING_TAX_PCT   = 0.05

# ── Compute Pricing ────────────────────────────────────────────────────────────
BASE_GPU_HOUR_USD    = 0.40
MIN_JOB_USD          = 0.10

# ── Tier multipliers (Phase 4.2) ───────────────────────────────────────────────
# Standard tier: 1 miner, optimistic with challenges. Baseline cost.
# Redundant tier: 3 miners independently, majority consensus. 3x cost reflects
# the actual 3x compute. DAO can tune via on-chain governance once Phase 2 of
# governance kicks in (Year 2). Locked at 3.0 for testnet.
TIER_MULTIPLIER_STANDARD  = 1.0
TIER_MULTIPLIER_REDUNDANT = 3.0
REDUNDANT_MINER_COUNT     = 3      # how many miners run a redundant job
REDUNDANT_TIMEOUT_S       = 600    # 10 min from assignment to finalize


# ── Stablecoin Registry ────────────────────────────────────────────────────────

class Stablecoin(str, Enum):
    USDC = 'USDC'
    DAI  = 'DAI'
    USDT = 'USDT'
    EURC = 'EURC'

# Target basket weights — must sum to 1.0
BASKET_TARGETS = {
    Stablecoin.USDC: 0.40,
    Stablecoin.DAI : 0.35,
    Stablecoin.USDT: 0.15,
    Stablecoin.EURC: 0.10,
}
assert abs(sum(BASKET_TARGETS.values()) - 1.0) < 1e-9

# Deposit addresses per stablecoin per network (custodial at soft launch)
DEPOSIT_ADDRESSES = {
    Stablecoin.USDC: {
        'ethereum': '0x742d35Cc6634C0532925a3b8D4C9B5dF8b4C0532',
        'polygon' : '0x8ba1f109551bD432803012645Ac136ddd64DBA72',
        'base'    : '0x953d21d517f5d1c21b8d2a7e4e2b9cb2f4b8c3a1',
        'arbitrum': '0x6B175474E89094C44Da98b954EedeAC495271d0F',
    },
    Stablecoin.DAI: {
        'ethereum': '0x5d3a536E4D6DbD6114cc1Ead35777bAB948E3643',
        'polygon' : '0x27F8D03b3a2196956ED754baDc28D73be8830A6e',
        'base'    : '0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb',
        'arbitrum': '0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1',
    },
    Stablecoin.USDT: {
        'ethereum': '0xdAC17F958D2ee523a2206206994597C13D831ec7',
        'polygon' : '0xc2132D05D31c914a87C6611C10748AEb04B58e8F',
        'base'    : '0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2',
        'arbitrum': '0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9',
    },
    Stablecoin.EURC: {
        'ethereum': '0x1aBaEA1f7C830bD89Acc67eC4af516284b1bC33c',
        'polygon' : '0x0000000000000000000000000000000000000000',  # not yet
        'base'    : '0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42',
        'arbitrum': '0x0000000000000000000000000000000000000000',  # not yet
    },
}

# Risk profiles for user communication
STABLECOIN_RISK = {
    Stablecoin.USDC: {
        'issuer'    : 'Circle (US)',
        'backing'   : 'US Treasuries + bank cash',
        'risk'      : 'US banking system exposure, freeze function exists',
        'censorship': 'Centralized — addresses can be frozen',
        'rating'    : 'Medium',
    },
    Stablecoin.DAI: {
        'issuer'    : 'MakerDAO (decentralized)',
        'backing'   : 'Crypto collateral + RWA (partially USDC-backed)',
        'risk'      : 'Smart contract risk, inherits some USDC exposure',
        'censorship': 'Decentralized — no freeze function',
        'rating'    : 'Medium-Low',
    },
    Stablecoin.USDT: {
        'issuer'    : 'Tether (BVI)',
        'backing'   : 'Cash, T-bills, other (less transparent)',
        'risk'      : 'Reserve transparency concerns, regulatory risk',
        'censorship': 'Centralized — addresses can be frozen',
        'rating'    : 'Medium-High',
    },
    Stablecoin.EURC: {
        'issuer'    : 'Circle (EU/US)',
        'backing'   : 'Euro bank deposits',
        'risk'      : 'Euro currency exposure, low liquidity',
        'censorship': 'Centralized — addresses can be frozen',
        'rating'    : 'Low-Medium',
    },
}


# ── Data Classes ───────────────────────────────────────────────────────────────

@dataclass
class StablecoinBalance:
    """Track balances of each stablecoin separately for full audit trail."""
    usdc : float = 0.0
    dai  : float = 0.0
    usdt : float = 0.0
    eurc : float = 0.0

    def get(self, coin: Stablecoin) -> float:
        return getattr(self, coin.value.lower(), 0.0)

    def add(self, coin: Stablecoin, amount: float):
        key = coin.value.lower()
        setattr(self, key, getattr(self, key) + amount)

    def subtract(self, coin: Stablecoin, amount: float) -> bool:
        key = coin.value.lower()
        current = getattr(self, key)
        if current < amount:
            return False
        setattr(self, key, current - amount)
        return True

    def total_usd(self, rates: dict) -> float:
        """Total USD value using current oracle rates."""
        return sum(
            self.get(c) * rates.get(c, 1.0)
            for c in Stablecoin
        )

    def basket_weights(self, rates: dict) -> dict:
        """Current actual weights vs targets."""
        total = self.total_usd(rates)
        if total <= 0:
            return {c: 0.0 for c in Stablecoin}
        return {
            c: (self.get(c) * rates.get(c, 1.0)) / total
            for c in Stablecoin
        }

    def most_overweight(self, rates: dict) -> Optional[Stablecoin]:
        """Return the stablecoin most over its target weight — used for payouts."""
        weights = self.basket_weights(rates)
        excess  = {c: weights[c] - BASKET_TARGETS[c] for c in Stablecoin}
        best = max(excess, key=lambda c: excess[c])
        return best if excess[best] > 0 else list(Stablecoin)[0]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OracleRates:
    """
    USD exchange rates for each stablecoin.
    In production: updated every 5 minutes from Chainlink or Pyth.
    At soft launch: manually updated or hardcoded near 1.0.
    EURC tracks EUR/USD rate.
    """
    usdc : float = 1.000
    dai  : float = 1.000
    usdt : float = 1.000
    eurc : float = 1.085   # approximate EUR/USD

    def get(self, coin: Stablecoin) -> float:
        return getattr(self, coin.value.lower(), 1.0)

    def update(self, coin: Stablecoin, rate: float):
        setattr(self, coin.value.lower(), max(0.01, rate))
        log.info(f"Oracle updated: {coin.value} = ${rate:.4f}")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MultiAssetAMMPool:
    """
    Constant-product AMM denominated in basket-USD.
    Accepts any supported stablecoin — converts to USD equivalent internally.
    basket_usd_reserve × oby_reserve = k
    """
    basket_usd_reserve  : float = 0.0    # USD-equivalent value of all stablecoins in pool
    oby_reserve         : float = 0.0
    stablecoin_balances : StablecoinBalance = field(default_factory=StablecoinBalance)
    total_swaps         : int   = 0
    total_usd_in        : float = 0.0
    total_oby_sold      : float = 0.0
    total_oby_bought    : float = 0.0

    @property
    def k(self) -> float:
        return self.basket_usd_reserve * self.oby_reserve

    @property
    def spot_price_usd(self) -> float:
        if self.oby_reserve <= 0:
            return 0.0
        return self.basket_usd_reserve / self.oby_reserve

    def quote_buy_with_stable(
        self, coin: Stablecoin, amount: float, rates: OracleRates
    ) -> float:
        """How much OBY for `amount` of stablecoin `coin`."""
        usd_in         = amount * rates.get(coin)
        usd_after_fee  = usd_in * (1 - AMM_FEE_PCT)
        new_usd        = self.basket_usd_reserve + usd_after_fee
        if new_usd <= 0:
            return 0.0
        new_oby        = self.k / new_usd
        return round(max(0, self.oby_reserve - new_oby), 8)

    def quote_sell_oby(self, oby_amount: float, coin: Stablecoin, rates: OracleRates) -> float:
        """How much of stablecoin `coin` for `oby_amount` OBY."""
        new_oby        = self.oby_reserve + oby_amount
        new_usd        = self.k / new_oby
        gross_usd      = self.basket_usd_reserve - new_usd
        net_usd        = gross_usd * (1 - AMM_FEE_PCT)
        return round(net_usd / rates.get(coin), 6)

    def execute_buy(
        self, coin: Stablecoin, amount: float, rates: OracleRates
    ) -> float:
        """User pays `amount` stablecoin, receives OBY. Returns OBY out."""
        if self.basket_usd_reserve < MIN_LIQUIDITY:
            raise ValueError("Pool not seeded yet")
        oby_out       = self.quote_buy_with_stable(coin, amount, rates)
        usd_in        = amount * rates.get(coin) * (1 - AMM_FEE_PCT)
        self.basket_usd_reserve   += usd_in
        self.stablecoin_balances.add(coin, amount * (1 - AMM_FEE_PCT))
        self.oby_reserve          -= oby_out
        self.total_swaps          += 1
        self.total_oby_bought     += oby_out
        self.total_usd_in         += usd_in
        return oby_out

    def execute_sell(
        self, oby_in: float, coin: Stablecoin, rates: OracleRates
    ) -> float:
        """User pays OBY, receives stablecoin. Returns stablecoin out."""
        if self.basket_usd_reserve < MIN_LIQUIDITY:
            raise ValueError("Pool not seeded yet")
        stable_out     = self.quote_sell_oby(oby_in, coin, rates)
        usd_out        = stable_out * rates.get(coin)
        # Pay out from most overweight stablecoin if requested coin is short
        available = self.stablecoin_balances.get(coin)
        if available < stable_out:
            # Fall back to most overweight stablecoin
            coin       = self.stablecoin_balances.most_overweight(rates.__dict__)
            stable_out = self.quote_sell_oby(oby_in, coin, rates)
            usd_out    = stable_out * rates.get(coin)
        self.oby_reserve                  += oby_in
        self.basket_usd_reserve           -= usd_out
        self.stablecoin_balances.subtract(coin, stable_out)
        self.total_swaps                  += 1
        self.total_oby_sold               += oby_in
        return stable_out

    def add_liquidity(
        self,
        coin      : Stablecoin,
        amount    : float,
        rates     : OracleRates,
        oby_amount: float = 0.0,
    ):
        """Add stablecoin liquidity to pool."""
        usd_value = amount * rates.get(coin)
        if self.basket_usd_reserve < MIN_LIQUIDITY:
            if oby_amount <= 0:
                raise ValueError("Must provide OBY to seed pool")
            self.basket_usd_reserve = usd_value
            self.oby_reserve        = oby_amount
            self.stablecoin_balances.add(coin, amount)
            self.total_usd_in      += usd_value
            log.info(
                f"Pool seeded: ${usd_value:,.2f} USD ({amount:,.2f} {coin.value}) "
                f"+ {oby_amount:,.2f} OBY → ${self.spot_price_usd:.4f}/OBY"
            )
        else:
            ratio      = usd_value / self.basket_usd_reserve
            oby_to_add = self.oby_reserve * ratio if oby_amount <= 0 else oby_amount
            self.basket_usd_reserve        += usd_value
            self.oby_reserve               += oby_to_add
            self.stablecoin_balances.add(coin, amount)
            self.total_usd_in              += usd_value

    def basket_composition(self, rates: OracleRates) -> dict:
        """Current vs target weights for each stablecoin."""
        weights = self.stablecoin_balances.basket_weights(
            {c: rates.get(c) for c in Stablecoin}
        )
        return {
            c.value: {
                'balance'       : round(self.stablecoin_balances.get(c), 4),
                'usd_value'     : round(self.stablecoin_balances.get(c) * rates.get(c), 4),
                'actual_weight' : round(weights[c] * 100, 2),
                'target_weight' : round(BASKET_TARGETS[c] * 100, 2),
                'deviation'     : round((weights[c] - BASKET_TARGETS[c]) * 100, 2),
            }
            for c in Stablecoin
        }

    def to_dict(self) -> dict:
        return {
            'basket_usd_reserve' : round(self.basket_usd_reserve, 4),
            'oby_reserve'        : round(self.oby_reserve, 4),
            'spot_price_usd'     : round(self.spot_price_usd, 6),
            'k'                  : round(self.k, 2),
            'total_swaps'        : self.total_swaps,
            'total_usd_in'       : round(self.total_usd_in, 4),
            'total_oby_sold'     : round(self.total_oby_sold, 4),
            'total_oby_bought'   : round(self.total_oby_bought, 4),
            'stablecoins'        : self.stablecoin_balances.to_dict(),
        }


@dataclass
class FeeReceipt:
    job_id           : str
    gross_usd        : float         # USD equivalent
    stablecoin       : str           # which coin was paid
    stablecoin_amount: float         # actual amount paid
    liquidity_usd    : float         # 90% → AMM
    creator_usd      : float         # 5%  → creator share
    dao_usd          : float         # 5%  → DAO
    oby_price_at_fee : float
    timestamp        : int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CreatorShare:
    """
    Permanent protocol fee — 5% of all fees.
    Constitutional — not DAO-controlled. Holds multiple stablecoins.
    """
    balances          : StablecoinBalance = field(default_factory=StablecoinBalance)
    total_received_usd: float = 0.0
    total_disbursed_usd:float = 0.0
    last_disbursement : int   = 0
    address           : str   = ''

    def to_dict(self) -> dict:
        return {
            'balances'           : self.balances.to_dict(),
            'total_received_usd' : round(self.total_received_usd, 4),
            'total_disbursed_usd': round(self.total_disbursed_usd, 4),
            'last_disbursement'  : self.last_disbursement,
            'address'            : self.address,
        }


@dataclass
class DAOFund:
    balances          : StablecoinBalance = field(default_factory=StablecoinBalance)
    total_received_usd: float = 0.0
    oby_burned        : float = 0.0
    burn_enabled      : bool  = False
    burn_pct_of_dao   : float = 0.0
    multisig_address  : str   = ''
    is_multisig       : bool  = True
    # DAO Vault — OBY from 5% mining tax (block rewards + compute job rewards)
    vault_oby         : float = 0.0    # total OBY accumulated in vault
    vault_deposits    : int   = 0      # number of tax deposits received

    def to_dict(self) -> dict:
        return {
            'balances'           : self.balances.to_dict(),
            'total_received_usd' : round(self.total_received_usd, 4),
            'oby_burned'         : round(self.oby_burned, 6),
            'burn_enabled'       : self.burn_enabled,
            'burn_pct_of_dao'    : self.burn_pct_of_dao,
            'multisig_address'   : self.multisig_address,
            'is_multisig'        : self.is_multisig,
            'vault_oby'          : round(self.vault_oby, 8),
            'vault_deposits'     : self.vault_deposits,
        }


@dataclass
class ComputeJob:
    job_id         : str
    developer_addr : str
    job_type       : str
    model_id       : str
    gpu_hours      : float
    stablecoin     : str
    stable_paid    : float
    usd_paid       : float
    oby_to_miner   : float = 0.0
    miner_addr     : str   = ''
    status         : str   = 'pending'
    created_at     : int   = field(default_factory=lambda: int(time.time()))
    completed_at   : int   = 0
    result_cid     : str   = ''
    result_hash    : str   = ''
    # Determinism envelope — required for the verification engine to challenge.
    # These are SHA-256 hex strings (or 'sha256:<hex>' for container_digest)
    # and a uint64 seed propagated to the inference framework.
    model_hash         : str = ''
    container_digest   : str = ''
    seed               : int = 0
    input_payload_hash : str = ''
    input_schema_hash  : str = ''
    # Raw inputs needed by the challenger to reproduce the work.
    # Privacy note: standard tier exposes inputs to the assigned miner AND
    # any challenger. Higher tiers (pipeline, TEE) don't expose to challengers
    # via this field — they verify differently. Stored as JSON-serializable
    # list/dict, default empty.
    inputs             : list  = field(default_factory=list)
    task               : str   = 'text-generation'
    params             : dict  = field(default_factory=dict)
    # Refund accounting when this job's miner was faulted
    refund_oby         : float = 0.0
    refund_settled     : bool  = False   # True once swept to dev's stablecoin balance
    refund_stable_paid : float = 0.0     # actual stablecoin amount credited
    refund_settled_at  : int   = 0
    # ── Tier (Phase 4.2) ──
    # 'standard'  : 1 miner, optimistic with random challenges (default)
    # 'redundant' : 3 miners independently, majority consensus, outliers slashed
    # Future tiers: 'pipeline' (layers split geographically), 'tee_attested'
    # (hardware attestation). Each tier has its own settlement code path; the
    # tier field is the dispatch key.
    tier               : str   = 'standard'
    # For redundant tier: addresses of all assigned miners. For standard tier
    # this stays empty and miner_addr is the single assignee.
    assigned_miners    : list  = field(default_factory=list)
    # For redundant tier: maps miner_addr -> {result_hash, result_cid, submitted_at}
    # As each of the N miners submits, the consensus engine accumulates here.
    result_submissions : dict  = field(default_factory=dict)
    # For redundant tier: which miners won majority (credited) and which were
    # outliers (slashed). Empty before finalize() runs.
    consensus_winners  : list  = field(default_factory=list)
    consensus_outliers : list  = field(default_factory=list)
    # For redundant tier: deadline (unix ts) after which we finalize whatever
    # results are in. 10 min from assignment by default; longer than typical
    # inference, short enough not to block the dev forever.
    consensus_deadline : int   = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MinerProfile:
    address        : str
    gpu_model      : str
    gpu_count      : int
    vram_gb        : int
    bandwidth_gbps : float
    region         : str
    stake_oby      : float
    online_since   : int   = field(default_factory=lambda: int(time.time()))
    jobs_completed : int   = 0
    jobs_failed    : int   = 0
    uptime_hours   : float = 0.0
    oby_earned     : float = 0.0
    reputation     : float = 1.0
    # Verification engine state (escalating slash + ban tracking)
    offence_count       : int           = 0
    banned_until_block  : Optional[int] = None
    last_heartbeat      : int           = field(default_factory=lambda: int(time.time()))

    def is_banned(self, current_block: int) -> bool:
        return (
            self.banned_until_block is not None
            and current_block < self.banned_until_block
        )

    @property
    def score(self) -> float:
        return (
            self.reputation              * 0.40 +
            min(1.0, self.gpu_count / 8) * 0.30 +
            min(1.0, self.bandwidth_gbps / 25) * 0.20 +
            min(1.0, self.stake_oby / 10_000)  * 0.10
        )

    def to_dict(self) -> dict:
        return asdict(self)


# ── Engine ─────────────────────────────────────────────────────────────────────

class TokenomicsEngine:
    """
    Obelyth economic engine v4.
    Multi-stablecoin reserve basket with diversification against USD risk.
    Thread-safe. All state transitions logged.

    Owns:
      - Stablecoin AMM pool (deposits, swaps, fee splits)
      - Creator Share + DAO accounts
      - Miner profiles (stake, reputation, offence_count, ban state)
      - Job ledger (pending, assigned, done, faulted)
      - Verification engine for optimistic compute audits

    The verification engine is integrated via callbacks so its side-effects
    (slash, refund, ban) flow through this engine's state and accounting.
    """

    def __init__(
        self,
        creator_address  : str = '',
        dao_address      : str = '',
        genesis_timestamp: int = None,
        block_height_provider     : Optional[callable] = None,
        block_hash_provider       : Optional[callable] = None,
    ):
        self.genesis_ts  = genesis_timestamp or int(time.time())
        self.pool        = MultiAssetAMMPool()
        self.creator     = CreatorShare(address=creator_address)
        self.dao         = DAOFund(multisig_address=dao_address, is_multisig=True)
        self.rates       = OracleRates()
        self._jobs       : dict[str, ComputeJob]   = {}
        self._miners     : dict[str, MinerProfile] = {}
        self._receipts   : list[FeeReceipt]        = []
        self._oby_price  = 0.10
        self._lock       = threading.RLock()

        # Providers from the chain layer. Defaults are safe for unit tests but
        # the live node must pass these so verification has the canonical
        # block_hash and block_height for deterministic decisions.
        self._block_height = block_height_provider or (lambda: 0)
        self._block_hash   = block_hash_provider   or (lambda: b'\x00' * 32)

        # Verification engine — integrated, callbacks wired below
        from compute.verification import VerificationEngine
        self.verification = VerificationEngine(
            on_slash            = self._on_slash,
            on_refund           = self._on_refund,
            on_ban              = self._on_ban,
            on_pass             = self._on_pass,
            block_hash_provider = self._block_hash,
        )

        # Consensus engine — handles redundant tier. Shares the same
        # callback set as the verification engine so settlement logic
        # is unified across tiers.
        from compute.consensus import ConsensusEngine
        self.consensus = ConsensusEngine(
            on_pass   = self._on_pass,
            on_slash  = self._on_slash,
            on_refund = self._on_refund,
            on_ban    = self._on_ban,
        )

        log.info(
            f"TokenomicsEngine v4 | multi-stablecoin reserve\n"
            f"  Basket: USDC {BASKET_TARGETS[Stablecoin.USDC]*100:.0f}% / "
            f"DAI {BASKET_TARGETS[Stablecoin.DAI]*100:.0f}% / "
            f"USDT {BASKET_TARGETS[Stablecoin.USDT]*100:.0f}% / "
            f"EURC {BASKET_TARGETS[Stablecoin.EURC]*100:.0f}%\n"
            f"  Creator : {creator_address or 'unset'}\n"
            f"  DAO     : {dao_address or 'unset (multisig)'}\n"
            f"  Verification: integrated | Consensus (redundant tier): integrated"
        )

    # ── Verification engine callbacks ─────────────────────────────────────────
    #
    # The verification engine calls these when a challenge resolves. We mutate
    # miner state and the job ledger atomically. The actual OBY transfers
    # (slashed stake → developer refund or burn) are reflected in the
    # MinerProfile and ComputeJob records here. When the chain layer is wired
    # in Phase 2, these will also produce on-chain transactions.

    def _on_slash(
        self,
        miner_addr   : str,
        job_id       : str,
        slash_pct    : float,
        slashed_oby  : float,
        offence_count: int,
    ):
        with self._lock:
            m = self._miners.get(miner_addr)
            if not m:
                log.warning(f"on_slash: unknown miner {miner_addr[:16]}")
                return
            m.stake_oby     = max(0.0, m.stake_oby - slashed_oby)
            m.reputation    = 0.0       # constitutional: reset on any fault
            m.jobs_failed  += 1
            m.offence_count = offence_count
            j = self._jobs.get(job_id)
            # For standard tier, a slash means the job itself faulted.
            # For redundant tier, the consensus engine has already set the
            # correct job status — slashing an outlier on a 2-of-3 majority
            # job does NOT make the whole job faulted (majority still wins).
            if j is not None and j.tier == 'standard':
                j.status = 'faulted'
        log.warning(
            f"on_slash: {miner_addr[:16]} slashed {slashed_oby:.4f} OBY "
            f"({slash_pct*100:.0f}%, offence #{offence_count})"
        )

    def _on_refund(
        self,
        developer_addr: str,
        job_id        : str,
        refund_oby    : float,
    ):
        with self._lock:
            j = self._jobs.get(job_id)
            if j is not None:
                j.refund_oby = refund_oby
            # NOTE: The developer paid in stablecoin, so a refund in OBY
            # creates a currency mismatch. For Phase 1 we record the refund
            # against the job; Phase 2 will route it through the AMM to
            # credit the developer's stablecoin balance via the accounts
            # registry. Until then this is an accounting entry only.
        log.info(
            f"on_refund: dev {developer_addr[:16]} owed {refund_oby:.4f} OBY "
            f"from fault on job {job_id}"
        )

    def _on_ban(self, miner_addr: str, until_block: int):
        with self._lock:
            m = self._miners.get(miner_addr)
            if not m:
                log.warning(f"on_ban: unknown miner {miner_addr[:16]}")
                return
            m.banned_until_block = until_block
        log.warning(
            f"on_ban: {miner_addr[:16]} banned until block {until_block}"
        )

    def _on_pass(self, miner_addr: str, job_id: str, method: str):
        """
        Called by:
          - VerificationEngine on PASSED challenges or expired-without-dispute
            (standard tier, fires once per job at most)
          - ConsensusEngine on each redundant-tier winner (can fire multiple
            times per job — once per winner)

        Optimistic and ZK accepts in standard tier are NOT routed here — those
        credit synchronously in complete_job_with_verification() because the
        job transition happens in-line on submit.

        Idempotency strategy differs by method:
          - method='consensus' : per-(job, miner) idempotency. Check that
            this miner isn't already in result_submissions[miner]['credited'].
            Status is shared across N winners so we can't gate on it.
          - method in ('challenged', 'optimistic', 'zk') : per-job idempotency,
            so a slow expiry firing after a manual resolve doesn't double-credit.
        """
        with self._lock:
            j = self._jobs.get(job_id)
            if j is None:
                log.warning(f"on_pass: unknown job {job_id}")
                return
            m = self._miners.get(miner_addr)
            if m is None:
                log.warning(f"on_pass: unknown miner {miner_addr}")
                return

            if method == 'consensus':
                # Per-(job, miner) idempotency: mark the submission as credited
                sub = j.result_submissions.get(miner_addr)
                if sub is None:
                    log.warning(
                        f"on_pass(consensus): {miner_addr[:16]} has no "
                        f"submission for {job_id}"
                    )
                    return
                if sub.get('credited'):
                    return   # already credited
                sub['credited'] = True
                m.jobs_completed += 1
                m.oby_earned     += j.oby_to_miner
            else:
                # Standard tier: per-job idempotency
                if j.status == 'done':
                    return
                j.status       = 'done'
                j.completed_at = int(time.time())
                m.jobs_completed += 1
                m.oby_earned     += j.oby_to_miner

        log.info(
            f"on_pass: {miner_addr[:16]} credited {j.oby_to_miner:.4f} OBY "
            f"for job {job_id} ({method})"
        )

    # ── Oracle ─────────────────────────────────────────────────────────────────

    def update_rate(self, coin: Stablecoin, usd_rate: float):
        """Update oracle price for a stablecoin (e.g. EURC/USD)."""
        self.rates.update(coin, usd_rate)

    def update_oby_price(self, price: float):
        self._oby_price = max(1e-8, price)

    def _usd_to_oby(self, usd: float) -> float:
        return round(usd / self._oby_price, 8)

    def _stable_to_usd(self, coin: Stablecoin, amount: float) -> float:
        return amount * self.rates.get(coin)

    # ── Pool Seeding ───────────────────────────────────────────────────────────

    def seed_pool(
        self,
        coin      : Stablecoin,
        amount    : float,
        oby_amount: float,
    ):
        """
        Seed the AMM pool at launch.
        Example: seed_pool(Stablecoin.USDC, 100_000, 1_000_000)
        → initial price $0.10/OBY
        """
        with self._lock:
            self.pool.add_liquidity(coin, amount, self.rates, oby_amount)

    def seed_pool_multi(self, seeds: dict, oby_total: float):
        """
        Seed with multiple stablecoins at once.
        seeds = {Stablecoin.USDC: 40000, Stablecoin.DAI: 35000, ...}
        """
        total_usd = sum(
            amt * self.rates.get(c) for c, amt in seeds.items()
        )
        with self._lock:
            first = True
            for coin, amount in seeds.items():
                usd_share = (amount * self.rates.get(coin)) / total_usd
                oby_share = oby_total * usd_share
                if first:
                    self.pool.add_liquidity(coin, amount, self.rates, oby_share)
                    first = False
                else:
                    self.pool.add_liquidity(coin, amount, self.rates, oby_share)
        log.info(
            f"Pool seeded with {len(seeds)} stablecoins | "
            f"${total_usd:,.2f} USD | {oby_total:,.0f} OBY | "
            f"price=${self.pool.spot_price_usd:.4f}/OBY"
        )

    # ── Fee Processing ─────────────────────────────────────────────────────────

    def process_job_fee(
        self,
        job_id    : str,
        coin      : Stablecoin,
        amount    : float,
    ) -> FeeReceipt:
        """
        Route incoming stablecoin fee to the three buckets.
        90% → pool liquidity, 5% → creator, 5% → DAO.
        """
        usd_value = self._stable_to_usd(coin, amount)

        with self._lock:
            liq_usd     = usd_value * FEE_TO_LIQUIDITY
            creator_usd = usd_value * FEE_TO_CREATOR
            dao_usd     = usd_value * FEE_TO_DAO

            liq_stable     = amount * FEE_TO_LIQUIDITY
            creator_stable = amount * FEE_TO_CREATOR
            dao_stable     = amount * FEE_TO_DAO

            # 90% → deepen AMM pool
            if self.pool.basket_usd_reserve >= MIN_LIQUIDITY:
                self.pool.add_liquidity(coin, liq_stable, self.rates)
            else:
                self.pool.total_usd_in += liq_usd

            # 5% → creator share (holds original stablecoin)
            self.creator.balances.add(coin, creator_stable)
            self.creator.total_received_usd += creator_usd

            # 5% → DAO
            if self.dao.burn_enabled and self.dao.burn_pct_of_dao > 0:
                burn_stable = dao_stable * self.dao.burn_pct_of_dao
                grant_stable = dao_stable - burn_stable
                if self.pool.basket_usd_reserve >= MIN_LIQUIDITY and burn_stable > 0:
                    oby_burned = self.pool.execute_buy(coin, burn_stable, self.rates)
                    self.dao.oby_burned += oby_burned
                self.dao.balances.add(coin, grant_stable)
            else:
                self.dao.balances.add(coin, dao_stable)
            self.dao.total_received_usd += dao_usd

            receipt = FeeReceipt(
                job_id            = job_id,
                gross_usd         = usd_value,
                stablecoin        = coin.value,
                stablecoin_amount = amount,
                liquidity_usd     = liq_usd,
                creator_usd       = creator_usd,
                dao_usd           = dao_usd,
                oby_price_at_fee  = self.pool.spot_price_usd,
            )
            self._receipts.append(receipt)

        log.info(
            f"Fee {job_id} | {amount:.4f} {coin.value} (${usd_value:.4f}) | "
            f"pool +${liq_usd:.4f} | creator +${creator_usd:.4f} | "
            f"dao +${dao_usd:.4f}"
        )
        return receipt

    # ── Swaps ──────────────────────────────────────────────────────────────────

    def buy_oby(
        self,
        coin      : Stablecoin,
        amount    : float,
        user_addr : str = '',
    ) -> dict:
        """User buys OBY with any supported stablecoin."""
        with self._lock:
            if self.pool.basket_usd_reserve < MIN_LIQUIDITY:
                raise ValueError("Pool not seeded yet")
            price_before = self.pool.spot_price_usd
            oby_out      = self.pool.execute_buy(coin, amount, self.rates)
            price_after  = self.pool.spot_price_usd
            impact       = (price_after - price_before) / price_before * 100

        log.info(
            f"BUY {amount:.4f} {coin.value} → {oby_out:.4f} OBY "
            f"impact={impact:+.3f}% {user_addr[:16]}"
        )
        return {
            'stablecoin'      : coin.value,
            'stable_in'       : amount,
            'usd_in'          : round(self._stable_to_usd(coin, amount), 4),
            'oby_out'         : oby_out,
            'price_before'    : price_before,
            'price_after'     : price_after,
            'price_impact_pct': round(impact, 4),
            'effective_price' : round(self._stable_to_usd(coin, amount) / oby_out, 6)
                                if oby_out else 0,
        }

    def sell_oby(
        self,
        oby_in    : float,
        coin      : Stablecoin,
        user_addr : str = '',
    ) -> dict:
        """User sells OBY for any supported stablecoin."""
        with self._lock:
            price_before = self.pool.spot_price_usd
            stable_out   = self.pool.execute_sell(oby_in, coin, self.rates)
            price_after  = self.pool.spot_price_usd
            impact       = (price_after - price_before) / price_before * 100

        return {
            'oby_in'          : oby_in,
            'stablecoin'      : coin.value,
            'stable_out'      : stable_out,
            'usd_out'         : round(self._stable_to_usd(coin, stable_out), 4),
            'price_before'    : price_before,
            'price_after'     : price_after,
            'price_impact_pct': round(impact, 4),
        }

    def quote_buy(self, coin: Stablecoin, amount: float) -> dict:
        with self._lock:
            oby_out = self.pool.quote_buy_with_stable(coin, amount, self.rates)
        return {
            'stable_in'   : amount,
            'coin'        : coin.value,
            'oby_out'     : oby_out,
            'spot_price'  : self.pool.spot_price_usd,
            'fee_stable'  : round(amount * AMM_FEE_PCT, 6),
        }

    def quote_sell(self, oby_in: float, coin: Stablecoin) -> dict:
        with self._lock:
            stable_out = self.pool.quote_sell_oby(oby_in, coin, self.rates)
        return {
            'oby_in'     : oby_in,
            'coin'       : coin.value,
            'stable_out' : stable_out,
            'usd_out'    : round(self._stable_to_usd(coin, stable_out), 4),
            'spot_price' : self.pool.spot_price_usd,
        }

    # ── Jobs ───────────────────────────────────────────────────────────────────

    def submit_job(
        self,
        developer_addr : str,
        job_type       : str,
        model_id       : str,
        coin           : Stablecoin,
        gpu_count      : int   = 1,
        duration_hr    : float = 1.0,
        stable_paid    : float = None,
    ) -> tuple['ComputeJob', FeeReceipt]:
        import uuid
        job_id  = str(uuid.uuid4())[:16]
        stable  = stable_paid or self.quote_job(
            job_type, model_id, coin, gpu_count, duration_hr
        )['stable_cost']
        receipt = self.process_job_fee(job_id, coin, stable)

        # Gross OBY reward valued at current spot price
        gross_oby    = self._usd_to_oby(receipt.liquidity_usd)
        # Apply 5% DAO mining tax on compute job OBY rewards
        dao_tax_oby  = round(gross_oby * DAO_MINING_TAX_PCT, 8)
        miner_oby    = round(gross_oby - dao_tax_oby, 8)

        job = ComputeJob(
            job_id         = job_id,
            developer_addr = developer_addr,
            job_type       = job_type,
            model_id       = model_id,
            gpu_hours      = gpu_count * duration_hr,
            stablecoin     = coin.value,
            stable_paid    = stable,
            usd_paid       = receipt.gross_usd,
            oby_to_miner   = miner_oby,
        )
        with self._lock:
            self._jobs[job_id]       = job
            self.dao.vault_oby      += dao_tax_oby
            self.dao.vault_deposits += 1
        log.info(
            f"Job {job_id} | miner gets {miner_oby:.4f} OBY | "
            f"dao vault +{dao_tax_oby:.4f} OBY (5% tax)"
        )
        return job, receipt

    def assign_job(self, job_id: str) -> Optional[str]:
        """
        Assign a pending job to a miner via stake-weighted random sampling
        (consensus-deterministic). Reputation is NOT used here — per the
        verification engine's constitutional design, reputation only gates
        challenge rate, not work distribution.

        For redundant-tier jobs, this dispatches to assign_redundant_job
        and returns the first assignee's address (the API layer uses
        assign_redundant_job directly when it needs the full list).
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != 'pending':
                return None

        if job.tier == 'redundant':
            picks = self.assign_redundant_job(job_id)
            return picks[0] if picks else None

        # Standard tier
        from compute.verification import assign_miner, NoEligibleMinersError
        with self._lock:
            current_block = self._block_height()
            pool = [
                {
                    'address'   : m.address,
                    'stake_oby' : m.stake_oby,
                    'is_banned' : m.is_banned(current_block),
                }
                for m in self._miners.values()
            ]
            try:
                chosen = assign_miner(
                    job_id     = job_id,
                    block_hash = self._block_hash(),
                    miners     = pool,
                )
            except NoEligibleMinersError:
                log.warning(f"assign_job: no eligible miners for {job_id}")
                return None
            job.miner_addr = chosen
            job.status     = 'assigned'
            return chosen

    def assign_redundant_job(self, job_id: str) -> list[str]:
        """
        Assign a redundant-tier job to N miners (REDUNDANT_MINER_COUNT).

        Sets job.assigned_miners and job.consensus_deadline, transitions
        job.status to 'assigned'. Returns the picked addresses in order.

        Returns [] if the job doesn't exist, is the wrong tier, or no
        N eligible miners are available.
        """
        from compute.verification import assign_miners_redundant, NoEligibleMinersError
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return []
            if job.tier != 'redundant':
                log.warning(
                    f"assign_redundant_job: {job_id} is tier={job.tier!r}"
                )
                return []
            if job.status != 'pending':
                # Already assigned — return the existing picks for idempotency
                return list(job.assigned_miners)

            current_block = self._block_height()
            pool = [
                {
                    'address'   : m.address,
                    'stake_oby' : m.stake_oby,
                    'is_banned' : m.is_banned(current_block),
                }
                for m in self._miners.values()
            ]
            try:
                picks = assign_miners_redundant(
                    job_id     = job_id,
                    block_hash = self._block_hash(),
                    miners     = pool,
                    n          = REDUNDANT_MINER_COUNT,
                )
            except NoEligibleMinersError as e:
                log.warning(
                    f"assign_redundant_job: {job_id} — {e} "
                    f"(redundant tier needs {REDUNDANT_MINER_COUNT}+ miners)"
                )
                return []

            job.assigned_miners    = picks
            job.miner_addr         = picks[0]   # convenience for status display
            job.status             = 'assigned'
            job.consensus_deadline = int(time.time()) + REDUNDANT_TIMEOUT_S
            log.info(
                f"Redundant job {job_id} assigned to {len(picks)} miners: "
                f"{[a[:12] for a in picks]} deadline=+{REDUNDANT_TIMEOUT_S}s"
            )
            return picks

    def submit_job_with_verification(
        self,
        developer_addr     : str,
        job_type           : str,
        model_id           : str,
        coin               : Stablecoin,
        model_hash         : str,
        container_digest   : str,
        seed               : int,
        input_payload_hash : str,
        input_schema_hash  : str,
        gpu_count          : int   = 1,
        duration_hr        : float = 1.0,
        stable_paid        : float = None,
        inputs             : list  = None,
        task               : str   = 'text-generation',
        params             : dict  = None,
        tier               : str   = 'standard',
    ) -> tuple['ComputeJob', FeeReceipt]:
        """
        Submit a job AND register it with the verification or consensus engine
        depending on tier.

        Standard tier: registers with VerificationEngine, optimistic with
        random challenges. oby_to_miner is the full payment.

        Redundant tier: registers with ConsensusEngine, three miners run the
        job in parallel and the majority wins. Total miner payment is 3x the
        standard cost (priced in at quote time); oby_to_miner stores the
        per-miner share, so the consensus engine doesn't need to know about
        the multiplier.

        Both paths still validate the determinism envelope and route fees
        through process_job_fee identically. The fee split (90/5/5) is
        applied to the total — DAO and Creator still get their cuts of the
        gross.
        """
        from compute.verification import JobSpec, validate_job_submission
        import uuid
        job_id = str(uuid.uuid4())[:16]

        stable = stable_paid or self.quote_job(
            job_type, model_id, coin, gpu_count, duration_hr, tier=tier,
        )['stable_cost']
        approx_payment_oby = self._usd_to_oby(stable * FEE_TO_LIQUIDITY)
        spec = JobSpec(
            job_id             = job_id,
            developer_addr     = developer_addr,
            model_hash         = model_hash,
            container_digest   = container_digest,
            seed               = seed,
            input_payload_hash = input_payload_hash,
            input_schema_hash  = input_schema_hash,
            payment_oby        = max(approx_payment_oby, 1e-8),
        )
        validate_job_submission(spec)  # raises JobValidationError if malformed

        # Validation passed — proceed with fee processing
        receipt   = self.process_job_fee(job_id, coin, stable)
        gross_oby = self._usd_to_oby(receipt.liquidity_usd)
        dao_tax   = round(gross_oby * DAO_MINING_TAX_PCT, 8)
        total_miner_oby = round(gross_oby - dao_tax, 8)

        # For redundant tier the total is shared across REDUNDANT_MINER_COUNT
        # miners. oby_to_miner stores the per-miner share so the consensus
        # engine and the on_pass settlement code don't need tier-specific math.
        if tier == 'redundant':
            per_miner_oby = round(total_miner_oby / REDUNDANT_MINER_COUNT, 8)
        else:
            per_miner_oby = total_miner_oby

        # Finalise the spec with the actual payment
        spec.payment_oby = per_miner_oby

        job = ComputeJob(
            job_id             = job_id,
            developer_addr     = developer_addr,
            job_type           = job_type,
            model_id           = model_id,
            gpu_hours          = gpu_count * duration_hr,
            stablecoin         = coin.value,
            stable_paid        = stable,
            usd_paid           = receipt.gross_usd,
            oby_to_miner       = per_miner_oby,
            model_hash         = model_hash,
            container_digest   = container_digest,
            seed               = seed,
            input_payload_hash = input_payload_hash,
            input_schema_hash  = input_schema_hash,
            inputs             = inputs if inputs is not None else [],
            task               = task,
            params             = params if params is not None else {},
            tier               = tier,
        )
        with self._lock:
            self._jobs[job_id]       = job
            self.dao.vault_oby      += dao_tax
            self.dao.vault_deposits += 1
            # Register with verification engine ONLY for standard tier.
            # Redundant tier uses the consensus engine; it doesn't need
            # the verification engine's challenge state machine.
            if tier == 'standard':
                self.verification.register_job(spec)

        log.info(
            f"Job {job_id} ({tier}) | per-miner {per_miner_oby:.4f} OBY | "
            f"dao vault +{dao_tax:.4f} OBY (5% tax) | "
            f"model_hash={model_hash[:12]}.. seed={seed}"
        )
        return job, receipt

    def complete_job_with_verification(
        self,
        job_id      : str,
        miner_addr  : str,
        result_cid  : str,
        result_hash : str,
        zk_proof    : str = '',
    ) -> 'VerificationResult':
        """
        Submit a completed job. Dispatches on tier:

        Standard tier:
          Routes through VerificationEngine. The engine decides whether to
          issue a challenge based on the miner's tier (new/trusted/slashed)
          and a deterministic per-job seed. Optimistic/ZK accepts credit
          immediately; challenged results wait for resolve_challenge().

        Redundant tier:
          Accumulates the submission via ConsensusEngine. The job sits in
          'assigned' until either all N miners have submitted or the
          consensus deadline passes — only then does finalize() run.

        Returns a VerificationResult for standard tier or a synthesized
        VerificationResult-shaped dict for redundant tier so the API layer
        can present a consistent response shape.
        """
        from compute.verification import VerificationResult
        from compute.consensus import ConsensusError

        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise ValueError(f"unknown job {job_id}")
            tier = job.tier
            m    = self._miners.get(miner_addr)
            if m is None:
                raise ValueError(f"unregistered miner {miner_addr}")

        # ── Redundant tier: route through consensus ──
        if tier == 'redundant':
            try:
                with self._lock:
                    sub = self.consensus.submit_result(
                        job        = job,
                        miner_addr = miner_addr,
                        result_hash= result_hash,
                        result_cid = result_cid,
                    )
            except ConsensusError as e:
                raise ValueError(str(e))

            # If all N have submitted, finalize immediately
            if sub['ready_to_finalize']:
                self._finalize_redundant_locked(job)

            return VerificationResult(
                job_id  = job_id,
                passed  = True,
                method  = 'consensus_pending',
                details = (
                    f"submission {sub['submissions_count']}/"
                    f"{len(job.assigned_miners)} accepted"
                ),
            )

        # ── Standard tier (unchanged) ──
        if job.miner_addr and job.miner_addr != miner_addr:
            raise ValueError(
                f"miner mismatch: job assigned to {job.miner_addr}, "
                f"submitted by {miner_addr}"
            )
        miner_rep    = m.reputation
        miner_jobs   = m.jobs_completed
        miner_banned = m.is_banned(self._block_height())

        result = self.verification.submit_result(
            job_id        = job_id,
            miner_addr    = miner_addr,
            miner_rep     = miner_rep,
            miner_jobs    = miner_jobs,
            miner_banned  = miner_banned,
            result_cid    = result_cid,
            result_hash   = result_hash,
            zk_proof      = zk_proof,
        )

        # If the result was accepted (optimistic or ZK), credit the miner
        # immediately and mark the job done. Challenged results remain
        # 'assigned' until resolve_challenge() lands.
        if result.passed and result.method in ('optimistic', 'zk'):
            with self._lock:
                job.status       = 'done'
                job.completed_at = int(time.time())
                job.result_cid   = result_cid
                job.result_hash  = result_hash
                if m:
                    m.jobs_completed += 1
                    m.oby_earned     += job.oby_to_miner

        return result

    def _finalize_redundant_locked(self, job):
        """Run consensus finalization on a redundant job. Must be called with
        self._lock held. Wires the offence/stake/block-height providers so
        the consensus engine can compute slash amounts and ban deadlines
        without coupling to internal state.
        """
        def _offence_count(addr: str) -> int:
            m = self._miners.get(addr)
            return m.offence_count if m else 0
        def _stake(addr: str) -> float:
            m = self._miners.get(addr)
            return m.stake_oby if m else 0.0

        return self.consensus.finalize(
            job                    = job,
            offence_count_provider = _offence_count,
            stake_provider         = _stake,
            block_height_provider  = self._block_height,
        )

    def finalize_due_redundant_jobs(self) -> list:
        """
        Walk all assigned redundant jobs and finalize any past their
        consensus_deadline (timeout path). Called by the node's background
        sweep loop every 30s or so.

        Returns the list of ConsensusOutcome for jobs that were finalized
        on this call.
        """
        now = int(time.time())
        finalized = []
        with self._lock:
            candidates = [
                j for j in self._jobs.values()
                if j.tier == 'redundant'
                and j.status in ('assigned', 'pending')
                and self.consensus.is_ready_to_finalize(j, now)
            ]
        for job in candidates:
            with self._lock:
                outcome = self._finalize_redundant_locked(job)
            finalized.append(outcome)
        if finalized:
            log.info(f"finalize_due_redundant_jobs: settled {len(finalized)}")
        return finalized

    def resolve_job_challenge(
        self,
        challenge_id : str,
        rerun_hash   : str,
    ) -> 'ChallengeStatus':
        """
        Apply a challenger's rerun verdict. The verification engine returns
        the new status; on FAILED, its callbacks (already wired in __init__)
        will have updated miner state and recorded the refund.

        Returns the ChallengeStatus enum.
        """
        # Find the challenge to look up the miner's prior offence_count and stake
        challenge = None
        for c in self.verification.pending_challenges():
            if c.challenge_id == challenge_id:
                challenge = c
                break
        if challenge is None:
            from compute.verification import ChallengeStatus
            return ChallengeStatus.EXPIRED

        with self._lock:
            m = self._miners.get(challenge.miner_addr)
            stake_oby     = m.stake_oby if m else 0.0
            offence_count = m.offence_count if m else 0
            current_block = self._block_height()

        # The engine fires on_slash/on_refund/on_ban callbacks synchronously
        return self.verification.resolve_challenge(
            challenge_id  = challenge_id,
            rerun_hash    = rerun_hash,
            stake_oby     = stake_oby,
            offence_count = offence_count,
            current_block = current_block,
        )

    # ── Refund settlement ─────────────────────────────────────────────────────

    def process_pending_refunds(
        self,
        accounts_registry = None,
    ) -> dict:
        """
        Sweep all faulted jobs with unsettled refunds. For each:
          - convert the OBY refund to the developer's original stablecoin
            via the AMM (sell_oby)
          - credit the converted amount to the dev's account balance
          - mark the job's refund_settled = True so it doesn't sweep twice

        Returns a summary dict {settled, total_oby_swept, total_usd_credited,
        skipped_no_account, errors}.

        If accounts_registry is None we mark jobs settled with 0 USD credit
        and log the would-have-credited amount — useful for dev/testnet modes
        where the registry isn't wired.

        Idempotent: running twice in a row does nothing on the second call.
        """
        from tokenomics.engine import Stablecoin
        summary = {
            'settled'            : 0,
            'total_oby_swept'    : 0.0,
            'total_usd_credited' : 0.0,
            'skipped_no_account' : 0,
            'errors'             : 0,
        }

        with self._lock:
            # Snapshot the candidates so we don't iterate while mutating
            candidates = [
                j for j in self._jobs.values()
                if j.refund_oby > 0
                and not j.refund_settled
                and j.status == 'faulted'
            ]

        for job in candidates:
            try:
                # Resolve the developer's stablecoin preference from the job
                try:
                    coin = Stablecoin(job.stablecoin)
                except ValueError:
                    log.warning(
                        f"refund: unknown stablecoin {job.stablecoin!r} on "
                        f"job {job.job_id}, defaulting to USDC"
                    )
                    coin = Stablecoin.USDC

                # AMM swap OBY -> stablecoin
                swap = self.sell_oby(
                    oby_in=job.refund_oby, coin=coin,
                    user_addr=job.developer_addr,
                )
                stable_out = swap['stable_out']
                usd_out    = swap['usd_out']

                # Credit dev's account balance (only if registry is wired)
                if accounts_registry is not None:
                    acct = accounts_registry.get_by_id(job.developer_addr)
                    if acct is not None:
                        accounts_registry.credit_refund(
                            account_id=job.developer_addr,
                            amount_usd=usd_out,
                            job_id=job.job_id,
                        )
                        summary['total_usd_credited'] += usd_out
                    else:
                        summary['skipped_no_account'] += 1
                        log.warning(
                            f"refund: no account for {job.developer_addr[:12]}.. "
                            f"on job {job.job_id} — sweep recorded but balance "
                            f"NOT credited"
                        )

                # Mark settled atomically so a second sweep can't double-credit
                with self._lock:
                    job.refund_settled     = True
                    job.refund_stable_paid = stable_out
                    job.refund_settled_at  = int(time.time())

                summary['settled']         += 1
                summary['total_oby_swept'] += job.refund_oby

                log.info(
                    f"refund swept: job {job.job_id} | "
                    f"{job.refund_oby:.4f} OBY -> {stable_out:.4f} {coin.value} "
                    f"(${usd_out:.4f}) credited to {job.developer_addr[:12]}.."
                )
            except Exception as e:
                summary['errors'] += 1
                log.error(
                    f"refund sweep failed for job {job.job_id}: {e}"
                )

        return summary

    # ── DAO ────────────────────────────────────────────────────────────────────

    def dao_enable_burn(self, burn_pct: float):
        with self._lock:
            self.dao.burn_enabled    = burn_pct > 0
            self.dao.burn_pct_of_dao = max(0.0, min(1.0, burn_pct))
        log.info(f"DAO burn: {'ENABLED' if self.dao.burn_enabled else 'DISABLED'} "
                 f"at {burn_pct*100:.0f}% of DAO slice")

    def dao_add_liquidity(self, coin: Stablecoin, amount: float) -> float:
        with self._lock:
            available = self.dao.balances.get(coin)
            amount    = min(amount, available)
            if amount <= 0:
                return 0.0
            self.dao.balances.subtract(coin, amount)
            self.pool.add_liquidity(coin, amount, self.rates)
        log.info(f"DAO → pool: {amount:.4f} {coin.value}")
        return amount

    def dao_transition_to_onchain(self, contract_address: str):
        with self._lock:
            self.dao.is_multisig      = False
            self.dao.multisig_address = contract_address
        log.info(f"DAO transitioned to on-chain governance: {contract_address}")

    # ── Creator ────────────────────────────────────────────────────────────────

    def creator_disburse(
        self, coin: Stablecoin = None, amount: float = None
    ) -> float:
        """Disburse creator share. Defaults to USDC if no coin specified."""
        with self._lock:
            c = coin or Stablecoin.USDC
            avail    = self.creator.balances.get(c)
            disburse = min(avail, amount or avail)
            if disburse <= 0:
                return 0.0
            self.creator.balances.subtract(c, disburse)
            usd_val = self._stable_to_usd(c, disburse)
            self.creator.total_disbursed_usd += usd_val
            self.creator.last_disbursement    = int(time.time())
        log.info(
            f"Creator disbursement: {disburse:.4f} {c.value} "
            f"(${usd_val:.4f}) → {self.creator.address}"
        )
        return disburse

    # ── Miners ─────────────────────────────────────────────────────────────────

    def register_miner(self, profile: MinerProfile):
        with self._lock:
            self._miners[profile.address] = profile

    def record_uptime(self, addr: str, hours: float) -> float:
        with self._lock:
            m = self._miners.get(addr)
            if not m:
                return 0.0
            m.uptime_hours += hours
            bonus = hours * UPTIME_BONUS_OBY
            m.oby_earned  += bonus
            return bonus

    def record_heartbeat(self, addr: str) -> bool:
        """
        Update the miner's last_heartbeat timestamp. Returns True if the
        miner is known, False otherwise. Used by /compute/heartbeat to
        track liveness for the upcoming online-miners metric.
        """
        with self._lock:
            m = self._miners.get(addr)
            if not m:
                return False
            m.last_heartbeat = int(time.time())
            return True

    def pending_jobs_for_assignment(self) -> list[str]:
        """Return job_ids in 'pending' state, ordered by created_at ascending."""
        with self._lock:
            return [
                j.job_id for j in sorted(
                    (j for j in self._jobs.values() if j.status == 'pending'),
                    key=lambda j: j.created_at,
                )
            ]

    def get_job(self, job_id: str) -> Optional['ComputeJob']:
        with self._lock:
            return self._jobs.get(job_id)

    # ── Block Reward ───────────────────────────────────────────────────────────

    def block_reward_oby(self, height: int) -> float:
        halvings = height // HALVING_INTERVAL
        if halvings >= 64:
            return 0.0
        return round(
            (INITIAL_BLOCK_REWARD / (2 ** halvings)) * self.bootstrap_multiplier(height),
            8
        )

    def bootstrap_multiplier(self, block: int) -> float:
        if block >= BOOTSTRAP_BLOCKS:
            return 1.0
        return BOOTSTRAP_BONUS - (BOOTSTRAP_BONUS - 1.0) * (block / BOOTSTRAP_BLOCKS)

    # ── Quote ──────────────────────────────────────────────────────────────────

    def quote_job(
        self,
        job_type   : str,
        model_id   : str,
        coin       : Stablecoin = Stablecoin.USDC,
        gpu_count  : int   = 1,
        duration_hr: float = 1.0,
        tier       : str   = 'standard',
    ) -> dict:
        """
        Price a job. tier='standard' uses the base rate; tier='redundant'
        applies the 3x multiplier so the dev pays for all three miners.

        Pricing is transparent — there's no hidden DAO subsidy for redundant
        tier. The developer who needs trust-minimized verification pays for
        it; the price signal correctly directs cheap jobs to standard tier
        and trust-critical jobs to redundant tier.
        """
        mults = {
            'inference'  : 0.05,
            'embedding'  : 0.10,
            'fine_tuning': 1.00,
            'benchmark'  : 0.00,
        }
        tier_mults = {
            'standard'  : TIER_MULTIPLIER_STANDARD,
            'redundant' : TIER_MULTIPLIER_REDUNDANT,
        }
        if tier not in tier_mults:
            raise ValueError(f'unknown tier: {tier!r}')
        tier_mult = tier_mults[tier]

        base_usd    = BASE_GPU_HOUR_USD * gpu_count * duration_hr * mults.get(job_type, 1.0)
        usd_cost    = max(MIN_JOB_USD, base_usd * tier_mult)
        rate        = self.rates.get(coin)
        stable_cost = round(usd_cost / rate, 6)
        return {
            'job_type'       : job_type,
            'tier'           : tier,
            'tier_multiplier': tier_mult,
            'coin'           : coin.value,
            'stable_cost'    : stable_cost,
            'usd_cost'       : round(usd_cost, 4),
            'liquidity_usd'  : round(usd_cost * FEE_TO_LIQUIDITY, 4),
            'creator_usd'    : round(usd_cost * FEE_TO_CREATOR, 4),
            'dao_usd'        : round(usd_cost * FEE_TO_DAO, 4),
            'aws_equiv_usd'  : round(0.918 * gpu_count * duration_hr, 4),
            'savings_pct'    : round((1 - usd_cost / max(0.01, 0.918 * gpu_count * duration_hr)) * 100, 1),
        }

    # ── Dashboard ──────────────────────────────────────────────────────────────

    def dashboard(self, current_block: int = 0) -> dict:
        with self._lock:
            jobs    = list(self._jobs.values())
            miners  = list(self._miners.values())
            receipts= list(self._receipts)

        total_usd = sum(r.gross_usd for r in receipts)
        creator_usd = self.creator.balances.total_usd(
            {c: self.rates.get(c) for c in Stablecoin}
        )
        dao_usd = self.dao.balances.total_usd(
            {c: self.rates.get(c) for c in Stablecoin}
        )

        return {
            # Pool
            'pool'                  : self.pool.to_dict(),
            'basket_composition'    : self.pool.basket_composition(self.rates),

            # Oracle rates
            'oracle_rates'          : self.rates.to_dict(),

            # Creator Share
            'creator_share_usd'     : round(creator_usd, 4),
            'creator_share_balances': self.creator.balances.to_dict(),
            'creator_total_usd'     : round(self.creator.total_received_usd, 4),
            'creator_disbursed_usd' : round(self.creator.total_disbursed_usd, 4),

            # DAO Fund — stablecoin income (5% of compute fees)
            'dao_fund_usd'          : round(dao_usd, 4),
            'dao_balances'          : self.dao.balances.to_dict(),
            'dao_burn_enabled'      : self.dao.burn_enabled,
            'dao_oby_burned'        : round(self.dao.oby_burned, 6),
            'dao_is_multisig'       : self.dao.is_multisig,
            # DAO Vault — OBY income (5% mining tax on all miner earnings)
            'dao_vault_oby'         : round(self.dao.vault_oby, 8),
            'dao_vault_deposits'    : self.dao.vault_deposits,
            'dao_mining_tax_pct'    : DAO_MINING_TAX_PCT * 100,

            # Network
            'total_fees_usd'        : round(total_usd, 4),
            'jobs_total'            : len(jobs),
            'jobs_done'             : sum(1 for j in jobs if j.status == 'done'),
            'miners_registered'     : len(miners),
            'total_gpus'            : sum(m.gpu_count for m in miners),
            'bootstrap_mult'        : self.bootstrap_multiplier(current_block),
            'savings_vs_aws_pct'    : round((1 - BASE_GPU_HOUR_USD / 0.918) * 100, 1),
        }

    def save(self, path: str):
        from pathlib import Path
        with self._lock:
            data = {
                'genesis_ts'   : self.genesis_ts,
                'oby_price'    : self._oby_price,
                'pool'         : self.pool.to_dict(),
                'rates'        : self.rates.to_dict(),
                'creator'      : self.creator.to_dict(),
                'dao'          : self.dao.to_dict(),
                'jobs'         : {k: v.to_dict() for k, v in self._jobs.items()},
                'miners'       : {k: v.to_dict() for k, v in self._miners.items()},
            }
        Path(path).write_text(json.dumps(data, indent=2))
        log.info(f"State saved → {path}")

    def load(self, path: str):
        """
        Restore engine state from a save() snapshot.

        Restores miners and jobs into the in-memory dicts. Pool, rates, creator,
        and DAO balances are best-effort restored via their own from_dict-style
        constructors where available; if those don't round-trip cleanly, the
        snapshot's values are reflected in summary/dashboard but the live
        objects retain their defaults. The miner registry and job ledger DO
        round-trip cleanly — those are the critical ones for verification.

        Note: this does NOT restore the VerificationEngine's pending challenges
        or _jobs dict. Pending challenges are inherently in-flight state; on
        node restart they expire and re-resolve via the watchdog. The
        verification _jobs dict is repopulated by replaying any jobs whose
        status is 'pending' or 'assigned'.
        """
        from pathlib import Path
        from compute.verification import JobSpec
        raw = json.loads(Path(path).read_text())

        with self._lock:
            self.genesis_ts = raw.get('genesis_ts', self.genesis_ts)
            self._oby_price = raw.get('oby_price', self._oby_price)

            # Miners — fully reconstructible
            self._miners.clear()
            for addr, m_dict in raw.get('miners', {}).items():
                # Filter to known fields in case the snapshot has extras
                known_fields = {
                    'address', 'gpu_model', 'gpu_count', 'vram_gb',
                    'bandwidth_gbps', 'region', 'stake_oby', 'online_since',
                    'jobs_completed', 'jobs_failed', 'uptime_hours',
                    'oby_earned', 'reputation', 'offence_count',
                    'banned_until_block', 'last_heartbeat',
                }
                clean = {k: v for k, v in m_dict.items() if k in known_fields}
                self._miners[addr] = MinerProfile(**clean)

            # Jobs — fully reconstructible
            self._jobs.clear()
            for jid, j_dict in raw.get('jobs', {}).items():
                known_fields = {
                    'job_id', 'developer_addr', 'job_type', 'model_id',
                    'gpu_hours', 'stablecoin', 'stable_paid', 'usd_paid',
                    'oby_to_miner', 'miner_addr', 'status', 'created_at',
                    'completed_at', 'result_cid', 'result_hash',
                    'model_hash', 'container_digest', 'seed',
                    'input_payload_hash', 'input_schema_hash',
                    'inputs', 'task', 'params',
                    'refund_oby', 'refund_settled', 'refund_stable_paid',
                    'refund_settled_at',
                    'tier', 'assigned_miners', 'result_submissions',
                    'consensus_winners', 'consensus_outliers',
                    'consensus_deadline',
                }
                clean = {k: v for k, v in j_dict.items() if k in known_fields}
                self._jobs[jid] = ComputeJob(**clean)

            # Repopulate verification engine's job specs for any
            # pending/assigned jobs so /compute/result can still issue
            # challenges against them after restart
            for jid, job in self._jobs.items():
                if job.status not in ('pending', 'assigned'):
                    continue
                if not (job.model_hash and job.container_digest):
                    continue   # legacy job without determinism — skip
                try:
                    spec = JobSpec(
                        job_id             = job.job_id,
                        developer_addr     = job.developer_addr,
                        model_hash         = job.model_hash,
                        container_digest   = job.container_digest,
                        seed               = job.seed,
                        input_payload_hash = job.input_payload_hash,
                        input_schema_hash  = job.input_schema_hash,
                        payment_oby        = max(job.oby_to_miner, 1e-8),
                    )
                    self.verification.register_job(spec)
                except Exception as e:
                    log.warning(
                        f"load: could not restore JobSpec for {jid}: {e}"
                    )

        log.info(
            f"State loaded ← {path} "
            f"({len(self._miners)} miners, {len(self._jobs)} jobs)"
        )
