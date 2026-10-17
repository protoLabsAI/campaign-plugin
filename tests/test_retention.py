"""Retention (retention.py): what the prune lets go, and — above all — what it never touches.

The invariants under test, in order of how much they matter:

1. An APPROVED asset is never deleted — not its row, not its file — by any rule or any race.
2. A READY-FOR-REVIEW asset is never deleted (stale ones are only flagged).
3. Nothing an approved or ready-for-review asset depends on is deleted (parent chain, montage
   inputs, a bookended clip's raw master, a take's stills/timing named in a keeper's meta).
4. Nothing outside the campaign media root is deleted; neither is a symlink or a directory.
5. The dry run touches nothing.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import campaign
import pytest
from campaign import paths, retention, store, surface
from fastapi import FastAPI
from fastapi.testclient import TestClient

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _age(asset_id: int, days: float) -> None:
    """Back-date a row's created/updated/reviewed_at by ``days`` before NOW."""
    ts = (NOW - timedelta(days=days)).isoformat(timespec="seconds")
    conn = store.connect()
    try:
        with conn:
            conn.execute(
                "UPDATE assets SET created = ?, updated = ?, reviewed_at = CASE WHEN reviewed_at = '' THEN '' ELSE ? END"
                " WHERE id = ?",
                (ts, ts, ts, asset_id),
            )
    finally:
        conn.close()


def _set(asset_id: int, **cols) -> None:
    conn = store.connect()
    try:
        with conn:
            for k, v in cols.items():
                conn.execute(f"UPDATE assets SET {k} = ? WHERE id = ?", (json.dumps(v) if k == "meta" else v, asset_id))
    finally:
        conn.close()


@pytest.fixture
def c():
    return store.create_campaign("Launch")


def _file(c, rel: str, data: bytes = b"x" * 1000):
    p = paths.campaign_dir(c["id"], c["name"]) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _asset(c, rel, status="rendered", kind="clip", days=10.0, parent_id=0, meta=None, data=b"x" * 1000):
    p = _file(c, rel, data) if rel else None
    a = store.add_asset(c["id"], kind, rel or "planned", path=str(p) if p else "", status="captured" if p else "planned",
                        parent_id=parent_id, meta=meta)  # fmt: skip
    if status in ("ready_for_review", "approved", "rejected"):
        store.update_asset(a["id"], status="ready_for_review")
        if status != "ready_for_review":
            store.review(a["id"], status, "ok")
    elif status not in ("captured", "planned"):
        store.update_asset(a["id"], status=status)
    _age(a["id"], days)
    return store.get_asset(a["id"])


def _ids(report):
    return {x["asset_id"] for x in report["actions"]}


# ── the rules ─────────────────────────────────────────────────────────────────
def test_old_intermediates_go_young_ones_stay(c):
    old = _asset(c, "renders/a/old.mp4", "rendered", days=8)
    young = _asset(c, "renders/b/young.mp4", "rendered", days=6)
    take = _asset(c, "shots/t/take.webm", "captured", days=7.5)
    r = retention.plan(NOW)
    assert _ids(r) == {old["id"], take["id"]}
    assert r["by_rule"]["intermediate"]["count"] == 2 and r["by_rule"]["intermediate"]["bytes"] == 2000
    assert young["id"] not in _ids(r)


def test_superseded_files_go_after_7_days_and_the_row_stays_as_a_tombstone(c):
    new = _asset(c, "renders/new/new.mp4", "ready_for_review", days=1)
    old = _asset(c, "renders/old/old.mp4", "rendered", days=20)
    store.update_asset(old["id"], status="superseded", superseded_by=[new["id"]])
    _age(old["id"], 7.1)
    fresh = _asset(c, "renders/f/f.mp4", "rendered", days=20)
    store.update_asset(fresh["id"], status="superseded", superseded_by=[new["id"]])
    _age(fresh["id"], 3)
    r = retention.execute(NOW)
    assert r["errors"] == [] and old["id"] in r["done"] and fresh["id"] not in r["done"]
    tomb = store.get_asset(old["id"])
    assert tomb is not None, "a superseded row stays so the gallery's chain still reads"
    assert tomb["status"] == "superseded" and tomb["superseded_by"] == [new["id"]] and tomb["path"] == ""
    assert tomb["meta"]["pruned"]["rule"] == "superseded_file" and tomb["meta"]["pruned"]["bytes"] == 1000
    assert not os.path.exists(old["path"]) and os.path.exists(fresh["path"])
    assert store.get_asset(new["id"])["path"] == new["path"] and os.path.exists(new["path"])
    assert store.is_retired(tomb)


