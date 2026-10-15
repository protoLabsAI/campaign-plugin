"""Outro — a brand-level ending any montage can append: the last shot's beat, a bumper, a CTA card.

The operator sets it up ONCE, as the plugin's ``outro`` setting (YAML)::

    outro:
      caption_hold_s: 2.5      # extra seconds the last shot's final caption stays up (frozen frame)
      min_caption_s: 4.0       # REFUSE a cut that keeps less of the final caption than this
      caption_fade_s: 0.5      # the caption then fades out (clip `caption.region` painted over)…
      beat_s: 2.0              # …and the clean shot holds this long before the transition
      transition: {style: fade, duration: 0.6}
      bumper: {path: ~/media/protoagent-splash.mp4, in: 0.15, out: 2.7}   # a recorded bumper clip
      cta:
        template: cta-slide    # cta-slide (big accent button) | title-slide
        eyebrow: "Open source · MIT"
        title: "Star protoAgent on GitHub"
        subtitle: "…"
        cta: "★ Star on GitHub"
        url: github.com/protoLabsAI/protoAgent
        duration: 4

and a montage appends it with a last item ``{outro: true}`` — or ``{outro: {…}}`` to override any
key for that cut (an override's bumper may name a campaign ``clip`` asset; only the operator's
setting may name a file ``path``).

The beat works on the item BEFORE the outro, which must be a clip. Its final frame is frozen for
``caption_hold_s`` (+ ``caption_fade_s`` + ``beat_s`` when the clip says where its caption is),
so footage is never cut short to make room. A clip item can carry::

    caption: {at: 100.4, region: {x: 140, y: 1140, w: 2260, h: 140}, color: "#0a0a0c"}

``at`` — when (source seconds) the final caption became fully visible; with it the cut is
CHECKED: ``(out − at) / speed + caption_hold_s ≥ min_caption_s`` or the montage is refused (a
cut must never trim into the last caption's reading time). ``until`` — when the footage's own
caption starts fading (an app that ends on a native hold + fade + beat, like Atlas's record
mode): then the caption counts only ``(until − at) / speed`` — set ``caption_hold_s``,
``caption_fade_s`` and ``beat_s`` to 0 and let the footage carry the beat. ``region`` (source pixels) — where
the caption sits; on the frozen frame it fades to ``color`` (default: the brand background)
after the hold, leaving the clean shot for the beat. Without ``region`` there is no fade: the
caption stays through the beat. Without ``at`` the check can't run, and the montage says so.

Pure validation + expansion (no store, no ffmpeg) — the montage renders what this produces.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

OUTRO_KEYS = {"caption_hold_s", "min_caption_s", "caption_fade_s", "beat_s", "transition", "bumper", "cta"}
BUMPER_KEYS = {"path", "clip", "in", "out", "theme_color"}
CTA_KEYS = {"template", "title", "subtitle", "eyebrow", "url", "cta", "duration", "zoom", "logo", "bg", "fg", "accent"}
DEFAULTS: dict[str, Any] = {
    "caption_hold_s": 2.5,
    "min_caption_s": 4.0,
    "caption_fade_s": 0.5,
    "beat_s": 2.0,
    "transition": {"style": "fade", "duration": 0.6},
}
CTA_TEMPLATES = ("cta-slide", "title-slide")

_PRESET: dict[str, Any] = {}


def configure(setting: Any) -> list[str]:
    """Load the operator's ``outro`` setting (a mapping or YAML text). Returns problems (the
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
            return [f"outro setting isn't valid YAML: {e}"]
    if not isinstance(data, dict):
        return ["outro setting must be a mapping"]
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


