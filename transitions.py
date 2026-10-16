"""The signature video transition — the protoLabs motion language for cuts, evaluated frame-exactly.

**edge_sweep** — 600 ms, chosen by Josh from the design-system prototypes (protoContent#606): the
default between beats and the ONE moving transition of the brand intro/outro (title card → main
clip, main clip → bumper). Both shots stay still; the incoming shot is revealed left → right,
``x_reveal = W·E(u)``, behind a narrow accent leading band (``32 / 2560`` of the width) whose
RIGHT edge is the reveal edge. (The contract's second concept, Boot Seam, was tried and REMOVED
on Josh's call — the brand boundaries around the bumper are plain cuts now.)

**The contract** (one function for Campaign and the console — see ``manifest()`` /
``transitions.json``):

* easing ``E(u) = 4u³`` for ``u < ½``, else ``1 − (−2u + 2)³ / 2`` (easeInOutCubic, exact — not a
  cubic-bezier approximation); ``u = clamp(t / duration, 0, 1)``;
* frame quantization: ``N = max(1, floor(duration_ms · fps / 1000 + 0.5))`` frames, effective
  duration ``N / fps``; transition frame ``k`` (``0 … N−1``) is evaluated at ``u = k / N``;
* endpoint ownership: frame ``k = 0`` IS the outgoing shot (``E(0) = 0``, no band), and the
  incoming shot owns ``u = 1`` — its first frame after the overlap. No frame is duplicated or
  dropped; the overlap is ``N`` frames, like an xfade of ``N / fps``;
* geometry is normalized to the canvas; colour is the brand accent (``--pl-color-accent`` from the
  brand snapshot), opaque; edges are antialiased by exact pixel COVERAGE of the continuous
  geometry (a pixel ``[x, x+1)`` partly revealed gets that fraction), per plane;
* reduced motion: the N-frame interval is KEPT, as a stationary hard cut inside it — the outgoing
  shot owns ``k < ceil(N/2)``, the incoming one ``k ≥ ceil(N/2)`` (no band or fade); so the
  timeline and every reading hold are identical with or without it.

This implements the contract posted on protoContent#606 (``protolabs.brand-video/1``, schema 1):
band ``[max(0, f − W/80), f)``; paint order A → B → accent; the five analytic
reference samples at 2560×1440 are :data:`ORACLE` (the suite checks them). Blending happens in
Y′CbCr 4:2:0 — an affine map of the encoded sRGB values, i.e. non-premultiplied encoded-sRGB
blending, with chroma at half resolution; edges are antialiased by exact coverage, so they differ
from the pixel-centre hard-mask fixtures only within one output pixel of the analytical edge.

ffmpeg renders it with masks, not per-pixel expressions: the geometry depends only on x, so each
mask is a 1-pixel-thin ``geq`` strip per frame
(evaluated at ``u = n / N`` from the frame index, never from wall-clock), stretched to the canvas
and applied with ``maskedmerge`` — frame-exact, and ~100× faster than an xfade ``custom`` expr.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

PROFILE = "protolabs-video-transitions"
PROFILE_VERSION = "2.0.0"  # 2: Boot Seam removed
CONTRACT_ID = "protolabs.brand-video/1"  # protoContent#606, schemaVersion 1
CONTRACT_SCHEMA = 1
# The contract's analytic reference frames (2560×1440).
ORACLE = {
    "width": 2560,
    "height": 1440,
    "samples": [
        {"u": 0, "e": 0, "edgeX": 0, "band": None},
        {"u": 0.25, "e": 0.0625, "edgeX": 160, "band": [128, 160]},
        {"u": 0.5, "e": 0.5, "edgeX": 1280, "band": [1248, 1280]},
        {"u": 0.75, "e": 0.9375, "edgeX": 2400, "band": [2368, 2400]},
        {"u": 1, "e": 1, "edgeX": 2560, "band": None},
    ],
}
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
}
MANIFEST_PATH = Path(__file__).resolve().parent / "transitions.json"


def ease(u: float) -> float:
    """easeInOutCubic, exact."""
    u = min(1.0, max(0.0, float(u)))
    return 4 * u * u * u if u < 0.5 else 1 - (-2 * u + 2) ** 3 / 2


def frames(style: str, fps: float) -> int:
    """N — the transition's frame count at ``fps`` (refuses a non-finite / non-positive rate)."""
    if not (isinstance(fps, (int, float)) and math.isfinite(fps) and fps > 0):
        raise ValueError(f"fps must be a positive finite number, got {fps!r}")
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


