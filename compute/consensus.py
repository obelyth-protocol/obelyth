"""
Obelyth Consensus Engine (Redundant Tier)
==============================================

Aggregates N parallel result submissions for redundant-tier jobs, runs
deterministic majority consensus, and emits settlement callbacks compatible
with the verification engine's callback surface.

Design (locked, see Phase 4.2 plan)
-----------------------------------
- Redundant tier runs N miners independently. Default N=3.
- Each miner submits a result_hash via the existing /compute/result endpoint.
- The consensus engine accumulates submissions until either:
    a) all N have submitted, or
    b) consensus_deadline (typically 10 min from assignment) has passed.
- At finalization, the engine determines the verdict:

    Returns      | Outcome
    -------------|------------------------------------------------------------
    N/N unanimous| All credited
    N/N majority | Majority credited, outlier(s) slashed
    N/N all-diff | Disputed: refund dev, no slash, no credit
    K/N majority | Winners credited, missing miners slashed for timeout (1 < K < N)
    K/N tied     | Disputed: refund dev, no slash for returners
    1/N          | Refund dev, missing miners slashed
    0/N          | Refund dev, all miners slashed

The dispute outcome is a safety valve: when miners disagree N-ways, we
can't conclude maliciousness — the determinism envelope itself may be
buggy or there's an unknown driver-level non-determinism. Punishing
anyone in that case would be unjust; we punt to dispute resolution
(governance, Phase 6+).

Callbacks (same shape as VerificationEngine)
--------------------------------------------
- on_pass(miner_addr, job_id, method='consensus')
    Called for each winning miner. Credits oby_to_miner (which is already
    the per-miner share — engine divides total by REDUNDANT_MINER_COUNT
    at submit time).
- on_slash(miner_addr, job_id, slash_pct, slashed_oby, offence_count)
    Called for each outlier or timeout. Same escalating policy as standard
    tier: 1st = 20%, 2nd = 50% + ban.
- on_refund(developer_addr, job_id, refund_oby)
    Called when the job is disputed or all miners failed. Refund is up to
    2x the developer's payment, funded from slashed stake when possible.
- on_ban(miner_addr, until_block)
    Called on 2nd+ offence — 30 days at 10s blocks = 259,200 blocks.

These match the VerificationEngine callbacks exactly so TokenomicsEngine
can wire one set of handlers for both engines.

State persistence
-----------------
The consensus engine is stateless beyond the in-flight job submissions
dict. Submissions are stored on the ComputeJob itself (result_submissions
field), so they persist across restarts via the engine's load() path.
On startup, the background sweep walks all 'assigned' redundant jobs and
re-evaluates whether they're due for finalization.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger('obelyth.consensus')


# ── Errors ──────────────────────────────────────────────────────────────────

class ConsensusError(RuntimeError):
    """Base class for consensus-engine refusals."""


class UnknownJob(ConsensusError):
    pass


class WrongTier(ConsensusError):
    pass


class NotAssigned(ConsensusError):
    """Miner submitted a result for a redundant job they weren't assigned to."""


class DuplicateSubmission(ConsensusError):
    """Miner submitted twice for the same job."""


class JobAlreadyFinalized(ConsensusError):
    pass


# ── Consensus outcome ───────────────────────────────────────────────────────

@dataclass
class ConsensusOutcome:
    job_id    : str
    status    : str          # 'done' | 'faulted' | 'disputed'
    winners   : list[str] = field(default_factory=list)
    outliers  : list[str] = field(default_factory=list)
    missing   : list[str] = field(default_factory=list)
    # The result hash that won the majority. Empty on dispute.
    consensus_hash : str = ''
    # Was finalization triggered by timeout (vs all-N-returned)?
    by_timeout : bool = False
    # Reason for human-readable logging
    reason     : str = ''

    def to_dict(self) -> dict:
        return {
            'job_id'        : self.job_id,
            'status'        : self.status,
            'winners'       : self.winners,
            'outliers'      : self.outliers,
            'missing'       : self.missing,
            'consensus_hash': self.consensus_hash,
            'by_timeout'    : self.by_timeout,
            'reason'        : self.reason,
        }


