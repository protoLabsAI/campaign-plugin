"""Back up APPROVED media to protoContent — release assets for the files, a catalog via PR.

protoContent (``protoLabsAI/protoContent``, private) is the marketing hub. What lands there:

* **Files** → a GitHub **release asset** on a release per campaign, tag ``media/<slug>`` (made
  when missing, never marked "latest", so it can't displace a package release). Video never
  goes into git. Asset names are ``<sha256[:12]>-<file name>`` — content-addressed, so the same
  bytes never upload twice.
* **The catalog** → ``specs/media/catalog/<slug>.json`` (the ``archive_catalog_dir`` setting),
  validating against protoContent's ``schemas/media/catalog.schema.json``: one entry per
  approved asset (id, title, lane, kind, dims, duration, size, sha256, approved_at,
  review_note, the release asset URL, the source campaign). protoContent's ``main`` takes
  changes only through PRs, so catalog updates are **batched onto one open PR per campaign**
  (branch ``media-catalog/<slug>``): every drain adds its new entries to that branch, and a new
  PR opens only after the last one merged. Nothing here ever merges it.

**Flow.** Each pass first :func:`reconcile`-s: every approved asset with no archive record (or
none for its current campaign's catalog) is queued — so an approval in the gallery, or an
approved asset collected into another campaign, needs no hook anywhere. :func:`backfill`
re-queues every approved, non-superseded asset explicitly (and un-parks a parked one). The library surface drains the
queue every minute while ``archive_enabled`` is on and a token is set: hash → upload (unless
``meta.archive.sha256`` already matches — same sha256 = already archived; a same-named asset
already on the release with the right size is adopted instead of re-uploaded) → record
``meta.archive = {url, sha256, at, …}`` → commit the campaign's catalog once for the batch.
A failure backs off (1 min → 6 h) and retries; nothing is lost.

**The token** (``archive_token``, a secret) goes only into an ``Authorization`` header to
``api.github.com`` / ``uploads.github.com``, never into a log line, an error, a URL or a
return value (:func:`_redact` strips it from anything that might echo it). It needs, on
protoContent only: **Contents: read & write** (releases, assets, the catalog branch + commit)
and **Pull requests: read & write** (open the catalog PR). Metadata: read comes with it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import paths, store

log = logging.getLogger("protoagent.plugins.campaign")

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
DEFAULT_REPO = "protoLabsAI/protoContent"
DEFAULT_CATALOG_DIR = "specs/media/catalog"
TAG_PREFIX = "media/"
BRANCH_PREFIX = "media-catalog/"
BATCH = 25  # assets per drain — a drain stays bounded; the next tick takes the rest
BACKOFF_S = (60, 300, 1800, 7200, 21600)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")  # protoContent's slug pattern
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_]{20,255}$")  # GitHub tokens: no spaces, newlines or quotes
PARK_AFTER = 10  # attempts; then it waits for a backfill / re-approval instead of retrying forever


class ArchiveError(RuntimeError):
    pass


def _redact(text: str, token: str) -> str:
    text = str(text)
    if token:
        text = text.replace(token, "***")
    return re.sub(r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})", "***", text)


# ── settings ──────────────────────────────────────────────────────────────────
def settings(cfg: dict[str, Any] | None) -> dict[str, Any]:
    cfg = cfg or {}
    repo = str(cfg.get("archive_repo") or DEFAULT_REPO).strip()
    cat = str(cfg.get("archive_catalog_dir") or DEFAULT_CATALOG_DIR).strip().strip("/")
    enabled = cfg.get("archive_enabled")
    return {
        "enabled": enabled is True or str(enabled).strip().lower() in ("true", "1", "yes", "on"),
        "repo": repo,
        "catalog_dir": cat,
        "token": str(cfg.get("archive_token") or "").strip(),
    }


def problems(s: dict[str, Any]) -> list[str]:
    out = []
    if not s["enabled"]:
        out.append("archive_enabled is off")
    if not s["token"]:
        out.append("no archive_token is set")
    elif not TOKEN_RE.fullmatch(s["token"]):
        out.append("archive_token isn't a GitHub token (letters, digits and _ only) — re-paste it")
    if not REPO_RE.fullmatch(s["repo"]):
        out.append(f"archive_repo {s['repo']!r} isn't owner/name")
    if not s["catalog_dir"] or ".." in s["catalog_dir"].split("/"):
        out.append(f"archive_catalog_dir {s['catalog_dir']!r} isn't a plain repo path")
    return out


# ── the GitHub client (stdlib only; the opener is injectable for tests) ───────
class GitHub:
    def __init__(self, token: str, repo: str, opener: Callable[..., Any] | None = None, timeout: float = 120):
        if not token:
            raise ArchiveError("no token")
        self._token = token
        self.repo = repo
        self._open = opener or urllib.request.urlopen
        self._timeout = timeout

    def _req(self, method: str, url: str, body: Any = None, *, data: Any = None, headers: dict | None = None,
             ok: tuple[int, ...] = (200, 201), none_on: tuple[int, ...] = ()) -> Any:  # fmt: skip
        if not url.startswith((API + "/", UPLOADS + "/")):
            raise ArchiveError("refusing a request to a host other than GitHub's API")
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "protoagent-campaign-archive",
            "Authorization": f"Bearer {self._token}",
            **(headers or {}),
        }
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with self._open(req, timeout=self._timeout) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, (e.read() or b"")
        except Exception as e:  # noqa: BLE001 — URLError, OSError, http.client errors, a bad header…
            # …any of which may echo the request (the header carries the token): redact, re-raise ours
            raise ArchiveError(
                _redact(f"{method} {urllib.parse.urlsplit(url).path}: {type(e).__name__}: {e}", self._token)
            ) from None
        if status in none_on:
            return None
        if status not in ok:
            try:
                msg = json.loads(raw or b"{}").get("message") or ""
            except ValueError:
                msg = ""
            raise ArchiveError(
                _redact(f"{method} {urllib.parse.urlsplit(url).path} → HTTP {status} {msg}".strip(), self._token)
            )
        if status == 204 or not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return {}

    def _r(self, path: str) -> str:
        return f"{API}/repos/{self.repo}{path}"

    # releases
    def release_by_tag(self, tag: str) -> dict | None:
        return self._req("GET", self._r(f"/releases/tags/{urllib.parse.quote(tag, safe='')}"), none_on=(404,))

    def create_release(self, tag: str, name: str, body: str, target: str) -> dict:
        return self._req("POST", self._r("/releases"), {"tag_name": tag, "target_commitish": target, "name": name,
                                                       "body": body, "draft": False, "prerelease": False,
                                                       "make_latest": "false"})  # fmt: skip

    def release_assets(self, release_id: int) -> list[dict]:
        out, page = [], 1
        while True:
            got = self._req("GET", self._r(f"/releases/{int(release_id)}/assets?per_page=100&page={page}")) or []
            out += got
            if len(got) < 100:
                return out
            page += 1

    def delete_asset(self, asset_id: int) -> None:
        self._req("DELETE", self._r(f"/releases/assets/{int(asset_id)}"), ok=(204, 200), none_on=(404,))

    def upload_asset(self, release_id: int, name: str, path: Path, content_type: str) -> dict:
        size = path.stat().st_size
        url = f"{UPLOADS}/repos/{self.repo}/releases/{int(release_id)}/assets?name={urllib.parse.quote(name)}"
        with path.open("rb") as fh:
            return self._req("POST", url, data=fh, headers={"Content-Type": content_type, "Content-Length": str(size)})

    # git + contents + PRs
    def default_branch(self) -> str:
        return (self._req("GET", f"{API}/repos/{self.repo}") or {}).get("default_branch") or "main"

    def branch_sha(self, branch: str) -> str | None:
        r = self._req("GET", self._r(f"/git/ref/heads/{urllib.parse.quote(branch)}"), none_on=(404,))
        return ((r or {}).get("object") or {}).get("sha") if r else None

    def create_branch(self, branch: str, sha: str) -> None:
        self._req("POST", self._r("/git/refs"), {"ref": f"refs/heads/{branch}", "sha": sha})

    def reset_branch(self, branch: str, sha: str) -> None:
        self._req("PATCH", self._r(f"/git/refs/heads/{urllib.parse.quote(branch)}"), {"sha": sha, "force": True})

    def get_file(self, path: str, ref: str) -> tuple[Any, str] | None:
        r = self._req("GET", self._r(f"/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}"),
                      none_on=(404,))  # fmt: skip
        if not r:
            return None
        import base64

        content = r.get("content") or ""
        if not content and int(r.get("size") or 0) > 0 and r.get("sha"):
            # the Contents API leaves `content` empty past 1 MB — the blob API serves up to 100 MB
            content = (self._req("GET", self._r(f"/git/blobs/{r['sha']}")) or {}).get("content") or ""
        try:
            return json.loads(base64.b64decode(content)), r.get("sha") or ""
        except ValueError:
            raise ArchiveError(f"{path} on {ref} isn't valid JSON — fix it by hand before the next archive") from None

    def put_file(self, path: str, branch: str, text: str, message: str, sha: str = "") -> None:
        import base64

        body = {"message": message, "content": base64.b64encode(text.encode()).decode(), "branch": branch}
        if sha:
            body["sha"] = sha
        self._req("PUT", self._r(f"/contents/{urllib.parse.quote(path)}"), body)

    def open_pr(self, branch: str) -> dict | None:
        owner = self.repo.split("/")[0]
        got = self._req("GET", self._r(f"/pulls?state=open&head={urllib.parse.quote(f'{owner}:{branch}')}")) or []
        return got[0] if got else None

    def create_pr(self, branch: str, base: str, title: str, body: str) -> dict:
        return self._req("POST", self._r("/pulls"), {"title": title, "head": branch, "base": base, "body": body})


# ── naming ────────────────────────────────────────────────────────────────────
def campaign_slug(c: dict[str, Any]) -> str:
    """The campaign's archive slug — pinned on first use (a rename never forks its release)."""
    if c.get("archive_slug"):
        return c["archive_slug"]
    base = paths.slug(c.get("name") or "", "campaign").strip("-")[:56].strip("-") or "campaign"
    taken = {x.get("archive_slug") for x in store.list_campaigns(include_archived=True) if x["id"] != c["id"]}
    slug = base if base not in taken else f"{base[:50].strip('-')}-{c['id']}"
    if not SLUG_RE.fullmatch(slug):
        slug = f"campaign-{c['id']}"
    store.set_archive_slug(c["id"], slug)
    return slug


