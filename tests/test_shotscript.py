"""Shot-script validation — every problem reported with its step, before a browser starts."""

from __future__ import annotations

import pytest
from campaign import shotscript
from campaign.shotscript import ScriptError, validate

BASE = {"base_url": "http://localhost:7871", "steps": [{"goto": "/app/"}]}


def _with(*steps, **top):
    return {**BASE, **top, "steps": [{"goto": "/app/"}, *steps]}


def _problems(script) -> str:
    with pytest.raises(ScriptError) as e:
        validate(script)
    return "\n".join(e.value.problems)


def test_the_bundled_template_is_valid():
    s = validate(shotscript.example())
    assert s["name"] == "install-from-url"
    ops = [st["op"] for st in s["steps"]]
    assert ops[:3] == ["goto", "wait_for", "mark"]
    # `text` in a type step is the payload, never a target.
    typ = next(st for st in s["steps"] if st["op"] == "type")
    assert typ["text"].startswith("https://") and typ["target"] == {"placeholder": "https://github.com/…"}


def test_defaults_are_deterministic():
    s = validate(BASE)
    assert s["timezone_id"] == "UTC" and s["locale"] == "en-US" and s["color_scheme"] == "dark"
    assert s["viewport"] == {"width": 1280, "height": 800} and s["device_scale_factor"] == 2


def test_yaml_and_json_text_both_parse():
    assert validate("base_url: http://x.test\nsteps:\n  - goto: /\n")["base_url"] == "http://x.test"
    assert validate('{"steps": [{"goto": "https://x.test"}]}')["steps"][0]["url"] == "https://x.test"


def test_unknown_step_names_the_step_and_suggests():
    msg = _problems(_with({"clik": "button"}))
    assert "step 2" in msg and "unknown step `clik`" in msg and "did you mean `click`" in msg


def test_missing_target_is_reported_with_its_step():
    msg = _problems(_with({"hover": {}}, {"fill": {"value": "x"}}, {"click": {"name": "Save"}}))
    assert "step 2 (hover): needs a target" in msg
    assert "step 3 (fill)" in msg
    assert "step 4 (click)" in msg and "needs a `role`" in msg


def test_two_targets_at_once_is_ambiguous():
    assert "give ONE of" in _problems(_with({"click": {"role": "button", "selector": ".x"}}))


def test_target_shapes_normalize():
    s = validate(
        _with(
            {"click": "button.save"},
            {"click": {"role": "button", "name": "Save", "exact": True}},
            {"click": {"text": "Plugins", "nth": 1}},
            {"fill": {"label": "Email", "value": "a"}},
            {"type": {"target": {"text": "Search"}, "text": "hello"}},
        )
    )
    t = [st.get("target") for st in s["steps"][1:]]
    assert t[0] == {"selector": "button.save"}
    assert t[1] == {"role": "button", "name": "Save", "exact": True}
    assert t[2] == {"text": "Plugins", "nth": 1}
    assert t[3] == {"label": "Email"}
    assert t[4] == {"text": "Search"}


def test_nth_takes_an_index_or_first_last():
    s = validate(_with({"click": {"text": "a", "nth": "first"}}, {"click": {"text": "a", "nth": "LAST"}},
                       {"click": {"text": "a", "nth": "2"}}, {"click": {"text": "a", "nth": 0}}))  # fmt: skip
    assert [st["target"]["nth"] for st in s["steps"][1:]] == ["first", "last", 2, 0]
    for bad in (-1, "second", True, 1.5, None):
        assert "nth must be a 0-based index" in _problems(_with({"click": {"text": "a", "nth": bad}})), bad


def test_exact_and_nth_show_in_the_step_description():
    from campaign.shotscript import describe_step

    s = validate(_with({"click": {"text": "3 passed", "exact": True, "nth": "last"}}))
    assert describe_step(s["steps"][1]) == "click text='3 passed' exact nth=last"


def test_top_level_problems_are_all_reported_together():
    msg = _problems(
        {
            "base_url": "localhost:7871",
            "viewport": {"width": 50, "height": 800},
            "device_scale_factor": 9,
            "color_scheme": "sepia",
            "timezone_id": "Mars/Olympus",
            "colour_scheme": "dark",
            "steps": [{"goto": "/"}],
        }
    )
    for needle in (
        "base_url",
        "viewport.width",
        "device_scale_factor",
        "color_scheme",
        "timezone_id",
        "did you mean `color_scheme`",
    ):
        assert needle in msg, needle


