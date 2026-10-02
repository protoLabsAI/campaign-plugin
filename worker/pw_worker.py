"""The Playwright worker — every browser this plugin drives runs HERE, in its own process.

Why a separate process: the plugin's host may be the frozen protoAgent desktop app, which can
only add pure-Python wheels to itself — and Playwright ships platform wheels plus a Node
driver. So the host never imports playwright. It hands this script a JSON job on **stdin**,
run by an interpreter that HAS playwright (the managed Python runtime on desktop, the agent's
own venv on a source install — see ``interpreter.py``), and reads back a JSON report.

This file is deliberately self-contained: **stdlib only at module level**, no imports from the
plugin, so any interpreter with playwright can run it by path. The host also imports it (as
``campaign.worker.pw_worker``) for the pure helpers below — the URL fence and the JS it
injects — so the two sides can never disagree about them.

Usage (by the host, never by hand)::

    python -E pw_worker.py < job.json          # run a job; writes job["report_path"]
    python -E pw_worker.py --probe             # print what this interpreter can do (JSON)

Job (``v: 1``) — secrets travel ONLY in this stdin payload, never argv or a file::

    {"v": 1, "kind": "shoot" | "card", "report_path": "...",
     "fence": {"block_paths": ["<regex on the decoded URL path>", ...]},   # REQUIRED
     # shoot: "script": {validated script, goto steps carry "_url"}, "out_dir": "...",
     #        "bearer": "<token or ''>"
     # card:  "html": "...", "out_path": "...png", "width": W, "height": H, "max_bytes": N|null}

Report: ``{"ok": bool, "kind": ..., "result": {...}, "error": "..."}``. The bearer is scrubbed
from every string in it.

Security properties enforced HERE (the host can't reach into this process to enforce them):

* **The fence.** Every request whose decoded path matches a ``fence.block_paths`` pattern is
  aborted — the plugin's own data API, so a recording browser can never open the gallery and
  approve its own work. A job with no fence is refused (fail closed).
* **Bearer scoping.** The bearer is attached per request, only to the script's ``base_url``
  origin and never to a fenced path — no context-wide header, so a CDN or analytics pixel
  never sees it. Service workers are blocked (they'd answer outside the route guards).
* **Cards load nothing.** A card page is self-contained (images are data: URIs), so every
  network request from it is aborted.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

JOB_VERSION = 1
MAX_JOB_BYTES = 64 * 1024 * 1024  # a card's inlined screenshot can be ~16 MB of base64

# ── the fence + bearer scoping (pure; the host imports these) ─────────────────


def origin(url: str) -> str:
    """``scheme://host:port`` (default ports made explicit) — '' for a non-http(s) URL."""
    try:
        u = urlsplit(url)
        port = u.port or {"http": 80, "https": 443}.get(u.scheme.lower())
    except ValueError:
        return ""
    if u.scheme.lower() not in ("http", "https") or not u.hostname:
        return ""
    return f"{u.scheme.lower()}://{u.hostname.lower()}:{port}"


