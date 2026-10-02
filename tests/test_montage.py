"""Montage: validation, framing, the timeline, filter-graph construction, the tools — and a REAL
ffmpeg montage of synthetic solid-colour clips with colour-wipe transitions."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from campaign import deps, limits, montage, paths, store
from conftest import have_chromium, have_ffmpeg

LOOK = {
    "colors": {"bg": "#0d0f14", "fg": "#f4f5f7", "accent": "#7c5cff", "muted": "#9aa0ac"},
    "fonts": {"heading": "", "body": ""},
    "logo": "",
    "name": "",
}


def _opts(**kw):
    return montage.normalize_output(kw)


def _clip(n, length, **kw):
    it = {"kind": "clip", "n": n, "asset_id": n, "in": 0.0, "out": length, "speed": 1.0, "length": length,
          "path": f"/m/c{n}.mp4", "label": "", "theme_color": None, "transition": None, "fit": None, "focus": None,
          "src_width": 1920, "src_height": 1080}  # fmt: skip
    it.update(kw)
    return it


def _card(n, length, **kw):
    it = {"kind": "card", "n": n, "card": {"title": "Your agent.", "colors": {}, "logo": False}, "duration": length,
          "length": length, "zoom": False, "theme_color": None, "transition": None}  # fmt: skip
    it.update(kw)
    return it


# ── validation ────────────────────────────────────────────────────────────────
def test_a_valid_sequence_parses_both_item_kinds():
    items = montage.parse_sequence(
        [
            {"clip": 3, "in": 1, "out": 6.5, "speed": 1.5, "label": "Schedules", "theme_color": "#2c5"},
            {"card": {"title": "Your agent. Your data. Your way.", "accent": "#ff00aa"}, "duration": 2.5,
             "transition": {"style": "fade", "duration": 0.6}, "zoom": True},
        ]
    )  # fmt: skip
    clip, card = items
    assert clip["theme_color"] == "#22cc55" and clip["speed"] == 1.5 and clip["out"] == 6.5
    assert card["card"]["colors"] == {"accent": "#ff00aa"} and card["transition"] == {"style": "fade", "duration": 0.6}
    assert card["zoom"] is True and card["card"]["logo"] is False


def test_validation_is_loud_and_lists_every_problem_with_its_item():
    with pytest.raises(montage.MontageError) as e:
        montage.parse_sequence(
            [
                {"clip": 1, "transition": "fade", "colour": "#fff"},  # first item can't transition; typo key
                {"clip": 2, "speed": [{"from": 1, "to": 2, "factor": 4}]},  # a ramp
                {"clip": 3, "speed": 8},  # a jump cut
                {"clip": 4, "in": 5, "out": 2},
                {"card": {"title": "Hi", "font": "Comic"}},  # unknown card key, no duration
                {"card": {"title": "x"}, "duration": 2, "transition": "starwipe"},
                {"clip": 5, "theme_color": "teal"},
                {"clip": "five"},
                {"clip": 6, "card": {}},
            ]
        )
    probs = "\n".join(e.value.problems)
    assert "item 1: has a transition, but nothing comes before it" in probs
    assert "item 1: unknown key(s) colour" in probs
    assert "item 2.speed: a montage clip plays ONE continuous stretch" in probs
    assert "item 3.speed 8 is outside 0.25..4" in probs and "jump cut" in probs
    assert "item 4: out (2s) must be after in (5s)" in probs
    assert "item 5.card: unknown key(s) font" in probs and "item 5: a card needs duration" in probs
    assert "item 6.transition: unknown transition 'starwipe'" in probs
    assert "item 7.theme_color: 'teal' isn't a hex colour" in probs
    assert "item 8.clip must be an asset id" in probs
    assert "item 9: give exactly one of `clip`" in probs


def test_focus_shape_is_validated():
    with pytest.raises(montage.MontageError, match="focus.*unknown key.*width"):
        montage.parse_sequence([{"clip": 1, "focus": {"x": 0, "y": 0, "width": 100, "h": 100}}])
    with pytest.raises(montage.MontageError, match="at least 64px"):
        montage.parse_sequence([{"clip": 1, "focus": {"x": 0, "y": 0, "w": 10, "h": 100}}])
    (it,) = montage.parse_sequence([{"clip": 1, "focus": {"x": 1200.4, "y": 200, "w": 400, "h": 400}}])
    assert it["focus"] == {"x": 1200, "y": 200, "w": 400, "h": 400}


def test_output_options_presets_and_limits():
    o = _opts()
    assert (o["width"], o["height"], o["fps"], o["fit"]) == (1920, 1080, 30, "auto")
    assert o["transition"] == {"style": "colorwipe", "duration": 0.45}
    v = montage.normalize_output("vertical")
    assert (v["width"], v["height"], v["orientation"]) == (1080, 1920, "vertical")
    assert montage.normalize_output("square")["orientation"] == "square"
    assert (montage.normalize_output("640x360")["width"], montage.normalize_output("640x360")["height"]) == (640, 360)
    assert _opts(limit="github_attachment_video_free", max_bytes=20_000_000)["max_bytes"] == 10_000_000
    with pytest.raises(montage.MontageError) as e:
        montage.normalize_output(
            {"preset": "cinema", "fit": "stretch", "limit": "nope", "speed": 2, "transition": "zoomzoom", "gif": "yes"}
        )
    probs = "\n".join(e.value.problems)
    for want in ("unknown key(s) speed", "output.preset must be one of", "output.fit must be one of",
                 "unknown limit 'nope'", "unknown transition 'zoomzoom'", "output.gif must be true or false"):  # fmt: skip
        assert want in probs, want
    with pytest.raises(montage.MontageError, match="even number"):
        montage.normalize_output({"size": "641x360"})


# ── resolving clips against the store ─────────────────────────────────────────
@pytest.fixture
def camp(tmp_path):
    c = store.create_campaign("Launch")
    d = paths.campaign_dir(c["id"], "Launch")

    def clip(status="rendered", kind="clip", campaign=None, file=True, title="beat"):
        p = d / f"{title}-{status}-{kind}.mp4"
        if file:
            p.write_bytes(b"x")
        a = store.add_asset(campaign or c["id"], kind, title, path=str(p) if file else "", status="captured")
        if status in ("ready_for_review", "approved", "rejected"):
            store.update_asset(a["id"], status="ready_for_review")
            if status != "ready_for_review":
                store.review(a["id"], status, "not this one")
        elif status != "captured":
            store.update_asset(a["id"], status=status)
        return store.get_asset(a["id"])

    return c, clip


def _probe(_path):
    return {"width": 1920, "height": 1080, "duration_s": 8.0}


def test_resolve_refuses_missing_foreign_rejected_and_out_of_range_clips(camp):
    c, clip = camp
    other = store.create_campaign("Other")
    foreign = clip(campaign=other["id"])
    rejected = clip("rejected", title="bad")
    still = clip(kind="still", title="pic")
    empty = clip(file=False, title="empty")
    ok = clip(title="ok")
    items = montage.parse_sequence(
        [{"clip": 999}, {"clip": foreign["id"]}, {"clip": rejected["id"]}, {"clip": still["id"]},
         {"clip": empty["id"]}, {"clip": ok["id"], "out": 9}, {"clip": ok["id"], "in": 8.5}]
    )  # fmt: skip
    with pytest.raises(montage.MontageError) as e:
        montage.resolve_items(c["id"], items, probe=_probe)
    probs = "\n".join(e.value.problems)
    assert "item 1 (clip #999): no such asset" in probs
    assert f"item 2 (clip #{foreign['id']}): no such asset in campaign {c['id']}" in probs
    assert "REJECTED it: not this one" in probs
    assert "is a still" in probs and "has no file" in probs
    assert "out 9s is past the end of the 8.00s file" in probs and "in 8.5s is past the end" in probs


def test_unapproved_clips_warn_and_are_marked(camp):
    c, clip = camp
    approved = clip("approved", title="a")
    review = clip("ready_for_review", title="r")
    items, warnings = montage.resolve_items(
        c["id"], montage.parse_sequence([{"clip": approved["id"]}, {"clip": review["id"], "in": 2, "speed": 2}]), _probe
    )
    assert [it["unapproved"] for it in items] == [False, True]
    assert items[1]["length"] == 3.0, "(8 - 2) / 2"
    assert len(warnings) == 1 and "is ready_for_review, not approved" in warnings[0] and "DRAFT" in warnings[0]


# ── framing: the canonical canvas, focus crops ────────────────────────────────
def test_a_1920x1080_clip_passes_straight_through_on_the_landscape_canvas():
    it = _clip(1, 3)
    assert montage.framing([it], _opts()) == []
    assert it["frame"] == {"mode": "pass", "rect": None}
    assert montage.frame_chain(it["frame"], 1920, 1080, "#000000") == "null"


def test_other_sizes_are_letterboxed_with_a_warning_that_names_them():
    it = _clip(2, 3, src_width=1280, src_height=800, title="settings")
    (w,) = montage.framing([it], _opts())
    assert "item 2 (clip #2 'settings') is 1280×800, not the canonical 1920×1080 — letterboxed" in w
    assert it["frame"]["mode"] == "letterbox"
    chain = montage.frame_chain(it["frame"], 1920, 1080, "#0d0f14")
    assert "force_original_aspect_ratio=decrease" in chain and "pad=1920:1080" in chain and "0x0d0f14" in chain


def test_vertical_and_square_take_the_centre_by_default_and_the_focus_when_given():
    v = _opts(preset="vertical")
    centre = _clip(1, 3)
    montage.framing([centre], v)
    assert centre["frame"] == {"mode": "crop", "rect": (657, 0, 606, 1080)}, "a centred 9:16 slice of 1920×1080"
    focus = _clip(2, 3, focus={"x": 1200, "y": 200, "w": 400, "h": 400})
    montage.framing([focus], v)
    x, y, w, h = focus["frame"]["rect"]
    assert (x, w) == (1200, 400) and abs(w / h - 1080 / 1920) < 0.01
    assert y <= 200 and y + h >= 600, "the focus area is always inside the crop"
    assert (
        montage.frame_chain(focus["frame"], 1080, 1920, "#000") == f"crop={w}:{h}:{x}:{y},scale=1080:1920:flags=lanczos"
    )
    sq = _clip(3, 3, focus={"x": 0, "y": 900, "w": 300, "h": 180})
    montage.framing([sq], _opts(preset="square"))
    x, y, w, h = sq["frame"]["rect"]
    assert w == h == 300 and y + h == 1080, "grown to 1:1 and kept inside the source"


def test_a_focus_outside_the_source_is_refused():
    it = _clip(4, 3, focus={"x": 1800, "y": 0, "w": 400, "h": 400})
    with pytest.raises(montage.MontageError, match=r"focus 400×400 at \(1800, 0\) runs outside the 1920×1080 source"):
        montage.framing([it], _opts(preset="vertical"))


def test_focus_rect_math():
    # Wider than the canvas aspect → grow height; taller → grow width; clamp at the edges.
    assert montage.focus_rect({"x": 100, "y": 100, "w": 800, "h": 100}, 1920, 1080, 16 / 9) == (100, 0, 800, 450)
    assert montage.focus_rect({"x": 0, "y": 0, "w": 100, "h": 900}, 1920, 1080, 16 / 9)[2:] == (1600, 900)
    x, y, w, h = montage.focus_rect({"x": 1900, "y": 1060, "w": 20, "h": 20}, 1920, 1080, 1.0)
    assert x + w <= 1920 and y + h <= 1080


# ── the timeline ──────────────────────────────────────────────────────────────
def test_timeline_math_for_colour_wipes_xfades_and_cuts():
    items = [
        _clip(1, 3.0, theme_color="#111111"),
        _clip(2, 4.0, theme_color="#22c55e"),  # colorwipe in (default): no overlap
        _card(3, 2.0, transition={"style": "fade", "duration": 0.5}),  # xfade: overlaps 0.5
        _clip(4, 3.0, transition={"style": "cut"}),
    ]
    tl = montage.plan(items, _opts(), LOOK)
    assert tl["starts"] == [0.0, 3.0, 6.5, 8.5]
    assert tl["total"] == 11.5
    wipe, fade, cut = tl["transitions"]
    assert (wipe["style"], wipe["color"], wipe["at"]) == ("colorwipe", "#22c55e", 3.0), "the NEXT item's theme colour"
    assert fade["color"] == LOOK["colors"]["accent"] and fade["at"] == 6.75
    assert cut["style"] == "cut" and cut["at"] == 8.5


def test_a_card_wipes_in_with_its_accent():
    tl = montage.plan(
        [_clip(1, 3), _card(2, 2, card={"title": "t", "colors": {"accent": "#ff00aa"}, "logo": False})], _opts(), LOOK
    )
    assert tl["transitions"][0]["color"] == "#ff00aa"


def test_transitions_that_eat_an_item_are_refused():
    items = [_clip(1, 3), _clip(2, 0.8), _clip(3, 3)]
    with pytest.raises(montage.MontageError, match="item 2: plays 0.80s but its transitions take 0.90s"):
        montage.plan(items, _opts(transition="fade", transition_duration=0.45), LOOK)
    montage.plan(items, _opts(transition="colorwipe", transition_duration=0.45), LOOK)  # 0.225 a side fits


def test_a_hard_limit_is_enforced_on_the_planned_cut():
    items = [_clip(1, 100), _clip(2, 60)]
    with pytest.raises(montage.MontageError) as e:
        montage.plan(items, _opts(limit="x_video"), LOOK)
    assert "output.limit x_video: 160.0s is over the 140s maximum" in str(e.value)
    with pytest.raises(montage.MontageError, match="landscape 1920×1080 video isn't accepted"):
        montage.plan([_clip(1, 5)], _opts(limit="youtube_shorts"), LOOK)
    montage.plan([_clip(1, 5)], _opts(limit="youtube_shorts", preset="vertical"), LOOK)


# ── the filter graph ──────────────────────────────────────────────────────────
def test_filter_graph_normalises_every_item_and_chains_the_transitions():
    items = [
        _clip(1, 3.0, theme_color="#111111", **{"in": 1.0, "out": 5.5}, speed=1.5),
        _clip(2, 4.0, theme_color="#22c55e", src_width=1280, src_height=800),
        _card(3, 2.0, transition={"style": "slideleft", "duration": 0.5}, zoom=True),
        _clip(4, 3.0, transition={"style": "cut"}),
    ]
    opts = _opts()
    montage.framing(items, opts)
    tl = montage.plan(items, opts, LOOK)
    args, graph = montage.build_graph(tl, opts, LOOK, {2: {"card": "/w/card02.png"}})
    assert args[:6] == ["-ss", "1", "-t", "4.6", "-i", "/m/c1.mp4"], "the take is trimmed at its ENDS only"
    assert ["-loop", "1", "-framerate", "30", "-t", "2", "-i", "/w/card02.png"] == args[12:20]
    chains = graph.split(";")
    assert chains[0].startswith("[0:v]setpts=(PTS-STARTPTS)/1.5,fps=30,null,setsar=1,format=yuv420p,settb=AVTB")
    assert "tpad=stop_mode=clone:stop_duration=1,trim=duration=3,setpts=PTS-STARTPTS,fps=30,settb=AVTB[v0]" in chains[0]
    assert "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x0d0f14" in chains[1], "the 1280×800 clip is letterboxed"
    assert "zoompan=z='1+0.04*on/60'" in chains[2] and "s=1920x1080" in chains[2]
    # The colour wipe: a colour source in the NEXT clip's theme colour, then two hard-edged wipes.
    assert "color=c=0x22c55e:s=1920x1080:r=30:d=0.45,setsar=1,format=yuv420p,settb=AVTB[c1]" in graph
    assert "[v0][c1]xfade=transition=wiperight:duration=0.225:offset=2.775[w1]" in graph
    assert "[w1][v1]xfade=transition=wiperight:duration=0.225:offset=3[x1]" in graph
    assert "[x1][v2]xfade=transition=slideleft:duration=0.5:offset=6.5[x2]" in graph
    assert "[x2][v3]concat=n=2:v=1:a=0,fps=30,settb=AVTB[x3]" in graph
    assert graph.endswith("[x3]format=yuv420p[out]")


def test_labels_are_drawtext_lower_thirds_or_png_overlays():
    items = [_clip(1, 3.0, label="Schedules", theme_color="#22c55e"), _clip(2, 3.0, label="Delegates")]
    opts = _opts()
    montage.framing(items, opts)
    tl = montage.plan(items, opts, LOOK)
    _, graph = montage.build_graph(
        tl, opts, LOOK, {0: {"label_text": "label00.txt", "font": "font.ttf"}, 1: {"label_png": "/w/label01.png"}}
    )
    assert "drawtext=fontfile=font.ttf:textfile=label00.txt:expansion=none" in graph
    assert "drawbox=" in graph and "color=0x22c55e:t=fill" in graph, "the accent bar takes the clip's theme colour"
    assert "boxcolor=0x0d0f14@0.82" in graph and "enable='between(t,0.2,2.8)'" in graph
    assert "[b1][l1]overlay=0:0:eof_action=pass" in graph


def test_vertical_labels_sit_above_the_platform_ui():
    g = montage.label_geometry(1080, 1920)
    assert g["y"] < 1920 * 0.78 and montage.label_geometry(1920, 1080)["y"] > 1080 * 0.8


def test_encode_is_h264_high_yuv420p_faststart_and_the_ladder_caps_bitrate():
    cmd = " ".join(montage.encode_cmd("ffmpeg", "m.mp4", "o.mp4", crf=20))
    for flag in ("-c:v libx264", "-profile:v high", "-pix_fmt yuv420p", "-movflags +faststart", "-crf 20", "-an"):
        assert flag in cmd
    assert montage.encode_ladder(20, None, 30) == [{"crf": 20}]
    ladder = montage.encode_ladder(20, 10_000_000, 40)
    assert [s["crf"] for s in ladder[:3]] == [20, 24, 28]
    assert ladder[3]["bitrate"] == int(10_000_000 * 8 * 0.9 / 40)
    assert "-maxrate" in montage.encode_cmd("ffmpeg", "m", "o", crf=0, bitrate=1_800_000)


def test_find_font_prefers_the_brand_family_then_falls_back(tmp_path, monkeypatch):
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    for n in ("Inter-Regular.ttf", "Inter-Bold.ttf", "Inter-BoldItalic.ttf", "DejaVuSans-Bold.ttf"):
        (fonts / n).write_bytes(b"f")
    monkeypatch.setattr(montage, "FONT_DIRS", (str(fonts),))
    path, how = montage.find_font("Inter")
    assert path.endswith("Inter-Bold.ttf") and "brand font" in how
    path, how = montage.find_font("Satoshi")
    assert path.endswith("DejaVuSans-Bold.ttf") and "isn't installed as a file" in how
    explicit = fonts / "Inter-Regular.ttf"
    assert montage.find_font("Inter", str(explicit)) == (str(explicit), "label_font")
    monkeypatch.setattr(montage, "FONT_DIRS", (str(tmp_path / "none"),))
    assert montage.find_font("Inter") == ("", "no font file found")


def test_without_drawtext_or_a_browser_cards_fail_loudly_and_labels_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(montage, "ffmpeg_filters", lambda ff, runner=None: {"xfade", "tpad"})
    opts = _opts()
    tl = {"items": [_clip(1, 3, label="Schedules")], "transitions": [], "starts": [0], "total": 3}
    media, warnings, engines = montage.prepare_media(
        tl, opts, LOOK, tmp_path, ff="ffmpeg", runner=None, browser_ok=False
    )
    assert media == {} and engines["labels"] == "none" and any("labels SKIPPED" in w for w in warnings)
    tl["items"].append(_card(2, 2))
    with pytest.raises(montage.MontageError, match="cards can't be rendered"):
        montage.prepare_media(tl, opts, LOOK, tmp_path, ff="ffmpeg", runner=None, browser_ok=False)


def test_cards_go_to_the_card_renderer_and_labels_to_html_overlays_without_drawtext(tmp_path, monkeypatch):
    monkeypatch.setattr(montage, "ffmpeg_filters", lambda ff, runner=None: set())
    seen = {}
    tl = {"items": [_clip(1, 3, label="Schedules"), _card(2, 2)], "transitions": [], "starts": [0, 3], "total": 5}
    media, warnings, engines = montage.prepare_media(
        tl, _opts(), LOOK, tmp_path, ff="ffmpeg", runner=None, browser_ok=True,
        card_renderer=lambda card, w, h, out: seen.setdefault("card", (card["title"], w, h, out))[3],
        page_renderer=lambda page, w, h, out: seen.setdefault("page", (page, out))[1],
    )  # fmt: skip
    assert seen["card"][:3] == ("Your agent.", 1920, 1080) and media[1]["card"].endswith("card01.png")
    assert "Schedules" in seen["page"][0] and "background:transparent" in seen["page"][0]
    assert engines == {"cards": "html", "labels": "html", "font": engines["font"]}
    assert any("HTML overlays" in w for w in warnings)


def test_card_data_maps_onto_the_title_slide():
    d = montage.card_data({"title": "T", "subtitle": "S", "eyebrow": "", "url": "github.com/x", "cta": "Star it",
                           "colors": {"bg": "#000000"}, "logo": False})  # fmt: skip
    assert d == {"title": "T", "subtitle": "S", "eyebrow": "", "url": "github.com/x", "footer": "Star it",
                 "colors": {"bg": "#000000"}, "logo": False}  # fmt: skip
    from campaign import brand

    assert brand.resolve({"logo": False})["logo"] == "", "logo: false really means no logo"


# ── the tools ─────────────────────────────────────────────────────────────────
@pytest.fixture
def tools(registry, monkeypatch):
    import campaign

    monkeypatch.setattr(deps, "ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(deps, "ffprobe", lambda: "/usr/bin/ffprobe")
    campaign.register(registry)
    return registry


def test_the_montage_tool_reports_every_validation_problem(tools, camp):
    c, _ = camp
    out = tools.tool("campaign_montage").invoke(
        {"campaign_id": c["id"], "sequence": json.dumps([{"clip": 1, "lable": "x"}, {"card": {"title": "t"}}]),
         "output": {"preset": "landscape", "tempo": 2}}
    )  # fmt: skip
    assert out.startswith("Not rendered — fix these and resend:")
    assert "output: unknown key(s) tempo" in out
    out = tools.tool("campaign_montage").invoke(
        {"campaign_id": c["id"], "sequence": [{"clip": 1, "lable": "x"}, {"card": {"title": "t"}}]}
    )
    assert "item 1: unknown key(s) lable" in out and "item 2: a card needs duration" in out
    out = tools.tool("campaign_montage").invoke(
        {
            "campaign_id": c["id"],
            "sequence": "[{card: {title: t}, duration: 2}]",
            "output": "preset: vertical\ntempo: 2",
        }
    )
    assert "output: unknown key(s) tempo" in out, "YAML output text is parsed, not taken as a preset name"


def test_the_montage_tool_registers_a_montage_its_poster_and_marks_draft_inputs(tools, camp, monkeypatch, tmp_path):
    c, clip = camp
    review = clip("ready_for_review", title="r")

    def fake_render(campaign_id, seq, out, out_dir, **kw):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "launch.mp4").write_bytes(b"0" * 5000)
        (out_dir / "launch-poster.png").write_bytes(b"png")
        return {
            "path": str(out_dir / "launch.mp4"), "width": 1920, "height": 1080, "duration_s": 12.0,
            "size_bytes": 5000, "expected_duration_s": 12.0, "attempts": [{"crf": 20, "size_bytes": 5000}],
            "violations": [], "warnings": ["item 1 … not approved"], "poster": str(out_dir / "launch-poster.png"),
            "gif": {}, "opts": montage.normalize_output(out), "engines": {"cards": "html", "labels": "drawtext"},
            "timeline": [{"n": 1, "kind": "clip", "start": 0, "length": 12, "asset_id": review["id"], "in": 0,
                          "out": 12, "speed": 1, "label": ""}],
            "unapproved": [review["id"]], "inputs": [review["id"]],
        }  # fmt: skip

    monkeypatch.setattr(montage, "render_montage", fake_render)
    out = tools.tool("campaign_montage").invoke(
        {
            "campaign_id": c["id"],
            "sequence": [{"clip": review["id"]}],
            "output": {"name": "launch"},
            "title": "Launch cut",
        }
    )
    assert "Montage → asset #" in out and "1920×1080 mp4, 12.00s" in out and "DRAFT" in out
    m = store.list_assets(c["id"], kind="montage")[0]
    assert m["title"] == "Launch cut" and m["status"] == "rendered"
    assert m["notes"].startswith("DRAFT INPUTS: built from unapproved #")
    assert m["meta"]["unapproved_inputs"] == [review["id"]] and m["meta"]["sequence"] == [{"clip": review["id"]}]
    poster = [a for a in store.list_assets(c["id"], kind="still") if a["parent_id"] == m["id"]]
    assert len(poster) == 1 and tools.events[-1][0] == "render_finished"
    # The agent can offer it for review; the operator still decides.
    assert store.update_asset(m["id"], status="ready_for_review")["status"] == "ready_for_review"


def test_the_storyboard_tool_returns_the_sheet_as_an_image(tools, camp, monkeypatch):
    c, clip = camp
    from campaign import tools as toolsmod

    def fake_board(campaign_id, seq, out, dst, **kw):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"png")
        return {"path": str(dst), "warnings": [], "total": 3.0, "opts": montage.normalize_output(out),
                "grid": {"columns": 4, "rows": 1, "cell": [384, 216]}, "engines": {},
                "timeline": [{"n": 1, "kind": "card", "start": 0, "length": 3, "title": "Hi"}]}  # fmt: skip

    monkeypatch.setattr(montage, "storyboard", fake_board)
    monkeypatch.setattr(toolsmod.look, "_encode", lambda *a, **k: b"jpeg")
    seen = {}
    monkeypatch.setattr(
        toolsmod, "_multimodal_fn", lambda: lambda text, images: seen.update(text=text, images=images) or "ENVELOPE"
    )
    out = tools.tool("campaign_storyboard").invoke(
        {"campaign_id": c["id"], "sequence": [{"card": {"title": "Hi"}, "duration": 3}]}
    )
    assert out == "ENVELOPE" and seen["images"][0]["mime"] == "image/jpeg"
    assert "Storyboard — 1 items, 3.00s at 1920×1080" in seen["text"] and "card “Hi”" in seen["text"]


def test_gallery_offers_the_montage_kind():
    from campaign.view import PAGE

    assert "<option>montage</option>" in PAGE and "DRAFT INPUTS" in PAGE


# ── REAL ffmpeg ───────────────────────────────────────────────────────────────
def _rgb(path: Path, t_frame: int, x: int, y: int) -> tuple[int, int, int]:
    """The RGB of one pixel of frame number ``t_frame`` (decoded by ffmpeg)."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"select=eq(n\\,{t_frame}),crop=2:2:{x}:{y}",
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return tuple(raw[:3])


