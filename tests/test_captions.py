"""Captions on campaign_render: strict spec keys (#51), source→output timing through the ramps,
the minimum hold, wrapping, bookends, and one real ffmpeg render read back frame by frame."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from campaign import captions, deps, montage, render
from conftest import have_chromium, have_ffmpeg

MARKS = {"start": 0.0, "typed": 2.0, "sent": 4.0, "card": 6.0, "board": 8.0, "end": 10.0}


def _spec(**kw):
    base = {"name": "flow", "format": "mp4", "bookends": "none"}
    base.update(kw)
    return render.normalize_output(base, MARKS, 10.0)


# ── strict keys (#51) ────────────────────────────────────────────────────────
def test_an_unknown_top_level_key_is_refused_naming_it_and_the_allowed_set():
    with pytest.raises(render.RenderError) as e:
        _spec(ramps=[{"from": 1, "to": 2, "factor": 2}])
    msg = str(e.value)
    assert "unknown key 'ramps'" in msg and "did you mean 'speed'?" in msg
    assert "allowed: " + ", ".join(render.OUTPUT_KEYS) in msg and "Nothing was rendered" in msg


def test_unknown_keys_inside_speed_crop_and_captions_are_refused():
    with pytest.raises(
        render.RenderError, match=r"speed ramp: unknown key 'speed_factor'.*allowed: from, to, factor, ease.*'factor'"
    ):
        _spec(speed=[{"from": 1, "to": 3, "speed_factor": 2}])
    with pytest.raises(render.RenderError, match=r"crop: unknown key 'w'.*did you mean 'width'"):
        _spec(crop={"x": 0, "y": 0, "w": 10, "width": 10, "height": 10})
    with pytest.raises(
        render.RenderError, match=r"captions\[2\]: unknown key 'duration' — allowed: text, start, end, at"
    ):
        _spec(captions=[{"text": "a", "at": 1}, {"text": "b", "start": 3, "duration": 2}])


def test_every_currently_valid_key_still_parses():
    s = render.normalize_output(
        {"name": "all", "format": "mp4", "title": "T", "start": "start", "end": "end", "continuous": True,
         "speed": [{"from": "typed", "to": "sent", "factor": 2, "ease": True}],
         "crop": {"x": 0, "y": 0, "width": 640, "height": 360}, "width": 640, "fps": 30, "crf": 18,
         "limit": "github_attachment_video_free", "max_bytes": 5_000_000, "at": 1, "bookends": "none",
         "captions": [{"text": "hi", "at": 1}]},
        MARKS, 10.0,
    )  # fmt: skip
    assert s["ramps"] == [(2.0, 4.0, 2.0)] and s["captions"][0]["text"] == "hi"
    poster = render.normalize_output({"name": "p", "format": "poster", "at": 2}, MARKS, 10.0)
    assert poster["captions"] == []


def test_caption_shapes_are_validated():
    assert _spec(captions=[{"text": "b", "at": "sent"}, {"text": "  a \n  x ", "start": 1, "end": "typed"}])[
        "captions"
    ] == [
        {"text": "a x", "start": 1.0, "end": 2.0, "n": 2},
        {"text": "b", "start": 4.0, "end": None, "n": 1},
    ], "sorted, whitespace collapsed, marks resolved"
    for bad, match in [
        ([{"text": "", "at": 1}], "text is empty"),
        ([{"text": "a"}], "needs start"),
        ([{"text": "a", "start": 1, "at": 2}], "start or at, not both"),
        ([{"text": "a", "start": 3, "end": 2}], "must be after start"),
        ([{"text": "a", "start": 1, "end": 5}, {"text": "b", "start": 4}], "overlaps"),
        (["just text"], "got a string"),
        ({"text": "a"}, "must be a list"),
    ]:
        with pytest.raises(render.RenderError, match=match):
            _spec(captions=bad)
    with pytest.raises(render.RenderError, match="not a poster"):
        render.normalize_output({"name": "p", "format": "poster", "captions": [{"text": "a", "at": 1}]}, MARKS, 10.0)


# ── timing ───────────────────────────────────────────────────────────────────
def test_source_times_map_through_multi_segment_eased_ramps():
    s = _spec(
        speed=[{"from": "typed", "to": "sent", "factor": 4, "ease": 0.5}, {"from": "card", "to": "board", "factor": 2, "ease": 0.3}],
        captions=[{"text": "chat", "at": 0.2}, {"text": "form", "start": 2.2, "end": 5.5},
                  {"text": "card", "start": "card", "end": 9.0}],
    )  # fmt: skip
    segs = render.segments(s["start"], s["end"], s["ramps"], s["eases"])
    timed, warnings = captions.plan(s["captions"], segs, 0.0, 10.0)
    # The ramp 2–4 s at 4× eased 0.5 lasts 2/4 + 0.5·0.75 = 0.875 s; 6–8 s at 2× eased 0.3 lasts 1 + 0.15.
    assert render.timeline_time(segs, 4.0) == pytest.approx(2.875)
    assert render.timeline_time(segs, 8.0) == pytest.approx(2.875 + 2 + 1.15)
    for c, src in zip(timed, [(0.2, 2.2), (2.2, 5.5), (6.0, 9.0)]):
        assert (c["start"], c["end"]) == pytest.approx(src), "source times are kept for a re-render"
    assert timed[0]["out_start"] == pytest.approx(0.2) and timed[0]["out_end"] == pytest.approx(
        render.timeline_time(segs, 2.2), abs=1e-3
    )
    assert timed[1]["out_start"] == pytest.approx(render.timeline_time(segs, 2.2), abs=1e-3)
    assert timed[1]["out_start"] < 2.2, "inside the ramp, the output clock runs behind the source"
    assert timed[1]["out_end"] == pytest.approx(2.875 + 1.5, abs=1e-3)
    assert timed[2]["out_start"] == pytest.approx(4.875)
    assert timed[2]["out_end"] == pytest.approx(2.875 + 2 + 1.15 + 1, abs=1e-3)
    assert warnings == []


def test_a_caption_squeezed_by_a_ramp_is_held_and_warned_and_the_next_waits():
    s = _spec(speed=[{"from": 1, "to": 5, "factor": 4, "ease": 0.4}],
              captions=[{"text": "fast part", "start": 2, "end": 4}, {"text": "next", "start": 4.5, "end": 8}])  # fmt: skip
    segs = render.segments(s["start"], s["end"], s["ramps"], s["eases"])
    timed, warnings = captions.plan(s["captions"], segs, 0.0, 10.0)
    first, second = timed
    assert first["out_end"] - first["out_start"] == pytest.approx(captions.MIN_HOLD_S)
    assert second["out_start"] == pytest.approx(first["out_end"]), "one caption at a time: the next waits"
    assert second["out_end"] - second["out_start"] >= captions.MIN_HOLD_S - 1e-6
    assert any("'fast part'" in w and "held to 1.8s" in w for w in warnings)
    assert any("'next'" in w and "starts" in w and "late" in w for w in warnings)
    assert (first["start"], first["end"]) == (2.0, 4.0), "the source times recorded are what was asked"


def test_a_short_caption_at_the_very_end_backs_up_rather_than_running_past_the_clip():
    s = _spec(captions=[{"text": "done", "at": 9.5}])
    segs = render.segments(s["start"], s["end"], s["ramps"], s["eases"])
    (c,), warnings = captions.plan(s["captions"], segs, 0.0, 10.0)
    assert c["out_end"] == pytest.approx(10.0) and c["out_start"] == pytest.approx(10.0 - captions.MIN_HOLD_S)
    assert any("early" in w for w in warnings)
    with pytest.raises(render.RenderError, match="outside the cut"):
        s2 = _spec(start=2, end=8, captions=[{"text": "late", "at": 9}])
        captions.plan(s2["captions"], render.segments(2, 8, []), 2.0, 8.0)


# ── layout ───────────────────────────────────────────────────────────────────
def test_text_wraps_to_two_lines_at_the_clip_width_and_longer_is_refused():
    line = "The agent builds a spec form from the chat and the user sends it to the board for triage"
    assert len(captions.wrap(line, 1920, 1080)) == 2
    assert captions.wrap("Send it", 1920, 1080) == ["Send it"]
    with pytest.raises(captions.CaptionError, match=r"wraps to [3-9] lines at 1080×1920 — at most 2 lines"):
        captions.wrap(line, 1080, 1920)  # 9:16 is narrower AND keeps clear of the action rail
    land, vert = captions.geometry(1920, 1080, 2), captions.geometry(1080, 1920, 2)
    assert vert["y"] + vert["block"] + vert["pad"] <= 1920 * 0.78 + 1, "vertical sits above the platform's bottom strip"
    assert vert["x"] + vert["max_w"] <= 1080 * 0.84, "and left of its right-hand rail"
    assert land["y"] < captions.geometry(1920, 1080, 1)["y"], "a second line grows the block upward"


def test_output_size_follows_crop_and_scale():
    s = _spec(crop={"x": 0, "y": 0, "width": 1000, "height": 562}, width=640)
    assert captions.output_size(s, {"width": 2560, "height": 1440}, 640) == (640, 360)
    assert captions.output_size(_spec(), {"width": 2560, "height": 1440}, None) == (2560, 1440)


# ── the render path (fake ffmpeg) ────────────────────────────────────────────
class FakeArt:
    engine, notes = "fake", []

    def __init__(self):
        self.sizes = []

    def pngs(self, timed, w, h):
        self.sizes.append((w, h))
        return [f"/tmp/cap{k}.png" for k in range(len(timed))]


def test_render_overlays_each_caption_in_its_output_window_after_the_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(deps, "ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(deps, "ffprobe", lambda: "/usr/bin/ffprobe")
    cmds = []

    def ff(cmd):
        cmds.append(cmd)
        if cmd[0].endswith("ffprobe"):
            return subprocess.CompletedProcess(cmd, 0, '{"streams":[{"width":1280,"height":720,"duration":"6"}]}', "")
        Path(cmd[-1]).write_bytes(b"0" * 100)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    s = _spec(speed=[{"from": "typed", "to": "sent", "factor": 4, "ease": 0.5}],
              captions=[{"text": "a", "at": 1}, {"text": "b", "start": 2.5, "end": 3.5}, {"text": "c", "at": "card"}])  # fmt: skip
    art = FakeArt()
    r = render.render_output("in.webm", tmp_path, s, marks=MARKS, source={"width": 1280, "height": 720, "duration_s": 10},
                             runner=ff, caption_art=art)  # fmt: skip
    cmd = cmds[0]
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert cmd.count("-loop") == 3 and art.sizes == [(1280, 720)]
    assert "[base]fps=30[cfps]" in fc and fc.endswith(";[framed]format=yuv420p[out]")
    starts = [float(x) for x in re.findall(r"PTS-STARTPTS\+([0-9.]+)/TB", fc)]
    assert starts == pytest.approx([c["out_start"] for c in r["captions"]], abs=1e-3)
    durs = [float(cmd[i + 1]) for i, a in enumerate(cmd) if a == "-t"]
    assert all(c["out_end"] <= 10 - 2 + 0.875 + 1e-3 for c in r["captions"]), "inside the clip's own timeline"
    assert durs == pytest.approx([c["out_end"] - c["out_start"] for c in r["captions"]], abs=1e-3)
    assert "fade=t=in:st=0:d=0.200:alpha=1" in fc and r["caption_engine"] == "fake"
    assert any("'b'" in w and "held" in w for w in r["warnings"]), "the 1 s caption inside the 4× ramp is held"
    # A caption list that can't fit is refused before ffmpeg runs.
    cmds.clear()
    long = _spec(captions=[{"text": "word " * 39, "at": 1}])
    with pytest.raises(captions.CaptionError, match="at most 2 lines"):
        render.render_output("in.webm", tmp_path, long, marks=MARKS, source={"width": 320, "height": 180, "duration_s": 10},
                             runner=ff, caption_art=art)  # fmt: skip
    assert cmds == []


def test_bookends_wrap_the_captioned_clip_and_the_cards_get_none(tmp_path, monkeypatch):
    from campaign import store, tools

    c = store.create_campaign("Launch")
    clip = tmp_path / "flow.mp4"
    clip.write_bytes(b"x")
    meta = tools._caption_meta({"captions": [{"text": "a", "start": 1.0, "end": 2.0, "out_start": 1.0, "out_end": 2.8}],
                                "caption_engine": "drawtext"})  # fmt: skip
    assert meta["captions"] == [{"text": "a", "start": 1.0, "end": 2.0}], "re-passable as captions=… as-is"
    assert meta["caption_timing"] == [{"text": "a", "out_start": 1.0, "out_end": 2.8}]
    assert render.normalize_output({"name": "again", "format": "mp4", "captions": meta["captions"]}, {}, 10.0)[
        "captions"
    ]
    a = store.add_asset(c["id"], "clip", "flow", path=str(clip), status="rendered", meta={"spec": {}, **meta})
    seen = {}

    def fake(campaign_id, seq, opts, out_dir, **kw):
        seen.update(seq=seq, opts=opts)
        out = Path(out_dir) / "flow-bookends.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"y")
        return {"path": str(out), "size_bytes": 1, "width": 1280, "height": 720, "duration_s": 20.0}

    monkeypatch.setattr(tools.montage, "render_montage", fake)
    monkeypatch.setattr(tools.intromod, "preset", lambda: {"x": 1})
    monkeypatch.setattr(tools.outromod, "preset", lambda: {"x": 1})
    spec = render.normalize_output(
        {"name": "flow", "format": "mp4", "title": "Flow", "captions": meta["captions"]}, {}, 10.0
    )
    tools._bookend_render(c, a, spec, {"path": str(clip), "width": 1280, "height": 720}, tmp_path)
    assert seen["seq"] == [{"clip": a["id"]}], "the cards wrap the already-captioned clip — no caption or label on them"
    assert store.get_asset(a["id"])["meta"]["captions"] == meta["captions"], "what was burned in survives bookending"


# ── the real thing ───────────────────────────────────────────────────────────
def _region_luma(path: Path, t: float, box: tuple[int, int, int, int]) -> float:
    """Mean luma of one rectangle of the frame shown at output time ``t``."""
    x, y, w, h = box
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-vf", f"crop={w}:{h}:{x}:{y},format=gray",
         "-frames:v", "1", "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return sum(raw) / max(1, len(raw))


@pytest.mark.integration
@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg/ffprobe not installed")
def test_real_captions_burn_in_at_the_mapped_times(tmp_path, monkeypatch):
    ff = shutil.which("ffmpeg")
    monkeypatch.setattr(deps, "ffmpeg", lambda: ff)
    monkeypatch.setattr(deps, "ffprobe", lambda: shutil.which("ffprobe"))
    if "drawtext" in montage.ffmpeg_filters(ff) and montage.find_font()[0]:
        art = captions.Art(tmp_path / "art", engine="auto")
    elif have_chromium():
        art = captions.Art(tmp_path / "art", engine="html")
    else:
        pytest.skip("neither drawtext nor Chromium — captions can't be drawn here")
    # A flat dark take, so the caption box is the only bright thing in the lower third.
    src = tmp_path / "take.webm"
    subprocess.run(
        [ff, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x202020:s=640x360:r=25", "-t", "6",
         "-c:v", "libvpx-vp9", "-b:v", "200k", str(src)],
        check=True,
    )  # fmt: skip
    marks = {"a": 1.0, "b": 2.5, "c": 4.0, "d": 5.0}
    source = render.probe(src)
    spec = render.normalize_output(
        {"name": "cap", "format": "mp4", "bookends": "none",
         "speed": [{"from": "a", "to": "b", "factor": 3, "ease": 0.3}, {"from": "c", "to": "d", "factor": 2, "ease": 0.2}],
         "captions": [{"text": "One", "start": 0.2, "end": "a"}, {"text": "Two, inside the ramp", "start": 1.2, "end": 2.3},
                      {"text": "Three", "at": 4.5}]},
        marks, source["duration_s"],
    )  # fmt: skip
    r = render.render_output(src, tmp_path / "out", spec, marks=marks, source=source, caption_art=art)
    assert src.stat().st_size and render.probe(src)["duration_s"] == source["duration_s"], "the take is untouched"
    timed = r["captions"]
    segs = render.segments(spec["start"], spec["end"], spec["ramps"], spec["eases"])
    assert timed[0]["out_start"] == pytest.approx(render.timeline_time(segs, 0.2), abs=1e-3)
    out = Path(r["path"])
    g = captions.geometry(640, 360, 1)
    box = (g["x"], g["y"], 24, g["size"])  # the start of the first line: text on the brand box
    bar = (g["x"] - g["pad"] - g["bar"], g["y"], g["bar"], g["size"])
    blank = _region_luma(out, 0.05, box)
    for c in timed:
        mid = (c["out_start"] + c["out_end"]) / 2
        assert abs(_region_luma(out, mid, box) - blank) > 12, f"caption {c['text']!r} is up at {mid:.2f}s"
        assert abs(_region_luma(out, mid, bar) - blank) > 12, "with its accent bar"
    assert timed[1]["out_start"] >= timed[0]["out_end"] - 1e-6, "one at a time"
    if timed[2]["out_start"] - timed[1]["out_end"] > 0.6:
        between = (timed[1]["out_end"] + timed[2]["out_start"]) / 2
        assert abs(_region_luma(out, between, box) - blank) < 4, "nothing up between captions"