def test_planned_rows_with_no_file_drop_after_30_days(c):
    stale = _asset(c, "", "planned", days=31)
    recent = _asset(c, "", "planned", days=29)
    r = retention.execute(NOW)
    assert stale["id"] in r["done"] and store.get_asset(stale["id"]) is None
    assert store.get_asset(recent["id"]) is not None


def test_ready_for_review_is_never_pruned_only_flagged_stale(c):
    rfr = _asset(c, "renders/r/r.mp4", "ready_for_review", days=40)
    fresh = _asset(c, "renders/s/s.mp4", "ready_for_review", days=13)
    r = retention.execute(NOW)
    assert rfr["id"] not in r["done"] and os.path.exists(rfr["path"])
    assert [s["asset_id"] for s in r["stale_review"]] == [rfr["id"]]
    assert fresh["id"] not in {s["asset_id"] for s in r["stale_review"]}


# ── invariant 1: approved is never deleted ────────────────────────────────────
def test_approved_assets_are_never_pruned_however_old(c):
    a = _asset(c, "renders/a/hero.mp4", "approved", days=400)
    r = retention.execute(NOW)
    assert a["id"] not in _ids(r) and os.path.exists(a["path"])
    assert store.get_asset(a["id"])["status"] == "approved"


@pytest.mark.parametrize("status", ["approved", "ready_for_review"])
def test_prune_asset_refuses_protected_rows_whatever_the_plan_says(c, status):
    a = _asset(c, "renders/a/x.mp4", status, days=400)
    for mode in ("tombstone", "delete"):
        with pytest.raises(ValueError, match="never pruned"):
            store.prune_asset(a["id"], expect_status=status, expect_path=a["path"], mode=mode, record={})
    assert store.get_asset(a["id"])["path"] == a["path"] and os.path.exists(a["path"])


def test_delete_assets_still_refuses_approved(c):
    a = _asset(c, "renders/a/x.mp4", "approved", days=1)
    r = _asset(c, "renders/b/y.mp4", "rendered", days=1)
    assert store.delete_assets([a["id"], r["id"]]) == 1
    assert store.get_asset(a["id"]) is not None and store.get_asset(r["id"]) is None


def test_an_approval_landing_after_the_plan_wins(c):
    """The plan saw a ready-for-review-able render; the operator approves before execute acts."""
    a = _asset(c, "renders/a/x.mp4", "rendered", days=10)
    plan = retention.plan(NOW)
    assert a["id"] in _ids(plan)
    store.update_asset(a["id"], status="ready_for_review")
    store.review(a["id"], "approve")
    x = next(x for x in plan["actions"] if x["asset_id"] == a["id"])
    with pytest.raises(ValueError, match="never pruned"):
        store.prune_asset(a["id"], expect_status=x["status"], expect_path=x["path"], mode=x["row"], record={})
    # and a fresh execute simply doesn't plan it
    r = retention.execute(NOW)
    assert a["id"] not in r["done"] and os.path.exists(a["path"])


def test_a_row_that_changed_since_the_plan_is_left_alone(c):
    a = _asset(c, "renders/a/x.mp4", "rendered", days=10)
    x = next(x for x in retention.plan(NOW)["actions"] if x["asset_id"] == a["id"])
    store.update_asset(a["id"], status="captured")
    with pytest.raises(ValueError, match="changed since"):
        store.prune_asset(a["id"], expect_status=x["status"], expect_path=x["path"], mode=x["row"], record={})
    assert os.path.exists(a["path"])


