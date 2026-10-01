"""External requirements — a Python with Playwright (+ its Chromium) and ffmpeg — probed,
never auto-installed.

* **A Python with playwright.** Playwright never runs in the agent's process; the worker runs
  under the interpreter ``interpreter.resolve()`` picks (``interpreter`` setting → the managed
  Python runtime → the agent's own Python when not frozen). playwright is a declared
  ``requires_pip`` dep (optional, ``scope: runtime``), so the banner's *Install dependencies*
  button puts it where that resolution looks: the managed runtime on the desktop app (which
  must be provisioned first — Settings ▸ Tools), the agent's own venv on a source install.
* **Chromium** for Playwright is a ~150 MB download, installed ONLY from the banner's *Install
  Chromium* button (``register_setup_step``) by running ``<that same python> -m playwright
  install chromium`` — never as a side effect of a tool call. It lands in Playwright's default
  machine-wide cache (``~/Library/Caches/ms-playwright``, ``~/.cache/ms-playwright``,
  ``%LOCALAPPDATA%\\ms-playwright``) unless the operator sets ``PLAYWRIGHT_BROWSERS_PATH``
  (passed through to both the installer and the worker, so they always agree).
* **ffmpeg** is a system binary: the banner says how to get it (brew / apt / winget) and
  offers the ``ffmpeg_path`` setting for a non-PATH install.

Probing an interpreter spawns it once (``pw_worker.py --probe``: stdlib only, ~50 ms) and is
cached for a few seconds; tools degrade to an actionable message instead of a traceback.
After *Install dependencies* (which doesn't reload the plugin) the banner's *Check again*
button — or any media tool call — re-probes and clears what's fixed.
"""

from __future__ import annotations

import logging
import shutil
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from . import interpreter

log = logging.getLogger("protoagent.plugins.campaign")

STEP_INSTALL_CHROMIUM = "install-chromium"
STEP_RECHECK = "check-setup"
GAP_PLAYWRIGHT = "playwright"
GAP_CHROMIUM = "chromium"
GAP_FFMPEG = "ffmpeg"

_FFMPEG_OVERRIDE = ""


def configure(ffmpeg_path: str = "", python: str = "") -> None:
    """``python`` is the ``interpreter`` setting (named so core's agent self-config fence —
    which refuses any leaf key called ``interpreter``/``executable``/``command`` — keeps an
    agent from pointing the worker at a program of its choosing)."""
    global _FFMPEG_OVERRIDE
    _FFMPEG_OVERRIDE = (ffmpeg_path or "").strip()
    interpreter.configure(python)


# ── ffmpeg ───────────────────────────────────────────────────────────────────
def ffmpeg() -> str | None:
    """Absolute path of ffmpeg, or None."""
    if _FFMPEG_OVERRIDE:
        p = Path(_FFMPEG_OVERRIDE).expanduser()
        return str(p) if p.is_file() else shutil.which(_FFMPEG_OVERRIDE)
    return shutil.which("ffmpeg")


def ffprobe() -> str | None:
    ff = ffmpeg()
    if ff:
        sibling = Path(ff).with_name("ffprobe" + (".exe" if ff.lower().endswith(".exe") else ""))
        if sibling.is_file():
            return str(sibling)
    return shutil.which("ffprobe")


def ffmpeg_hint() -> str:
    if sys.platform == "darwin":
        how = "`brew install ffmpeg`"
    elif sys.platform.startswith("win"):
        how = "`winget install Gyan.FFmpeg`"
    else:
        how = "`sudo apt install ffmpeg` (or your distro's package)"
    return f"ffmpeg isn't on PATH — install it with {how}, or point the plugin's ffmpeg_path setting at the binary."


# ── the gate tools call ──────────────────────────────────────────────────────
def resolve() -> interpreter.Resolution:
    """The worker interpreter (or what's missing). A seam: tests monkeypatch it."""
    return interpreter.resolve()


NOT_RUNNABLE = ("interpreter", "runtime", "deps")  # needs where no worker can start at all

RUNTIME_STEPS = (
    "provision the Python runtime (Settings ▸ Tools), then click **Install dependencies** on the "
    "Campaign Studio banner, then **Install Chromium**"
)


def browser_reason(res: interpreter.Resolution) -> str | None:
    """The actionable sentence for ``res.need`` — None when the browser is ready."""
    if res.need == "interpreter":
        return (
            f"The plugin's interpreter setting can't run the browser worker: {res.detail}. "
            "Fix or clear the setting (blank = the managed Python runtime / the agent's own Python)."
        )
    if res.need == "runtime":
        return (
            "Recording takes and rendering cards run Playwright in the desktop app's managed Python "
            f"runtime, which isn't provisioned yet. To fix: {RUNTIME_STEPS}."
        )
    if res.need == "deps":
        if interpreter.frozen():
            return (
                "The managed Python runtime doesn't have Playwright yet. Click **Install dependencies** on "
                "the Campaign Studio setup banner (or run `protoagent plugin install-deps campaign`), "
                "then **Install Chromium**."
            )
        return (
            "Playwright isn't installed in the agent's Python. Click **Install dependencies** on the "
            "Campaign Studio setup banner (or run `python -m server plugin install-deps campaign`), "
            "then **Install Chromium**."
        )
    if res.need == "chromium":
        return (
            f"Playwright is installed ({res.source}) but its Chromium isn't. Click **Install Chromium** on "
            f"the Campaign Studio setup banner (a ~150 MB download), or run "
            f"`{res.python} -m playwright install chromium`."
        )
    return None


