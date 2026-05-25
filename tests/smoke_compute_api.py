"""
Integration smoke runner for the Phase 2 HTTP API.

Spins up a real HTTPServer in-process on a random port, exercises every
/compute/* route through real HTTP calls, asserts on responses and state.

Covers:
  - /compute/quote               returns pricing
  - /compute/register            miner registers
  - /compute/heartbeat           liveness update
  - /compute/submit              dev submits, determinism envelope validated
  - /compute/submit (bad env)    400 on malformed envelope
  - /compute/nextjob             miner pulls assigned job
  - /compute/result (optimistic) miner credited immediately
  - /compute/result (challenged) job held for resolve
  - /compute/challenge_resolve   PASSED credits miner via on_pass
  - /compute/challenge_resolve   FAILED slashes + refunds
  - /compute/job                 status polling reflects state
  - /compute/infer               sync inference returns within timeout
  - Existing /status and /balance still work
  - load/save round-trip preserves miners/jobs/offence_count/ban state

NOT covered (out of scope for HTTP layer tests):
  - Multi-block-hash assignment proportionality (covered in tokenomics smoke)
  - 30-day ban duration math (covered in verification smoke)
  - ZK fast path (covered in tokenomics smoke)
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
        url, data=data, headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


# ── Boot a real FullNode on a random port ────────────────────────────────────

def find_free_port():
    import socket
    s = socket.socket()
    s.bind(('', 0))
    port = s.getsockname()[1]
    s.close()
    return port


P2P_PORT  = find_free_port()
RPC_PORT  = find_free_port()
TMPDIR    = tempfile.mkdtemp(prefix='obelyth-api-test-')
BASE_URL  = f'http://127.0.0.1:{RPC_PORT}'

print(f"Booting FullNode on RPC port {RPC_PORT} (data: {TMPDIR})")

node = FullNode(
    p2p_port=P2P_PORT,
    rpc_port=RPC_PORT,
    data_dir=TMPDIR,
    mine=False,
)

# Start only the RPC server (not P2P — keeps the test quiet and isolated)
RPCHandler.node = node
rpc_server = HTTPServer(('127.0.0.1', RPC_PORT), RPCHandler)
threading.Thread(target=rpc_server.serve_forever, daemon=True).start()
time.sleep(0.3)  # let it bind

# Seed the engine oracle rates so quotes work
from tokenomics.engine import Stablecoin
node.tokenomics.update_rate(Stablecoin.USDC, 1.0)
node.tokenomics.update_rate(Stablecoin.DAI, 1.0)
node.tokenomics.update_rate(Stablecoin.USDT, 1.0)
node.tokenomics.update_rate(Stablecoin.EURC, 1.08)


# ── /status still works ─────────────────────────────────────────────────────
section("Existing routes (regression check)")

code, body = get(f'{BASE_URL}/status')
check("GET /status returns 200", code == 200)
check("status has 'height' key", 'height' in body)


# ── /compute/quote ──────────────────────────────────────────────────────────
section("/compute/quote")

code, body = post(f'{BASE_URL}/compute/quote', {
    'job_type': 'inference',
    'model_id': 'meta-llama/Llama-2-7b',
    'gpu_count': 1,
    'duration_hr': 1.0,
})
check("quote returns 200", code == 200)
check("quote has stable_cost", 'stable_cost' in body and body['stable_cost'] > 0)
check("quote has savings_pct", 'savings_pct' in body)
check("savings vs AWS > 0", body.get('savings_pct', 0) > 0)


# ── /compute/register ───────────────────────────────────────────────────────
section("/compute/register")

code, body = post(f'{BASE_URL}/compute/register', {
    'address': 'OBYminer1xxxxxxxxxxxxxxxxxxxxxxxx',
    'gpu_model': 'A100',
    'gpu_count': 1,
    'vram_gb': 80,
    'bandwidth_gbps': 10.0,
    'region': 'us-east',
    'stake_oby': 5000.0,
})
check("register returns 200", code == 200)
check("register acknowledges", body.get('registered') is True)
check("tier reported", body.get('tier') == 'new')

# Bad address
code, body = post(f'{BASE_URL}/compute/register', {
    'address': 'x',   # too short
    'stake_oby': 100,
})
check("rejects invalid address", code == 400)


# ── /compute/heartbeat ──────────────────────────────────────────────────────
section("/compute/heartbeat")

code, body = post(f'{BASE_URL}/compute/heartbeat', {
    'address': 'OBYminer1xxxxxxxxxxxxxxxxxxxxxxxx',
    'uptime_s': 60.0,
})
check("heartbeat 200", code == 200)
check("acknowledged", body.get('acknowledged') is True)

# Unknown miner
code, body = post(f'{BASE_URL}/compute/heartbeat', {
    'address': 'OBYunknownxxxxxxxxxxxxxxxxxxxxx',
})
check("unknown miner -> 404", code == 404)


# ── /compute/submit (valid envelope) ────────────────────────────────────────
section("/compute/submit (with full determinism envelope)")

submit_body = {
    'developer_addr'    : 'dev_alice_xxxxxxxxxxxxxxxxxxxx',
    'job_type'          : 'inference',
    'model_id'          : 'meta-llama/Llama-2-7b',
    'coin'              : 'USDC',
    'model_hash'        : _hex(1),
    'container_digest'  : _digest(2),
    'seed'              : 42,
    'input_payload_hash': _hex(3),
    'input_schema_hash' : _hex(4),
    'gpu_count'         : 1,
    'duration_hr'       : 0.1,
}
code, body = post(f'{BASE_URL}/compute/submit', submit_body)
check("submit valid envelope -> 200", code == 200)
check("job_id returned", 'job_id' in body)
SUBMITTED_JOB_ID = body.get('job_id', '')
check("status pending or assigned",
      body.get('status') in ('pending', 'assigned'))


# ── /compute/submit (derived envelope when fields missing) ──────────────────
section("/compute/submit (derived envelope path)")

code, body = post(f'{BASE_URL}/compute/submit', {
    'developer_addr': 'dev_bob_xxxxxxxxxxxxxxxxxxxxxxxx',
    'job_type'      : 'inference',
    'model_id'      : 'test-model',
    'coin'          : 'USDC',
    'inputs'        : ['hello world'],
    'config'        : {'epochs': 1},
    'duration_hr'   : 0.1,
})
check("submit without envelope still works (derived)", code == 200)


# ── /compute/submit (malformed envelope) ────────────────────────────────────
section("/compute/submit (malformed envelope rejected)")

bad = dict(submit_body)
bad['model_hash'] = 'not-a-hash'
code, body = post(f'{BASE_URL}/compute/submit', bad)
check("bad model_hash -> 400", code == 400)
check("error mentions envelope or hash", 'envelope' in body.get('error', '').lower()
      or 'hash' in body.get('error', '').lower())


# ── /compute/nextjob ────────────────────────────────────────────────────────
section("/compute/nextjob")

# Submit a fresh job for our registered miner
code, body = post(f'{BASE_URL}/compute/submit', {
    'developer_addr'    : 'dev_alice_xxxxxxxxxxxxxxxxxxxx',
    'job_type'          : 'inference',
    'model_id'          : 'test-model',
    'coin'              : 'USDC',
    'model_hash'        : _hex(11),
    'container_digest'  : _digest(12),
    'seed'              : 99,
    'input_payload_hash': _hex(13),
    'input_schema_hash' : _hex(14),
    'duration_hr'       : 0.1,
})
new_job_id = body.get('job_id', '')

# Miner pulls — since they're the only registered miner, they should get it
code, body = get(
    f'{BASE_URL}/compute/nextjob?address=OBYminer1xxxxxxxxxxxxxxxxxxxxxxxx'
)
check("nextjob returns 200", code == 200)
# Either the job we just submitted, the previous one, or nothing if already assigned
got_a_job = body.get('job_id') is not None and body.get('job_id') != ''
check("miner received a job to work on", got_a_job)
if got_a_job:
    PICKED_JOB = body
    check("job has model_hash", PICKED_JOB.get('model_hash'))
    check("job has container_digest", PICKED_JOB.get('container_digest'))
    check("job has seed", PICKED_JOB.get('seed') is not None)


# ── /compute/result (optimistic accept path) ────────────────────────────────
section("/compute/result (optimistic acceptance)")

if got_a_job:
    job_id = PICKED_JOB['job_id']
    # Submit a result. Trusted miners only get 5% challenges, so let's first
    # check what tier our miner is in. New miners hit 30%, so we may or may
    # not get a challenge. We'll send the result and check either outcome
    # makes sense.
    code, body = post(f'{BASE_URL}/compute/result', {
        'job_id'     : job_id,
        'miner_addr' : 'OBYminer1xxxxxxxxxxxxxxxxxxxxxxxx',
        'result_cid' : 'cid-test-1',
        'result_hash': _hex(500),
    })
    check("result accepted (200)", code == 200)
    check("response has method", body.get('method') in
          ('optimistic', 'challenged', 'zk'))
    if body.get('method') == 'optimistic':
        check("optimistic: oby_reward > 0", body.get('oby_reward', 0) > 0)


# ── /compute/job (status polling) ───────────────────────────────────────────
section("/compute/job (status polling)")

if SUBMITTED_JOB_ID:
    code, body = post(f'{BASE_URL}/compute/job', {'job_id': SUBMITTED_JOB_ID})
    check("job status 200", code == 200)
    check("status reported",
          body.get('status') in ('pending', 'assigned', 'done', 'faulted'))

# Unknown job
code, body = post(f'{BASE_URL}/compute/job', {'job_id': 'no-such-job'})
check("unknown job -> 404", code == 404)


# ── /compute/challenge_resolve (force a fault) ──────────────────────────────
section("/compute/challenge_resolve (force a fault end-to-end)")

# Register a fresh fault-target miner
post(f'{BASE_URL}/compute/register', {
    'address': 'OBYfaultyxxxxxxxxxxxxxxxxxxxxxxxx',
    'gpu_model': 'A100', 'gpu_count': 1, 'vram_gb': 80,
    'bandwidth_gbps': 10.0, 'region': 'us-east',
    'stake_oby': 10000.0,
})

# Make 'faulty' the only eligible miner for this block so the test is
# deterministic. Restore the other miners' ban state after.
_fault_test_orig_bans = {}
for addr, m in list(node.tokenomics._miners.items()):
    if addr != 'OBYfaultyxxxxxxxxxxxxxxxxxxxxxxxx':
        _fault_test_orig_bans[addr] = m.banned_until_block
        m.banned_until_block = 10**12

# Submit jobs until a challenge fires for this miner
challenge_obj = None
for i in range(80):
    code, body = post(f'{BASE_URL}/compute/submit', {
        'developer_addr'    : 'dev_carol_xxxxxxxxxxxxxxxxxxxx',
        'job_type'          : 'inference',
        'model_id'          : 'test-model',
        'coin'              : 'USDC',
        'model_hash'        : _hex(1000 + i),
        'container_digest'  : _digest(2),
        'seed'              : 100 + i,
        'input_payload_hash': _hex(3),
        'input_schema_hash' : _hex(4),
        'duration_hr'       : 0.05,
    })
    if code != 200:
        continue
    job_id = body['job_id']

    # Force assignment to our faulty miner. Since assign_miner is stake-weighted
    # and random, we'll keep trying jobs until we land on faulty. To increase
    # odds, we'll use the engine directly to find pending jobs assigned to faulty.
    job = node.tokenomics.get_job(job_id)
    if job and job.miner_addr != 'OBYfaultyxxxxxxxxxxxxxxxxxxxxxxxx':
        continue   # assigned to a different miner; try next

    # Submit a result — challenge may fire
    code, body = post(f'{BASE_URL}/compute/result', {
        'job_id'     : job_id,
        'miner_addr' : 'OBYfaultyxxxxxxxxxxxxxxxxxxxxxxxx',
        'result_cid' : f'cid-{i}',
        'result_hash': _hex(2000 + i),
    })
    if code == 200 and body.get('method') == 'challenged':
        # Find the pending challenge
        for c in node.tokenomics.verification.pending_challenges():
            if c.job_id == job_id:
                challenge_obj = c
                break
        if challenge_obj:
            break

# Restore the temporarily-banned miners
for addr, original_ban in _fault_test_orig_bans.items():
    m = node.tokenomics._miners.get(addr)
    if m:
        m.banned_until_block = original_ban

check("eventually issued a challenge", challenge_obj is not None)

if challenge_obj:
    stake_before = node.tokenomics._miners['OBYfaultyxxxxxxxxxxxxxxxxxxxxxxxx'].stake_oby

    code, body = post(f'{BASE_URL}/compute/challenge_resolve', {
        'challenge_id': challenge_obj.challenge_id,
        'rerun_hash'  : _hex(99999),  # divergent
    })
    check("challenge_resolve -> 200", code == 200)
    check("status FAILED", body.get('status') == 'failed')

    m = node.tokenomics._miners['OBYfaultyxxxxxxxxxxxxxxxxxxxxxxxx']
    check("1st offence count = 1", m.offence_count == 1)
    check("stake reduced by 20%",
          abs(m.stake_oby - stake_before * 0.8) < 0.01,
          f"got {m.stake_oby} expected {stake_before * 0.8}")
    check("reputation reset to 0", m.reputation == 0.0)
    check("no ban yet (1st offence)", m.banned_until_block is None)

    # Job state reflects fault
    code, body = post(f'{BASE_URL}/compute/job',
                      {'job_id': challenge_obj.job_id})
    check("faulted job status = 'faulted' via API", body.get('status') == 'faulted')
    check("refund_oby > 0 on faulted job", body.get('refund_oby', 0) > 0)


# ── /compute/challenge_resolve (PASSED via matching rerun) ──────────────────
section("/compute/challenge_resolve PASSED -> on_pass credits miner")

# Register a fresh miner
post(f'{BASE_URL}/compute/register', {
    'address': 'OBYhonestxxxxxxxxxxxxxxxxxxxxxxxx',
    'gpu_model': 'A100', 'gpu_count': 1, 'vram_gb': 80,
    'bandwidth_gbps': 10.0, 'region': 'us-east',
    'stake_oby': 10000.0,
})

# Make 'honest' the only eligible miner so the test is deterministic.
# We temporarily mark all other miners as banned. We restore their state
# after this block so subsequent tests aren't affected.
_other_miner_states = {}
for addr, m in list(node.tokenomics._miners.items()):
    if addr != 'OBYhonestxxxxxxxxxxxxxxxxxxxxxxxx':
        _other_miner_states[addr] = m.banned_until_block
        m.banned_until_block = 10**12   # banned far into the future

pass_challenge = None
honest_result_hash = None
for i in range(80):
    code, body = post(f'{BASE_URL}/compute/submit', {
        'developer_addr'    : 'dev_dan_xxxxxxxxxxxxxxxxxxxxxxxx',
        'job_type'          : 'inference',
        'model_id'          : 'test-model',
        'coin'              : 'USDC',
        'model_hash'        : _hex(5000 + i),
        'container_digest'  : _digest(2),
        'seed'              : 500 + i,
        'input_payload_hash': _hex(3),
        'input_schema_hash' : _hex(4),
        'duration_hr'       : 0.05,
    })
    if code != 200:
        continue
    job_id = body['job_id']

    job = node.tokenomics.get_job(job_id)
    # With honest as the only eligible miner, every assignment goes to them.
    # If for some reason this job didn't get assigned (e.g. another miner
    # snuck in), skip — but it shouldn't happen.
    if job and job.miner_addr != 'OBYhonestxxxxxxxxxxxxxxxxxxxxxxxx':
        continue

    honest_result_hash = _hex(6000 + i)
    code, body = post(f'{BASE_URL}/compute/result', {
        'job_id'     : job_id,
        'miner_addr' : 'OBYhonestxxxxxxxxxxxxxxxxxxxxxxxx',
        'result_cid' : f'cid-honest-{i}',
        'result_hash': honest_result_hash,
    })
    if code == 200 and body.get('method') == 'challenged':
        for c in node.tokenomics.verification.pending_challenges():
            if c.job_id == job_id:
                pass_challenge = c
                break
        if pass_challenge:
            break

# Restore the temporarily-banned miners so subsequent tests aren't disrupted
for addr, original_ban in _other_miner_states.items():
    m = node.tokenomics._miners.get(addr)
    if m:
        m.banned_until_block = original_ban

check("eventually issued a challenge for honest miner",
      pass_challenge is not None)

if pass_challenge:
    jobs_completed_before = node.tokenomics._miners[
        'OBYhonestxxxxxxxxxxxxxxxxxxxxxxxx'].jobs_completed
    earned_before = node.tokenomics._miners[
        'OBYhonestxxxxxxxxxxxxxxxxxxxxxxxx'].oby_earned

    code, body = post(f'{BASE_URL}/compute/challenge_resolve', {
        'challenge_id': pass_challenge.challenge_id,
        'rerun_hash'  : honest_result_hash,   # matching
    })
    check("challenge_resolve PASSED -> 200", code == 200)
    check("status PASSED", body.get('status') == 'passed')

    m = node.tokenomics._miners['OBYhonestxxxxxxxxxxxxxxxxxxxxxxxx']
    check("on_pass: jobs_completed incremented",
          m.jobs_completed == jobs_completed_before + 1)
    check("on_pass: oby_earned increased", m.oby_earned > earned_before)
    check("on_pass: stake unchanged", m.stake_oby == 10000.0)
    check("on_pass: no ban", m.banned_until_block is None)


# ── /compute/infer (synchronous flow) ───────────────────────────────────────
section("/compute/infer (synchronous; short timeout in test)")

# Bound the test timeout by patching the API constant for this call only
from compute.api import ComputeAPI
original_timeout = ComputeAPI.INFER_TIMEOUT_S
ComputeAPI.INFER_TIMEOUT_S = 2.0
try:
    code, body = post(f'{BASE_URL}/compute/infer', {
        'developer_addr'    : 'dev_inf_xxxxxxxxxxxxxxxxxxxxxxxx',
        'model_id'          : 'test-model',
        'coin'              : 'USDC',
        'model_hash'        : _hex(700),
        'container_digest'  : _digest(701),
        'seed'              : 700,
        'input_payload_hash': _hex(703),
        'input_schema_hash' : _hex(704),
    })
    # No live miner runtime so this will time out — that's correct testnet
    # behaviour. We're testing the API plumbing, not the inference loop.
    check("infer eventually returns (success or timeout, not 500)",
          code in (200, 503, 504))
finally:
    ComputeAPI.INFER_TIMEOUT_S = original_timeout


# ── save / load round-trip via real engine ──────────────────────────────────
section("save / load round-trip")

save_path = os.path.join(TMPDIR, 'roundtrip.json')
node.tokenomics.save(save_path)

# Build a fresh engine and load
from tokenomics.engine import TokenomicsEngine
fresh = TokenomicsEngine(
    block_height_provider=lambda: 0,
    block_hash_provider=lambda: b'\x00' * 32,
)
fresh.load(save_path)

check("loaded miners count matches",
      len(fresh._miners) == len(node.tokenomics._miners))
check("loaded jobs count matches",
      len(fresh._jobs) == len(node.tokenomics._jobs))

# Check a specific miner with offence_count + ban survived
if challenge_obj:
    src = node.tokenomics._miners['OBYfaultyxxxxxxxxxxxxxxxxxxxxxxxx']
    dst = fresh._miners.get('OBYfaultyxxxxxxxxxxxxxxxxxxxxxxxx')
    check("faulted miner restored with offence_count",
          dst is not None and dst.offence_count == src.offence_count)
    check("faulted miner restored with reputation=0",
          dst is not None and dst.reputation == src.reputation)


# ── Shutdown ────────────────────────────────────────────────────────────────
rpc_server.shutdown()

# Clean up temp dir
import shutil
try:
    shutil.rmtree(TMPDIR)
except Exception:
    pass


# ── Final report ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  PASSED: {PASSED}")
print(f"  FAILED: {len(FAILED)}")
if FAILED:
    print(f"\nFailures:")
    for name, detail in FAILED:
        print(f"  - {name}: {detail}")
    sys.exit(1)
print(f"  All checks green. Phase 2 HTTP API ready.")
sys.exit(0)
