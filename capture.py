"""Device-pixel capture — a ``capture: device`` take's frames → one constant-frame-rate H.264 mp4.

The worker (``ScreencastRecorder`` in ``worker/pw_worker.py``) writes the page's DEVICE-pixel
frames as JPEGs plus ``frames.json``: each frame's file and its time on the take's clock (seconds
since page creation — the clock every ``mark`` uses), and ``end_s``, when recording stopped.
Chromium only sends a frame when the page's pixels change, so that is a variable-frame-rate
sequence with gaps wherever the page sat still.

This module turns it into a constant-frame-rate video, on the HOST (it has ffmpeg; the worker
stays stdlib + playwright):

* :func:`cfr_schedule` — for every output tick ``k / fps`` over ``[0, end_s)``, the frame on
  screen at that moment: the latest one stamped at or before it (half a tick of tolerance, so a
  frame lands on its NEAREST tick). An idle stretch holds the last frame; the first frame is held
  back to t=0 (the page was blank-then-loading before Chromium's first paint anyway); the last is
  held to ``end_s``. So the video's t is the take's clock — marks line up — and its length is the
  take's length.
* :func:`assemble` — hardlinks (or copies) the scheduled frames into a numbered sequence and
  encodes it: libx264, yuv420p, ``crf`` (16 by default), ``+faststart``. yuv420p, not 4:4:4: the
  take must play in the gallery and every browser (4:4:4 H.264 doesn't), and at 2× device pixels
  the halved chroma is still at CSS-pixel resolution. (ffmpeg's concat demuxer with per-file
  durations was tried first and rejected: an image's timestamps get quantized to 1/25 s and the
  output ran 25% long.)

Pure apart from the ffmpeg call, whose runner is injectable — the suite tests it host-free.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from . import deps, render


class CaptureError(RuntimeError):
    pass


def clean_frames(frames: list[Any]) -> list[tuple[str, float]]:
    """``[[file, t], …]`` → ``(file, t)`` sorted by time, negatives clamped to 0, junk dropped.
    A frame stamped no later than the one before it would never be on screen: the later-arriving
    of two equal stamps wins."""
    out: list[tuple[str, float]] = []
    for f in frames or []:
        try:
            name, t = str(f[0]), max(0.0, float(f[1]))
        except (TypeError, ValueError, IndexError):
            continue
        out.append((name, t))
    out.sort(key=lambda x: x[1])
    dedup: list[tuple[str, float]] = []
    for name, t in out:
        if dedup and t <= dedup[-1][1]:
            dedup[-1] = (name, t)
        else:
            dedup.append((name, t))
    return dedup


def cfr_schedule(times: list[float], end_s: float, fps: float) -> list[int]:
    """The frame index on screen at each output tick ``k / fps`` for ``k`` in ``0 .. n-1``,
    ``n = round(end_s * fps)`` (at least one tick when there are frames). ``times`` must be
    sorted (see :func:`clean_frames`). Ticks before the first frame show the first frame;
    ticks after the last show the last."""
    if not times:
        return []
    end = max(float(end_s), times[-1])
    n = max(1, int(round(end * fps)))
    half = 0.5 / fps
    out: list[int] = []
    i = 0
    for k in range(n):
        tick = k / fps + half
        while i + 1 < len(times) and times[i + 1] <= tick:
            i += 1
        out.append(i)
    return out


def encode_cmd(ff: str, pattern: str, dst: str, *, fps: int, crf: int, width: int, height: int) -> list[str]:
    # JPEG frames are full-range (yuvj420p); convert to limited (tv) range so the mp4 is plain
    # yuv420p — the yuvj formats are deprecated and some players mis-handle them.
    vf = f"scale={width}:{height}:flags=lanczos:in_range=pc:out_range=tv,format=yuv420p"
    return [
        ff, "-y", "-hide_banner", "-loglevel", "error", "-framerate", str(fps), "-start_number", "0",
        "-i", pattern, "-vf", vf, "-an", "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-color_range", "tv", "-r", str(fps), "-movflags", "+faststart", dst,
    ]  # fmt: skip


def _link(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except OSError:  # a filesystem without hardlinks
        shutil.copyfile(src, dst)


def assemble(
    manifest_path: str | Path,
    dst: str | Path,
    *,
    fps: int = 30,
    crf: int = 16,
    runner: render.Runner | None = None,
    keep_frames: bool = False,
) -> dict[str, Any]:
    """Encode a ``frames.json`` into ``dst`` (an .mp4). Returns
    ``{video, width, height, fps, frames, ticks, duration_s}``; raises :class:`CaptureError`.
    The frame directory is removed once the mp4 is written (it is ~0.3–0.6 GB a minute)."""
    manifest_path = Path(manifest_path)
    try:
        man = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CaptureError(f"the capture's frame list couldn't be read: {e}") from None
    fdir = Path(man.get("dir") or manifest_path.parent)
    frames = [(n, t) for n, t in clean_frames(man.get("frames") or []) if (fdir / n).is_file()]
    if not frames:
        raise CaptureError("the browser sent no frames — nothing was painted, so there is no video")
    size = man.get("size") or {}
    w, h = int(size.get("width") or 0) // 2 * 2, int(size.get("height") or 0) // 2 * 2
    if not (w and h):
        raise CaptureError("the capture's frame size is missing")
    ff = deps.ffmpeg()
    if not ff:
        raise CaptureError("capture: device needs ffmpeg to assemble the take — " + deps.ffmpeg_hint())
    sched = cfr_schedule([t for _, t in frames], float(man.get("end_s") or 0), fps)
    seq = fdir / "seq"
    shutil.rmtree(seq, ignore_errors=True)
    seq.mkdir(parents=True)
    for k, i in enumerate(sched):
        _link(fdir / frames[i][0], seq / f"{k:07d}.jpg")
    dst = Path(dst)
    try:
        render._ff(
            runner or render._default_runner,
            encode_cmd(ff, str(seq / "%07d.jpg"), str(dst), fps=fps, crf=crf, width=w, height=h),
        )
    except render.RenderError as e:
        raise CaptureError(f"assembling the take failed: {e}") from None
    finally:
        shutil.rmtree(seq, ignore_errors=True)
    if not dst.is_file():
        raise CaptureError("ffmpeg wrote no video")
    if not keep_frames:
        shutil.rmtree(fdir, ignore_errors=True)
    return {
        "video": str(dst),
        "width": w,
        "height": h,
        "fps": fps,
        "frames": len(frames),
        "ticks": len(sched),
        "duration_s": round(len(sched) / fps, 3),
    }