def test_relative_goto_needs_a_base_url():
    assert "relative but the script has no base_url" in _problems({"steps": [{"goto": "/app"}]})


def test_script_must_navigate_first():
    assert "never navigates" in _problems({"steps": [{"hold": 100}]})
    assert "step 1 should be `goto`" in _problems(
        {"base_url": "http://x.test", "steps": [{"click": "a"}, {"goto": "/"}]}
    )


def test_literal_bearer_tokens_are_refused():
    msg = _problems(_with(auth={"bearer": "sk-abc"}))
    assert "bearer_env" in msg
    s = validate(_with(auth={"bearer_env": "MY_TOKEN", "storage_state": "~/state.json"}))
    assert s["auth"] == {"bearer_env": "MY_TOKEN", "storage_state": "~/state.json"}


def test_bounds_on_holds_and_timeouts():
    assert "outside 0..60000" in _problems(_with({"hold": 600_000}))
    assert "step_timeout_ms" in _problems(_with(step_timeout_ms=10))
    assert "over total_timeout_s" in _problems(_with({"hold": 6000}, total_timeout_s=5))


def test_marks_and_screenshots_need_unique_safe_names():
    msg = _problems(_with({"mark": "a"}, {"mark": "a"}, {"screenshot": "../etc"}))
    assert "duplicate mark" in msg and "screenshot needs a short `name`" in msg


def test_mask_and_redact_validate():
    s = validate(_with({"mask": [".secret", "#email"]}, redact={"presets": ["home_paths", "emails"]}))
    assert s["steps"][1] == {"op": "mask", "index": 2, "selectors": [".secret", "#email"], "mode": "blur"}
    assert s["redact"]["presets"] == ["home_paths", "emails"]
    msg = _problems(
        _with({"mask": {"selectors": ["a{color:red}"]}}, redact={"presets": ["homepaths"], "patterns": ["("]})
    )
    assert "no braces" in msg and "unknown redact preset" in msg and "not a valid regex" in msg


def test_wait_for_variants():
    s = validate(
        _with(
            {"wait_for": {"network_idle": True}}, {"wait_for": 250}, {"wait_for": {"text": "Done", "state": "visible"}}
        )
    )
    assert s["steps"][1]["network_idle"] is True
    assert s["steps"][2]["ms"] == 250
    assert s["steps"][3]["target"] == {"text": "Done"}


def test_resolve_url_and_descriptions():
    s = validate(BASE)
    assert shotscript.resolve_url(s, "/x") == "http://localhost:7871/x"
    assert shotscript.resolve_url(s, "https://other.test/") == "https://other.test/"
    assert (
        shotscript.describe_step({"op": "click", "target": {"role": "button", "name": "Go"}})
        == "click role='button' name='Go'"
    )


def test_garbage_input():
    assert "empty" in _problems("")
    assert "mapping" in _problems("- just\n- a list\n")
    assert "not valid YAML" in _problems("steps: [unclosed")


# ── overrides: a one-step fix without resubmitting the script ────────────────
def test_overrides_replace_a_step_or_merge_options_and_never_touch_the_original():
    raw = _with({"click": {"role": "button", "name": "Go"}}, {"wait_for": {"text": "Done"}}, {"hold": 500})
    out, changed = shotscript.apply_overrides(
        raw, {"2": {"click": {"text": "Start", "exact": True}}, 3: {"timeout_ms": 90_000}, 4: {"hold": 900}}
    )
    assert changed == [2, 3, 4]
    assert out["steps"][1] == {"click": {"text": "Start", "exact": True}}
    assert out["steps"][2] == {"wait_for": {"text": "Done", "timeout_ms": 90_000}}
    assert out["steps"][3] == {"hold": 900}
    assert raw["steps"][1] == {"click": {"role": "button", "name": "Go"}}, "the input is not mutated"
    assert validate(out)["steps"][2]["timeout_ms"] == 90_000


def test_bad_overrides_name_every_problem():
    raw = _with({"hold": 500})
    with pytest.raises(ScriptError) as e:
        shotscript.apply_overrides(raw, {0: {"hold": 1}, "x": {"hold": 1}, 2: {"ms": 900}, 1: "goto /"})
    msg = "\n".join(e.value.problems)
    assert "0 is not a step number 1..2" in msg and "'x' is not a step number" in msg
    assert "step 2: the step is `hold` written in short form" in msg
    assert "overrides step 1: give {op: {...}}" in msg
    with pytest.raises(ScriptError, match="must be a mapping"):
        shotscript.apply_overrides(raw, [])


