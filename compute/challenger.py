"""
Obelyth Challenger Daemon
============================
A separate node-side role: polls the verification engine for challenges,
re-runs each challenged job with the same pinned (model_hash, container_digest,
inference_seed) envelope, and submits its rerun_hash via /compute/challenge_resolve.

The engine compares the rerun_hash against the miner's submitted result_hash:
  match    → on_pass callback fires (miner credited)
  mismatch → escalating slash + dev refund + (if 2nd offence) ban

Architecture
------------
Challengers don't need stake. They DO need the same model weights and
container the miner used (the determinism envelope tells them what to use).
A challenger that lies has no consequence to the miner — the engine just
compares hashes. If two challengers disagree, that's a dispute resolution
case for governance; for testnet, first verdict in wins.

Multi-challenger handling (testnet posture)
-------------------------------------------
The engine's resolve_challenge marks a challenge resolved on first call.
Subsequent calls return EXPIRED. So in practice the first challenger to
respond wins. Production will need quorum (Phase 6/7 work).

Run mode
--------
Single-threaded FIFO. Polls every POLL_INTERVAL seconds, processes one
challenge at a time. Predictable, easy to reason about, easy to test.
Higher throughput is a Phase 5+ optimization.

Wiring
------
The challenger is started by passing --challenger to node/fullnode.py.
Alternatively it can run as its own process, polling any reachable node's
/compute/pending_challenges endpoint.
"""

import hashlib
import json
import logging
import threading
import time
import urllib.request
import urllib.error

log = logging.getLogger('obelyth.challenger')


# ── Helpers (shared with miner runtime) ──────────────────────────────────────

_HEX64 = frozenset('0123456789abcdef')


def _is_sha256_hex(s: str) -> bool:
    return (isinstance(s, str) and len(s) == 64
            and all(c in _HEX64 for c in s.lower()))


def _is_oci_digest(s: str) -> bool:
    return (isinstance(s, str) and s.startswith('sha256:')
            and _is_sha256_hex(s[len('sha256:'):]))


def rerun_inference(
    model_id : str,
    inputs   : list,
    task     : str,
    params   : dict,
    seed     : int,
) -> str:
    """
    Re-run inference with the pinned envelope and return the result_hash.

    Uses the SAME stub logic as compute/miner.py::JobRunner.run_inference
    so the hashes match when the miner is honest. In production (Phase 5)
    both will call into the real container with the pinned image digest
    and the pinned seed propagated to vLLM SamplingParams.

    Returns the result_hash as 64-char lowercase hex.
    """
    outputs = []
    for inp in inputs:
        stub_text = (
            f"[Obelyth inference stub] "
            f"Model '{model_id}' (seed={seed}) processed: '{str(inp)[:60]}'"
        )
        outputs.append({'generated_text': stub_text, 'score': 0.99})
    # MUST match the miner's canonical-JSON convention exactly:
    # sorted keys, compact separators, SHA-256.
    return hashlib.sha256(
        json.dumps(outputs, sort_keys=True,
                   separators=(',', ':')).encode()
    ).hexdigest()


# ── Challenger daemon ─────────────────────────────────────────────────────────

