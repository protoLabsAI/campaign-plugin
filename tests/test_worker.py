"""The out-of-process worker: job serialization, fail-closed fence, bearer scrubbing — unit
(real worker code, fake browser) — and, when Chromium is present, the REAL subprocess:
fence + bearer enforced inside it, a card that can't phone home, and a shoot + card from a
host process that is FORBIDDEN to import playwright."""

from __future__ import annotations

import http.server
import json
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest
from campaign import cards, interpreter, shoot
from campaign.shotscript import validate
from campaign.worker import pw_worker
from conftest import ROOT, FakePlaywright, have_chromium


def _script(**top):
    return validate({"base_url": "http://app.test:8080", "steps": [{"goto": "/"}], **top})


# ── serialization ─────────────────────────────────────────────────────────────
def test_the_shoot_job_is_plain_json_with_everything_the_worker_needs(tmp_path):
    s = _script(steps=[{"goto": "/a"}, {"click": {"role": "button", "name": "Go"}}])
    job = shoot.build_job(s, tmp_path / "take", bearer="tok")
    assert json.loads(json.dumps(job)) == job, "round-trips losslessly"
    assert job["v"] == pw_worker.JOB_VERSION and job["kind"] == "shoot"
    assert job["fence"] == {"block_paths": shoot.FENCE_PATTERNS}
    goto, click = job["script"]["steps"]
    assert goto["_url"] == "http://app.test:8080/a", "relative URLs are resolved on the host"
    assert click["_desc"] == "click role='button' name='Go'", "descriptions are precomputed on the host"


def test_the_bearer_rides_stdin_never_argv_or_the_report(tmp_path, monkeypatch):
    seen = {}

    def fake_run_python(python, args, *, stdin=None, timeout, env=None):
        seen.update(args=args, stdin=stdin)
        job = json.loads(stdin)
        Path(job["report_path"]).write_text(json.dumps({"ok": True, "kind": "shoot", "result": {"dir": "x"}}))
        return interpreter.Completed(0, "", "")

    from campaign import deps

    monkeypatch.setattr(deps, "resolve", lambda: interpreter.Resolution(python=sys.executable, chromium=True))
    monkeypatch.setattr(interpreter, "run_python", fake_run_python)
    shoot.run(_script(auth={"bearer_env": "CAMPAIGN_T"}), tmp_path, env={"CAMPAIGN_T": "s3cret-tok"})
    assert "s3cret-tok" not in " ".join(seen["args"])
    assert json.loads(seen["stdin"])["bearer"] == "s3cret-tok"


def test_a_worker_that_dies_without_a_report_is_explained(tmp_path, monkeypatch):
    from campaign import deps

    monkeypatch.setattr(deps, "resolve", lambda: interpreter.Resolution(python=sys.executable, chromium=True))
    monkeypatch.setattr(
        interpreter,
        "run_python",
        lambda *a, **k: interpreter.Completed(1, "", "Traceback…\nImportError: s3cret-tok broke"),
    )
    with pytest.raises(shoot.WorkerError) as e:
        shoot.run(_script(auth={"bearer_env": "CAMPAIGN_T"}), tmp_path, env={"CAMPAIGN_T": "s3cret-tok"})
    assert "exited 1 without a report" in str(e.value) and "ImportError" in str(e.value)
    assert "s3cret-tok" not in str(e.value), "stderr is scrubbed of the bearer"


def test_an_overrun_is_a_clear_error(tmp_path, monkeypatch):
    from campaign import deps

    monkeypatch.setattr(deps, "resolve", lambda: interpreter.Resolution(python=sys.executable, chromium=True))
    monkeypatch.setattr(interpreter, "run_python", lambda *a, **k: interpreter.Completed(-9, "", "", timed_out=True))
    with pytest.raises(shoot.WorkerError, match="killed"):
        shoot.run(_script(), tmp_path)


def test_no_worker_runs_without_a_usable_interpreter(tmp_path, monkeypatch):
    from campaign import deps

    monkeypatch.setattr(deps, "resolve", lambda: interpreter.Resolution(need="runtime"))
    monkeypatch.setattr(interpreter, "run_python", lambda *a, **k: pytest.fail("must not spawn"))
    with pytest.raises(shoot.WorkerError, match="Settings ▸ Tools"):
        shoot.run(_script(), tmp_path)


# ── the worker itself (real code, fake browser) ───────────────────────────────
def test_a_job_without_a_fence_is_refused_before_any_browser(tmp_path):
    pw = FakePlaywright()
    job = shoot.build_job(_script(), tmp_path)
    for bad in (None, {}, {"block_paths": []}, {"block_paths": [""]}, "x"):
        report = pw_worker.run_job({**job, "fence": bad}, pw)
        assert not report["ok"] and "fence" in report["error"]
    assert pw.browser is None


