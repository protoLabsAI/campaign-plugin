"""Test bootstrap — import the plugin with NO protoAgent host present.

The host loads a plugin under a synthetic package; the suite does the same so the modules'
relative imports (``from . import store``) resolve standalone. Executing ``__init__.py`` is
safe precisely because every host-only import lives inside a function.

Every test runs against a temp data dir (``CAMPAIGN_DIR`` autouse) so nothing can write into
a developer's real campaigns. Playwright is mocked at its entry point (``FakePlaywright``);
the real browser/ffmpeg tests are marked ``integration`` and skip when either is absent.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = "campaign"

if PKG not in sys.modules:
    _spec = importlib.util.spec_from_file_location(PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    assert _spec and _spec.loader
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[PKG] = _mod
    _spec.loader.exec_module(_mod)


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    from campaign import brand, deps, interpreter, limits, paths, shoot

    data = tmp_path / "data"
    monkeypatch.setenv("CAMPAIGN_DIR", str(data))
    # No real Social Studio brand kit may leak into a test.
    monkeypatch.setattr(brand, "_social_default_base", lambda: tmp_path / "social-default")
    for var in ("SOCIAL_BRAND_KIT", "SOCIAL_DIR", "PROTOAGENT_INSTANCE"):
        monkeypatch.delenv(var, raising=False)
    paths.configure("", "")
    limits.configure("")
    deps.configure("")
    brand.configure({}, None)
    shoot.configure("")
    # No real managed runtime (~/.protoagent/…) may be picked up; probes start fresh.
    monkeypatch.setattr(interpreter, "managed_python", lambda: None)
    interpreter.invalidate()
    monkeypatch.setattr(deps, "_REGISTRY", None)
    monkeypatch.setattr(deps, "_LAST_SIG", None)
    yield data


# ── a fake Playwright, mocked at the sync_playwright() boundary ──────────────
class FakeLocator:
    def __init__(self, page, how, args, kw):
        self.page, self.how, self.args, self.kw = page, how, args, kw
        self.idx = None

    def _log(self, action, **kw):
        self.page.calls.append(
            (action, self.how, self.args, dict(self.kw, **({"nth": self.idx} if self.idx is not None else {})), kw)
        )
        if self.page.fail_on and self.page.fail_on(action, self.how, self.args):
            raise RuntimeError(f"Timeout 15000ms exceeded.\nwaiting for {self.how}{self.args}")

    def nth(self, i):
        self.idx = i
        return self

    def click(self, **kw):
        self._log("click", **kw)

    def hover(self, **kw):
        self._log("hover", **kw)

    def fill(self, value, **kw):
        self._log("fill", value=value)

    def press(self, key, **kw):
        self._log("press", key=key)

    def wait_for(self, **kw):
        self._log("wait_for", **kw)

    def scroll_into_view_if_needed(self, **kw):
        self._log("scroll_into_view")

    def bounding_box(self, **kw):
        return {"x": 10, "y": 20, "width": 100, "height": 40}

    def screenshot(self, path, **kw):
        self._log("el_screenshot")
        Path(path).write_bytes(PNG_1x1)


class _Keyboard:
    def __init__(self, page):
        self.page = page

    def type(self, text, delay=0):
        self.page.calls.append(("keyboard.type", text, delay))

    def press(self, key):
        self.page.calls.append(("keyboard.press", key))


class _Mouse:
    def __init__(self, page):
        self.page = page

    def move(self, x, y, steps=1):
        self.page.calls.append(("mouse.move", x, y))

    def wheel(self, x, y):
        self.page.calls.append(("mouse.wheel", x, y))


class _Video:
    def __init__(self, path):
        self._path = path

    def path(self):
        return self._path


class _FakeHandle:
    def __init__(self, frame):
        self.frame = frame

    def content_frame(self):
        return self.frame

    def dispose(self):
        pass


class FakeFrame:
    """A child frame of a FakePage. Locators made in it log ``frame=<url>`` in their kwargs."""

    def __init__(self, page, url, selector=None, parent=None):
        self.page, self.url, self.selector, self.parent = page, url, selector, parent
        self.child_frames: list[FakeFrame] = []
        self.detached = False
        self.calls = page.calls
        self.fail_on = page.fail_on

    def is_detached(self):
        return self.detached

    def query_selector_all(self, sel):
        return [_FakeHandle(f) for f in self.child_frames if f.selector == sel]

    def _loc(self, how, args, kw):
        return FakeLocator(self.page, how, args, dict(kw, frame=self.url))

    def locator(self, sel):
        return self._loc("locator", (sel,), {})

    def get_by_role(self, role, **kw):
        return self._loc("role", (role,), kw)

    def get_by_text(self, text, **kw):
        return self._loc("text", (text,), kw)

    def get_by_label(self, text, **kw):
        return self._loc("label", (text,), kw)

    def get_by_placeholder(self, text, **kw):
        return self._loc("placeholder", (text,), kw)

    def get_by_test_id(self, text):
        return self._loc("test_id", (text,), {})

    def add_style_tag(self, content):
        self.calls.append(("frame.style", self.url, content))

    def evaluate(self, js, arg=None):
        self.calls.append(("frame.evaluate", self.url, js[:40], arg))

    def wait_for_load_state(self, state, **kw):
        self.calls.append(("frame.load_state", self.url, state))

    def frame_element(self):
        frame = self

        class _El:
            def screenshot(self, path, **kw):
                frame.calls.append(("frame_element.screenshot", frame.url))
                Path(path).write_bytes(PNG_1x1)

        return _El()


class FakePage:
    def __init__(self, ctx):
        self.ctx = ctx
        self.calls = ctx.calls
        self.fail_on = ctx.fail_on
        self.keyboard = _Keyboard(self)
        self.mouse = _Mouse(self)
        vdir = Path(ctx.kw["record_video_dir"]) if ctx.kw.get("record_video_dir") else None
        self.video = _Video(str(vdir / "page@abc.webm")) if vdir else None
        self.content = ""
        # The per-step default timeouts the worker sets (kept off `calls` so call-sequence
        # assertions stay about what happens on screen).
        self.default_timeouts: list[float] = []
        self.child_frames: list[FakeFrame] = []
        # Frames to attach after N wait_for_timeout polls: [(polls_left, url, selector, parent_url)]
        self.pending_frames: list[list] = list(ctx.browser.pw.frames)

    @property
    def main_frame(self):
        return self

    @property
    def frames(self):
        out, todo = [self], list(self.child_frames)
        while todo:
            f = todo.pop(0)
            out.append(f)
            todo.extend(f.child_frames)
        return out

    def query_selector_all(self, sel):
        return [_FakeHandle(f) for f in self.child_frames if f.selector == sel]

    def _attach_due(self):
        for item in list(self.pending_frames):
            item[0] -= 1
            if item[0] <= 0:
                self.pending_frames.remove(item)
                _, url, selector, parent_url = item
                parent = next((f for f in self.frames if f is not self and f.url == parent_url), self)
                parent.child_frames.append(FakeFrame(self, url, selector, None if parent is self else parent))

    def set_default_timeout(self, ms):
        self.default_timeouts.append(ms)

    def set_default_navigation_timeout(self, ms):
        pass

    def goto(self, url, **kw):
        self.calls.append(("goto", url, kw))
        self._attach_due()
        if self.fail_on and self.fail_on("goto", url, ()):
            raise RuntimeError("net::ERR_CONNECTION_REFUSED")

    def locator(self, sel):
        return FakeLocator(self, "locator", (sel,), {})

    def get_by_role(self, role, **kw):
        return FakeLocator(self, "role", (role,), kw)

    def get_by_text(self, text, **kw):
        return FakeLocator(self, "text", (text,), kw)

    def get_by_label(self, text, **kw):
        return FakeLocator(self, "label", (text,), kw)

    def get_by_placeholder(self, text, **kw):
        return FakeLocator(self, "placeholder", (text,), kw)

    def get_by_test_id(self, text):
        return FakeLocator(self, "test_id", (text,), {})

    def wait_for_timeout(self, ms):
        self.calls.append(("wait_for_timeout", ms))
        self._attach_due()

    def wait_for_load_state(self, state, **kw):
        self.calls.append(("load_state", state))

    def screenshot(self, path, **kw):
        self.calls.append(("screenshot", Path(path).name, kw))
        size = self.ctx.browser.pw.screenshot_bytes(kw)
        Path(path).write_bytes(size)

    def evaluate(self, js, arg=None):
        self.calls.append(("evaluate", js[:40], arg))
        return True

    def add_style_tag(self, content):
        self.calls.append(("style", content))

    def set_content(self, html, **kw):
        self.content = html
        self.calls.append(("set_content", len(html)))


class FakeContext:
    def __init__(self, browser, kw):
        self.browser, self.kw = browser, kw
        self.calls = browser.pw.calls
        self.fail_on = browser.pw.fail_on
        self.init_scripts = []
        self.routes = []  # (url matcher, handler) — the request guards shoot installs
        self.page = None

    def route(self, matcher, handler):
        self.routes.append((matcher, handler))

    def set_default_timeout(self, ms):
        self.calls.append(("default_timeout", ms))

    def add_init_script(self, js):
        self.init_scripts.append(js)

    def new_page(self, **kw):
        self.page = FakePage(self)
        return self.page

    def close(self):
        if self.page and self.page.video:
            p = Path(self.page.video.path())
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\x1a\x45\xdf\xa3fakewebm")
        self.calls.append(("context.close",))


class FakeBrowser:
    def __init__(self, pw):
        self.pw = pw
        self.contexts = []

    def new_context(self, **kw):
        c = FakeContext(self, kw)
        self.contexts.append(c)
        return c

    def new_page(self, **kw):
        c = FakeContext(self, kw)
        self.contexts.append(c)
        return c.new_page()

    def close(self):
        self.pw.calls.append(("browser.close",))


class _Chromium:
    def __init__(self, pw):
        self.pw = pw

    def launch(self, **kw):
        self.pw.calls.append(("launch", kw))
        self.pw.browser = FakeBrowser(self.pw)
        return self.pw.browser


class FakePlaywright:
    """Call it to get a context manager, exactly like ``sync_playwright()``."""

    def __init__(self, fail_on=None, png_size=2_000, jpeg_sizes=None, frames=None):
        self.calls = []
        # Child frames the page grows: (after N polls/gotos, url, iframe selector, parent url|None)
        self.frames = [list(f) for f in (frames or [])]
        self.fail_on = fail_on
        self.chromium = _Chromium(self)
        self.browser = None
        self.png_size = png_size
        self.jpeg_sizes = list(jpeg_sizes or [])

    def screenshot_bytes(self, kw):
        if kw.get("type") == "jpeg":
            n = self.jpeg_sizes.pop(0) if self.jpeg_sizes else 500
            return b"\xff\xd8" + b"0" * n
        return PNG_1x1 + b"\0" * max(0, self.png_size - len(PNG_1x1))

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


@pytest.fixture
def fake_pw():
    return FakePlaywright()


class FakeRegistry:
    """Stands in for the host's PluginRegistry — records what register() contributes."""

    def __init__(self, config=None):
        self.config = config or {}
        self.plugin_id = "campaign"
        self.tools, self.routers, self.subagents, self.events = [], [], [], []
        self.gaps: dict[str, tuple] = {}
        self.steps: dict[str, object] = {}
        self.media = []

    def register_tool(self, t):
        self.tools.append(t)

    def register_tools(self, ts):
        self.tools.extend(ts)

    def register_router(self, router, prefix):
        self.routers.append((prefix, router))

    def register_subagent(self, cfg):
        self.subagents.append(cfg)

    def register_setup_step(self, step, fn):
        self.steps[step] = fn

    def report_setup_gap(self, key, message, *, label=None, action=None):
        if message is None:
            self.gaps.pop(key, None)
        else:
            self.gaps[key] = (message, action)

    def save_media(self, data, mime, meta=None):
        self.media.append((data, mime, meta))
        return {"id": "m1", "url": f"/media/m{len(self.media)}", "path": str(data), "mime": mime}

    def emit(self, topic, data):
        self.events.append((topic, data))

    def tool_names(self):
        return [getattr(t, "name", "?") for t in self.tools]

    def tool(self, name):
        for t in self.tools:
            if getattr(t, "name", None) == name:
                return t
        raise KeyError(f"no tool named {name!r} — have {self.tool_names()}")


@pytest.fixture
def registry():
    return FakeRegistry()


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def have_chromium() -> bool:
    """A Python with playwright + its Chromium resolvable the way production resolves it."""
    try:
        from campaign import interpreter

        return interpreter.resolve().ready
    except Exception:  # noqa: BLE001
        return False


def in_process_worker(pw):
    """A ``shoot.run_worker`` stand-in that runs the REAL worker code in-process against the
    fake browser — the job still round-trips through JSON, exactly as on the wire."""
    import json

    from campaign.worker import pw_worker

    def run_worker(job, timeout, playwright_factory=None):
        return pw_worker.run_job(json.loads(json.dumps(job)), playwright_factory or pw)

    return run_worker
