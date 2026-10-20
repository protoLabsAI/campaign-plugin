"""produce — one request → a finished, self-reviewed video, as a background job.

``campaign_produce_video`` hands a shot script (Atlas's ``atlas_record_script`` output, or any
Campaign script) to :func:`start`, which returns a job id at once and runs the pipeline on a
worker thread:

    record   the take (``capture: device`` at the requested quality — 2k = 2560 px wide)
    measure  the first SETTLED frame after ``start`` (the cut's in-point — never mid-motion)
             and, for Atlas footage, when the final caption is fully up / starts fading
    render   [intro] → the walkthrough (in → ``end``) → [outro] as a review mp4 (crf 16) and a
             GitHub-sized variant (``github_attachment_video_free``, ≤ 10 MB)
    review   automated gates (:func:`review`) + review assets: a labelled contact sheet and a
             100 % crop of the densest small text
    deliver  register the files as campaign assets, publish them through the host's media
             store (signed links), and nudge the requesting chat to hand them over

A failed gate gets ONE automatic retry where a retry can help (a blank frame or a short
resolution → re-record; a size overrun → re-render the small variant at a lower fps); anything
else, or a second failure, ends the job ``needs_review`` with the reasons. A failing video is
never reported ``done``.

Jobs are JSON files under ``<data dir>/videos/`` keyed by a hash of their inputs, so asking
again with the same inputs returns the same job (``force`` re-runs it; a failed or orphaned
job resumes, reusing a good take). Every external effect — the browser, ffmpeg, the card
renderer, the host's media store, the chat nudge — is an injectable seam (:class:`Hooks`), so
the suite drives the whole pipeline host-free.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from . import deps, limits, montage, paths, render, shoot, shotscript, store

log = logging.getLogger("protoagent.plugins.campaign")

QUALITIES = {"2k": 2560, "1080p": 1920}
GH_LIMIT = "github_attachment_video_free"
TERMINAL = ("done", "needs_review", "failed")
# Stage → (progress at its start, at its end). Recording dominates the wall clock.
_SPAN = {
    "queued": (0.0, 0.02),
    "recording": (0.02, 0.6),
    "measuring": (0.6, 0.63),
    "rendering": (0.63, 0.88),
    "reviewing": (0.88, 0.96),
    "delivering": (0.96, 1.0),
}
BLANK_SPREAD = 10  # luma p95−p5 below this on a 64×36 sample = a blank (uniform) frame
DARK_MEAN = 10  # …and a mean under this makes it "near-black"
SAMPLE_EVERY_S = 1.0
MAX_SAMPLES = 180
CROP = (640, 360)  # the 100 % text crop, device px
DEFAULT_CAPTION_S = 4.0

_LOCK = threading.Lock()  # one job at a time records + renders (CPU and the screen budget)
_THREADS: dict[str, threading.Thread] = {}


class ProduceError(ValueError):
    pass


# ── seams ────────────────────────────────────────────────────────────────────
def _ff_bytes(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=render.FFMPEG_TIMEOUT_S)


@dataclass
class Hooks:
    """Everything the pipeline does to the outside world. Defaults are the real ones."""

    shoot: Callable[[dict[str, Any], Path], dict[str, Any]] | None = None
    render_montage: Callable[..., dict[str, Any]] | None = None
    ff: Callable[[list[str]], subprocess.CompletedProcess] = _ff_bytes  # raw bytes out
    page_renderer: Callable[[str, int, int, Path], str] | None = None
    save_media: Callable[[str, str, dict], Any] | None = None
    notify: Callable[[str, str, str], Any] | None = None  # (session_id, job_id, text)
    emit: Callable[[str, dict], Any] | None = None
    playwright_factory: Callable | None = None
    # core's `artifact.save_file` service (#4254), resolved at call time; None → follow-up turn
    save_artifact: Callable[[], Callable[..., dict] | None] | None = None
    # the preflight's network + "which console am I" seams
    http_get: Callable[[str, dict[str, str]], int] | None = None
    console_urls: Callable[[], list[str]] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ── the script ───────────────────────────────────────────────────────────────
_FENCE = re.compile(r"```(?:ya?ml)?\s*\n(.*?)```", re.S)
_PREDICT = re.compile(r"about\s+(\d+(?:\.\d+)?)\s*s\b")
_SLIDES = re.compile(r"\((\d+)\s+slides")
TOUR_DONE = "[data-atlas-state=tour-done]"


def script_text(raw: str) -> str:
    """The YAML inside ``raw`` — a whole ``atlas_record_script`` reply (prose + a fenced block)
    is accepted as-is, so the agent can hand it over unchanged."""
    m = _FENCE.search(raw or "")
    return m.group(1) if m else (raw or "")


def predicted_s(raw: str) -> float | None:
    m = _PREDICT.search(raw or "")
    return float(m.group(1)) if m else None


def _wait_selector(step: dict[str, Any]) -> str:
    w = step.get("wait_for")
    return str(w.get("selector") or "") if isinstance(w, dict) else ""


def is_atlas(data: dict[str, Any]) -> bool:
    return any(_wait_selector(s) == TOUR_DONE for s in data.get("steps") or [] if isinstance(s, dict))


def prepare(raw: str, quality: str = "2k") -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Parse + upgrade a shot script for production. Returns (data, validated, notes).

    ``quality`` 2k/1080p sets ``device_scale_factor`` = target width ÷ viewport width and
    ``capture: device`` (device-pixel frames: crisp small text). An Atlas record script also
    gets a ``last-slide`` mark (when the final slide arrives) so the final caption can be
    measured from the footage."""
    q = (quality or "2k").lower()
    if q not in QUALITIES:
        raise ProduceError(f"quality must be one of {', '.join(QUALITIES)}, got {quality!r}")
    try:
        data = shotscript.parse(script_text(raw))
    except shotscript.ScriptError as e:
        raise ProduceError("the shot script doesn't parse:\n" + "\n".join(f"- {p}" for p in e.problems)) from None
    notes: list[str] = []
    vp = data.get("viewport") or shotscript.DEFAULTS.get("viewport") or {"width": 1280, "height": 720}
    vw = int(vp.get("width") or 1280)
    dsf = round(QUALITIES[q] / vw, 4)
    if not 1 <= dsf <= 3:
        raise ProduceError(
            f"quality {q} needs {QUALITIES[q]} px wide from a {vw} px viewport — a ×{dsf:g} scale is "
            "outside 1..3; use a 1280-wide viewport"
        )
    if dsf > 1:
        data["device_scale_factor"] = dsf
        data["capture"] = "device"
        notes.append(f"capture: device ×{dsf:g} → {QUALITIES[q]}×{round(int(vp.get('height') or 720) * dsf)}")
    steps = list(data.get("steps") or [])
    if is_atlas(data) and not any(isinstance(s, dict) and s.get("mark") == "last-slide" for s in steps):
        m = _SLIDES.search(str(data.get("description") or ""))
        k = next(i for i, s in enumerate(steps) if isinstance(s, dict) and _wait_selector(s) == TOUR_DONE)
        frame = (steps[k]["wait_for"] or {}).get("frame")
        if m:
            wait = {"selector": f'[data-atlas-slide="{int(m.group(1))}"]', "timeout_ms": 170000}
            if frame:
                wait["frame"] = frame
            steps[k:k] = [{"wait_for": wait}, {"mark": "last-slide"}]
            data["steps"] = steps
            notes.append("added a `last-slide` mark (the final caption is measured after it)")
    try:
        norm = shotscript.validate(data)
    except shotscript.ScriptError as e:
        raise ProduceError("the shot script is invalid:\n" + "\n".join(f"- {p}" for p in e.problems)) from None
    return data, norm, notes


