"""Intro — a brand-level opening any montage can prepend: a bumper, a title card, a crossfade in.

The mirror of :mod:`outro`. The operator sets it up ONCE, as the plugin's ``intro`` setting (YAML)::

    intro:
      bumper: {path: ~/media/protoagent-splash.mp4, in: 0.1, out: 2.3}   # the real boot splash
      # bumper: outro          ← reuse the outro preset's bumper file (with this intro's in/out)
      card:
        template: title-slide  # the title card, drawn in the brand's card recipe
        eyebrow: "Architecture walkthrough"
        title: "protoAgent"    # a montage / produce_video usually overrides title + subtitle
        subtitle: ""
        duration: 3.0
      transition: {style: fade, duration: 0.6}   # bumper → card AND card → the first shot
      max_s: 6.0               # REFUSE an intro longer than this (until the first shot is fully up)

and a montage prepends it with a FIRST item ``{intro: true}`` — or ``{intro: {card: {title:
…}}}`` to override any key for that cut (an override's bumper may name a campaign ``clip``
asset; only the operator's setting may name a file ``path``).

The intro never hard-cuts into the footage (Josh's no-jump-cuts rule): the transition into the
first shot must be a crossfade-type xfade (``fade``, ``dissolve``, …) of at least
:data:`MIN_CROSSFADE_S`. The first shot itself should start on a SETTLED frame — trim its ``in``
past any opening motion (produce_video measures it from the footage).

Pure validation (no store, no ffmpeg) — the montage expands and renders what this produces.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

INTRO_KEYS = {"bumper", "card", "transition", "max_s"}
BUMPER_KEYS = {"path", "clip", "in", "out", "theme_color"}
CARD_KEYS = {"template", "title", "subtitle", "eyebrow", "url", "duration", "zoom", "logo", "bg", "fg", "accent"}
CARD_TEMPLATES = ("title-slide", "cta-slide")
CROSSFADES = ("fade", "dissolve", "fadeblack", "fadewhite")
MIN_CROSSFADE_S = 0.3
DEFAULTS: dict[str, Any] = {"transition": {"style": "fade", "duration": 0.6}, "max_s": 6.0}

_PRESET: dict[str, Any] = {}


def configure(setting: Any) -> list[str]:
    """Load the operator's ``intro`` setting (a mapping or YAML text). Returns problems (the
    preset is left empty when it's invalid — a montage asking for it then says why)."""
    global _PRESET
    _PRESET = {}
    if setting in (None, "", {}):
        return []
    data = setting
    if isinstance(setting, str):
        try:
            import yaml

            data = yaml.safe_load(setting)
        except Exception as e:  # noqa: BLE001
            return [f"intro setting isn't valid YAML: {e}"]
    if not isinstance(data, dict):
        return ["intro setting must be a mapping"]
    problems: list[str] = []
    normalize(data, problems, trusted=True)
    if not problems:
        _PRESET = dict(data)
    return problems


def preset() -> dict[str, Any]:
    return dict(_PRESET)


def _num(v: Any, what: str, problems: list[str], lo: float, hi: float) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        problems.append(f"{what} must be a number, got {v!r}")
        return None
    if not lo <= f <= hi:
        problems.append(f"{what} {f:g} is outside {lo:g}..{hi:g}")
        return None
    return f


def _outro_bumper() -> dict[str, Any] | None:
    from . import outro

    b = outro.preset().get("bumper")
    return dict(b) if isinstance(b, dict) else None


