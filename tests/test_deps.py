"""Setup probing, banners, and the setup steps — never installs on its own.

Each banner must name exactly what's missing: provision the runtime / Install dependencies /
Install Chromium / ffmpeg / a bad interpreter setting.
"""

from __future__ import annotations

import subprocess

import pytest
from campaign import deps, interpreter
from campaign.interpreter import Resolution

READY = Resolution(python="/rt/bin/python3", source=interpreter.SOURCE_MANAGED, playwright="1.50.0", chromium=True)
NO_CHROMIUM = Resolution(
    python="/rt/bin/python3", source=interpreter.SOURCE_MANAGED, playwright="1.50.0", need="chromium"
)


@pytest.fixture
def state(monkeypatch):
    """Drive deps through a chosen Resolution (and ffmpeg presence)."""
    box = {"res": READY}
    monkeypatch.setattr(deps, "resolve", lambda: box["res"])
    monkeypatch.setattr(deps, "ffmpeg", lambda: "/usr/bin/ffmpeg")
    deps._INSTALL.update(state="idle", error="")
    return box


def test_frozen_without_a_runtime_says_provision_it_first(registry, state, monkeypatch):
    monkeypatch.setattr(interpreter, "frozen", lambda: True)
    state["res"] = Resolution(need="runtime")
    deps.report(registry)
    msg, actions = registry.gaps["playwright"]
    assert "Provision the Python runtime (Settings ▸ Tools)" in msg
    assert "Install dependencies" in msg and "Install Chromium" in msg
    assert [a["kind"] for a in actions] == ["global_settings", "install_deps", "plugin_setup"]
    assert actions[0]["target"] == "tools" and actions[2]["step"] == deps.STEP_RECHECK
    assert "chromium" not in registry.gaps and "ffmpeg" not in registry.gaps
    assert "Settings ▸ Tools" in deps.need_browser()


def test_frozen_runtime_without_playwright_says_install_dependencies(registry, state, monkeypatch):
    monkeypatch.setattr(interpreter, "frozen", lambda: True)
    state["res"] = Resolution(python="/rt/bin/python3", source=interpreter.SOURCE_MANAGED, need="deps")
    deps.report(registry)
    msg, actions = registry.gaps["playwright"]
    assert "managed Python runtime" in msg and "Install dependencies" in msg
    assert actions[0] == {"kind": "install_deps", "label": "Install dependencies"}
    assert "managed Python runtime doesn't have Playwright" in deps.need_browser()


def test_source_install_without_playwright_says_install_dependencies(registry, state, monkeypatch):
    monkeypatch.setattr(interpreter, "frozen", lambda: False)
    state["res"] = Resolution(need="deps")
    deps.report(registry)
    msg, _ = registry.gaps["playwright"]
    assert "the agent's Python" in msg
    assert "Install dependencies" in deps.need_browser()


def test_a_bad_interpreter_setting_points_at_the_setting(registry, state):
    state["res"] = Resolution(
        python="/nope", source=interpreter.SOURCE_CONFIG, need="interpreter", detail="/nope doesn't exist"
    )
    deps.report(registry)
    msg, actions = registry.gaps["playwright"]
    assert "/nope doesn't exist" in msg and actions[0]["fields"] == ["interpreter"]


def test_missing_chromium_offers_the_setup_step(registry, state):
    state["res"] = NO_CHROMIUM
    deps.report(registry)
    assert "playwright" not in registry.gaps
    msg, action = registry.gaps["chromium"]
    assert action == {"kind": "plugin_setup", "step": deps.STEP_INSTALL_CHROMIUM, "label": "Install Chromium"}
    assert "managed Python runtime" in msg
    reason = deps.need_browser()
    assert "Install Chromium" in reason and "/rt/bin/python3 -m playwright install chromium" in reason


def test_missing_ffmpeg_hints_and_offers_the_path_setting(registry, state, monkeypatch):
    monkeypatch.setattr(deps, "ffmpeg", lambda: None)
    deps.report(registry)
    msg, action = registry.gaps["ffmpeg"]
    assert "install it with" in msg and action["fields"] == ["ffmpeg_path"]


def test_gaps_self_clear_when_fixed(registry, state, monkeypatch):
    state["res"] = Resolution(need="deps")
    monkeypatch.setattr(deps, "ffmpeg", lambda: None)
    deps.report(registry)
    assert set(registry.gaps) == {"playwright", "ffmpeg"}
    state["res"] = READY
    monkeypatch.setattr(deps, "ffmpeg", lambda: "/x/ffmpeg")
    deps.report(registry)
    assert registry.gaps == {}


