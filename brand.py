"""Brand look for cards — colours, fonts, logo, name.

Source order (first that has a value wins, per field):

1. The **Social Studio brand kit** YAML, if one exists. This plugin READS THE FILE ONLY —
   it never imports the social plugin (ADR 0039: plugins don't import each other). The path
   comes from this plugin's ``brand_kit_path`` setting, else the social plugin's own config
   section in the live host config, else social's documented default location.
2. This plugin's ``brand_*`` settings.
3. Neutral defaults.

The social kit's schema is about voice, so visual keys are optional there. Recognised
shapes: ``visual: {colors, fonts, logo}`` or top-level ``colors`` / ``fonts`` / ``logo``;
colours as a mapping (``background``/``bg``, ``text``/``fg``, ``accent``/``primary``,
``muted``/``secondary``) or a list of hex strings (first = accent).
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


def _kit_visual(kit: dict[str, Any]) -> dict[str, Any]:
    visual = kit.get("visual") if isinstance(kit.get("visual"), dict) else {}
    out: dict[str, Any] = {"colors": {}, "fonts": {}, "logo": "", "name": ""}
    colors = visual.get("colors", kit.get("colors"))
    if isinstance(colors, list):
        hexes = [c for c in colors if isinstance(c, str) and _HEX.match(c.strip())]
        if hexes:
            out["colors"]["accent"] = hexes[0]
    elif isinstance(colors, dict):
        low = {str(k).lower(): v for k, v in colors.items()}
        for slot, names in _COLOR_ALIASES.items():
            for n in names:
                if isinstance(low.get(n), str) and low[n].strip():
                    out["colors"][slot] = low[n].strip()
                    break
    fonts = visual.get("fonts", kit.get("fonts"))
    if isinstance(fonts, dict):
        for slot in ("heading", "body"):
            if fonts.get(slot):
                out["fonts"][slot] = str(fonts[slot])
    elif isinstance(fonts, str) and fonts.strip():
        out["fonts"] = {"heading": fonts.strip(), "body": fonts.strip()}
    logo = visual.get("logo", kit.get("logo"))
    if isinstance(logo, str):
        out["logo"] = logo
    b = kit.get("brand")
    if isinstance(b, dict):
        out["name"] = str(b.get("name") or "")
    elif isinstance(b, str):
        out["name"] = b
    return out


def resolve(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """The effective brand. ``overrides`` (a card call's own data) beat everything."""
    source = "defaults"
    kit_vis: dict[str, Any] = {"colors": {}, "fonts": {}, "logo": "", "name": ""}
    kit_path = social_kit_path()
    if kit_path:
        try:
            import yaml

            kit = yaml.safe_load(kit_path.read_text(encoding="utf-8")) or {}
            if isinstance(kit, dict):
                kit_vis = _kit_visual(kit)
                source = f"social brand kit ({kit_path})"
        except Exception:  # noqa: BLE001 — a broken kit falls back to config, and says so
            source = f"defaults (could not parse {kit_path})"
    cfg_colors = parse_pairs(_CONFIG.get("brand_colors"))
    cfg_fonts = parse_pairs(_CONFIG.get("brand_fonts"))
    ov = overrides or {}
    out = {
        "name": ov.get("brand") or kit_vis["name"] or str(_CONFIG.get("brand_name") or "") or DEFAULTS["name"],
        "colors": {},
        "fonts": {},
        "logo": ov.get("logo") or kit_vis["logo"] or str(_CONFIG.get("brand_logo") or ""),
        "source": source,
    }
    ov_colors = parse_pairs(ov.get("colors"))
    for slot in DEFAULTS["colors"]:
        val = ov_colors.get(slot) or kit_vis["colors"].get(slot) or cfg_colors.get(slot) or DEFAULTS["colors"][slot]
        out["colors"][slot] = val if _HEX.match(val) else DEFAULTS["colors"][slot]
    ov_fonts = parse_pairs(ov.get("fonts"))
    for slot in ("heading", "body"):
        out["fonts"][slot] = ov_fonts.get(slot) or kit_vis["fonts"].get(slot) or cfg_fonts.get(slot) or ""
    if kit_path and out["logo"] and not Path(out["logo"]).expanduser().is_absolute():
        out["logo"] = str(kit_path.parent / out["logo"])
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
