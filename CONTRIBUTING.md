# Contributing to Obelyth

Thank you for contributing. Every merged PR earns points in the pre-mainnet community tracker toward your testnet genesis NRN allocation. See the [leaderboard](https://obelyth.io/leaderboard) to track your progress.

## Getting Started

```bash
git clone https://github.com/obelyth-protocol/obelyth
cd obelyth
pip install -r requirements.txt
pip install -r requirements-dev.txt
python launch.py   # starts a local testnet node
```

## Areas Most Needed

| Area | Files | Priority |
|---|---|---|
| **Rust port** | `core/`, `network/` | Highest — see `docs/RUST_PORT.md` |
| **vLLM integration** | `compute/miner.py` | High — needs GPU end-to-end test |
| **JS/TS SDK** | `sdk/` | High — port of `sdk/norn.py` |
| **Network explorer** | New | High — React frontend |
| **Tests** | `tests/` | Medium — coverage is low |
| **Documentation** | `docs/` | Medium — tutorials, translations |

## How to Contribute

1. **Check existing issues** — look for `good first issue` labels
2. **Open an issue** before starting large changes to discuss approach
3. **Fork and branch** — `git checkout -b your-feature-name`
4. **Write tests** for new functionality
5. **Run the test suite** — `python -m unittest discover tests/`
6. **Open a pull request** with a clear description

## Code Standards

- Python 3.10+ with type hints on all public functions
- Follow existing naming conventions — `snake_case` for functions, `UPPER_CASE` for constants
- Every module needs a docstring explaining its purpose
- No new dependencies outside `requirements.txt` without discussion first
- Use `log = logging.getLogger('obelyth.yourmodule')` — not `print()`

## Commit Messages

```
type: short description (under 72 chars)

Optional explanation. What changed and why.
Refs: #123
```

Types: `feat` · `fix` · `docs` · `test` · `refactor` · `perf` · `chore`

## Earning Testnet NRN

Your contributions are tracked automatically:

- **PRs merged** — base points scaled by complexity score (assigned by maintainers)
- **Reviews given** — reviewing other contributors' PRs earns points too
- **Issues resolved** — confirmed bug fixes earn bonus points
- **Areas covered** — contributing across multiple areas earns a diversity bonus

**Max allocation: 15,000 OBY** per contributor. Vests 6 months from mainnet genesis.

Register your testnet address at [obelyth.io/contribute](https://obelyth.io/contribute) to ensure contributions are tracked.

## Questions

- **Discord:** [discord.gg/xppWjgYnT](https://discord.gg/xppWjgYnT) — `#dev` channel
- **Email:** [hello@obelyth.io](mailto:hello@obelyth.io)
