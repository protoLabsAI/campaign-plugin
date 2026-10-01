"""Interpreter resolution, the scrubbed child env, and spawning with a hard kill — real
subprocesses where it matters (the kill, the probe), fakes only for which interpreters exist."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from campaign import interpreter
from campaign.interpreter import Probe

REAL_MANAGED_PYTHON = interpreter.managed_python  # captured before the autouse stub


@pytest.fixture
def probes(monkeypatch):
    """Fake per-interpreter probe answers: {python: Probe}."""
    table: dict[str, Probe] = {}
    monkeypatch.setattr(
        interpreter, "probe", lambda py: table.get(py, Probe(py, ok=False, error=f"{py} doesn't exist"))
    )
    return table


def _p(py, **kw):
    return Probe(py, ok=True, **{"playwright": "1.50.0", "chromium": True, **kw})


# ── resolution order ──────────────────────────────────────────────────────────
def test_the_setting_wins_and_is_strict(probes, monkeypatch):
    probes["/opt/py"] = _p("/opt/py")
    probes["/rt/python3"] = _p("/rt/python3")
    monkeypatch.setattr(interpreter, "managed_python", lambda: "/rt/python3")
    interpreter.configure("/opt/py")
    r = interpreter.resolve()
    assert (r.python, r.source, r.ready) == ("/opt/py", interpreter.SOURCE_CONFIG, True)

    interpreter.configure("/missing/py")
    r = interpreter.resolve()
    assert r.need == "interpreter" and "doesn't exist" in r.detail, "a bad setting never silently falls through"

    probes["/opt/nopw"] = _p("/opt/nopw", playwright=None)
    interpreter.configure("/opt/nopw")
    r = interpreter.resolve()
    assert r.need == "interpreter" and "pip install" in r.detail


def test_managed_runtime_before_the_host(probes, monkeypatch):
    probes["/rt/python3"] = _p("/rt/python3")
    probes[sys.executable] = _p(sys.executable)
    monkeypatch.setattr(interpreter, "managed_python", lambda: "/rt/python3")
    monkeypatch.setattr(interpreter, "host_has_playwright", lambda: True)
    r = interpreter.resolve()
    assert (r.python, r.source) == ("/rt/python3", interpreter.SOURCE_MANAGED)


def test_a_runtime_without_playwright_falls_through_to_the_host_on_a_source_install(probes, monkeypatch):
    probes["/rt/python3"] = _p("/rt/python3", playwright=None, chromium=False)
    probes[sys.executable] = _p(sys.executable, chromium=False)
    monkeypatch.setattr(interpreter, "managed_python", lambda: "/rt/python3")
    monkeypatch.setattr(interpreter, "host_has_playwright", lambda: True)
    monkeypatch.setattr(interpreter, "frozen", lambda: False)
    r = interpreter.resolve()
    assert (r.python, r.source, r.need) == (sys.executable, interpreter.SOURCE_HOST, "chromium")


def test_source_install_with_nothing_needs_deps(probes, monkeypatch):
    monkeypatch.setattr(interpreter, "host_has_playwright", lambda: False)
    monkeypatch.setattr(interpreter, "frozen", lambda: False)
    assert interpreter.resolve().need == "deps"


def test_frozen_never_uses_its_own_executable(probes, monkeypatch):
    # A frozen sys.executable is the server binary: running a script with it relaunches the app.
    probes[sys.executable] = _p(sys.executable)
    monkeypatch.setattr(interpreter, "frozen", lambda: True)
    monkeypatch.setattr(interpreter, "host_has_playwright", lambda: True)
    assert interpreter.resolve().need == "runtime", "no managed runtime → provision it"

    probes["/rt/python3"] = _p("/rt/python3", playwright=None, chromium=False)
    monkeypatch.setattr(interpreter, "managed_python", lambda: "/rt/python3")
    r = interpreter.resolve()
    assert (r.need, r.python) == ("deps", "/rt/python3"), "runtime present, playwright not → Install dependencies"


def test_managed_python_seam_degrades_without_a_host(monkeypatch):
    monkeypatch.setattr(interpreter, "managed_python", REAL_MANAGED_PYTHON)
    assert interpreter.managed_python() is None, "no `infra` package here → not provisioned, never an exception"


def test_managed_python_seam_reads_cores_function(monkeypatch, tmp_path):
    import types

    monkeypatch.setattr(interpreter, "managed_python", REAL_MANAGED_PYTHON)
    fake = types.ModuleType("infra.python_runtime")
    fake.managed_python_exe = lambda: tmp_path / "bin" / "python3"
    monkeypatch.setitem(sys.modules, "infra", types.ModuleType("infra"))
    monkeypatch.setitem(sys.modules, "infra.python_runtime", fake)
    assert interpreter.managed_python() == str(tmp_path / "bin" / "python3")
    fake.managed_python_exe = lambda: None
    assert interpreter.managed_python() is None
    del fake.managed_python_exe
    assert interpreter.managed_python() is None, "a renamed seam degrades, it doesn't raise"


def _fake_host(monkeypatch, *, sdk_fn=None, infra_fn=None):
    """Install a fake `graph.sdk` (with or without the accessor) and `infra.python_runtime`."""
    import types

    graph = types.ModuleType("graph")
    sdk = types.ModuleType("graph.sdk")
    if sdk_fn is not None:
        sdk.managed_python_exe = sdk_fn
    graph.sdk = sdk
    monkeypatch.setitem(sys.modules, "graph", graph)
    monkeypatch.setitem(sys.modules, "graph.sdk", sdk)
    pr = types.ModuleType("infra.python_runtime")
    if infra_fn is not None:
        pr.managed_python_exe = infra_fn
    monkeypatch.setitem(sys.modules, "infra", types.ModuleType("infra"))
    monkeypatch.setitem(sys.modules, "infra.python_runtime", pr)


def test_managed_python_prefers_the_public_sdk_accessor(monkeypatch, tmp_path):
    monkeypatch.setattr(interpreter, "managed_python", REAL_MANAGED_PYTHON)
    _fake_host(
        monkeypatch, sdk_fn=lambda: tmp_path / "sdk" / "python3", infra_fn=lambda: tmp_path / "infra" / "python3"
    )
    assert interpreter.managed_python() == str(tmp_path / "sdk" / "python3")


def test_managed_python_falls_back_to_infra_on_an_older_core(monkeypatch, tmp_path):
    """A core before protoAgent#3992 has `graph.sdk` but no `managed_python_exe` on it."""
    monkeypatch.setattr(interpreter, "managed_python", REAL_MANAGED_PYTHON)
    _fake_host(monkeypatch, sdk_fn=None, infra_fn=lambda: tmp_path / "infra" / "python3")
    assert interpreter.managed_python() == str(tmp_path / "infra" / "python3")


