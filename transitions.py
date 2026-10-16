"""Signature video transitions — the protoLabs motion language for cuts, evaluated frame-exactly.

Two transitions, chosen by Josh from the design-system prototypes (protoContent#606):

* **edge_sweep** — 600 ms, the default for beat → beat / chapter changes. Both shots stay still;
  the incoming shot is revealed left → right, ``x_reveal = W·E(u)``, behind a narrow accent
  leading band (``32 / 2560`` of the width) whose RIGHT edge is the reveal edge.
* **boot_seam** — 400 ms, for brand boundaries (splash → title card, content → outro). The
  incoming shot is revealed through a horizontal seam that opens vertically from the centre,
  ``y ∈ [H·(1−E)/2, H·(1+E)/2]``, edged by two accent lines (``6 / 1440`` of the height,
  centred on the seam edges) at opacity ``1 − E(u)``.

**The contract** (one function for Campaign and the console — see ``manifest()`` /
``transitions.json``):

* easing ``E(u) = 4u³`` for ``u < ½``, else ``1 − (−2u + 2)³ / 2`` (easeInOutCubic, exact — not a
  cubic-bezier approximation); ``u = clamp(t / duration, 0, 1)``;
* frame quantization: ``N = max(1, floor(duration_ms · fps / 1000 + 0.5))`` frames, effective
  duration ``N / fps``; transition frame ``k`` (``0 … N−1``) is evaluated at ``u = k / N``;
* endpoint ownership: frame ``k = 0`` IS the outgoing shot (``E(0) = 0``, no band/seam), and the
  incoming shot owns ``u = 1`` — its first frame after the overlap. No frame is duplicated or
  dropped; the overlap is ``N`` frames, like an xfade of ``N / fps``;
* geometry is normalized to the canvas; colour is the brand accent (``--pl-color-accent`` from the
  brand snapshot), opaque; edges are antialiased by exact pixel COVERAGE of the continuous
  geometry (a pixel ``[x, x+1)`` partly revealed gets that fraction), per plane;
* reduced motion: no spatial movement — an instantaneous cut (the surrounding holds are kept).

ffmpeg renders it with masks, not per-pixel expressions: the geometry is separable (a sweep
depends only on x, a seam only on y), so each mask is a 1-pixel-thin ``geq`` strip per frame
(evaluated at ``u = n / N`` from the frame index, never from wall-clock), stretched to the canvas
and applied with ``maskedmerge`` — frame-exact, and ~100× faster than an xfade ``custom`` expr.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

PROFILE = "protolabs-video-transitions"
PROFILE_VERSION = "0.1.0"  # pre-DS-export: protoContent#606 proposal numbers
EASING = "easeInOutCubic"
SIGNATURES: dict[str, dict[str, Any]] = {
    "edge_sweep": {
        "duration_ms": 600,
        "role": "beat → beat, chapter changes (the default)",
        "geometry": {
            "reveal": "x < W·E(u)",
            "band_width": 32 / 2560,
            "band_side": "trailing (left of the reveal edge)",
        },
    },
    "boot_seam": {
        "duration_ms": 400,
        "role": "brand boundaries: splash → title card, content → outro",
        "geometry": {"reveal": "H·(1−E)/2 ≤ y < H·(1+E)/2", "seam_width": 6 / 1440, "seam_opacity": "1 − E(u)"},
    },
}
MANIFEST_PATH = Path(__file__).resolve().parent / "transitions.json"


def ease(u: float) -> float:
    """easeInOutCubic, exact."""
    u = min(1.0, max(0.0, float(u)))
    return 4 * u * u * u if u < 0.5 else 1 - (-2 * u + 2) ** 3 / 2


def frames(style: str, fps: float) -> int:
    """N — the transition's frame count at ``fps``."""
    ms = SIGNATURES[style]["duration_ms"]
    return max(1, math.floor(ms * fps / 1000 + 0.5))


def duration_s(style: str, fps: float) -> float:
    """The effective (frame-quantized) duration ``N / fps``."""
    return frames(style, fps) / fps


def progress(k: int, n: int) -> float:
    """u for transition frame ``k`` of ``n`` (``k = 0`` is the outgoing shot; ``u = 1`` is not a
    transition frame — the incoming shot owns it)."""
    return k / n


def coverage(lo: float, hi: float, px: int) -> float:
    """How much of pixel ``[px, px+1)`` the interval ``[lo, hi)`` covers (0..1)."""
    return max(0.0, min(px + 1.0, hi) - max(float(px), lo))


def edge_sweep(u: float, width: int) -> dict[str, float]:
    """Edge-sweep geometry in pixels: ``reveal`` (incoming shown at x < reveal), ``band`` (x0, x1)."""
    x = width * ease(u)
    bw = width * SIGNATURES["edge_sweep"]["geometry"]["band_width"]
    return {"reveal": x, "band_x0": x - bw, "band_x1": x, "band": 0.0 < u < 1.0}


def boot_seam(u: float, height: int) -> dict[str, float]:
    """Boot-seam geometry in pixels: the open band ``[y0, y1)``, the seam lines' half width and opacity."""
    e = ease(u)
    return {
        "y0": height * (1 - e) / 2,
        "y1": height * (1 + e) / 2,
        "half_width": height * SIGNATURES["boot_seam"]["geometry"]["seam_width"] / 2,
        "opacity": (1 - e) if 0.0 < u < 1.0 else 0.0,
    }


