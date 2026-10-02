"""Montage — many short clips + title cards cut into ONE launch video, with colour-keyed transitions.

A montage is an ordered ``sequence`` of items::

    {"clip": 12, "in": 1.5, "out": 7.0, "speed": 1.5, "label": "Schedules", "theme_color": "#22c55e"}
    {"card": {"title": "Your agent. Your data. Your way.", "subtitle": "…"}, "duration": 2.5}

and an ``output`` mapping (preset, fit, fps, transition, limit, …; see ``OUTPUT_DEFAULTS``).

**The continuous-footage rule.** A clip item is ONE unbroken stretch of its take: ``in``/``out``
trim only its two ENDS and ``speed`` is one uniform factor (≤ 4×, the same jump-cut ceiling
``campaign_render`` holds a ramp to). There is no way to drop a chunk from inside a clip —
cut the take into two items (two beats) if it needs that.

**Rendering** is one ffmpeg ``filter_complex``:

* every item is normalised to one canvas — size, fps, square pixels, yuv420p, a common
  timebase — by letterboxing (``fit: letterbox``, bars in ``background``) or fill-cropping
  (``fit: crop``);
* cards are rendered by the card renderer (HTML → PNG in the out-of-process browser) and
  looped into N-second segments, optionally with a subtle zoom; with no browser, a plain
  ffmpeg ``drawtext`` card is the fallback;
* a lower-third ``label`` is drawn with ``drawtext`` in the brand font (resolved to a font
  file; a well-known sans is the fallback), or — on an ffmpeg built without drawtext — as a
  transparent HTML overlay; with neither, labels are skipped and the reply says so;
* transitions are ``xfade`` styles (fade, wipeleft, slideleft, smoothleft, circleopen, …),
  a hard ``cut``, or the signature **colour wipe**: a full-frame bar in the NEXT item's
  ``theme_color`` sweeps across, covering the outgoing shot, then sweeps on and reveals the
  incoming one. It's two hard-edged ``xfade`` wipes through a ``color`` source, so the edge
  stays crisp. A colour wipe of duration T covers the last T/2 of one item and the first T/2
  of the next and doesn't shorten the timeline; an ``xfade`` of T overlaps the two by T.

The graph renders once to a near-lossless master; the shipped mp4 (H.264 High, yuv420p,
``+faststart``) is encoded from it, stepping CRF and then a capped bitrate until it fits the
size ceiling. A poster PNG and (optionally) a GIF — kept only if it fits its limit — come from
the same master.

``storyboard`` renders a contact sheet instead (one frame per item, a swatch for each
transition) so the cut can be reviewed before the full render.

Everything that touches ffmpeg goes through an injectable ``runner(cmd, cwd)``, so the suite
tests validation and graph construction with no ffmpeg present.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from . import brand as brandmod
from . import deps, limits, render, store

Runner = Callable[[list[str], "str | None"], subprocess.CompletedProcess]

PRESETS: dict[str, tuple[int, int]] = {
    "landscape": (1920, 1080),  # 16:9 — a README, X/LinkedIn feed, YouTube
    "vertical": (1080, 1920),  # 9:16 — Shorts / Reels / TikTok
    "square": (1080, 1080),  # 1:1 — feeds that crop everything else
}
FITS = ("auto", "letterbox", "crop")
# The canonical canvas: beats recorded at a 1920×1080 viewport (device scale 1, full frame)
# pass straight through — no scaling, no letterbox — so every cut lines up pixel for pixel.
CANONICAL = (1920, 1080)
FOCUS_KEYS = {"x", "y", "w", "h"}
MIN_FOCUS = 64
# xfade styles that read well on UI footage (all present in ffmpeg ≥ 4.4).
XFADE_STYLES = (
    "fade", "fadeblack", "fadewhite", "dissolve",
    "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown",
    "smoothleft", "smoothright", "smoothup", "smoothdown",
    "circleopen", "circleclose", "circlecrop", "rectcrop",
    "horzopen", "horzclose", "vertopen", "vertclose", "radial", "pixelize",
)  # fmt: skip
# colour wipe → the xfade wipe each half uses (xfade's wiperight grows the NEW frame from the left).
COLOR_WIPES = {
    "colorwipe": "wiperight",  # the bar sweeps left → right
    "colorwipe_left": "wipeleft",  # right → left
    "colorwipe_up": "wipeup",  # bottom → top
    "colorwipe_down": "wipedown",  # top → bottom
}
TRANSITIONS = ("cut", *COLOR_WIPES, *XFADE_STYLES)

CLIP_KEYS = {"clip", "in", "out", "speed", "label", "theme_color", "transition", "fit", "focus"}
CARD_ITEM_KEYS = {"card", "duration", "theme_color", "transition", "zoom"}
CARD_KEYS = {"title", "subtitle", "eyebrow", "url", "cta", "bg", "fg", "accent", "logo"}
TRANSITION_KEYS = {"style", "duration"}
OUTPUT_DEFAULTS: dict[str, Any] = {
    "name": "montage",
    "title": "",
    "preset": "landscape",  # landscape | vertical | square
    "size": "",  # WxH — overrides the preset (even numbers, 320..3840)
    "fit": "auto",  # auto | letterbox | crop — how a clip that isn't the canvas's shape meets it
    "fps": 30,
    "background": "",  # letterbox bars; blank = the brand background
    "transition": "colorwipe",  # the default between items (an item's own `transition` wins)
    "transition_duration": 0.45,
    "limit": "",  # a hard-limit id (campaign_limits) the mp4 must meet
    "max_bytes": None,  # and/or an explicit ceiling
    "crf": 20,
    "poster": True,
    "poster_at": None,  # seconds into the montage; default: the middle of the first clip
    "gif": False,  # also try a GIF — kept only if it fits gif_limit
    "gif_limit": "github_attachment_image",
    "gif_width": 720,
    "label_font": "",  # a font FILE for labels; blank = the brand heading font, resolved
    "card_engine": "auto",  # auto | html | ffmpeg
    "label_engine": "auto",  # auto | drawtext | html | none
}
CARD_ENGINES = ("auto", "html", "ffmpeg")
LABEL_ENGINES = ("auto", "drawtext", "html", "none")

MAX_ITEMS = 40
MAX_TOTAL_S = 600.0
MIN_SPEED, MAX_SPEED = 0.25, 4.0  # 4× = campaign_render's jump-cut ceiling for a continuous clip
MIN_ITEM_S = 0.4
MIN_VISIBLE_S = 0.2  # of an item, after its incoming + outgoing transitions take their share
CARD_S = (0.5, 15.0)
TRANSITION_S = (0.1, 2.0)
ZOOM = 0.04  # a card's "subtle zoom": 4 % over its duration
MASTER_CRF = 12
FFMPEG_TIMEOUT_S = 1800
OUTPUT_BUDGET_S = 2400
_HEX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_SIZE = re.compile(r"^(\d{3,4})x(\d{3,4})$")


class MontageError(ValueError):
    """Validation failed. ``problems`` lists EVERY problem found, each with its item number."""

    def __init__(self, problems: list[str] | str):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems))


def _default_runner(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_S, cwd=cwd)


def _ff(runner: Runner, cmd: list[str], cwd: str | None = None) -> None:
    try:
        res = runner(cmd, cwd)
    except subprocess.TimeoutExpired:
        raise render.RenderError(f"ffmpeg ran past its {FFMPEG_TIMEOUT_S}s limit and was stopped") from None
    except OSError as e:
        raise render.RenderError(f"couldn't run ffmpeg: {e}") from None
    if res.returncode != 0:
        lines = (res.stderr or "").strip().splitlines()
        # The cause is usually near the TOP (the filter that refused); the bottom is the fallout.
        tail = "\n".join(lines if len(lines) <= 8 else [*lines[:4], "…", *lines[-3:]])
        raise render.RenderError(f"ffmpeg failed ({res.returncode}): {tail or 'no output'}")


def _fmt(t: float) -> str:
    return render._fmt(round(float(t), 4))


# ── colours ──────────────────────────────────────────────────────────────────
def hex_color(value: Any, what: str) -> str:
    """'#abc' / 'abc' / '#aabbcc' → '#aabbcc'; anything else is a loud error."""
    m = _HEX.match(str(value or "").strip())
    if not m:
        raise MontageError(f"{what}: {value!r} isn't a hex colour like #22c55e")
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h.lower()


def ff_color(hex6: str, alpha: float | None = None) -> str:
    """'#aabbcc' → '0xaabbcc' (ffmpeg's colour syntax; '#' is fine too, but 0x needs no quoting)."""
    out = "0x" + hex6.lstrip("#")
    return f"{out}@{alpha:g}" if alpha is not None else out


# ── validation (pure) ────────────────────────────────────────────────────────
def _num(value: Any, what: str, problems: list[str], lo: float | None = None, hi: float | None = None):
    if isinstance(value, bool):
        problems.append(f"{what} must be a number, got {value!r}")
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        problems.append(f"{what} must be a number, got {value!r}")
        return None
    if v != v or v in (float("inf"), float("-inf")):
        problems.append(f"{what} must be a finite number")
        return None
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        problems.append(f"{what} {v:g} is outside {lo:g}..{hi:g}")
        return None
    return v


def _unknown(obj: dict, allowed: set[str], what: str, problems: list[str]) -> None:
    extra = sorted(set(obj) - allowed)
    if extra:
        problems.append(f"{what}: unknown key(s) {', '.join(extra)} — allowed: {', '.join(sorted(allowed))}")


def _transition(value: Any, what: str, problems: list[str]) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = {"style": value}
    if not isinstance(value, dict):
        problems.append(f"{what}: a transition is a style name or {{style, duration}}")
        return None
    _unknown(value, TRANSITION_KEYS, what, problems)
    style = str(value.get("style") or "").strip().lower()
    if style not in TRANSITIONS:
        problems.append(f"{what}: unknown transition {value.get('style')!r} — use one of {', '.join(TRANSITIONS)}")
        return None
    out: dict[str, Any] = {"style": style}
    if value.get("duration") is not None:
        d = _num(value["duration"], f"{what}.duration", problems, *TRANSITION_S)
        if d is not None:
            out["duration"] = d
    return out


def normalize_output(output: Any) -> dict[str, Any]:
    """The montage-wide options, validated. A string is shorthand for a preset or WxH."""
    if output in (None, ""):
        output = {}
    if isinstance(output, str):
        text = output.strip().lower()
        output = {"size": text} if _SIZE.match(text) else {"preset": text}
    if not isinstance(output, dict):
        raise MontageError("output must be a mapping of montage options (or a preset name)")
    problems: list[str] = []
    _unknown(output, set(OUTPUT_DEFAULTS), "output", problems)
    o = {**OUTPUT_DEFAULTS, **{k: v for k, v in output.items() if v is not None}}
    name = str(o["name"] or "")
    if not render._NAME_RE.match(name):
        problems.append(f"output.name {name!r} must be short: letters, digits, - _ .")
    preset = str(o["preset"] or "").lower()
    if o.get("size"):
        m = _SIZE.match(str(o["size"]).strip().lower())
        if not m:
            problems.append(f"output.size must look like 1280x720, got {o['size']!r}")
            w, h = PRESETS["landscape"]
        else:
            w, h = int(m.group(1)), int(m.group(2))
            if not (320 <= w <= 3840 and 320 <= h <= 3840) or w % 2 or h % 2:
                problems.append("output.size: each side must be an even number of pixels, 320..3840")
        preset = "custom"
    elif preset in PRESETS:
        w, h = PRESETS[preset]
    else:
        problems.append(f"output.preset must be one of {', '.join(PRESETS)} (or give size: WxH), got {o['preset']!r}")
        w, h = PRESETS["landscape"]
    fit = str(o["fit"] or "").lower()
    if fit not in FITS:
        problems.append(f"output.fit must be one of {', '.join(FITS)}, got {o['fit']!r}")
    fps = _num(o["fps"], "output.fps", problems, 10, render.MAX_FPS) or 30
    background = ""
    if o.get("background"):
        try:
            background = hex_color(o["background"], "output.background")
        except MontageError as e:
            problems += e.problems
    default_tr = _transition(
        {"style": o["transition"], "duration": o["transition_duration"]}, "output.transition", problems
    ) or {"style": "colorwipe", "duration": 0.45}
    crf = _num(o["crf"], "output.crf", problems, 0, 40)
    max_bytes = None
    if o.get("max_bytes") not in (None, "", 0):
        mb = _num(o["max_bytes"], "output.max_bytes", problems, 1000, 10**12)
        max_bytes = int(mb) if mb else None
    limit_id = str(o.get("limit") or "")
    row = limits.get(limit_id) if limit_id else None
    if limit_id and row is None:
        problems.append(f"output.limit: unknown limit {limit_id!r} — campaign_limits lists them")
    if row and row.get("max_bytes"):
        max_bytes = min(max_bytes, int(row["max_bytes"])) if max_bytes else int(row["max_bytes"])
    gif_limit = str(o.get("gif_limit") or "")
    if o.get("gif") and gif_limit and limits.get(gif_limit) is None:
        problems.append(f"output.gif_limit: unknown limit {gif_limit!r}")
    poster_at = None
    if o.get("poster_at") not in (None, ""):
        poster_at = _num(o["poster_at"], "output.poster_at", problems, 0, MAX_TOTAL_S)
    gif_width = _num(o["gif_width"], "output.gif_width", problems, 160, 1920) or 720
    for key, allowed in (("card_engine", CARD_ENGINES), ("label_engine", LABEL_ENGINES)):
        if str(o[key]).lower() not in allowed:
            problems.append(f"output.{key} must be one of {', '.join(allowed)}")
    label_font = str(o.get("label_font") or "").strip()
    if label_font and not Path(label_font).expanduser().is_file():
        problems.append(f"output.label_font {label_font} isn't a font file on this machine")
    for key in ("poster", "gif"):
        if not isinstance(o[key], bool):
            problems.append(f"output.{key} must be true or false")
    if problems:
        raise MontageError(problems)
    return {
        "name": name,
        "title": str(o.get("title") or ""),
        "preset": preset,
        "width": w,
        "height": h,
        "orientation": limits.orientation(w, h),
        "fit": fit,
        "fps": fps,
        "background": background,
        "transition": default_tr,
        "crf": int(crf if crf is not None else 20),
        "max_bytes": max_bytes,
        "limit": limit_id,
        "poster": bool(o["poster"]),
        "poster_at": poster_at,
        "gif": bool(o["gif"]),
        "gif_limit": gif_limit,
        "gif_width": int(gif_width) // 2 * 2,
        "label_font": str(Path(label_font).expanduser()) if label_font else "",
        "card_engine": str(o["card_engine"]).lower(),
        "label_engine": str(o["label_engine"]).lower(),
    }


def parse_sequence(sequence: Any) -> list[dict[str, Any]]:
    """Shape-check every item (no store, no ffmpeg). Raises with EVERY problem found."""
    if not isinstance(sequence, list) or not sequence:
        raise MontageError("sequence must be a non-empty list of {clip: …} and {card: …} items")
    problems: list[str] = []
    if len(sequence) > MAX_ITEMS:
        problems.append(f"{len(sequence)} items is too many for one montage — at most {MAX_ITEMS}")
    items: list[dict[str, Any]] = []
    for n, raw in enumerate(sequence, start=1):
        what = f"item {n}"
        if not isinstance(raw, dict):
            problems.append(f"{what}: each item is a mapping with `clip` or `card`, got {raw!r}")
            continue
        if ("clip" in raw) == ("card" in raw):
            problems.append(f"{what}: give exactly one of `clip` (an asset id) or `card` (a mapping)")
            continue
        theme = None
        if raw.get("theme_color") not in (None, ""):
            try:
                theme = hex_color(raw["theme_color"], f"{what}.theme_color")
            except MontageError as e:
                problems += e.problems
        tr = _transition(raw.get("transition"), f"{what}.transition", problems)
        if n == 1 and tr is not None:
            problems.append(f"{what}: has a transition, but nothing comes before it — the first item opens cold")
        if "clip" in raw:
            _unknown(raw, CLIP_KEYS, what, problems)
            try:
                asset_id = int(raw["clip"])
                if asset_id <= 0 or isinstance(raw["clip"], bool):
                    raise ValueError
            except (TypeError, ValueError):
                problems.append(f"{what}.clip must be an asset id (from campaign_assets), got {raw['clip']!r}")
                continue
            t_in = _num(raw.get("in", 0) or 0, f"{what}.in", problems, 0, 36_000)
            t_out = None
            if raw.get("out") not in (None, ""):
                t_out = _num(raw["out"], f"{what}.out", problems, 0, 36_000)
            speed_raw = raw.get("speed", 1)
            if isinstance(speed_raw, (list, dict)):
                problems.append(
                    f"{what}.speed: a montage clip plays ONE continuous stretch at ONE speed — no ramps or "
                    "interior cuts. Split it into two items, or ramp it with campaign_render and montage that"
                )
                speed = None
            else:
                speed = _num(speed_raw if speed_raw not in (None, "") else 1, f"{what}.speed", problems)
                if speed is not None and not MIN_SPEED <= speed <= MAX_SPEED:
                    problems.append(
                        f"{what}.speed {speed:g} is outside {MIN_SPEED:g}..{MAX_SPEED:g} — faster than "
                        f"{MAX_SPEED:g}× reads as a jump cut; pick a shorter in/out instead"
                    )
                    speed = None
            if t_in is not None and t_out is not None and t_out <= t_in:
                problems.append(f"{what}: out ({t_out:g}s) must be after in ({t_in:g}s)")
            fit = None
            if raw.get("fit") not in (None, ""):
                fit = str(raw["fit"]).lower()
                if fit not in FITS:
                    problems.append(f"{what}.fit must be one of {', '.join(FITS)}")
            focus = None
            if raw.get("focus") is not None:
                f = raw["focus"]
                if not isinstance(f, dict):
                    problems.append(f"{what}.focus must be {{x, y, w, h}} in source pixels")
                else:
                    _unknown(f, FOCUS_KEYS, f"{what}.focus", problems)
                    vals = {k: _num(f.get(k), f"{what}.focus.{k}", problems, 0, 16_384) for k in ("x", "y", "w", "h")}
                    if all(v is not None for v in vals.values()):
                        if vals["w"] < MIN_FOCUS or vals["h"] < MIN_FOCUS:
                            problems.append(
                                f"{what}.focus is {vals['w']:g}×{vals['h']:g} — at least {MIN_FOCUS}px a side"
                            )
                        else:
                            focus = {k: int(round(v)) for k, v in vals.items()}
            label = str(raw.get("label") or "").strip()
            if len(label) > 60:
                problems.append(
                    f"{what}.label is {len(label)} characters — a lower third reads at ≤ 60 (aim for 2–4 words)"
                )
            if "\n" in label:
                problems.append(f"{what}.label must be one line")
            items.append(
                {
                    "kind": "clip",
                    "n": n,
                    "asset_id": asset_id,
                    "in": t_in or 0.0,
                    "out": t_out,
                    "speed": speed or 1.0,
                    "label": label,
                    "theme_color": theme,
                    "transition": tr,
                    "fit": fit,
                    "focus": focus,
                }
            )
        else:
            _unknown(raw, CARD_ITEM_KEYS, what, problems)
            card = raw.get("card")
            if not isinstance(card, dict):
                problems.append(f"{what}.card must be a mapping {{title, subtitle?, bg?, fg?, accent?, logo?}}")
                continue
            _unknown(card, CARD_KEYS, f"{what}.card", problems)
            if not str(card.get("title") or "").strip():
                problems.append(f"{what}.card needs a title")
            colors: dict[str, str] = {}
            for key in ("bg", "fg", "accent"):
                if card.get(key) not in (None, ""):
                    try:
                        colors[key] = hex_color(card[key], f"{what}.card.{key}")
                    except MontageError as e:
                        problems += e.problems
            logo = card.get("logo", False)
            if not isinstance(logo, bool):
                problems.append(f"{what}.card.logo must be true or false")
            dur = _num(raw.get("duration"), f"{what}.duration", problems, *CARD_S) if "duration" in raw else None
            if "duration" not in raw:
                problems.append(f"{what}: a card needs duration (seconds on screen, {CARD_S[0]:g}..{CARD_S[1]:g})")
            zoom = raw.get("zoom", False)
            if not isinstance(zoom, bool):
                problems.append(f"{what}.zoom must be true or false")
            items.append(
                {
                    "kind": "card",
                    "n": n,
                    "card": {
                        **{k: str(card.get(k) or "").strip() for k in ("title", "subtitle", "eyebrow", "url", "cta")},
                        "colors": colors,
                        "logo": bool(logo),
                    },
                    "duration": dur or 0.0,
                    "zoom": bool(zoom),
                    "theme_color": theme,
                    "transition": tr,
                }
            )
    if problems:
        raise MontageError(problems)
    return items


def resolve_items(
    campaign_id: int, items: list[dict[str, Any]], probe: Callable[[str], dict[str, Any]] | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Attach each clip's file and real duration; check it belongs to THIS campaign.

    Returns (items, warnings). Missing/foreign/rejected/fileless clips and in/out past the end
    are errors (all of them, at once); an input that isn't approved yet is a WARNING, and the
    item carries ``unapproved`` so the montage can be marked as built from draft inputs."""
    probe = probe or render.probe
    problems: list[str] = []
    warnings: list[str] = []
    out: list[dict[str, Any]] = []
    for it in items:
        it = dict(it)
        if it["kind"] == "card":
            it["length"] = it["duration"]
            out.append(it)
            continue
        what = f"item {it['n']} (clip #{it['asset_id']})"
        a = store.get_asset(it["asset_id"])
        if a is None or a["campaign_id"] != int(campaign_id):
            problems.append(f"{what}: no such asset in campaign {campaign_id} — campaign_assets lists them")
            continue
        if a["kind"] not in ("clip", "gif"):
            problems.append(f"{what}: is a {a['kind']} — a montage clip must be a clip (a take or a rendered mp4)")
            continue
        if a["status"] == "rejected":
            problems.append(f"{what}: the operator REJECTED it{(': ' + a['review_note']) if a['review_note'] else ''}")
            continue
        p = Path(str(a.get("path") or ""))
        if not a.get("path") or not p.is_file():
            problems.append(f"{what}: has no file ({a['status']}) — shoot or render it first")
            continue
        try:
            info = probe(str(p))
        except render.RenderError as e:
            problems.append(f"{what}: {e}")
            continue
        dur = float(info.get("duration_s") or 0)
        t_out = it["out"] if it["out"] is not None else dur
        if dur and it["in"] >= dur:
            problems.append(f"{what}: in {it['in']:g}s is past the end of the {dur:.2f}s file")
            continue
        if dur and t_out > dur + 0.05:
            problems.append(f"{what}: out {t_out:g}s is past the end of the {dur:.2f}s file")
            continue
        t_out = min(t_out, dur) if dur else t_out
        length = (t_out - it["in"]) / it["speed"]
        if length < MIN_ITEM_S:
            problems.append(f"{what}: plays for {length:.2f}s — at least {MIN_ITEM_S:g}s (one idea needs time to land)")
            continue
        it.update(
            out=t_out,
            length=round(length, 4),
            path=str(p),
            status=a["status"],
            title=a["title"],
            src_width=int(info.get("width") or 0),
            src_height=int(info.get("height") or 0),
            unapproved=a["status"] != "approved",
        )
        if it["unapproved"]:
            warnings.append(
                f"{what} '{a['title']}' is {a['status']}, not approved — the montage is a DRAFT until it is"
            )
        out.append(it)
    if problems:
        raise MontageError(problems)
    return out, warnings


# ── the timeline ─────────────────────────────────────────────────────────────
def _share(tr: dict[str, Any]) -> float:
    """How much of EACH neighbouring item a transition covers."""
    if tr["style"] == "cut":
        return 0.0
    return tr["duration"] / 2 if tr["style"] in COLOR_WIPES else tr["duration"]


def plan(items: list[dict[str, Any]], opts: dict[str, Any], look: dict[str, Any]) -> dict[str, Any]:
    """Resolve every transition (style, duration, colour) and lay out the timeline.

    ``items`` need ``length`` (resolved). Returns {items, transitions, starts, total}; each
    transition is {into: index, style, duration, color, at} where ``at`` is the moment the
    swap happens (a colour wipe's fully-covered instant; an xfade's midpoint)."""
    problems: list[str] = []
    trs: list[dict[str, Any] | None] = [None]
    accent = look["colors"]["accent"]
    for k in range(1, len(items)):
        it = items[k]
        tr = dict(opts["transition"])
        if it.get("transition"):
            tr.update(it["transition"])
            if "duration" not in it["transition"]:
                tr["duration"] = opts["transition"]["duration"]
        tr["color"] = it.get("theme_color") or (it.get("card", {}).get("colors", {}).get("accent")) or accent
        tr["into"] = k
        trs.append(tr)
    for k, it in enumerate(items):
        head = _share(trs[k]) if trs[k] else 0.0
        tail = _share(trs[k + 1]) if k + 1 < len(items) and trs[k + 1] else 0.0
        if head + tail > it["length"] - MIN_VISIBLE_S:
            problems.append(
                f"item {it['n']}: plays {it['length']:.2f}s but its transitions take {head + tail:.2f}s of it — "
                "lengthen it, shorten the transitions, or use cut"
            )
    starts: list[float] = []
    acc = 0.0
    for k, it in enumerate(items):
        tr = trs[k]
        if tr is None or tr["style"] == "cut" or tr["style"] in COLOR_WIPES:
            start = acc
        else:
            start = acc - tr["duration"]
        if tr is not None:
            tr["at"] = round(start + (tr["duration"] / 2 if tr["style"] in XFADE_STYLES else 0.0), 4)
        starts.append(round(start, 4))
        acc = start + it["length"]
    total = round(acc, 4)
    if total > MAX_TOTAL_S:
        problems.append(f"the montage runs {total:.1f}s — at most {MAX_TOTAL_S:g}s in one render")
    row = limits.get(opts["limit"]) if opts.get("limit") else None
    if row:
        for v in limits.check(opts["limit"], size=None, width=opts["width"], height=opts["height"], duration=total):
            problems.append(f"output.limit {opts['limit']}: {v}")
    if problems:
        raise MontageError(problems)
    return {"items": items, "transitions": [t for t in trs if t], "starts": starts, "total": total}


# ── the filter graph ─────────────────────────────────────────────────────────
def _even(v: float) -> int:
    return max(2, int(v) // 2 * 2)


def center_rect(src_w: int, src_h: int, aspect: float) -> tuple[int, int, int, int]:
    """The largest rect of ``aspect`` centred in the source: (x, y, w, h)."""
    if src_w / src_h > aspect:
        w, h = _even(src_h * aspect), _even(src_h)
    else:
        w, h = _even(src_w), _even(src_w / aspect)
    return (src_w - w) // 2, (src_h - h) // 2, w, h


def focus_rect(focus: dict[str, int], src_w: int, src_h: int, aspect: float) -> tuple[int, int, int, int]:
    """Grow the focus area to the canvas's aspect around its centre, kept inside the source.

    The focus area itself is always inside the result; if the source can't supply enough
    pixels on one axis, the rect stops at the source edge (the remainder is letterboxed)."""
    fx, fy, fw, fh = focus["x"], focus["y"], focus["w"], focus["h"]
    if fw / fh < aspect:
        w, h = fh * aspect, fh
    else:
        w, h = fw, fw / aspect
    w, h = _even(min(w, src_w)), _even(min(h, src_h))
    cx, cy = fx + fw / 2, fy + fh / 2
    x = int(round(min(max(0, cx - w / 2), src_w - w)))
    y = int(round(min(max(0, cy - h / 2), src_h - h)))
    return x, y, w, h


def frame_plan(it: dict[str, Any], opts: dict[str, Any]) -> dict[str, Any]:
    """How one clip meets the canvas: ``pass`` (already the canvas — untouched), ``focus``
    (its focus area, grown to the canvas aspect), ``crop`` (a centred canvas-aspect crop),
    or ``letterbox`` (scaled to fit, bars in the background colour)."""
    W, H = opts["width"], opts["height"]
    sw, sh = int(it.get("src_width") or 0), int(it.get("src_height") or 0)
    if it.get("focus") and sw and sh:
        return {"mode": "focus", "rect": focus_rect(it["focus"], sw, sh, W / H)}
    if (sw, sh) == (W, H):
        return {"mode": "pass", "rect": None}
    fit = it.get("fit") or opts["fit"]
    if fit == "auto":
        # A landscape cut letterboxes a stray shape; a vertical/square cut of landscape
        # footage takes the centre (pass `focus` to choose the area instead).
        fit = "letterbox" if opts["orientation"] == "landscape" else "crop"
    if fit == "crop" and sw and sh:
        return {"mode": "crop", "rect": center_rect(sw, sh, W / H)}
    return {"mode": "letterbox", "rect": None}


def framing(items: list[dict[str, Any]], opts: dict[str, Any]) -> list[str]:
    """Decide each clip's frame plan; validate focus areas. Returns warnings that NAME every
    clip that isn't on the canonical canvas (it gets scaled, and letterboxed if its shape differs)."""
    problems: list[str] = []
    warnings: list[str] = []
    for it in items:
        if it["kind"] != "clip":
            continue
        what = f"item {it['n']} (clip #{it['asset_id']} '{it.get('title', '')}')"
        sw, sh = int(it.get("src_width") or 0), int(it.get("src_height") or 0)
        f = it.get("focus")
        if f:
            if not (sw and sh):
                problems.append(f"{what}: its size couldn't be read, so focus can't be checked")
                continue
            if f["x"] + f["w"] > sw or f["y"] + f["h"] > sh:
                problems.append(
                    f"{what}: focus {f['w']}×{f['h']} at ({f['x']}, {f['y']}) runs outside the {sw}×{sh} source"
                )
                continue
        it["frame"] = frame_plan(it, opts)
        if sw and sh and (sw, sh) != CANONICAL and it["frame"]["mode"] != "pass":
            how = {"letterbox": "letterboxed", "crop": "centre-cropped", "focus": "cropped to its focus"}.get(
                it["frame"]["mode"], "passed through"
            )
            warnings.append(
                f"{what} is {sw}×{sh}, not the canonical {CANONICAL[0]}×{CANONICAL[1]} — {how} and scaled; "
                "re-record it at a 1920×1080 viewport, device_scale_factor 1, full frame"
            )
    if problems:
        raise MontageError(problems)
    return warnings


def frame_chain(frame: dict[str, Any] | None, w: int, h: int, bg: str) -> str:
    """The crop/scale/pad that puts a clip on a ``w``×``h`` canvas, per its frame plan."""
    frame = frame or {"mode": "letterbox", "rect": None}
    if frame["mode"] == "pass":
        return "null"
    parts = []
    rect = frame.get("rect")
    if rect:
        x, y, rw, rh = rect
        parts.append(f"crop={rw}:{rh}:{x}:{y}")
        if abs(rw / rh - w / h) < 0.004:
            parts.append(f"scale={w}:{h}:flags=lanczos")
            return ",".join(parts)
    parts.append(fit_chain("letterbox", w, h, bg))
    return ",".join(parts)


def fit_chain(fit: str, w: int, h: int, bg: str) -> str:
    if fit == "crop":
        return f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h}"
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={ff_color(bg)}"
    )