# ── invariant 3: what a keeper depends on stays ───────────────────────────────
@pytest.mark.parametrize("keeper", ["approved", "ready_for_review"])
def test_the_whole_parent_chain_of_a_keeper_stays(c, keeper):
    take = _asset(c, "shots/t/take.webm", "captured", days=30)
    render = _asset(c, "renders/t/r.mp4", "rendered", days=30, parent_id=take["id"])
    book = _asset(c, "renders/t/bookends/r-b.mp4", keeper, days=30, parent_id=render["id"])
    unrelated = _asset(c, "renders/u/u.mp4", "rendered", days=30)
    r = retention.execute(NOW)
    assert _ids(r) == {unrelated["id"]}
    for a in (take, render, book):
        assert store.get_asset(a["id"])["path"] == a["path"] and os.path.exists(a["path"])
    kept = {k["asset_id"]: k["why"] for k in r["kept"]}
    assert f"#{book['id']} ({keeper}) depends on it" in kept[render["id"]]
    assert f"#{book['id']}" in kept[take["id"]], "the grandparent is kept for the same keeper"


def test_a_fresh_intermediate_protects_its_old_take(c):
    take = _asset(c, "shots/t/take.webm", "captured", days=30)
    _asset(c, "renders/t/new.mp4", "rendered", days=1, parent_id=take["id"])
    assert take["id"] not in _ids(retention.plan(NOW))


def test_a_superseded_child_does_not_protect_its_parent_once_its_own_file_goes(c):
    take = _asset(c, "shots/t/take.webm", "captured", days=30)
    old = _asset(c, "renders/t/old.mp4", "rendered", days=30, parent_id=take["id"])
    store.update_asset(old["id"], status="superseded")
    _age(old["id"], 30)
    r = retention.execute(NOW)
    assert {take["id"], old["id"]} <= set(r["done"])
    # the take is named by the tombstone's parent_id → it stays as a tombstone, not a hole
    assert store.get_asset(take["id"])["meta"]["pruned"] and not os.path.exists(take["path"])


def test_montage_inputs_and_sequence_clips_are_dependencies(c):
    a = _asset(c, "renders/a/a.mp4", "rendered", days=30)
    b = _asset(c, "renders/b/b.mp4", "rendered", days=30)
    _asset(c, "montages/m/m.mp4", "approved", kind="montage", days=30, meta={"inputs": [a["id"]]})
    _asset(c, "montages/n/n.mp4", "ready_for_review", kind="montage", days=30,
           meta={"sequence": [{"card": {"title": "x"}}, {"clip": b["id"], "in": 1}]})  # fmt: skip
    assert not ({a["id"], b["id"]} & _ids(retention.plan(NOW)))


def test_the_raw_master_of_an_approved_bookended_clip_is_never_deleted(c):
    """produce/_bookend_render keeps the unbookended master as a sibling FILE (no row of its own)
    and names it in the bookended clip's meta. Should a row ever point at that file, it stays."""
    master = _file(c, "renders/t/B1-2k.mp4")
    stray = store.add_asset(c["id"], "clip", "same file, old row", path=str(master), status="captured")
    store.update_asset(stray["id"], status="rendered")
    _age(stray["id"], 30)
    book = _asset(c, "renders/t/bookends/B1-2k-bookends.mp4", "approved", days=30, meta={"unbookended": str(master)})
    r = retention.execute(NOW)
    assert stray["id"] not in r["done"] and master.exists()
    assert "also named by" in {k["asset_id"]: k["why"] for k in r["kept"]}[stray["id"]]
    assert os.path.exists(book["path"])


def test_a_take_kept_for_a_keeper_keeps_its_stills_files(c):
    take = _asset(c, "shots/t/take.webm", "captured", days=30)
    still = _asset(c, "shots/t/frame.png", "captured", kind="still", days=30, parent_id=take["id"])
    _set(take["id"], meta={"screenshots": {"frame": still["path"]}, "take_dir": os.path.dirname(take["path"])})
    _asset(c, "renders/t/r.mp4", "approved", days=30, parent_id=take["id"])
    r = retention.execute(NOW)
    assert still["id"] not in r["done"] and os.path.exists(still["path"])


def test_a_pruned_take_lets_its_stills_go_too(c):
    take = _asset(c, "shots/t/take.webm", "captured", days=30)
    still = _asset(c, "shots/t/frame.png", "captured", kind="still", days=30, parent_id=take["id"])
    _set(take["id"], meta={"screenshots": {"frame": still["path"]}})
    r = retention.execute(NOW)
    assert r["errors"] == [] and {take["id"], still["id"]} <= set(r["done"])
    assert store.get_asset(take["id"]) is None and store.get_asset(still["id"]) is None
    assert not os.path.exists(take["path"]) and not os.path.exists(still["path"])


