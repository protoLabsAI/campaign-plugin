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


# ── Social Studio's `visual:` contract, read defensively (v0.1.1) ──────────────
def _kit(tmp_path, monkeypatch, data, *, logos=()):
    d = tmp_path / "brandkit"
    d.mkdir(exist_ok=True)
    for name in logos:
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(PNG_1x1)
    kit = d / "brand-kit.yaml"
    kit.write_text(data if isinstance(data, str) else yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("SOCIAL_BRAND_KIT", str(kit))
    return d


def test_the_full_visual_contract(tmp_path, monkeypatch):
    d = _kit(
        tmp_path,
        monkeypatch,
        """
brand: Acme
visual:
  colors: {primary: "#1F6FEB", accent: "#F78166", background: "#0D1117", foreground: "#E6EDF3"}
  fonts: {heading: Inter, body: "IBM Plex Sans", heading_url: "https://fonts.example/inter.css"}
  logo: {path: assets/logo.png, dark: assets/logo-on-dark.png, light: assets/logo-on-light.png}
  wordmark: "ACME"
""",
        logos=("assets/logo.png", "assets/logo-on-dark.png", "assets/logo-on-light.png"),
    )
    b = brand.resolve()
    assert b["colors"] == {"bg": "#0D1117", "fg": "#E6EDF3", "accent": "#F78166", "muted": "#9aa0ac"}
    assert b["fonts"] == {"heading": "Inter", "body": "IBM Plex Sans"}
    assert b["name"] == "ACME", "the wordmark is the name as set in type"
    assert b["logo"] == str(d / "assets/logo-on-dark.png"), "dark card → the logo variant for dark backgrounds"
    b = brand.resolve({"colors": {"bg": "#ffffff"}})
    assert b["logo"] == str(d / "assets/logo-on-light.png")
    html = cards.build_html("og-1280x640", {"title": "t"}, (1280, 640), brand.resolve())
    assert "fonts.example" not in html, "font stylesheet URLs are never loaded — cards stay offline"


def test_primary_stands_in_for_a_missing_accent(tmp_path, monkeypatch):
    _kit(tmp_path, monkeypatch, {"visual": {"colors": {"primary": "#1F6FEB"}}})
    assert brand.resolve()["colors"]["accent"] == "#1F6FEB"


def test_a_logo_mapping_with_only_a_path_and_a_missing_variant(tmp_path, monkeypatch):
    d = _kit(tmp_path, monkeypatch, {"visual": {"logo": {"path": "logo.png", "dark": "nope.png"}}}, logos=("logo.png",))
    assert brand.resolve()["logo"] == str(d / "logo.png"), "a variant that doesn't exist falls back to path"


def test_a_nonexistent_kit_logo_falls_back_to_the_setting(tmp_path, monkeypatch):
    _kit(tmp_path, monkeypatch, {"visual": {"logo": {"path": "gone.png"}}})
    setting = tmp_path / "cfg-logo.png"
    setting.write_bytes(PNG_1x1)
    brand.configure({"brand_logo": str(setting)})
    assert brand.resolve()["logo"] == str(setting)
    brand.configure({})
    assert brand.resolve()["logo"] == ""
    assert (
        "logo" not in cards.build_html("og-1280x640", {"title": "t"}, (1280, 640), brand.resolve()).split("<body>")[1]
    )


def test_only_the_kits_own_logo_is_kit_relative(tmp_path, monkeypatch):
    d = _kit(tmp_path, monkeypatch, {"visual": {"colors": {}}})
    brand.configure({"brand_logo": "rel/logo.png"})
    assert brand.resolve()["logo"] == "rel/logo.png", "a setting's path is NOT re-rooted at the kit's dir"
    assert brand.resolve({"logo": "call.png"})["logo"] == "call.png"
    assert not brand.resolve()["logo"].startswith(str(d))


def test_absolute_and_home_logo_paths_are_kept(tmp_path, monkeypatch):
    abs_logo = tmp_path / "abs.png"
    abs_logo.write_bytes(PNG_1x1)
    _kit(tmp_path, monkeypatch, {"visual": {"logo": {"path": str(abs_logo)}}})
    assert brand.resolve()["logo"] == str(abs_logo)


@pytest.mark.parametrize(
    "visual",
    [
        "not a mapping",
        ["a", "list"],
        {"colors": "nope", "fonts": ["Inter"], "logo": 42, "wordmark": {"x": 1}},
        {"colors": {"primary": None, "background": 12, "foreground": ""}},  # unquoted '#…' parses as None
        {"colors": None, "fonts": None, "logo": None},
        {"fonts": {"heading": {"family": "Inter"}, "body": 7}},
        {"logo": {"path": None, "dark": 3, "light": ""}},
    ],
)
def test_malformed_visual_sections_degrade_to_defaults(tmp_path, monkeypatch, visual):
    _kit(tmp_path, monkeypatch, {"brand": "Kitco", "visual": visual})
    b = brand.resolve()
    assert b["colors"] == brand.DEFAULTS["colors"] and b["logo"] == "" and b["name"] == "Kitco"
    assert all(isinstance(v, str) for v in b["fonts"].values())
    cards.build_html("square-1080", {"title": "t"}, (1080, 1080), b)


def test_an_invalid_kit_colour_falls_through_to_a_valid_setting(tmp_path, monkeypatch):
    _kit(tmp_path, monkeypatch, {"visual": {"colors": {"accent": "red", "background": "#12"}}})
    brand.configure({"brand_colors": "accent=#abcdef, bg=#101010"})
    b = brand.resolve()
    assert b["colors"]["accent"] == "#abcdef" and b["colors"]["bg"] == "#101010"


def test_the_pre_contract_top_level_shape_still_reads(tmp_path, monkeypatch):
    d = _kit(tmp_path, monkeypatch, {"colors": ["#ff00ff"], "fonts": "Inter", "logo": "l.png"}, logos=("l.png",))
    b = brand.resolve()
    assert b["colors"]["accent"] == "#ff00ff" and b["fonts"]["heading"] == "Inter" and b["logo"] == str(d / "l.png")
