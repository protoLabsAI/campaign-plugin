"""campaign_produce_video: one request → a finished, self-reviewed video, as a job.

The browser, ffmpeg, the montage renderer, the card page renderer, the media store and the chat
nudge are all faked at the produce.Hooks seam, so the whole pipeline (record → measure → render
→ gates → retry → deliver) runs host-free."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from campaign import montage, produce, store

ATLAS_REPLY = """Shot script for tour overview — about 85 s of footage between mark start and mark end:

```yaml
name: "atlas-overview"
description: "Atlas walkthrough — protoAgent in 85 seconds (8 slides, ~85 s), recorded continuously in record mode."
base_url: "http://localhost:7870"
viewport: {width: 1280, height: 720}
color_scheme: dark
cursor: false
step_timeout_ms: 20000
total_timeout_s: 174
steps:
  - goto: /app/
  - wait_for: {selector: '[data-atlas-state=presenting]', frame: "/plugins/atlas/view", timeout_ms: 10000}
  - mark: start
  - wait_for: {selector: '[data-atlas-state=tour-done]', frame: "/plugins/atlas/view", timeout_ms: 104740}
  - mark: end
  - hold: 500
```
"""


class World:
    """The fake outside world, with knobs a test turns."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.shots = 0
        self.renders: list[dict] = []
        self.blank_attempts: set[int] = set()  # record attempts whose footage has blank frames
        self.gh_size = 7_000_000
        self.gh_size_at_24 = 6_000_000
        self.notified: list[tuple] = []
        self.media: list[tuple] = []
        self.refuse_render = ""
        self.codes = {"/app/": 200, "/api/campaign-preflight": 404}  # 404 behind the auth gate = authed
        self.need_token = ""  # when set, the auth probe needs exactly this bearer
        self.gets: list[tuple[str, dict]] = []
        self.mine = ["http://127.0.0.1:7870"]
        self.artifact_svc = None

    def http_get(self, url, headers):
        self.gets.append((url, headers))
        path = "/" + url.split("/", 3)[3] if url.count("/") >= 3 else "/"
        if path == "/api/campaign-preflight" and self.need_token:
            if headers.get("Authorization") != f"Bearer {self.need_token}":
                return 401
        if path not in self.codes:
            raise OSError("connection refused")
        return self.codes[path]

    # shoot
    def shoot(self, norm, out_dir):
        self.shots += 1
        out_dir.mkdir(parents=True, exist_ok=True)
        v = out_dir / f"take{self.shots}.mp4"
        v.write_bytes(b"take")
        return {"video": str(v), "marks": {"start": 4.0, "last-slide": 80.0, "end": 89.0}}

    # montage
    def render_montage(self, cid, seq, opts, base):
        if self.refuse_render:
            raise montage.MontageError([self.refuse_render])
        self.renders.append({"seq": seq, "opts": dict(opts)})
        base.mkdir(parents=True, exist_ok=True)
        p = base / f"{opts['name']}.mp4"
        p.write_bytes(b"v" + str(self.shots).encode())
        gh = "limit" in opts
        size = (self.gh_size_at_24 if opts.get("fps") == 24 else self.gh_size) if gh else 9_000_000
        clip = next(s for s in seq if "clip" in s)
        return {
            "path": str(p), "width": 2560, "height": 1440, "duration_s": 96.0, "size_bytes": size,
            "violations": ["over 10 MB"] if gh and size > 10_000_000 else [], "warnings": [], "attempts": [{}],
            "timeline": [
                {"n": 1, "kind": "clip", "asset_id": 0, "title": "intro bumper", "start": 0.0, "length": 2.2},
                {"n": 1, "kind": "card", "title": "t", "start": 1.6, "length": 3.0,
                 "transition": {"style": "fade", "duration": 0.6}},
                {"n": 2, "kind": "clip", "asset_id": clip["clip"], "title": "take", "start": 4.0, "length": 85.6,
                 "transition": {"style": "fade", "duration": 0.6}},
                {"n": 3, "kind": "card", "title": "Star", "start": 89.0, "length": 4.5},
            ],
            "expected_duration_s": 96.0, "caption_visible_s": 4.6, "intro_s": 4.6,
        }  # fmt: skip

    # ffmpeg
    def ff(self, cmd):
        vf = cmd[cmd.index("-vf") + 1] if "-vf" in cmd else ""
        out, err = b"", ""
        if "showinfo" in vf:
            t0 = float(cmd[cmd.index("-ss") + 1])
            if vf.startswith("crop="):  # the caption band: in at ~78.9/82.4, fade-out at 87.5
                rel = [x - t0 for x in (78.9, 79.2, 82.4, 82.8, 87.47, 87.7)]
            else:  # the opening: motion until start + 0.4 s, then still
                rel = [0.1, 0.2, 0.3, 0.4]
            err = "".join(f"[Parsed_showinfo_1] n:{i} pts_time:{t:.3f}\n" for i, t in enumerate(rel) if t >= 0)
        elif "rgb24" in cmd:
            out = bytes.fromhex("0a0a0c")
        elif "64:36" in vf:
            n = int((float(cmd[cmd.index("-to") + 1]) - float(cmd[cmd.index("-ss") + 1])))
            busy = bytes((x * 37) % 256 for x in range(64 * 36))
            frames = [busy] * n
            if self.shots in self.blank_attempts:
                frames[n // 2] = bytes(64 * 36)  # one near-black frame mid-walkthrough
            out = b"".join(frames)
        elif "format=gray" in vf:  # the text-crop search (half resolution): thin strokes in one patch
            w, h = (int(v) for v in vf.split("scale=")[1].split(",")[0].split(":"))
            out = bytes(
                (200 if i % 3 == 0 and 300 <= i % w < 500 and 200 <= i // w < 300 else 30) for i in range(w * h)
            )
        else:
            Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(cmd[-1]).write_bytes(b"png")
        return subprocess.CompletedProcess(cmd, 0, out, err)

    def page(self, html, w, h, dst):
        Path(dst).write_bytes(html.encode())
        return str(dst)

    def save_media(self, path, mime, meta):
        self.media.append((path, mime))
        return {"url": f"/media/{Path(path).name}?sig=x"}

    def hooks(self):
        return produce.Hooks(
            shoot=self.shoot, render_montage=self.render_montage, ff=self.ff, page_renderer=self.page,
            save_media=self.save_media, notify=lambda s, j, t: self.notified.append((s, j, t)),
            http_get=self.http_get, console_urls=lambda: self.mine,
            save_artifact=lambda: self.artifact_svc,
        )  # fmt: skip


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path)
    monkeypatch.setattr(produce.render, "probe", lambda p, *a: {"width": 2560, "height": 1440, "duration_s": 89.6,
                                                                "size_bytes": 10})  # fmt: skip
    return w


def _start(world, **kw):
    args = dict(title="protoAgent in 85 seconds", subtitle="How it fits together", session_id="sess-1",
                hooks=world.hooks(), background=False)  # fmt: skip
    args.update(kw)
    return produce.start(ATLAS_REPLY, **args)


# ── the script ────────────────────────────────────────────────────────────────
def test_prepare_takes_the_whole_atlas_reply_and_upgrades_it_to_2k():
    data, norm, notes = produce.prepare(ATLAS_REPLY, "2k")
    assert norm["device_scale_factor"] == 2 and data["capture"] == "device"
    assert produce.predicted_s(ATLAS_REPLY) == 85
    marks = [s.get("mark") for s in data["steps"] if "mark" in s]
    assert marks == ["start", "last-slide", "end"], "the final slide's arrival is marked for the caption measure"
    assert data["steps"][3]["wait_for"]["selector"] == '[data-atlas-slide="8"]'
    assert produce.caption_region(norm) == {"x": 140, "y": 1140, "w": 2260, "h": 140}
    with pytest.raises(produce.ProduceError, match="quality must be one of"):
        produce.prepare(ATLAS_REPLY, "8k")


def test_settle_point_and_final_caption_are_measured_from_scene_changes():
    assert produce.settle_point([4.1, 4.2, 4.3, 4.4], 4.0) == pytest.approx(4.433, abs=1e-3)
    assert produce.settle_point([], 4.0) == 4.0, "a still opening keeps the mark"
    at, until = produce.final_caption([78.9, 79.2, 82.4, 82.8, 87.47, 87.7], 89.0)
    assert at == pytest.approx(82.833, abs=1e-3) and until == 87.47


def test_blank_frames_flags_uniform_and_near_black_samples():
    busy = bytes((x * 37) % 256 for x in range(64 * 36))
    dark_ui = bytes(12 if x % 9 else 200 for x in range(64 * 36))  # a dark theme with content is NOT blank
    assert produce.blank_frames(busy + bytes(64 * 36) + dark_ui + bytes([128]) * (64 * 36)) == [
        (1, 0.0, 0), (3, 128.0, 0)]  # fmt: skip


# ── the job ───────────────────────────────────────────────────────────────────
def test_one_request_runs_to_a_delivered_self_reviewed_video(world, tmp_path):
    job = _start(world, deliver_dir=str(tmp_path / "out"))
    assert job["status"] == "done", job["reasons"]
    gates = {g["id"]: g for g in job["gates"]}
    assert set(gates) == {"resolution", "final_caption", "intro", "no_blank_frames", "size_budget", "review_assets"}
    assert all(g["ok"] for g in gates.values())
    seq = world.renders[0]["seq"]
    assert seq[0] == {"intro": {"card": {"title": "protoAgent in 85 seconds", "subtitle": "How it fits together",
                                         "eyebrow": "Architecture walkthrough"}}}  # fmt: skip
    clip = seq[1]
    assert clip["in"] == pytest.approx(4.433, abs=1e-3), "cut in on the first SETTLED frame, not at `start`"
    assert clip["out"] == 89.0 and clip["caption"]["until"] == 87.47 and clip["caption"]["color"] == "#0a0a0c"
    assert seq[2] == {"outro": True}
    assert [r["opts"].get("limit") for r in world.renders] == [None, "github_attachment_video_free"]
    assert world.renders[0]["opts"]["size"] == "2560x1440"
    d = job["delivery"]
    assert set(d["assets"]) == {"final", "gh", "sheet", "text_crop"}
    assert store.get_asset(d["assets"]["final"])["status"] == "ready_for_review"
    assert d["links"]["final"].startswith("/media/") and ("video/mp4" in {m for _p, m in world.media})
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "vfr-protoagent-in-85-seconds-gh.mp4", "vfr-protoagent-in-85-seconds-sheet.png",
        "vfr-protoagent-in-85-seconds-text-crop.png", "vfr-protoagent-in-85-seconds.mp4"]  # fmt: skip
    sheet = Path(d["files"]["sheet"]).read_text()
    for label in ("intro bumper", "crossfade in", "first settled frame", "final caption / beat"):
        assert label in sheet, f"the contact sheet has a {label!r} tile"
    (sess, jid, text) = world.notified[0]
    assert sess == "sess-1" and jid == job["id"] and "save_file_artifact" in text
    assert "[PASS]" in produce.summary(job) and "link: /media/" in produce.summary(job)


