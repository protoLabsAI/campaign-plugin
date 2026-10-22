"""Render — a recorded take → shareable outputs, via ffmpeg on PATH.

Each output spec says what to cut and how to ship it::

    {"name": "hero", "format": "mp4",           # mp4 | gif | poster
     "start": "start", "end": "end",            # seconds, or a mark name from the take
     "speed": [{"from": "typed", "to": "dialog", "factor": 4, "ease": 0.4}],   # ramp dead time
     "continuous": True,                        # default: warn on ramps that read as jump cuts
     "crop": {"x": 0, "y": 0, "width": 2560, "height": 1440},     # source pixels
     "width": 1280, "fps": 30,
     "limit": "github_attachment_video_free",   # a hard-limit id (limits.py) …
     "max_bytes": 8000000,                      # … and/or an explicit ceiling
     "captions": [{"text": "Send the form", "start": "typed", "end": 5.5}]}  # see captions.py

**Strict keys.** A key the spec doesn't know — at the top level, in a ``speed`` ramp, a
``crop`` or a caption — is refused with the allowed set (``ramps`` silently rendering no ramp
wasted a whole pass, #51). ``OUTPUT_KEYS`` / ``SPEED_KEYS`` / ``CROP_KEYS`` are the contract.

* **mp4** — H.264 (libx264 ``-preset slow``), yuv420p, ``+faststart``, at ``crf``
  ``DELIVERABLE_CRF`` (16 — the same quality as the capture master) unless the spec names a ``crf``.

**A size ceiling never silently degrades a deliverable.** With a platform ``limit`` that caps
bytes (a documented hard cap, e.g. GitHub's 10 MB attachment), the mp4 steps CRF up, then width
down, until it fits — and every step past ``QUALITY_FLOOR_CRF`` (20) or below full width comes
back as a ``warnings`` entry naming what was traded. A bare ``max_bytes`` with no platform limit
is a wish, not a cap: the CRF steps up to ``QUALITY_FLOOR_CRF`` at full width and stops there;
a file still over it is kept at that quality and reported in ``warnings`` (not a violation). An
explicit ``crf`` above the floor is the caller choosing a smaller file and is honoured as the
ladder's start.
* **gif** — palettegen/paletteuse (``stats_mode=diff``, bayer dither, rectangle diffs);
  steps fps and width down until the file fits.
* **poster** — one PNG frame at ``at`` (default: the start).

**Continuous by default.** The timeline is one unbroken run from ``start`` to ``end`` — there is
no way to drop a chunk from inside it, on purpose: viewers read a hard cut inside the action as
missing frames. The one way left to skip action is a steep speed ramp, so with ``continuous``
(the default) a ramp faster than ``MAX_CONTINUOUS_FACTOR`` (4×) still renders but comes back
with a ``warnings`` entry saying it will read as a jump cut — compress time with a uniform
1.5–2× over the run and ≤4× over pure dead time instead. ``"continuous": false`` marks an
output as a deliberate time-lapse and silences the warning. Any factor in 0.25..32 stays valid.

**Eased ramps.** A ramp switches speed on one frame, and a 1×→4× step reads as a jump even
when no frame is skipped. ``ease`` (seconds of SOURCE time, or ``true`` for
``DEFAULT_EASE_S``) makes the ramp accelerate smoothly from 1× over its first ``ease`` seconds
and decelerate back to 1× over its last ``ease`` seconds; it's clamped to half the ramp. Its
piece of the remap is a closed-form curve whose slope (source→output rate) moves along a
smoothstep from 1 to 1/factor — so the frames kept and the continuity guarantees are exactly
those of an un-eased ramp; only the timestamps in between change, monotonically.

**One remap, exact boundaries.** The whole run is ONE ``trim`` and ONE ``setpts`` expression
mapping the source clock onto the output clock, piece by piece, each at the summed computed
duration of the pieces before it. (Splitting into pieces and concatenating them let concat
size each piece's last frame as if the speed were flat, drifting every later piece by up to
(1/f − 1) source frames per ramp, compounding.) An eased ramp of length L and factor f lasts
``L/f + ease·(1 − 1/f)`` in the output (a little longer than the un-eased L/f). With
``continuous`` on, a ramp of ``EASE_SUGGEST_FACTOR``× or more with no ease is flagged.

An output that still doesn't fit after its ladder is kept (so the operator can look) but is
reported with its violations, and the review gate refuses to offer it as ready.

The ffmpeg runner is injectable so the suite tests command building with no ffmpeg present.
"""

from __future__ import annotations

import difflib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from . import deps, limits

