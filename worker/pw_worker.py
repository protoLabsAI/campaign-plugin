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
     "fence": {"block_paths": ["<regex, case-insensitive, on every normalized form of the path>", ...]},   # REQUIRED
     # shoot: "script": {validated script, goto steps carry "_url"}, "out_dir": "...",
     #        "bearer": "<token or ''>"
     # card:  "html": "...", "out_path": "...png", "width": W, "height": H, "max_bytes": N|null,
     #        "transparent": bool (optional — PNG with alpha, for a montage's lower-third overlay)}

Report: ``{"ok": bool, "kind": ..., "result": {...}, "error": "..."}``. The bearer is scrubbed
from every string in it.

Security properties enforced HERE (the host can't reach into this process to enforce them):

* **The fence.** Every request whose path — in any decoded/normalized form (see
  ``fence_views``) — matches a ``fence.block_paths`` pattern is aborted, and so is every
  request whose URL can't be parsed or decoded (fail closed) — the plugin's own data API, so a recording browser can never open the gallery and
  approve its own work. A job with no fence is refused (fail closed).
* **Bearer scoping.** The bearer is attached per request, only to the script's ``base_url``
  origin and never to a fenced path — no context-wide header, so a CDN or analytics pixel
  never sees it. Service workers are blocked (they'd answer outside the route guards).
* **Storage + init_script stay on their origin.** ``storage`` is written only in top-level
  documents whose ``location.origin`` is the storage origin; ``init_script`` runs only in
  documents on the ``base_url`` origin. Both are installed before any page script runs.
* **Uploads are pre-checked.** An ``upload`` step carries ``_files`` the host already resolved
  and checked against the operator's ``upload_dirs`` allowlist (``shoot.check_uploads``); a step
  without them is refused here.
* **Cards load nothing.** A card page is self-contained (images are data: URIs), so every
  network request from it is aborted.
"""

from __future__ import annotations

import fnmatch
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


_MAX_DECODE_ROUNDS = 8
_CONTROL = re.compile(r"[\x00-\x20\x7f]")


def _resolve_dots(path: str) -> str:
    """RFC 3986 dot-segment removal — ``/a/x/../b`` → ``/a/b`` (``.`` dropped)."""
    out: list[str] = []
    for seg in path.split("/"):
        if seg == "..":
            if len(out) > 1:
                out.pop()
        elif seg != ".":
            out.append(seg)
    return "/".join(out)


def fence_views(url: str) -> list[str]:
    """Every form of ``url``'s path a server (or a hop in front of one) could end up routing.

    Raises ``ValueError`` when the URL can't be parsed or decoded — the caller BLOCKS then.

    * **Percent-decoding to a fixpoint.** Starlette routes on the decoded path, and the
      fleet proxy (``/agents/<slug>/<path>``) forwards the DECODED path verbatim to the member,
      which decodes it again — so ``%2563ampaign`` reaches ``campaign`` two hops later. Each
      round is a strict UTF-8 decode; a path still changing after the cap is refused.
    * per decoded form: as-is, ``\\``→``/`` with ``//`` runs collapsed, dot segments resolved
      (``x%2F..%2Fcampaign``), and with control/space characters stripped.

    Matching is case-insensitive at the caller (a case-folding hop is defence in depth)."""
    path = urlsplit(url).path  # ValueError on a malformed URL (bad IPv6 brackets, port, …)
    decoded = [path]
    for _ in range(_MAX_DECODE_ROUNDS):
        nxt = unquote(decoded[-1], errors="strict")  # UnicodeDecodeError ⊂ ValueError
        if nxt == decoded[-1]:
            break
        decoded.append(nxt)
    else:
        raise ValueError("URL path is still percent-encoded after the decode cap")
    views: list[str] = []
    for d in decoded:
        for base in (d, _CONTROL.sub("", d)):
            slashed = re.sub(r"/{2,}", "/", base.replace("\\", "/"))
            views.extend((base, slashed, _resolve_dots(slashed)))
    return list(dict.fromkeys(views))


def path_blocked(url: str, patterns: list[str]) -> bool:
    """True when any normalized form of the URL's path matches a fence pattern — FAIL CLOSED.

    The fence keeps a recording browser off the plugin's own data API (so a shot script can't
    approve its own assets). Anything that can't be parsed, decoded, or matched is BLOCKED:
    a request the fence can't reason about is never let through."""
    if not isinstance(url, str):
        return True
    try:
        views = fence_views(url)
        return any(re.search(p, v, re.IGNORECASE) for p in patterns for v in views)
    except Exception:  # noqa: BLE001 — fail closed on ANY parse/decode/match error
        return True


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
  const KEY = '__campaignCursor';
  const inFrame = window.top !== window;
  // A pointer event from a child frame arrives as a message; offset it by that iframe's box.
  // Registered by the init script, so it runs first: our messages never reach page listeners
  // (a console's plugin-view bridge, say).
  const fromChild = (cb) => window.addEventListener('message', e => {
    const d = e.data && e.data[KEY]; if (!d) return;
    e.stopImmediatePropagation();
    const fr = Array.from(document.querySelectorAll('iframe,frame')).find(f => f.contentWindow === e.source);
    if (!fr) return;
    const r = fr.getBoundingClientRect();
    cb(d.x + r.left + fr.clientLeft, d.y + r.top + fr.clientTop, d.t);
  }, true);
  if (inFrame) {
    // Inside an iframe (a plugin view): draw NO second pointer — report this frame's pointer
    // up to the parent, which offsets it and passes it on, so the top-level dot follows the
    // mouse into the frame instead of freezing at its edge.
    const post = (x, y, t) => { try { window.parent.postMessage({[KEY]: {x, y, t}}, '*'); } catch (e) {} };
    for (const t of ['mousemove', 'mousedown', 'mouseup'])
      document.addEventListener(t, e => post(e.clientX, e.clientY, t), true);
    fromChild(post);
    return;
  }
  let c = null, x = -100, y = -100;
  const set = (nx, ny, t) => {
    x = nx; y = ny;
    if (c) c.style.transform = `translate(${x}px,${y}px)` + (t === 'mousedown' ? ' scale(.7)' : '');
  };
  const install = () => {
    if (!document.body || document.getElementById('__campaign_cursor')) return;
    c = document.createElement('div');
    c.id = '__campaign_cursor';
    c.setAttribute('aria-hidden', 'true');
    c.style.cssText = 'position:fixed;left:0;top:0;width:18px;height:18px;margin:-9px 0 0 -9px;' +
      'border-radius:50%;background:rgba(255,255,255,.92);border:2px solid rgba(0,0,0,.55);' +
      'box-shadow:0 1px 6px rgba(0,0,0,.35);z-index:2147483647;pointer-events:none;' +
      'transition:transform .12s ease;transform:translate(-100px,-100px)';
    document.body.appendChild(c);
  };
  for (const t of ['mousemove', 'mousedown', 'mouseup'])
    document.addEventListener(t, e => set(e.clientX, e.clientY, t), true);
  fromChild(set);
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


# Browser storage seeded BEFORE the app boots (a context init script runs before any page
# script): written only when the document's origin is the storage origin, and only in the
# top-level document — so a same-origin iframe loading later never clobbers what the app has
# changed since. Every top-level load on that origin (a goto, a reload) starts from the seed.
STORAGE_JS = r"""
(cfg) => {
  if (location.origin !== cfg.origin || window.top !== window) return;
  const put = (area, kv) => { try { const s = window[area]; for (const k of Object.keys(kv)) s.setItem(k, kv[k]); }
    catch (e) { console.warn('[campaign] storage seed failed', area, e); } };
  put('localStorage', cfg.local || {}); put('sessionStorage', cfg.session || {});
}
"""


def storage_init_js(storage: dict[str, Any]) -> str:
    cfg = {"origin": storage["origin"], "local": storage.get("local") or {}, "session": storage.get("session") or {}}
    return f"({STORAGE_JS})({json.dumps(cfg)});"


def guarded_init_js(source: str, origin_: str) -> str:
    """The script's ``init_script``, run only in documents on ``origin_`` (every frame on it,
    like any init script). It runs inside a function: declare globals as ``window.x = …``."""
    return (
        f"(() => {{ if (location.origin !== {json.dumps(origin_)}) return;\n"
        f"try {{\n{source}\n}} catch (e) {{ console.error('[campaign] init_script failed', e); }}\n}})();"
    )


def video_size(script: dict[str, Any]) -> dict[str, int]:
    """The recording size: the viewport in CSS pixels.

    Playwright's screencast captures CSS-pixel frames whatever ``device_scale_factor`` says;
    asking for a bigger video does NOT upscale them — it pads the frame with grey (measured on
    Chromium 153 / Playwright 1.63). So the video is the viewport, and ``device_scale_factor``
    sharpens the ``screenshot`` stills only.
    """
    vp = script["viewport"]
    return {"width": int(vp["width"]) // 2 * 2, "height": int(vp["height"]) // 2 * 2}


def launch_args(script: dict[str, Any]) -> list[str]:
    """Chromium flags for a shoot.

    ``--force-device-scale-factor`` matching the context's ``device_scale_factor``: Playwright's
    DPR *emulation* sets ``window.devicePixelRatio`` but a ``ResizeObserver`` on
    ``device-pixel-content-box`` still reports 1x sizes. xterm.js's WebGL renderer (the
    protoAgent Terminal view) sizes its canvas from that box, so at dsf 2 its canvas came out
    half the size of its text layer and drew NOTHING — "connected", blank terminal — until a
    later redraw (measured: Chromium headless shell 1243 / Playwright 1.63; recordVideo and the
    iframe were not factors; GPU/swiftshader flags didn't help). Forcing the real scale factor
    makes both agree."""
    dsf = float(script.get("device_scale_factor") or 1)
    return [] if dsf == 1 else [f"--force-device-scale-factor={dsf:g}"]


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


# ── frames ─────────────────────────────────────────────────────────────────────
# A protoAgent console plugin view (the Terminal rail view, …) is an <iframe> at
# /plugins/<id>/view. A target with ``frame: {url?, selector?, frame?}`` is located inside it.

_GLOB = re.compile(r"[*?\[]")


def frame_url_matches(url: str, pattern: str) -> bool:
    """A frame ``url`` pattern: a glob on the WHOLE URL when it holds ``*?[``, else a substring."""
    if _GLOB.search(pattern):
        return fnmatch.fnmatchcase(url or "", pattern)
    return pattern in (url or "")


def describe_frame(spec: dict[str, Any] | None) -> str:
    if not spec:
        return ""
    parts = [f"{k}={spec[k]!r}" for k in ("url", "selector") if k in spec]
    inner = describe_frame(spec.get("frame"))
    return "frame(" + " ".join(parts) + ")" + (f" > {inner}" if inner else "")


def _descendants(frame) -> list:
    out, todo = [], list(frame.child_frames)
    while todo:
        f = todo.pop(0)
        out.append(f)
        todo.extend(f.child_frames)
    return out


def _frame_candidates(parent, spec: dict[str, Any]) -> list:
    """The frames under ``parent`` matching ONE level of ``spec`` — right now, no waiting."""
    if "selector" in spec:
        frames = []
        for el in parent.query_selector_all(spec["selector"]):
            try:
                f = el.content_frame()
            finally:
                el.dispose()
            if f is not None:
                frames.append(f)
    else:  # url only: any frame below the parent (plugin views may sit a layout iframe deep)
        frames = _descendants(parent)
    if "url" in spec:
        frames = [f for f in frames if frame_url_matches(f.url, spec["url"])]
    seen: list = []
    for f in frames:
        if not f.is_detached() and not any(f is g for g in seen):
            seen.append(f)
    return seen


class FrameAmbiguous(ValueError):
    pass


def _find_frame(parent, spec: dict[str, Any]):
    found = _frame_candidates(parent, spec)
    if len(found) > 1:
        raise FrameAmbiguous(
            f"{describe_frame(spec)} matches {len(found)} frames ({', '.join(f.url for f in found)}) — "
            "narrow it with a longer url or an iframe selector"
        )
    if not found:
        return None
    return _find_frame(found[0], spec["frame"]) if spec.get("frame") else found[0]


def resolve_frame(page, spec: dict[str, Any], timeout: float):
    """The Frame ``spec`` names, waiting for it (an iframe attaches and navigates after its
    parent loads) for at most ``timeout`` ms. A miss names every frame the page DOES have."""
    deadline = time.monotonic() + timeout / 1000
    last = ""
    while True:
        try:
            f = _find_frame(page.main_frame, spec)
            if f is not None:
                return f
        except FrameAmbiguous:
            raise
        except Exception as e:  # noqa: BLE001 — mid-navigation DOM; retry until the deadline
            last = first_line(e)
        left = (deadline - time.monotonic()) * 1000
        if left <= 0:
            urls = [f.url for f in page.frames if f is not page.main_frame]
            raise TimeoutError(
                f"Timeout {int(timeout)}ms exceeded waiting for {describe_frame(spec)}; "
                f"frames on the page: {urls or 'none'}" + (f" (last error: {last})" if last else "")
            )
        page.wait_for_timeout(min(100.0, max(1.0, left)))


def _in_frame(page, spec: dict[str, Any] | None, timeout: float):
    """(root to locate in, ms left for the step) — the page itself when there's no frame."""
    if not spec:
        return page, timeout
    started = time.monotonic()
    root = resolve_frame(page, spec, timeout)
    return root, max(1.0, timeout - (time.monotonic() - started) * 1000)


def _each_child_frame(page, fn) -> None:
    """Apply ``fn`` to every frame below the main one. A frame mid-navigation (or detached)
    may refuse — the context init script covers it once it loads."""
    for f in list(page.frames):
        if f is page.main_frame:
            continue
        try:
            fn(f)
        except Exception:  # noqa: BLE001 — see above
            pass


# ── the shoot ──────────────────────────────────────────────────────────────────


def locate(page, target: dict[str, Any], *, pick: bool = True):
    """A Playwright locator for a normalized target. Strict: >1 match is an error unless nth.

    ``page`` is the page or (for a ``frame:`` target) the Frame :func:`resolve_frame` found.
    ``pick=False`` ignores ``nth`` — every match (the ambiguity report counts them)."""
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
    if pick and "nth" in target:
        nth = target["nth"]
        loc = loc.nth(0 if nth == "first" else -1 if nth == "last" else int(nth))
    return loc


# An ACTION needs exactly one element; these ops act on their target (a wait_for doesn't).
ACTION_OPS = ("click", "hover", "fill", "type", "press", "scroll", "screenshot", "upload")
MAX_LISTED_MATCHES = 3
_MATCH_JS = """e => {
  const t = (e.innerText || e.textContent || '').replace(/\\s+/g, ' ').trim();
  const r = e.getBoundingClientRect();
  return {tag: e.tagName.toLowerCase(), role: e.getAttribute('role') || '', text: t.slice(0, 60),
          visible: r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'};
}"""


def target_label(t: dict[str, Any]) -> str:
    """``text='3 passed'`` — a target as the script wrote it (frame left out; the step says it).
    Mirrors ``shotscript.describe_target``: the worker runs out of process and imports nothing
    from the plugin, so the few lines are repeated on purpose."""
    for k in ("selector", "role", "text", "label", "placeholder", "test_id"):
        if k in t:
            s = f"{k}={t[k]!r}"
            if k == "role" and t.get("name"):
                s += f" name={t['name']!r}"
            return s + (" exact" if t.get("exact") else "")
    return str(t)


class AmbiguousTarget(ValueError):
    """An action's target matched more than one element."""


def is_strict_violation(e: BaseException) -> bool:
    return "strict mode violation" in str(e)


def describe_matches(root, target: dict[str, Any], limit: int = MAX_LISTED_MATCHES) -> tuple[int, list[str]]:
    """(how many elements the target matches ignoring nth, a short line for each of the first
    ``limit``) — e.g. ``<strong> "3 passed"``. Best-effort: a match that can't be read is
    listed as ``?``."""
    loc = locate(root, target, pick=False)
    n = loc.count()
    out = []
    for i in range(min(n, limit)):
        try:
            m = loc.nth(i).evaluate(_MATCH_JS, timeout=1000)
            role = f" role={m['role']}" if m.get("role") else ""
            hidden = "" if m.get("visible", True) else " (hidden)"
            out.append(f"<{m.get('tag', '?')}{role}> {m.get('text', '')!r}{hidden}")
        except Exception:  # noqa: BLE001 — detached mid-read; the count still tells the story
            out.append("?")
    return n, out


def ambiguity_message(root, target: dict[str, Any]) -> str:
    try:
        n, lines = describe_matches(root, target)
    except Exception:  # noqa: BLE001 — never mask the real failure with a reporting one
        n, lines = 0, []
    listed = "; ".join(f"{i + 1}) {ln}" for i, ln in enumerate(lines))
    more = f" (+{n - len(lines)} more)" if n > len(lines) else ""
    found = f"matches {n} elements: {listed}{more}" if n > 1 else "matches more than one element"
    return (
        f"{target_label(target)} {found} — an action needs exactly one: pick it with "
        "`nth` (0-based, or first/last), tighten it with `exact: true`, or use a narrower "
        "role/name or selector"
    )


def wait_any(root, target: dict[str, Any], state: str, timeout: float) -> None:
    """Wait for ``target`` to reach ``state`` — satisfied when ANY match does (visible /
    attached), or when NONE is left (hidden: no visible match; detached: no match). Playwright's
    strict mode would refuse a target that matches twice; a wait has no reason to.

    ``hidden`` really means NONE visible: a locator re-resolves on every poll, so when the
    first visible match hides, the next one becomes ``visible=true >> nth=0`` and the wait
    goes on (pinned by a real-Chromium test)."""
    loc = locate(root, target)
    if "nth" in target:  # the script picked one — wait on exactly that one
        loc.wait_for(state=state, timeout=timeout)
    elif state in ("visible", "hidden"):
        loc.locator("visible=true").nth(0).wait_for(state=state, timeout=timeout)
    else:  # attached / detached
        loc.nth(0).wait_for(state=state, timeout=timeout)


def check_targets(page, script: dict[str, Any], timeout_ms: float = 1000) -> list[dict[str, Any]]:
    """A cheap preflight: how many elements each step's target matches on the page AS IT IS
    NOW (a script's later targets usually appear only after earlier steps run — read the
    report against the page state you loaded). One entry per step with a target:

    ``{index, op, desc, count, status, matches}`` — status is ``ok`` (one match, or a picked
    ``nth`` in range), ``missing`` (none yet), ``ambiguous`` (an ACTION matching several: it
    will fail), ``ambiguous-wait`` (a wait matching several: fine, it waits for any), or
    ``frame-missing`` / ``error``."""
    out: list[dict[str, Any]] = []
    for step in script.get("steps", []):
        target = step.get("target")
        if not target:
            continue
        entry: dict[str, Any] = {"index": step.get("index"), "op": step["op"], "desc": describe(step)}
        try:
            root, _ = _in_frame(page, target.get("frame") or step.get("frame"), timeout_ms)
        except Exception as e:  # noqa: BLE001
            out.append({**entry, "count": 0, "status": "frame-missing", "matches": [], "error": first_line(e)})
            continue
        try:
            n, lines = describe_matches(root, target)
        except Exception as e:  # noqa: BLE001
            out.append({**entry, "count": 0, "status": "error", "matches": [], "error": first_line(e)})
            continue
        if n == 0:
            status = "missing"
        elif n == 1 or "nth" in target:
            nth = target.get("nth")
            status = "missing" if isinstance(nth, int) and nth >= n else "ok"
        else:
            status = "ambiguous" if step["op"] in ACTION_OPS else "ambiguous-wait"
        out.append({**entry, "count": n, "status": status, "matches": lines})
    return out


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


_IS_FILE_INPUT_JS = "e => e.tagName === 'INPUT' && (e.type || '').toLowerCase() === 'file'"


def upload(page, root, target: dict[str, Any], files: list[str], timeout: float) -> None:
    """Hand ``files`` to a file input. An ``<input type=file>`` target (hidden ones included) gets
    them directly; any other target (a button, a drop zone) is CLICKED and must open a file
    chooser, which gets them instead. The host checked every path against its upload fence and
    resolved symlinks; this re-checks only that each is still a regular file."""
    if not files:
        raise ValueError("upload step carries no checked files (the host's upload fence didn't run)")
    import stat

    for f in files:
        # The host resolved symlinks and checked the link count; re-check WITHOUT following a
        # link, so a file swapped for a symlink (or a hardlink) since then isn't uploaded.
        try:
            st = os.lstat(f)
        except OSError as e:
            raise FileNotFoundError(f"{f} is gone since the host checked it") from e
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            raise ValueError(f"{f} changed since the host checked it (not a plain, unlinked regular file) — refusing")
    started = time.monotonic()
    loc = locate(root, target)
    loc.wait_for(state="attached", timeout=timeout)
    if loc.evaluate(_IS_FILE_INPUT_JS, timeout=timeout):
        loc.set_input_files(files, timeout=timeout)
        return
    left = max(1.0, timeout - (time.monotonic() - started) * 1000)
    clicked = False
    try:
        with page.expect_file_chooser(timeout=left) as chooser:
            _move_to(page, loc, left)
            loc.click(timeout=left)
            clicked = True
    except Exception as e:
        if clicked:  # the click landed; the wait for a chooser is what failed
            raise RuntimeError(
                f"{target_label(target)} is not an <input type=file> and clicking it opened no file chooser — "
                "target the file input itself (hidden is fine) or the control that opens the chooser"
            ) from e
        raise
    fc = chooser.value
    if len(files) > 1 and not fc.is_multiple():
        raise ValueError(f"the file chooser takes ONE file, the step gives {len(files)}")
    fc.set_files(files, timeout=left)


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
    failed_step = 0
    failure_png = ""
    video_path = ""
    t0 = time.monotonic()

    def remaining_ms() -> float:
        return max(0.0, (deadline - time.monotonic()) * 1000)

    factory = playwright_factory or _default_factory
    with factory() as pw:
        browser = pw.chromium.launch(headless=True, args=launch_args(script))
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
                # Storage first, then the script's own init_script: both before any page script.
                if script.get("storage"):
                    context.add_init_script(storage_init_js(script["storage"]))
                if script.get("init_script"):
                    home = script.get("_base_origin") or ""
                    if not home:  # validate() requires a base_url; never run it unscoped
                        raise ValueError("init_script has no base_url origin to run on — refusing")
                    context.add_init_script(guarded_init_js(script["init_script"], home))
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
                            failed_step = step["index"]
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
                            failed_step = step["index"]
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
                    # A failed take keeps its recording: read the video's path, then close the
                    # page so Playwright flushes the file — whatever the step loop did.
                    try:
                        if page.video:
                            video_path = str(page.video.path())
                    except Exception:  # noqa: BLE001
                        video_path = ""
                    try:
                        page.close()
                    except Exception:  # noqa: BLE001 — context.close() below still finalizes
                        pass
            finally:
                try:
                    context.close()  # finalizes the video file
                except Exception:  # noqa: BLE001 — keep whatever was flushed; the browser still closes
                    pass
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
        "failed_step": failed_step,
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
        "failed_step": failed_step,
        "failure_png": failure_png,
    }
    return result, error


def _do(page, context, step, timeout, out_dir: Path, marks, stills, t0) -> None:
    target = step.get("target")
    # A frame target: find the frame first (within the step's timeout), then the element in it.
    root, timeout = _in_frame(page, (target or {}).get("frame") or step.get("frame"), timeout)
    try:
        _act(page, root, context, step, timeout, out_dir, marks, stills, t0)
    except Exception as e:
        # Playwright's strict mode refused an ambiguous target: say WHICH elements matched and
        # how to pick one, instead of a bare "resolved to 2 elements".
        if target and step["op"] in ACTION_OPS and "nth" not in target and is_strict_violation(e):
            raise AmbiguousTarget(ambiguity_message(root, target)) from e
        raise


def _act(page, root, context, step, timeout, out_dir: Path, marks, stills, t0) -> None:
    op = step["op"]
    target = step.get("target")
    if op == "goto":
        page.goto(step["_url"], wait_until=step.get("wait_until", "load"), timeout=timeout)
    elif op in ("click", "hover"):
        loc = locate(root, target)
        _move_to(page, loc, timeout)
        if op == "click":
            loc.click(timeout=timeout)
        else:
            loc.hover(timeout=timeout)
    elif op == "fill":
        loc = locate(root, target)
        _move_to(page, loc, timeout)
        loc.fill(step["value"], timeout=timeout)
    elif op == "type":
        if target:
            loc = locate(root, target)
            _move_to(page, loc, timeout)
            loc.click(timeout=timeout)
            _park_below(page, loc, timeout)
        page.keyboard.type(step["text"], delay=step["delay_ms"])
    elif op == "press":
        if target:
            locate(root, target).press(step["key"], timeout=timeout)
        else:
            page.keyboard.press(step["key"])
    elif op == "wait_for":
        if step.get("network_idle"):
            page.wait_for_load_state("networkidle", timeout=timeout)
        elif "ms" in step:
            page.wait_for_timeout(min(step["ms"], timeout))
        elif target:
            wait_any(root, target, step.get("state", "visible"), timeout)
        else:  # `wait_for: {frame: …}` — the frame is there; let its document parse
            root.wait_for_load_state("domcontentloaded", timeout=timeout)
    elif op == "hold":
        page.wait_for_timeout(min(step["ms"], timeout))
    elif op == "scroll":
        if target:
            locate(root, target).scroll_into_view_if_needed(timeout=timeout)
        elif root is not page:  # scroll INSIDE the frame — the wheel goes wherever the mouse is
            root.evaluate(
                "([x, y, s]) => window.scrollBy({left: x, top: y, behavior: s ? 'smooth' : 'instant'})",
                [step.get("x", 0), step.get("y", 0), step.get("smooth", True)],
            )
            page.wait_for_timeout(700 if step.get("smooth", True) else 50)
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
            if target:
                locate(root, target).screenshot(path=str(path), timeout=timeout)
            elif root is not page:  # the whole iframe, as it sits in the page
                root.frame_element().screenshot(path=str(path), timeout=timeout)
            else:
                page.screenshot(path=str(path), full_page=bool(step.get("full_page")), timeout=timeout)
        finally:
            page.evaluate(_CURSOR_VIS, "visible")
        stills[step["name"]] = str(path)
    elif op == "mask":
        css = mask_css(step)
        page.add_style_tag(content=css)
        # Frames already on the page get it now; frames (and navigations) still to come get it
        # from the init script, which Playwright runs in EVERY frame.
        _each_child_frame(page, lambda f: f.add_style_tag(content=css))
        context.add_init_script(mask_init_js(css))
    elif op == "upload":
        upload(page, root, target, step.get("_files") or [], timeout)
    elif op == "redact":
        rules = redact_rules(step)
        page.evaluate(f"({REDACT_JS})", {"rules": rules})
        _each_child_frame(page, lambda f: f.evaluate(f"({REDACT_JS})", {"rules": rules}))
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
    transparent = bool(job.get("transparent"))
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
            page.screenshot(path=str(out_path), type="png", omit_background=transparent)
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
