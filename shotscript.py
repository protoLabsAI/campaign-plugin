"""Shot scripts — the declarative recipe for one recorded take.

A shot script is YAML (or JSON) the agent writes and the operator can read. It says how
the browser is set up (viewport, scale, colour scheme, timezone, locale, auth) and what
happens on screen, step by step. :func:`validate` turns it into a normalized dict or a
list of actionable errors naming the step — before any browser starts.

Pure stdlib + pyyaml; no Playwright here, so the whole contract is testable host-free.

Example::

    name: install-from-url
    base_url: http://localhost:7871
    viewport: {width: 1280, height: 800}
    device_scale_factor: 2
    color_scheme: dark
    timezone_id: UTC
    locale: en-US
    redact: {presets: [home_paths, emails, secrets]}
    steps:
      - goto: /app/
      - wait_for: {role: button, name: Settings}
      - mark: start
      - click: {role: button, name: Settings}
      - type: {placeholder: "https://github.com/…", text: "https://github.com/acme/x", delay_ms: 45}
      - hold: 1500
      - wait_for: {text: "Installed", timeout_ms: 60000}
      - screenshot: dialog
      - mark: end

Targets (click/focus/hover/drag/fill/type/press/wait_for/scroll/screenshot) are either a string
(a Playwright selector: CSS, ``text=…``, ``role=button[name="Save"]``) or a mapping with
ONE of ``selector`` / ``role`` (+ ``name``) / ``text`` / ``label`` / ``placeholder`` /
``test_id``, plus optional ``exact`` (whole-string, case-sensitive text/name match instead of
substring) and ``nth`` (0-based index, or ``first`` / ``last``). Prefer role/text/label — they
survive restyles; CSS classes don't.

Several matches: a WAIT (``wait_for`` on a target) is satisfied when ANY match reaches the
state — "3 passed" in a bold summary AND a code span is fine to wait on. An ACTION (click,
focus, hover, drag, fill, type, press, scroll, screenshot of a target) needs exactly ONE element: an
ambiguous target fails the step with the first few matches listed — pick one with ``nth``,
tighten it with ``exact: true``, or use a narrower role/selector — or scope it to a container
with ``within:`` (click / focus / hover / wait_for): the target is looked for inside that
container only, e.g. the "Save" button of ONE dialog::

    - click: {role: button, name: Save, within: {role: dialog, name: Settings}}

Pointer and keyboard: ``focus: <target>`` focuses an element without clicking it;
``mouse_move: {x, y}`` parks the visible pointer at viewport coordinates (``smooth: false`` to
jump) so it doesn't sit on what the viewer must read; ``drag: {target, to: {dx, dy}}`` presses on
the target's centre, moves by that offset and releases (a resizable divider, a slider);
``press: {key, repeat: N, delay_ms: 80}`` presses a key N times (``delay_ms`` between presses).

Frames: a target inside an ``<iframe>`` (a protoAgent console plugin view is one — e.g. the
Terminal rail view at ``/plugins/terminal/view``) adds ``frame:`` — the step's mapping or the
target mapping, either. Select the frame by ``url`` (a substring of the frame's URL, or a glob
on the whole URL when it holds ``*``/``?``/``[``) and/or by ``selector`` (a CSS selector for the
``<iframe>`` element in its parent); a nested ``frame:`` inside it reaches one level deeper. A
bare string is a ``url``. The frame is waited for within the step's timeout::

    - wait_for: {text: "connected", frame: {url: "/plugins/terminal/view"}}
    - type: {target: {selector: "textarea", frame: "/plugins/terminal/view"}, text: "ls"}

``wait_for: {frame: …}`` with no target waits for the frame itself; ``screenshot`` with only a
``frame`` shoots the iframe element; ``scroll`` with only a ``frame`` scrolls inside it. Masks
and redaction reach into every frame (including ones that load later). CSS can't touch text drawn
on a ``<canvas>``, so for an xterm.js terminal ``redact`` filters what its ``write()`` is given
(and blurs an xterm canvas it can't hook) — still keep secrets off a terminal in the shot itself.

Waits: every step that waits on the page (goto, click, focus, hover, drag, fill, type, press, wait_for,
scroll, screenshot) gives up after ``step_timeout_ms`` (script-wide, default 15000). A step
that waits on something slow — an agent run, a build — sets its own ``timeout_ms`` (up to
600000 = 10 min; more is a validation error, not a silent clamp), and every step is also
bounded by what is left of ``total_timeout_s`` (default 300, max 900). Don't use
``wait_for: {network_idle: true}`` on an app that holds a stream open (SSE / websockets — the
protoAgent console does): it never settles. Wait for the element you need instead.

Storage seeded before the app boots — for an app that reads persisted UI state (panel widths,
a selected tab, a dismissed tour) from ``localStorage``/``sessionStorage`` while it starts::

    storage:
      origin: http://localhost:7871          # optional — default: base_url's origin
      local: {protoagent.ui: {state: {rightWidth: 860}, version: 14}}
      session: {tab: plugins}

A string value is stored as-is; anything else is JSON-serialized. Written by a context init
script — before ANY page script — in top-level documents on that origin only (every load
there starts from the seed). ``init_script: "<js>"`` is the escape hatch for other set-up: run
before page scripts in every document on the base_url's origin only (same-origin child frames,
about:blank/srcdoc ones included), inside a function (assign
``window.x`` for a global), ≤ 64 KB. It is operator/agent-authored code, trusted like the rest
of the script, and runs only in the recording browser (which is fenced off this plugin's API).

Uploads — ``upload: {target, files: [/abs/path, …]}``: an ``<input type=file>`` target (hidden
is fine) gets the files directly; any other target (a button, a drop zone) is clicked and the
file chooser it opens gets them. Files must sit under the operator's ``upload_dirs`` setting
(empty = uploads refused), be regular files ≤ 50 MB, and never be keys/credentials or anything
in the agent's home — checked by the host at shoot time (``shoot.check_uploads``).
"""