FORMATS = ("mp4", "gif", "poster")
Runner = Callable[[list[str]], subprocess.CompletedProcess]
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# Bounds on what one spec may ask ffmpeg for. An 8K-wide scale of a 1280px take, a 10,000 fps
# GIF, or an output list of hundreds would tie the agent's turn (and the machine) up for hours.
MAX_WIDTH = 7680
MAX_FPS = 60
MAX_OUTPUTS = 12
FFMPEG_TIMEOUT_S = 600  # one ffmpeg run
# Deliverable quality (Josh 10-15: 2K device-pixel capture, crisp smallest text at 100 %). The
# capture master is crf 16 (capture.py); every deliverable mp4 — campaign_render, the brand
# bookends, produce_video's review cut, montages — defaults to the same, libx264 preset slow.
DELIVERABLE_CRF = 16
# The worst CRF a byte ceiling may push a deliverable to WITHOUT a platform limit behind it.
QUALITY_FLOOR_CRF = 20
OUTPUT_BUDGET_S = 1200  # one output's whole size ladder


class RenderError(RuntimeError):
    pass


OUTPUT_KEYS = (
    "name", "format", "title", "start", "end", "speed", "continuous", "crop", "width", "fps", "crf",
    "limit", "max_bytes", "at", "bookends", "captions",
)  # fmt: skip
SPEED_KEYS = ("from", "to", "factor", "ease")
CROP_KEYS = ("x", "y", "width", "height")
# Names an agent reaches for that mean a real key.
KEY_ALIASES = {
    "ramps": "speed", "ramp": "speed", "speeds": "speed", "speed_ramps": "speed",
    "caption": "captions", "subtitles": "captions", "subs": "captions", "lower_thirds": "captions",
    "label": "captions", "labels": "captions", "in": "start", "out": "end", "from": "start", "to": "end",
    "speed_factor": "factor", "rate": "factor", "easing": "ease", "w": "width", "h": "height", "time": "at",
}  # fmt: skip


def refuse_unknown(obj: dict[str, Any], allowed: tuple[str, ...], what: str) -> None:
    """Raise on any key ``obj`` has that ``allowed`` doesn't — naming it and the allowed set."""
    extra = [k for k in obj if k not in allowed]
    if not extra:
        return
    hints = []
    for k in extra:
        guess = KEY_ALIASES.get(str(k).lower())
        if guess not in allowed:
            close = difflib.get_close_matches(str(k), allowed, n=1, cutoff=0.75)
            guess = close[0] if close else None
        if guess:
            hints.append(f"{k!r} → did you mean {guess!r}?")
    raise RenderError(
        f"{what}: unknown key{'s' if len(extra) > 1 else ''} {', '.join(repr(k) for k in extra)} — allowed: "
        + ", ".join(allowed)
        + (f" ({'; '.join(hints)})" if hints else "")
        + ". Nothing was rendered."
    )


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_S)


def _call(runner: Runner, cmd: list[str]) -> subprocess.CompletedProcess:
    """Run ffmpeg/ffprobe; a timeout or a launch failure becomes a RenderError, never a crash."""
    try:
        return runner(cmd)
    except subprocess.TimeoutExpired:
        raise RenderError(f"{Path(cmd[0]).name} ran past its {FFMPEG_TIMEOUT_S}s limit and was stopped") from None
    except OSError as e:
        raise RenderError(f"couldn't run {Path(cmd[0]).name}: {e}") from None


def _ff(runner: Runner, cmd: list[str]) -> None:
    res = _call(runner, cmd)
    if res.returncode != 0:
        tail = "\n".join((res.stderr or "").strip().splitlines()[-6:])
        raise RenderError(f"ffmpeg failed ({res.returncode}): {tail or 'no output'}")


# ── probing ──────────────────────────────────────────────────────────────────
def probe(path: str | Path, runner: Runner | None = None) -> dict[str, Any]:
    """{width, height, duration_s, size_bytes} for a media file (ffprobe)."""
    runner = runner or _default_runner
    fp = deps.ffprobe()
    if not fp:
        raise RenderError("ffprobe isn't available — it ships with ffmpeg; " + deps.ffmpeg_hint())
    p = Path(path)
    res = _call(
        runner,
        [
            fp,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,duration:format=duration",
            "-of",
            "json",
            str(p),
        ],
    )
    if res.returncode != 0:
        raise RenderError(f"ffprobe couldn't read {p.name}: {(res.stderr or '').strip()[:300]}")
    try:
        data = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        raise RenderError(f"ffprobe returned unreadable output for {p.name}") from None
    stream = (data.get("streams") or [{}])[0]
    dur = _num(stream.get("duration")) or _num((data.get("format") or {}).get("duration"))
    if not dur and p.suffix.lower() in (".webm", ".mkv"):
        # Playwright's webm carries no duration header — read the last packet's timestamp.
        res2 = _call(
            runner,
            [fp, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(p)],
        )
        stamps = [_num(x) for x in (res2.stdout or "").split()]
        stamps = [s for s in stamps if s is not None]
        dur = max(stamps) if stamps else 0.0
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "duration_s": round(float(dur or 0), 3),
        "size_bytes": p.stat().st_size if p.exists() else 0,
    }


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN guard


