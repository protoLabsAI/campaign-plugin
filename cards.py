"""Cards — branded HTML/CSS templates rendered to PNG by the out-of-process Playwright worker.

Bundled templates live in ``templates/<id>.html``; each id ends in its pixel size, which is
the default render size. Every value from ``data`` is HTML-escaped before it reaches the
page; images (logo, screenshot) are inlined as data: URIs, so the page loads nothing from
the network.

A card that must meet a hard limit (``limit``, e.g. ``github_social_preview`` < 1 MB) is
re-rendered as JPEG at stepped quality when the PNG is over — and reported, not silently
shipped, if even that doesn't fit.

The page is built HERE (escaping, brand look, inlined images); only the finished HTML goes to
the worker, which aborts every network request the page makes.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any, Callable

from . import brand as brandmod
from . import limits
from .worker import pw_worker

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
DEFAULT_LIMITS = {"og-1280x640": "github_social_preview"}
TEXT_KEYS = ("title", "subtitle", "eyebrow", "url", "footer", "brand")
CARD_TIMEOUT_S = 120.0
_SIZE_RE = re.compile(r"^(\d{2,4})x(\d{2,4})$")

BASE_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:var(--w);height:var(--h);overflow:hidden;background:var(--bg)}
body{font-family:var(--font-body);color:var(--fg);-webkit-font-smoothing:antialiased}
.card{position:relative;width:var(--w);height:var(--h);overflow:hidden;background:var(--bg);
  padding:calc(var(--u)*7) calc(var(--u)*8);display:flex}
.glow{position:absolute;inset:0;pointer-events:none;
  background:radial-gradient(ellipse 70% 90% at 0% 0%, color-mix(in srgb,var(--accent) 34%,transparent), transparent 60%),
             radial-gradient(ellipse 60% 70% at 100% 100%, color-mix(in srgb,var(--accent) 16%,transparent), transparent 70%)}
.card::after{content:"";position:absolute;inset:0;pointer-events:none;opacity:.05;
  background-image:linear-gradient(var(--fg) 1px,transparent 1px),linear-gradient(90deg,var(--fg) 1px,transparent 1px);
  background-size:calc(var(--u)*6) calc(var(--u)*6);mask-image:linear-gradient(120deg,#000,transparent 70%)}
.copy,.center,.bottom,.shot{position:relative;z-index:1}
h1{font-family:var(--font-heading);font-weight:750;letter-spacing:-.025em;line-height:1.04;
  font-size:var(--title-size);text-wrap:balance}
.sub{color:var(--muted);font-size:calc(var(--u)*2.6);line-height:1.35;margin-top:calc(var(--u)*2.2);
  text-wrap:pretty;max-width:34em}
.eyebrow{color:var(--accent);font-weight:650;font-size:calc(var(--u)*1.9);letter-spacing:.08em;
  text-transform:uppercase;margin-bottom:calc(var(--u)*1.8)}
.eyebrow.inline{margin:0 0 0 auto}
.top{display:flex;align-items:center;gap:calc(var(--u)*1.4);font-weight:650;font-size:calc(var(--u)*2.2)}
.logo{height:calc(var(--u)*4.4);width:auto;display:block}
.brand{color:var(--fg)}
.url{font-family:var(--font-mono);color:var(--fg);opacity:.85;font-size:calc(var(--u)*2)}
.footer{color:var(--muted);font-size:calc(var(--u)*1.8);margin-left:auto}
.bottom{display:flex;align-items:center;gap:calc(var(--u)*2)}
.shot{border-radius:calc(var(--u)*1.4);overflow:hidden;
  box-shadow:0 calc(var(--u)*3) calc(var(--u)*8) rgba(0,0,0,.45),0 0 0 1px color-mix(in srgb,var(--fg) 14%,transparent)}
.shot img{display:block;width:100%;height:100%;object-fit:cover;object-position:top left}
.empty{display:none !important}
/* image_fit: contain — the frame takes the image's own aspect instead of cropping it */
.shot.fit{height:auto !important;flex:0 0 auto;align-self:center;max-width:100%}
.shot.fit img{height:auto;object-fit:contain}

/* og: copy column left, screenshot bleeding off the right */
.og .copy{display:flex;flex-direction:column;justify-content:space-between;flex:1;min-width:0}
.og.has-image .copy{max-width:56%}
.og .mid{margin-top:auto;margin-bottom:auto}
.og .shot{position:absolute;right:calc(var(--u)*-6);top:calc(var(--u)*9);width:46%;height:calc(100% - var(--u)*18)}
.og .shot.fit{right:calc(var(--u)*6);top:50%;transform:translateY(-50%);width:38%}
.og.has-image .copy:has(~ .shot.fit){max-width:52%}

/* wide: headline row, then a big shot */
.wide{flex-direction:column}
.wide .copy{max-width:80%}
.wide .shot{margin-top:calc(var(--u)*4);flex:1;min-height:0;width:100%}
.wide .bottom{margin-top:calc(var(--u)*3)}
.wide:not(.has-image) .copy{margin:auto 0}

/* square: centred stack */
.square{flex-direction:column;text-align:center;align-items:center}
.square .top{justify-content:center;margin-bottom:calc(var(--u)*3)}
.square .sub{margin-left:auto;margin-right:auto}
.square .shot{margin-top:calc(var(--u)*4);flex:1;min-height:0;width:100%}
.square .bottom{margin-top:calc(var(--u)*3);justify-content:center}
.square:not(.has-image) .copy{margin:auto 0}

/* slide: centred end frame */
.slide{align-items:center;justify-content:center;text-align:center}
.slide .center{display:flex;flex-direction:column;align-items:center}
.slide .logo{height:calc(var(--u)*9);margin-bottom:calc(var(--u)*4)}
.slide .sub{margin-left:auto;margin-right:auto}
.slide .pill{margin-top:calc(var(--u)*4);padding:calc(var(--u)*1.2) calc(var(--u)*2.6);border-radius:999px;
  border:1px solid color-mix(in srgb,var(--fg) 22%,transparent);background:color-mix(in srgb,var(--fg) 6%,transparent)}
.slide .bottom{position:absolute;bottom:calc(var(--u)*5);left:0;right:0;justify-content:center}
.slide .footer{margin:0}
"""

