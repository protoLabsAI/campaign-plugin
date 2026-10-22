"""Captions — timed lower-third text burned into a ``campaign_render`` clip.

An output spec's ``captions`` is a list of::

    {"text": "The agent builds a spec form", "start": 4.0, "end": "form_ready"}
    {"text": "Send it", "at": "sent"}          # ends when the next caption starts (or the clip ends)

**Times are SOURCE seconds** (or mark names) — the take's own clock, the one the ``speed`` ramp
marks use — and are mapped through the speed map (:func:`render.timeline_time`) onto the
output clock. So a caption stays on the action however the ramps change.

* **Look** — the montage lower third: an accent bar + the text on a translucent brand-bg box in
  the brand heading font, with a short fade in and out. One caption on screen at a time.
* **Hold** — every caption stays up at least ``MIN_HOLD_S`` of OUTPUT time. A ramp that would
  squeeze one shorter extends its end (pushing the next one later if it must) and says so in
  the render's ``warnings`` — never a silent clip.
* **Layout** — the text wraps to at most ``MAX_LINES`` lines at the clip's width; longer text is
  refused before anything renders. 9:16 keeps to the vertical safe area (above the platform's
  caption/buttons strip, left of its right-hand action rail).
* **Rendering** — each caption becomes one transparent PNG at the output size (ffmpeg
  ``drawtext`` in the brand font when this ffmpeg has it, else an HTML page in the headless
  browser — the label engines), then is overlaid over its own window with an alpha fade.
  Captions belong to the clip: brand bookends wrap the captioned clip, so the cards never get
  one. The take itself is only ever read.
"""

from __future__ import annotations

import html as _html
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from . import deps, render

CAPTION_KEYS = ("text", "start", "end", "at")
MAX_CAPTIONS = 60
MAX_TEXT = 200
MAX_LINES = 2
MIN_HOLD_S = 1.8  # output seconds a caption stays readable, at least
FADE_S = 0.2  # fade in / fade out (clamped to a quarter of a caption's time on screen)
CHAR_W = 0.56  # average advance of a bold sans glyph, in font sizes (wrap estimate)
LINE_GAP = 0.25  # extra space between two lines, in font sizes

PngRunner = Callable[[list[str], str], subprocess.CompletedProcess]


class CaptionError(render.RenderError):
    pass


# ── validation ───────────────────────────────────────────────────────────────
def normalize(value: Any, marks: dict[str, float], name: str) -> list[dict[str, Any]]:
    """The spec's ``captions`` → ``[{text, start, end|None}]`` in source seconds, sorted.
    Unknown keys, empty text, a start with no time and overlapping ranges are refused."""
    if value in (None, "", []):
        return []
    if not isinstance(value, list):
        raise CaptionError(f"output {name}: captions must be a list of {{text, start, end}} or {{text, at}}")
    if len(value) > MAX_CAPTIONS:
        raise CaptionError(f"output {name}: {len(value)} captions is too many — at most {MAX_CAPTIONS}")
    out: list[dict[str, Any]] = []
    for i, c in enumerate(value, 1):
        what = f"output {name}: captions[{i}]"
        if isinstance(c, str):
            raise CaptionError(f"{what} must be a mapping {{text, start, end}} or {{text, at}}, got a string")
        if not isinstance(c, dict):
            raise CaptionError(f"{what} must be a mapping {{text, start, end}} or {{text, at}}")
        render.refuse_unknown(c, CAPTION_KEYS, what)
        text = " ".join(str(c.get("text") or "").split())
        if not text:
            raise CaptionError(f"{what}: text is empty")
        if len(text) > MAX_TEXT:
            raise CaptionError(f"{what}: text is {len(text)} characters — a caption reads at ≤ {MAX_TEXT} (2 lines)")
        if c.get("start") not in (None, "") and c.get("at") not in (None, ""):
            raise CaptionError(f"{what}: give start or at, not both")
        key = "start" if c.get("start") not in (None, "") else "at"
        start = render.resolve_time(c.get(key), marks, f"{name}.captions[{i}].{key}")
        if start is None:
            raise CaptionError(f"{what}: needs start (or at) — seconds of the take or a mark name")
        end = render.resolve_time(c.get("end"), marks, f"{name}.captions[{i}].end")
        if end is not None and end <= start:
            raise CaptionError(f"{what}: end ({end:.2f}s) must be after start ({start:.2f}s)")
        out.append({"text": text, "start": start, "end": end, "n": i})
    out.sort(key=lambda c: c["start"])
    for a, b in zip(out, out[1:]):
        if a["start"] == b["start"]:
            raise CaptionError(f"output {name}: captions[{a['n']}] and [{b['n']}] both start at {a['start']:.2f}s")
        if a["end"] is not None and a["end"] > b["start"] + 1e-6:
            raise CaptionError(
                f"output {name}: captions[{a['n']}] ({a['start']:.2f}–{a['end']:.2f}s) overlaps captions[{b['n']}] "
                f"(from {b['start']:.2f}s) — one caption is on screen at a time"
            )
    return out