def label_geometry(w: int, h: int) -> dict[str, int]:
    """Where a lower third sits. Vertical keeps clear of the platform UI along the bottom."""
    short = min(w, h)
    size = max(14, round(short * (0.034 if w >= h else 0.042)))
    pad = max(6, round(size * 0.55))
    margin = max(12, round(short * 0.06))
    bottom = round(h * 0.22) if h > w else margin  # Shorts/Reels overlay their caption + buttons down there
    y = h - bottom - size - pad
    return {
        "size": size,
        "pad": pad,
        "x": margin + max(4, round(size * 0.18)) + pad,
        "y": y,
        "bar": max(4, round(size * 0.18)),
    }


def drawtext_label(
    textfile: str, fontfile: str, w: int, h: int, colors: dict[str, str], bar: str, length: float
) -> str:
    """A lower third: an accent bar + the label on a translucent brand-bg box. File names are
    RELATIVE (ffmpeg runs in the work dir), so nothing in them needs filtergraph escaping."""
    g = label_geometry(w, h)
    on = f"enable='between(t,{_fmt(min(0.2, length / 4))},{_fmt(max(length - 0.2, length * 3 / 4))})'"
    bar_x = g["x"] - g["pad"] - g["bar"]
    return (
        f"drawbox=x={bar_x}:y={g['y'] - g['pad']}:w={g['bar']}:h={g['size'] + 2 * g['pad']}:"
        f"color={ff_color(bar)}:t=fill:{on},"
        f"drawtext=fontfile={fontfile}:textfile={textfile}:expansion=none:fontsize={g['size']}:"
        f"fontcolor={ff_color(colors['fg'])}:x={g['x']}:y={g['y']}:box=1:boxcolor={ff_color(colors['bg'], 0.82)}:"
        f"boxborderw={g['pad']}:{on}"
    )