def test_sdk_accessor_saying_not_provisioned_is_final(monkeypatch, tmp_path):
    """None from the SDK means not provisioned — no second opinion from the internal."""
    monkeypatch.setattr(interpreter, "managed_python", REAL_MANAGED_PYTHON)
    _fake_host(monkeypatch, sdk_fn=lambda: None, infra_fn=lambda: tmp_path / "infra" / "python3")
    assert interpreter.managed_python() is None


def test_a_raising_sdk_accessor_degrades(monkeypatch):
    def boom():
        raise OSError("nope")

    monkeypatch.setattr(interpreter, "managed_python", REAL_MANAGED_PYTHON)
    _fake_host(monkeypatch, sdk_fn=boom)
    assert interpreter.managed_python() is None


# ── the real probe ────────────────────────────────────────────────────────────
def test_the_worker_probe_runs_for_real_on_this_interpreter():
    interpreter.invalidate()
    p = interpreter.probe(sys.executable)
    assert p.ok, p.error
    try:
        import importlib.util

        has = importlib.util.find_spec("playwright") is not None
    except ImportError:
        has = False
    assert bool(p.playwright) == has


def test_probe_of_a_non_python_is_a_clear_error(tmp_path):
    fake = tmp_path / "python"
    fake.write_text("#!/bin/sh\necho nope\nexit 3\n")
    fake.chmod(0o755)
    p = interpreter.probe(str(fake))
    assert not p.ok and "couldn't run the probe" in p.error
    assert not interpreter.probe(str(tmp_path / "absent")).ok


