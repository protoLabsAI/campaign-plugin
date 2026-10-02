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

Targets (click/hover/fill/type/press/wait_for/scroll/screenshot) are either a string
(a Playwright selector: CSS, ``text=…``, ``role=button[name="Save"]``) or a mapping with
ONE of ``selector`` / ``role`` (+ ``name``, ``exact``) / ``text`` / ``label`` /
``placeholder`` / ``test_id``, plus an optional ``nth``. Prefer role/text/label — they
survive restyles; CSS classes don't.

Waits: every step that waits on the page (goto, click, hover, fill, type, press, wait_for,
scroll, screenshot) gives up after ``step_timeout_ms`` (script-wide, default 15000). A step
that waits on something slow — an agent run, a build — sets its own ``timeout_ms`` (up to
180000 = 3 min; more is a validation error, not a silent clamp), and every step is also
bounded by what is left of ``total_timeout_s`` (default 300, max 900). Don't use
``wait_for: {network_idle: true}`` on an app that holds a stream open (SSE / websockets — the
protoAgent console does): it never settles. Wait for the element you need instead.
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
    "steps",
}
STEP_OPS = (
    "goto",
    "click",
    "fill",
    "type",
    "press",
    "hover",
    "wait_for",
    "hold",
    "scroll",
    "mark",
    "screenshot",
    "mask",
    "redact",
)
TARGET_KEYS = ("selector", "role", "text", "label", "placeholder", "test_id")
TARGET_OPTS = ("name", "exact", "nth")
COLOR_SCHEMES = ("light", "dark", "no-preference")
MASK_MODES = ("blur", "hide", "remove")
REDACT_PRESETS = ("home_paths", "emails", "secrets")

MAX_HOLD_MS = 60_000
# The ceiling for ONE step's wait (script-wide ``step_timeout_ms`` or a step's own
# ``timeout_ms``). 180s covers a slow real run on screen (an agent turn, a build, an install)
# while still failing a stuck step inside one shoot. A larger ask is a validation ERROR — never
# silently clamped — and every step is also bounded by what is left of ``total_timeout_s``.
MAX_STEP_TIMEOUT_MS = 180_000
MIN_STEP_TIMEOUT_MS = 100
MAX_TOTAL_S = 900
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
_TGT = ("target", "selector", "role", "text", "label", "placeholder", "test_id", "name", "exact", "nth")
# Every key a step's mapping body may carry. Anything else is an ERROR naming the key — a typo'd
# or unsupported option (``timout_ms``, ``timeout``) must never be dropped on the floor.
STEP_KEYS: dict[str, tuple[str, ...]] = {
    "goto": ("url", "wait_until", "timeout_ms"),
    "click": (*_TGT, "timeout_ms"),
    "hover": (*_TGT, "timeout_ms"),
    "fill": (*_TGT, "value", "timeout_ms"),
    "type": (*_TGT, "delay_ms", "timeout_ms"),
    "press": (*_TGT, "key", "timeout_ms"),
    "wait_for": (*_TGT, "state", "network_idle", "ms", "timeout_ms"),
    "hold": ("ms",),
    "scroll": (*_TGT, "x", "y", "smooth", "timeout_ms"),
    "mark": ("name",),
    "screenshot": (*_TGT, "full_page", "timeout_ms"),
    "mask": ("selectors", "mode"),
    "redact": ("presets", "patterns", "replacement"),
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
        try:
            out["nth"] = int(raw["nth"])
        except (TypeError, ValueError):
            problems.append(f"{where}: nth must be an integer")
    return out


def _target_from_mapping(
    body: dict[str, Any], where: str, problems: list[str], *, required: bool, exclude: tuple[str, ...] = ()
) -> dict | None:
    sub = {k: body[k] for k in (*TARGET_KEYS, *TARGET_OPTS) if k in body and k not in exclude}
    if "target" in body:
        return _target(body["target"], where, problems, required=required)
    return _target(sub or None, where, problems, required=required)


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
    elif op in ("click", "hover"):
        t = (
            _target(body, where, problems)
            if not isinstance(body, dict)
            else _target_from_mapping(body, where, problems, required=True)
        )
        if t is None:
            return None
        step["target"] = t
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
    return step


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
    for st_ in norm:
        if st_.get("timeout_ms", 0) > budget_ms:
            problems.append(
                f"step {st_['index']} ({st_['op']}) timeout_ms: {st_['timeout_ms']}ms is longer than the whole shoot "
                f"(total_timeout_s={out['total_timeout_s']}) — raise total_timeout_s (max {MAX_TOTAL_S})"
            )
    hold_total = sum(s.get("ms", 0) for s in norm if s["op"] in ("hold", "wait_for"))
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


def describe_target(t: dict[str, Any] | None) -> str:
    if not t:
        return "(focused element)"
    for k in TARGET_KEYS:
        if k in t:
            s = f"{k}={t[k]!r}"
            if k == "role" and t.get("name"):
                s += f" name={t['name']!r}"
            if "nth" in t:
                s += f" nth={t['nth']}"
            return s
    return str(t)


def describe_step(step: dict[str, Any]) -> str:
    desc = _describe_step(step)
    if step.get("timeout_ms"):
        desc += f" (timeout {step['timeout_ms']}ms)"
    return desc


def _describe_step(step: dict[str, Any]) -> str:
    op = step["op"]
    if op == "goto":
        return f"goto {step['url']}"
    if op in ("click", "hover"):
        return f"{op} {describe_target(step['target'])}"
    if op == "fill":
        return f"fill {describe_target(step['target'])}"
    if op == "type":
        return f"type {len(step['text'])} chars into {describe_target(step.get('target'))}"
    if op == "press":
        return f"press {step['key']}"
    if op == "wait_for":
        if step.get("network_idle"):
            return "wait for network idle"
        if "ms" in step:
            return f"wait {step['ms']}ms"
        return f"wait for {describe_target(step['target'])} ({step.get('state')})"
    if op in ("hold",):
        return f"hold {step['ms']}ms"
    if op in ("mark", "screenshot"):
        return f"{op} {step['name']}"
    return op


def example() -> str:
    """A template the agent can adapt — returned by campaign_script_save('template')."""
    import textwrap

    block = (__doc__ or "").split("Example::", 1)[1].split("Targets (", 1)[0]
    return textwrap.dedent(block).strip("\n")