def build_graph(
    timeline: dict[str, Any], opts: dict[str, Any], look: dict[str, Any], media: dict[int, dict[str, str]]
) -> tuple[list[str], str]:
    """The ffmpeg input args and filter_complex for the whole montage → ``[out]``.

    ``media[k]`` holds what item k needs on disk: ``card`` (a PNG) for a card; for a clip
    with a label, either ``label_text`` + ``font`` (drawtext) or ``label_png`` (an overlay)."""
    w, h, fps = opts["width"], opts["height"], opts["fps"]
    bg = opts["background"] or look["colors"]["bg"]
    args: list[str] = []
    parts: list[str] = []
    n_in = 0
    norm = "setsar=1,format=yuv420p,settb=AVTB"
    # Every stream that meets an xfade must DECLARE a constant frame rate (ffmpeg ≥ 7 refuses
    # 1/0 — what overlay, concat and trim chains can leave behind), on one common timebase.
    cfr = f"fps={_fmt(fps)},settb=AVTB"

    for k, it in enumerate(timeline["items"]):
        L = it["length"]
        m = media.get(k, {})
        if it["kind"] == "clip":
            src_len = it["out"] - it["in"]
            args += ["-ss", _fmt(it["in"]), "-t", _fmt(src_len + 0.1), "-i", it["path"]]
            idx, n_in = n_in, n_in + 1
            chain = (
                f"[{idx}:v]setpts=(PTS-STARTPTS)/{_fmt(it['speed'])},fps={_fmt(fps)},"
                f"{frame_chain(it.get('frame'), w, h, bg)},{norm},"
                # A file that ends a hair early holds its last frame, so every xfade offset is exact.
                f"tpad=stop_mode=clone:stop_duration=1,trim=duration={_fmt(L)},setpts=PTS-STARTPTS"
            )
            bar = it.get("theme_color") or look["colors"]["accent"]
            if m.get("label_text") and m.get("font"):
                chain += "," + drawtext_label(m["label_text"], m["font"], w, h, look["colors"], bar, L)
                parts.append(f"{chain},{cfr}[v{k}]")
            elif m.get("label_png"):
                args += ["-loop", "1", "-framerate", _fmt(fps), "-t", _fmt(L), "-i", m["label_png"]]
                lab, n_in = n_in, n_in + 1
                parts.append(f"{chain}[b{k}]")
                on = f"enable='between(t,{_fmt(min(0.2, L / 4))},{_fmt(max(L - 0.2, L * 3 / 4))})'"
                parts.append(f"[{lab}:v]format=rgba,setsar=1[l{k}]")
                parts.append(f"[b{k}][l{k}]overlay=0:0:eof_action=pass:{on},format=yuv420p,{cfr}[v{k}]")
            else:
                parts.append(f"{chain},{cfr}[v{k}]")
        else:
            png = m["card"]
            if it.get("zoom"):
                frames = max(1, round(L * fps))
                args += ["-loop", "1", "-framerate", _fmt(fps), "-t", _fmt(L), "-i", png]
                idx, n_in = n_in, n_in + 1
                parts.append(
                    f"[{idx}:v]scale={w * 2}:{h * 2}:flags=lanczos,"
                    f"zoompan=z='1+{ZOOM}*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:"
                    f"s={w}x{h}:fps={_fmt(fps)},{norm},trim=duration={_fmt(L)},setpts=PTS-STARTPTS,{cfr}[v{k}]"
                )
            else:
                args += ["-loop", "1", "-framerate", _fmt(fps), "-t", _fmt(L), "-i", png]
                idx, n_in = n_in, n_in + 1
                parts.append(
                    f"[{idx}:v]scale={w}:{h}:flags=lanczos,fps={_fmt(fps)},{norm},"
                    f"trim=duration={_fmt(L)},setpts=PTS-STARTPTS,{cfr}[v{k}]"
                )

    acc, acc_len = "[v0]", timeline["items"][0]["length"]
    trs = {t["into"]: t for t in timeline["transitions"]}
    for k in range(1, len(timeline["items"])):
        L = timeline["items"][k]["length"]
        tr = trs[k]
        nxt = f"[x{k}]"
        if tr["style"] == "cut":
            parts.append(f"{acc}[v{k}]concat=n=2:v=1:a=0,{cfr}{nxt}")
            acc_len += L
        elif tr["style"] in COLOR_WIPES:
            wipe, half = COLOR_WIPES[tr["style"]], tr["duration"] / 2
            parts.append(
                f"color=c={ff_color(tr['color'])}:s={w}x{h}:r={_fmt(fps)}:d={_fmt(tr['duration'])},{norm}[c{k}]"
            )
            # In: the bar covers the outgoing shot. Out: it sweeps on and reveals the incoming one.
            parts.append(
                f"{acc}[c{k}]xfade=transition={wipe}:duration={_fmt(half)}:offset={_fmt(acc_len - half)}[w{k}]"
            )
            parts.append(f"[w{k}][v{k}]xfade=transition={wipe}:duration={_fmt(half)}:offset={_fmt(acc_len)}{nxt}")
            acc_len += L
        else:
            d = tr["duration"]
            parts.append(
                f"{acc}[v{k}]xfade=transition={tr['style']}:duration={_fmt(d)}:offset={_fmt(acc_len - d)}{nxt}"
            )
            acc_len += L - d
        acc = nxt
    parts.append(f"{acc}format=yuv420p[out]")
    return args, ";".join(parts)


