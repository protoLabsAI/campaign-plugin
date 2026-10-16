"""The brand outro: preset validation, expansion onto a montage (beat → bumper → CTA card), the
final-caption reading-time guard, and the filter graph for the frozen beat + caption fade."""

from __future__ import annotations

from pathlib import Path

import pytest
from campaign import cards, montage, outro, paths, store
from conftest import have_ffmpeg

LOOK = {
    "colors": {"bg": "#0a0a0c", "fg": "#ededed", "accent": "#9b87f2", "muted": "#a1a1aa"},
    "fonts": {"heading": "", "body": ""},
    "logo": "",
    "name": "",
}
CTA = {"title": "Star protoAgent on GitHub", "eyebrow": "Open source · MIT", "cta": "★ Star on GitHub",
       "url": "github.com/protoLabsAI/protoAgent", "duration": 4}  # fmt: skip


@pytest.fixture
def bumper(tmp_path):
    p = tmp_path / "splash.mp4"
    p.write_bytes(b"x")
    return p


@pytest.fixture
def take():
    c = store.create_campaign("Launch")
    p = paths.campaign_dir(c["id"], "Launch") / "walk.mp4"
    p.write_bytes(b"x")
    return c, store.add_asset(c["id"], "clip", "walkthrough", path=str(p), status="captured")


def _probe(path):
    return {"width": 2560, "height": 1440, "duration_s": 108.0 if "walk" in path else 4.3}


def _preset(bumper, **kw):
    return {"bumper": {"path": str(bumper), "in": 0.1, "out": 2.7}, "cta": CTA, **kw}


# ── the preset ────────────────────────────────────────────────────────────────
def test_the_operator_preset_validates_and_reports_every_problem(bumper, tmp_path):
    assert outro.configure(_preset(bumper)) == []
    assert outro.preset()["cta"]["title"] == CTA["title"]
    probs = outro.configure(
        {"beet_s": 2, "bumper": {"path": str(tmp_path / "nope.mp4"), "in": 3, "out": 1}, "cta": {"duration": 4}}
    )
    text = "\n".join(probs)
    for bit in ("unknown key `beet_s`", "nope.mp4 doesn't exist", "out must be after in", "outro.cta needs a title"):
        assert bit in text
    assert outro.preset() == {}, "an invalid setting leaves no preset behind"
    assert "isn't valid YAML" in outro.configure("a: [")[0]


def test_outro_true_needs_a_preset_and_an_override_may_not_name_a_file(bumper):
    with pytest.raises(montage.MontageError, match="no outro preset is configured"):
        montage.parse_sequence([{"clip": 1}, {"outro": True}])
    outro.configure(_preset(bumper))
    with pytest.raises(montage.MontageError, match="only come from the operator's outro setting"):
        montage.parse_sequence([{"clip": 1}, {"outro": {"bumper": {"path": "/etc/hosts"}}}])


def test_the_outro_is_last_and_follows_a_clip(bumper):
    outro.configure(_preset(bumper))
    with pytest.raises(montage.MontageError, match="must be the LAST item"):
        montage.parse_sequence([{"clip": 1}, {"outro": True}, {"clip": 2}])
    with pytest.raises(montage.MontageError, match="follows a CLIP"):
        montage.parse_sequence([{"card": {"title": "x"}, "duration": 2}, {"outro": True}])


# ── expansion + the caption guard ────────────────────────────────────────────
def test_the_outro_freezes_the_last_shot_then_appends_the_bumper_and_the_cta(bumper):
    outro.configure(_preset(bumper))
    region = {"x": 140, "y": 1140, "w": 2260, "h": 140}
    items = montage.parse_sequence(
        [{"clip": 1, "in": 3.9, "out": 107.9, "caption": {"at": 100.5, "region": region}}, {"outro": True}]
    )
    walk, bump, card = items
    # caption_hold 2.5 + fade 0.5 + beat 2.0 + the 0.6 s fade into the bumper.
    assert walk["hold"] == 5.6 and walk["outro"]["min_caption_s"] == 4.0
    assert bump["fixed_path"] == str(bumper.resolve()) and (bump["in"], bump["out"]) == (0.1, 2.7)
    assert bump["transition"] == {"style": "fade", "duration": 0.6}
    assert card["kind"] == "card" and card["card"]["template"] == "cta-slide" and card["duration"] == 4.0
    assert card["card"]["cta"] == "★ Star on GitHub" and card["card"]["logo"] is True


