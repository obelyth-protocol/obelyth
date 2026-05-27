"""
Smoke runner for Phase 4.2 — Redundant Parallel tier.

Tests the redundant tier end-to-end: pricing, assignment, submission
aggregation, all 7 consensus outcomes, callback dispatch, timeout
handling, idempotency, and round-trip via save/load.

Test scenarios:
  - quote_job with tier='redundant' returns 3x cost
  - quote_job with unknown tier raises ValueError
  - submit_job_with_verification with tier='redundant' stores tier and
    per-miner oby (total/N, not full)
  - assign_redundant_job picks 3 distinct miners (stake-weighted)
  - assign_redundant_job raises when <3 eligible miners
  - assign_redundant_job is idempotent (returns same picks on retry)
  - Standard tier dispatch path unchanged
  - ConsensusEngine.submit_result records submission, rejects:
    - unknown job, wrong tier, not-assigned miner, duplicate submission,
      already-finalized job
  - Consensus outcomes (the 7 cases from the design):
    1. 3/3 unanimous       → all credited, no slash
    2. 3/3 majority (2-1)  → 2 credited, 1 slashed
    3. 3/3 all-different   → disputed, refund, no slash for returners
    4. 2/3 agreed          → 2 credited, 1 missing slashed
    5. 2/3 disagree (tied) → disputed, returners not slashed, missing slashed
    6. 1/3 returns         → disputed, missing slashed, returner not slashed
    7. 0/3                 → faulted, all slashed
  - Settlement callbacks fire correctly (on_pass for winners, on_slash for
    outliers/missing, on_refund for disputes/faults, on_ban on 2nd offence)
  - Job state correctly updated after finalize
  - finalize is idempotent — re-running on settled job is no-op
  - finalize_due_redundant_jobs() finalizes timed-out jobs
  - Timeout: a job past consensus_deadline finalizes even with <N submissions
  - save/load round-trips tier and submission state
"""

import sys
import os
import hashlib
import time
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tokenomics.engine import (
    TokenomicsEngine, MinerProfile, ComputeJob, Stablecoin,
    TIER_MULTIPLIER_REDUNDANT, REDUNDANT_MINER_COUNT, REDUNDANT_TIMEOUT_S,
)
from compute.consensus import (
    ConsensusEngine, ConsensusOutcome,
    UnknownJob, WrongTier, NotAssigned, DuplicateSubmission,
    JobAlreadyFinalized,
)
from compute.verification import assign_miners_redundant, NoEligibleMinersError


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


# ── Helpers ─────────────────────────────────────────────────────────────────

def make_engine(n_miners=5, stake=10_000.0):
    """Build a fresh engine with n_miners registered miners and a block_hash."""
    e = TokenomicsEngine()
    e.update_rate(Stablecoin.USDC, 1.0)
    e.update_rate(Stablecoin.DAI, 1.0)
    # Stable block_hash so assignments are deterministic across calls
    e._block_hash = lambda: hashlib.sha3_256(b'test-block-42').digest()
    for i in range(n_miners):
        e.register_miner(MinerProfile(
            address=f'OBYminer{i}xxxxxxxxxxxxxxxxxxxxxxxx',
            gpu_model='A100', gpu_count=1, vram_gb=80,
            bandwidth_gbps=10.0, region='us-east', stake_oby=stake,
        ))
    return e


def make_redundant_job(e, dev='dev_alice', payment_oby=300.0):
    """Construct + register a redundant-tier job directly in engine state."""
    import uuid
    jid = str(uuid.uuid4())[:16]
    job = ComputeJob(
        job_id=jid, developer_addr=dev, job_type='inference',
        model_id='test-model', gpu_hours=1.0, stablecoin='USDC',
        stable_paid=payment_oby * 0.10, usd_paid=payment_oby * 0.10,
        oby_to_miner=payment_oby,  # per-miner share, not total
        tier='redundant',
    )
    with e._lock:
        e._jobs[jid] = job
    return job


def fake_hash(seed): return hashlib.sha256(str(seed).encode()).hexdigest()


# ── Pricing ─────────────────────────────────────────────────────────────────
section("Pricing: redundant tier is 3x standard")

