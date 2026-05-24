"""
Obelyth Optimistic Verification
==================================
Solves the verification dilemma for AI compute:

Approach: Optimistic Execution + Deterministic Random Challenge
  1. Job submission is validated for determinism (pinned model + container + seed)
  2. Miner is assigned via stake-weighted random sampling (consensus-deterministic)
  3. Miner completes job, submits result hash + output CID
  4. A per-job deterministic seed decides whether to challenge
  5. On challenge, validator re-runs the FULL job with the same pinned params
  6. If rerun hash matches → miner honest, reputation bumps
  7. If rerun diverges → escalating slash + developer refund from slashed stake

Three-tier challenge rate (constitutional):
  - New miner (jobs < 100 or rep not yet established): 30% of jobs challenged
  - Trusted miner (rep ≥ 0.95 AND jobs ≥ 100):          5% of jobs challenged
  - Slashed miner (rep < 0.70):                          60% of jobs challenged

Escalating slash (constitutional):
  - 1st offence: 20% slash + reputation reset to 0
  - 2nd+ offence: 50% slash + 30-day ban
  - Developer of faked job receives refund from slashed stake (capped at 2× payment)

Architectural choices documented in the whitepaper, driven by community design
review on r/CryptoTechnology:
  - Job assignment is stake-weighted, NOT reputation-weighted. Reputation-weighted
    assignment creates a Sybil reputation market and rich-get-richer concentration.
  - Challenges rerun the FULL job, not a sampled slice. Slice sampling lets a
    malicious miner fake the unsampled fraction.
  - Determinism is enforced at submission: model hash, container digest, and
    seed must be pinned. Non-pinned jobs are rejected before they enter the pool.

Future: ZK proofs (Groth16 / EZKL / Risc Zero) replace random audits entirely.
"""

import hashlib
import time
import logging
import threading
from dataclasses import dataclass, field
from typing      import Optional, Callable
from enum        import Enum

log = logging.getLogger('obelyth.verify')


# ── Constitutional constants ──────────────────────────────────────────────────
# These values appear in the whitepaper. Changing them in code without a
# corresponding whitepaper amendment is a protocol violation.

CHALLENGE_RATE_NEW      = 0.30   # 30% — new miner
CHALLENGE_RATE_TRUSTED  = 0.05   #  5% — trusted miner
CHALLENGE_RATE_SLASHED  = 0.60   # 60% — slashed miner

TRUSTED_REP_THRESHOLD   = 0.95
TRUSTED_JOBS_THRESHOLD  = 100
SLASHED_REP_THRESHOLD   = 0.70

FIRST_OFFENCE_SLASH_PCT  = 0.20  # 20% of stake
SECOND_OFFENCE_SLASH_PCT = 0.50  # 50% of remaining stake

# 30-day ban at 10s target block time: 30*24*60*6 = 259,200 blocks
SECOND_OFFENCE_BAN_BLOCKS = 259_200

# Developer refund cap: refund ≤ min(slashed_amount, payment × multiplier)
REFUND_MULTIPLIER = 2.0

# Stake floor for assignment eligibility (testnet value; reviewed before mainnet)
MIN_STAKE_OBY = 1000.0


# ── Enums ─────────────────────────────────────────────────────────────────────

class ChallengeStatus(str, Enum):
    PENDING  = 'pending'
    PASSED   = 'passed'
    FAILED   = 'failed'
    EXPIRED  = 'expired'


class MinerTier(str, Enum):
    NEW      = 'new'
    TRUSTED  = 'trusted'
    SLASHED  = 'slashed'
    BANNED   = 'banned'


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class JobSpec:
    """
    The pinned-determinism envelope a developer submits.
    The engine rejects anything that does not bind enough state for a
    reproducible re-run on a different node.
    """
    job_id            : str
    developer_addr    : str
    model_hash        : str        # 64-char lowercase hex SHA-256 of model weights or HF revision SHA
    container_digest  : str        # OCI image digest, "sha256:<64hex>"
    seed              : int        # uint64 propagated to inference framework
    input_payload_hash: str        # SHA-256 of input bytes
    input_schema_hash : str        # SHA-256 of the declared JSON-Schema doc
    payment_oby       : float      # quote at submission time

    def to_dict(self) -> dict:
        return {
            'job_id'            : self.job_id,
            'developer_addr'    : self.developer_addr,
            'model_hash'        : self.model_hash,
            'container_digest'  : self.container_digest,
            'seed'              : self.seed,
            'input_payload_hash': self.input_payload_hash,
            'input_schema_hash' : self.input_schema_hash,
            'payment_oby'       : self.payment_oby,
        }


