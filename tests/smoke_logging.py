"""
Smoke runner for the structured JSON logging surface (Phase 5.5b).

Verifies:
  - JsonFormatter emits one valid JSON object per log record
  - All required fields (ts, level, logger, event, msg) present
  - extra={'foo': 'bar'} fields flow through to JSON
  - log_event() helper attaches event + context correctly
  - install() is idempotent and replaces existing handlers
  - OBELYTH_LOG=text env variable falls back to text formatter
  - Existing log.info() calls in stdlib-style work unchanged
  - Exception info is captured cleanly
"""

import sys
import os
import io
import json
import logging
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.logging import (
    JsonFormatter,
    install,
    log_event,
    JSON_FORMAT_ENV,
    TEXT_FORMAT_VALUE,
    RESERVED_FIELDS,
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


def capture_logs(logger_name='obelyth.test', level=logging.DEBUG):
    """Set up a logger that writes JSON to an in-memory buffer for inspection."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    handler.setLevel(level)
    lg = logging.getLogger(logger_name)
    # Strip existing handlers so test output isolates
    for h in list(lg.handlers):
        lg.removeHandler(h)
    lg.addHandler(handler)
    lg.setLevel(level)
    lg.propagate = False
    return lg, buf


def reset_logger(name):
    """Wipe a logger's handlers and reset its config so subsequent tests
    don't inherit leftover state from earlier capture_logs() calls."""
    lg = logging.getLogger(name)
    for h in list(lg.handlers):
        lg.removeHandler(h)
    lg.propagate = True
    lg.setLevel(logging.NOTSET)


def reset_all_test_loggers():
    """Reset every logger we touch across the test file.

    Python's logging module keeps all loggers as singletons in a global
    dict, so settings made by earlier tests (propagate=False, attached
    handlers) persist into later sections. The install() tests need a
    clean tree below the root to actually propagate messages back to
    the root handler.
    """
    names = [
        'obelyth', 'obelyth.test1', 'obelyth.test2', 'obelyth.test3',
        'obelyth.test4', 'obelyth.test5', 'obelyth.test6', 'obelyth.test7',
        'obelyth.specific', 'obelyth.fresh', 'obelyth.text', 'obelyth.engine',
    ]
    for n in names:
        reset_logger(n)


def parse_lines(buf):
    buf.seek(0)
    lines = [l for l in buf.read().splitlines() if l.strip()]
    return [json.loads(l) for l in lines]


# ══════════════════════════════════════════════════════════════════════════════
# JsonFormatter
# ══════════════════════════════════════════════════════════════════════════════

section("JsonFormatter: basic record shape")
lg, buf = capture_logs('obelyth.test1')
lg.info("hello world")
records = parse_lines(buf)
check("emits exactly 1 record for 1 log call", len(records) == 1)
r = records[0]
check("has 'ts' field",     'ts' in r)
check("has 'level' field",  'level' in r)
check("has 'logger' field", 'logger' in r)
check("has 'event' field",  'event' in r)
check("has 'msg' field',",  'msg' in r)
check("level is 'info'",    r.get('level') == 'info')
check("logger matches",     r.get('logger') == 'obelyth.test1')
check("msg matches",        r.get('msg') == 'hello world')
check("default event is 'log' for plain log.info() calls",
      r.get('event') == 'log')


section("JsonFormatter: timestamp format")
ts = records[0]['ts']
check("ts is ISO 8601",
      len(ts) == 24 and ts[10] == 'T' and ts.endswith('Z'),
      detail=f"ts={ts!r}")
check("ts contains millisecond field",
      '.' in ts and ts.split('.')[1].rstrip('Z').isdigit(),
      detail=f"ts={ts!r}")
# Sanity-check the ts parses back to a recent time
import datetime
parsed = datetime.datetime.strptime(ts, '%Y-%m-%dT%H:%M:%S.%fZ')
parsed = parsed.replace(tzinfo=datetime.timezone.utc)
delta_s = abs((datetime.datetime.now(datetime.timezone.utc) - parsed).total_seconds())
check("ts is within last 5 seconds (clock sanity)",
      delta_s < 5,
      detail=f"delta_s={delta_s}")


section("JsonFormatter: level mapping")
lg, buf = capture_logs('obelyth.test2')
lg.debug("d");    lg.info("i")
lg.warning("w");  lg.error("e")
lg.critical("c")
records = parse_lines(buf)
levels = [r['level'] for r in records]
check("debug → 'debug'",    'debug' in levels)
check("info → 'info'",      'info'  in levels)
check("warning → 'warn'",   'warn'  in levels)
check("error → 'error'",    'error' in levels)
check("critical → 'error'", levels.count('error') == 2)


section("JsonFormatter: extra fields propagate")
lg, buf = capture_logs('obelyth.test3')
lg.info("structured", extra={'height': 42, 'hash': '0xabc', 'tier': 'standard'})
records = parse_lines(buf)
r = records[0]
check("height=42 in record",     r.get('height') == 42)
check("hash='0xabc' in record",  r.get('hash') == '0xabc')
check("tier='standard' in record", r.get('tier') == 'standard')


section("JsonFormatter: reserved fields don't leak from LogRecord")
lg, buf = capture_logs('obelyth.test4')
lg.info("plain")
r = parse_lines(buf)[0]
for f in ['module', 'pathname', 'lineno', 'funcName', 'thread', 'process']:
    check(f"reserved field '{f}' not in JSON output", f not in r)


section("JsonFormatter: non-serializable extras handled gracefully")
class Weird:
    def __repr__(self): return '<Weird obj>'
lg, buf = capture_logs('obelyth.test5')
lg.info("weird", extra={'obj': Weird()})
records = parse_lines(buf)
check("emits one record without crashing", len(records) == 1)
check("non-serializable object becomes its repr",
      records[0].get('obj') == '<Weird obj>')


section("JsonFormatter: exception info captured")
lg, buf = capture_logs('obelyth.test6')
try:
    raise ValueError("test failure")
except ValueError:
    lg.exception("caught it")
records = parse_lines(buf)
check("emits one record", len(records) == 1)
check("has 'exc' field", 'exc' in records[0])
check("exc field mentions ValueError",
      'ValueError' in records[0].get('exc', ''))
check("exc field mentions message",
      'test failure' in records[0].get('exc', ''))


section("JsonFormatter: unicode survives")
lg, buf = capture_logs('obelyth.test7')
lg.info("addr lookup", extra={'address': 'Hēllo世界'})
records = parse_lines(buf)
check("unicode preserved", records[0].get('address') == 'Hēllo世界')


# ══════════════════════════════════════════════════════════════════════════════
# log_event helper
# ══════════════════════════════════════════════════════════════════════════════

section("log_event: emits structured event")
lg, buf = capture_logs('obelyth')   # use default logger name
log_event('info', 'block_accepted',
          msg='Block 42 accepted',
          height=42, hash='0xabc', miner='H123')
records = parse_lines(buf)
check("emits one record", len(records) == 1)
r = records[0]
check("event field set", r.get('event') == 'block_accepted')
check("level field set", r.get('level') == 'info')
check("msg field set",   r.get('msg') == 'Block 42 accepted')
check("context height",  r.get('height') == 42)
check("context hash",    r.get('hash') == '0xabc')
check("context miner",   r.get('miner') == 'H123')


section("log_event: msg defaults to event name when not given")
lg, buf = capture_logs('obelyth')
log_event('info', 'persist_complete', duration_ms=23.5)
r = parse_lines(buf)[0]
check("msg defaults to event name", r.get('msg') == 'persist_complete')


section("log_event: level aliases work")
lg, buf = capture_logs('obelyth')
log_event('warn',  'mempool_overflow', size=5001)
log_event('warning', 'tip_stale', age_s=1900)
log_event('error', 'persist_failure', reason='disk_full')
records = parse_lines(buf)
levels = [r['level'] for r in records]
check("warn → 'warn'",    levels[0] == 'warn')
check("warning → 'warn'", levels[1] == 'warn')
check("error → 'error'",  levels[2] == 'error')


section("log_event: unknown level falls back to info")
lg, buf = capture_logs('obelyth')
log_event('nonexistent_level', 'whatever', x=1)
records = parse_lines(buf)
check("unknown level still emits", len(records) == 1)
check("falls back to info", records[0]['level'] == 'info')


section("log_event: custom logger respected")
lg_a, buf_a = capture_logs('obelyth.specific')
log_event('info', 'something', logger=lg_a, foo='bar')
r = parse_lines(buf_a)[0]
check("emitted on the specified logger",
      r.get('logger') == 'obelyth.specific')


# ══════════════════════════════════════════════════════════════════════════════
# install() integration
# ══════════════════════════════════════════════════════════════════════════════

section("install(): replaces root logger handlers and writes JSON to stream")
# Manually reset state (test isolation)
import core.logging as cl
cl._installed = False

# Reset the entire test logger tree — earlier sections set propagate=False
# on various loggers including 'obelyth' (the parent). Without resetting,
# log messages from 'obelyth.fresh' would be absorbed by 'obelyth' before
# reaching the root handler that install() set up.
reset_all_test_loggers()

# Reset the root logger too
root = logging.getLogger()
for h in list(root.handlers):
    root.removeHandler(h)

buf = io.StringIO()
install(stream=buf)

logging.getLogger('obelyth.fresh').info("freshly installed")

# Force the StreamHandler to flush so we see the content
for h in root.handlers:
    h.flush()

output = buf.getvalue()
lines = [l for l in output.splitlines() if l.strip()]
check("install emits to stream by default",
      any('freshly installed' in l for l in lines),
      detail=f"output={output!r}")

matching = [l for l in lines if 'freshly installed' in l]
if matching:
    try:
        record = json.loads(matching[0])
        check("emitted line is valid JSON", True)
        check("level field present", 'level' in record)
        check("msg field present",   record.get('msg') == 'freshly installed')
        check("logger name preserved",
              record.get('logger') == 'obelyth.fresh')
    except json.JSONDecodeError as e:
        check("emitted line is valid JSON", False, detail=str(e))


section("install(): idempotent")
cl._installed = True
handler_count_before = len(logging.getLogger().handlers)
install()
handler_count_after = len(logging.getLogger().handlers)
check("idempotent: handler count unchanged on second call",
      handler_count_before == handler_count_after)


section("install(): OBELYTH_LOG=text falls back to text formatter")
# Full reset for a clean text-mode test
cl._installed = False
reset_all_test_loggers()
for h in list(logging.getLogger().handlers):
    logging.getLogger().removeHandler(h)

os.environ[JSON_FORMAT_ENV] = TEXT_FORMAT_VALUE
buf = io.StringIO()
install(stream=buf)
logging.getLogger('obelyth.text').info("plain text mode")

# Flush handlers so we capture output before reading
for h in logging.getLogger().handlers:
    h.flush()

output = buf.getvalue()
check("text mode does NOT emit valid JSON",
      not output.strip().startswith('{'),
      detail=f"output={output!r}")
check("text mode emits the message",
      'plain text mode' in output,
      detail=f"output={output!r}")
check("text mode includes logger name",
      'obelyth.text' in output,
      detail=f"output={output!r}")

# Clean up env so other tests aren't affected
del os.environ[JSON_FORMAT_ENV]
cl._installed = False


# ══════════════════════════════════════════════════════════════════════════════
# Real-world integration — make sure existing log.info calls produce JSON
# after install()
# ══════════════════════════════════════════════════════════════════════════════

section("Existing-style log.info() calls become JSON after install")
# Full reset
cl._installed = False
reset_all_test_loggers()
for h in list(logging.getLogger().handlers):
    logging.getLogger().removeHandler(h)

buf = io.StringIO()
install(stream=buf)
existing_logger = logging.getLogger('obelyth.engine')   # name used by blockchain.py
existing_logger.info("Mined block 42")
existing_logger.warning("Mempool large: 5234")
existing_logger.error("Failed to save: disk full")

for h in logging.getLogger().handlers:
    h.flush()

output = buf.getvalue()
lines = [l for l in output.splitlines() if l.strip()]
check("3 lines emitted", len(lines) == 3,
      detail=f"got {len(lines)} lines, output={output!r}")

if len(lines) == 3:
    for i, l in enumerate(lines):
        try:
            json.loads(l)
            check(f"line {i} is valid JSON", True)
        except json.JSONDecodeError as e:
            check(f"line {i} is valid JSON", False, detail=str(e))
    parsed = [json.loads(l) for l in lines]
    check("line 1: level=info",    parsed[0]['level'] == 'info')
    check("line 2: level=warn",    parsed[1]['level'] == 'warn')
    check("line 3: level=error",   parsed[2]['level'] == 'error')
    check("line 1: msg has block info",
          'Mined block 42' in parsed[0]['msg'])
    check("all use logger 'obelyth.engine'",
          all(p['logger'] == 'obelyth.engine' for p in parsed))


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
    print("  Some checks red. Phase 5.5b needs fixes.")
    sys.exit(1)
print("  All checks green. Phase 5.5b structured logging ready.")
sys.exit(0)
