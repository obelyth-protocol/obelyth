"""
Obelyth Transaction & Block Structures
==========================================
Transactions:
  - Regular (UTXO model)
  - Coinbase (block reward)
  - ZK-private (amount/addresses hidden, validity proven off-chain stub)
  - Vesting (founder unlock — consensus-enforced)

Blocks:
  - DAG-aware: multiple parent hashes
  - Consensus type: PoW | PoS | DAG-tip
  - Adaptive size cap
"""

import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from typing import Optional
from enum import Enum

from core.crypto import sha3_256, double_sha3, merkle_root, PublicKey


# ── Enums ──────────────────────────────────────────────────────────────────────

class TxType(str, Enum):
    REGULAR  = 'regular'
    COINBASE = 'coinbase'
    ZK       = 'zk_private'
    VESTING  = 'vesting'

class ConsensusType(str, Enum):
    POW = 'pow'
    POS = 'pos'
    DAG = 'dag'


# ── UTXO ───────────────────────────────────────────────────────────────────────

@dataclass
class UTXO:
    tx_hash : str
    index   : int
    address : str
    amount  : float     # OBY
    spent   : bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'UTXO':
        return cls(**d)


# ── Transaction Input / Output ─────────────────────────────────────────────────

@dataclass
class TxInput:
    utxo_tx_hash : str        # hash of tx containing the UTXO
    utxo_index   : int        # which output in that tx
    signature    : str        # hex DER sig
    public_key   : str        # hex compressed pubkey

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'TxInput':
        return cls(**d)


@dataclass
class TxOutput:
    address : str
    amount  : float   # OBY

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'TxOutput':
        return cls(**d)


# ── Transaction ────────────────────────────────────────────────────────────────