def test_a_cut_that_trims_into_the_final_caption_is_refused(bumper, take):
    c, a = take
    outro.configure(_preset(bumper, min_caption_s=6))
    seq = [{"clip": a["id"], "in": 3.9, "out": 102.0, "caption": {"at": 100.5}}, {"outro": True}]
    with pytest.raises(montage.MontageError, match=r"on screen 4.00s — under the outro's min_caption_s 6s"):
        montage.resolve_items(c["id"], montage.parse_sequence(seq), _probe)
    seq[0]["out"] = 107.9
    items, _ = montage.resolve_items(c["id"], montage.parse_sequence(seq), _probe)
    assert items[0]["caption_visible_s"] == pytest.approx(7.4 + 2.5)


def test_a_caption_the_footage_fades_itself_counts_only_until_its_fade(bumper, take):
    c, a = take
    # Atlas's record mode: final caption up at 100 s, fades at 103 s, clean beat to tour-done 106 s.
    outro.configure(_preset(bumper, caption_hold_s=0, caption_fade_s=0, beat_s=0))
    seq = [{"clip": a["id"], "in": 3.9, "out": 106.0, "caption": {"at": 100.0, "until": 103.0}}, {"outro": True}]
    with pytest.raises(montage.MontageError, match=r"on screen 3.00s — under the outro's min_caption_s 4s"):
        montage.resolve_items(c["id"], montage.parse_sequence(seq), _probe)
    seq[0]["caption"]["until"] = 104.6
    items, _ = montage.resolve_items(c["id"], montage.parse_sequence(seq), _probe)
    assert items[0]["caption_visible_s"] == pytest.approx(4.6)
    assert items[0]["hold"] == 0.6, "no edit-side beat: only the transition's share is frozen"
    assert outro.caption_visible_s(0, 10, 1, 4, 2.5, until=12) == 8.5, "still up at the cut → the hold counts"


def test_without_caption_at_the_guard_says_it_could_not_check(bumper, take):
    c, a = take
    outro.configure(_preset(bumper))
    items, warnings = montage.resolve_items(
        c["id"], montage.parse_sequence([{"clip": a["id"], "in": 3.9}, {"outro": True}]), _probe
    )
    assert any("can't check the final caption" in w for w in warnings)
    assert items[0]["length"] == pytest.approx(108.0 - 3.9 + 2.5 + 2.0 + 0.6), "no region: hold + beat, no fade"
    assert items[1]["path"] == str(bumper.resolve()) and items[1]["unapproved"] is False


def test_the_beat_is_a_frozen_frame_and_the_caption_fades_on_it(bumper, take):
    c, a = take
    outro.configure(_preset(bumper))
    region = {"x": 140, "y": 1140, "w": 2260, "h": 140}
    seq = [{"clip": a["id"], "in": 3.9, "out": 107.9, "caption": {"at": 100.5, "region": region}}, {"outro": True}]
    items, _ = montage.resolve_items(c["id"], montage.parse_sequence(seq), _probe)
    opts = montage.normalize_output({"size": "2560x1440"})
    montage.framing(items, opts)
    tl = montage.plan(items, opts, LOOK)
    _args, graph = montage.build_graph(tl, opts, LOOK, {2: {"card": "/w/card.png"}})
    chains = graph.split(";")
    assert "tpad=stop_mode=clone:stop_duration=6.6,trim=duration=109.6" in chains[0], "the footage is frozen, not cut"
    # The caption box fades in at footage end (104 s) + caption_hold (2.5 s), over 0.5 s.
    assert chains[1] == ("color=c=0x0a0a0c:s=2260x140:r=30:d=109.6,format=rgba,fade=t=in:st=106.5:d=0.5:alpha=1[cb0]")
    assert chains[2].startswith("[s0][cb0]overlay=140:1140:eof_action=pass,null,")
    assert tl["total"] == pytest.approx(109.6 + 2.6 - 0.6 + 4.0 - 0.6)