def caption_region(norm: dict[str, Any]) -> dict[str, int] | None:
    """Where Atlas's record-mode caption band sits, in DEVICE px: 70 css px in from the left
    and right edges' 80, 70 tall, its bottom 80 css px above the frame's (measured at 1280×720)."""
    vp = norm.get("viewport") or {}
    w, h = int(vp.get("width") or 0), int(vp.get("height") or 0)
    s = float(norm.get("device_scale_factor") or 1)
    if not (w and h):
        return None
    return {"x": int(70 * s), "y": int((h - 150) * s), "w": int((w - 150) * s), "h": int(70 * s)}


# ── preflight: never start a browser against an unverified console ───────────
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"}
# Any path behind the host's auth gate: 401 without a valid bearer, 404 past it (= authed). Not
# this plugin's own API — the worker's fence keeps every request off that.
AUTH_PROBE = "/api/campaign-preflight"


def own_console_urls() -> list[str]:
    """The URL(s) THIS agent's console serves on, from the host. ``graph.sdk.console_url()``
    when the core has it (protoAgent#4256) is authoritative — ``A2A_PUBLIC_URL``, else the
    running server's bound host/port, and on a hub-spawned fleet member the member's OWN
    console (not the hub's ``/agents/<id>`` tenant path, whose origin is the hub's own agent);
    its ``None`` means unknown, ``[]``. An older core without the seam: what core advertises
    in its A2A card — ``A2A_PUBLIC_URL`` and the actually-bound loopback port. ``[]`` with no
    host."""
    import os

    try:
        from graph import sdk  # host import — lazy

        fn = getattr(sdk, "console_url", None)
    except Exception:  # noqa: BLE001
        fn = None
    if callable(fn):
        try:
            url = str(fn() or "").strip().rstrip("/")
            return [url] if url else []
        except Exception:  # noqa: BLE001 — a broken seam: the card's rule below
            pass
    out: list[str] = []
    pub = (os.environ.get("A2A_PUBLIC_URL") or "").strip().rstrip("/")
    if pub:
        out.append(pub)
    try:
        from runtime.state import STATE  # host import — the same port core's card advertises

        port = int(getattr(STATE, "active_port", 0) or 0)
        if port:
            out.append(f"http://127.0.0.1:{port}")
    except Exception:  # noqa: BLE001
        pass
    return out


def link_bases() -> list[str]:
    """Bases to make a ``/media`` link absolute for a REMOTE caller: the public URL first
    (``A2A_PUBLIC_URL`` — on a fleet member the hub path a remote can reach), then this
    agent's own console URL(s)."""
    import os

    pub = (os.environ.get("A2A_PUBLIC_URL") or "").strip().rstrip("/")
    return ([pub] if pub else []) + [u for u in own_console_urls() if u != pub]


def _origin_key(url: str) -> tuple[str, str, int] | None:
    from urllib.parse import urlsplit

    try:
        u = urlsplit((url or "").strip())
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    host = u.hostname.lower()
    return (u.scheme, "loopback" if host in LOOPBACK else host, port)


def same_console(a: str, b: str) -> bool:
    """Same scheme + host + port, every loopback spelling equal (127.0.0.1 ≡ localhost)."""
    ka, kb = _origin_key(a), _origin_key(b)
    return ka is not None and ka == kb


def _worker_get(norm: dict[str, Any], hooks: "Hooks") -> Callable[[str, dict[str, str]], int]:
    """The real preflight GET: through the browser worker's request context (no browser), so
    the plugin's own Python never opens a connection. An unreachable URL raises OSError."""

    def get(url: str, headers: dict[str, str]) -> int:
        bearer = headers.get("Authorization", "").removeprefix("Bearer ").strip()
        (r,) = shoot.probe(str(norm.get("base_url") or ""), [url], bearer, hooks.playwright_factory)
        if "status" not in r:
            raise OSError(r.get("error") or "no response")
        return int(r["status"])

    return get


def _stored_token(norm: dict[str, Any], origin: str) -> str:
    """A storage_state file's ``protoagent.authToken`` for ``origin`` (the console's own token)."""
    sp = (norm.get("auth") or {}).get("storage_state")
    if not sp:
        return ""
    try:
        data = json.loads(Path(str(sp)).expanduser().read_text())
    except (OSError, ValueError):
        return ""
    for o in data.get("origins") or []:
        if same_console(str(o.get("origin") or ""), origin):
            for kv in o.get("localStorage") or []:
                if kv.get("name") == "protoagent.authToken":
                    return str(kv.get("value") or "")
    return ""