def test_same_request_is_the_same_job_and_force_re_records(world):
    first = _start(world)
    again = _start(world)
    assert again["id"] == first["id"] and again["reused"] and world.shots == 1
    _start(world, force=True)
    assert world.shots == 2


def test_a_failed_job_resumes_reusing_its_good_take(world):
    world.refuse_render = "boom"
    job = _start(world)
    assert job["status"] == "needs_review" and world.shots == 1
    job["status"] = "failed"
    produce.save(job)
    world.refuse_render = ""
    assert _start(world)["status"] == "done" and world.shots == 1, "the good take is reused"


def test_a_blank_frame_gets_one_re_record_then_passes(world):
    world.blank_attempts = {1}
    job = _start(world)
    assert world.shots == 2 and job["attempt"] == 2 and job["status"] == "done"


def test_a_blank_frame_twice_ends_needs_review_never_done(world):
    world.blank_attempts = {1, 2}
    job = _start(world)
    assert world.shots == 2 and job["status"] == "needs_review"
    assert any("blank" in r for r in job["reasons"])
    assert store.get_asset(job["delivery"]["assets"]["final"])["status"] == "rendered", "not offered for review"
    assert "NEEDS_REVIEW" in world.notified[0][2] and "do not present it as finished" in world.notified[0][2]