def master_cmd(ff: str, args: list[str], graph: str, dst: str, fps: float) -> list[str]:
    return [
        ff, "-y", "-hide_banner", "-loglevel", "error", *args, "-filter_complex", graph, "-map", "[out]",
        "-an", "-r", _fmt(fps), "-c:v", "libx264", "-preset", "veryfast", "-crf", str(MASTER_CRF),
        "-pix_fmt", "yuv420p", dst,
    ]  # fmt: skip


def encode_cmd(ff: str, src: str, dst: str, *, crf: int, bitrate: int | None = None) -> list[str]:
    """The shipped mp4: H.264 High, yuv420p, square pixels, moov up front."""
    cmd = [
        ff, "-y", "-hide_banner", "-loglevel", "error", "-i", src, "-an", "-c:v", "libx264",
        "-preset", "slow", "-profile:v", "high", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
    ]  # fmt: skip
    if bitrate:
        cmd += ["-b:v", str(bitrate), "-maxrate", str(bitrate), "-bufsize", str(bitrate * 2)]
    else:
        cmd += ["-crf", str(crf)]
    return cmd + [dst]


def encode_ladder(crf: int, max_bytes: int | None, duration: float) -> list[dict[str, int]]:
    """Quality first (CRF), then a capped average bitrate that lands under the ceiling."""
    if not max_bytes:
        return [{"crf": crf}]
    steps = [{"crf": crf}, {"crf": min(crf + 4, 40)}, {"crf": min(crf + 8, 40)}]
    dur = max(duration, 0.5)
    for frac in (0.9, 0.75, 0.6):
        steps.append({"crf": 0, "bitrate": max(100_000, int(max_bytes * 8 * frac / dur))})
    return steps


