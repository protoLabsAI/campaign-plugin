"""Where this plugin keeps its state — instance-scoped, host-free.

Resolution order for the base directory:

1. ``CAMPAIGN_DIR`` (env) — the hook the test suite uses to point everything at a temp dir.
2. The operator's ``data_dir`` setting, taken literally.
3. The host's ``sdk.plugin_store()`` directory, handed in by ``register()`` — already
   instance-scoped by the host (ADR 0004 / 0065).
4. ``~/.protoagent/campaign`` plus a per-instance subdir when ``PROTOAGENT_INSTANCE`` is
   set — the same convention as the social plugin, for hosts without ``plugin_store``.

Every campaign gets its own directory under ``<base>/campaigns/<id>-<slug>/``; the gallery's
file route refuses to serve anything outside ``<base>/campaigns``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Set by register() from plugin config / the host; the env var still wins.
_CONFIGURED_DIR: str = ""
_HOST_STORE_DIR: str = ""


def configure(directory: str = "", host_store: str = "") -> None:
    """Point the plugin at an operator-configured data dir and/or the host's plugin store."""
    global _CONFIGURED_DIR, _HOST_STORE_DIR
    _CONFIGURED_DIR = (directory or "").strip()
    _HOST_STORE_DIR = (host_store or "").strip()


def data_dir() -> Path:
    """The directory holding the campaign database and every campaign's media."""
    env = os.environ.get("CAMPAIGN_DIR", "").strip()
    if env:
        root = Path(env).expanduser()
    elif _CONFIGURED_DIR:
        root = Path(_CONFIGURED_DIR).expanduser()
    elif _HOST_STORE_DIR:
        root = Path(_HOST_STORE_DIR)
    else:
        root = Path.home() / ".protoagent" / "campaign"
        inst = os.environ.get("PROTOAGENT_INSTANCE", "").strip()
        if inst:
            root = root / inst
    root.mkdir(parents=True, exist_ok=True)
    return root


def media_root() -> Path:
    """The root every served asset must live under (path containment boundary)."""
    p = data_dir() / "campaigns"
    p.mkdir(parents=True, exist_ok=True)
    return p


def slug(text: str, fallback: str = "item") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s or fallback)[:48]


def campaign_dir(campaign_id: int, name: str = "") -> Path:
    """``<media_root>/<id>-<slug>`` — created on first use."""
    root = media_root()
    # Re-use an existing dir for this id even if the campaign was renamed since.
    for existing in root.glob(f"{int(campaign_id)}-*"):
        if existing.is_dir():
            return existing
    p = root / f"{int(campaign_id)}-{slug(name, 'campaign')}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def is_contained(path: str | os.PathLike, root: Path | None = None) -> bool:
    """True when ``path`` (symlinks resolved) sits inside ``root`` (default: media root)."""
    root = (root or media_root()).resolve()
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return resolved == root or root in resolved.parents
