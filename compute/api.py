"""
Obelyth Compute API — HTTP-to-engine wiring layer.

Sits between node/fullnode.py HTTP handlers and tokenomics/engine.py state.
Each method takes a dict (parsed from JSON body or query string) and returns
a (status_code, response_dict) tuple. This keeps the HTTP handlers thin and
the business logic testable without an HTTP server.

Endpoints handled
-----------------
  /compute/quote               POST  developer
  /compute/submit              POST  developer    (requires api_key)
  /compute/job                 POST  developer
  /compute/infer               POST  developer    (requires api_key; sync)
  /compute/register            POST  miner
  /compute/heartbeat           POST  miner
  /compute/nextjob             GET   miner        (?address=...)
  /compute/result              POST  miner
  /compute/challenge_resolve   POST  challenger

Auth
----
Developer endpoints (/submit, /infer) require a valid API key looked up via
accounts/registry. Miner and challenger endpoints are open for testnet; basic
per-IP rate-limiting will be added before mainnet.

Determinism envelope
--------------------
The SDK should send model_hash/container_digest/seed/input_payload_hash/
input_schema_hash with every /compute/submit. If the SDK doesn't provide
them yet, this API derives them from the request payload deterministically:
  - model_hash         = sha256(model_id || ":testnet-stub")
  - container_digest   = "sha256:" + sha256("obelyth-vllm-stub:testnet")
  - seed               = sha256(api_key || job_id)[:8] as uint64
  - input_payload_hash = sha256(JSON-serialized inputs)
  - input_schema_hash  = sha256(JSON-serialized config / params)

These derived values are NOT secure — a miner who knows the convention can
guess them. They exist so the testnet flow works end-to-end while we wait
for SDK v2 to send real pinned hashes. The API logs a warning whenever it
falls back to derived hashes so we know who's on the old SDK.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.parse
from typing import Optional, Any

log = logging.getLogger('obelyth.api')


# ── Helpers ───────────────────────────────────────────────────────────────────

def _err(code: int, msg: str) -> tuple[int, dict]:
    return code, {'error': msg}


def _ok(payload: dict) -> tuple[int, dict]:
    return 200, payload


def _stable_canonical_json(obj: Any) -> bytes:
    """JSON with sorted keys and no whitespace — deterministic across runs."""
    return json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()


def _sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _derive_determinism(
    model_id : str,
    inputs   : Any,
    config   : Any,
    api_key  : str,
    job_id   : str,
) -> dict:
    """Derive a deterministic envelope when the SDK doesn't supply one."""
    model_hash = _sha256_hex(f"{model_id}:testnet-stub".encode())
    container_digest = "sha256:" + _sha256_hex(b"obelyth-vllm-stub:testnet")
    seed_bytes = hashlib.sha256(f"{api_key}{job_id}".encode()).digest()
    seed = int.from_bytes(seed_bytes[:8], 'big')
    input_payload_hash = _sha256_hex(_stable_canonical_json(inputs or []))
    input_schema_hash = _sha256_hex(_stable_canonical_json(config or {}))
    return {
        'model_hash'         : model_hash,
        'container_digest'   : container_digest,
        'seed'               : seed,
        'input_payload_hash' : input_payload_hash,
        'input_schema_hash'  : input_schema_hash,
    }


def _is_valid_address(addr: str) -> bool:
    """Cheap sanity check — full validation is done by core/crypto on transactions."""
    if not isinstance(addr, str):
        return False
    if len(addr) < 20 or len(addr) > 80:
        return False
    return addr.replace('-', '').replace('_', '').isalnum()


# ── API class ─────────────────────────────────────────────────────────────────