def asset_name(path: str, sha: str) -> str:
    stem = Path(path).stem
    suffix = Path(path).suffix.lower()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.")[:80] or "asset"
    return f"{sha[:12]}-{safe}{suffix}"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _file(a: dict[str, Any]) -> Path:
    p = str(a.get("path") or "")
    if not p:
        raise ArchiveError("it has no file")
    if not paths.is_contained(p):
        raise ArchiveError("its file is missing or outside the campaign media root")
    f = Path(p).expanduser().resolve()
    if not f.is_file():
        raise ArchiveError("its file is not a regular file")
    return f


# ── one asset: upload (idempotent by sha256) ──────────────────────────────────
def ensure_release(gh: GitHub, c: dict[str, Any], slug: str, base: str, catalog_path: str) -> dict:
    tag = TAG_PREFIX + slug
    rel = gh.release_by_tag(tag)
    if rel:
        return rel
    body = (
        f"Approved media from the **{c['name']}** campaign, archived by the Campaign Studio plugin.\n\n"
        f"Catalog: `{catalog_path}` (one entry per asset: sha256, dims, approval note, this release's URL).\n"
        "Assets are content-addressed (`<sha256[:12]>-<name>`). Not a software release."
    )
    return gh.create_release(tag, f"Media — {c['name']}", body, base)


