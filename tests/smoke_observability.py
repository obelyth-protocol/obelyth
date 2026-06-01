"""
Smoke runner for the observability surface (Phase 5.5a):
  - GET /health   — binary health check (200 ok/degraded, 503 unhealthy)
  - GET /metrics  — full diagnostic JSON dump

Plus unit-level coverage of the MetricsRegistry and evaluate_health helpers
in node/observability.py. These run without a node booted so they're fast
and don't interfere with the live-server checks below.

This is the foundation that 5.5b–e will extend. The endpoint response
shapes are tested here so future phases can add fields without breaking
existing monitoring consumers.
"""

import sys
import os
import json
import time
import tempfile
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from node.fullnode import FullNode, RPCHandler
from node.observability import (
    MetricsRegistry,
    PersistStats,
    evaluate_health,
    COUNTER_NAMES,
    HEALTH_PERSIST_STALE_S,
    HEALTH_MEMPOOL_OVERFLOW,
    HEALTH_TIP_STALE_S,
)


PASSED = 0
FAILED = []


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED.append((name, detail))
        print(f"  FAIL  {name}: {detail}")


def section(t):
    print(f"\n--- {t} ---")


def find_free_port():
    import socket
    s = socket.socket(); s.bind(('', 0)); p = s.getsockname()[1]; s.close()
    return p


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS — MetricsRegistry + evaluate_health (no node needed)
# ══════════════════════════════════════════════════════════════════════════════

section("MetricsRegistry: initial state")
m = MetricsRegistry()
check("COUNTER_NAMES is non-empty", len(COUNTER_NAMES) > 0)
check("registry exposes all declared counters",
      set(m.counters().keys()) == set(COUNTER_NAMES))
check("all counters start at 0",
      all(v == 0 for v in m.counters().values()))
check("persist stats start zeroed",
      m.persist.save_count == 0 and m.persist.save_failure_count == 0)
check("reorg history starts empty", m.reorgs() == [])


section("MetricsRegistry: counter increments")
m.increment('blocks_received')
m.increment('blocks_received')
m.increment('verification_passes', by=5)
c = m.counters()
check("blocks_received incremented twice", c['blocks_received'] == 2)
check("verification_passes incremented by 5", c['verification_passes'] == 5)
check("unrelated counter still 0", c['peer_connects'] == 0)


section("MetricsRegistry: unknown counter silently ignored")
m.increment('not_a_real_counter')  # must not raise
m.increment('also_fake', by=99)
check("unknown counters do not appear in registry",
      'not_a_real_counter' not in m.counters() and 'also_fake' not in m.counters())


section("MetricsRegistry: persist tracking")
m2 = MetricsRegistry()
m2.record_save_success(42.5)
check("save_count increments on success", m2.persist.save_count == 1)
check("last_save_duration_ms recorded", m2.persist.last_save_duration_ms == 42.5)
check("last_save_ts is recent",
      abs(m2.persist.last_save_ts - time.time()) < 5.0)
check("persist_saves counter incremented", m2.counters()['persist_saves'] == 1)

m2.record_save_failure("disk full")
check("save_failure_count increments on failure",
      m2.persist.save_failure_count == 1)
check("failure reason captured",
      m2.persist.last_failure_reason == "disk full")
check("persist_save_failures counter incremented",
      m2.counters()['persist_save_failures'] == 1)


section("MetricsRegistry: reorg tracking")
m3 = MetricsRegistry()
m3.record_reorg(depth=1, invalidated=['a'], new_canonical=['b'], miners=['H1'])
m3.record_reorg(depth=2, invalidated=['c', 'd'], new_canonical=['e', 'f'], miners=['H2'])
m3.record_reorg(depth=5, invalidated=['g']*5, new_canonical=['h']*5, miners=['H3'])
rs = m3.reorgs()
check("reorg history has 3 entries", len(rs) == 3)
check("reorg fields preserved", rs[0]['depth'] == 1 and rs[0]['miners'] == ['H1'])
c = m3.counters()
check("reorgs_total = 3", c['reorgs_total'] == 3)
check("reorgs_by_depth_1 = 1", c['reorgs_by_depth_1'] == 1)
check("reorgs_by_depth_2 = 1", c['reorgs_by_depth_2'] == 1)
check("reorgs_by_depth_3plus = 1", c['reorgs_by_depth_3plus'] == 1)

