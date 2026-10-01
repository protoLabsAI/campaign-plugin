"""Run a blocking callable on a fresh worker thread.

Playwright's sync API refuses to start on a thread that has a running asyncio loop — and a
tool call from the agent lands on exactly such a thread (or an executor thread that may
reuse one). A dedicated short-lived thread sidesteps that everywhere, at no cost.
"""

from __future__ import annotations

import threading
from typing import Any, Callable


class Wedged(TimeoutError):
    pass


def in_thread(fn: Callable[[], Any], timeout_s: float, name: str = "campaign-worker") -> Any:
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["result"] = fn()
        except BaseException as e:  # noqa: BLE001 — re-raised on the caller's thread
            box["error"] = e

    t = threading.Thread(target=_target, name=name, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise Wedged(f"{name} didn't finish within {timeout_s:.0f}s")
    if "error" in box:
        raise box["error"]
    return box.get("result")