def test_an_oversize_github_variant_is_re_rendered_once_at_24_fps(world):
    world.gh_size = 12_000_000
    job = _start(world)
    assert world.shots == 1 and world.renders[-1]["opts"]["fps"] == 24 and job["status"] == "done"
    world.gh_size_at_24 = 11_000_000
    world2_job = _start(world, force=True)
    assert world2_job["status"] == "needs_review" and any("GitHub variant" in r for r in world2_job["reasons"])


def test_a_refused_cut_ends_needs_review_with_the_reason(world):
    world.refuse_render = "the final caption would be on screen 2.10s — under the outro's min_caption_s 4s"
    job = _start(world)
    assert job["status"] == "needs_review" and "min_caption_s 4s" in job["reasons"][0]
    assert job["attempt"] == 1, "a re-render can't fix a script/preset problem — no retry"


def test_no_intro_no_outro_and_bad_requests(world):
    job = _start(world, intro="none", outro="none")
    assert [list(s) for s in world.renders[0]["seq"]] == [["clip", "in", "out", "caption"]]
    assert "final_caption" not in {g["id"] for g in job["gates"]} and "intro" not in {g["id"] for g in job["gates"]}
    with pytest.raises(produce.ProduceError, match="title"):
        produce.start(ATLAS_REPLY, title=" ")
    with pytest.raises(produce.ProduceError, match="intro must be 'brand'"):
        produce.start(ATLAS_REPLY, title="x", intro="sparkly")


