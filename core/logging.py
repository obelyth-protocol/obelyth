"""Structured JSON logging for Obelyth.

Phase 5.5b. Goals:

  1. Every existing log.info()/log.warning()/log.error() in the codebase
     gets converted to a JSON line automatically. Zero call-site changes.
  2. A new log_event(level, event, **context) helper for places that want
     to emit explicit event names with structured context (used by 5.5c
     to tie log events to counter increments).
  3. The text format remains available — set OBELYTH_LOG=text in the env
     to keep human-readable logs during local development.

Output schema (one object per line):

  {
    "ts": "2026-06-03T12:34:56.789Z",   # ISO 8601 UTC
    "level": "info",                     # info | warn | error | debug
    "logger": "obelyth.node",            # which module emitted it
    "event": "block_accepted",           # short event identifier
    "msg": "Accepted block 42",          # human-readable message
    ...context...                        # any extra key/value pairs
  }

Event naming convention:
  - Lower snake_case, short
  - Verb-past for state transitions: block_accepted, peer_disconnected
  - Verb-present for actions: persist_attempt, challenge_issued
  - Tied to counter names from observability.py where applicable
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Optional


# ── Constants ────────────────────────────────────────────────────────────────

# Set OBELYTH_LOG=text to skip the JSON formatter (useful for local dev).
JSON_FORMAT_ENV = 'OBELYTH_LOG'
TEXT_FORMAT_VALUE = 'text'

# Standard fields the formatter emits on every line. Anything in extra=
# beyond these names becomes part of the context block.
RESERVED_FIELDS = frozenset({
    'name', 'msg', 'args', 'levelname', 'levelno', 'pathname', 'filename',
    'module', 'exc_info', 'exc_text', 'stack_info', 'lineno', 'funcName',
    'created', 'msecs', 'relativeCreated', 'thread', 'threadName',
    'processName', 'process', 'message', 'asctime', 'taskName',
})


# ── Formatter ────────────────────────────────────────────────────────────────

class JsonFormatter(logging.Formatter):
    """Emits one JSON object per log record.

    Inherits from logging.Formatter so it slots into the stdlib's logging
    pipeline cleanly — handlers can use it like any other formatter.
    """

    # Map stdlib log level names to our shorter forms
    LEVEL_MAP = {
        'DEBUG'   : 'debug',
        'INFO'    : 'info',
        'WARNING' : 'warn',
        'WARN'    : 'warn',
        'ERROR'   : 'error',
        'CRITICAL': 'error',
        'FATAL'   : 'error',
    }

    def format(self, record: logging.LogRecord) -> str:
        # ISO 8601 UTC with millisecond precision. We use time.gmtime
        # against record.created (set by stdlib at log emit) so the ts
        # exactly matches when the event happened, not when the formatter
        # ran (which can differ under load).
        ts_struct = time.gmtime(record.created)
        ts = (
            time.strftime('%Y-%m-%dT%H:%M:%S', ts_struct)
            + f'.{int(record.msecs):03d}Z'
        )

        msg = record.getMessage()
        out: dict[str, Any] = {
            'ts'    : ts,
            'level' : self.LEVEL_MAP.get(record.levelname, record.levelname.lower()),
            'logger': record.name,
        }

        # If the call site passed an explicit event= via extra, use it.
        # Otherwise, derive a default event name from the logger ("obelyth.node"
        # → "log") — keeps every line parseable but doesn't pretend to know
        # what the event actually is. New code should pass event= explicitly.
        out['event'] = getattr(record, 'event', 'log')
        out['msg']   = msg

        # Pull any extra fields the caller attached via extra={...} or via
        # log_event(**context). RESERVED_FIELDS protects against the stdlib's
        # built-in record attributes leaking into the JSON.
        for key, value in record.__dict__.items():
            if key in RESERVED_FIELDS or key in out:
                continue
            # Best-effort JSON serialization: anything not natively
            # serializable becomes its repr() so we don't drop the line.
            try:
                json.dumps(value)
                out[key] = value
            except (TypeError, ValueError):
                out[key] = repr(value)

        # Exception info — flatten to a single string field for easy grepping.
        if record.exc_info:
            out['exc'] = self.formatException(record.exc_info)

        # ensure_ascii=False keeps unicode (addresses, names) readable.
        # default=str catches any stragglers (datetime, Path, etc.)
        return json.dumps(out, ensure_ascii=False, default=str)


# ── Installation ─────────────────────────────────────────────────────────────

_installed = False


def install(level: int = logging.INFO, stream=None) -> None:
    """Replace the root logger's handlers with one JSON-emitting handler.

    Idempotent — safe to call multiple times. Call once at process startup
    BEFORE any module-level logger gets used (i.e. before importing modules
    that emit log lines on import).

    If OBELYTH_LOG=text is set in the environment, falls back to a plain
    text formatter for development — keeps the operator-friendly local
    output when JSON would be noise.
    """
    global _installed
    if _installed:
        return

    stream = stream or sys.stdout
    use_text = os.environ.get(JSON_FORMAT_ENV, '').lower() == TEXT_FORMAT_VALUE

    root = logging.getLogger()
    # Strip any handlers a previous basicConfig() installed. This is the
    # whole point of running install() at startup — we want our handler,
    # not the stdlib's default.
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(stream)
    if use_text:
        handler.setFormatter(logging.Formatter(
            fmt='%(asctime)s [%(name)s] %(levelname)s %(message)s',
            datefmt='%H:%M:%S',
        ))
    else:
        handler.setFormatter(JsonFormatter())
    handler.setLevel(level)

    root.addHandler(handler)
    root.setLevel(level)

    _installed = True


# ── log_event helper ─────────────────────────────────────────────────────────

# Default logger for log_event() calls that don't specify one. Most call sites
# in observability/health/persist code will use this.
_event_logger = logging.getLogger('obelyth')


def log_event(
    level     : str,
    event     : str,
    msg       : Optional[str] = None,
    logger    : Optional[logging.Logger] = None,
    **context : Any,
) -> None:
    """Emit a structured event.

    Args:
        level    : 'info' | 'warn' | 'error' | 'debug'
        event    : short snake_case event name (e.g. 'block_accepted')
        msg      : optional human-readable message; if None, the event name
                   is used.
        logger   : specific logger to emit on; defaults to 'obelyth'
        **context: arbitrary structured fields to attach to the event

    Example:
        log_event('info', 'block_accepted', height=42, hash='0xabc...')
        log_event('warn', 'block_rejected', reason='invalid_dao_tax',
                  expected=50, got=42)
    """
    lg = logger or _event_logger
    log_method = {
        'debug': lg.debug,
        'info' : lg.info,
        'warn' : lg.warning,
        'warning': lg.warning,
        'error': lg.error,
    }.get(level.lower(), lg.info)

    # Build extra dict — 'event' attaches via __dict__ on the LogRecord and
    # the JsonFormatter picks it up. Same for any **context kwargs.
    extra = {'event': event, **context}
    log_method(msg or event, extra=extra)