@dataclass
class Challenge:
    challenge_id     : str
    job_id           : str
    miner_addr       : str
    challenger_addr  : str
    result_hash      : str        # hash miner submitted
    challenge_seed   : str        # deterministic per-job seed
    model_hash       : str        # pinned at job submission, must match on rerun
    container_digest : str        # pinned at job submission, must match on rerun
    inference_seed   : int        # pinned at job submission, must match on rerun
    created_at       : int        = field(default_factory=lambda: int(time.time()))
    expires_at       : int        = 0
    status           : ChallengeStatus = ChallengeStatus.PENDING
    rerun_hash       : str        = ''
    slash_amount_oby : float      = 0.0
    refund_amount_oby: float      = 0.0

    def is_expired(self) -> bool:
        return int(time.time()) > self.expires_at

    def to_dict(self) -> dict:
        return {
            'challenge_id'    : self.challenge_id,
            'job_id'          : self.job_id,
            'miner_addr'      : self.miner_addr,
            'challenger_addr' : self.challenger_addr,
            'result_hash'     : self.result_hash,
            'challenge_seed'  : self.challenge_seed,
            'model_hash'      : self.model_hash,
            'container_digest': self.container_digest,
            'inference_seed'  : self.inference_seed,
            'created_at'      : self.created_at,
            'expires_at'      : self.expires_at,
            'status'          : self.status.value,
            'rerun_hash'      : self.rerun_hash,
            'slash_amount_oby' : self.slash_amount_oby,
            'refund_amount_oby': self.refund_amount_oby,
        }


@dataclass
class VerificationResult:
    job_id    : str
    passed    : bool
    method    : str           # 'optimistic' | 'challenged' | 'zk'
    details   : str = ''
    latency_ms: float = 0.0


# ── Validation: determinism enforcement ───────────────────────────────────────

_HEX64 = set('0123456789abcdef')


def _is_sha256_hex(s: str) -> bool:
    return isinstance(s, str) and len(s) == 64 and all(c in _HEX64 for c in s.lower())


def _is_oci_digest(s: str) -> bool:
    """Canonical OCI digest form: 'sha256:' + 64 lowercase hex."""
    return (
        isinstance(s, str)
        and s.startswith('sha256:')
        and _is_sha256_hex(s[len('sha256:'):])
    )


class JobValidationError(ValueError):
    pass