def _near(rgb, hex6, tol=40):
    want = tuple(int(hex6.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    return all(abs(a - b) <= tol for a, b in zip(rgb, want)), (rgb, hex6)


def _synthetic(path: Path, color: str, w: int, h: int, seconds: float, box: str = "") -> None:
    vf = ["-vf", box] if box else []
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:r=30:d={seconds}", *vf,
         "-pix_fmt", "yuv420p", "-c:v", "libx264", str(path)],
        check=True,
    )  # fmt: skip


def _can_render_cards() -> bool:
    return have_chromium() or "drawtext" in montage.ffmpeg_filters("ffmpeg")


@pytest.mark.integration
@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg not installed")
def test_real_montage_of_three_clips_and_a_card_with_colour_wipes(tmp_path):
    if not _can_render_cards():
        pytest.skip("neither Chromium nor an ffmpeg with drawtext — a card can't be rendered here")
    c = store.create_campaign("Launch")
    d = paths.campaign_dir(c["id"], "Launch")
    ids = []
    for name, color, secs in (("red", "0xff0000", 2.0), ("green", "0x00ff00", 4.0), ("blue", "0x0000ff", 2.0)):
        p = d / f"{name}.mp4"
        _synthetic(p, color, 640, 360, secs)
        a = store.add_asset(c["id"], "clip", name, path=str(p), status="rendered")
        store.update_asset(a["id"], status="ready_for_review")
        store.review(a["id"], "approve")
        ids.append(a["id"])
    seq = [
        {"clip": ids[0], "theme_color": "#ffcc00"},
        {"clip": ids[1], "speed": 2, "theme_color": "#ff00ff"},  # 4s of take at 2× → 2s
        {"card": {"title": "Your agent. Your data. Your way.", "bg": "#101010", "accent": "#00ffff"}, "duration": 1.5},
        {"clip": ids[2], "theme_color": "#ffcc00"},
    ]
    out = d / "montages" / "t"
    r = montage.render_montage(c["id"], seq, {"size": "640x360", "name": "launch", "gif": True}, out)

    assert r["expected_duration_s"] == 7.5, "colour wipes don't shorten the cut: 2 + 2 + 1.5 + 2"
    assert abs(r["duration_s"] - 7.5) < 0.1, r["duration_s"]
    assert (r["width"], r["height"]) == (640, 360) and r["violations"] == [] and r["unapproved"] == []
    probe = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=codec_name,pix_fmt,profile,sample_aspect_ratio", "-of", "json", r["path"]],
            capture_output=True, text=True, check=True,
        ).stdout
    )["streams"][0]  # fmt: skip
    assert probe["codec_name"] == "h264" and probe["pix_fmt"] == "yuv420p" and probe["profile"] == "High"
    with open(r["path"], "rb") as fh:
        head = fh.read(64 * 1024)
    assert head.find(b"moov") < head.find(b"mdat"), "faststart: the moov atom comes first"
    assert Path(r["poster"]).is_file()

    mp4 = Path(r["path"])
    # The first wipe swaps at t=2.0s (frame 60): the bar fully covers the frame in clip 2's theme colour.
    assert _near(_rgb(mp4, 60, 320, 180), "#ff00ff")[0], _rgb(mp4, 60, 320, 180)
    # Mid-way in (frame 57): the bar has swept in from the LEFT over the outgoing red clip.
    assert _near(_rgb(mp4, 57, 20, 180), "#ff00ff")[0] and _near(_rgb(mp4, 57, 630, 180), "#ff0000")[0]
    # Mid-way out (frame 63): the incoming green clip is revealed from the left, the bar still on the right.
    assert _near(_rgb(mp4, 63, 20, 180), "#00ff00")[0] and _near(_rgb(mp4, 63, 630, 180), "#ff00ff")[0]
    # Into the card (t=4.0s): its accent; into the last clip (t=5.5s): that clip's theme colour.
    assert _near(_rgb(mp4, 120, 320, 180), "#00ffff")[0]
    assert _near(_rgb(mp4, 165, 320, 180), "#ffcc00")[0]
    # Clips play untouched between transitions.
    assert _near(_rgb(mp4, 30, 320, 180), "#ff0000")[0] and _near(_rgb(mp4, 210, 320, 180), "#0000ff")[0]
    if r["gif"]:
        assert Path(r["gif"]["path"]).stat().st_size <= limits.get("github_attachment_image")["max_bytes"]

    board = montage.storyboard(c["id"], seq, {"size": "640x360"}, d / "board.png")
    w, h = (int(v) for v in subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", board["path"]],
        capture_output=True, text=True, check=True,
    ).stdout.strip().split(","))  # fmt: skip
    assert w > 4 * 384 and h >= 216, "four cells in a row, with transition swatches between them"


