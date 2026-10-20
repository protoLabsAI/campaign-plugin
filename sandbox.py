"""``campaign_sandbox_reset`` — clear a SANDBOX console between takes.

A take of a protoAgent console starts from whatever the last take left there: old chat tabs,
artifacts in the panel, a running goal. brandLaunch hand-wrote a ``browser_eval`` snippet that
deleted them (36 times in one night, #48). This is that snippet as a tool, with the guard the
snippet never had: it only ever touches a console the campaign already RECORDS (a saved shot
script's ``base_url``), and refuses outright when that console is:

* not on this machine (loopback only — sandboxes are local instances);
* the operator's default or dev instance (:7870 / :7871, ADR 0004) — the live consoles whose
  chats are real work (a plugin that guessed :7870 once drove the live instance);
* THIS agent's own console — clearing it would delete the very chat asking for the reset; and
  when the agent can't tell which console is its own, it refuses rather than guess.

The requests run in the browser worker (the plugin's own Python opens no connection), only to
that origin, with the script's own credentials (``auth.bearer_env`` / a ``storage_state``
token) — never the agent's.
"""

from __future__ import annotations

from typing import Any

from . import produce, shoot, shotscript, store
from .worker import pw_worker

PROTECTED_PORTS = {7870: "the operator's default (live) instance", 7871: "the operator's dev instance"}
CLEARABLE = tuple(pw_worker.RESET_RECIPES)
RESET_TIMEOUT_S = 90


class SandboxError(ValueError):
    """The reset was refused before anything was sent."""


def parse_clear(raw: Any) -> list[str]:
    if raw in (None, "", []):
        return list(CLEARABLE)
    items = raw if isinstance(raw, list) else str(raw).replace(";", ",").split(",")
    names = [str(x).strip().lower() for x in items if str(x).strip()]
    bad = [n for n in names if n not in CLEARABLE]
    if bad:
        raise SandboxError(f"clear: unknown {', '.join(map(repr, bad))} — choose from {', '.join(CLEARABLE)}")
    return list(dict.fromkeys(names))


def target(campaign_id: int, script_id: int = 0, base_url: str = "") -> tuple[dict[str, Any], str]:
    """The saved script whose sandbox to reset, and that sandbox's origin.

    The origin must be one the campaign already records: ``script_id``'s ``base_url``, or a
    ``base_url`` matching one of the campaign's saved scripts, or — with neither — the one
    sandbox all its scripts share."""
    store.require_campaign(campaign_id)
    rows = store.scripts(campaign_id)
    if script_id:
        row = next((r for r in rows if r["id"] == int(script_id)), None)
        if row is None:
            raise SandboxError(f"No shot script #{script_id} in campaign {campaign_id}.")
        candidates = [row]
    else:
        candidates = [r for r in rows if str(r["body"].get("base_url") or "").strip()]
        if base_url:
            candidates = [r for r in candidates if produce.same_console(r["body"]["base_url"], base_url)]
            if not candidates:
                known = sorted(
                    {shotscript.browser_origin(r["body"]["base_url"]) for r in rows if r["body"].get("base_url")}
                )
                raise SandboxError(
                    f"{base_url} isn't a console this campaign records — only a saved script's base_url can be "
                    f"reset ({', '.join(known) or 'no script has a base_url yet'})."
                )
    origins = sorted({shotscript.browser_origin(str(r["body"].get("base_url") or "")) for r in candidates} - {""})
    if not origins:
        raise SandboxError(
            "No sandbox to reset — the script has no base_url." if script_id
            else "No saved shot script in this campaign has a base_url — save one first."
        )  # fmt: skip
    if len(origins) > 1:
        raise SandboxError(f"This campaign records several consoles ({', '.join(origins)}) — pass script_id.")
    row = max((r for r in candidates if shotscript.browser_origin(str(r["body"].get("base_url") or "")) == origins[0]),
              key=lambda r: r["id"])  # fmt: skip
    return row, origins[0]


