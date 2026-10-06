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
    failed = e.value.result
    assert Path(failed["video"]).stat().st_size > 1000, "a failed take keeps its finalized recording"
    assert json.loads(Path(failed["timing"]).read_text())["failed_step"] == 2

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

    # The failed take plays, and a beat renders from it (to a time — it reached no marks).
    fsrc = render.probe(failed["video"])
    assert fsrc["width"] == bad["viewport"]["width"] and fsrc["duration_s"] > 0.5, (
        "flushed: ffprobe reads the whole failed take"
    )
    fspec = render.normalize_output({"name": "beat", "format": "mp4", "end": 0.5}, failed["marks"], fsrc["duration_s"])
    beat = render.render_output(failed["video"], tmp_path / "rf", fspec, marks=failed["marks"], source=fsrc)
    assert beat["size_bytes"] > 0 and 0.3 <= beat["duration_s"] <= 0.7


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


# ── several matches, for real: Playwright strict mode vs a wait and an action ──
TWICE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>body{font:18px system-ui}</style></head>
<body><p class="gone" style="display:none">3 passed (stale)</p><div id="out"></div><script>
  // The result lands late, in TWO places — a bold summary and an inline code span — and a hidden
  // stale copy sits FIRST in the DOM: a wait must neither refuse the ambiguity nor stall on it.
  setTimeout(() => { document.getElementById('out').innerHTML =
    '<p><strong>3 passed</strong></p><p>ran <code>pytest -q: 3 passed in 0.4s</code></p>'; }, 600);
</script></body></html>"""


@pytest.fixture
def twice_site(site, tmp_path):
    (Path(tmp_path) / "site" / "twice.html").write_text(TWICE_PAGE, encoding="utf-8")
    return site


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
def test_real_wait_on_a_twice_matched_text_succeeds_and_a_click_on_it_names_both(twice_site, tmp_path):
    base = {"base_url": twice_site, "viewport": {"width": 480, "height": 320}, "step_timeout_ms": 5000}
    ok = validate(
        {
            **base,
            "name": "twice",
            "steps": [
                {"goto": "/twice.html"},
                {"wait_for": {"text": "3 passed"}},  # 3 matches (one hidden) — any visible one will do
                {"wait_for": {"text": "3 passed", "state": "attached"}},
                {"click": {"text": "3 passed", "exact": True}},  # exact: only the bold summary
                {"click": {"text": "3 passed", "nth": "last"}},
                {"wait_for": {"text": "nothing like this", "state": "hidden"}},
            ],
        }
    )
    res = shoot.run(ok, tmp_path / "ok")
    assert res["error"] == ""

    bad = validate({**base, "name": "twice-bad", "steps": [
        {"goto": "/twice.html"}, {"wait_for": {"text": "3 passed"}}, {"click": {"text": "3 passed"}}]})  # fmt: skip
    with pytest.raises(shoot.ShootError) as e:
        shoot.run(bad, tmp_path / "bad")
    msg = str(e.value)
    assert msg.startswith("step 3 (click text='3 passed') failed: text='3 passed' matches 3 elements:"), msg
    assert "<p> '3 passed (stale)' (hidden)" in msg
    assert "<strong> '3 passed'" in msg and "<code> 'pytest -q: 3 passed in 0.4s'" in msg
    assert "`nth`" in msg and "exact: true" in msg


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
def test_check_targets_reports_ambiguous_targets_on_a_live_page(twice_site):
    sync_api = pytest.importorskip("playwright.sync_api")
    script = validate(
        {
            "base_url": twice_site,
            "steps": [
                {"goto": "/twice.html"},
                {"wait_for": {"text": "3 passed"}},
                {"click": {"text": "3 passed"}},
                {"click": {"text": "3 passed", "exact": True}},
                {"click": {"text": "3 passed", "nth": 5}},
                {"hover": {"role": "button", "name": "Nope"}},
            ],
        }
    )
    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(twice_site + "/twice.html")
            page.get_by_text("3 passed", exact=True).wait_for()
            report = pw_worker.check_targets(page, script)
        finally:
            browser.close()
    assert [(r["index"], r["status"], r["count"]) for r in report] == [
        (2, "ambiguous-wait", 3),
        (3, "ambiguous", 3),
        (4, "ok", 1),
        (5, "missing", 3),
        (6, "missing", 0),
    ]
    assert report[1]["matches"][1] == "<strong> '3 passed'"


HIDE_PAGE = """<!doctype html><html><body><p id="a">3 passed</p><p id="b">ran: 3 passed</p><script>
  setTimeout(() => { document.getElementById('a').style.display = 'none'; }, 300);
  setTimeout(() => { document.getElementById('b').style.display = 'none'; }, 1800);
