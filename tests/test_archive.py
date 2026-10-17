"""The protoContent archive (archive.py), against an in-memory GitHub.

What matters: an approval gets backed up exactly once (same sha256 = already archived), the
catalog is batched onto ONE open PR per campaign (never a PR per asset, never a merge), a
failure retries with backoff, and the token never shows up anywhere but the auth header."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

import campaign
import pytest
from campaign import archive, library, paths, store, surface
from fastapi import FastAPI
from fastapi.testclient import TestClient

TOKEN = "github_pat_11AAAAAAA0123456789_abcdefghijklmnopqrstuvwxyzABCDEFGHIJ"
CFG = {"archive_enabled": True, "archive_token": TOKEN}
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
SCHEMA = json.loads((Path(__file__).parent / "fixtures" / "media-catalog.schema.json").read_text())


# ── an in-memory GitHub with the same surface as archive.GitHub ───────────────
class FakeGitHub:
    def __init__(self, repo="protoLabsAI/protoContent"):
        self.repo = repo
        self.releases: dict[str, dict] = {}
        self.branches: dict[str, dict[str, str]] = {"main": {}}  # branch → {path: text}
        self.prs: list[dict] = []
        self.calls: list[tuple] = []
        self.fail_upload = 0
        self.commits: list[tuple[str, str]] = []
        self._n = 100

    def _id(self):
        self._n += 1
        return self._n

    def default_branch(self):
        return "main"

    def release_by_tag(self, tag):
        return self.releases.get(tag)

    def create_release(self, tag, name, body, target):
        self.calls.append(("create_release", tag, target))
        r = {"id": self._id(), "tag_name": tag, "name": name, "body": body, "assets": [], "make_latest": "false"}
        self.releases[tag] = r
        return r

    def _rel(self, release_id):
        return next(r for r in self.releases.values() if r["id"] == release_id)

    def release_assets(self, release_id):
        return list(self._rel(release_id)["assets"])

    def delete_asset(self, asset_id):
        self.calls.append(("delete_asset", asset_id))
        for r in self.releases.values():
            r["assets"] = [a for a in r["assets"] if a["id"] != asset_id]

    def upload_asset(self, release_id, name, path, content_type):
        self.calls.append(("upload", name))
        if self.fail_upload:
            self.fail_upload -= 1
            raise archive.ArchiveError(f"POST /repos/x/releases/{release_id}/assets → HTTP 502 Bad Gateway")
        r = self._rel(release_id)
        data = Path(path).read_bytes()
        a = {"id": self._id(), "name": name, "size": len(data), "sha": hashlib.sha256(data).hexdigest(),
             "url": f"https://api.github.com/repos/{self.repo}/releases/assets/{self._n}",
             "browser_download_url": f"https://github.com/{self.repo}/releases/download/{r['tag_name']}/{name}"}  # fmt: skip
        r["assets"].append(a)
        return a

    def branch_sha(self, branch):
        return f"sha-{branch}" if branch in self.branches else None

    def create_branch(self, branch, sha):
        self.calls.append(("create_branch", branch))
        self.branches[branch] = dict(self.branches["main"])

    def reset_branch(self, branch, sha):
        self.calls.append(("reset_branch", branch))
        self.branches[branch] = dict(self.branches["main"])

    def get_file(self, path, ref):
        text = self.branches[ref].get(path)
        return (json.loads(text), f"blob-{len(text)}") if text is not None else None

    def put_file(self, path, branch, text, message, sha=""):
        self.commits.append((branch, message))
        self.branches[branch][path] = text

    def open_pr(self, branch):
        return next((p for p in self.prs if p["head"] == branch and p["state"] == "open"), None)

    def create_pr(self, branch, base, title, body):
        pr = {"head": branch, "base": base, "title": title, "body": body, "state": "open",
              "html_url": f"https://github.com/{self.repo}/pull/{len(self.prs) + 1}"}  # fmt: skip
        self.prs.append(pr)
        return pr

    # what Josh does
    def merge(self, pr):
        self.branches["main"].update(self.branches.pop(pr["head"]))
        pr["state"] = "merged"

    def close(self, pr):
        pr["state"] = "closed"


@pytest.fixture
def gh():
    return FakeGitHub()


@pytest.fixture
def c():
    c = store.create_campaign("protoAgent — GitHub Trending launch (Oct 13)")
    store.upsert_lane(c["id"], "A · Claude Code")
    return c


def _approved(c, name="hero.mp4", data=None, note="ship it", lane="A · Claude Code"):
    p = paths.campaign_dir(c["id"], c["name"]) / "renders" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if data is not None else name.encode() * 50)
    ln = store.lane_by_name(c["id"], lane) if lane else None
    a = store.add_asset(c["id"], "clip", f"Title {name}", path=str(p), status="captured", size_bytes=p.stat().st_size,
                        width=1280, height=720, duration_s=12.5, lane_id=(ln or {}).get("id", 0))  # fmt: skip
    store.update_asset(a["id"], status="ready_for_review")
    return store.review(a["id"], "approve", note)


def _catalog(gh, branch, slug="protoagent-github-trending-launch-oct-13"):
    text = gh.branches[branch].get(f"specs/media/catalog/{slug}.json")
    return json.loads(text) if text else None


def _validate(doc, schema=SCHEMA, root=SCHEMA, at="$"):
    """A small draft-07 subset — enough to keep the writer and protoContent's schema in step."""
    if "$ref" in schema:
        schema = root["definitions"][schema["$ref"].split("/")[-1]]
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float)}
    if t:
        assert isinstance(doc, types[t]) and not isinstance(doc, bool), f"{at}: not {t}"
    if "const" in schema:
        assert doc == schema["const"], at
    if "enum" in schema:
        assert doc in schema["enum"], f"{at}: {doc!r} not in enum"
    if "pattern" in schema:
        assert re.search(schema["pattern"], doc), f"{at}: {doc!r} !~ {schema['pattern']}"
    if "minimum" in schema:
        assert doc >= schema["minimum"], at
    if t == "object":
        for k in schema.get("required", []):
            assert k in doc, f"{at}: missing {k}"
        if schema.get("additionalProperties") is False:
            extra = set(doc) - set(schema.get("properties", {}))
            assert not extra, f"{at}: unexpected {extra}"
        for k, sub in schema.get("properties", {}).items():
            if k in doc:
                _validate(doc[k], sub, root, f"{at}.{k}")
    if t == "array":
        for i, v in enumerate(doc):
            _validate(v, schema.get("items", {}), root, f"{at}[{i}]")


