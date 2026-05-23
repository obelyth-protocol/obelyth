"""
Obelyth Cryptographic Primitives
====================================
- SHA3-256 hashing (quantum-resistant step up from SHA2)
- ECDSA secp256k1 key pairs (same curve as Bitcoin; swap for Dilithium in Rust port)
- Address derivation: pubkey -> SHA3-256 -> RIPEMD160 -> Base58Check
- Vesting lock verification
"""

import hashlib
import hmac
import os
import base64
import struct
from typing import Tuple

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature, encode_dss_signature
)
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.backends import default_backend

# ── Constants ──────────────────────────────────────────────────────────────────
CURVE       = ec.SECP256K1()
HASH_ALG    = hashes.SHA3_256()
ADDRESS_VER = b'\x28'          # version byte → addresses start with 'N'
BASE58_ALPHA = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


# ── Hashing ────────────────────────────────────────────────────────────────────

def sha3_256(data: bytes) -> bytes:
    return hashlib.sha3_256(data).digest()

def double_sha3(data: bytes) -> bytes:
    return sha3_256(sha3_256(data))

def ripemd160(data: bytes) -> bytes:
    h = hashlib.new('ripemd160')
    h.update(data)
    return h.digest()

def hash160(data: bytes) -> bytes:
    """SHA3-256 then RIPEMD160 — used for address derivation."""
    return ripemd160(sha3_256(data))

def merkle_root(hashes: list[bytes]) -> bytes:
    """Build Merkle root from list of tx hashes."""
    if not hashes:
        return sha3_256(b'')
    layer = hashes[:]
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])   # duplicate last if odd
        layer = [sha3_256(layer[i] + layer[i+1]) for i in range(0, len(layer), 2)]
    return layer[0]


# ── Base58Check ────────────────────────────────────────────────────────────────

def _b58encode(data: bytes) -> str:
    n = int.from_bytes(data, 'big')
    result = []
    while n:
        n, r = divmod(n, 58)
        result.append(BASE58_ALPHA[r])
    # leading zero bytes → leading '1's
    for byte in data:
        if byte == 0:
            result.append(BASE58_ALPHA[0])
        else:
            break
    return ''.join(reversed(result))

def _b58decode(s: str) -> bytes:
    n = 0
    for char in s:
        n = n * 58 + BASE58_ALPHA.index(char)
    result = []
    while n:
        n, r = divmod(n, 256)
        result.append(r)
    for char in s:
        if char == BASE58_ALPHA[0]:
            result.append(0)
        else:
            break
    return bytes(reversed(result))

def b58check_encode(payload: bytes) -> str:
    checksum = double_sha3(payload)[:4]
    return _b58encode(payload + checksum)

def b58check_decode(s: str) -> bytes:
    data = _b58decode(s)
    payload, checksum = data[:-4], data[-4:]
    if double_sha3(payload)[:4] != checksum:
        raise ValueError(f"Base58Check checksum failure for: {s}")
    return payload


# ── Key Pairs ─────────────────────────────────────────────────────────────────

class PrivateKey:
    def __init__(self, key=None):
        if key is None:
            self._key = ec.generate_private_key(CURVE, default_backend())
        elif isinstance(key, bytes):
            self._key = ec.derive_private_key(
                int.from_bytes(key, 'big'), CURVE, default_backend()
            )
        else:
            self._key = key

    @classmethod
    def from_wif(cls, wif: str) -> 'PrivateKey':
        payload = b58check_decode(wif)
        assert payload[0:1] == b'\x80', "Invalid WIF version byte"
        raw = payload[1:33]
        return cls(raw)

    def to_wif(self) -> str:
        raw = self._key.private_numbers().private_value.to_bytes(32, 'big')
        return b58check_encode(b'\x80' + raw)

    def to_bytes(self) -> bytes:
        return self._key.private_numbers().private_value.to_bytes(32, 'big')

    def public_key(self) -> 'PublicKey':
        return PublicKey(self._key.public_key())

    def sign(self, message: bytes) -> bytes:
        """Sign raw bytes; returns DER-encoded signature."""
        sig = self._key.sign(message, ec.ECDSA(hashes.SHA3_256()))
        return sig


class PublicKey:
    def __init__(self, key):
        self._key = key

    @classmethod
    def from_bytes(cls, data: bytes) -> 'PublicKey':
        key = ec.EllipticCurvePublicKey.from_encoded_point(CURVE, data)
        return cls(key)

    def to_bytes(self, compressed=True) -> bytes:
        fmt = (serialization.Encoding.X962,
               serialization.PublicFormat.CompressedPoint if compressed
               else serialization.PublicFormat.UncompressedPoint)
        return self._key.public_bytes(*fmt)

    def to_address(self) -> str:
        """Derive Obelyth address (Base58Check, version 0x28 → 'N' prefix)."""
        h = hash160(self.to_bytes())
        return b58check_encode(ADDRESS_VER + h)

    def verify(self, message: bytes, signature: bytes) -> bool:
        try:
            self._key.verify(signature, message, ec.ECDSA(hashes.SHA3_256()))
            return True
        except Exception:
            return False


def generate_keypair() -> Tuple[PrivateKey, PublicKey, str]:
    """Generate a new key pair and return (private, public, address)."""
    priv = PrivateKey()
    pub  = priv.public_key()
    addr = pub.to_address()
    return priv, pub, addr


# ── Vesting Lock ───────────────────────────────────────────────────────────────

class VestingSchedule:
    """
    Founder allocation vesting.
    Coins unlock linearly after cliff, over total_months from genesis.
    All parameters are enforced at the consensus layer — no override possible.
    """
    def __init__(
        self,
        founder_address: str,
        total_nxs: float,
        cliff_months: int  = 12,
        total_months: int  = 48,
        genesis_timestamp: int = None,
    ):
        self.founder_address  = founder_address
        self.total_nxs        = total_nxs
        self.cliff_months     = cliff_months
        self.total_months     = total_months
        self.genesis_timestamp = genesis_timestamp or int(__import__('time').time())

    def vested_amount(self, at_timestamp: int) -> float:
        """Return how many NXS are unlocked at the given Unix timestamp."""
        import time
        elapsed_seconds = at_timestamp - self.genesis_timestamp
        elapsed_months  = elapsed_seconds / (30.44 * 86400)   # avg month

        if elapsed_months < self.cliff_months:
            return 0.0

        fraction = min(1.0, elapsed_months / self.total_months)
        return round(self.total_nxs * fraction, 8)

    def locked_amount(self, at_timestamp: int) -> float:
        return round(self.total_nxs - self.vested_amount(at_timestamp), 8)

    def to_dict(self) -> dict:
        return {
            'founder_address' : self.founder_address,
            'total_nxs'       : self.total_nxs,
            'cliff_months'    : self.cliff_months,
            'total_months'    : self.total_months,
            'genesis_timestamp': self.genesis_timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'VestingSchedule':
        return cls(**d)