@pytest.mark.integration
@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg not installed")
def test_real_vertical_cut_crops_to_the_focus_not_the_centre(tmp_path):
    c = store.create_campaign("Launch")
    d = paths.campaign_dir(c["id"], "Launch")
    p = d / "app.mp4"
    # A full-frame 1920×1080 'app': grey, with a yellow 'plugin view' at x 1200–1600, y 200–600.
    _synthetic(p, "0x808080", 1920, 1080, 2.0, box="drawbox=x=1200:y=200:w=400:h=400:color=0xffff00:t=fill")
    a = store.add_asset(c["id"], "clip", "app", path=str(p), status="rendered")
    out = d / "montages"
    seq_focus = [{"clip": a["id"], "focus": {"x": 1200, "y": 200, "w": 400, "h": 400}}]
    r = montage.render_montage(c["id"], seq_focus, {"size": "360x640", "name": "focus", "poster": False}, out / "f")
    assert (r["width"], r["height"]) == (360, 640)
    assert _near(_rgb(Path(r["path"]), 15, 180, 320), "#ffff00")[0], "the focus area fills the vertical frame"
    assert r["unapproved"] == [a["id"]]
    r2 = montage.render_montage(
        c["id"], [{"clip": a["id"]}], {"size": "360x640", "name": "centre", "poster": False}, out / "c"
    )
    assert _near(_rgb(Path(r2["path"]), 15, 180, 320), "#808080")[0], "no focus → the centre of the frame"