# ── fonts + text engines ─────────────────────────────────────────────────────
_FILTERS: dict[str, set[str]] = {}

FONT_DIRS = (
    "~/Library/Fonts", "/Library/Fonts", "/System/Library/Fonts", "/System/Library/Fonts/Supplemental",
    "/usr/share/fonts", "/usr/local/share/fonts", "~/.fonts", "~/.local/share/fonts", "C:/Windows/Fonts",
)  # fmt: skip
FALLBACK_FONTS = (
    "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "NotoSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf",
    "Helvetica.ttc", "HelveticaNeue.ttc", "SFNS.ttf", "DejaVuSans.ttf", "Arial.ttf", "arial.ttf",
)  # fmt: skip
FONT_EXT = (".ttf", ".otf", ".ttc")


def ffmpeg_filters(ff: str, runner: Runner | None = None) -> set[str]:
    """The filter names this ffmpeg build has (cached per binary)."""
    if ff in _FILTERS:
        return _FILTERS[ff]
    runner = runner or _default_runner
    names: set[str] = set()
    try:
        res = runner([ff, "-hide_banner", "-filters"], None)
        for line in (res.stdout or "").splitlines():
            bits = line.split()
            if len(bits) >= 3 and re.fullmatch(r"[.A-Z|]{2,3}", bits[0]):
                names.add(bits[1])
    except Exception:  # noqa: BLE001 — unknown build: assume nothing optional
        pass
    _FILTERS[ff] = names
    return names