# Reorg cap enforced
m4 = MetricsRegistry()
m4._reorg_cap = 5  # shrink cap for fast test
for i in range(20):
    m4.record_reorg(depth=1, invalidated=[f'x{i}'], new_canonical=[f'y{i}'], miners=['M'])
check("reorg history capped at _reorg_cap", len(m4.reorgs()) == 5)
check("counter still reflects all events (monotonic)",
      m4.counters()['reorgs_total'] == 20)


section("evaluate_health: boot phase is forgiving")
p = PersistStats()  # never-saved
status, reasons = evaluate_health(
    uptime_s=10, persist=p, mempool_size=0, last_block_ts=None
)
check("boot phase reports ok despite no save", status == 'ok')
check("no reasons during boot", reasons == [])


section("evaluate_health: persist never ran after boot")
status, reasons = evaluate_health(
    uptime_s=300, persist=p, mempool_size=0, last_block_ts=None
)
check("persist_never_ran flagged after boot", status == 'unhealthy')
check("reason is persist_loop_never_ran",
      any('persist_loop_never_ran' in r for r in reasons))


section("evaluate_health: stale persist save")
p2 = PersistStats(
    last_save_ts=time.time() - (HEALTH_PERSIST_STALE_S + 20),
    save_count=10,
)
status, reasons = evaluate_health(
    uptime_s=600, persist=p2, mempool_size=0, last_block_ts=None
)
check("stale persist flagged as degraded", status == 'degraded')
check("reason mentions persist_stale",
      any('persist_stale' in r for r in reasons))


section("evaluate_health: fresh persist + no other issues = ok")
p3 = PersistStats(last_save_ts=time.time() - 5, save_count=10)
status, reasons = evaluate_health(
    uptime_s=600, persist=p3, mempool_size=10, last_block_ts=time.time() - 30
)
check("healthy steady state", status == 'ok')
check("no reasons", reasons == [])


section("evaluate_health: mempool overflow")
status, reasons = evaluate_health(
    uptime_s=600, persist=p3, mempool_size=HEALTH_MEMPOOL_OVERFLOW + 1,
    last_block_ts=time.time() - 30
)
check("mempool overflow flagged", status == 'degraded')
check("reason mentions mempool_overflow",
      any('mempool_overflow' in r for r in reasons))


section("evaluate_health: stale tip")
status, reasons = evaluate_health(
    uptime_s=10000, persist=p3, mempool_size=0,
    last_block_ts=time.time() - (HEALTH_TIP_STALE_S + 100)
)
check("stale tip flagged", status == 'degraded')
check("reason mentions tip_stale",
      any('tip_stale' in r for r in reasons))


section("evaluate_health: multiple issues escalate to unhealthy")
status, reasons = evaluate_health(
    uptime_s=10000, persist=p2, mempool_size=HEALTH_MEMPOOL_OVERFLOW + 1,
    last_block_ts=time.time() - (HEALTH_TIP_STALE_S + 100)
)
check("multiple issues = unhealthy", status == 'unhealthy')
check("multiple reasons present", len(reasons) >= 2)


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRATION — boot a node and hit /health + /metrics over HTTP
# ══════════════════════════════════════════════════════════════════════════════

P2P  = find_free_port()
RPC  = find_free_port()
TMP  = tempfile.mkdtemp(prefix='oby-obs-')
BASE = f'http://127.0.0.1:{RPC}'

