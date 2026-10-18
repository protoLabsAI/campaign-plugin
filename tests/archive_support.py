"""Shared test support for the archive suites: an in-memory GitHub with the same surface as
``archive.GitHub``, a campaign factory and an approval helper. Plain module (no fixtures), so
each suite declares its own fixtures and nothing is imported from another test module."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from campaign import archive, paths, store

TOKEN = "github_pat_11AAAAAAA0123456789_abcdefghijklmnopqrstuvwxyzABCDEFGHIJ"
CFG = {"archive_enabled": True, "archive_token": TOKEN}
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


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
        self.on_put = None  # a hook: what happens right after a commit lands (a racing merge)

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
        if self.on_put:
            self.on_put(branch)
        return f"commit-{len(self.commits)}"

    def open_pr(self, branch):
        return next((p for p in self.prs if p["head"] == branch and p["state"] == "open"), None)

    def create_pr(self, branch, base, title, body):
        pr = {"number": len(self.prs) + 1, "head": branch, "base": base, "title": title, "body": body,
              "state": "open", "html_url": f"https://github.com/{self.repo}/pull/{len(self.prs) + 1}"}  # fmt: skip
        self.prs.append(pr)
        return pr

    def pr(self, number):
        return next((dict(p) for p in self.prs if p["number"] == number), None)

    # what Josh does
    def merge(self, pr):
        self.branches["main"].update(self.branches.pop(pr["head"]))
        pr["state"] = "merged"

    def close(self, pr):
        pr["state"] = "closed"


def new_campaign():
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
