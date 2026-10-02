"""Pre-seeded browser storage, the init_script escape hatch, and the upload step — host side
(validation + the upload fence) and the worker side against the fake browser."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from campaign import shoot
from campaign.shotscript import ScriptError, browser_origin, validate
from campaign.worker import pw_worker
from conftest import FakePlaywright

UI = {"state": {"panelWidths": {"right": 860}}, "version": 0}


def _script(*steps, **top):
    return validate({"base_url": "http://app.test:7871", "steps": [{"goto": "/"}, *steps], **top})


def _problems(**top) -> str:
    with pytest.raises(ScriptError) as e:
        validate({"steps": [{"goto": "http://x.test/"}], **top})
    return str(e.value)


# ── storage ───────────────────────────────────────────────────────────────────
def test_storage_defaults_to_the_base_url_origin_and_json_serializes_objects():
    s = _script(storage={"local": {"protoagent.ui": UI, "flag": "on", "n": 3, "b": True}})
    assert s["storage"] == {
        "origin": "http://app.test:7871",
        "local": {"protoagent.ui": json.dumps(UI, separators=(",", ":")), "flag": "on", "n": "3", "b": "true"},
        "session": {},
    }
    assert json.loads(s["storage"]["local"]["protoagent.ui"]) == UI, "JSON.parse reads back the object"


def test_storage_origin_is_spelled_like_location_origin():
    assert browser_origin("HTTP://Localhost:80/app/") == "http://localhost"
    assert browser_origin("https://x.test:443") == "https://x.test"
    assert browser_origin("http://127.0.0.1:7871/") == "http://127.0.0.1:7871"
    assert browser_origin("http://[::1]:7871") == "http://[::1]:7871"
    assert browser_origin("file:///etc/passwd") == ""
    s = _script(storage={"origin": "http://Other.test:9000/", "session": {"k": "v"}})
    assert s["storage"]["origin"] == "http://other.test:9000" and s["storage"]["session"] == {"k": "v"}


@pytest.mark.parametrize(
    "storage, msg",
    [
        ({"local": {"k": "v"}}, "storage needs an origin"),
        ({"origin": "http://x.test/app", "local": {"k": "v"}}, "origin only"),
        ({"origin": "ftp://x.test", "local": {"k": "v"}}, "http(s) origin"),
        ({"origin": "http://x.test"}, "needs `local` and/or `session`"),
        ({"origin": "http://x.test", "locals": {"k": "v"}, "local": {"k": "v"}}, "did you mean `local`"),
        ({"origin": "http://x.test", "local": ["k"]}, "storage.local: must be a mapping"),
        ({"origin": "http://x.test", "local": {"k": float("nan")}}, "can't be JSON-serialized"),
        ({"origin": "http://x.test", "local": {"k": "x" * 300_000}}, "byte max"),
        ({"origin": "http://x.test", "local": {f"k{i}": "v" for i in range(201)}}, "entry max"),
        ("localStorage=1", "storage must be a mapping"),
    ],
)
def test_storage_shape_errors_are_actionable(storage, msg):
    assert msg in _problems(storage=storage)


def test_storage_and_init_script_are_installed_before_the_first_page_script(tmp_path):
    pw = FakePlaywright()
    s = _script(storage={"local": {"protoagent.ui": UI}}, init_script="window.__demo = 1;")
    shoot.run(s, tmp_path, playwright_factory=pw)
    ctx = pw.browser.contexts[0]
    seed = next(js for js in ctx.init_scripts if "localStorage" in js)
    assert '"origin": "http://app.test:7871"' in seed and "location.origin !== cfg.origin" in seed
    assert "window.top !== window" in seed, "only the top-level document seeds"
    assert json.dumps(json.dumps(UI, separators=(",", ":")))[1:-1] in seed
    own = next(js for js in ctx.init_scripts if "__demo" in js)
    assert own.startswith('(() => { if (location.origin !== "http://app.test:7871") return;')
    assert ctx.init_scripts.index(seed) < ctx.init_scripts.index(own), "storage first, then init_script"


def test_init_script_is_bounded_and_needs_a_base_url():
    assert "must be a string" in _problems(base_url="http://x.test", init_script=["a"])
    assert "64 KB max" in _problems(base_url="http://x.test", init_script="x" * (64 * 1024 + 1))
    assert "needs a base_url" in _problems(init_script="window.a = 1")
    assert _script(init_script="")["init_script"] == ""


def test_the_worker_refuses_an_init_script_with_no_origin(tmp_path):
    job = shoot.build_job(_script(init_script="window.a=1"), tmp_path)
    job["script"]["_base_origin"] = ""
    rep = pw_worker.run_job(json.loads(json.dumps(job)), FakePlaywright())
    assert not rep["ok"] and "refusing" in rep["error"]


# ── the upload step: validation ──────────────────────────────────────────────
@pytest.mark.parametrize(
    "body, msg",
    [
        ({"target": "#f"}, "needs a target"),
        ({"target": "#f", "files": []}, "non-empty list"),
        ({"target": "#f", "files": ["rel/a.png"]}, "must be an absolute path"),
        ({"target": "#f", "files": [""]}, "non-empty path string"),
        ({"target": "#f", "files": [f"/a/{i}.png" for i in range(21)]}, "20-file max"),
        ({"files": ["/a.png"]}, "needs a target — a selector"),
        ({"target": "#f", "files": ["/a.png"], "file": "/b"}, "unknown option `file`"),
        ("/a.png", "needs a target"),
    ],
)
def test_upload_step_shape_errors(body, msg):
    assert msg in _problems(steps=[{"goto": "http://x.test/"}, {"upload": body}])


def test_upload_step_normalizes_a_single_path_and_a_frame():
    s = _script({"upload": {"role": "button", "name": "Attach", "files": "/tmp/a.png", "frame": "/plugins/x/view"}})
    step = s["steps"][1]
    assert step["files"] == ["/tmp/a.png"]
    assert step["target"] == {"role": "button", "name": "Attach", "frame": {"url": "/plugins/x/view"}}


# ── the upload fence (host side) ─────────────────────────────────────────────
@pytest.fixture
def uploads(tmp_path):
    d = tmp_path / "demo-assets"
    d.mkdir()
    (d / "logo.png").write_bytes(b"\x89PNG fake")
    shoot.configure("", str(d))
    return d


def _upload_script(*files):
    return _script({"upload": {"selector": "input[type=file]", "files": list(files)}})


def test_uploads_are_refused_until_the_operator_allowlists_a_dir(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    with pytest.raises(shoot.ShootError) as e:
        shoot.build_job(_upload_script(str(f)), tmp_path / "t")
    assert str(e.value).startswith(
        "step 2 (upload a.png via selector='input[type=file]') refused: file uploads are off"
    )
    assert "upload_dirs" in str(e.value)


def test_an_allowlisted_file_is_resolved_into_the_job(uploads, tmp_path):
    job = shoot.build_job(_upload_script(str(uploads / "logo.png")), tmp_path / "t")
    step = job["script"]["steps"][1]
    assert step["_files"] == [str((uploads / "logo.png").resolve())]


@pytest.mark.parametrize(
    "make, msg",
    [
        (lambda d, t: str(t / "outside.png"), "outside the plugin's upload_dirs"),
        (lambda d, t: str(d / "missing.png"), "doesn't exist"),
        (lambda d, t: str(d / "sub"), "not a regular file"),
        (lambda d, t: str(d / "link.png"), "outside the plugin's upload_dirs"),  # symlink out
        (lambda d, t: str(d / "x" / ".." / ".." / "outside.png"), "outside the plugin's upload_dirs"),
        (lambda d, t: str(d / "secrets.yaml"), "key or credentials file"),
        (lambda d, t: str(d / ".env.local"), "key or credentials file"),
        (lambda d, t: str(d / "deploy.pem"), "key or credentials file"),
        (lambda d, t: str(d / "id_ed25519"), "key or credentials file"),
        (lambda d, t: str(d / ".ssh" / "notes.txt"), "credentials directory"),
        (lambda d, t: str(d / "big.bin"), "per-file max"),
    ],
)
def test_the_upload_fence(uploads, tmp_path, monkeypatch, make, msg):
    (tmp_path / "outside.png").write_bytes(b"x")
    (uploads / "sub").mkdir()
    (uploads / "x").mkdir()
    os.symlink(tmp_path / "outside.png", uploads / "link.png")
    for name in ("secrets.yaml", ".env.local", "deploy.pem", "id_ed25519"):
        (uploads / name).write_text("s")
    (uploads / ".ssh").mkdir()
    (uploads / ".ssh" / "notes.txt").write_text("s")
    (uploads / "big.bin").write_bytes(b"0" * 11)
    monkeypatch.setattr(shoot, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(shoot.ShootError) as e:
        shoot.build_job(_upload_script(make(uploads, tmp_path)), tmp_path / "t")
    assert msg in str(e.value) and "refused" in str(e.value)


def test_the_agents_home_is_refused_even_when_allowlisted(tmp_path, monkeypatch):
    home = tmp_path / "agent-home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "notes.png").write_bytes(b"x")
    monkeypatch.setenv("PROTOAGENT_HOME", str(home))
    shoot.configure("", str(home / "config"))
    with pytest.raises(shoot.ShootError) as e:
        shoot.build_job(_upload_script(str(home / "config" / "notes.png")), tmp_path / "t")
    assert "inside the agent's home" in str(e.value)


def test_the_plugins_own_media_is_exempt_from_the_agent_home_rule(tmp_path, monkeypatch, isolated_data_dir):
    from campaign import paths

    monkeypatch.setenv("PROTOAGENT_HOME", str(isolated_data_dir))
    still = paths.media_root() / "c1" / "hero.png"
    still.parent.mkdir(parents=True)
    still.write_bytes(b"x")
    shoot.configure("", str(paths.media_root()))
    job = shoot.build_job(_upload_script(str(still)), tmp_path / "t")
    assert job["script"]["steps"][1]["_files"] == [str(still.resolve())]


def test_too_broad_allowlist_entries_are_ignored_and_said_so(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    shoot.configure("", f"/, {Path.home()}, relative/dir, {tmp_path / 'nope'}")
    with pytest.raises(shoot.ShootError) as e:
        shoot.build_job(_upload_script(str(f)), tmp_path / "t")
    msg = str(e.value)
    assert "file uploads are off" in msg and "too broad" in msg and "not an absolute path" in msg
    assert "doesn't exist" in msg


def test_a_step_over_the_total_cap_is_refused(uploads, tmp_path, monkeypatch):
    (uploads / "b.png").write_bytes(b"0" * 8)
    (uploads / "c.png").write_bytes(b"0" * 8)
    monkeypatch.setattr(shoot, "MAX_UPLOAD_TOTAL_BYTES", 10)
    with pytest.raises(shoot.ShootError) as e:
        shoot.build_job(_upload_script(str(uploads / "b.png"), str(uploads / "c.png")), tmp_path / "t")
    assert "in one step is over" in str(e.value)


# ── the upload step in the worker (fake browser) ─────────────────────────────
def test_a_file_input_target_gets_the_files_directly(uploads, tmp_path):
    pw = FakePlaywright(file_inputs={"input[type=file]"})
    shoot.run(_upload_script(str(uploads / "logo.png")), tmp_path / "t", playwright_factory=pw)
    sets = [c for c in pw.calls if c[0] == "set_input_files"]
    assert sets and sets[0][4]["files"] == [str((uploads / "logo.png").resolve())]
    assert not any(c[0] == "expect_file_chooser" for c in pw.calls)


def test_a_button_target_is_clicked_and_its_file_chooser_gets_the_files(uploads, tmp_path):
    pw = FakePlaywright()
    s = _script({"upload": {"role": "button", "name": "Attach", "files": [str(uploads / "logo.png")]}})
    shoot.run(s, tmp_path / "t", playwright_factory=pw)
    names = [c[0] for c in pw.calls]
    assert names.index("expect_file_chooser") < names.index("click") < names.index("chooser.set_files")
    assert ("chooser.set_files", [str((uploads / "logo.png").resolve())]) in pw.calls


def test_the_worker_refuses_an_upload_the_host_never_checked(tmp_path):
    job = shoot.build_job(_script({"mark": "a"}), tmp_path)
    job["script"]["steps"].append(
        {"op": "upload", "index": 3, "target": {"selector": "#f"}, "files": ["/etc/hosts"], "_desc": "upload"}
    )
    rep = pw_worker.run_job(json.loads(json.dumps(job)), FakePlaywright(file_inputs={"#f"}))
    assert not rep["ok"] and "upload fence didn't run" in rep["error"]
