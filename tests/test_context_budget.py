"""What the campaign tools put back into the agent's context (#48).

brandLaunch's night of takes grew one thread from 64k to 478k prompt tokens: shoot replies that
quoted the same error three times and every path twice, 42 full-YAML script saves, 36
hand-written ``browser_eval`` cleanups, page after page of contact sheets. These pin the
compact replies, the save-once/iterate-by-id path, and ``campaign_sandbox_reset`` with its
guards."""

from __future__ import annotations

import contextlib
import json
from urllib.parse import urlsplit

import campaign
import pytest
from campaign import produce, sandbox, shoot, shotscript, store
from campaign.worker import pw_worker
from test_tools import SCRIPT, call
from test_tools import browser_ok as browser_ok  # noqa: F401 — fixtures
from test_tools import ffmpeg_ok as ffmpeg_ok  # noqa: F401
from test_tools import tools as tools  # noqa: F401

LONG_ERROR = (
    'Locator.click: Timeout 15000ms exceeded.\nCall log:\n  - waiting for get_by_role("button", name="Go")\n'
    + "  - locator resolved to <button>…</button>\n" * 20
)


# ── campaign_shoot ─────────────────────────────────────────────────────────────
def test_a_shoot_reply_is_a_short_summary_with_one_directory(tools, browser_ok, ffmpeg_ok):  # noqa: F811
    call(tools, "campaign_create", name="L")
    out = call(tools, "campaign_shoot", campaign_id=1, script=SCRIPT)
    take = store.get_asset(1)
    take_dir = take["meta"]["take_dir"]
    assert out.startswith("Recorded take → asset #1 (") and "from script #1" in out, out
    assert out.count(take_dir) == 1, "the take dir is named once; the files are named relative to it"
    assert "(hero.webm, timing.json)" in out and "- marks: start=" in out and "- stills: #2" in out
    assert "campaign_render(asset_id=1" in out and "overrides" in out, "the next call, and the iteration path"
    body = out.split("\n\n![", 1)[0]  # the embedded still is for the chat, not the model's reading
    assert len(body) < 520 + len(take_dir), body
    # The detail the reply leaves out is on the asset.
    assert take["meta"]["timing"].endswith("timing.json") and take["meta"]["marks"]


def test_a_failed_shoot_quotes_the_error_once_without_its_call_log(tools, browser_ok, ffmpeg_ok, monkeypatch):  # noqa: F811
    real = pw_worker.run_job

    def lengthen(job, factory=None):  # a real Playwright error carries a long call log
        rep = real(job, factory)
        if rep["kind"] == "shoot" and rep["error"]:
            rep["error"] = rep["error"].split(":", 1)[0] + ": " + LONG_ERROR
            for st in rep["result"].get("steps", []):
                if st.get("error"):
                    st["error"] = LONG_ERROR
        return rep

    monkeypatch.setattr(
        shoot, "run_worker", lambda job, timeout, f=None: lengthen(json.loads(json.dumps(job)), browser_ok)
    )
    browser_ok.fail_on = lambda action, how, args: action == "click"
    call(tools, "campaign_create", name="L")
    out = call(tools, "campaign_shoot", campaign_id=1, script=SCRIPT)
    assert out.startswith("Take failed — "), out
    assert out.count("Timeout 15000ms exceeded") == 1, "the error once — not in the header, the steps AND the note"
    assert "Call log" not in out and "locator resolved" not in out
    assert "- steps before it: 1. goto / @" in out and "2. mark start" in out
    assert "Footage KEPT → asset #1 [captured]" in out and "marks reached: start=" in out
    assert "failure.png" in out and out.count(store.get_asset(1)["meta"]["take_dir"]) == 1
    assert "overrides={3: {...}}" in out and "script_id=1" in out and "don't resend the script" in out
    assert "campaign_asset(1)" in out
    body = out.split("\n\n![", 1)[0]
    assert len(body) < 900 + len(store.get_asset(1)["meta"]["take_dir"]), body
    # The full error — call log and all — is kept on the asset.
    failed = store.get_asset(1)["meta"]["failed"]
    assert "locator resolved" in failed["error"] and failed["failure_png"].endswith("failure.png")
    assert "locator resolved" in call(tools, "campaign_asset", asset_id=1)


