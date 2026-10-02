"""Render — a recorded take → shareable outputs, via ffmpeg on PATH.

Each output spec says what to cut and how to ship it::

    {"name": "hero", "format": "mp4",           # mp4 | gif | poster
     "start": "start", "end": "end",            # seconds, or a mark name from the take
     "speed": [{"from": "typed", "to": "dialog", "factor": 4}],   # ramp dead time
     "continuous": True,                        # default: warn on ramps that read as jump cuts
     "crop": {"x": 0, "y": 0, "width": 2560, "height": 1440},     # source pixels
     "width": 1280, "fps": 30,
     "limit": "github_attachment_video_free",   # a hard-limit id (limits.py) …
     "max_bytes": 8000000}                      # … and/or an explicit ceiling

* **mp4** — H.264, yuv420p, ``+faststart``; when there's a size ceiling it steps CRF up,
  then width down, until the file fits.
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

An output that still doesn't fit after its ladder is kept (so the operator can look) but is
reported with its violations, and the review gate refuses to offer it as ready.

The ffmpeg runner is injectable so the suite tests command building with no ffmpeg present.
"""

from __future__ import annotations

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
OUTPUT_BUDGET_S = 1200  # one output's whole size ladder


class RenderError(RuntimeError):
    pass


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


def normalize_output(spec: dict[str, Any], marks: dict[str, float], duration: float) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise RenderError(f"each output must be a mapping, got {spec!r}")
    name = str(spec.get("name") or "")
    if not _NAME_RE.match(name):
        raise RenderError(f"output name {name!r} must be short: letters, digits, - _ .")
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
    for r in spec.get("speed") or []:
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
        if continuous and f > MAX_CONTINUOUS_FACTOR:
            warnings.append(
                f"speed factor {f:g} ({a:.2f}–{b:.2f}s) will read as a jump cut — a continuous clip "
                f"ramps at most {MAX_CONTINUOUS_FACTOR:g}× and only over pure dead time (typing, a "
                "spinner); use a uniform 1.5–2× over the run and a longer clip instead, or set "
                "continuous: false if this is a deliberate time-lapse"
            )
        ramps.append((max(a, start), min(b, end) if end is not None else b, f))
    ramps.sort()
    for (a1, b1, _), (a2, _b2, _) in zip(ramps, ramps[1:]):
        if a2 < b1:
            raise RenderError(f"output {name}: speed ramps overlap ({a1:.2f}–{b1:.2f} and {a2:.2f}…)")
    crop = spec.get("crop")
    if crop is not None:
        try:
            crop = {k: int(crop[k]) for k in ("x", "y", "width", "height")}
        except (KeyError, TypeError, ValueError):
            raise RenderError(f"output {name}: crop needs integer x, y, width, height") from None
        if crop["x"] < 0 or crop["y"] < 0 or not (2 <= crop["width"] <= MAX_WIDTH and 2 <= crop["height"] <= MAX_WIDTH):
            raise RenderError(f"output {name}: crop must be inside the frame (x, y ≥ 0; 2..{MAX_WIDTH}px per side)")
        crop["width"] -= crop["width"] % 2
        crop["height"] -= crop["height"] % 2
    max_bytes = _bounded_int(spec, "max_bytes", 1_000, 10**12, name)
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
    limit_id = str(spec.get("limit") or "")
    if limit_id:
        row = limits.get(limit_id)
        if row is None:
            raise RenderError(f"output {name}: unknown limit {limit_id!r} — campaign_limits lists them")
        if row.get("max_bytes"):
            max_bytes = min(int(max_bytes), int(row["max_bytes"])) if max_bytes else int(row["max_bytes"])
    return {
        "name": name,
        "format": fmt,
        "start": start,
        "end": end,
        "ramps": ramps,
        "continuous": continuous,
        "warnings": warnings,
        "crop": crop,
        "width": width,
        "fps": fps,
        "crf": 23 if crf is None else crf,
        "max_bytes": int(max_bytes) if max_bytes else None,
        "limit": limit_id,
        "at": resolve_time(spec.get("at"), marks, f"{name}.at"),
        "title": str(spec.get("title") or ""),
    }