def test_densest_text_finds_the_smallest_text_not_the_big_heading():
    w, h = 96, 54
    raw = bytearray([30] * (w * h))
    for y in range(4, 16):  # a big heading: 6 px wide strokes (no thin-stroke extrema inside)
        for x in range(8, 40):
            raw[y * w + x] = 220 if (x // 6) % 2 else 30
    for y in range(30, 40):  # small text: 1 px strokes
        for x in range(60, 84, 2):
            raw[y * w + x] = 200
    x, y, score = produce.densest_text(bytes(raw), w, h, (24, 12))
    assert 56 <= x <= 64 and 28 <= y <= 32 and score > 0
    assert produce.densest_text(bytes(raw), w, h, (24, 12), margin=0.5)[2] <= score


# ── the tools + route ─────────────────────────────────────────────────────────
@pytest.fixture
def client(registry):
    import campaign
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    campaign.register(registry)
    app = FastAPI()
    for prefix, router in registry.routers:
        app.include_router(router, prefix=prefix)
    return TestClient(app)


def test_status_tool_and_route_report_the_job(world, registry, client):
    job = _start(world)
    out = registry.tool("campaign_video_status").invoke({"job_id": job["id"]})
    assert "status: done" in out and "[PASS] rendered ≥ 2560 px wide" in out
    assert "No video job" in registry.tool("campaign_video_status").invoke({"job_id": "vid-000000000000"})
    assert job["id"] in registry.tool("campaign_video_status").invoke({})
    r = client.get(f"/api/plugins/campaign/videos/{job['id']}").json()
    assert r["status"] == "done" and r["downloads"]["final"].startswith("/api/plugins/campaign/file/")
    assert client.get("/api/plugins/campaign/videos/vid-nope").status_code == 404


def test_videos_listing_maps_jobs_and_skips_malformed_records(world, client):
    job = _start(world)
    # A record with no id (hand-edited / truncated) must not 500 the whole listing.
    (produce._jobs_dir() / "vid-broken0000.json").write_text('{"status": "done"}')
    r = client.get("/api/plugins/campaign/videos")
    assert r.status_code == 200
    jobs = r.json()["jobs"]
    assert [j["id"] for j in jobs] == [job["id"]]
    assert jobs[0]["status"] == "done" and jobs[0]["downloads"]["final"].startswith("/api/plugins/campaign/file/")


def test_list_jobs_tolerates_a_file_deleted_mid_listing(world, monkeypatch):
    job = _start(world)
    ghost = produce._jobs_dir() / "vid-ghost0000.json"
    ghost.write_text("{}")
    real_stat = type(ghost).stat

    def stat(self, *a, **k):
        if self.name == ghost.name:
            raise FileNotFoundError(self)
        return real_stat(self, *a, **k)

    monkeypatch.setattr(type(ghost), "stat", stat)
    assert job["id"] in [j.get("id") for j in produce.list_jobs()]


# ── preflight (#22): never start a browser against an unverified console ─────
def test_same_console_treats_loopback_spellings_alike():
    assert produce.same_console("http://localhost:7870", "http://127.0.0.1:7870/")
    assert not produce.same_console("http://localhost:7870", "http://127.0.0.1:7953")
    assert not produce.same_console("https://a.example", "http://a.example")


def test_another_agents_console_is_refused_before_any_recording(world):
    world.mine = ["http://127.0.0.1:7953"]
    with pytest.raises(
        produce.ProduceError, match=r"records http://localhost:7870, but this agent's console is http://127.0.0.1:7953"
    ):
        _start(world)
    assert world.shots == 0 and world.gets == [] and produce.list_jobs() == [], "nothing queued, nothing fetched"
    assert _start(world, allow_other_console=True)["status"] == "done", "an explicit opt-in records it"


def test_an_unknown_own_console_needs_the_opt_in(world):
    world.mine = []
    with pytest.raises(produce.ProduceError, match="can't tell which console"):
        _start(world)
    assert world.shots == 0


def test_an_unreachable_or_broken_console_fails_fast(world):
    world.codes.pop("/app/")
    with pytest.raises(produce.ProduceError, match="unreachable .* Nothing was recorded"):
        _start(world)
    world.codes["/app/"] = 502
    with pytest.raises(produce.ProduceError, match="answered HTTP 502"):
        _start(world)
    assert world.shots == 0


def test_a_token_protected_console_needs_a_working_token(world, tmp_path, monkeypatch):
    world.need_token = "tok-1"
    with pytest.raises(produce.ProduceError, match="needs a token and the script's has none: set a CAMPAIGN_"):
        _start(world)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"origins": [{"origin": "http://localhost:7870",
                                              "localStorage": [{"name": "protoagent.authToken", "value": "tok-1"}]}]}))  # fmt: skip
    reply = ATLAS_REPLY.replace("steps:\n", f"auth: {{storage_state: {state}}}\nsteps:\n", 1)
    job = produce.start(reply, title="t", hooks=world.hooks(), background=False)
    assert job["status"] == "done" and world.gets[-1][1] == {"Authorization": "Bearer tok-1"}
    monkeypatch.setenv("CAMPAIGN_CONSOLE_TOKEN", "wrong")
    bad = ATLAS_REPLY.replace("steps:\n", "auth: {bearer_env: CAMPAIGN_CONSOLE_TOKEN}\nsteps:\n", 1)
    with pytest.raises(produce.ProduceError, match="token was refused"):
        produce.start(bad, title="t2", hooks=world.hooks(), background=False)


