"""Replay of recent database query results, for fast rebuilds.

A rate or rule saved on the Data page changes how commissions are CALCULATED,
not what the invoices ARE. Rebuilding the report used to download every
invoice again all the same -- about fifty seconds, forty-seven of them the
Internal Basic Commission query alone -- while the calculation itself takes
under a second. During that minute the report showed the old figures, or none.

Every query helper the commission scripts use records its latest result here.
A rebuild started because a rule changed runs inside ``replaying()``, and any
query whose text is identical to one already recorded is answered from the
record instead of the database. The data is exactly as fresh as what the
report was already showing: this only skips downloading it a second time.

A rule change that alters a query (a new payment milestone percentage adds a
column to the invoice query, a role change alters an agent list) produces
different SQL, finds no record, and goes to the database as usual -- so a
replay can never answer a question it was not asked. Scheduled refreshes and
"Sync Data" never replay, so new invoices and payments still arrive on the
normal timetable.
"""

import copy
import json
import threading
import time
from contextlib import contextmanager

_lock = threading.Lock()
_store: dict = {}          # key -> (recorded_at, result)
_MAX_ENTRIES = 256
_replaying = 0
_stats = {"hits": 0, "misses": 0}


def _key(db_name, sql, params) -> str:
    # Whitespace-insensitive: the same query built by two f-strings with
    # different indentation is the same query.
    return json.dumps([str(db_name or ""), " ".join(str(sql or "").split()), params or []],
                      default=str, sort_keys=True)


def lookup(db_name, sql, params=None):
    """The recorded result for this exact query while a replay is running,
    else None (meaning: ask the database)."""
    if not _replaying:
        return None
    k = _key(db_name, sql, params)
    with _lock:
        hit = _store.get(k)
        if hit is None:
            _stats["misses"] += 1
            return None
        _stats["hits"] += 1
    # Callers mutate the rows they get back; never hand out the record itself.
    return copy.deepcopy(hit[1])


def record(db_name, sql, params, result) -> None:
    """Remember a result fetched from the database."""
    k = _key(db_name, sql, params)
    snapshot = copy.deepcopy(result)
    with _lock:
        _store[k] = (time.time(), snapshot)
        if len(_store) > _MAX_ENTRIES:
            for old in sorted(_store, key=lambda x: _store[x][0])[: len(_store) - _MAX_ENTRIES]:
                _store.pop(old, None)


@contextmanager
def replaying():
    """Answer identical queries from the record for the duration."""
    global _replaying
    with _lock:
        _replaying += 1
        _stats["hits"] = _stats["misses"] = 0
    try:
        yield
    finally:
        with _lock:
            _replaying -= 1


def stats() -> dict:
    with _lock:
        return dict(_stats, entries=len(_store))


def snapshot() -> dict:
    """Everything recorded, for saving alongside the dashboard cache so the
    first rule change after a restart is fast too."""
    with _lock:
        return dict(_store)


def restore(saved) -> None:
    if not isinstance(saved, dict):
        return
    with _lock:
        for k, v in saved.items():
            if k not in _store and isinstance(v, tuple) and len(v) == 2:
                _store[k] = v