# ── timing ───────────────────────────────────────────────────────────────────
def plan(
    captions: list[dict[str, Any]], segs: list, cut_start: float, cut_end: float, name: str = "output"
) -> tuple[list[dict[str, Any]], list[str]]:
    """Map captions onto the output clock and enforce the hold. Returns (timed, warnings).

    Each timed caption: ``{text, start, end}`` (source seconds — what to pass back to re-render)
    and ``{out_start, out_end}`` (output seconds of the clip, before any bookends)."""
    total = render.timeline_time(segs, cut_end)
    timed: list[dict[str, Any]] = []
    warnings: list[str] = []
    prev_end = 0.0
    for i, c in enumerate(captions):
        label = f"caption {c['n']} ({_short(c['text'])!r})"
        if c["start"] >= cut_end - 1e-6 or (c["end"] is not None and c["end"] <= cut_start + 1e-6):
            raise CaptionError(
                f"output {name}: {label} at {c['start']:.2f}s is outside the cut ({cut_start:.2f}–{cut_end:.2f}s)"
            )
        src_s = max(c["start"], cut_start)
        nxt = captions[i + 1]["start"] if i + 1 < len(captions) else cut_end
        src_e = min(c["end"] if c["end"] is not None else nxt, cut_end)
        o_s, o_e = render.timeline_time(segs, src_s), render.timeline_time(segs, src_e)
        natural = o_e - o_s
        if o_s < prev_end - 1e-6:
            warnings.append(
                f"{label} starts {prev_end - o_s:.2f}s late (at {prev_end:.2f}s out) — the caption before it is "
                f"held {MIN_HOLD_S:g}s"
            )
            o_s = prev_end
        if o_e - o_s < MIN_HOLD_S - 1e-6:
            want = o_s + MIN_HOLD_S
            if want <= total + 1e-6:
                warnings.append(
                    f"{label} would be on screen {natural:.2f}s"
                    + (
                        " (under the hold — ramps or a short span)"
                        if natural < MIN_HOLD_S
                        else " after the one before it"
                    )
                    + " — held to "
                    f"{MIN_HOLD_S:g}s, until {want:.2f}s out; slow the ramp there or move the caption if that's late"
                )
                o_e = want
            else:
                o_e = total
                back = max(prev_end, o_e - MIN_HOLD_S)
                if back < o_s - 1e-6:
                    warnings.append(
                        f"{label} starts {o_s - back:.2f}s early to stay up {o_e - back:.2f}s before the end"
                    )
                    o_s = back
                if o_e - o_s < MIN_HOLD_S - 1e-6:
                    warnings.append(
                        f"{label} is on screen only {o_e - o_s:.2f}s — the clip ends; under the {MIN_HOLD_S:g}s hold. "
                        "End the clip later or caption earlier"
                    )
        timed.append({
            "text": c["text"], "start": round(src_s, 3), "end": round(src_e, 3),
            "out_start": round(o_s, 3), "out_end": round(o_e, 3),
        })  # fmt: skip
        prev_end = o_e
    return timed, warnings