from __future__ import annotations

import difflib
import json
import re
from typing import Any

TOP_KEYS = {
    "name",
    "description",
    "base_url",
    "viewport",
    "device_scale_factor",
    "color_scheme",
    "timezone_id",
    "locale",
    "auth",
    "cursor",
    "step_timeout_ms",
    "total_timeout_s",
    "mask",
    "redact",
    "storage",
    "init_script",
    "steps",
}
STEP_OPS = (
    "goto",
    "click",
    "focus",
    "fill",
    "type",
    "press",
    "hover",
    "mouse_move",
    "drag",
    "wait_for",
    "hold",
    "scroll",
    "mark",
    "screenshot",
    "mask",
    "redact",
    "upload",
)
TARGET_KEYS = ("selector", "role", "text", "label", "placeholder", "test_id")
TARGET_OPTS = ("name", "exact", "nth")
FRAME_KEYS = ("url", "selector", "frame")
MAX_FRAME_DEPTH = 2  # a frame, and one frame inside it
COLOR_SCHEMES = ("light", "dark", "no-preference")
MASK_MODES = ("blur", "hide", "remove")
REDACT_PRESETS = ("home_paths", "emails", "secrets")

MAX_HOLD_MS = 60_000
# The ceiling for ONE step's wait (script-wide ``step_timeout_ms`` or a step's own
# ``timeout_ms``). 10 min covers a slow real run on screen (a long agent turn, a build, an
# install) — 3 min proved too short for real agent turns. A stuck step still fails inside one
# shoot: a larger ask is a validation ERROR (never silently clamped), and every step is also
# bounded by what is left of ``total_timeout_s`` (max 900s), so a long wait needs a raised
# total_timeout_s too.
MAX_STEP_TIMEOUT_MS = 600_000
MIN_STEP_TIMEOUT_MS = 100
MAX_TOTAL_S = 900
# Browser storage seeded before the app boots, and the init_script escape hatch. Both are
# bounded so a script can't smuggle megabytes into every page load.
STORAGE_KEYS = ("origin", "local", "session")
MAX_STORAGE_ENTRIES = 200
MAX_STORAGE_BYTES = 256 * 1024
MAX_STORAGE_KEY_LEN = 512
MAX_INIT_SCRIPT_BYTES = 64 * 1024
# The upload step: how many files one step may hand to a file input (paths are checked against
# the operator's upload_dirs allowlist by the host at shoot time — see shoot.check_uploads).
MAX_UPLOAD_FILES = 20
# press: how many times one step may press its key, and the pause between presses.
MAX_PRESS_REPEAT = 200
DEFAULT_PRESS_GAP_MS = 80
# drag: how far (CSS px) one drag may move the pointer from the target's centre, per axis.
MAX_DRAG_PX = 4000
DEFAULTS = {
    "viewport": {"width": 1280, "height": 800},
    "device_scale_factor": 2,
    "color_scheme": "dark",
    "timezone_id": "UTC",
    "locale": "en-US",
    "cursor": True,
    "step_timeout_ms": 15_000,
    "total_timeout_s": 300,
}
_TGT = ("target", "selector", "role", "text", "label", "placeholder", "test_id", "name", "exact", "nth", "frame")
# Every key a step's mapping body may carry. Anything else is an ERROR naming the key — a typo'd
# or unsupported option (``timout_ms``, ``timeout``) must never be dropped on the floor.
STEP_KEYS: dict[str, tuple[str, ...]] = {
    "goto": ("url", "wait_until", "timeout_ms"),
    "click": (*_TGT, "within", "timeout_ms"),
    "focus": (*_TGT, "within", "timeout_ms"),
    "hover": (*_TGT, "within", "timeout_ms"),
    "mouse_move": ("x", "y", "smooth"),
    "drag": (*_TGT, "to", "timeout_ms"),
    "fill": (*_TGT, "value", "timeout_ms"),
    "type": (*_TGT, "delay_ms", "timeout_ms"),
    "press": (*_TGT, "key", "repeat", "delay_ms", "timeout_ms"),
    "wait_for": (*_TGT, "within", "state", "network_idle", "ms", "timeout_ms"),
    "hold": ("ms",),
    "scroll": (*_TGT, "x", "y", "smooth", "timeout_ms"),
    "mark": ("name",),
    "screenshot": (*_TGT, "full_page", "timeout_ms"),
    "mask": ("selectors", "mode"),
    "redact": ("presets", "patterns", "replacement"),
    "upload": (*_TGT, "files", "timeout_ms"),
}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ScriptError(ValueError):
    """Raised with every problem found, one per line."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


def parse(text_or_obj: Any) -> dict[str, Any]:
    """YAML/JSON text (or an already-parsed mapping) → mapping."""
    if isinstance(text_or_obj, dict):
        return text_or_obj
    text = str(text_or_obj or "").strip()
    if not text:
        raise ScriptError(["the shot script is empty"])
    try:
        data = json.loads(text) if text.startswith("{") else None
    except json.JSONDecodeError:
        data = None
    if data is None:
        import yaml

        try:
            data = yaml.safe_load(text)
        except Exception as e:  # noqa: BLE001 — parse errors are the useful output
            raise ScriptError([f"the shot script is not valid YAML/JSON: {e}"]) from e
    if not isinstance(data, dict):
        raise ScriptError(["the shot script must be a mapping with a `steps:` list"])
    return data


def _suggest(word: str, choices: tuple[str, ...] | set[str]) -> str:
    near = difflib.get_close_matches(str(word), list(choices), n=1)
    return f" (did you mean `{near[0]}`?)" if near else ""


def _target(raw: Any, where: str, problems: list[str], *, required: bool = True) -> dict[str, Any] | None:
    if raw is None or raw == "" or raw == {}:
        if required:
            problems.append(f"{where}: needs a target — a selector string or one of {', '.join(TARGET_KEYS)}")
        return None
    if isinstance(raw, str):
        return {"selector": raw}
    if not isinstance(raw, dict):
        problems.append(f"{where}: target must be a string or a mapping, got {type(raw).__name__}")
        return None
    for k in raw:
        if k not in (*TARGET_KEYS, *TARGET_OPTS, "frame"):
            problems.append(
                f"{where}: unknown target key `{k}`{_suggest(k, (*TARGET_KEYS, *TARGET_OPTS, 'frame'))}"
                f" — a target takes one of {', '.join(TARGET_KEYS)} plus {', '.join(TARGET_OPTS)}, frame"
            )
    keys = [k for k in TARGET_KEYS if raw.get(k) not in (None, "")]
    if not keys:
        if required:
            problems.append(f"{where}: needs one of {', '.join(TARGET_KEYS)} (a bare `name` needs a `role`)")
        return None
    if len(keys) > 1:
        problems.append(f"{where}: give ONE of {', '.join(keys)} — not several")
        return None
    out: dict[str, Any] = {keys[0]: str(raw[keys[0]])}
    if "name" in raw and keys[0] == "role":
        out["name"] = str(raw["name"])
    if "exact" in raw:
        out["exact"] = bool(raw["exact"])
    if "nth" in raw:
        nth = _nth(raw["nth"])
        if nth is None:
            problems.append(f"{where}: nth must be a 0-based index (0, 1, …) or `first` / `last`, got {raw['nth']!r}")
        else:
            out["nth"] = nth
    if raw.get("frame") is not None:
        f = _frame(raw["frame"], f"{where} frame", problems)
        if f is not None:
            out["frame"] = f
    return out


def _nth(raw: Any) -> int | str | None:
    """``nth`` — a 0-based index, or ``first`` / ``last``. ``None`` when it's neither."""
    if isinstance(raw, str) and raw.strip().lower() in ("first", "last"):
        return raw.strip().lower()
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw if raw >= 0 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def _frame(raw: Any, where: str, problems: list[str], depth: int = 1) -> dict[str, Any] | None:
    """``frame:`` — which ``<iframe>`` a target lives in: ``{url, selector, frame}``.

    ``url`` is a substring of the frame's URL (a glob on the whole URL when it holds ``*?[``);
    ``selector`` is a CSS selector for the ``<iframe>`` element in the parent; a nested ``frame``
    goes one level deeper. A bare string is a ``url``. Unknown keys are errors."""
    if isinstance(raw, str):
        raw = {"url": raw}
    if not isinstance(raw, dict):
        problems.append(f"{where}: must be a URL string or {{url: …, selector: …}}, got {type(raw).__name__}")
        return None
    bad = False
    for k in raw:
        if k not in FRAME_KEYS:
            problems.append(
                f"{where}: unknown frame key `{k}`{_suggest(k, FRAME_KEYS)} — a frame takes url, selector, frame"
            )
            bad = True
    out: dict[str, Any] = {}
    for k in ("url", "selector"):
        if k in raw:
            v = raw[k]
            if not isinstance(v, str) or not v.strip():
                problems.append(f"{where}.{k}: must be a non-empty string")
                bad = True
            else:
                out[k] = v.strip()
    if not out and not bad:
        problems.append(
            f"{where}: needs a `url` (substring or glob of the frame's URL) and/or a `selector` (the iframe)"
        )
        bad = True
    if raw.get("frame") is not None:
        if depth >= MAX_FRAME_DEPTH:
            problems.append(f"{where}: frames nest at most {MAX_FRAME_DEPTH} deep (a frame and one inside it)")
            bad = True
        else:
            inner = _frame(raw["frame"], f"{where}.frame", problems, depth + 1)
            if inner is None:
                bad = True
            else:
                out["frame"] = inner
    return None if bad else out