def upload(gh: GitHub, a: dict[str, Any], release: Callable[[], dict], now: datetime) -> tuple[dict[str, Any], bool]:
    """Make sure ``a``'s file is on its campaign's release. Returns ``(meta.archive record,
    uploaded?)`` — ``False`` when ``meta.archive.sha256`` already matches the file (same sha256 =
    already archived; ``release`` isn't even called, so a copy never makes an empty release)."""
    f = _file(a)
    digest = sha256(f)
    prev = (a.get("meta") or {}).get("archive") or {}
    if prev.get("sha256") == digest and prev.get("url"):
        return prev, False
    rel = release()
    name = asset_name(str(f), digest)
    size = f.stat().st_size
    have = next((x for x in gh.release_assets(rel["id"]) if x.get("name") == name), None)
    if have is not None and int(have.get("size") or -1) != size:
        gh.delete_asset(have["id"])  # a partial upload from a failed attempt
        have = None
    if have is None:
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        have = gh.upload_asset(rel["id"], name, f, ctype)
    return {
        "url": have.get("browser_download_url") or "",
        "api_url": have.get("url") or "",
        "sha256": digest,
        "at": now.isoformat(timespec="seconds"),
        "repo": gh.repo,
        "release_tag": rel.get("tag_name") or "",
        "asset_name": name,
        "release_asset_id": have.get("id"),
        "size_bytes": size,
        "cataloged_in": [],
    }, True