# ── spec handling ────────────────────────────────────────────────────────────
def resolve_time(value: Any, marks: dict[str, float], what: str) -> float | None:
    """Seconds from a number, a mark name, or ``mark:<name>``. None = not given."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _finite(float(value), what)
    s = str(value).strip()
    if s.startswith("mark:"):
        s = s[5:]
    if s in marks:
        return float(marks[s])
    try:
        return _finite(float(s), what)
    except ValueError:
        known = ", ".join(sorted(marks)) or "none recorded"
        raise RenderError(f"{what}: {value!r} is neither seconds nor a mark of this take (marks: {known})") from None


def _finite(v: float, what: str) -> float:
    if v != v or v in (float("inf"), float("-inf")) or v < 0:
        raise RenderError(f"{what}: {v} isn't a usable time — give seconds from 0 or a mark name")
    return v


def _bounded_int(spec: dict[str, Any], key: str, lo: int, hi: int, name: str) -> int | None:
    if spec.get(key) in (None, ""):
        return None
    try:
        v = int(spec[key])
    except (TypeError, ValueError):
        raise RenderError(f"output {name}: {key} must be an integer") from None
    if not lo <= v <= hi:
        raise RenderError(f"output {name}: {key} {v} is outside {lo}..{hi}")
    return v


MAX_CONTINUOUS_FACTOR = 4.0
# ``ease: true`` → this many seconds of acceleration/deceleration at each edge of a ramp.
DEFAULT_EASE_S = 0.4
MAX_EASE_S = 10.0
# A continuous clip's un-eased ramp this steep (or the inverse, for slow motion) gets a warning.
EASE_SUGGEST_FACTOR = 2.0


def _ease_seconds(value: Any, name: str) -> float:
    """``ease`` → seconds: absent/false/0 → 0, true → DEFAULT_EASE_S, else a number in 0..MAX_EASE_S."""
    if value is None or value is False or value == "":
        return 0.0
    if value is True:
        return DEFAULT_EASE_S
    if isinstance(value, str) and value.strip().lower() in ("true", "yes", "on"):
        return DEFAULT_EASE_S
    if isinstance(value, str) and value.strip().lower() in ("false", "no", "off"):
        return 0.0
    try:
        e = float(value)
    except (TypeError, ValueError):
        raise RenderError(f"output {name}: speed ease must be seconds (e.g. 0.4) or true") from None
    if not 0 <= e <= MAX_EASE_S:  # also rejects NaN
        raise RenderError(f"output {name}: speed ease {value} is outside 0..{MAX_EASE_S:g}s")
    return e


def normalize_output(spec: dict[str, Any], marks: dict[str, float], duration: float) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise RenderError(f"each output must be a mapping, got {spec!r}")
    name = str(spec.get("name") or "")
    if not _NAME_RE.match(name):
        raise RenderError(f"output name {name!r} must be short: letters, digits, - _ .")
    refuse_unknown(spec, OUTPUT_KEYS, f"output {name}")
    fmt = str(spec.get("format") or "").lower()
    if fmt not in FORMATS:
        raise RenderError(f"output {name}: format must be one of {', '.join(FORMATS)}")
    start = resolve_time(spec.get("start"), marks, f"{name}.start") or 0.0
    end = resolve_time(spec.get("end"), marks, f"{name}.end")
    end = duration if end is None or (duration and end > duration) else end
    if duration and start >= duration:
        raise RenderError(f"output {name}: start {start:.2f}s is past the end of the take ({duration:.2f}s)")
    if end is not None and end <= start:
        raise RenderError(f"output {name}: end ({end:.2f}s) must be after start ({start:.2f}s)")
    continuous = spec.get("continuous", True)
    if not isinstance(continuous, bool):
        raise RenderError(f"output {name}: continuous must be true or false")
    ramps, warnings = [], []
    eased: list[tuple[tuple[float, float, float], float]] = []
    for r in spec.get("speed") or []:
        if not isinstance(r, dict):
            raise RenderError(f"output {name}: each speed ramp must be a mapping {{from, to, factor, ease}}")
        refuse_unknown(r, SPEED_KEYS, f"output {name}: speed ramp")
        a = resolve_time(r.get("from"), marks, f"{name}.speed.from")
        b = resolve_time(r.get("to"), marks, f"{name}.speed.to")
        try:
            f = float(r.get("factor") or 1)
        except (TypeError, ValueError):
            raise RenderError(f"output {name}: speed factor must be a number") from None
        if a is None or b is None or b <= a:
            raise RenderError(f"output {name}: each speed ramp needs from < to")
        if not 0.25 <= f <= 32:  # also rejects NaN
            raise RenderError(f"output {name}: speed factor {f} is outside 0.25..32")
        ease = _ease_seconds(r.get("ease"), name)
        if continuous and f > MAX_CONTINUOUS_FACTOR:
            warnings.append(
                f"speed factor {f:g} ({a:.2f}–{b:.2f}s) will read as a jump cut — a continuous clip "
                f"ramps at most {MAX_CONTINUOUS_FACTOR:g}× and only over pure dead time (typing, a "
                "spinner); use a uniform 1.5–2× over the run and a longer clip instead, or set "
                "continuous: false if this is a deliberate time-lapse"
                + (" (ease smooths the edges; it doesn't bring back the frames a steep ramp skips)" if ease else "")
            )
        if continuous and not ease and (f >= EASE_SUGGEST_FACTOR or f <= 1 / EASE_SUGGEST_FACTOR):
            warnings.append(
                f"speed factor {f:g} ({a:.2f}–{b:.2f}s) starts and stops abruptly — the speed change on "
                f"one frame reads as a jump; add ease: true (or ease: seconds, e.g. {DEFAULT_EASE_S:g}) to "
                "accelerate into and out of it"
            )
        ramp = (max(a, start), min(b, end) if end is not None else b, f)
        ramps.append(ramp)
        eased.append((ramp, ease))
    order = sorted(range(len(ramps)), key=lambda i: ramps[i])
    eases = [min(eased[i][1], max(0.0, (ramps[i][1] - ramps[i][0]) / 2)) for i in order]
    ramps = [ramps[i] for i in order]
    for (a1, b1, _), (a2, _b2, _) in zip(ramps, ramps[1:]):
        if a2 < b1:
            raise RenderError(f"output {name}: speed ramps overlap ({a1:.2f}–{b1:.2f} and {a2:.2f}…)")
    crop = spec.get("crop")
    if crop is not None:
        if isinstance(crop, dict):
            refuse_unknown(crop, CROP_KEYS, f"output {name}: crop")
        try:
            crop = {k: int(crop[k]) for k in ("x", "y", "width", "height")}
        except (KeyError, TypeError, ValueError):
            raise RenderError(f"output {name}: crop needs integer x, y, width, height") from None
        if crop["x"] < 0 or crop["y"] < 0 or not (2 <= crop["width"] <= MAX_WIDTH and 2 <= crop["height"] <= MAX_WIDTH):
            raise RenderError(f"output {name}: crop must be inside the frame (x, y ≥ 0; 2..{MAX_WIDTH}px per side)")
        crop["width"] -= crop["width"] % 2
        crop["height"] -= crop["height"] % 2
    max_bytes = _bounded_int(spec, "max_bytes", 1_000, 10**12, name)
    limit_bytes = None
    width = _bounded_int(spec, "width", 16, MAX_WIDTH, name)
    crf = _bounded_int(spec, "crf", 0, 51, name)
    fps = None
    if spec.get("fps") not in (None, ""):
        try:
            fps = float(spec["fps"])
        except (TypeError, ValueError):
            raise RenderError(f"output {name}: fps must be a number") from None
        if not 1 <= fps <= MAX_FPS:  # also rejects NaN
            raise RenderError(f"output {name}: fps {spec['fps']} is outside 1..{MAX_FPS}")
    from . import captions as capmod

    caps = capmod.normalize(spec.get("captions"), marks, name)
    if caps and fmt == "poster":
        raise RenderError(f"output {name}: captions are timed — they burn into an mp4 or a gif, not a poster")
    limit_id = str(spec.get("limit") or "")
    if limit_id:
        row = limits.get(limit_id)
        if row is None:
            raise RenderError(f"output {name}: unknown limit {limit_id!r} — campaign_limits lists them")
        if row.get("max_bytes"):
            limit_bytes = int(row["max_bytes"])
            max_bytes = min(int(max_bytes), limit_bytes) if max_bytes else limit_bytes
    return {
        "name": name,
        "format": fmt,
        "start": start,
        "end": end,
        "ramps": ramps,
        "eases": eases,
        "continuous": continuous,
        "warnings": warnings,
        "crop": crop,
        "width": width,
        "fps": fps,
        "crf": DELIVERABLE_CRF if crf is None else crf,
        "max_bytes": int(max_bytes) if max_bytes else None,
        # the platform cap behind max_bytes (a ``limit`` row's bytes), or None for a bare max_bytes
        "limit_bytes": limit_bytes,
        "limit": limit_id,
        "at": resolve_time(spec.get("at"), marks, f"{name}.at"),
        "title": str(spec.get("title") or ""),
        # protoLabs bookends v1 around a FULL clip (mp4): brand (the default) | none. A GIF or a
        # poster never gets them (loops and stills).
        "bookends": _bookends(spec.get("bookends"), fmt, name),
        # [{text, start, end|None, n}] in SOURCE seconds (captions.py maps them onto the output)
        "captions": caps,
    }


def _bookends(value: Any, fmt: str, name: str) -> str:
    if fmt != "mp4":
        if value in ("brand", True):
            raise RenderError(f"output {name}: bookends only wrap an mp4 (a {fmt} is a loop / still)")
        return "none"
    if value in (None, "", "brand", True):
        return "brand"
    if value in ("none", False):
        return "none"
    raise RenderError(f"output {name}: bookends must be brand or none, got {value!r}")


Segment = tuple  # (a, b, speed) or, for an eased ramp, (a, b, speed, ease)


def segments(
    start: float,
    end: float | None,
    ramps: list[tuple[float, float, float]],
    eases: list[float] | None = None,
) -> list[Segment]:
    """Split [start, end] into (a, b, speed) pieces around the ramps. An eased ramp
    (``eases[i] > 0``) comes back as (a, b, speed, ease), its ease clamped to half its length."""
    out: list[Segment] = []
    eases = list(eases or [])
    cur = start
    for i, (a, b, f) in enumerate(ramps):
        if end is not None and a >= end:
            break
        if a > cur:
            out.append((cur, a, 1.0))
        lo = max(a, cur)
        e = eases[i] if i < len(eases) else 0.0
        e = min(e, max(0.0, (b - lo) / 2)) if e and f != 1.0 else 0.0
        out.append((lo, b, f, e) if e > 0 else (lo, b, f))
        cur = b
    if end is None or cur < end:
        out.append((cur, end, 1.0))
    return [s for s in out if s[1] is None or s[1] - s[0] > 0.01]


def _fmt(t: float) -> str:
    return f"{t:.3f}".rstrip("0").rstrip(".") or "0"


def _x(v: float) -> str:
    """A full-precision number for an ffmpeg expression (the curve must join up exactly)."""
    return f"({float(v):.9f})"


def eased_time(t: float, length: float, factor: float, ease: float) -> float:
    """Output seconds at source offset ``t`` into an eased ramp of ``length`` source seconds.

    The source→output rate r(t) = dτ/dt moves from 1 to k = 1/factor along a smoothstep over
    the first ``ease`` seconds, holds at k, and returns to 1 over the last ``ease`` seconds.
    r is continuous, never below min(1, k) > 0 — so τ is strictly increasing — and the
    integral is closed-form: ∫₀ᵘ smoothstep = u³ − u⁴/2. Past ``length`` it runs at 1×.
    """
    k, e, L = 1.0 / factor, ease, length
    d, m = k - 1.0, L - e

    def big_s(u: float) -> float:
        return u**3 - u**4 / 2

    if t <= 0:
        return t
    if t < e:
        return t + d * e * big_s(t / e)
    head = e * (1 + k) / 2
    if t < m:
        return head + k * (t - e)
    mid = head + k * (m - e)
    if t < L:
        return mid + k * (t - m) - d * e * big_s((t - m) / e)
    return k * L + e * (1 - k) + (t - L)


def _ease_piece(t: str, length: float, factor: float, ease: float) -> str:
    """:func:`eased_time` as an ffmpeg expression of the local time ``t`` (a sub-expression,
    seconds from the piece's start). Same formula, branch for branch."""
    k, e, L = 1.0 / factor, ease, length
    d, m = k - 1.0, L - e
    head = e * (1 + k) / 2
    mid = head + k * (m - e)
    total = k * L + e * (1 - k)

    def big_s(u: str) -> str:
        return f"(pow({u},3)-pow({u},4)/2)"

    u1 = f"({t}/{_x(e)})"
    u3 = f"(({t}-{_x(m)})/{_x(e)})"
    first = f"{t}+{_x(d * e)}*{big_s(u1)}"
    middle = f"{_x(head)}+{_x(k)}*({t}-{_x(e)})"
    last = f"{_x(mid)}+{_x(k)}*({t}-{_x(m)})-{_x(d * e)}*{big_s(u3)}"
    after = f"{_x(total)}+({t}-{_x(L)})"
    return f"if(lt({t},{_x(e)}),{first},if(lt({t},{_x(m)}),{middle},if(lt({t},{_x(L)}),{last},{after})))"


def ease_expr(length: float, factor: float, ease: float) -> str:
    """One eased piece's ``setpts`` expression of ``T`` (seconds from its start), in TB units."""
    return f"({_ease_piece('T', length, factor, ease)})/TB"


def segment_duration(seg: Segment) -> float | None:
    """Output seconds of one piece (None for an open-ended one)."""
    a, b, f = seg[0], seg[1], seg[2]
    if b is None:
        return None
    if len(seg) > 3 and seg[3]:
        return eased_time(b - a, b - a, f, seg[3])
    return (b - a) / f


def timeline_duration(segs: list[Segment]) -> float | None:
    """Expected output seconds of a timeline (None if it runs to the end of the take)."""
    parts = [segment_duration(s) for s in segs]
    return None if any(p is None for p in parts) else sum(parts)  # type: ignore[arg-type]


def timeline_time(segs: list[Segment], t: float) -> float:
    """Output seconds of SOURCE time ``t`` on the timeline — what :func:`remap_expr` computes."""
    start, out = segs[0][0], 0.0
    for seg in segs:
        a, b, f = seg[0], seg[1], seg[2]
        ease = seg[3] if len(seg) > 3 else 0.0
        if b is None or t < b or seg is segs[-1]:
            local = max(0.0, t - a)
            return out + (eased_time(local, b - a, f, ease) if ease and b is not None else local / f)
        out += segment_duration(seg) or 0.0
    return out + max(0.0, t - start)  # unreachable: the last piece always returns


def remap_expr(segs: list[Segment]) -> str:
    """The whole timeline as ONE ``setpts`` expression of the source time ``T``, in TB units.

    Each piece maps its source span onto the output at its exact offset — the sum of the
    earlier pieces' computed durations — so a boundary lands where the arithmetic says, not
    where a concat filter guesses from the last frame's duration (which drifts by up to a
    source frame × (1/f − 1) per ramp, and compounds)."""

    def piece(seg: Segment, offset: float) -> str:
        a, b, f = seg[0], seg[1], seg[2]
        ease = seg[3] if len(seg) > 3 else 0.0
        t = f"(T-{_x(a)})"
        body = _ease_piece(t, b - a, f, ease) if ease and b is not None else f"{t}/{_x(f)}"
        return f"{_x(offset)}+({body})"

    offsets, acc = [], 0.0
    for seg in segs:
        offsets.append(acc)
        acc += segment_duration(seg) or 0.0
    expr = piece(segs[-1], offsets[-1])
    for seg, off in zip(reversed(segs[:-1]), reversed(offsets[:-1])):
        expr = f"if(lt(T,{_x(seg[1])}),{piece(seg, off)},{expr})"
    return f"({expr})/TB"


def timeline_filter(segs: list[Segment], crop: dict | None, width: int | None) -> str:
    """The filter_complex up to ``[base]`` — cut, speed, crop, scale.

    One ``trim`` for the whole run (continuous: there is no interior cut), then — when any
    piece runs at another speed — one ``setpts`` remap of the source clock onto the output
    clock (:func:`remap_expr`). Every frame is kept and stays in order; the ``fps`` pass after
    this is what thins a sped-up stretch."""
    start, end = segs[0][0], segs[-1][1]
    trim = f"trim=start={_fmt(start)}" + (f":end={_fmt(end)}" if end is not None else "")
    if all(seg[2] == 1.0 for seg in segs):
        pts = "setpts=PTS-STARTPTS"
    else:
        # Quoted: the expression's commas must not split the filter chain.
        pts = f"setpts='{remap_expr(segs)}'"
    post = [trim, pts]
    if crop:
        post.append(f"crop={crop['width']}:{crop['height']}:{crop['x']}:{crop['y']}")
    if width:
        post.append(f"scale={int(width) // 2 * 2}:-2:flags=lanczos")
    return f"[0:v]{','.join(post)}[base]"


Captions = tuple  # (extra input args, the overlay chain from [cfps] to [framed])


def _with_captions(base_filter: str, fps: float, caps: Captions | None) -> tuple[list[str], str]:
    """(extra input args, the graph head): the timeline at ``fps``, then any caption overlays.
    The next filter is appended straight onto the head (without captions the graph is unchanged)."""
    if not caps:
        return [], f"{base_filter};[base]fps={_fmt(fps)},"
    inputs, chain = caps
    return list(inputs), f"{base_filter};[base]fps={_fmt(fps)}[cfps];{chain};[framed]"


def mp4_cmd(
    ff: str, src: str, dst: str, base_filter: str, *, crf: int, fps: float, caps: Captions | None = None
) -> list[str]:
    inputs, graph = _with_captions(base_filter, fps, caps)
    fc = f"{graph}format=yuv420p[out]"
    return [
        ff, "-y", "-hide_banner", "-loglevel", "error", "-i", src, *inputs, "-filter_complex", fc, "-map", "[out]",
        "-an", "-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", dst,
    ]  # fmt: skip


def gif_cmd(ff: str, src: str, dst: str, base_filter: str, *, fps: float, caps: Captions | None = None) -> list[str]:
    inputs, graph = _with_captions(base_filter, fps, caps)
    fc = (
        f"{graph}split[g1][g2];"
        "[g1]palettegen=stats_mode=diff:max_colors=256[pal];"
        "[g2][pal]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle[out]"
    )
    return [
        ff,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        src,
        *inputs,
        "-filter_complex",
        fc,
        "-map",
        "[out]",
        "-loop",
        "0",
        dst,
    ]


def poster_cmd(ff: str, src: str, dst: str, at: float, crop: dict | None, width: int | None) -> list[str]:
    vf = []
    if crop:
        vf.append(f"crop={crop['width']}:{crop['height']}:{crop['x']}:{crop['y']}")
    if width:
        vf.append(f"scale={int(width) // 2 * 2}:-2:flags=lanczos")
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-ss", _fmt(at), "-i", src]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    return cmd + ["-frames:v", "1", "-update", "1", dst]


def mp4_ladder(crf: int, width: int | None, src_width: int, *, degrade: bool = True) -> list[tuple[int, int | None]]:
    """(crf, width) attempts in order — quality first, then size.

    ``degrade=False`` (a bare ``max_bytes``, no platform limit): CRF only, at full width, never
    past ``max(crf, QUALITY_FLOOR_CRF)``."""
    w = width or src_width or None
    if not degrade:
        top = quality_floor(crf)
        return [(c, w) for c in range(crf, top, 4)] + [(top, w)]
    steps: list[tuple[int, int | None]] = [(min(crf + d, 40), w) for d in (0, 4, 8, 12)]
    if w:
        for factor, c in ((0.8, crf + 8), (0.8, crf + 12), (0.64, crf + 12), (0.5, crf + 12)):
            steps.append((min(c, 40), int(w * factor) // 2 * 2))
    seen, out = set(), []
    for s in steps:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def gif_ladder(fps: float | None, width: int | None, src_width: int) -> list[tuple[float, int | None]]:
    """(fps, width) attempts in order."""
    f = fps or 15.0
    w = width or src_width or None
    plan = [
        (1.0, f),
        (1.0, min(f, 12)),
        (0.85, min(f, 12)),
        (0.85, min(f, 10)),
        (0.72, min(f, 10)),
        (0.6, min(f, 10)),
        (0.6, min(f, 8)),
        (0.5, min(f, 8)),
    ]
    seen, out = set(), []
    for factor, ff in plan:
        step = (ff, int(w * factor) // 2 * 2 if w else None)
        if step not in seen:
            seen.add(step)
            out.append(step)
    return out


def render_output(
    src: str | Path,
    out_dir: str | Path,
    spec: dict[str, Any],
    *,
    marks: dict[str, float],
    source: dict[str, Any],
    runner: Runner | None = None,
    caption_art: Any = None,
) -> dict[str, Any]:
    """Render ONE normalized output. Returns {path, size_bytes, width, height, duration_s, attempts,
    violations, warnings, captions, caption_engine}. ``caption_art`` (a :class:`captions.Art`) is
    the seam that draws caption PNGs; by default one is made beside the output with the label engines."""
    runner = runner or _default_runner
    ff = deps.ffmpeg()
    if not ff:
        raise RenderError(deps.ffmpeg_hint())
    deadline = time.monotonic() + OUTPUT_BUDGET_S
    out_of_time = False
    src = str(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    src_w = (spec["crop"] or {}).get("width") or int(source.get("width") or 0)
    ext = {"mp4": "mp4", "gif": "gif", "poster": "png"}[spec["format"]]
    dst = out_dir / f"{spec['name']}.{ext}"
    attempts: list[dict[str, Any]] = []
    timed: list[dict[str, Any]] = []
    cap_warnings: list[str] = []
    art: Any = None

    if spec["format"] == "poster":
        at = spec["at"] if spec["at"] is not None else spec["start"]
        _ff(runner, poster_cmd(ff, src, str(dst), at, spec["crop"], spec["width"]))
        attempts.append({"at": at})
    else:
        segs = segments(spec["start"], spec["end"], spec["ramps"], spec.get("eases"))
        caps_for: Callable[..., Captions] | None = None
        if spec.get("captions"):
            from . import captions as capmod

            cut_end = spec["end"] if spec["end"] is not None else (float(source.get("duration_s") or 0) or None)
            if cut_end is None:
                raise RenderError(f"output {spec['name']}: captions need the clip's end — give the output an end")
            if segs[-1][1] is None:
                segs = segments(spec["start"], cut_end, spec["ramps"], spec.get("eases"))
            timed, cap_warnings = capmod.plan(spec["captions"], segs, spec["start"], cut_end, spec["name"])
            # Wrap at the full output size first: too-long text is refused before ffmpeg runs.
            full_w, full_h = capmod.output_size(spec, source, spec["width"])
            for c in timed:
                capmod.wrap(c["text"], full_w, full_h)
            art = caption_art or capmod.Art(out_dir / f".{spec['name']}-captions")
            cap_warnings += list(art.notes)

            def caps_for(width: int | None, fps: float) -> Captions:
                w, h = capmod.output_size(spec, source, width)
                pngs = art.pngs(timed, w, h)
                return capmod.overlay_inputs(timed, pngs, fps), capmod.overlay_chain(timed, "cfps", "framed")

        if spec["format"] == "mp4":
            capped = bool(spec.get("limit_bytes"))
            ladder = (
                mp4_ladder(spec["crf"], spec["width"], src_w, degrade=capped)
                if spec["max_bytes"]
                else [(spec["crf"], spec["width"])]
            )
            full_w, floor = spec["width"] or src_w or None, quality_floor(spec["crf"])
            for crf, width in ladder:
                degrading = crf > floor or bool(full_w and width and width < full_w)
                if degrading and attempts and attempts[-1]["size_bytes"] <= (spec.get("limit_bytes") or 0):
                    break  # it already fits the platform cap; only a tighter bare max_bytes is unmet
                base = timeline_filter(segs, spec["crop"], width if width != src_w else None)
                fps = spec["fps"] or 30
                caps = caps_for(width if width != src_w else None, fps) if caps_for else None
                _ff(runner, mp4_cmd(ff, src, str(dst), base, crf=crf, fps=fps, caps=caps))
                size = dst.stat().st_size if dst.exists() else 0
                attempts.append({"crf": crf, "width": width, "size_bytes": size})
                if not spec["max_bytes"] or size <= spec["max_bytes"]:
                    break
                if time.monotonic() > deadline:
                    out_of_time = True
                    break
        else:
            ladder = (
                gif_ladder(spec["fps"], spec["width"], src_w)
                if spec["max_bytes"]
                else [(spec["fps"] or 15.0, spec["width"])]
            )
            for fps, width in ladder:
                base = timeline_filter(segs, spec["crop"], width if width != src_w else None)
                caps = caps_for(width if width != src_w else None, fps) if caps_for else None
                _ff(runner, gif_cmd(ff, src, str(dst), base, fps=fps, caps=caps))
                size = dst.stat().st_size if dst.exists() else 0
                attempts.append({"fps": fps, "width": width, "size_bytes": size})
                if not spec["max_bytes"] or size <= spec["max_bytes"]:
                    break
                if time.monotonic() > deadline:
                    out_of_time = True
                    break

    info = probe(dst, runner) if dst.exists() else {"width": 0, "height": 0, "duration_s": 0, "size_bytes": 0}
    violations: list[str] = []
    warnings = list(spec.get("warnings") or []) + cap_warnings
    if spec["format"] == "mp4" and attempts:
        warnings += quality_notes(spec, attempts, src_w, info["size_bytes"])
    over = bool(spec["max_bytes"] and info["size_bytes"] > spec["max_bytes"])
    if over and spec["format"] == "mp4" and not (spec.get("limit_bytes") and info["size_bytes"] > spec["limit_bytes"]):
        over = False  # over a bare max_bytes only: quality_notes said so; not a hard-limit violation
    if over:
        violations.append(
            f"{limits.human_bytes(info['size_bytes'])} is still over the {limits.human_bytes(spec['max_bytes'])} ceiling "
            f"after {len(attempts)} attempts"
            + (f" (stopped at the {OUTPUT_BUDGET_S}s render budget)" if out_of_time else "")
            + " — shorten it, crop it, or speed-ramp more dead time"
        )
    if spec["limit"]:
        for v in limits.check(
            spec["limit"], size=info["size_bytes"], width=info["width"], height=info["height"], fmt=ext
        ):
            if v not in violations and "over the" not in v:
                violations.append(v)
    return {
        "path": str(dst),
        **info,
        "attempts": attempts,
        "violations": violations,
        "warnings": warnings,
        "format": spec["format"],
        # what was burned in: {text, start, end} (source s — pass back to re-render) + out_start/out_end
        "captions": timed,
        "caption_engine": art.engine if (art is not None and timed) else "",
    }


def quality_floor(crf: int) -> int:
    """The worst CRF a bare ``max_bytes`` may push a deliverable to: the caller's own crf if
    it's already above ``QUALITY_FLOOR_CRF``, else the floor."""
    return max(int(crf), QUALITY_FLOOR_CRF)


def size_notes(
    *, crf: int, last_crf: int, traded: list[str], limit: str, max_bytes: int | None, limit_bytes: int | None, size: int
) -> list[str]:
    """The size-ceiling warnings shared by render and montage — so what a ceiling cost (or would
    have cost) a deliverable is never silent. ``traded`` lists the degradations a platform
    ``limit`` forced (e.g. ``"crf 16 → 24"``); empty when nothing past the floor was traded."""
    out: list[str] = []
    if limit_bytes and traded:
        out.append(
            f"quality traded to fit {limit} ({limits.human_bytes(max_bytes)}): " + ", ".join(traded)
            + " — small text may soften; ship the full-quality render where the platform allows it"
        )  # fmt: skip
    if max_bytes and size > max_bytes and not (limit_bytes and size > limit_bytes):
        out.append(
            f"kept deliverable quality (crf {last_crf}): {limits.human_bytes(size)} is over max_bytes "
            f"{limits.human_bytes(max_bytes)}, and a bare max_bytes may not push a deliverable past "
            f"crf {quality_floor(crf)} — pass a platform limit (campaign_limits) when one really caps the "
            "file, or an explicit higher crf / smaller size"
        )
    return out


def quality_notes(spec: dict[str, Any], attempts: list[dict[str, Any]], src_w: int, size: int) -> list[str]:
    """Say what a size ceiling cost (or would have cost) an mp4 — never let it happen silently."""
    last, full_w = attempts[-1], spec["width"] or src_w or None
    traded = []
    if last["crf"] > quality_floor(spec["crf"]):
        traded.append(f"crf {spec['crf']} → {last['crf']}")
    if full_w and last.get("width") and last["width"] < full_w:
        traded.append(f"width {full_w} → {last['width']} px")
    return size_notes(
        crf=spec["crf"], last_crf=last["crf"], traded=traded, limit=spec["limit"], max_bytes=spec["max_bytes"],
        limit_bytes=spec.get("limit_bytes"), size=size,
    )  # fmt: skip