</script></body></html>"""


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
def test_real_wait_for_hidden_on_a_twice_matched_text_waits_for_the_last_one(site, tmp_path):
    # The locator re-resolves every poll: when the first visible match hides, the next one
    # becomes "the first visible match" — so `hidden` holds only once NONE is visible.
    (Path(tmp_path) / "site" / "hide.html").write_text(HIDE_PAGE, encoding="utf-8")
    script = validate(
        {
            "base_url": site,
            "name": "hide",
            "step_timeout_ms": 5000,
            "steps": [
                {"goto": "/hide.html"},
                {"mark": "loaded"},
                {"wait_for": {"text": "3 passed", "state": "hidden"}},
                {"mark": "gone"},
            ],
        }
    )
    res = shoot.run(script, tmp_path / "hide")
    assert res["marks"]["gone"] - res["marks"]["loaded"] > 1.2, res["marks"]


# ── pre-seeded storage + init_script, for real: the app reads localStorage while it boots ──
STORAGE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><script>
  // Runs while the page PARSES — before any shot-script step could touch it. The init scripts
  // must already have run for the first render to see the seed.
  let w = 'none';
  try { w = JSON.parse(localStorage.getItem('protoagent.ui')).state.rightWidth; } catch (e) {}
  window.__first = 'width ' + w + ' / tab ' + (sessionStorage.getItem('tab') || 'none')
    + ' / init ' + (window.__campaignInit || 'no');
</script></head><body><p id="out"></p>
<script>document.getElementById('out').textContent = window.__first;</script></body></html>"""


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
def test_real_storage_is_seeded_before_the_app_boots_and_only_on_its_origin(site, tmp_path):
    (Path(tmp_path) / "site" / "storage.html").write_text(STORAGE_PAGE, encoding="utf-8")
    port = site.rsplit(":", 1)[1]
    other = f"http://localhost:{port}"  # same server, a DIFFERENT origin from 127.0.0.1
    script = validate(
        {
            "name": "seeded",
            "base_url": site,
            "viewport": {"width": 480, "height": 320},
            "device_scale_factor": 1,
            "step_timeout_ms": 5000,
            "storage": {
                "local": {"protoagent.ui": {"state": {"rightWidth": 860}, "version": 14}},
                "session": {"tab": "plugins"},
            },
            "init_script": "window.__campaignInit = 'yes';",
            "steps": [
                {"goto": "/storage.html"},
                {"wait_for": {"text": "width 860 / tab plugins / init yes", "exact": True}},
                {"goto": f"{other}/storage.html"},
                {"wait_for": {"text": "width none / tab none / init no", "exact": True}},
            ],
        }
    )
    res = shoot.run(script, tmp_path / "take")
    assert res["error"] == ""

    # Not vacuous: without the seed the same page boots with nothing.
    bare = validate({k: script[k] for k in ("base_url", "viewport", "step_timeout_ms")}
                    | {"steps": [{"goto": "/storage.html"},
                                 {"wait_for": {"text": "width none / tab none / init no", "exact": True}}]})  # fmt: skip
    assert shoot.run(bare, tmp_path / "bare")["error"] == ""


# ── the upload step, for real: a hidden file input behind a button + a drop zone ──
UPLOAD_PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
  body{font:16px system-ui} #drop{width:300px;height:80px;border:2px dashed #888;display:grid;place-items:center}