def reduced_owner(k: int, n: int) -> str:
    """Reduced motion: who owns transition frame k of n — "a" for k < ceil(N/2), else "b"."""
    return "a" if k < math.ceil(n / 2) else "b"


def pixel(style: str, u: float, x: int, y: int, w: int, h: int, reduced: bool = False) -> dict[str, float]:
    """The reference compositing weights for ONE pixel: ``{"a", "b", "accent"}`` summing to 1 —
    what both renderers must produce (within raster tolerance)."""
    if reduced:
        return {"a": 1.0, "accent": 0.0, "b": 0.0} if u < 0.5 else {"a": 0.0, "accent": 0.0, "b": 1.0}
    if style == "edge_sweep":
        g = edge_sweep(u, w)
        c_edge = coverage(-1e9, g["reveal"], x)  # left of the reveal edge: band or incoming
        c_in = coverage(-1e9, g["band_x0"], x)  # left of the band: incoming
        return {"a": 1 - c_edge, "accent": c_edge - c_in, "b": c_in}
    raise ValueError(f"unknown transition {style!r}")


# ── ffmpeg ─────────────────────────────────────────────────────────────────────
def _u(n_frames: int) -> str:
    return f"(N/{n_frames})"


def _ease_expr(n_frames: int) -> str:
    u = _u(n_frames)
    return f"if(lt({u},0.5),4*{u}*{u}*{u},1-4*(1-{u})*(1-{u})*(1-{u}))"


def _strip(w: int, h: int, fps: float, n_frames: int, expr: str, *, vertical: bool = False) -> str:
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


def graph(
    style: str, a: str, b: str, out: str, *, w: int, h: int, fps: float, accent: str, tag: str, reduced: bool = False
) -> list[str]:
    """filter_complex chains that composite ``N`` frames of ``a`` (outgoing) and ``b`` (incoming),
    both exactly ``N`` frames long, into ``out``. ``accent`` is an ffmpeg colour (0xRRGGBB).
    ``reduced``: the same N frames as a hard cut at ``ceil(N/2)``."""
    n = frames(style, fps)
    if reduced:
        half = math.ceil(n / 2)
        return [
            f"{_strip(w, h, fps, n, f'255*gte(N,{half})', vertical=False)}[{tag}mr]",
            f"[{a}][{b}][{tag}mr]maskedmerge[{out}]",
        ]
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
    raise ValueError(f"unknown transition {style!r}")


# ── the shared contract (console parity) ───────────────────────────────────────
def manifest() -> dict[str, Any]:
    return {
        "contractId": CONTRACT_ID,
        "schemaVersion": CONTRACT_SCHEMA,
        "profile": PROFILE,
        "version": PROFILE_VERSION,
        "source": "protoLabsAI/protoContent#606 — the posted contract (Josh picked A Edge Sweep; C Boot Seam was removed)",
        "easing": {"id": EASING, "definition": "u<0.5 ? 4u^3 : 1-((-2u+2)^3)/2", "u": "clamp(elapsed/duration,0,1)"},
        "quantization": {
            "frames": "N = max(1, floor(duration_ms*fps/1000 + 0.5))",
            "effective_duration": "N/fps",
            "frame_u": "u = k/N for k = 0..N-1",
            "endpoints": "k=0 is the outgoing shot; the incoming shot owns u=1 (its first frame after the overlap)",
            "examples": {s: {str(f): frames(s, f) for f in (24, 30, 60)} for s in SIGNATURES},
        },
        "compositing": "opaque; per-pixel coverage of the continuous geometry; accent = --pl-color-accent",
        "reduced_motion": "the N-frame interval is kept: A owns k < ceil(N/2), B owns k >= ceil(N/2); no band/fade",
        "blending": "Y'CbCr 4:2:0 maskedmerge (affine in encoded sRGB: non-premultiplied); exact-coverage antialiasing",
        "oracle": ORACLE,
        "transitions": SIGNATURES,
    }


def write_manifest(path: Path = MANIFEST_PATH) -> None:
    path.write_text(json.dumps(manifest(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write_manifest()
    print(f"wrote {MANIFEST_PATH}")
