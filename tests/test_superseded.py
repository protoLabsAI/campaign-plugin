"""The ``superseded`` status: retiring a take a retake replaced, without approving or rejecting it."""

from __future__ import annotations

import sqlite3

import campaign
import pytest
from campaign import api, montage, paths, store, view
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _take(c, title="take", status="rendered"):
    p = paths.campaign_dir(c["id"], c["name"]) / f"{title}.mp4"
    p.write_bytes(b"x")
    a = store.add_asset(c["id"], "clip", title, path=str(p), status="captured")
    if status in ("ready_for_review", "approved", "rejected"):
        store.update_asset(a["id"], status="ready_for_review")
        if status != "ready_for_review":
            store.review(a["id"], status, "note")
    elif status != "captured":
        store.update_asset(a["id"], status=status)
    return store.get_asset(a["id"])


@pytest.fixture
def c():
    return store.create_campaign("Launch")


@pytest.mark.parametrize("start", ["planned", "rendered", "ready_for_review", "rejected"])
def test_the_agent_supersedes_any_non_approved_asset(c, start):
    old = _take(c, "old", start) if start != "planned" else store.add_asset(c["id"], "clip", "old")
    new = _take(c, "new")
    a = store.update_asset(old["id"], status="superseded", superseded_by=[new["id"]], notes="retake: cursor jitter")
    assert a["status"] == "superseded" and a["superseded_by"] == [new["id"]]
    assert a["notes"] == "retake: cursor jitter"


def test_superseded_by_is_validated_and_only_goes_with_superseded(c):
    old, new = _take(c, "old"), _take(c, "new")
    other = store.create_campaign("Other")
    foreign = _take(other, "foreign")
    with pytest.raises(ValueError, match="itself"):
        store.update_asset(old["id"], status="superseded", superseded_by=[old["id"]])
    with pytest.raises(ValueError, match=f"no asset #{foreign['id']} in campaign"):
        store.update_asset(old["id"], status="superseded", superseded_by=[foreign["id"]])
    with pytest.raises(ValueError, match="only goes with"):
        store.update_asset(old["id"], status="rendered", superseded_by=[new["id"]])
    # No replacement named is fine; bringing it back clears superseded_by.
    store.update_asset(old["id"], status="superseded", superseded_by=[new["id"]])
    back = store.update_asset(old["id"], status="rendered")
    assert back["status"] == "rendered" and back["superseded_by"] == []


def test_the_agent_cannot_supersede_an_approved_asset_but_the_operator_can(c):
    approved, new = _take(c, "hero", "approved"), _take(c, "new")
    with pytest.raises(ValueError, match="only the operator can supersede an approved"):
        store.update_asset(approved["id"], status="superseded", superseded_by=[new["id"]])
    assert store.get_asset(approved["id"])["status"] == "approved"
    a = store.review(approved["id"], "supersede", "the new cut replaces it", superseded_by=[new["id"]])
    assert a["status"] == "superseded" and a["superseded_by"] == [new["id"]]


def test_the_agent_still_cannot_approve_or_reject(c):
    a = _take(c, "x", "ready_for_review")
    for forbidden in ("approved", "rejected"):
        with pytest.raises(ValueError, match="only the operator"):
            store.update_asset(a["id"], status=forbidden)
    with pytest.raises(ValueError):
        store.add_asset(c["id"], "clip", "y", status="superseded")


def test_the_review_queue_and_counts_exclude_superseded(c):
    old = _take(c, "old", "ready_for_review")
    new = _take(c, "new", "ready_for_review")
    _take(c, "done", "approved")
    store.update_asset(old["id"], status="superseded", superseded_by=[new["id"]])
    s = store.status_summary(c["id"])
    queue = " | ".join(i["item"] for i in s["on_operator"] + s["on_agent"])
    assert f"#{new['id']}" in queue and f"#{old['id']}" not in queue
    assert s["progress"] == {"approved": 1, "total": 2}
    assert "superseded" not in s["counts"] and s["superseded"] == 1
    assert s["counts"]["ready_for_review"] == 1


def test_montage_and_storyboard_refuse_a_superseded_beat_naming_its_replacement(c):
    old, new = _take(c, "old"), _take(c, "new")
    store.update_asset(old["id"], status="superseded", superseded_by=[new["id"]])
    items = montage.parse_sequence([{"clip": old["id"]}, {"clip": new["id"]}])
    with pytest.raises(montage.MontageError) as e:
        montage.resolve_items(c["id"], items, probe=lambda _p: {"width": 1920, "height": 1080, "duration_s": 8.0})
    probs = "\n".join(e.value.problems)
    assert f"clip #{old['id']}): is SUPERSEDED (replaced by #{new['id']}" in probs
    assert f"clip #{new['id']}" not in probs
    # storyboard resolves through the same gate, before any ffmpeg work.
    with pytest.raises(montage.MontageError, match="SUPERSEDED"):
        montage.storyboard(c["id"], [{"clip": old["id"]}], "", paths.campaign_dir(c["id"], "Launch") / "sb.png")


