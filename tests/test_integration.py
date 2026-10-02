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
from campaign.worker import pw_worker
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
            ctx.add_init_script(pw_worker.redact_init_js(pw_worker.redact_rules(script["redact"])))
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


SLOW_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>slow</title></head><body>
<h1>Running…</h1><div id="out"></div>
<script>setTimeout(() => { document.getElementById('out').textContent = 'Run complete'; }, 20000);</script>
</body></html>"""


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
def test_a_step_waits_past_the_default_15s_when_its_timeout_ms_says_so(site, tmp_path):
    """The live failure: a Claude Code run that takes 17–44s died at 'Timeout 15000ms exceeded'
    because the step's timeout_ms was dropped. Real Chromium, an element that appears at ~20s."""
    (Path(tmp_path) / "site" / "slow.html").write_text(SLOW_PAGE, encoding="utf-8")
    script = validate(
        {
            "name": "slow",
            "base_url": site,
            "viewport": {"width": 480, "height": 320},
            "device_scale_factor": 1,
            "cursor": False,
            "steps": [
                {"goto": "/slow.html"},
                {"mark": "start"},
                {"wait_for": {"text": "Run complete", "timeout_ms": 30_000}},
                {"mark": "done"},
            ],
        }
    )
    assert script["step_timeout_ms"] == 15_000, "the script-wide default is still 15s"
    res = shoot.run(script, tmp_path / "take")
    waited = res["marks"]["done"] - res["marks"]["start"]
    assert 18 < waited < 30, f"waited {waited:.1f}s — the element appears at ~20s"
    assert res["error"] == ""


# ── frames, for real: a parent page with a same-origin iframe (a plugin view's shape) ──
FRAME_PARENT = """<!doctype html><html><head><meta charset="utf-8"><title>console</title>
<style>body{margin:0;background:#000} iframe{border:0;width:400px;height:240px;display:block}</style>
</head><body><div id="slot"></div>
<script>
  // The view iframe is attached AFTER the parent loads — like a console rail view.
  setTimeout(() => { const f = document.createElement('iframe'); f.title = 'Terminal';
    f.src = '/plugins/terminal/view.html'; document.getElementById('slot').appendChild(f); }, 500);
</script></body></html>"""

FRAME_VIEW = """<!doctype html><html><head><meta charset="utf-8">
<style>body{margin:0;background:#000;font:16px monospace;color:#000}
div{width:120px;height:60px;display:inline-block;margin:4px}
.late-secret{background:#ff0000} .step-secret{background:#ff0000} #ok{background:#0000ff}
input{width:120px;margin:4px}</style></head><body>
<input id="cmd" aria-label="command">
<div class="late-secret"></div><div class="step-secret"></div><div id="ok"></div>
<script>
  document.getElementById('cmd').addEventListener('input', e => {
    if (e.target.value === 'echo hi') document.getElementById('ok').style.background = '#00ff00'; });
  // The element the script waits for renders late.
  setTimeout(() => { const p = document.createElement('p'); p.textContent = 'connected';
    p.style.color = '#000'; document.body.appendChild(p); }, 1200);
</script></body></html>"""


def _png_pixels(data: bytes) -> list[tuple[int, int, int]]:
    """Decode an 8-bit RGB/RGBA non-interlaced PNG (what Chromium writes) — stdlib only."""
    import zlib

    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w = 8, b"", 0
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos : pos + 8])
        body = data[pos + 8 : pos + 8 + n]
        if kind == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
            assert depth == 8 and ctype in (2, 6), (depth, ctype)
            bpp = 3 if ctype == 2 else 4
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    raw, stride, prev, out = zlib.decompress(idat), w * bpp, bytearray(w * bpp), []
    for y in range(h):
        ft, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b, c = prev[i], prev[i - bpp] if i >= bpp else 0
            if ft == 1:
                line[i] = (line[i] + a) & 255
            elif ft == 2:
                line[i] = (line[i] + b) & 255
            elif ft == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif ft == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out += [tuple(line[i : i + 3]) for i in range(0, stride, bpp)]
        prev = line
    return out