@pytest.mark.integration
@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg not installed")
def test_real_drawtext_label(tmp_path):
    if "drawtext" not in montage.ffmpeg_filters("ffmpeg") or not montage.find_font()[0]:
        pytest.skip("this ffmpeg has no drawtext filter (or no font file) — the HTML overlay path covers labels")
    c = store.create_campaign("Launch")
    d = paths.campaign_dir(c["id"], "Launch")
    p = d / "a.mp4"
    _synthetic(p, "0x000000", 640, 360, 2.0)
    a = store.add_asset(c["id"], "clip", "a", path=str(p), status="rendered")
    r = montage.render_montage(
        c["id"], [{"clip": a["id"], "label": "Schedules", "theme_color": "#22c55e"}],
        {"size": "640x360", "name": "lab", "poster": False, "label_engine": "drawtext"}, d / "m",
    )  # fmt: skip
    g = montage.label_geometry(640, 360)
    bar_x = g["x"] - g["pad"] - g["bar"] + 1
    assert _near(_rgb(Path(r["path"]), 30, bar_x, g["y"] + 2), "#22c55e")[0], "the accent bar is drawn"
    assert r["engines"]["labels"] == "drawtext"


@pytest.mark.integration
@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg not installed")
def test_real_ffmpeg_card_fallback_and_a_zoomed_card(tmp_path):
    if "drawtext" not in montage.ffmpeg_filters("ffmpeg") or not montage.find_font()[0]:
        pytest.skip("this ffmpeg has no drawtext filter (or no font file)")
    c = store.create_campaign("Launch")
    d = paths.campaign_dir(c["id"], "Launch")
    seq = [
        {"card": {"title": "Your agent. Your data. Your way.", "subtitle": "Open source", "bg": "#123456"},
         "duration": 1.0},
        {"card": {"title": "protoAgent", "url": "github.com/protoLabsAI/protoAgent", "cta": "Star it", "bg": "#123456"},
         "duration": 1.0, "zoom": True, "transition": "fade"},
    ]  # fmt: skip
    r = montage.render_montage(c["id"], seq, {"size": "640x360", "card_engine": "ffmpeg", "poster": False}, d / "m")
    assert abs(r["duration_s"] - 1.55) < 0.1 and r["engines"]["cards"] == "ffmpeg"
    assert _near(_rgb(Path(r["path"]), 3, 4, 4), "#123456")[0], "the card's own background"