def _target_from_mapping(
    body: dict[str, Any], where: str, problems: list[str], *, required: bool, exclude: tuple[str, ...] = ()
) -> dict | None:
    sub = {k: body[k] for k in (*TARGET_KEYS, *TARGET_OPTS) if k in body and k not in exclude}
    if "target" in body:
        t = _target(body["target"], where, problems, required=required)
    else:
        t = _target(sub or None, where, problems, required=required)
    if body.get("frame") is not None and t is not None:
        if "frame" in t:
            problems.append(f"{where}: `frame` is given twice — on the step and on its target; keep one")
            return None
        f = _frame(body["frame"], f"{where} frame", problems)
        if f is not None:
            t["frame"] = f
    return t


def _step_frame(body: Any, step: dict[str, Any], where: str, problems: list[str]) -> None:
    """A ``frame`` on a step with NO target: kept on the step (wait_for / screenshot / scroll
    act on the frame itself); anywhere else it is an error, never silently dropped."""
    if not isinstance(body, dict) or body.get("frame") is None or step.get("target"):
        return
    if step["op"] in ("wait_for", "screenshot", "scroll") and not step.get("network_idle") and "ms" not in step:
        f = _frame(body["frame"], f"{where} frame", problems)
        if f is not None:
            step["frame"] = f
    else:
        problems.append(f"{where}: `frame` needs a target inside that frame (a selector, role, text, …)")


