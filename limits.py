"""Hard limits — the ONE table of platform constraints this plugin enforces.

Only *hard* limits live here: a number the platform documents and rejects or degrades
past (an upload that fails, a preview that won't save). Soft facts — the "ideal" clip
length, how long a GIF should loop, which aspect ratio "performs" — are deliberately
absent: they drift, and a compiled-in number turns last year's folklore into a rule.
The agent researches those and writes them into the campaign plan as sourced, dated
assumptions instead.

Every row carries the ``source`` URL it was read from and the ``as_of`` date it was
checked. Byte limits are interpreted conservatively as DECIMAL megabytes (10 MB =
10,000,000 bytes) so a file that passes here never fails on the platform because of a
MiB/MB ambiguity. Operators override or add rows with the ``limit_overrides`` setting
(a YAML mapping of id → fields), e.g. a paid GitHub plan's 100 MB video limit.
"""

from __future__ import annotations

from typing import Any

MB = 1_000_000

# id → {label, kinds, formats, max_bytes, min_width, min_height, width, height,
#       min_duration_s, max_duration_s, orientations, source, as_of, note}
DEFAULT_LIMITS: dict[str, dict[str, Any]] = {
    "github_attachment_image": {
        "label": "GitHub issue/PR/README attachment — image or GIF",
        "kinds": ["gif", "still", "card"],
        "formats": ["png", "gif", "jpg", "jpeg", "svg"],
        "max_bytes": 10 * MB,
        "source": "https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/attaching-files",
        "as_of": "2026-10-01",
        "note": "10MB for images and gifs.",
    },
    "github_attachment_video_free": {
        "label": "GitHub attachment — video, free plan",
        "kinds": ["clip", "montage"],
        "formats": ["mp4", "mov", "webm"],
        "max_bytes": 10 * MB,
        "source": "https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/attaching-files",
        "as_of": "2026-10-01",
        "note": "10MB for videos on a free plan; paid plans allow 100MB (override this row if you have one).",
    },
    "github_attachment_video_paid": {
        "label": "GitHub attachment — video, paid plan",
        "kinds": ["clip", "montage"],
        "formats": ["mp4", "mov", "webm"],
        "max_bytes": 100 * MB,
        "source": "https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/attaching-files",
        "as_of": "2026-10-01",
        "note": "100MB for videos on a paid plan.",
    },
    "github_social_preview": {
        "label": "GitHub repository social preview (OG image)",
        "kinds": ["card", "still"],
        "formats": ["png", "jpg", "jpeg", "gif"],
        "max_bytes": 1 * MB,
        "min_width": 640,
        "min_height": 320,
        "width": 1280,
        "height": 640,
        "source": "https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview",
        "as_of": "2026-10-01",
        "note": "Under 1 MB; at least 640x320; 1280x640 recommended for best display.",
    },
    # ── video platforms a launch montage ships to ──
    "x_video": {
        "label": "X (Twitter) post video — non-Premium account, web/app upload",
        "kinds": ["clip", "montage"],
        "formats": ["mp4", "mov"],
        "max_bytes": 512 * MB,
        "min_duration_s": 0.5,
        "max_duration_s": 140,
        "source": "https://help.x.com/en/using-x/x-videos",
        "as_of": "2026-10-01",
        "note": "2 min 20 s and 512 MB for accounts without Premium (Premium uploads run longer). The X "
        "API's own media rules differ (docs.x.com/x-api/media/quickstart/best-practices: ≤1280×1024, "
        "yuv420p, square pixels, ≤60 fps) — use x_api_video when posting through the API.",
    },
    "x_api_video": {
        "label": "X API media upload — video (posting through the API)",
        "kinds": ["clip", "montage"],
        "formats": ["mp4", "mov"],
        "max_bytes": 8000 * MB,
        "max_width": 1280,
        "max_height": 1024,
        "min_duration_s": 0.5,
        "max_duration_s": 1200,
        "source": "https://docs.x.com/x-api/media/quickstart/best-practices",
        "as_of": "2026-10-01",
        "note": "8 GB, 32x32 to 1280x1024, ≤60 fps, YUV 4:2:0, 1:1 pixel aspect, 0.5 s–20 min on a default "
        "account. Render a 1280×720 / 720×1280 / 720×720 size for it.",
    },
    "linkedin_video": {
        "label": "LinkedIn feed video",
        "kinds": ["clip", "montage"],
        "formats": ["mp4", "mov", "webm"],
        "max_bytes": 5000 * MB,
        "min_width": 256,
        "min_height": 144,
        "max_width": 4096,
        "max_height": 2304,
        "min_duration_s": 3,
        "max_duration_s": 900,
        "source": "https://www.linkedin.com/help/linkedin/answer/a548372",
        "as_of": "2026-10-01",
        "note": "75 KB–5 GB, 3 s–15 min (2 s on mobile), 256x144–4096x2304, 10–60 fps.",
    },
    "youtube_shorts": {
        "label": "YouTube Shorts — what YouTube classifies as a Short",
        "kinds": ["clip", "montage"],
        "formats": ["mp4", "mov", "webm"],
        "max_duration_s": 180,
        "orientations": ["vertical", "square"],
        "source": "https://support.google.com/youtube/answer/15424877",
        "as_of": "2026-10-01",
        "note": "Square or vertical and up to 3 minutes is categorised as a Short; a 16:9 upload is "
        "long-form however short it is.",
    },
}

