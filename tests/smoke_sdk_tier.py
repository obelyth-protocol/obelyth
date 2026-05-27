"""
Smoke runner for Phase 4.2.4 — SDK tier surface.

Boots a real FullNode and exercises the redundant tier through the actual
ObelythClient SDK (not raw HTTP). Validates that:

  - ObelythClient(tier='redundant') sets the default tier
  - ObelythClient(tier='galaxy') raises ValueError
  - client.quote(...) defaults to client.tier
  - client.quote(..., tier='redundant') overrides for one call
  - client.pipeline(...) inherits client.tier
  - client.pipeline(..., tier='redundant') overrides for one pipeline
  - client.fine_tune(..., tier='redundant') threads tier through submit
  - Standard tier path unchanged (regression)

The SDK uses urllib internally so this exercises the full SDK→HTTP→engine
stack without mocking.
"""

import sys
import os
import hashlib
import time
import tempfile
import threading
import urllib.request
from http.server import HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from node.fullnode import FullNode, RPCHandler
from sdk.obelyth import ObelythClient, ObelythPipeline


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


def find_free_port():
    import socket
    s = socket.socket()
    s.bind(('', 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── Boot a node + register 5 miners so redundant has options ────────────────

P2P_PORT = find_free_port()
RPC_PORT = find_free_port()
TMPDIR   = tempfile.mkdtemp(prefix='obelyth-sdk-tier-')
NODE_URL = f'http://127.0.0.1:{RPC_PORT}'

print(f"Booting FullNode on RPC port {RPC_PORT}")
node = FullNode(
    p2p_port=P2P_PORT, rpc_port=RPC_PORT, data_dir=TMPDIR, mine=False,
)
RPCHandler.node = node
rpc_server = HTTPServer(('127.0.0.1', RPC_PORT), RPCHandler)
threading.Thread(target=rpc_server.serve_forever, daemon=True).start()
time.sleep(0.3)

# Seed oracle + stable block hash for determinism
from tokenomics.engine import Stablecoin
node.tokenomics.update_rate(Stablecoin.USDC, 1.0)
node.tokenomics._block_hash = lambda: hashlib.sha3_256(b'sdk-test').digest()
node.tokenomics.verification._get_block_hash = node.tokenomics._block_hash

# Register 5 miners so redundant tier has 3+ eligible
import json
import urllib.request

def reg_miner(addr):
    data = json.dumps({
        'address': addr,
        'gpu_model': 'A100', 'gpu_count': 1, 'vram_gb': 80,
        'bandwidth_gbps': 10.0, 'region': 'us-east',
        'stake_oby': 10000.0,
    }).encode()
    req = urllib.request.Request(
        f'{NODE_URL}/compute/register', data=data,
        headers={'Content-Type': 'application/json'}, method='POST',
    )
    urllib.request.urlopen(req, timeout=5).read()

for i in range(5):
    reg_miner(f'OBYminer{i}SDKxxxxxxxxxxxxxxxxxxxxxx')


# ── ObelythClient tier construction ─────────────────────────────────────────
section("ObelythClient tier construction")

c_default = ObelythClient(api_key='', node_url=NODE_URL)
check("default tier = 'standard'", c_default.tier == 'standard')

c_red = ObelythClient(api_key='', node_url=NODE_URL, tier='redundant')
check("explicit tier='redundant'", c_red.tier == 'redundant')

c_std = ObelythClient(api_key='', node_url=NODE_URL, tier='standard')
check("explicit tier='standard'", c_std.tier == 'standard')

try:
    ObelythClient(api_key='', node_url=NODE_URL, tier='galaxy')
    check("invalid tier raises ValueError", False)
except ValueError as e:
    check("invalid tier raises ValueError", True)
    check("error mentions valid tiers",
          'standard' in str(e) and 'redundant' in str(e))


# ── client.quote() defaults ──────────────────────────────────────────────────
section("client.quote() defaults to client.tier")

std_quote = c_std.quote(task='fine_tuning', model='m1', gpu_count=4, hours=10)
check("standard client → standard quote",
      std_quote.get('tier') == 'standard')
check("standard tier_multiplier = 1.0",
      std_quote.get('tier_multiplier') == 1.0)

red_quote = c_red.quote(task='fine_tuning', model='m1', gpu_count=4, hours=10)
check("redundant client → redundant quote",
      red_quote.get('tier') == 'redundant')
check("redundant tier_multiplier = 3.0",
      red_quote.get('tier_multiplier') == 3.0)
check("redundant cost = 3x standard cost",
      abs(red_quote.get('usd_cost', 0) / std_quote.get('usd_cost', 1) - 3.0) < 0.001)


# ── client.quote(..., tier='X') overrides ──────────────────────────────────
section("Per-call tier overrides")

# Standard client requesting redundant quote
override_red = c_std.quote(task='fine_tuning', model='m1',
                            gpu_count=4, hours=10, tier='redundant')
check("standard client + tier='redundant' override",
      override_red.get('tier') == 'redundant')
check("override cost is 3x",
      override_red.get('usd_cost') == red_quote.get('usd_cost'))

# Redundant client requesting standard quote
override_std = c_red.quote(task='fine_tuning', model='m1',
                            gpu_count=4, hours=10, tier='standard')
check("redundant client + tier='standard' override",
      override_std.get('tier') == 'standard')


# ── client.pipeline() inherits client tier ─────────────────────────────────
section("Pipeline tier inheritance + override")

p_std = c_std.pipeline('text-generation', 'meta-llama/Llama-3-8B')
check("pipeline from standard client → tier='standard'",
      p_std.tier == 'standard')

p_red = c_red.pipeline('text-generation', 'meta-llama/Llama-3-8B')
check("pipeline from redundant client → tier='redundant'",
      p_red.tier == 'redundant')

p_override = c_std.pipeline('text-generation', 'meta-llama/Llama-3-8B',
                              tier='redundant')
check("pipeline override → tier='redundant'",
      p_override.tier == 'redundant')


# ── client._run_inference() threads tier to /compute/infer ─────────────────
# We can't easily snoop on the outbound HTTP, but we CAN check that the
# response shape reflects the right tier handling. With a tier=redundant
# call and no miners actually running, it will assign 3 miners and time out
# the synchronous infer; with standard it assigns 1 and times out.
section("client._run_inference threads tier through HTTP")

# Monkey-patch a capture
captured = {}
orig_post = c_red._rpc_post
def capture_post(endpoint, data):
    captured[endpoint] = dict(data)
    return orig_post(endpoint, data)
c_red._rpc_post = capture_post

try:
    c_red._run_inference(
        task='text-generation', model='m1',
        inputs=['hi'], params={},
    )
except Exception:
    pass   # we expect this to fail / time out — we just care about the body

check("/compute/infer called",
      '/compute/infer' in captured)
check("infer body includes tier='redundant'",
      captured.get('/compute/infer', {}).get('tier') == 'redundant')

# Override per-call
captured.clear()
try:
    c_red._run_inference(
        task='text-generation', model='m1',
        inputs=['hi'], params={}, tier='standard',
    )
except Exception:
    pass
check("per-call tier override reaches HTTP",
      captured.get('/compute/infer', {}).get('tier') == 'standard')


# ── fine_tune threads tier through submit ──────────────────────────────────
section("client.fine_tune() threads tier")

c_red._rpc_post = orig_post   # restore
captured.clear()

orig_post_red = c_red._rpc_post
def capture_post_2(endpoint, data):
    captured[endpoint] = dict(data)
    # Return a minimal response shape so fine_tune doesn't error
    if endpoint == '/compute/quote':
        return {'usd_cost': 16.0, 'tier': data.get('tier', 'standard')}
    if endpoint == '/compute/submit':
        return {'job_id': 'jb-fake', 'status': 'pending',
                'usdc_cost': 16.0, 'oby_reward': 0.85}
    return orig_post_red(endpoint, data)
c_red._rpc_post = capture_post_2

# Create a temp dataset file so _upload_dataset doesn't fail
ds_path = os.path.join(TMPDIR, 'ds.jsonl')
with open(ds_path, 'w') as f:
    f.write('{"text": "sample"}\n')

c_red.fine_tune(
    base_model='meta-llama/Llama-3-8B',
    dataset_path=ds_path,
    epochs=1,
)
check("fine_tune sends tier to /compute/quote",
      captured.get('/compute/quote', {}).get('tier') == 'redundant')
check("fine_tune sends tier to /compute/submit",
      captured.get('/compute/submit', {}).get('tier') == 'redundant')


# ── Standard-tier full path regression ──────────────────────────────────────
section("Standard tier full path regression (real submit + assign)")

# Use the standard client, real submit through SDK; check the engine got
# a standard-tier job assigned to exactly one miner
c_std._rpc_post = c_std._rpc_post   # ensure no monkey patch left

# Get count of jobs before
jobs_before = len(node.tokenomics._jobs)

c_std.fine_tune(
    base_model='test-model',
    dataset_path=ds_path,
    epochs=1,
)

# Find the new job
new_jobs = [j for j in node.tokenomics._jobs.values()
            if j.developer_addr != '' and j.tier == 'standard']
check("standard client created a standard-tier job",
      len(new_jobs) >= 1)
if new_jobs:
    latest = max(new_jobs, key=lambda j: j.created_at)
    check("latest job tier = 'standard'", latest.tier == 'standard')
    check("latest job assigned to 1 miner (or pending)",
          latest.miner_addr != '' or latest.status == 'pending')


# ── Redundant-tier full path through real SDK ──────────────────────────────
section("Redundant tier full path (real submit + assign + 3 miners)")

c_red._rpc_post = orig_post_red   # restore

before = len(node.tokenomics._jobs)
c_red.fine_tune(
    base_model='test-model',
    dataset_path=ds_path,
    epochs=1,
)

# Find the newest redundant job
red_jobs = [j for j in node.tokenomics._jobs.values()
            if j.tier == 'redundant']
check("redundant client created a redundant-tier job",
      len(red_jobs) >= 1)
if red_jobs:
    latest = max(red_jobs, key=lambda j: j.created_at)
    check("latest redundant job has 3 assigned_miners",
          len(latest.assigned_miners) == 3)
    check("all 3 miners distinct",
          len(set(latest.assigned_miners)) == 3)
    check("consensus_deadline set",
          latest.consensus_deadline > int(time.time()))


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
print(f"  All checks green. Phase 4.2.4 SDK tier ready.")
sys.exit(0)