def _int(value: Any, where: str, problems: list[str], lo: int, hi: int) -> int | None:
    try:
        v = int(value)
    except (TypeError, ValueError):
        problems.append(f"{where}: must be an integer, got {value!r}")
        return None
    if not lo <= v <= hi:
        problems.append(f"{where}: {v} is outside {lo}..{hi}")
        return None
    return v


def _timeout_ms(value: Any, where: str, problems: list[str]) -> int | None:
    """A per-step (or script-wide) wait ceiling in ms. Over the max is an ERROR, never a clamp."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        problems.append(f"{where}: must be an integer number of milliseconds, got {value!r}")
        return None
    if v > MAX_STEP_TIMEOUT_MS:
        problems.append(
            f"{where}: {v}ms is over the {MAX_STEP_TIMEOUT_MS}ms ({MAX_STEP_TIMEOUT_MS // 1000}s) per-step max — "
            "wait for an intermediate sign of progress first and split the wait into several steps"
        )
        return None
    if v < MIN_STEP_TIMEOUT_MS:
        problems.append(f"{where}: {v}ms is under the {MIN_STEP_TIMEOUT_MS}ms minimum")
        return None
    return v


def _mask(raw: Any, where: str, problems: list[str]) -> dict[str, Any] | None:
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, list):
        raw = {"selectors": raw}
    if not isinstance(raw, dict) or not raw.get("selectors"):
        problems.append(
            f"{where}: mask needs a list of CSS selectors (or {{selectors: [...], mode: blur|hide|remove}})"
        )
        return None
    sels = raw["selectors"]
    if isinstance(sels, str):
        sels = [sels]
    mode = str(raw.get("mode", "blur")).lower()
    if mode not in MASK_MODES:
        problems.append(f"{where}: mask mode must be blur, hide (keeps its space) or remove (collapses it)")
        return None
    bad = [s for s in sels if not isinstance(s, str) or not s.strip() or "{" in s or "}" in s]
    if bad:
        problems.append(f"{where}: mask selectors must be plain CSS selectors (no braces): {bad}")
        return None
    return {"selectors": [s.strip() for s in sels], "mode": mode}


def _redact(raw: Any, where: str, problems: list[str]) -> dict[str, Any] | None:
    if isinstance(raw, list):
        raw = {"presets": raw}
    if not isinstance(raw, dict):
        problems.append(f"{where}: redact must be {{presets: [...], patterns: [...]}}")
        return None
    presets = raw.get("presets") or []
    patterns = raw.get("patterns") or []
    for p in presets:
        if p not in REDACT_PRESETS:
            problems.append(f"{where}: unknown redact preset {p!r}{_suggest(p, REDACT_PRESETS)}")
    for pat in patterns:
        try:
            re.compile(pat)
        except (re.error, TypeError) as e:
            problems.append(f"{where}: redact pattern {pat!r} is not a valid regex ({e})")
    if not presets and not patterns:
        problems.append(f"{where}: redact needs presets or patterns")
    return {
        "presets": [p for p in presets if p in REDACT_PRESETS],
        "patterns": [str(p) for p in patterns],
        "replacement": str(raw.get("replacement", "•••")),
    }


def _upload_files(raw: Any, where: str, problems: list[str]) -> list[str] | None:
    """``files:`` — one absolute path or a list of them. Shape only: whether each file exists,
    is a regular file, sits under the operator's ``upload_dirs`` and fits the size cap is
    checked by the host at shoot time (``shoot.check_uploads``), against the disk as it is then."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        problems.append(f"{where}: `files` must be an absolute path or a non-empty list of them")
        return None
    if len(raw) > MAX_UPLOAD_FILES:
        problems.append(f"{where}: {len(raw)} files is over the {MAX_UPLOAD_FILES}-file max for one upload step")
        return None
    out: list[str] = []
    for f in raw:
        if not isinstance(f, str) or not f.strip():
            problems.append(f"{where}: every entry in `files` must be a non-empty path string, got {f!r}")
            return None
        f = f.strip()
        if not (f.startswith("/") or f.startswith("~") or re.match(r"^[A-Za-z]:[\\/]", f)):
            problems.append(f"{where}: {f!r} must be an absolute path (a relative path depends on the worker's cwd)")
            return None
        out.append(f)
    return out


