"""The viral-clip craft: footage zooms, the final-frame hold, short bookends, caption position,
campaign_pacing, the keyboard warning and the producer's reach, with real ffmpeg where it counts."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from campaign import brand, captions, deps, montage, outro, pacing, render, shotscript, subagents
from conftest import have_chromium, have_ffmpeg

KIT = Path(__file__).resolve().parent.parent / "brands" / "protolabs" / "brand-kit.yaml"
MARKS = {"a": 1.0, "b": 2.0, "c": 3.0}


def _spec(**kw):
    return render.normalize_output({"name": "flow", "format": "mp4", "bookends": "none", **kw}, MARKS, 10.0)


def _zoom(**kw):
    return {"start": 1, "end": 3, "x": 100, "y": 100, "factor": 1.5, **kw}


# ── A: zooms ─────────────────────────────────────────────────────────────────
def test_a_zoom_normalizes_marks_and_the_default_ease():
    z = _spec(zooms=[_zoom(start="a", end="c")])["zooms"][0]
    assert (z["start"], z["end"], z["factor"], z["ease"]) == (1.0, 3.0, 1.5, 0.45)


@pytest.mark.parametrize(
    "bad, says",
    [
        (_zoom(factor=9), "outside 1.05..4"),
        (_zoom(factor="big"), "numeric x, y"),
        ({**_zoom(), "scale": 2}, "did you mean 'factor'?"),
        (_zoom(end=0.5), "must be after start"),
        (_zoom(start=20, end=None), "outside the cut"),
        (_zoom(ease=9), "ease 9 is outside"),
    ],
)
def test_bad_zooms_are_refused_by_name(bad, says):
    with pytest.raises(render.RenderError) as e:
        _spec(zooms=[bad])
    assert says in str(e.value)


def test_overlapping_zooms_and_a_zoom_on_a_poster_are_refused():
    with pytest.raises(render.RenderError, match="overlaps"):
        _spec(zooms=[_zoom(), _zoom(start=2, end=4)])
    with pytest.raises(render.RenderError, match="one frame"):
        render.normalize_output({"name": "p", "format": "poster", "zooms": [_zoom()]}, MARKS, 10.0)


def test_a_soft_zoom_warns_and_one_reaching_the_end_stays_in():
    s = _spec(zooms=[_zoom(factor=3, end=10)])
    assert s["zooms"][0]["end"] is None and any("soften" in w for w in s["warnings"])


def test_a_zoom_window_is_mapped_through_a_speed_ramp():
    segs = render.segments(0.0, 10.0, [(0.0, 8.0, 4.0)], [0.0])
    w = render.zoom_windows([{"start": 8.0, "end": 9.0, "ease": 0.45}], segs, 99.0)[0]
    assert (w["out_start"], w["out_end"]) == (2.0, 3.0)


def test_the_zoom_filter_uses_zoompan_over_a_2x_upscale_and_clamps():
    z = [{"out_start": 1.0, "out_end": 2.0, "out_ease": 0.4, "x": 100, "y": 50, "factor": 2.0}]
    f = render.zoom_filter(z, (640, 400), 30)
    assert f.startswith("scale=1280:800") and "zoompan=" in f and "s=640x400" in f and "(on/30)" in f
    assert "clip(" in f and "iw-iw/zoom" in f


def test_zooms_sit_before_captions_so_captions_stay_fixed():
    _, head = render._with_captions("[0:v]null[base]", 30, (["-i", "c.png"], "[cfps]null[framed]"), "zoompan=d=1")
    assert head.index("zoompan") < head.index("[cfps]")


def test_a_zoom_centre_outside_the_frame_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(deps, "ffmpeg", lambda: "ffmpeg")
    with pytest.raises(render.RenderError, match="outside the 640×400 frame"):
        render.render_output(
            "in.mp4", tmp_path, _spec(end=5, zooms=[_zoom(x=900)]), marks=MARKS,
            source={"width": 640, "height": 400, "duration_s": 10.0}, runner=lambda c: None,
        )  # fmt: skip


# ── B: hold ──────────────────────────────────────────────────────────────────
def test_hold_validates_and_a_poster_has_none():
    assert _spec(hold=2.5)["hold"] == 2.5 and _spec()["hold"] == 0.0
    with pytest.raises(render.RenderError, match="outside"):
        _spec(hold=99)
    with pytest.raises(render.RenderError, match="poster"):
        render.normalize_output({"name": "p", "format": "poster", "hold": 1}, MARKS, 10.0)


def test_a_caption_to_the_clip_end_stays_up_through_the_hold_and_an_earlier_one_does_not():
    segs = render.segments(0.0, 10.0, [], [])
    timed, _ = captions.plan(
        [{"text": "a", "start": 1.0, "end": 4.0, "n": 1}, {"text": "b", "start": 5.0, "end": None, "n": 2}],
        segs, 0.0, 10.0, hold=3.0,
    )  # fmt: skip
    assert timed[0]["out_end"] == 4.0 and timed[1]["out_end"] == 13.0


# ── C: short bookends ────────────────────────────────────────────────────────
@pytest.fixture
def kit():
    brand.configure({"brand_kit_path": str(KIT)})


def test_short_bookends_are_no_intro_a_fade_and_2s_on_the_cta(kit):
    opts = montage.normalize_output({"name": "x", "bookends": "short"})
    seq, notes = montage.with_bookends([{"clip": 1}], opts)
    assert seq[0] == {"clip": 1} and len(seq) == 2 and "outro" in seq[1] and "short bookends" in notes[0]
    o = seq[1]["outro"]
    assert o["bumper"] is None and o["beat_s"] == 0 and o["cta"]["duration"] == 2.0
    assert o["transition"] == {"style": "fade", "duration": 0.3}
    assert o["cta"]["title"] == outro.preset()["cta"]["title"]


def test_brand_stays_the_render_default_and_short_is_accepted():
    assert _spec(bookends=None)["bookends"] == "brand" and _spec(bookends="short")["bookends"] == "short"
    with pytest.raises(render.RenderError, match="brand, short or none"):
        _spec(bookends="tiny")


def test_short_bookends_refuse_a_brand_with_no_cta(monkeypatch):
    monkeypatch.setattr(montage.outromod, "preset", lambda: {})
    with pytest.raises(montage.MontageError, match="no CTA card"):
        montage.with_bookends([{"clip": 1}], {"bookends": "short"})


def test_campaign_render_routes_short_to_the_outro_only(kit, tmp_path, monkeypatch):
    from campaign import paths, store, tools

    c = store.create_campaign("Launch")
    plain = paths.campaign_dir(c["id"], "Launch") / "hero.mp4"
    plain.write_bytes(b"x")
    a = store.add_asset(c["id"], "clip", "hero", path=str(plain), status="rendered", width=2560, height=1600)
    seen = {}

    def fake(campaign_id, seq, opts, out_dir, **kw):
        seen.update(opts)
        out = Path(out_dir) / "hero-bookends.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"y")
        return {"path": str(out), "size_bytes": 1, "width": 2560, "height": 1600, "duration_s": 12.0}

    monkeypatch.setattr(tools.montage, "render_montage", fake)
    spec = _spec(bookends="short", title="Hero")
    tools._bookend_render(c, a, spec, {"path": str(plain), "width": 2560, "height": 1600}, tmp_path)
    assert seen["bookends"] == "short"
    assert store.get_asset(a["id"])["meta"]["bookends"] == montage.SHORT_BOOKENDS_RECIPE


# ── E: caption position ──────────────────────────────────────────────────────
def test_a_caption_can_sit_at_the_top_and_a_bad_position_is_refused():
    caps = _spec(captions=[{"text": "hi", "at": 1, "position": "top"}])["captions"]
    assert caps[0]["position"] == "top"
    assert captions.geometry(2560, 1600, 1, "top")["y"] < 200 < captions.geometry(2560, 1600, 1)["y"]
    assert captions.geometry(2560, 1600, 2, "top")["y"] == captions.geometry(2560, 1600, 1, "top")["y"]
    with pytest.raises(captions.CaptionError, match="position must be"):
        _spec(captions=[{"text": "hi", "at": 1, "position": "left"}])


def test_position_reaches_both_caption_engines():
    cmd = captions.drawtext_png_cmd(
        "ff", "t.txt", "f.ttf", 1, 2560, 1600, {"fg": "#fff", "bg": "#000"}, "#0f0", "o.png", "top"
    )
    assert f"y={captions.geometry(2560, 1600, 1, 'top')['y']}:" in " ".join(cmd)
    look = {"colors": {"fg": "#fff", "bg": "#000"}, "fonts": {}}
    page = captions.caption_html(["hi"], 2560, 1600, look, "#0f0", "top")
    assert captions.caption_html(["hi"], 2560, 1600, look, "#0f0") != page


# ── D: pacing ────────────────────────────────────────────────────────────────
def _by_rule(rows):
    return {r["rule"]: r for r in rows}


def test_pacing_passes_a_busy_short_clip_with_readable_captions():
    rows = _by_rule(
        pacing.rules(
            duration=20.0, changed=[1.0 + i * 2 for i in range(10)],
            captions=[{"text": "Keep.", "out_start": 1.0, "out_end": 3.0}], bookend_share=0.1, bookends="short",
        )
    )  # fmt: skip
    assert {r["status"] for r in rows.values()} == {"pass"}, rows


def test_pacing_warns_on_a_late_start_dead_air_fast_captions_heavy_bookends_and_length():
    rows = _by_rule(
        pacing.rules(
            duration=40.0, changed=[6.0, 7.0],
            captions=[{"text": "x" * 60, "out_start": 1.0, "out_end": 3.0}], bookend_share=0.33, bookends="brand",
        )
    )  # fmt: skip
    for rule in ("first action", "longest quiet span", "caption 1", "bookend share", "length"):
        assert rows[rule]["status"] == "warn", rule
    assert "33.0s" in rows["longest quiet span"]["value"] and "reading hold" in rows["longest quiet span"]["detail"]
    assert "bookends: short" in rows["first action"]["detail"]


def test_pacing_says_unmeasured_rather_than_guessing_and_notes_defaults():
    rows = pacing.rules(duration=20.0, changed=None, captions=[], bookend_share=0.0, bookends="none")
    assert rows[0]["status"] == "unmeasured"
    text = pacing.report_text({"rules": rows})
    assert "UNMEASURED first action" in text and "starting defaults, not facts" in text


def test_events_merge_runs_and_quiet_spans_count_the_ends():
    assert pacing.events([1.0, 1.1, 1.2, 4.0]) == [1.0, 4.0]
    assert pacing.quiet_spans([1.0, 2.0], 10.0)[0] == 8.0


def test_a_measurement_failure_never_fails_a_render(monkeypatch):
    monkeypatch.setattr(pacing, "measure_asset", lambda a: 1 / 0)
    assert pacing.summary_line({}).startswith("pacing: not measured")


# ── F: producer + keyboard ───────────────────────────────────────────────────
def test_the_producer_can_register_and_measure():
    assert {"campaign_asset_add", "campaign_pacing"} <= set(subagents.PRODUCER_TOOLS)
    assert "viral-clip-craft" in subagents.PRODUCER_PROMPT and "campaign_pacing" in subagents.PRODUCER_PROMPT


BASE = {"base_url": "http://localhost:7871"}


def _script(*steps, **top):
    return shotscript.validate({**BASE, **top, "steps": [{"goto": "/app/"}, *steps]})


def test_a_keypress_warns_but_enter_after_typing_and_allow_keyboard_do_not():
    assert "can't see a keypress" in shotscript.keyboard_warnings(_script({"press": {"key": "k"}}))[0]
    typed = _script({"fill": {"role": "textbox", "name": "x", "value": "hi"}}, {"press": {"key": "Enter"}})
    assert shotscript.keyboard_warnings(typed) == []
    assert shotscript.keyboard_warnings(_script({"press": {"key": "k"}}, allow_keyboard=True)) == []
    with pytest.raises(shotscript.ScriptError, match="allow_keyboard"):
        _script(allow_keyboard="yes")


# ── the real thing ───────────────────────────────────────────────────────────
def _pixel(video: Path, t: float, x: int, y: int) -> int:
    res = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-ss", str(t), "-i", str(video), "-frames:v", "1", "-vf",
         f"crop=2:2:{x}:{y},format=gray", "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    )  # fmt: skip
    return res.stdout[0]


@pytest.fixture
def take(tmp_path, monkeypatch):
    monkeypatch.setattr(deps, "ffmpeg", lambda: shutil.which("ffmpeg"))
    monkeypatch.setattr(deps, "ffprobe", lambda: shutil.which("ffprobe"))
    src = tmp_path / "take.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=640x400:r=30:d=4",
         "-vf", "drawbox=x=520:y=300:w=80:h=60:color=white:t=fill", "-pix_fmt", "yuv420p", str(src)],
        check=True,
    )  # fmt: skip
    return src


def _render(src, out, **kw):
    source = render.probe(src)
    spec = render.normalize_output({"name": "c", "format": "mp4", "bookends": "none", **kw}, {}, source["duration_s"])
    return render.render_output(src, out, spec, marks={}, source=source)


@pytest.mark.integration
@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg/ffprobe not installed")
def test_real_zoom_lands_on_its_centre_then_returns(take, tmp_path):
    r = _render(take, tmp_path / "o", zooms=[{"start": 1, "end": 3, "x": 560, "y": 330, "factor": 3, "ease": 0.2}])
    assert (r["width"], r["height"]) == (640, 400) and 3.9 <= r["duration_s"] <= 4.1
    video = Path(r["path"])
    assert _pixel(video, 0.5, 320, 200) < 40, "before the zoom the frame's centre is black"
    assert _pixel(video, 2.0, 320, 200) > 200, "pushed in, the white box fills the centre"
    assert _pixel(video, 3.6, 320, 200) < 40, "and it pulls back out"


@pytest.mark.integration
@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg/ffprobe not installed")
def test_real_zoom_near_a_corner_stays_inside_the_frame(take, tmp_path):
    r = _render(take, tmp_path / "o", zooms=[{"start": 1, "x": 639, "y": 399, "factor": 2, "ease": 0.1}])
    # Clamped to the bottom-right quarter: the box's top-left corner lands at (2×(520-320), 2×(300-200)).
    assert _pixel(Path(r["path"]), 2.5, 500, 260) > 200
    assert _pixel(Path(r["path"]), 2.5, 300, 100) < 40


@pytest.mark.integration
@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg/ffprobe not installed")
def test_real_hold_extends_the_clip_with_a_frozen_frame(take, tmp_path):
    r = _render(take, tmp_path / "o", hold=2)
    assert 5.9 <= r["duration_s"] <= 6.1
    assert _pixel(Path(r["path"]), 5.5, 560, 330) > 200


@pytest.mark.integration
@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg/ffprobe not installed")
def test_real_pacing_finds_the_change_and_the_dead_air(take, tmp_path):
    moving = tmp_path / "moving.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(take), "-vf",
         "drawbox=x=100:y=100:w=60:h=60:color=white:t=fill:enable='gte(t,1)*lt(t,2)'",
         "-pix_fmt", "yuv420p", str(moving)],
        check=True,
    )  # fmt: skip
    changed = pacing._changed_times(str(moving))
    assert changed and 0.8 <= changed[0] <= 1.2
    assert all(t < 2.3 for t in changed), changed
    report = pacing.measure_asset({"path": str(moving), "duration_s": 4.0, "meta": {}})
    assert _by_rule(report["rules"])["first action"]["status"] == "pass"


@pytest.mark.integration
@pytest.mark.skipif(not (have_ffmpeg() and have_chromium()), reason="ffmpeg or chromium not installed")
def test_real_short_bookends_end_on_a_card_after_about_2s(take, tmp_path, monkeypatch):
    from campaign import paths, store

    monkeypatch.setattr(
        montage.outromod, "preset", lambda: {"cta": {"title": "Star it", "cta": "Star", "bg": "#123456"}}
    )
    monkeypatch.setattr(montage.intromod, "preset", lambda: {})
    c = store.create_campaign("Launch")
    d = paths.campaign_dir(c["id"], "Launch")
    p = d / "c.mp4"
    shutil.copy(take, p)
    a = store.add_asset(c["id"], "clip", "c", path=str(p), status="rendered")
    opts = {"size": "640x400", "poster": False, "bookends": "short"}
    r = montage.render_montage(c["id"], [{"clip": a["id"]}], opts, d / "m")
    assert 5.8 <= r["duration_s"] <= 6.3, r["duration_s"]
    assert _pixel(Path(r["path"]), 2.0, 560, 330) > 200, "the clip plays with no intro in front of it"
    assert (
        abs(_pixel(Path(r["path"]), r["duration_s"] - 0.5, 4, 4) - _pixel(Path(r["path"]), r["duration_s"] - 0.1, 4, 4))
        < 3
    )