_OVERRIDES: dict[str, dict[str, Any]] = {}


def configure(overrides: Any) -> list[str]:
    """Apply operator overrides (a mapping, or YAML text of one). Returns warnings."""
    global _OVERRIDES
    warnings: list[str] = []
    data: Any = overrides
    if isinstance(overrides, str):
        text = overrides.strip()
        if not text:
            data = {}
        else:
            import yaml

            try:
                data = yaml.safe_load(text) or {}
            except Exception as e:  # noqa: BLE001 — a bad override must not sink the plugin
                warnings.append(f"limit_overrides is not valid YAML ({e}); using the defaults")
                data = {}
    if not isinstance(data, dict):
        warnings.append("limit_overrides must be a mapping of limit id → fields; ignored")
        data = {}
    clean: dict[str, dict[str, Any]] = {}
    for key, row in data.items():
        if not isinstance(row, dict):
            warnings.append(f"limit_overrides.{key} must be a mapping; ignored")
            continue
        clean[str(key)] = dict(row)
    _OVERRIDES = clean
    return warnings


def table() -> dict[str, dict[str, Any]]:
    """The effective table: defaults with overrides merged per row (new ids allowed)."""
    out = {k: dict(v) for k, v in DEFAULT_LIMITS.items()}
    for key, row in _OVERRIDES.items():
        merged = dict(out.get(key, {}))
        merged.update(row)
        merged.setdefault("label", key)
        merged.setdefault("source", "operator override")
        merged.setdefault("as_of", "")
        merged["overridden"] = True
        out[key] = merged
    return out


def get(limit_id: str) -> dict[str, Any] | None:
    return table().get((limit_id or "").strip())


def check(
    limit_id: str,
    *,
    size: int | None,
    width: int | None = None,
    height: int | None = None,
    fmt: str = "",
    duration: float | None = None,
) -> list[str]:
    """Violations of ``limit_id`` for a file with these properties ([] = within limits)."""
    row = get(limit_id)
    if row is None:
        return [f"unknown limit {limit_id!r} — see campaign_limits for the table"]
    problems: list[str] = []
    max_bytes = row.get("max_bytes")
    if max_bytes and size is not None and int(size) > int(max_bytes):
        problems.append(f"{human_bytes(size)} is over the {human_bytes(max_bytes)} limit ({row['label']})")
    if fmt and row.get("formats") and fmt.lower().lstrip(".") not in row["formats"]:
        problems.append(f"format {fmt} is not accepted ({', '.join(row['formats'])})")
    if width is not None and row.get("min_width") and int(width) < int(row["min_width"]):
        problems.append(f"width {width}px is under the {row['min_width']}px minimum")
    if height is not None and row.get("min_height") and int(height) < int(row["min_height"]):
        problems.append(f"height {height}px is under the {row['min_height']}px minimum")
    if width is not None and row.get("max_width") and int(width) > int(row["max_width"]):
        problems.append(f"width {width}px is over the {row['max_width']}px maximum ({row['label']})")
    if height is not None and row.get("max_height") and int(height) > int(row["max_height"]):
        problems.append(f"height {height}px is over the {row['max_height']}px maximum ({row['label']})")
    if duration is not None and row.get("max_duration_s") and float(duration) > float(row["max_duration_s"]):
        problems.append(f"{duration:.1f}s is over the {float(row['max_duration_s']):g}s maximum ({row['label']})")
    if duration is not None and row.get("min_duration_s") and float(duration) < float(row["min_duration_s"]):
        problems.append(f"{duration:.1f}s is under the {float(row['min_duration_s']):g}s minimum ({row['label']})")
    if width and height and row.get("orientations"):
        shape = orientation(int(width), int(height))
        if shape not in row["orientations"]:
            problems.append(
                f"a {shape} {width}×{height} video isn't accepted ({' or '.join(row['orientations'])} only)"
            )
    return problems


def orientation(width: int, height: int) -> str:
    """landscape | vertical | square."""
    return "square" if width == height else ("vertical" if height > width else "landscape")


def human_bytes(n: int | float | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1000
    return f"{n:.2f} GB"


def brief() -> str:
    """The table as markdown, with sources — what campaign_limits returns."""
    lines = [
        "Hard limits enforced by Campaign Studio (decimal MB; soft norms are NOT here — research and date those in the plan):",
        "",
        "| id | limit | max size | dims / length | source | as of |",
        "|---|---|---|---|---|---|",
    ]
    for key, row in table().items():
        dims = ""
        if row.get("min_width"):
            dims = f"≥{row['min_width']}×{row.get('min_height', '?')}"
        if row.get("max_width"):
            dims += f" ≤{row['max_width']}×{row.get('max_height', '?')}"
        if row.get("width"):
            dims += f" (rec. {row['width']}×{row.get('height', '?')})"
        if row.get("orientations"):
            dims += f" {'/'.join(row['orientations'])} only"
        if row.get("max_duration_s"):
            lo = f"{float(row['min_duration_s']):g}–" if row.get("min_duration_s") else "≤"
            dims += f" {lo}{float(row['max_duration_s']):g}s"
        label = row.get("label", key) + (" *(override)*" if row.get("overridden") else "")
        lines.append(
            f"| `{key}` | {label} | {human_bytes(row.get('max_bytes')) if row.get('max_bytes') else '—'} | "
            f"{dims or '—'} | {row.get('source', '')} | {row.get('as_of', '')} |"
        )
    return "\n".join(lines)