def test_the_cta_card_renders_its_button_and_hides_empty_slots():
    data = montage.card_data({"title": "Star it", "cta": "★ Star on GitHub", "url": "github.com/x/y",
                              "template": "cta-slide"})  # fmt: skip
    assert data["cta"] == "★ Star on GitHub" and "footer" not in data
    page = cards.build_html("cta-slide-1920x1080", data, (2560, 1440), LOOK)
    assert '<div class="button">★ Star on GitHub</div>' in page and "github.com/x/y" in page
    assert 'class="sub empty"' in page, "no subtitle → its slot is hidden"
    bare = cards.build_html("cta-slide-1920x1080", {"title": "x"}, (1920, 1080), LOOK)
    assert 'class="button empty"' in bare
    assert montage.card_data({"title": "t", "cta": "go"})["footer"] == "go", "title-slide keeps cta as its footer"


@pytest.mark.integration
@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg not installed")
def test_real_outro_freezes_fades_the_caption_then_bumper_then_card(tmp_path):
    import subprocess

    from test_montage import _near, _rgb, _synthetic

    c = store.create_campaign("Launch")
    d = paths.campaign_dir(c["id"], "Launch")
    walk, bump, card_png = d / "walk.mp4", tmp_path / "bump.mp4", tmp_path / "card.png"
    # A grey 'diagram' with a white 'caption' band at y 300–340; it ends at 3 s.
    _synthetic(walk, "0x404040", 640, 360, 3.0, box="drawbox=x=40:y=300:w=560:h=40:color=0xffffff:t=fill")
    _synthetic(bump, "0x0000ff", 640, 360, 2.0)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x00ff00:s=640x360", "-frames:v", "1",
                    str(card_png)], check=True)  # fmt: skip
    a = store.add_asset(c["id"], "clip", "walk", path=str(walk), status="rendered")
    outro.configure({"bumper": {"path": str(bump)}, "cta": {**CTA, "duration": 1.5}, "beat_s": 1.0,
                     "caption_hold_s": 1.0, "caption_fade_s": 0.2, "transition": {"style": "fade", "duration": 0.4}})  # fmt: skip
    seq = [{"clip": a["id"], "caption": {"at": 0.0, "region": {"x": 40, "y": 300, "w": 560, "h": 40},
                                         "color": "#404040"}}, {"outro": True}]  # fmt: skip
    r = montage.render_montage(
        c["id"], seq, {"size": "640x360", "name": "o", "poster": False}, d / "m",
        browser_ok=True, card_renderer=lambda card, w, h, out: str(card_png),
    )  # fmt: skip
    # 3 s footage + hold (1.0 + 0.2 + 1.0 + 0.4) = 5.6; + bumper 2.0 − 0.4; + card 1.5 − 0.4.
    assert abs(r["duration_s"] - 8.3) < 0.1, r["duration_s"]
    assert r["caption_visible_s"] == pytest.approx(4.0)
    mp4 = Path(r["path"])
    assert _near(_rgb(mp4, 105, 300, 318), "#ffffff")[0], "3.5 s: the frozen frame still shows the caption"
    assert _near(_rgb(mp4, 135, 300, 318), "#404040")[0], "4.5 s: the caption has faded — the clean beat"
    assert _near(_rgb(mp4, 195, 300, 180), "#0000ff")[0], "6.5 s: the bumper"
    assert _near(_rgb(mp4, 234, 300, 180), "#00ff00")[0], "7.8 s: the CTA card"


def test_no_edit_side_fade_paints_nothing_over_the_footage(bumper, take):
    c, a = take
    outro.configure(_preset(bumper, caption_hold_s=0, caption_fade_s=0, beat_s=0))
    region = {"x": 140, "y": 1140, "w": 2260, "h": 140}
    seq = [{"clip": a["id"], "out": 106.0, "caption": {"at": 100.0, "until": 105.0, "region": region}}, {"outro": True}]
    items, _ = montage.resolve_items(c["id"], montage.parse_sequence(seq), _probe)
    assert "clear" not in items[0], "the app faded its caption itself — no box over the footage"