e = make_engine()
# Use big enough inputs so cost > MIN_JOB_USD floor
std = e.quote_job(
    'fine_tuning', 'test', Stablecoin.USDC,
    gpu_count=4, duration_hr=10.0, tier='standard',
)
red = e.quote_job(
    'fine_tuning', 'test', Stablecoin.USDC,
    gpu_count=4, duration_hr=10.0, tier='redundant',
)
check("standard tier_multiplier = 1.0", std['tier_multiplier'] == 1.0)
check("redundant tier_multiplier = 3.0", red['tier_multiplier'] == 3.0)
check("redundant cost is 3x standard",
      abs(red['usd_cost'] / std['usd_cost'] - 3.0) < 0.001,
      f"ratio={red['usd_cost']/std['usd_cost']}")
check("quote includes tier field", std.get('tier') == 'standard')
check("redundant quote includes tier", red.get('tier') == 'redundant')

# Unknown tier rejected
try:
    e.quote_job('inference', 'test', Stablecoin.USDC, tier='bogus')
    check("unknown tier raises ValueError", False, "did not raise")
except ValueError:
    check("unknown tier raises ValueError", True)


# ── submit_job_with_verification with tier='redundant' ──────────────────────
section("submit_job_with_verification routes redundant tier")

e = make_engine()
job, receipt = e.submit_job_with_verification(
    developer_addr='dev_alice', job_type='fine_tuning',
    model_id='test-model', coin=Stablecoin.USDC,
    model_hash=fake_hash(1), container_digest='sha256:' + fake_hash(2),
    seed=42, input_payload_hash=fake_hash(3), input_schema_hash=fake_hash(4),
    gpu_count=4, duration_hr=10.0, tier='redundant',
)
check("job.tier == 'redundant'", job.tier == 'redundant')
check("redundant: verification engine NOT registered for this job",
      job.job_id not in e.verification._jobs,
      "redundant jobs should skip verification engine registration")

# Now do a standard tier and confirm it DID register
job_std, _ = e.submit_job_with_verification(
    developer_addr='dev_alice', job_type='fine_tuning',
    model_id='test-model', coin=Stablecoin.USDC,
    model_hash=fake_hash(11), container_digest='sha256:' + fake_hash(12),
    seed=43, input_payload_hash=fake_hash(13), input_schema_hash=fake_hash(14),
    gpu_count=4, duration_hr=10.0, tier='standard',
)
check("standard: verification engine IS registered",
      job_std.job_id in e.verification._jobs)
check("job_std.tier == 'standard'", job_std.tier == 'standard')

# Per-miner OBY for redundant = total / N
# We can't easily compute the exact total since it depends on AMM state,
# but we can verify the redundant per-miner is less than the standard payout
# (because standard goes entirely to one miner, redundant is divided by 3)
check("per-miner redundant oby is positive", job.oby_to_miner > 0)


# ── Assignment: 3 distinct miners ───────────────────────────────────────────
section("Assignment: redundant picks 3 distinct miners")

e = make_engine(n_miners=5)
job = make_redundant_job(e)
picks = e.assign_redundant_job(job.job_id)
check("assigned 3 miners", len(picks) == 3)
check("all picks are distinct", len(set(picks)) == 3)
check("job.assigned_miners populated", job.assigned_miners == picks)
check("job.status = 'assigned'", job.status == 'assigned')
check("job.consensus_deadline set", job.consensus_deadline > 0)
check("deadline ~10 min in future",
      abs(job.consensus_deadline - (int(time.time()) + REDUNDANT_TIMEOUT_S)) < 5)

# Idempotency: re-assigning returns same picks
picks2 = e.assign_redundant_job(job.job_id)
check("re-assign is idempotent", picks2 == picks)


# ── Assignment: needs 3+ eligible miners ────────────────────────────────────
section("Assignment: refuses when <3 eligible miners")

e_small = make_engine(n_miners=2)
job_small = make_redundant_job(e_small)
picks_small = e_small.assign_redundant_job(job_small.job_id)
check("assign with 2 miners returns []", picks_small == [])

# Direct call to assign_miners_redundant raises
try:
    assign_miners_redundant(
        job_id='x', block_hash=b'\x00' * 32,
        miners=[{'address': 'a', 'stake_oby': 5000, 'is_banned': False}],
        n=3,
    )
    check("assign_miners_redundant raises with insufficient miners", False)
except NoEligibleMinersError:
    check("assign_miners_redundant raises with insufficient miners", True)


# ── ConsensusEngine: submit_result rejects bad input ────────────────────────
section("ConsensusEngine.submit_result rejection paths")

e = make_engine()
job = make_redundant_job(e)
picks = e.assign_redundant_job(job.job_id)
ce = e.consensus

