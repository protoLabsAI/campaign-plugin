"""Let the agent SEE its own media — stills, and frames pulled from clips — as image content.

The asset-review self-check ("legible at half size? anything secret on screen?") is
meaningless if the model only ever reads a file path. :func:`view` turns one campaign file
into at most a few downscaled JPEGs:

* a still / card / poster → the image itself, its long side ≤ ``max_side``;
* a clip or GIF → ``frames`` evenly spaced frames (1–3 come back one image each, full
  ``max_side``; 4–12 come back as ONE contact sheet in reading order);
* ``around=<mark or seconds>`` → a frame just before and just after that moment (a cut, a
  speed-ramp boundary, a mark), each full size.

The tool wraps the result in protoAgent core's ``multimodal_tool_result`` envelope (via
``graph.sdk``, looked up lazily and getattr-guarded — older cores lack it), which the host's
middleware turns into image blocks on a vision model. Core caps an envelope at 3 images of
≤ 2 MiB each, so this module never builds more and re-encodes until each fits.

Every path is contained to the campaign's own media directory (symlinks resolved). ffmpeg
does the decoding/scaling — no image library is needed in the host.
"""

from __future__ import annotations

import base64
import math
import tempfile
from pathlib import Path
from typing import Any

from . import deps, paths, render, store

STILL_EXT = (".png", ".jpg", ".jpeg", ".webp")
MOTION_EXT = (".webm", ".mp4", ".mov", ".mkv", ".m4v", ".gif")
MAX_IMAGES = 3  # core's MAX_IMAGES_PER_RESULT
MAX_IMAGE_BYTES = 2 * 1024 * 1024  # core's MAX_IMAGE_BYTES (decoded)
MAX_FRAMES = 12
DEFAULT_MAX_SIDE = 1280
MIN_SIDE, MAX_SIDE = 320, 2048
AROUND_OFFSET_S = 0.3  # how far either side of `around` the two frames are taken
JPEG_QUALITIES = (3, 6, 10, 16)  # ffmpeg -q:v (lower = better)


class LookError(ValueError):
    pass


def resolve(campaign_id: int, asset_id: int = 0, path: str = "") -> tuple[Path, dict[str, Any] | None]:
    """The file to look at — an asset of THIS campaign, or a path inside its media dir."""
    c = store.require_campaign(campaign_id)
    root = paths.campaign_dir(c["id"], c["name"])
    asset = None
    if asset_id:
        asset = store.get_asset(asset_id)
        if asset is None or asset["campaign_id"] != int(campaign_id):
            raise LookError(f"no asset #{asset_id} in campaign {campaign_id} — campaign_assets lists them")
        if not asset.get("path"):
            raise LookError(f"asset #{asset_id} ({asset['kind']}, {asset['status']}) has no file yet")
        raw = asset["path"]
    elif path:
        raw = path
    else:
        raise LookError("pass asset_id (from campaign_assets) or a path inside the campaign's media dir")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = root / p
    if not paths.is_contained(p, root):
        raise LookError(f"{raw} is not a file inside this campaign's media dir ({root})")
    p = p.resolve()
    if not p.is_file():
        raise LookError(f"{raw} is not a file")
    if p.suffix.lower() not in STILL_EXT + MOTION_EXT:
        raise LookError(f"{p.name}: can only look at {', '.join(STILL_EXT + MOTION_EXT)} files")
    return p, asset


def _scale(max_w: int, max_h: int) -> str:
    return f"scale='min({max_w},iw)':'min({max_h},ih)':force_original_aspect_ratio=decrease"


def _encode(runner, ff: str, src_args: list[str], vf: str, out: Path) -> bytes:
    """Run ffmpeg to one JPEG, stepping quality down until it fits core's per-image cap."""
    for q in JPEG_QUALITIES:
        render._ff(runner, [ff, "-v", "error", "-y", *src_args, "-frames:v", "1", "-vf", vf, "-q:v", str(q), str(out)])
        if not out.is_file() or out.stat().st_size == 0:
            raise LookError(f"ffmpeg produced no image for {out.name}")
        data = out.read_bytes()
        if len(data) <= MAX_IMAGE_BYTES:
            return data
    raise LookError(f"{out.name} stays over {MAX_IMAGE_BYTES} bytes even at low quality — lower max_side")


def _frame(runner, ff: str, src: Path, t: float, max_w: int, max_h: int, out: Path) -> bytes:
    return _encode(runner, ff, ["-ss", f"{t:.3f}", "-i", str(src)], _scale(max_w, max_h), out)


def frame_times(duration: float, n: int) -> list[float]:
    """``n`` evenly spaced times — the centres of n equal slices, so neither the blank first
    frame nor the (possibly missing) last one is picked."""
    if duration <= 0:
        return [0.0]
    return [round(duration * (i + 0.5) / n, 3) for i in range(n)]


def _clamp(t: float, duration: float) -> float:
    return round(min(max(0.0, t), max(0.0, duration - 0.05)), 3)