</style></head><body>
<input type="file" id="f" multiple hidden>
<button id="choose">Choose files</button>
<div id="drop" role="button" aria-label="Drop zone">Drop files here</div>
<button id="nothing">Does nothing</button>
<p id="out">no files</p>
<script>
  const f = document.getElementById('f');
  document.getElementById('choose').onclick = () => f.click();
  const drop = document.getElementById('drop');
  drop.onclick = () => f.click();  // a react-dropzone-style zone: click opens the chooser
  drop.ondragover = e => e.preventDefault();
  drop.ondrop = e => { e.preventDefault(); show(e.dataTransfer.files); };
  f.onchange = () => show(f.files);
  async function show(files) {
    const parts = [];
    for (const file of files) parts.push(file.name + ':' + (await file.text()).trim());
    document.getElementById('out').textContent = 'got ' + parts.join(', ');
  }
</script></body></html>"""


@pytest.fixture
def upload_site(site, tmp_path):
    (Path(tmp_path) / "site" / "upload.html").write_text(UPLOAD_PAGE, encoding="utf-8")
    assets = tmp_path / "demo-assets"
    assets.mkdir()
    for name, body in (("a.txt", "alpha"), ("b.txt", "bravo"), ("c.txt", "charlie"), ("d.txt", "delta")):
        (assets / name).write_text(body, encoding="utf-8")
    shoot.configure("", str(assets))
    return site, assets


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
def test_real_upload_through_a_button_a_drop_zone_and_the_hidden_input(upload_site, tmp_path):
    site, assets = upload_site
    base = {"base_url": site, "viewport": {"width": 480, "height": 320}, "device_scale_factor": 1}
    script = validate(
        {
            **base,
            "name": "upload",
            "steps": [
                {"goto": "/upload.html"},
                # A button that opens a native chooser (it clicks the hidden input).
                {
                    "upload": {
                        "role": "button",
                        "name": "Choose files",
                        "files": [str(assets / "a.txt"), str(assets / "b.txt")],
                    }
                },  # fmt: skip
                {"wait_for": {"text": "got a.txt:alpha, b.txt:bravo"}},
                # A drop zone whose click opens the same chooser.
                {"upload": {"target": {"role": "button", "name": "Drop zone"}, "files": str(assets / "c.txt")}},
                {"wait_for": {"text": "got c.txt:charlie"}},
                # The hidden <input type=file> itself — files set directly, no chooser.
                {"upload": {"selector": "#f", "files": [str(assets / "d.txt")]}},
                {"wait_for": {"text": "got d.txt:delta"}},
            ],
        }
    )
    res = shoot.run(script, tmp_path / "take")
    assert res["error"] == "" and len(res["steps"]) == 7

    # A target that opens no chooser fails the step and says what to target instead.
    bad = validate({**base, "step_timeout_ms": 1500, "steps": [
        {"goto": "/upload.html"},
        {"upload": {"role": "button", "name": "Does nothing", "files": [str(assets / "a.txt")]}}]})  # fmt: skip
    with pytest.raises(shoot.ShootError) as e:
        shoot.run(bad, tmp_path / "bad")
    assert "is not an <input type=file> and clicking it opened no file chooser" in str(e.value)

    # A file outside upload_dirs never reaches the browser.
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    leak = validate(
        {**base, "steps": [{"goto": "/upload.html"}, {"upload": {"selector": "#f", "files": [str(outside)]}}]}
    )
    with pytest.raises(shoot.ShootError) as e:
        shoot.run(leak, tmp_path / "leak")
    assert "outside the plugin's upload_dirs" in str(e.value)


# ── redaction reaches a terminal's canvas: xterm is filtered at write() ──────
# A stand-in for xterm.js's UMD bundle: it assigns `Terminal` onto the global (as the real one
# does) and "draws" whatever write() is given into `drawn` — which is exactly what a canvas
# renderer would paint. (The real xterm 5 bundle was checked by hand against this hook.)
FAKE_XTERM = """!function(e){class Terminal{constructor(){this.drawn=''}
open(p){this.element=document.createElement('div');this.element.className='xterm';
this.element.appendChild(document.createElement('canvas'));p.appendChild(this.element)}
write(d,cb){this.drawn+=typeof d==='string'?d:new TextDecoder().decode(d);cb&&cb()}
writeln(d,cb){this.write(d);this.write('\\r\\n',cb)}
reset(){this.drawn+='<reset>'}}
var x={Terminal:Terminal};for(var s in x)e[s]=x[s]}(globalThis);"""

TERM_PAGE = """<!doctype html><html><body><div id="host"></div>
<div class="xterm" id="esm"><canvas id="esm-canvas"></canvas></div>
<script>
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const s = document.createElement('script'); s.src = 'xterm.js';
s.onload = async () => {
  const t = new window.Terminal(); t.open(document.getElementById('host')); window.t = t;
  // Bytes arrive in chunks; a path split across two writes, even 150ms apart, must not leak.
  for (const [c, gap] of [['$ ls /Us', 150], ['ers/alice/dev\\r\\n', 5], ['mail bob@exam', 5],
                          ['ple.org\\r\\n', 5], ['C:\\\\Users\\\\carol\\\\x\\r\\n', 5]]) { t.write(c); await sleep(gap); }
  t.write(new TextEncoder().encode('bytes /home/dave/y\\r\\n'));
  t.writeln('tail /Users/erin');
  t.write('held /Users/fr'); t.reset(); t.write('ank\\r\\n');
  await sleep(1300); window.done = true;
};
document.head.appendChild(s);
</script></body></html>"""


@pytest.fixture
def term_site(site, tmp_path):
    root = Path(tmp_path) / "site"
    (root / "xterm.js").write_text(FAKE_XTERM, encoding="utf-8")
    (root / "term.html").write_text(TERM_PAGE, encoding="utf-8")
    return site


def _terminal_page(site, install):
    from playwright.sync_api import sync_playwright

    from campaign.threads import in_thread

    def check():
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            ctx = b.new_context()
            rules = pw_worker.redact_rules({"presets": ["home_paths", "emails"]})
            if install == "init":
                ctx.add_init_script(pw_worker.redact_init_js(rules))
            page = ctx.new_page()
            page.goto(site + "/term.html")
            if install == "step":  # a `redact` step after the terminal already exists
                page.wait_for_function("window.t")
                page.evaluate(f"({pw_worker.REDACT_JS})", pw_worker.redact_cfg(rules))
            page.wait_for_function("window.done", timeout=10_000)
            out = page.evaluate(
                """() => ({drawn: t.drawn, mark: t.element.getAttribute('data-campaign-redacted'),
                    own: getComputedStyle(t.element.querySelector('canvas')).filter,
                    esm: getComputedStyle(document.getElementById('esm-canvas')).filter})"""
            )
            b.close()
            return out

    return in_thread(check, 60)


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
def test_redaction_filters_what_a_terminal_draws_and_blurs_terminals_it_cannot_hook(term_site):
    out = _terminal_page(term_site, "init")
    drawn = out["drawn"]
    for name in ("alice", "bob", "carol", "dave", "erin", "/Users/fr"):
        assert name not in drawn, f"{name!r} reached the terminal canvas: {drawn!r}"
    assert "$ ls ~/dev\r\n" in drawn and "mail you@example.com\r\n" in drawn and "~\\x\r\n" in drawn
    assert "bytes ~/y\r\n" in drawn and "tail ~\r\n" in drawn
    # What was held back is written (redacted) BEFORE a reset, in order — a reset is a boundary.
    assert "held ~<reset>ank" in drawn
    assert out["mark"] == "1" and out["own"] == "none", "a hooked terminal is drawn clearly"
    assert out["esm"].startswith("blur"), "a terminal the hook can't reach is blurred, never shown raw"


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
def test_a_redact_step_hooks_a_terminal_that_already_exists(term_site):
    out = _terminal_page(term_site, "step")
    # Writes before the step went out raw (that's why redact belongs at the top of the script);
    # everything after it is filtered.
    assert "erin" not in out["drawn"] and "tail ~\r\n" in out["drawn"]


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
def test_without_redaction_the_terminal_draws_everything(term_site):
    out = _terminal_page(term_site, "none")
    assert "/Users/alice/dev" in out["drawn"] and out["esm"] == "none", "the checks above are not vacuous"


# ── pointer + keyboard steps, for real ───────────────────────────────────────
STEPS_PAGE = """<!doctype html><html><body style="margin:0;font:16px system-ui">
<div role="dialog" aria-label="First"><button onclick="log('saved first')">Save</button></div>
<div role="dialog" aria-label="Second"><button onclick="log('saved second')">Save</button></div>
<input aria-label="Count" onfocus="log('focused')"
  onkeydown="if (event.key === 'ArrowUp') { this.dataset.n = (+this.dataset.n || 0) + 1; log('up ' + this.dataset.n); }">