@pytest.fixture
def frame_site(site, tmp_path):
    root = Path(tmp_path) / "site"
    (root / "console.html").write_text(FRAME_PARENT, encoding="utf-8")
    (root / "plugins" / "terminal").mkdir(parents=True)
    # `.html` so the static server types it as a page; `url: /plugins/terminal/view` is a substring.
    (root / "plugins" / "terminal" / "view.html").write_text(FRAME_VIEW, encoding="utf-8")
    return site


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
def test_real_take_waits_types_masks_and_shoots_inside_an_iframe(frame_site, tmp_path):
    view = {"url": "/plugins/terminal/view"}
    script = validate(
        {
            "name": "framed",
            "base_url": frame_site,
            "viewport": {"width": 480, "height": 320},
            "device_scale_factor": 1,
            # Installed before the frame exists — it must reach the frame when it loads.
            "mask": {"selectors": [".late-secret"], "mode": "hide"},
            "steps": [
                {"goto": "/console.html"},
                # The iframe attaches at ~0.5s and its element renders at ~1.2s after that.
                {"wait_for": {"text": "connected", "frame": view, "timeout_ms": 10_000}},
                {
                    "type": {
                        "target": {"label": "command", "frame": {"selector": "iframe[title='Terminal']"}},
                        "text": "echo hi",
                        "delay_ms": 10,
                    }
                },  # fmt: skip
                # A mask step AFTER the frame loaded must reach into it too.
                {"mask": {"selectors": [".step-secret"], "mode": "hide"}},
                {"hold": 200},
                {"screenshot": {"name": "view", "frame": view}},
                {"screenshot": {"name": "ok", "selector": "#ok", "frame": view}},
            ],
        }
    )
    res = shoot.run(script, tmp_path / "take")
    assert res["error"] == ""
    px = _png_pixels(Path(res["screenshots"]["view"]).read_bytes())
    assert len(px) == 400 * 240, "the frame-only screenshot is the iframe element"
    red = sum(1 for r, g, b in px if r > 200 and g < 60 and b < 60)
    assert red == 0, f"{red} red pixels — a mask didn't reach into the frame"
    green = sum(1 for r, g, b in px if g > 200 and r < 60 and b < 60)
    assert green > 120 * 60 * 0.9, "the typed text reached the input inside the frame"
    ok = _png_pixels(Path(res["screenshots"]["ok"]).read_bytes())
    assert ok and all(g > 200 and r < 60 for r, g, _ in ok), "element screenshot inside the frame"

    # Without the masks, the same frame shows its red blocks — the check above is not vacuous.
    bare = validate({**{k: script[k] for k in ("base_url", "viewport", "device_scale_factor")},
                     "steps": [{"goto": "/console.html"}, {"wait_for": {"text": "connected", "frame": view}},
                               {"screenshot": {"name": "bare", "frame": view}}]})  # fmt: skip
    px = _png_pixels(Path(shoot.run(bare, tmp_path / "bare")["screenshots"]["bare"]).read_bytes())
    assert sum(1 for r, g, b in px if r > 200 and g < 60 and b < 60) > 2 * 120 * 60 * 0.9


DPR_PAGE = """<!doctype html><html><body><div id="box" style="width:100px;height:50px"></div><p id="out"></p>
<script>
  new ResizeObserver(([e]) => { const s = e.devicePixelContentBoxSize[0];
    document.getElementById('out').textContent = 'dpcb ' + s.inlineSize + 'x' + s.blockSize;
  }).observe(document.getElementById('box'), {box: 'device-pixel-content-box'});
</script></body></html>"""


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
def test_device_pixel_content_box_matches_the_scale_factor(site, tmp_path):
    """xterm's WebGL renderer sizes its canvas from device-pixel-content-box; under DPR
    emulation alone that box reports 1x and the terminal draws blank (see launch_args)."""
    (Path(tmp_path) / "site" / "dpr.html").write_text(DPR_PAGE, encoding="utf-8")
    script = validate(
        {
            "base_url": site,
            "viewport": {"width": 320, "height": 200},
            "device_scale_factor": 2,
            "steps": [{"goto": "/dpr.html"}, {"wait_for": {"text": "dpcb 200x100", "timeout_ms": 5000}}],
        }
    )
    assert shoot.run(script, tmp_path / "take")["error"] == ""
