"""Tool flows end to end through the registered tools, with the browser and ffmpeg faked."""

from __future__ import annotations

import json
from pathlib import Path

import campaign
import pytest
from campaign import deps, render, shoot, store
from conftest import FakePlaywright
from test_render import FakeFF


@pytest.fixture
def tools(registry):
    campaign.register(registry)
    return registry


@pytest.fixture
def browser_ok(monkeypatch):
    pw = FakePlaywright()
    monkeypatch.setattr(deps, "need_browser", lambda: None)
    monkeypatch.setattr(shoot, "_default_factory", pw)
    from campaign import cards

    monkeypatch.setattr(cards, "_default_factory", pw)
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
    monkeypatch.setattr(deps, "playwright_installed", lambda: False)
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
