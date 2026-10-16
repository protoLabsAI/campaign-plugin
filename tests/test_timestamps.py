"""When a clip was made: per-asset timestamps in the gallery API, and the gallery's time display (#27)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime

import campaign
import pytest
from campaign import paths, store, view
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def c():
    return store.create_campaign("Launch")


@pytest.fixture
def client(registry):
    campaign.register(registry)
    app = FastAPI()
    for prefix, router in registry.routers:
        app.include_router(router, prefix=prefix)
    return TestClient(app)


def _file(c, rel: str):
    p = paths.campaign_dir(c["id"], c["name"]) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def test_a_shot_folder_stamp_is_when_the_take_was_made(c):
    p = _file(c, "shots/hero-20261016-204318/take.webm")
    a = store.add_asset(c["id"], "clip", "hero", path=str(p), status="captured")
    t = store.asset_times(a)
    assert t["made_at"] == "2026-10-16T20:43:18+00:00" and t["made_from"] == "stamp"
    assert t["take_at"] == ""
    assert t["created_at"] == a["created"] and t["updated_at"] == a["updated"]


def test_a_render_carries_its_own_stamp_and_its_takes(c):
    # renders/<take folder>/<render stamp>[-2]/out.mp4 — the newest stamp is the render.
    p = _file(c, "renders/hero-20261016-204318/20261016-211502-2/out.mp4")
    a = store.add_asset(c["id"], "clip", "hero cut", path=str(p), status="rendered")
    t = store.asset_times(a)
    assert t["made_at"] == "2026-10-16T21:15:02+00:00" and t["made_from"] == "stamp"
    assert t["take_at"] == "2026-10-16T20:43:18+00:00"


def test_no_stamp_falls_back_to_the_files_mtime(c):
    p = _file(c, "stills/cover.png")
    when = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC).timestamp()
    os.utime(p, (when, when))
    a = store.add_asset(c["id"], "still", "cover", path=str(p), status="rendered")
    t = store.asset_times(a)
    assert t["made_at"] == "2026-10-01T12:00:00+00:00" and t["made_from"] == "mtime"


def test_no_file_means_no_made_time_but_the_row_times_still_show(c):
    a = store.add_asset(c["id"], "clip", "planned")
    t = store.asset_times(a)
    assert t["made_at"] == "" and t["made_from"] == "" and t["take_at"] == ""
    assert t["created_at"] and t["updated_at"]
    gone = store.add_asset(c["id"], "clip", "gone", path="/nowhere/at/all.mp4")
    assert store.asset_times(gone)["made_at"] == ""


def test_a_bad_stamp_is_ignored(c):
    p = _file(c, "shots/x-20261399-999999/take.webm")  # not a real date
    a = store.add_asset(c["id"], "clip", "x", path=str(p), status="captured")
    assert store.asset_times(a)["made_from"] == "mtime"


def test_the_gallery_api_exposes_the_times(client, c):
    p = _file(c, "renders/hero-20261016-204318/20261016-211502/out.mp4")
    a = store.add_asset(c["id"], "clip", "hero", path=str(p), status="rendered")
    planned = store.add_asset(c["id"], "clip", "later")
    data = client.get(f"/api/plugins/campaign/campaigns/{c['id']}/assets").json()
    by_id = {x["id"]: x for x in data["assets"]}
    got = by_id[a["id"]]
    assert got["created_at"] == a["created"] and got["updated_at"] == a["updated"]
    assert got["made_at"] == "2026-10-16T21:15:02+00:00" and got["take_at"] == "2026-10-16T20:43:18+00:00"
    assert by_id[planned["id"]]["made_at"] == "" and by_id[planned["id"]]["created_at"]
    # The raw path is still never exposed.
    assert not got["path"].startswith("/")


def test_the_gallery_shows_relative_times_and_sorts_newest_first():
    page = view.PAGE
    assert 'id="sort"' in page and 'sort: "newest"' in page
    assert '<option value="newest">Newest first</option>' in page
    assert 'class="when"' in page and "title=" in page and "whenTitle(a)" in page
    assert "Replaces " in page  # the retake chain reads both ways


def _time_helpers() -> str:
    page = view.PAGE
    start = page.index("const ts = (iso)")
    end = page.index("function whenTitle")
    return page[start:end]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_relative_time_formatting_in_the_page():
    now = 1_800_000_000_000  # a fixed "now", ms
    cases = {
        "a": now - 10_000,
        "b": now - 12 * 60_000,
        "c": now - 3 * 3_600_000,
        "d": now - 2 * 86_400_000,
        "e": now + 5_000,  # a clock a little ahead
    }
    script = (
        _time_helpers()
        + f"\nconst now = {now}; const cases = {json.dumps(cases)};\n"
        + "const out = {}; for (const [k, t] of Object.entries(cases)) out[k] = relTime(t, now);\n"
        + "out.later = relDelta(180000); out.earlier = relDelta(-7200000); out.same = relDelta(0);\n"
        + "out.nan = relTime(NaN, now);\n"
        + "console.log(JSON.stringify(out));"
    )
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True)
    out = json.loads(res.stdout)
    assert out == {
        "a": "just now",
        "b": "12 min ago",
        "c": "3 h ago",
        "d": "2 d ago",
        "e": "just now",
        "later": "3 min later",
        "earlier": "2 h earlier",
        "same": "same time",
        "nan": "",
    }