def browser_origin(url: str) -> str:
    """``url``'s origin exactly as a page's ``location.origin`` spells it — lowercase scheme and
    host, the port only when it isn't the scheme's default — or '' for a non-http(s) URL."""
    from urllib.parse import urlsplit

    try:
        u = urlsplit(url.strip())
        port = u.port
    except ValueError:
        return ""
    scheme = u.scheme.lower()
    if scheme not in ("http", "https") or not u.hostname:
        return ""
    host = u.hostname.lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    if port is not None and port != {"http": 80, "https": 443}[scheme]:
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def _storage_area(raw: Any, where: str, problems: list[str]) -> dict[str, str]:
    """``{key: value}`` → ``{key: str}``. A string value is stored as-is; anything else
    (a mapping, list, number, bool, null) is JSON-serialized — what ``JSON.parse`` reads back."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        problems.append(f"{where}: must be a mapping of key → value")
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not k or len(k) > MAX_STORAGE_KEY_LEN:
            problems.append(f"{where}: key {k!r} must be a non-empty string of at most {MAX_STORAGE_KEY_LEN} chars")
            continue
        if isinstance(v, str):
            out[k] = v
            continue
        try:
            out[k] = json.dumps(v, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as e:
            problems.append(f"{where}.{k}: can't be JSON-serialized ({e})")
    return out


def _storage(raw: Any, base: str, problems: list[str]) -> dict[str, Any] | None:
    """``storage: {origin?, local: {…}, session?: {…}}`` — browser storage written on ONE origin
    before any page script runs (default origin: the base_url's)."""
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict):
        problems.append("storage must be a mapping: {origin?: URL, local: {key: value}, session?: {key: value}}")
        return None
    for k in raw:
        if k not in STORAGE_KEYS:
            problems.append(
                f"storage: unknown key `{k}`{_suggest(k, STORAGE_KEYS)} — storage takes {', '.join(STORAGE_KEYS)}"
            )
    if raw.get("origin") not in (None, ""):
        o = raw["origin"]
        org = browser_origin(o) if isinstance(o, str) else ""
        if not org:
            problems.append(f"storage.origin {o!r} must be an http(s) origin like http://localhost:7871")
            return None
        from urllib.parse import urlsplit

        u = urlsplit(o.strip())
        if u.path not in ("", "/") or u.query or u.fragment or u.username or u.password:
            problems.append(f"storage.origin {o!r} must be an origin only — no path, query or fragment")
            return None
    else:
        org = browser_origin(base) if base else ""
        if not org:
            problems.append("storage needs an origin — set base_url, or give storage.origin")
            return None
    local = _storage_area(raw.get("local"), "storage.local", problems)
    session = _storage_area(raw.get("session"), "storage.session", problems)
    if not local and not session:
        problems.append("storage needs `local` and/or `session` entries")
        return None
    n = len(local) + len(session)
    size = sum(len(k.encode()) + len(v.encode()) for area in (local, session) for k, v in area.items())
    if n > MAX_STORAGE_ENTRIES:
        problems.append(f"storage: {n} entries is over the {MAX_STORAGE_ENTRIES}-entry max")
    if size > MAX_STORAGE_BYTES:
        problems.append(f"storage: {size} bytes is over the {MAX_STORAGE_BYTES}-byte max")
    return {"origin": org, "local": local, "session": session}


def _init_script(raw: Any, base: str, problems: list[str]) -> str:
    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str):
        problems.append(f"init_script must be a string of JavaScript source, got {type(raw).__name__}")
        return ""
    if len(raw.encode("utf-8")) > MAX_INIT_SCRIPT_BYTES:
        problems.append(f"init_script is over the {MAX_INIT_SCRIPT_BYTES // 1024} KB max")
        return ""
    if not (browser_origin(base) if base else ""):
        problems.append("init_script needs a base_url — it runs only on that origin")
        return ""
    return raw


