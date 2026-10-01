"""Which Python runs the Playwright worker — and running it safely.

The host process never imports playwright (the frozen desktop app can't install it: platform
wheels + a Node driver). Every browser runs in ``worker/pw_worker.py`` under an interpreter
that HAS playwright, resolved in this order:

1. **``interpreter`` setting** — explicit, and strict: if it's set, it's the only candidate,
   and a bad value is reported rather than silently replaced by another interpreter.
2. **The managed Python runtime** (ADR 0094 P2, Settings ▸ Tools) — where the desktop app's
   *Install dependencies* puts a ``scope: runtime`` dep. Read through core's
   ``infra.python_runtime.managed_python_exe()`` (lazy + guarded — there is no plugin SDK
   accessor for it yet). Counts only if playwright is installed in it.
3. **This process's own interpreter** — only when NOT frozen (a frozen ``sys.executable`` is
   the server binary, not a Python) and playwright is findable here. That's every source /
   venv install, where *Install dependencies* pips into the agent's own venv.

Each candidate is asked what it can do by running the worker with ``--probe`` (stdlib only —
no browser, no playwright import); answers are cached briefly.

Spawning (:func:`run_python`): a scrubbed, allowlisted environment (no host credentials, no
frozen-app ``PYTHON*``/loader vars); the job — and any bearer — on **stdin**, never argv;
stdout/stderr to temp files and only a bounded tail read back; a new process group/session,
and on timeout the WHOLE group (driver + Chromium) is SIGKILLed — after a normal exit too,
so nothing a worker started can outlive it.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("protoagent.plugins.campaign")

WORKER = Path(__file__).resolve().parent / "worker" / "pw_worker.py"
OUTPUT_TAIL_BYTES = 16 * 1024
PROBE_TIMEOUT_S = 30.0
PROBE_TTL_S = 15.0

SOURCE_CONFIG = "interpreter setting"
SOURCE_MANAGED = "managed Python runtime"
SOURCE_HOST = "agent's own Python"

_PYTHON_PATH = ""


def configure(python: str = "") -> None:
    """The ``interpreter`` setting (blank = resolve automatically)."""
    global _PYTHON_PATH
    _PYTHON_PATH = (python or "").strip()
    invalidate()


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


# ── the managed runtime seam ──────────────────────────────────────────────────


def managed_python() -> str | None:
    """The managed runtime's interpreter, or None (not provisioned / host too old / no host).

    SEAM: core has no public plugin-SDK accessor for this; ``infra.python_runtime`` is what
    core's own execute_code uses. Imported lazily and fully guarded so a refactor there
    degrades to "not provisioned", never an exception."""
    try:
        import infra.python_runtime as pr  # host import — lazy
    except Exception:  # noqa: BLE001 — no host (tests, standalone)
        return None
    fn = getattr(pr, "managed_python_exe", None)
    if not callable(fn):
        return None
    try:
        exe = fn()
    except Exception:  # noqa: BLE001
        log.debug("[campaign] managed runtime lookup failed", exc_info=True)
        return None
    return str(exe) if exe else None


def host_has_playwright() -> bool:
    """Is playwright findable from THIS process? (find_spec — never imports it.)"""
    try:
        return importlib.util.find_spec("playwright") is not None
    except (ImportError, ValueError):
        return False


# ── the child environment ─────────────────────────────────────────────────────

_ENV_KEEP = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TEMP", "TMP",
    # Windows essentials
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "USERNAME",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    # display / fonts (headless needs few, but fontconfig matters for cards)
    "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "FONTCONFIG_FILE", "FONTCONFIG_PATH",
    # network plumbing the browser + `playwright install` need behind a proxy / custom CA
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
}  # fmt: skip
_ENV_KEEP_PREFIXES = ("LC_", "XDG_", "PLAYWRIGHT_")


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """An allowlisted copy of the environment for the worker / installer.

    Nothing else crosses: no operator or fleet token, no API key, no ``PYTHONPATH`` /
    ``PYTHONHOME`` (``-E`` ignores them anyway), and no frozen-app loader path — PyInstaller
    points ``LD_LIBRARY_PATH`` at its own bundle and keeps the original in ``*_ORIG``."""
    src = dict(os.environ if base is None else base)
    env = {k: v for k, v in src.items() if k in _ENV_KEEP or k.startswith(_ENV_KEEP_PREFIXES)}
    if frozen():
        if src.get("LD_LIBRARY_PATH_ORIG"):
            env["LD_LIBRARY_PATH"] = src["LD_LIBRARY_PATH_ORIG"]
    elif src.get("LD_LIBRARY_PATH"):
        env["LD_LIBRARY_PATH"] = src["LD_LIBRARY_PATH"]
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


# ── spawning, bounded and killable ────────────────────────────────────────────


@dataclass
class Completed:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    duration_s: float = 0.0


def _tail(f, limit: int = OUTPUT_TAIL_BYTES) -> str:
    try:
        f.flush()
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - limit))
        data = f.read()
    except (OSError, ValueError):
        return ""
    text = data.decode("utf-8", errors="replace")
    return ("…" + text) if size > limit else text


def _descendants(pid: int) -> list[int]:
    """Every live descendant of ``pid`` (POSIX, via ``ps``) — best-effort, [] on any failure."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return []
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found, stack = [], [pid]
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in found:
                found.append(c)
                stack.append(c)
    return found