def guard(origin: str, own: list[str]) -> None:
    """Refuse anything that isn't plainly a local sandbox. Raises :class:`SandboxError`."""
    key = produce._origin_key(origin)
    if key is None:
        raise SandboxError(f"{origin!r} isn't an http(s) console URL.")
    _scheme, host, port = key
    if host != "loopback":
        raise SandboxError(f"{origin} isn't on this machine — a sandbox reset only clears a local (loopback) console.")
    if port in PROTECTED_PORTS:
        raise SandboxError(
            f"Refused: {origin} is :{port}, {PROTECTED_PORTS[port]}. A reset only clears a recording sandbox "
            "(a separate instance on its own port) — never the operator's console."
        )
    if not own:
        raise SandboxError(
            "Refused: this agent can't tell which console it serves on, so it can't rule out clearing its own "
            "chats. Nothing was sent."
        )
    if any(produce.same_console(origin, m) for m in own):
        raise SandboxError(
            f"Refused: {origin} is THIS agent's own console — clearing it would delete this conversation."
        )


def reset(
    campaign_id: int,
    *,
    script_id: int = 0,
    base_url: str = "",
    clear: Any = "",
    theme: str = "",
    own: list[str] | None = None,
    playwright_factory: Any = None,
) -> dict[str, Any]:
    """Resolve, guard, and run the reset in the worker. Returns the worker's result plus
    ``script_id`` / ``origin``. Raises :class:`SandboxError` on a refusal, ``shoot.WorkerError``
    when the worker can't run."""
    names = parse_clear(clear)
    row, origin = target(campaign_id, script_id, base_url)
    guard(origin, produce.own_console_urls() if own is None else own)
    try:
        norm = shotscript.validate(row["body"])
    except shotscript.ScriptError as e:
        raise SandboxError(f"script #{row['id']} doesn't validate: {'; '.join(e.problems[:3])}") from None
    bearer = ""
    if (norm.get("auth") or {}).get("bearer_env"):
        try:
            bearer = shoot.read_bearer(norm)
        except shoot.ShootError as e:
            raise SandboxError(f"script #{row['id']}'s bearer: {e}") from None
    bearer = bearer or produce._stored_token(norm, origin)
    theme_body = None
    if str(theme or "").strip():
        mode = norm.get("color_scheme") if norm.get("color_scheme") in ("dark", "light") else "dark"
        theme_body = {"mode": mode, "overrides": {}, "preset": str(theme).strip(), "edits": []}
    job = {"v": pw_worker.JOB_VERSION, "kind": "reset", "base_url": origin, "bearer": bearer, "clear": names,
           "theme": theme_body, "fence": shoot.fence()}  # fmt: skip
    report = shoot.run_worker(job, RESET_TIMEOUT_S, playwright_factory)
    result = dict(report.get("result") or {})
    result.update(script_id=row["id"], origin=origin, ok=bool(report.get("ok")), error=report.get("error") or "")
    return result


def describe(result: dict[str, Any]) -> str:
    """The tool's reply — one line per thing cleared."""
    head = f"Sandbox {result['origin']} (script #{result['script_id']})"
    if not result.get("ok"):
        return f"{head}: NOT reset — {result.get('error') or 'unknown error'}"
    parts = []
    for name, n in (result.get("cleared") or {}).items():
        left = (result.get("left") or {}).get(name)
        parts.append(f"{name} {n} cleared" + (f", {left} LEFT" if left else ""))
    if "theme" in result:
        parts.append(f"theme set (HTTP {result['theme']})")
    errs = result.get("errors") or []
    return (
        f"{head} reset: " + ("; ".join(parts) or "nothing to clear")
        + "".join(f"\n- {e}" for e in errs[:5])
        + (f"\n- … {len(errs) - 5} more" if len(errs) > 5 else "")
    )  # fmt: skip