def test_a_shared_file_is_kept_while_any_keeper_names_it(c):
    a = _asset(c, "renders/a/x.mp4", "rendered", days=30)
    store.add_asset(c["id"], "clip", "dup", path=a["path"], status="captured")  # young: a keeper
    r = retention.execute(NOW)
    assert a["id"] not in r["done"] and os.path.exists(a["path"])


def test_lane_hero_assets_are_kept(c):
    a = _asset(c, "renders/a/x.mp4", "rendered", days=30)
    store.upsert_lane(c["id"], "A", hero_asset_id=a["id"])
    assert a["id"] not in _ids(retention.plan(NOW))


def test_dependencies_cross_campaigns(c):
    other = store.create_campaign("Best of")
    take = _asset(c, "shots/t/take.webm", "captured", days=30)
    _asset(other, "renders/t/r.mp4", "approved", days=30, parent_id=take["id"])
    assert take["id"] not in _ids(retention.plan(NOW, campaign_id=c["id"]))


# ── row fate: delete vs tombstone ─────────────────────────────────────────────
def test_an_intermediate_named_by_a_surviving_row_becomes_a_tombstone(c):
    a = _asset(c, "renders/a/a.mp4", "rendered", days=30)
    b = _asset(c, "renders/b/b.mp4", "ready_for_review", days=1)
    # an old superseded take names `a` as its replacement → `a`'s row must outlive its file
    s = _asset(c, "renders/s/s.mp4", "rendered", days=30)
    store.update_asset(s["id"], status="superseded", superseded_by=[a["id"]])
    _age(s["id"], 2)  # young: keeps its file, and its chain names a
    r = retention.execute(NOW)
    assert a["id"] in r["done"]
    tomb = store.get_asset(a["id"])
    assert tomb is not None and tomb["meta"]["pruned"]["rule"] == "intermediate" and not os.path.exists(a["path"])
    assert store.get_asset(b["id"])["path"] == b["path"]


def test_a_video_job_naming_an_asset_keeps_its_row(c, monkeypatch):
    from campaign import produce

    a = _asset(c, "renders/a/a.mp4", "rendered", days=30)
    monkeypatch.setattr(produce, "list_jobs", lambda n=20: [{"id": "vid-0", "delivery": {"assets": {"gh": a["id"]}}}])
    (x,) = retention.plan(NOW)["actions"]
    assert x["asset_id"] == a["id"] and x["row"] == "tombstone"


# ── invariant 4: nothing outside the media root ───────────────────────────────
def test_files_outside_the_media_root_are_never_deleted(c, tmp_path):
    outside = tmp_path / "elsewhere" / "keep.mp4"
    outside.parent.mkdir()
    outside.write_bytes(b"precious")
    a = store.add_asset(c["id"], "clip", "outside", path=str(outside), status="captured")
    _age(a["id"], 30)
    r = retention.execute(NOW)
    assert a["id"] not in r["done"] and outside.read_bytes() == b"precious"
    assert "outside the campaign media root" in {k["asset_id"]: k["why"] for k in r["kept"]}[a["id"]]


def test_a_symlink_in_the_media_root_is_never_followed_or_deleted(c, tmp_path):
    target = tmp_path / "secret.mp4"
    target.write_bytes(b"precious")
    link = paths.campaign_dir(c["id"], c["name"]) / "renders" / "link.mp4"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    a = store.add_asset(c["id"], "clip", "link", path=str(link), status="captured")
    _age(a["id"], 30)
    r = retention.execute(NOW)
    assert a["id"] not in r["done"] and link.is_symlink() and target.read_bytes() == b"precious"


def test_a_directory_path_is_never_deleted(c):
    d = paths.campaign_dir(c["id"], c["name"]) / "shots" / "dir"
    d.mkdir(parents=True)
    (d / "inner.png").write_bytes(b"x")
    a = store.add_asset(c["id"], "clip", "dir", path=str(d), status="captured")
    _age(a["id"], 30)
    r = retention.execute(NOW)
    assert a["id"] not in r["done"] and (d / "inner.png").exists()