# ── the child environment ─────────────────────────────────────────────────────
def test_child_env_carries_no_credentials_or_python_overrides(monkeypatch):
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "HTTPS_PROXY": "http://proxy:3128",
        "PLAYWRIGHT_BROWSERS_PATH": "/pw",
        "A2A_AUTH_TOKEN": "op",
        "PROTOAGENT_FLEET_TOKEN": "fleet",
        "OPENAI_API_KEY": "sk-x",
        "CAMPAIGN_APP_TOKEN": "app",
        "GITHUB_TOKEN": "ghp",
        "PYTHONPATH": "/frozen/_MEI",
        "PYTHONHOME": "/frozen",
        "LD_LIBRARY_PATH": "/frozen/_MEI",
        "LD_LIBRARY_PATH_ORIG": "/usr/local/lib",
    }
    monkeypatch.setattr(interpreter, "frozen", lambda: True)
    env = interpreter.child_env(base)
    for k in ("PATH", "HOME", "LANG", "LC_ALL", "HTTPS_PROXY", "PLAYWRIGHT_BROWSERS_PATH"):
        assert env[k] == base[k]
    for k in ("A2A_AUTH_TOKEN", "PROTOAGENT_FLEET_TOKEN", "OPENAI_API_KEY", "CAMPAIGN_APP_TOKEN", "GITHUB_TOKEN"):
        assert k not in env, f"{k} must not reach the worker's environment (the bearer goes on stdin)"
    assert "PYTHONPATH" not in env and "PYTHONHOME" not in env
    assert env["LD_LIBRARY_PATH"] == "/usr/local/lib", "the frozen app's loader path is swapped for the original"


# ── spawning: stdin, bounded output, the hard kill ────────────────────────────
def test_stdin_carries_the_payload_and_argv_does_not(tmp_path):
    out = tmp_path / "seen.txt"
    script = tmp_path / "echo.py"
    script.write_text(
        f"import sys, pathlib\npathlib.Path({str(out)!r}).write_text(sys.stdin.read() + '|' + ' '.join(sys.argv))\n"
    )
    r = interpreter.run_python(sys.executable, [str(script)], stdin=b'{"bearer": "s3cret"}', timeout=30)
    assert r.returncode == 0 and not r.timed_out
    payload, argv = out.read_text().split("|")
    assert "s3cret" in payload and "s3cret" not in argv


def test_output_is_bounded(tmp_path):
    script = tmp_path / "loud.py"
    script.write_text("import sys\nsys.stdout.write('x' * 2_000_000)\nsys.stderr.write('e' * 500_000 + 'END')\n")
    r = interpreter.run_python(sys.executable, [str(script)], timeout=30)
    assert len(r.stdout) <= interpreter.OUTPUT_TAIL_BYTES + 1 and len(r.stderr) <= interpreter.OUTPUT_TAIL_BYTES + 1
    assert r.stderr.endswith("END"), "the tail is what's kept"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A killed child of a dead parent can linger as a zombie until init reaps it.
    try:
        import subprocess

        stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(stat) and not stat.startswith("Z")
    except Exception:  # noqa: BLE001
        return True


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_an_overrunning_worker_is_killed_with_everything_it_started(tmp_path):
    # The worker sleeps; it has a child in its group (the Node driver's place) and a child in a
    # NEW session (where Playwright puts a detached Chromium). All three must die.
    pids = tmp_path / "pids.txt"
    script = tmp_path / "hang.py"
    script.write_text(
        "import os, subprocess, sys, time\n"
        "a = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "b = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], start_new_session=True)\n"
        f"open({str(pids)!r}, 'w').write(f'{{os.getpid()}} {{a.pid}} {{b.pid}}')\n"
        "time.sleep(120)\n"
    )
    t = time.monotonic()
    r = interpreter.run_python(sys.executable, [str(script)], timeout=3)
    assert r.timed_out and time.monotonic() - t < 30
    worker, driver, browser = (int(x) for x in pids.read_text().split())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(_alive(p) for p in (worker, driver, browser)):
        time.sleep(0.2)
    assert not _alive(worker) and not _alive(driver), "the worker's process group is killed"
    assert not _alive(browser), "a detached (own-session) descendant — Chromium's case — is killed too"


def test_worker_path_is_shipped_beside_the_plugin():
    assert interpreter.WORKER.is_file() and interpreter.WORKER.parent == Path(interpreter.__file__).parent / "worker"


def test_a_failed_host_probe_is_explained(probes, monkeypatch):
    probes[sys.executable] = Probe(sys.executable, ok=False, error="boom: broken venv")
    monkeypatch.setattr(interpreter, "host_has_playwright", lambda: True)
    monkeypatch.setattr(interpreter, "frozen", lambda: False)
    r = interpreter.resolve()
    assert r.need == "deps" and "boom: broken venv" in " ".join(r.considered) and r.detail == "boom: broken venv"


def test_concurrent_probes_of_one_interpreter_spawn_once(monkeypatch):
    import threading

    calls = []

    def slow(py):
        calls.append(py)
        time.sleep(0.3)
        return Probe(py, ok=True, playwright="1")

    monkeypatch.setattr(interpreter, "_probe_uncached", slow)
    interpreter.invalidate()
    ts = [threading.Thread(target=interpreter.probe, args=("/same/py",)) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert calls == ["/same/py"]
