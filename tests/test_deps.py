"""Setup probing, banners, and the Install Chromium step — never installs on its own."""

from __future__ import annotations

import subprocess

from campaign import deps


def test_missing_playwright_raises_an_install_deps_banner(registry, monkeypatch):
    monkeypatch.setattr(deps, "playwright_installed", lambda: False)
    monkeypatch.setattr(deps, "ffmpeg", lambda: "/usr/bin/ffmpeg")
    deps.report(registry)
    msg, action = registry.gaps["playwright"]
    assert action == {"kind": "install_deps", "label": "Install dependencies"}
    assert "chromium" not in registry.gaps and "ffmpeg" not in registry.gaps
    assert "Install dependencies" in deps.need_browser()


def test_missing_chromium_offers_the_setup_step(registry, monkeypatch):
    monkeypatch.setattr(deps, "playwright_installed", lambda: True)
    monkeypatch.setattr(deps, "chromium_installed", lambda: False)
    deps.report(registry)
    _, action = registry.gaps["chromium"]
    assert action == {"kind": "plugin_setup", "step": deps.STEP_INSTALL_CHROMIUM, "label": "Install Chromium"}
    assert "Install Chromium" in deps.need_browser()


def test_missing_ffmpeg_hints_and_offers_the_path_setting(registry, monkeypatch):
    monkeypatch.setattr(deps, "ffmpeg", lambda: None)
    deps.report(registry)
    msg, action = registry.gaps["ffmpeg"]
    assert "install it with" in msg and action["fields"] == ["ffmpeg_path"]


def test_gaps_self_clear_when_fixed(registry, monkeypatch):
    monkeypatch.setattr(deps, "playwright_installed", lambda: False)
    monkeypatch.setattr(deps, "ffmpeg", lambda: None)
    deps.report(registry)
    assert set(registry.gaps) == {"playwright", "ffmpeg"}
    monkeypatch.setattr(deps, "playwright_installed", lambda: True)
    monkeypatch.setattr(deps, "chromium_installed", lambda: True)
    monkeypatch.setattr(deps, "ffmpeg", lambda: "/x/ffmpeg")
    deps.report(registry)
    assert registry.gaps == {}


def test_install_step_runs_in_the_background_and_reports(monkeypatch):
    monkeypatch.setattr(deps, "playwright_installed", lambda: True)
    monkeypatch.setattr(deps, "chromium_installed", lambda: False)
    deps._INSTALL.update(state="idle", error="")
    seen, refreshed = [], []

    def runner(cmd):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    out = deps.install_chromium(lambda: refreshed.append(1), runner=runner)
    assert out["ok"] and out["pending"] and "_thread" not in out
    deps._LAST_THREAD.join(5)
    assert seen and seen[0][1:] == ["-m", "playwright", "install", "chromium"]
    assert deps._INSTALL["state"] == "done" and len(refreshed) >= 2


def test_install_failure_lands_on_the_banner(registry, monkeypatch):
    monkeypatch.setattr(deps, "playwright_installed", lambda: True)
    monkeypatch.setattr(deps, "chromium_installed", lambda: False)
    deps._INSTALL.update(state="idle", error="")
    out = deps.install_chromium(None, runner=lambda cmd: subprocess.CompletedProcess(cmd, 1, "", "disk full"))
    deps._LAST_THREAD.join(5)
    assert out["pending"] and deps._INSTALL["state"] == "failed"
    deps.report(registry)
    assert "disk full" in registry.gaps["chromium"][0]


def test_install_refuses_without_playwright(monkeypatch):
    monkeypatch.setattr(deps, "playwright_installed", lambda: False)
    assert deps.install_chromium()["ok"] is False


def test_chromium_probe_reads_the_expected_revision(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setattr(deps, "_expected_revisions", lambda: {"chromium-headless-shell": "1243"})
    assert deps.chromium_installed() is False
    (tmp_path / "chromium_headless_shell-1200").mkdir()
    assert deps.chromium_installed() is False, "a stale revision doesn't count"
    (tmp_path / "chromium_headless_shell-1243").mkdir()
    assert deps.chromium_installed() is True


def test_ffmpeg_path_override(tmp_path):
    ff = tmp_path / "ffmpeg"
    ff.write_text("")
    (tmp_path / "ffprobe").write_text("")
    deps.configure(str(ff))
    assert deps.ffmpeg() == str(ff) and deps.ffprobe() == str(tmp_path / "ffprobe")