# ── campaign_render ────────────────────────────────────────────────────────────
def test_a_render_reply_names_one_dir_and_one_line_per_output(tools, browser_ok, ffmpeg_ok):  # noqa: F811
    call(tools, "campaign_create", name="L")
    call(tools, "campaign_shoot", campaign_id=1, script=SCRIPT)
    out = call(
        tools,
        "campaign_render",
        asset_id=1,
        outputs=[
            {"name": "hero", "format": "mp4", "start": 0, "end": 9, "bookends": "none",
             "speed": [{"from": 1, "to": 8, "factor": 3}]},
            {"name": "poster", "format": "poster", "at": 1},
        ],
    )  # fmt: skip
    first, *rows = out.splitlines()
    assert first.startswith("Rendered into ") and first.endswith(":"), out
    out_dir = first.removeprefix("Rendered into ").rstrip(":")
    assert out.count(out_dir) == 1, "the render dir once; outputs relative to it"
    assert any(r.startswith("- #3 hero.mp4 — ") for r in rows) and any(r.startswith("- #4 poster.png — ") for r in rows)
    assert "attempt" not in out, "attempts stay on the asset"
    warns = [r for r in rows if r.lstrip().startswith("⚠")]
    assert warns and all(len(w) < 220 for w in warns), "one short line per warning"
    assert store.get_asset(3)["meta"]["attempts"], "the attempt ladder is on the asset"


def test_rendering_from_a_failed_take_names_the_step_not_the_whole_error(tools, browser_ok, ffmpeg_ok):  # noqa: F811
    browser_ok.fail_on = lambda action, how, args: action == "click"
    call(tools, "campaign_create", name="L")
    call(tools, "campaign_shoot", campaign_id=1, script=SCRIPT)
    out = call(tools, "campaign_render", asset_id=1, outputs=[{"name": "a", "format": "mp4", "end": 4.0}])
    assert out.startswith("Rendering from a FAILED take (FAILED at step 3) — only footage"), out
    assert "Timeout" not in out


# ── save once, iterate by id ───────────────────────────────────────────────────
def test_saving_the_same_script_again_is_unchanged_and_points_at_overrides(tools):  # noqa: F811
    call(tools, "campaign_create", name="L")
    first = call(tools, "campaign_script_save", campaign_id=1, script=SCRIPT)
    assert first.startswith("Saved shot script #1 'hero'") and "overrides" in first and "```" not in first
    again = call(tools, "campaign_script_save", campaign_id=1, script=SCRIPT)
    assert again.startswith("Unchanged — shot script #1 'hero'") and "overrides" in again
    changed = call(tools, "campaign_script_save", campaign_id=1, script=SCRIPT.replace("/\n", "/app\n", 1))
    assert "#1 'hero'" in changed and "replacing the earlier version" in changed
    assert len(store.scripts(1)) == 1


def test_script_get_can_show_just_the_steps_being_fixed(tools):  # noqa: F811
    call(tools, "campaign_create", name="L")
    call(tools, "campaign_script_save", campaign_id=1, script=SCRIPT)
    whole = call(tools, "campaign_script_get", script_id=1)
    part = call(tools, "campaign_script_get", script_id=1, steps="3-4")
    assert "# step 1" in whole and "# step 5" in whole
    assert "steps 3–4 of 5" in part and "# step 3" in part and "# step 4" in part
    assert "# step 2" not in part and "# step 5" not in part and "base_url: http://app.test" in part
    assert len(part) < len(whole)
    assert "steps must be" in call(tools, "campaign_script_get", script_id=1, steps="x")
    assert "past the end" in call(tools, "campaign_script_get", script_id=1, steps="9")