def test_a_tool_call_resyncs_the_banner_after_install_dependencies(registry, state):
    # core's install-deps doesn't reload the plugin, so the next probe must clear the banner.
    state["res"] = Resolution(need="deps")
    deps.report(registry)
    assert "playwright" in registry.gaps
    state["res"] = READY
    assert deps.need_browser() is None
    assert registry.gaps == {}


def test_check_again_reprobes(registry, state, monkeypatch):
    calls = []
    monkeypatch.setattr(interpreter, "invalidate", lambda: calls.append("inv"))
    out = deps.recheck(lambda: deps.report(registry))
    assert calls == ["inv"] and out["ok"] and "Ready" in out["message"] and "/rt/bin/python3" in out["message"]


def test_install_step_uses_the_worker_interpreter(state):
    state["res"] = NO_CHROMIUM
    seen, refreshed = [], []

    def runner(python, args):
        seen.append((python, args))
        return subprocess.CompletedProcess(args, 0, "", "")

    out = deps.install_chromium(lambda: refreshed.append(1), runner=runner)
    assert out["ok"] and out["pending"] and "/rt/bin/python3" in out["message"]
    deps._LAST_THREAD.join(30)
    assert not deps._LAST_THREAD.is_alive(), "the install thread finished"
    assert seen == [("/rt/bin/python3", ["-m", "playwright", "install", "chromium"])]
    assert deps._INSTALL["state"] == "done" and len(refreshed) >= 2


def test_install_failure_lands_on_the_banner(registry, state):
    state["res"] = NO_CHROMIUM
    out = deps.install_chromium(None, runner=lambda py, a: subprocess.CompletedProcess(a, 1, "", "disk full"))
    deps._LAST_THREAD.join(30)
    assert not deps._LAST_THREAD.is_alive(), "the install thread finished"
    assert out["pending"] and deps._INSTALL["state"] == "failed"
    deps.report(registry)
    assert "disk full" in registry.gaps["chromium"][0]


def test_an_install_that_overruns_is_reported(state):
    state["res"] = NO_CHROMIUM
    done = interpreter.Completed(returncode=-9, stdout="", stderr="", timed_out=True)
    deps.install_chromium(None, runner=lambda py, a: done)
    deps._LAST_THREAD.join(30)
    assert not deps._LAST_THREAD.is_alive(), "the install thread finished"
    assert deps._INSTALL["state"] == "failed" and "overran" in deps._INSTALL["error"]


@pytest.mark.parametrize("need", ["runtime", "deps", "interpreter"])
def test_install_refuses_without_a_python_that_has_playwright(state, need):
    state["res"] = Resolution(need=need, detail="x")
    out = deps.install_chromium(runner=lambda *a: pytest.fail("must not run"))
    assert out["ok"] is False and out["message"]


def test_ffmpeg_path_override(tmp_path):
    ff = tmp_path / "ffmpeg"
    ff.write_text("")
    (tmp_path / "ffprobe").write_text("")
    deps.configure(str(ff))
    assert deps.ffmpeg() == str(ff) and deps.ffprobe() == str(tmp_path / "ffprobe")


def test_brief_names_the_interpreter_and_what_was_considered(state):
    state["res"] = Resolution(
        python="/rt/bin/python3",
        source=interpreter.SOURCE_MANAGED,
        playwright="1.50.0",
        chromium=True,
        considered=["managed Python runtime: /rt/bin/python3"],
    )
    out = deps.brief()
    assert "/rt/bin/python3 (managed Python runtime)" in out and "playwright: 1.50.0" in out


@pytest.mark.parametrize("need", ["runtime", "deps", "interpreter", "chromium"])
def test_banner_messages_fit_the_hosts_300_char_cap(registry, state, monkeypatch, need):
    # core truncates a gap message past 300 chars — the fix must never be the part cut off.
    monkeypatch.setattr(interpreter, "frozen", lambda: True)
    state["res"] = Resolution(python="/" + "p" * 120, source=interpreter.SOURCE_CONFIG, need=need, detail="d" * 400)
    deps._INSTALL.update(state="idle", error="")
    deps.report(registry)
    for msg, _ in registry.gaps.values():
        assert len(msg) <= 300 and "**" not in msg, msg