FONT_STACK = (
    'Inter, "SF Pro Display", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif'
)
MONO_STACK = '"JetBrains Mono", "SF Mono", Menlo, Consolas, monospace'


def templates() -> dict[str, tuple[int, int]]:
    out = {}
    for p in sorted(TEMPLATE_DIR.glob("*.html")):
        m = re.search(r"(\d{3,4})x(\d{3,4})$", p.stem)
        if m:
            out[p.stem] = (int(m.group(1)), int(m.group(2)))
        elif p.stem.endswith("-1080"):
            out[p.stem] = (1080, 1080)
    return out


def parse_size(size: str, default: tuple[int, int]) -> tuple[int, int]:
    if not size:
        return default
    m = _SIZE_RE.match(str(size).strip().lower())
    if not m:
        raise ValueError(f"size must look like 1280x640, got {size!r}")
    w, h = int(m.group(1)), int(m.group(2))
    if not (200 <= w <= 4096 and 200 <= h <= 4096):
        raise ValueError("card size must be between 200 and 4096 px per side")
    return w, h


def _font(name: str, stack: str) -> str:
    name = re.sub(r"[^A-Za-z0-9 \-_]", "", name or "").strip()
    return f'"{name}", {stack}' if name else stack


def build_html(template: str, data: dict[str, Any], size: tuple[int, int], look: dict[str, Any]) -> str:
    """The full card page. Every text value is escaped; images arrive as data: URIs."""
    path = TEMPLATE_DIR / f"{template}.html"
    if not path.is_file():
        raise ValueError(f"unknown template {template!r} — bundled: {', '.join(templates())}")
    body = path.read_text(encoding="utf-8")
    w, h = size
    unit = min(w, h * 2) / 160  # one design unit; type and spacing scale with the canvas
    title = str(data.get("title") or "")
    title_size = unit * (7.2 if len(title) <= 28 else 6.0 if len(title) <= 48 else 5.0)
    if template.startswith("title-slide"):
        title_size *= 1.15
    c, f = look["colors"], look["fonts"]
    css_vars = (
        f":root{{--w:{w}px;--h:{h}px;--u:{unit:.3f}px;--title-size:{title_size:.1f}px;"
        f"--bg:{c['bg']};--fg:{c['fg']};--accent:{c['accent']};--muted:{c['muted']};"
        f"--font-heading:{_font(f.get('heading', ''), FONT_STACK)};--font-body:{_font(f.get('body', ''), FONT_STACK)};"
        f"--font-mono:{MONO_STACK}}}"
    )
    logo = brandmod.data_uri(look.get("logo", ""))
    image = brandmod.data_uri(str(data.get("image") or ""), max_bytes=12_000_000)
    values = {k: html.escape(str(data.get(k) or "")) for k in TEXT_KEYS}
    values["brand"] = html.escape(str(data.get("brand") or look.get("name") or ""))
    fit = " fit" if str(data.get("image_fit") or "cover").lower() == "contain" else ""
    raw = {
        "logo_html": f'<img class="logo" alt="" src="{logo}">' if logo else "",
        "image_html": f'<div class="shot{fit}"><img alt="" src="{image}"></div>' if image else "",
        "layout": "has-image" if image else "no-image",
    }

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key in raw:
            return raw[key]
        return values.get(key, "")

    body = re.sub(r"\{\{\s*([a-z_]+)\s*\}\}", sub, body)
    # Hide empty text slots rather than leaving gaps.
    for key in ("eyebrow", "subtitle", "url", "footer"):
        if not values[key]:
            cls = {"subtitle": "sub"}.get(key, key)
            body = re.sub(rf'class="({cls}[^"]*)"', r'class="\1 empty"', body, count=1)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{css_vars}{BASE_CSS}</style></head><body>{body}</body></html>"
    )