def _step(i: int, raw: Any, problems: list[str], has_base: bool) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or len(raw) != 1:
        problems.append(f"step {i}: each step is a one-key mapping like `- click: ...`, got {raw!r}")
        return None
    op, body = next(iter(raw.items()))
    where = f"step {i} ({op})"
    if op not in STEP_OPS:
        problems.append(f"step {i}: unknown step `{op}`{_suggest(op, STEP_OPS)} — steps are {', '.join(STEP_OPS)}")
        return None
    step: dict[str, Any] = {"op": op, "index": i}
    if isinstance(body, dict):
        allowed = STEP_KEYS[op]
        for k in body:
            if k not in allowed:
                hint = _suggest(k, allowed)
                if not hint and k in ("timeout", "timeout_s", "wait_ms"):
                    hint = " (did you mean `timeout_ms`?)" if "timeout_ms" in allowed else ""
                if k == "frame" and op in ("mask", "redact"):
                    hint = f" ({op} already reaches into every frame, including ones that load later)"
                problems.append(f"{where}: unknown option `{k}`{hint} — {op} takes {', '.join(allowed)}")
        if "timeout_ms" in body:
            if op == "wait_for" and "ms" in body:
                problems.append(
                    f"{where}: `timeout_ms` bounds a wait for an element or network idle — a fixed "
                    "`ms` wait doesn't take one"
                )
            else:
                t = _timeout_ms(body["timeout_ms"], f"{where} timeout_ms", problems)
                if t is not None:
                    step["timeout_ms"] = t

    if op == "goto":
        url = body.get("url") if isinstance(body, dict) else body
        if not isinstance(url, str) or not url.strip():
            problems.append(f"{where}: needs a URL or a path")
            return None
        if not re.match(r"^https?://", url) and not has_base:
            problems.append(f"{where}: {url!r} is relative but the script has no base_url")
        step["url"] = url.strip()
        if isinstance(body, dict) and body.get("wait_until"):
            wu = str(body["wait_until"])
            if wu not in ("load", "domcontentloaded", "networkidle", "commit"):
                problems.append(f"{where}: wait_until must be load|domcontentloaded|networkidle|commit")
            step["wait_until"] = wu
    elif op in ("click", "hover", "focus"):
        t = (
            _target(body, where, problems)
            if not isinstance(body, dict)
            else _target_from_mapping(body, where, problems, required=True)
        )
        if t is None:
            return None
        step["target"] = t
    elif op == "mouse_move":
        if not isinstance(body, dict) or "x" not in body or "y" not in body:
            problems.append(f"{where}: needs {{x, y}} — viewport CSS pixels to park the pointer at")
            return None
        x = _int(body["x"], f"{where} x", problems, 0, 3840)
        y = _int(body["y"], f"{where} y", problems, 0, 2160)
        if x is None or y is None:
            return None
        step.update(x=x, y=y, smooth=bool(body.get("smooth", True)))
    elif op == "drag":
        if not isinstance(body, dict) or "to" not in body:
            problems.append(f"{where}: needs a target to grab and `to: {{dx, dy}}` — how far to drag it (px)")
            return None
        t = _target_from_mapping(body, where, problems, required=True)
        to = body["to"]
        if not isinstance(to, dict) or not to or any(k not in ("dx", "dy") for k in to):
            problems.append(f"{where}: `to` must be {{dx: px, dy: px}} — an offset from the target's centre")
            return None
        dx = _int(to.get("dx", 0), f"{where} to.dx", problems, -MAX_DRAG_PX, MAX_DRAG_PX)
        dy = _int(to.get("dy", 0), f"{where} to.dy", problems, -MAX_DRAG_PX, MAX_DRAG_PX)
        if t is None or dx is None or dy is None:
            return None
        if not dx and not dy:
            problems.append(f"{where}: `to` moves nowhere — give a non-zero dx and/or dy")
            return None
        step.update(target=t, dx=dx, dy=dy)
    elif op == "fill":
        if not isinstance(body, dict) or "value" not in body:
            problems.append(f"{where}: needs a target and a `value`")
            return None
        t = _target_from_mapping(body, where, problems, required=True)
        if t is None:
            return None
        step.update(target=t, value=str(body["value"]))
    elif op == "type":
        if isinstance(body, str):
            body = {"text": body}
        if not isinstance(body, dict) or "text" not in body:
            problems.append(f"{where}: needs `text` (and optionally a target and delay_ms)")
            return None
        # `text` is the payload here — a text-matched target goes under `target: {text: …}`.
        step["target"] = _target_from_mapping(body, where, problems, required=False, exclude=("text",))
        step["text"] = str(body["text"])
        d = _int(body.get("delay_ms", 45), f"{where} delay_ms", problems, 0, 1000)
        step["delay_ms"] = 45 if d is None else d
    elif op == "press":
        if isinstance(body, str):
            body = {"key": body}
        if not isinstance(body, dict) or not body.get("key"):
            problems.append(f"{where}: needs a `key` like Enter, Escape, Meta+K")
            return None
        step["key"] = str(body["key"])
        step["target"] = _target_from_mapping(body, where, problems, required=False)
        rp = _int(body.get("repeat", 1), f"{where} repeat", problems, 1, MAX_PRESS_REPEAT)
        step["repeat"] = rp or 1
        gap = _int(body.get("delay_ms", DEFAULT_PRESS_GAP_MS), f"{where} delay_ms", problems, 0, 5000)
        step["delay_ms"] = DEFAULT_PRESS_GAP_MS if gap is None else gap
    elif op == "wait_for":
        if isinstance(body, (int, float)) and not isinstance(body, bool):
            body = {"ms": body}
        if not isinstance(body, dict):
            problems.append(f"{where}: needs {{selector|text|role…}}, {{network_idle: true}} or {{ms: N}}")
            return None
        if body.get("network_idle"):
            step["network_idle"] = True
        elif "ms" in body:
            ms = _int(body["ms"], f"{where} ms", problems, 0, MAX_HOLD_MS)
            step["ms"] = ms or 0
        elif body.get("frame") is not None and "target" not in body and not any(k in body for k in TARGET_KEYS):
            pass  # wait for the frame itself — _step_frame keeps it
        else:
            t = _target_from_mapping(body, where, problems, required=True)
            if t is None:
                return None
            step["target"] = t
            state = str(body.get("state", "visible"))
            if state not in ("visible", "hidden", "attached", "detached"):
                problems.append(f"{where}: state must be visible|hidden|attached|detached")
            step["state"] = state
    elif op == "hold":
        ms = body.get("ms") if isinstance(body, dict) else body
        v = _int(ms, where, problems, 0, MAX_HOLD_MS)
        if v is None:
            return None
        step["ms"] = v
    elif op == "scroll":
        if isinstance(body, (int, float)) and not isinstance(body, bool):
            body = {"y": body}
        if not isinstance(body, dict):
            problems.append(f"{where}: needs {{y: pixels}} and/or a target to scroll into view")
            return None
        step["target"] = _target_from_mapping(body, where, problems, required=False)
        step["x"] = int(body.get("x", 0) or 0)
        step["y"] = int(body.get("y", 0) or 0)
        step["smooth"] = bool(body.get("smooth", True))
        if not step["target"] and not step["x"] and not step["y"]:
            problems.append(f"{where}: give a distance (y/x) or a target")
    elif op == "mark":
        name = body.get("name") if isinstance(body, dict) else body
        if not isinstance(name, str) or not _NAME_RE.match(name):
            problems.append(f"{where}: a mark needs a short name (letters, digits, - _ .), got {name!r}")
            return None
        step["name"] = name
    elif op == "screenshot":
        if isinstance(body, str):
            body = {"name": body}
        if not isinstance(body, dict) or not isinstance(body.get("name"), str) or not _NAME_RE.match(body["name"]):
            problems.append(f"{where}: a screenshot needs a short `name` (letters, digits, - _ .)")
            return None
        step["name"] = body["name"]
        step["target"] = _target_from_mapping(body, where, problems, required=False)
        step["full_page"] = bool(body.get("full_page", False))
    elif op == "mask":
        m = _mask(body, where, problems)
        if m is None:
            return None
        step.update(m)
    elif op == "redact":
        r = _redact(body, where, problems)
        if r is None:
            return None
        step.update(r)
    elif op == "upload":
        if not isinstance(body, dict) or "files" not in body:
            problems.append(
                f"{where}: needs a target (the <input type=file>, or the button/drop zone that opens a "
                "file chooser) and `files: [/absolute/path, …]`"
            )
            return None
        t = _target_from_mapping(body, where, problems, required=True)
        files = _upload_files(body["files"], where, problems)
        if t is None or files is None:
            return None
        step.update(target=t, files=files)
    _step_frame(body, step, where, problems)
    _within(body, step, where, problems)
    return step


