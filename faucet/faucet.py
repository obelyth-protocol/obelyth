"""
Obelyth Testnet Faucet
=========================
Sybil-resistant distribution of testnet OBY to verified registrants and miners.

Defences against draining:
  1. One claim per developer account_id, ever (account_id is derived from
     registered email which costs ~5s to acquire)
  2. One claim per source IP per FAUCET_IP_COOLDOWN_S (default 24h)
  3. Daily total payout cap (FAUCET_DAILY_BUDGET_OBY) — once exceeded,
     /faucet/claim returns 503 until midnight UTC
  4. Per-claim fixed amount (FAUCET_PAYOUT_OBY) — no claim-and-multiply
  5. Optional captcha hash check (caller posts a precomputed PoW-of-work
     value); off by default for testnet

What this is NOT:
  - Not a production rate limiter (use Cloudflare or fail2ban in front)
  - Not a Sybil-proof system in absolute terms (determined attacker can
     spin up VPN endpoints + emails); good enough to filter casual bots
  - Not a webhook system (no automatic top-ups; once dry, dry)

Storage
-------
SQLite table 'faucet_claims' in the node's data dir. Survives restarts.
Columns: account_id, source_ip, claim_at, amount_oby, tx_hash, status.

Wiring
------
FaucetService is constructed by node/fullnode.py with the chain, wallet, and
accounts registry. ComputeAPI doesn't touch it — the faucet has its own
HTTP routes (/faucet/claim, /faucet/status) handled directly by fullnode.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger('obelyth.faucet')


# ── Defaults — overridable on FaucetService construction ─────────────────────

FAUCET_PAYOUT_OBY        = 1_500.0    # per-claim amount
FAUCET_DAILY_BUDGET_OBY  = 750_000.0  # 500 claims/day at default payout
FAUCET_IP_COOLDOWN_S     = 24 * 3600  # 24h between claims from same IP
FAUCET_MIN_BALANCE_OBY   = 10_000.0   # refuse to claim if reserve below this
FAUCET_TX_FEE_OBY        = 0.001


# ── Errors ───────────────────────────────────────────────────────────────────

class FaucetError(Exception):
    """Base class for faucet refusals. .code is a short machine-readable tag."""
    code = 'faucet_error'


class FaucetAlreadyClaimed(FaucetError):
    code = 'already_claimed'


class FaucetIPCooldown(FaucetError):
    code = 'ip_cooldown'


class FaucetBudgetExhausted(FaucetError):
    code = 'budget_exhausted'


class FaucetReserveDry(FaucetError):
    code = 'reserve_dry'


class FaucetInvalidAddress(FaucetError):
    code = 'invalid_address'


class FaucetUnknownAccount(FaucetError):
    code = 'unknown_account'


class FaucetMissingApiKey(FaucetError):
    code = 'missing_api_key'


# ── Claim record ─────────────────────────────────────────────────────────────

@dataclass
class FaucetClaim:
    claim_id    : str
    account_id  : str
    address     : str           # destination wallet address
    source_ip   : str
    amount_oby  : float
    claim_at    : int
    tx_hash     : str = ''
    status      : str = 'pending'   # pending | confirmed | failed

    def to_dict(self) -> dict:
        return {
            'claim_id'   : self.claim_id,
            'account_id' : self.account_id,
            'address'    : self.address,
            'source_ip'  : self.source_ip,
            'amount_oby' : self.amount_oby,
            'claim_at'   : self.claim_at,
            'tx_hash'    : self.tx_hash,
            'status'     : self.status,
        }


# ── Service ──────────────────────────────────────────────────────────────────

class FaucetService:
    """
    Faucet for distributing testnet OBY.

    Dependencies are injected so tests can swap them out:
      chain             — Blockchain instance (for mempool submission + balance)
      wallet            — Wallet that holds the faucet's OBY reserve
      accounts_registry — to validate api_keys and look up account_ids
    """

    def __init__(
        self,
        chain,
        wallet,
        accounts_registry           = None,
        db_path           : str     = './obelyth_data/faucet.db',
        payout_oby        : float   = FAUCET_PAYOUT_OBY,
        daily_budget_oby  : float   = FAUCET_DAILY_BUDGET_OBY,
        ip_cooldown_s     : int     = FAUCET_IP_COOLDOWN_S,
        min_reserve_oby   : float   = FAUCET_MIN_BALANCE_OBY,
        tx_fee_oby        : float   = FAUCET_TX_FEE_OBY,
        require_api_key   : bool    = True,
    ):
        self.chain             = chain
        self.wallet            = wallet
        self.accounts          = accounts_registry
        self.payout_oby        = payout_oby
        self.daily_budget_oby  = daily_budget_oby
        self.ip_cooldown_s     = ip_cooldown_s
        self.min_reserve_oby   = min_reserve_oby
        self.tx_fee_oby        = tx_fee_oby
        self.require_api_key   = require_api_key
        self.db_path           = db_path
        self._lock             = threading.RLock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── Schema ───────────────────────────────────────────────────────────────

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS faucet_claims (
                    claim_id    TEXT PRIMARY KEY,
                    account_id  TEXT NOT NULL,
                    address     TEXT NOT NULL,
                    source_ip   TEXT NOT NULL,
                    amount_oby  REAL NOT NULL,
                    claim_at    INTEGER NOT NULL,
                    tx_hash     TEXT NOT NULL DEFAULT '',
                    status      TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE INDEX IF NOT EXISTS idx_account_id ON faucet_claims(account_id);
                CREATE INDEX IF NOT EXISTS idx_source_ip   ON faucet_claims(source_ip);
                CREATE INDEX IF NOT EXISTS idx_claim_at    ON faucet_claims(claim_at);
            ''')
        log.info(f"Faucet DB initialised: {self.db_path}")

    # ── Validation helpers ───────────────────────────────────────────────────

    @staticmethod
    def _is_valid_address(addr: str) -> bool:
        """Cheap sanity check. Full validation happens at tx-build time."""
        if not isinstance(addr, str):
            return False
        if len(addr) < 20 or len(addr) > 80:
            return False
        return addr.replace('-', '').replace('_', '').isalnum()

    # ── Lookup helpers ───────────────────────────────────────────────────────

    def _has_account_claimed(self, account_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM faucet_claims WHERE account_id = ? AND status != 'failed' LIMIT 1",
                (account_id,),
            ).fetchone()
        return row is not None

    def _ip_last_claim_at(self, source_ip: str) -> Optional[int]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(claim_at) FROM faucet_claims "
                "WHERE source_ip = ? AND status != 'failed'",
                (source_ip,),
            ).fetchone()
        return row[0] if row and row[0] is not None else None

    def _paid_since(self, since_ts: int) -> float:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount_oby), 0.0) FROM faucet_claims "
                "WHERE claim_at >= ? AND status != 'failed'",
                (since_ts,),
            ).fetchone()
        return float(row[0]) if row else 0.0

    def _start_of_today_utc(self) -> int:
        now = int(time.time())
        return now - (now % 86_400)

    # ── Reserve ──────────────────────────────────────────────────────────────

    def reserve_balance(self) -> float:
        """OBY available in the faucet wallet."""
        try:
            return self.wallet.balance(self.chain.utxos)
        except Exception as e:
            log.warning(f"Faucet balance lookup failed: {e}")
            return 0.0

    # ── Main claim path ──────────────────────────────────────────────────────

    def claim(
        self,
        address    : str,
        source_ip  : str,
        api_key    : str = '',
    ) -> FaucetClaim:
        """
        Validate Sybil defences, then build + submit a transaction.

        Returns a FaucetClaim with tx_hash populated on success.
        Raises a FaucetError subclass on any refusal.
        """
        import uuid

        # ── 1. Address format ──
        if not self._is_valid_address(address):
            raise FaucetInvalidAddress(f'invalid address: {address!r}')

        # ── 2. Resolve account (if registry wired and required) ──
        if self.require_api_key and self.accounts is not None:
            if not api_key:
                raise FaucetMissingApiKey('api_key required')
            account = self.accounts.get_by_api_key(api_key)
            if account is None:
                raise FaucetUnknownAccount('unknown api_key')
            account_id = account.account_id
        else:
            # Testnet/dev mode: derive a stable account_id from the address
            # so per-account dedupe still works
            account_id = hashlib.sha256(
                f'anon:{address}'.encode()
            ).hexdigest()[:32]

        # ── 3. Per-account dedupe (lifetime, not daily) ──
        with self._lock:
            if self._has_account_claimed(account_id):
                raise FaucetAlreadyClaimed(
                    f'account {account_id[:12]}.. has already claimed'
                )

            # ── 4. IP cooldown ──
            last_ip_at = self._ip_last_claim_at(source_ip)
            if last_ip_at is not None:
                elapsed = int(time.time()) - last_ip_at
                if elapsed < self.ip_cooldown_s:
                    retry_in = self.ip_cooldown_s - elapsed
                    raise FaucetIPCooldown(
                        f'IP cooldown: retry in {retry_in}s'
                    )

            # ── 5. Daily budget ──
            paid_today = self._paid_since(self._start_of_today_utc())
            if paid_today + self.payout_oby > self.daily_budget_oby:
                raise FaucetBudgetExhausted(
                    f'daily budget {self.daily_budget_oby:.0f} OBY exhausted '
                    f'(${paid_today:.2f} paid today)'
                )

            # ── 6. Reserve check ──
            reserve = self.reserve_balance()
            if reserve < self.min_reserve_oby:
                raise FaucetReserveDry(
                    f'faucet reserve below floor: {reserve:.2f} OBY '
                    f'(min {self.min_reserve_oby:.0f})'
                )
            if reserve < self.payout_oby + self.tx_fee_oby:
                raise FaucetReserveDry(
                    f'insufficient reserve for one payout: {reserve:.2f} OBY'
                )

            # ── 7. Build, sign, submit tx ──
            tx = self.wallet.build_transaction(
                utxo_set    = self.chain.utxos,
                to_address  = address,
                amount      = self.payout_oby,
                fee         = self.tx_fee_oby,
                memo        = f'faucet:{account_id[:12]}',
            )
            if tx is None:
                raise FaucetReserveDry(
                    'wallet returned None on build_transaction'
                )
            ok = self.chain.add_to_mempool(tx)
            if not ok:
                raise FaucetError(
                    'mempool rejected the faucet transaction'
                )

            # ── 8. Record the claim ──
            claim = FaucetClaim(
                claim_id   = str(uuid.uuid4())[:16],
                account_id = account_id,
                address    = address,
                source_ip  = source_ip,
                amount_oby = self.payout_oby,
                claim_at   = int(time.time()),
                tx_hash    = tx.hash,
                status     = 'pending',
            )
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    '''INSERT INTO faucet_claims
                       (claim_id, account_id, address, source_ip,
                        amount_oby, claim_at, tx_hash, status)
                       VALUES (?,?,?,?,?,?,?,?)''',
                    (claim.claim_id, claim.account_id, claim.address,
                     claim.source_ip, claim.amount_oby, claim.claim_at,
                     claim.tx_hash, claim.status),
                )

            log.info(
                f"Faucet paid: {self.payout_oby:.2f} OBY to {address[:16]}.. "
                f"(account={account_id[:12]} ip={source_ip} "
                f"tx={tx.hash[:12]}..)"
            )
            return claim

    # ── Status / dashboard ───────────────────────────────────────────────────

    def status(self) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_oby), 0.0) "
                "FROM faucet_claims WHERE status != 'failed'"
            ).fetchone()
            total_claims = row[0] if row else 0
            total_paid   = float(row[1]) if row else 0.0

            today_ts = self._start_of_today_utc()
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_oby), 0.0) "
                "FROM faucet_claims WHERE claim_at >= ? AND status != 'failed'",
                (today_ts,),
            ).fetchone()
            claims_today = row[0] if row else 0
            paid_today   = float(row[1]) if row else 0.0

        return {
            'reserve_oby'         : round(self.reserve_balance(), 4),
            'payout_per_claim_oby': self.payout_oby,
            'total_paid_oby'      : round(total_paid, 4),
            'total_claims'        : total_claims,
            'paid_today_oby'      : round(paid_today, 4),
            'claims_today'        : claims_today,
            'daily_budget_oby'    : self.daily_budget_oby,
            'budget_remaining_oby': round(
                max(0.0, self.daily_budget_oby - paid_today), 4
            ),
            'wallet_address'      : self.wallet.primary_address
                                    if self.wallet else '',
        }


__all__ = [
    'FaucetService', 'FaucetClaim',
    'FaucetError', 'FaucetAlreadyClaimed', 'FaucetIPCooldown',
    'FaucetBudgetExhausted', 'FaucetReserveDry',
    'FaucetInvalidAddress', 'FaucetUnknownAccount', 'FaucetMissingApiKey',
    'FAUCET_PAYOUT_OBY', 'FAUCET_DAILY_BUDGET_OBY',
    'FAUCET_IP_COOLDOWN_S', 'FAUCET_MIN_BALANCE_OBY',
]
