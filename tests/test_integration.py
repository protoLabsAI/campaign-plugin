"""ONE real end-to-end take: a tiny local page, real headless Chromium, then a real render.

Skipped when Playwright's Chromium (or ffmpeg, for the render half) isn't installed — the
rest of the suite covers the same code with the browser mocked at its boundary.
"""

from __future__ import annotations

import functools
import http.server
import json
import struct
import threading
from pathlib import Path

import pytest
from campaign import render, shoot
from campaign.shotscript import validate
from conftest import have_chromium, have_ffmpeg

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>tiny</title>
<style>body{font:20px system-ui;background:#101418;color:#eee;padding:40px}
button{font-size:20px;padding:8px 16px} #out{margin-top:20px}</style></head><body>
<h1>Tiny app</h1>
<p class="who">Signed in as /Users/alice — alice@example.org</p>
<label>Repo <input id="repo" placeholder="https://github.com/…"></label>
<button onclick="setTimeout(()=>{document.getElementById('out').textContent='Installing '+document.getElementById('repo').value},300)">Install</button>
<div id="out"></div>
<div class="token">sk-abcdefghijklmnopqrstuvwxyz123456</div>
</body></html>"""


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text(PAGE, encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
def test_real_take_of_a_tiny_local_page(site, tmp_path):
    script = validate(
        {
            "name": "tiny",
            "base_url": site,
            "viewport": {"width": 640, "height": 400},
            "device_scale_factor": 2,
            "redact": {"presets": ["home_paths", "emails", "secrets"]},
            "steps": [
                {"goto": "/index.html"},
                {"wait_for": {"role": "button", "name": "Install"}},
                {"mark": "start"},
                {"type": {"placeholder": "https://github.com/…", "text": "https://github.com/acme/x", "delay_ms": 10}},
                {"click": {"role": "button", "name": "Install"}},
                {"wait_for": {"text": "Installing https://github.com/acme/x"}},
                {"hold": 400},
                {"screenshot": "done"},
                {"mark": "end"},
            ],
        }
    )
    res = shoot.run(script, tmp_path / "take")
    assert Path(res["video"]).stat().st_size > 1000
    assert res["marks"]["end"] > res["marks"]["start"] > 0
    png = Path(res["screenshots"]["done"]).read_bytes()
    assert struct.unpack(">II", png[16:24]) == (1280, 800), "stills honour device_scale_factor"
    timing = json.loads(Path(res["timing"]).read_text())
    assert timing["error"] == "" and len(timing["steps"]) == 9

    # Failure path, for real: a target that doesn't exist → step number + a screenshot.
    bad = validate({**{k: script[k] for k in ("base_url",)}, "step_timeout_ms": 800,
                    "steps": [{"goto": "/index.html"}, {"click": {"role": "button", "name": "Nope"}}]})  # fmt: skip
    with pytest.raises(shoot.ShootError) as e:
        shoot.run(bad, tmp_path / "bad")
    assert str(e.value).startswith("step 2 (click role='button' name='Nope') failed")
    assert Path(e.value.result["failure_png"]).is_file()

    if not have_ffmpeg():
        pytest.skip("ffmpeg not installed — take verified, render half skipped")
    source = render.probe(res["video"])
    # The video is CSS pixels — never a 2x canvas with the page padded into its corner.
    assert (source["width"], source["height"]) == (640, 400) and source["duration_s"] > 0.5
    spec = render.normalize_output(
        {"name": "clip", "format": "mp4", "start": "start", "end": "end", "limit": "github_attachment_video_free"},
        res["marks"],
        source["duration_s"],
    )
    out = render.render_output(res["video"], tmp_path / "r", spec, marks=res["marks"], source=source)
    assert out["violations"] == [] and out["size_bytes"] > 0 and out["width"] == 640


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
def test_redaction_really_rewrites_the_page(site, tmp_path):
    script = validate(
        {
            "base_url": site,
            "viewport": {"width": 640, "height": 400},
            "device_scale_factor": 1,
            "redact": {"presets": ["home_paths", "emails", "secrets"]},
            "steps": [{"goto": "/index.html"}, {"wait_for": {"text": "Tiny app"}}, {"hold": 200}],
        }
    )
    from playwright.sync_api import sync_playwright

    # Run the same init scripts the shoot installs, then read the DOM back.
    from campaign.threads import in_thread

    def check():
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            ctx = b.new_context()
            ctx.add_init_script(shoot._redact_init_js(shoot.redact_rules(script["redact"])))
            page = ctx.new_page()
            page.goto(site + "/index.html")
            page.wait_for_timeout(200)
            text = page.inner_text("body")
            b.close()
            return text

    text = in_thread(check, 60)
    assert "alice" not in text and "sk-abcdef" not in text
    assert "~" in text and "you@example.com" in text and "•••" in text


# ── the shoot browser's guards, for real ─────────────────────────────────────
def _serve(handler_cls):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture
def host_with_gallery(registry):
    """A stand-in protoAgent host: the plugin's REAL routers behind a core-like bearer gate on
    /api (token mode), or open (no gate) — plus a third-party origin that logs what it receives."""
    import campaign
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    campaign.register(registry)
    app = FastAPI()
    for prefix, router in registry.routers:
        app.include_router(router, prefix=prefix)
    client = TestClient(app)
    state = {"token": "", "third_party_auth": [], "host_auth": [], "api_hits": []}

    class ThirdParty(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            state["third_party_auth"].append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Type", "image/gif")
            self.end_headers()
            self.wfile.write(b"GIF89a")

    tp_srv, tp_url = _serve(ThirdParty)

    class Host(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _go(self, method):
            if self.path == "/landing":
                state["host_auth"].append(self.headers.get("Authorization"))
                body = f'<html><body><h1>app</h1><img src="{tp_url}/pixel.gif"></body></html>'.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/api/"):
                state["api_hits"].append(self.path)
                if state["token"] and self.headers.get("Authorization") != f"Bearer {state['token']}":
                    self.send_response(401)
                    self.end_headers()
                    return
            n = int(self.headers.get("Content-Length") or 0)
            r = client.request(
                method,
                self.path,
                content=self.rfile.read(n) if n else None,
                headers={"Content-Type": self.headers.get("Content-Type", "")},
            )
            self.send_response(r.status_code)
            self.send_header("Content-Type", r.headers.get("content-type", "text/plain"))
            self.end_headers()
            self.wfile.write(r.content)

        def do_GET(self):
            self._go("GET")

        def do_POST(self):
            self._go("POST")

    host_srv, host_url = _serve(Host)
    state["url"] = host_url
    yield state
    host_srv.shutdown()
    tp_srv.shutdown()


def _asset_awaiting_review():
    from campaign import paths, store
    from conftest import PNG_1x1

    c = store.create_campaign("Launch")
    f = paths.campaign_dir(c["id"], "Launch") / "hero.png"
    f.write_bytes(PNG_1x1)
    a = store.add_asset(c["id"], "still", "hero", path=str(f), status="captured")
    return store.update_asset(a["id"], status="ready_for_review")


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
def test_real_bearer_reaches_base_url_only_never_a_third_party(host_with_gallery, tmp_path):
    h = host_with_gallery
    script = validate(
        {
            "base_url": h["url"],
            "auth": {"bearer_env": "CAMPAIGN_APP_TOKEN"},
            "steps": [{"goto": "/landing"}, {"wait_for": {"network_idle": True}}],
        }
    )
    shoot.run(script, tmp_path / "t", env={"CAMPAIGN_APP_TOKEN": "app-tok"})
    assert h["host_auth"] == ["Bearer app-tok"], "the app itself gets the bearer"
    assert h["third_party_auth"] == [None], "the third-party pixel was fetched WITHOUT the bearer"


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
@pytest.mark.parametrize("mode", ["open", "token"])
def test_real_shoot_browser_cannot_approve_through_the_gallery(host_with_gallery, tmp_path, monkeypatch, mode):
    from campaign import store

    h = host_with_gallery
    a = _asset_awaiting_review()
    env = {}
    auth = {}
    if mode == "token":
        # Even an operator who allowlisted the host token by mistake doesn't hand it over.
        h["token"] = "operator-secret"
        env = {"A2A_AUTH_TOKEN": "operator-secret"}
        auth = {"auth": {"bearer_env": "A2A_AUTH_TOKEN"}}
    script = validate(
        {
            "base_url": h["url"],
            "step_timeout_ms": 3000,
            **auth,
            "steps": [
                {"goto": "/plugins/campaign/view"},
                {"click": {"role": "button", "name": "Approve", "exact": True}},
            ],
        }
    )
    with pytest.raises(shoot.ShootError):
        shoot.run(script, tmp_path / "t", env=env)
    assert store.get_asset(a["id"])["status"] == "ready_for_review"
    assert h["api_hits"] == [], "no request from the shoot browser ever reached the plugin's API"
