"""Render: ffmpeg command building + the size ladders (fake runner), and one real render."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from campaign import deps, render, shotscript
from conftest import have_ffmpeg

MARKS = {"start": 1.0, "typed": 3.0, "dialog": 7.0, "end": 9.0}


class FakeFF:
    """Pretends to be ffmpeg/ffprobe: ffmpeg writes its output at the next queued size."""

    def __init__(self, sizes=(), probe=None):
        self.sizes = list(sizes)
        self.cmds: list[list[str]] = []
        self.probe = probe or {"width": 1280, "height": 800, "duration": "8.0"}

    def __call__(self, cmd):
        self.cmds.append(cmd)
        if cmd[0].endswith("ffprobe"):
            out = {
                "streams": [
                    {"width": self.probe["width"], "height": self.probe["height"], "duration": self.probe["duration"]}
                ]
            }
            return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")
        dst = Path(cmd[-1])
        dst.write_bytes(b"0" * (self.sizes.pop(0) if self.sizes else 1000))
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture(autouse=True)
def fake_bins(monkeypatch):
    monkeypatch.setattr(deps, "ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(deps, "ffprobe", lambda: "/usr/bin/ffprobe")


def _spec(**kw):
    base = {"name": "hero", "format": "mp4"}
    base.update(kw)
    return render.normalize_output(base, MARKS, 10.0)


def test_times_resolve_from_marks_or_seconds():
    assert render.resolve_time("typed", MARKS, "x") == 3.0
    assert render.resolve_time("mark:dialog", MARKS, "x") == 7.0
    assert render.resolve_time(2.5, MARKS, "x") == 2.5
    assert render.resolve_time("4", MARKS, "x") == 4.0
    with pytest.raises(render.RenderError, match="marks: dialog, end, start, typed"):
        render.resolve_time("nope", MARKS, "x")


def test_segments_split_around_speed_ramps():
    segs = render.segments(1.0, 9.0, [(3.0, 7.0, 4.0)])
    assert segs == [(1.0, 3.0, 1.0), (3.0, 7.0, 4.0), (7.0, 9.0, 1.0)]
    assert render.segments(0.0, None, []) == [(0.0, None, 1.0)]


def test_spec_validation():
    with pytest.raises(render.RenderError, match="format"):
        _spec(format="mov")
    with pytest.raises(render.RenderError, match="end"):
        _spec(start="dialog", end="typed")
    with pytest.raises(render.RenderError, match="overlap"):
        _spec(speed=[{"from": 1, "to": 5, "factor": 2}, {"from": 4, "to": 6, "factor": 2}])
    with pytest.raises(render.RenderError, match="unknown limit"):
        _spec(limit="nope")
    with pytest.raises(render.RenderError, match="past the end"):
        _spec(start=12)
    s = _spec(
        limit="github_attachment_video_free", max_bytes=8_000_000, crop={"x": 0, "y": 0, "width": 1001, "height": 601}
    )
    assert s["max_bytes"] == 8_000_000, "the tighter of max_bytes and the limit wins"
    assert s["crop"]["width"] == 1000 and s["crop"]["height"] == 600, "crop dims forced even"


def test_clips_are_continuous_by_default_so_steep_ramps_are_flagged_as_jump_cuts():
    # Operator feedback on real launch clips: "too many cut frames, missing chunks of action".
    # The timeline has no interior cuts; a steep ramp is the one way left to skip action. It is
    # flagged, not refused, so every factor in 0.25..32 that was valid before still renders.
    s = _spec(speed=[{"from": "typed", "to": "dialog", "factor": 4}])
    assert s["continuous"] is True and s["ramps"] == [(3.0, 7.0, 4.0)] and s["warnings"] == []
    steep = _spec(speed=[{"from": "typed", "to": "dialog", "factor": 8}])
    assert steep["ramps"] == [(3.0, 7.0, 8.0)], "still valid, still rendered"
    assert len(steep["warnings"]) == 1 and "jump cut" in steep["warnings"][0] and "3.00–7.00s" in steep["warnings"][0]
    lapse = _spec(continuous=False, speed=[{"from": "typed", "to": "dialog", "factor": 32}])
    assert lapse["ramps"] == [(3.0, 7.0, 32.0)] and lapse["warnings"] == [], "an explicit time-lapse is silent"
    with pytest.raises(render.RenderError, match="continuous must be true or false"):
        _spec(continuous="no")
    segs = render.segments(s["start"], s["end"], s["ramps"])
    assert all(b1 == a2 for (_, b1, _), (a2, _, _) in zip(segs, segs[1:])), "no gap between pieces"


def test_a_jump_cut_warning_reaches_the_render_report_without_blocking(tmp_path):
    ff = FakeFF()
    s = _spec(speed=[{"from": "typed", "to": "dialog", "factor": 8}])
    r = render.render_output("in.webm", tmp_path, s, marks=MARKS, source={"width": 1280}, runner=ff)
    assert r["violations"] == [] and r["warnings"] and "jump cut" in r["warnings"][0]


def test_filter_graph_trims_ramps_concats_crops_and_scales():
    s = _spec(
        start="start",
        end="end",
        speed=[{"from": "typed", "to": "dialog", "factor": 4}],
        crop={"x": 10, "y": 20, "width": 800, "height": 600},
    )
    fc = render.timeline_filter(render.segments(s["start"], s["end"], s["ramps"]), s["crop"], 640)
    assert fc.startswith("[0:v]split=3[s0][s1][s2]")
    assert "[s1]trim=start=3:end=7,setpts=PTS-STARTPTS,setpts=PTS/4[v1]" in fc
    assert "[v0][v1][v2]concat=n=3:v=1:a=0[cat]" in fc
    assert fc.endswith("[cat]crop=800:600:10:20,scale=640:-2:flags=lanczos[base]")


def test_mp4_command_is_h264_yuv420p_faststart():
    cmd = render.mp4_cmd("ffmpeg", "in.webm", "out.mp4", "[0:v]null[base]", crf=23, fps=30)
    joined = " ".join(cmd)
    for flag in ("-c:v libx264", "-pix_fmt yuv420p", "-movflags +faststart", "-crf 23", "-an"):
        assert flag in joined
    assert "fps=30,format=yuv420p[out]" in joined


def test_gif_command_uses_palettegen_and_paletteuse():
    joined = " ".join(render.gif_cmd("ffmpeg", "in.webm", "out.gif", "[0:v]null[base]", fps=12))
    assert "palettegen=stats_mode=diff" in joined and "paletteuse=dither=bayer" in joined and "fps=12" in joined


def test_mp4_ladder_steps_crf_then_width_until_it_fits(tmp_path):
    ff = FakeFF(sizes=[12_000_000, 11_000_000, 9_500_000])
    s = _spec(limit="github_attachment_video_free")
    r = render.render_output("in.webm", tmp_path, s, marks=MARKS, source={"width": 2560}, runner=ff)
    assert [a["crf"] for a in r["attempts"]] == [23, 27, 31]
    assert r["violations"] == [] and r["size_bytes"] == 9_500_000
    assert ladder_monotonic(render.mp4_ladder(23, None, 2560))


def ladder_monotonic(ladder):
    return all(b[0] >= a[0] or (b[1] or 0) < (a[1] or 0) for a, b in zip(ladder, ladder[1:]))


def test_gif_ladder_steps_fps_then_width(tmp_path):
    ff = FakeFF(sizes=[30_000_000, 20_000_000, 9_000_000])
    s = _spec(format="gif", limit="github_attachment_image", width=1280)
    r = render.render_output("in.webm", tmp_path, s, marks=MARKS, source={"width": 2560}, runner=ff)
    assert [(a["fps"], a["width"]) for a in r["attempts"]] == [(15.0, 1280), (12, 1280), (12, 1088)]
    assert r["violations"] == []


def test_an_output_that_never_fits_is_reported_not_hidden(tmp_path):
    ff = FakeFF(sizes=[50_000_000] * 20)
    s = _spec(format="gif", limit="github_attachment_image")
    r = render.render_output("in.webm", tmp_path, s, marks=MARKS, source={"width": 1280}, runner=ff)
    assert len(r["attempts"]) == len(render.gif_ladder(None, None, 1280))
    assert r["violations"] and "still over the 10.00 MB ceiling" in r["violations"][0]


def test_no_ceiling_means_one_attempt(tmp_path):
    ff = FakeFF(sizes=[99_000_000])
    r = render.render_output("in.webm", tmp_path, _spec(), marks=MARKS, source={"width": 1280}, runner=ff)
    assert len(r["attempts"]) == 1 and r["violations"] == []


def test_poster_seeks_to_its_mark(tmp_path):
    ff = FakeFF()
    s = _spec(format="poster", at="dialog", width=1280)
    r = render.render_output("in.webm", tmp_path, s, marks=MARKS, source={"width": 2560}, runner=ff)
    cmd = ff.cmds[0]
    assert cmd[cmd.index("-ss") + 1] == "7" and "-frames:v" in cmd and r["path"].endswith("hero.png")


def test_ffmpeg_failure_surfaces_its_stderr(tmp_path):
    def broken(cmd):
        return subprocess.CompletedProcess(cmd, 1, "", "line1\nInvalid argument")

    with pytest.raises(render.RenderError, match="Invalid argument"):
        render.render_output("in.webm", tmp_path, _spec(), marks=MARKS, source={}, runner=broken)


def test_missing_ffmpeg_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setattr(deps, "ffmpeg", lambda: None)
    with pytest.raises(render.RenderError, match="ffmpeg isn't on PATH"):
        render.render_output("in.webm", tmp_path, _spec(), marks=MARKS, source={})


# ── the real thing ───────────────────────────────────────────────────────────
@pytest.mark.integration
@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg/ffprobe not installed")
def test_real_render_of_a_tiny_take(tmp_path, monkeypatch):
    import shutil

    monkeypatch.setattr(deps, "ffmpeg", lambda: shutil.which("ffmpeg"))
    monkeypatch.setattr(deps, "ffprobe", lambda: shutil.which("ffprobe"))
    src = tmp_path / "take.webm"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x400:rate=25", "-t", "4",
         "-c:v", "libvpx-vp9", "-b:v", "300k", str(src)],
        check=True,
    )  # fmt: skip
    marks = {"a": 0.5, "b": 1.5, "c": 3.5}
    source = render.probe(src)
    assert source["width"] == 640 and 3.5 < source["duration_s"] <= 4.1
    out = tmp_path / "out"
    mp4 = render.render_output(
        src, out, render.normalize_output({"name": "clip", "format": "mp4", "start": "a", "end": "c",
                                           "speed": [{"from": "b", "to": "c", "factor": 4}],
                                           "limit": "github_attachment_video_free"}, marks, source["duration_s"]),
        marks=marks, source=source,
    )  # fmt: skip
    # 1s at 1× + 2s at 4× = 1.5s
    assert 1.3 <= mp4["duration_s"] <= 1.7 and mp4["violations"] == []
    gif = render.render_output(
        src,
        out,
        render.normalize_output(
            {"name": "loop", "format": "gif", "width": 320, "max_bytes": 400_000}, marks, source["duration_s"]
        ),
        marks=marks,
        source=source,
    )
    assert gif["width"] <= 320 and gif["size_bytes"] <= 400_000 and gif["violations"] == []
    poster = render.render_output(
        src,
        out,
        render.normalize_output({"name": "poster", "format": "poster", "at": "b"}, marks, source["duration_s"]),
        marks=marks,
        source=source,
    )
    assert Path(poster["path"]).read_bytes()[:4] == b"\x89PNG" and poster["width"] == 640


# ── hardening (v0.1.1) ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "bad, match",
    [
        ({"width": 100_000}, "width 100000 is outside"),
        ({"width": -4}, "width -4 is outside"),
        ({"fps": 10_000}, "fps 10000 is outside"),
        ({"fps": "nan"}, "fps nan is outside"),
        ({"crf": 99}, "crf 99 is outside"),
        ({"start": "nan"}, "isn't a usable time"),
        ({"end": "inf"}, "isn't a usable time"),
        ({"start": -3}, "isn't a usable time"),
        ({"speed": [{"from": 1, "to": 2, "factor": "nan"}]}, "outside 0.25..32"),
        ({"crop": {"x": -1, "y": 0, "width": 100, "height": 100}}, "crop must be inside"),
        ({"crop": {"x": 0, "y": 0, "width": 0, "height": 100}}, "crop must be inside"),
        ({"max_bytes": 5}, "max_bytes 5 is outside"),
    ],
)
def test_specs_are_bounded(bad, match):
    with pytest.raises(render.RenderError, match=match):
        _spec(**bad)


def test_an_ffmpeg_timeout_is_a_render_error_not_a_crash(tmp_path):
    def runner(cmd):
        raise subprocess.TimeoutExpired(cmd, render.FFMPEG_TIMEOUT_S)

    with pytest.raises(render.RenderError, match="ran past its 600s limit"):
        render.render_output("/x.webm", tmp_path, _spec(), marks=MARKS, source={"width": 1280}, runner=runner)
    with pytest.raises(render.RenderError, match="ran past"):
        render.probe("/x.webm", runner=runner)


def test_unreadable_ffprobe_output_is_a_render_error():
    def runner(cmd):
        return subprocess.CompletedProcess(cmd, 0, "not json", "")

    with pytest.raises(render.RenderError, match="unreadable"):
        render.probe("/x.webm", runner=runner)


def test_the_size_ladder_stops_at_its_time_budget(tmp_path, monkeypatch):
    clock = iter([0.0] + [render.OUTPUT_BUDGET_S + 1.0] * 50)
    monkeypatch.setattr(render.time, "monotonic", lambda: next(clock))
    ff = FakeFF(sizes=[50_000_000] * 20)
    r = render.render_output(
        "/x.webm", tmp_path, _spec(max_bytes=1_000_000), marks=MARKS, source={"width": 1280}, runner=ff
    )
    assert len(r["attempts"]) == 1 and "render budget" in r["violations"][0]


def test_the_shot_scripting_worked_example_is_a_valid_script_and_a_continuous_render():
    """The skill's example is copied by agents — it must validate as written."""
    skill = (Path(render.__file__).parent / "skills" / "shot-scripting" / "SKILL.md").read_text()
    blocks = re.findall(r"```yaml\n(.*?)```", skill, re.S)
    script = shotscript.validate(next(b for b in blocks if "base_url:" in b))
    ops = [st["op"] for st in script["steps"]]
    marks = [st["name"] for st in script["steps"] if st["op"] == "mark"]
    assert ops.index("click") < ops.index("type"), "the result's view is opened BEFORE the action"
    assert script["steps"][-1]["op"] == "mark" and ops[-3] == "hold", "ends held on the result"
    out = yaml.safe_load(next(b for b in blocks if "outputs:" in b))["outputs"][0]
    spec = render.normalize_output(out, {m: float(i) for i, m in enumerate(marks)}, 99.0)
    assert spec["continuous"] and all(f <= render.MAX_CONTINUOUS_FACTOR for _, _, f in spec["ramps"])