def preflight(norm: dict[str, Any], hooks: Hooks, *, allow_other_console: bool = False) -> list[str]:
    """Check the recording target BEFORE any browser starts. Raises ProduceError with an
    actionable message; returns notes on success.

    1. the script's ``base_url`` is THIS agent's console (unless ``allow_other_console``);
    2. its first page answers 200;
    3. the script's credentials (``auth.bearer_env`` / a ``storage_state`` token) get past the
       console's auth gate — one cheap authenticated GET."""
    base = str(norm.get("base_url") or "").rstrip("/")
    if not base:
        raise ProduceError("the shot script has no base_url — set it to THIS console's URL")
    origin = shotscript.browser_origin(base)
    notes: list[str] = []
    mine = (hooks.console_urls or own_console_urls)()
    if not allow_other_console:
        if not mine:
            raise ProduceError(
                f"can't tell which console this agent serves on, so {origin} can't be verified as ours — pass "
                "allow_other_console=true only if recording that console is intended"
            )
        if not any(same_console(origin, m) for m in mine):
            raise ProduceError(
                f"the script records {origin}, but this agent's console is {mine[0]} — re-run "
                f"atlas_record_script with base_url={mine[0]!r} (a blank base_url falls back to another "
                "agent's :7870). Recording someone else's console needs allow_other_console=true"
            )
        notes.append(f"base_url {origin} is this agent's console")
    else:
        notes.append(f"recording {origin} (allow_other_console)")
    get = hooks.http_get or _worker_get(norm, hooks)
    first = next((st.get("url") for st in norm.get("steps") or [] if st.get("op") == "goto"), "/")
    page = shotscript.resolve_url(norm, str(first))
    try:
        code = get(page, {})
    except Exception as e:  # noqa: BLE001 — refused / DNS / timeout
        raise ProduceError(f"{page} is unreachable ({e}) — is that console running? Nothing was recorded") from None
    if code != 200:
        raise ProduceError(
            f"{page} answered HTTP {code}, not 200 — fix base_url / the first goto. Nothing was recorded"
        )
    token = ""
    if (norm.get("auth") or {}).get("bearer_env"):
        try:
            token = shoot.read_bearer(norm)
        except shoot.ShootError as e:
            raise ProduceError(f"the script's bearer: {e}") from None
    token = token or _stored_token(norm, origin)
    try:
        code = get(origin + AUTH_PROBE, {"Authorization": f"Bearer {token}"} if token else {})
    except Exception as e:  # noqa: BLE001
        raise ProduceError(f"{origin} is unreachable ({e})") from None
    if code in (401, 403):
        raise ProduceError(
            f"{origin} needs a token and the script's "
            + ("token was refused" if token else "has none")
            + ": set a CAMPAIGN_* env var to this console's token and pass bearer_env=<its name> to "
            "atlas_record_script (never the token itself). Nothing was recorded"
        )
    notes.append(f"{page} → 200, auth ok" if token else f"{page} → 200 (no auth needed)")
    return notes