# ── settings ──────────────────────────────────────────────────────────────────
def test_nothing_runs_until_enabled_with_a_token(c, gh):
    _approved(c)
    assert "archive_enabled is off" in archive.drain({}, gh=gh)["skipped"]
    assert "no archive_token" in archive.drain({"archive_enabled": True}, gh=gh)["skipped"]
    assert "isn't owner/name" in archive.drain({**CFG, "archive_repo": "nope"}, gh=gh)["skipped"]
    assert "plain repo path" in archive.drain({**CFG, "archive_catalog_dir": "../x"}, gh=gh)["skipped"]
    assert gh.calls == [] and store.archive_queue() == [], "nothing is touched while it's off"
    assert archive.drain(CFG, gh=gh, now=NOW)["cataloged"], "turned on, the pass finds the approval itself"


# ── the happy path ────────────────────────────────────────────────────────────
def test_an_approval_is_uploaded_recorded_and_cataloged_on_a_pr(c, gh):
    a = _approved(c)
    out = archive.drain(CFG, gh=gh, now=NOW)
    assert out["uploaded"] == [a["id"]] and out["cataloged"] == [a["id"]] and out["errors"] == []
    slug = "protoagent-github-trending-launch-oct-13"
    rel = gh.releases[f"media/{slug}"]
    assert rel["make_latest"] == "false" and ("create_release", f"media/{slug}", "main") in gh.calls
    sha = hashlib.sha256(Path(a["path"]).read_bytes()).hexdigest()
    (up,) = rel["assets"]
    assert up["name"] == f"{sha[:12]}-hero.mp4" and up["sha"] == sha
    rec = store.get_asset(a["id"])["meta"]["archive"]
    assert rec["url"] == up["browser_download_url"] and rec["sha256"] == sha and rec["at"] == NOW.isoformat()
    assert rec["cataloged_in"] == [slug] and rec["catalog_pr"].endswith("/pull/1")
    (pr,) = gh.prs
    assert pr["head"] == f"media-catalog/{slug}" and pr["base"] == "main" and pr["state"] == "open"
    doc = _catalog(gh, pr["head"])
    _validate(doc)
    (e,) = doc["assets"]
    assert e == {
        "id": a["id"], "title": "Title hero.mp4", "lane": "A · Claude Code", "kind": "clip", "width": 1280,
        "height": 720, "duration_s": 12.5, "size_bytes": Path(a["path"]).stat().st_size, "sha256": sha,
        "file_name": "hero.mp4", "approved_at": a["reviewed_at"], "review_note": "ship it",
        "url": up["browser_download_url"], "release_tag": f"media/{slug}", "release_asset_name": up["name"],
        "archived_at": NOW.isoformat(), "source_campaign": {"id": c["id"], "name": c["name"], "slug": slug},
    }  # fmt: skip
    assert store.archive_queue() == []
    assert "main" in gh.branches and gh.branches["main"] == {}, "nothing ever lands on main without a merge"


