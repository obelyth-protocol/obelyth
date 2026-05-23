# Security Policy

## Reporting a Vulnerability

**Email:** security@obelyth.io
**Response time:** 72 hours to acknowledge, 14 days to fix critical issues
**Bug bounty:** Funded from DAO OBY vault — no hard cap

Please do not open a public GitHub issue for security vulnerabilities.

## Severity Tiers & Awards

| Severity | Definition | Award |
|---|---|---|
| **Critical** | Consensus break, fund theft, supply cap bypass, permanent network halt | Up to 50,000 OBY |
| **High** | Network disruption, economic attack enabling significant profit, privacy breach | 10,000–25,000 OBY |
| **Medium** | Degraded performance, minor economic imbalance, non-critical logic error | 2,000–5,000 OBY |
| **Low** | Code quality issues, best practice deviations, non-exploitable edge cases | 250–1,000 OBY |
| **Informational** | Documentation errors, minor suggestions | 0–250 NRN |

All awards vest linearly over 6 months from the date of issue. Awards are paid in NRN from the DAO vault — no fixed budget ceiling.

## Responsible Disclosure Policy

1. **Report privately** to security@obelyth.io before any public disclosure
2. **Include** a description of the vulnerability, affected components, steps to reproduce, and proof-of-concept if possible
3. **Allow 72 hours** for acknowledgement
4. **Allow 14 days** for a fix to be developed and deployed for critical issues
5. **Coordinate disclosure** timing with the team before publishing

Researchers who follow this policy and are first to report a valid vulnerability receive the full award. Public disclosure before a fix is deployed forfeits the award entirely.

## Scope

**In scope:**
- `core/` — consensus, UTXO, block validation, cryptography
- `network/` — P2P protocol, peer discovery
- `tokenomics/` — AMM, fee routing, economic logic
- `sdk/` — developer SDK, API authentication
- `payments/` — deposit watching, settlement, balance management
- `node/` — RPC API, governance, bounty, OpCo
- Economic attacks on the AMM, fee split, or vesting schedule

**Out of scope:**
- Denial-of-service attacks requiring significant resources
- Social engineering of team members
- Physical security
- Issues in third-party dependencies (report to those projects directly)

## Bug Bounty Program

**Testnet phase:** Report directly to security@obelyth.io. Rewards paid from the pre-mainnet community pool.

**Post-mainnet:** Full program hosted on [Immunefi](https://immunefi.com/bounty/obelyth). Funded from DAO OBY vault on an ongoing basis — no hard cap.

The DAO OBY vault accumulates 5% of all miner earnings in perpetuity. Security is funded by the network's success.
