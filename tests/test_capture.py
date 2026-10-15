"""capture: device — the schema, the worker's screencast recorder (fake browser), and the host's
VFR → CFR frame assembly (fake ffmpeg). Host-free; one real-ffmpeg test skips without it."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from campaign import capture, deps, montage, render, shoot
from campaign.shotscript import ScriptError, capture_size, validate
from campaign.worker import pw_worker
from conftest import FakePlaywright, have_ffmpeg


def _raw(**top):
    return {"base_url": "http://app.test", "steps": [{"goto": "/"}, {"hold": 200}], **top}


# ── schema ────────────────────────────────────────────────────────────────────
def test_capture_defaults_to_css_so_existing_scripts_record_exactly_as_before():
    s = validate(_raw())
    assert s["capture"] == {"scale": "css"}
    assert pw_worker.video_size(s) == {"width": 1280, "height": 800}


def test_capture_device_shorthand_fills_the_defaults_and_doubles_the_frame():
    s = validate(_raw(capture="device", device_scale_factor=2, viewport={"width": 1280, "height": 720}))
    assert s["capture"] == {"scale": "device", "fps": 30, "quality": 92, "crf": 16}
    assert capture_size(s) == (2560, 1440)
    assert pw_worker.video_size(s) == {"width": 2560, "height": 1440}
    again = validate(_raw(capture=s["capture"], device_scale_factor=2))
    assert again["capture"] == s["capture"], "the normalized capture validates to itself"


def test_capture_mapping_options_are_bounded():
    s = validate(_raw(capture={"scale": "device", "fps": 60, "quality": 95, "crf": 18}))
    assert s["capture"] == {"scale": "device", "fps": 60, "quality": 95, "crf": 18}
    with pytest.raises(ScriptError) as e:
        validate(_raw(capture={"scale": "device", "fps": 120, "quality": 20, "crf": 40, "codec": "vp9"}))
    text = "\n".join(e.value.problems)
    for bit in ("capture.fps: 120 is outside 10..60", "capture.quality: 20", "capture.crf: 40", "`codec`"):
        assert bit in text


def test_capture_rejects_a_bad_scale_and_device_only_options_on_css():
    with pytest.raises(ScriptError, match="capture.scale must be one of css, device"):
        validate(_raw(capture="retina"))
    with pytest.raises(ScriptError, match="only apply to `scale: device`"):
        validate(_raw(capture={"scale": "css", "fps": 60}))


def test_capture_device_needs_a_scale_above_one_and_a_bounded_frame():
    with pytest.raises(ScriptError, match="device_scale_factor 1 records the same pixels"):
        validate(_raw(capture="device", device_scale_factor=1))
    with pytest.raises(ScriptError, match=r"is 5760×3240, over the 4096px side"):
        validate(_raw(capture="device", device_scale_factor=3, viewport={"width": 1920, "height": 1080}))
    # 1920×1080 at 2 → 3840×2160 (4K) is inside the cap.
    assert validate(_raw(capture="device", viewport={"width": 1920, "height": 1080}))["capture"]["scale"] == "device"


# ── VFR → CFR ─────────────────────────────────────────────────────────────────
def test_clean_frames_sorts_clamps_and_lets_the_later_of_equal_stamps_win():
    raw = [["b", 0.5], ["a", -0.2], ["c", 0.5], ["junk"], ["d", "x"], ["e", 1.0]]
    assert capture.clean_frames(raw) == [("a", 0.0), ("c", 0.5), ("e", 1.0)]


def test_cfr_schedule_holds_idle_stretches_and_both_ends():
    # Frames at 0.2s, 0.25s and 1.0s; recording stopped at 1.5s; 10 fps → 15 ticks.
    sched = capture.cfr_schedule([0.2, 0.25, 1.0], 1.5, 10)
    assert len(sched) == 15, "the video is exactly the take's length"
    assert sched[:2] == [0, 0], "before the first paint, the first frame is held back to t=0"
    assert sched[2] == 1, "0.25s lands on its NEAREST tick (0.2s ±half a tick), so 0.2 is replaced"
    assert sched[3:10] == [1] * 7, "the idle 0.25 → 1.0 stretch holds the last frame"
    assert sched[10:] == [2] * 5, "the last frame is held to the end"


def test_cfr_schedule_drops_frames_faster_than_the_rate_and_never_runs_backwards():
    times = [i / 60 for i in range(120)]  # 60 fps for 2 s
    sched = capture.cfr_schedule(times, 2.0, 30)
    assert len(sched) == 60
    assert sched == sorted(sched) and len(set(sched)) == 60, "every tick a newer frame, in order"
    assert capture.cfr_schedule([], 5.0, 30) == []
    assert len(capture.cfr_schedule([3.0], 1.0, 30)) == 90, "a frame after end_s stretches the end"


def _manifest(tmp_path, frames, end_s=1.0, size=(2560, 1600)):
    d = tmp_path / ".frames"
    d.mkdir()
    for name, _ in frames:
        (d / name).write_bytes(b"\xff\xd8" + name.encode())
    man = {"dir": str(d), "frames": [list(f) for f in frames], "end_s": end_s,
           "size": {"width": size[0], "height": size[1]}}  # fmt: skip
    (d / "frames.json").write_text(json.dumps(man))
    return d / "frames.json"


class FakeFF:
    def __init__(self):
        self.cmds: list[list[str]] = []
        self.seq: list[bytes] = []

    def __call__(self, cmd):
        self.cmds.append(cmd)
        pattern = Path(cmd[cmd.index("-i") + 1])
        self.seq = [p.read_bytes() for p in sorted(pattern.parent.glob("*.jpg"))]
        Path(cmd[-1]).write_bytes(b"mp4")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def test_assemble_links_one_frame_per_tick_and_encodes_h264(tmp_path, monkeypatch):
    monkeypatch.setattr(deps, "ffmpeg", lambda: "/usr/bin/ffmpeg")
    man = _manifest(tmp_path, [("f0.jpg", 0.1), ("f1.jpg", 0.45)], end_s=1.0)
    ff = FakeFF()
    info = capture.assemble(man, tmp_path / "take.mp4", fps=10, crf=16, runner=ff)
    assert info == {"video": str(tmp_path / "take.mp4"), "width": 2560, "height": 1600, "fps": 10,
                    "frames": 2, "ticks": 10, "duration_s": 1.0}  # fmt: skip
    assert ff.seq == [b"\xff\xd8f0.jpg"] * 4 + [b"\xff\xd8f1.jpg"] * 6
    cmd = " ".join(ff.cmds[0])
    for bit in ("-framerate 10", "-c:v libx264", "-crf 16", "-pix_fmt yuv420p", "+faststart", "-r 10",
                "scale=2560:1600:flags=lanczos:in_range=pc:out_range=tv,format=yuv420p"):  # fmt: skip
        assert bit in cmd
    assert not man.parent.exists(), "the frames are removed once the mp4 exists"


def test_assemble_refuses_no_frames_and_no_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(deps, "ffmpeg", lambda: "/usr/bin/ffmpeg")
    with pytest.raises(capture.CaptureError, match="sent no frames"):
        capture.assemble(_manifest(tmp_path, []), tmp_path / "x.mp4", runner=FakeFF())
    monkeypatch.setattr(deps, "ffmpeg", lambda: None)
    other = tmp_path / "b"
    other.mkdir()
    with pytest.raises(capture.CaptureError, match="needs ffmpeg"):
        capture.assemble(_manifest(other, [("f0.jpg", 0.0)]), other / "x.mp4", runner=FakeFF())


# ── the worker + shoot.run ────────────────────────────────────────────────────
def _device(**top):
    return validate(
        {"base_url": "http://app.test", "name": "hi", "capture": "device", "device_scale_factor": 2,
         "steps": [{"goto": "/"}, {"mark": "start"}, {"hold": 300}, {"mark": "end"}], **top}
    )  # fmt: skip


def test_device_capture_screencasts_device_pixels_and_the_host_assembles_an_mp4(tmp_path, monkeypatch):
    monkeypatch.setattr(deps, "ffmpeg", lambda: "/usr/bin/ffmpeg")
    pw, ff = FakePlaywright(), FakeFF()
    res = shoot.run(_device(), tmp_path / "take", playwright_factory=pw, runner=ff)

    ctx = pw.browser.contexts[0]
    assert "record_video_dir" not in ctx.kw, "Playwright's own VP8 recorder is off"
    start = dict(ctx.cdp.sent)["Page.startScreencast"]
    assert start == {"format": "jpeg", "quality": 92, "maxWidth": 2560, "maxHeight": 1600, "everyNthFrame": 1}
    acks = [p["sessionId"] for m, p in ctx.cdp.sent if m == "Page.screencastFrameAck"]
    assert acks == [1, 2, 3], "every frame is acked"
    assert ("Page.stopScreencast", {}) in ctx.cdp.sent
    assert Path(res["video"]).name == "hi.mp4" and Path(res["video"]).is_file()
    assert res["capture_info"]["width"] == 2560 and res["capture_info"]["frames"] == 3
    assert not (tmp_path / "take" / ".frames").exists()
    timing = json.loads(Path(res["timing"]).read_text())
    assert timing["video_size"] == {"width": 2560, "height": 1600} and timing["capture"]["scale"] == "device"


def test_device_capture_refuses_up_front_without_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(deps, "ffmpeg", lambda: None)
    pw = FakePlaywright()
    with pytest.raises(shoot.ShootError, match="needs|ffmpeg"):
        shoot.run(_device(), tmp_path, playwright_factory=pw)
    assert pw.browser is None, "no browser was started"


def test_a_failed_device_take_still_keeps_its_footage(tmp_path, monkeypatch):
    monkeypatch.setattr(deps, "ffmpeg", lambda: "/usr/bin/ffmpeg")
    pw = FakePlaywright(fail_on=lambda action, how, args: action == "click")
    s = _device(steps=[{"goto": "/"}, {"hold": 100}, {"click": {"role": "button", "name": "Nope"}}])
    with pytest.raises(shoot.ShootError, match="step 3") as e:
        shoot.run(s, tmp_path, playwright_factory=pw, runner=FakeFF())
    assert Path(e.value.result["video"]).suffix == ".mp4" and Path(e.value.result["video"]).is_file()


def test_the_frame_budget_drops_frames_and_says_so(tmp_path):
    rec = pw_worker.ScreencastRecorder(None, None, tmp_path, {"width": 4, "height": 4}, 90, max_bytes=5)

    class S:
        def send(self, *a):
            pass

    rec.session, rec.wall0 = S(), 100.0
    import base64

    for n in range(3):
        rec._on_frame({"data": base64.b64encode(b"abc").decode(), "sessionId": n, "metadata": {"timestamp": 100.5}})
    man = rec.stop(1.0)
    assert len(man["frames"]) == 1 and man["dropped"] == 2 and man["frames"][0] == ["f000000.jpg", 0.5]


# ── downstream: montage + the size ladder at 2K ───────────────────────────────
def test_a_hidpi_take_of_the_canvas_shape_downscales_onto_it_without_a_warning():
    opts = montage.normalize_output({})
    it = {"kind": "clip", "n": 1, "asset_id": 1, "title": "", "src_width": 2560, "src_height": 1440, "focus": None,
          "fit": None}  # fmt: skip
    assert montage.framing([it], opts) == []
    odd = {**it, "src_width": 2560, "src_height": 1600}
    (w,) = montage.framing([odd], opts)
    assert "capture: device" in w and "2560×1440" in w


def test_a_2k_take_can_still_be_stepped_under_a_10mb_limit():
    ladder = render.mp4_ladder(18, None, 2560)
    assert ladder[0] == (18, 2560), "quality first, at the full 2K width"
    assert min(w for _, w in ladder) <= 1280, "then down to ≤1280 wide, so a 10 MB attachment stays reachable"


@pytest.mark.integration
@pytest.mark.skipif(not have_ffmpeg(), reason="needs ffmpeg + ffprobe")
def test_real_ffmpeg_assembles_a_constant_rate_take(tmp_path):
    d = tmp_path / ".frames"
    d.mkdir()
    for n, color in enumerate(("red", "blue")):
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color={color}:s=64x48", "-frames:v", "1",
                        str(d / f"f{n}.jpg")], check=True)  # fmt: skip
    man = {"dir": str(d), "frames": [["f0.jpg", 0.0], ["f1.jpg", 0.9]], "end_s": 2.0,
           "size": {"width": 64, "height": 48}}  # fmt: skip
    (d / "frames.json").write_text(json.dumps(man))
    info = capture.assemble(d / "frames.json", tmp_path / "t.mp4", fps=30)
    probe = render.probe(info["video"])
    assert (probe["width"], probe["height"]) == (64, 48) and abs(probe["duration_s"] - 2.0) < 0.05
