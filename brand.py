"""Brand look for cards — colours, fonts, logo, name.

Source order (first that has a value wins, per field):

1. The **Social Studio brand kit** YAML, if one exists. This plugin READS THE FILE ONLY —
   it never imports the social plugin (ADR 0039: plugins don't import each other). The path
   comes from this plugin's ``brand_kit_path`` setting, else the social plugin's own config
   section in the live host config, else social's documented default location.
2. This plugin's ``brand_*`` settings.
3. Neutral defaults.

The social kit's schema is about voice, so visual keys are optional there. Social Studio's
``visual:`` contract is::

    visual:
      colors:   {primary, accent, background, foreground}   # quoted hex
      fonts:    {heading, body, heading_url, body_url}      # family names (+ stylesheet URLs)
      logo:     {path, dark, light}                         # relative to the kit file, or absolute
      wordmark: "Acme"                                      # the name as set in type

Read defensively — any of it may be absent, half-filled, or the wrong type, and a bad value
falls through to the next source instead of winning. Also accepted: top-level ``colors`` /
``fonts`` / ``logo`` (the pre-contract shape); colours as a list of hex strings (first =
accent); ``logo`` as a bare path string. Relative logo paths resolve against the KIT FILE's
directory; ``logo.dark`` is chosen on a dark card background and ``logo.light`` on a light
one, falling back to ``logo.path``; a logo file that doesn't exist is skipped. The
``*_url`` font stylesheets are deliberately NOT loaded — cards load nothing from the network.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Callable

DEFAULTS = {
    "name": "",
    "colors": {"bg": "#0d0f14", "fg": "#f4f5f7", "accent": "#7c5cff", "muted": "#9aa0ac"},
    "fonts": {"heading": "", "body": ""},
    "logo": "",
}
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_COLOR_ALIASES = {
    "bg": ("bg", "background", "base", "dark"),
    "fg": ("fg", "text", "foreground", "ink"),
    "accent": ("accent", "primary", "brand"),
    "muted": ("muted", "secondary", "subtle"),
}

_CONFIG: dict[str, Any] = {}
_HOST_CONFIG: Callable[[], Any] | None = None


def configure(config: dict[str, Any] | None = None, host_config: Callable[[], Any] | None = None) -> None:
    global _CONFIG, _HOST_CONFIG
    _CONFIG = dict(config or {})
    _HOST_CONFIG = host_config


def parse_pairs(text: Any) -> dict[str, str]:
    """``"bg=#111, fg=#eee"`` (or ``bg:#111``) or a mapping → dict."""
    if isinstance(text, dict):
        return {str(k): str(v) for k, v in text.items() if v}
    out: dict[str, str] = {}
    for part in re.split(r"[,\n;]", str(text or "")):
        if "=" in part or ":" in part:
            k, v = re.split(r"[=:]", part, maxsplit=1)
            if k.strip() and v.strip():
                out[k.strip().lower()] = v.strip()
    return out


def _social_section() -> dict[str, Any]:
    if _HOST_CONFIG is None:
        return {}
    try:
        cfg = _HOST_CONFIG()
        pc = getattr(cfg, "plugin_config", None)
        if pc is None and isinstance(cfg, dict):
            pc = cfg.get("plugin_config")
        sec = (pc or {}).get("social") or {}
        return sec if isinstance(sec, dict) else {}
    except Exception:  # noqa: BLE001 — no host / odd config → fall through to defaults
        return {}


def _social_default_base() -> Path:
    """Social Studio's documented default data dir (its paths.py)."""
    return Path.home() / ".protoagent" / "social"


def social_kit_path() -> Path | None:
    """Where the Social Studio brand kit would be, or None if no candidate exists."""
    candidates: list[Path] = []
    if str(_CONFIG.get("brand_kit_path") or "").strip():
        candidates.append(Path(str(_CONFIG["brand_kit_path"])).expanduser())
    sec = _social_section()
    if str(sec.get("brand_kit_path") or "").strip():
        candidates.append(Path(str(sec["brand_kit_path"])).expanduser())
    if str(sec.get("data_dir") or "").strip():
        candidates.append(Path(str(sec["data_dir"])).expanduser() / "brand-kit.yaml")
    if os.environ.get("SOCIAL_BRAND_KIT", "").strip():
        candidates.append(Path(os.environ["SOCIAL_BRAND_KIT"]).expanduser())
    if os.environ.get("SOCIAL_DIR", "").strip():
        candidates.append(Path(os.environ["SOCIAL_DIR"]).expanduser() / "brand-kit.yaml")
    base = _social_default_base()
    inst = os.environ.get("PROTOAGENT_INSTANCE", "").strip()
    if inst:
        candidates.append(base / inst / "brand-kit.yaml")
    candidates.append(base / "brand-kit.yaml")
    for c in candidates:
        if c.is_file():
            return c
    return None


def _str(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _kit_visual(kit: dict[str, Any], kit_dir: Path | None = None) -> dict[str, Any]:
    """The usable visual bits of a brand kit. Invalid values are dropped, never passed on."""
    visual = kit.get("visual") if isinstance(kit.get("visual"), dict) else {}
    out: dict[str, Any] = {"colors": {}, "fonts": {}, "logos": {}, "name": ""}

    colors = visual.get("colors") if visual.get("colors") is not None else kit.get("colors")
    if isinstance(colors, list):
        hexes = [c.strip() for c in colors if isinstance(c, str) and _HEX.match(c.strip())]
        if hexes:
            out["colors"]["accent"] = hexes[0]
    elif isinstance(colors, dict):
        low = {str(k).lower(): v for k, v in colors.items()}
        for slot, names in _COLOR_ALIASES.items():
            for n in names:
                v = _str(low.get(n))
                if v and _HEX.match(v):  # an invalid value falls through to the next alias/source
                    out["colors"][slot] = v
                    break

    fonts = visual.get("fonts") if visual.get("fonts") is not None else kit.get("fonts")
    if isinstance(fonts, dict):
        for slot in ("heading", "body"):
            if _str(fonts.get(slot)):
                out["fonts"][slot] = _str(fonts.get(slot))
    elif _str(fonts):
        out["fonts"] = {"heading": _str(fonts), "body": _str(fonts)}

    logo = visual.get("logo") if visual.get("logo") is not None else kit.get("logo")
    if isinstance(logo, str):
        logo = {"path": logo}
    if isinstance(logo, dict):
        for key in ("path", "dark", "light"):
            v = _str(logo.get(key))
            if not v:
                continue
            lp = Path(v).expanduser()
            if not lp.is_absolute() and kit_dir is not None:
                lp = kit_dir / lp
            out["logos"][key] = str(lp)

    b = kit.get("brand")
    name = ""
    if isinstance(b, dict):
        name = next((_str(b.get(k)) for k in ("name", "brand", "title", "product") if _str(b.get(k))), "")
    elif isinstance(b, str):
        name = b.strip()
    out["name"] = _str(visual.get("wordmark")) or name
    return out


def is_dark(hex_color: str) -> bool:
    """Relative luminance < 0.5 (sRGB, no gamma — good enough to pick a logo variant)."""
    h = hex_color.lstrip("#")
    if len(h) in (3, 4):
        h = "".join(c * 2 for c in h[:3])
    try:
        r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return True
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 < 0.5


def _pick_logo(logos: dict[str, str], dark_bg: bool) -> str:
    """The kit's logo for this background: the matching variant, else ``path`` — if it exists."""
    order = ("dark", "path", "light") if dark_bg else ("light", "path", "dark")
    for key in order[:2]:
        p = logos.get(key)
        if p and Path(p).is_file():
            return p
    return ""


def resolve(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """The effective brand. ``overrides`` (a card call's own data) beat everything."""
    source = "defaults"
    kit_vis: dict[str, Any] = {"colors": {}, "fonts": {}, "logos": {}, "name": ""}
    kit_path = social_kit_path()
    if kit_path:
        try:
            import yaml

            kit = yaml.safe_load(kit_path.read_text(encoding="utf-8")) or {}
            if isinstance(kit, dict):
                kit_vis = _kit_visual(kit, kit_path.parent)
                source = f"social brand kit ({kit_path})"
        except Exception:  # noqa: BLE001 — a broken kit falls back to config, and says so
            source = f"defaults (could not parse {kit_path})"
    cfg_colors = parse_pairs(_CONFIG.get("brand_colors"))
    cfg_fonts = parse_pairs(_CONFIG.get("brand_fonts"))
    ov = overrides or {}
    out = {
        "name": _str(ov.get("brand")) or kit_vis["name"] or str(_CONFIG.get("brand_name") or "") or DEFAULTS["name"],
        "colors": {},
        "fonts": {},
        "logo": "",
        "source": source,
    }
    ov_colors = parse_pairs(ov.get("colors"))
    for slot in DEFAULTS["colors"]:
        # First VALID value wins — a typo in one source must not knock out a good one below it.
        candidates = (ov_colors.get(slot), kit_vis["colors"].get(slot), cfg_colors.get(slot))
        out["colors"][slot] = next(
            (c.strip() for c in candidates if isinstance(c, str) and _HEX.match(c.strip())), DEFAULTS["colors"][slot]
        )
    ov_fonts = parse_pairs(ov.get("fonts"))
    for slot in ("heading", "body"):
        out["fonts"][slot] = ov_fonts.get(slot) or kit_vis["fonts"].get(slot) or cfg_fonts.get(slot) or ""
    # The logo: the call's own, else the kit's variant for this background (resolved against
    # the KIT's directory), else the plugin setting. Only the kit's paths are kit-relative.
    # ``logo: False`` (a montage card that opts out) means NO logo, not "fall through".
    out["logo"] = (
        ""
        if ov.get("logo") is False
        else _str(ov.get("logo"))
        or _pick_logo(kit_vis["logos"], is_dark(out["colors"]["bg"]))
        or str(_CONFIG.get("brand_logo") or "").strip()
    )
    return out


def data_uri(path: str, max_bytes: int = 3_000_000) -> str:
    """A file → data: URI (for the card page, which has no file access). '' if unusable."""
    if not path:
        return ""
    p = Path(path).expanduser()
    if not p.is_file() or p.stat().st_size > max_bytes:
        return ""
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    if not mime.startswith("image/"):
        return ""
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"