def render_page(
    page_html: str,
    out_path: str | Path,
    width: int,
    height: int,
    *,
    max_bytes: int | None = None,
    transparent: bool = False,
    playwright_factory: Callable | None = None,
) -> dict[str, Any]:
    """Render a finished, self-contained HTML page to PNG in the out-of-process worker.

    Returns the worker's ``{path, attempts}``. ``transparent`` keeps the page's own alpha (a
    montage's lower-third overlay); the page loads nothing from the network either way."""
    out_path = Path(out_path).with_suffix(".png")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    from . import shoot  # the worker runner (and its fence) lives with the shoot

    job = {
        "v": pw_worker.JOB_VERSION,
        "kind": "card",
        "html": page_html,
        "out_path": str(out_path),
        "width": int(width),
        "height": int(height),
        "max_bytes": max_bytes,
        "transparent": bool(transparent),
        "fence": shoot.fence(),
    }
    report = shoot.run_worker(job, CARD_TIMEOUT_S, playwright_factory)
    if not report.get("ok"):
        raise shoot.WorkerError(f"the card didn't render: {report.get('error') or 'unknown error'}")
    return report["result"]


def render(
    template: str,
    data: dict[str, Any],
    out_path: str | Path,
    *,
    size: str = "",
    limit: str | None = None,
    playwright_factory: Callable | None = None,
) -> dict[str, Any]:
    """Render a card. Returns {path, size_bytes, width, height, format, attempts, violations, brand_source}."""
    tpls = templates()
    if template not in tpls:
        raise ValueError(f"unknown template {template!r} — bundled: {', '.join(tpls)}")
    w, h = parse_size(size, tpls[template])
    look = brandmod.resolve(data)
    page_html = build_html(template, data, (w, h), look)
    limit_id = DEFAULT_LIMITS.get(template, "") if limit is None else limit
    row = limits.get(limit_id) if limit_id else None
    if limit_id and row is None:
        raise ValueError(f"unknown limit {limit_id!r} — campaign_limits lists them")
    max_bytes = int(row["max_bytes"]) if row and row.get("max_bytes") else None
    res = render_page(page_html, out_path, w, h, max_bytes=max_bytes, playwright_factory=playwright_factory)
    final = Path(res["path"])
    size_bytes = final.stat().st_size
    violations = (
        limits.check(limit_id, size=size_bytes, width=w, height=h, fmt=final.suffix.lstrip(".")) if limit_id else []
    )
    return {
        "path": str(final),
        "size_bytes": size_bytes,
        "width": w,
        "height": h,
        "format": final.suffix.lstrip("."),
        "attempts": res["attempts"],
        "violations": violations,
        "limit": limit_id,
        "brand_source": look["source"],
    }