# ── the catalog (batched onto one open PR per campaign) ───────────────────────
def entry(a: dict[str, Any], c: dict[str, Any], slug: str) -> dict[str, Any]:
    arch = (a.get("meta") or {}).get("archive") or {}
    lane = store.lane_by_id(a["campaign_id"], a["lane_id"]) if a.get("lane_id") else None
    return {
        "id": a["id"],
        "title": a.get("title") or "",
        "lane": (lane or {}).get("name", ""),
        "kind": a["kind"],
        "width": int(a.get("width") or 0),
        "height": int(a.get("height") or 0),
        "duration_s": round(float(a.get("duration_s") or 0), 3),
        "size_bytes": int(arch.get("size_bytes") or a.get("size_bytes") or 0),
        "sha256": arch["sha256"],
        "file_name": Path(str(a.get("path") or "")).name,
        "approved_at": a.get("reviewed_at") or "",
        "review_note": a.get("review_note") or "",
        "url": arch["url"],
        "release_tag": arch.get("release_tag") or "",
        "release_asset_name": arch.get("asset_name") or "",
        "archived_at": arch.get("at") or "",
        "source_campaign": {"id": c["id"], "name": c["name"], "slug": slug},
    }


def _id_key(v: Any) -> int:
    """A sortable id for any entry — a foreign entry's id may be anything (never a crash)."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def catalog_doc(prev: dict[str, Any] | None, c: dict[str, Any], slug: str, entries: list[dict[str, Any]],
                now: datetime) -> dict[str, Any]:  # fmt: skip
    """Append/update only: every entry already in the file stays; ours upsert by sha256 — the
    content's identity, so two agent instances (each with its own asset ids) sharing a campaign
    slug never overwrite each other's entries."""
    from . import __version__

    def k(e: dict[str, Any]) -> str:
        return str(e.get("sha256") or f"id:{e.get('id')}")

    keep = {k(e): e for e in ((prev or {}).get("assets") or []) if isinstance(e, dict)}
    for e in entries:
        keep[k(e)] = e
    return {
        "slug": slug,
        "campaign": {"id": c["id"], "name": c["name"]},
        "release_tag": TAG_PREFIX + slug,
        "source": {"plugin": "campaign", "plugin_version": __version__},
        "updated_at": now.isoformat(timespec="seconds"),
        "assets": sorted(keep.values(), key=lambda e: (_id_key(e.get("id")), k(e))),
    }