def segments(
    start: float, end: float | None, ramps: list[tuple[float, float, float]]
) -> list[tuple[float, float | None, float]]:
    """Split [start, end] into (a, b, speed) pieces around the ramps."""
    out: list[tuple[float, float | None, float]] = []
    cur = start
    for a, b, f in ramps:
        if end is not None and a >= end:
            break
        if a > cur:
            out.append((cur, a, 1.0))
        out.append((max(a, cur), b, f))
        cur = b
    if end is None or cur < end:
        out.append((cur, end, 1.0))
    return [s for s in out if s[1] is None or s[1] - s[0] > 0.01]


def _fmt(t: float) -> str:
    return f"{t:.3f}".rstrip("0").rstrip(".") or "0"


def timeline_filter(segs: list[tuple[float, float | None, float]], crop: dict | None, width: int | None) -> str:
    """The filter_complex up to ``[base]`` — cut, speed, concat, crop, scale."""
    parts: list[str] = []
    n = len(segs)
    labels = [f"s{i}" for i in range(n)]
    src = "[0:v]"
    if n > 1:
        parts.append(f"[0:v]split={n}" + "".join(f"[{lab}]" for lab in labels))
    for i, (a, b, f) in enumerate(segs):
        inp = f"[{labels[i]}]" if n > 1 else src
        trim = f"trim=start={_fmt(a)}" + (f":end={_fmt(b)}" if b is not None else "")
        pts = "setpts=PTS-STARTPTS" + (f",setpts=PTS/{_fmt(f)}" if f != 1.0 else "")
        parts.append(f"{inp}{trim},{pts}[v{i}]")
    if n > 1:
        parts.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[cat]")
        tail_in = "[cat]"
    else:
        tail_in = "[v0]"
    post = []
    if crop:
        post.append(f"crop={crop['width']}:{crop['height']}:{crop['x']}:{crop['y']}")
    if width:
        post.append(f"scale={int(width) // 2 * 2}:-2:flags=lanczos")
    parts.append(f"{tail_in}{','.join(post) if post else 'null'}[base]")
    return ";".join(parts)


def mp4_cmd(ff: str, src: str, dst: str, base_filter: str, *, crf: int, fps: float) -> list[str]:
    fc = f"{base_filter};[base]fps={_fmt(fps)},format=yuv420p[out]"
    return [
        ff, "-y", "-hide_banner", "-loglevel", "error", "-i", src, "-filter_complex", fc, "-map", "[out]",
        "-an", "-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", dst,
    ]  # fmt: skip


def gif_cmd(ff: str, src: str, dst: str, base_filter: str, *, fps: float) -> list[str]:
    fc = (
        f"{base_filter};[base]fps={_fmt(fps)},split[g1][g2];"
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


def mp4_ladder(crf: int, width: int | None, src_width: int) -> list[tuple[int, int | None]]:
    """(crf, width) attempts in order — quality first, then size."""
    w = width or src_width or None
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
) -> dict[str, Any]:
    """Render ONE normalized output. Returns {path, size_bytes, width, height, duration_s, attempts, violations}."""
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

    if spec["format"] == "poster":
        at = spec["at"] if spec["at"] is not None else spec["start"]
        _ff(runner, poster_cmd(ff, src, str(dst), at, spec["crop"], spec["width"]))
        attempts.append({"at": at})
    else:
        segs = segments(spec["start"], spec["end"], spec["ramps"])
        if spec["format"] == "mp4":
            ladder = (
                mp4_ladder(spec["crf"], spec["width"], src_w) if spec["max_bytes"] else [(spec["crf"], spec["width"])]
            )
            for crf, width in ladder:
                base = timeline_filter(segs, spec["crop"], width if width != src_w else None)
                _ff(runner, mp4_cmd(ff, src, str(dst), base, crf=crf, fps=spec["fps"] or 30))
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
                _ff(runner, gif_cmd(ff, src, str(dst), base, fps=fps))
                size = dst.stat().st_size if dst.exists() else 0
                attempts.append({"fps": fps, "width": width, "size_bytes": size})
                if not spec["max_bytes"] or size <= spec["max_bytes"]:
                    break
                if time.monotonic() > deadline:
                    out_of_time = True
                    break

    info = probe(dst, runner) if dst.exists() else {"width": 0, "height": 0, "duration_s": 0, "size_bytes": 0}
    violations: list[str] = []
    if spec["max_bytes"] and info["size_bytes"] > spec["max_bytes"]:
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
        "warnings": list(spec.get("warnings") or []),
        "format": spec["format"],
    }
