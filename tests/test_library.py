"""Collections (library.py): move/copy assets with their lineage, and archiving a campaign.

What matters most: lineage comes along (so retention keeps a collected render's take), files
are copied (not shared) on a copy, approval stays the operator's, and nothing about the source
campaign's approved assets changes."""

from __future__ import annotations

import os
from pathlib import Path

import campaign
import pytest
from campaign import library, paths, retention, store
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def src():
    c = store.create_campaign("Launch Oct 13")
    store.upsert_lane(c["id"], "A · Hero", pitch="the pitch")
    return c


@pytest.fixture
def best():
    return store.create_campaign("Best of — launch")


def _file(c, rel, data=b"x" * 100):
    p = paths.campaign_dir(c["id"], c["name"]) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _row(c, rel, status="rendered", kind="clip", parent_id=0, meta=None, lane=0):
    p = _file(c, rel) if rel else None
    a = store.add_asset(c["id"], kind, rel, path=str(p or ""), status="captured" if p else "planned",
                        parent_id=parent_id, meta=meta, lane_id=lane)  # fmt: skip
    if status in ("ready_for_review", "approved", "rejected"):
        store.update_asset(a["id"], status="ready_for_review")
        if status != "ready_for_review":
            store.review(a["id"], status, "lovely")
    elif status not in ("captured", "planned"):
        store.update_asset(a["id"], status=status)
    return store.get_asset(a["id"])


def _chain(c):
    lane = store.lane_by_name(c["id"], "A · Hero")["id"]
    take = _row(c, "shots/t-20261002-010000/take.webm", "captured", lane=lane)
    still = _row(c, "shots/t-20261002-010000/frame.png", "captured", kind="still", parent_id=take["id"])
    store.update_asset(take["id"], meta={"screenshots": {"frame": still["path"]},
                                         "take_dir": str(Path(take["path"]).parent)})  # fmt: skip
    render = _row(c, "renders/t/20261002-010100/cut.mp4", "rendered", parent_id=take["id"], lane=lane)
    master = _file(c, "renders/t/20261002-010100/cut-master.mp4")
    hero = _row(c, "renders/t/20261002-010100/bookends/cut-b.mp4", "approved", parent_id=render["id"],
                meta={"unbookended": str(master)}, lane=lane)  # fmt: skip
    return take, still, render, hero, master


# ── plan ──────────────────────────────────────────────────────────────────────
def test_the_plan_brings_the_lineage_and_touches_nothing(src, best):
    take, still, render, hero, _ = _chain(src)
    p = library.plan([hero["id"]], best["id"], mode="copy")
    assert [x["asset_id"] for x in p["items"]] == [take["id"], render["id"], hero["id"]]
    assert [x["selected"] for x in p["items"]] == [False, False, True]
    assert p["bytes"] == 300 and store.list_assets(best["id"]) == []
    assert [x["asset_id"] for x in library.plan([hero["id"]], best["id"], lineage=False, mode="copy")["items"]] == [
        hero["id"]
    ]


def test_montage_inputs_are_lineage(src, best):
    a = _row(src, "renders/a.mp4")
    m = _row(src, "montages/m.mp4", "ready_for_review", kind="montage", meta={"inputs": [a["id"]]})
    assert {x["asset_id"] for x in library.plan([m["id"]], best["id"])["items"]} == {a["id"], m["id"]}


@pytest.mark.parametrize("bad", [{"asset_ids": []}, {"asset_ids": [999]}, {"mode": "steal"}])
def test_bad_requests_are_refused(src, best, bad):
    a = _row(src, "renders/a.mp4")
    kw = {"asset_ids": [a["id"]], "mode": "move", **bad}
    with pytest.raises(ValueError):
        library.run(kw["asset_ids"], best["id"], mode=kw["mode"])
    with pytest.raises(ValueError, match="no campaign"):
        library.plan([a["id"]], 999)


# ── move ──────────────────────────────────────────────────────────────────────
def test_the_agent_cannot_move_an_approved_asset_the_operator_can(src, best):
    take, still, render, hero, _ = _chain(src)
    with pytest.raises(ValueError, match="move only by the operator"):
        library.move_assets([hero["id"]], best["id"])
    assert store.get_asset(hero["id"])["campaign_id"] == src["id"]
    with pytest.raises(ValueError, match="only the operator moves"):
        store.move_rows({hero["id"]: 0}, best["id"])
    out = library.move_assets([hero["id"]], best["id"], operator=True)
    assert out["done"] == [take["id"], render["id"], hero["id"]]
    moved = store.get_asset(hero["id"])
    assert moved["campaign_id"] == best["id"] and moved["status"] == "approved" and moved["review_note"] == "lovely"
    assert moved["path"] == hero["path"] and os.path.exists(moved["path"]), "a move keeps the file where it is"
    assert store.get_asset(render["id"])["campaign_id"] == best["id"]
    assert store.get_asset(still["id"])["campaign_id"] == src["id"], "a sibling still isn't lineage"
    lane = store.lane_by_name(best["id"], "A · Hero")
    assert lane and moved["lane_id"] == lane["id"] and lane["pitch"] == "the pitch"