def commit_catalog(gh: GitHub, c: dict[str, Any], slug: str, catalog_path: str, base: str,
                   now: datetime) -> dict[str, Any]:  # fmt: skip
    """Write the campaign's catalog onto its open PR's branch (opening branch + PR when none is
    open). The entries are EVERY archived approved asset of the campaign (the local DB is the
    source of truth), merged into what the branch already holds — so a PR closed unmerged is
    simply regenerated, and entries from elsewhere are never dropped."""
    branch = BRANCH_PREFIX + slug
    pr = gh.open_pr(branch)
    if pr is None:
        head = gh.branch_sha(base)
        if not head:
            raise ArchiveError(f"{gh.repo} has no {base} branch")
        if gh.branch_sha(branch):
            gh.reset_branch(branch, head)  # a leftover branch with no open PR starts again from main
        else:
            gh.create_branch(branch, head)
    mine = [
        entry(a, c, slug)
        for a in store.list_assets(c["id"], status="approved")
        if ((a.get("meta") or {}).get("archive") or {}).get("sha256")
    ]
    got = gh.get_file(catalog_path, branch)
    prev, blob = got if got else (None, "")
    doc = catalog_doc(prev, c, slug, mine, now)

    def _same(x: dict[str, Any] | None) -> dict[str, Any]:  # what counts as a change: the entries
        return {k: v for k, v in (x or {}).items() if k not in ("updated_at", "source")}

    if prev and _same(prev) == _same(doc):
        changed = False
    else:
        changed = True
        n_new = len(doc["assets"]) - len((prev or {}).get("assets") or [])
        gh.put_file(catalog_path, branch, json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                    f"media: catalog {slug} ({len(doc['assets'])} assets, +{n_new})", blob)  # fmt: skip
    if pr is None and changed:
        pr = gh.create_pr(
            branch,
            base,
            f"media: archive catalog — {c['name']}",
            (
                f"Approved media from the **{c['name']}** campaign, archived by the brandLaunch agent's Campaign "
                f"Studio plugin.\n\n- Files: release `{TAG_PREFIX}{slug}` (release assets, not git).\n"
                f"- Catalog: `{catalog_path}` — validates against `schemas/media/catalog.schema.json`.\n\n"
                "This PR stays open and collects further approvals as new commits; merge it whenever. "
                "The next approval after a merge opens a fresh one. Data only — no package changes."
            ),
        )
    return {"pr": (pr or {}).get("html_url", ""), "changed": changed, "entries": len(doc["assets"])}


# ── the queue ─────────────────────────────────────────────────────────────────
def _backoff(attempts: int) -> float:
    return BACKOFF_S[min(max(attempts, 1), len(BACKOFF_S)) - 1]


def reconcile() -> list[int]:
    """Find what needs backing up — no hook in the review route required. Queues (once; a
    parked row stays parked) every APPROVED asset that has no archive record yet, or whose
    record doesn't list its CURRENT campaign's catalog (it was approved, moved or copied into a
    collection since). Cheap: it reads the approved rows, never a file."""
    queued = {q["asset_id"] for q in store.archive_queue()}
    added = []
    for a in store.list_assets(status="approved"):
        if a["id"] in queued:
            continue
        arch = (a.get("meta") or {}).get("archive") or {}
        if arch.get("url") and arch.get("sha256"):
            c = store.get_campaign(a["campaign_id"])
            if c is None or campaign_slug(c) in (arch.get("cataloged_in") or []):
                continue
        if store.enqueue_archive_new(a["id"], a["campaign_id"]):
            added.append(a["id"])
    return added


def backfill(campaign_id: int = 0) -> list[int]:
    """Queue every approved, non-superseded asset (``campaign_id`` 0 = every campaign). Already
    archived ones are cheap: the drain sees the matching sha256 and only re-checks the catalog."""
    ids = []
    for a in store.list_assets(campaign_id, status="approved"):
        store.enqueue_archive(a["id"], a["campaign_id"])
        ids.append(a["id"])
    return ids


_DRAIN_LOCK = threading.Lock()


def drain(cfg: dict[str, Any] | None, *, gh: GitHub | None = None, now: datetime | None = None,
          limit: int = BATCH) -> dict[str, Any]:  # fmt: skip
    """One pass over the due queue: upload what isn't archived yet, then commit each touched
    campaign's catalog ONCE. Returns what happened (never raises for a GitHub failure). Two
    drains never overlap — the second returns at once."""
    if not _DRAIN_LOCK.acquire(blocking=False):
        return {"skipped": "another archive pass is running"}
    try:
        if not problems(settings(cfg)):
            reconcile()
        return _drain(cfg, gh=gh, now=now, limit=limit)
    finally:
        _DRAIN_LOCK.release()