# ── per-step timeout_ms (the 15s cap that silently ate a 17–44s agent run) ────
def test_a_step_can_ask_for_its_own_timeout_up_to_the_max():
    s = validate(
        _with(
            {"wait_for": {"text": "Done", "timeout_ms": 30_000}},
            {"click": {"role": "button", "name": "Go", "timeout_ms": 600_000}},
            total_timeout_s=900,
        )
    )
    wf, click = s["steps"][1], s["steps"][2]
    assert wf["timeout_ms"] == 30_000 and wf["target"] == {"text": "Done"}
    # 10 min: a real agent turn on screen ran past the old 3-min cap (brandLaunch, 10-01).
    assert click["timeout_ms"] == shotscript.MAX_STEP_TIMEOUT_MS == 600_000
    assert "(timeout 30000ms)" in shotscript.describe_step(wf)
    # Every waiting op takes it.
    for op, body in (
        ("goto", {"url": "/x", "timeout_ms": 20_000}),
        ("hover", {"text": "a", "timeout_ms": 20_000}),
        ("fill", {"label": "a", "value": "v", "timeout_ms": 20_000}),
        ("type", {"text": "abc", "timeout_ms": 20_000}),
        ("press", {"key": "Enter", "timeout_ms": 20_000}),
        ("scroll", {"y": 100, "timeout_ms": 20_000}),
        ("screenshot", {"name": "s", "timeout_ms": 20_000}),
        ("wait_for", {"network_idle": True, "timeout_ms": 20_000}),
    ):
        assert validate(_with({op: body}))["steps"][1]["timeout_ms"] == 20_000, op


def test_a_timeout_over_the_max_fails_validation_loudly_never_clamps():
    p = _problems(_with({"wait_for": {"text": "Done", "timeout_ms": 700_000}}, total_timeout_s=900))
    assert "step 2 (wait_for) timeout_ms: 700000ms is over the 600000ms (600s) per-step max" in p
    p = _problems(_with(step_timeout_ms=650_000, total_timeout_s=900))
    assert "step_timeout_ms: 650000ms is over the 600000ms (600s) per-step max" in p
    assert "must be an integer" in _problems(_with({"click": {"text": "x", "timeout_ms": "soon"}}))
    assert "under the 100ms minimum" in _problems(_with({"click": {"text": "x", "timeout_ms": 5}}))


def test_a_step_timeout_longer_than_the_whole_shoot_is_an_error():
    p = _problems(_with({"wait_for": {"text": "Done", "timeout_ms": 60_000}}, total_timeout_s=30))
    assert "longer than the whole shoot (total_timeout_s=30)" in p
    assert "longer than the whole shoot" in _problems(_with(step_timeout_ms=60_000, total_timeout_s=30))
    # A long per-step wait is still bounded by the whole shoot: 10 min needs total_timeout_s ≥ 600.
    p = _problems(_with({"wait_for": {"text": "Done", "timeout_ms": 600_000}}))
    assert "longer than the whole shoot (total_timeout_s=300)" in p and "max 900" in p


# ── pointer + keyboard steps: focus, mouse_move, drag, press repeat, within ───
def test_focus_mouse_move_and_drag_validate_and_describe():
    s = validate(
        _with(
            {"focus": {"role": "textbox", "name": "Message"}},
            {"focus": "#search"},
            {"mouse_move": {"x": 1200, "y": 780}},
            {"mouse_move": {"x": 0, "y": 0, "smooth": False}},
            {"drag": {"role": "separator", "name": "Resize dock", "to": {"dx": -320}}},
            {
                "drag": {
                    "target": {"selector": ".divider", "frame": TERM},
                    "to": {"dx": 10, "dy": -5},
                    "timeout_ms": 5000,
                }
            },
        )
    )
    f1, f2, m1, m2, d1, d2 = s["steps"][1:]
    assert f1["target"] == {"role": "textbox", "name": "Message"} and f2["target"] == {"selector": "#search"}
    assert (m1["x"], m1["y"], m1["smooth"]) == (1200, 780, True) and m2["smooth"] is False
    assert (d1["dx"], d1["dy"]) == (-320, 0) and d1["target"] == {"role": "separator", "name": "Resize dock"}
    assert d2["target"]["frame"] == {"url": TERM} and d2["timeout_ms"] == 5000
    assert shotscript.describe_step(f1) == "focus role='textbox' name='Message'"
    assert shotscript.describe_step(m1) == "move the pointer to (1200, 780)"
    assert shotscript.describe_step(d1) == "drag role='separator' name='Resize dock' by (-320, +0)"


