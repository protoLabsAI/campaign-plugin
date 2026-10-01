"""The shoot runner — a validated shot script → a recorded take.

Drives headless Chromium through **Playwright (sync API)** with ``record_video`` on, and
writes into the take directory:

* ``<name>.webm`` — the raw recording (video t=0 is page creation);
* ``<shot>.png`` — one still per ``screenshot`` step;
* ``timing.json`` — every step's start/end and every ``mark`` in seconds on the video's
  clock, which is what ``campaign_render`` trims and speed-ramps by;
* ``failure.png`` — on a failed step, what the page looked like at that moment.

Bounded: every step runs under ``step_timeout_ms`` (clamped to what's left of
``total_timeout_s``), and the run stops between steps once the total budget is spent.

Playwright's sync API refuses to run on a thread with a live asyncio loop — which is
exactly where a tool call lands — so :func:`run` always executes on a fresh worker
thread. The Playwright entry point is injectable (``playwright_factory``) so the suite can
mock the browser at this boundary.
"""

from __future__ import annotations

import json
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .shotscript import describe_step, resolve_url

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


class ShootError(RuntimeError):
    """A step failed. ``result`` carries the partial take (log, failure still, video)."""

    def __init__(self, message: str, result: dict[str, Any]):
        super().__init__(message)
        self.result = result


def mask_css(mask: dict[str, Any]) -> str:
    rule = {
        "blur": "filter: blur(7px) !important;",
        "hide": "visibility: hidden !important;",
        "remove": "display: none !important;",
    }[mask.get("mode", "blur")]
    return "\n".join(f"{sel} {{ {rule} }}" for sel in mask["selectors"])


def _mask_init_js(css: str) -> str:
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


def _redact_init_js(rules: list[list[str]]) -> str:
    return f"({REDACT_JS})({json.dumps({'rules': rules})});"


def video_size(script: dict[str, Any]) -> dict[str, int]:
    """The recording size: the viewport in CSS pixels.

    Playwright's screencast captures CSS-pixel frames whatever ``device_scale_factor`` says;
    asking for a bigger video does NOT upscale them — it pads the frame with grey (measured on
    Chromium 153 / Playwright 1.63). So the video is the viewport, and ``device_scale_factor``
    sharpens the ``screenshot`` stills only. For a crisper clip, record a larger viewport, or
    crop the region in ``campaign_render``.
    """
    vp = script["viewport"]
    # Encoders want even dimensions.
    return {"width": int(vp["width"]) // 2 * 2, "height": int(vp["height"]) // 2 * 2}


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
    else:  # validate() makes this unreachable
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
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def _first_line(e: BaseException) -> str:
    text = str(e).strip() or type(e).__name__
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # Playwright errors carry a call log after the headline; keep the headline + first log line.
    return " | ".join(lines[:3])[:600]


def _run(script: dict[str, Any], out_dir: Path, playwright_factory: Callable | None, bearer: str) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    video_tmp = out_dir / ".video"
    started = datetime.now(UTC).isoformat(timespec="seconds")
    deadline = time.monotonic() + float(script["total_timeout_s"])
    step_timeout = float(script["step_timeout_ms"])
    log: list[dict[str, Any]] = []
    marks: dict[str, float] = {}
    stills: dict[str, str] = {}
    error: str = ""
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
            }
            if bearer:
                ctx_kw["extra_http_headers"] = {"Authorization": f"Bearer {bearer}"}
            if script["auth"].get("storage_state"):
                ctx_kw["storage_state"] = str(Path(script["auth"]["storage_state"]).expanduser())
            context = browser.new_context(**ctx_kw)
            try:
                context.set_default_timeout(step_timeout)
                if script.get("cursor", True):
                    context.add_init_script(CURSOR_JS)
                if script.get("mask"):
                    context.add_init_script(_mask_init_js(mask_css(script["mask"])))
                if script.get("redact"):
                    context.add_init_script(_redact_init_js(redact_rules(script["redact"])))
                page = context.new_page()
                t0 = time.monotonic()
                try:
                    for step in script["steps"]:
                        if remaining_ms() <= 0:
                            error = (
                                f"step {step['index']} ({describe_step(step)}) not started: "
                                f"total_timeout_s={script['total_timeout_s']} spent"
                            )
                            break
                        # Holds are bounded by the total budget, not the per-step timeout.
                        timeout = remaining_ms() if step["op"] == "hold" else min(step_timeout, remaining_ms())
                        entry = {"index": step["index"], "op": step["op"], "desc": describe_step(step)}
                        entry["t_start"] = round(time.monotonic() - t0, 3)
                        try:
                            _do(page, context, step, timeout, out_dir, marks, stills, t0)
                        except Exception as e:  # noqa: BLE001 — becomes the actionable error below
                            entry["t_end"] = round(time.monotonic() - t0, 3)
                            entry["error"] = _first_line(e)
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
    if error:
        raise ShootError(error, result)
    return result


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
        context.add_init_script(_mask_init_js(css))
    elif op == "redact":
        rules = redact_rules(step)
        page.evaluate(f"({REDACT_JS})", {"rules": rules})
        context.add_init_script(_redact_init_js(rules))
    else:  # validate() makes this unreachable
        raise ValueError(f"unknown step {op}")


def run(
    script: dict[str, Any],
    out_dir: str | Path,
    *,
    playwright_factory: Callable | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Record one take of a VALIDATED script into ``out_dir`` (on a fresh thread).

    Raises :class:`ShootError` (with the partial result) when a step fails.
    """
    import os

    env = os.environ if env is None else env
    bearer = ""
    if script["auth"].get("bearer_env"):
        bearer = env.get(script["auth"]["bearer_env"], "")
        if not bearer:
            raise ShootError(
                f"auth.bearer_env names {script['auth']['bearer_env']}, which isn't set in the agent's environment",
                {"steps": [], "error": "missing bearer"},
            )
    # Resolve relative goto URLs once, so the runner never sees a bare path.
    script = dict(script)
    script["steps"] = [
        {**s, "_url": resolve_url(script, s["url"])} if s["op"] == "goto" else s for s in script["steps"]
    ]
    from .threads import Wedged, in_thread

    try:
        # The run bounds itself; this join bound is the backstop for a wedged browser.
        return in_thread(
            lambda: _run(script, Path(out_dir), playwright_factory, bearer),
            float(script["total_timeout_s"]) + 90,
            name="campaign-shoot",
        )
    except Wedged:
        raise ShootError(
            f"the browser didn't finish within total_timeout_s={script['total_timeout_s']} (+90s grace) — "
            "it may be wedged; the take is abandoned",
            {"steps": [], "error": "wedged"},
        ) from None