def test_a_top_level_override_points_the_same_script_at_another_sandbox():
    raw = {"name": "t", "base_url": "http://127.0.0.1:7954", "steps": [{"goto": "/"}, {"hold": 500}]}
    out, changed = shotscript.apply_overrides(
        raw, {"base_url": "http://127.0.0.1:7951", "color_scheme": "light", 2: {"hold": 900}}
    )
    assert changed == [2, "base_url", "color_scheme"]
    assert out["base_url"] == "http://127.0.0.1:7951" and out["color_scheme"] == "light"
    assert raw["base_url"] == "http://127.0.0.1:7954", "the input is not mutated"
    with pytest.raises(shotscript.ScriptError) as e:
        shotscript.apply_overrides(raw, {"name": "other", "steps": []})
    assert all("top-level setting" in p for p in e.value.problems), "name and steps aren't overridable"


def test_shoot_with_a_base_url_override_saves_it_on_the_same_script(tools, browser_ok, ffmpeg_ok):  # noqa: F811
    call(tools, "campaign_create", name="L")
    call(tools, "campaign_script_save", campaign_id=1, script=SCRIPT)
    out = call(tools, "campaign_shoot", campaign_id=1, script_id=1, overrides={"base_url": "http://other.test"})
    assert "Recorded take" in out, out
    assert store.get_script(1)["body"]["base_url"] == "http://other.test" and len(store.scripts(1)) == 1


# ── campaign_video_status ──────────────────────────────────────────────────────
def test_video_status_is_compact_and_verbose_has_the_whole_log():
    files = {k: f"/data/campaigns/1-videos/videos/vid-1-a1/{n}" for k, n in
             (("final", "v.mp4"), ("gh", "v-gh.mp4"))}  # fmt: skip
    files["sheet"] = "/data/campaigns/1-videos/videos/vid-1-a1/review/contact-sheet.png"
    job = {
        "id": "vid-1", "status": "done", "stage": "delivering", "attempt": 1,
        "inputs": {"title": "T", "quality": "2k", "intro": "brand", "outro": "brand"},
        "gates": [{"ok": True, "label": "final caption ≥ 4 s", "detail": "x" * 400}],
        "delivery": {"files": files, "assets": {"final": 2}, "links": {"final": "/media/v.mp4?sig=x"}},
        "log": [f"10:00:{i:02d} step {i} " + "ffmpeg -i … " * 30 for i in range(40)],
    }  # fmt: skip
    out = produce.summary(job)
    assert "Files (in /data/campaigns/1-videos/videos/vid-1-a1):" in out and "- sheet: review/contact-sheet.png" in out
    assert "- final: v.mp4 (asset #2) link: /media/v.mp4?sig=x" in out
    assert "Recent (of 40; verbose=true for all):" in out and "step 39" in out and "step 36 " not in out
    assert all(len(ln) <= 200 for ln in out.splitlines()), "every line clipped"
    full = produce.summary(job, verbose=True)
    assert "step 0 " in full and "step 39" in full and len(full) > 3 * len(out)


