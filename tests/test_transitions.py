"""Signature transitions (transitions.py): the contract — exact easing, frame quantization,
endpoint ownership, geometry/coverage — the manifest the console shares, and REAL ffmpeg renders
checked frame by frame against the reference weights."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from campaign import montage, paths, store
from campaign import transitions as T
from conftest import have_ffmpeg


def test_the_easing_is_exact_ease_in_out_cubic():
    assert (T.ease(0), T.ease(0.5), T.ease(1)) == (0.0, 0.5, 1.0)
    assert T.ease(0.25) == 4 * 0.25**3 and T.ease(0.75) == 1 - 0.5**3 / 2
    assert T.ease(-1) == 0 and T.ease(2) == 1, "u is clamped"
    for u in (0.1, 0.3, 0.45):
        assert T.ease(u) + T.ease(1 - u) == pytest.approx(1.0), "point-symmetric about ½"


@pytest.mark.parametrize(("style", "fps", "n"), [
    ("edge_sweep", 24, 14), ("edge_sweep", 30, 18), ("edge_sweep", 60, 36),
    ("boot_seam", 24, 10), ("boot_seam", 30, 12), ("boot_seam", 60, 24),
])  # fmt: skip
def test_frame_quantization(style, fps, n):
    assert T.frames(style, fps) == n and T.duration_s(style, fps) == n / fps
    assert [T.progress(k, n) for k in (0, n - 1)] == [0.0, (n - 1) / n], "k = 0 … N−1; the next shot owns u = 1"


def test_reference_weights_endpoints_and_antialiased_edges():
    w, h = 2560, 1440
    for style in T.SIGNATURES:
        for x, y in ((0, 0), (1280, 720), (2559, 1439)):
            assert T.pixel(style, 0.0, x, y, w, h) == {"a": 1, "accent": 0, "b": 0}, "k = 0 is the outgoing shot"
        for u in (0.25, 0.5, 0.75):
            p = T.pixel(style, u, 900, 300, w, h)
            assert sum(p.values()) == pytest.approx(1.0)
    # Edge sweep at u = ½: reveal at x = 1280, a 32 px band 1248–1280 at 2560 wide.
    assert T.pixel("edge_sweep", 0.5, 1247, 0, w, h) == {"a": 0, "accent": 0, "b": 1}
    assert T.pixel("edge_sweep", 0.5, 1260, 0, w, h) == {"a": 0, "accent": 1, "b": 0}
    assert T.pixel("edge_sweep", 0.5, 1280, 0, w, h) == {"a": 1, "accent": 0, "b": 0}
    g = T.edge_sweep(1 / 18, w)
    edge = int(g["reveal"])
    p = T.pixel("edge_sweep", 1 / 18, edge, 0, w, h)
    assert 0 < p["accent"] < 1 and p["a"] == pytest.approx(1 - (g["reveal"] - edge)), "subpixel coverage, not a step"
    # Boot seam at u = ½: open band y 360–1080; seam lines at opacity ½.
    s = T.boot_seam(0.5, h)
    assert (s["y0"], s["y1"], s["opacity"]) == (360.0, 1080.0, 0.5)
    assert T.pixel("boot_seam", 0.5, 0, 720, w, h)["b"] == 1 and T.pixel("boot_seam", 0.5, 0, 100, w, h)["a"] == 1
    assert T.pixel("boot_seam", 0.5, 0, 360, w, h)["accent"] == pytest.approx(0.5)


def test_the_checked_in_manifest_is_the_module():
    assert json.loads(T.MANIFEST_PATH.read_text()) == json.loads(json.dumps(T.manifest())), (
        "transitions.json is stale — run `python transitions.py`"
    )


def test_signatures_are_fixed_length_and_reduced_motion_cuts():
    with pytest.raises(montage.MontageError, match="fixed 600 ms signature"):
        montage.parse_sequence([{"clip": 1}, {"clip": 2, "transition": {"style": "edge_sweep", "duration": 1}}])
    items = [{"kind": "clip", "n": n, "length": 3.0, "transition": None, "theme_color": None} for n in (1, 2, 3)]
    items[2]["transition"] = {"style": "fade", "duration": 0.5}
    look = {"colors": {"accent": "#9b87f2", "bg": "#0a0a0c"}}
    tl = montage.plan([dict(i) for i in items], montage.normalize_output({}), look)
    sweep, fade = tl["transitions"]
    assert (sweep["style"], sweep["frames"], sweep["duration"], sweep["color"]) == ("edge_sweep", 18, 0.6, "#9b87f2")
    assert tl["starts"] == [0.0, 2.4, 4.9]
    rm = montage.plan([dict(i) for i in items], montage.normalize_output({"reduced_motion": True}), look)
    assert [(t["style"], t.get("reduced")) for t in rm["transitions"]] == [("edge_sweep", True), ("fade", None)]
    assert rm["total"] == tl["total"], "reduced motion keeps the N-frame interval: the timeline doesn't move"
    items[2]["transition"] = {"style": "slideleft", "duration": 0.5}
    rm2 = montage.plan([dict(i) for i in items], montage.normalize_output({"reduced_motion": True}), look)
    assert rm2["transitions"][1]["style"] == "cut" and rm2["transitions"][1]["reduced_from"] == "slideleft"


def test_the_contract_oracle_and_reduced_ownership():
    """protoContent#606's analytic reference frames (2560×1440) and the reduced-motion split."""
    w, h = T.ORACLE["width"], T.ORACLE["height"]
    for smp in T.ORACLE["samples"]:
        u = smp["u"]
        assert T.ease(u) == smp["e"]
        g = T.edge_sweep(u, w)
        assert g["reveal"] == smp["edgeX"]
        assert ([g["band_x0"], g["band_x1"]] if g["band"] else None) == smp["band"]
        b = T.boot_seam(u, h)
        assert [b["y0"], b["y1"]] == smp["bootBounds"] and b["opacity"] == smp["seamOpacity"]
    assert T.boot_seam(0.25, h)["half_width"] * 2 == h / 240 == 6
    assert [T.reduced_owner(k, 18) for k in (8, 9)] == ["a", "b"]
    assert [T.reduced_owner(k, 7) for k in range(7)].count("a") == 4, "odd N: A has one more frame"
    assert T.reduced_owner(0, 1) == "a"
    assert T.pixel("edge_sweep", 0.4, 0, 0, w, h, reduced=True)["a"] == 1
    m = T.manifest()
    assert (m["contractId"], m["schemaVersion"]) == ("protolabs.brand-video/1", 1)
    with pytest.raises(ValueError):
        T.frames("edge_sweep", float("nan"))