# ── delivery through core seams (#4254 artifact.save_file, #4255 attach_file) ─
def test_artifact_save_file_service_puts_the_files_in_the_panel(world):
    calls = []

    def svc(**kw):
        calls.append(kw)
        return {"ok": True, "id": f"art-{len(calls)}", "version": 1, "message": "saved", "ref": ""}

    world.artifact_svc = svc
    job = _start(world)
    assert [Path(c["path"]).name for c in calls] == ["protoagent-in-85-seconds.mp4", "contact-sheet.png"]
    assert calls[0]["session_id"] == "sess-1" and calls[0]["roots"]
    assert job["delivery"]["artifacts_saved"] is True
    text = world.notified[0][2]
    assert "already in this chat's Artifact panel" in text and "save_file_artifact" not in text


def test_without_the_service_or_on_refusal_the_follow_up_turn_saves_them(world):
    world.artifact_svc = lambda **kw: {"ok": False, "message": "File too large — raise max_blob_kb"}
    job = _start(world)
    assert job["delivery"]["artifacts_saved"] is False
    assert "save_file_artifact(path=" in world.notified[0][2]
    assert any("max_blob_kb" in line for line in job["log"])


def test_status_attaches_the_files_and_speaks_to_remote_callers(world, registry, monkeypatch):
    import sys
    import types

    import campaign

    attached = []
    fake = types.ModuleType("graph.sdk")
    fake.attach_file = lambda ref, name="": attached.append((ref, name)) or True
    fake.console_url = lambda: "https://agent.example"
    pkg = types.ModuleType("graph")
    pkg.sdk = fake
    monkeypatch.setitem(sys.modules, "graph", pkg)
    monkeypatch.setitem(sys.modules, "graph.sdk", fake)
    job = _start(world, allow_other_console=True)
    campaign.register(registry)
    out = registry.tool("campaign_video_status").invoke({"job_id": job["id"]})
    assert [n for _u, n in attached] == ["protoagent-in-85-seconds.mp4", "protoagent-in-85-seconds-gh.mp4",
                                         "protoagent-in-85-seconds-sheet.png"]  # fmt: skip
    assert "3 file(s) attached" in out and "link: https://agent.example/media/" in out
    assert "no Artifact panel or Download button on their side" in out
    again = registry.tool("campaign_produce_video")
    assert again  # the tool exists; reuse wording is covered below


