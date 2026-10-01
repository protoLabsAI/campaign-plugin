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

# id → {label, kinds, formats, max_bytes, min_width, min_height, width, height, source, as_of, note}
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
        "kinds": ["clip"],
        "formats": ["mp4", "mov", "webm"],
        "max_bytes": 10 * MB,
        "source": "https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/attaching-files",
        "as_of": "2026-10-01",
        "note": "10MB for videos on a free plan; paid plans allow 100MB (override this row if you have one).",
    },
    "github_attachment_video_paid": {
        "label": "GitHub attachment — video, paid plan",
        "kinds": ["clip"],
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
    limit_id: str, *, size: int | None, width: int | None = None, height: int | None = None, fmt: str = ""
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
    return problems


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
        "| id | limit | max size | dims | source | as of |",
        "|---|---|---|---|---|---|",
    ]
    for key, row in table().items():
        dims = ""
        if row.get("min_width"):
            dims = f"≥{row['min_width']}×{row.get('min_height', '?')}"
        if row.get("width"):
            dims += f" (rec. {row['width']}×{row.get('height', '?')})"
        label = row.get("label", key) + (" *(override)*" if row.get("overridden") else "")
        lines.append(
            f"| `{key}` | {label} | {human_bytes(row.get('max_bytes')) if row.get('max_bytes') else '—'} | "
            f"{dims or '—'} | {row.get('source', '')} | {row.get('as_of', '')} |"
        )
    return "\n".join(lines)
