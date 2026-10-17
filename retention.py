"""Retention — what the gallery keeps, what it lets go, and when (Josh's policy, 2026-10-16).

======================  ==========================================================  ========
Rule                    What goes                                                   After
======================  ==========================================================  ========
approved                NEVER pruned — it is archived instead (``archive``)          —
ready_for_review        NEVER pruned — flagged ``stale`` in the report and gallery    14 days
superseded              the FILE; the row stays as a tombstone so the "superseded     7 days
                        by" chain still reads
rendered / captured     the file, and the row unless another row still names it      7 days
                        (then a tombstone) — unless an approved or ready-for-review
                        asset depends on it
planned (no file)       the row, unless another row names it                          30 days
======================  ==========================================================  ========

**Depends on** is a closure over every row that keeps its file (approved, ready for review,
rejected, scripted, and anything younger than its rule): its ``parent_id`` chain (render → take,
bookended clip → render, poster → render, final → take), a montage's ``meta.inputs`` and the
clip ids in its sequence/timeline, and a lane's hero asset. Anything a keeper reaches is kept,
and so is anything THAT reaches. Files are protected the same way: a file any keeper names —
as its own path, a bookended clip's raw master (``meta.unbookended``, a file with no row of its
own), a take's stills or timing log — is never deleted. When in doubt, keep.

Ages: a superseded row from when it was superseded (its last update or review, whichever is
later — a later edit only delays it); intermediates and planned rows from their last update.

The prune never deletes outside the campaign media root, never the root itself, never a
directory (it only ``rmdir``-s folders it emptied), and never an approved or ready-for-review
row: :func:`store.prune_asset` re-checks each row under the write lock before anything is
unlinked, so a plan can't outrun an approval.

``plan()`` is the dry run (nothing is touched); ``execute()`` is the real run. The background
surface runs it daily in the mode the ``retention_mode`` setting names — ``report`` (the
default: a dry run whose report the gallery and ``campaign_prune`` show) until the operator
flips it to ``prune``, or ``disabled``.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import paths, store

log = logging.getLogger("protoagent.plugins.campaign")

SUPERSEDED_DAYS = 7
INTERMEDIATE_DAYS = 7
PLANNED_DAYS = 30
STALE_REVIEW_DAYS = 14
INTERMEDIATE_STATUSES = ("rendered", "captured")
MODES = ("report", "prune", "disabled")
REPORT_NAME = "retention-report.json"

RULES = {
    "superseded_file": f"superseded ≥ {SUPERSEDED_DAYS} days ago — file deleted, row kept as a tombstone",
    "intermediate": f"rendered/captured, untouched ≥ {INTERMEDIATE_DAYS} days, nothing kept depends on it",
    "planned_empty": f"planned with no file, untouched ≥ {PLANNED_DAYS} days",
}


def mode(cfg: dict[str, Any] | None) -> str:
    """The configured mode — anything unrecognised reads as ``report`` (fail safe: never prune)."""
    raw = (cfg or {}).get("retention_mode")
    if raw is False:  # YAML 1.1 reads a bare `off` / `no` as false — the operator meant "disabled"
        return "disabled"
    m = str(raw or "report").strip().lower()
    return m if m in MODES else "report"


# ── time ──────────────────────────────────────────────────────────────────────
def parse_ts(ts: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(ts or "").strip())
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def age_days(now: datetime, *stamps: str) -> float | None:
    """Days since the LATEST of ``stamps`` (None when none parse — an unknown age never prunes)."""
    ds = [d for d in (parse_ts(s) for s in stamps) if d]
    if not ds:
        return None
    return (now - max(ds)).total_seconds() / 86400


# ── the graph ─────────────────────────────────────────────────────────────────
def _ints(value: Any) -> list[int]:
    out = []
    for v in value if isinstance(value, list) else [value]:
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            out.append(v)
        elif isinstance(v, str) and v.isdigit():
            out.append(int(v))
    return out


def _scan_ids(obj: Any, keys: tuple[str, ...] = ("asset_id", "clip")) -> set[int]:
    """Asset ids anywhere under ``obj`` held by one of ``keys`` (a montage's sequence/timeline)."""
    found: set[int] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys:
                found.update(_ints(v))
            else:
                found |= _scan_ids(v, keys)
    elif isinstance(obj, list):
        for v in obj:
            found |= _scan_ids(v, keys)
    return found


def depends_on(asset: dict[str, Any]) -> set[int]:
    """The assets ``asset`` was made FROM — what must outlive it."""
    meta = asset.get("meta") if isinstance(asset.get("meta"), dict) else {}
    deps = set(_ints(asset.get("parent_id") or 0)) - {0}
    deps.update(_ints(meta.get("inputs") or []))
    for key in ("sequence", "timeline"):
        deps |= _scan_ids(meta.get(key))
    deps.discard(int(asset.get("id") or 0))
    return deps


def named_files(asset: dict[str, Any]) -> set[str]:
    """Every file ``asset`` names — its own, plus the side files its meta points at."""
    out: set[str] = set()
    meta = asset.get("meta") if isinstance(asset.get("meta"), dict) else {}
    for p in (asset.get("path"), meta.get("unbookended"), meta.get("timing")):
        if p and isinstance(p, str):
            out.add(p)
    shots = meta.get("screenshots")
    if isinstance(shots, dict):
        out.update(p for p in shots.values() if isinstance(p, str) and p)
    failed = meta.get("failed")
    if isinstance(failed, dict) and isinstance(failed.get("failure_png"), str) and failed["failure_png"]:
        out.add(failed["failure_png"])
    return out


def _key(p: str) -> str:
    """A path's identity for comparisons — resolved when it exists, else as written."""
    try:
        return str(Path(p).expanduser().resolve())
    except (OSError, RuntimeError):
        return str(p)


def _job_refs() -> set[int]:
    try:
        from . import produce

        jobs = produce.list_jobs(100000)
    except Exception:  # noqa: BLE001 — no jobs dir yet
        return set()
    ids: set[int] = set()
    for j in jobs:
        ids |= _scan_ids(j, ("asset_id",))
        d = (j.get("delivery") or {}) if isinstance(j, dict) else {}
        ids.update(_ints(list((d.get("assets") or {}).values())))
    return ids


def _size(p: str) -> int:
    try:
        f = Path(p)
        return f.stat().st_size if f.is_file() else 0
    except OSError:
        return 0


def _contained_file(p: str) -> str:
    """'' when ``p`` is a regular file strictly inside the media root; else why it isn't touched."""
    root = paths.media_root().resolve()
    try:
        f = Path(p).expanduser()
        if f.is_symlink():
            return "it is a symlink"
        r = f.resolve(strict=True)
    except (OSError, RuntimeError):
        return "missing"
    if r == root or root not in r.parents:
        return "it is outside the campaign media root"
    if not r.is_file():
        return "it is not a regular file"
    return ""


# ── the plan (dry run) ────────────────────────────────────────────────────────
def plan(now: datetime | None = None, campaign_id: int = 0) -> dict[str, Any]:
    """What a prune WOULD do, without touching anything. ``campaign_id`` 0 = every campaign.

    Dependencies are always computed over EVERY campaign — a collection can hold a render whose
    take still lives in the old campaign."""
    now = now or datetime.now(UTC)
    rows = store.list_assets()
    by_id = {a["id"]: a for a in rows}
    hero = {ln["hero_asset_id"] for c in store.list_campaigns(include_archived=True) for ln in store.lanes(c["id"])}

    # 1. Candidates by rule, before any protection.
    cand: dict[int, str] = {}
    for a in rows:
        if store.is_pruned(a) or a["status"] in store.PRUNE_PROTECTED:
            continue
        has_file = bool(a.get("path"))
        if a["status"] == "superseded" and has_file:
            age = age_days(now, a.get("updated"), a.get("reviewed_at"))
            if age is not None and age >= SUPERSEDED_DAYS:
                cand[a["id"]] = "superseded_file"
        elif a["status"] in INTERMEDIATE_STATUSES:
            age = age_days(now, a.get("updated"), a.get("created"))
            if age is not None and age >= INTERMEDIATE_DAYS:
                cand[a["id"]] = "intermediate"
        elif a["status"] == "planned" and not has_file:
            age = age_days(now, a.get("updated"), a.get("created"))
            if age is not None and age >= PLANNED_DAYS:
                cand[a["id"]] = "planned_empty"

    # 2. Protection: everything that keeps its file is a root; what a root depends on is kept,
    #    and so is what THAT depends on (a breadth-first closure).
    kept: dict[int, str] = {}
    roots = [a for a in rows if a["id"] not in cand and not store.is_pruned(a)]
    queue = [(a, a) for a in roots]
    for h in hero:
        if h in cand:
            kept[h] = "it is a lane's hero asset"
            queue.append((by_id[h], by_id[h]))
    while queue:
        a, root = queue.pop()
        for dep in depends_on(a):
            if dep in cand and dep not in kept:
                kept[dep] = f"#{root['id']} ({root['status']}) depends on it"
                queue.append((by_id[dep], root))

    # 3. Files a keeper names are never deleted, whichever row they belong to.
    keepers = [a for a in rows if (a["id"] not in cand or a["id"] in kept) and not store.is_pruned(a)]
    protected_files: dict[str, int] = {}
    for a in keepers:
        for p in named_files(a):
            protected_files.setdefault(_key(p), a["id"])

    actions: list[dict[str, Any]] = []
    for aid in sorted(cand):
        if aid in kept:
            continue
        a = by_id[aid]
        p = str(a.get("path") or "")
        if p:
            owner = protected_files.get(_key(p))
            if owner is not None:
                kept[aid] = f"its file is also named by #{owner}"
                continue
            why = _contained_file(p)
            if why and why != "missing":
                kept[aid] = f"its file is never touched: {why}"
                continue
        actions.append({"asset_id": aid, "rule": cand[aid], "status": a["status"], "path": p,
                        "bytes": _size(p) if p else 0, "file_missing": bool(p) and not Path(p).exists(),
                        "kind": a["kind"], "title": a.get("title") or "", "campaign_id": a["campaign_id"]})  # fmt: skip

    # 4. Row fate: a superseded row always stays (a tombstone). An intermediate or planned row is
    #    deleted unless a SURVIVING row still names it — then it is a tombstone too. Iterate:
    #    a row only goes once everything that names it goes.
    referrers: dict[int, set[int]] = {}
    for a in rows:
        for t in {*depends_on(a), *_ints(a.get("superseded_by") or [])}:
            referrers.setdefault(t, set()).add(a["id"])
    named_elsewhere = hero | _job_refs()
    deletable = {x["asset_id"] for x in actions if x["rule"] != "superseded_file"}
    deletable -= named_elsewhere
    changed = True
    while changed:
        changed = False
        for aid in list(deletable):
            if referrers.get(aid, set()) - deletable - {aid}:
                deletable.discard(aid)
                changed = True
    for x in actions:
        x["row"] = "delete" if x["asset_id"] in deletable else "tombstone"

    stale = []
    for a in rows:
        if a["status"] == "ready_for_review":
            age = age_days(now, a.get("updated"), a.get("created"))
            if age is not None and age >= STALE_REVIEW_DAYS:
                stale.append({"asset_id": a["id"], "title": a.get("title") or "", "days": round(age, 1),
                              "campaign_id": a["campaign_id"]})  # fmt: skip

    if campaign_id:
        actions = [x for x in actions if x["campaign_id"] == int(campaign_id)]
        stale = [s for s in stale if s["campaign_id"] == int(campaign_id)]
        kept = {k: v for k, v in kept.items() if by_id[k]["campaign_id"] == int(campaign_id)}

    by_rule = {r: {"count": 0, "bytes": 0, "rows_deleted": 0, "tombstones": 0} for r in RULES}
    for x in actions:
        r = by_rule[x["rule"]]
        r["count"] += 1
        r["bytes"] += x["bytes"]
        r["rows_deleted" if x["row"] == "delete" else "tombstones"] += 1
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "campaign_id": int(campaign_id or 0),
        "rules": RULES,
        "by_rule": by_rule,
        "total": {"count": len(actions), "bytes": sum(x["bytes"] for x in actions)},
        "actions": actions,
        "kept": [{"asset_id": k, "status": by_id[k]["status"], "why": v} for k, v in sorted(kept.items())],
        "stale_review": stale,
    }