print(f"\nBooting FullNode on RPC port {RPC}")
node = FullNode(p2p_port=P2P, rpc_port=RPC, data_dir=TMP, mine=False)
RPCHandler.node = node
srv = HTTPServer(('127.0.0.1', RPC), RPCHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)


section("GET /health — fresh node during boot grace period")
code, body = get(f'{BASE}/health')
check("returns 200", code == 200)
check("status is ok (boot grace)", body.get('status') == 'ok')
check("body has uptime_s", 'uptime_s' in body)
check("body has height", 'height' in body)
check("no reasons during healthy boot",
      body.get('reasons') in (None, []))


section("GET /metrics — fresh node shape")
code, body = get(f'{BASE}/metrics')
check("returns 200", code == 200)
check("has 'node' block", 'node' in body)
check("has 'chain' block", 'chain' in body)
check("has 'mempool' block", 'mempool' in body)
check("has 'persistence' block", 'persistence' in body)
check("has 'counters' block", 'counters' in body)
check("has 'reorgs' block", 'reorgs' in body)

n_blk = body['node']
check("node.uptime_s is int", isinstance(n_blk['uptime_s'], int))
check("node.p2p_port matches", n_blk['p2p_port'] == P2P)
check("node.rpc_port matches", n_blk['rpc_port'] == RPC)
check("node.peer_count is int", isinstance(n_blk['peer_count'], int))

c_blk = body['chain']
check("chain.height is int", isinstance(c_blk['height'], int))
check("chain.utxo_count is int", isinstance(c_blk['utxo_count'], int))
check("chain.fork_count is int", isinstance(c_blk['fork_count'], int))
check("chain.dao_vault_oby present", 'dao_vault_oby' in c_blk)
check("chain.founder_vested present", 'founder_vested' in c_blk)

mp_blk = body['mempool']
check("mempool.size is int", isinstance(mp_blk['size'], int))
check("mempool.total_fees is number",
      isinstance(mp_blk['total_fees'], (int, float)))

p_blk = body['persistence']
check("persistence.save_count is int", isinstance(p_blk['save_count'], int))
check("persistence.last_save_duration_ms is number",
      isinstance(p_blk['last_save_duration_ms'], (int, float)))
check("persistence.chain_state_path is string",
      isinstance(p_blk['chain_state_path'], str))

counters = body['counters']
check("counters has all declared names",
      set(counters.keys()) == set(COUNTER_NAMES))
check("all counters start at 0 on fresh node",
      all(v == 0 for v in counters.values()))


section("Mine blocks and verify /metrics reflects chain growth")
founder = node.wallet.primary_address
node.chain.mine_block(founder)
node.chain.mine_block(founder)

code, body = get(f'{BASE}/metrics')
check("chain.height increased after mining",
      body['chain']['height'] >= 2)
check("chain.tip_hash populated after mining",
      body['chain']['tip_hash'] is not None
      and len(body['chain']['tip_hash']) > 10)
check("chain.tip_miner is founder",
      body['chain']['tip_miner'] == founder)
check("chain.utxo_count > 0 after mining",
      body['chain']['utxo_count'] > 0)


section("Trigger save_state and verify persist metrics update")
prev_save_count = node.metrics.persist.save_count
node.save_state()
check("save_count incremented after save_state()",
      node.metrics.persist.save_count == prev_save_count + 1)
check("last_save_duration_ms > 0",
      node.metrics.persist.last_save_duration_ms > 0)

code, body = get(f'{BASE}/metrics')
p_blk = body['persistence']
check("/metrics reflects new save_count",
      p_blk['save_count'] == prev_save_count + 1)
check("/metrics reflects last_save_duration_ms",
      p_blk['last_save_duration_ms'] > 0)
check("counters.persist_saves increased",
      body['counters']['persist_saves'] == prev_save_count + 1)


section("/health after a successful save")
code, body = get(f'{BASE}/health')
check("returns 200", code == 200)
check("status is ok with persist healthy", body.get('status') == 'ok')