def test_the_approved_row_is_otherwise_untouched(c, gh):
    a = _approved(c)
    archive.drain(CFG, gh=gh, now=NOW)
    after = store.get_asset(a["id"])
    assert {k: v for k, v in after.items() if k != "meta"} == {k: v for k, v in a.items() if k != "meta"}
    assert {k: v for k, v in after["meta"].items() if k != "archive"} == a["meta"]


# ── idempotency ───────────────────────────────────────────────────────────────
def test_same_sha256_is_already_archived(c, gh):
    a = _approved(c)
    archive.drain(CFG, gh=gh, now=NOW)
    commits = len(gh.commits)
    assert archive.backfill() == [a["id"]]
    out = archive.drain(CFG, gh=gh, now=NOW + timedelta(hours=1))
    assert out["uploaded"] == [] and out["already"] == [a["id"]]
    assert [x for x in gh.calls if x[0] == "upload"] == [
        ("upload", store.get_asset(a["id"])["meta"]["archive"]["asset_name"])
    ]
    assert len(gh.commits) == commits, "an unchanged catalog isn't re-committed"
    assert len(gh.prs) == 1


def test_a_same_named_asset_already_on_the_release_is_adopted_not_reuploaded(c, gh):
    a = _approved(c)
    data = Path(a["path"]).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    rel = gh.create_release("media/protoagent-github-trending-launch-oct-13", "x", "", "main")
    rel["assets"].append({"id": 7, "name": f"{sha[:12]}-hero.mp4", "size": len(data), "url": "u",
                          "browser_download_url": "https://github.com/x/dl/hero.mp4"})  # fmt: skip
    archive.drain(CFG, gh=gh, now=NOW)
    assert not [x for x in gh.calls if x[0] == "upload"]
    assert store.get_asset(a["id"])["meta"]["archive"]["url"] == "https://github.com/x/dl/hero.mp4"


def test_a_partial_upload_is_replaced(c, gh):
    a = _approved(c)
    sha = hashlib.sha256(Path(a["path"]).read_bytes()).hexdigest()
    rel = gh.create_release("media/protoagent-github-trending-launch-oct-13", "x", "", "main")
    rel["assets"].append({"id": 7, "name": f"{sha[:12]}-hero.mp4", "size": 3, "url": "u", "browser_download_url": "b"})
    archive.drain(CFG, gh=gh, now=NOW)
    assert ("delete_asset", 7) in gh.calls and [x[0] for x in gh.calls].count("upload") == 1