def test_a_repeat_request_says_it_reused_the_job(world):
    first = _start(world)
    again = _start(world)
    assert again["reused"] and "REUSED" in produce.summary(again)
    assert "reused this job" in produce.load(first["id"])["log"][-1]
    assert "reused" not in produce.load(first["id"]), "the flag is per reply, never persisted"


def test_the_preflight_probe_runs_in_the_worker_fenced_and_bearer_scoped():
    import contextlib

    from campaign import shoot
    from campaign.worker import pw_worker

    seen = []

    class Resp:
        def __init__(self, status):
            self.status = status

    class Ctx:
        def get(self, url, headers=None, **kw):
            seen.append((url, headers))
            if "down" in url:
                raise RuntimeError("connect ECONNREFUSED")
            return Resp(200)

        def dispose(self):
            pass

    class PW:
        class request:  # noqa: N801 — mirrors playwright's attribute
            @staticmethod
            def new_context():
                return Ctx()

    job = {"v": pw_worker.JOB_VERSION, "kind": "probe", "base_url": "http://127.0.0.1:7953", "bearer": "sekret",
           "fence": shoot.fence(), "urls": ["http://127.0.0.1:7953/app/", "http://127.0.0.1:7953/api/plugins/campaign/x",
                                             "https://cdn.example/x", "http://down.example/"]}  # fmt: skip
    rep = pw_worker.run_job(job, lambda: contextlib.nullcontext(PW()))
    r = rep["result"]["responses"]
    assert r[0] == {"url": "http://127.0.0.1:7953/app/", "status": 200}
    assert r[1]["error"] == "blocked by the fence", "never this plugin's own data API"
    assert seen[0][1] == {"Authorization": "Bearer sekret"} and seen[1][1] == {}, "the bearer goes home only"
    assert "ECONNREFUSED" in r[3]["error"] and "sekret" not in json.dumps(rep)


def test_the_intro_gate_reads_the_presets_max_s(world, tmp_path):
    from campaign import intro

    splash = tmp_path / "s.mp4"
    splash.write_bytes(b"x")
    assert intro.configure({"bumper": {"path": str(splash)}, "card": {"title": "t"}, "max_s": 4.0}) == []
    job = _start(world)  # the fake render reports intro_s 4.6
    gate = next(g for g in job["gates"] if g["id"] == "intro")
    assert not gate["ok"] and gate["label"] == "intro ≤ 4 s, crossfaded in" and job["status"] == "needs_review"
    assert intro.configure({"bumper": {"path": str(splash)}, "card": {"title": "t"}, "max_s": 8.0}) == []
    job = _start(world, force=True)
    gate = next(g for g in job["gates"] if g["id"] == "intro")
    assert gate["ok"] and gate["label"] == "intro ≤ 8 s, crossfaded in", "a valid intro under a custom max_s passes"


def test_mask_css_refuses_an_unknown_mode_clearly():
    from campaign.worker import pw_worker

    with pytest.raises(ValueError, match="mask mode 'smudge' is not one of blur, hide, remove"):
        pw_worker.mask_css({"mode": "smudge", "selectors": [".x"]})
    assert "blur(7px)" in pw_worker.mask_css({"selectors": [".x"]})


@pytest.mark.parametrize("report", ['{"ok": tr', "[1, 2]"])
def test_run_worker_names_a_corrupt_or_partial_report(monkeypatch, report):
    from campaign import deps, interpreter, shoot

    monkeypatch.setattr(deps, "resolve", lambda: interpreter.Resolution(python="/py", source="test"))

    def fake_run(python, args, *, stdin=None, timeout=None, **kw):
        Path(json.loads(stdin)["report_path"]).write_text(report)
        return interpreter.Completed(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(interpreter, "run_python", fake_run)
    with pytest.raises(shoot.WorkerError, match="corrupt or partial report|isn't a JSON object"):
        shoot.run_worker({"v": 1, "kind": "probe", "fence": shoot.fence()}, 5)
