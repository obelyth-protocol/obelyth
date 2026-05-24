"""
Smoke runner for compute/verification.py.

Runs against the in-place module so we know it works in the actual repo
layout. No pytest dependency.
"""

import sys
import os
import hashlib
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compute.verification import (
    CHALLENGE_RATE_NEW, CHALLENGE_RATE_SLASHED, CHALLENGE_RATE_TRUSTED,
    FIRST_OFFENCE_SLASH_PCT, SECOND_OFFENCE_SLASH_PCT,
    MIN_STAKE_OBY, SECOND_OFFENCE_BAN_BLOCKS, REFUND_MULTIPLIER,
    ChallengeStatus, MinerTier,
    JobSpec, Challenge, VerificationResult,
    JobValidationError, NoEligibleMinersError,
    validate_job_submission, compute_tier, challenge_rate_for_tier,
    should_challenge, assign_miner,
    VerificationEngine,
)


def _hex(seed): return hashlib.sha256(str(seed).encode()).hexdigest()
def _digest(seed): return "sha256:" + _hex(seed)


def make_job(jid="job-1", payment=10.0, dev="dev1"):
    return JobSpec(
        job_id=jid, developer_addr=dev,
        model_hash=_hex(2), container_digest=_digest(3),
        seed=42, input_payload_hash=_hex(4), input_schema_hash=_hex(5),
        payment_oby=payment,
    )


PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED.append((name, detail))
        print(f"  FAIL  {name}: {detail}")


def section(title):
    print(f"\n--- {title} ---")


# ── Tier classification ──────────────────────────────────────────────────────
section("Tier classification")

check("new tier",
      compute_tier(reputation=1.0, jobs_completed=0, is_banned=False) == MinerTier.NEW)
check("trusted requires rep + jobs",
      compute_tier(reputation=0.96, jobs_completed=500, is_banned=False) == MinerTier.TRUSTED)
check("rep alone not enough",
      compute_tier(reputation=0.96, jobs_completed=50, is_banned=False) == MinerTier.NEW)
check("jobs alone not enough",
      compute_tier(reputation=0.80, jobs_completed=500, is_banned=False) == MinerTier.NEW)
check("slashed tier",
      compute_tier(reputation=0.65, jobs_completed=500, is_banned=False) == MinerTier.SLASHED)
check("banned overrides all",
      compute_tier(reputation=1.0, jobs_completed=500, is_banned=True) == MinerTier.BANNED)
check("exact trusted threshold",
      compute_tier(reputation=0.95, jobs_completed=100, is_banned=False) == MinerTier.TRUSTED)
check("exact slashed cutoff (0.70 not slashed)",
      compute_tier(reputation=0.70, jobs_completed=500, is_banned=False) == MinerTier.NEW)

# ── Challenge rates ──────────────────────────────────────────────────────────
section("Challenge rates")

check("new rate 30%", challenge_rate_for_tier(MinerTier.NEW) == 0.30)
check("trusted rate 5%", challenge_rate_for_tier(MinerTier.TRUSTED) == 0.05)
check("slashed rate 60%", challenge_rate_for_tier(MinerTier.SLASHED) == 0.60)
check("banned uses slashed rate", challenge_rate_for_tier(MinerTier.BANNED) == 0.60)


# ── Determinism enforcement ──────────────────────────────────────────────────
section("Determinism enforcement (validate_job_submission)")

try:
    validate_job_submission(make_job())
    check("valid job passes", True)
except Exception as e:
    check("valid job passes", False, str(e))

for bad_field, mutate in [
    ("model_hash bad", lambda j: setattr(j, "model_hash", "not-a-hash")),
    ("container_digest no prefix", lambda j: setattr(j, "container_digest", "latest")),
    ("container_digest bad hex", lambda j: setattr(j, "container_digest", "sha256:xyz")),
    ("seed negative", lambda j: setattr(j, "seed", -1)),
    ("seed too big", lambda j: setattr(j, "seed", 2**64)),
    ("seed wrong type", lambda j: setattr(j, "seed", "42")),
    ("input_payload_hash bad", lambda j: setattr(j, "input_payload_hash", "short")),
    ("input_schema_hash bad", lambda j: setattr(j, "input_schema_hash", "short")),
    ("zero payment", lambda j: setattr(j, "payment_oby", 0)),
    ("negative payment", lambda j: setattr(j, "payment_oby", -1)),
]:
    j = make_job()
    mutate(j)
    try:
        validate_job_submission(j)
        check(f"rejects: {bad_field}", False, "no exception")
    except JobValidationError:
        check(f"rejects: {bad_field}", True)


# ── Deterministic challenge selection ────────────────────────────────────────
section("Deterministic challenge selection")

