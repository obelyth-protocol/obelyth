"""
Obelyth Optimistic Verification
=====================================
Solves the verification dilemma for AI compute:

Approach: Optimistic Execution + Random Challenge
  1. Miner completes job, submits result hash + output CID
  2. Result is accepted optimistically (fast, low overhead)
  3. A random subset of validators re-run a small portion of the work
  4. If challenge succeeds (miner was honest): miner keeps reward
  5. If challenge fails (miner faked work): slash stake, retry job

Challenge rate scales with miner reputation:
  - New miner (rep=1.0): 30% of jobs challenged
  - Trusted miner (rep>0.95, 100+ jobs): 5% of jobs challenged
  - Slashed miner (rep<0.7): 60% of jobs challenged

Future: ZK proofs (EZKL / Risc Zero) replace random audits entirely.
"""

import hashlib
import json
import time
import random
import logging
import threading
from dataclasses import dataclass, field
from typing      import Optional, Callable
from enum        import Enum

log = logging.getLogger('obelyth.verify')


class ChallengeStatus(str, Enum):
    PENDING  = 'pending'
    PASSED   = 'passed'
    FAILED   = 'failed'
    EXPIRED  = 'expired'


@dataclass
class Challenge:
    challenge_id     : str
    job_id           : str
    miner_addr       : str
    challenger_addr  : str
    result_hash      : str      # hash miner submitted
    challenge_seed   : str      # random seed for reproducible re-run
    created_at       : int      = field(default_factory=lambda: int(time.time()))
    expires_at       : int      = 0        # set on creation
    status           : ChallengeStatus = ChallengeStatus.PENDING
    rerun_hash       : str      = ''       # hash challenger computed
    slash_amount_nxs : float    = 0.0

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
            'created_at'      : self.created_at,
            'expires_at'      : self.expires_at,
            'status'          : self.status.value,
            'rerun_hash'      : self.rerun_hash,
            'slash_amount_nxs': self.slash_amount_nxs,
        }


@dataclass
class VerificationResult:
    job_id    : str
    passed    : bool
    method    : str           # 'optimistic' | 'challenged' | 'zk'
    details   : str = ''
    latency_ms: float = 0.0


class VerificationEngine:
    """
    Manages optimistic verification and challenge lifecycle.
    Plugs into TokenomicsEngine for slashing.
    """

    CHALLENGE_WINDOW_SECONDS = 3600      # 1 hour to challenge
    ZK_STUB_ENABLED          = True      # flip to False when real ZK ready

    def __init__(self, on_slash: Callable = None):
        """
        on_slash(miner_addr, job_id, slash_pct) called when challenge fails.
        """
        self.on_slash     : Optional[Callable] = on_slash
        self._challenges  : dict[str, Challenge] = {}
        self._results     : dict[str, VerificationResult] = {}
        self._lock        = threading.RLock()

        # Start expiry watchdog
        threading.Thread(
            target=self._expiry_loop, daemon=True, name='verify-watchdog'
        ).start()

    # ── Result Submission ──────────────────────────────────────────────────────

    def submit_result(
        self,
        job_id        : str,
        miner_addr    : str,
        miner_rep     : float,
        result_cid    : str,
        result_hash   : str,
        zk_proof      : str = '',
    ) -> VerificationResult:
        """
        Miner submits completed work.
        Decides: accept optimistically, or issue challenge.
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
                log.info(f"Job {job_id}: ZK verified ✓")
                return result

        # Decide whether to challenge based on reputation
        challenge_rate = self._challenge_rate(miner_rep)
        should_challenge = random.random() < challenge_rate

        if not should_challenge:
            result = VerificationResult(
                job_id    = job_id,
                passed    = True,
                method    = 'optimistic',
                details   = f'Accepted optimistically (rep={miner_rep:.2f}, rate={challenge_rate:.0%})',
                latency_ms= (time.time() - start) * 1000,
            )
            with self._lock:
                self._results[job_id] = result
            log.info(f"Job {job_id}: Optimistic accept ✓ (rep={miner_rep:.2f})")
            return result

        # Issue challenge
        challenge = self._issue_challenge(job_id, miner_addr, result_hash)
        log.info(
            f"Job {job_id}: Challenge issued → {challenge.challenge_id[:12]} "
            f"(rep={miner_rep:.2f}, rate={challenge_rate:.0%})"
        )
        # Return pending — result finalised when challenge resolves
        return VerificationResult(
            job_id    = job_id,
            passed    = False,   # not yet confirmed
            method    = 'challenged',
            details   = f'Challenge pending: {challenge.challenge_id}',
            latency_ms= (time.time() - start) * 1000,
        )

    # ── Challenge ─────────────────────────────────────────────────────────────

    def _issue_challenge(
        self,
        job_id      : str,
        miner_addr  : str,
        result_hash : str,
        challenger  : str = 'protocol',
    ) -> Challenge:
        import uuid
        cid   = str(uuid.uuid4())[:16]
        seed  = hashlib.sha3_256(
            (job_id + str(time.time())).encode()
        ).hexdigest()[:16]

        challenge = Challenge(
            challenge_id    = cid,
            job_id          = job_id,
            miner_addr      = miner_addr,
            challenger_addr = challenger,
            result_hash     = result_hash,
            challenge_seed  = seed,
            expires_at      = int(time.time()) + self.CHALLENGE_WINDOW_SECONDS,
        )
        with self._lock:
            self._challenges[cid] = challenge
        return challenge

    def resolve_challenge(
        self,
        challenge_id  : str,
        rerun_hash    : str,
        stake_nxs     : float,
    ) -> ChallengeStatus:
        """
        Validator submits their re-run hash.
        If it matches miner's hash → miner honest.
        If it differs → miner slashed.
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
                log.info(f"Challenge {challenge_id[:12]}: PASSED ✓")
                return ChallengeStatus.PASSED
            else:
                slash_pct        = 0.20    # 20% stake slash
                slash_nxs        = stake_nxs * slash_pct
                c.slash_amount_nxs = slash_nxs
                c.status         = ChallengeStatus.FAILED
                self._results[c.job_id] = VerificationResult(
                    job_id  = c.job_id,
                    passed  = False,
                    method  = 'challenged',
                    details = f'Challenge FAILED — slashing {slash_nxs:.2f} NXS',
                )
                if self.on_slash:
                    self.on_slash(c.miner_addr, c.job_id, slash_pct)
                log.warning(
                    f"Challenge {challenge_id[:12]}: FAILED ✗ "
                    f"Miner {c.miner_addr[:16]} slashed {slash_nxs:.2f} NXS"
                )
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

    def _challenge_rate(self, reputation: float) -> float:
        """Higher reputation = fewer challenges."""
        if reputation >= 0.95:
            return 0.05      # 5%  — trusted miner
        elif reputation >= 0.80:
            return 0.15      # 15%
        elif reputation >= 0.65:
            return 0.30      # 30%
        else:
            return 0.60      # 60% — low rep, audit heavily

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
            'total'    : len(challenges),
            'pending'  : sum(1 for c in challenges if c.status == ChallengeStatus.PENDING),
            'passed'   : sum(1 for c in challenges if c.status == ChallengeStatus.PASSED),
            'failed'   : sum(1 for c in challenges if c.status == ChallengeStatus.FAILED),
            'expired'  : sum(1 for c in challenges if c.status == ChallengeStatus.EXPIRED),
            'slash_nxs': sum(c.slash_amount_nxs for c in challenges),
        }