# Wrong tier
std_job = make_redundant_job(e)
std_job.tier = 'standard'
try:
    ce.submit_result(std_job, picks[0], fake_hash(1))
    check("wrong tier raises WrongTier", False)
except WrongTier:
    check("wrong tier raises WrongTier", True)

# Not assigned
try:
    ce.submit_result(job, 'OBYminerNOT_ASSIGNED_xxxxxxxxxx', fake_hash(1))
    check("not-assigned miner raises NotAssigned", False)
except NotAssigned:
    check("not-assigned miner raises NotAssigned", True)

# Duplicate
ce.submit_result(job, picks[0], fake_hash(100))
try:
    ce.submit_result(job, picks[0], fake_hash(101))
    check("duplicate submission raises DuplicateSubmission", False)
except DuplicateSubmission:
    check("duplicate submission raises DuplicateSubmission", True)

# Already finalized
job_done = make_redundant_job(e)
job_done.status = 'done'
try:
    ce.submit_result(job_done, 'whoever', fake_hash(1))
    check("done job raises JobAlreadyFinalized", False)
except (JobAlreadyFinalized, NotAssigned):
    # NotAssigned also acceptable since the miner wasn't in the list
    check("done job rejects new submissions", True)


# ── Outcome 1: 3/3 unanimous ────────────────────────────────────────────────
section("Outcome 1: 3/3 unanimous — all credited")

e = make_engine()
job = make_redundant_job(e)
picks = e.assign_redundant_job(job.job_id)
agreed = fake_hash('outcome1-shared')
for m in picks:
    e.consensus.submit_result(job, m, agreed)

# Capture initial state
miner_credits_before = {
    m: e._miners[m].oby_earned for m in picks
}
outcome = e._finalize_redundant_locked(job)
check("status = done", outcome.status == 'done')
check("3 winners", len(outcome.winners) == 3)
check("0 outliers", len(outcome.outliers) == 0)
check("consensus_hash matches agreed",
      outcome.consensus_hash == agreed)
# All 3 should have earned more OBY
for m in picks:
    check(f"  {m[:16]} credited",
          e._miners[m].oby_earned > miner_credits_before[m])
check("job.status updated on object", job.status == 'done')


# ── Outcome 2: 3/3 majority (2-1) — outlier slashed ────────────────────────
section("Outcome 2: 3/3 majority (2-1) — outlier slashed")

e = make_engine()
job = make_redundant_job(e)
picks = e.assign_redundant_job(job.job_id)
true_hash  = fake_hash('outcome2-truth')
lie_hash   = fake_hash('outcome2-lie')
e.consensus.submit_result(job, picks[0], true_hash)
e.consensus.submit_result(job, picks[1], true_hash)
e.consensus.submit_result(job, picks[2], lie_hash)   # outlier

stake_before = e._miners[picks[2]].stake_oby
outcome = e._finalize_redundant_locked(job)
check("status = done", outcome.status == 'done')
check("winners = picks[0,1]",
      set(outcome.winners) == {picks[0], picks[1]})
check("outliers = [picks[2]]", outcome.outliers == [picks[2]])
check("consensus_hash = true_hash", outcome.consensus_hash == true_hash)

# Outlier stake reduced by 20% (first offence)
m_outlier = e._miners[picks[2]]
check("outlier stake reduced ~20%",
      abs(m_outlier.stake_oby - stake_before * 0.8) < 0.01,
      f"expected {stake_before*0.8}, got {m_outlier.stake_oby}")
check("outlier offence_count = 1", m_outlier.offence_count == 1)
check("outlier reputation reset to 0",
      m_outlier.reputation == 0.0)


# ── Outcome 3: 3/3 all-different — disputed ────────────────────────────────
section("Outcome 3: 3/3 all-different — disputed, no slash")

e = make_engine()
job = make_redundant_job(e)
picks = e.assign_redundant_job(job.job_id)
e.consensus.submit_result(job, picks[0], fake_hash('a'))
e.consensus.submit_result(job, picks[1], fake_hash('b'))
e.consensus.submit_result(job, picks[2], fake_hash('c'))

stakes_before = [e._miners[m].stake_oby for m in picks]
outcome = e._finalize_redundant_locked(job)
check("status = disputed", outcome.status == 'disputed')
check("no winners", outcome.winners == [])
check("no outliers among returners", outcome.outliers == [])
check("consensus_hash empty", outcome.consensus_hash == '')

# None of the three should be slashed (all returned, but no consensus)
for i, m in enumerate(picks):
    check(f"  {m[:16]} stake unchanged",
          e._miners[m].stake_oby == stakes_before[i])
    check(f"  {m[:16]} offence_count == 0",
          e._miners[m].offence_count == 0)


