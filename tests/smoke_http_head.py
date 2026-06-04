"""
Smoke runner for HEAD method support (Phase 5.5b — pre-existing quirk fix).

The Python stdlib's BaseHTTPRequestHandler returns 501 Not Implemented for
HEAD requests unless we explicitly override do_HEAD. UptimeRobot / curl -I /
health checkers all use HEAD before GET, so 501 = false-positive alert.

This suite verifies:
  - HEAD /health, /metrics, /status, /blocks return 200 with no body
  - GET still returns 200 with body
  - Content-Length on HEAD matches what GET would return
"""

import sys
import os
import json
import time
import tempfile
import threading
import urllib.request
import socket
from http.server import HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from node.fullnode import FullNode, RPCHandler


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
    s = socket.socket(); s.bind(('', 0)); p = s.getsockname()[1]; s.close()
    return p


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, dict(r.headers), r.read()


def head(url):
    req = urllib.request.Request(url, method='HEAD')
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, dict(r.headers), r.read()


# ── Boot the node ────────────────────────────────────────────────────────────

P2P  = find_free_port()
RPC  = find_free_port()
TMP  = tempfile.mkdtemp(prefix='oby-head-')
BASE = f'http://127.0.0.1:{RPC}'

print(f"\nBooting FullNode on RPC port {RPC}")
node = FullNode(p2p_port=P2P, rpc_port=RPC, data_dir=TMP, mine=False)
RPCHandler.node = node
srv = HTTPServer(('127.0.0.1', RPC), RPCHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)

# Mine a couple of blocks so /metrics has something to return
founder = node.wallet.primary_address
node.chain.mine_block(founder)
node.chain.mine_block(founder)


# ── HEAD requests should NOT return 501 ──────────────────────────────────────

section("HEAD /health returns 200")
status, headers, body = head(f'{BASE}/health')
check("returns 200, not 501", status == 200,
      detail=f"got {status}")
check("body is empty (HEAD)", body == b'',
      detail=f"body={body!r}")
check("Content-Length header present",
      'Content-Length' in headers or 'content-length' in headers)


section("HEAD /metrics returns 200 with non-zero Content-Length")
status, headers, body = head(f'{BASE}/metrics')
check("returns 200", status == 200)
check("body empty on HEAD", body == b'')
cl = int(headers.get('Content-Length', headers.get('content-length', '0')))
check("Content-Length is non-zero (would have a real body)",
      cl > 100,
      detail=f"Content-Length={cl}")


section("HEAD /status returns 200")
status, headers, body = head(f'{BASE}/status')
check("returns 200", status == 200)
check("body empty", body == b'')


section("HEAD /blocks returns 200")
status, headers, body = head(f'{BASE}/blocks')
check("returns 200", status == 200)
check("body empty", body == b'')


# ── GET still works (didn't break GET while adding HEAD) ─────────────────────

section("GET /health still works")
status, headers, body = get(f'{BASE}/health')
check("returns 200", status == 200)
check("body is non-empty JSON", len(body) > 0)
parsed = json.loads(body)
check("body parses as JSON", isinstance(parsed, dict))
check("body has status field", 'status' in parsed)


section("GET /metrics still works")
status, headers, body = get(f'{BASE}/metrics')
check("returns 200", status == 200)
check("body has node block", b'"node"' in body)
check("body has chain block", b'"chain"' in body)


# ── HEAD Content-Length matches GET body length ──────────────────────────────

section("HEAD Content-Length equals GET body length")
_, head_headers, _ = head(f'{BASE}/metrics')
_, _, get_body = get(f'{BASE}/metrics')
head_cl = int(head_headers.get('Content-Length', head_headers.get('content-length', '0')))
check("HEAD Content-Length matches GET body byte count",
      head_cl == len(get_body),
      detail=f"HEAD={head_cl}, GET body={len(get_body)} bytes")


# ── OPTIONS still works (didn't break CORS preflight) ────────────────────────

section("OPTIONS /sendtx still returns 204 (CORS preflight unbroken)")
req = urllib.request.Request(f'{BASE}/sendtx', method='OPTIONS')
with urllib.request.urlopen(req, timeout=5) as r:
    check("OPTIONS returns 204", r.status == 204)


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
    print("  Some checks red. HEAD support needs fixes.")
    sys.exit(1)
print("  All checks green. HEAD method support ready.")
sys.exit(0)
