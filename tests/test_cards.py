"""Cards + brand resolution: escaping, the social kit read (file only), size enforcement."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from campaign import brand, cards
from conftest import PNG_1x1, FakePlaywright, have_chromium


def test_bundled_templates_and_their_sizes():
    assert cards.templates() == {
        "og-1280x640": (1280, 640),
        "square-1080": (1080, 1080),
        "title-slide-1920x1080": (1920, 1080),
        "x-card-1600x900": (1600, 900),
    }


def test_size_parsing():
    assert cards.parse_size("", (1, 2)) == (1, 2)
    assert cards.parse_size("1200x630", (1, 2)) == (1200, 630)
    for bad in ("big", "10x10", "9000x100"):
        with pytest.raises(ValueError):
            cards.parse_size(bad, (1, 2))


def test_text_is_escaped_and_empty_slots_hidden():
    look = brand.resolve()
    page = cards.build_html("og-1280x640", {"title": "<script>alert(1)</script> & co"}, (1280, 640), look)
    assert "<script>alert" not in page and "&lt;script&gt;" in page and "&amp; co" in page
    assert 'class="eyebrow empty"' in page and 'class="sub empty"' in page
    assert "no-image" in page


def test_images_are_inlined_never_linked(tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(PNG_1x1)
    page = cards.build_html("x-card-1600x900", {"title": "t", "image": str(shot)}, (1600, 900), brand.resolve())
    assert 'src="data:image/png;base64,' in page and "has-image" in page
    assert str(shot) not in page


def test_brand_defaults_then_config_then_social_kit_then_call_overrides(tmp_path, monkeypatch):
    assert brand.resolve()["source"] == "defaults"
    brand.configure({"brand_name": "Cfg", "brand_colors": "bg=#000000, accent=#ff0000", "brand_fonts": "heading=Inter"})
    b = brand.resolve()
    assert b["name"] == "Cfg" and b["colors"]["accent"] == "#ff0000" and b["fonts"]["heading"] == "Inter"

    kit = tmp_path / "kit.yaml"
    kit.write_text(
        yaml.safe_dump(
            {
                "brand": {"name": "Kitco"},
                "visual": {"colors": {"primary": "#00ff00", "background": "#111111"}, "logo": "logo.png"},
            }
        )
    )
    (tmp_path / "logo.png").write_bytes(PNG_1x1)
    monkeypatch.setenv("SOCIAL_BRAND_KIT", str(kit))
    b = brand.resolve()
    assert b["name"] == "Kitco" and b["colors"]["accent"] == "#00ff00" and b["colors"]["bg"] == "#111111"
    assert b["colors"]["fg"] == brand.DEFAULTS["colors"]["fg"], "unset slots fall through"
    assert b["logo"] == str(tmp_path / "logo.png"), "a relative logo resolves beside the kit"
    assert "social brand kit" in b["source"]

    b = brand.resolve({"colors": {"accent": "#abcdef"}, "brand": "Override"})
    assert b["colors"]["accent"] == "#abcdef" and b["name"] == "Override"


def test_social_kit_path_comes_from_the_social_plugins_live_config(tmp_path):
    kit = tmp_path / "social-data" / "brand-kit.yaml"
    kit.parent.mkdir()
    kit.write_text("brand: Fromhost\n")

    class Cfg:
        plugin_config = {"social": {"data_dir": str(kit.parent)}}

    brand.configure({}, lambda: Cfg())
    assert brand.social_kit_path() == kit
    assert brand.resolve()["name"] == "Fromhost"


def test_a_broken_social_kit_falls_back_and_says_so(tmp_path, monkeypatch):
    kit = tmp_path / "kit.yaml"
    kit.write_text("brand: [unclosed")
    monkeypatch.setenv("SOCIAL_BRAND_KIT", str(kit))
    assert "could not parse" in brand.resolve()["source"]


def test_bad_colours_never_reach_the_css():
    b = brand.resolve({"colors": "accent=red;}body{display:none"})
    assert b["colors"]["accent"] == brand.DEFAULTS["colors"]["accent"]


def test_render_png_under_the_og_limit(tmp_path):
    pw = FakePlaywright(png_size=200_000)
    r = cards.render("og-1280x640", {"title": "Hi"}, tmp_path / "og", playwright_factory=pw)
    assert r["format"] == "png" and r["limit"] == "github_social_preview" and r["violations"] == []
    page = pw.browser.contexts[0].page
    assert "Hi" in page.content


def test_an_oversized_og_png_falls_back_to_stepped_jpeg(tmp_path):
    pw = FakePlaywright(png_size=1_500_000, jpeg_sizes=[1_200_000, 900_000])
    r = cards.render("og-1280x640", {"title": "Hi"}, tmp_path / "og", playwright_factory=pw)
    assert r["format"] == "jpg" and r["violations"] == []
    assert [a.get("quality") for a in r["attempts"]] == [None, 92, 86]
    assert not (tmp_path / "og.png").exists(), "the oversize PNG isn't left lying around"


def test_a_card_that_cannot_fit_is_reported(tmp_path):
    pw = FakePlaywright(png_size=1_500_000, jpeg_sizes=[1_400_000] * 5)
    r = cards.render("og-1280x640", {"title": "Hi"}, tmp_path / "og", playwright_factory=pw)
    assert r["violations"] and "over the 1.00 MB" in r["violations"][0]


def test_non_og_templates_have_no_default_limit(tmp_path):
    r = cards.render(
        "square-1080", {"title": "x"}, tmp_path / "sq", playwright_factory=FakePlaywright(png_size=5_000_000)
    )
    assert r["limit"] == "" and r["violations"] == []


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
def test_real_og_card_renders_at_size_and_under_1mb(tmp_path):
    r = cards.render("og-1280x640", {"title": "Ship plugins from a URL", "url": "github.com/acme/x"}, tmp_path / "og")
    data = Path(r["path"]).read_bytes()
    assert data[:4] == b"\x89PNG" and r["size_bytes"] < 1_000_000
    import struct

    assert struct.unpack(">II", data[16:24]) == (1280, 640)