# ── Consensus engine ────────────────────────────────────────────────────────

class ConsensusEngine:
    """
    Stateless aggregator and majority decider for redundant-tier jobs.

    All persistent state lives on the ComputeJob itself (result_submissions,
    consensus_winners, consensus_outliers, status). This class just walks
    over that state and applies the rules.

    Callbacks are optional — if not provided, finalization still updates
    job state but does not propagate slash/refund/credit to ledger.
    """

    def __init__(
        self,
        on_pass   : Optional[Callable] = None,
        on_slash  : Optional[Callable] = None,
        on_refund : Optional[Callable] = None,
        on_ban    : Optional[Callable] = None,
    ):
        self.on_pass   = on_pass
        self.on_slash  = on_slash
        self.on_refund = on_refund
        self.on_ban    = on_ban

    # ── Submission ───────────────────────────────────────────────────────────

    def submit_result(
        self,
        job,                # ComputeJob (duck-typed to avoid circular import)
        miner_addr : str,
        result_hash: str,
        result_cid : str = '',
    ) -> dict:
        """
        Record a miner's submission against a redundant-tier job.

        Returns a dict with:
          - 'accepted': bool — submission was recorded
          - 'status': current job status after this submission
          - 'submissions_count': how many of N have submitted so far
          - 'ready_to_finalize': True if all N have submitted (caller should
            call finalize() immediately)

        Raises ConsensusError subclasses on rejection:
          - UnknownJob, WrongTier: job doesn't exist or isn't redundant
          - NotAssigned: miner wasn't one of the N assigned for this job
          - DuplicateSubmission: miner already submitted
          - JobAlreadyFinalized: status is 'done', 'faulted', or 'disputed'
        """
        if job is None:
            raise UnknownJob('no job provided')
        if job.tier != 'redundant':
            raise WrongTier(f'job {job.job_id} is tier={job.tier!r}')
        if job.status not in ('assigned', 'pending'):
            raise JobAlreadyFinalized(
                f'job {job.job_id} status={job.status!r}'
            )
        if miner_addr not in job.assigned_miners:
            raise NotAssigned(
                f'{miner_addr} not in assigned_miners for {job.job_id}'
            )
        if miner_addr in job.result_submissions:
            raise DuplicateSubmission(
                f'{miner_addr} already submitted for {job.job_id}'
            )

        # Record the submission
        job.result_submissions[miner_addr] = {
            'result_hash' : result_hash,
            'result_cid'  : result_cid,
            'submitted_at': int(time.time()),
        }

        n_submitted = len(job.result_submissions)
        n_expected  = len(job.assigned_miners)
        ready       = (n_submitted >= n_expected)

        log.info(
            f"Redundant submission: {job.job_id} from {miner_addr[:16]} "
            f"({n_submitted}/{n_expected}) hash={result_hash[:12]}.."
        )

        return {
            'accepted'         : True,
            'status'           : job.status,
            'submissions_count': n_submitted,
            'ready_to_finalize': ready,
        }

    # ── Finalization ─────────────────────────────────────────────────────────

    def is_ready_to_finalize(self, job, now_ts: Optional[int] = None) -> bool:
        """True if a redundant job is ready for finalize():
           - all assigned miners have submitted, OR
           - consensus_deadline has passed (timeout)."""
        if job is None or job.tier != 'redundant':
            return False
        if job.status not in ('assigned', 'pending'):
            return False
        if len(job.result_submissions) >= len(job.assigned_miners):
            return True
        now_ts = now_ts or int(time.time())
        if job.consensus_deadline > 0 and now_ts >= job.consensus_deadline:
            return True
        return False

    def finalize(
        self,
        job,
        offence_count_provider: Callable[[str], int] = None,
        stake_provider        : Callable[[str], float] = None,
        block_height_provider : Callable[[], int]      = None,
    ) -> ConsensusOutcome:
        """
        Determine the verdict for a redundant-tier job, fire callbacks,
        and update job state.

        Providers (all optional) let the engine compute slash amounts and
        ban deadlines without coupling to TokenomicsEngine internals.

        Returns a ConsensusOutcome with status and roster.

        Idempotent: if the job is already finalized, returns the prior
        outcome reconstructed from job state.
        """
        if job is None:
            raise UnknownJob('no job provided')
        if job.tier != 'redundant':
            raise WrongTier(f'job {job.job_id} is tier={job.tier!r}')

        # ── Idempotency: re-finalizing a settled job is a no-op ──
        if job.status in ('done', 'faulted', 'disputed'):
            return ConsensusOutcome(
                job_id   = job.job_id,
                status   = job.status,
                winners  = list(job.consensus_winners),
                outliers = list(job.consensus_outliers),
                missing  = [
                    a for a in job.assigned_miners
                    if a not in job.result_submissions
                ],
                consensus_hash = job.result_hash,
                reason         = 'already_finalized',
            )

        # ── Tally submissions by result_hash ──
        # buckets: result_hash -> list of miner addresses
        buckets: dict[str, list[str]] = {}
        for miner_addr, sub in job.result_submissions.items():
            buckets.setdefault(sub['result_hash'], []).append(miner_addr)

        n_expected  = len(job.assigned_miners)
        n_submitted = len(job.result_submissions)
        missing     = [a for a in job.assigned_miners
                       if a not in job.result_submissions]
        now_ts      = int(time.time())
        by_timeout  = (job.consensus_deadline > 0
                       and now_ts >= job.consensus_deadline
                       and n_submitted < n_expected)

        outcome = self._classify(
            job_id      = job.job_id,
            buckets     = buckets,
            missing     = missing,
            n_expected  = n_expected,
            by_timeout  = by_timeout,
        )

        # ── Apply outcome to job state ──
        job.consensus_winners  = list(outcome.winners)
        job.consensus_outliers = list(outcome.outliers)
        job.result_hash        = outcome.consensus_hash
        job.status             = outcome.status
        job.completed_at       = now_ts

        # ── Fire callbacks ──
        self._dispatch_callbacks(
            job                    = job,
            outcome                = outcome,
            offence_count_provider = offence_count_provider,
            stake_provider         = stake_provider,
            block_height_provider  = block_height_provider,
        )

        log.info(
            f"Redundant job {job.job_id} finalized: status={outcome.status} "
            f"winners={len(outcome.winners)} outliers={len(outcome.outliers)} "
            f"missing={len(outcome.missing)} reason={outcome.reason}"
        )
        return outcome

    def _classify(
        self,
        job_id     : str,
        buckets    : dict,
        missing    : list[str],
        n_expected : int,
        by_timeout : bool,
    ) -> ConsensusOutcome:
        """The pure decision logic. No side effects, no callbacks."""
        n_missing   = len(missing)
        n_submitted = n_expected - n_missing

        # Case 0/N: nothing returned. Refund dev, slash all assignees for timeout.
        if n_submitted == 0:
            return ConsensusOutcome(
                job_id     = job_id,
                status     = 'faulted',
                winners    = [],
                outliers   = list(missing),   # all missing get slashed
                missing    = list(missing),
                by_timeout = True,
                reason     = '0_of_N_timeout',
            )

        # Case 1/N: only one returned. Can't form majority — refund.
        # The single returner returned honestly but we can't credit without
        # corroboration. Missing miners are slashed for timeout.
        if n_submitted == 1:
            return ConsensusOutcome(
                job_id     = job_id,
                status     = 'disputed',
                winners    = [],   # the one returner gets nothing, but not slashed
                outliers   = list(missing),
                missing    = list(missing),
                by_timeout = by_timeout,
                reason     = '1_of_N_insufficient_corroboration',
            )

        # ≥2 submitted: try to find a majority bucket
        # Sort buckets by size desc, then by hash for determinism
        sorted_buckets = sorted(
            buckets.items(),
            key=lambda kv: (-len(kv[1]), kv[0]),
        )
        top_hash, top_miners = sorted_buckets[0]
        top_size = len(top_miners)

        # Check for a tie at the top (no strict majority)
        if len(sorted_buckets) > 1 and len(sorted_buckets[1][1]) == top_size:
            # Multiple buckets tied for first place → no consensus
            # All assigned-but-returners get NO credit but also NO slash.
            # Missing miners DO get slashed for timeout.
            return ConsensusOutcome(
                job_id         = job_id,
                status         = 'disputed',
                winners        = [],
                outliers       = list(missing),   # only the no-shows
                missing        = list(missing),
                consensus_hash = '',
                by_timeout     = by_timeout,
                reason         = 'no_majority_tied',
            )

        # We have a clear top bucket — it wins the consensus
        outliers = []
        for h, miners in sorted_buckets[1:]:
            outliers.extend(miners)
        # Plus any timeouts get slashed too
        outliers.extend(missing)

        return ConsensusOutcome(
            job_id         = job_id,
            status         = 'done',
            winners        = list(top_miners),
            outliers       = outliers,
            missing        = list(missing),
            consensus_hash = top_hash,
            by_timeout     = by_timeout,
            reason         = f'majority_{top_size}_of_{n_expected}',
        )

    def _dispatch_callbacks(
        self,
        job,
        outcome: ConsensusOutcome,
        offence_count_provider: Callable = None,
        stake_provider        : Callable = None,
        block_height_provider : Callable = None,
    ):
        """Wire outcome → settlement callbacks. Mirrors VerificationEngine's
        slash/refund/ban behaviour so the engine's existing handlers fit."""

        # Credit winners
        if self.on_pass:
            for w in outcome.winners:
                try:
                    self.on_pass(w, job.job_id, 'consensus')
                except Exception as e:
                    log.error(f"on_pass({w}) failed: {e}")

        # Slash outliers + missing
        if self.on_slash:
            for o in outcome.outliers:
                try:
                    offence_count = (
                        offence_count_provider(o) + 1
                        if offence_count_provider else 1
                    )
                    stake = stake_provider(o) if stake_provider else 0.0
                    slash_pct = 0.20 if offence_count == 1 else 0.50
                    slashed   = round(stake * slash_pct, 8)
                    self.on_slash(o, job.job_id, slash_pct, slashed, offence_count)

                    # Ban on 2nd+ offence — 30 days at 10s blocks
                    if offence_count >= 2 and self.on_ban and block_height_provider:
                        BLOCKS_30_DAYS = 30 * 24 * 60 * 6   # 30d * 86400s / 10s
                        until = block_height_provider() + BLOCKS_30_DAYS
                        try:
                            self.on_ban(o, until)
                        except Exception as e:
                            log.error(f"on_ban({o}) failed: {e}")
                except Exception as e:
                    log.error(f"on_slash({o}) failed: {e}")

        # Refund dev when consensus failed (disputed) or job faulted entirely
        if self.on_refund and outcome.status in ('disputed', 'faulted'):
            # Refund cap: 2x the developer's payment, same as standard tier
            refund_oby = round(job.oby_to_miner * len(job.assigned_miners) * 2.0, 8)
            # The on_refund callback caps at 2x internally if it wants;
            # we pass the natural amount.
            try:
                self.on_refund(job.developer_addr, job.job_id, refund_oby)
            except Exception as e:
                log.error(f"on_refund failed: {e}")


__all__ = [
    'ConsensusEngine', 'ConsensusOutcome',
    'ConsensusError', 'UnknownJob', 'WrongTier', 'NotAssigned',
    'DuplicateSubmission', 'JobAlreadyFinalized',
]
