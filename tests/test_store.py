"""The plan store: campaigns, lanes, assets, milestones, decisions, scripts, and the gates."""

from __future__ import annotations

import sqlite3

import pytest
from campaign import paths, store


def _campaign(**kw):
    return store.create_campaign("Launch", product="Widget", goal="stars", **kw)


def test_campaign_round_trip_and_partial_update():
    c = _campaign(target_url="https://example.test")
    assert c["status"] == "draft" and c["target_url"] == "https://example.test"
    c2 = store.update_campaign(c["id"], status="active", goal_math="baseline UNKNOWN")
    assert c2["status"] == "active" and c2["goal_math"] == "baseline UNKNOWN"
    assert c2["product"] == "Widget", "untouched fields survive a partial update"
    with pytest.raises(ValueError):
        store.update_campaign(c["id"], status="shipped")
    with pytest.raises(ValueError):
        store.create_campaign("  ")


def test_archived_campaigns_are_hidden_by_default():
    a = _campaign()
    store.update_campaign(a["id"], status="archived")
    b = _campaign()
    assert [c["id"] for c in store.list_campaigns()] == [b["id"]]
    assert len(store.list_campaigns(include_archived=True)) == 2


def test_lanes_upsert_by_name_and_keep_unpassed_fields():
    c = _campaign()
    ln = store.upsert_lane(c["id"], "authors", pitch="install in a click", audience="plugin authors")
    again = store.upsert_lane(c["id"], "authors", hero_asset_id=7)
    assert again["id"] == ln["id"]
    assert again["pitch"] == "install in a click" and again["hero_asset_id"] == 7
    renamed = store.upsert_lane(c["id"], "builders", lane_id=ln["id"])
    assert renamed["name"] == "builders" and len(store.lanes(c["id"])) == 1


def test_asset_status_flow_refuses_agent_approval():
    c = _campaign()
    a = store.add_asset(c["id"], "copy_ref", "launch post", spec="social post #4")
    for st in ("scripted", "captured", "rendered", "ready_for_review"):
        a = store.update_asset(a["id"], status=st)
    assert a["status"] == "ready_for_review"
    for forbidden in ("approved", "rejected"):
        with pytest.raises(ValueError, match="only the operator"):
            store.update_asset(a["id"], status=forbidden)
    with pytest.raises(ValueError):
        store.add_asset(c["id"], "clip", "x", status="approved")


def test_ready_for_review_gate_needs_a_file_and_respects_hard_limits():
    c = _campaign()
    a = store.add_asset(c["id"], "gif", "demo", limit_id="github_attachment_image")
    with pytest.raises(ValueError, match="no file"):
        store.update_asset(a["id"], status="ready_for_review")
    big = paths.campaign_dir(c["id"], "Launch") / "big.gif"
    big.write_bytes(b"0" * 10_000_001)
    store.update_asset(a["id"], path=str(big))
    with pytest.raises(ValueError, match="over the 10.00 MB limit"):
        store.update_asset(a["id"], status="ready_for_review")
    big.write_bytes(b"0" * 9_000_000)
    assert store.update_asset(a["id"], status="ready_for_review")["status"] == "ready_for_review"


def test_operator_review_approves_and_rejects_with_a_note():
    c = _campaign()
    f = paths.campaign_dir(c["id"], "Launch") / "x.png"
    f.write_bytes(b"png")
    a = store.add_asset(c["id"], "still", "x", path=str(f), status="ready_for_review")
    r = store.review(a["id"], "reject", "text too small")
    assert r["status"] == "rejected" and r["review_note"] == "text too small" and r["reviewed_at"]
    assert store.review(a["id"], "approve")["status"] == "approved"
    with pytest.raises(ValueError):
        store.review(a["id"], "maybe")


def test_an_approved_asset_cannot_have_its_file_swapped():
    c = _campaign()
    f = paths.campaign_dir(c["id"], "Launch") / "x.png"
    f.write_bytes(b"png")
    a = store.add_asset(c["id"], "still", "x", path=str(f))
    store.review(a["id"], "approve")
    with pytest.raises(ValueError, match="approved"):
        store.update_asset(a["id"], path=str(f))
    # notes are fine
    assert store.update_asset(a["id"], notes="ships in README")["notes"] == "ships in README"


