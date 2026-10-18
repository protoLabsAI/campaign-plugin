"""Short-lived signed links for the gallery's "Open in new tab" — host-free.

The gallery's media are private: the page fetches them through the gated
``/api/plugins/campaign/file/<id>`` with the operator bearer and shows ``blob:`` URLs, which a
new browser tab (or the desktop app's system browser) can't open. So "Open in new tab" asks the
gated mint route for a link that works WITHOUT a bearer, for one asset, for 15 minutes:

    /plugins/campaign/open/<id>?exp=<unix seconds>&sig=<64 hex>

``sig`` is HMAC-SHA256 over the asset id, the expiry and the file's fingerprint — its path
relative to the media root, its size and its mtime (ns) — keyed by a random per-instance secret
kept beside the plan db (``<data_dir>/open-link.key``, mode 600, never under the media root so
it can never be served). A re-render (a new file, or the same file rewritten) changes the
fingerprint, so every link minted for the old bytes stops working. Nothing is stored per link:
a link is valid exactly while its signature verifies and its expiry is ahead.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

from . import paths

TTL_S = 15 * 60
# A link never verifies with an expiry further out than this (clock skew slack included), so
# even a link minted under a longer TTL by some future version is capped here.
_MAX_AHEAD_S = TTL_S + 60
KEY_FILE = "open-link.key"
OPEN_PREFIX = "/plugins/campaign/open/"

_cache: dict[str, bytes] = {}


def _key() -> bytes:
    """The instance's signing secret — created once (32 random bytes, hex, mode 600)."""
    path = paths.data_dir() / KEY_FILE
    try:  # keyed on the file's identity, so deleting / replacing it revokes links WITHOUT a restart
        st = path.stat()
        ident = f"{path}\n{st.st_ino}\n{st.st_mtime_ns}\n{st.st_size}"
    except OSError:
        ident = ""
    hit = _cache.get(ident) if ident else None
    if hit:
        return hit
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(secrets.token_hex(32))
    raw = path.read_text(encoding="ascii").strip()
    if len(raw) < 64:  # empty / truncated (a crash mid-write): replace it, old links die
        tmp = path.with_name(KEY_FILE + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(secrets.token_hex(32))
        os.replace(tmp, path)
        raw = path.read_text(encoding="ascii").strip()
    key = raw.encode("ascii")
    st = path.stat()
    _cache.clear()
    _cache[f"{path}\n{st.st_ino}\n{st.st_mtime_ns}\n{st.st_size}"] = key
    return key


def resolve(raw: str) -> Path | None:
    """The asset file, symlinks resolved, when it exists, is a regular file and sits inside the
    media root — else None. Resolved ONCE; callers serve exactly this path."""
    if not raw:
        return None
    try:
        p = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not p.is_file() or not paths.is_contained(p):
        return None
    return p


def _fingerprint(p: Path) -> str:
    st = p.stat()
    rel = p.relative_to(paths.media_root().resolve()).as_posix()
    return f"{rel}\n{st.st_size}\n{st.st_mtime_ns}"


def _sign(asset_id: int, exp: int, fingerprint: str) -> str:
    msg = f"campaign-open/v1\n{int(asset_id)}\n{int(exp)}\n{fingerprint}".encode()
    return hmac.new(_key(), msg, hashlib.sha256).hexdigest()


def mint(asset_id: int, p: Path, *, now: float | None = None) -> dict:
    """A link for the (already resolved + contained) file ``p`` of asset ``asset_id``."""
    exp = int(now if now is not None else time.time()) + TTL_S
    sig = _sign(asset_id, exp, _fingerprint(p))
    return {"url": f"{OPEN_PREFIX}{int(asset_id)}?exp={exp}&sig={sig}", "expires_in": TTL_S, "exp": exp}


def check(asset_id: int, exp: str, sig: str, p: Path | None, *, now: float | None = None) -> str:
    """``ok`` | ``expired`` | ``invalid``. ``p`` is the asset's resolved, contained file (None
    when it has none). The expiry is checked before the signature, and every other failure —
    a forged or malformed signature, another asset's link, a re-rendered or missing file — is
    the same ``invalid``, so a caller without a link learns nothing about which assets exist."""
    if not (isinstance(exp, str) and exp.isascii() and exp.isdigit() and len(exp) <= 12):
        return "invalid"
    if not (isinstance(sig, str) and len(sig) == 64 and all(c in "0123456789abcdef" for c in sig)):
        return "invalid"
    t = int(now if now is not None else time.time())
    e = int(exp)
    if e > t + _MAX_AHEAD_S:
        return "invalid"
    if e <= t:
        return "expired"
    if p is None:
        return "invalid"
    try:
        want = _sign(asset_id, e, _fingerprint(p))
    except (OSError, ValueError):
        return "invalid"
    return "ok" if hmac.compare_digest(want, sig) else "invalid"
