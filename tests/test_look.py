"""campaign_view — the agent SEES its stills and clip frames (core's multimodal envelope).

Core's ``graph.sdk.multimodal_tool_result`` is faked at the boundary (with core's own caps:
3 images, 2 MiB each); frame extraction runs real ffmpeg in the ``integration`` tests.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import subprocess
import sys
import types
import zlib
from pathlib import Path

import campaign
import pytest
from campaign import deps, look, paths, store
from conftest import have_ffmpeg


def _png(w: int, h: int) -> bytes:
    """A tiny real RGB PNG (stdlib only)."""
    rows = b"".join(b"\x00" + bytes(v for x in range(w) for v in (x % 256, y % 256, 128)) for y in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


class FakeCore:
    """graph.sdk with core's multimodal_tool_result CONTRACT (caps enforced the same way)."""

    MAX_IMAGES_PER_RESULT = 3
    MAX_IMAGE_BYTES = 2 * 1024 * 1024

    def __init__(self):
        self.calls: list[tuple[str, list[dict]]] = []

    def multimodal_tool_result(self, text, images):
        if not images or len(images) > self.MAX_IMAGES_PER_RESULT:
            raise ValueError(f"too many images: {len(images)}")
        for img in images:
            assert img["mime"].startswith("image/")
            if len(base64.b64decode(img["b64"], validate=True)) > self.MAX_IMAGE_BYTES:
                raise ValueError("image over MAX_IMAGE_BYTES")
        self.calls.append((text, images))
        return "\x1e[multimodal-tool-v1]" + json.dumps({"text": text, "images": images})


@pytest.fixture
def core(monkeypatch):
    fake = FakeCore()
    graph = types.ModuleType("graph")
    sdk = types.ModuleType("graph.sdk")
    sdk.multimodal_tool_result = fake.multimodal_tool_result
    graph.sdk = sdk
    monkeypatch.setitem(sys.modules, "graph", graph)
    monkeypatch.setitem(sys.modules, "graph.sdk", sdk)
    return fake


@pytest.fixture
def tools(registry):
    campaign.register(registry)
    return registry


def view(reg, **kw):
    return reg.tool("campaign_view").invoke(kw)


@pytest.fixture
def camp():
    c = store.create_campaign("Launch X")
    return c, paths.campaign_dir(c["id"], c["name"])


def test_a_still_comes_back_as_an_image_the_model_sees(tools, core, camp, monkeypatch):
    monkeypatch.setattr(deps, "ffmpeg", lambda: None)  # no ffmpeg: the small PNG goes as-is
    c, root = camp
    p = root / "shots" / "s.png"
    p.parent.mkdir(parents=True)
    p.write_bytes(_png(64, 40))
    a = store.add_asset(c["id"], "still", "hero: s", status="captured", path=str(p))
    out = view(tools, campaign_id=c["id"], asset_id=a["id"])
    assert out.startswith("\x1e[multimodal-tool-v1]")
    text, images = core.calls[0]
    assert f"asset #{a['id']} (still, captured)" in text and "image 1: still" in text
    assert images == [{"b64": base64.b64encode(p.read_bytes()).decode(), "mime": "image/png"}]


def test_without_core_vision_it_says_so_never_pretends(tools, camp, monkeypatch):
    monkeypatch.setattr(deps, "ffmpeg", lambda: None)
    c, root = camp
    (root / "s.png").write_bytes(_png(8, 8))
    # (a) no graph.sdk at all (an older or absent core)
    monkeypatch.setitem(sys.modules, "graph", None)
    out = view(tools, campaign_id=c["id"], path="s.png")
    assert "Vision isn't available" in out and "NOT reviewed" in out and "\x1e" not in out
    # (b) graph.sdk without the helper
    graph, sdk = types.ModuleType("graph"), types.ModuleType("graph.sdk")
    graph.sdk = sdk
    monkeypatch.setitem(sys.modules, "graph", graph)
    monkeypatch.setitem(sys.modules, "graph.sdk", sdk)
    assert "Vision isn't available" in view(tools, campaign_id=c["id"], path="s.png")


def test_paths_are_contained_to_this_campaigns_dir(tools, core, camp, tmp_path, monkeypatch):
    monkeypatch.setattr(deps, "ffmpeg", lambda: None)
    c, root = camp
    outside = tmp_path / "secret.png"
    outside.write_bytes(_png(8, 8))
    assert "not a file inside this campaign's media dir" in view(tools, campaign_id=c["id"], path=str(outside))
    assert "not a file inside" in view(tools, campaign_id=c["id"], path="../../../secret.png")
    if hasattr(os, "symlink"):
        (root / "link.png").symlink_to(outside)
        assert "not a file inside" in view(tools, campaign_id=c["id"], path="link.png")
    # Another campaign's dir and assets are out of bounds too.
    other = store.create_campaign("Other")
    odir = paths.campaign_dir(other["id"], other["name"])
    (odir / "o.png").write_bytes(_png(8, 8))
    assert "not a file inside" in view(tools, campaign_id=c["id"], path=str(odir / "o.png"))
    oa = store.add_asset(other["id"], "still", "o", status="captured", path=str(odir / "o.png"))
    assert f"no asset #{oa['id']} in campaign {c['id']}" in view(tools, campaign_id=c["id"], asset_id=oa["id"])
    # An asset whose path points outside its campaign is refused as well.
    bad = store.add_asset(c["id"], "still", "x", status="captured", path=str(outside))
    assert "not a file inside" in view(tools, campaign_id=c["id"], asset_id=bad["id"])
    assert core.calls == []