def _short(text: str, n: int = 32) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


# ── layout ───────────────────────────────────────────────────────────────────
def geometry(w: int, h: int, lines: int = 1) -> dict[str, int]:
    """Where a caption sits: the montage lower third, grown upward for a second line. A
    vertical frame keeps above the platform's bottom strip and left of its action rail."""
    from . import montage

    g = dict(montage.label_geometry(w, h))
    gap = round(g["size"] * LINE_GAP)
    block = lines * g["size"] + (lines - 1) * gap
    g["gap"] = gap
    g["y"] = g["y"] - (block - g["size"])
    g["block"] = block
    right = round(w * 0.16) if h > w else max(12, round(min(w, h) * 0.06))
    g["max_w"] = max(40, w - g["x"] - right - g["pad"])
    return g


def wrap(text: str, w: int, h: int) -> list[str]:
    """Lines of ``text`` at a ``w``×``h`` frame. Refuses text that needs more than MAX_LINES."""
    from . import montage

    g = geometry(w, h)
    per_line = max(8, int(g["max_w"] / (g["size"] * CHAR_W)))
    lines = montage.wrap(text, per_line)
    if len(lines) > MAX_LINES:
        raise CaptionError(
            f"caption {_short(text)!r} wraps to {len(lines)} lines at {w}×{h} — at most {MAX_LINES} lines "
            f"(~{per_line * MAX_LINES} characters) fit; shorten it or split it into two captions"
        )
    return lines


def output_size(spec: dict[str, Any], source: dict[str, Any], width: int | None) -> tuple[int, int]:
    """The rendered frame size for a timeline at ``width`` (the scale filter's ``-2`` rounding)."""
    crop = spec.get("crop") or {}
    w0 = int(crop.get("width") or source.get("width") or 0)
    h0 = int(crop.get("height") or source.get("height") or 0)
    if not (w0 and h0):
        raise CaptionError("captions need the take's frame size, and ffprobe found none")
    if not width or width == w0:
        return w0, h0
    w = int(width) // 2 * 2
    return w, max(2, int(w * h0 / (w0 * 2) + 0.5) * 2)


# ── the art ──────────────────────────────────────────────────────────────────
def caption_html(lines: list[str], w: int, h: int, look: dict[str, Any], bar: str) -> str:
    """A transparent full-frame page holding one caption (the no-drawtext engine)."""
    g = geometry(w, h, len(lines))
    c = look["colors"]
    font = re.sub(r"[^A-Za-z0-9 \-_]", "", look["fonts"].get("heading") or "")
    stack = (
        f'"{font}", ' if font else ""
    ) + 'Inter, "SF Pro Display", -apple-system, "Segoe UI", Roboto, Arial, sans-serif'
    body = "<br>".join(_html.escape(line) for line in lines)
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        f"html,body{{margin:0;width:{w}px;height:{h}px;background:transparent;overflow:hidden}}"
        f".l{{position:absolute;left:{g['x'] - g['pad'] - g['bar']}px;top:{g['y'] - g['pad']}px;display:flex;"
        f"font:650 {g['size']}px/{g['size'] + g['gap']}px {stack};color:{c['fg']}}}"
        f".b{{width:{g['bar']}px;background:{bar}}}"
        f".t{{padding:{g['pad'] - round(g['gap'] / 2)}px {g['pad']}px;"
        f"background:color-mix(in srgb,{c['bg']} 82%,transparent);white-space:nowrap}}"
        f"</style></head><body><div class='l'><div class='b'></div><div class='t'>{body}</div></div>"
        "</body></html>"
    )