def _font_files() -> list[Path]:
    out: list[Path] = []
    for d in FONT_DIRS:
        root = Path(d).expanduser()
        if root.is_dir():
            try:
                out += [p for p in root.rglob("*") if p.suffix.lower() in FONT_EXT]
            except OSError:
                continue
    return out


def find_font(family: str = "", explicit: str = "") -> tuple[str, str]:
    """A font FILE for drawtext. Returns (path, how) — path '' when nothing usable exists.

    Order: an explicit file → the brand family (a path, or a file whose name matches it; bold
    weights preferred, a lower third is set bold) → a well-known sans."""
    if explicit and Path(explicit).is_file():
        return explicit, "label_font"
    fam = (family or "").strip()
    if fam and Path(fam).expanduser().is_file():
        return str(Path(fam).expanduser()), "brand font file"
    files = _font_files()
    if fam:
        key = re.sub(r"[^a-z0-9]", "", fam.lower())
        hits = [p for p in files if re.sub(r"[^a-z0-9]", "", p.stem.lower()).startswith(key)]
        if hits:

            def rank(p: Path) -> tuple[int, int]:
                s = p.stem.lower()
                weight = 0 if "semibold" in s else 1 if "bold" in s and "extra" not in s else 2 if "medium" in s else 3
                return (weight + (5 if "italic" in s or "oblique" in s else 0), len(s))

            return str(sorted(hits, key=rank)[0]), f"brand font {fam!r}"
    by_name = {p.name.lower(): p for p in files}
    for name in FALLBACK_FONTS:
        if name.lower() in by_name:
            how = f"fallback {name}" + (f" (brand font {fam!r} isn't installed as a file)" if fam else "")
            return str(by_name[name.lower()]), how
    return "", "no font file found"


def wrap(text: str, max_chars: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for wd in words:
        if cur and len(cur) + 1 + len(wd) > max_chars:
            lines.append(cur)
            cur = wd
        else:
            cur = f"{cur} {wd}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def card_data(card: dict[str, Any]) -> dict[str, Any]:
    """A montage card → the card renderer's ``data`` (title-slide template)."""
    data: dict[str, Any] = {k: card.get(k, "") for k in ("title", "subtitle", "eyebrow", "url")}
    data["footer"] = card.get("cta", "")
    if card.get("colors"):
        data["colors"] = dict(card["colors"])
    if not card.get("logo"):
        data["logo"] = False
    return data


def card_look(card: dict[str, Any], look: dict[str, Any]) -> dict[str, str]:
    colors = dict(look["colors"])
    colors.update(card.get("colors") or {})
    return colors


def ffmpeg_card_cmd(
    ff: str, card: dict[str, Any], colors: dict[str, str], w: int, h: int, font: str, dst: str, workdir: Path
) -> list[str]:
    """The no-browser card: brand bg, an accent rule, title/subtitle/url/CTA via drawtext."""
    short = min(w, h)
    title_px = round(short * 0.085)
    sub_px = round(short * 0.036)
    small_px = round(short * 0.028)
    max_chars = max(8, int(w * 0.84 / (title_px * 0.55)))
    title_lines = wrap(card["title"], max_chars)
    block = len(title_lines) * title_px * 1.15
    top = h / 2 - block / 2 - (sub_px if card.get("subtitle") else 0)
    filters = [
        f"drawbox=x=(iw-{round(short * 0.08)})/2:y={round(top - title_px * 0.9)}:w={round(short * 0.08)}:h={max(3, round(short * 0.006))}:color={ff_color(colors['accent'])}:t=fill"
    ]

    def text(name: str, value: str, px: int, y: float, color: str) -> None:
        (workdir / name).write_text(value, encoding="utf-8")
        filters.append(
            f"drawtext=fontfile={font}:textfile={name}:expansion=none:fontsize={px}:fontcolor={ff_color(color)}:"
            f"x=(w-text_w)/2:y={round(y)}"
        )

    for i, line in enumerate(title_lines):
        text(f"{Path(dst).stem}-t{i}.txt", line, title_px, top + i * title_px * 1.15, colors["fg"])
    y = top + block + sub_px * 0.8
    if card.get("subtitle"):
        for i, line in enumerate(wrap(card["subtitle"], max(12, int(w * 0.8 / (sub_px * 0.52))))[:3]):
            text(f"{Path(dst).stem}-s{i}.txt", line, sub_px, y + i * sub_px * 1.3, colors.get("muted") or colors["fg"])
    if card.get("url"):
        text(f"{Path(dst).stem}-u.txt", card["url"], small_px, h * 0.74, colors["fg"])
    if card.get("cta"):
        text(f"{Path(dst).stem}-c.txt", card["cta"], small_px, h * 0.84, colors["accent"])
    return [
        ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", f"color=c={ff_color(colors['bg'])}:s={w}x{h}:d=1", "-vf", ",".join(filters),
        "-frames:v", "1", "-update", "1", dst,
    ]  # fmt: skip


def label_html(label: str, w: int, h: int, look: dict[str, Any], bar: str) -> str:
    """The transparent overlay page for a lower third when ffmpeg has no drawtext."""
    import html as _html

    g = label_geometry(w, h)
    c = look["colors"]
    font = re.sub(r"[^A-Za-z0-9 \-_]", "", look["fonts"].get("heading") or "")
    stack = (
        f'"{font}", ' if font else ""
    ) + 'Inter, "SF Pro Display", -apple-system, "Segoe UI", Roboto, Arial, sans-serif'
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        f"html,body{{margin:0;width:{w}px;height:{h}px;background:transparent;overflow:hidden}}"
        f".l{{position:absolute;left:{g['x'] - g['pad'] - g['bar']}px;top:{g['y'] - g['pad']}px;display:flex;"
        f"font:650 {g['size']}px/{g['size'] + 2}px {stack};color:{c['fg']}}}"
        f".b{{width:{g['bar']}px;background:{bar}}}"
        f".t{{padding:{g['pad']}px;background:color-mix(in srgb,{c['bg']} 82%,transparent);white-space:nowrap}}"
        f"</style></head><body><div class='l'><div class='b'></div><div class='t'>{_html.escape(label)}</div></div>"
        "</body></html>"
    )


