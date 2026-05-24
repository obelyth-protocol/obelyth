<div align="center">

# Obelyth

**Proof-of-Useful-Work blockchain for decentralized AI compute**

[![License: MIT](https://img.shields.io/badge/License-MIT-gold.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://python.org)
[![Testnet: Coming Soon](https://img.shields.io/badge/Testnet-Coming%20Soon-yellow.svg)](https://obelyth.io)
[![Discord](https://img.shields.io/badge/Discord-Join-7289da.svg)](https://discord.gg/xppWjgYnT)

[Website](https://obelyth.io) · [Whitepaper](https://obelyth.io/whitepaper) · [Docs](https://docs.obelyth.io) · [Explorer](https://explorer.obelyth.io)

*The obelisk channels compute into intelligence — GPU work becomes verifiable AI inference.*

</div>

---

## What is Obelyth?

Obelyth replaces purposeless hash-grinding with verifiable AI compute. Miners earn OBY by running real inference jobs and fine-tuning language models — not by solving arbitrary puzzles. Developers pay in stablecoins (USDC, DAI, USDT, EURC) and get GPU compute at roughly 56% below AWS pricing.

90% of every developer fee is permanently locked into a diversified stablecoin AMM reserve, which continuously deepens the protocol's liquidity infrastructure as usage grows.

**Core properties:**
- 21,000,000 OBY hard cap — inviolable, no governance override
- 92% of supply earned through mining — zero VC allocation, ever
- Constant-product AMM — protocol liquidity infrastructure from day one
- Diversified reserve basket — USDC 40% / DAI 35% / USDT 15% / EURC 10%
- HuggingFace drop-in — two lines of code to integrate

---

## Quick Start

### Run a node (testnet)

```bash
git clone https://github.com/obelyth-protocol/obelyth
cd obelyth
python launch.py
```

Requires Python 3.10+. The launcher installs dependencies, generates your key, starts the node, and opens the wallet UI automatically.

### Use the SDK

```bash
pip install obelyth-sdk
```

```python
from obelyth import ObelythClient

client = ObelythClient(api_key="oby_your_key_here")

# Drop-in replacement for HuggingFace pipeline
pipe = client.pipeline("text-generation", model="meta-llama/Llama-3-8B")
result = pipe("Explain proof-of-useful-work in one sentence")[0]
print(result.generated_text)
```

Get an API key and testnet OBY at [obelyth.io/faucet](https://obelyth.io/faucet).

---

## Repository Structure

```
obelyth/
├── core/                   # Blockchain engine
│   ├── blockchain.py       # DAG chain, UTXO set, consensus, DAO tax enforcement
│   ├── structures.py       # Block, Transaction, TxOutput, BlockHeader, DAG
│   └── crypto.py           # SHA3-256, ECDSA secp256k1, Base58Check, vesting
│
├── network/                # P2P layer
│   └── p2p.py              # TCP peer discovery, gossip, block sync
│
├── node/                   # Full node and governance
│   ├── fullnode.py         # Node entrypoint, HTTP JSON-RPC API, auto-miner
│   ├── governance.py       # Three-phase progressive governance engine
│   ├── testnet.py          # Pre-mainnet community tracker — all 8 roles
│   ├── bounty.py           # Bug bounty program, severity tiers, DAO-funded
│   └── opco.py             # Operations Company — DAO → legal entity bridge
│
├── tokenomics/             # Economic engine
│   └── engine.py           # Multi-stablecoin AMM, fee routing, OBY vault
│
├── compute/                # GPU compute layer
│   ├── miner.py            # GPU miner daemon, job queue, vLLM integration
│   └── verification.py     # Optimistic verification + ZK proof stubs
│
├── sdk/                    # Developer SDK
│   └── obelyth.py          # HuggingFace drop-in, balance checks, job submission
│
├── payments/               # Payment infrastructure
│   ├── manager.py          # Payment lifecycle orchestration
│   ├── deposit_watcher.py  # EVM deposit monitoring (Ethereum/Base/Polygon/Arbitrum)
│   ├── settlement.py       # Batch 90/5/5 settlement engine
│   ├── notifications.py    # Email + webhook + SDK balance warnings
│   └── gas_manager.py      # Gas reserve, multicall batcher, Base chain distribution
│
├── accounts/               # Developer account system
│   └── registry.py         # Registration, API keys, deposit address derivation
│
├── wallet/                 # HD wallet
│   └── wallet.py           # Key derivation, address generation, tx builder
│
├── cli/                    # Command line interface
│   └── obelyth.py          # CLI tool
│
├── docs/                   # Documentation
│   └── RUST_PORT.md        # Rust port implementation guide
├── tests/                  # Test suite
├── scripts/                # Utility scripts
├── config/                 # Configuration templates
│   └── miner.example.toml  # Miner config — copy to miner.toml and edit
└── launch.py               # Cross-platform testnet launcher
```

---

## Token Economics

| Allocation | % | OBY | Notes |
|---|---|---|---|
| Mined supply | 92% | ~19,320,000 | Block rewards + compute job rewards |
| Pre-mainnet community | 3% | 630,000 | Early miners, devs, validators, security researchers |
| Year 1 DAO discretionary | 2% | 420,000 | Grants and ecosystem, DAO-governed |
| Founder | 3% | 630,000 | 12-month cliff, 48-month linear vest |
| **VC allocation** | **0%** | **0** | **None. Ever.** |

### Fee split — constitutional, cannot be changed by governance

| Destination | % | Mechanism |
|---|---|---|
| Liquidity reserve | 90% | Hard-locked in diversified stablecoin AMM |
| Creator Share | 5% | Permanent protocol fee — auditable at `/treasury` RPC |
| DAO fund | 5% | Governance-controlled stablecoins |

**DAO mining tax:** 5% of all OBY mined is automatically redirected to the DAO vault at the consensus layer. Enforced in every coinbase transaction.

---

## Earn on Testnet

The 3% pre-mainnet community pool (630,000 OBY) rewards everyone who builds and hardens the network before mainnet. All roles compete on the same leaderboard. No single role dominates — a great documentation writer earns as much as a good GPU miner.

| Role | What you do | How to join |
|---|---|---|
| **Validator** | Run a validator node, sign blocks | [obelyth.io/validate](https://obelyth.io/validate) |
| **GPU Miner** | Run AI compute jobs on your GPU | [obelyth.io/mine](https://obelyth.io/mine) |
| **AI Developer** | Use the SDK, test models, give feedback | [obelyth.io/dev](https://obelyth.io/dev) |
| **Code Contributor** | Improve the node, SDK, or tooling | Open a PR on this repo |
| **Documentation** | Write tutorials, guides, translations | [docs.obelyth.io/contribute](https://docs.obelyth.io/contribute) |
| **Data Scientist** | Build economic models and dashboards | [obelyth.io/community](https://obelyth.io/community) |
| **Security Researcher** | Find and disclose vulnerabilities | [security@obelyth.io](mailto:security@obelyth.io) |
| **Community** | Grow and support the community | [discord.gg/xppWjgYnT](https://discord.gg/xppWjgYnT) |

**Max per participant: 15,000 OBY.** All allocations vest 6 months from mainnet genesis.

[View the live leaderboard →](https://obelyth.io/leaderboard)

---

## Running a Validator Node

**Requirements:** Python 3.10+, 2GB RAM (8GB recommended), 20GB disk, stable internet, 1,000 OBY staked (free from the faucet during testnet).

```bash
git clone https://github.com/obelyth-protocol/obelyth
cd obelyth
pip install -r requirements.txt
python -m node.fullnode --port 8333 --rpc-port 8334
```

Register at [obelyth.io/validate](https://obelyth.io/validate) to appear on the leaderboard.

---

## Running a GPU Miner

**Requirements:** NVIDIA GPU 8GB+ VRAM, CUDA 12.x, Python 3.10+, [vLLM](https://github.com/vllm-project/vllm).

```bash
git clone https://github.com/obelyth-protocol/obelyth
cd obelyth
pip install -r requirements.txt
pip install vllm

cp config/miner.example.toml config/miner.toml
# Edit miner.toml — add your OBY address and GPU settings

python -m compute.miner --config config/miner.toml
```

[View miner dashboard →](https://obelyth.io/miner)

---

## Security

Bug bounty funded from the DAO OBY vault — no hard cap.

| Severity | Definition | Award |
|---|---|---|
| Critical | Consensus break, fund theft, supply inflation | Up to 50,000 OBY |
| High | Network disruption, economic attack | 10,000–25,000 OBY |
| Medium | Degraded performance, minor economic issue | 2,000–5,000 OBY |
| Low | Code quality, best practice deviations | 250–1,000 OBY |

**Report:** [security@obelyth.io](mailto:security@obelyth.io) — see [SECURITY.md](SECURITY.md) for full policy.

---

## Architecture

```
Developer (USDC / DAI / USDT / EURC)
    │
    ▼
Unique Deposit Address (per developer, per chain)
    │
    ▼
Deposit Watcher (Ethereum · Base · Polygon · Arbitrum)
    │
    ▼
Internal Balance Ledger
    │
    ├── Job submitted → Miner selected (reputation score)
    │                        │
    │                        ▼
    │               GPU runs AI job via vLLM
    │                        │
    │                        ▼
    │               Result returned → Fee charged
    │
    └── Daily settlement (Base chain, Multicall3)
            → 90% AMM liquidity pool
            →  5% Creator Share wallet
            →  5% DAO multisig

Miner block reward (50 OBY gross)
    ├── 47.5 OBY → Miner wallet
    └──  2.5 OBY → DAO vault (5% constitutional tax)
```

---

## Roadmap

Mainnet launches on Rust. No calendar override — only when the Rust node has run as primary testnet node for 60+ consecutive stable days.

| Phase | Milestone | Target |
|---|---|---|
| **Soft launch** | Python reference node, AMM, SDK, wallet, community tracker | Now |
| **Legal + Rust starts** | Cayman Foundation, securities opinion — Rust development begins | Month 1–2 |
| **Public testnet** | All 8 community roles, GPU onboarding, vLLM, explorer, faucet | Month 2–3 |
| **Rust on testnet** | Rust node joins alongside Python — both must agree on every block | Month 3–4 |
| **Rust primary** | Rust becomes primary. 60-day stability clock starts. | Month 5–6 |
| **Mainnet** | Genesis block on Rust. Vesting begins. First real compute jobs. | Month 6–8 |
| **DAO transition** | On-chain governance, steering committee elected | Month 12 |
| **ZK verification** | Groth16 ZK proofs replace optimistic job verification | Month 12–18 |
| **Exchange listing** | External DEX/CEX. Internal AMM continues. | Month 12–18 |

---

## Testnet Graduation Criteria

Mainnet launches when **all four** conditions are simultaneously true:

- [ ] Rust node running as primary for 60+ consecutive stable days
- [ ] 60 consecutive stable days with no critical bug
- [ ] 10+ independent validators across 3+ countries
- [ ] 5+ developers with completed AI compute SDK jobs

[Current testnet status →](https://obelyth.io/testnet)

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Every merged PR earns points in the pre-mainnet community tracker toward your genesis OBY allocation.

**High-priority areas:**
- Rust port (`core/`, `network/`) — see [docs/RUST_PORT.md](docs/RUST_PORT.md)
- vLLM integration testing (`compute/miner.py`)
- JavaScript/TypeScript SDK
- Network explorer frontend
- Documentation and tutorials

---

## Grants

Actively applying for ecosystem grants to fund legal structure, security review, and core engineering. If you represent a foundation interested in decentralized AI compute infrastructure: [grants@obelyth.io](mailto:grants@obelyth.io)

Current targets: Filecoin/Protocol Labs ProPGF · Arbitrum Foundation · Base Builder · Uniswap Foundation · Ethereum Foundation ESP

---

## License

MIT — see [LICENSE](LICENSE).

---

<div align="center">

| | |
|---|---|
| Website | [obelyth.io](https://obelyth.io) |
| Docs | [docs.obelyth.io](https://docs.obelyth.io) |
| Explorer | [explorer.obelyth.io](https://explorer.obelyth.io) |
| Whitepaper | [obelyth.io/whitepaper](https://obelyth.io/whitepaper) |
| Discord | [discord.gg/xppWjgYnT](https://discord.gg/xppWjgYnT) |
| Twitter/X | [@Obelyth_Chain](https://x.com/Obelyth_Chain) |
| Security | [security@obelyth.io](mailto:security@obelyth.io) |
| General | [hello@obelyth.io](mailto:hello@obelyth.io) |

<br>
<sub>Built with zero VC funding. 92% of OBY earned through work.</sub>

</div>
