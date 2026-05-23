# Rust Port — Implementation Guide

The Python node is the reference implementation. The Rust port is not an optional upgrade — it is a **mainnet requirement** and development must begin at Month 1-2, in parallel with testnet.

## Why Rust Must Run on Testnet First

Launching mainnet on code that was never battle-tested at network scale is how projects get exploited. The plan is:

1. Python node runs testnet from day one
2. Rust node joins testnet in Month 3-4 alongside Python
3. Both nodes cross-validate — they must agree on every block and transaction
4. Any consensus divergence is a bug caught on testnet, not mainnet
5. Rust node becomes primary at Month 5-6
6. Mainnet launches only after 60 consecutive stable days of Rust-primary testnet

The 60-day clock resets if any consensus bug is found. This is a public commitment.

## Priority Order

Port each layer in this order. Each must be tested before the next begins.

| Priority | Module | Python Reference | Notes |
|---|---|---|---|
| 1 | Cryptography | `core/crypto.py` | SHA3-256, secp256k1, Base58Check, vesting schedule |
| 2 | Data structures | `core/structures.py` | Block, Transaction, TxOutput, BlockHeader, DAG |
| 3 | UTXO set | `core/blockchain.py` | UTXOSet, validation, coinbase rules |
| 4 | Consensus / Mining | `core/blockchain.py` | PoW, PoS, difficulty adjustment, DAO tax enforcement |
| 5 | P2P Network | `network/p2p.py` | TCP, peer discovery, gossip, sync |
| 6 | RPC API | `node/fullnode.py` | HTTP JSON-RPC — maintain same endpoints |
| 7 | Tokenomics | `tokenomics/engine.py` | AMM, fee routing — can stay Python longer |

## Key Differences from Python

- Replace ECDSA secp256k1 with **CRYSTALS-Dilithium** (post-quantum)
  - Use the `pqcrypto` crate or `crystals-dilithium` crate
- Use **RocksDB** for UTXO storage instead of in-memory dict
  - Use the `rocksdb` crate
- All cryptographic operations must be **constant-time**
- Consensus validation must be **deterministic** across platforms

## Crate Recommendations

```toml
[dependencies]
sha3 = "0.10"
secp256k1 = "0.28"        # Phase 1 — replace with Dilithium in Phase 2
rocksdb = "0.21"
tokio = { version = "1", features = ["full"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
hex = "0.4"
```

## Testing

Every Rust module must pass cross-validation against the Python reference:
generate the same block hashes, the same addresses, the same UTXO states
given the same inputs. A cross-language test suite is in `tests/cross_validation/`.
