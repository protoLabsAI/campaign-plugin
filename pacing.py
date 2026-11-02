"""Pacing — what a clip's file and captions measure against the viral-clip-craft defaults.

Reads the video through ffmpeg (a frame-difference pass) and the asset's recorded captions.
It reports numbers and a pass/warn per rule. The thresholds are starting defaults, not facts:
only the reading speed comes from a published standard (Netflix's 20 characters a second), the
rest are vendor-grade guidance. A warn is something to look at, and it can be right to keep one,
such as a held frame while a reply is read. Measurement never fails a render: if ffmpeg can't
read the file, the report says it wasn't measured.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from . import deps, render

SAMPLE_FPS = 10
CHANGE_LEVEL = 40.0  # a sampled frame "changed" if some pixel moved this far (0-255); skips encoder noise
EVENT_GAP_S = 0.3  # changes closer than this are one event

MAX_FIRST_ACTION_S = 3.0
MAX_QUIET_S = 5.0
MAX_CADENCE_S = 5.0
MAX_CPS = 20.0
MIN_CAPTION_S = 1.0
LENGTH_S = (15.0, 30.0)
MAX_BOOKEND_SHARE = 0.15

DEFAULTS_NOTE = "Thresholds are starting defaults, not facts; judge a warn by watching the clip."

_YMAX = re.compile(r"lavfi\.signalstats\.YMAX=([0-9.]+)")


def _changed_times(path: str, runner: Any = None) -> list[float] | None:
    """Times (s) of sampled frames that differ visibly from the frame before, or None."""
    ff = deps.ffmpeg()
    if not ff:
        return None
    cmd = [
        ff, "-hide_banner", "-loglevel", "error", "-nostats", "-i", path, "-an", "-vf",
        f"fps={SAMPLE_FPS},scale=640:-2,format=gray,tblend=all_mode=difference,signalstats,"
        "metadata=print:key=lavfi.signalstats.YMAX:file=-",
        "-f", "null", "-",
    ]  # fmt: skip
    try:
        res = (runner or _run)(cmd)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    values = [float(v) for v in _YMAX.findall(res.stdout or "")]
    if not values:
        return None
    return [round((i + 1) / SAMPLE_FPS, 2) for i, v in enumerate(values) if v >= CHANGE_LEVEL]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=render.FFMPEG_TIMEOUT_S)


def events(changed: list[float]) -> list[float]:
    """Each run of changes as one event, at the time it starts."""
    out: list[float] = []
    prev = None
    for t in changed:
        if prev is None or t - prev > EVENT_GAP_S:
            out.append(t)
        prev = t
    return out


def quiet_spans(changed: list[float], duration: float) -> tuple[float, float, float]:
    """(seconds, from, to) of the longest stretch with no visible change, ends included."""
    best = (0.0, 0.0, 0.0)
    for a, b in zip([0.0, *changed], [*changed, duration]):
        if b - a > best[0]:
            best = (round(b - a, 2), a, b)
    return best


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def rules(
    *,
    duration: float,
    total: float | None = None,
    changed: list[float] | None,
    captions: list[dict[str, Any]],
    bookend_share: float,
    bookends: str,
) -> list[dict[str, Any]]:
    """Each rule as {rule, status: pass|warn|unmeasured, value, detail}. ``duration`` is the clip
    the changes were measured on; ``total`` the whole file with its bookends (default: the same)."""
    total = duration if total is None else total
    out: list[dict[str, Any]] = []

    def add(rule: str, ok: bool | None, value: str, detail: str = "") -> None:
        status = "unmeasured" if ok is None else ("pass" if ok else "warn")
        out.append({"rule": rule, "status": status, "value": value, "detail": detail})

    if changed is None:
        add("first action", None, "not measured", "ffmpeg couldn't read the frames")
        add("longest quiet span", None, "not measured")
        add("change cadence", None, "not measured")
    else:
        first = changed[0] if changed else None
        opens_on_logo = bookends == "brand"
        add(
            "first action",
            first is not None and first <= MAX_FIRST_ACTION_S and not opens_on_logo,
            "none" if first is None else f"{first:.1f}s",
            "opens on the brand intro: use bookends: short" if opens_on_logo else f"want ≤ {MAX_FIRST_ACTION_S:g}s",
        )
        quiet, a, b = quiet_spans(changed, duration)
        add(
            "longest quiet span",
            quiet <= MAX_QUIET_S,
            f"{quiet:.1f}s",
            f"at {a:.1f}–{b:.1f}s; want ≤ {MAX_QUIET_S:g}s, unless it's a deliberate reading hold",
        )
        ev = events(changed)
        gaps = [round(q - p, 2) for p, q in zip(ev, ev[1:])]
        cadence = _median(gaps) if gaps else duration
        add("change cadence", cadence <= MAX_CADENCE_S, f"{cadence:.1f}s median", f"want ≤ {MAX_CADENCE_S:g}s")
    for i, c in enumerate(captions, 1):
        s, e = c.get("out_start"), c.get("out_end")
        if s is None or e is None or e <= s:
            continue
        secs = e - s
        cps = len(str(c.get("text") or "")) / secs
        add(
            f"caption {i}",
            cps <= MAX_CPS and secs >= MIN_CAPTION_S,
            f"{secs:.1f}s, {cps:.0f} chars/s",
            f"{str(c.get('text'))[:40]!r}; want ≤ {MAX_CPS:g} chars/s",
        )
    add("bookend share", bookend_share <= MAX_BOOKEND_SHARE, f"{bookend_share:.0%}", f"want ≤ {MAX_BOOKEND_SHARE:.0%}")
    add(
        "length",
        LENGTH_S[0] <= total <= LENGTH_S[1],
        f"{total:.1f}s",
        f"a teaser wants {LENGTH_S[0]:g}–{LENGTH_S[1]:g}s",
    )
    return out


def measure_asset(asset: dict[str, Any], runner: Any = None) -> dict[str, Any]:
    """The report for one clip asset: its rules, measured from the file it points at."""
    meta = asset.get("meta") or {}
    path = str(asset.get("path") or "")
    duration = float(asset.get("duration_s") or 0.0)
    if not duration:
        try:
            duration = float(render.probe(path).get("duration_s") or 0.0)
        except (render.RenderError, OSError):
            duration = 0.0
    recipe = str(meta.get("bookends") or "")
    bookends = "short" if recipe.startswith("short") else ("brand" if recipe else "none")
    clip = str(meta.get("unbookended") or "")
    clip_s = 0.0
    if clip and Path(clip).is_file():
        try:
            clip_s = float(render.probe(clip).get("duration_s") or 0.0)
        except (render.RenderError, OSError):
            clip_s = 0.0
    share = max(0.0, 1 - clip_s / duration) if clip_s and duration else 0.0
    changed = _changed_times(clip if bookends != "none" and clip_s else path, runner)
    return {
        "duration_s": duration,
        "bookends": bookends,
        "rules": rules(
            duration=clip_s or duration,
            total=duration,
            changed=changed,
            captions=meta.get("caption_timing") or [],
            bookend_share=share,
            bookends=bookends,
        ),
    }


def report_text(report: dict[str, Any]) -> str:
    lines = [
        f"- {r['status'].upper()} {r['rule']}: {r['value']}" + (f" ({r['detail']})" if r["detail"] else "")
        for r in report["rules"]
    ]
    return "\n".join([*lines, DEFAULTS_NOTE])


def summary_line(asset: dict[str, Any]) -> str:
    """One line for a render report."""
    try:
        report = measure_asset(asset)
    except Exception as e:  # noqa: BLE001 — a measurement never fails a render
        return f"pacing: not measured ({e})"
    warns = [r for r in report["rules"] if r["status"] == "warn"]
    if all(r["status"] == "unmeasured" for r in report["rules"][:3]):
        return "pacing: not measured"
    head = f"pacing: {len(report['rules']) - len(warns)}/{len(report['rules'])} defaults met"
    return head + (f"; warn: {', '.join(r['rule'] + ' ' + r['value'] for r in warns[:3])}" if warns else "")
