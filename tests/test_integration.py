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