def test_bad_requests_are_explained(tools, core, camp, monkeypatch):
    c, root = camp
    (root / "notes.txt").write_text("hi")
    assert "can only look at" in view(tools, campaign_id=c["id"], path="notes.txt")
    planned = store.add_asset(c["id"], "clip", "hero")
    assert "has no file yet" in view(tools, campaign_id=c["id"], asset_id=planned["id"])
    (root / "s.png").write_bytes(_png(8, 8))
    assert "max_side must be" in view(tools, campaign_id=c["id"], path="s.png", max_side=99999)
    assert "frames must be 0..12" in view(tools, campaign_id=c["id"], path="s.png", frames=50)
    assert "a still is a single frame" in view(tools, campaign_id=c["id"], path="s.png", around="cut")
    (root / "t.webm").write_bytes(b"\x1aE\xdf\xa3")
    monkeypatch.setattr(deps, "ffmpeg", lambda: None)
    assert "needs ffmpeg" in view(tools, campaign_id=c["id"], path="t.webm")


def test_frame_times_are_slice_centres():
    assert look.frame_times(6.0, 3) == [1.0, 3.0, 5.0]
    assert look.frame_times(0, 4) == [0.0]


def test_the_producer_can_look():
    from campaign import subagents

    assert "campaign_view" in subagents.PRODUCER_TOOLS
    skill = (Path(campaign.__file__).parent / "skills" / "asset-review" / "SKILL.md").read_text()
    assert "campaign_view" in skill


# ── real ffmpeg ───────────────────────────────────────────────────────────────
def _dims(data: bytes, tmp: Path) -> tuple[int, int]:
    f = tmp / "probe.jpg"
    f.write_bytes(data)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "json", str(f)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    s = json.loads(out)["streams"][0]
    return s["width"], s["height"]


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True)


@pytest.mark.integration
@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg not installed")
def test_real_frames_from_a_clip(tools, core, camp, tmp_path):
    c, root = camp
    clip = root / "shots" / "take.webm"
    clip.parent.mkdir(parents=True)
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=4", "-c:v", "libvpx", "-b:v", "500k", str(clip))
    a = store.add_asset(c["id"], "clip", "take", status="captured", path=str(clip), meta={"marks": {"cut": 2.0}})

    # 1–3 frames: one full-size image each, evenly spaced, all different moments.
    view(tools, campaign_id=c["id"], asset_id=a["id"], frames=3)
    text, imgs = core.calls[-1]
    assert len(imgs) == 3 and all(i["mime"] == "image/jpeg" for i in imgs)
    raws = [base64.b64decode(i["b64"]) for i in imgs]
    assert all(_dims(r, tmp_path) == (640, 360) for r in raws)
    assert len(set(raws)) == 3
    assert "frame @ 0.67s" in text and "frame @ 2.00s" in text and "marks: cut=2.00s" in text

    # 4+ frames: ONE contact sheet, bounded by max_side; a partial last row still renders.
    for n, (cols, rows) in ((6, (3, 2)), (5, (3, 2)), (12, (4, 3))):
        view(tools, campaign_id=c["id"], asset_id=a["id"], frames=n)
        text, imgs = core.calls[-1]
        assert len(imgs) == 1 and f"contact sheet {cols}×{rows}" in text
        w, h = _dims(base64.b64decode(imgs[0]["b64"]), tmp_path)
        assert w <= 1280 and h <= 1280 and w > 2 * h / rows, (n, w, h)

    # around a mark: the frame just before and just after, plus the sheet — never over 3.
    view(tools, campaign_id=c["id"], asset_id=a["id"], frames=4, around="cut")
    text, imgs = core.calls[-1]
    assert len(imgs) == 3
    assert "before cut @ 1.70s" in text and "after cut @ 2.30s" in text and "contact sheet" in text
    before, after = (base64.b64decode(i["b64"]) for i in imgs[:2])
    assert before != after and _dims(before, tmp_path) == (640, 360)

    # seconds work too; a mark the take doesn't have is named.
    view(tools, campaign_id=c["id"], asset_id=a["id"], frames=0, around="3.5")
    assert len(core.calls[-1][1]) == 2
    out = view(tools, campaign_id=c["id"], asset_id=a["id"], around="nope")
    assert "neither seconds nor a mark" in out and "cut" in out


@pytest.mark.integration
@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg not installed")
def test_real_still_is_downscaled_to_a_bounded_jpeg(tools, core, camp, tmp_path):
    c, root = camp
    big = root / "shots" / "retina.png"
    big.parent.mkdir(parents=True)
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=2560x1600", "-frames:v", "1", str(big))
    view(tools, campaign_id=c["id"], path=str(big))
    _, imgs = core.calls[-1]
    data = base64.b64decode(imgs[0]["b64"])
    assert imgs[0]["mime"] == "image/jpeg" and data[:2] == b"\xff\xd8"
    assert _dims(data, tmp_path) == (1280, 800) and len(data) < look.MAX_IMAGE_BYTES
    view(tools, campaign_id=c["id"], path="shots/retina.png", max_side=640)
    assert _dims(base64.b64decode(core.calls[-1][1][0]["b64"]), tmp_path) == (640, 400)