def test_unknown_versions_and_kinds_are_refused(tmp_path):
    job = shoot.build_job(_script(), tmp_path)
    assert "version" in pw_worker.run_job({**job, "v": 99}, FakePlaywright())["error"]
    assert "kind" in pw_worker.run_job({**job, "kind": "exec"}, FakePlaywright())["error"]


def test_the_fence_in_the_job_is_what_the_worker_enforces(tmp_path):
    pw = FakePlaywright()
    job = shoot.build_job(_script(), tmp_path)
    job["fence"] = {"block_paths": [r"/private(?:/|$)"]}
    assert pw_worker.run_job(job, pw)["ok"]
    ((block, _),) = pw.browser.contexts[0].routes
    assert block("http://anything:1/private/x") and block("http://x//%70rivate")
    assert not block("http://x/api/plugins/campaign/x"), "the worker obeys the job, not a hardcoded list"


def test_the_bearer_is_scrubbed_from_everything_the_worker_reports(tmp_path):
    pw = FakePlaywright(fail_on=lambda action, how, args: action == "click")
    s = _script(auth={"bearer_env": "CAMPAIGN_T"}, steps=[{"goto": "/"}, {"click": {"text": "s3cret-tok"}}])
    job = shoot.build_job(s, tmp_path, bearer="s3cret-tok")
    report = pw_worker.run_job(json.loads(json.dumps(job)), pw)
    assert not report["ok"] and "s3cret-tok" not in json.dumps(report)


def test_a_card_page_can_load_nothing(tmp_path):
    pw = FakePlaywright()
    cards.render("og-1280x640", {"title": "Hi"}, tmp_path / "og", playwright_factory=pw)
    ctx = pw.browser.contexts[0]
    fence, catch_all = ctx.routes
    assert fence[0]("http://h/api/plugins/campaign/x")
    assert catch_all[0]("https://fonts.example.com/x.woff2") and catch_all[0]("http://127.0.0.1/")


