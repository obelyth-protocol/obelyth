"""
Smoke runner for Phase 3.3 — challenger validator process.

End-to-end test: boots a real FullNode, registers a miner, submits a job,
miner submits a result that triggers a challenge, then a ChallengerDaemon
fetches the pending challenge, reruns the work, and posts the verdict.

Tests two scenarios:
  1. Honest miner: miner returns the same hash the challenger computes
     → on_pass fires, miner credited, job marked done
  2. Faulty miner: miner returns a wrong hash, challenger's rerun matches
     the expected output (computed via rerun_inference()) but NOT the
     miner's submitted hash → on_slash fires, dev gets refund, miner faulted

Also covers:
  - /compute/pending_challenges returns the full envelope + work payload
  - ChallengerDaemon.run_once() processes available challenges synchronously
  - Pending challenges list shrinks after resolution
  - Stats tracking (passed_count, failed_count) on the daemon

Uses the deterministic-isolation pattern from smoke_compute_api.py: ban all
miners except the one being tested so assignment lottery doesn't interfere.
"""

import sys
import os
import json
import hashlib
import time
import tempfile
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from node.fullnode import FullNode, RPCHandler
from compute.challenger import ChallengerDaemon, rerun_inference


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


def post(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def find_free_port():
    import socket
    s = socket.socket()
    s.bind(('', 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── Boot FullNode ───────────────────────────────────────────────────────────

P2P_PORT = find_free_port()
RPC_PORT = find_free_port()
TMPDIR   = tempfile.mkdtemp(prefix='obelyth-challenger-test-')
BASE_URL = f'http://127.0.0.1:{RPC_PORT}'

print(f"Booting FullNode on RPC port {RPC_PORT}")
node = FullNode(
    p2p_port=P2P_PORT,
    rpc_port=RPC_PORT,
    data_dir=TMPDIR,
    mine=False,
)
RPCHandler.node = node
rpc_server = HTTPServer(('127.0.0.1', RPC_PORT), RPCHandler)
threading.Thread(target=rpc_server.serve_forever, daemon=True).start()
time.sleep(0.3)

# Seed oracle rates
from tokenomics.engine import Stablecoin
node.tokenomics.update_rate(Stablecoin.USDC, 1.0)
node.tokenomics.update_rate(Stablecoin.DAI, 1.0)
node.tokenomics.update_rate(Stablecoin.USDT, 1.0)
node.tokenomics.update_rate(Stablecoin.EURC, 1.08)


# ── /compute/pending_challenges with no challenges ─────────────────────────
section("/compute/pending_challenges — empty list")

code, body = get(f'{BASE_URL}/compute/pending_challenges')
check("empty pending_challenges returns 200", code == 200)
check("returns 'challenges' list", isinstance(body.get('challenges'), list))
check("count == 0 initially", body.get('count') == 0)


# ── Register a miner ────────────────────────────────────────────────────────
section("Register honest miner")

MINER_ADDR = 'OBYhonestMiner111111111111111111'
code, body = post(f'{BASE_URL}/compute/register', {
    'address': MINER_ADDR,
    'gpu_model': 'A100', 'gpu_count': 1, 'vram_gb': 80,
    'bandwidth_gbps': 10.0, 'region': 'us-east',
    'stake_oby': 10000.0,
})
check("miner registered", code == 200)


# Make this miner the only eligible one
def isolate_miner(target_addr):
    """Ban all miners except target so assignment is deterministic."""
    orig = {}
    for addr, m in list(node.tokenomics._miners.items()):
        if addr != target_addr:
            orig[addr] = m.banned_until_block
            m.banned_until_block = 10**12
    return orig


def restore_miners(orig):
    for addr, ban in orig.items():
        m = node.tokenomics._miners.get(addr)
        if m:
            m.banned_until_block = ban


# ── Helper: force a challenge for a specific miner ─────────────────────────

def force_challenge_for(miner_addr, dev_label, hash_strategy):
    """
    Submit jobs until a challenge fires for `miner_addr`.
    hash_strategy(inputs, seed) returns the result_hash the miner submits.
    Returns (challenge_dict, expected_honest_hash, job_dict) or (None, None, None).

    Note: we isolate the miner first so the assignment is deterministic.
    """
    orig = isolate_miner(miner_addr)
    try:
        for i in range(80):
            # Vary block_hash provider to get different challenge rolls
            bh = hashlib.sha3_256(f'block-{dev_label}-{i}'.encode()).digest()
            node.tokenomics._block_hash = (lambda b=bh: b)
            node.tokenomics.verification._get_block_hash = (lambda b=bh: b)

            inputs = [f'test input {dev_label} {i}']
            seed = 1000 + i
            code, body = post(f'{BASE_URL}/compute/submit', {
                'developer_addr'    : f'dev_{dev_label}_xxxxxxxxxxxxxxxxxxxx',
                'job_type'          : 'inference',
                'model_id'          : 'test-model',
                'coin'              : 'USDC',
                'model_hash'        : _hex(5000 + i),
                'container_digest'  : _digest(2),
                'seed'              : seed,
                'input_payload_hash': _hex(3),
                'input_schema_hash' : _hex(4),
                'inputs'            : inputs,
                'task'              : 'text-generation',
                'duration_hr'       : 0.05,
            })
            if code != 200:
                continue
            job_id = body['job_id']

            # Compute the honest hash the challenger will get
            honest_hash = rerun_inference(
                'test-model', inputs, 'text-generation', {}, seed
            )
            # Compose the hash the "miner" submits
            miner_hash = hash_strategy(inputs, seed, honest_hash)

            code, body = post(f'{BASE_URL}/compute/result', {
                'job_id'     : job_id,
                'miner_addr' : miner_addr,
                'result_cid' : f'cid-{dev_label}-{i}',
                'result_hash': miner_hash,
            })
            if code == 200 and body.get('method') == 'challenged':
                # Find the pending challenge
                for c in node.tokenomics.verification.pending_challenges():
                    if c.job_id == job_id:
                        return c.to_dict(), honest_hash, {
                            'job_id': job_id, 'inputs': inputs, 'seed': seed,
                        }
    finally:
        restore_miners(orig)
    return None, None, None


# ── Scenario 1: HONEST miner — challenger sees match, on_pass fires ────────
section("Scenario 1: Honest miner — challenger sees MATCH")

# Miner returns the same hash the challenger would compute
challenge, honest_hash, jobinfo = force_challenge_for(
    MINER_ADDR, 'honest',
    hash_strategy=lambda inputs, seed, honest: honest,   # honest = honest
)
check("eventually issued a challenge for honest miner", challenge is not None)

if challenge:
    # Check pending_challenges response shape
    code, body = get(f'{BASE_URL}/compute/pending_challenges')
    check("pending_challenges shows 1 challenge", body.get('count') == 1)
    if body.get('challenges'):
        ch = body['challenges'][0]
        check("challenge includes model_hash", _hex(0) != ch.get('model_hash', '')
              and len(ch.get('model_hash', '')) == 64)
        check("challenge includes container_digest",
              ch.get('container_digest', '').startswith('sha256:'))
        check("challenge includes inference_seed",
              isinstance(ch.get('inference_seed'), int))
        check("challenge includes inputs payload",
              ch.get('inputs') == jobinfo['inputs'])
        check("challenge includes task", ch.get('task') == 'text-generation')

    # Snapshot miner state before resolve
    miner_jobs_before = node.tokenomics._miners[MINER_ADDR].jobs_completed
    miner_earn_before = node.tokenomics._miners[MINER_ADDR].oby_earned

    # Spin up the challenger daemon and process the challenge
    daemon = ChallengerDaemon(
        node_url=BASE_URL,
        challenger_addr='OBYchallenger111111111111111111',
    )
    processed = daemon.run_once()
    check("challenger processed 1 challenge", processed == 1)
    check("challenger stats: passed = 1", daemon.passed_count == 1)
    check("challenger stats: failed = 0", daemon.failed_count == 0)

    # Pending challenges should now be empty
    code, body = get(f'{BASE_URL}/compute/pending_challenges')
    check("pending_challenges is empty after resolution",
          body.get('count') == 0)

    # Miner should have been credited via on_pass callback
    m = node.tokenomics._miners[MINER_ADDR]
    check("on_pass: miner jobs_completed incremented",
          m.jobs_completed == miner_jobs_before + 1)
    check("on_pass: miner oby_earned increased",
          m.oby_earned > miner_earn_before)
    check("on_pass: miner not slashed", m.stake_oby == 10000.0)


# ── Scenario 2: FAULTY miner — challenger sees DIVERGE, on_slash fires ─────
section("Scenario 2: Faulty miner — challenger sees DIVERGE")

# Register a separate faulty miner
FAULTY_ADDR = 'OBYfaultyMiner111111111111111111'
post(f'{BASE_URL}/compute/register', {
    'address': FAULTY_ADDR,
    'gpu_model': 'A100', 'gpu_count': 1, 'vram_gb': 80,
    'bandwidth_gbps': 10.0, 'region': 'us-east',
    'stake_oby': 10000.0,
})

# This miner submits a LYING hash (not the honest one)
challenge, honest_hash, jobinfo = force_challenge_for(
    FAULTY_ADDR, 'faulty',
    hash_strategy=lambda inputs, seed, honest: _hex(99999),  # lie
)
check("eventually issued a challenge for faulty miner",
      challenge is not None)

if challenge:
    # Snapshot before
    fm = node.tokenomics._miners[FAULTY_ADDR]
    stake_before = fm.stake_oby
    offence_before = fm.offence_count

    # Run the challenger
    daemon = ChallengerDaemon(
        node_url=BASE_URL,
        challenger_addr='OBYchallenger111111111111111111',
    )
    processed = daemon.run_once()
    check("challenger processed 1 faulty challenge", processed == 1)
    check("challenger stats: passed = 0", daemon.passed_count == 0)
    check("challenger stats: failed = 1", daemon.failed_count == 1)

    # Pending now empty
    code, body = get(f'{BASE_URL}/compute/pending_challenges')
    check("pending_challenges empty after fault resolution",
          body.get('count') == 0)

    # Miner should be slashed
    fm = node.tokenomics._miners[FAULTY_ADDR]
    check("on_slash: faulty miner offence_count incremented",
          fm.offence_count == offence_before + 1)
    check("on_slash: faulty miner stake reduced ~20%",
          abs(fm.stake_oby - stake_before * 0.8) < 0.01)
    check("on_slash: faulty miner reputation reset to 0",
          fm.reputation == 0.0)

    # Job marked faulted
    job = node.tokenomics.get_job(jobinfo['job_id'])
    check("faulty job marked 'faulted'", job is not None and job.status == 'faulted')
    check("dev refund recorded on job", job is not None and job.refund_oby > 0)


# ── Multiple pending challenges → daemon handles all ───────────────────────
section("Multiple pending challenges processed in one run_once()")

# Force two more challenges back-to-back (use the honest miner again)
for label, strategy in (
    ('multi1', lambda i, s, h: h),       # honest
    ('multi2', lambda i, s, h: _hex(7)), # lying
):
    c, _, _ = force_challenge_for(MINER_ADDR, label, hash_strategy=strategy)
    if c is None:
        # Honest miner is now slashed so wouldn't be selected — accept that
        # and break out
        break

pending_before = len(node.tokenomics.verification.pending_challenges())
if pending_before >= 1:
    daemon = ChallengerDaemon(
        node_url=BASE_URL,
        challenger_addr='OBYchallenger111111111111111111',
    )
    processed = daemon.run_once()
    check(f"daemon processed all {pending_before} pending in one call",
          processed == pending_before)
    code, body = get(f'{BASE_URL}/compute/pending_challenges')
    check("pending_challenges drained to 0", body.get('count') == 0)
else:
    print("  (skipped — could not force multi-challenge scenario)")


# ── ChallengerDaemon start/stop lifecycle ──────────────────────────────────
section("ChallengerDaemon background-thread lifecycle")

daemon = ChallengerDaemon(
    node_url=BASE_URL,
    challenger_addr='OBYbgchallenger1111111111111111',
    poll_interval_s=0.5,
)
daemon.start()
check("daemon is_running after start", daemon._running)
check("daemon thread is alive", daemon._thread is not None
      and daemon._thread.is_alive())

time.sleep(1.0)   # Let it run a couple polls

daemon.stop()
time.sleep(1.0)   # Let it observe stop
check("daemon stopped (_running false)", not daemon._running)


# ── Shutdown ────────────────────────────────────────────────────────────────
rpc_server.shutdown()
import shutil
try:
    shutil.rmtree(TMPDIR)
except Exception:
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
print(f"  All checks green. Phase 3.3 challenger ready.")
sys.exit(0)