def _within(body: Any, step: dict[str, Any], where: str, problems: list[str]) -> None:
    """``within: <target>`` — scope a click / hover / focus / wait_for target to a container
    (a panel, a dialog, a list row): the target is looked for INSIDE the container only. The
    container is a target like any other (string or mapping, ``nth``/``exact`` allowed); its
    frame comes from the step or the target, never from ``within`` itself."""
    if not isinstance(body, dict) or body.get("within") in (None, ""):
        return
    if not step.get("target"):
        problems.append(f"{where}: `within` scopes a target — this step has none to scope")
        return
    w = _target(body["within"], f"{where} within", problems)
    if w is None:
        return
    if "frame" in w:
        problems.append(f"{where} within: put `frame` on the step or its target — the container is looked for in it")
        return
    step["within"] = w


def validate(text_or_obj: Any) -> dict[str, Any]:
    """Parse + validate. Returns the normalized script or raises :class:`ScriptError`."""
    data = parse(text_or_obj)
    problems: list[str] = []
    for key in data:
        if key not in TOP_KEYS:
            problems.append(f"unknown top-level key `{key}`{_suggest(key, TOP_KEYS)}")

    out: dict[str, Any] = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    name = str(data.get("name") or "take")
    if not _NAME_RE.match(name):
        problems.append(f"name {name!r} must be short: letters, digits, - _ .")
    out["name"] = name
    out["description"] = str(data.get("description") or "")

    base = str(data.get("base_url") or "").strip()
    if base and not re.match(r"^https?://[^\s/]+", base):
        problems.append(f"base_url {base!r} must be an http(s) URL")
    out["base_url"] = base.rstrip("/")

    vp = data.get("viewport", DEFAULTS["viewport"])
    if not isinstance(vp, dict):
        problems.append("viewport must be {width, height}")
    else:
        w = _int(vp.get("width"), "viewport.width", problems, 200, 3840)
        h = _int(vp.get("height"), "viewport.height", problems, 200, 2160)
        out["viewport"] = {"width": w or 1280, "height": h or 800}
    try:
        dsf = float(data.get("device_scale_factor", DEFAULTS["device_scale_factor"]))
        if not 1 <= dsf <= 3:
            raise ValueError
        out["device_scale_factor"] = dsf
    except (TypeError, ValueError):
        problems.append("device_scale_factor must be a number from 1 to 3")

    cs = str(data.get("color_scheme", DEFAULTS["color_scheme"]))
    if cs not in COLOR_SCHEMES:
        problems.append(f"color_scheme must be one of {', '.join(COLOR_SCHEMES)}")
    out["color_scheme"] = cs

    tz = str(data.get("timezone_id", DEFAULTS["timezone_id"]))
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz)
    except Exception:  # noqa: BLE001 — unknown key OR no tz database; both mean "can't trust it"
        if tz != "UTC":
            problems.append(f"timezone_id {tz!r} is not a known IANA zone (e.g. UTC, America/New_York)")
    out["timezone_id"] = tz
    out["locale"] = str(data.get("locale", DEFAULTS["locale"]))
    out["cursor"] = bool(data.get("cursor", True))

    st = _timeout_ms(data.get("step_timeout_ms", DEFAULTS["step_timeout_ms"]), "step_timeout_ms", problems)
    out["step_timeout_ms"] = st or DEFAULTS["step_timeout_ms"]
    tt = _int(data.get("total_timeout_s", DEFAULTS["total_timeout_s"]), "total_timeout_s", problems, 5, MAX_TOTAL_S)
    out["total_timeout_s"] = tt or DEFAULTS["total_timeout_s"]

    auth = data.get("auth") or {}
    out["auth"] = {}
    if not isinstance(auth, dict):
        problems.append("auth must be a mapping: {bearer_env: VAR} and/or {storage_state: path}")
    else:
        for k in auth:
            if k not in ("bearer_env", "storage_state", "bearer"):
                problems.append(f"auth.{k} is not supported — use bearer_env or storage_state")
        if "bearer" in auth:
            problems.append(
                "auth.bearer would store a credential in the plan database — put the token in an env var "
                "and pass its NAME as auth.bearer_env"
            )
        if auth.get("bearer_env"):
            if not re.match(r"^[A-Z_][A-Z0-9_]*$", str(auth["bearer_env"])):
                problems.append("auth.bearer_env must be an environment variable NAME like MY_TOKEN")
            out["auth"]["bearer_env"] = str(auth["bearer_env"])
            if not base:
                problems.append("auth.bearer_env needs a base_url — the bearer is sent to that origin only")
        if auth.get("storage_state"):
            out["auth"]["storage_state"] = str(auth["storage_state"])

    for key in ("mask", "redact"):
        if isinstance(data.get(key), dict):
            for k in data[key]:
                if k not in STEP_KEYS[key]:
                    problems.append(
                        f"{key}: unknown option `{k}`{_suggest(k, STEP_KEYS[key])} — {key} takes "
                        f"{', '.join(STEP_KEYS[key])} (it already reaches into every frame)"
                    )
    out["storage"] = _storage(data.get("storage"), base, problems)
    out["init_script"] = _init_script(data.get("init_script"), base, problems)
    out["mask"] = _mask(data["mask"], "mask", problems) if data.get("mask") else None
    out["redact"] = _redact(data["redact"], "redact", problems) if data.get("redact") else None

    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append("`steps:` must be a non-empty list")
        steps = []
    norm: list[dict[str, Any]] = []
    marks: set[str] = set()
    shots: set[str] = set()
    for i, raw in enumerate(steps, start=1):
        s = _step(i, raw, problems, bool(base))
        if s is None:
            continue
        if s["op"] == "mark":
            if s["name"] in marks:
                problems.append(f"step {i} (mark): duplicate mark name {s['name']!r}")
            marks.add(s["name"])
        if s["op"] == "screenshot":
            if s["name"] in shots:
                problems.append(f"step {i} (screenshot): duplicate screenshot name {s['name']!r}")
            shots.add(s["name"])
        norm.append(s)
    if norm and not any(s["op"] == "goto" for s in norm):
        problems.append("the script never navigates — add a `goto` step")
    elif norm and norm[0]["op"] not in ("goto", "mask", "redact", "mark"):
        problems.append("step 1 should be `goto` (or a mask/redact before it) — nothing is loaded yet")
    budget_ms = out["total_timeout_s"] * 1000
    if "step_timeout_ms" in data and out["step_timeout_ms"] > budget_ms:
        problems.append(
            f"step_timeout_ms={out['step_timeout_ms']} is longer than the whole shoot "
            f"(total_timeout_s={out['total_timeout_s']}) — raise total_timeout_s (max {MAX_TOTAL_S})"
        )
    vw, vh = out["viewport"]["width"], out["viewport"]["height"]
    for st_ in norm:
        if st_["op"] == "mouse_move" and (st_["x"] >= vw or st_["y"] >= vh):
            problems.append(
                f"step {st_['index']} (mouse_move): ({st_['x']}, {st_['y']}) is outside the {vw}×{vh} viewport"
            )
    for st_ in norm:
        if st_.get("timeout_ms", 0) > budget_ms:
            problems.append(
                f"step {st_['index']} ({st_['op']}) timeout_ms: {st_['timeout_ms']}ms is longer than the whole shoot "
                f"(total_timeout_s={out['total_timeout_s']}) — raise total_timeout_s (max {MAX_TOTAL_S})"
            )
    hold_total = sum(s.get("ms", 0) for s in norm if s["op"] in ("hold", "wait_for"))
    hold_total += sum((s["repeat"] - 1) * s["delay_ms"] for s in norm if s["op"] == "press")
    if hold_total / 1000 > out["total_timeout_s"]:
        problems.append(
            f"holds/waits add up to {hold_total / 1000:.0f}s, over total_timeout_s={out['total_timeout_s']}"
        )
    out["steps"] = norm
    if problems:
        raise ScriptError(problems)
    return out