def normalize(raw: dict[str, Any], problems: list[str], *, trusted: bool) -> dict[str, Any]:
    """Validate an outro mapping. ``trusted`` (the operator's setting) may name a bumper ``path``."""
    for k in raw:
        if k not in OUTRO_KEYS:
            problems.append(f"outro: unknown key `{k}` — it takes {', '.join(sorted(OUTRO_KEYS))}")
    out: dict[str, Any] = {}
    for key, lo, hi in (
        ("caption_hold_s", 0, 10),
        ("min_caption_s", 0, 30),
        ("caption_fade_s", 0, 3),
        ("beat_s", 0, 10),
    ):
        v = _num(raw.get(key, DEFAULTS[key]), f"outro.{key}", problems, lo, hi)
        out[key] = DEFAULTS[key] if v is None else v
    out["transition"] = raw.get("transition", DEFAULTS["transition"])
    b = raw.get("bumper")
    out["bumper"] = None
    if b is not None:
        if not isinstance(b, dict):
            problems.append("outro.bumper must be {path | clip, in?, out?}")
        else:
            for k in b:
                if k not in BUMPER_KEYS:
                    problems.append(f"outro.bumper: unknown key `{k}`")
            if ("path" in b) == ("clip" in b):
                problems.append("outro.bumper: give exactly one of `path` (operator setting) or `clip` (an asset id)")
            elif "path" in b:
                if not trusted:
                    problems.append(
                        "outro.bumper.path can only come from the operator's outro setting — name a campaign `clip` instead"
                    )
                else:
                    p = Path(str(b["path"])).expanduser()
                    if not p.is_file():
                        problems.append(f"outro.bumper.path {p} doesn't exist")
            bin_ = _num(b.get("in", 0) or 0, "outro.bumper.in", problems, 0, 3600)
            bout = _num(b["out"], "outro.bumper.out", problems, 0, 3600) if b.get("out") not in (None, "") else None
            if bin_ is not None and bout is not None and bout <= bin_:
                problems.append("outro.bumper: out must be after in")
            out["bumper"] = {**b, "in": bin_ or 0.0, "out": bout}
    c = raw.get("cta")
    out["cta"] = None
    if c is not None:
        if not isinstance(c, dict):
            problems.append("outro.cta must be a card mapping {title, …, duration}")
        else:
            for k in c:
                if k not in CTA_KEYS:
                    problems.append(f"outro.cta: unknown key `{k}`")
            if not str(c.get("title") or "").strip():
                problems.append("outro.cta needs a title")
            tpl = str(c.get("template") or "cta-slide")
            if tpl not in CTA_TEMPLATES:
                problems.append(f"outro.cta.template must be one of {', '.join(CTA_TEMPLATES)}")
            out["cta"] = {**c, "template": tpl, "duration": c.get("duration", 4.0)}
    if out["bumper"] is None and out["cta"] is None and out["beat_s"] == 0 and out["caption_hold_s"] == 0:
        problems.append("outro: has nothing to add — set a beat, a bumper and/or a cta")
    return out


def resolve(value: Any, problems: list[str]) -> dict[str, Any]:
    """A montage item's ``outro`` value (``true`` or overrides) → the effective outro."""
    if value is True:
        if not _PRESET:
            problems.append(
                "outro: true — but no outro preset is configured (the plugin's `outro` setting); "
                "give the outro inline: {outro: {beat_s, bumper: {clip}, cta: {…}}}"
            )
            return {}
        return normalize(_PRESET, problems, trusted=True)
    if isinstance(value, dict):
        merged = {**_PRESET, **value}
        # Only the operator's setting may name a file path; an override may not swap one in.
        if isinstance(value.get("bumper"), dict) and "path" in value["bumper"]:
            problems.append("outro.bumper.path can only come from the operator's outro setting — name a `clip` instead")
            return {}
        return normalize(merged, problems, trusted=True)
    problems.append("outro must be true (the brand preset) or a mapping of overrides")
    return {}


def beat_hold(o: dict[str, Any], has_region: bool, transition_share: float) -> float:
    """Seconds the last shot's final frame is frozen: the caption's extra hold, then (with a
    caption region) its fade and the clean beat, then what the outgoing transition eats."""
    hold = o["caption_hold_s"] + (o["caption_fade_s"] + o["beat_s"] if has_region else o["beat_s"])
    return round(hold + transition_share, 4)


def caption_visible_s(
    clip_in: float, clip_out: float, speed: float, caption_at: float, hold_s: float, until: float | None = None
) -> float:
    """How long the final caption is on screen in the cut: from ``caption_at`` until it starts
    fading in the footage (``until`` — an app that fades its own caption, e.g. Atlas's record
    mode) or the cut; plus the frozen hold when it is still up at the cut."""
    end = clip_out if until is None or until >= clip_out else until
    extra = hold_s if until is None or until >= clip_out else 0.0
    return max(0.0, (end - max(caption_at, clip_in)) / speed) + extra