block_hash = b"\x00" * 32
d1 = should_challenge(1.0, 500, False, block_hash, "job-x")
d2 = should_challenge(1.0, 500, False, block_hash, "job-x")
check("same inputs → same decision", d1 == d2)

# Frequency check: trusted miner over 5000 jobs should hit ~5%
hits = sum(
    1 for i in range(5000)
    if should_challenge(1.0, 500, False, block_hash, f"job-{i}")
)
rate = hits / 5000
check(f"trusted rate ~5% (got {rate:.3f})", 0.035 < rate < 0.065)

# New miner: ~30%
hits = sum(
    1 for i in range(5000)
    if should_challenge(1.0, 0, False, block_hash, f"job-{i}")
)
rate = hits / 5000
check(f"new rate ~30% (got {rate:.3f})", 0.275 < rate < 0.325)


# ── Stake-weighted assignment ────────────────────────────────────────────────
section("Stake-weighted assignment")

# Deterministic
miners = [
    {"address": f"m{i}", "stake_oby": 10000.0, "is_banned": False}
    for i in range(5)
]
a1 = assign_miner("job-abc", b"\x00" * 32, miners)
a2 = assign_miner("job-abc", b"\x00" * 32, miners)
check("assignment deterministic", a1 == a2)

# Proportionality: 10x stake → ~10x assignments
miners = [
    {"address": "small", "stake_oby": 1000.0, "is_banned": False},
    {"address": "big", "stake_oby": 10000.0, "is_banned": False},
]
counts = Counter()
for i in range(5000):
    bh = hashlib.sha3_256(f"block-{i}".encode()).digest()
    counts[assign_miner(f"job-{i}", bh, miners)] += 1
ratio = counts["big"] / counts["small"]
check(f"10x stake → ~10x (got {ratio:.2f})", 8.0 < ratio < 12.5)

# Banned excluded
miners = [
    {"address": "banned", "stake_oby": 100000.0, "is_banned": True},
    {"address": "ok", "stake_oby": 1000.0, "is_banned": False},
]
chosen = assign_miner("job-x", b"\x00" * 32, miners)
check("banned excluded", chosen == "ok")

# Min stake gate
miners = [
    {"address": "tiny", "stake_oby": MIN_STAKE_OBY - 1, "is_banned": False},
    {"address": "ok", "stake_oby": MIN_STAKE_OBY, "is_banned": False},
]
all_ok = True
for i in range(50):
    bh = hashlib.sha3_256(f"b{i}".encode()).digest()
    if assign_miner(f"job-{i}", bh, miners) != "ok":
        all_ok = False
        break
check("under-staked excluded", all_ok)

# Empty pool raises
try:
    assign_miner("job-x", b"\x00" * 32, [
        {"address": "tiny", "stake_oby": 0, "is_banned": False}
    ])
    check("empty pool raises", False, "no exception")
except NoEligibleMinersError:
    check("empty pool raises", True)


# ── VerificationEngine: end-to-end ───────────────────────────────────────────
section("VerificationEngine end-to-end")

# Track callback invocations
slashes = []
refunds = []
bans = []


def on_slash(miner, job_id, pct, slashed_oby, offence_count):
    slashes.append((miner, job_id, pct, slashed_oby, offence_count))


def on_refund(dev, job_id, amount):
    refunds.append((dev, job_id, amount))


def on_ban(miner, until_block):
    bans.append((miner, until_block))


engine = VerificationEngine(
    on_slash=on_slash,
    on_refund=on_refund,
    on_ban=on_ban,
    block_hash_provider=lambda: b"\x00" * 32,
)

# Register a job
job = make_job(jid="e2e-job-1", payment=10.0, dev="alice")
engine.register_job(job)
check("job registered", "e2e-job-1" in engine._jobs)

# Submit a result — trusted miner, low challenge rate, may or may not challenge
# To force a challenge, use a new miner (30% rate) and try until we get one
challenge_issued = None
for attempt_id in range(50):
    j = make_job(jid=f"force-{attempt_id}", payment=10.0, dev="alice")
    engine.register_job(j)
    result_hash = _hex(99)
    res = engine.submit_result(
        job_id=j.job_id,
        miner_addr="miner-fault",
        miner_rep=1.0, miner_jobs=0, miner_banned=False,   # new miner, 30%
        result_cid="cid-1",
        result_hash=result_hash,
    )
    if res.method == "challenged":
        challenge_issued = engine.pending_challenges()[0]
        break

check("eventually issues a challenge", challenge_issued is not None)

