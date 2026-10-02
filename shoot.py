"""The shoot — a validated shot script → a recorded take, via the out-of-process worker.

The browser itself runs in ``worker/pw_worker.py`` under an interpreter that has playwright
(``interpreter.py`` resolves it); THIS module never imports playwright. It owns the host-side
half: which bearer a script may carry (and reading it from the environment), resolving goto
URLs, the fence the worker must enforce, and turning the worker's report back into a result
(or a :class:`ShootError`). The worker writes into the take directory:

* ``<name>.webm`` — the raw recording (video t=0 is page creation);
* ``<shot>.png`` — one still per ``screenshot`` step;
* ``timing.json`` — every step's start/end and every ``mark`` in seconds on the video's
  clock, which is what ``campaign_render`` trims and speed-ramps by;
* ``failure.png`` — on a failed step, what the page looked like at that moment.

Bounded twice: the worker runs every step under ``step_timeout_ms`` (clamped to what's left of
``total_timeout_s``) and stops between steps once the budget is spent; and the host kills the
worker's whole process tree (driver + Chromium) if it overruns ``total_timeout_s`` + grace.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any, Callable

from . import interpreter
from .shotscript import browser_origin, describe_step, resolve_url
from .worker import pw_worker
from .worker.pw_worker import (  # noqa: F401 — re-exported: one definition, host and worker
    CURSOR_JS,
    REDACT_JS,
    REDACT_PRESET_RULES,
    mask_css,
    origin,
    redact_rules,
    video_size,
)


# Seconds past total_timeout_s before the worker is killed: browser launch + context close
# (which finalizes the video) + interpreter start-up.
KILL_GRACE_S = 60.0

# ── what the shoot browser may carry and reach ─────────────────────────────────
# A shot script is agent-written. Two things it must never be able to do:
#
# 1. Lift an arbitrary secret out of the host's environment. ``auth.bearer_env`` may only
#    name a variable the operator set up FOR this purpose: ``CAMPAIGN_*``, or one listed in
#    the ``bearer_envs`` setting. The host's own operator/fleet credentials are refused even
#    if listed — with one, the shoot browser IS the operator.
# 2. Act as the operator. The browser never reaches this plugin's data API (where the
#    approve/reject route lives) on ANY host, so a script can't open the gallery and click
#    Approve — in open mode (no bearer) as much as with a stolen one.
#
# And the bearer it does carry goes ONLY to the script's own ``base_url`` origin — never to a
# CDN, an analytics pixel, or whatever third-party origin the page pulls in.
BEARER_ENV_PREFIX = "CAMPAIGN_"
FORBIDDEN_BEARER_ENVS = frozenset({"A2A_AUTH_TOKEN", "PROTOAGENT_FLEET_TOKEN", "FEDERATION_TOKEN"})
_ALLOWED_BEARER_ENVS: frozenset[str] = frozenset()
# The fence the worker enforces: this plugin's own data API, on any host or fleet-proxy
# prefix — matched case-insensitively against every decoded/normalized form of the path (see
# pw_worker.fence_views), and FAIL CLOSED on a URL it cannot read. The approve/reject route lives there.
FENCE_PATTERNS = [r"/api/plugins/campaign(?:/|$)"]

# ── what an `upload` step may hand to a file input ─────────────────────────────
# A shot script is agent-written, and a file it uploads lands in whatever app the browser has
# open — so an upload is an EXFILTRATION path unless it's fenced. The fence (checked here, on
# the host, at shoot time — against the disk as it is THEN, symlinks resolved):
#
# * the operator's ``upload_dirs`` allowlist; EMPTY (the default) refuses every upload;
# * each path must exist and be a regular file (no dirs, devices, FIFOs), ≤ MAX_UPLOAD_BYTES
#   each and MAX_UPLOAD_TOTAL_BYTES per step;
# * never the agent's secrets, even inside an allowlisted dir: credential dirs (.ssh, .aws,
#   .gnupg, …), key/credential file names (secrets.yaml, .env, id_rsa, *.pem, …), and anything
#   under the protoAgent home (~/.protoagent, $PROTOAGENT_HOME) except this plugin's own media;
# * an allowlist entry that is the filesystem root or the home dir itself is ignored — too broad.
MAX_UPLOAD_BYTES = 50 * 1000 * 1000
MAX_UPLOAD_TOTAL_BYTES = 100 * 1000 * 1000
_UPLOAD_DIRS: tuple[str, ...] = ()
SECRET_DIR_NAMES = frozenset(
    {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store", ".gcloud", "gcloud", "keychains"}
)
SECRET_FILE_RE = re.compile(
    r"^(?:secrets?\.(?:ya?ml|json|toml)|\.env(?:\..*)?|\.netrc|_netrc|\.git-credentials|\.pgpass|\.npmrc|\.pypirc"
    r"|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|credentials(?:\..*)?|auth\.json|.*\.(?:pem|key|p12|pfx|kdbx|keychain-db))$",
    re.IGNORECASE,
)


def configure(bearer_envs: Any = "", upload_dirs: Any = "") -> None:
    """Extra env-var NAMES (besides ``CAMPAIGN_*``) a shot script may use as its bearer, and
    the directories an ``upload`` step may read files from (empty = uploads refused)."""
    global _ALLOWED_BEARER_ENVS, _UPLOAD_DIRS
    if isinstance(bearer_envs, (list, tuple, set)):
        names = [str(n) for n in bearer_envs]
    else:
        names = re.split(r"[,\s]+", str(bearer_envs or ""))
    _ALLOWED_BEARER_ENVS = frozenset(n.strip() for n in names if n.strip())
    if isinstance(upload_dirs, (list, tuple, set)):
        dirs = [str(d) for d in upload_dirs]
    else:
        dirs = re.split(r"[,\n]+", str(upload_dirs or ""))
    _UPLOAD_DIRS = tuple(d.strip() for d in dirs if d.strip())


def _protoagent_homes() -> list[Path]:
    import os

    homes = [Path.home() / ".protoagent"]
    env = os.environ.get("PROTOAGENT_HOME", "").strip()
    if env:
        homes.append(Path(env).expanduser())
    out = []
    for h in homes:
        try:
            out.append(h.resolve())
        except (OSError, RuntimeError):
            pass
    return out


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def upload_roots() -> tuple[list[Path], list[str]]:
    """(the usable allowlisted dirs, resolved; a note for each configured entry that isn't)."""
    roots: list[Path] = []
    notes: list[str] = []
    home = Path.home().resolve()
    for d in _UPLOAD_DIRS:
        p = Path(d).expanduser()
        if not p.is_absolute():
            notes.append(f"upload_dirs entry {d!r} is not an absolute path — ignored")
            continue
        try:
            r = p.resolve(strict=True)
        except (OSError, RuntimeError):
            notes.append(f"upload_dirs entry {d!r} doesn't exist — ignored")
            continue
        if not r.is_dir():
            notes.append(f"upload_dirs entry {d!r} is not a directory — ignored")
        elif r == Path(r.anchor) or r == home:
            notes.append(f"upload_dirs entry {d!r} is the filesystem root or your home dir — too broad, ignored")
        else:
            roots.append(r)
    return roots, notes


def upload_problem(raw: str, roots: list[Path]) -> tuple[str | None, str, int]:
    """(why ``raw`` may not be uploaded or None, its resolved path, its size)."""
    import stat

    p = Path(raw).expanduser()
    if not p.is_absolute():
        return f"{raw!r} is not an absolute path", "", 0
    try:
        real = p.resolve(strict=True)
        st = real.stat()
    except (OSError, RuntimeError):
        return f"{raw!r} doesn't exist", "", 0
    if not stat.S_ISREG(st.st_mode):
        return f"{raw!r} is not a regular file", "", 0
    # Symlinks are resolved FIRST, so a link inside an allowlisted dir can't point out of it.
    if not any(_within(real, r) for r in roots):
        return (
            f"{raw!r} is outside the plugin's upload_dirs ({', '.join(str(r) for r in roots)}) — "
            "copy it into one of them, or ask the operator to allowlist its directory",
            "",
            0,
        )
    parts = [x.lower() for x in real.parts]
    gh_config = any(a == ".config" and b == "gh" for a, b in zip(parts, parts[1:]))
    if gh_config or any(x in SECRET_DIR_NAMES for x in parts):
        return f"{raw!r} is inside a credentials directory — never uploaded", "", 0
    if SECRET_FILE_RE.match(real.name) or SECRET_FILE_RE.match(p.name):
        return f"{raw!r} looks like a key or credentials file — never uploaded", "", 0
    try:
        from . import paths

        own_media = paths.media_root().resolve()
    except Exception:  # noqa: BLE001 — no media root → nothing under the agent home is exempt
        own_media = None
    for h in _protoagent_homes():
        if _within(real, h) and not (own_media and _within(real, own_media)):
            return f"{raw!r} is inside the agent's home ({h}) — never uploaded (only this plugin's own media is)", "", 0
    if st.st_size > MAX_UPLOAD_BYTES:
        return f"{raw!r} is {st.st_size} bytes, over the {MAX_UPLOAD_BYTES // 1_000_000} MB per-file max", "", 0
    return None, str(real), st.st_size


def check_uploads(script: dict[str, Any]) -> dict[int, list[str]]:
    """Every ``upload`` step's files, checked against the fence above → ``{step index: [resolved
    paths]}``. Raises :class:`ShootError` naming the step and the file on the first refusal —
    before any browser starts."""
    steps = [s for s in script.get("steps", []) if s.get("op") == "upload"]
    if not steps:
        return {}
    roots, notes = upload_roots()

    def refuse(step: dict[str, Any], why: str) -> ShootError:
        return ShootError(
            f"step {step['index']} ({describe_step(step)}) refused: {why}", {"steps": [], "error": "upload refused"}
        )

    if not roots:
        why = (
            "file uploads are off — the plugin's upload_dirs setting allowlists no directory. Ask the "
            "operator to set upload_dirs (Settings ▸ Campaign Studio) to the folder(s) holding the files "
            "to upload, e.g. a demo-assets folder."
        )
        raise refuse(steps[0], why + (f" ({'; '.join(notes)})" if notes else ""))
    out: dict[int, list[str]] = {}
    for step in steps:
        resolved, total = [], 0
        for f in step["files"]:
            problem, real, size = upload_problem(f, roots)
            if problem:
                raise refuse(step, problem)
            total += size
            resolved.append(real)
        if total > MAX_UPLOAD_TOTAL_BYTES:
            raise refuse(step, f"{total} bytes in one step is over the {MAX_UPLOAD_TOTAL_BYTES // 1_000_000} MB max")
        out[step["index"]] = resolved
    return out


def bearer_env_problem(name: str) -> str | None:
    """Why ``name`` may not be used as a shot script's bearer (None = it may)."""
    if name in FORBIDDEN_BEARER_ENVS or name.endswith("_FLEET_TOKEN"):
        return (
            f"auth.bearer_env {name} is the host's own operator credential — a recording browser "
            "never carries it. Create a separate token for the target app."
        )
    if name.startswith(BEARER_ENV_PREFIX) or name in _ALLOWED_BEARER_ENVS:
        return None
    return (
        f"auth.bearer_env {name} isn't allowed: a shot script may only read env vars named "
        f"{BEARER_ENV_PREFIX}* or listed in the plugin's bearer_envs setting (so a script can't lift "
        "an unrelated secret out of the agent's environment). Ask the operator to set one up."
    )


def is_own_api(url: str) -> bool:
    """True for any request into this plugin's data API, on any host or fleet-proxy prefix."""
    return pw_worker.path_blocked(url, FENCE_PATTERNS)


class ShootError(RuntimeError):
    """A step failed. ``result`` carries the partial take (log, failure still, video)."""

    def __init__(self, message: str, result: dict[str, Any]):
        super().__init__(message)
        self.result = result


class WorkerError(RuntimeError):
    """The worker couldn't run at all (no interpreter, crashed, killed on overrun)."""


def fence() -> dict[str, list[str]]:
    return {"block_paths": list(FENCE_PATTERNS)}


def run_worker(job: dict[str, Any], timeout: float, playwright_factory: Callable | None = None) -> dict[str, Any]:
    """Run one worker job; return its report. Shared by shoots and cards.

    ``playwright_factory`` is the TEST seam: the job is round-tripped through JSON (exactly
    what the subprocess would receive) and run in-process against a fake browser. In
    production the job goes to a separate interpreter on stdin."""
    if playwright_factory is not None:
        return pw_worker.run_job(json.loads(json.dumps(job)), playwright_factory)
    from . import deps

    res = deps.resolve()
    if not res.python or res.need in deps.NOT_RUNNABLE:
        raise WorkerError(deps.need_browser() or "no Python with playwright was found")
    with tempfile.TemporaryDirectory(prefix="campaign-job-") as tmp:
        report_path = Path(tmp) / "report.json"
        payload = json.dumps({**job, "report_path": str(report_path)}).encode("utf-8")
        done = interpreter.run_python(res.python, [str(interpreter.WORKER)], stdin=payload, timeout=timeout)
        secret = str(job.get("bearer") or "")

        def clean(text: str) -> str:
            text = text.replace(secret, "•••") if secret else text
            lines = [ln for ln in text.strip().splitlines() if ln.strip()]
            return " | ".join(lines[-3:])[:600]

        if done.timed_out:
            raise WorkerError(
                f"the browser worker overran its {timeout:.0f}s budget and was killed (with its Chromium)"
            )
        if not report_path.is_file():
            raise WorkerError(
                f"the browser worker ({res.source}: {res.python}) exited {done.returncode} without a report"
                + (f": {clean(done.stderr or done.stdout)}" if (done.stderr or done.stdout).strip() else "")
            )
        return json.loads(report_path.read_text(encoding="utf-8"))


def read_bearer(script: dict[str, Any], env: dict[str, str] | None = None) -> str:
    """The script's bearer, read from the HOST environment under the allowlist — or ''."""
    import os

    env = os.environ if env is None else env
    name = script["auth"].get("bearer_env")
    if not name:
        return ""
    problem = bearer_env_problem(name)
    if problem is None and not origin(script.get("base_url") or ""):
        problem = "auth.bearer_env needs a base_url — the bearer is sent to that origin only"
    if problem:
        raise ShootError(problem, {"steps": [], "error": "bearer refused"})
    bearer = env.get(name, "")
    if not bearer:
        raise ShootError(
            f"auth.bearer_env names {name}, which isn't set in the agent's environment",
            {"steps": [], "error": "missing bearer"},
        )
    return bearer


def build_job(script: dict[str, Any], out_dir: str | Path, bearer: str = "") -> dict[str, Any]:
    """The worker job for a VALIDATED script. Goto URLs are resolved and step descriptions
    precomputed here, so the worker needs nothing from the plugin."""
    script = dict(script)
    uploads = check_uploads(script)
    script["steps"] = [
        {
            **s,
            "_desc": describe_step(s),
            **({"_url": resolve_url(script, s["url"])} if s["op"] == "goto" else {}),
            **({"_files": uploads[s["index"]]} if s["op"] == "upload" else {}),
        }
        for s in script["steps"]
    ]
    # Browser-side, both run inside a `location.origin` check: storage on its origin, the
    # init_script on the base_url's.
    script["_base_origin"] = browser_origin(script.get("base_url") or "")
    return {
        "v": pw_worker.JOB_VERSION,
        "kind": "shoot",
        "script": script,
        "out_dir": str(Path(out_dir)),
        "bearer": bearer,
        "fence": fence(),
    }


def run(
    script: dict[str, Any],
    out_dir: str | Path,
    *,
    playwright_factory: Callable | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Record one take of a VALIDATED script into ``out_dir`` through the worker.

    Raises :class:`ShootError` (with the partial result) when a step fails, and
    :class:`WorkerError` when the worker can't run or is killed on overrun.
    """
    bearer = read_bearer(script, env)
    job = build_job(script, out_dir, bearer)
    report = run_worker(job, float(script["total_timeout_s"]) + KILL_GRACE_S, playwright_factory)
    result = report.get("result") or {}
    if not report.get("ok"):
        raise ShootError(report.get("error") or "the take failed", {"steps": [], **result})
    return result