class ComputeAPI:
    """
    Thin business-logic layer between HTTP handlers and the tokenomics engine.

    Construct once per node; share across all RPC threads. Internal locking
    is delegated to the engine.

    Auth model: developer endpoints look up the api_key via accounts_registry
    (if provided); miner and challenger endpoints are open.
    """

    # How long /compute/infer blocks waiting for a miner result. Practical
    # value for testnet — too short and small-network latency causes false
    # negatives, too long and the SDK call feels like it's hung.
    INFER_TIMEOUT_S = 30.0
    INFER_POLL_INTERVAL_S = 0.5

    def __init__(
        self,
        engine,
        accounts_registry = None,
    ):
        self.engine   = engine
        self.accounts = accounts_registry
        log.info(
            f"ComputeAPI ready. Accounts: "
            f"{'enabled' if accounts_registry else 'disabled (testnet/dev)'}"
        )

    # ── Auth helper ────────────────────────────────────────────────────────────

    def _resolve_account(self, api_key: str):
        """Return the developer account for an api_key, or None if not found
        or accounts registry is disabled (dev/testnet mode)."""
        if not self.accounts:
            return None
        if not api_key:
            return None
        return self.accounts.get_by_api_key(api_key)

    # ── Developer endpoints ────────────────────────────────────────────────────

    def quote(self, body: dict) -> tuple[int, dict]:
        """POST /compute/quote — no auth required, dev sees pricing before signing up.

        Accepts an optional 'tier' parameter ('standard' | 'redundant').
        Redundant tier returns 3x the standard cost because three miners run
        the job in parallel for trust-minimized verification. Default tier
        is 'standard' for backwards compatibility with v1 SDK clients.
        """
        from tokenomics.engine import Stablecoin
        try:
            coin_str = body.get('coin', 'USDC').upper()
            coin = Stablecoin[coin_str]
        except KeyError:
            return _err(400, f'unsupported stablecoin: {body.get("coin")}')

        tier = body.get('tier', 'standard')
        try:
            quote = self.engine.quote_job(
                job_type    = body.get('job_type', 'inference'),
                model_id    = body.get('model_id', ''),
                coin        = coin,
                gpu_count   = int(body.get('gpu_count', 1)),
                duration_hr = float(body.get('duration_hr', 1.0)),
                tier        = tier,
            )
        except ValueError as e:
            return _err(400, str(e))
        return _ok(quote)

    def submit(self, body: dict) -> tuple[int, dict]:
        """POST /compute/submit — developer submits a job."""
        from tokenomics.engine import Stablecoin
        from compute.verification import JobValidationError

        api_key = body.get('api_key', '')
        account = self._resolve_account(api_key)

        # If accounts registry is wired, require a valid API key. Otherwise
        # (testnet/dev mode) accept the developer_addr from the body directly.
        if self.accounts is not None and account is None:
            return _err(401, 'invalid or missing api_key')
        # If we resolved an account, enforce the developer_addr from it
        # (the body cannot spoof a different one) and check balance.
        if account is not None:
            if not account.is_active:
                return _err(403, f'account status: {account.status.value}')
            if not account.balance_sufficient:
                return _err(402,
                    f'insufficient balance: ${account.balance_usd:.4f}')
            developer_addr = account.account_id
        else:
            developer_addr = body.get('developer_addr', '')
        if not developer_addr:
            return _err(400, 'missing developer_addr (or api_key)')

        # Coin defaults to USDC
        try:
            coin = Stablecoin[body.get('coin', 'USDC').upper()]
        except KeyError:
            return _err(400, f'unsupported stablecoin: {body.get("coin")}')

        # Pull determinism envelope from body, falling back to derived values
        # if the SDK didn't supply them.
        import uuid
        provisional_job_id = str(uuid.uuid4())[:16]
        derived = _derive_determinism(
            model_id = body.get('model_id', ''),
            inputs   = body.get('inputs'),
            config   = body.get('config'),
            api_key  = api_key,
            job_id   = provisional_job_id,
        )
        envelope_supplied = all(
            body.get(k) for k in (
                'model_hash', 'container_digest', 'seed',
                'input_payload_hash', 'input_schema_hash',
            )
        )
        if not envelope_supplied:
            log.info(
                f"submit: deriving determinism envelope for "
                f"dev={developer_addr[:12]}.. (SDK v1 client)"
            )

        tier = body.get('tier', 'standard')
        if tier not in ('standard', 'redundant'):
            return _err(400, f'unknown tier: {tier!r}')

        try:
            job, receipt = self.engine.submit_job_with_verification(
                developer_addr     = developer_addr,
                job_type           = body.get('job_type', 'inference'),
                model_id           = body.get('model_id', ''),
                coin               = coin,
                model_hash         = body.get('model_hash')         or derived['model_hash'],
                container_digest   = body.get('container_digest')   or derived['container_digest'],
                seed               = int(body.get('seed') or derived['seed']),
                input_payload_hash = body.get('input_payload_hash') or derived['input_payload_hash'],
                input_schema_hash  = body.get('input_schema_hash')  or derived['input_schema_hash'],
                gpu_count          = int(body.get('gpu_count', 1)),
                duration_hr        = float(body.get('duration_hr', 1.0)),
                inputs             = body.get('inputs') or [],
                task               = body.get('task', 'text-generation'),
                params             = body.get('params') or {},
                tier               = tier,
            )
        except JobValidationError as e:
            return _err(400, f'determinism envelope invalid: {e}')
        except Exception as e:
            log.exception(f"submit failed: {e}")
            return _err(500, f'submit failed: {e}')

        # Assignment depends on tier:
        #  standard  → assign_job picks 1 miner (or returns None if no pool)
        #  redundant → assign_redundant_job picks N=3 distinct miners
        if tier == 'redundant':
            picks = self.engine.assign_redundant_job(job.job_id)
            assigned = len(picks) == 0 and False or (len(picks) > 0)
            return _ok({
                'job_id'             : job.job_id,
                'tier'               : 'redundant',
                'status'             : job.status,
                'assigned_miners'    : picks,
                'assigned'           : assigned,
                'usdc_cost'          : receipt.gross_usd,
                'oby_per_miner'      : job.oby_to_miner,
                'oby_total'          : round(
                    job.oby_to_miner * len(picks) if picks else 0, 8
                ),
                'consensus_deadline' : job.consensus_deadline,
                'stablecoin'         : job.stablecoin,
                'model_hash'         : job.model_hash,
                'seed'               : job.seed,
            })

        # Standard tier
        miner = self.engine.assign_job(job.job_id)
        return _ok({
            'job_id'      : job.job_id,
            'tier'        : 'standard',
            'status'      : job.status,
            'miner_addr'  : job.miner_addr or '',
            'assigned'    : miner is not None,
            'usdc_cost'   : receipt.gross_usd,
            'oby_reward'  : job.oby_to_miner,
            'stablecoin'  : job.stablecoin,
            'model_hash'  : job.model_hash,
            'seed'        : job.seed,
        })

    def job_status(self, body: dict) -> tuple[int, dict]:
        """POST /compute/job — poll job status.

        Returns the standard set of job fields for all jobs, plus tier-specific
        extras for redundant jobs (assigned_miners, submissions count, winners,
        outliers, consensus_deadline). Devs polling redundant jobs can use the
        submissions count to track progress toward finalization.
        """
        job_id = body.get('job_id', '')
        job = self.engine.get_job(job_id)
        if not job:
            return _err(404, 'job not found')

        resp = {
            'job_id'      : job.job_id,
            'tier'        : job.tier,
            'status'      : job.status,
            'miner_addr'  : job.miner_addr,
            'model_id'    : job.model_id,
            'result_cid'  : job.result_cid,
            'result_hash' : job.result_hash,
            'usdc_cost'   : job.usd_paid,
            'oby_reward'  : job.oby_to_miner,
            'refund_oby'  : job.refund_oby,
            'created_at'  : job.created_at,
            'completed_at': job.completed_at,
        }
        if job.tier == 'redundant':
            resp.update({
                'assigned_miners'   : list(job.assigned_miners),
                'submissions_count' : len(job.result_submissions),
                'submissions_needed': len(job.assigned_miners),
                'consensus_winners' : list(job.consensus_winners),
                'consensus_outliers': list(job.consensus_outliers),
                'consensus_deadline': job.consensus_deadline,
            })
        return _ok(resp)

    def infer(self, body: dict) -> tuple[int, dict]:
        """
        POST /compute/infer — synchronous inference.

        Submits the job, waits up to INFER_TIMEOUT_S for a miner result,
        then returns the result or a timeout error.
        """
        # Force job_type to inference and rebuild as a /submit call
        sub_body = dict(body)
        sub_body.setdefault('job_type', 'inference')
        sub_body.setdefault('gpu_count', 1)
        sub_body.setdefault('duration_hr', 0.05)   # short — inference is fast
        code, resp = self.submit(sub_body)
        if code != 200:
            return code, resp

        job_id = resp['job_id']
        deadline = time.time() + self.INFER_TIMEOUT_S
        while time.time() < deadline:
            job = self.engine.get_job(job_id)
            if job and job.status == 'done':
                return _ok({
                    'job_id'     : job.job_id,
                    'outputs'    : [],   # populated when miner runtime returns text
                    'result_cid' : job.result_cid,
                    'result_hash': job.result_hash,
                    'latency_ms' : (job.completed_at - job.created_at) * 1000
                                    if job.completed_at else 0,
                    'method'     : 'optimistic',
                })
            if job and job.status == 'faulted':
                return _err(503, 'miner faulted — automatic retry not yet wired')
            time.sleep(self.INFER_POLL_INTERVAL_S)
        return _err(504, f'inference timed out after {self.INFER_TIMEOUT_S}s')

    # ── Miner endpoints ────────────────────────────────────────────────────────

    def register_miner(self, body: dict) -> tuple[int, dict]:
        """POST /compute/register — miner registers with this node."""
        from tokenomics.engine import MinerProfile

        addr = body.get('address', '')
        if not _is_valid_address(addr):
            return _err(400, 'invalid address')
        stake_oby = float(body.get('stake_oby', 0))
        if stake_oby < 0:
            return _err(400, 'negative stake')

        profile = MinerProfile(
            address        = addr,
            gpu_model      = body.get('gpu_model', 'unknown'),
            gpu_count      = int(body.get('gpu_count', 1)),
            vram_gb        = int(body.get('vram_gb', 0)),
            bandwidth_gbps = float(body.get('bandwidth_gbps', 1.0)),
            region         = body.get('region', 'unknown'),
            stake_oby      = stake_oby,
        )
        self.engine.register_miner(profile)
        log.info(
            f"miner registered: {addr[:16]} "
            f"stake={stake_oby:.2f} OBY {profile.gpu_count}x{profile.gpu_model}"
        )
        return _ok({
            'registered': True,
            'address'   : addr,
            'tier'      : 'new',
        })

    def heartbeat(self, body: dict) -> tuple[int, dict]:
        """POST /compute/heartbeat — miner liveness ping."""
        addr = body.get('address', '')
        if not addr:
            return _err(400, 'missing address')
        uptime_s = float(body.get('uptime_s', 0))
        ok = self.engine.record_heartbeat(addr)
        if not ok:
            return _err(404, 'miner not registered')
        if uptime_s > 0:
            self.engine.record_uptime(addr, uptime_s / 3600.0)
        return _ok({'acknowledged': True})

    @staticmethod
    def _job_payload_for_miner(j) -> dict:
        """The job payload that gets sent to a miner via /compute/nextjob.
        Includes inputs/task/params so the miner can actually run the work,
        plus the full determinism envelope so it can validate before running."""
        return {
            'job_id'            : j.job_id,
            'job_type'          : j.job_type,
            'model_id'          : j.model_id,
            'gpu_hours'         : j.gpu_hours,
            'model_hash'        : j.model_hash,
            'container_digest'  : j.container_digest,
            'seed'              : j.seed,
            'input_payload_hash': j.input_payload_hash,
            'input_schema_hash' : j.input_schema_hash,
            'inputs'            : j.inputs,
            'task'              : j.task,
            'params'            : j.params,
            'oby_reward'        : j.oby_to_miner,
        }

    def next_job(self, query_string: str) -> tuple[int, dict]:
        """GET /compute/nextjob?address=...&job_type=... — miner pulls work.

        Two-pass: first return any job already 'assigned' to this miner that
        hasn't been completed (the miner may have crashed and is re-polling
        to recover). Second, try assigning a pending job to this miner via
        the stake-weighted draw and return it if it lands here.

        Handles both standard and redundant tiers:
          - Standard: a job is "this miner's" when j.miner_addr == miner_addr
          - Redundant: a job is "this miner's" when miner_addr in
            j.assigned_miners AND this miner hasn't yet submitted a result
            for it (otherwise we'd hand the same job back to a miner who's
            done their part)
        """
        params = dict(urllib.parse.parse_qsl(query_string))
        miner_addr = params.get('address', '')
        if not miner_addr:
            return _err(400, 'missing ?address=')

        # Pass 1: pick up an existing assignment if one exists
        with self.engine._lock:
            for j in self.engine._jobs.values():
                if j.status != 'assigned':
                    continue
                if j.tier == 'redundant':
                    # Redundant: skip if this miner isn't assigned, or has
                    # already submitted (would otherwise re-issue the same
                    # work the miner has finished)
                    if miner_addr not in j.assigned_miners:
                        continue
                    if miner_addr in j.result_submissions:
                        continue
                    return _ok(self._job_payload_for_miner(j))
                # Standard tier
                if j.miner_addr == miner_addr:
                    return _ok(self._job_payload_for_miner(j))

        # Pass 2: try assigning a pending job (stake-weighted draw)
        for job_id in self.engine.pending_jobs_for_assignment():
            # Redundant jobs get assigned to N miners simultaneously in
            # submit(), not here. pending_jobs_for_assignment only returns
            # status='pending' which should never include redundant jobs
            # post-submit, but guard anyway.
            job = self.engine.get_job(job_id)
            if job and job.tier == 'redundant':
                continue
            assigned_to = self.engine.assign_job(job_id)
            if assigned_to == miner_addr:
                job = self.engine.get_job(job_id)
                return _ok(self._job_payload_for_miner(job))
        # Nothing available right now — 200 with empty body so miner polls again
        return _ok({})

    def submit_result(self, body: dict) -> tuple[int, dict]:
        """POST /compute/result — miner reports a completed job."""
        job_id      = body.get('job_id', '')
        miner_addr  = body.get('miner_addr', '')
        result_cid  = body.get('result_cid', '')
        result_hash = body.get('result_hash', '')
        zk_proof    = body.get('zk_proof', '')

        if not job_id or not miner_addr:
            return _err(400, 'missing job_id or miner_addr')
        if body.get('status') == 'failed':
            # Miner self-reported failure; mark the job faulted directly
            job = self.engine.get_job(job_id)
            if job:
                with self.engine._lock:
                    job.status = 'faulted'
            return _ok({'acknowledged': True, 'status': 'faulted'})

        if not result_hash:
            return _err(400, 'missing result_hash')

        try:
            result = self.engine.complete_job_with_verification(
                job_id      = job_id,
                miner_addr  = miner_addr,
                result_cid  = result_cid,
                result_hash = result_hash,
                zk_proof    = zk_proof,
            )
        except ValueError as e:
            return _err(400, str(e))
        except Exception as e:
            log.exception(f"submit_result failed: {e}")
            return _err(500, str(e))

        job = self.engine.get_job(job_id)
        return _ok({
            'job_id'    : job_id,
            'method'    : result.method,
            'passed'    : result.passed,
            'details'   : result.details,
            'oby_reward': job.oby_to_miner if (job and result.passed
                                                and result.method in ('optimistic', 'zk'))
                          else 0.0,
        })

    # ── Challenger endpoint ────────────────────────────────────────────────────

    def pending_challenges(self, query_string: str) -> tuple[int, dict]:
        """GET /compute/pending_challenges?challenger_addr=... — returns the
        list of challenges currently awaiting a verifier rerun.

        Includes the full work payload (model_id, inputs, task, params) so
        the challenger can reproduce the work locally. The pinned envelope
        (model_hash, container_digest, inference_seed) lets the challenger
        validate it's running the same code as the miner did.

        Anyone can poll; the engine's resolve_challenge enforces the verdict
        (the rerun_hash is what matters, not who submitted it).
        """
        challenges = self.engine.verification.pending_challenges()
        out = []
        for c in challenges:
            job = self.engine.get_job(c.job_id)
            out.append({
                'challenge_id'      : c.challenge_id,
                'job_id'            : c.job_id,
                'miner_addr'        : c.miner_addr,
                'result_hash'       : c.result_hash,
                # Determinism envelope — what the rerun MUST use
                'model_hash'        : c.model_hash,
                'container_digest'  : c.container_digest,
                'inference_seed'    : c.inference_seed,
                # Work payload — what to actually run
                'job_type'          : job.job_type if job else 'inference',
                'model_id'          : job.model_id if job else '',
                'inputs'            : job.inputs if job else [],
                'task'              : job.task if job else 'text-generation',
                'params'            : job.params if job else {},
                'created_at'        : c.created_at,
                'expires_at'        : c.expires_at,
            })
        return _ok({
            'challenges': out,
            'count'     : len(out),
        })

    def resolve_challenge(self, body: dict) -> tuple[int, dict]:
        """POST /compute/challenge_resolve — challenger reports rerun verdict."""
        challenge_id = body.get('challenge_id', '')
        rerun_hash   = body.get('rerun_hash', '')
        if not challenge_id or not rerun_hash:
            return _err(400, 'missing challenge_id or rerun_hash')
        status = self.engine.resolve_job_challenge(challenge_id, rerun_hash)
        return _ok({
            'challenge_id': challenge_id,
            'status'      : status.value,
        })


__all__ = ['ComputeAPI']