def test_emptied_folders_are_tidied_but_never_the_campaign_folder(c):
    a = _asset(c, "renders/take/stamp/a.mp4", "rendered", days=30)
    camp = paths.campaign_dir(c["id"], c["name"])
    retention.execute(NOW)
    assert not (camp / "renders" / "take").exists()
    assert camp.is_dir() and paths.media_root().is_dir()
    assert not os.path.exists(a["path"])


def test_a_missing_file_just_retires_the_row(c):
    a = _asset(c, "renders/a/a.mp4", "rendered", days=30)
    os.unlink(a["path"])
    r = retention.execute(NOW)
    assert a["id"] in r["done"] and r["errors"] == [] and store.get_asset(a["id"]) is None


# ── invariant 5: the dry run touches nothing ──────────────────────────────────
def test_the_dry_run_touches_nothing(c):
    a = _asset(c, "renders/a/a.mp4", "rendered", days=30)
    s = _asset(c, "renders/s/s.mp4", "rendered", days=30)
    store.update_asset(s["id"], status="superseded")
    _age(s["id"], 30)
    p = _asset(c, "", "planned", days=60)
    before = {x["id"]: x for x in store.list_assets()}
    r = retention.plan(NOW)
    assert _ids(r) == {a["id"], s["id"], p["id"]} and r["total"]["bytes"] == 2000
    assert {x["id"]: x for x in store.list_assets()} == before
    assert os.path.exists(a["path"]) and os.path.exists(s["path"])
    assert "dry run" in retention.summary({**r, "dry_run": True})


# ── modes, the schedule, the tool and the routes ──────────────────────────────
@pytest.mark.parametrize(("cfg", "want"), [({}, "report"), ({"retention_mode": "PRUNE"}, "prune"),
                                           ({"retention_mode": "disabled"}, "disabled"), ({"retention_mode": False}, "disabled"), ({"retention_mode": "yes"}, "report")])  # fmt: skip
def test_mode_fails_safe_to_report(cfg, want):
    assert retention.mode(cfg) == want


def test_report_mode_saves_a_report_and_deletes_nothing(c):
    a = _asset(c, "renders/a/a.mp4", "rendered", days=30)
    saved = retention.run_scheduled({}, NOW)
    assert saved["mode"] == "report" and saved["dry_run"] is True and saved["total"]["count"] == 1
    assert os.path.exists(a["path"]) and retention.last_report()["total"]["count"] == 1
    assert retention.run_scheduled({"retention_mode": "disabled"}, NOW) is None


def test_prune_mode_deletes_per_policy(c):
    a = _asset(c, "renders/a/a.mp4", "rendered", days=30)
    keep = _asset(c, "renders/k/k.mp4", "approved", days=30)
    saved = retention.run_scheduled({"retention_mode": "prune"}, NOW)
    assert saved["dry_run"] is False and saved["done"] == [a["id"]] and saved["freed_bytes"] == 1000
    assert not os.path.exists(a["path"]) and os.path.exists(keep["path"])


def test_the_surface_runs_daily_and_on_a_mode_change(c, monkeypatch):
    cfg = {"retention_mode": "report"}
    surface.configure(lambda: cfg)
    assert surface.tick(NOW) == {"retention": "report"}
    assert surface.tick(NOW + timedelta(hours=23)) == {}
    assert surface.tick(NOW + timedelta(hours=25)) == {"retention": "report"}
    cfg["retention_mode"] = "disabled"
    assert surface.tick(NOW + timedelta(hours=26)) == {}
    cfg["retention_mode"] = "prune"
    assert surface.tick(NOW + timedelta(hours=26)) == {"retention": "prune"}, "flipping the mode runs at once"


def test_a_failing_pass_never_kills_the_surface(monkeypatch):
    surface.configure(lambda: {})
    monkeypatch.setattr(retention, "run_scheduled", lambda *a, **k: 1 / 0)
    assert surface.tick(NOW) == {"retention": "error"}