def test_the_agent_moves_non_approved_assets(src, best):
    a = _row(src, "renders/a.mp4", "ready_for_review")
    out = library.move_assets([a["id"]], best["id"])
    assert out["done"] == [a["id"]] and store.get_asset(a["id"])["campaign_id"] == best["id"]
    assert library.move_assets([a["id"]], best["id"])["done"] == [], "already there: nothing to do"


def test_after_a_move_retention_still_protects_the_lineage(src, best):
    take, still, render, hero, master = _chain(src)
    library.move_assets([hero["id"]], best["id"], operator=True)
    from datetime import UTC, datetime, timedelta

    acts = {x["asset_id"] for x in retention.plan(datetime.now(UTC) + timedelta(days=60))["actions"]}
    assert not acts & {take["id"], render["id"], hero["id"]}
    assert still["id"] not in acts, "the kept take's meta names the still's file"


# ── copy ──────────────────────────────────────────────────────────────────────
def test_an_operator_copy_duplicates_files_and_repoints_the_lineage(src, best):
    take, still, render, hero, master = _chain(src)
    out = library.copy_assets([hero["id"]], best["id"], operator=True)
    new = {int(k): v for k, v in out["map"].items()}
    c_take, c_render, c_hero = (store.get_asset(new[i]) for i in (take["id"], render["id"], hero["id"]))
    best_dir = paths.campaign_dir(best["id"], best["name"])
    for orig, cp in ((take, c_take), (render, c_render), (hero, c_hero)):
        assert cp["campaign_id"] == best["id"] and cp["path"] != orig["path"]
        assert Path(cp["path"]).is_relative_to(best_dir) and Path(cp["path"]).read_bytes() == b"x" * 100
        assert os.path.exists(orig["path"]), "the source keeps its file"
        assert cp["meta"]["copied_from"]["asset_id"] == orig["id"]
    assert c_hero["status"] == "approved" and c_hero["review_note"] == "lovely"
    assert c_render["parent_id"] == c_take["id"] and c_hero["parent_id"] == c_render["id"]
    # side files: the raw master and the take's stills/take_dir follow the copy
    assert Path(c_hero["meta"]["unbookended"]).is_relative_to(best_dir) and Path(c_hero["meta"]["unbookended"]).exists()
    assert Path(c_take["meta"]["screenshots"]["frame"]).is_relative_to(best_dir)
    assert c_take["meta"]["take_dir"] == str(Path(c_take["path"]).parent)
    # the source is untouched
    assert store.get_asset(hero["id"]) == hero


def test_the_agents_copy_of_an_approved_asset_lands_for_review(src, best):
    _, _, _, hero, _ = _chain(src)
    out = library.copy_assets([hero["id"]], best["id"], lineage=False)
    cp = store.get_asset(out["map"][str(hero["id"])])
    assert cp["status"] == "ready_for_review" and cp["review_note"] == "" and cp["reviewed_at"] == ""
    assert "approved there — landed ready_for_review here" in cp["notes"]
    assert store.get_asset(hero["id"])["status"] == "approved"
    with pytest.raises(ValueError, match="only the operator lands"):
        store.clone_asset({**hero}, best["id"], 0)


def test_the_agent_cannot_launder_a_rejected_asset_into_review(src, best):
    r = _row(src, "renders/r.mp4", "rejected")
    out = library.copy_assets([r["id"]], best["id"])
    assert store.get_asset(out["map"][str(r["id"])])["status"] == "rendered"
    assert store.get_asset(r["id"])["status"] == "rejected"


def test_a_copy_that_cant_bring_its_file_never_waits_for_review(src, best, tmp_path):
    outside = tmp_path / "o.mp4"
    outside.write_bytes(b"x")
    a = store.add_asset(src["id"], "clip", "o", path=str(outside), status="captured")
    conn = store.connect()
    with conn:
        conn.execute("UPDATE assets SET status = 'approved' WHERE id = ?", (a["id"],))
    conn.close()
    for operator in (False, True):
        tgt = store.create_campaign(f"t{operator}")
        out = library.copy_assets([a["id"]], tgt["id"], operator=operator)
        assert store.get_asset(out["map"][str(a["id"])])["status"] == "planned"
        assert [x["lands_as"] for x in out["items"]] == ["planned"]


