"""Observability surface for the Obelyth node.

Phase 5.5a (this file): defines the data shapes and the registry that
/health and /metrics will read from. Subsequent subphases extend it:

  5.5b  — structured JSON logging plugs into log_event()
  5.5c  — counters get incremented from real call sites + reorg detection
  5.5e  — verification trace events get appended via log_event()

The design principle: the registry is a passive store. Nothing in this
file reaches into the node itself. The node injects values during its
loops (save timing, counter increments). That keeps the import graph
flat and the unit tests easy — no fullnode needed to test metrics shape.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional


# ── Counter names (5.5c will increment these) ────────────────────────────────
# Declared here so /metrics returns them as 0 from day one even before
# anything increments them. Keeps the response shape stable for monitoring
# tools that key off field presence.
COUNTER_NAMES: tuple[str, ...] = (
    # Block lifecycle
    'blocks_received',
    'blocks_accepted',
    'blocks_rejected_invalid_sig',
    'blocks_rejected_dao_tax',
    'blocks_rejected_other',
    # Transaction lifecycle
    'txs_received',
    'txs_accepted_mempool',
    'txs_rejected_double_spend',
    'txs_rejected_invalid_sig',
    'txs_rejected_other',
    # Peer lifecycle
    'peer_connects',
    'peer_disconnects',
    # Verification
    'verification_challenges_issued',
    'verification_passes',
    'verification_slashes_first',
    'verification_slashes_second',
    'dev_refunds_issued',
    # Persistence
    'persist_saves',
    'persist_save_failures',
    # Reorgs
    'reorgs_total',
    'reorgs_by_depth_1',
    'reorgs_by_depth_2',
    'reorgs_by_depth_3plus',
)


@dataclass
class PersistStats:
    """Tracks the chain persistence loop's health.

    The persist loop is mission-critical: if it stalls, restarts lose data.
    /health uses last_save_ts to flip to 503 when the loop falls behind,
    so the values here are watched by both the node itself and external
    monitors like UptimeRobot.
    """
    last_save_ts        : float = 0.0     # Unix ts of last successful save
    last_save_duration_ms: float = 0.0    # How long the last save took
    save_count          : int   = 0       # Lifetime successful saves
    save_failure_count  : int   = 0       # Lifetime failures
    last_failure_ts     : float = 0.0
    last_failure_reason : Optional[str] = None


class MetricsRegistry:
    """In-memory metrics store. Thread-safe.

    The node creates one instance, attaches it to itself, and hands a
    reference to RPCHandler so /health and /metrics can read it. Future
    subphases (5.5c onward) write to it from the consensus, network, and
    verification layers via the same shared reference.
    """

    def __init__(self):
        self._lock      = threading.RLock()
        self._counters  : dict[str, int] = {n: 0 for n in COUNTER_NAMES}
        self.persist    = PersistStats()
        # Reorg history — most recent N kept for /metrics inspection.
        # Each entry: {ts, depth, invalidated_hashes, new_canonical_hashes, miners}
        self._reorgs    : list[dict] = []
        self._reorg_cap = 50

    # ── Counter API (used by 5.5c+) ──────────────────────────────────────────
    def increment(self, name: str, by: int = 1) -> None:
        """Increment a named counter. Silently no-ops if the name isn't
        in COUNTER_NAMES — we'd rather drop the data point than crash the
        node over a typo."""
        if name not in self._counters:
            return
        with self._lock:
            self._counters[name] += by

    def counters(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)

    # ── Persistence tracking (used by 5.5a) ──────────────────────────────────
    def record_save_success(self, duration_ms: float) -> None:
        with self._lock:
            self.persist.last_save_ts        = time.time()
            self.persist.last_save_duration_ms = duration_ms
            self.persist.save_count         += 1
        self.increment('persist_saves')

    def record_save_failure(self, reason: str) -> None:
        with self._lock:
            self.persist.last_failure_ts     = time.time()
            self.persist.last_failure_reason = reason
            self.persist.save_failure_count += 1
        self.increment('persist_save_failures')

    # ── Reorg tracking (used by 5.5c) ────────────────────────────────────────
    def record_reorg(
        self,
        depth: int,
        invalidated: list[str],
        new_canonical: list[str],
        miners: list[str],
    ) -> None:
        with self._lock:
            entry = {
                'ts'                 : time.time(),
                'depth'              : depth,
                'invalidated_hashes' : invalidated,
                'new_canonical_hashes': new_canonical,
                'miners'             : miners,
            }
            self._reorgs.append(entry)
            if len(self._reorgs) > self._reorg_cap:
                self._reorgs = self._reorgs[-self._reorg_cap:]
        self.increment('reorgs_total')
        if depth == 1:
            self.increment('reorgs_by_depth_1')
        elif depth == 2:
            self.increment('reorgs_by_depth_2')
        else:
            self.increment('reorgs_by_depth_3plus')

    def reorgs(self) -> list[dict]:
        with self._lock:
            return list(self._reorgs)


# ── Health evaluation ────────────────────────────────────────────────────────
# Tunables for what "healthy" means. These thresholds are deliberately
# generous on a fresh testnet — we want /health to flip RED on real
# problems, not on routine startup quiet periods.

HEALTH_PERSIST_STALE_S  = 120.0   # No save in 2 min → unhealthy
HEALTH_MEMPOOL_OVERFLOW = 5000    # Mempool > 5k txs → unhealthy (clear spam signal)
HEALTH_TIP_STALE_S      = 1800.0  # No new block in 30 min → unhealthy (single-validator
                                  # testnet: must be lenient because the validator is YOU
                                  # and might be debugging. Tighten to 120s once multi-validator.)


def evaluate_health(
    uptime_s        : float,
    persist         : PersistStats,
    mempool_size    : int,
    last_block_ts   : Optional[float],
    persist_enabled : bool = True,
) -> tuple[str, list[str]]:
    """Returns ('ok' | 'degraded' | 'unhealthy', [reasons]).

    The grace period: during the first 60s of uptime, the persist loop
    hasn't fired yet (it sleeps before the first save), and there may be
    no blocks. Don't flag these as failures during boot.
    """
    reasons: list[str] = []
    in_boot = uptime_s < 60.0

    # Persist staleness — only check if persist is enabled and we're past boot
    if persist_enabled and not in_boot:
        if persist.save_count == 0:
            reasons.append('persist_loop_never_ran')
        else:
            age = time.time() - persist.last_save_ts
            if age > HEALTH_PERSIST_STALE_S:
                reasons.append(f'persist_stale_{int(age)}s')

    # Mempool overflow
    if mempool_size > HEALTH_MEMPOOL_OVERFLOW:
        reasons.append(f'mempool_overflow_{mempool_size}')

    # Block production staleness — only meaningful after boot
    if not in_boot and last_block_ts is not None:
        age = time.time() - last_block_ts
        if age > HEALTH_TIP_STALE_S:
            reasons.append(f'tip_stale_{int(age)}s')

    if not reasons:
        return ('ok', [])
    # Two tiers: degraded (one issue) vs unhealthy (multiple or persist failure)
    if 'persist_loop_never_ran' in reasons or len(reasons) >= 2:
        return ('unhealthy', reasons)
    return ('degraded', reasons)
