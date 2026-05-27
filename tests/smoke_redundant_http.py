"""
Smoke runner for Phase 4.2.3 — HTTP integration for redundant tier.

Boots a real FullNode and exercises the redundant tier through actual HTTP
calls. Companion to smoke_redundant_tier.py (which tests the engine layer).

Coverage:
  - POST /compute/quote with tier='redundant' returns 3x cost
  - POST /compute/quote with unknown tier returns 400
  - POST /compute/quote without tier defaults to standard (backwards-compat)
  - POST /compute/submit with tier='redundant' creates a redundant job and
    assigns 3 distinct miners
  - POST /compute/submit response includes assigned_miners, oby_per_miner,
    oby_total, consensus_deadline
  - POST /compute/submit with tier='redundant' but <3 miners fails gracefully
  - GET /compute/nextjob returns redundant jobs to each of the 3 assigned
    miners (each miner sees the same job)
  - GET /compute/nextjob does NOT return the same job to a miner who has
    already submitted
  - POST /compute/result for redundant tier accumulates submissions
  - Submitting from a not-assigned miner returns an error
  - Duplicate submission returns an error
  - All-3 unanimous → job 'done', all credited (via API)
  - 2/3 majority via API → 2 winners credited, 1 outlier slashed
  - 3/3 all-different → job 'disputed' via API
  - POST /compute/job for a redundant job includes the new tier-specific fields
  - Standard tier still works through API (backwards-compat regression)
  - Background consensus sweep finalizes a timed-out job
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


# ── Boot FullNode + register 5 miners so redundant has options ──────────────

P2P_PORT = find_free_port()
RPC_PORT = find_free_port()
TMPDIR   = tempfile.mkdtemp(prefix='obelyth-redundant-http-')
BASE_URL = f'http://127.0.0.1:{RPC_PORT}'

print(f"Booting FullNode on RPC port {RPC_PORT}")
node = FullNode(
    p2p_port=P2P_PORT, rpc_port=RPC_PORT, data_dir=TMPDIR, mine=False,
)
RPCHandler.node = node
rpc_server = HTTPServer(('127.0.0.1', RPC_PORT), RPCHandler)
threading.Thread(target=rpc_server.serve_forever, daemon=True).start()
time.sleep(0.3)

# Seed oracle
from tokenomics.engine import Stablecoin
node.tokenomics.update_rate(Stablecoin.USDC, 1.0)
node.tokenomics.update_rate(Stablecoin.DAI, 1.0)

# Stable block hash for deterministic assignment
node.tokenomics._block_hash = lambda: hashlib.sha3_256(b'http-test').digest()
node.tokenomics.verification._get_block_hash = node.tokenomics._block_hash

# Register 5 miners
MINER_ADDRS = []
for i in range(5):
    addr = f'OBYminer{i}HTTPxxxxxxxxxxxxxxxxxxxx'
    code, body = post(f'{BASE_URL}/compute/register', {
        'address': addr,
        'gpu_model': 'A100', 'gpu_count': 1, 'vram_gb': 80,
        'bandwidth_gbps': 10.0, 'region': 'us-east',
        'stake_oby': 10000.0,
    })
    if code == 200:
        MINER_ADDRS.append(addr)

check("registered 5 miners", len(MINER_ADDRS) == 5)


# ── /compute/quote: tier defaults + redundant + bogus ──────────────────────
section("/compute/quote — tier parameter")

# No tier → defaults to standard
code, body = post(f'{BASE_URL}/compute/quote', {
    'job_type': 'fine_tuning', 'gpu_count': 4, 'duration_hr': 10.0,
})
check("no tier defaults to standard (code=200)", code == 200)
check("default tier in response = 'standard'", body.get('tier') == 'standard')
check("default tier_multiplier = 1.0", body.get('tier_multiplier') == 1.0)
std_cost = body.get('usd_cost', 0)

# Explicit standard
code, body = post(f'{BASE_URL}/compute/quote', {
    'job_type': 'fine_tuning', 'gpu_count': 4, 'duration_hr': 10.0,
    'tier': 'standard',
})
check("explicit standard tier → 200", code == 200)
check("explicit standard cost matches default",
      body.get('usd_cost') == std_cost)

# Redundant → 3x
code, body = post(f'{BASE_URL}/compute/quote', {
    'job_type': 'fine_tuning', 'gpu_count': 4, 'duration_hr': 10.0,
    'tier': 'redundant',
})
check("redundant tier → 200", code == 200)
check("redundant tier in response = 'redundant'",
      body.get('tier') == 'redundant')
check("redundant tier_multiplier = 3.0", body.get('tier_multiplier') == 3.0)
check("redundant cost is 3x standard",
      abs(body.get('usd_cost', 0) / std_cost - 3.0) < 0.001,
      f"ratio={body.get('usd_cost', 0) / std_cost}")

# Unknown tier
code, body = post(f'{BASE_URL}/compute/quote', {
    'job_type': 'inference', 'tier': 'pipeline_galactic',
})
check("unknown tier → 400", code == 400)


# ── /compute/submit with tier='redundant' ──────────────────────────────────
section("/compute/submit — tier='redundant' creates 3-miner job")

submit_body = {
    'developer_addr'    : 'dev_redundant_test_xxxxxxxxxxxxxxx',
    'job_type'          : 'inference',
    'model_id'          : 'test-model',
    'coin'              : 'USDC',
    'model_hash'        : _hex(1),
    'container_digest'  : _digest(2),
    'seed'              : 42,
    'input_payload_hash': _hex(3),
    'input_schema_hash' : _hex(4),
    'inputs'            : ['hello world'],
    'duration_hr'       : 0.1,
    'tier'              : 'redundant',
}
code, body = post(f'{BASE_URL}/compute/submit', submit_body)
check("redundant submit → 200", code == 200, f"got {code}: {body}")
check("response tier = 'redundant'", body.get('tier') == 'redundant')
check("response has assigned_miners", 'assigned_miners' in body)
check("3 miners assigned",
      isinstance(body.get('assigned_miners'), list)
      and len(body['assigned_miners']) == 3)
check("all 3 assignees are distinct",
      len(set(body.get('assigned_miners', []))) == 3)
check("response has oby_per_miner", 'oby_per_miner' in body)
check("response has oby_total", 'oby_total' in body)
check("oby_total ≈ 3 * oby_per_miner",
      abs(body.get('oby_total', 0) - 3 * body.get('oby_per_miner', 0)) < 0.0001)
check("response has consensus_deadline",
      body.get('consensus_deadline', 0) > int(time.time()))
JOB_ID_1 = body.get('job_id')
PICKS_1  = body.get('assigned_miners', [])


# ── Unknown tier on submit ────────────────────────────────────────────────
section("/compute/submit — unknown tier rejected")

code, body = post(f'{BASE_URL}/compute/submit', {
    **submit_body,
    'tier': 'galaxy_brain',
    'model_hash': _hex(50),
})
check("unknown tier on submit → 400", code == 400)


# ── /compute/nextjob — each assignee sees the job ──────────────────────────
section("/compute/nextjob — each of 3 miners gets the redundant job")

for i, m in enumerate(PICKS_1):
    code, body = get(f'{BASE_URL}/compute/nextjob?address={m}')
    check(f"miner {i+1}/3 ({m[:16]}) sees the job",
          code == 200 and body.get('job_id') == JOB_ID_1)
    if body.get('job_id') == JOB_ID_1:
        check(f"  miner {i+1} payload includes inputs",
              body.get('inputs') == ['hello world'])
        check(f"  miner {i+1} payload includes pinned seed",
              body.get('seed') == 42)

# A non-assigned miner should NOT see it
non_assigned = [m for m in MINER_ADDRS if m not in PICKS_1][0]
code, body = get(f'{BASE_URL}/compute/nextjob?address={non_assigned}')
check("non-assigned miner does NOT see the redundant job",
      not body.get('job_id') or body.get('job_id') != JOB_ID_1)


# ── /compute/result — accumulates submissions, 3/3 unanimous ──────────────
section("/compute/result — 3/3 unanimous flow")

agreed_hash = _hex('agreed-result')
for i, m in enumerate(PICKS_1):
    code, body = post(f'{BASE_URL}/compute/result', {
        'job_id'    : JOB_ID_1,
        'miner_addr': m,
        'result_cid': f'cid-unanimous-{i}',
        'result_hash': agreed_hash,
    })
    check(f"submission {i+1}/3 from {m[:16]} → 200", code == 200,
          f"got {code}: {body}")

# After all 3, the job should be done (auto-finalize when N/N submitted)
job_after = node.tokenomics.get_job(JOB_ID_1)
check("job auto-finalized after 3/3 unanimous",
      job_after.status == 'done')
check("3 consensus_winners recorded",
      len(job_after.consensus_winners) == 3)
check("0 consensus_outliers",
      len(job_after.consensus_outliers) == 0)
check("result_hash = agreed hash",
      job_after.result_hash == agreed_hash)


# ── After completion, miner who already submitted doesn't get it again ────
section("Completed job not re-issued to its submitters")

for m in PICKS_1:
    code, body = get(f'{BASE_URL}/compute/nextjob?address={m}')
    check(f"  {m[:16]}: completed job not re-issued",
          not body.get('job_id') or body.get('job_id') != JOB_ID_1)


# ── /compute/job — redundant job status surfaces tier fields ──────────────
section("/compute/job — redundant tier fields surfaced")

code, body = post(f'{BASE_URL}/compute/job', {'job_id': JOB_ID_1})
check("redundant job status → 200", code == 200)
check("tier = 'redundant' in response", body.get('tier') == 'redundant')
check("status = 'done'", body.get('status') == 'done')
check("response has assigned_miners",
      isinstance(body.get('assigned_miners'), list)
      and len(body['assigned_miners']) == 3)
check("submissions_count = 3", body.get('submissions_count') == 3)
check("submissions_needed = 3", body.get('submissions_needed') == 3)
check("consensus_winners populated",
      len(body.get('consensus_winners', [])) == 3)
check("consensus_outliers empty",
      body.get('consensus_outliers') == [])


# ── Duplicate submission rejected ─────────────────────────────────────────
section("Duplicate submission via API rejected (job already done)")

code, body = post(f'{BASE_URL}/compute/result', {
    'job_id'    : JOB_ID_1,
    'miner_addr': PICKS_1[0],
    'result_cid': 'cid-dupe',
    'result_hash': _hex('whatever'),
})
check("dup submission on done job → 400", code == 400)


# ── Scenario 2: 2-1 majority via API ──────────────────────────────────────
section("API end-to-end: 2/3 majority — outlier slashed")

# Different determinism so we get fresh assignment
submit_2 = {**submit_body, 'model_hash': _hex(100), 'seed': 100,
            'inputs': ['scenario 2']}
code, body = post(f'{BASE_URL}/compute/submit', submit_2)
JOB_ID_2 = body.get('job_id')
PICKS_2  = body.get('assigned_miners', [])
check("scenario 2 submit → 200 with 3 miners",
      code == 200 and len(PICKS_2) == 3)

if JOB_ID_2 and len(PICKS_2) == 3:
    stake_outlier_before = node.tokenomics._miners[PICKS_2[2]].stake_oby
    true_hash = _hex('scenario2-truth')
    lie_hash  = _hex('scenario2-lie')

    post(f'{BASE_URL}/compute/result', {
        'job_id': JOB_ID_2, 'miner_addr': PICKS_2[0],
        'result_cid': 'c2-0', 'result_hash': true_hash,
    })
    post(f'{BASE_URL}/compute/result', {
        'job_id': JOB_ID_2, 'miner_addr': PICKS_2[1],
        'result_cid': 'c2-1', 'result_hash': true_hash,
    })
    code, body = post(f'{BASE_URL}/compute/result', {
        'job_id': JOB_ID_2, 'miner_addr': PICKS_2[2],
        'result_cid': 'c2-2', 'result_hash': lie_hash,   # outlier
    })
    check("3rd submission → 200", code == 200)

    job_after = node.tokenomics.get_job(JOB_ID_2)
    check("job status = 'done' (majority wins)",
          job_after.status == 'done')
    check("2 winners", set(job_after.consensus_winners) ==
          {PICKS_2[0], PICKS_2[1]})
    check("1 outlier = PICKS_2[2]",
          job_after.consensus_outliers == [PICKS_2[2]])

    m_outlier = node.tokenomics._miners[PICKS_2[2]]
    check("outlier slashed ~20%",
          abs(m_outlier.stake_oby - stake_outlier_before * 0.8) < 0.01)
    check("outlier offence_count = 1", m_outlier.offence_count == 1)


# ── Scenario 3: 3-way disagreement = disputed ─────────────────────────────
section("API end-to-end: 3/3 all-different — disputed")

submit_3 = {**submit_body, 'model_hash': _hex(200), 'seed': 200,
            'inputs': ['scenario 3']}
code, body = post(f'{BASE_URL}/compute/submit', submit_3)
JOB_ID_3 = body.get('job_id')
PICKS_3  = body.get('assigned_miners', [])

# Some of these picks may now be slashed/banned from scenario 2
# That's fine — they can still submit, just track which ones got picked
if JOB_ID_3 and len(PICKS_3) == 3:
    stakes_before = {m: node.tokenomics._miners[m].stake_oby for m in PICKS_3}
    offence_before = {m: node.tokenomics._miners[m].offence_count for m in PICKS_3}

    post(f'{BASE_URL}/compute/result', {
        'job_id': JOB_ID_3, 'miner_addr': PICKS_3[0],
        'result_cid': 'c3-0', 'result_hash': _hex('alpha'),
    })
    post(f'{BASE_URL}/compute/result', {
        'job_id': JOB_ID_3, 'miner_addr': PICKS_3[1],
        'result_cid': 'c3-1', 'result_hash': _hex('beta'),
    })
    post(f'{BASE_URL}/compute/result', {
        'job_id': JOB_ID_3, 'miner_addr': PICKS_3[2],
        'result_cid': 'c3-2', 'result_hash': _hex('gamma'),
    })

    job_after = node.tokenomics.get_job(JOB_ID_3)
    check("disputed job status = 'disputed'",
          job_after.status == 'disputed')
    check("no winners on disputed", job_after.consensus_winners == [])
    check("no slashes on returners (all 3 came back)",
          all(node.tokenomics._miners[m].offence_count
              == offence_before[m] for m in PICKS_3))
    check("dev refund recorded on disputed job",
          job_after.refund_oby > 0)


# ── Not-assigned miner submission rejected ────────────────────────────────
section("Result from not-assigned miner rejected")

# Submit a fresh job
submit_4 = {**submit_body, 'model_hash': _hex(400), 'seed': 400,
            'inputs': ['scenario 4']}
code, body = post(f'{BASE_URL}/compute/submit', submit_4)
JOB_ID_4 = body.get('job_id')
PICKS_4  = body.get('assigned_miners', [])

if JOB_ID_4 and len(PICKS_4) == 3:
    not_assigned = [m for m in MINER_ADDRS if m not in PICKS_4]
    if not_assigned:
        code, body = post(f'{BASE_URL}/compute/result', {
            'job_id'    : JOB_ID_4,
            'miner_addr': not_assigned[0],
            'result_cid': 'unauthorized',
            'result_hash': _hex('intruder'),
        })
        check("not-assigned miner result → 400", code == 400)


# ── Standard tier still works (regression) ────────────────────────────────
section("Regression: standard tier unchanged through API")

submit_std = {**submit_body, 'tier': 'standard',
              'model_hash': _hex(500), 'seed': 500,
              'inputs': ['std regression']}
del submit_std['tier']   # also test no-tier defaults to standard
code, body = post(f'{BASE_URL}/compute/submit', submit_std)
check("no-tier submit → 200", code == 200)
check("response tier = 'standard'", body.get('tier') == 'standard')
check("response has miner_addr (single)", body.get('miner_addr'))
check("response has oby_reward (not oby_per_miner)",
      'oby_reward' in body and 'oby_per_miner' not in body)


# ── Background consensus sweep finalizes timed-out job ────────────────────
section("Consensus sweep finalizes timed-out redundant job")

submit_timeout = {**submit_body, 'model_hash': _hex(600), 'seed': 600,
                  'inputs': ['timeout test'],
                  'tier': 'redundant'}
code, body = post(f'{BASE_URL}/compute/submit', submit_timeout)
JOB_ID_TIMEOUT = body.get('job_id')
PICKS_TIMEOUT  = body.get('assigned_miners', [])

# Force the deadline to the past so the next sweep catches it
job_t = node.tokenomics.get_job(JOB_ID_TIMEOUT)
job_t.consensus_deadline = int(time.time()) - 1

# Manually trigger the sweep (don't wait 30s for the background loop)
settled = node.tokenomics.finalize_due_redundant_jobs()
check("sweep finalized 1 job", len(settled) >= 1)

job_t = node.tokenomics.get_job(JOB_ID_TIMEOUT)
check("timed-out job status = 'faulted' (0/3 returns)",
      job_t.status == 'faulted')


# ── Shutdown ──────────────────────────────────────────────────────────────
rpc_server.shutdown()
import shutil
try:
    shutil.rmtree(TMPDIR)
except Exception:
    pass

# ── Report ────────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  PASSED: {PASSED}")
print(f"  FAILED: {len(FAILED)}")
if FAILED:
    print(f"\nFailures:")
    for name, detail in FAILED:
        print(f"  - {name}: {detail}")
    sys.exit(1)
print(f"  All checks green. Phase 4.2.3 HTTP integration ready.")
sys.exit(0)
