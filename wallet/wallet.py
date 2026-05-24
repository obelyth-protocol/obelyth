"""
Obelyth Wallet
==================
- HD-style key derivation (HMAC-SHA3 based, simplified BIP32-like)
- Address book
- Transaction builder & signer
- Balance queries
- Vesting dashboard for founder
"""

import os
import json
import time
import hmac
import hashlib
import logging
from pathlib import Path
from typing import Optional

from core.crypto import (
    PrivateKey, PublicKey, generate_keypair, sha3_256, VestingSchedule
)
from core.structures import (
    Transaction, TxInput, TxOutput, TxType, UTXO
)

log = logging.getLogger('obelyth.wallet')


# ── HD Key Derivation (simplified BIP32-like) ──────────────────────────────────

def derive_child_key(master_seed: bytes, index: int) -> PrivateKey:
    """
    Derive child private key from master seed + index.
    Uses HMAC-SHA3-512; in the Rust port, replace with full BIP32 secp256k1 derivation.
    """
    data  = master_seed + index.to_bytes(4, 'big')
    child = hmac.new(b'Obelyth seed', data, hashlib.sha3_512).digest()
    # Use first 32 bytes as private key scalar
    scalar = int.from_bytes(child[:32], 'big') % (
        0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
    )  # secp256k1 order
    return PrivateKey(scalar.to_bytes(32, 'big'))


class KeyPair:
    def __init__(self, private: PrivateKey, index: int = 0, label: str = ''):
        self.private = private
        self.public  = private.public_key()
        self.address = self.public.to_address()
        self.index   = index
        self.label   = label

    def to_dict(self, include_private: bool = False) -> dict:
        d = {
            'index'  : self.index,
            'label'  : self.label,
            'address': self.address,
            'pubkey' : self.public.to_bytes().hex(),
        }
        if include_private:
            d['wif'] = self.private.to_wif()
        return d


