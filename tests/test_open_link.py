"""The gallery's "Open in new tab": a gated mint route, a public signed-link serve route."""

from __future__ import annotations

import os
import re
import stat
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import campaign
import pytest
import yaml
from campaign import openlink, paths, store, view
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text(encoding="utf-8"))
PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"


class GatedApp:
    """The host's posture in miniature: /api/* needs the bearer; manifest public_paths (matched
    as PREFIXES, like a2a_impl/auth.py `_is_public`) pass without one; everything else needs it."""

    def __init__(self, registry):
        app = FastAPI()
        for prefix, router in registry.routers:
            app.include_router(router, prefix=prefix)

        @app.middleware("http")
        async def _gate(request, call_next):
            from fastapi.responses import JSONResponse

            path = request.url.path
            if any(path.startswith(p) for p in MANIFEST["public_paths"]):
                return await call_next(request)
            if request.headers.get("authorization") != "Bearer op":
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            return await call_next(request)

        self.client = TestClient(app)


@pytest.fixture
def gated(registry):
    campaign.register(registry)
    return GatedApp(registry).client


AUTH = {"authorization": "Bearer op"}


def _image(name="card.png", data=PNG):
    c = store.create_campaign("Launch")
    p = paths.campaign_dir(c["id"], "Launch") / name
    p.write_bytes(data)
    return store.add_asset(c["id"], "card", "t", path=str(p), status="ready_for_review")