def _drain(cfg: dict[str, Any] | None, *, gh: GitHub | None, now: datetime | None, limit: int) -> dict[str, Any]:
    s = settings(cfg)
    why = problems(s)
    if why:
        return {"skipped": "; ".join(why)}
    now = now or datetime.now(UTC)
    stamp = now.isoformat(timespec="seconds")
    due = [q for q in store.archive_queue() if not q["next_at"] or q["next_at"] <= stamp][:limit]
    if not due:
        return {"uploaded": [], "cataloged": [], "errors": []}
    gh = gh or GitHub(s["token"], s["repo"])
    out: dict[str, Any] = {"uploaded": [], "already": [], "cataloged": [], "dropped": [], "errors": [], "prs": {}}

    def _fail(aid: int, q: dict[str, Any], err: str) -> None:
        n = int(q["attempts"]) + 1
        nxt = (
            "9999-12-31T00:00:00+00:00"
            if n >= PARK_AFTER
            else (now + timedelta(seconds=_backoff(n))).isoformat(timespec="seconds")
        )
        store.update_archive_queue(aid, attempts=n, last_error=_redact(err, s["token"])[:500], next_at=nxt)
        out["errors"].append({"asset_id": aid, "error": _redact(err, s["token"])})

    try:
        base = gh.default_branch()
    except Exception as e:  # noqa: BLE001 — never let a client error (and what it echoes) escape
        for q in due:
            _fail(q["asset_id"], q, str(e))
        return out

    touched: dict[int, list[dict[str, Any]]] = {}
    releases: dict[int, dict] = {}
    for q in due:
        aid = q["asset_id"]
        a = store.get_asset(aid)
        if a is None or a["status"] != "approved":
            store.dequeue_archive([aid])  # un-approved (or gone) since it was queued: nothing to back up
            out["dropped"].append(aid)
            continue
        c = store.require_campaign(a["campaign_id"])
        slug = campaign_slug(c)
        catalog_path = f"{s['catalog_dir']}/{slug}.json"

        def _release(c=c, slug=slug, catalog_path=catalog_path) -> dict:
            if c["id"] not in releases:
                releases[c["id"]] = ensure_release(gh, c, slug, base, catalog_path)
            return releases[c["id"]]

        try:
            rec, fresh = upload(gh, a, _release, now)
            if fresh:
                store.set_archive_meta(aid, rec)
                out["uploaded"].append(aid)
            else:
                out["already"].append(aid)
            store.update_archive_queue(aid, stage="catalog", campaign_id=c["id"])
            touched.setdefault(c["id"], []).append(q)
        except ArchiveError as e:
            _fail(aid, q, str(e))
        except OSError as e:
            _fail(aid, q, f"reading the file: {e}")
        except Exception as e:  # noqa: BLE001 — one asset never takes the pass (or the token) down
            _fail(aid, q, f"{type(e).__name__}: {e}")

    for cid, qs in touched.items():
        c = store.require_campaign(cid)
        slug = campaign_slug(c)
        try:
            res = commit_catalog(gh, c, slug, f"{s['catalog_dir']}/{slug}.json", base, now)
        except Exception as e:  # noqa: BLE001 — ArchiveError or anything else: redacted, retried
            for q in qs:
                _fail(q["asset_id"], q, f"catalog: {e}")
            continue
        for q in qs:
            a = store.get_asset(q["asset_id"])
            rec = dict((a.get("meta") or {}).get("archive") or {})
            rec["cataloged_in"] = sorted({*rec.get("cataloged_in", []), slug})
            rec["catalog_pr"] = res["pr"]
            store.set_archive_meta(a["id"], rec)
        store.dequeue_archive([q["asset_id"] for q in qs])
        out["cataloged"] += [q["asset_id"] for q in qs]
        out["prs"][slug] = res["pr"]
    if out["uploaded"] or out["errors"]:
        log.info(
            "[campaign] archive: %d uploaded, %d already archived, %d cataloged, %d error(s)",
            len(out["uploaded"]), len(out["already"]), len(out["cataloged"]), len(out["errors"]),
        )  # fmt: skip
    return out


def status() -> dict[str, Any]:
    q = store.archive_queue()
    archived = [
        a for a in store.list_assets(status="approved") if ((a.get("meta") or {}).get("archive") or {}).get("url")
    ]
    return {
        "queued": len(q),
        "waiting_retry": [
            {
                "asset_id": r["asset_id"],
                "attempts": r["attempts"],
                "next_at": r["next_at"],
                "last_error": r["last_error"],
            }
            for r in q
            if r["attempts"]
        ],  # fmt: skip
        "archived": len(archived),
        "approved": len(store.list_assets(status="approved")),
    }