def test_a_reshoot_after_a_move_never_deletes_a_still_left_in_the_old_campaign(src, best):
    """B3: the take moves to the collection; its APPROVED still stays behind in the old campaign.
    Re-recording the take must not rmtree the folder that still holds that approved still."""
    from campaign import tools

    take = _row(src, "shots/t-20261002-010000/take.webm", "captured")
    still = _row(src, "shots/t-20261002-010000/frame.png", "approved", kind="still", parent_id=take["id"])
    store.update_asset(take["id"], meta={"take_dir": str(Path(take["path"]).parent)})
    library.move_assets([take["id"]], best["id"], lineage=False)
    moved = store.get_asset(take["id"])
    new_dir = paths.campaign_dir(best["id"], best["name"]) / "shots" / "t-new"
    new_dir.mkdir(parents=True)
    notes = tools._supersede_take(moved, str(new_dir))
    assert os.path.exists(still["path"]) and store.get_asset(still["id"])["status"] == "approved"
    assert any("APPROVED" in n for n in notes)


def test_a_reshoot_after_a_move_keeps_a_folder_another_campaign_still_uses(src, best):
    from campaign import tools

    take = _row(src, "shots/t-20261002-010000/take.webm", "captured")
    other = _row(src, "shots/t-20261002-010000/other.png", "rendered", kind="still")  # not the take's still
    store.update_asset(take["id"], meta={"take_dir": str(Path(take["path"]).parent)})
    library.move_assets([take["id"]], best["id"], lineage=False)
    tools._supersede_take(store.get_asset(take["id"]), str(paths.media_root() / "elsewhere"))
    assert os.path.exists(other["path"])


def test_copying_twice_is_idempotent(src, best):
    a = _row(src, "renders/a.mp4", "ready_for_review")
    first = library.copy_assets([a["id"]], best["id"])
    again = library.copy_assets([a["id"]], best["id"])
    assert again["done"] == [] and again["already_copied"] == [a["id"]]
    assert again["map"] == first["map"] and len(store.list_assets(best["id"])) == 1


def test_a_copy_names_forward_chains_by_the_new_ids(src, best):
    old = _row(src, "renders/old.mp4")
    new = _row(src, "renders/new.mp4", "ready_for_review")
    store.update_asset(old["id"], status="superseded", superseded_by=[new["id"]])
    out = library.copy_assets([old["id"], new["id"]], best["id"])
    m = {int(k): v for k, v in out["map"].items()}
    assert store.get_asset(m[old["id"]])["superseded_by"] == [m[new["id"]]]


def test_a_copy_never_reads_files_outside_the_media_root(src, best, tmp_path):
    secret = tmp_path / "secret.mp4"
    secret.write_bytes(b"secret")
    a = store.add_asset(src["id"], "clip", "outside", path=str(secret), status="captured")
    out = library.copy_assets([a["id"]], best["id"])
    cp = store.get_asset(out["map"][str(a["id"])])
    assert cp["path"] == "" and out["no_file"] == [a["id"]]
    assert not any(p.name == "secret.mp4" for p in paths.campaign_dir(best["id"], best["name"]).rglob("*"))


def test_a_same_named_file_in_the_target_is_never_overwritten(src, best):
    a = _row(src, "renders/a.mp4")
    clash = paths.campaign_dir(best["id"], best["name"]) / "renders" / "a.mp4"
    clash.parent.mkdir(parents=True)
    clash.write_bytes(b"already here")
    out = library.copy_assets([a["id"]], best["id"])
    cp = store.get_asset(out["map"][str(a["id"])])
    assert clash.read_bytes() == b"already here" and cp["path"] != str(clash) and Path(cp["path"]).exists()


# ── archive ───────────────────────────────────────────────────────────────────
def test_archiving_hides_a_campaign_and_restores_its_status(src):
    store.update_campaign(src["id"], status="launched")
    a = _row(src, "renders/a.mp4", "approved")
    c = store.set_archived(src["id"])
    assert c["status"] == "archived" and src["id"] not in {x["id"] for x in store.list_campaigns()}
    assert store.get_asset(a["id"]) == a, "archiving never touches an asset"
    assert store.set_archived(src["id"])["status"] == "archived", "archiving twice is a no-op"
    assert store.set_archived(src["id"], False)["status"] == "launched"


# ── tools + routes ────────────────────────────────────────────────────────────
@pytest.fixture
def tools(registry):
    campaign.register(registry)
    return registry