def normalize(raw: dict[str, Any], problems: list[str], *, trusted: bool) -> dict[str, Any]:
    """Validate an intro mapping. ``trusted`` (the operator's setting) may name a bumper ``path``."""
    for k in raw:
        if k not in INTRO_KEYS:
            problems.append(f"intro: unknown key `{k}` — it takes {', '.join(sorted(INTRO_KEYS))}")
    out: dict[str, Any] = {}
    tr = raw.get("transition", DEFAULTS["transition"])
    if not isinstance(tr, dict):
        tr = {"style": str(tr)}
    style = str(tr.get("style") or "fade")
    dur = _num(tr.get("duration", 0.6), "intro.transition.duration", problems, 0, 2) or 0.6
    if style not in CROSSFADES:
        problems.append(
            f"intro.transition.style {style!r}: the intro crossfades into the first shot — one of "
            f"{', '.join(CROSSFADES)} (never a cut)"
        )
    elif dur < MIN_CROSSFADE_S:
        problems.append(f"intro.transition.duration {dur:g}s: a crossfade needs at least {MIN_CROSSFADE_S:g}s")
    out["transition"] = {"style": style, "duration": dur}
    out["max_s"] = _num(raw.get("max_s", DEFAULTS["max_s"]), "intro.max_s", problems, 1, 15) or DEFAULTS["max_s"]

    b = raw.get("bumper")
    out["bumper"] = None
    if b == "outro" or (isinstance(b, dict) and b.get("from") == "outro"):
        ob = _outro_bumper()
        if not ob or "path" not in ob:
            problems.append("intro.bumper: outro — but the outro preset has no bumper file")
        else:
            extra = {k: v for k, v in (b.items() if isinstance(b, dict) else []) if k != "from"}
            b = {"path": ob["path"], "in": ob.get("in", 0), "out": ob.get("out"), **extra}
    if b is not None:
        if not isinstance(b, dict):
            problems.append("intro.bumper must be {path | clip, in?, out?} or `outro`")
        else:
            for k in b:
                if k not in BUMPER_KEYS:
                    problems.append(f"intro.bumper: unknown key `{k}`")
            if ("path" in b) == ("clip" in b):
                problems.append("intro.bumper: give exactly one of `path` (operator setting) or `clip` (an asset id)")
            elif "path" in b:
                if not trusted:
                    problems.append("intro.bumper.path can only come from the operator's intro setting — name a `clip`")
                elif not Path(str(b["path"])).expanduser().is_file():
                    problems.append(f"intro.bumper.path {Path(str(b['path'])).expanduser()} doesn't exist")
            bin_ = _num(b.get("in", 0) or 0, "intro.bumper.in", problems, 0, 3600)
            bout = _num(b["out"], "intro.bumper.out", problems, 0, 3600) if b.get("out") not in (None, "") else None
            if bin_ is not None and bout is not None and bout <= bin_:
                problems.append("intro.bumper: out must be after in")
            out["bumper"] = {**b, "in": bin_ or 0.0, "out": bout}

    c = raw.get("card")
    out["card"] = None
    if c is not None:
        if not isinstance(c, dict):
            problems.append("intro.card must be a card mapping {title, subtitle?, eyebrow?, duration}")
        else:
            for k in c:
                if k not in CARD_KEYS:
                    problems.append(f"intro.card: unknown key `{k}`")
            tpl = str(c.get("template") or "title-slide")
            if tpl not in CARD_TEMPLATES:
                problems.append(f"intro.card.template must be one of {', '.join(CARD_TEMPLATES)}")
            out["card"] = {**c, "template": tpl, "duration": c.get("duration", 3.0)}
    if out["bumper"] is None and out["card"] is None:
        problems.append("intro: has nothing to add — set a bumper and/or a card")
    return out


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge, but a ``card`` override merges INTO the preset's card (title/subtitle per cut)."""
    merged = {**base, **over}
    if isinstance(base.get("card"), dict) and isinstance(over.get("card"), dict):
        merged["card"] = {**base["card"], **over["card"]}
    return merged


def resolve(value: Any, problems: list[str]) -> dict[str, Any]:
    """A montage item's ``intro`` value (``true`` or overrides) → the effective intro."""
    if value is True:
        if not _PRESET:
            problems.append(
                "intro: true — but no intro preset is configured (the plugin's `intro` setting); "
                "give it inline: {intro: {bumper: {clip}, card: {title, duration}}}"
            )
            return {}
        out = normalize(_PRESET, problems, trusted=True)
    elif isinstance(value, dict):
        if isinstance(value.get("bumper"), dict) and "path" in value["bumper"]:
            problems.append("intro.bumper.path can only come from the operator's intro setting — name a `clip` instead")
            return {}
        out = normalize(_merge(_PRESET, value), problems, trusted=True)
    else:
        problems.append("intro must be true (the brand preset) or a mapping of overrides")
        return {}
    if out.get("card") is not None and not str(out["card"].get("title") or "").strip():
        problems.append("intro.card needs a title — pass the video's title as {intro: {card: {title: …}}}")
    return out


def intro_seconds(starts: list[float], first_content: int, crossfade_s: float) -> float:
    """How long the intro runs: until the first shot is FULLY on screen (its crossfade done)."""
    return round(starts[first_content] + crossfade_s, 4)
