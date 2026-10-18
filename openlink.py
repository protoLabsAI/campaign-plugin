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
import stat
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


def _valid_key(raw: str) -> bool:
    return len(raw) == 64 and all(c in "0123456789abcdef" for c in raw)


def _read_key(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _write_new_key(path: Path, *, replace: bool) -> None:
    """Write a fresh key to a UNIQUE temp file (mode 600), then put it in place atomically:
    ``os.link`` when creating (fails if another process won the race — its key stands), or
    ``os.replace`` when repairing a corrupt one. Readers never see a half-written key."""
    tmp = path.with_name(f"{KEY_FILE}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(secrets.token_hex(32))
        if replace:
            os.replace(tmp, path)
        else:
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


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
    if not path.exists():
        _write_new_key(path, replace=False)
    raw = _read_key(path)
    if not _valid_key(raw):  # empty / truncated / garbage: replace it — any old links die
        _write_new_key(path, replace=True)
        raw = _read_key(path)
        if not _valid_key(raw):
            raise OSError("could not write the open-link key")
    try:
        os.chmod(path, 0o600)  # a key file someone loosened goes back to owner-only
    except OSError:
        pass
    st = path.stat()
    _cache.clear()
    _cache[f"{path}\n{st.st_ino}\n{st.st_mtime_ns}\n{st.st_size}"] = raw.encode("ascii")
    return raw.encode("ascii")


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


def _fingerprint(p: Path, st: os.stat_result | None = None) -> str:
    st = st or p.stat()
    rel = p.relative_to(paths.media_root().resolve()).as_posix()
    return f"{rel}\n{st.st_size}\n{st.st_mtime_ns}\n{st.st_dev}:{st.st_ino}"


def _sign(asset_id: int, exp: int, fingerprint: str) -> str:
    msg = f"campaign-open/v1\n{int(asset_id)}\n{int(exp)}\n{fingerprint}".encode()
    return hmac.new(_key(), msg, hashlib.sha256).hexdigest()


def mint(asset_id: int, p: Path, *, now: float | None = None) -> dict:
    """A link for the (already resolved + contained) file ``p`` of asset ``asset_id``."""
    exp = int(now if now is not None else time.time()) + TTL_S
    sig = _sign(asset_id, exp, _fingerprint(p))
    return {"url": f"{OPEN_PREFIX}{int(asset_id)}?exp={exp}&sig={sig}", "expires_in": TTL_S, "exp": exp}


def _precheck(exp: str, sig: str, now: float | None) -> tuple[str, int]:
    if not (isinstance(exp, str) and exp.isascii() and exp.isdigit() and len(exp) <= 12):
        return "invalid", 0
    if not (isinstance(sig, str) and len(sig) == 64 and all(c in "0123456789abcdef" for c in sig)):
        return "invalid", 0
    t = int(now if now is not None else time.time())
    e = int(exp)
    if e > t + _MAX_AHEAD_S:
        return "invalid", 0
    if e <= t:
        return "expired", 0
    return "ok", e


def open_checked(asset_id: int, exp: str, sig: str, p: Path | None, *, now: float | None = None):
    """``(verdict, fd, stat)`` — verdict ``ok`` | ``expired`` | ``invalid``; on ``ok`` an OPEN
    read-only descriptor of exactly the bytes the signature was checked against (the caller
    streams from it and closes it), else ``(verdict, None, None)``.

    ``p`` is the asset's resolved, contained file (None when it has none). The expiry is checked
    before anything else, and every other failure — a forged or malformed signature, another
    asset's link, a re-rendered, moved or missing file — is the same ``invalid``, so a caller
    without a link learns nothing about which assets exist.

    The file is opened ONCE (no following a final symlink) and everything is checked against
    that descriptor: it must be a regular file, the path must STILL resolve inside the media
    root to the same inode, and the signature covers its size, mtime and inode as fstat sees
    them. Swapping the file for a symlink (or anything else) between the check and the send
    can't change what is served."""
    verdict, e = _precheck(exp, sig, now)
    if verdict != "ok":
        return verdict, None, None
    if p is None:
        return "invalid", None, None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(p, flags)
    except OSError:
        return "invalid", None, None
    ok = False
    try:
        st = os.fstat(fd)
        real = resolve(str(p)) if stat.S_ISREG(st.st_mode) else None
        if real is not None:
            now_st = os.stat(real)
            if (now_st.st_dev, now_st.st_ino) == (st.st_dev, st.st_ino):
                want = _sign(asset_id, e, _fingerprint(real, st))
                ok = hmac.compare_digest(want, sig)
    except (OSError, ValueError):
        ok = False
    finally:
        if not ok:
            os.close(fd)
    return ("ok", fd, st) if ok else ("invalid", None, None)


def check(asset_id: int, exp: str, sig: str, p: Path | None, *, now: float | None = None) -> str:
    """Just the verdict of ``open_checked`` (the descriptor closed again)."""
    verdict, fd, _ = open_checked(asset_id, exp, sig, p, now=now)
    if fd is not None:
        os.close(fd)
    return verdict