# ── batching: one PR per campaign, never one per asset ────────────────────────
def test_a_batch_is_one_commit_and_later_approvals_join_the_open_pr(c, gh):
    ids = [_approved(c, f"clip{i}.mp4")["id"] for i in range(3)]
    archive.drain(CFG, gh=gh, now=NOW)
    assert len(gh.prs) == 1 and len(gh.commits) == 1
    assert [e["id"] for e in _catalog(gh, gh.prs[0]["head"])["assets"]] == ids
    more = [_approved(c, f"late{i}.mp4")["id"] for i in range(2)]
    archive.drain(CFG, gh=gh, now=NOW + timedelta(minutes=5))
    assert len(gh.prs) == 1 and len(gh.commits) == 2, "the open PR collects the next batch"
    assert [e["id"] for e in _catalog(gh, gh.prs[0]["head"])["assets"]] == ids + more


def test_after_a_merge_the_next_approval_opens_a_fresh_pr_from_main(c, gh):
    first = _approved(c, "one.mp4")
    archive.drain(CFG, gh=gh, now=NOW)
    gh.merge(gh.prs[0])
    second = _approved(c, "two.mp4")
    archive.drain(CFG, gh=gh, now=NOW + timedelta(days=1))
    assert [p["state"] for p in gh.prs] == ["merged", "open"]
    assert [e["id"] for e in _catalog(gh, gh.prs[1]["head"])["assets"]] == [first["id"], second["id"]]


def test_a_pr_closed_unmerged_is_regenerated_in_full(c, gh):
    first = _approved(c, "one.mp4")
    archive.drain(CFG, gh=gh, now=NOW)
    gh.close(gh.prs[0])  # the branch is left behind with no open PR
    second = _approved(c, "two.mp4")
    archive.drain(CFG, gh=gh, now=NOW + timedelta(days=1))
    assert ("reset_branch", gh.prs[0]["head"]) in gh.calls and gh.prs[1]["state"] == "open"
    assert [e["id"] for e in _catalog(gh, gh.prs[1]["head"])["assets"]] == [first["id"], second["id"]]


def test_the_catalog_is_append_update_only(c, gh):
    slug = "protoagent-github-trending-launch-oct-13"
    foreign = {"id": 99999, "title": "from another instance", "sha256": "f" * 64}
    gh.branches["main"][f"specs/media/catalog/{slug}.json"] = json.dumps({"assets": [foreign]})
    a = _approved(c)
    archive.drain(CFG, gh=gh, now=NOW)
    doc = _catalog(gh, gh.prs[0]["head"])
    assert [e["id"] for e in doc["assets"]] == [a["id"], 99999] and doc["assets"][1] == foreign


def test_two_campaigns_get_two_releases_and_two_prs(c, gh):
    other = store.create_campaign("Fresh start")
    _approved(c)
    _approved(other, lane="")
    archive.drain(CFG, gh=gh, now=NOW)
    assert set(gh.releases) == {"media/protoagent-github-trending-launch-oct-13", "media/fresh-start"}
    assert sorted(p["head"] for p in gh.prs) == [
        "media-catalog/fresh-start",
        "media-catalog/protoagent-github-trending-launch-oct-13",
    ]


# ── retries ───────────────────────────────────────────────────────────────────
def test_a_failure_backs_off_and_retries(c, gh):
    a = _approved(c)
    gh.fail_upload = 1
    out = archive.drain(CFG, gh=gh, now=NOW)
    assert out["errors"] and "HTTP 502" in out["errors"][0]["error"]
    (q,) = store.archive_queue()
    assert q["attempts"] == 1 and q["next_at"] == (NOW + timedelta(seconds=60)).isoformat() and "502" in q["last_error"]
    assert archive.drain(CFG, gh=gh, now=NOW + timedelta(seconds=30))["uploaded"] == [], "not due yet"
    out = archive.drain(CFG, gh=gh, now=NOW + timedelta(seconds=61))
    assert out["uploaded"] == [a["id"]] and store.archive_queue() == []


def test_backoff_grows_and_caps():
    assert [archive._backoff(n) for n in (1, 2, 3, 4, 5, 9)] == [60, 300, 1800, 7200, 21600, 21600]


