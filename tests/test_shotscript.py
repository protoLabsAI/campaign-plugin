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


# ── per-step timeout_ms (the 15s cap that silently ate a 17–44s agent run) ────
def test_a_step_can_ask_for_its_own_timeout_up_to_the_max():
    s = validate(
        _with(
            {"wait_for": {"text": "Done", "timeout_ms": 30_000}},
            {"click": {"role": "button", "name": "Go", "timeout_ms": 180_000}},
        )
    )
    wf, click = s["steps"][1], s["steps"][2]
    assert wf["timeout_ms"] == 30_000 and wf["target"] == {"text": "Done"}
    assert click["timeout_ms"] == shotscript.MAX_STEP_TIMEOUT_MS == 180_000
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
    p = _problems(_with({"wait_for": {"text": "Done", "timeout_ms": 300_000}}))
    assert "step 2 (wait_for) timeout_ms: 300000ms is over the 180000ms (180s) per-step max" in p
    p = _problems(_with(step_timeout_ms=200_000))
    assert "step_timeout_ms: 200000ms is over the 180000ms (180s) per-step max" in p
    assert "must be an integer" in _problems(_with({"click": {"text": "x", "timeout_ms": "soon"}}))
    assert "under the 100ms minimum" in _problems(_with({"click": {"text": "x", "timeout_ms": 5}}))


def test_a_step_timeout_longer_than_the_whole_shoot_is_an_error():
    p = _problems(_with({"wait_for": {"text": "Done", "timeout_ms": 60_000}}, total_timeout_s=30))
    assert "longer than the whole shoot (total_timeout_s=30)" in p
    assert "longer than the whole shoot" in _problems(_with(step_timeout_ms=60_000, total_timeout_s=30))


def test_unknown_step_options_are_errors_not_dropped():
    p = _problems(_with({"wait_for": {"text": "Done", "timout_ms": 30_000}}))
    assert "unknown option `timout_ms` (did you mean `timeout_ms`?)" in p
    assert "did you mean `timeout_ms`" in _problems(_with({"click": {"text": "Go", "timeout": 30}}))
    assert "unknown option `timeout_ms`" in _problems(_with({"hold": {"ms": 100, "timeout_ms": 5000}}))
    assert "a fixed `ms` wait doesn't take one" in _problems(_with({"wait_for": {"ms": 500, "timeout_ms": 5000}}))