def kill_group(proc: subprocess.Popen, *, tree: bool = False) -> None:
    """SIGKILL the worker's whole process group (POSIX) / tree (Windows). Idempotent.

    ``tree`` also hunts down descendants that left the group: Playwright launches Chromium
    *detached* (its own session + process group) on POSIX, so a group kill of the worker
    alone would orphan a hung browser. Each descendant's own group is killed too (that takes
    Chromium's renderers/GPU helpers with it)."""
    if os.name != "nt" and tree:
        own = os.getpgrp()
        for pid in _descendants(proc.pid):
            try:
                pgid = os.getpgid(pid)
                if pgid != own:
                    os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=15, check=False
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.kill()
        except OSError:
            pass
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # start_new_session ⇒ pgid == pid
    except (ProcessLookupError, PermissionError):
        pass


def run_python(
    python: str,
    args: list[str],
    *,
    stdin: bytes | None = None,
    timeout: float,
    env: dict[str, str] | None = None,
) -> Completed:
    """Run ``python -E <args>`` in its own process group with a hard deadline.

    ``stdin`` is the only channel for secrets (argv is visible to every local user via
    ``ps``). On timeout the group is SIGKILLed; on a normal exit it is swept too, so a
    Chromium the worker orphaned can't linger."""
    argv = [python, "-E", *args]
    kw: dict[str, Any] = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
    else:
        kw["start_new_session"] = True
    started = time.monotonic()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(  # noqa: S603 — argv is [resolved interpreter, -E, our own script, fixed flags]
            argv,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            env=child_env() if env is None else env,
            cwd=str(WORKER.parent),
            **kw,
        )
        timed_out = False
        try:
            proc.communicate(input=stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_group(proc, tree=True)  # while the worker lives, so its descendants are findable
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        finally:
            kill_group(proc)  # sweep anything the worker left behind
        return Completed(
            returncode=proc.returncode,
            stdout=_tail(out),
            stderr=_tail(err),
            timed_out=timed_out,
            duration_s=round(time.monotonic() - started, 3),
        )


# ── probing + resolution ──────────────────────────────────────────────────────


@dataclass
class Probe:
    python: str
    ok: bool  # the interpreter ran the probe at all
    playwright: str | None = None
    chromium: bool = False
    browsers_root: str = ""
    error: str = ""


_PROBES: dict[str, tuple[float, Probe]] = {}
_PROBE_LOCK = threading.Lock()
_PATH_LOCKS: dict[str, threading.Lock] = {}


def invalidate() -> None:
    with _PROBE_LOCK:
        _PROBES.clear()


def probe(python: str) -> Probe:
    """Ask ``python`` (via the worker's ``--probe``) whether it has playwright + Chromium.

    One probe per interpreter at a time: concurrent callers for the same path wait for the
    first one's answer instead of each spawning their own."""
    with _PROBE_LOCK:
        lock = _PATH_LOCKS.setdefault(python, threading.Lock())
    with lock:
        with _PROBE_LOCK:
            hit = _PROBES.get(python)
            if hit and time.monotonic() - hit[0] < PROBE_TTL_S:
                return hit[1]
        p = _probe_uncached(python)
        with _PROBE_LOCK:
            _PROBES[python] = (time.monotonic(), p)
        return p


def _probe_uncached(python: str) -> Probe:
    if not Path(python).is_file():
        return Probe(python, ok=False, error=f"{python} doesn't exist")
    try:
        r = run_python(python, [str(WORKER), "--probe"], timeout=PROBE_TIMEOUT_S)
    except OSError as e:
        return Probe(python, ok=False, error=f"couldn't run {python}: {e}")
    if r.timed_out:
        return Probe(python, ok=False, error=f"{python} didn't answer within {PROBE_TIMEOUT_S:.0f}s")
    try:
        data = json.loads(r.stdout.strip() or "{}")
        if r.returncode != 0 or not isinstance(data, dict) or "playwright" not in data:
            raise ValueError
    except ValueError:
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["no output"]
        return Probe(python, ok=False, error=f"{python} couldn't run the probe ({tail[0][:200]})")
    return Probe(
        python,
        ok=True,
        playwright=data.get("playwright"),
        chromium=bool(data.get("chromium")),
        browsers_root=str(data.get("browsers_root") or ""),
    )


@dataclass
class Resolution:
    """The interpreter the worker will run with — or why there isn't one.

    ``need`` is the FIRST thing to fix: ``interpreter`` (the setting points somewhere
    useless), ``runtime`` (frozen app, no managed runtime: provision it), ``deps`` (no
    candidate has playwright: Install dependencies), ``chromium``, or '' (ready)."""

    python: str = ""
    source: str = ""
    playwright: str | None = None
    chromium: bool = False
    browsers_root: str = ""
    need: str = ""
    detail: str = ""
    considered: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.need


def _from_probe(p: Probe, source: str, considered: list[str]) -> Resolution:
    return Resolution(
        python=p.python,
        source=source,
        playwright=p.playwright,
        chromium=p.chromium,
        browsers_root=p.browsers_root,
        need="" if p.chromium else "chromium",
        considered=considered,
    )


def resolve() -> Resolution:
    """Resolve the worker interpreter (see the module docstring for the order)."""
    considered: list[str] = []
    if _PYTHON_PATH:
        exe = str(Path(_PYTHON_PATH).expanduser())
        p = probe(exe)
        considered.append(f"{SOURCE_CONFIG}: {exe}")
        if not p.ok:
            return Resolution(
                python=exe, source=SOURCE_CONFIG, need="interpreter", detail=p.error, considered=considered
            )
        if not p.playwright:
            return Resolution(
                python=exe,
                source=SOURCE_CONFIG,
                need="interpreter",
                detail=f"{exe} has no playwright — run `{exe} -m pip install 'playwright>=1.45'`, or clear the setting",
                considered=considered,
            )
        return _from_probe(p, SOURCE_CONFIG, considered)

    managed = managed_python()
    if managed:
        p = probe(managed)
        considered.append(f"{SOURCE_MANAGED}: {managed}" + ("" if p.ok else f" ({p.error})"))
        if p.ok and p.playwright:
            return _from_probe(p, SOURCE_MANAGED, considered)

    if not frozen():
        if not host_has_playwright():
            considered.append(f"{SOURCE_HOST}: {sys.executable} (no playwright)")
            return Resolution(need="deps", considered=considered)
        p = probe(sys.executable)
        considered.append(f"{SOURCE_HOST}: {sys.executable}" + ("" if p.ok else f" ({p.error})"))
        if p.ok and p.playwright:
            return _from_probe(p, SOURCE_HOST, considered)
        return Resolution(need="deps", detail=p.error, considered=considered)

    # Frozen desktop app: the managed runtime is the only place playwright can live.
    if not managed:
        return Resolution(need="runtime", considered=considered)
    return Resolution(python=managed, source=SOURCE_MANAGED, need="deps", considered=considered)