def test_pointer_steps_reject_bad_shapes():
    assert "needs {x, y}" in _problems(_with({"mouse_move": {"x": 10}}))
    p = _problems(_with({"mouse_move": {"x": 1280, "y": 10}}))
    assert "step 2 (mouse_move): (1280, 10) is outside the 1280×800 viewport" in p
    assert "unknown option `steps`" in _problems(_with({"mouse_move": {"x": 1, "y": 1, "steps": 3}}))
    assert "needs a target to grab and `to: {dx, dy}`" in _problems(_with({"drag": {"text": "x"}}))
    assert "`to` must be {dx: px, dy: px}" in _problems(_with({"drag": {"text": "x", "to": {"x": 4}}}))
    assert "moves nowhere" in _problems(_with({"drag": {"text": "x", "to": {"dx": 0}}}))
    assert "is outside -4000..4000" in _problems(_with({"drag": {"text": "x", "to": {"dy": 9000}}}))
    assert "needs a target" in _problems(_with({"drag": {"to": {"dx": 5}}}))
    assert "needs a target" in _problems(_with({"focus": {}}))


def test_press_repeat_and_its_gap():
    s = validate(_with({"press": {"key": "ArrowDown", "repeat": 5}}, {"press": "Enter"},
                       {"press": {"key": "Tab", "repeat": 3, "delay_ms": 0, "role": "textbox", "name": "Q"}}))  # fmt: skip
    p5, p1, p3 = s["steps"][1:]
    assert (p5["repeat"], p5["delay_ms"]) == (5, shotscript.DEFAULT_PRESS_GAP_MS)
    assert p1["repeat"] == 1 and p3["delay_ms"] == 0 and p3["target"] == {"role": "textbox", "name": "Q"}
    assert shotscript.describe_step(p5) == "press ArrowDown ×5" and shotscript.describe_step(p1) == "press Enter"
    assert "outside 1..200" in _problems(_with({"press": {"key": "a", "repeat": 0}}))
    assert "outside 1..200" in _problems(_with({"press": {"key": "a", "repeat": 1000}}))
    # The gaps count against the shoot's budget like holds do.
    p = _problems(_with({"press": {"key": "a", "repeat": 200, "delay_ms": 5000}}, total_timeout_s=60))
    assert "holds/waits add up to" in p


def test_within_scopes_click_hover_focus_and_wait_for_to_a_container():
    s = validate(
        _with(
            {"click": {"role": "button", "name": "Save", "within": {"role": "dialog", "name": "Settings"}}},
            {"wait_for": {"text": "Saved", "within": ".toast", "frame": TERM}},
            {"hover": {"text": "row", "within": {"test_id": "list", "nth": "last"}}},
            {"focus": {"role": "textbox", "within": "form#login"}},
        )
    )
    click, wait, hover, focus = s["steps"][1:]
    assert click["within"] == {"role": "dialog", "name": "Settings"} and click["target"] == {
        "role": "button",
        "name": "Save",
    }
    assert wait["within"] == {"selector": ".toast"} and wait["target"]["frame"] == {"url": TERM}
    assert hover["within"] == {"test_id": "list", "nth": "last"} and focus["within"] == {"selector": "form#login"}
    assert shotscript.describe_step(click) == "click role='button' name='Save' within role='dialog' name='Settings'"
    assert "within selector='.toast'" in shotscript.describe_step(wait)


def test_within_errors_name_the_problem():
    assert "this step has none to scope" in _problems(_with({"wait_for": {"ms": 100, "within": ".x"}}))
    assert "put `frame` on the step or its target" in _problems(
        _with({"click": {"text": "Go", "within": {"selector": ".x", "frame": TERM}}})
    )
    assert "unknown option `within`" in _problems(_with({"fill": {"label": "a", "value": "v", "within": ".x"}}))
    assert "within: needs one of" in _problems(_with({"click": {"text": "Go", "within": {"nth": 1}}}))


def test_unknown_step_options_are_errors_not_dropped():
    p = _problems(_with({"wait_for": {"text": "Done", "timout_ms": 30_000}}))
    assert "unknown option `timout_ms` (did you mean `timeout_ms`?)" in p
    assert "did you mean `timeout_ms`" in _problems(_with({"click": {"text": "Go", "timeout": 30}}))
    assert "unknown option `timeout_ms`" in _problems(_with({"hold": {"ms": 100, "timeout_ms": 5000}}))
    assert "a fixed `ms` wait doesn't take one" in _problems(_with({"wait_for": {"ms": 500, "timeout_ms": 5000}}))