def resolve_url(script: dict[str, Any], url: str) -> str:
    if re.match(r"^https?://", url):
        return url
    base = script.get("base_url") or ""
    return base + (url if url.startswith("/") else "/" + url)


def describe_frame(f: dict[str, Any] | None) -> str:
    if not f:
        return ""
    parts = [f"{k}={f[k]!r}" for k in ("url", "selector") if k in f]
    inner = describe_frame(f.get("frame"))
    return "frame(" + " ".join(parts) + ")" + (f" > {inner}" if inner else "")


def describe_target(t: dict[str, Any] | None) -> str:
    if not t:
        return "(focused element)"
    for k in TARGET_KEYS:
        if k in t:
            s = f"{k}={t[k]!r}"
            if k == "role" and t.get("name"):
                s += f" name={t['name']!r}"
            if t.get("exact"):
                s += " exact"
            if "nth" in t:
                s += f" nth={t['nth']}"
            if t.get("frame"):
                s += f" in {describe_frame(t['frame'])}"
            return s
    return str(t)


def describe_step(step: dict[str, Any]) -> str:
    desc = _describe_step(step)
    if step.get("timeout_ms"):
        desc += f" (timeout {step['timeout_ms']}ms)"
    return desc


def _describe_within(step: dict[str, Any]) -> str:
    return f" within {describe_target(step['within'])}" if step.get("within") else ""


def _describe_step(step: dict[str, Any]) -> str:
    op = step["op"]
    if op == "goto":
        return f"goto {step['url']}"
    if op in ("click", "hover", "focus"):
        return f"{op} {describe_target(step['target'])}" + _describe_within(step)
    if op == "mouse_move":
        return f"move the pointer to ({step['x']}, {step['y']})"
    if op == "drag":
        return f"drag {describe_target(step['target'])} by ({step['dx']:+d}, {step['dy']:+d})"
    if op == "fill":
        return f"fill {describe_target(step['target'])}"
    if op == "type":
        return f"type {len(step['text'])} chars into {describe_target(step.get('target'))}"
    if op == "press":
        times = f" ×{step['repeat']}" if step.get("repeat", 1) > 1 else ""
        return f"press {step['key']}{times}"
    if op == "wait_for":
        if step.get("network_idle"):
            return "wait for network idle"
        if "ms" in step:
            return f"wait {step['ms']}ms"
        if step.get("frame") and not step.get("target"):
            return f"wait for {describe_frame(step['frame'])}"
        return f"wait for {describe_target(step['target'])}{_describe_within(step)} ({step.get('state')})"
    if op in ("hold",):
        return f"hold {step['ms']}ms"
    if op == "screenshot" and step.get("target"):
        return f"screenshot {step['name']} of {describe_target(step['target'])}"
    if op == "screenshot" and step.get("frame"):
        return f"screenshot {step['name']} of {describe_frame(step['frame'])}"
    if op in ("mark", "screenshot"):
        return f"{op} {step['name']}"
    if op == "upload":
        names = ", ".join(f.replace("\\", "/").rsplit("/", 1)[-1] for f in step["files"])
        return f"upload {names} via {describe_target(step['target'])}"
    return op


def example() -> str:
    """A template the agent can adapt — returned by campaign_script_save('template')."""
    import textwrap

    block = (__doc__ or "").split("Example::", 1)[1].split("Targets (", 1)[0]
    ops = textwrap.fill(", ".join(STEP_OPS), 92, initial_indent="# ", subsequent_indent="#   ")
    return textwrap.dedent(block).strip("\n") + "\n# Every step op (the shot-scripting skill has the details):\n" + ops