if challenge_issued:
    # Resolve with diverging hash → fault
    cid = challenge_issued.challenge_id
    status = engine.resolve_challenge(
        challenge_id=cid,
        rerun_hash=_hex(100),  # different from result_hash
        stake_oby=10000.0,
        offence_count=0,  # first offence
        current_block=1000,
    )
    check("diverging rerun → FAILED", status == ChallengeStatus.FAILED)
    check("on_slash fired", len(slashes) >= 1)
    if slashes:
        last = slashes[-1]
        check("1st offence 20% slash", last[2] == FIRST_OFFENCE_SLASH_PCT)
        check("slashed amount = stake × 20%", abs(last[3] - 2000.0) < 0.01)
        check("offence_count = 1", last[4] == 1)
    check("on_refund fired", len(refunds) >= 1)
    if refunds:
        # Refund capped at 2 × 10 = 20
        check("refund capped at 2× payment", abs(refunds[-1][2] - 20.0) < 0.01)
    check("no ban on 1st offence", len(bans) == 0)


# 2nd offence: should ban
slashes.clear(); refunds.clear(); bans.clear()
for attempt_id in range(50):
    j = make_job(jid=f"force2-{attempt_id}", payment=10.0, dev="alice")
    engine.register_job(j)
    res = engine.submit_result(
        job_id=j.job_id,
        miner_addr="miner-fault2",
        miner_rep=1.0, miner_jobs=0, miner_banned=False,
        result_cid="cid-2",
        result_hash=_hex(99),
    )
    if res.method == "challenged":
        challenge_issued = engine.pending_challenges()[0]
        # Hack: find the just-issued challenge by job_id
        for c in engine.pending_challenges():
            if c.job_id == j.job_id:
                challenge_issued = c
                break
        break

if challenge_issued:
    cid = challenge_issued.challenge_id
    status = engine.resolve_challenge(
        challenge_id=cid,
        rerun_hash=_hex(100),
        stake_oby=8000.0,         # remaining after 1st slash
        offence_count=1,          # this is the 2nd offence
        current_block=2000,
    )
    check("2nd offence → FAILED", status == ChallengeStatus.FAILED)
    if slashes:
        check("2nd offence 50% slash", slashes[-1][2] == SECOND_OFFENCE_SLASH_PCT)
        check("2nd offence amount = stake × 50%", abs(slashes[-1][3] - 4000.0) < 0.01)
    check("2nd offence triggers ban", len(bans) == 1)
    if bans:
        check("ban until = current + 30 days blocks",
              bans[-1][1] == 2000 + SECOND_OFFENCE_BAN_BLOCKS)


# ── Matching rerun → PASSED, no slash ────────────────────────────────────────
section("Matching rerun")

slashes.clear(); refunds.clear(); bans.clear()
for attempt_id in range(50):
    j = make_job(jid=f"honest-{attempt_id}", payment=10.0, dev="alice")
    engine.register_job(j)
    res = engine.submit_result(
        job_id=j.job_id,
        miner_addr="miner-honest",
        miner_rep=1.0, miner_jobs=0, miner_banned=False,
        result_cid="cid-3",
        result_hash=_hex(99),
    )
    if res.method == "challenged":
        target = None
        for c in engine.pending_challenges():
            if c.job_id == j.job_id:
                target = c
                break
        if target:
            status = engine.resolve_challenge(
                challenge_id=target.challenge_id,
                rerun_hash=_hex(99),  # matches
                stake_oby=10000.0,
                offence_count=0,
                current_block=1000,
            )
            check("matching rerun → PASSED", status == ChallengeStatus.PASSED)
            check("honest miner not slashed", len(slashes) == 0)
            check("honest miner no refund", len(refunds) == 0)
            check("honest miner no ban", len(bans) == 0)
            break


# ── ZK fast path ─────────────────────────────────────────────────────────────
section("ZK fast path")

j = make_job(jid="zk-job", payment=10.0)
engine.register_job(j)
res = engine.submit_result(
    job_id="zk-job",
    miner_addr="m-zk",
    miner_rep=0.5, miner_jobs=0, miner_banned=False,  # would normally trigger 60% challenge
    result_cid="cid-zk",
    result_hash=_hex(50),
    zk_proof="zk-stub::" + _hex(51),
)
check("valid ZK proof → passed", res.passed and res.method == "zk")


# ── stats / get_result / pending ─────────────────────────────────────────────
section("Helpers")

s = engine.stats()
check("stats keys present",
      all(k in s for k in ["total", "pending", "passed", "failed", "expired",
                            "slash_oby", "refund_oby"]))
check("slash_oby key present (no stale NXS)", "slash_oby" in s)

r = engine.get_result("zk-job")
check("get_result returns ZK pass", r is not None and r.passed)


# ── Final report ─────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  PASSED: {PASSED}")
print(f"  FAILED: {len(FAILED)}")
if FAILED:
    print(f"\nFailures:")
    for name, detail in FAILED:
        print(f"  - {name}: {detail}")
    sys.exit(1)
print(f"  All checks green. Module ready to ship.")
sys.exit(0)