def path_blocked(url: str, patterns: list[str]) -> bool:
    """True when the URL's DECODED path (``//`` runs collapsed) matches any fence pattern.

    Starlette routes on the decoded path, so that is what is matched — ``%63ampaign`` and
    ``//api//plugins`` must not slip past a pattern written for ``/api/plugins/campaign``."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return False
    clean = re.sub(r"/{2,}", "/", unquote(path))
    return any(re.search(p, clean) for p in patterns)


def _fence_patterns(job: dict[str, Any]) -> list[str]:
    fence = job.get("fence")
    pats = fence.get("block_paths") if isinstance(fence, dict) else None
    if not isinstance(pats, list) or not pats or not all(isinstance(p, str) and p for p in pats):
        raise ValueError("job has no fence (fence.block_paths) — refusing to drive a browser unfenced")
    for p in pats:
        re.compile(p)
    return list(pats)


def guard_context(context, *, base_url: str, bearer: str, fence: list[str]) -> None:
    """Install the request guards on a browser context: abort fenced paths; scope the bearer."""
    context.route(lambda url: path_blocked(url, fence), lambda route: route.abort("blockedbyclient"))
    if bearer:
        home = origin(base_url)

        def _same_origin(url: str) -> bool:
            return bool(home) and origin(url) == home and not path_blocked(url, fence)

        def _with_bearer(route) -> None:
            headers = {**route.request.headers, "authorization": f"Bearer {bearer}"}
            route.continue_(headers=headers)

        context.route(_same_origin, _with_bearer)


# ── injected page scripts (pure; the host imports these for tests) ────────────

# A visible pointer: headless recordings have no cursor, which makes a click read as a
# page that changed by itself. The dot follows real mouse events the runner dispatches.
CURSOR_JS = r"""
(() => {
  if (window.__campaignCursor) return; window.__campaignCursor = true;
  const install = () => {
    if (!document.body || document.getElementById('__campaign_cursor')) return;
    const c = document.createElement('div');
    c.id = '__campaign_cursor';
    c.setAttribute('aria-hidden', 'true');
    c.style.cssText = 'position:fixed;left:0;top:0;width:18px;height:18px;margin:-9px 0 0 -9px;' +
      'border-radius:50%;background:rgba(255,255,255,.92);border:2px solid rgba(0,0,0,.55);' +
      'box-shadow:0 1px 6px rgba(0,0,0,.35);z-index:2147483647;pointer-events:none;' +
      'transition:transform .12s ease;transform:translate(-100px,-100px)';
    document.body.appendChild(c);
    let x = -100, y = -100;
    document.addEventListener('mousemove', e => { x = e.clientX; y = e.clientY;
      c.style.transform = `translate(${x}px,${y}px)`; }, true);
    document.addEventListener('mousedown', () => { c.style.transform = `translate(${x}px,${y}px) scale(.7)`; }, true);
    document.addEventListener('mouseup', () => { c.style.transform = `translate(${x}px,${y}px)`; }, true);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install); else install();
})();
"""

_CURSOR_VIS = "(v) => { const c = document.getElementById('__campaign_cursor'); if (c) c.style.visibility = v; }"

# Text redaction: rewrites matching text in text nodes and input values, and keeps doing so
# as the page changes (MutationObserver), so a path that renders late is still masked.
REDACT_JS = r"""
(cfg) => {
  const rules = cfg.rules.map(r => [new RegExp(r[0], 'g'), r[1]]);
  const fix = (s) => { let o = s; for (const [re, rep] of rules) o = o.replace(re, rep); return o; };
  const walk = (root) => {
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n; while ((n = w.nextNode())) { const v = fix(n.nodeValue); if (v !== n.nodeValue) n.nodeValue = v; }
    (root.querySelectorAll ? root.querySelectorAll('input,textarea') : []).forEach(el => {
      const v = fix(el.value || ''); if (v !== el.value) el.value = v; });
  };
  const start = () => {
    if (!document.body) return;
    walk(document.body);
    if (window.__campaignRedact) return; window.__campaignRedact = true;
    new MutationObserver(ms => { for (const m of ms) {
      if (m.type === 'characterData') { const v = fix(m.target.nodeValue); if (v !== m.target.nodeValue) m.target.nodeValue = v; }
      else m.addedNodes.forEach(n => n.nodeType === 3 ? (n.nodeValue = fix(n.nodeValue)) : (n.nodeType === 1 && walk(n)));
    } }).observe(document.body, { subtree: true, childList: true, characterData: true });
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
}
"""

REDACT_PRESET_RULES: dict[str, list[tuple[str, str]]] = {
    "home_paths": [
        (r"(?:/Users|/home)/[^/\s\"'<>:]+", "~"),
        (r"[A-Za-z]:\\\\Users\\\\[^\\\\\s\"'<>]+", "~"),
    ],
    "emails": [(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "you@example.com")],
    "secrets": [
        (
            r"(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}"
            r"|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})",
            "•••",
        )
    ],
}


def mask_css(mask: dict[str, Any]) -> str:
    rule = {
        "blur": "filter: blur(7px) !important;",
        "hide": "visibility: hidden !important;",
        "remove": "display: none !important;",
    }[mask.get("mode", "blur")]
    return "\n".join(f"{sel} {{ {rule} }}" for sel in mask["selectors"])


def mask_init_js(css: str) -> str:
    return (
        "(() => { const add = () => { const s = document.createElement('style');"
        " s.setAttribute('data-campaign-mask', '1'); s.textContent = " + json.dumps(css) + ";"
        " (document.head || document.documentElement).appendChild(s); };"
        " if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', add); else add(); })();"
    )


def redact_rules(redact: dict[str, Any]) -> list[list[str]]:
    rules: list[list[str]] = []
    for p in redact.get("presets", []):
        rules += [list(r) for r in REDACT_PRESET_RULES.get(p, [])]
    rep = redact.get("replacement", "•••")
    rules += [[pat, rep] for pat in redact.get("patterns", [])]
    return rules


def redact_init_js(rules: list[list[str]]) -> str:
    return f"({REDACT_JS})({json.dumps({'rules': rules})});"


def video_size(script: dict[str, Any]) -> dict[str, int]:
    """The recording size: the viewport in CSS pixels.

    Playwright's screencast captures CSS-pixel frames whatever ``device_scale_factor`` says;
    asking for a bigger video does NOT upscale them — it pads the frame with grey (measured on
    Chromium 153 / Playwright 1.63). So the video is the viewport, and ``device_scale_factor``
    sharpens the ``screenshot`` stills only.
    """
    vp = script["viewport"]
    return {"width": int(vp["width"]) // 2 * 2, "height": int(vp["height"]) // 2 * 2}


def step_timeout_for(step: dict[str, Any], step_timeout_ms: float, remaining_ms: float) -> float:
    """The ms one step may wait: its own ``timeout_ms`` (validated ≤ the per-step max by the
    host), else the script-wide ``step_timeout_ms`` — always bounded by what is left of the
    shoot's ``total_timeout_s``. Fixed waits (``hold``, ``wait_for: {ms}``) are bounded by the
    total budget alone: their length IS the step."""
    if step.get("op") == "hold" or (step.get("op") == "wait_for" and "ms" in step):
        return max(1.0, remaining_ms)
    own = step.get("timeout_ms")
    limit = float(own) if own else float(step_timeout_ms)
    # Never 0: to Playwright a 0 timeout means "wait forever".
    return max(1.0, min(limit, remaining_ms))


def describe(step: dict[str, Any]) -> str:
    """The host's step description, carried in the job (``_desc``) so the worker needs no
    plugin import; a bare op name is the fallback."""
    return str(step.get("_desc") or step.get("op") or "?")


# ── the shoot ──────────────────────────────────────────────────────────────────


def locate(page, target: dict[str, Any]):
    """A Playwright locator for a normalized target. Strict: >1 match is an error unless nth."""
    exact = target.get("exact")
    if "selector" in target:
        loc = page.locator(target["selector"])
    elif "role" in target:
        kw: dict[str, Any] = {}
        if target.get("name"):
            kw["name"] = target["name"]
        if exact is not None:
            kw["exact"] = exact
        loc = page.get_by_role(target["role"], **kw)
    elif "text" in target:
        loc = page.get_by_text(target["text"], exact=bool(exact))
    elif "label" in target:
        loc = page.get_by_label(target["label"], exact=bool(exact))
    elif "placeholder" in target:
        loc = page.get_by_placeholder(target["placeholder"], exact=bool(exact))
    elif "test_id" in target:
        loc = page.get_by_test_id(target["test_id"])
    else:  # the host's validate() makes this unreachable
        raise ValueError(f"unusable target {target!r}")
    if "nth" in target:
        loc = loc.nth(int(target["nth"]))
    return loc


def _move_to(page, loc, timeout: float) -> None:
    """Glide the (visible) pointer to the element's centre before acting on it."""
    loc.scroll_into_view_if_needed(timeout=timeout)
    box = loc.bounding_box(timeout=timeout)
    if box:
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=18)