def need_browser() -> str | None:
    """None when shooting/cards can run; otherwise the actionable reason (and the banner is
    re-synced, so a fix made elsewhere — Install dependencies — clears it)."""
    res = resolve()
    _sync(res)
    return browser_reason(res)


def need_ffmpeg() -> str | None:
    return None if ffmpeg() else ffmpeg_hint()


def probe(res: interpreter.Resolution | None = None) -> dict[str, Any]:
    res = res or resolve()
    return {
        "python": res.python,
        "python_source": res.source,
        "considered": list(res.considered),
        "playwright": bool(res.playwright) and res.need not in NOT_RUNNABLE,
        "playwright_version": res.playwright,
        "chromium": res.ready,
        "browsers_root": res.browsers_root,
        "need": res.need,
        "ffmpeg": ffmpeg(),
        "ffprobe": ffprobe(),
        "chromium_install": dict(_INSTALL),
    }


def brief() -> str:
    res = resolve()
    p = probe(res)
    lines = ["Campaign Studio setup:"]
    if p["python"] and p["playwright"]:
        lines.append(f"- browser worker python: {p['python']} ({p['python_source']})")
    else:
        lines.append("- browser worker python: MISSING (no Python with playwright)")
    lines.append(f"- playwright: {p['playwright_version'] if p['playwright'] else 'MISSING'}")
    lines.append(
        f"- chromium for playwright: {'ok' if p['chromium'] else 'MISSING'}"
        + (f" ({p['browsers_root']})" if p["browsers_root"] else "")
    )
    lines.append(f"- ffmpeg: {p['ffmpeg'] or 'MISSING'}")
    lines.append(f"- ffprobe: {p['ffprobe'] or 'MISSING'}")
    if p["considered"]:
        lines.append("- interpreters considered: " + "; ".join(p["considered"]))
    for reason in (browser_reason(res), need_ffmpeg()):
        if reason:
            lines.append(f"\n{reason}")
    if _INSTALL.get("state") == "installing":
        lines.append("\nChromium install in progress.")
    return "\n".join(lines)


# ── setup gaps + the setup steps ─────────────────────────────────────────────
_REGISTRY: Any = None
_LAST_SIG: tuple | None = None

RECHECK_ACTION = {"kind": "plugin_setup", "step": STEP_RECHECK, "label": "Check again"}


def _browser_gap(res: interpreter.Resolution) -> tuple[str | None, Any]:
    """(message, action) for the ``playwright`` gap — None message clears it."""
    if res.need == "runtime":
        return (
            "Takes and cards need Playwright, which runs in the desktop app's managed Python runtime — "
            "not provisioned yet. Provision the Python runtime (Settings ▸ Tools), then Install "
            "dependencies, then Install Chromium.",
            [
                {"kind": "global_settings", "target": "tools", "label": "Open Settings ▸ Tools"},
                {"kind": "install_deps", "label": "Install dependencies"},
                RECHECK_ACTION,
            ],
        )
    if res.need == "deps":
        where = "the managed Python runtime" if interpreter.frozen() else "the agent's Python"
        return (
            f"Takes and cards need the playwright package in {where}. "
            "Click Install dependencies, then Install Chromium.",
            [{"kind": "install_deps", "label": "Install dependencies"}, RECHECK_ACTION],
        )
    if res.need == "interpreter":
        return (
            f"The interpreter setting can't run the browser worker: {res.detail}"[:290],
            [
                {"kind": "plugin_config", "label": "Set browser worker Python", "fields": ["interpreter"]},
                RECHECK_ACTION,
            ],
        )
    return None, None


def _sync(res: interpreter.Resolution) -> None:
    """Re-report the banners if the picture changed since the last report."""
    if _REGISTRY is None:
        return
    sig = (res.need, res.python, _INSTALL.get("state"), bool(ffmpeg()))
    if sig != _LAST_SIG:
        report(_REGISTRY, res)


