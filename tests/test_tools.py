"""Tool flows end to end through the registered tools, with the browser and ffmpeg faked."""

from __future__ import annotations

import json
from pathlib import Path

import campaign
import pytest
from campaign import deps, render, shoot, store
from conftest import FakePlaywright, in_process_worker
from test_render import FakeFF


@pytest.fixture
def tools(registry):
    campaign.register(registry)
    return registry


@pytest.fixture
def browser_ok(monkeypatch):
    pw = FakePlaywright()
    monkeypatch.setattr(deps, "need_browser", lambda: None)
    monkeypatch.setattr(shoot, "run_worker", in_process_worker(pw))
    return pw


@pytest.fixture
def ffmpeg_ok(monkeypatch):
    ff = FakeFF(probe={"width": 2560, "height": 1600, "duration": "9.5"})
    monkeypatch.setattr(deps, "ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(deps, "ffprobe", lambda: "/usr/bin/ffprobe")
    monkeypatch.setattr(render, "_default_runner", ff)
    return ff


def call(reg, tool_name, **kw):
    return reg.tool(tool_name).invoke(kw)


SCRIPT = """
name: hero
base_url: http://app.test
steps:
  - goto: /
  - mark: start
  - click: {role: button, name: Go}
  - screenshot: result
  - mark: end
"""


def test_plan_flow(tools):
    out = call(tools, "campaign_create", name="Launch X", product="X", goal="stars", target_url="http://app.test")
    assert "Created campaign #1" in out
    assert "Lane" in call(tools, "campaign_lane", campaign_id=1, name="authors", pitch="one click")
    assert "Added asset #1" in call(
        tools,
        "campaign_asset_add",
        campaign_id=1,
        kind="clip",
        title="hero",
        lane="authors",
        limit_id="github_attachment_video_free",
    )
    assert "no lane named" in call(tools, "campaign_asset_add", campaign_id=1, kind="clip", title="x", lane="nope")
    call(tools, "campaign_milestone", campaign_id=1, title="takes", date="2026-10-02", owner="agent")
    call(
        tools,
        "campaign_decision",
        campaign_id=1,
        question="Lead lane?",
        options=["authors", "ops"],
        recommendation="authors",
    )
    plan = call(tools, "campaign_get", campaign_id=1)
    for needle in ("Launch X", "authors", "#1 [planned] clip", "takes", "Lead lane?", "(open)"):
        assert needle in plan
    assert "#1 Launch X [draft]" in call(tools, "campaign_list")
    status = call(tools, "campaign_status", campaign_id=1)
    payload = json.loads(status.split("```json\n", 1)[1].rsplit("```", 1)[0])
    assert payload["keyvalue"]["items"][0] == {"label": "Approved", "value": "0 / 1"}
    assert payload["table"]["columns"] == ["Blocked on", "Item", "Why"]
    assert any(r[0] == "operator" and "Lead lane?" in r[1] for r in payload["table"]["rows"])


def test_agent_cannot_approve_through_the_update_tool(tools):
    call(tools, "campaign_create", name="L")
    call(tools, "campaign_asset_add", campaign_id=1, kind="copy_ref", title="post")
    assert "only the operator" in call(tools, "campaign_asset_update", asset_id=1, status="approved")


def test_update_refuses_files_outside_the_media_dir(tools, tmp_path):
    call(tools, "campaign_create", name="L")
    call(tools, "campaign_asset_add", campaign_id=1, kind="still", title="s", owner="operator")
    outside = tmp_path / "phone.png"
    outside.write_bytes(b"x")
    assert "outside the campaign media dir" in call(tools, "campaign_asset_update", asset_id=1, path=str(outside))


def test_script_save_reports_every_problem(tools):
    call(tools, "campaign_create", name="L")
    out = call(tools, "campaign_script_save", campaign_id=1, script="steps:\n  - goto: /\n  - clik: x\n")
    assert out.startswith("Not saved") and "relative but the script has no base_url" in out and "`clik`" in out
    assert "```yaml" in call(tools, "campaign_script_save", campaign_id=1, script="template")
    assert "Saved shot script #1 'hero'" in call(tools, "campaign_script_save", campaign_id=1, script=SCRIPT)


def test_shoot_registers_the_take_and_its_stills(tools, browser_ok, ffmpeg_ok):
    call(tools, "campaign_create", name="L")
    call(tools, "campaign_asset_add", campaign_id=1, kind="clip", title="planned hero")
    out = call(tools, "campaign_shoot", campaign_id=1, script=SCRIPT, asset_id=1)
    assert "Recorded take → asset #1" in out, out
    take = store.get_asset(1)
    assert take["status"] == "captured" and take["path"].endswith("hero.webm") and take["script_id"] == 1
    assert set(take["meta"]["marks"]) == {"start", "end"}
    (still,) = store.list_assets(1, kind="still")
    assert still["parent_id"] == 1 and still["path"].endswith("result.png")
    assert ("shoot_finished", {"campaign_id": 1, "asset_id": 1, "stills": [still["id"]]}) in tools.events
    assert "![hero still](/media/m1)" in out, "the first still is embedded in chat via the core media store"


def test_shoot_failure_is_actionable(tools, browser_ok):
    browser_ok.fail_on = lambda action, how, args: action == "click"
    call(tools, "campaign_create", name="L")
    out = call(tools, "campaign_shoot", campaign_id=1, script=SCRIPT)
    assert out.startswith("Take failed — step 3 (click role='button' name='Go') failed")
    assert "Screenshot at failure:" in out and "failure.png" in out
    assert store.list_assets(1) == [], "a failed take registers nothing"


def test_shoot_without_a_browser_says_how_to_fix_it(tools, monkeypatch):
    from campaign.interpreter import Resolution

    monkeypatch.setattr(deps, "resolve", lambda: Resolution(need="deps"))
    call(tools, "campaign_create", name="L")
    assert "Install dependencies" in call(tools, "campaign_shoot", campaign_id=1, script=SCRIPT)


def test_render_registers_outputs_and_flags_violations(tools, browser_ok, ffmpeg_ok):
    call(tools, "campaign_create", name="L")
    call(tools, "campaign_shoot", campaign_id=1, script=SCRIPT)
    ffmpeg_ok.sizes = [3_000_000, 60_000_000] + [60_000_000] * 10
    out = call(
        tools,
        "campaign_render",
        asset_id=1,
        outputs=[
            {"name": "hero", "format": "mp4", "start": 0, "end": 5, "limit": "github_attachment_video_free"},
            {"name": "loop", "format": "gif", "limit": "github_attachment_image"},
        ],
    )
    assert "#3 hero.mp4" in out and "⚠" in out and "still over" in out, out
    mp4, gif = store.get_asset(3), store.get_asset(4)
    assert mp4["status"] == "rendered" and mp4["parent_id"] == 1 and mp4["limit_id"] == "github_attachment_video_free"
    assert gif["notes"].startswith("VIOLATES")
    assert store.get_asset(1)["status"] == "rendered"
    # The gate holds: an over-limit render can't be offered for review.
    assert "Not updated" in call(tools, "campaign_asset_update", asset_id=4, status="ready_for_review")
    assert "ready_for_review" in call(tools, "campaign_asset_update", asset_id=3, status="ready_for_review")


def test_render_accepts_yaml_text_and_reports_bad_marks(tools, browser_ok, ffmpeg_ok):
    call(tools, "campaign_create", name="L")
    call(tools, "campaign_shoot", campaign_id=1, script=SCRIPT)
    out = call(tools, "campaign_render", asset_id=1, outputs="- {name: a, format: mp4, start: nowhere}")
    assert "FAILED" in out and "nowhere" in out


def test_card_tool_registers_a_card(tools, browser_ok):
    call(tools, "campaign_create", name="L")
    out = call(tools, "campaign_card", campaign_id=1, template="og-1280x640", data={"title": "Ship it"})
    assert "Card → asset #1: 1280×640 png" in out and "![og-1280x640]" in out
    a = store.get_asset(1)
    assert a["kind"] == "card" and a["limit_id"] == "github_social_preview" and Path(a["path"]).is_file()
    assert "doesn't exist" in call(
        tools, "campaign_card", campaign_id=1, template="og-1280x640", data={"image": "/nope.png"}
    )
    assert "unknown template" in call(tools, "campaign_card", campaign_id=1, template="poster", data={})


def test_limits_and_setup_tools(tools):
    assert "github_social_preview" in call(tools, "campaign_limits")
    assert "Campaign Studio setup" in call(tools, "campaign_setup")


# ── hardening (v0.1.1) ─────────────────────────────────────────────────────────
SCRIPT_TWO_STILLS = SCRIPT.replace("  - screenshot: result\n", "  - screenshot: result\n  - screenshot: detail\n")


def test_rerecording_into_an_asset_supersedes_the_previous_takes_stills(tools, browser_ok, ffmpeg_ok):
    call(tools, "campaign_create", name="L")
    call(tools, "campaign_asset_add", campaign_id=1, kind="clip", title="hero")
    call(tools, "campaign_shoot", campaign_id=1, script=SCRIPT_TWO_STILLS, asset_id=1)
    first = store.get_asset(1)
    old_dir = Path(first["meta"]["take_dir"])
    old_stills = store.list_assets(1, kind="still")
    assert len(old_stills) == 2 and old_dir.is_dir()

    out = call(tools, "campaign_shoot", campaign_id=1, script=SCRIPT, asset_id=1)
    assert "superseded 2 still(s) of the previous take" in out, out
    stills = store.list_assets(1, kind="still")
    assert len(stills) == 1 and stills[0]["parent_id"] == 1
    assert Path(stills[0]["path"]).parent == Path(store.get_asset(1)["meta"]["take_dir"]) != old_dir
    assert not old_dir.exists(), "the superseded take's files go with it"
    assert all(store.get_asset(s["id"]) is None for s in old_stills)


def test_rerecording_keeps_a_still_the_operator_approved(tools, browser_ok, ffmpeg_ok):
    call(tools, "campaign_create", name="L")
    call(tools, "campaign_asset_add", campaign_id=1, kind="clip", title="hero")
    call(tools, "campaign_shoot", campaign_id=1, script=SCRIPT_TWO_STILLS, asset_id=1)
    keep, drop = store.list_assets(1, kind="still")
    store.update_asset(keep["id"], status="ready_for_review")
    store.review(keep["id"], "approve")
    out = call(tools, "campaign_shoot", campaign_id=1, script=SCRIPT, asset_id=1)
    assert f"kept the previous take's APPROVED still(s) #{keep['id']}" in out, out
    assert store.get_asset(keep["id"])["status"] == "approved" and Path(keep["path"]).is_file()
    assert store.get_asset(drop["id"]) is None


def test_a_take_or_card_cannot_land_in_another_campaigns_asset(tools, browser_ok, ffmpeg_ok):
    call(tools, "campaign_create", name="One")
    call(tools, "campaign_create", name="Two")
    call(tools, "campaign_asset_add", campaign_id=1, kind="card", title="one's card")
    out = call(tools, "campaign_card", campaign_id=2, template="og-1280x640", data={"title": "x"}, asset_id=1)
    assert "no asset #1 in campaign 2" in out and store.get_asset(1)["path"] == ""
    out = call(tools, "campaign_shoot", campaign_id=2, script=SCRIPT, asset_id=1)
    assert "no asset #1 in campaign 2" in out and browser_ok.browser is None, "refused before recording"


def test_filling_an_approved_asset_is_refused_not_raised(tools, browser_ok, ffmpeg_ok):
    call(tools, "campaign_create", name="L")
    call(tools, "campaign_card", campaign_id=1, template="og-1280x640", data={"title": "x"})
    store.update_asset(1, status="ready_for_review")
    store.review(1, "approve")
    before = store.get_asset(1)["path"]
    out = call(tools, "campaign_card", campaign_id=1, template="og-1280x640", data={"title": "y"}, asset_id=1)
    assert "approved" in out and store.get_asset(1)["path"] == before
    out = call(tools, "campaign_shoot", campaign_id=1, script=SCRIPT, asset_id=1)
    assert "approved" in out


def test_same_second_renders_never_overwrite_each_other(tools, browser_ok, ffmpeg_ok, monkeypatch):
    from datetime import UTC, datetime

    from campaign import tools as toolsmod

    frozen = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(toolsmod, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: frozen)}))
    call(tools, "campaign_create", name="L")
    call(tools, "campaign_shoot", campaign_id=1, script=SCRIPT)
    call(tools, "campaign_shoot", campaign_id=1, script=SCRIPT)
    takes = [a for a in store.list_assets(1, kind="clip")]
    assert len({a["path"] for a in takes}) == 2, "two takes in one second got two directories"
    spec = [{"name": "hero", "format": "mp4"}]
    call(tools, "campaign_render", asset_id=1, outputs=spec)
    call(tools, "campaign_render", asset_id=1, outputs=spec)
    renders = [a for a in store.list_assets(1, kind="clip") if a["parent_id"] == 1]
    assert len({a["path"] for a in renders}) == 2
    call(tools, "campaign_card", campaign_id=1, template="square-1080", data={"title": "a"})
    call(tools, "campaign_card", campaign_id=1, template="square-1080", data={"title": "b"})
    cards_ = store.list_assets(1, kind="card")
    assert len({a["path"] for a in cards_}) == 2