def _park_below(page, loc, timeout: float) -> None:
    """Move the pointer just under a field before typing, so it never sits on the text."""
    try:
        box = loc.bounding_box(timeout=timeout)
    except Exception:  # noqa: BLE001 — cosmetic
        return
    if box:
        page.mouse.move(box["x"] + box["width"] * 0.85, box["y"] + box["height"] + 16, steps=8)


def _default_factory():
    from playwright.sync_api import sync_playwright  # the ONLY playwright import in the plugin

    return sync_playwright()


def first_line(e: BaseException) -> str:
    text = str(e).strip() or type(e).__name__
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # Playwright errors carry a call log after the headline; keep the headline + first log line.
    return " | ".join(lines[:3])[:600]


def run_shoot(job: dict[str, Any], playwright_factory: Callable | None = None) -> tuple[dict[str, Any], str]:
    """Record one take. Returns ``(result, error)`` — error '' on success."""
    fence = _fence_patterns(job)
    script = job["script"]
    bearer = str(job.get("bearer") or "")
    out_dir = Path(job["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    video_tmp = out_dir / ".video"
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    deadline = time.monotonic() + float(script["total_timeout_s"])
    step_timeout = float(script["step_timeout_ms"])
    log: list[dict[str, Any]] = []
    marks: dict[str, float] = {}
    stills: dict[str, str] = {}
    error = ""
    failure_png = ""
    video_path = ""
    t0 = time.monotonic()

    def remaining_ms() -> float:
        return max(0.0, (deadline - time.monotonic()) * 1000)

    factory = playwright_factory or _default_factory
    with factory() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx_kw: dict[str, Any] = {
                "viewport": dict(script["viewport"]),
                "device_scale_factor": float(script["device_scale_factor"]),
                "color_scheme": script["color_scheme"],
                "timezone_id": script["timezone_id"],
                "locale": script["locale"],
                "record_video_dir": str(video_tmp),
                "record_video_size": video_size(script),
                # Service workers would answer requests outside the route guards below.
                "service_workers": "block",
            }
            if script["auth"].get("storage_state"):
                ctx_kw["storage_state"] = str(Path(script["auth"]["storage_state"]).expanduser())
            context = browser.new_context(**ctx_kw)
            try:
                guard_context(context, base_url=script.get("base_url") or "", bearer=bearer, fence=fence)
                context.set_default_timeout(step_timeout)
                if script.get("cursor", True):
                    context.add_init_script(CURSOR_JS)
                if script.get("mask"):
                    context.add_init_script(mask_init_js(mask_css(script["mask"])))
                if script.get("redact"):
                    context.add_init_script(redact_init_js(redact_rules(script["redact"])))
                page = context.new_page()
                t0 = time.monotonic()
                try:
                    for step in script["steps"]:
                        if remaining_ms() <= 0:
                            error = (
                                f"step {step['index']} ({describe(step)}) not started: "
                                f"total_timeout_s={script['total_timeout_s']} spent"
                            )
                            break
                        timeout = step_timeout_for(step, step_timeout, remaining_ms())
                        # Any Playwright call in the step that takes no explicit timeout (an
                        # evaluate, a keyboard op's auto-wait) must honour the SAME ceiling — not
                        # the context default, which is only the script-wide step_timeout_ms.
                        page.set_default_timeout(timeout)
                        page.set_default_navigation_timeout(timeout)
                        entry = {"index": step["index"], "op": step["op"], "desc": describe(step)}
                        entry["t_start"] = round(time.monotonic() - t0, 3)
                        try:
                            _do(page, context, step, timeout, out_dir, marks, stills, t0)
                        except Exception as e:  # noqa: BLE001 — becomes the actionable error below
                            entry["t_end"] = round(time.monotonic() - t0, 3)
                            entry["error"] = first_line(e)
                            log.append(entry)
                            error = f"step {step['index']} ({entry['desc']}) failed: {entry['error']}"
                            try:
                                fp = out_dir / "failure.png"
                                page.screenshot(path=str(fp), timeout=5000)
                                failure_png = str(fp)
                            except Exception:  # noqa: BLE001 — the screenshot is a bonus
                                pass
                            break
                        entry["t_end"] = round(time.monotonic() - t0, 3)
                        log.append(entry)
                finally:
                    try:
                        if page.video:
                            video_path = str(page.video.path())
                    except Exception:  # noqa: BLE001
                        video_path = ""
            finally:
                context.close()  # finalizes the video file
        finally:
            browser.close()

    duration = round(time.monotonic() - t0, 3)
    final_video = ""
    if video_path and Path(video_path).is_file():
        final_video = str(out_dir / f"{script['name']}.webm")
        shutil.move(video_path, final_video)
    shutil.rmtree(video_tmp, ignore_errors=True)

    timing = {
        "script": script["name"],
        "started_at": started,
        "clock": "seconds since page creation = video t=0 (±0.1s)",
        "viewport": script["viewport"],
        "device_scale_factor": script["device_scale_factor"],
        "video_size": video_size(script),
        "duration_s": duration,
        "marks": marks,
        "screenshots": stills,
        "steps": log,
        "error": error,
    }
    (out_dir / "timing.json").write_text(json.dumps(timing, indent=2), encoding="utf-8")
    result = {
        "dir": str(out_dir),
        "video": final_video,
        "timing": str(out_dir / "timing.json"),
        "marks": marks,
        "screenshots": stills,
        "duration_s": duration,
        "steps": log,
        "error": error,
        "failure_png": failure_png,
    }
    return result, error


def _do(page, context, step, timeout, out_dir: Path, marks, stills, t0) -> None:
    op = step["op"]
    if op == "goto":
        page.goto(step["_url"], wait_until=step.get("wait_until", "load"), timeout=timeout)
    elif op in ("click", "hover"):
        loc = locate(page, step["target"])
        _move_to(page, loc, timeout)
        if op == "click":
            loc.click(timeout=timeout)
        else:
            loc.hover(timeout=timeout)
    elif op == "fill":
        loc = locate(page, step["target"])
        _move_to(page, loc, timeout)
        loc.fill(step["value"], timeout=timeout)
    elif op == "type":
        if step.get("target"):
            loc = locate(page, step["target"])
            _move_to(page, loc, timeout)
            loc.click(timeout=timeout)
            _park_below(page, loc, timeout)
        page.keyboard.type(step["text"], delay=step["delay_ms"])
    elif op == "press":
        if step.get("target"):
            locate(page, step["target"]).press(step["key"], timeout=timeout)
        else:
            page.keyboard.press(step["key"])
    elif op == "wait_for":
        if step.get("network_idle"):
            page.wait_for_load_state("networkidle", timeout=timeout)
        elif "ms" in step:
            page.wait_for_timeout(min(step["ms"], timeout))
        else:
            locate(page, step["target"]).wait_for(state=step.get("state", "visible"), timeout=timeout)
    elif op == "hold":
        page.wait_for_timeout(min(step["ms"], timeout))
    elif op == "scroll":
        if step.get("target"):
            locate(page, step["target"]).scroll_into_view_if_needed(timeout=timeout)
        elif step.get("smooth", True):
            page.evaluate(
                "([x, y]) => window.scrollBy({left: x, top: y, behavior: 'smooth'})",
                [step.get("x", 0), step.get("y", 0)],
            )
            page.wait_for_timeout(700)
        else:
            page.mouse.wheel(step.get("x", 0), step.get("y", 0))
    elif op == "mark":
        marks[step["name"]] = round(time.monotonic() - t0, 3)
    elif op == "screenshot":
        path = out_dir / f"{step['name']}.png"
        # Stills are reused as card images and posters — the pointer has no place in them.
        page.evaluate(_CURSOR_VIS, "hidden")
        try:
            if step.get("target"):
                locate(page, step["target"]).screenshot(path=str(path), timeout=timeout)
            else:
                page.screenshot(path=str(path), full_page=bool(step.get("full_page")), timeout=timeout)
        finally:
            page.evaluate(_CURSOR_VIS, "visible")
        stills[step["name"]] = str(path)
    elif op == "mask":
        css = mask_css(step)
        page.add_style_tag(content=css)
        context.add_init_script(mask_init_js(css))
    elif op == "redact":
        rules = redact_rules(step)
        page.evaluate(f"({REDACT_JS})", {"rules": rules})
        context.add_init_script(redact_init_js(rules))
    else:  # the host's validate() makes this unreachable
        raise ValueError(f"unknown step {op}")


# ── cards ──────────────────────────────────────────────────────────────────────

JPEG_QUALITIES = (92, 86, 80, 72, 64)


def run_card(job: dict[str, Any], playwright_factory: Callable | None = None) -> tuple[dict[str, Any], str]:
    """Render one card page to PNG (stepping to JPEG when over ``max_bytes``)."""
    fence = _fence_patterns(job)  # a card loads nothing at all, but the fence is still required
    out_path = Path(job["out_path"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    w, h = int(job["width"]), int(job["height"])
    max_bytes = int(job["max_bytes"]) if job.get("max_bytes") else None
    attempts: list[dict[str, Any]] = []
    factory = playwright_factory or _default_factory
    with factory() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1)
            # Self-contained page: abort EVERY request (data: URIs never reach the router).
            guard_context(context, base_url="", bearer="", fence=fence)
            context.route(lambda url: True, lambda route: route.abort("blockedbyclient"))
            page = context.new_page()
            page.set_content(job["html"], wait_until="load")
            try:
                page.evaluate("document.fonts && document.fonts.ready.then(() => true)")
            except Exception:  # noqa: BLE001 — fonts.ready is a nicety
                pass
            page.screenshot(path=str(out_path), type="png")
            final = out_path
            attempts.append({"format": "png", "size_bytes": out_path.stat().st_size})
            if max_bytes and out_path.stat().st_size > max_bytes:
                jpg = out_path.with_suffix(".jpg")
                for q in JPEG_QUALITIES:
                    page.screenshot(path=str(jpg), type="jpeg", quality=q)
                    attempts.append({"format": "jpeg", "quality": q, "size_bytes": jpg.stat().st_size})
                    final = jpg
                    if jpg.stat().st_size <= max_bytes:
                        break
                if final == jpg:
                    out_path.unlink(missing_ok=True)
            context.close()
        finally:
            browser.close()
    return {"path": str(final), "attempts": attempts}, ""


# ── dispatch ───────────────────────────────────────────────────────────────────

RUNNERS = {"shoot": run_shoot, "card": run_card}


def _scrub(value: Any, secret: str) -> Any:
    """Replace ``secret`` in every string of a JSON-able value."""
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "•••")
    if isinstance(value, list):
        return [_scrub(v, secret) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v, secret) for k, v in value.items()}
    return value


def run_job(job: dict[str, Any], playwright_factory: Callable | None = None) -> dict[str, Any]:
    """Run a job and return its report (never raises for a job-level failure)."""
    kind = job.get("kind")
    secret = str(job.get("bearer") or "")
    if job.get("v") != JOB_VERSION:
        return {"ok": False, "kind": kind, "result": {}, "error": f"unsupported job version {job.get('v')!r}"}
    runner = RUNNERS.get(str(kind))
    if runner is None:
        return {"ok": False, "kind": kind, "result": {}, "error": f"unknown job kind {kind!r}"}
    try:
        result, error = runner(job, playwright_factory)
    except Exception as e:  # noqa: BLE001 — a launch failure etc. is reported, not a traceback
        result, error = {}, f"{type(e).__name__}: {first_line(e)}"
    return _scrub({"ok": not error, "kind": kind, "result": result, "error": error}, secret)


def probe() -> dict[str, Any]:
    """What THIS interpreter can do — without importing playwright (no driver start)."""
    import importlib.util

    out: dict[str, Any] = {
        "python": sys.executable,
        "version": ".".join(map(str, sys.version_info[:3])),
        "playwright": None,
        "chromium": False,
        "browsers_root": "",
    }
    spec = importlib.util.find_spec("playwright")
    if spec is None or not spec.submodule_search_locations:
        return out
    try:
        from importlib.metadata import version

        out["playwright"] = version("playwright")
    except Exception:  # noqa: BLE001 — present but unversioned still counts
        out["playwright"] = "unknown"
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if env == "0":  # hermetic: browsers inside the package — let the launch be the judge
        out["browsers_root"] = "(inside the playwright package)"
        out["chromium"] = True
        return out
    if env:
        root = Path(env).expanduser()
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches" / "ms-playwright"
    elif sys.platform.startswith("win"):
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ms-playwright"
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "ms-playwright"
    out["browsers_root"] = str(root)
    pkg = Path(next(iter(spec.submodule_search_locations)))
    revs: dict[str, str] = {}
    try:
        data = json.loads((pkg / "driver" / "package" / "browsers.json").read_text(encoding="utf-8"))
        for b in data.get("browsers", []):
            if b.get("name") in ("chromium", "chromium-headless-shell"):
                revs[b["name"]] = str(b.get("revision", ""))
    except (OSError, ValueError):
        pass
    if revs:
        out["chromium"] = any(
            (root / f"{'chromium_headless_shell' if n == 'chromium-headless-shell' else 'chromium'}-{r}").is_dir()
            for n, r in revs.items()
        )
    else:
        out["chromium"] = root.is_dir() and any(root.glob("chromium*-*"))
    return out


def main(argv: list[str]) -> int:
    if argv[1:2] == ["--probe"]:
        sys.stdout.write(json.dumps(probe()))
        return 0
    raw = sys.stdin.buffer.read(MAX_JOB_BYTES + 1)
    if len(raw) > MAX_JOB_BYTES:
        sys.stderr.write("job too large\n")
        return 2
    try:
        job = json.loads(raw.decode("utf-8"))
        report_path = Path(job["report_path"])
    except (ValueError, KeyError, TypeError) as e:
        sys.stderr.write(f"bad job: {type(e).__name__}\n")
        return 2
    report = run_job(job)
    tmp = report_path.with_name(report_path.name + ".tmp")
    tmp.write_text(json.dumps(report), encoding="utf-8")
    os.replace(tmp, report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