def view(
    campaign_id: int,
    asset_id: int = 0,
    path: str = "",
    *,
    frames: int = 3,
    around: str = "",
    max_side: int = DEFAULT_MAX_SIDE,
    runner=None,
) -> dict[str, Any]:
    """Build the images. Returns ``{"text": caption, "images": [{"b64", "mime", "label"}]}``."""
    runner = runner or render._default_runner
    src, asset = resolve(campaign_id, asset_id, path)
    try:
        max_side = int(max_side)
        frames = int(frames)
    except (TypeError, ValueError):
        raise LookError("frames and max_side must be integers") from None
    if not MIN_SIDE <= max_side <= MAX_SIDE:
        raise LookError(f"max_side must be {MIN_SIDE}..{MAX_SIDE}")
    if not 0 <= frames <= MAX_FRAMES:
        raise LookError(f"frames must be 0..{MAX_FRAMES} (4+ come back as one contact sheet)")
    ff = deps.ffmpeg()
    label = f"asset #{asset['id']} ({asset['kind']}, {asset['status']})" if asset else src.name
    head = [f"{label} — {src}"]
    images: list[dict[str, Any]] = []
    is_still = src.suffix.lower() in STILL_EXT

    if is_still:
        if around:
            raise LookError("`around` is for clips — a still is a single frame")
        if ff:
            with tempfile.TemporaryDirectory(prefix="campaign-look-") as td:
                data = _encode(runner, ff, ["-i", str(src)], _scale(max_side, max_side), Path(td) / "still.jpg")
            images.append({"b64": base64.b64encode(data).decode(), "mime": "image/jpeg", "label": "still"})
        else:
            raw = src.read_bytes()
            mime = {".png": "image/png", ".webp": "image/webp"}.get(src.suffix.lower(), "image/jpeg")
            if len(raw) > MAX_IMAGE_BYTES:
                raise LookError(
                    f"{src.name} is {len(raw)} bytes — over the {MAX_IMAGE_BYTES}-byte image cap, and "
                    "ffmpeg (needed to downscale it) isn't available. " + deps.ffmpeg_hint()
                )
            images.append({"b64": base64.b64encode(raw).decode(), "mime": mime, "label": "still (full size)"})
        return {"text": "\n".join(head), "images": images}

    if not ff:
        raise LookError("pulling frames from a clip needs ffmpeg — " + deps.ffmpeg_hint())
    if frames == 0 and not around:
        raise LookError("ask for frames (1–12) and/or around=<mark or seconds>")
    try:
        info = render.probe(src, runner)
    except render.RenderError as e:
        raise LookError(str(e)) from None
    dur = float(info.get("duration_s") or 0)
    head.append(f"{info.get('width')}×{info.get('height')}, {dur:.2f}s")
    marks = ((asset or {}).get("meta") or {}).get("marks") or {}
    if marks:
        head.append("marks: " + ", ".join(f"{k}={v:.2f}s" for k, v in marks.items()))

    with tempfile.TemporaryDirectory(prefix="campaign-look-") as td:
        tmp = Path(td)
        if around:
            try:
                t = render.resolve_time(around, marks, "around")
            except render.RenderError as e:
                parent = (asset or {}).get("parent_id")
                hint = (
                    f" — marks are timed on the recorded take (asset #{parent}); view that, or give seconds in this file"
                    if parent and not marks
                    else ""
                )
                raise LookError(f"{e}{hint}") from None
            if t is None or t > dur + 0.05:
                raise LookError(f"around={around!r} is past the end of this {dur:.2f}s file")
            for side, at in (("before", _clamp(t - AROUND_OFFSET_S, dur)), ("after", _clamp(t + AROUND_OFFSET_S, dur))):
                data = _frame(runner, ff, src, at, max_side, max_side, tmp / f"{side}.jpg")
                images.append(
                    {
                        "b64": base64.b64encode(data).decode(),
                        "mime": "image/jpeg",
                        "label": f"{side} {around} @ {at:.2f}s",
                    }
                )
        room = MAX_IMAGES - len(images)
        one_each = frames == 1 or (not around and frames <= room)
        if frames and one_each:
            for i, at in enumerate(frame_times(dur, frames), start=1):
                at = _clamp(at, dur)
                data = _frame(runner, ff, src, at, max_side, max_side, tmp / f"f{i:03d}.jpg")
                images.append(
                    {"b64": base64.b64encode(data).decode(), "mime": "image/jpeg", "label": f"frame @ {at:.2f}s"}
                )
        elif frames:
            cols = min(frames, 4 if frames > 9 else 3)
            rows = math.ceil(frames / cols)
            cell_w, cell_h = max(64, (max_side - 4 * (cols - 1)) // cols), max(64, (max_side - 4 * (rows - 1)) // rows)
            times = [_clamp(t, dur) for t in frame_times(dur, frames)]
            for i, at in enumerate(times, start=1):
                _frame(runner, ff, src, at, cell_w, cell_h, tmp / f"cell{i:03d}.jpg")
            data = _encode(
                runner,
                ff,
                ["-framerate", "1", "-start_number", "1", "-i", str(tmp / "cell%03d.jpg")],
                f"tile={cols}x{rows}:padding=4:color=black",
                tmp / "sheet.jpg",
            )
            images.append(
                {
                    "b64": base64.b64encode(data).decode(),
                    "mime": "image/jpeg",
                    "label": f"contact sheet {cols}×{rows}, left→right, top→bottom @ "
                    + ", ".join(f"{t:.2f}s" for t in times),
                }
            )
    return {"text": "\n".join(head), "images": images}


def caption(result: dict[str, Any]) -> str:
    lines = [result["text"], ""]
    lines += [f"image {i}: {img['label']}" for i, img in enumerate(result["images"], start=1)]
    return "\n".join(lines)
