"""The shoot runner — Playwright mocked at the sync_playwright() boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from campaign import shoot
from campaign.shotscript import validate
from campaign.worker import pw_worker
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
    assert r["failed_step"] == 3 and json.loads(Path(r["timing"]).read_text())["failed_step"] == 3


def test_a_failed_take_closes_the_page_then_the_context_so_the_video_is_flushed(tmp_path):
    pw = FakePlaywright(fail_on=lambda action, how, args: action == "click")
    with pytest.raises(shoot.ShootError) as e:
        shoot.run(_script({"mark": "go"}, {"click": {"text": "Install"}}), tmp_path / "t", playwright_factory=pw)
    r = e.value.result
    ctx = pw.browser.contexts[0]
    assert getattr(ctx.page, "closed", False), "the page is closed in a finally"
    assert ("context.close",) in pw.calls and pw.calls[-1] == ("browser.close",)
    assert Path(r["video"]).is_file() and Path(r["video"]).name == "take.webm"
    assert r["marks"] == {"go": r["marks"]["go"]}, "the marks reached before the failure are reported"


def _crash_on_close(self):
    # The browser crashed while finalizing: a truncated webm is on disk and close() raises.
    p = Path(self.page.video.path())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x1a\x45\xdf\xa3trunc")
    raise RuntimeError("Target page, context or browser has been closed")


def test_a_clean_run_whose_video_cannot_be_finalized_fails_the_take(tmp_path, monkeypatch):
    import conftest

    monkeypatch.setattr(conftest.FakeContext, "close", _crash_on_close)
    pw = FakePlaywright()
    with pytest.raises(shoot.ShootError) as e:  # every step passed; only finalizing failed
        shoot.run(_script({"mark": "go"}), tmp_path / "t", playwright_factory=pw)
    msg = str(e.value)
    assert (
        "couldn't be finalized after step 2" in msg and "may be truncated" in msg and "browser has been closed" in msg
    )
    r = e.value.result
    assert r["failed_step"] == 2 and Path(r["video"]).is_file(), "kept — but as a failed take"
    assert json.loads(Path(r["timing"]).read_text())["error"] == msg
    assert pw.calls[-1] == ("browser.close",)


def test_a_failed_run_whose_close_also_raises_keeps_the_step_error(tmp_path, monkeypatch):
    import conftest

    monkeypatch.setattr(conftest.FakeContext, "close", _crash_on_close)
    pw = FakePlaywright(fail_on=lambda action, how, args: action == "click")
    with pytest.raises(shoot.ShootError) as e:
        shoot.run(_script({"click": {"text": "Install"}}), tmp_path / "t", playwright_factory=pw)
    assert str(e.value).startswith("step 2 (click text='Install') failed") and e.value.result["failed_step"] == 2


def test_a_page_close_that_raises_still_finalizes_and_keeps_the_video(tmp_path, monkeypatch):
    import conftest

    def boom(self):
        raise RuntimeError("target closed")

    monkeypatch.setattr(conftest.FakePage, "close", boom)
    pw = FakePlaywright(fail_on=lambda action, how, args: action == "click")
    with pytest.raises(shoot.ShootError) as e:
        shoot.run(_script({"click": {"text": "Install"}}), tmp_path / "t", playwright_factory=pw)
    assert Path(e.value.result["video"]).is_file()


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
    ctx = pw.browser.contexts[0]
    # Never a context-wide header: that rides along to EVERY origin the page touches.
    assert "extra_http_headers" not in ctx.kw
    assert ctx.kw["service_workers"] == "block", "a service worker would answer outside the guards"


def _guards(tmp_path, bearer="t0k", base="http://app.test:8080"):
    pw = FakePlaywright()
    s = validate({"base_url": base, "auth": {"bearer_env": "CAMPAIGN_T"}, "steps": [{"goto": "/"}]})
    shoot.run(s, tmp_path, playwright_factory=pw, env={"CAMPAIGN_T": bearer})
    return pw.browser.contexts[0].routes


class _Route:
    def __init__(self, url, headers=None):
        self.request = type("R", (), {"url": url, "headers": dict(headers or {"accept": "*/*"})})()
        self.done = None

    def abort(self, reason=""):
        self.done = ("abort", reason)

    def continue_(self, **kw):
        self.done = ("continue", kw)


def test_the_bearer_goes_to_the_base_url_origin_only(tmp_path):
    (block_match, _), (auth_match, auth_handler) = _guards(tmp_path)
    assert auth_match("http://app.test:8080/x") and auth_match("http://APP.test:8080/a?b=1")
    for other in (
        "https://app.test:8080/x",  # scheme
        "http://app.test/x",  # port
        "http://cdn.example.com/lib.js",
        "http://app.test.evil.com:8080/",
        "http://evil.com/?u=http://app.test:8080/",
        "data:text/html,hi",
    ):
        assert not auth_match(other), other
    r = _Route("http://app.test:8080/x")
    auth_handler(r)
    assert r.done == ("continue", {"headers": {"accept": "*/*", "authorization": "Bearer t0k"}})


def test_the_shoot_browser_can_never_reach_this_plugins_own_api(tmp_path):
    (block_match, block_handler), (auth_match, _) = _guards(tmp_path)
    for url in (
        "http://127.0.0.1:7870/api/plugins/campaign/assets/1/review",
        "http://localhost:7871/api/plugins/campaign/campaigns",
        "http://hub:7870/agents/foo/api/plugins/campaign/file/3",  # through the fleet proxy
        "http://127.0.0.1:7870/api/plugins/%63ampaign/assets/1/review",  # percent-encoded
        "http://127.0.0.1:7870//api//plugins/campaign/assets/1/review",
        "http://app.test:8080/api/plugins/campaign",
    ):
        assert shoot.is_own_api(url) and block_match(url), url
    assert not auth_match("http://app.test:8080/api/plugins/campaign/x"), "never authed toward our API either"
    for url in ("http://127.0.0.1:7870/plugins/campaign/view", "http://x/api/plugins/campaigner/x", "http://x/"):
        assert not shoot.is_own_api(url), url
    r = _Route("http://127.0.0.1:7870/api/plugins/campaign/assets/1/review")
    block_handler(r)
    assert r.done[0] == "abort"


# Every way a URL can say "/api/plugins/campaign" to some hop between the browser and the
# route: case, single/double/triple percent-encoding, encoded slashes, `..` (plain, encoded,
# split around the target), backslashes, `//` runs, control characters, host forms (trailing
# dot, IPv6, userinfo) and the fleet proxy's `/agents/<slug>/` prefix (which DECODES once and
# forwards — so a double-encoded path is decoded twice before the member routes it).
FENCE_TRICKS = (
    "http://h/API/Plugins/CAMPAIGN/assets/1/review",
    "http://h/api/plugins/%2563ampaign/assets/1/review",
    "http://h/api/plugins/%252563ampaign/x",
    "http://h/api%2Fplugins%2Fcampaign/x",
    "http://h/api%252Fplugins%252Fcampaign",
    "http://h/api/plugins/x%2F..%2Fcampaign/assets/1/review",
    "http://h/api/plugins/x/%2e%2e/campaign/x",
    "http://h/api/plugins/x/./../campaign",
    "http://h/api/x/../plugins/campaign",
    "http://h/api\\plugins\\campaign/x",
    "http://h/api%5Cplugins%5Ccampaign/x",
    "http://h/api/plugins///campaign/x",
    "http://h/api/plugins/camp%09aign/x",
    "http://h/api/plugins/campaign%00/x",
    "http://h./api/plugins/campaign/x",
    "http://[::1]:7870/api/plugins/campaign/x",
    "http://user:pw@h:7870/api/plugins/campaign/x",
    "http://h:7870@evil.test/api/plugins/campaign/x",
    "https://hub/agents/foo/api/plugins/campaign/assets/1/review",
    "https://hub/agents/foo/api/plugins/%2563ampaign/assets/1/review",
    "https://hub/agents/foo/%2Fapi%2Fplugins%2Fcampaign",
    "https://hub/agents/foo/x%2F..%2F..%2Fapi/plugins/campaign",
)


@pytest.mark.parametrize("url", FENCE_TRICKS)
def test_the_fence_sees_through_every_encoding_trick(url):
    assert shoot.is_own_api(url), url


@pytest.mark.parametrize(
    "url",
    [
        "http://[::1/api/plugins/campaign",  # unparseable (bad IPv6 bracket)
        "http://h/x/%ff%fe",  # not UTF-8 once decoded
        "http://h/%" + "25" * 12 + "41",  # still encoded after the decode cap
        None,
        b"http://h/",
    ],
)
def test_the_fence_fails_closed_on_anything_it_cant_read(url):
    assert pw_worker.path_blocked(url, shoot.FENCE_PATTERNS)


def test_a_broken_fence_pattern_blocks_rather_than_letting_through():
    assert pw_worker.path_blocked("http://h/anything", ["("])


def test_the_fence_still_lets_ordinary_app_paths_through():
    for url in (
        "http://127.0.0.1:7870/plugins/campaign/view",
        "http://x/api/plugins/campaigner/x",
        "http://x/",
        "http://x/docs/caf%C3%A9",
        "http://x/a/../b?next=/api/plugins/campaign",  # the QUERY isn't routed
        "http://x/search#/api/plugins/campaign",
        "data:text/html,hi",
    ):
        assert not shoot.is_own_api(url), url


def test_the_guard_is_installed_even_without_a_bearer(tmp_path):
    pw = FakePlaywright()
    shoot.run(_script(), tmp_path, playwright_factory=pw)
    ((match, _),) = pw.browser.contexts[0].routes
    assert match("http://127.0.0.1:7870/api/plugins/campaign/assets/1/review")


@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "GITHUB_TOKEN", "HOME", "ANTHROPIC_API_KEY", "MY_TOKEN"])
def test_bearer_env_cannot_lift_an_arbitrary_secret(tmp_path, name):
    pw = FakePlaywright()
    s = _script(auth={"bearer_env": name})
    with pytest.raises(shoot.ShootError, match="isn't allowed"):
        shoot.run(s, tmp_path, playwright_factory=pw, env={name: "sekrit"})
    assert pw.browser is None, "refused before any browser starts"


@pytest.mark.parametrize("name", ["A2A_AUTH_TOKEN", "PROTOAGENT_FLEET_TOKEN"])
def test_the_hosts_operator_credential_is_refused_even_if_allowlisted(tmp_path, name):
    shoot.configure(name)
    s = _script(auth={"bearer_env": name})
    with pytest.raises(shoot.ShootError, match="operator credential"):
        shoot.run(s, tmp_path, playwright_factory=FakePlaywright(), env={name: "op"})


def test_operator_allowlisted_bearer_env_works(tmp_path):
    shoot.configure("STAGING_TOKEN, OTHER")
    pw = FakePlaywright()
    shoot.run(
        _script(auth={"bearer_env": "STAGING_TOKEN"}), tmp_path, playwright_factory=pw, env={"STAGING_TOKEN": "x"}
    )
    assert len(pw.browser.contexts[0].routes) == 2


def test_a_bearer_needs_a_base_url_to_scope_it_to():
    from campaign.shotscript import ScriptError

    with pytest.raises(ScriptError, match="needs a base_url"):
        validate({"auth": {"bearer_env": "CAMPAIGN_T"}, "steps": [{"goto": "http://app.test/"}]})


def test_total_budget_stops_between_steps(tmp_path, monkeypatch):
    pw = FakePlaywright()
    s = _script({"hold": 100}, {"hold": 100}, total_timeout_s=5)
    clock = iter([0, 0, 0] + [1000.0] * 50)
    monkeypatch.setattr(pw_worker.time, "monotonic", lambda: next(clock))
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


def test_a_steps_own_timeout_reaches_playwright_bounded_by_the_budget(tmp_path):
    pw = FakePlaywright()
    s = _script(
        {"wait_for": {"text": "Done", "timeout_ms": 30_000}},
        {"click": {"text": "Go"}},
        {"wait_for": {"ms": 20_000}},
        step_timeout_ms=15_000,
    )
    shoot.run(s, tmp_path, playwright_factory=pw)
    waits = [c for c in pw.calls if c[0] == "wait_for"]
    assert waits[0][4]["timeout"] == 30_000.0, "the step's timeout_ms, not the 15s script default"
    clicks = [c for c in pw.calls if c[0] == "click"]
    assert clicks[0][4]["timeout"] == 15_000.0, "steps without one keep step_timeout_ms"
    # A fixed 20s wait is its own length — never cut to the 15s step timeout.
    assert ("wait_for_timeout", 20_000) in pw.calls
    page = pw.browser.contexts[0].page
    assert 30_000.0 in page.default_timeouts, "calls without an explicit timeout honour it too"


def test_step_timeouts_never_outlive_the_shoot():
    f = pw_worker.step_timeout_for
    assert f({"op": "wait_for", "timeout_ms": 120_000}, 15_000, 40_000) == 40_000
    assert f({"op": "click"}, 15_000, 400_000) == 15_000
    assert f({"op": "hold", "ms": 5000}, 15_000, 400_000) == 400_000
    assert f({"op": "click"}, 15_000, 0) == 1.0, "never 0 — Playwright reads 0 as 'wait forever'"


# ── frames (a protoAgent plugin view is an iframe) ─────────────────────────────
TERM = "http://app.test/plugins/terminal/view"


def test_a_frame_target_waits_for_the_frame_then_acts_inside_it(tmp_path):
    # The iframe attaches only after a few polls — the step waits for it.
    pw = FakePlaywright(frames=[(4, TERM, "iframe[title='Terminal']", None)])
    s = _script(
        {"wait_for": {"text": "connected", "frame": {"url": "/plugins/terminal/view"}}},
        {"type": {"target": {"selector": "textarea", "frame": {"selector": "iframe[title='Terminal']"}}, "text": "ls"}},
        {"press": {"selector": "textarea", "key": "Enter", "frame": "*/plugins/terminal/*"}},
        {"screenshot": {"name": "term", "frame": "/plugins/terminal/view"}},
    )
    res = shoot.run(s, tmp_path, playwright_factory=pw)
    assert res["error"] == ""
    waits = [c for c in pw.calls if c[0] == "wait_for"]
    assert waits[0][1:3] == ("text", ("connected",)) and waits[0][3]["frame"] == TERM
    clicks = [c for c in pw.calls if c[0] == "click"]
    assert clicks[0][3]["frame"] == TERM, "the type target is clicked INSIDE the frame"
    assert ("keyboard.type", "ls", 45) in pw.calls
    assert [c for c in pw.calls if c[0] == "press"][0][3]["frame"] == TERM
    assert ("frame_element.screenshot", TERM) in pw.calls
    polls = [c for c in pw.calls if c[0] == "wait_for_timeout"]
    assert polls and all(c[1] <= 100 for c in polls), "the frame is polled, not slept on"


def test_a_nested_frame_is_found_one_level_down(tmp_path):
    inner = "http://app.test/inner.html"
    pw = FakePlaywright(
        frames=[
            (1, "http://app.test/outer.html", "iframe.outer", None),
            (2, inner, "iframe.in", "http://app.test/outer.html"),
        ]
    )
    s = _script({"click": {"text": "deep", "frame": {"selector": "iframe.outer", "frame": {"selector": "iframe.in"}}}})
    shoot.run(s, tmp_path, playwright_factory=pw)
    assert [c for c in pw.calls if c[0] == "click"][0][3]["frame"] == inner


def test_a_frame_that_never_appears_fails_the_step_naming_the_frames_there_are(tmp_path):
    pw = FakePlaywright(frames=[(1, "http://app.test/plugins/notes/view", "iframe", None)])
    s = _script({"wait_for": {"text": "x", "frame": "/plugins/terminal/view", "timeout_ms": 300}})
    with pytest.raises(shoot.ShootError) as e:
        shoot.run(s, tmp_path, playwright_factory=pw)
    msg = str(e.value)
    assert msg.startswith("step 2 (wait for text='x' in frame(url='/plugins/terminal/view')")
    assert "Timeout 300ms" in msg and "/plugins/notes/view" in msg


def test_two_matching_frames_are_an_error_not_a_guess(tmp_path):
    pw = FakePlaywright(frames=[(1, TERM + "?a", "iframe", None), (1, TERM + "?b", "iframe", None)])
    with pytest.raises(shoot.ShootError, match="matches 2 frames"):
        shoot.run(_script({"click": {"text": "x", "frame": "/plugins/terminal/view"}}), tmp_path, playwright_factory=pw)


def test_mask_and_redact_steps_reach_into_frames_already_loaded(tmp_path):
    pw = FakePlaywright(frames=[(1, TERM, "iframe", None)])
    s = _script(
        {"wait_for": {"frame": "/plugins/terminal/view"}},
        {"mask": {"selectors": [".secret"], "mode": "hide"}},
        {"redact": {"patterns": ["acme-[0-9]+"]}},
    )
    shoot.run(s, tmp_path, playwright_factory=pw)
    assert ("frame.style", TERM, ".secret { visibility: hidden !important; }") in pw.calls
    assert any(
        c[0] == "frame.evaluate" and c[1] == TERM and c[3]["rules"] == [["acme-[0-9]+", "•••"]] for c in pw.calls
    )
    # …and frames still to come get both from the context init scripts (Playwright runs them in every frame).
    init = "\n".join(pw.browser.contexts[0].init_scripts)
    assert ".secret" in init and "acme-[0-9]+" in init


def test_frame_url_patterns_are_substrings_or_whole_url_globs():
    m = pw_worker.frame_url_matches
    assert m(TERM, "/plugins/terminal/view") and m(TERM, "*/plugins/terminal/*")
    assert not m(TERM, "/plugins/terminal/view/x") and not m(TERM, "/plugins/*/view"), "a glob is on the WHOLE url"


def test_the_pointer_follows_into_frames_instead_of_a_second_one_drawn_there():
    js = pw_worker.CURSOR_JS
    assert "window.top !== window" in js and "postMessage" in js and "stopImmediatePropagation" in js


def test_the_shoot_forces_the_real_scale_factor_so_webgl_canvases_size_right(tmp_path):
    pw = FakePlaywright()
    shoot.run(_script(device_scale_factor=2), tmp_path / "a", playwright_factory=pw)
    assert next(c for c in pw.calls if c[0] == "launch")[1]["args"] == ["--force-device-scale-factor=2"]
    pw = FakePlaywright()
    shoot.run(_script(device_scale_factor=1), tmp_path / "b", playwright_factory=pw)
    assert next(c for c in pw.calls if c[0] == "launch")[1]["args"] == []


# ── several matches: a wait takes any, an action needs one ──────────────────────
TWICE = {"3 passed": ["3 passed", "pytest -q: 3 passed in 0.4s"]}


def test_a_wait_on_a_target_that_matches_twice_waits_for_any_visible_match(tmp_path):
    pw = FakePlaywright(matches=TWICE)
    res = shoot.run(_script({"wait_for": {"text": "3 passed"}}), tmp_path, playwright_factory=pw)
    assert res["error"] == ""
    wait = [c for c in pw.calls if c[0] == "wait_for"][0]
    assert wait[3]["filter"] == "visible=true" and wait[3]["nth"] == 0 and wait[4]["state"] == "visible"


def test_a_wait_that_picks_nth_waits_on_exactly_that_one(tmp_path):
    pw = FakePlaywright(matches=TWICE)
    shoot.run(_script({"wait_for": {"text": "3 passed", "nth": "last", "state": "attached"}}), tmp_path,
              playwright_factory=pw)  # fmt: skip
    wait = [c for c in pw.calls if c[0] == "wait_for"][0]
    assert "filter" not in wait[3] and wait[3]["nth"] == -1 and wait[4]["state"] == "attached"


def test_an_action_on_an_ambiguous_target_lists_the_matches_and_how_to_pick(tmp_path):
    pw = FakePlaywright(matches=TWICE)
    with pytest.raises(shoot.ShootError) as e:
        shoot.run(_script({"click": {"text": "3 passed"}}), tmp_path, playwright_factory=pw)
    msg = str(e.value)
    assert msg.startswith("step 2 (click text='3 passed') failed: text='3 passed' matches 2 elements:")
    assert "1) <span> '3 passed'; 2) <span> 'pytest -q: 3 passed in 0.4s'" in msg
    assert "`nth`" in msg and "exact: true" in msg and "narrower" in msg


def test_an_action_with_nth_on_an_ambiguous_target_acts_on_that_one(tmp_path):
    pw = FakePlaywright(matches=TWICE)
    res = shoot.run(_script({"click": {"text": "3 passed", "nth": 1}}), tmp_path, playwright_factory=pw)
    assert res["error"] == ""
    assert [c for c in pw.calls if c[0] == "click"][0][3]["nth"] == 1


def test_other_failures_are_not_reworded_as_ambiguity(tmp_path):
    pw = FakePlaywright(fail_on=lambda action, how, args: action == "click")
    with pytest.raises(shoot.ShootError) as e:
        shoot.run(_script({"click": {"text": "Go"}}), tmp_path, playwright_factory=pw)
    assert "Timeout 15000ms exceeded" in str(e.value) and "matches" not in str(e.value)


# ── pointer + keyboard steps: focus, mouse_move, drag, press repeat, within ───
def test_focus_mouse_move_and_drag_drive_the_real_pointer(tmp_path):
    pw = FakePlaywright()
    res = shoot.run(
        _script(
            {"focus": {"role": "textbox", "name": "Message"}},
            {"mouse_move": {"x": 1200, "y": 780}},
            {"drag": {"role": "separator", "name": "Resize", "to": {"dx": -300, "dy": 0}}},
        ),
        tmp_path,
        playwright_factory=pw,
    )
    assert res["error"] == ""
    focus = next(c for c in pw.calls if c[0] == "focus")
    assert focus[1:4] == ("role", ("textbox",), {"name": "Message"})
    assert ("mouse.move", 1200, 780) in pw.calls
    # The fake box is x=10 y=20 100×40 → centre (60, 40): press there, move by the offset, release.
    i = pw.calls.index(("mouse.down",))
    assert pw.calls[i - 1] == ("mouse.move", 60.0, 40.0), "the pointer glides to the handle first"
    assert pw.calls[i + 1 : i + 3] == [("mouse.move", -240.0, 40.0), ("mouse.up",)]


def test_an_ambiguous_drag_or_focus_target_names_the_matches(tmp_path):
    pw = FakePlaywright(matches={".divider": ["a", "b"]})
    with pytest.raises(shoot.ShootError) as e:
        shoot.run(_script({"focus": ".divider"}), tmp_path, playwright_factory=pw)
    assert "matches 2 elements" in str(e.value) and "step 2 (focus" in str(e.value)


def test_press_repeat_presses_n_times_with_the_gap_between(tmp_path):
    pw = FakePlaywright()
    shoot.run(
        _script(
            {"press": {"key": "ArrowDown", "repeat": 3, "delay_ms": 120}},
            {"press": {"key": "Tab", "repeat": 2, "label": "Search"}},
        ),
        tmp_path,
        playwright_factory=pw,
    )
    keys = [c for c in pw.calls if c[0] in ("keyboard.press", "wait_for_timeout", "press")]
    assert keys == [
        ("keyboard.press", "ArrowDown"),
        ("wait_for_timeout", 120),
        ("keyboard.press", "ArrowDown"),
        ("wait_for_timeout", 120),
        ("keyboard.press", "ArrowDown"),
        ("press", "label", ("Search",), {"exact": False}, {"key": "Tab"}),
        ("wait_for_timeout", 80),
        ("press", "label", ("Search",), {"exact": False}, {"key": "Tab"}),
    ]


def test_within_looks_for_the_target_inside_the_container_only(tmp_path):
    # "Save" matches twice on the page, but ONE inside the dialog: within scopes it, no strict error.
    pw = FakePlaywright(matches={"button": ["Save", "Save"]})
    res = shoot.run(
        _script(
            {"wait_for": {"text": "Saved", "within": ".toast"}},
            {"click": {"selector": "#save", "within": {"role": "dialog", "name": "Settings"}}},
        ),
        tmp_path,
        playwright_factory=pw,
    )
    assert res["error"] == ""
    wait = next(c for c in pw.calls if c[0] == "wait_for")
    assert wait[1:3] == ("text", ("Saved",)) and wait[3]["within"] == ("locator", (".toast",))
    click = next(c for c in pw.calls if c[0] == "click")
    assert click[1:3] == ("locator", ("#save",)) and click[3]["within"] == ("role", ("dialog",))


def test_check_targets_scopes_within_too(monkeypatch):
    pw = FakePlaywright()
    script = _script({"click": {"text": "Save", "within": "#dlg"}})
    seen = []
    orig = pw_worker.describe_matches

    def spy(root, target, limit=3):
        seen.append(getattr(root, "args", None))
        return orig(root, target, limit)

    monkeypatch.setattr(pw_worker, "describe_matches", spy)
    with pw() as p:
        page = p.chromium.launch().new_context(record_video_dir="").new_page()
        report = pw_worker.check_targets(page, script)
    assert seen == [("#dlg",)], "the preflight counts matches inside the container"
    assert report[0]["status"] == "ok"
