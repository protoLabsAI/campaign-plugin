"""External requirements — Playwright (+ its Chromium) and ffmpeg — probed, never auto-installed.

* **playwright** is a declared ``requires_pip`` dep (optional, host-scoped): the operator
  installs it with the banner's *Install dependencies* button (or ``plugin install-deps``).
* **Chromium** for Playwright is a ~150 MB download. It is installed ONLY from the setup
  banner's *Install Chromium* button (``register_setup_step``) — never as a side effect of a
  tool call.
* **ffmpeg** is a system binary: the banner says how to get it (brew / apt / winget) and
  offers the ``ffmpeg_path`` setting for a non-PATH install.

Every probe here is cheap (imports + file checks, no subprocess) so it can run at load and
before each tool call; tools degrade to an actionable message instead of a traceback.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("protoagent.plugins.campaign")

STEP_INSTALL_CHROMIUM = "install-chromium"
GAP_PLAYWRIGHT = "playwright"
GAP_CHROMIUM = "chromium"
GAP_FFMPEG = "ffmpeg"

_FFMPEG_OVERRIDE = ""


def configure(ffmpeg_path: str = "") -> None:
    global _FFMPEG_OVERRIDE
    _FFMPEG_OVERRIDE = (ffmpeg_path or "").strip()


# ── playwright ───────────────────────────────────────────────────────────────
def playwright_installed() -> bool:
    return importlib.util.find_spec("playwright") is not None


def _browsers_root() -> Path:
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if env and env != "0":
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "ms-playwright"


def _expected_revisions() -> dict[str, str]:
    """Chromium revisions the installed playwright package expects, from its browsers.json."""
    spec = importlib.util.find_spec("playwright")
    if not spec or not spec.submodule_search_locations:
        return {}
    pkg = Path(next(iter(spec.submodule_search_locations)))
    bj = pkg / "driver" / "package" / "browsers.json"
    try:
        data = json.loads(bj.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    for b in data.get("browsers", []):
        if b.get("name") in ("chromium", "chromium-headless-shell"):
            out[b["name"]] = str(b.get("revision", ""))
    return out


def chromium_installed() -> bool:
    """True when a Chromium build the installed playwright can drive headless is on disk."""
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip() == "0":
        # Browsers live inside the package (hermetic install) — trust the launch to tell us.
        return True
    root = _browsers_root()
    revs = _expected_revisions()
    if revs:
        for name, rev in revs.items():
            folder = "chromium_headless_shell" if name == "chromium-headless-shell" else "chromium"
            if (root / f"{folder}-{rev}").is_dir():
                return True
        return False
    return any(root.glob("chromium*-*")) if root.is_dir() else False


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
def need_browser() -> str | None:
    """None when shooting/cards can run; otherwise the actionable reason."""
    if not playwright_installed():
        return (
            "Playwright isn't installed in the agent's Python. Click **Install dependencies** on the "
            "Campaign Studio setup banner (or run `python -m server plugin install-deps campaign`), "
            "then **Install Chromium**."
        )
    if not chromium_installed():
        return (
            "Playwright is installed but its Chromium isn't. Click **Install Chromium** on the Campaign "
            "Studio setup banner (a ~150 MB download), or run `python -m playwright install chromium`."
        )
    return None


def need_ffmpeg() -> str | None:
    return None if ffmpeg() else ffmpeg_hint()


def probe() -> dict[str, Any]:
    return {
        "playwright": playwright_installed(),
        "chromium": playwright_installed() and chromium_installed(),
        "ffmpeg": ffmpeg(),
        "ffprobe": ffprobe(),
        "chromium_install": dict(_INSTALL),
    }


def brief() -> str:
    p = probe()
    lines = ["Campaign Studio setup:"]
    lines.append(f"- playwright (python): {'ok' if p['playwright'] else 'MISSING'}")
    lines.append(f"- chromium for playwright: {'ok' if p['chromium'] else 'MISSING'}")
    lines.append(f"- ffmpeg: {p['ffmpeg'] or 'MISSING'}")
    lines.append(f"- ffprobe: {p['ffprobe'] or 'MISSING'}")
    for reason in (need_browser(), need_ffmpeg()):
        if reason:
            lines.append(f"\n{reason}")
    if _INSTALL.get("state") == "installing":
        lines.append("\nChromium install in progress.")
    return "\n".join(lines)


# ── setup gaps + the Install Chromium step ───────────────────────────────────
def report(registry) -> dict[str, Any]:
    """Push the current state through ``registry.report_setup_gap`` (self-clearing)."""
    fn = getattr(registry, "report_setup_gap", None)
    p = probe()
    if not callable(fn):
        return p
    import inspect

    try:
        takes_action = "action" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        takes_action = False

    def _gap(key: str, message: str | None, action: dict | None = None) -> None:
        try:
            if takes_action and action is not None and message:
                fn(key, message, label="Campaign Studio", action=action)
            else:
                fn(key, message, label="Campaign Studio")
        except Exception:  # noqa: BLE001 — a banner must never break loading
            log.exception("[campaign] reporting setup gap %s failed", key)

    if not p["playwright"]:
        _gap(
            GAP_PLAYWRIGHT,
            "Recording takes and rendering cards needs the `playwright` Python package.",
            {"kind": "install_deps", "label": "Install dependencies"},
        )
        _gap(GAP_CHROMIUM, None)
    else:
        _gap(GAP_PLAYWRIGHT, None)
        if p["chromium"]:
            _gap(GAP_CHROMIUM, None)
        elif _INSTALL.get("state") == "installing":
            _gap(GAP_CHROMIUM, "Installing Chromium for Playwright…")
        else:
            err = f" Last attempt failed: {_INSTALL['error']}" if _INSTALL.get("error") else ""
            _gap(
                GAP_CHROMIUM,
                "Playwright needs its headless Chromium (~150 MB) to record takes and render cards." + err,
                {"kind": "plugin_setup", "step": STEP_INSTALL_CHROMIUM, "label": "Install Chromium"},
            )
    if p["ffmpeg"]:
        _gap(GAP_FFMPEG, None)
    else:
        _gap(
            GAP_FFMPEG, ffmpeg_hint(), {"kind": "plugin_config", "label": "Set ffmpeg path", "fields": ["ffmpeg_path"]}
        )
    return p


_INSTALL: dict[str, Any] = {"state": "idle", "error": ""}
_INSTALL_LOCK = threading.Lock()
_LAST_THREAD: threading.Thread | None = None  # the running install, so tests can join it


def _install_cmd() -> list[str]:
    return [sys.executable, "-m", "playwright", "install", "chromium"]


def install_chromium(refresh=None, *, runner=None) -> dict[str, Any]:
    """The Install Chromium button. Starts a background install and returns ``pending``.

    Idempotent: a second click while one runs joins it. ``runner`` is injectable for tests.
    """
    if not playwright_installed():
        return {"ok": False, "message": "Install the playwright package first (Install dependencies)."}
    if chromium_installed():
        if callable(refresh):
            refresh()
        return {"ok": True, "message": "Chromium for Playwright is already installed."}
    with _INSTALL_LOCK:
        if _INSTALL.get("state") == "installing":
            return {"ok": True, "pending": True, "message": "Chromium is already being installed."}
        _INSTALL.update(state="installing", error="")

    run = runner or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=900))

    def _work() -> None:
        try:
            res = run(_install_cmd())
            if getattr(res, "returncode", 1) != 0:
                tail = (getattr(res, "stderr", "") or getattr(res, "stdout", "") or "").strip().splitlines()[-3:]
                _INSTALL.update(state="failed", error=" ".join(tail)[:400] or "the installer exited non-zero")
            else:
                _INSTALL.update(state="done", error="")
        except Exception as e:  # noqa: BLE001 — surfaced on the banner
            _INSTALL.update(state="failed", error=str(e)[:400])
        finally:
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
        "message": "Installing Chromium for Playwright (~150 MB) — the banner clears when it's done.",
    }