def test_render_caps_the_number_of_outputs(tools, browser_ok, ffmpeg_ok):
    call(tools, "campaign_create", name="L")
    call(tools, "campaign_shoot", campaign_id=1, script=SCRIPT)
    outs = [{"name": f"o{i}", "format": "poster"} for i in range(render.MAX_OUTPUTS + 1)]
    assert "too many" in call(tools, "campaign_render", asset_id=1, outputs=outs)
    assert ffmpeg_ok.cmds and all(c[0].endswith("ffprobe") for c in ffmpeg_ok.cmds), "no ffmpeg run started"


def test_a_numeric_lane_must_exist_in_this_campaign(tools):
    call(tools, "campaign_create", name="A", product="A", goal="g", target_url="http://a.test")
    call(tools, "campaign_create", name="B", product="B", goal="g", target_url="http://b.test")
    call(tools, "campaign_lane", campaign_id=1, name="authors")  # lane 1, campaign 1
    call(tools, "campaign_lane", campaign_id=2, name="ops")  # lane 2, campaign 2
    call(tools, "campaign_lane", campaign_id=2, name="2024")  # lane 3: an all-digit NAME

    def add(cid, lane):
        return call(tools, "campaign_asset_add", campaign_id=cid, kind="clip", title="x", lane=lane)

    # A lane id that doesn't exist, and another campaign's lane id, are refused — nothing filed.
    assert "no lane 99 in campaign 1" in add(1, "99")
    assert "no lane 2 in campaign 1" in add(1, "2")
    assert store.list_assets(1) == []
    # This campaign's own lane id, its name, and an all-digit name all resolve.
    assert "Added asset" in add(1, "1") and "Added asset" in add(1, "authors")
    assert [a["lane_id"] for a in store.list_assets(1)] == [1, 1]
    assert "Added asset" in add(2, "2024") and store.list_assets(2)[0]["lane_id"] == 3
    assert "Added asset" in add(2, "0") and store.list_assets(2)[-1]["lane_id"] == 0, "0 = no lane"
    # The same check guards moving an asset and filtering by lane.
    first = store.list_assets(1)[0]["id"]
    assert "no lane 2 in campaign 1" in call(tools, "campaign_asset_update", asset_id=first, lane="2")
    assert store.get_asset(first)["lane_id"] == 1
    assert "no lane 2 in campaign 1" in call(tools, "campaign_assets", campaign_id=1, lane="2")
