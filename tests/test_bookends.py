"""protoLabs bookends v1 — the brand kit's default intro + outro, applied by default to every clip
Campaign produces (produce_video, montages, campaign_render mp4s), with per-render opt-outs."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from campaign import brand, intro, montage, outro, produce, render

KIT = Path(__file__).resolve().parent.parent / "brands" / "protolabs" / "brand-kit.yaml"


@pytest.fixture
def kit():
    brand.configure({"brand_kit_path": str(KIT)})


def test_the_protolabs_kit_carries_bookends_v1(kit):
    b = brand.kit_bookends()
    assert b["recipe"] == "protoLabs bookends v1"
    i, o = intro.preset(), outro.preset()
    assert Path(i["bumper"]["path"]).is_file() and Path(i["bumper"]["path"]).name == "protoagent-splash.mp4"
    assert (i["bumper_fade_ms"], i["into_content"], i["max_s"]) == (300, "edge_sweep", 6.0)
    assert (o["transition"], o["bumper_fade_ms"], o["min_caption_s"]) == ("edge_sweep", 300, 4.0)
    assert o["cta"]["title"] == "Star protoAgent on GitHub" and o["cta"]["template"] == "cta-slide"


def test_bookends_v1_expand_to_fade_sweep_content_sweep_fade(kit):
    seq, notes = montage.with_bookends([{"clip": 1, "out": 30}], montage.normalize_output({"title": "Hero"}))
    assert seq[0] == {"intro": {"card": {"title": "Hero"}}} and seq[-1] == {"outro": True}
    assert any("protoLabs bookends v1" in n for n in notes)
    bump, card, clip, obump, cta = montage.parse_sequence(seq)
    assert card["transition"] == {"style": "ease_fade", "ms": 300}, "splash → 300 ms fade → title card"
    assert clip["transition"] == {"style": "edge_sweep"}, "title card → edge sweep → content"
    assert obump["transition"] == {"style": "edge_sweep"}, "content → edge sweep → bumper"
    assert (
        cta["transition"] == {"style": "ease_fade", "ms": 300} and cta["card"]["title"] == "Star protoAgent on GitHub"
    )


def test_bookends_opt_out_existing_ends_and_errors(kit):
    plain = [{"clip": 1}]
    assert montage.with_bookends(plain, montage.normalize_output({"bookends": False}))[0] == plain, "loop tiles opt out"
    framed = [{"intro": True}, {"clip": 1}, {"outro": True}]
    assert montage.with_bookends(framed, montage.normalize_output({"title": "x"}))[0] == framed, "never doubled"
    with pytest.raises(montage.MontageError, match="needs output.title .* or output.bookends: false"):
        montage.with_bookends(plain, montage.normalize_output({}))
    seq, notes = montage.with_bookends([{"clip": 1}, {"card": {"title": "end"}, "duration": 2}],
                                       montage.normalize_output({"title": "x"}))  # fmt: skip
    assert "outro" not in seq[-1] and any("ends on a card" in n for n in notes)
    with pytest.raises(montage.MontageError, match="output.bookends must be"):
        montage.normalize_output({"bookends": "sometimes"})


def test_without_brand_bookends_auto_is_a_no_op_and_true_is_refused():
    brand.configure({})
    plain = [{"clip": 1}]
    assert montage.with_bookends(plain, montage.normalize_output({}))[0] == plain
    with pytest.raises(montage.MontageError, match="the brand has no bookends"):
        montage.with_bookends(plain, montage.normalize_output({"bookends": True}))


def test_operator_settings_win_over_the_kit(kit, tmp_path):
    other = tmp_path / "b.mp4"
    other.write_bytes(b"x")
    intro.configure({"bumper": {"path": str(other)}, "card": {"title": "t"}, "bumper_fade_ms": 0})
    assert intro.preset()["bumper"]["path"] == str(other) and intro.preset()["bumper_fade_ms"] == 0


def test_render_specs_bookend_full_clips_not_loops():
    mp4 = render.normalize_output({"name": "hero", "format": "mp4"}, {}, 10.0)
    gif = render.normalize_output({"name": "loop", "format": "gif"}, {}, 10.0)
    assert (mp4["bookends"], gif["bookends"]) == ("brand", "none")
    assert render.normalize_output({"name": "h", "format": "mp4", "bookends": "none"}, {}, 10.0)["bookends"] == "none"
    with pytest.raises(render.RenderError, match="bookends only wrap an mp4"):
        render.normalize_output({"name": "g", "format": "gif", "bookends": "brand"}, {}, 10.0)


def test_produce_video_defaults_to_the_brand_bookends():
    sig = inspect.signature(produce.start)
    assert sig.parameters["intro"].default == "brand" and sig.parameters["outro"].default == "brand"


def test_campaign_render_wraps_a_full_clip_in_the_bookends(kit, tmp_path, monkeypatch):
    from campaign import paths, store, tools

    c = store.create_campaign("Launch")
    plain = paths.campaign_dir(c["id"], "Launch") / "hero.mp4"
    plain.write_bytes(b"x")
    a = store.add_asset(c["id"], "clip", "hero", path=str(plain), status="rendered", width=2560, height=1440)
    seen = {}

    def fake(campaign_id, seq, opts, out_dir, **kw):
        seen.update(seq=seq, opts=opts)
        out = Path(out_dir) / "hero-bookends.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"y")
        return {"path": str(out), "size_bytes": 1, "width": 2560, "height": 1440, "duration_s": 20.0}

    monkeypatch.setattr(tools.montage, "render_montage", fake)
    spec = render.normalize_output({"name": "hero", "format": "mp4", "title": "Hero cut"}, {}, 10.0)
    r = {"path": str(plain), "width": 2560, "height": 1440}
    out = tools._bookend_render(c, a, spec, r, tmp_path)
    assert out.endswith("hero-bookends.mp4") and seen["seq"] == [{"clip": a["id"]}]
    assert seen["opts"]["title"] == "Hero cut" and seen["opts"]["size"] == "2560x1440"
    got = store.get_asset(a["id"])
    assert got["path"] == out and got["meta"]["bookends"] == "protoLabs bookends v1"
    assert got["meta"]["unbookended"] == str(plain)