def test_a_catalog_failure_keeps_the_upload_and_retries_only_the_catalog(c, gh, monkeypatch):
    a = _approved(c)
    real = gh.put_file
    gh.put_file = lambda *a, **k: (_ for _ in ()).throw(archive.ArchiveError("PUT → HTTP 409 conflict"))
    out = archive.drain(CFG, gh=gh, now=NOW)
    assert out["uploaded"] == [a["id"]] and out["errors"][0]["error"].startswith("catalog:")
    assert store.get_asset(a["id"])["meta"]["archive"]["url"]
    gh.put_file = real
    out = archive.drain(CFG, gh=gh, now=NOW + timedelta(minutes=2))
    assert out["already"] == [a["id"]] and out["cataloged"] == [a["id"]]
    assert [x[0] for x in gh.calls].count("upload") == 1


def test_an_asset_unapproved_since_it_was_queued_is_dropped(c, gh):
    a = _approved(c)
    b = _approved(c, "b.mp4")
    archive.reconcile()  # both queued while approved…
    store.review(a["id"], "supersede", "replaced", superseded_by=[b["id"]])  # …then a is retired
    out = archive.drain(CFG, gh=gh, now=NOW)
    assert out["dropped"] == [a["id"]] and out["cataloged"] == [b["id"]]


def test_backfill_queues_every_approved_non_superseded_asset(c):
    a = _approved(c)
    b = _approved(c, "b.mp4")
    store.review(a["id"], "supersede", superseded_by=[b["id"]])
    store.dequeue_archive([a["id"], b["id"]])
    rfr = store.add_asset(c["id"], "clip", "not yet")
    assert archive.backfill() == [b["id"]]
    assert [q["asset_id"] for q in store.archive_queue()] == [b["id"]] and rfr


def test_a_file_outside_the_media_root_is_never_uploaded(c, gh, tmp_path):
    a = _approved(c)
    secret = tmp_path / "secret.mp4"
    secret.write_bytes(b"secret")
    conn = store.connect()
    with conn:
        conn.execute("UPDATE assets SET path = ? WHERE id = ?", (str(secret), a["id"]))
    conn.close()
    out = archive.drain(CFG, gh=gh, now=NOW)
    assert "outside the campaign media root" in out["errors"][0]["error"] and not gh.calls[1:]


# ── collections: a collected copy is cataloged, never re-uploaded ─────────────
def test_a_collected_approved_copy_is_cataloged_in_the_collection_without_reuploading(c, gh):
    a = _approved(c)
    archive.drain(CFG, gh=gh, now=NOW)
    best = store.create_campaign("Best of")
    out = library.copy_assets([a["id"]], best["id"], lineage=False, operator=True)
    cp = out["map"][str(a["id"])]
    assert archive.reconcile() == [cp], "the copy's record doesn't list the collection's catalog yet"
    res = archive.drain(CFG, gh=gh, now=NOW + timedelta(minutes=1))
    assert res["already"] == [cp] and [x[0] for x in gh.calls].count("upload") == 1
    assert "media/best-of" not in gh.releases, "no empty release for a collection of already-archived files"
    (e,) = _catalog(gh, "media-catalog/best-of", "best-of")["assets"]
    assert e["id"] == cp and e["release_tag"] == "media/protoagent-github-trending-launch-oct-13"
    assert e["source_campaign"]["slug"] == "best-of"
    assert sorted(store.get_asset(cp)["meta"]["archive"]["cataloged_in"]) == [
        "best-of",
        "protoagent-github-trending-launch-oct-13",
    ]


def test_the_slug_is_pinned_across_a_rename(c, gh):
    _approved(c)
    archive.drain(CFG, gh=gh, now=NOW)
    store.update_campaign(c["id"], name="Renamed")
    _approved(c, "two.mp4")
    archive.drain(CFG, gh=gh, now=NOW + timedelta(minutes=1))
    assert list(gh.releases) == ["media/protoagent-github-trending-launch-oct-13"] and len(gh.prs) == 1