def drawtext_png_cmd(
    ff: str, textfile: str, fontfile: str, lines: int, w: int, h: int, colors: dict[str, str], bar: str, dst: str
) -> list[str]:
    """One caption as an RGBA PNG via drawtext. drawbox/drawtext don't write alpha onto a
    transparent canvas, so the colour and the coverage are drawn on two opaque canvases and
    merged: bar opaque, box at 0.82, text opaque. File names are RELATIVE (run in the work dir).
    The bar is the same text block's box shifted left by the bar's width, under the real box —
    so it is exactly the box's height for one line or two."""
    from . import montage

    g = geometry(w, h, lines)
    common = (
        f"fontfile={fontfile}:textfile={textfile}:expansion=none:fontsize={g['size']}:line_spacing={g['gap']}:"
        f"y={g['y']}:box=1:boxborderw={g['pad']}"
    )
    fg, bg = montage.ff_color(colors["fg"]), montage.ff_color(colors["bg"])
    color = (
        f"[0:v]drawtext={common}:x={g['x'] - g['bar']}:fontcolor={montage.ff_color(bar)}:boxcolor={montage.ff_color(bar)},"
        f"drawtext={common}:x={g['x']}:fontcolor={fg}:boxcolor={bg}[c]"
    )
    mask = (
        f"[1:v]drawtext={common}:x={g['x'] - g['bar']}:fontcolor=white:boxcolor=white,"
        f"drawtext={common}:x={g['x']}:fontcolor=white:boxcolor=0xd1d1d1,format=gray[m]"
    )
    return [
        ff, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c={bg}:s={w}x{h},format=rgb24",
        "-f", "lavfi", "-i", f"color=c=black:s={w}x{h},format=rgb24",
        "-filter_complex", f"{color};{mask};[c][m]alphamerge,format=rgba[o]",
        "-map", "[o]", "-frames:v", "1", "-update", "1", dst,
    ]  # fmt: skip


def _png_runner(cmd: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=render.FFMPEG_TIMEOUT_S, cwd=cwd)


class Art:
    """Makes caption PNGs for one render — once per frame size — with the label engines:
    drawtext in the brand font when this ffmpeg has it, else the browser's HTML overlay."""

    def __init__(
        self,
        workdir: Path,
        *,
        engine: str = "auto",
        png_runner: PngRunner | None = None,
        page_renderer: Callable[[str, int, int, str], str] | None = None,
        browser_ok: bool | None = None,
        look: dict[str, Any] | None = None,
    ) -> None:
        from . import brand as brandmod
        from . import montage

        self.workdir = Path(workdir)
        self.look = look or brandmod.resolve({})
        self.bar = self.look["colors"].get("accent") or "#22c55e"
        self.png_runner = png_runner or _png_runner
        self.page_renderer = page_renderer
        ff = deps.ffmpeg()
        self.ff = str(Path(ff).absolute()) if ff else "ffmpeg"  # the PNGs are drawn from the work dir
        self.font, self.font_how = "", ""
        self.notes: list[str] = []
        if engine == "auto":
            has_drawtext = "drawtext" in montage.ffmpeg_filters(self.ff)
            fonts = self.look.get("fonts") or {}
            self.font, self.font_how = montage.find_font(fonts.get("heading") or fonts.get("body") or "")
            if has_drawtext and self.font:
                engine = "drawtext"
            else:
                ok = (
                    browser_ok if browser_ok is not None else (page_renderer is not None or deps.need_browser() is None)
                )
                if not ok:
                    raise CaptionError(
                        "captions can't be drawn: "
                        + (
                            "no font file was found for drawtext"
                            if has_drawtext
                            else "this ffmpeg has no drawtext filter"
                        )
                        + " and the headless browser isn't available — install Chromium (campaign_setup) "
                        "or an ffmpeg built with libfreetype"
                    )
                engine = "html"
        elif engine == "drawtext" and not self.font:
            fonts = self.look.get("fonts") or {}
            self.font, self.font_how = montage.find_font(fonts.get("heading") or fonts.get("body") or "")
        self.engine = engine
        if (
            engine == "drawtext"
            and self.font_how.startswith("fallback")
            and (self.look.get("fonts") or {}).get("heading")
        ):
            self.notes.append(f"captions use {self.font_how}")
        self._cache: dict[tuple, list[str]] = {}

    def pngs(self, timed: list[dict[str, Any]], w: int, h: int) -> list[str]:
        key = (w, h, tuple(c["text"] for c in timed))  # same size AND the same captions
        if key in self._cache:
            return self._cache[key]
        d = self.workdir / f"captions-{w}x{h}-{len(self._cache)}"  # never overwrite an earlier set
        d.mkdir(parents=True, exist_ok=True)
        out: list[str] = []
        font_rel = ""
        if self.engine == "drawtext":
            font_rel = "font" + Path(self.font).suffix.lower()
            shutil.copyfile(self.font, d / font_rel)
        for k, c in enumerate(timed):
            lines = wrap(c["text"], w, h)
            png = d / f"caption{k:02d}.png"
            if self.engine == "drawtext":
                (d / f"caption{k:02d}.txt").write_text("\n".join(lines), encoding="utf-8")
                cmd = drawtext_png_cmd(
                    self.ff, f"caption{k:02d}.txt", font_rel, len(lines), w, h, self.look["colors"], self.bar, png.name
                )
                try:
                    res = self.png_runner(cmd, str(d))
                except (OSError, subprocess.TimeoutExpired) as e:
                    raise CaptionError(f"couldn't draw caption {k + 1}: {e}") from None
                if res.returncode != 0:
                    tail = "\n".join((res.stderr or "").strip().splitlines()[-4:])
                    raise CaptionError(f"ffmpeg couldn't draw caption {k + 1}: {tail or 'no output'}")
                out.append(str(png))
            else:
                page = caption_html(lines, w, h, self.look, self.bar)
                out.append(str(self._page(page, w, h, str(png))))
        self._cache[key] = out
        return out

    def _page(self, page: str, w: int, h: int, dst: str) -> str:
        if self.page_renderer is not None:
            return self.page_renderer(page, w, h, dst)
        from . import cards

        return cards.render_page(page, dst, w, h, transparent=True)["path"]


