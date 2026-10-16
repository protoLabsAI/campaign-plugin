"""The protoLabs brand snapshot (generated from @protolabsai/design tokens) and the video end-card
recipe it drives: bundled fonts embedded + verified, the bordered-rectangle CTA, the restrained
background — and the generic Campaign look untouched for every other brand."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from campaign import brand, cards
from conftest import FakePlaywright, have_chromium

ROOT = Path(__file__).resolve().parent.parent
KIT = ROOT / "brands" / "protolabs" / "brand-kit.yaml"


def _gen():
    spec = importlib.util.spec_from_file_location("gen_brand_protolabs", ROOT / "scripts" / "gen_brand_protolabs.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_the_snapshot_is_exactly_what_the_pinned_ds_tokens_produce():
    gen = _gen()
    assert gen.check() == "", "brand-kit.yaml drifted from ds-tokens.json — run scripts/gen_brand_protolabs.py"
    import yaml

    kit = yaml.safe_load(KIT.read_text())
    assert kit["source"]["package"] == "@protolabsai/design" and kit["source"]["version"] == "0.12.0"
    assert kit["visual"]["colors"] == {"background": "#0a0a0c", "foreground": "#ededed", "accent": "#9b87f2",
                                       "muted": "#a1a1aa"}  # fmt: skip
    for f in kit["visual"]["fonts"]["files"]:
        assert (KIT.parent / f["path"]).is_file()
    assert "SIL Open Font License" in (KIT.parent / "fonts" / "OFL.txt").read_text()


def test_the_kit_resolves_fonts_files_and_the_card_recipe(monkeypatch):
    brand.configure({"brand_kit_path": str(KIT)})
    look = brand.resolve()
    assert look["fonts"] == {"heading": "Geist", "body": "Geist", "mono": "Geist Mono"}
    assert [f["family"] for f in look["font_files"]] == ["Geist", "Geist Mono"]
    assert look["card"] == {"button": "outline", "background": "subtle", "title_weight": 600, "eyebrow_weight": 500,
                            "button_weight": 500, "body_weight": 440, "radius_px": 4.0, "border_px": 1.0}  # fmt: skip
    assert look["logo"].endswith("protolabs-icon-outline.svg")


def test_the_end_card_embeds_the_fonts_and_draws_the_bordered_button():
    brand.configure({"brand_kit_path": str(KIT)})
    page = cards.build_html("cta-slide-1920x1080", {"title": "Star it", "cta": "Star on GitHub"}, (2560, 1440),
                            brand.resolve())  # fmt: skip
    assert page.count("@font-face") == 2 and 'font-family:"Geist";src:url(data:font/woff2;base64,' in page
    assert '<body class="btn-outline bg-subtle">' in page
    # DS radius 4 px / border 1 px at 1280 wide → ×2 at 2560 (the border doubled for video).
    assert "--btn-radius:8.0px;--btn-border:4.0px" in page
    assert "--w-title:600;--w-eyebrow:500;--w-button:500;--w-body:440;" in page
    assert "http" not in page.split("<body")[0].replace("http://www.w3.org", ""), "nothing to fetch"


def test_other_brands_keep_the_generic_look():
    brand.configure({})
    page = cards.build_html("cta-slide-1920x1080", {"title": "t", "cta": "go"}, (1920, 1080), brand.resolve())
    assert "<body>" in page and "@font-face" not in page and "--w-title:" not in page


def test_a_bundled_font_that_did_not_paint_fails_the_card(tmp_path):
    brand.configure({"brand_kit_path": str(KIT)})
    with pytest.raises(ValueError, match="bundled font\\(s\\) Geist didn't load"):
        cards.render("cta-slide-1920x1080", {"title": "t"}, tmp_path / "c.png", size="1280x720",
                     playwright_factory=FakePlaywright())  # fmt: skip


def test_bad_recipe_and_font_entries_are_dropped(tmp_path):
    (tmp_path / "f.woff2").write_bytes(b"x")
    (tmp_path / "f.exe").write_bytes(b"x")
    files = [{"family": "A", "path": "f.woff2", "weight": "400 700"}, {"family": "B", "path": "f.exe"},
             {"family": "C", "path": "nope.woff2"}, {"path": "f.woff2"}]  # fmt: skip
    assert brand._font_files(files, tmp_path) == [
        {"family": "A", "path": str(tmp_path / "f.woff2"), "weight": "400 700"}
    ]
    assert brand.card_recipe({"button": "glow-pill", "background": "subtle", "title_weight": 1200, "radius_px": 4}) == {
        "background": "subtle",
        "radius_px": 4.0,
    }


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
def test_real_card_paints_the_bundled_geist(tmp_path):
    brand.configure({"brand_kit_path": str(KIT)})
    res = cards.render("cta-slide-1920x1080", {"title": "Star protoAgent on GitHub", "cta": "Star on GitHub",
                                               "url": "github.com/protoLabsAI/protoAgent"},
                       tmp_path / "c.png", size="1280x720", limit="")  # fmt: skip
    assert {"Geist", "Geist Mono"} <= set(res["fonts_loaded"])