def test_slugs_fit_protocontents_pattern():
    long = store.create_campaign("—" * 3 + "A very long campaign name " * 6 + "!!")
    s = archive.campaign_slug(long)
    assert archive.SLUG_RE.fullmatch(s) and len(s) <= 64
    dup = store.create_campaign(long["name"])
    assert archive.campaign_slug(dup) != s and archive.SLUG_RE.fullmatch(archive.campaign_slug(dup))
    assert archive.asset_name("/x/My Clip (final).MP4", "a" * 64) == "aaaaaaaaaaaa-My-Clip-final.mp4"


# ── the HTTP layer and the token ──────────────────────────────────────────────
class _Resp(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode() if not isinstance(body, bytes) else body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_client_sends_the_token_only_as_a_header_to_github():
    seen = []

    def opener(req, timeout):
        seen.append(req)
        return _Resp(200, {"default_branch": "main"})

    g = archive.GitHub(TOKEN, "protoLabsAI/protoContent", opener=opener)
    assert g.default_branch() == "main"
    (req,) = seen
    assert req.full_url == "https://api.github.com/repos/protoLabsAI/protoContent"
    assert req.get_header("Authorization") == f"Bearer {TOKEN}" and TOKEN not in req.full_url
    with pytest.raises(archive.ArchiveError, match="other than GitHub"):
        g._req("GET", "https://evil.example/steal")


def test_errors_never_carry_the_token(caplog, c):
    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Bad credentials", {}, io.BytesIO(
            json.dumps({"message": f"Bad credentials for {TOKEN}"}).encode()))  # fmt: skip

    g = archive.GitHub(TOKEN, "protoLabsAI/protoContent", opener=opener)
    with pytest.raises(archive.ArchiveError) as ei:
        g.release_by_tag("media/x")
    assert TOKEN not in str(ei.value) and "HTTP 401" in str(ei.value)

    _approved(c)
    caplog.set_level(logging.DEBUG)
    out = archive.drain(CFG, gh=g, now=NOW)
    blob = json.dumps(out) + caplog.text + json.dumps(store.archive_queue())
    assert out["errors"] and TOKEN not in blob
    assert archive._redact("token ghp_" + "a" * 36, "") == "token ***"


def test_a_404_is_none_and_a_network_error_is_an_archive_error():
    def opener(req, timeout):
        if "tags" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b"{}"))
        raise urllib.error.URLError("timed out")

    g = archive.GitHub(TOKEN, "o/r", opener=opener)
    assert g.release_by_tag("media/x") is None
    with pytest.raises(archive.ArchiveError, match="timed out"):
        g.default_branch()


def test_release_creation_never_marks_latest():
    sent = []

    def opener(req, timeout):
        sent.append(json.loads(req.data))
        return _Resp(201, {"id": 1, "tag_name": "media/x"})

    archive.GitHub(TOKEN, "o/r", opener=opener).create_release("media/x", "Media — X", "body", "main")
    assert sent == [{"tag_name": "media/x", "target_commitish": "main", "name": "Media — X", "body": "body",
                     "draft": False, "prerelease": False, "make_latest": "false"}]  # fmt: skip