# ── the real run ──────────────────────────────────────────────────────────────
def _tidy_dirs(f: Path) -> None:
    """Remove the folders a deletion emptied — up to, never including, a campaign's own folder."""
    root = paths.media_root().resolve()
    d = f.parent
    while d != root and root in d.parents and d.parent != root:
        try:
            d.rmdir()  # only ever succeeds on an empty folder
        except OSError:
            return
        d = d.parent


def execute(now: datetime | None = None, campaign_id: int = 0) -> dict[str, Any]:
    """Prune for real: plan, then act on each row through :func:`store.prune_asset` (which
    re-checks it under the write lock) and only THEN unlink its file. Returns the plan with
    ``done`` / ``errors`` filled in."""
    now = now or datetime.now(UTC)
    report = plan(now, campaign_id)
    done, errors, freed = [], [], 0
    for x in report["actions"]:  # ascending id: a take before its stills (their files are in its meta)
        record = {"at": now.isoformat(timespec="seconds"), "rule": x["rule"], "path": x["path"], "bytes": x["bytes"]}
        try:
            gone = store.prune_asset(
                x["asset_id"], expect_status=x["status"], expect_path=x["path"], mode=x["row"], record=record
            )
        except ValueError as e:
            errors.append({"asset_id": x["asset_id"], "error": str(e)})
            continue
        if gone:
            why = _contained_file(gone)
            if why == "missing":
                pass
            elif why:  # re-checked right before the unlink: never outside the media root
                errors.append({"asset_id": x["asset_id"], "error": f"file left in place: {why}"})
            else:
                f = Path(gone).expanduser().resolve()
                try:
                    f.unlink()
                    freed += x["bytes"]
                    _tidy_dirs(f)
                except OSError as e:
                    errors.append({"asset_id": x["asset_id"], "error": f"row pruned, file not deleted: {e}"})
        done.append(x["asset_id"])
    report.update(dry_run=False, done=done, errors=errors, freed_bytes=freed)
    return report