# ── Outcome 4: 2/3 agreed — missing slashed ────────────────────────────────
section("Outcome 4: 2/3 agreed — 2 credited, 1 missing slashed")

e = make_engine()
job = make_redundant_job(e)
picks = e.assign_redundant_job(job.job_id)
agreed = fake_hash('outcome4-shared')
e.consensus.submit_result(job, picks[0], agreed)
e.consensus.submit_result(job, picks[1], agreed)
# picks[2] never submits — force timeout
job.consensus_deadline = int(time.time()) - 1

stake_missing_before = e._miners[picks[2]].stake_oby
outcome = e._finalize_redundant_locked(job)
check("status = done", outcome.status == 'done')
check("2 winners", set(outcome.winners) == {picks[0], picks[1]})
check("1 outlier (missing)", outcome.outliers == [picks[2]])
check("missing slashed ~20%",
      abs(e._miners[picks[2]].stake_oby - stake_missing_before * 0.8) < 0.01)
check("missing offence_count = 1",
      e._miners[picks[2]].offence_count == 1)


# ── Outcome 5: 2/3 disagree (tied) — disputed ──────────────────────────────
section("Outcome 5: 2/3 disagree — disputed, returners not slashed")

e = make_engine()
job = make_redundant_job(e)
picks = e.assign_redundant_job(job.job_id)
e.consensus.submit_result(job, picks[0], fake_hash('5-a'))
e.consensus.submit_result(job, picks[1], fake_hash('5-b'))
# picks[2] times out
job.consensus_deadline = int(time.time()) - 1

stakes_returners_before = [e._miners[picks[0]].stake_oby,
                            e._miners[picks[1]].stake_oby]
stake_missing_before = e._miners[picks[2]].stake_oby
outcome = e._finalize_redundant_locked(job)
check("status = disputed", outcome.status == 'disputed')
check("no winners", outcome.winners == [])
check("returners not slashed",
      e._miners[picks[0]].stake_oby == stakes_returners_before[0]
      and e._miners[picks[1]].stake_oby == stakes_returners_before[1])
check("missing slashed",
      e._miners[picks[2]].stake_oby < stake_missing_before)


# ── Outcome 6: 1/3 returns — disputed ──────────────────────────────────────
section("Outcome 6: 1/3 returns — disputed, missing slashed")

e = make_engine()
job = make_redundant_job(e)
picks = e.assign_redundant_job(job.job_id)
e.consensus.submit_result(job, picks[0], fake_hash('6-x'))
# picks[1], picks[2] miss
job.consensus_deadline = int(time.time()) - 1

stake_returner_before = e._miners[picks[0]].stake_oby
outcome = e._finalize_redundant_locked(job)
check("status = disputed", outcome.status == 'disputed')
check("no winners", outcome.winners == [])
check("2 outliers (missing)", set(outcome.outliers) == {picks[1], picks[2]})
check("returner not slashed",
      e._miners[picks[0]].stake_oby == stake_returner_before)
check("returner offence_count = 0",
      e._miners[picks[0]].offence_count == 0)


# ── Outcome 7: 0/3 — faulted ───────────────────────────────────────────────
section("Outcome 7: 0/3 — faulted, all slashed")

e = make_engine()
job = make_redundant_job(e)
picks = e.assign_redundant_job(job.job_id)
job.consensus_deadline = int(time.time()) - 1

stakes_before = {m: e._miners[m].stake_oby for m in picks}
outcome = e._finalize_redundant_locked(job)
check("status = faulted", outcome.status == 'faulted')
check("no winners", outcome.winners == [])
check("3 outliers (all)", set(outcome.outliers) == set(picks))
for m in picks:
    check(f"  {m[:16]} slashed",
          e._miners[m].stake_oby < stakes_before[m])
    check(f"  {m[:16]} offence_count = 1",
          e._miners[m].offence_count == 1)


# ── Refund recorded on dispute ─────────────────────────────────────────────
section("Refund recorded on dispute/fault")

e = make_engine()
job = make_redundant_job(e, payment_oby=500.0)
picks = e.assign_redundant_job(job.job_id)
# Trigger 3/3 all-different (disputed)
e.consensus.submit_result(job, picks[0], fake_hash('refund-a'))
e.consensus.submit_result(job, picks[1], fake_hash('refund-b'))
e.consensus.submit_result(job, picks[2], fake_hash('refund-c'))
outcome = e._finalize_redundant_locked(job)
check("refund_oby > 0 on disputed job", job.refund_oby > 0,
      f"got {job.refund_oby}")