# ── the graph ────────────────────────────────────────────────────────────────
def overlay_inputs(timed: list[dict[str, Any]], pngs: list[str], fps: float) -> list[str]:
    """ffmpeg input args: each caption PNG looped for exactly its window (inputs 1..N)."""
    args: list[str] = []
    for c, png in zip(timed, pngs):
        dur = max(0.05, c["out_end"] - c["out_start"])
        args += ["-loop", "1", "-framerate", render._fmt(fps), "-t", f"{dur:.3f}", "-i", png]
    return args


def overlay_chain(timed: list[dict[str, Any]], src: str, dst: str, first_input: int = 1) -> str:
    """Filters from ``[src]`` to ``[dst]``: each caption fades in, holds, fades out, at its
    output window — the overlay only ever touches the clip's own frames."""
    if not timed:
        return f"[{src}]null[{dst}]"
    parts, cur = [], src
    for k, c in enumerate(timed):
        s, e = c["out_start"], c["out_end"]
        dur = max(0.05, e - s)
        f = min(FADE_S, dur / 4)
        parts.append(
            f"[{first_input + k}:v]format=rgba,fade=t=in:st=0:d={f:.3f}:alpha=1,"
            f"fade=t=out:st={dur - f:.3f}:d={f:.3f}:alpha=1,setpts=PTS-STARTPTS+{s:.3f}/TB[cap{k}]"
        )
        nxt = dst if k == len(timed) - 1 else f"capd{k}"
        parts.append(f"[{cur}][cap{k}]overlay=0:0:eof_action=pass[{nxt}]")
        cur = nxt
    return ";".join(parts)


def summary(timed: list[dict[str, Any]], engine: str) -> str:
    """One reply line: how many, which engine, and each one's output window."""
    spans = ", ".join(f"{c['out_start']:.1f}–{c['out_end']:.1f}s" for c in timed)
    return f"captions: {len(timed)} ({engine}) at {spans} (output time, before bookends)"