# ── frames ────────────────────────────────────────────────────────────────────
TERM = "/plugins/terminal/view"


def test_frame_on_the_step_or_the_target_normalizes_into_the_target():
    s = validate(
        _with(
            {"wait_for": {"text": "connected", "frame": {"url": TERM}, "timeout_ms": 30_000}},
            {"type": {"target": {"selector": "textarea", "frame": TERM}, "text": "ls"}},
            {"click": {"target": "#go", "frame": {"selector": "iframe[title='Terminal']"}}},
            {"press": {"role": "textbox", "key": "Enter", "frame": {"url": "*/plugins/*/view*"}}},
            {"hover": {"text": "x", "frame": {"selector": "iframe.outer", "frame": {"url": "inner.html"}}}},
        )
    )
    t = [st["target"] for st in s["steps"][1:]]
    assert t[0] == {"text": "connected", "frame": {"url": TERM}}
    assert t[1] == {"selector": "textarea", "frame": {"url": TERM}}, "a bare string is a url"
    assert t[2]["frame"] == {"selector": "iframe[title='Terminal']"}
    assert t[3]["frame"] == {"url": "*/plugins/*/view*"}
    assert t[4]["frame"] == {"selector": "iframe.outer", "frame": {"url": "inner.html"}}
    assert "in frame(url='/plugins/terminal/view')" in shotscript.describe_step(s["steps"][1])


def test_a_frame_alone_is_a_wait_a_screenshot_or_a_scroll_of_that_frame():
    s = validate(
        _with(
            {"wait_for": {"frame": TERM}},
            {"screenshot": {"name": "term", "frame": {"url": TERM}}},
            {"scroll": {"y": 300, "frame": TERM}},
        )
    )
    w, shot, sc = s["steps"][1:]
    assert w["frame"] == {"url": TERM} and not w.get("target")
    assert shot["frame"] == {"url": TERM} and not shot["target"]
    assert sc["frame"] == {"url": TERM} and sc["y"] == 300
    assert shotscript.describe_step(w) == "wait for frame(url='/plugins/terminal/view')"


def test_unknown_frame_keys_and_bad_frames_error_loudly():
    msg = _problems(
        _with(
            {"click": {"text": "x", "frame": {"src": TERM}}},
            {"click": {"text": "x", "frame": {"url": TERM, "name": "t"}}},
            {"click": {"text": "x", "frame": {}}},
            {"click": {"text": "x", "frame": 3}},
            {"click": {"text": "x", "frame": {"url": "a", "frame": {"url": "b", "frame": {"url": "c"}}}}},
            {"click": {"target": {"text": "x", "frame": TERM}, "frame": TERM}},
            {"type": {"text": "ls", "frame": TERM}},
            {"press": {"key": "Enter", "frame": TERM}},
            {"mask": {"selectors": [".x"], "frame": TERM}},
            {"click": {"target": {"text": "x", "frmae": TERM}}},
        )
    )
    assert "step 2 (click) frame: unknown frame key `src`" in msg
    assert "step 3 (click) frame: unknown frame key `name`" in msg
    assert "step 4 (click) frame: needs a `url`" in msg
    assert "step 5 (click) frame: must be a URL string" in msg
    assert "nest at most 2 deep" in msg
    assert "step 7 (click): `frame` is given twice" in msg
    assert "step 8 (type): `frame` needs a target" in msg
    assert "step 9 (press): `frame` needs a target" in msg
    assert "step 10 (mask): unknown option `frame`" in msg and "reaches into every frame" in msg
    assert "step 11 (click): unknown target key `frmae`" in msg and "did you mean `frame`" in msg


def test_top_level_mask_and_redact_reject_unknown_options():
    msg = _problems(_with(mask={"selectors": [".x"], "frame": TERM}, redact={"presets": ["emails"], "mode": "x"}))
    assert "mask: unknown option `frame`" in msg and "redact: unknown option `mode`" in msg


def test_the_template_lists_every_step_op():
    tpl = shotscript.example()
    for op in shotscript.STEP_OPS:
        assert op in tpl.split("# Every step op", 1)[1], op
    assert validate(tpl)["name"] == "install-from-url"