# ── the saved report (what the daily run found last) ──────────────────────────
def save_report(report: dict[str, Any], run_mode: str) -> dict[str, Any]:
    out = {**report, "mode": run_mode, "dry_run": report.get("dry_run", True)}
    p = paths.data_dir() / REPORT_NAME
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=1, default=str))
    tmp.replace(p)
    return out


def last_report() -> dict[str, Any] | None:
    try:
        return json.loads((paths.data_dir() / REPORT_NAME).read_text())
    except (OSError, ValueError):
        return None


def run_scheduled(cfg: dict[str, Any] | None, now: datetime | None = None) -> dict[str, Any] | None:
    """One scheduled pass in the configured mode: ``report`` → dry run, ``prune`` → real run,
    ``disabled`` → nothing. The report is saved either way."""
    m = mode(cfg)
    if m == "disabled":
        return None
    report = execute(now) if m == "prune" else {**plan(now), "dry_run": True}
    saved = save_report(report, m)
    t = saved["total"]
    log.info(
        "[campaign] retention %s: %d item(s), %s%s",
        "pruned" if m == "prune" else "dry run (report only)",
        t["count"],
        _mb(t["bytes"]),
        f", {len(saved.get('errors') or [])} error(s)" if m == "prune" else "",
    )
    return saved