class ChallengerDaemon:
    """
    Polls /compute/pending_challenges and resolves each by re-running the
    work and posting the rerun_hash.

    Construct with a node_url (e.g. 'http://127.0.0.1:8334') and a
    challenger_addr (any string; used for logging and audit trail).
    """

    POLL_INTERVAL_S = 5.0
    ERROR_BACKOFF_S = 30.0

    def __init__(
        self,
        node_url        : str,
        challenger_addr : str,
        poll_interval_s : float = None,
    ):
        self.node_url        = node_url.rstrip('/')
        self.challenger_addr = challenger_addr
        self.poll_interval   = poll_interval_s or self.POLL_INTERVAL_S
        self._running        = False
        self._thread         = None
        # Stats — useful for the dashboard
        self.processed       = 0
        self.passed_count    = 0
        self.failed_count    = 0
        self.errors          = 0

    # ── Public lifecycle ─────────────────────────────────────────────────────

    def start(self):
        """Start the daemon in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True,
            name=f'challenger-{self.challenger_addr[:8]}',
        )
        self._thread.start()
        log.info(
            f"Challenger started: addr={self.challenger_addr[:16]} "
            f"node={self.node_url} poll={self.poll_interval}s"
        )

    def stop(self):
        self._running = False
        log.info("Challenger stop requested")

    def run_once(self) -> int:
        """Synchronous version for tests — poll once, process all challenges,
        return the count processed."""
        challenges = self._fetch_challenges()
        for c in challenges:
            self._process(c)
        return len(challenges)

    # ── Internals ────────────────────────────────────────────────────────────

    def _loop(self):
        while self._running:
            try:
                challenges = self._fetch_challenges()
                if challenges:
                    log.info(f"Challenger: {len(challenges)} pending")
                    for c in challenges:
                        self._process(c)
                time.sleep(self.poll_interval)
            except Exception as e:
                self.errors += 1
                log.error(f"Challenger loop error: {e}")
                time.sleep(self.ERROR_BACKOFF_S)
        log.info(
            f"Challenger stopped. Stats: "
            f"processed={self.processed} pass={self.passed_count} "
            f"fail={self.failed_count} errors={self.errors}"
        )

    def _fetch_challenges(self) -> list:
        url = (
            f"{self.node_url}/compute/pending_challenges"
            f"?challenger_addr={self.challenger_addr}"
        )
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read().decode())
            return data.get('challenges', [])
        except urllib.error.URLError as e:
            log.warning(f"Challenger fetch failed: {e}")
            self.errors += 1
            return []

    def _process(self, c: dict):
        challenge_id = c.get('challenge_id', '')
        job_id       = c.get('job_id', '')
        log.info(
            f"Challenger: rerunning job {job_id} "
            f"(challenge {challenge_id[:12]})"
        )

        # ── Validate the envelope before doing work ──
        if not _is_sha256_hex(c.get('model_hash', '')):
            log.warning(
                f"Challenger: skipping {challenge_id[:12]} — bad model_hash"
            )
            return
        if not _is_oci_digest(c.get('container_digest', '')):
            log.warning(
                f"Challenger: skipping {challenge_id[:12]} — bad container_digest"
            )
            return
        seed = c.get('inference_seed')
        if not isinstance(seed, int) or seed < 0 or seed >= 2**64:
            log.warning(
                f"Challenger: skipping {challenge_id[:12]} — bad seed"
            )
            return

        # ── Phase 3.3 stub: would verify model + pull container here ──
        # Phase 5 replaces these with real downloads + SHA verification.
        log.info(
            f"  determinism: model={c['model_hash'][:12]}.. "
            f"container={c['container_digest'][:24]}.. seed={seed}"
        )

        # ── Re-run the work ──
        try:
            rerun_hash = rerun_inference(
                model_id = c.get('model_id', ''),
                inputs   = c.get('inputs', []),
                task     = c.get('task', 'text-generation'),
                params   = c.get('params', {}),
                seed     = seed,
            )
        except Exception as e:
            log.error(f"  rerun failed for {challenge_id[:12]}: {e}")
            self.errors += 1
            return

        log.info(
            f"  rerun_hash={rerun_hash[:12]}.. "
            f"miner_hash={c.get('result_hash', '')[:12]}.. "
            f"{'MATCH' if rerun_hash == c.get('result_hash') else 'DIVERGE'}"
        )

        # ── Submit verdict ──
        try:
            body = json.dumps({
                'challenge_id': challenge_id,
                'rerun_hash'  : rerun_hash,
            }).encode()
            req = urllib.request.Request(
                f"{self.node_url}/compute/challenge_resolve",
                data=body,
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                resp = json.loads(r.read().decode())
            status = resp.get('status', 'unknown')
            self.processed += 1
            if status == 'passed':
                self.passed_count += 1
            elif status == 'failed':
                self.failed_count += 1
            log.info(
                f"  resolved: {challenge_id[:12]} -> {status}"
            )
        except urllib.error.URLError as e:
            log.error(f"  resolve POST failed: {e}")
            self.errors += 1


# ── CLI entry ─────────────────────────────────────────────────────────────────

def main():
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(name)s] %(levelname)s %(message)s',
    )
    parser = argparse.ArgumentParser(description='Obelyth challenger daemon')
    parser.add_argument(
        '--node-url', default='http://127.0.0.1:8334',
        help='Node RPC URL',
    )
    parser.add_argument(
        '--address', required=True,
        help='Challenger address (any string; used for logging)',
    )
    parser.add_argument(
        '--poll-interval', type=float, default=5.0,
        help='Seconds between polls (default 5)',
    )
    args = parser.parse_args()

    daemon = ChallengerDaemon(
        node_url        = args.node_url,
        challenger_addr = args.address,
        poll_interval_s = args.poll_interval,
    )
    daemon.start()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        daemon.stop()
        time.sleep(1)


if __name__ == '__main__':
    main()