class Wallet:
    """
    Obelyth software wallet.
    Manages keys, signs transactions, tracks balances.
    """

    def __init__(self, seed: bytes = None, label: str = 'default'):
        self.label = label
        self.seed  = seed or os.urandom(32)
        self._keypairs : list[KeyPair]   = []
        self._utxo_cache: dict[str, list[UTXO]] = {}   # address -> UTXOs

        # Derive initial keypair (index 0)
        self._derive_next()

    @classmethod
    def new(cls, label: str = 'default') -> 'Wallet':
        return cls(os.urandom(32), label)

    @classmethod
    def from_seed_hex(cls, seed_hex: str, label: str = 'default') -> 'Wallet':
        return cls(bytes.fromhex(seed_hex), label)

    # ── Key Derivation ─────────────────────────────────────────────────────────

    def _derive_next(self) -> KeyPair:
        idx = len(self._keypairs)
        priv = derive_child_key(self.seed, idx)
        kp   = KeyPair(priv, idx)
        self._keypairs.append(kp)
        log.debug(f"Derived key #{idx}: {kp.address}")
        return kp

    def new_address(self, label: str = '') -> str:
        """Derive a fresh receiving address."""
        kp = self._derive_next()
        kp.label = label
        return kp.address

    @property
    def primary_address(self) -> str:
        return self._keypairs[0].address

    @property
    def all_addresses(self) -> list[str]:
        return [kp.address for kp in self._keypairs]

    def keypair_for(self, address: str) -> Optional[KeyPair]:
        return next((kp for kp in self._keypairs if kp.address == address), None)

    # ── Balance & UTXOs ────────────────────────────────────────────────────────

    def balance(self, utxo_set) -> float:
        """Sum unspent outputs across all wallet addresses."""
        total = 0.0
        for addr in self.all_addresses:
            total += utxo_set.balance(addr)
        return round(total, 8)

    def collect_utxos(self, utxo_set, min_amount: float = 0.0) -> list[UTXO]:
        """Gather all unspent UTXOs for this wallet."""
        utxos = []
        for addr in self.all_addresses:
            utxos.extend(utxo_set.unspent_for(addr))
        return [u for u in utxos if u.amount >= min_amount]

    # ── Transaction Building ───────────────────────────────────────────────────

    def build_transaction(
        self,
        utxo_set,
        to_address   : str,
        amount       : float,
        fee          : float   = 0.001,
        memo         : str     = '',
        use_zk       : bool    = False,
    ) -> Optional[Transaction]:
        """
        Build and sign a transaction.
        Automatically selects UTXOs (largest-first coin selection).
        Returns None if insufficient funds.
        """
        if amount <= 0:
            raise ValueError("Amount must be positive")
        if fee < 0:
            raise ValueError("Fee cannot be negative")

        needed = amount + fee
        available_utxos = sorted(
            self.collect_utxos(utxo_set),
            key=lambda u: u.amount,
            reverse=True,
        )

        # Coin selection: greedy largest-first
        selected   : list[UTXO] = []
        selected_total = 0.0
        for utxo in available_utxos:
            selected.append(utxo)
            selected_total += utxo.amount
            if selected_total >= needed:
                break

        if selected_total < needed:
            log.warning(f"Insufficient funds: have {selected_total:.8f}, need {needed:.8f}")
            return None

        change = round(selected_total - needed, 8)

        # Build outputs
        outputs = [TxOutput(address=to_address, amount=amount)]
        if change > 1e-8:
            change_addr = self.new_address('change')
            outputs.append(TxOutput(address=change_addr, amount=change))

        # Build inputs (unsigned first, then sign)
        inputs = [
            TxInput(
                utxo_tx_hash = u.tx_hash,
                utxo_index   = u.index,
                signature    = '',        # filled below
                public_key   = '',        # filled below
            )
            for u in selected
        ]

        tx = Transaction(
            tx_type  = TxType.ZK if use_zk else TxType.REGULAR,
            inputs   = inputs,
            outputs  = outputs,
            fee      = fee,
            memo     = memo,
        )

        # Sign each input with the owning keypair
        signing_payload = tx._signing_payload()
        for inp, utxo in zip(inputs, selected):
            kp = self.keypair_for(utxo.address)
            if kp is None:
                log.error(f"No key for address {utxo.address}")
                return None
            sig = kp.private.sign(signing_payload)
            inp.signature  = sig.hex()
            inp.public_key = kp.public.to_bytes().hex()

            # ZK mode: replace addresses with blind placeholders
            if use_zk:
                tx.zk_proof = self._generate_zk_stub(tx, signing_payload)

        tx._hash = tx.compute_hash()
        log.info(f"Built tx {tx.hash[:12]}... {amount} OBY → {to_address} fee={fee}")
        return tx

    def _generate_zk_stub(self, tx: Transaction, payload: bytes) -> str:
        """
        Stub ZK proof.
        In the Rust port, replace with a real ZK-SNARK (e.g. Groth16 or PLONK)
        that proves: input UTXOs are valid and unspent, outputs balance, without
        revealing addresses or amounts.
        """
        commitment = sha3_256(payload + self.seed[:8]).hex()
        return f"zk-stub::{commitment}"

    # ── Vesting Dashboard ──────────────────────────────────────────────────────

    def vesting_status(self, vesting: VestingSchedule) -> dict:
        now = int(time.time())
        return {
            'founder_address' : vesting.founder_address,
            'total_allocation': vesting.total_oby,
            'vested_now'      : vesting.vested_amount(now),
            'locked_now'      : vesting.locked_amount(now),
            'cliff_months'    : vesting.cliff_months,
            'total_months'    : vesting.total_months,
            'genesis_ts'      : vesting.genesis_timestamp,
            'pct_vested'      : round(
                vesting.vested_amount(now) / vesting.total_oby * 100, 2
            ),
        }

    # ── Persistence ────────────────────────────────────────────────────────────

    def save(self, path: str, passphrase: str = ''):
        """
        Save wallet to disk.
        WARNING: In production, encrypt with passphrase using AES-256-GCM.
        This stub stores plaintext — use hardware wallet or encrypted keystore in prod.
        """
        data = {
            'label'    : self.label,
            'seed_hex' : self.seed.hex(),   # ENCRYPT THIS IN PRODUCTION
            'keypairs' : [kp.to_dict() for kp in self._keypairs],
        }
        Path(path).write_text(json.dumps(data, indent=2))
        log.info(f"Wallet saved to {path}")

    @classmethod
    def load(cls, path: str) -> 'Wallet':
        data = json.loads(Path(path).read_text())
        w = cls.from_seed_hex(data['seed_hex'], data['label'])
        # Re-derive all saved keypairs
        extra = len(data['keypairs']) - len(w._keypairs)
        for _ in range(extra):
            w._derive_next()
        for kp, saved in zip(w._keypairs, data['keypairs']):
            kp.label = saved.get('label', '')
        return w

    def __repr__(self):
        return (f"Wallet({self.label!r} "
                f"addresses={len(self._keypairs)} "
                f"primary={self.primary_address[:16]}...)")