def _mint(client, asset_id):
    r = client.post(f"/api/plugins/campaign/assets/{asset_id}/open-link", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def _split(url):
    u = urlsplit(url)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    return u.path, q


# ── mint → serve ──────────────────────────────────────────────────────────────
def test_mint_then_open_serves_the_file_with_no_bearer(gated):
    a = _image()
    j = _mint(gated, a["id"])
    assert j["expires_in"] == 15 * 60
    path, q = _split(j["url"])
    assert path == f"/plugins/campaign/open/{a['id']}" and set(q) == {"exp", "sig"}
    assert re.fullmatch(r"[0-9a-f]{64}", q["sig"]) and abs(int(q["exp"]) - (time.time() + 900)) < 5
    r = gated.get(j["url"])  # no Authorization header
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png"


def test_open_headers(gated):
    a = _image()
    r = gated.get(_mint(gated, a["id"])["url"])
    h = r.headers
    assert h["content-disposition"] == "inline"
    assert h["cache-control"] == "private, no-store"
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "no-referrer"
    assert "sandbox" in h["content-security-policy"] and "default-src 'none'" in h["content-security-policy"]


def test_the_link_is_relative_so_the_view_can_put_the_slug_base_in_front(gated):
    a = _image()
    url = _mint(gated, a["id"])["url"]
    assert url.startswith("/plugins/campaign/open/") and "://" not in url


# ── refusals ──────────────────────────────────────────────────────────────────
def test_an_expired_link_is_refused(gated, monkeypatch):
    a = _image()
    url = _mint(gated, a["id"])["url"]
    real = time.time
    monkeypatch.setattr(openlink.time, "time", lambda: real() + 15 * 60 + 1)
    r = gated.get(url)
    assert r.status_code == 403 and "expired" in r.json()["detail"]


def test_a_forged_signature_is_refused(gated):
    a = _image()
    path, q = _split(_mint(gated, a["id"])["url"])
    flipped = ("0" if q["sig"][0] != "0" else "1") + q["sig"][1:]
    assert gated.get(path, params={"exp": q["exp"], "sig": flipped}).status_code == 403
    assert gated.get(path, params={"exp": q["exp"], "sig": q["sig"].upper()}).status_code == 403
    assert gated.get(path, params={"exp": q["exp"]}).status_code == 403
    assert gated.get(path).status_code == 403
    assert gated.get(path, params={"exp": "abc", "sig": q["sig"]}).status_code == 403


def test_a_stretched_expiry_is_refused(gated):
    a = _image()
    path, q = _split(_mint(gated, a["id"])["url"])
    assert gated.get(path, params={"exp": str(int(q["exp"]) + 1), "sig": q["sig"]}).status_code == 403
    far = str(int(time.time()) + 86400)
    assert gated.get(path, params={"exp": far, "sig": q["sig"]}).status_code == 403


def test_one_assets_link_never_opens_another(gated):
    a, b = _image("a.png"), _image("b.png", PNG + b"b")
    _, q = _split(_mint(gated, a["id"])["url"])
    r = gated.get(f"/plugins/campaign/open/{b['id']}", params=q)
    assert r.status_code == 403


def test_a_rerendered_file_invalidates_old_links(gated):
    a = _image()
    url = _mint(gated, a["id"])["url"]
    p = Path(a["path"])
    p.write_bytes(PNG + b"re-rendered")  # same path, new bytes
    assert gated.get(url).status_code == 403
    fresh = _mint(gated, a["id"])["url"]
    assert gated.get(fresh).content.endswith(b"re-rendered")


def test_a_same_size_rewrite_still_invalidates(gated):
    a = _image()
    url = _mint(gated, a["id"])["url"]
    p = Path(a["path"])
    st = p.stat()
    p.write_bytes(PNG[:-1] + b"X")  # same size
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert gated.get(url).status_code == 403


def test_repointing_the_row_at_another_file_invalidates(gated):
    a = _image("a.png")
    url = _mint(gated, a["id"])["url"]
    other = Path(a["path"]).with_name("other.png")
    other.write_bytes(PNG)
    store.update_asset(a["id"], path=str(other))
    assert gated.get(url).status_code == 403


def test_a_deleted_file_is_refused_and_cant_be_minted(gated):
    a = _image()
    url = _mint(gated, a["id"])["url"]
    Path(a["path"]).unlink()
    assert gated.get(url).status_code == 403
    assert gated.post(f"/api/plugins/campaign/assets/{a['id']}/open-link", headers=AUTH).status_code == 404
    assert gated.post("/api/plugins/campaign/assets/99999/open-link", headers=AUTH).status_code == 404


def test_a_pruned_tombstone_cant_be_minted(gated):
    a = _image()
    store.update_asset(a["id"], path="", meta={"pruned": True})
    assert gated.post(f"/api/plugins/campaign/assets/{a['id']}/open-link", headers=AUTH).status_code == 404


# ── containment ───────────────────────────────────────────────────────────────
def test_files_outside_the_media_root_are_never_minted_or_served(gated, tmp_path):
    c = store.create_campaign("Launch")
    outside = tmp_path / "elsewhere" / "x.png"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(PNG)
    a = store.add_asset(c["id"], "still", "t", path=str(outside))
    link = paths.campaign_dir(c["id"], "Launch") / "link.png"
    os.symlink(outside, link)
    b = store.add_asset(c["id"], "still", "t", path=str(link))
    sneaky = store.add_asset(c["id"], "still", "t", path=str(paths.media_root() / ".." / "x.png"))
    (paths.data_dir() / "x.png").write_bytes(PNG)
    for x in (a, b, sneaky):
        assert gated.post(f"/api/plugins/campaign/assets/{x['id']}/open-link", headers=AUTH).status_code == 404
        # …and a link signed for it anyway (the right key, the file's real fingerprint) is still refused:
        # the serve route re-checks containment itself.
        exp = int(time.time()) + 600
        p = Path(x["path"]).resolve()
        sig = openlink._sign(x["id"], exp, f"{p.name}\n{p.stat().st_size}\n{p.stat().st_mtime_ns}")
        assert gated.get(f"/plugins/campaign/open/{x['id']}", params={"exp": exp, "sig": sig}).status_code == 403


def test_a_file_moved_out_after_minting_is_refused(gated, tmp_path):
    a = _image()
    url = _mint(gated, a["id"])["url"]
    p = Path(a["path"])
    dest = tmp_path / "out.png"
    p.rename(dest)
    os.symlink(dest, p)  # the row's path now escapes through a symlink
    assert gated.get(url).status_code == 403


@pytest.mark.parametrize("name", ["evil.html", "evil.svg", "notes.txt"])
def test_only_servable_media_get_a_link(gated, name):
    c = store.create_campaign("Launch")
    p = paths.campaign_dir(c["id"], "Launch") / name
    p.write_text("<script>alert(1)</script>")
    a = store.add_asset(c["id"], "still", "t", path=str(p))
    assert gated.post(f"/api/plugins/campaign/assets/{a['id']}/open-link", headers=AUTH).status_code == 415
    exp = int(time.time()) + 600
    sig = openlink._sign(a["id"], exp, openlink._fingerprint(p.resolve()))
    assert gated.get(f"/plugins/campaign/open/{a['id']}", params={"exp": exp, "sig": sig}).status_code == 403


# ── public vs gated ───────────────────────────────────────────────────────────
def test_the_open_route_is_public_and_minting_is_gated(gated):
    a = _image()
    assert gated.post(f"/api/plugins/campaign/assets/{a['id']}/open-link").status_code == 401
    assert "/plugins/campaign/open/" in MANIFEST["public_paths"]
    # Public in the manifest means only /plugins/campaign/open/…, never the data API.
    assert not any(p.startswith("/api/") for p in MANIFEST["public_paths"])
    assert gated.get(f"/plugins/campaign/open/{a['id']}").status_code == 403  # reachable, but refuses unsigned
    assert gated.get(f"/api/plugins/campaign/file/{a['id']}").status_code == 401


def test_the_mint_route_is_post_only(gated):
    a = _image()
    assert gated.get(f"/api/plugins/campaign/assets/{a['id']}/open-link", headers=AUTH).status_code == 405


def test_the_open_route_lives_on_the_existing_view_router(registry):
    campaign.register(registry)
    prefixes = [p for p, _ in registry.routers]
    assert len(prefixes) == len(set(prefixes)) and registry.dropped_routers == []
    (view_router,) = [r for p, r in registry.routers if p == "/plugins/campaign"]
    assert {r.path for r in view_router.routes} == {"/view", "/open/{asset_id}"}


# ── the signing key ───────────────────────────────────────────────────────────
def test_the_key_is_per_instance_random_owner_only_and_outside_the_media_root():
    openlink._cache.clear()
    k = openlink._key()
    f = paths.data_dir() / openlink.KEY_FILE
    assert f.read_text().strip().encode() == k and len(k) == 64
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert not paths.is_contained(f)
    openlink._cache.clear()
    assert openlink._key() == k  # generated once, then reused


def test_a_truncated_key_file_is_replaced(tmp_path):
    openlink._cache.clear()
    f = paths.data_dir() / openlink.KEY_FILE
    f.write_text("")
    k = openlink._key()
    assert len(k) == 64 and stat.S_IMODE(f.stat().st_mode) == 0o600


def test_a_new_key_kills_old_links(gated):
    a = _image()
    url = _mint(gated, a["id"])["url"]
    (paths.data_dir() / openlink.KEY_FILE).unlink()  # no cache clear: revocation needs no restart
    assert gated.get(url).status_code == 403
    assert gated.get(_mint(gated, a["id"])["url"]).status_code == 200


# ── the view ──────────────────────────────────────────────────────────────────
def test_the_view_has_the_chats_open_in_new_tab_button():
    page = view.PAGE
    # The chat's exact label and NewTabIcon path (DS markdown-image.tsx).
    assert 'data-img="newtab" title="Open in new tab" aria-label="Open in new tab"' in page
    assert 'd="M7 3.5H3.5v9h9V9M9.75 2.75h3.5v3.5M13 3 7.75 8.25"' in page
    assert 'api(`/assets/${id}/open-link`, { method: "POST" })' in page
    assert "location.origin + BASE + j.url" in page  # slug-aware: through /agents/<slug>/ under the hub
    assert 'window.open(url, "_blank")' in page and "w.opener = null" in page
    assert "blocked the new tab" in page  # an honest message when the pop-up is blocked
    # Order matches chat: Download · View fullscreen · Open in new tab.
    i = [page.index(f'data-img="{k}"') for k in ("download", "fullscreen", "newtab")]
    assert i == sorted(i)


# ── what is checked is what is sent ───────────────────────────────────────────
@pytest.mark.parametrize("when", ["after_open", "before_open"])
def test_swapping_the_file_for_a_symlink_mid_request_never_serves_the_target(gated, tmp_path, monkeypatch, when):
    a = _image()
    url = _mint(gated, a["id"])["url"]
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"TOP SECRET outside media root")
    p = Path(a["path"])
    real_open = openlink.os.open

    def swap():
        p.unlink()
        os.symlink(secret, p)

    def racing_open(path, flags, *args):
        if when == "before_open":
            swap()  # the open itself now meets the symlink (O_NOFOLLOW refuses it)
            return real_open(path, flags, *args)
        fd = real_open(path, flags, *args)
        swap()  # served from the descriptor already open on the real file; the re-resolve sees the swap
        return fd

    monkeypatch.setattr(openlink.os, "open", racing_open)
    r = gated.get(url)
    assert b"TOP SECRET" not in r.content and r.status_code == 403


def test_the_body_is_exactly_the_checked_size(gated):
    a = _image()
    r = gated.get(_mint(gated, a["id"])["url"])
    assert r.headers["content-length"] == str(len(PNG)) and r.content == PNG


def test_a_garbage_key_file_is_replaced_not_a_500(gated):
    a = _image()
    f = paths.data_dir() / openlink.KEY_FILE
    f.write_bytes("é".encode() * 40)  # non-ASCII, long enough to look like a key
    assert gated.get(_mint(gated, a["id"])["url"]).status_code == 200
    assert len(f.read_text().strip()) == 64


def test_a_loosened_key_file_goes_back_to_owner_only():
    f = paths.data_dir() / openlink.KEY_FILE
    openlink._key()
    os.chmod(f, 0o644)
    openlink._cache.clear()
    openlink._key()
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_concurrent_first_use_agrees_on_one_key():
    from concurrent.futures import ThreadPoolExecutor

    openlink._cache.clear()
    with ThreadPoolExecutor(8) as ex:
        keys = set(ex.map(lambda _: openlink._key(), range(32)))
    assert len(keys) == 1
    assert not list(paths.data_dir().glob(openlink.KEY_FILE + ".*.tmp")), "no temp files left behind"