# ── producing the pieces ─────────────────────────────────────────────────────
def prepare_media(
    timeline: dict[str, Any],
    opts: dict[str, Any],
    look: dict[str, Any],
    workdir: Path,
    *,
    ff: str,
    runner: Runner,
    browser_ok: bool,
    card_renderer: Callable[..., str] | None = None,
    page_renderer: Callable[..., str] | None = None,
) -> tuple[dict[int, dict[str, str]], list[str], dict[str, str]]:
    """Render cards and labels into ``workdir``. Returns (media, warnings, engines-used).

    ``card_renderer(card, w, h, out_png) → path`` and ``page_renderer(html, w, h, out_png) →
    path`` are the browser seams (the real ones go through the out-of-process worker)."""
    from . import cards as cardsmod

    w, h = opts["width"], opts["height"]
    warnings: list[str] = []
    media: dict[int, dict[str, str]] = {}
    has_drawtext = "drawtext" in ffmpeg_filters(ff, runner)
    font, font_how = find_font(look["fonts"].get("heading") or look["fonts"].get("body") or "", opts["label_font"])
    font_rel = ""
    if font:
        font_rel = "font" + Path(font).suffix.lower()
        shutil.copyfile(font, workdir / font_rel)

    def _html_card(card: dict[str, Any], out_png: Path) -> str:
        res = cardsmod.render("title-slide-1920x1080", card_data(card), out_png, size=f"{w}x{h}", limit="")
        return res["path"]

    def _html_page(page: str, out_png: Path) -> str:
        return cardsmod.render_page(page, out_png, w, h, transparent=True)["path"]

    card_renderer = card_renderer or (lambda card, ww, hh, out: _html_card(card, Path(out)))
    page_renderer = page_renderer or (lambda page, ww, hh, out: _html_page(page, Path(out)))

    engine = opts["card_engine"]
    if engine == "auto":
        engine = "html" if browser_ok else "ffmpeg"
    if engine == "html" and not browser_ok:
        raise MontageError("card_engine html needs the headless browser — " + (deps.need_browser() or "unavailable"))
    has_cards = any(it["kind"] == "card" for it in timeline["items"])
    if has_cards and engine == "ffmpeg" and not (has_drawtext and font):
        raise MontageError(
            "cards can't be rendered: the browser isn't available"
            + (" and this ffmpeg has no drawtext filter" if not has_drawtext else " and no font file was found")
            + " — install Chromium (campaign_setup) or an ffmpeg built with libfreetype"
        )
    if has_cards and engine == "ffmpeg" and opts["card_engine"] == "auto":
        warnings.append(
            "cards were drawn by ffmpeg (no browser): plain type, no logo — install Chromium for branded cards"
        )

    lab_engine = opts["label_engine"]
    has_labels = any(it["kind"] == "clip" and it.get("label") for it in timeline["items"])
    if lab_engine == "auto":
        lab_engine = "drawtext" if has_drawtext and font else "html" if browser_ok else "none"
        if has_labels and lab_engine == "html":
            warnings.append(
                "labels were rendered as HTML overlays — this ffmpeg has no drawtext"
                if not has_drawtext
                else "labels were rendered as HTML overlays — no font file was found for drawtext"
            )
        if has_labels and lab_engine == "none":
            warnings.append(
                "labels SKIPPED: this ffmpeg has no drawtext (or no font file) and the browser isn't available"
            )
    elif lab_engine == "drawtext" and has_labels and not (has_drawtext and font):
        raise MontageError(
            "label_engine drawtext: "
            + ("this ffmpeg has no drawtext filter" if not has_drawtext else "no font file found — set label_font")
        )
    elif lab_engine == "html" and has_labels and not browser_ok:
        raise MontageError("label_engine html needs the headless browser")
    if has_labels and lab_engine == "drawtext" and font_how.startswith("fallback") and look["fonts"].get("heading"):
        warnings.append(f"labels use {font_how}")

    for k, it in enumerate(timeline["items"]):
        if it["kind"] == "card":
            out = workdir / f"card{k:02d}.png"
            if engine == "html":
                media[k] = {"card": str(card_renderer(it["card"], w, h, str(out)))}
            else:
                _ff(
                    runner,
                    ffmpeg_card_cmd(ff, it["card"], card_look(it["card"], look), w, h, font_rel, out.name, workdir),
                    str(workdir),
                )
                media[k] = {"card": str(out)}
        elif it.get("label") and lab_engine != "none":
            bar = it.get("theme_color") or look["colors"]["accent"]
            if lab_engine == "drawtext":
                (workdir / f"label{k:02d}.txt").write_text(it["label"], encoding="utf-8")
                media[k] = {"label_text": f"label{k:02d}.txt", "font": font_rel}
            else:
                out = workdir / f"label{k:02d}.png"
                media[k] = {"label_png": str(page_renderer(label_html(it["label"], w, h, look, bar), w, h, str(out)))}
    return (
        media,
        warnings,
        {"cards": engine if has_cards else "", "labels": lab_engine if has_labels else "", "font": font_how},
    )