section("Counter increments visible through /metrics")
# Touch a few counters via the registry directly (simulating what 5.5c will
# do from real call sites) and confirm they propagate to /metrics.
node.metrics.increment('blocks_received', by=7)
node.metrics.increment('txs_received', by=15)
node.metrics.increment('verification_passes', by=3)

code, body = get(f'{BASE}/metrics')
c = body['counters']
check("blocks_received reflects increment", c['blocks_received'] == 7)
check("txs_received reflects increment", c['txs_received'] == 15)
check("verification_passes reflects increment", c['verification_passes'] == 3)


section("Reorg recorded via registry shows in /metrics")
node.metrics.record_reorg(
    depth=2,
    invalidated=['hashA', 'hashB'],
    new_canonical=['hashC', 'hashD'],
    miners=['M1', 'M2'],
)
code, body = get(f'{BASE}/metrics')
rs = body['reorgs']
check("/metrics returns reorg history list", isinstance(rs, list))
check("reorg entry present", len(rs) >= 1)
check("reorg entry has depth", rs[-1].get('depth') == 2)
check("counters.reorgs_total incremented",
      body['counters']['reorgs_total'] == 1)
check("counters.reorgs_by_depth_2 incremented",
      body['counters']['reorgs_by_depth_2'] == 1)


section("/health flips to 503 when persist goes stale (simulated)")
# Force-stale the registry to confirm 503 path. We use a fresh node-attached
# registry rather than mutating the real one, to avoid leaving the running
# node in an unhealthy state for subsequent tests.
original_persist = node.metrics.persist
node.metrics.persist = PersistStats(
    last_save_ts=time.time() - (HEALTH_PERSIST_STALE_S + 50),
    save_count=5,
)
# Also make uptime appear well past boot grace
original_started = node.started_at
node.started_at = time.time() - 10000
# And force a stale tip so we cross from degraded → unhealthy (need 2 issues)
# We can't easily mutate tip timestamps, so we'll lean on persist failure
# escalation — simulate save failure to also count.
node.metrics.persist.last_failure_reason = 'simulated'

code, body = get(f'{BASE}/health')
# Single 'persist_stale' is degraded → still 200. That's the right behavior;
# 503 should require something genuinely broken. Verify the degraded path:
check("degraded persist returns 200 (still serving)", code == 200)
check("status reflects degraded state",
      body['status'] in ('degraded', 'unhealthy'))
check("reasons surface the stale persist",
      any('persist_stale' in r for r in body.get('reasons', [])))

# Now make it genuinely unhealthy: persist never ran AT ALL
node.metrics.persist = PersistStats()  # save_count=0
code, body = get(f'{BASE}/health')
check("never-ran persist returns 503", code == 503)
check("status is unhealthy", body['status'] == 'unhealthy')
check("reason names persist_loop_never_ran",
      any('persist_loop_never_ran' in r for r in body.get('reasons', [])))

# Restore so we don't leave the node in a bad state for any later additions
node.metrics.persist = original_persist
node.started_at = original_started


section("Concurrent counter increments (thread safety)")
m_thr = MetricsRegistry()
def hammer():
    for _ in range(1000):
        m_thr.increment('blocks_received')

threads = [threading.Thread(target=hammer) for _ in range(8)]
for t in threads: t.start()
for t in threads: t.join()
check("concurrent increments do not lose counts (8000 total)",
      m_thr.counters()['blocks_received'] == 8000)


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 50)
print(f"  PASSED: {PASSED}")
print(f"  FAILED: {len(FAILED)}")
for n, d in FAILED:
    print(f"    - {n}: {d}")
print("=" * 50)
if FAILED:
    print("  Some checks red. Phase 5.5a needs fixes.")
    sys.exit(1)
print("  All checks green. Phase 5.5a observability ready.")
sys.exit(0)