def test_register_adds_the_library_surface_with_module_level_hooks(registry):
    calls = []
    registry.register_surface = lambda start, stop=None, name=None, reload=None: calls.append(
        (start, stop, name, reload)
    )
    campaign.register(registry)
    assert calls == [(surface.start, surface.stop, "campaign-library", surface.reload)]


@pytest.fixture
def tools(registry):
    campaign.register(registry)
    return registry


def test_the_tool_dry_runs_by_default_and_deletes_only_once_the_operator_flipped_the_mode(tools, c):
    a = _asset(c, "renders/a/a.mp4", "rendered", days=30)
    _age(a["id"], 3650)  # older than the tool's real clock, whatever day the suite runs
    prune = tools.tool("campaign_prune")
    out = prune.invoke({})
    assert "dry run" in out and os.path.exists(a["path"])
    out = prune.invoke({"dry_run": False})
    assert out.startswith("Not pruned — retention_mode is 'report'") and os.path.exists(a["path"])
    tools.config["retention_mode"] = "prune"
    out = prune.invoke({"dry_run": False})
    assert out.startswith("Pruned: 1 item(s)") and not os.path.exists(a["path"])
    assert "APPROVED" in prune.description


@pytest.fixture
def client(registry):
    campaign.register(registry)
    app = FastAPI()
    for prefix, router in registry.routers:
        app.include_router(router, prefix=prefix)
    return TestClient(app)


def test_retention_routes(client, c):
    a = _asset(c, "renders/a/a.mp4", "rendered", days=3650)
    got = client.get("/api/plugins/campaign/retention").json()
    assert got["last"] is None and [x["asset_id"] for x in got["plan"]["actions"]] == [a["id"]]
    dry = client.post("/api/plugins/campaign/retention/prune", json={}).json()
    assert dry["dry_run"] is True and os.path.exists(a["path"])
    real = client.post("/api/plugins/campaign/retention/prune", json={"dry_run": False}).json()
    assert real["done"] == [a["id"]] and not os.path.exists(a["path"])


def test_the_gallery_flags_stale_and_pruned_rows(client, c):
    rfr = _asset(c, "renders/r/r.mp4", "ready_for_review", days=3650)
    new = _asset(c, "renders/n/n.mp4", "ready_for_review", days=0)
    old = _asset(c, "renders/o/o.mp4", "rendered", days=3650)
    store.update_asset(old["id"], status="superseded", superseded_by=[new["id"]])
    _age(old["id"], 3650)
    client.post("/api/plugins/campaign/retention/prune", json={"dry_run": False})
    rows = {a["id"]: a for a in client.get(f"/api/plugins/campaign/campaigns/{c['id']}/assets").json()["assets"]}
    assert rows[rfr["id"]]["stale"] is True and rows[new["id"]]["stale"] is False
    assert rows[old["id"]]["pruned"] is True and rows[old["id"]]["has_file"] is False
    assert rows[old["id"]]["superseded_by"] == [new["id"]]
    (camp,) = client.get("/api/plugins/campaign/campaigns").json()["campaigns"]
    assert camp["superseded"] == 1 and camp["review"] == 2


def test_api_and_view_never_drop_the_manifest_setting():
    import yaml
    from pathlib import Path

    m = yaml.safe_load((Path(campaign.__file__).parent / "protoagent.plugin.yaml").read_text())
    assert m["config"]["retention_mode"] == "report", "report-only until Josh flips it"
    (row,) = [s for s in m["settings"] if s["key"] == "retention_mode"]
    assert row["options"] == ["report", "prune", "disabled"] and row.get("spawns") is True


def test_a_pruned_tombstone_serves_no_file(client, c):
    old = _asset(c, "renders/o/o.mp4", "rendered", days=3650)
    keep = _asset(c, "renders/k/k.mp4", "rendered", days=0)
    s = store.update_asset(old["id"], status="superseded", superseded_by=[keep["id"]])
    _age(s["id"], 3650)
    client.post("/api/plugins/campaign/retention/prune", json={"dry_run": False})
    assert client.get(f"/api/plugins/campaign/file/{old['id']}").status_code == 404
    assert client.get(f"/api/plugins/campaign/file/{keep['id']}").status_code == 200
    assert (
        client.get(f"/api/plugins/campaign/file/{old['id']}")
        .headers.get("content-type", "")
        .startswith("application/json")
    )