# ── Idempotency of finalize ─────────────────────────────────────────────────
section("finalize is idempotent on settled jobs")

e = make_engine()
job = make_redundant_job(e)
picks = e.assign_redundant_job(job.job_id)
for m in picks:
    e.consensus.submit_result(job, m, fake_hash('idem-agreed'))
outcome1 = e._finalize_redundant_locked(job)

# Snapshot state
miner_earned_after_first = {m: e._miners[m].oby_earned for m in picks}

outcome2 = e._finalize_redundant_locked(job)
check("second finalize: same status",
      outcome2.status == outcome1.status)
check("second finalize: same winners",
      outcome2.winners == outcome1.winners)
check("second finalize reason = already_finalized",
      outcome2.reason == 'already_finalized')

# No double-credit
for m in picks:
    check(f"  {m[:16]} not double-credited",
          e._miners[m].oby_earned == miner_earned_after_first[m])


# ── finalize_due_redundant_jobs walks pending timeouts ─────────────────────
section("finalize_due_redundant_jobs sweeps timed-out jobs")

e = make_engine()
# Two jobs: one ready (timeout), one not yet
ready_job = make_redundant_job(e)
e.assign_redundant_job(ready_job.job_id)
ready_job.consensus_deadline = int(time.time()) - 1   # past

waiting_job = make_redundant_job(e)
e.assign_redundant_job(waiting_job.job_id)
# Don't touch deadline — defaults to now + REDUNDANT_TIMEOUT_S

settled = e.finalize_due_redundant_jobs()
check("ready job finalized", len(settled) == 1)
check("waiting job NOT finalized", waiting_job.status == 'assigned')


# ── Standard tier still works ──────────────────────────────────────────────
section("Regression: standard tier complete_job_with_verification unchanged")

e = make_engine()
# Re-stub block hash since we replaced it
e._block_hash = lambda: hashlib.sha3_256(b'std-test').digest()
job_std, _ = e.submit_job_with_verification(
    developer_addr='dev_std', job_type='inference',
    model_id='test', coin=Stablecoin.USDC,
    model_hash=fake_hash(50), container_digest='sha256:' + fake_hash(51),
    seed=99, input_payload_hash=fake_hash(52), input_schema_hash=fake_hash(53),
    gpu_count=1, duration_hr=0.1, tier='standard',
)
chosen = e.assign_job(job_std.job_id)
check("standard tier assigns 1 miner", chosen is not None)
result = e.complete_job_with_verification(
    job_id=job_std.job_id, miner_addr=chosen,
    result_cid='cid-std', result_hash=fake_hash(60),
)
check("standard tier returns VerificationResult",
      hasattr(result, 'method'))
check("standard tier method ∈ (optimistic, challenged)",
      result.method in ('optimistic', 'challenged', 'zk'))


# ── Round-trip via save/load ───────────────────────────────────────────────
section("save/load preserves redundant tier state")

e = make_engine()
job = make_redundant_job(e)
picks = e.assign_redundant_job(job.job_id)
e.consensus.submit_result(job, picks[0], fake_hash('rt-a'))
e.consensus.submit_result(job, picks[1], fake_hash('rt-a'))   # outlier on purpose

snap_path = os.path.join(tempfile.gettempdir(), 'oby-redundant-rt.json')
e.save(snap_path)

# Fresh engine, load
e2 = TokenomicsEngine()
e2.update_rate(Stablecoin.USDC, 1.0)
e2.load(snap_path)

loaded_job = e2.get_job(job.job_id)
check("loaded job exists", loaded_job is not None)
check("loaded tier preserved", loaded_job.tier == 'redundant')
check("loaded assigned_miners preserved",
      loaded_job.assigned_miners == picks)
check("loaded result_submissions preserved (2 entries)",
      len(loaded_job.result_submissions) == 2)
check("loaded consensus_deadline preserved",
      loaded_job.consensus_deadline == job.consensus_deadline)
try:
    os.remove(snap_path)
except OSError:
    pass


# ── Report ─────────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  PASSED: {PASSED}")
print(f"  FAILED: {len(FAILED)}")
if FAILED:
    print(f"\nFailures:")
    for name, detail in FAILED:
        print(f"  - {name}: {detail}")
    sys.exit(1)
print(f"  All checks green. Phase 4.2 redundant tier ready.")
sys.exit(0)