def _ordinal_suffix(n: int) -> str:
    """1 → 'st', 2 → 'nd', 3 → 'rd', 4+ → 'th' (with 11/12/13 → 'th')."""
    if 10 <= (n % 100) <= 20:
        return 'th'
    return {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')


def validate_job_submission(job: JobSpec) -> None:
    """
    Reject jobs that don't pin enough state for a deterministic rerun.

    A job that can't be rerun reproducibly cannot be verified, so it must
    not enter the pool. This is the front-line defence against the
    'drivers and kernels make outputs vary' attack vector raised in
    community review.

    Raises JobValidationError if any pinned-determinism field is malformed.
    """
    if not _is_sha256_hex(job.model_hash):
        raise JobValidationError(
            f'model_hash must be 64-char lowercase hex SHA-256, got {job.model_hash!r}'
        )
    if not _is_oci_digest(job.container_digest):
        raise JobValidationError(
            f'container_digest must be "sha256:<64hex>", got {job.container_digest!r}'
        )
    if not isinstance(job.seed, int) or job.seed < 0 or job.seed >= 2**64:
        raise JobValidationError(
            f'seed must be uint64 (0 <= seed < 2^64), got {job.seed!r}'
        )
    if not _is_sha256_hex(job.input_payload_hash):
        raise JobValidationError('input_payload_hash must be SHA-256 hex')
    if not _is_sha256_hex(job.input_schema_hash):
        raise JobValidationError('input_schema_hash must be SHA-256 hex')
    if not isinstance(job.payment_oby, (int, float)) or job.payment_oby <= 0:
        raise JobValidationError(
            f'payment_oby must be positive, got {job.payment_oby!r}'
        )


# ── Tier and challenge rate selection ─────────────────────────────────────────

def compute_tier(
    reputation     : float,
    jobs_completed : int,
    is_banned      : bool,
) -> MinerTier:
    """
    Three-tier classification. Banned takes priority over everything.
    Re-earning trusted status after a slash requires building both
    reputation (≥ 0.95) and job count (≥ 100) from zero.
    """
    if is_banned:
        return MinerTier.BANNED
    if reputation < SLASHED_REP_THRESHOLD:
        return MinerTier.SLASHED
    if reputation >= TRUSTED_REP_THRESHOLD and jobs_completed >= TRUSTED_JOBS_THRESHOLD:
        return MinerTier.TRUSTED
    return MinerTier.NEW


def challenge_rate_for_tier(tier: MinerTier) -> float:
    if tier in (MinerTier.SLASHED, MinerTier.BANNED):
        return CHALLENGE_RATE_SLASHED
    if tier == MinerTier.TRUSTED:
        return CHALLENGE_RATE_TRUSTED
    return CHALLENGE_RATE_NEW


def _deterministic_uniform(seed_bytes: bytes) -> float:
    """
    Map seed bytes to a uniform [0, 1) draw deterministically.
    Used for both per-job challenge decisions and stake-weighted assignment.
    Every node arrives at the same result given the same seed.
    """
    digest = hashlib.sha3_256(seed_bytes).digest()
    u64 = int.from_bytes(digest[:8], 'big')
    return u64 / 2**64


def should_challenge(
    miner_rep      : float,
    miner_jobs     : int,
    miner_banned   : bool,
    block_hash     : bytes,
    job_id         : str,
) -> bool:
    """
    Deterministic per-job challenge decision. Every node independently
    arrives at the same answer, so consensus on whether a job was audited
    is automatic.

    Do NOT use random.random() here — that would break consensus across
    nodes and let a miner who knows which node will audit get away with
    cheating the others.
    """
    tier = compute_tier(miner_rep, miner_jobs, miner_banned)
    rate = challenge_rate_for_tier(tier)
    seed = block_hash + job_id.encode('utf-8')
    return _deterministic_uniform(seed) < rate


# ── Stake-weighted job assignment ─────────────────────────────────────────────

class NoEligibleMinersError(RuntimeError):
    pass


def assign_miner(
    job_id        : str,
    block_hash    : bytes,
    miners        : list[dict],
) -> str:
    """
    Select a miner for this job via stake-weighted random sampling.

    `miners` is a list of dicts with keys: 'address', 'stake_oby',
    'is_banned'. Eligibility requires not banned AND stake_oby ≥ MIN_STAKE_OBY.

    The selection is fully deterministic given (job_id, block_hash, miner set),
    so every node arrives at the same assignment. Assignment is part of
    consensus, not a private scheduler decision.

    Reputation is intentionally NOT used. Per community design review,
    reputation-weighted assignment creates a Sybil reputation market and
    a rich-get-richer concentration. Stake-weighting still requires
    capital to participate but doesn't compound advantage from prior
    work performance.

    Returns the chosen miner's address.
    Raises NoEligibleMinersError if no miner meets the bar.
    """
    pool = [
        m for m in miners
        if not m.get('is_banned', False)
           and float(m.get('stake_oby', 0)) >= MIN_STAKE_OBY
    ]
    if not pool:
        raise NoEligibleMinersError('no miners meet stake/ban requirements')

    # Canonical order so all nodes agree on the walk
    pool.sort(key=lambda m: m['address'])

    total_stake = sum(float(m['stake_oby']) for m in pool)
    if total_stake <= 0:
        raise NoEligibleMinersError('total eligible stake is zero')

    draw = _deterministic_uniform(
        block_hash + job_id.encode('utf-8')
    ) * total_stake

    cumulative = 0.0
    for m in pool:
        cumulative += float(m['stake_oby'])
        if draw < cumulative:
            return m['address']
    # Floating-point edge case: round up to last
    return pool[-1]['address']


# ── Verification engine ───────────────────────────────────────────────────────

class VerificationEngine:
    """
    Manages optimistic verification and challenge lifecycle.

    Stateless w.r.t. miner records — the caller passes in miner_rep,
    stake_oby, offence_count, and is_banned with each call. The engine
    returns side-effects via the on_slash, on_refund, and on_ban
    callbacks so they can be applied atomically to the ledger by the
    node that holds it.

    Wiring:
      submit_result()   ← node/fullnode.py on /compute/result endpoint
      resolve_challenge() ← node/fullnode.py on challenger rerun result
      assign_miner()    ← module-level function called by /compute/nextjob
    """

    CHALLENGE_WINDOW_SECONDS = 3600      # 1 hour to challenge
    ZK_STUB_ENABLED          = True      # flip to False when real ZK ready

    def __init__(
        self,
        on_slash : Optional[Callable] = None,
        on_refund: Optional[Callable] = None,
        on_ban   : Optional[Callable] = None,
        block_hash_provider: Optional[Callable] = None,
    ):
        """
        Callbacks (all optional):
          on_slash(miner_addr, job_id, slash_pct, slashed_oby, offence_count)
              Called when a challenge fails. Slash applied to miner's stake.
          on_refund(developer_addr, job_id, refund_oby)
              Called when a developer is owed a refund from slashed stake.
          on_ban(miner_addr, until_block)
              Called on 2nd+ offence when miner is time-locked.

        block_hash_provider() returns the current chain-tip block hash as bytes.
        Required for deterministic challenge selection.
        """
        self.on_slash  = on_slash
        self.on_refund = on_refund
        self.on_ban    = on_ban
        self._get_block_hash = block_hash_provider or (lambda: b'\x00' * 32)

        self._challenges : dict[str, Challenge] = {}
        self._results    : dict[str, VerificationResult] = {}
        self._jobs       : dict[str, JobSpec] = {}      # job_id → pinned spec
        self._lock       = threading.RLock()

        # Start expiry watchdog
        threading.Thread(
            target=self._expiry_loop, daemon=True, name='verify-watchdog'
        ).start()

    # ── Job registration ───────────────────────────────────────────────────────

    def register_job(self, job: JobSpec) -> None:
        """
        Validate and store a job's pinned-determinism envelope.
        Called when a developer submits a job, BEFORE assignment.
        Raises JobValidationError on malformed determinism fields.
        """
        validate_job_submission(job)
        with self._lock:
            self._jobs[job.job_id] = job
        log.info(
            f'Job {job.job_id} registered: model={job.model_hash[:12]}.. '
            f'container={job.container_digest[:20]}.. seed={job.seed}'
        )

    # ── Result Submission ──────────────────────────────────────────────────────

    def submit_result(
        self,
        job_id        : str,
        miner_addr    : str,
        miner_rep     : float,
        miner_jobs    : int,
        miner_banned  : bool,
        result_cid    : str,
        result_hash   : str,
        zk_proof      : str = '',
    ) -> VerificationResult:
        """
        Miner submits completed work.
        Decides: accept optimistically, or issue challenge.

        miner_jobs and miner_banned must be supplied by the caller from the
        miner registry so tier classification is correct.
        """
        start = time.time()

        # ZK path (future): if valid proof, accept immediately
        if zk_proof and self.ZK_STUB_ENABLED:
            if self._verify_zk_stub(result_hash, zk_proof):
                result = VerificationResult(
                    job_id    = job_id,
                    passed    = True,
                    method    = 'zk',
                    details   = 'ZK proof verified',
                    latency_ms= (time.time() - start) * 1000,
                )
                with self._lock:
                    self._results[job_id] = result
                log.info(f'Job {job_id}: ZK verified')
                return result

        # Tier + deterministic challenge decision
        tier = compute_tier(miner_rep, miner_jobs, miner_banned)
        rate = challenge_rate_for_tier(tier)
        block_hash = self._get_block_hash()
        challenge_now = should_challenge(
            miner_rep, miner_jobs, miner_banned, block_hash, job_id
        )

        if not challenge_now:
            result = VerificationResult(
                job_id    = job_id,
                passed    = True,
                method    = 'optimistic',
                details   = (
                    f'Accepted optimistically '
                    f'(tier={tier.value}, rate={rate:.0%})'
                ),
                latency_ms= (time.time() - start) * 1000,
            )
            with self._lock:
                self._results[job_id] = result
            log.info(
                f'Job {job_id}: Optimistic accept '
                f'(tier={tier.value}, rate={rate:.0%})'
            )
            return result

        # Issue challenge — carry the pinned determinism so the verifier
        # can rerun with the same environment.
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            # Defensive: if a result arrives for a job we never registered,
            # we can't issue a meaningful challenge. Accept optimistically
            # but log loudly so this is investigated.
            log.warning(
                f'Job {job_id}: challenge selected but no JobSpec registered. '
                f'Accepting optimistically. This indicates a wiring bug.'
            )
            result = VerificationResult(
                job_id    = job_id,
                passed    = True,
                method    = 'optimistic',
                details   = 'Accepted (no JobSpec to challenge against)',
                latency_ms= (time.time() - start) * 1000,
            )
            with self._lock:
                self._results[job_id] = result
            return result

        challenge = self._issue_challenge(job, miner_addr, result_hash)
        log.info(
            f'Job {job_id}: Challenge issued → {challenge.challenge_id[:12]} '
            f'(tier={tier.value}, rate={rate:.0%})'
        )
        return VerificationResult(
            job_id    = job_id,
            passed    = False,    # not yet confirmed
            method    = 'challenged',
            details   = f'Challenge pending: {challenge.challenge_id}',
            latency_ms= (time.time() - start) * 1000,
        )

    # ── Challenge ─────────────────────────────────────────────────────────────

    def _issue_challenge(
        self,
        job        : JobSpec,
        miner_addr : str,
        result_hash: str,
        challenger : str = 'protocol',
    ) -> Challenge:
        import uuid
        cid  = str(uuid.uuid4())[:16]
        seed = hashlib.sha3_256(
            (job.job_id + str(time.time())).encode()
        ).hexdigest()[:16]

        challenge = Challenge(
            challenge_id     = cid,
            job_id           = job.job_id,
            miner_addr       = miner_addr,
            challenger_addr  = challenger,
            result_hash      = result_hash,
            challenge_seed   = seed,
            model_hash       = job.model_hash,
            container_digest = job.container_digest,
            inference_seed   = job.seed,
            expires_at       = int(time.time()) + self.CHALLENGE_WINDOW_SECONDS,
        )
        with self._lock:
            self._challenges[cid] = challenge
        return challenge

    def resolve_challenge(
        self,
        challenge_id  : str,
        rerun_hash    : str,
        stake_oby     : float,
        offence_count : int = 0,
        current_block : int = 0,
    ) -> ChallengeStatus:
        """
        Validator submits their re-run hash.
          - If it matches miner's hash → miner honest, challenge PASSED.
          - If it differs → escalating slash, FAILED.

        offence_count is the miner's PRIOR offence count (before this event).
        current_block is the block height at which this resolution lands.

        Side-effects flow through on_slash, on_refund, on_ban callbacks.
        """
        with self._lock:
            c = self._challenges.get(challenge_id)
            if not c or c.status != ChallengeStatus.PENDING:
                return ChallengeStatus.EXPIRED

            c.rerun_hash = rerun_hash

            if c.is_expired():
                c.status = ChallengeStatus.EXPIRED
                # Expired without resolution → benefit of the doubt → pass
                self._results[c.job_id] = VerificationResult(
                    job_id  = c.job_id,
                    passed  = True,
                    method  = 'challenged',
                    details = 'Challenge expired without dispute',
                )
                return ChallengeStatus.EXPIRED

            if rerun_hash == c.result_hash:
                c.status = ChallengeStatus.PASSED
                self._results[c.job_id] = VerificationResult(
                    job_id  = c.job_id,
                    passed  = True,
                    method  = 'challenged',
                    details = 'Challenge passed — miner honest',
                )
                log.info(f'Challenge {challenge_id[:12]}: PASSED')
                return ChallengeStatus.PASSED

            # ── Fault path: escalating slash ──────────────────────────────────
            new_offence_count = offence_count + 1
            if new_offence_count == 1:
                slash_pct = FIRST_OFFENCE_SLASH_PCT
                ban_until = None
                slash_note = '1st offence: 20% slash + reputation reset to 0'
            else:
                slash_pct = SECOND_OFFENCE_SLASH_PCT
                ban_until = current_block + SECOND_OFFENCE_BAN_BLOCKS
                suffix = _ordinal_suffix(new_offence_count)
                slash_note = (
                    f'{new_offence_count}{suffix} offence: 50% slash '
                    f'+ ban until block {ban_until}'
                )

            slashed_oby = stake_oby * slash_pct

            # Developer refund: capped at REFUND_MULTIPLIER × payment
            job = self._jobs.get(c.job_id)
            payment = job.payment_oby if job else 0.0
            refund_cap = payment * REFUND_MULTIPLIER
            refund_oby = min(slashed_oby, refund_cap)

            c.slash_amount_oby  = slashed_oby
            c.refund_amount_oby = refund_oby
            c.status            = ChallengeStatus.FAILED

            self._results[c.job_id] = VerificationResult(
                job_id  = c.job_id,
                passed  = False,
                method  = 'challenged',
                details = (
                    f'Challenge FAILED — {slash_note} '
                    f'(slashed {slashed_oby:.4f} OBY, '
                    f'refund {refund_oby:.4f} OBY to developer)'
                ),
            )

            log.warning(
                f'Challenge {challenge_id[:12]}: FAILED. '
                f'Miner {c.miner_addr[:16]} {slash_note} '
                f'(slashed {slashed_oby:.4f} OBY)'
            )

            # Fire callbacks. Caller applies these atomically with the block
            # that records the fault proof.
            if self.on_slash:
                self.on_slash(
                    c.miner_addr, c.job_id, slash_pct, slashed_oby,
                    new_offence_count,
                )
            if self.on_refund and job and refund_oby > 0:
                self.on_refund(job.developer_addr, c.job_id, refund_oby)
            if self.on_ban and ban_until is not None:
                self.on_ban(c.miner_addr, ban_until)

            return ChallengeStatus.FAILED

    # ── ZK Stub ────────────────────────────────────────────────────────────────

    def _verify_zk_stub(self, result_hash: str, zk_proof: str) -> bool:
        """
        Stub ZK verifier.
        In production: replace with arkworks-rs Groth16 or PLONK verifier.
        The real proof would attest: 'I ran model M on input I and got output O'
        without revealing the input (for privacy) or requiring re-execution.
        """
        return zk_proof.startswith('zk-stub::') and len(result_hash) == 64

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _expiry_loop(self):
        """Expire stale challenges every minute."""
        while True:
            time.sleep(60)
            with self._lock:
                for c in list(self._challenges.values()):
                    if c.status == ChallengeStatus.PENDING and c.is_expired():
                        c.status = ChallengeStatus.EXPIRED
                        self._results[c.job_id] = VerificationResult(
                            job_id  = c.job_id,
                            passed  = True,
                            method  = 'challenged',
                            details = 'Challenge window expired — accepted',
                        )

    def get_result(self, job_id: str) -> Optional[VerificationResult]:
        return self._results.get(job_id)

    def pending_challenges(self) -> list[Challenge]:
        with self._lock:
            return [c for c in self._challenges.values()
                    if c.status == ChallengeStatus.PENDING]

    def stats(self) -> dict:
        with self._lock:
            challenges = list(self._challenges.values())
        return {
            'total'      : len(challenges),
            'pending'    : sum(1 for c in challenges if c.status == ChallengeStatus.PENDING),
            'passed'     : sum(1 for c in challenges if c.status == ChallengeStatus.PASSED),
            'failed'     : sum(1 for c in challenges if c.status == ChallengeStatus.FAILED),
            'expired'    : sum(1 for c in challenges if c.status == ChallengeStatus.EXPIRED),
            'slash_oby'  : sum(c.slash_amount_oby for c in challenges),
            'refund_oby' : sum(c.refund_amount_oby for c in challenges),
        }


__all__ = [
    # Constants
    'CHALLENGE_RATE_NEW',
    'CHALLENGE_RATE_TRUSTED',
    'CHALLENGE_RATE_SLASHED',
    'TRUSTED_REP_THRESHOLD',
    'TRUSTED_JOBS_THRESHOLD',
    'SLASHED_REP_THRESHOLD',
    'FIRST_OFFENCE_SLASH_PCT',
    'SECOND_OFFENCE_SLASH_PCT',
    'SECOND_OFFENCE_BAN_BLOCKS',
    'REFUND_MULTIPLIER',
    'MIN_STAKE_OBY',
    # Enums
    'ChallengeStatus',
    'MinerTier',
    # Dataclasses
    'JobSpec',
    'Challenge',
    'VerificationResult',
    # Errors
    'JobValidationError',
    'NoEligibleMinersError',
    # Pure functions
    'validate_job_submission',
    'compute_tier',
    'challenge_rate_for_tier',
    'should_challenge',
    'assign_miner',
    # Engine
    'VerificationEngine',
]