def test_overlapping_seams_union_their_coverage():
    # Near u = 1 − ε both seams sit at the canvas edges; at u ≈ 0 they overlap at the centre.
    g = T.boot_seam(0.02, 1440)
    assert g["y1"] - g["y0"] < 2 * g["half_width"], "the two seam rectangles overlap here"
    p = T.pixel("boot_seam", 0.02, 0, 720, 2560, 1440)
    assert p["accent"] == pytest.approx(g["opacity"]), "union coverage 1, opacity applied once — not doubled"


def _rgb(path: Path, frame: int, x: int, y: int) -> tuple[int, int, int]:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"select=eq(n\\,{frame}),crop=2:2:{x}:{y},format=rgb24",
         "-frames:v", "1", "-fps_mode", "passthrough", "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return tuple(raw[:3])


def _near(rgb, want, tol=14):
    return all(abs(a - b) <= tol for a, b in zip(rgb, want))


@pytest.mark.integration
@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg not installed")
def test_real_signature_transitions_are_frame_exact(tmp_path):
    c = store.create_campaign("Launch")
    d = paths.campaign_dir(c["id"], "Launch")
    ids = []
    for name, color in (("red", "0xff0000"), ("blue", "0x0000ff"), ("green", "0x00ff00")):
        p = d / f"{name}.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=1280x720:r=30:d=2",
                        "-pix_fmt", "yuv420p", "-c:v", "libx264", "-qp", "0", str(p)], check=True)  # fmt: skip
        ids.append(store.add_asset(c["id"], "clip", name, path=str(p), status="rendered")["id"])
    seq = [{"clip": ids[0]}, {"clip": ids[1]}, {"clip": ids[2], "transition": "boot_seam"}]
    r = montage.render_montage(c["id"], seq, {"size": "1280x720", "name": "sig", "poster": False, "crf": 4}, d / "m")
    # 2 s + 2 s − 0.6 + 2 s − 0.4 = 5.0 s = 150 frames, exactly.
    n = int(subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries", "stream=nb_read_frames",
                            "-of", "csv=p=0", r["path"]], capture_output=True, text=True).stdout)  # fmt: skip
    assert n == 150
    mp4, accent = Path(r["path"]), (124, 92, 255)  # the brand accent (defaults: #7c5cff)
    # Edge sweep: frames 42 … 59 (k = 0 … 17). k = 0 is pure red; k = 9 (u = ½) puts the edge at 640.
    assert _near(_rgb(mp4, 42, 640, 360), (255, 0, 0)) and _near(_rgb(mp4, 42, 0, 360), (255, 0, 0))
    assert _near(_rgb(mp4, 51, 600, 360), (0, 0, 255)), "left of the band: the incoming shot"
    assert _near(_rgb(mp4, 51, 630, 360), accent, 24), "the 16 px band (32/2560 of 1280) is the brand accent"
    assert _near(_rgb(mp4, 51, 660, 360), (255, 0, 0)), "right of the edge: still the outgoing shot"
    assert _near(_rgb(mp4, 60, 1200, 360), (0, 0, 255)), "u = 1 belongs to the incoming shot"
    # Boot seam into green: frames 90 … 101 (the sweep's 102 frames − 12); k = 6 (u = ½) opens y 180–540.
    assert _near(_rgb(mp4, 90, 640, 360), (0, 0, 255)), "k = 0: no seam residue"
    assert _near(_rgb(mp4, 96, 640, 360), (0, 255, 0)) and _near(_rgb(mp4, 96, 640, 80), (0, 0, 255))
    assert _near(_rgb(mp4, 102, 640, 10), (0, 255, 0))