@dataclass
class Transaction:
    tx_type   : TxType
    inputs    : list[TxInput]
    outputs   : list[TxOutput]
    fee       : float
    timestamp : int        = field(default_factory=lambda: int(time.time()))
    nonce     : int        = field(default_factory=lambda: int.from_bytes(
                                       __import__('os').urandom(4), 'big'))
    zk_proof  : str        = ''    # stub — full ZK proof goes here
    memo      : str        = ''
    _hash     : str        = field(default='', repr=False)

    # ── Serialisation ──
    def body_bytes(self) -> bytes:
        """Deterministic bytes for signing/hashing (excludes _hash field)."""
        body = {
            'type'     : self.tx_type.value,
            'inputs'   : [i.to_dict() for i in self.inputs],
            'outputs'  : [o.to_dict() for o in self.outputs],
            'fee'      : self.fee,
            'timestamp': self.timestamp,
            'nonce'    : self.nonce,
            'zk_proof' : self.zk_proof,
            'memo'     : self.memo,
        }
        return json.dumps(body, sort_keys=True).encode()

    def compute_hash(self) -> str:
        return double_sha3(self.body_bytes()).hex()

    @property
    def hash(self) -> str:
        if not self._hash:
            self._hash = self.compute_hash()
        return self._hash

    def to_dict(self) -> dict:
        return {
            'tx_type'  : self.tx_type.value,
            'inputs'   : [i.to_dict() for i in self.inputs],
            'outputs'  : [o.to_dict() for o in self.outputs],
            'fee'      : self.fee,
            'timestamp': self.timestamp,
            'nonce'    : self.nonce,
            'zk_proof' : self.zk_proof,
            'memo'     : self.memo,
            'hash'     : self.hash,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'Transaction':
        tx = cls(
            tx_type  = TxType(d['tx_type']),
            inputs   = [TxInput.from_dict(i) for i in d['inputs']],
            outputs  = [TxOutput.from_dict(o) for o in d['outputs']],
            fee      = d['fee'],
            timestamp= d['timestamp'],
            nonce    = d['nonce'],
            zk_proof = d.get('zk_proof', ''),
            memo     = d.get('memo', ''),
        )
        tx._hash = d.get('hash', tx.compute_hash())
        return tx

    # ── Verification ──
    def verify_signatures(self, utxo_set: dict) -> bool:
        """Verify all input signatures against UTXOs."""
        if self.tx_type in (TxType.COINBASE, TxType.VESTING):
            return True  # no input sigs needed

        if self.tx_type == TxType.ZK:
            return bool(self.zk_proof)  # stub: real ZK verifier goes here

        signing_data = self._signing_payload()
        for inp in self.inputs:
            key = f"{inp.utxo_tx_hash}:{inp.utxo_index}"
            utxo = utxo_set.get(key)
            if utxo is None or utxo.spent:
                return False
            try:
                pub  = PublicKey.from_bytes(bytes.fromhex(inp.public_key))
                addr = pub.to_address()
                if addr != utxo.address:
                    return False
                sig = bytes.fromhex(inp.signature)
                if not pub.verify(signing_data, sig):
                    return False
            except Exception:
                return False
        return True

    def _signing_payload(self) -> bytes:
        """Bytes inputs are signed over (excludes signatures themselves)."""
        payload = {
            'type'   : self.tx_type.value,
            'inputs' : [{'utxo_tx_hash': i.utxo_tx_hash, 'utxo_index': i.utxo_index}
                        for i in self.inputs],
            'outputs': [o.to_dict() for o in self.outputs],
            'fee'    : self.fee,
            'nonce'  : self.nonce,
        }
        return json.dumps(payload, sort_keys=True).encode()


# ── Block Header ───────────────────────────────────────────────────────────────

@dataclass
class BlockHeader:
    height          : int
    parent_hashes   : list[str]       # DAG: 1-N parents
    merkle_root     : str
    timestamp       : int
    consensus_type  : ConsensusType
    miner_address   : str
    difficulty      : int             # PoW: leading zero bits required
    nonce           : int  = 0        # PoW nonce
    validator_sig   : str  = ''       # PoS: validator signature
    _hash           : str  = field(default='', repr=False)

    def header_bytes(self) -> bytes:
        data = {
            'height'        : self.height,
            'parent_hashes' : sorted(self.parent_hashes),
            'merkle_root'   : self.merkle_root,
            'timestamp'     : self.timestamp,
            'consensus_type': self.consensus_type.value,
            'miner_address' : self.miner_address,
            'difficulty'    : self.difficulty,
            'nonce'         : self.nonce,
        }
        return json.dumps(data, sort_keys=True).encode()

    def compute_hash(self) -> str:
        return double_sha3(self.header_bytes()).hex()

    @property
    def hash(self) -> str:
        if not self._hash:
            self._hash = self.compute_hash()
        return self._hash

    def invalidate(self):
        self._hash = ''

    def to_dict(self) -> dict:
        return {
            'height'        : self.height,
            'parent_hashes' : self.parent_hashes,
            'merkle_root'   : self.merkle_root,
            'timestamp'     : self.timestamp,
            'consensus_type': self.consensus_type.value,
            'miner_address' : self.miner_address,
            'difficulty'    : self.difficulty,
            'nonce'         : self.nonce,
            'validator_sig' : self.validator_sig,
            'hash'          : self.hash,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'BlockHeader':
        h = cls(
            height         = d['height'],
            parent_hashes  = d['parent_hashes'],
            merkle_root    = d['merkle_root'],
            timestamp      = d['timestamp'],
            consensus_type = ConsensusType(d['consensus_type']),
            miner_address  = d['miner_address'],
            difficulty     = d['difficulty'],
            nonce          = d.get('nonce', 0),
            validator_sig  = d.get('validator_sig', ''),
        )
        h._hash = d.get('hash', h.compute_hash())
        return h


# ── Block ──────────────────────────────────────────────────────────────────────

# Adaptive block size: 1 MB base, up to 8 MB, adjusts every 100 blocks
BASE_BLOCK_SIZE  = 1_048_576    # 1 MB
MAX_BLOCK_SIZE   = 8_388_608    # 8 MB
MIN_BLOCK_SIZE   = 262_144      # 256 KB
BURN_RATE        = 0.002        # 0.2% of fees burned per block


@dataclass
class Block:
    header       : BlockHeader
    transactions : list[Transaction]
    size_limit   : int = BASE_BLOCK_SIZE

    @property
    def hash(self) -> str:
        return self.header.hash

    @property
    def height(self) -> int:
        return self.header.height

    @property
    def total_fees(self) -> float:
        return sum(tx.fee for tx in self.transactions
                   if tx.tx_type not in (TxType.COINBASE, TxType.VESTING))

    @property
    def burned_fees(self) -> float:
        return round(self.total_fees * BURN_RATE, 8)

    def serialised_size(self) -> int:
        return len(json.dumps(self.to_dict()).encode())

    def to_dict(self) -> dict:
        return {
            'header'      : self.header.to_dict(),
            'transactions': [tx.to_dict() for tx in self.transactions],
            'size_limit'  : self.size_limit,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'Block':
        return cls(
            header       = BlockHeader.from_dict(d['header']),
            transactions = [Transaction.from_dict(tx) for tx in d['transactions']],
            size_limit   = d.get('size_limit', BASE_BLOCK_SIZE),
        )

    def __repr__(self):
        return (f"Block(#{self.height} hash={self.hash[:12]}... "
                f"txs={len(self.transactions)} type={self.header.consensus_type.value})")