def test_a_superseded_beat_with_no_replacement_still_says_where_to_look(c):
    old = _take(c, "old")
    store.update_asset(old["id"], status="superseded")
    with pytest.raises(montage.MontageError, match="no replacement recorded"):
        montage.resolve_items(c["id"], montage.parse_sequence([{"clip": old["id"]}]), probe=lambda _p: {})


def test_old_databases_migrate_cleanly(tmp_path):
    """A v0.3.2 DB (no superseded_by column) opens, keeps its rows, and can supersede."""
    db = store.db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    old_schema = store._SCHEMA  # the v0.3.2 schema is this one minus the added column
    conn.executescript(old_schema)
    assert "superseded_by" not in {r[1] for r in conn.execute("PRAGMA table_info(assets)")}
    conn.execute("INSERT INTO campaigns (created, updated, name) VALUES ('t','t','Old')")
    for title, st in (("a", "rendered"), ("b", "approved"), ("c", "ready_for_review")):
        conn.execute(
            "INSERT INTO assets (campaign_id, created, updated, kind, title, status) VALUES (1,'t','t','clip',?,?)",
            (title, st),
        )
    conn.commit()
    conn.close()

    rows = store.list_assets(1)
    assert [(r["title"], r["status"], r["superseded_by"]) for r in rows] == [
        ("a", "rendered", []),
        ("b", "approved", []),
        ("c", "ready_for_review", []),
    ]
    a = store.update_asset(rows[0]["id"], status="superseded", superseded_by=[rows[2]["id"]])
    assert a["superseded_by"] == [rows[2]["id"]]
    assert store.status_summary(1)["progress"] == {"approved": 1, "total": 2}


# ── tools ─────────────────────────────────────────────────────────────────────
@pytest.fixture
def tools(registry):
    campaign.register(registry)
    return registry


def test_the_update_tool_supersedes_and_refuses_approved(tools, c):
    old, new, hero = _take(c, "old", "ready_for_review"), _take(c, "new"), _take(c, "hero", "approved")
    upd = tools.tool("campaign_asset_update")
    out = upd.invoke({"asset_id": old["id"], "status": "superseded", "superseded_by": [new["id"]], "notes": "retake"})
    assert out.startswith("Updated:") and "[superseded]" in out and f"replaced by #{new['id']}" in out
    out = upd.invoke({"asset_id": hero["id"], "status": "superseded", "superseded_by": [new["id"]]})
    assert out.startswith("Not updated") and "only the operator" in out
    assert "superseded" in upd.description and "superseded_by" in upd.description

    listed = tools.tool("campaign_assets").invoke({"campaign_id": c["id"]})
    assert f"#{old['id']}" not in listed and "1 superseded take(s) hidden" in listed
    listed = tools.tool("campaign_assets").invoke({"campaign_id": c["id"], "include_superseded": True})
    assert f"#{old['id']} [superseded]" in listed
    assert "1/2 assets approved" in tools.tool("campaign_list").invoke({})
    assert "(1 superseded, not counted)" in tools.tool("campaign_status").invoke({"campaign_id": c["id"]})


# ── the gallery ───────────────────────────────────────────────────────────────
@pytest.fixture
def client(registry):
    campaign.register(registry)
    app = FastAPI()
    for prefix, router in registry.routers:
        app.include_router(router, prefix=prefix)
    return TestClient(app)


def test_gallery_counts_exclude_superseded_and_the_operator_can_supersede_approved(client, c):
    old, new, hero = _take(c, "old", "ready_for_review"), _take(c, "new", "ready_for_review"), _take(c, "h", "approved")
    store.update_asset(old["id"], status="superseded", superseded_by=[new["id"]])
    (row,) = client.get("/api/plugins/campaign/campaigns").json()["campaigns"]
    assert row["assets"] == 2 and row["review"] == 1 and row["superseded"] == 1
    data = client.get(f"/api/plugins/campaign/campaigns/{c['id']}/assets").json()
    assert "superseded" in data["statuses"]
    by_id = {a["id"]: a for a in data["assets"]}
    assert by_id[old["id"]]["superseded_by"] == [new["id"]]

    r = client.post(
        f"/api/plugins/campaign/assets/{hero['id']}/review",
        json={"decision": "supersede", "note": "new cut", "superseded_by": [new["id"]]},
    )
    assert r.status_code == 200 and r.json()["asset"]["status"] == "superseded"
    r = client.post(f"/api/plugins/campaign/assets/{new['id']}/review", json={"decision": "ship"})
    assert r.status_code == 400 and "supersede" in r.json()["detail"]


def test_gallery_hides_superseded_by_default_with_a_toggle():
    page = view.PAGE
    assert 'id="show-superseded"' in page and "showSuperseded: false" in page
    assert 'state.showSuperseded || state.status === "superseded" || !retired(a)' in page
    assert 'decision: "supersede"' in page
    assert api  # the route module is the one the gallery talks to