# ── the whole render ─────────────────────────────────────────────────────────
def render_montage(
    campaign_id: int,
    sequence: Any,
    output: Any,
    out_dir: Path,
    *,
    runner: Runner | None = None,
    browser_ok: bool | None = None,
    card_renderer: Callable[..., str] | None = None,
    page_renderer: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Validate, render and measure a montage into ``out_dir``. Doesn't touch the store's rows
    (the tool registers the result). Raises MontageError (validation) or RenderError (ffmpeg)."""
    runner = runner or _default_runner
    ff = deps.ffmpeg()
    if not ff:
        raise render.RenderError(deps.ffmpeg_hint())
    opts = normalize_output(output)
    items = parse_sequence(sequence)
    items, warnings = resolve_items(campaign_id, items)
    warnings += framing(items, opts)
    look = brandmod.resolve({})
    timeline = plan(items, opts, look)
    if browser_ok is None:
        browser_ok = deps.need_browser() is None
    out_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="work-", dir=out_dir))
    started = time.monotonic()
    try:
        media, w2, engines = prepare_media(
            timeline, opts, look, workdir, ff=ff, runner=runner, browser_ok=browser_ok,
            card_renderer=card_renderer, page_renderer=page_renderer,
        )  # fmt: skip
        warnings += w2
        args, graph = build_graph(timeline, opts, look, media)
        master = workdir / "master.mp4"
        _ff(runner, master_cmd(ff, args, graph, str(master), opts["fps"]), str(workdir))

        dst = out_dir / f"{opts['name']}.mp4"
        attempts: list[dict[str, Any]] = []
        for step in encode_ladder(opts["crf"], opts["max_bytes"], timeline["total"]):
            _ff(runner, encode_cmd(ff, str(master), str(dst), crf=step["crf"], bitrate=step.get("bitrate")), None)
            size = dst.stat().st_size if dst.exists() else 0
            attempts.append({**step, "size_bytes": size})
            if not opts["max_bytes"] or size <= opts["max_bytes"] or time.monotonic() - started > OUTPUT_BUDGET_S:
                break
        info = render.probe(dst, lambda c: runner(c, None))
        violations: list[str] = []
        if opts["max_bytes"] and info["size_bytes"] > opts["max_bytes"]:
            violations.append(
                f"{limits.human_bytes(info['size_bytes'])} is still over the {limits.human_bytes(opts['max_bytes'])} "
                f"ceiling after {len(attempts)} attempts — cut beats or use a smaller preset"
            )
        if opts["limit"]:
            # size=None: the byte ceiling (the tighter of max_bytes and the limit's) is checked above.
            violations += limits.check(
                opts["limit"], size=None, width=info["width"], height=info["height"], fmt="mp4",
                duration=info["duration_s"],
            )  # fmt: skip

        poster = ""
        if opts["poster"]:
            at = opts["poster_at"]
            if at is None:
                first = next((k for k, it in enumerate(items) if it["kind"] == "clip"), 0)
                at = timeline["starts"][first] + items[first]["length"] / 2
            at = min(max(0.0, at), max(0.0, info["duration_s"] - 0.05))
            pdst = out_dir / f"{opts['name']}-poster.png"
            _ff(runner, render.poster_cmd(ff, str(dst), str(pdst), at, None, None), None)
            poster = str(pdst) if pdst.exists() else ""

        gif: dict[str, Any] = {}
        if opts["gif"]:
            gif = _gif(ff, runner, master, out_dir / f"{opts['name']}.gif", opts, warnings)
        return {
            "path": str(dst),
            **info,
            "expected_duration_s": timeline["total"],
            "attempts": attempts,
            "violations": violations,
            "warnings": warnings,
            "poster": poster,
            "gif": gif,
            "opts": opts,
            "timeline": summarize(timeline),
            "engines": engines,
            "unapproved": [it["asset_id"] for it in items if it["kind"] == "clip" and it.get("unapproved")],
            "inputs": sorted({it["asset_id"] for it in items if it["kind"] == "clip"}),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _gif(ff: str, runner: Runner, master: Path, dst: Path, opts: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    row = limits.get(opts["gif_limit"]) if opts["gif_limit"] else None
    ceiling = int(row["max_bytes"]) if row and row.get("max_bytes") else None
    width = min(opts["gif_width"], opts["width"])
    for fps, wd in render.gif_ladder(12, width, opts["width"]):
        base = f"[0:v]scale={int(wd or width) // 2 * 2}:-2:flags=lanczos[base]"
        _ff(runner, render.gif_cmd(ff, str(master), str(dst), base, fps=fps), None)
        size = dst.stat().st_size if dst.exists() else 0
        if not ceiling or size <= ceiling:
            return {"path": str(dst), "size_bytes": size, "fps": fps, "width": wd}
    dst.unlink(missing_ok=True)
    warnings.append(
        f"GIF skipped — even at its smallest step it stays over {limits.human_bytes(ceiling)} ({opts['gif_limit']}); "
        "ship the mp4, or GIF a single beat with campaign_render"
    )
    return {}


def summarize(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    """The cut sheet: one row per item (+ the transition into it), times in the montage."""
    trs = {t["into"]: t for t in timeline["transitions"]}
    rows = []
    for k, it in enumerate(timeline["items"]):
        row: dict[str, Any] = {"n": it["n"], "kind": it["kind"], "start": timeline["starts"][k], "length": it["length"]}
        if it["kind"] == "clip":
            row.update(
                asset_id=it["asset_id"],
                **{"in": it["in"], "out": it["out"]},
                speed=it["speed"],
                label=it.get("label", ""),
            )
        else:
            row["title"] = it["card"]["title"]
        if k in trs:
            t = trs[k]
            row["transition"] = {"style": t["style"], "duration": t["duration"], "color": t["color"], "at": t["at"]}
        rows.append(row)
    return rows


def cut_sheet(rows: list[dict[str, Any]], total: float) -> str:
    out = []
    for r in rows:
        tr = r.get("transition")
        if tr:
            col = f" {tr['color']}" if tr["style"] in COLOR_WIPES else ""
            out.append(f"   ↳ {tr['style']}{col} {tr['duration']:g}s @ {tr['at']:.2f}s")
        if r["kind"] == "clip":
            sp = f" @{r['speed']:g}×" if r["speed"] != 1 else ""
            lab = f" “{r['label']}”" if r.get("label") else ""
            out.append(
                f"{r['n']:>2}. {r['start']:6.2f}s clip #{r['asset_id']} {r['in']:g}–{r['out']:g}s{sp} → {r['length']:.2f}s{lab}"
            )
        else:
            out.append(f"{r['n']:>2}. {r['start']:6.2f}s card “{r['title']}” {r['length']:.2f}s")
    out.append(f"    total {total:.2f}s")
    return "\n".join(out)


# ── storyboard ───────────────────────────────────────────────────────────────
def storyboard(
    campaign_id: int,
    sequence: Any,
    output: Any,
    dst: Path,
    *,
    runner: Runner | None = None,
    browser_ok: bool | None = None,
    card_renderer: Callable[..., str] | None = None,
    columns: int = 4,
) -> dict[str, Any]:
    """A contact-sheet PNG of the cut: one frame per item (a clip's middle frame, fitted the
    way the montage will fit it; the card itself), and between items a swatch of the
    transition — the wipe colour, an xfade's actual midpoint, or a thin bar for a cut."""
    runner = runner or _default_runner
    ff = deps.ffmpeg()
    if not ff:
        raise render.RenderError(deps.ffmpeg_hint())
    opts = normalize_output(output)
    items = parse_sequence(sequence)
    items, warnings = resolve_items(campaign_id, items)
    warnings += framing(items, opts)
    look = brandmod.resolve({})
    timeline = plan(items, opts, look)
    if browser_ok is None:
        browser_ok = deps.need_browser() is None
    w, h = opts["width"], opts["height"]
    scale = min(1.0, 400 / max(w, h)) if h > w else min(1.0, 384 / w)
    cw, ch = int(w * scale) // 2 * 2, int(h * scale) // 2 * 2
    sw = max(24, cw // 4) // 2 * 2
    bg = opts["background"] or look["colors"]["bg"]
    dst.parent.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="board-", dir=dst.parent))
    try:
        card_opts = {**opts, "label_engine": "none"}
        media, w2, engines = prepare_media(
            {**timeline, "items": [it for it in timeline["items"]]}, card_opts, look, workdir,
            ff=ff, runner=runner, browser_ok=browser_ok, card_renderer=card_renderer,
        )  # fmt: skip
        warnings += [x for x in w2 if not x.startswith("labels")]
        cells: list[str] = []
        for k, it in enumerate(timeline["items"]):
            out = workdir / f"cell{k:02d}.png"
            if it["kind"] == "clip":
                at = it["in"] + (it["out"] - it["in"]) / 2
                vf = f"{frame_chain(it.get('frame'), w, h, bg)},scale={cw}:{ch}:flags=lanczos,setsar=1"
                cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-ss", _fmt(at), "-i", it["path"], "-vf", vf, "-frames:v", "1", "-update", "1", str(out)]  # fmt: skip
            else:
                cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", media[k]["card"], "-vf", f"scale={cw}:{ch}:flags=lanczos,setsar=1", "-frames:v", "1", "-update", "1", str(out)]  # fmt: skip
            _ff(runner, cmd, None)
            cells.append(str(out))
        trs = {t["into"]: t for t in timeline["transitions"]}
        swatches: dict[int, str] = {}
        for k, tr in trs.items():
            out = workdir / f"swatch{k:02d}.png"
            if tr["style"] == "cut":
                cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=0x000000:s=8x{ch}:d=1", "-frames:v", "1", "-update", "1", str(out)]  # fmt: skip
            elif tr["style"] in COLOR_WIPES:
                cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={ff_color(tr['color'])}:s={sw}x{ch}:d=1", "-frames:v", "1", "-update", "1", str(out)]  # fmt: skip
            else:
                # The real thing, mid-way: the two neighbouring frames through this xfade.
                fc = (
                    f"[0:v]fps=10,format=yuv420p,setsar=1,settb=AVTB[a];[1:v]fps=10,format=yuv420p,setsar=1,settb=AVTB[b];"
                    f"[a][b]xfade=transition={tr['style']}:duration=1:offset=0,crop={sw}:{ch}:(iw-{sw})/2:0"
                )
                cmd = [
                    ff, "-y", "-hide_banner", "-loglevel", "error",
                    "-loop", "1", "-t", "1.2", "-i", cells[k - 1], "-loop", "1", "-t", "1.2", "-i", cells[k],
                    "-filter_complex", fc, "-ss", "0.5", "-frames:v", "1", "-update", "1", str(out),
                ]  # fmt: skip
            _ff(runner, cmd, None)
            swatches[k] = str(out)
        # Rows of `columns` items, each followed by the swatch into the next; rows padded to one width.
        cols = max(1, min(int(columns), 6))
        rows = [list(range(i, min(i + cols, len(cells)))) for i in range(0, len(cells), cols)]
        args: list[str] = []
        idx: dict[str, int] = {}

        def inp(p: str) -> str:
            if p not in idx:
                idx[p] = len(idx)
                args.extend(["-i", p])
            return f"[{idx[p]}:v]"

        parts, row_labels = [], []
        gap = 6
        row_w = cols * (cw + gap) + cols * (sw + gap)
        for r, ks in enumerate(rows):
            seq: list[str] = []
            for j, k in enumerate(ks):
                seq.append(inp(cells[k]))
                nxt = k + 1
                if nxt in swatches and (j < len(ks) - 1 or nxt < len(cells)):
                    seq.append(inp(swatches[nxt]))
            labs = []
            for i, s in enumerate(seq):
                lab = f"r{r}p{i}"
                parts.append(
                    f"{s}format=rgb24,setsar=1,pad=iw+{gap}:ih+{gap}:{gap // 2}:{gap // 2}:color=0x3a3a3a[{lab}]"
                )
                labs.append(f"[{lab}]")
            row = f"row{r}"
            if len(labs) > 1:
                parts.append("".join(labs) + f"hstack=inputs={len(labs)},pad={row_w}:ih:0:0:color=0x3a3a3a[{row}]")
            else:
                parts.append(f"{labs[0]}pad={row_w}:ih:0:0:color=0x3a3a3a[{row}]")
            row_labels.append(f"[{row}]")
        if len(row_labels) > 1:
            parts.append("".join(row_labels) + f"vstack=inputs={len(row_labels)}[sheet]")
        else:
            parts.append(f"{row_labels[0]}null[sheet]")
        _ff(
            runner,
            [
                ff,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                *args,
                "-filter_complex",
                ";".join(parts),
                "-map",
                "[sheet]",
                "-frames:v",
                "1",
                "-update",
                "1",
                str(dst),
            ],  # fmt: skip
            None,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return {
        "path": str(dst),
        "warnings": warnings,
        "timeline": summarize(timeline),
        "total": timeline["total"],
        "opts": opts,
        "grid": {"columns": cols, "rows": math.ceil(len(cells) / cols), "cell": [cw, ch]},
        "engines": engines,
    }