# ── jobs ─────────────────────────────────────────────────────────────────────
def _jobs_dir() -> Path:
    d = paths.data_dir() / "videos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def job_key(inputs: dict[str, Any], script: dict[str, Any]) -> str:
    blob = json.dumps({"inputs": inputs, "script": script}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def load(job_id: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"vid-[0-9a-f]{12}", job_id or ""):
        return None
    p = _jobs_dir() / f"{job_id}.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def save(job: dict[str, Any]) -> dict[str, Any]:
    job["updated"] = _now()
    p = _jobs_dir() / f"{job['id']}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=1, default=str))
    tmp.replace(p)
    return job


def _mtime(p: Path) -> float:
    """A job file's mtime, or 0 if it vanished between glob() and stat() (a concurrent delete)."""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def list_jobs(limit: int = 20) -> list[dict[str, Any]]:
    rows = []
    for p in sorted(_jobs_dir().glob("vid-*.json"), key=_mtime, reverse=True)[:limit]:
        try:
            rows.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    return rows


def _alive(job_id: str) -> bool:
    t = _THREADS.get(job_id)
    return bool(t and t.is_alive())


def _stage(job: dict[str, Any], stage: str, msg: str = "") -> None:
    job["stage"] = stage
    job["stage_started"] = time.time()
    job["progress"] = _SPAN.get(stage, (job.get("progress", 0), 0))[0]
    if msg:
        _log(job, msg)
    save(job)


def _log(job: dict[str, Any], msg: str) -> None:
    job.setdefault("log", []).append(f"{datetime.now(UTC).strftime('%H:%M:%S')} {msg}")
    job["log"] = job["log"][-60:]
    log.info("[campaign] video %s: %s", job.get("id"), msg)


def progress(job: dict[str, Any]) -> float:
    """0..1 — while recording, interpolated from the predicted footage length."""
    p = float(job.get("progress") or 0)
    if job.get("status") in TERMINAL:
        return 1.0
    if job.get("stage") == "recording" and job.get("predicted_s"):
        lo, hi = _SPAN["recording"]
        frac = min(0.98, (time.time() - float(job.get("stage_started") or time.time())) / (job["predicted_s"] + 25))
        p = max(p, lo + (hi - lo) * frac)
    return round(p, 3)


def start(
    raw_script: str,
    *,
    title: str,
    quality: str = "2k",
    intro: Any = "brand",
    outro: Any = "brand",
    subtitle: str = "",
    eyebrow: str = "Architecture walkthrough",
    campaign_id: int = 0,
    region: dict[str, int] | None = None,
    session_id: str = "",
    deliver_dir: str = "",
    force: bool = False,
    allow_other_console: bool = False,
    hooks: Hooks | None = None,
    background: bool = True,
) -> dict[str, Any]:
    """Validate, create (or find) the job, and run it. Returns the job dict immediately when
    ``background`` (a thread runs it), or after it finishes otherwise. Raises ProduceError for
    a bad request (nothing is started)."""
    title = (title or "").strip()
    if not title:
        raise ProduceError("give the video a title (it goes on the intro card and names the files)")
    data, norm, notes = prepare(raw_script, quality)
    intro_v, outro_v = _preset_arg(intro, "intro"), _preset_arg(outro, "outro")
    inputs = {
        "title": title,
        "quality": quality.lower(),
        "intro": intro_v,
        "outro": outro_v,
        "subtitle": subtitle,
        "eyebrow": eyebrow,
        "campaign_id": int(campaign_id or 0),
        "region": region,
    }
    key = job_key(inputs, data)
    job_id = "vid-" + key[:12]
    job = load(job_id)
    if job and not force:
        if job["status"] in ("done", "needs_review") or (job["status"] == "running" and _alive(job_id)):
            _log(job, "re-requested with the same inputs — reused this job, nothing re-recorded")
            save(job)
            return {**job, "reused": True}
    if job and _alive(job_id):
        raise ProduceError(f"job {job_id} is running — wait for it (campaign_video_status) before forcing a re-run")
    hooks = hooks or Hooks()
    notes = notes + preflight(norm, hooks, allow_other_console=allow_other_console)
    prior_take = (job or {}).get("take") if not force else None
    job = {
        "id": job_id,
        "key": key,
        "status": "running",
        "stage": "queued",
        "progress": 0.0,
        "created": (job or {}).get("created") or _now(),
        "inputs": inputs,
        "script": data,
        "predicted_s": predicted_s(raw_script),
        "notes": notes,
        "session_id": session_id,
        "deliver_dir": deliver_dir,
        "take": prior_take,
        "attempt": 0,
        "gates": [],
        "reasons": [],
        "outputs": {},
        "delivery": {},
        "log": [],
    }
    _log(job, f"queued — {title!r} at {quality}" + (f"; {'; '.join(notes)}" if notes else ""))
    save(job)
    if not background:
        run(job, hooks)
        return load(job_id) or job
    for jid in [k for k, th in _THREADS.items() if not th.is_alive()]:
        _THREADS.pop(jid, None)
    t = threading.Thread(target=run, args=(job, hooks), name=f"campaign-{job_id}", daemon=True)
    _THREADS[job_id] = t
    t.start()
    return job


def _preset_arg(v: Any, what: str) -> str:
    s = str(v if v not in (None, True) else "brand").strip().lower()
    if s in ("brand", "default", "true", "yes"):
        return "brand"
    if s in ("none", "", "false", "no", "off"):
        return "none"
    raise ProduceError(f"{what} must be 'brand' (the operator's {what} preset) or 'none', got {v!r}")


# ── the pipeline ─────────────────────────────────────────────────────────────
def run(job: dict[str, Any], hooks: Hooks) -> None:
    """Run (or resume) one job to a terminal status. Never raises."""
    with _LOCK:
        try:
            _run(job, hooks)
        except Exception as e:  # noqa: BLE001 — a job must always end in a terminal state
            log.exception("[campaign] video %s failed", job.get("id"))
            job["status"] = "failed"
            job["reasons"] = [*job.get("reasons", []), f"{type(e).__name__}: {e}"]
            _log(job, f"FAILED — {e}")
            save(job)
            _finish(job, hooks)


def _campaign(job: dict[str, Any]) -> dict[str, Any]:
    cid = int(job["inputs"].get("campaign_id") or 0)
    if cid:
        return store.require_campaign(cid)
    for c in store.list_campaigns():
        if c["name"] == "Videos":
            return c
    return store.create_campaign("Videos", goal="Finished videos produced from a request (campaign_produce_video).")


def _run(job: dict[str, Any], hooks: Hooks) -> None:
    c = _campaign(job)
    job["campaign_id"] = c["id"]
    norm = shotscript.validate(job["script"])
    retry_record = False
    while True:
        job["attempt"] += 1
        take = job.get("take")
        if retry_record or not (take and Path(take.get("video") or "").is_file()):
            take = _record(job, c, norm, hooks)
        else:
            _log(job, f"reusing the take {take['video']}")
        m = _measure(job, take, norm, hooks)
        try:
            out = _render(job, c, take, m, hooks)
        except montage.MontageError as e:
            # A refused cut (final caption too short, intro too long …): a script/preset fix,
            # not something a re-render can change.
            job["status"] = "needs_review"
            job["reasons"] = [f"the cut was refused: {p}" for p in e.problems]
            _log(job, "needs review — " + "; ".join(e.problems))
            save(job)
            _finish(job, hooks)
            return
        _stage(job, "reviewing", "self-review")
        gates = review(job, out, take, m, hooks)
        job["gates"] = gates
        failed = [g for g in gates if not g["ok"]]
        save(job)
        if failed and job["attempt"] < 2:
            ids = {g["id"] for g in failed}
            if ids & {"no_blank_frames", "resolution"}:
                _log(job, f"gates failed ({', '.join(sorted(ids))}) — re-recording once")
                retry_record = True
                continue
            if ids == {"size_budget"}:
                _log(job, "the GitHub variant is over budget — re-rendering it once at 24 fps")
                job["gh_fps"] = 24
                retry_record = False
                continue
        break
    _deliver(job, c, out, hooks)
    if failed:
        job["status"] = "needs_review"
        job["reasons"] = [f"{g['label']}: {g['detail']}" for g in failed]
        _log(job, "needs review — " + "; ".join(job["reasons"]))
    else:
        job["status"] = "done"
        _log(job, "done — every gate passed")
    job["progress"] = 1.0
    save(job)
    _finish(job, hooks)


def _record(job: dict[str, Any], c: dict[str, Any], norm: dict[str, Any], hooks: Hooks) -> dict[str, Any]:
    _stage(job, "recording", f"recording {norm['name']} (~{job.get('predicted_s') or '?'} s of footage)")
    out_dir = paths.unique_dir(
        paths.campaign_dir(c["id"], c["name"]) / "shots",
        f"{norm['name']}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}",
    )
    if hooks.shoot:
        res = hooks.shoot(norm, out_dir)
    else:
        res = shoot.run(norm, out_dir, playwright_factory=hooks.playwright_factory)
    info = render.probe(res["video"])
    a = store.add_asset(
        c["id"], "clip", f"take: {job['inputs']['title']}", status="captured", path=res["video"],
        size_bytes=info.get("size_bytes"), width=info.get("width"), height=info.get("height"),
        duration_s=info.get("duration_s"), meta={"marks": res.get("marks") or {}, "take_dir": str(out_dir),
                                                  "video_job": job["id"]},
    )  # fmt: skip
    take = {"asset_id": a["id"], "video": res["video"], "marks": res.get("marks") or {}, **info}
    job["take"] = take
    _log(job, f"take → asset #{a['id']} {info.get('width')}×{info.get('height')} {info.get('duration_s', 0):.1f}s")
    save(job)
    return take


def _scene_changes(
    hooks: Hooks, video: str, t0: float, t1: float, region: dict[str, int] | None, thr: float
) -> list[float]:
    ff = deps.ffmpeg() or "ffmpeg"
    crop = f"crop={region['w']}:{region['h']}:{region['x']}:{region['y']}," if region else "scale=640:-2,"
    r = hooks.ff([ff, "-hide_banner", "-ss", f"{t0:.3f}", "-to", f"{t1:.3f}", "-i", video, "-vf",
                  f"{crop}select='gt(scene,{thr})',showinfo", "-f", "null", "-"])  # fmt: skip
    err = r.stderr.decode("utf-8", "replace") if isinstance(r.stderr, bytes) else (r.stderr or "")
    return [t0 + float(t) for t in re.findall(r"pts_time:([0-9.]+)", err)]


def _bursts(times: list[float], gap: float = 0.5) -> list[list[float]]:
    out: list[list[float]] = []
    for t in times:
        if out and t - out[-1][-1] < gap:
            out[-1].append(t)
        else:
            out.append([t])
    return out


def settle_point(changes: list[float], start: float, quiet_s: float = 0.5) -> float:
    """The first moment at/after ``start`` the picture holds still for ``quiet_s``: the
    in-point that never opens on a frame mid-transition."""
    t = start
    for c in sorted(x for x in changes if x >= start):
        if c - t >= quiet_s:
            return round(t, 3)
        t = c + 1 / 30
    return round(t, 3)


def final_caption(changes: list[float], end: float) -> tuple[float | None, float | None]:
    """(fully up, starts fading) for the final caption, from the caption band's change bursts
    after the last slide arrives: the last caption fading IN, then (Atlas's native end) OUT."""
    bursts = _bursts(changes)
    if not bursts:
        return None, None
    fade_out = bursts[-1] if len(bursts) >= 2 and end - bursts[-1][-1] > 1.0 else None
    arrive = bursts[-2] if fade_out else bursts[-1]
    return round(arrive[-1] + 1 / 30, 3), (round(fade_out[0], 3) if fade_out else None)


def _measure(job: dict[str, Any], take: dict[str, Any], norm: dict[str, Any], hooks: Hooks) -> dict[str, Any]:
    _stage(job, "measuring", "measuring the in-point and the final caption")
    marks = take.get("marks") or {}
    dur = float(take.get("duration_s") or 0)
    start = float(marks.get("start", 0.0))
    end = float(marks.get("end", dur))
    m: dict[str, Any] = {"start": start, "end": end, "in": start, "out": round(min(end, dur or end), 3)}
    try:
        ch = _scene_changes(hooks, take["video"], start, min(start + 10, end), None, 0.002)
        m["in"] = settle_point(ch, start)
    except Exception as e:  # noqa: BLE001 — fall back to the mark
        _log(job, f"couldn't measure the settled in-point ({e}); cutting at `start`")
    region = job["inputs"].get("region") or (caption_region(norm) if is_atlas(job["script"]) else None)
    if region and "end" in marks:
        since = max(0.0, float(marks.get("last-slide", end - 20)) - 3)
        try:
            ch = _scene_changes(hooks, take["video"], since, end, region, 0.003)
            at, until = final_caption(ch, end)
            m.update(caption_at=at, caption_until=until, region=region)
            m["band"] = _pixel(hooks, take["video"], end, region)
        except Exception as e:  # noqa: BLE001
            _log(job, f"couldn't measure the final caption ({e})")
    job["measure"] = m
    _log(
        job,
        f"in {m['in']:.2f}s (start {start:.2f}s), out {m['out']:.2f}s"
        + (
            f"; final caption up {m.get('caption_at')}s → fades {m.get('caption_until')}s"
            if m.get("caption_at")
            else ""
        ),
    )
    save(job)
    return m


def _pixel(hooks: Hooks, video: str, t: float, r: dict[str, int]) -> str | None:
    ff = deps.ffmpeg() or "ffmpeg"
    res = hooks.ff([ff, "-v", "error", "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1", "-vf",
                    f"crop=2:2:{r['x'] + 8}:{r['y'] + 8},scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])  # fmt: skip
    px = res.stdout if isinstance(res.stdout, bytes) else b""
    return "#" + px[:3].hex() if len(px) >= 3 else None


def _sequence(job: dict[str, Any], take: dict[str, Any], m: dict[str, Any]) -> list[dict[str, Any]]:
    inp = job["inputs"]
    seq: list[dict[str, Any]] = []
    if inp["intro"] == "brand":
        card = {"title": inp["title"]}
        if inp.get("subtitle"):
            card["subtitle"] = inp["subtitle"]
        if inp.get("eyebrow"):
            card["eyebrow"] = inp["eyebrow"]
        seq.append({"intro": {"card": card}})
    clip: dict[str, Any] = {"clip": take["asset_id"], "in": m["in"], "out": m["out"]}
    if m.get("caption_at") is not None:
        cap: dict[str, Any] = {"at": m["caption_at"], "region": m["region"]}
        if m.get("caption_until"):
            cap["until"] = m["caption_until"]
        if m.get("band"):
            cap["color"] = m["band"]
        clip["caption"] = cap
    seq.append(clip)
    if inp["outro"] == "brand":
        seq.append({"outro": True})
    return seq


def _render(
    job: dict[str, Any], c: dict[str, Any], take: dict[str, Any], m: dict[str, Any], hooks: Hooks
) -> dict[str, Any]:
    _stage(job, "rendering", "rendering the review cut and the GitHub-sized variant")
    q = QUALITIES[job["inputs"]["quality"]]
    w, h = int(take.get("width") or q), int(take.get("height") or q * 9 // 16)
    size = f"{q}x{round(q * h / w) // 2 * 2}"
    seq = _sequence(job, take, m)
    job["sequence"] = seq
    name = paths.slug(job["inputs"]["title"], "video")[:40]
    base = paths.unique_dir(paths.campaign_dir(c["id"], c["name"]) / "videos", f"{job['id']}-a{job['attempt']}")
    rm = hooks.render_montage or montage.render_montage
    outs: dict[str, Any] = {}
    gh_opts: dict[str, Any] = {"name": f"{name}-gh", "size": size, "fps": int(job.get("gh_fps") or 30),
                               "limit": GH_LIMIT, "poster": False}  # fmt: skip
    for key, opts in (
        ("final", {"name": name, "size": size, "crf": render.DELIVERABLE_CRF, "fps": 30, "poster": False}),
        ("gh", gh_opts),
    ):
        r = rm(c["id"], seq, opts, base)
        outs[key] = {
            k: r.get(k)
            for k in ("path", "width", "height", "duration_s", "size_bytes", "violations", "warnings", "attempts",
                      "timeline", "expected_duration_s", "caption_visible_s", "intro_s")
        }  # fmt: skip
        _log(job, f"{key}: {r['width']}×{r['height']} {r['duration_s']:.2f}s {limits.human_bytes(r['size_bytes'])}")
        job["progress"] = _SPAN["rendering"][0] + (0.12 if key == "final" else 0.25)
        save(job)
    job["outputs"] = {**job.get("outputs", {}), **outs}
    save(job)
    return outs


# ── self-review ──────────────────────────────────────────────────────────────
def _gate(gid: str, label: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"id": gid, "label": label, "ok": bool(ok), "detail": detail}


def walk_span(timeline: list[dict[str, Any]], asset_id: int) -> tuple[float, float] | None:
    """Where the walkthrough clip sits in the rendered timeline (start, end) — the beat's
    frozen hold included; the intro/outro items excluded."""
    for r in timeline or []:
        if r.get("kind") == "clip" and int(r.get("asset_id") or 0) == int(asset_id):
            return float(r["start"]), float(r["start"]) + float(r["length"])
    return None


def blank_frames(raw: bytes, w: int = 64, h: int = 36) -> list[tuple[int, float, int]]:
    """(index, mean, spread) of every blank / near-black sample in a raw gray stream."""
    n = w * h
    out = []
    for i in range(len(raw) // n):
        px = sorted(raw[i * n : (i + 1) * n])
        mean = sum(px) / n
        spread = px[int(n * 0.95)] - px[int(n * 0.05)]
        if spread < BLANK_SPREAD or (mean < DARK_MEAN and spread < BLANK_SPREAD * 2):
            out.append((i, round(mean, 1), spread))
    return out


def review(
    job: dict[str, Any], out: dict[str, Any], take: dict[str, Any], m: dict[str, Any], hooks: Hooks
) -> list[dict[str, Any]]:
    """The automated gates, before anything is delivered. Each is {id, label, ok, detail}."""
    final, gh = out["final"], out["gh"]
    q = QUALITIES[job["inputs"]["quality"]]
    gates = [
        _gate("resolution", f"rendered ≥ {q} px wide", (final.get("width") or 0) >= q,
              f"{final.get('width')}×{final.get('height')} (take {take.get('width')}×{take.get('height')})"),
    ]  # fmt: skip
    if job["inputs"]["outro"] == "brand":
        from . import outro as outromod

        need = float((outromod.preset() or {}).get("min_caption_s", DEFAULT_CAPTION_S))
        seen = final.get("caption_visible_s")
        gates.append(
            _gate("final_caption", f"final caption visible ≥ {need:g} s before the outro",
                  seen is not None and seen + 1e-6 >= need,
                  f"{seen:.2f} s" if seen is not None else "couldn't measure the final caption — check it by eye")
        )  # fmt: skip
    if job["inputs"]["intro"] == "brand":
        from . import intro as intromod

        max_s = float((intromod.preset() or {}).get("max_s", intromod.DEFAULTS["max_s"]))
        isec = final.get("intro_s")
        gates.append(_gate("intro", f"intro ≤ {max_s:g} s, revealed in", isec is not None and isec <= max_s + 1e-6,
                           f"{isec:.2f} s until the first shot is fully up" if isec is not None else "no intro rendered"))  # fmt: skip
    span = walk_span(final.get("timeline") or [], take["asset_id"])
    if span:
        blanks = _blank_check(hooks, final["path"], span)
        gates.append(_gate("no_blank_frames", "no blank / near-black frames in the walkthrough", not blanks,
                           f"{len(blanks)} blank sample(s) at " + ", ".join(f"{t:.1f}s" for t in blanks[:8]) if blanks
                           else f"{int((span[1] - span[0]) / SAMPLE_EVERY_S)} samples {span[0]:.1f}–{span[1]:.1f}s, none blank"))  # fmt: skip
    else:
        gates.append(_gate("no_blank_frames", "no blank / near-black frames in the walkthrough", False,
                           "the walkthrough clip isn't in the rendered timeline"))  # fmt: skip
    row = limits.get(GH_LIMIT) or {}
    over = list(gh.get("violations") or [])
    gates.append(_gate("size_budget", f"GitHub variant ≤ {limits.human_bytes(row.get('max_bytes'))}", not over,
                       f"{limits.human_bytes(gh.get('size_bytes'))}" + (f" — {'; '.join(over)}" if over else "")))  # fmt: skip
    try:
        assets = review_assets(job, out, take, span, hooks)
        job["outputs"].update(assets)
        gates.append(_gate("review_assets", "contact sheet + 100 % text crop saved", True,
                           f"{Path(assets['sheet']).name}, {Path(assets['text_crop']).name} @ {assets['text_crop_at']:.1f}s"))  # fmt: skip
    except Exception as e:  # noqa: BLE001
        gates.append(_gate("review_assets", "contact sheet + 100 % text crop saved", False, str(e)))
    return gates


def _blank_check(hooks: Hooks, video: str, span: tuple[float, float]) -> list[float]:
    ff = deps.ffmpeg() or "ffmpeg"
    a, b = span
    fps = 1 / max(SAMPLE_EVERY_S, (b - a) / MAX_SAMPLES)
    r = hooks.ff([ff, "-v", "error", "-ss", f"{a + 0.25:.3f}", "-to", f"{b - 0.25:.3f}", "-i", video, "-vf",
                  f"fps={fps:.4f},scale=64:36,format=gray", "-f", "rawvideo", "-"])  # fmt: skip
    raw = r.stdout if isinstance(r.stdout, bytes) else b""
    return [round(a + 0.25 + i / fps, 2) for i, _mean, _spread in blank_frames(raw)]


def _frame_png(hooks: Hooks, video: str, t: float, dst: Path, vf: str = "") -> Path:
    ff = deps.ffmpeg() or "ffmpeg"
    cmd = [ff, "-v", "error", "-y", "-ss", f"{max(0.0, t):.3f}", "-i", video, "-frames:v", "1"]
    if vf:
        cmd += ["-vf", vf]
    r = hooks.ff([*cmd, str(dst)])
    if getattr(r, "returncode", 0) != 0 or not dst.exists():
        raise render.RenderError(f"couldn't pull a frame at {t:.2f}s")
    return dst


def densest_text(raw: bytes, w: int, h: int, win: tuple[int, int], margin: float = 0.0) -> tuple[int, int, int]:
    """(x, y, score) of the ``win``-sized window crowded with the THINNEST strokes in a gray
    ``w``×``h`` frame (sampled at half the device resolution): a pixel that is a local
    luminance extremum against BOTH horizontal neighbours by > 20 is a 1–2 px stroke — small
    text. Big headings' wide strokes don't count, so the crop lands on the smallest text.
    ``margin`` (a fraction of each side) keeps the search off the app chrome at the edges."""
    ww, wh = win
    acc_rows = [[0] * (w + 1)]
    for y in range(h):
        row = raw[y * w : (y + 1) * w]
        prev = acc_rows[-1]
        cur = [0] * (w + 1)
        acc = 0
        for x in range(1, w - 1):
            a, b, c = row[x - 1], row[x], row[x + 1]
            if (b - a > 20 and b - c > 20) or (a - b > 20 and c - b > 20):
                acc += 1
            cur[x + 1] = prev[x + 1] + acc
        cur[w] = prev[w] + acc
        acc_rows.append(cur)
    edge = acc_rows
    best = (0, 0, -1)
    step = max(1, min(ww, wh) // 4)
    mx, my = int(w * margin), int(h * margin)
    for y in range(my, max(my, h - my - wh) + 1, step):
        for x in range(mx, max(mx, w - mx - ww) + 1, step):
            s = edge[y + wh][x + ww] - edge[y][x + ww] - edge[y + wh][x] + edge[y][x]
            if s > best[2]:
                best = (x, y, s)
    return best


def review_assets(job: dict[str, Any], out: dict[str, Any], take: dict[str, Any], span: tuple[float, float] | None,
                  hooks: Hooks) -> dict[str, Any]:  # fmt: skip
    """A labelled contact sheet of the rendered video and a 100 % crop of its densest small text."""
    final = out["final"]
    video = final["path"]
    d = Path(video).parent / "review"
    d.mkdir(parents=True, exist_ok=True)
    W, H = int(final.get("width") or 2560), int(final.get("height") or 1440)
    # the 100 % crop: the densest-text window of three candidate walkthrough frames
    a, b = span or (0.0, float(final.get("duration_s") or 1))
    best = None
    sw, sh = W // 2, H // 2  # half resolution: small text keeps its thin strokes
    ff = deps.ffmpeg() or "ffmpeg"
    for frac in (0.15, 0.3, 0.45, 0.6, 0.75, 0.9):
        t = a + (b - a) * frac
        r = hooks.ff([ff, "-v", "error", "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1", "-vf",
                      f"scale={sw}:{sh},format=gray", "-f", "rawvideo", "-"])  # fmt: skip
        raw = r.stdout if isinstance(r.stdout, bytes) else b""
        if len(raw) < sw * sh:
            continue
        cw, ch = max(4, CROP[0] * sw // W), max(4, CROP[1] * sh // H)
        x, y, score = densest_text(raw, sw, sh, (cw, ch), margin=0.06)
        if best is None or score > best[3]:
            best = (t, x * W // sw, y * H // sh, score)
    if best is None:
        raise render.RenderError("couldn't sample the walkthrough for the text crop")
    t, x, y, _ = best
    x, y = min(x, W - CROP[0]) // 2 * 2, min(y, H - CROP[1]) // 2 * 2
    crop = _frame_png(hooks, video, t, d / "text-crop-100.png", f"crop={CROP[0]}:{CROP[1]}:{x}:{y}")
    sheet = contact_sheet(hooks, video, final, span, d / "contact-sheet.png")
    return {"sheet": str(sheet), "text_crop": str(crop), "text_crop_at": round(t, 2),
            "text_crop_box": {"x": x, "y": y, "w": CROP[0], "h": CROP[1]}}  # fmt: skip


def sheet_times(final: dict[str, Any], span: tuple[float, float] | None, n_walk: int = 8) -> list[tuple[float, str]]:
    """(time, label) for every tile: each intro/outro item's middle, the transition in, the first
    settled walkthrough frame, ``n_walk`` evenly spaced walkthrough frames, the final caption."""
    dur = float(final.get("duration_s") or 0)
    picks: list[tuple[float, str]] = []
    rows = final.get("timeline") or []
    for r in rows:
        s, ln = float(r["start"]), float(r["length"])
        is_walk = span is not None and abs(s - span[0]) < 1e-3
        if is_walk:
            continue
        what = r.get("title") or ("card" if r.get("kind") == "card" else "clip")
        picks.append((s + ln / 2, str(what)[:40]))
    if span:
        a, b = span
        k = next((i for i, r in enumerate(rows) if abs(float(r["start"]) - a) < 1e-3), None)
        tr = (rows[k].get("transition") or {}) if k is not None else {}
        if tr.get("duration"):
            picks.append((a + float(tr["duration"]) / 2, "transition in"))
            picks.append((a + float(tr["duration"]) + 0.1, "first settled frame"))
        for i in range(n_walk):
            picks.append((a + (b - a) * (i + 0.5) / n_walk, f"walkthrough {i + 1}/{n_walk}"))
        picks.append((b - 0.8, "final caption / beat"))
    picks = sorted((min(max(0.0, t), max(0.0, dur - 0.05)), lab) for t, lab in picks)
    return picks


def contact_sheet(hooks: Hooks, video: str, final: dict[str, Any], span: tuple[float, float] | None, dst: Path) -> Path:
    tiles = sheet_times(final, span)
    work = dst.parent / "tiles"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    frames = [_frame_png(hooks, video, t, work / f"{i:03d}.jpg", "scale=640:-2") for i, (t, _) in enumerate(tiles)]
    cols = 4
    rows = (len(frames) + cols - 1) // cols
    W = cols * 640
    Hh = rows * (360 + 40) + 20
    try:
        import base64

        cells = "".join(
            f'<figure><img src="data:image/jpeg;base64,{base64.b64encode(f.read_bytes()).decode()}">'
            f"<figcaption>#{i + 1} · {t:.2f}s · {lab}</figcaption></figure>"
            for i, (f, (t, lab)) in enumerate(zip(frames, tiles))
        )
        page = (
            "<!doctype html><html><head><meta charset='utf-8'><style>"
            f"*{{margin:0;padding:0;box-sizing:border-box}}html,body{{width:{W}px;height:{Hh}px;background:#202024}}"
            f"main{{display:grid;grid-template-columns:repeat({cols},640px);padding-top:10px}}"
            "figure{height:400px;display:flex;flex-direction:column}"
            "figcaption{order:-1;height:40px;line-height:40px;padding:0 10px;color:#ffd166;"
            "font:22px ui-monospace,Menlo,monospace;white-space:nowrap;overflow:hidden}"
            "img{width:640px;height:360px;object-fit:contain;background:#000}"
            f"</style></head><body><main>{cells}</main></body></html>"
        )
        if hooks.page_renderer:
            return Path(hooks.page_renderer(page, W, Hh, dst))
        from . import cards

        return Path(cards.render_page(page, dst, W, Hh, playwright_factory=hooks.playwright_factory)["path"])
    except Exception as e:  # noqa: BLE001 — no browser: an unlabelled ffmpeg tile, times in the job
        log.info("[campaign] labelled sheet failed (%s) — tiling with ffmpeg", e)
        ff = deps.ffmpeg() or "ffmpeg"
        hooks.ff([ff, "-v", "error", "-y", "-i", str(work / "%03d.jpg"), "-vf",
                  f"scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:-1:-1,tile={cols}x{rows}:padding=6",
                  "-frames:v", "1", str(dst)])  # fmt: skip
        return dst


# ── delivery ─────────────────────────────────────────────────────────────────
def _deliver(job: dict[str, Any], c: dict[str, Any], out: dict[str, Any], hooks: Hooks) -> None:
    _stage(job, "delivering", "registering + publishing the files")
    take = job["take"]
    title = job["inputs"]["title"]
    delivery: dict[str, Any] = {"assets": {}, "links": {}, "files": {}}
    for key, label, kind in (("final", "", "montage"), ("gh", " (GitHub ≤ 10 MB)", "montage")):
        o = out[key]
        a = store.add_asset(
            c["id"], kind, f"{title}{label}", status="rendered", path=o["path"], size_bytes=o["size_bytes"],
            width=o["width"], height=o["height"], duration_s=o["duration_s"],
            limit_id=GH_LIMIT if key == "gh" else "", parent_id=take["asset_id"],
            meta={"video_job": job["id"], "sequence": job.get("sequence"), "timeline": o.get("timeline"),
                  "violations": o.get("violations"), "gates": job.get("gates")},
            notes=("VIOLATES: " + "; ".join(o["violations"])) if o.get("violations") else "",
        )  # fmt: skip
        delivery["assets"][key] = a["id"]
        delivery["files"][key] = o["path"]
    for key in ("sheet", "text_crop"):
        p = job["outputs"].get(key)
        if p and Path(p).is_file():
            a = store.add_asset(c["id"], "still", f"{title}: {key.replace('_', ' ')}", status="rendered", path=p,
                                parent_id=delivery["assets"]["final"])  # fmt: skip
            delivery["assets"][key] = a["id"]
            delivery["files"][key] = p
    if not any(not g["ok"] for g in job.get("gates") or []):
        for key in ("final", "gh"):
            try:
                store.update_asset(delivery["assets"][key], status="ready_for_review")
            except ValueError as e:
                _log(job, f"couldn't mark {key} ready_for_review: {e}")
    if hooks.save_media:
        for key, mime in (
            ("final", "video/mp4"),
            ("gh", "video/mp4"),
            ("sheet", "image/png"),
            ("text_crop", "image/png"),
        ):
            p = delivery["files"].get(key)
            if not p:
                continue
            try:
                ref = hooks.save_media(
                    p, mime, {"source": "campaign", "title": f"{title} ({key})", "video_job": job["id"]}
                )
                url = getattr(ref, "url", None) or (ref.get("url") if isinstance(ref, dict) else None)
                if url:
                    delivery["links"][key] = url
            except Exception as e:  # noqa: BLE001 — the file path is still the answer
                _log(job, f"media store refused {key}: {e}")
    delivery["artifacts_saved"] = _save_artifacts(job, delivery, hooks)
    dd = job.get("deliver_dir")
    if dd:
        dst = Path(dd).expanduser()
        dst.mkdir(parents=True, exist_ok=True)
        stem = f"vfr-{paths.slug(title, 'video')[:40]}"
        for key, suffix in (
            ("final", ".mp4"),
            ("gh", "-gh.mp4"),
            ("sheet", "-sheet.png"),
            ("text_crop", "-text-crop.png"),
        ):
            p = delivery["files"].get(key)
            if p:
                shutil.copy(p, dst / f"{stem}{suffix}")
                delivery.setdefault("copied", {})[key] = str(dst / f"{stem}{suffix}")
    job["delivery"] = delivery
    save(job)


def _save_artifacts(job: dict[str, Any], delivery: dict[str, Any], hooks: Hooks) -> bool:
    """Put the review mp4 + sheet in the requesting chat's Artifact panel through core's
    ``artifact.save_file`` service (protoAgent#4254). False when the host lacks it (older core,
    artifact plugin off) — the completion turn then asks the agent to save_file_artifact."""
    svc = hooks.save_artifact() if hooks.save_artifact else None
    if svc is None or job["status"] == "failed":
        return False
    ok = True
    for key, title in (("final", job["inputs"]["title"]), ("sheet", f"{job['inputs']['title']} — review sheet")):
        p = delivery["files"].get(key)
        if not p:
            continue
        try:
            r = svc(path=p, title=title, session_id=job.get("session_id") or "", plugin_id="campaign",
                    roots=[str(paths.data_dir())])  # fmt: skip
        except Exception as e:  # noqa: BLE001 — a provider bug must not fail the job
            r = {"ok": False, "message": str(e)}
        delivery.setdefault("artifacts", {})[key] = r.get("id", "")
        if not r.get("ok"):
            ok = False
            _log(job, f"artifact panel refused {key}: {r.get('message')}")
    return ok


def _finish(job: dict[str, Any], hooks: Hooks) -> None:
    if hooks.emit:
        try:
            hooks.emit("video_finished", {"job_id": job["id"], "status": job["status"],
                                          "assets": (job.get("delivery") or {}).get("assets", {})})  # fmt: skip
        except Exception:  # noqa: BLE001
            pass
    if hooks.notify and job.get("session_id"):
        try:
            hooks.notify(job["session_id"], job["id"], completion_prompt(job))
        except Exception as e:  # noqa: BLE001
            log.warning("[campaign] video %s: couldn't nudge the chat: %s", job["id"], e)


def completion_prompt(job: dict[str, Any]) -> str:
    """The follow-up turn the requesting chat gets when the job ends: hand the files over."""
    d = job.get("delivery") or {}
    files = d.get("files") or {}
    status_call = f"campaign_video_status(job_id={job['id']!r})"
    if job["status"] == "done":
        if d.get("artifacts_saved"):
            how = (
                "The review mp4 and its sheet are already in this chat's Artifact panel. "
                f"Reply with {status_call} — its gate table and links (it also attaches the file to the reply)."
            )
        else:
            how = (
                "Deliver it now, following the produce-walkthrough-video skill: "
                f"save_file_artifact(path={files.get('final')!r}, title={job['inputs']['title']!r}) and "
                f"save_file_artifact(path={files.get('sheet')!r}, title='review sheet'), then reply with "
                f"{status_call} — its gate table and links."
            )
        return (
            f"[campaign] Video job {job['id']} ({job['inputs']['title']!r}) finished and passed every "
            f"self-review gate. {how}"
        )
    return (
        f"[campaign] Video job {job['id']} ({job['inputs']['title']!r}) ended {job['status'].upper()}: "
        + "; ".join(job.get("reasons") or ["no reason recorded"])
        + f". Read {status_call}, LOOK at the review sheet, and tell the operator "
        "what failed and what you propose — do not present it as finished."
    )


def absolute(url: str, bases: list[str]) -> str:
    """A server-relative ``/media/…`` link made absolute against this agent's public base."""
    if not url or not url.startswith("/") or not bases:
        return url
    return bases[0].rstrip("/") + url


def summary(job: dict[str, Any]) -> str:
    """The status text a tool / route returns."""
    inp = job["inputs"]
    lines = [
        *(
            ["(This request matched an existing job — it was REUSED, not re-recorded; force=True re-records.)"]
            if job.get("reused")
            else []
        ),
        f"Video job {job['id']} — {inp['title']!r} ({inp['quality']}, intro {inp['intro']}, outro {inp['outro']})",
        f"status: {job['status']} · stage: {job.get('stage')} · progress {round(progress(job) * 100)}% · attempt {job.get('attempt', 0)}",
    ]
    if job.get("gates"):
        lines.append("\nSelf-review gates:")
        lines += [f"- [{'PASS' if g['ok'] else 'FAIL'}] {g['label']} — {g['detail']}" for g in job["gates"]]
    if job.get("reasons") and job["status"] != "done":
        lines.append("\nWhy it isn't done:")
        lines += [f"- {r}" for r in job["reasons"]]
    d = job.get("delivery") or {}
    bases = link_bases()
    if d.get("files"):
        lines.append("\nFiles:")
        for k, p in d["files"].items():
            link = absolute((d.get("links") or {}).get(k) or "", bases)
            aid = (d.get("assets") or {}).get(k)
            lines.append(f"- {k}: {p}" + (f" (asset #{aid})" if aid else "") + (f"\n  link: {link}" if link else ""))
    if d.get("links"):
        lines.append(
            "\nTo a remote caller (A2A, /v1) give the links above — they download the file directly; "
            "there is no Artifact panel or Download button on their side."
            + ("" if bases else " The links are relative to this agent's base URL — prefix it.")
        )
    lines.append("\nRecent:")
    lines += [f"  {x}" for x in (job.get("log") or [])[-6:]]
    return "\n".join(lines)