# ── the REAL subprocess, real Chromium ────────────────────────────────────────
class _Hits(http.server.BaseHTTPRequestHandler):
    hits: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).hits.append((self.path, self.headers.get("Authorization")))
        body = (
            b'<html><body><h1>app</h1><img src="/private/pixel.gif">'
            b'<img src="/%70rivate//x.gif"><img src="/ok.gif"></body></html>'
            if self.path == "/landing"
            else b"GIF89a"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html" if self.path == "/landing" else "image/gif")
        self.end_headers()
        self.wfile.write(body)


def _server():
    handler = type("H", (_Hits,), {"hits": []})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, handler, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
def test_real_worker_process_enforces_the_fence_and_scopes_the_bearer(tmp_path):
    srv, handler, url = _server()
    other, other_h, other_url = _server()
    try:
        s = validate(
            {
                "base_url": url,
                "steps": [
                    {"goto": "/landing"},
                    {"goto": f"{other_url}/landing"},
                    {"wait_for": {"network_idle": True}},
                ],
            }
        )
        job = shoot.build_job(s, tmp_path / "t", bearer="app-tok")
        job["fence"] = {"block_paths": [r"/private(?:/|$)"]}
        report = shoot.run_worker(job, 120)
    finally:
        srv.shutdown()
        other.shutdown()
    assert report["ok"], report
    paths = [p for p, _ in handler.hits]
    assert "/landing" in paths and "/ok.gif" in paths
    assert not any(p.startswith("/private") or "rivate" in p for p in paths), "the fence held in the worker"
    assert all(auth == "Bearer app-tok" for _, auth in handler.hits), "base_url origin gets the bearer"
    assert other_h.hits and all(auth is None for _, auth in other_h.hits), "another origin never does"


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
def test_real_card_through_the_worker_cannot_phone_home(tmp_path, monkeypatch):
    srv, handler, url = _server()
    try:
        page = cards.build_html("og-1280x640", {"title": "Hi"}, (1280, 640), __import__("campaign").brand.resolve())
        page = page.replace("</body>", f'<img src="{url}/track.gif"><link rel="stylesheet" href="{url}/x.css"></body>')
        job = {
            "v": pw_worker.JOB_VERSION,
            "kind": "card",
            "html": page,
            "out_path": str(tmp_path / "c.png"),
            "width": 1280,
            "height": 640,
            "max_bytes": None,
            "fence": shoot.fence(),
        }
        report = shoot.run_worker(job, 120)
    finally:
        srv.shutdown()
    assert report["ok"], report
    assert Path(report["result"]["path"]).stat().st_size > 1000
    assert handler.hits == [], "a card page loads nothing over the network"


HOST_WITHOUT_PLAYWRIGHT = textwrap.dedent(
    """
    import functools, http.server, importlib.abc, importlib.util, json, pathlib, sys, threading

    class NoPlaywright(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == "playwright" or name.startswith("playwright."):
                raise ImportError("the HOST imported playwright: " + name)
            return None

    sys.meta_path.insert(0, NoPlaywright())
    ROOT, OUT, PY = sys.argv[1], sys.argv[2], sys.argv[3]
    spec = importlib.util.spec_from_file_location("campaign", ROOT + "/__init__.py", submodule_search_locations=[ROOT])
    mod = importlib.util.module_from_spec(spec); sys.modules["campaign"] = mod; spec.loader.exec_module(mod)
    from campaign import cards, deps, shoot
    from campaign.shotscript import validate
    deps.configure("", PY)
    res = deps.resolve()
    assert res.ready, res
    site = pathlib.Path(OUT) / "site"; site.mkdir()
    (site / "index.html").write_text("<h1>hello from the app</h1>")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    take = shoot.run(validate({"name": "t", "base_url": base,
                               "steps": [{"goto": "/index.html"}, {"hold": 300}, {"screenshot": "s"}]}), OUT + "/take")
    card = cards.render("square-1080", {"title": "From a host with no playwright"}, OUT + "/card")
    assert "playwright" not in sys.modules
    print(json.dumps({"video": take["video"], "still": take["screenshots"]["s"], "card": card["path"],
                      "python": res.python}))
    """
)


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
def test_end_to_end_shoot_and_card_from_a_host_that_cannot_import_playwright(tmp_path):
    # The "runtime" is this venv's interpreter (it has playwright); the "host" is a process
    # where importing playwright raises. Everything must still work — through the worker.
    worker_py = interpreter.resolve().python
    r = subprocess.run(
        [sys.executable, "-c", HOST_WITHOUT_PLAYWRIGHT, str(ROOT), str(tmp_path), worker_py],
        capture_output=True,
        text=True,
        timeout=300,
        env={**__import__("os").environ, "CAMPAIGN_DIR": str(tmp_path / "data")},
    )
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    for k in ("video", "still", "card"):
        assert Path(out[k]).stat().st_size > 500, k


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX process tree")
def test_a_hung_real_browser_is_killed_not_abandoned(tmp_path, monkeypatch):
    import os
    import time

    # Snapshot the worker's process tree independently of the code under test: grab the
    # Popen, and just before the deadline record every descendant (and its name).
    found: list[int] = []
    seen_names: list[str] = []
    real_popen = subprocess.Popen

    def popen(*a, **k):
        proc = real_popen(*a, **k)
        argv = a[0] if a else k.get("args", [])
        if not any(str(x).endswith("pw_worker.py") for x in argv):
            return proc  # only the worker spawn is snapshotted (ps etc. pass straight through)

        def snap():
            time.sleep(11)
            out = interpreter._descendants(proc.pid)
            found.extend(out)
            ps = subprocess.run(["ps", "-A", "-o", "pid=,comm="], capture_output=True, text=True).stdout
            seen_names.extend(ln for ln in ps.splitlines() if ln.split() and int(ln.split()[0]) in out)

        th = threading.Thread(target=snap, daemon=True)
        th.start()
        threads.append(th)
        return proc

    threads: list[threading.Thread] = []
    monkeypatch.setattr(interpreter.subprocess, "Popen", popen)
    srv, _, url = _server()
    try:
        s = validate({"base_url": url, "total_timeout_s": 300, "steps": [{"goto": "/landing"}, {"hold": 60000}]})
        t = time.monotonic()
        with pytest.raises(shoot.WorkerError, match="killed"):
            shoot.run_worker(shoot.build_job(s, tmp_path / "t"), 15)
        assert time.monotonic() - t < 60
    finally:
        srv.shutdown()
    for th in threads:
        th.join(20)
    names = subprocess.run(["ps", "-A", "-o", "pid=,comm="], capture_output=True, text=True).stdout
    assert any("chrom" in n.lower() or "headless" in n.lower() for n in seen_names), (
        f"Chromium was among the worker's descendants when it was killed: {seen_names}"
    )

    def alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(stat) and not stat.startswith("Z")

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and any(alive(p) for p in found):
        time.sleep(0.3)
    survivors = [
        ln for ln in names.splitlines() if ln.split() and int(ln.split()[0]) in found and alive(int(ln.split()[0]))
    ]
    assert not survivors, f"processes outlived the kill: {survivors}"