def test_unknown_limit_is_refused():
    c = _campaign()
    with pytest.raises(ValueError, match="unknown limit"):
        store.add_asset(c["id"], "clip", "x", limit_id="tiktok_vibes")


def test_milestones_and_decisions():
    c = _campaign()
    m = store.upsert_milestone(c["id"], "record takes", date="2026-10-03", owner="agent", workstream="capture")
    m = store.upsert_milestone(c["id"], milestone_id=m["id"], status="done")
    assert m["status"] == "done" and m["title"] == "record takes"
    with pytest.raises(ValueError):
        store.upsert_milestone(c["id"], "x", owner="intern")
    d = store.upsert_decision(c["id"], "Which lane leads?", options=["authors", "operators"], recommendation="authors")
    assert d["options"] == ["authors", "operators"] and d["answer"] == ""
    d = store.upsert_decision(c["id"], decision_id=d["id"], answer="authors")
    assert d["answer"] == "authors" and d["answered_at"]


def test_scripts_upsert_by_name():
    c = _campaign()
    s1 = store.save_script(c["id"], "hero", {"steps": [1]})
    s2 = store.save_script(c["id"], "hero", {"steps": [1, 2]})
    assert s1["id"] == s2["id"] and s2["body"] == {"steps": [1, 2]}
    assert store.get_script(s1["id"])["name"] == "hero"


def test_status_summary_splits_operator_from_agent():
    c = _campaign(launch_window="2026-10-10")
    f = paths.campaign_dir(c["id"], "Launch") / "x.png"
    f.write_bytes(b"png")
    store.add_asset(c["id"], "still", "for review", path=str(f), status="ready_for_review")
    store.add_asset(c["id"], "clip", "phone video", owner="operator")
    store.add_asset(c["id"], "clip", "browser take")
    rej = store.add_asset(c["id"], "still", "redo", path=str(f), status="ready_for_review")
    store.review(rej["id"], "reject", "blurry")
    store.upsert_decision(c["id"], "Launch date?", recommendation="Tue")
    store.upsert_milestone(c["id"], "approve renders", date="2000-01-01", owner="operator")
    s = store.status_summary(c["id"], today="2026-10-01")
    op = " | ".join(i["item"] + " " + i["why"] for i in s["on_operator"])
    ag = " | ".join(i["item"] + " " + i["why"] for i in s["on_agent"])
    assert "Launch date?" in op and "for review" in op and "phone video" in op and "OVERDUE" in op
    assert "browser take" in ag and "redo" in ag and "blurry" in ag
    assert s["progress"] == {"approved": 0, "total": 4}


def test_connection_uses_wal_and_a_busy_timeout():
    store.list_campaigns()
    conn = sqlite3.connect(store.db_path())
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()
    c = store.connect()
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    c.close()


def test_data_dir_resolution_order(tmp_path, monkeypatch):
    monkeypatch.delenv("CAMPAIGN_DIR")
    paths.configure("", str(tmp_path / "hoststore"))
    assert paths.data_dir() == tmp_path / "hoststore"
    paths.configure(str(tmp_path / "configured"), str(tmp_path / "hoststore"))
    assert paths.data_dir() == tmp_path / "configured"
    monkeypatch.setenv("CAMPAIGN_DIR", str(tmp_path / "env"))
    assert paths.data_dir() == tmp_path / "env"


def test_default_dir_is_instance_scoped(tmp_path, monkeypatch):
    monkeypatch.delenv("CAMPAIGN_DIR")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "dev")
    paths.configure("", "")
    assert paths.data_dir() == tmp_path / ".protoagent" / "campaign" / "dev"


def test_campaign_dir_is_stable_across_renames():
    c = _campaign()
    d1 = paths.campaign_dir(c["id"], "Launch")
    d2 = paths.campaign_dir(c["id"], "Renamed later")
    assert d1 == d2 and d1.parent == paths.media_root()