def report(registry, res: interpreter.Resolution | None = None) -> dict[str, Any]:
    """Push the current state through ``registry.report_setup_gap`` (self-clearing)."""
    global _REGISTRY, _LAST_SIG
    _REGISTRY = registry
    res = res or resolve()
    p = probe(res)
    _LAST_SIG = (res.need, res.python, _INSTALL.get("state"), bool(p["ffmpeg"]))
    fn = getattr(registry, "report_setup_gap", None)
    if not callable(fn):
        return p
    import inspect

    try:
        takes_action = "action" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        takes_action = False

    def _gap(key: str, message: str | None, action: Any = None) -> None:
        try:
            if takes_action and action is not None and message:
                fn(key, message, label="Campaign Studio", action=action)
            else:
                fn(key, message, label="Campaign Studio")
        except Exception:  # noqa: BLE001 — a banner must never break loading
            log.exception("[campaign] reporting setup gap %s failed", key)

    msg, action = _browser_gap(res)
    if msg:
        _gap(GAP_PLAYWRIGHT, msg, action)
        _gap(GAP_CHROMIUM, None)
    else:
        _gap(GAP_PLAYWRIGHT, None)
        if res.ready:
            _gap(GAP_CHROMIUM, None)
        elif _INSTALL.get("state") == "installing":
            _gap(GAP_CHROMIUM, "Installing Chromium for Playwright…")
        else:
            err = f" Last attempt failed: {_INSTALL['error']}" if _INSTALL.get("error") else ""
            _gap(
                GAP_CHROMIUM,
                f"Playwright ({res.source}) needs its headless Chromium (~150 MB) to record takes and render cards."
                + err,
                {"kind": "plugin_setup", "step": STEP_INSTALL_CHROMIUM, "label": "Install Chromium"},
            )
    if p["ffmpeg"]:
        _gap(GAP_FFMPEG, None)
    else:
        _gap(
            GAP_FFMPEG, ffmpeg_hint(), {"kind": "plugin_config", "label": "Set ffmpeg path", "fields": ["ffmpeg_path"]}
        )
    return p


def recheck(refresh: Callable | None = None) -> dict[str, Any]:
    """The Check again button: forget cached probes, re-resolve, re-report."""
    interpreter.invalidate()
    res = resolve()
    if callable(refresh):
        refresh()
    reason = browser_reason(res) or need_ffmpeg()
    if reason:
        return {"ok": True, "message": reason}
    return {"ok": True, "message": f"Ready — the browser worker runs on {res.python} ({res.source})."}


_INSTALL: dict[str, Any] = {"state": "idle", "error": ""}
_INSTALL_LOCK = threading.Lock()
_LAST_THREAD: threading.Thread | None = None  # the running install, so tests can join it
INSTALL_TIMEOUT_S = 900.0


def install_args() -> list[str]:
    return ["-m", "playwright", "install", "chromium"]


def _default_runner(python: str, args: list[str]):
    # Same scrubbed env + process-group kill as the worker; PLAYWRIGHT_BROWSERS_PATH passes
    # through, so the browser lands exactly where the worker will look for it.
    return interpreter.run_python(python, args, timeout=INSTALL_TIMEOUT_S)


def install_chromium(refresh=None, *, runner=None) -> dict[str, Any]:
    """The Install Chromium button. Starts a background install with the SAME interpreter the
    worker will use and returns ``pending``.

    Idempotent: a second click while one runs joins it. ``runner(python, args)`` is
    injectable for tests.
    """
    interpreter.invalidate()
    res = resolve()
    if res.need in NOT_RUNNABLE:
        return {"ok": False, "message": browser_reason(res)}
    if res.ready:
        if callable(refresh):
            refresh()
        return {"ok": True, "message": f"Chromium for Playwright is already installed ({res.source})."}
    with _INSTALL_LOCK:
        if _INSTALL.get("state") == "installing":
            return {"ok": True, "pending": True, "message": "Chromium is already being installed."}
        _INSTALL.update(state="installing", error="")

    run = runner or _default_runner
    python = res.python

    def _work() -> None:
        try:
            r = run(python, install_args())
            if getattr(r, "timed_out", False):
                _INSTALL.update(state="failed", error=f"the installer overran {INSTALL_TIMEOUT_S:.0f}s and was stopped")
            elif getattr(r, "returncode", 1) != 0:
                tail = (getattr(r, "stderr", "") or getattr(r, "stdout", "") or "").strip().splitlines()[-3:]
                _INSTALL.update(state="failed", error=" ".join(tail)[:400] or "the installer exited non-zero")
            else:
                _INSTALL.update(state="done", error="")
        except Exception as e:  # noqa: BLE001 — surfaced on the banner
            _INSTALL.update(state="failed", error=str(e)[:400])
        finally:
            interpreter.invalidate()
            if callable(refresh):
                try:
                    refresh()
                except Exception:  # noqa: BLE001
                    log.exception("[campaign] refreshing setup gaps failed")

    global _LAST_THREAD
    t = threading.Thread(target=_work, name="campaign-install-chromium", daemon=True)
    _LAST_THREAD = t
    t.start()
    if callable(refresh):
        try:
            refresh()
        except Exception:  # noqa: BLE001
            pass
    return {
        "ok": True,
        "pending": True,
        "message": f"Installing Chromium for Playwright (~150 MB) with {python} — the banner clears when it's done.",
    }