<div id="dock" style="position:relative;width:200px;height:60px;background:#333">
  <div role="separator" aria-label="Resize dock" style="position:absolute;right:0;top:0;width:10px;height:60px;background:#888"></div>
</div>
<div id="log"></div><div id="pos"></div>
<script>
  const log = (m) => { const p = document.createElement('p'); p.textContent = m; document.getElementById('log').appendChild(p); };
  const dock = document.getElementById('dock');
  let drag = null;
  document.querySelector('[role=separator]').addEventListener('mousedown', e => { drag = {x: e.clientX, w: dock.offsetWidth}; });
  document.addEventListener('mousemove', e => {
    if (drag) dock.style.width = (drag.w + e.clientX - drag.x) + 'px';
    document.getElementById('pos').textContent = 'pointer ' + e.clientX + ',' + e.clientY;
  });
  document.addEventListener('mouseup', () => { if (drag) { drag = null; log('dock ' + dock.offsetWidth); } });
</script></body></html>"""


@pytest.mark.integration
@pytest.mark.skipif(not have_chromium(), reason="playwright Chromium not installed")
def test_real_focus_within_press_repeat_drag_and_mouse_move(site, tmp_path):
    (Path(tmp_path) / "site" / "steps.html").write_text(STEPS_PAGE, encoding="utf-8")
    script = validate(
        {
            "base_url": site,
            "viewport": {"width": 640, "height": 400},
            "device_scale_factor": 1,
            "step_timeout_ms": 3000,
            "steps": [
                {"goto": "/steps.html"},
                # "Save" is on the page twice — within scopes the click to ONE dialog.
                {"click": {"role": "button", "name": "Save", "within": {"role": "dialog", "name": "Second"}}},
                {"wait_for": {"text": "saved second", "exact": True, "within": "#log"}},
                {"focus": {"label": "Count"}},
                {"wait_for": {"text": "focused", "exact": True}},
                {"press": {"key": "ArrowUp", "repeat": 4, "delay_ms": 20}},
                {"wait_for": {"text": "up 4", "exact": True}},
                {"drag": {"role": "separator", "name": "Resize dock", "to": {"dx": 150}}},
                {"wait_for": {"text": "dock 350", "exact": True}},
                {"mouse_move": {"x": 600, "y": 380, "smooth": False}},
                {"wait_for": {"text": "pointer 600,380", "exact": True}},
            ],
        }
    )
    res = shoot.run(script, tmp_path / "take")
    assert res["error"] == "", res["error"]
    assert "saved first" not in json.dumps(res)

    # Without `within`, the same click is ambiguous and the error names both matches.
    bare = validate({**{k: script[k] for k in ("base_url", "viewport", "device_scale_factor", "step_timeout_ms")},
                     "steps": [{"goto": "/steps.html"}, {"click": {"role": "button", "name": "Save"}}]})  # fmt: skip
    with pytest.raises(shoot.ShootError) as e:
        shoot.run(bare, tmp_path / "bare")
    assert "matches 2 elements" in str(e.value)