def _mb(n: int) -> str:
    return f"{n / 1e6:.1f} MB"


def summary(report: dict[str, Any], limit: int = 25) -> str:
    """The report as text — what the tool returns."""
    t = report["total"]
    head = "Pruned" if report.get("dry_run") is False else "Would prune (dry run — nothing was touched)"
    out = [f"{head}: {t['count']} item(s), {_mb(t['bytes'])}."]
    for rule, r in report["by_rule"].items():
        if r["count"]:
            out.append(
                f"- {rule}: {r['count']} ({_mb(r['bytes'])}) — {r['rows_deleted']} row(s) deleted, "
                f"{r['tombstones']} kept as tombstones · {RULES[rule]}"
            )
    kept = report.get("kept") or []
    if kept:
        out.append(f"Kept although old: {len(kept)} (something kept depends on them, or their file is shared).")
    stale = report.get("stale_review") or []
    if stale:
        out.append(
            f"Stale in review (≥ {STALE_REVIEW_DAYS} days, never auto-deleted): "
            + ", ".join(f"#{s['asset_id']} ({s['days']:.0f}d)" for s in stale[:limit])
        )
    if report.get("dry_run") is False:
        errs = report.get("errors") or []
        out.append(f"Freed {_mb(report.get('freed_bytes', 0))}; {len(errs)} error(s).")
        out += [f"  - #{e['asset_id']}: {e['error']}" for e in errs[:limit]]
    acts = report["actions"]
    if acts:
        out.append("Largest:")
        for x in sorted(acts, key=lambda x: -x["bytes"])[:limit]:
            out.append(
                f"  - #{x['asset_id']} [{x['status']}] {x['kind']} {_mb(x['bytes'])} — {x['rule']}, row: {x['row']}"
            )
    return "\n".join(out)