def test_the_collect_tool_dry_runs_first_and_refuses_approved_moves(tools, src, best):
    _, _, render, hero, _ = _chain(src)
    t = tools.tool("campaign_collect")
    out = t.invoke({"target_campaign_id": best["id"], "asset_ids": [render["id"]]})
    assert "Would move" in out and "dry run" in out and store.list_assets(best["id"]) == []
    out = t.invoke({"target_campaign_id": best["id"], "asset_ids": [hero["id"]], "dry_run": False})
    assert out.startswith("Not collected") and "operator" in out
    out = t.invoke({"target_campaign_id": best["id"], "asset_ids": [hero["id"]], "mode": "copy", "dry_run": False})
    assert out.startswith("Copied 3 asset(s)") and "lands ready_for_review" in out
    assert "APPROVED" in t.description


def test_the_archive_tool_and_list(tools, src, best):
    out = tools.tool("campaign_archive").invoke({"campaign_id": src["id"]})
    assert "is now archived" in out
    assert src["name"] not in tools.tool("campaign_list").invoke({})
    assert src["name"] in tools.tool("campaign_list").invoke({"include_archived": True})
    assert "is now draft" in tools.tool("campaign_archive").invoke({"campaign_id": src["id"], "archived": False})


@pytest.fixture
def client(registry):
    campaign.register(registry)
    app = FastAPI()
    for prefix, router in registry.routers:
        app.include_router(router, prefix=prefix)
    return TestClient(app)


def test_the_gallery_hides_archived_campaigns_by_default(client, src, best):
    client.post(f"/api/plugins/campaign/campaigns/{src['id']}/archive", json={"archived": True})
    got = client.get("/api/plugins/campaign/campaigns").json()
    assert [c["id"] for c in got["campaigns"]] == [best["id"]] and got["archived_hidden"] == 1
    got = client.get("/api/plugins/campaign/campaigns?include_archived=true").json()
    assert {c["id"]: c["archived"] for c in got["campaigns"]} == {src["id"]: True, best["id"]: False}
    assert client.post("/api/plugins/campaign/campaigns/999/archive", json={}).status_code == 404


def test_the_operator_route_moves_approved_assets(client, src, best):
    _, _, _, hero, _ = _chain(src)
    url = f"/api/plugins/campaign/campaigns/{best['id']}/collect"
    dry = client.post(url, json={"asset_ids": [hero["id"]]}).json()
    assert dry["dry_run"] is True and len(dry["items"]) == 3 and store.list_assets(best["id"]) == []
    real = client.post(url, json={"asset_ids": [hero["id"]], "dry_run": False}).json()
    assert len(real["done"]) == 3 and store.get_asset(hero["id"])["campaign_id"] == best["id"]
    assert client.post(url, json={"asset_ids": [hero["id"]], "mode": "steal"}).status_code == 422
    assert client.post(url, json={"asset_ids": [12345], "dry_run": False}).status_code == 400
    assert (
        client.post("/api/plugins/campaign/campaigns/999/collect", json={"asset_ids": [hero["id"]]}).status_code == 404
    )


def test_any_file_under_matches_folders_not_name_prefixes(src):
    a = _row(src, "shots/t_1/take.webm", "captured")
    d = str(Path(a["path"]).parent)
    assert store.any_file_under(d) and store.any_file_under(d + "/")
    assert not store.any_file_under(d[:-1]), "t_ is not a wildcard, and t_ isn't t_1"
    assert not store.any_file_under(d + "x")


def test_a_failed_copy_leaves_no_half_copied_files_and_no_rows(client, src, best, monkeypatch):
    take, _, render, hero, _ = _chain(src)
    real = library.shutil.copy2
    calls = []

    def flaky(s, d):
        calls.append(d)
        if len(calls) == 3:
            raise OSError(28, "No space left on device")
        return real(s, d)

    monkeypatch.setattr(library.shutil, "copy2", flaky)
    r = client.post(f"/api/plugins/campaign/campaigns/{best['id']}/collect",
                    json={"asset_ids": [hero["id"]], "mode": "copy", "dry_run": False})  # fmt: skip
    assert r.status_code == 500 and "OSError" in r.json()["detail"]
    assert store.list_assets(best["id"]) == []
    assert not [p for p in paths.campaign_dir(best["id"], best["name"]).rglob("*") if p.is_file()]


def test_a_copy_never_points_a_chain_into_another_campaign(src, best):
    old = _row(src, "renders/old.mp4")
    new = _row(src, "renders/new.mp4", "ready_for_review")
    store.update_asset(old["id"], status="superseded", superseded_by=[new["id"]])
    out = library.copy_assets([old["id"]], best["id"], lineage=False)
    assert store.get_asset(out["map"][str(old["id"])])["superseded_by"] == []
