"""The shoot runner — Playwright mocked at the sync_playwright() boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from campaign import shoot
from campaign.shotscript import validate
from conftest import FakePlaywright


def _script(*steps, **top):
    return validate({"base_url": "http://app.test", "steps": [{"goto": "/"}, *steps], **top})


def test_records_video_marks_stills_and_a_timing_log(tmp_path):
    pw = FakePlaywright()
    s = _script(
        {"mark": "start"},
        {"click": {"role": "button", "name": "Settings"}},
        {"type": {"placeholder": "URL", "text": "abc", "delay_ms": 30}},
        {"hold": 500},
        {"screenshot": "dialog"},
        {"mark": "end"},
        name="hero",
        device_scale_factor=2,
        viewport={"width": 1280, "height": 800},
    )
    res = shoot.run(s, tmp_path / "take", playwright_factory=pw)

    assert Path(res["video"]).name == "hero.webm" and Path(res["video"]).is_file()
    assert set(res["marks"]) == {"start", "end"} and res["marks"]["end"] >= res["marks"]["start"]
    assert Path(res["screenshots"]["dialog"]).is_file()
    timing = json.loads(Path(res["timing"]).read_text())
    assert timing["error"] == "" and len(timing["steps"]) == 7
    assert all("t_start" in st and "t_end" in st for st in timing["steps"])
    assert not (tmp_path / "take" / ".video").exists(), "the temp video dir is cleaned up"

    launch = next(c for c in pw.calls if c[0] == "launch")
    assert launch[1]["headless"] is True
    ctx = pw.browser.contexts[0].kw
    # The screencast is CSS pixels; a bigger video size would only pad it with grey.
    assert ctx["record_video_size"] == {"width": 1280, "height": 800}
    assert ctx["device_scale_factor"] == 2.0, "dsf still sharpens the stills"
    assert ctx["timezone_id"] == "UTC" and ctx["locale"] == "en-US" and ctx["color_scheme"] == "dark"
    assert ("goto", "http://app.test/", {"wait_until": "load", "timeout": 15000.0}) in pw.calls
    assert ("keyboard.type", "abc", 30) in pw.calls
    assert ("wait_for_timeout", 500) in pw.calls
    assert any(c[0] == "mouse.move" for c in pw.calls), "the visible pointer glides to targets"


def test_role_targets_use_get_by_role_and_nth(tmp_path):
    pw = FakePlaywright()
    shoot.run(_script({"click": {"role": "tab", "name": "Plugins", "nth": 1}}), tmp_path, playwright_factory=pw)
    clicks = [c for c in pw.calls if c[0] == "click"]
    assert clicks[0][1:4] == ("role", ("tab",), {"name": "Plugins", "nth": 1})


def test_a_failing_step_reports_which_step_and_screenshots_the_page(tmp_path):
    pw = FakePlaywright(fail_on=lambda action, how, args: action == "click" and args == ("Install",))
    s = _script({"click": {"text": "Plugins"}}, {"click": {"text": "Install"}}, {"hold": 100})
    with pytest.raises(shoot.ShootError) as e:
        shoot.run(s, tmp_path / "t", playwright_factory=pw)
    msg = str(e.value)
    assert msg.startswith("step 3 (click text='Install') failed") and "Timeout 15000ms" in msg
    r = e.value.result
    assert Path(r["failure_png"]).is_file()
    assert [st["index"] for st in r["steps"]] == [1, 2, 3], "nothing runs after the failure"
    assert json.loads(Path(r["timing"]).read_text())["error"] == msg
    assert r["video"], "the partial take is kept for diagnosis"


def test_masks_and_redaction_are_installed_before_the_first_frame(tmp_path):
    pw = FakePlaywright()
    s = _script(
        {"mask": {"selectors": [".token"], "mode": "hide"}},
        mask=[".email"],
        redact={"presets": ["home_paths", "secrets"], "patterns": ["acme-[0-9]+"]},
    )
    shoot.run(s, tmp_path, playwright_factory=pw)
    init = "\n".join(pw.browser.contexts[0].init_scripts)
    assert "__campaignCursor" in init
    assert ".email { filter: blur(7px) !important; }" in init
    assert "acme-[0-9]+" in init and "/Users" in init
    styles = [c[1] for c in pw.calls if c[0] == "style"]
    assert styles == [".token { visibility: hidden !important; }"]


def test_cursor_can_be_turned_off(tmp_path):
    pw = FakePlaywright()
    shoot.run(_script(cursor=False), tmp_path, playwright_factory=pw)
    assert not any("__campaignCursor" in s for s in pw.browser.contexts[0].init_scripts)


def test_bearer_comes_from_the_environment_never_the_script(tmp_path):
    pw = FakePlaywright()
    s = _script(auth={"bearer_env": "CAMPAIGN_TEST_TOKEN"})
    with pytest.raises(shoot.ShootError, match="isn't set"):
        shoot.run(s, tmp_path, playwright_factory=pw, env={})
    shoot.run(s, tmp_path, playwright_factory=pw, env={"CAMPAIGN_TEST_TOKEN": "t0k"})
    assert pw.browser.contexts[0].kw["extra_http_headers"] == {"Authorization": "Bearer t0k"}


def test_total_budget_stops_between_steps(tmp_path, monkeypatch):
    pw = FakePlaywright()
    s = _script({"hold": 100}, {"hold": 100}, total_timeout_s=5)
    clock = iter([0, 0, 0] + [1000.0] * 50)
    monkeypatch.setattr(shoot.time, "monotonic", lambda: next(clock))
    with pytest.raises(shoot.ShootError, match="total_timeout_s=5 spent"):
        shoot.run(s, tmp_path, playwright_factory=pw)


def test_video_size_is_the_even_viewport_whatever_the_scale():
    s = _script(viewport={"width": 1919, "height": 1079}, device_scale_factor=3)
    assert shoot.video_size(s) == {"width": 1918, "height": 1078}


def test_runs_fine_from_inside_an_event_loop(tmp_path):
    # A tool call lands on a thread with a running loop; the runner must not care.
    import asyncio

    async def main():
        return shoot.run(_script(), tmp_path, playwright_factory=FakePlaywright())

    assert asyncio.run(main())["video"]