# ── campaign_sandbox_reset ─────────────────────────────────────────────────────
class FakeConsole:
    """A protoAgent console's chat/artifact/goal/theme routes, in memory."""

    def __init__(self, origin="http://127.0.0.1:7944", token=""):
        self.origin, self.token = origin, token
        self.sessions = [{"id": f"chat-{i}"} for i in range(3)]
        self.artifacts = [{"id": "a-1"}, {"id": "a-2"}]
        self.goals = [{"session_id": "chat-0"}]
        self.theme = None
        self.calls: list[tuple] = []

    def factory(self):
        console = self

        class Resp:
            def __init__(self, status, body=None):
                self.status, self._body = status, body

            def json(self):
                return self._body

        class Ctx:
            def fetch(self, url, method="GET", headers=None, data=None, **kw):
                console.calls.append((method, url, dict(headers or {}), kw.get("max_redirects")))
                u = urlsplit(url)
                if f"{u.scheme}://{u.netloc}" != console.origin:
                    raise RuntimeError("connect ECONNREFUSED")
                if console.token and (headers or {}).get("Authorization") != f"Bearer {console.token}":
                    return Resp(401, {"detail": "unauthorized"})
                p = u.path
                rows = {"/api/chat/sessions": "sessions", "/api/plugins/artifact/history": "artifacts",
                        "/api/goals": "goals"}  # fmt: skip
                if method == "GET" and p in rows:
                    return Resp(200, {rows[p]: list(getattr(console, rows[p]))})
                if method == "DELETE":
                    for prefix, attr, key in (("/api/chat/sessions/", "sessions", "id"),
                                              ("/api/plugins/artifact/artifact/", "artifacts", "id"),
                                              ("/api/goals/", "goals", "session_id")):  # fmt: skip
                        if p.startswith(prefix):
                            rid = p[len(prefix) :]
                            setattr(console, attr, [r for r in getattr(console, attr) if r[key] != rid])
                            return Resp(200, {"ok": True})
                if method == "PUT" and p == "/api/theme":
                    console.theme = data["theme"]
                    return Resp(200, {"ok": True})
                return Resp(404, {"detail": "not found"})

            def dispose(self):
                pass

        class PW:
            class request:  # noqa: N801 — mirrors playwright's attribute
                @staticmethod
                def new_context():
                    return Ctx()

        return lambda: contextlib.nullcontext(PW())


@pytest.fixture
def console(registry, monkeypatch):
    c = FakeConsole()
    monkeypatch.setattr(campaign.deps, "need_browser", lambda: None)
    monkeypatch.setattr(
        shoot, "run_worker", lambda job, timeout, f=None: pw_worker.run_job(json.loads(json.dumps(job)), c.factory())
    )
    monkeypatch.setattr(produce, "own_console_urls", lambda: ["http://127.0.0.1:7953"])
    campaign.register(registry)
    call(registry, "campaign_create", name="L")
    return c, registry


def _save(reg, base_url, name="beat", **extra):
    script = {"name": name, "base_url": base_url, "steps": [{"goto": "/app/"}], **extra}
    return call(reg, "campaign_script_save", campaign_id=1, script=json.dumps(script))


def test_sandbox_reset_clears_the_console_a_script_records(console):
    c, reg = console
    _save(reg, "http://127.0.0.1:7944", color_scheme="dark")
    out = call(reg, "campaign_sandbox_reset", campaign_id=1, script_id=1, theme="gruvbox")
    assert out.startswith("Sandbox http://127.0.0.1:7944 (script #1) reset: "), out
    assert "sessions 3 cleared" in out and "artifacts 2 cleared" in out and "goals 1 cleared" in out
    assert c.sessions == [] and c.artifacts == [] and c.goals == []
    assert c.theme == {"mode": "dark", "overrides": {}, "preset": "gruvbox", "edits": []}
    assert all(url.startswith("http://127.0.0.1:7944/") for _m, url, _h, _r in c.calls), "its origin only"
    assert all(r == 0 for *_x, r in c.calls), "never follows a redirect off the sandbox"
    assert len(out) < 200


def test_sandbox_reset_with_no_script_id_uses_the_one_console_the_campaign_records(console):
    c, reg = console
    c.origin = "http://localhost:7944"
    _save(reg, "http://localhost:7944")
    assert "reset:" in call(reg, "campaign_sandbox_reset", campaign_id=1, clear="sessions")
    assert c.sessions == [] and len(c.artifacts) == 2, "clear= limits what is cleared"


@pytest.mark.parametrize(
    ("base_url", "why"),
    [
        ("http://127.0.0.1:7870", "operator's default (live) instance"),
        ("http://localhost:7871", "operator's dev instance"),
        ("http://127.0.0.1:7953", "THIS agent's own console"),
        ("https://agent.example", "isn't on this machine"),
    ],
)
def test_sandbox_reset_refuses_live_own_and_remote_consoles_before_sending_anything(console, base_url, why):
    c, reg = console
    _save(reg, base_url)
    out = call(reg, "campaign_sandbox_reset", campaign_id=1, script_id=1)
    assert out.startswith("Not reset — ") and why in out, out
    assert c.calls == [] and len(c.sessions) == 3


