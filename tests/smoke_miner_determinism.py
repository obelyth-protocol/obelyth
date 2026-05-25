"""
Smoke runner for Phase 3.2 — miner-side determinism enforcement.

Tests:
  - _validate_envelope() accepts valid envelopes
  - _validate_envelope() rejects malformed model_hash
  - _validate_envelope() rejects malformed container_digest
  - _validate_envelope() rejects out-of-range seed
  - _validate_envelope() rejects non-int seed
  - run_inference() with seed=N twice yields identical result_hash (determinism)
  - run_inference() with different seeds yields different result_hash
  - run_inference() with different inputs yields different result_hash
  - _execute_job() refuses to run when envelope is malformed (no result POST happens beyond failure)

These tests exercise the miner runtime directly — no live FullNode needed.
"""

import sys
import os
import hashlib
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compute.miner import MinerDaemon, JobRunner


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


def _hex(seed): return hashlib.sha256(str(seed).encode()).hexdigest()
def _digest(seed): return "sha256:" + _hex(seed)


def make_job(model_hash=None, container_digest=None, seed=42):
    return {
        'job_id'           : 'test-job-1',
        'job_type'         : 'inference',
        'model_id'         : 'test-model',
        'model_hash'       : model_hash if model_hash is not None else _hex(1),
        'container_digest' : container_digest if container_digest is not None else _digest(2),
        'seed'             : seed,
        'inputs'           : ['hello world'],
        'task'             : 'text-generation',
        'params'           : {},
    }


# Build a real MinerDaemon without starting the network thread
daemon = MinerDaemon.__new__(MinerDaemon)
daemon.address = 'OBYminer1xxxxxxxxxxxxxxxxxxxxxxxx'
daemon._earnings = 0.0
daemon.runner = JobRunner()
# We never start the HTTP loop, so no _post or _get calls happen


# ─── envelope validation ────────────────────────────────────────────────────
section("_validate_envelope: format validation")

ok, err = daemon._validate_envelope(make_job())
check("valid envelope accepted", ok, err)

ok, err = daemon._validate_envelope(make_job(model_hash='not-a-hash'))
check("malformed model_hash rejected", not ok)
check("error message mentions model_hash",
      'model_hash' in err.lower())

ok, err = daemon._validate_envelope(make_job(model_hash='abc'))
check("short model_hash rejected", not ok)

ok, err = daemon._validate_envelope(make_job(model_hash=_hex(1).upper()))
# Uppercase hex should still pass since we normalize via .lower()
check("uppercase hex model_hash accepted", ok, err)

ok, err = daemon._validate_envelope(make_job(container_digest='latest'))
check("non-sha256 container_digest rejected", not ok)
check("error mentions container_digest",
      'container_digest' in err.lower())

ok, err = daemon._validate_envelope(make_job(container_digest='sha256:xyz'))
check("bad-hex container_digest rejected", not ok)

ok, err = daemon._validate_envelope(make_job(seed=-1))
check("negative seed rejected", not ok)
check("error mentions uint64", 'uint64' in err.lower())

ok, err = daemon._validate_envelope(make_job(seed=2**64))
check("seed too large rejected", not ok)

ok, err = daemon._validate_envelope(make_job(seed='42'))
check("string seed rejected", not ok)

ok, err = daemon._validate_envelope(make_job(seed=2**64 - 1))
check("max uint64 seed accepted", ok)


# ─── determinism: same seed → same hash ─────────────────────────────────────
section("Inference determinism (same envelope → same result_hash)")

r1 = daemon.runner.run_inference(
    model_id='test-model',
    inputs=['hello world', 'foo bar'],
    task='text-generation',
    params={},
    seed=42,
)
r2 = daemon.runner.run_inference(
    model_id='test-model',
    inputs=['hello world', 'foo bar'],
    task='text-generation',
    params={},
    seed=42,
)
check("same inputs + same seed → identical result_hash",
      r1['result_hash'] == r2['result_hash'],
      f"r1={r1['result_hash'][:12]} r2={r2['result_hash'][:12]}")

r3 = daemon.runner.run_inference(
    model_id='test-model',
    inputs=['hello world', 'foo bar'],
    task='text-generation',
    params={},
    seed=43,  # different seed
)
check("same inputs + different seed → different result_hash",
      r1['result_hash'] != r3['result_hash'])

r4 = daemon.runner.run_inference(
    model_id='test-model',
    inputs=['DIFFERENT INPUT'],
    task='text-generation',
    params={},
    seed=42,
)
check("different inputs + same seed → different result_hash",
      r1['result_hash'] != r4['result_hash'])


# ─── result_hash format ─────────────────────────────────────────────────────
section("result_hash format conforms to verification engine expectations")

check("result_hash is 64-char hex (SHA-256)",
      len(r1['result_hash']) == 64
      and all(c in '0123456789abcdef' for c in r1['result_hash']))


# ─── _execute_job rejects malformed envelopes ───────────────────────────────
section("_execute_job rejects malformed envelopes without executing")

# Stub _post to capture what gets sent
posted = []
def capture_post(path, body):
    posted.append((path, body))
    return {'method': 'optimistic', 'passed': True, 'oby_reward': 0.0}
daemon._post = capture_post

posted.clear()
daemon._execute_job(make_job(model_hash='not-a-hash'))
check("malformed envelope POSTs a 'failed' status",
      len(posted) == 1 and posted[0][1].get('status') == 'failed')
check("failure payload includes error message",
      posted[0][1].get('error', '').startswith('envelope validation'))

posted.clear()
daemon._execute_job(make_job(seed=-1))
check("bad-seed envelope POSTs 'failed' status",
      len(posted) == 1 and posted[0][1].get('status') == 'failed')

posted.clear()
daemon._execute_job(make_job(container_digest='latest'))
check("bad-container envelope POSTs 'failed' status",
      len(posted) == 1 and posted[0][1].get('status') == 'failed')


# ─── _execute_job runs valid envelopes ──────────────────────────────────────
section("_execute_job executes valid envelopes")

posted.clear()
daemon._execute_job(make_job())   # valid envelope
check("valid envelope POSTs a result (not 'failed')",
      len(posted) == 1 and posted[0][1].get('status') != 'failed')
check("posted result_hash is 64-char hex",
      len(posted[0][1].get('result_hash', '')) == 64)


# ─── Final report ──────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  PASSED: {PASSED}")
print(f"  FAILED: {len(FAILED)}")
if FAILED:
    print(f"\nFailures:")
    for name, detail in FAILED:
        print(f"  - {name}: {detail}")
    sys.exit(1)
print(f"  All checks green. Phase 3.2 determinism enforcement ready.")
sys.exit(0)
