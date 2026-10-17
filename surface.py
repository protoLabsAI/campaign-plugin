"""The library's background surface — one daemon thread that keeps the gallery tidy.

Every ``TICK_S`` it runs :func:`tick`, which today does one job:

* **retention, daily** — :func:`retention.run_scheduled` in the ``retention_mode`` the operator
  set (``report`` by default: a dry run whose report is saved for the gallery and
  ``campaign_prune``; ``prune`` deletes; ``disabled`` skips). Due when the last saved report is a day
  old, or was made in a different mode (so flipping the setting takes effect within a tick).

Config is read LIVE on every tick (``registry.live_config()``), so a Settings save needs no
restart. ``start``/``stop``/``reload`` are module-level, so the host's re-register on a config
reload sees the same surface and keeps it running (protoAgent #3593). The thread waits
``FIRST_DELAY_S`` after boot before its first tick — never a burst of disk work at startup.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any, Callable

from . import retention

log = logging.getLogger("protoagent.plugins.campaign")

TICK_S = 60.0
FIRST_DELAY_S = 600.0
RETENTION_EVERY_S = 86400.0

_cfg: Callable[[], dict[str, Any]] = dict
_stop = threading.Event()
_thread: threading.Thread | None = None
_lock = threading.Lock()


def configure(get_cfg: Callable[[], dict[str, Any]]) -> None:
    global _cfg
    _cfg = get_cfg


def _config() -> dict[str, Any]:
    try:
        return dict(_cfg() or {})
    except Exception:  # noqa: BLE001 — a host hiccup reads as defaults (report-only: safe)
        return {}


def retention_due(cfg: dict[str, Any], now: datetime) -> bool:
    m = retention.mode(cfg)
    if m == "disabled":
        return False
    last = retention.last_report()
    if not last:
        return True
    if last.get("mode") != m:
        return True
    at = retention._parse(last.get("generated_at") or "")
    return at is None or (now - at).total_seconds() >= RETENTION_EVERY_S


def tick(now: datetime | None = None) -> dict[str, Any]:
    """One pass. Returns what ran (for tests and the log)."""
    now = now or datetime.now(UTC)
    cfg = _config()
    ran: dict[str, Any] = {}
    try:
        if retention_due(cfg, now):
            report = retention.run_scheduled(cfg, now)
            ran["retention"] = retention.mode(cfg) if report is not None else "disabled"
    except Exception:  # noqa: BLE001 — one bad pass never kills the loop
        log.exception("[campaign] retention pass failed")
        ran["retention"] = "error"
    return ran


def _loop() -> None:
    if _stop.wait(FIRST_DELAY_S):
        return
    while True:
        tick()
        if _stop.wait(TICK_S):
            return


def start() -> None:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="campaign-library", daemon=True)
        _thread.start()


def stop() -> None:
    global _thread
    with _lock:
        _stop.set()
        t, _thread = _thread, None
    if t is not None:
        t.join(timeout=5)


def reload(_cfg_obj: Any = None) -> None:
    """Config is read live each tick — nothing to swap."""
    return None