def test_sandbox_reset_refuses_when_it_cannot_tell_its_own_console(console, monkeypatch):
    c, reg = console
    monkeypatch.setattr(produce, "own_console_urls", lambda: [])
    _save(reg, "http://127.0.0.1:7944")
    out = call(reg, "campaign_sandbox_reset", campaign_id=1, script_id=1)
    assert "can't tell which console it serves on" in out and c.calls == []


def test_sandbox_reset_only_targets_a_console_the_campaign_records(console):
    c, reg = console
    _save(reg, "http://127.0.0.1:7944")
    _save(reg, "http://127.0.0.1:7945", name="other")
    out = call(reg, "campaign_sandbox_reset", campaign_id=1, base_url="http://127.0.0.1:7999")
    assert "isn't a console this campaign records" in out and "7944" in out and c.calls == []
    assert "several consoles" in call(reg, "campaign_sandbox_reset", campaign_id=1)
    assert "No shot script #9" in call(reg, "campaign_sandbox_reset", campaign_id=1, script_id=9)
    assert "unknown 'tabs'" in call(reg, "campaign_sandbox_reset", campaign_id=1, script_id=1, clear="tabs")
    assert "reset:" in call(reg, "campaign_sandbox_reset", campaign_id=1, base_url="http://localhost:7944/")


def test_sandbox_reset_deletes_nothing_on_something_that_is_not_a_protoagent_console(console, monkeypatch):
    c, reg = console
    c.token = "right"
    monkeypatch.setenv("CAMPAIGN_SANDBOX_TOKEN", "wrong")
    _save(reg, "http://127.0.0.1:7944", auth={"bearer_env": "CAMPAIGN_SANDBOX_TOKEN"})
    out = call(reg, "campaign_sandbox_reset", campaign_id=1, script_id=1)
    assert "NOT reset" in out and "not a protoAgent console" in out, out
    assert [m for m, *_ in c.calls] == ["GET"] and len(c.sessions) == 3
    assert "wrong" not in out


def test_sandbox_reset_sends_the_scripts_own_bearer(console, monkeypatch):
    c, reg = console
    c.token = "sekret"
    monkeypatch.setenv("CAMPAIGN_SANDBOX_TOKEN", "sekret")
    _save(reg, "http://127.0.0.1:7944", auth={"bearer_env": "CAMPAIGN_SANDBOX_TOKEN"})
    out = call(reg, "campaign_sandbox_reset", campaign_id=1, script_id=1)
    assert "sessions 3 cleared" in out and "sekret" not in out
    assert all(h == {"Authorization": "Bearer sekret"} for _m, _u, h, _r in c.calls)


def test_the_reset_worker_needs_a_fence_and_an_http_origin():
    c = FakeConsole()
    base = {"v": pw_worker.JOB_VERSION, "kind": "reset", "base_url": c.origin, "clear": ["sessions"]}
    rep = pw_worker.run_job(base, c.factory())
    assert not rep["ok"] and "no fence" in rep["error"] and c.calls == [], "unfenced: refused, nothing sent"
    rep = pw_worker.run_job({**base, "base_url": "file:///etc", "fence": shoot.fence()}, c.factory())
    assert not rep["ok"] and "http(s) base_url" in rep["error"] and c.calls == []
    rep = pw_worker.run_job({**base, "clear": ["sessions", "everything"], "fence": shoot.fence()}, c.factory())
    assert rep["ok"] and rep["result"]["cleared"] == {"sessions": 3}, "an unknown name is ignored, never guessed"
    assert sandbox.parse_clear("") == ["sessions", "artifacts", "goals"]
    assert sandbox.parse_clear(["Goals", "goals"]) == ["goals"]