def test_an_upload_streams_the_file_with_its_length(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x" * 1234)
    got = {}

    def opener(req, timeout):
        got.update(url=req.full_url, len=req.get_header("Content-length"), type=req.get_header("Content-type"),
                   body=req.data.read())  # fmt: skip
        return _Resp(201, {"id": 5, "name": "n"})

    archive.GitHub(TOKEN, "o/r", opener=opener).upload_asset(9, "abc-a.mp4", f, "video/mp4")
    assert got == {"url": "https://uploads.github.com/repos/o/r/releases/9/assets?name=abc-a.mp4", "len": "1234",
                   "type": "video/mp4", "body": b"x" * 1234}  # fmt: skip


# ── wiring ────────────────────────────────────────────────────────────────────
@pytest.fixture
def client(registry):
    campaign.register(registry)
    app = FastAPI()
    for prefix, router in registry.routers:
        app.include_router(router, prefix=prefix)
    return TestClient(app)


def test_the_archive_routes(client, c):
    p = paths.campaign_dir(c["id"], c["name"]) / "a.mp4"
    p.write_bytes(b"x")
    a = store.add_asset(c["id"], "clip", "a", path=str(p), status="captured")
    store.update_asset(a["id"], status="ready_for_review")
    client.post(f"/api/plugins/campaign/assets/{a['id']}/review", json={"decision": "approve"})
    st = client.get("/api/plugins/campaign/archive").json()
    assert st == {"queued": 0, "waiting_retry": [], "archived": 0, "approved": 1}
    assert client.post("/api/plugins/campaign/archive/backfill", json={}).json() == {"queued": [a["id"]]}
    assert "archive_enabled is off" in client.post("/api/plugins/campaign/archive/drain").json()["skipped"]


def test_reconcile_finds_approvals_without_any_hook_and_never_unparks(c, gh):
    a = _approved(c, "a.mp4")
    b = _approved(c, "b.mp4")
    assert sorted(archive.reconcile()) == sorted([a["id"], b["id"]]) and archive.reconcile() == []
    store.update_archive_queue(a["id"], attempts=10, next_at="9999-12-31T00:00:00+00:00")
    archive.reconcile()
    assert next(q for q in store.archive_queue() if q["asset_id"] == a["id"])["attempts"] == 10
    os.unlink(a["path"])
    archive.drain(CFG, gh=gh, now=NOW)  # b archives; a stays parked
    assert archive.reconcile() == [] and [q["asset_id"] for q in store.archive_queue()] == [a["id"]]
    rfr = store.add_asset(c["id"], "clip", "not approved")
    assert rfr["id"] not in archive.reconcile()


def test_the_surface_drains_while_enabled(c, gh, monkeypatch):
    _approved(c)
    calls = []
    monkeypatch.setattr(archive, "drain", lambda cfg, now=None, **k: calls.append(cfg) or {"uploaded": []})
    surface.configure(lambda: {"retention_mode": "disabled"})
    assert "archive" not in surface.tick(NOW)
    surface.configure(lambda: {**CFG, "retention_mode": "disabled"})
    assert surface.tick(NOW) == {"archive": {"uploaded": []}} and len(calls) == 1


def test_a_pass_with_nothing_due_makes_no_github_call(c, gh):
    assert archive.drain(CFG, gh=gh, now=NOW) == {"uploaded": [], "cataloged": [], "errors": []}
    assert gh.calls == []


def test_the_backup_tool_reports_and_backfills(registry, c):
    campaign.register(registry)
    a = _approved(c)
    store.dequeue_archive([a["id"]])
    t = registry.tool("campaign_backup")
    out = t.invoke({})
    assert "Archived: 0 of 1" in out and "0 queued" in out and "archive_enabled is off" in out
    out = t.invoke({"backfill": True})
    assert "Queued 1 approved" in out and "1 queued" in out
    registry.config.update(CFG)
    assert "Running: every minute → protoLabsAI/protoContent" in t.invoke({})
    assert TOKEN not in t.invoke({})


# ── adversarial-review regressions ────────────────────────────────────────────
def test_a_malformed_token_is_refused_before_any_request(c, gh, caplog):
    _approved(c)
    bad = TOKEN[:20] + "\n" + TOKEN[20:]
    caplog.set_level(logging.DEBUG)
    out = archive.drain({**CFG, "archive_token": bad}, gh=gh, now=NOW)
    assert "isn't a GitHub token" in out["skipped"] and gh.calls == []
    assert TOKEN[20:] not in caplog.text and TOKEN[20:] not in json.dumps(out)


def test_any_client_exception_is_redacted_and_retried(c, caplog):
    def opener(req, timeout):
        raise ValueError(f"Invalid header value b'Bearer {TOKEN}'")  # what http.client says for a bad header

    g = archive.GitHub(TOKEN, "o/r", opener=opener)
    with pytest.raises(archive.ArchiveError) as ei:
        g.default_branch()
    assert TOKEN not in str(ei.value) and "ValueError" in str(ei.value)
    _approved(c)
    caplog.set_level(logging.DEBUG)
    out = archive.drain(CFG, gh=g, now=NOW)
    assert out["errors"] and store.archive_queue()[0]["attempts"] == 1
    assert TOKEN not in caplog.text + json.dumps(out) + json.dumps(store.archive_queue())


def test_an_unexpected_error_on_one_asset_never_stops_the_pass(c, gh):
    a = _approved(c, "a.mp4")
    b = _approved(c, "b.mp4")
    real = gh.upload_asset

    def flaky(release_id, name, path, ctype):
        if "a.mp4" in name:
            raise KeyError(f"boom {TOKEN}")
        return real(release_id, name, path, ctype)

    gh.upload_asset = flaky
    out = archive.drain(CFG, gh=gh, now=NOW)
    assert out["uploaded"] == [b["id"]] and [e["asset_id"] for e in out["errors"]] == [a["id"]]
    assert TOKEN not in json.dumps(out) and TOKEN not in store.archive_queue()[0]["last_error"]


def test_a_hopeless_asset_is_parked_not_retried_forever(c, gh):
    a = _approved(c)
    os.unlink(a["path"])
    t = NOW
    for _ in range(archive.PARK_AFTER):
        archive.drain(CFG, gh=gh, now=t)
        t += timedelta(days=1)
    (q,) = store.archive_queue()
    assert q["attempts"] == archive.PARK_AFTER and q["next_at"].startswith("9999")
    assert archive.drain(CFG, gh=gh, now=t)["uploaded"] == []
    archive.backfill()  # the operator's backfill un-parks it
    assert store.archive_queue()[0]["next_at"] == ""


def test_catalog_entries_from_two_instances_with_the_same_ids_never_collide(c, gh):
    slug = "protoagent-github-trending-launch-oct-13"
    a = _approved(c)
    theirs = {"id": a["id"], "title": "the dev instance's asset with the SAME local id", "sha256": "e" * 64}
    gh.branches["main"][f"specs/media/catalog/{slug}.json"] = json.dumps({"assets": [theirs]})
    archive.drain(CFG, gh=gh, now=NOW)
    doc = _catalog(gh, gh.prs[0]["head"])
    assert {e["sha256"] for e in doc["assets"]} == {"e" * 64, store.get_asset(a["id"])["meta"]["archive"]["sha256"]}


def test_a_plugin_version_bump_alone_is_not_a_catalog_change(c, gh, monkeypatch):
    _approved(c)
    archive.drain(CFG, gh=gh, now=NOW)
    gh.merge(gh.prs[0])
    monkeypatch.setattr(campaign, "__version__", "9.9.9")
    archive.backfill()
    archive.drain(CFG, gh=gh, now=NOW + timedelta(days=1))
    assert len(gh.prs) == 1 and len(gh.commits) == 1


def test_a_catalog_over_1mb_is_read_through_the_blob_api():
    import base64

    doc = {"assets": [{"id": 1, "sha256": "a" * 64}]}
    calls = []

    def opener(req, timeout):
        calls.append(req.full_url)
        if "/contents/" in req.full_url:
            return _Resp(200, {"content": "", "encoding": "none", "size": 2_000_000, "sha": "blobsha"})
        return _Resp(200, {"content": base64.b64encode(json.dumps(doc).encode()).decode()})

    got = archive.GitHub(TOKEN, "o/r", opener=opener).get_file("specs/media/catalog/x.json", "b")
    assert got == (doc, "blobsha") and calls[-1].endswith("/git/blobs/blobsha")


@pytest.mark.parametrize(("method", "path", "fn"), [("get", "/archive", "status"), ("post", "/archive/backfill", "backfill"),
                                                    ("post", "/archive/drain", "drain")])  # fmt: skip
def test_the_archive_routes_fail_plainly(client, monkeypatch, method, path, fn):
    monkeypatch.setattr(archive, fn, lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"secret {TOKEN}")))
    kw = {"json": {}} if method == "post" else {}
    r = getattr(client, method)(f"/api/plugins/campaign{path}", **kw)
    assert r.status_code == 500 and TOKEN not in r.text and "RuntimeError" not in r.text and "agent log" in r.text