def pixel(style: str, u: float, x: int, y: int, w: int, h: int) -> dict[str, float]:
    """The reference compositing weights for ONE pixel: ``{"a", "b", "accent"}`` summing to 1 —
    what both renderers must produce (within raster tolerance)."""
    if style == "edge_sweep":
        g = edge_sweep(u, w)
        c_edge = coverage(-1e9, g["reveal"], x)  # left of the reveal edge: band or incoming
        c_in = coverage(-1e9, g["band_x0"], x)  # left of the band: incoming
        return {"a": 1 - c_edge, "accent": c_edge - c_in, "b": c_in}
    g = boot_seam(u, h)
    cov = coverage(g["y0"], g["y1"], y)
    hw = g["half_width"]
    line = min(1.0, coverage(g["y0"] - hw, g["y0"] + hw, y) + coverage(g["y1"] - hw, g["y1"] + hw, y))
    alpha = g["opacity"] * line
    return {"a": (1 - cov) * (1 - alpha), "b": cov * (1 - alpha), "accent": alpha}


# ── ffmpeg ─────────────────────────────────────────────────────────────────────
def _u(n_frames: int) -> str:
    return f"(N/{n_frames})"


def _ease_expr(n_frames: int) -> str:
    u = _u(n_frames)
    return f"if(lt({u},0.5),4*{u}*{u}*{u},1-4*(1-{u})*(1-{u})*(1-{u}))"


def _strip(w: int, h: int, fps: float, n_frames: int, expr: str, *, vertical: bool) -> str:
    """A per-frame mask: a thin geq strip (u from the frame index) stretched to w×h. In geq, W/H/X/Y
    are the CURRENT PLANE's, so the same normalized expression is right for luma and chroma."""
    size = f"2x{h}" if vertical else f"{w}x2"
    return (
        f"color=c=black:s={size}:r={fps:g}:d={n_frames / fps:.6f},format=yuv420p,"
        f"geq=lum='{expr}':cb='{expr}':cr='{expr}',scale={w}:{h}:flags=neighbor,setsar=1,{_grid(fps)}"
    )


def _grid(fps: float) -> str:
    """Frame k at pts k on a 1/fps timebase — every input of a composite on the same grid."""
    return f"settb=1/{fps:g},setpts=N"


def graph(style: str, a: str, b: str, out: str, *, w: int, h: int, fps: float, accent: str, tag: str) -> list[str]:
    """filter_complex chains that composite ``N`` frames of ``a`` (outgoing) and ``b`` (incoming),
    both exactly ``N`` frames long, into ``out``. ``accent`` is an ffmpeg colour (0xRRGGBB)."""
    n = frames(style, fps)
    e = _ease_expr(n)
    acc = f"color=c={accent}:s={w}x{h}:r={fps:g}:d={n / fps:.6f},format=yuv420p,setsar=1,{_grid(fps)}[{tag}c]"
    if style == "edge_sweep":
        bw = SIGNATURES["edge_sweep"]["geometry"]["band_width"]
        edge = f"255*clip(W*{e}-X,0,1)"
        inner = f"255*clip(W*{e}-W*{bw:.8f}-X,0,1)"
        return [
            acc,
            f"{_strip(w, h, fps, n, edge, vertical=False)}[{tag}me]",
            f"{_strip(w, h, fps, n, inner, vertical=False)}[{tag}mi]",
            f"[{a}][{tag}c][{tag}me]maskedmerge[{tag}t]",
            f"[{tag}t][{b}][{tag}mi]maskedmerge[{out}]",
        ]
    sw = SIGNATURES["boot_seam"]["geometry"]["seam_width"]
    y0, y1, hw = f"(H*(1-{e})/2)", f"(H*(1+{e})/2)", f"(H*{sw:.8f}/2)"
    cov = f"255*max(0,min(Y+1,{y1})-max(Y,{y0}))"

    def line(yc: str) -> str:
        return f"max(0,min(Y+1,{yc}+{hw})-max(Y,{yc}-{hw}))"

    alpha = f"255*(1-{e})*gt(N,0)*min(1,{line(y0)}+{line(y1)})"
    return [
        acc,
        f"{_strip(w, h, fps, n, cov, vertical=True)}[{tag}mc]",
        f"{_strip(w, h, fps, n, alpha, vertical=True)}[{tag}ma]",
        f"[{a}][{b}][{tag}mc]maskedmerge[{tag}t]",
        f"[{tag}t][{tag}c][{tag}ma]maskedmerge[{out}]",
    ]


# ── the shared contract (console parity) ───────────────────────────────────────
def manifest() -> dict[str, Any]:
    return {
        "profile": PROFILE,
        "version": PROFILE_VERSION,
        "source": "protoLabsAI/protoContent#606 (proposal; Josh picked A Edge Sweep + C Boot Seam)",
        "easing": {"id": EASING, "definition": "u<0.5 ? 4u^3 : 1-((-2u+2)^3)/2", "u": "clamp(elapsed/duration,0,1)"},
        "quantization": {
            "frames": "N = max(1, floor(duration_ms*fps/1000 + 0.5))",
            "effective_duration": "N/fps",
            "frame_u": "u = k/N for k = 0..N-1",
            "endpoints": "k=0 is the outgoing shot; the incoming shot owns u=1 (its first frame after the overlap)",
            "examples": {s: {str(f): frames(s, f) for f in (24, 30, 60)} for s in SIGNATURES},
        },
        "compositing": "opaque; per-pixel coverage of the continuous geometry; accent = --pl-color-accent",
        "reduced_motion": "instantaneous cut; surrounding holds preserved",
        "transitions": SIGNATURES,
    }


def write_manifest(path: Path = MANIFEST_PATH) -> None:
    path.write_text(json.dumps(manifest(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write_manifest()
    print(f"wrote {MANIFEST_PATH}")