@pytest.mark.integration
@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg not installed")
def test_real_validation_against_real_files(tmp_path):
    c = store.create_campaign("Launch")
    d = paths.campaign_dir(c["id"], "Launch")
    p = d / "short.mp4"
    _synthetic(p, "0x000000", 320, 180, 1.0)
    a = store.add_asset(c["id"], "clip", "short", path=str(p), status="rendered")
    with pytest.raises(montage.MontageError, match=r"out 3s is past the end of the 1.00s file"):
        montage.render_montage(c["id"], [{"clip": a["id"], "out": 3}], {}, d / "x")


@pytest.mark.integration
@pytest.mark.skipif(not (have_chromium() and have_ffmpeg()), reason="needs Chromium and ffmpeg")
def test_real_transparent_label_overlay_keeps_its_alpha(tmp_path):
    from campaign import cards

    page = montage.label_html("Schedules", 640, 360, LOOK, "#22c55e")
    res = cards.render_page(page, tmp_path / "label.png", 640, 360, transparent=True)
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", res["path"], "-vf", "crop=2:2:4:4", "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    assert raw[3] == 0, "the page outside the lower third is fully transparent"
    g = montage.label_geometry(640, 360)
    bar = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", res["path"], "-vf", f"crop=2:2:{g['x'] - g['pad'] - g['bar']}:{g['y']}",
         "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    assert _near(tuple(bar[:3]), "#22c55e")[0] and bar[3] == 255, "the accent bar is opaque, in the theme colour"


def test_the_review_gate_enforces_a_limits_duration(camp):
    c, _ = camp
    d = paths.campaign_dir(c["id"], "Launch")
    f = d / "long.mp4"
    f.write_bytes(b"0" * 1000)
    long = store.add_asset(
        c["id"], "montage", "long", path=str(f), status="rendered", duration_s=150.0, limit_id="x_video"
    )
    with pytest.raises(ValueError, match="150.0s is over the 140s maximum"):
        store.update_asset(long["id"], status="ready_for_review")
    ok = store.add_asset(c["id"], "montage", "ok", path=str(f), status="rendered", duration_s=60.0, limit_id="x_video")
    assert store.update_asset(ok["id"], status="ready_for_review")["status"] == "ready_for_review"
