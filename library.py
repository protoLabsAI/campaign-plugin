"""Collections — gather the good takes into one campaign, start fresh, archive the old one.

A *collection* is just a campaign (``store.campaigns``). This module adds the three verbs a
curated library needs:

* :func:`move_assets` — re-home assets (and, by default, their **lineage**: every ancestor the
  retention closure would protect — the parent chain, a montage's inputs) into another
  campaign. Rows keep their ids, files, statuses and approvals, so every chain that names them
  still reads; the files stay where they are on disk (still inside the media root). Lanes are
  matched by name in the target, and created there when missing.
* :func:`copy_assets` — duplicate assets + lineage into another campaign: the files are COPIED
  into the target's folder (same layout under it), side files too (a bookended clip's raw
  master, a take's timing log), and every id and path inside the copies is re-pointed at the
  copies. Copying twice is idempotent — an asset already copied into the target maps to its
  existing copy.
* archiving — ``store.set_archived`` (hidden from ``campaign_list`` and the gallery picker by
  default; restorable to the status it had).

**Approval stays the operator's.** The agent may move or copy anything that isn't approved. An
APPROVED asset moves only through the operator's route; the agent's COPY of one lands as
``ready_for_review`` (the operator re-approves it in the collection), and the operator's copy
stays approved. ``plan()`` is the dry run — what would move/copy, with sizes.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import paths, retention, store

MODES = ("move", "copy")


def _closure(ids: list[int], lineage: bool) -> list[dict[str, Any]]:
    """The selected assets plus (with ``lineage``) everything they were made from — ascending id,
    which puts every parent before its children (a parent always exists first)."""
    wanted = {int(i) for i in ids}
    seen: dict[int, dict[str, Any]] = {}
    stack = sorted(wanted)
    while stack:
        aid = stack.pop()
        if aid in seen:
            continue
        a = store.get_asset(aid)
        if a is None:
            if aid in wanted:
                raise ValueError(f"no asset with id {aid}")
            continue  # an ancestor that is already gone — nothing to bring
        seen[aid] = a
        if lineage:
            stack.extend(retention.depends_on(a))
    return [seen[k] for k in sorted(seen)]


def plan(asset_ids: list[int], target_campaign_id: int, *, mode: str = "move", lineage: bool = True,
         operator: bool = False) -> dict[str, Any]:  # fmt: skip
    """What a move/copy would do (touches nothing). Raises ValueError for a request that can't run."""
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        raise ValueError(f"mode must be move or copy — got {mode!r}")
    ids = sorted({int(i) for i in asset_ids or []})
    if not ids:
        raise ValueError("name the asset ids to collect")
    target = store.require_campaign(target_campaign_id)
    rows = _closure(ids, lineage)
    if mode == "move":
        rows = [a for a in rows if a["campaign_id"] != target["id"]]
        approved = [a["id"] for a in rows if a["status"] == "approved"]
        if approved and not operator:
            raise ValueError(
                "approved asset(s) " + ", ".join(f"#{i}" for i in approved) + " move only by the operator "
                "(POST /api/plugins/campaign/campaigns/<target>/collect) — their approval is theirs. "
                "Move the rest, or copy (an approved asset's copy lands ready_for_review)"
            )
    items = []
    for a in rows:
        status = a["status"]
        if mode == "copy" and not operator and status in ("approved", "rejected"):
            # the agent's copy of an approved asset goes back to the operator's queue; of a REJECTED
            # one, back to production — never laundered into the review queue
            status = "ready_for_review" if status == "approved" else "rendered"
        items.append({"asset_id": a["id"], "from_campaign": a["campaign_id"], "status": a["status"],
                      "lands_as": status, "kind": a["kind"], "title": a.get("title") or "",
                      "selected": a["id"] in ids, "bytes": retention._size(a.get("path") or "")})  # fmt: skip
    return {
        "mode": mode,
        "target": {"id": target["id"], "name": target["name"]},
        "lineage": bool(lineage),
        "items": items,
        "bytes": sum(x["bytes"] for x in items),
    }


def _lane_map(rows: list[dict[str, Any]], target_id: int) -> dict[tuple[int, int], int]:
    """(source campaign, source lane) → the target lane of the same name (created when missing)."""
    out: dict[tuple[int, int], int] = {}
    for a in rows:
        key = (a["campaign_id"], int(a.get("lane_id") or 0))
        if key in out:
            continue
        if not key[1] or key[0] == target_id:
            out[key] = key[1] if key[0] == target_id else 0
            continue
        src = store.lane_by_id(key[0], key[1])
        if src is None:
            out[key] = 0
            continue
        have = store.lane_by_name(target_id, src["name"])
        out[key] = (have or store.upsert_lane(target_id, src["name"], pitch=src["pitch"], audience=src["audience"]))[
            "id"
        ]
    return out


def move_assets(asset_ids: list[int], target_campaign_id: int, *, lineage: bool = True,
                operator: bool = False) -> dict[str, Any]:  # fmt: skip
    p = plan(asset_ids, target_campaign_id, mode="move", lineage=lineage, operator=operator)
    rows = [store.get_asset(x["asset_id"]) for x in p["items"]]
    rows = [a for a in rows if a is not None]
    lanes = _lane_map(rows, p["target"]["id"])
    moves = {a["id"]: lanes.get((a["campaign_id"], int(a.get("lane_id") or 0)), 0) for a in rows}
    store.move_rows(moves, p["target"]["id"], allow_approved=operator)
    _queue_catalog(sorted(moves))
    return {**p, "done": sorted(moves)}


def _queue_catalog(ids: list[int]) -> None:
    """An approved asset that arrives in a campaign belongs in THAT campaign's catalog too. The
    archive sees the same sha256 and only adds the catalog entry — the bytes never re-upload."""
    for aid in ids:
        a = store.get_asset(aid)
        if a and a["status"] == "approved":
            store.enqueue_archive(a["id"], a["campaign_id"])


# ── copy ──────────────────────────────────────────────────────────────────────
def _campaign_root(p: Path) -> Path | None:
    """The ``<media_root>/<id>-<slug>`` folder ``p`` lives in (None when outside the root)."""
    root = paths.media_root().resolve()
    try:
        rel = p.resolve().relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return root / rel.parts[0] if len(rel.parts) > 1 else None


def _dest(src: str, target_dir: Path, taken: set[Path]) -> Path | None:
    """Where ``src`` lands in the target: the same path under the target's folder as under its
    own campaign folder. None for a file that isn't a regular file inside the media root."""
    if not src or not paths.is_contained(src):
        return None
    f = Path(src).expanduser()
    if f.is_symlink() or not f.is_file():
        return None
    home = _campaign_root(f)
    if home is None:
        return None
    rel = f.resolve().relative_to(home)
    d = target_dir / rel
    n = 2
    while d in taken or d.exists():
        d = d.with_name(f"{f.stem}-copy{n if n > 2 else ''}{f.suffix}")
        n += 1
    taken.add(d)
    return d


def _remap(obj: Any, pmap: dict[str, str], imap: dict[int, int], key: str = "") -> Any:
    """Re-point paths (``pmap``) and asset ids under ``asset_id`` / ``clip`` / ``inputs`` keys."""
    if isinstance(obj, dict):
        return {k: _remap(v, pmap, imap, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_remap(v, pmap, imap, key) for v in obj]
    if isinstance(obj, str):
        return pmap.get(obj, obj)
    if key in ("asset_id", "clip", "inputs") and isinstance(obj, int) and not isinstance(obj, bool):
        return imap.get(obj, obj)
    return obj


def copy_assets(asset_ids: list[int], target_campaign_id: int, *, lineage: bool = True,
                operator: bool = False) -> dict[str, Any]:  # fmt: skip
    p = plan(asset_ids, target_campaign_id, mode="copy", lineage=lineage, operator=operator)
    target = store.require_campaign(p["target"]["id"])
    target_dir = paths.campaign_dir(target["id"], target["name"])
    rows = [store.get_asset(x["asset_id"]) for x in p["items"]]
    rows = [a for a in rows if a is not None]

    # An asset already copied into the target maps to its copy (idempotent re-runs).
    existing = {}
    for a in store.list_assets(target["id"]):
        src = ((a.get("meta") or {}).get("copied_from") or {}).get("asset_id")
        if isinstance(src, int):
            existing[src] = a["id"]
    imap: dict[int, int] = {k: v for k, v in existing.items()}
    todo = [a for a in rows if a["id"] not in existing]

    # 1. Where every file lands — the row's own and the side files its meta names.
    pmap: dict[str, str] = {}
    taken: set[Path] = set()
    for a in todo:
        for src in sorted(retention.named_files(a)):
            if src in pmap:
                continue
            d = _dest(src, target_dir, taken)
            if d is not None:
                pmap[src] = str(d)
        td = (a.get("meta") or {}).get("take_dir")
        if isinstance(td, str) and a.get("path") in pmap and Path(a["path"]).parent == Path(td):
            pmap[td] = str(Path(pmap[a["path"]]).parent)

    # 2. Copy the files (a row whose file can't be copied lands with no file — and says so).
    copied, skipped = [], []
    try:
        for src, dst in pmap.items():
            s = Path(src)
            if not s.is_file():
                continue  # a take_dir mapping, not a file
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, dst)
            copied.append(dst)
    except OSError:
        # all or nothing: no orphaned half-copy (no row was written yet), then re-raise
        for dst in copied:
            Path(dst).unlink(missing_ok=True)
        raise

    # 3. Insert the copies, parents first, ids and paths re-pointed.
    lanes = _lane_map(todo, target["id"])
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    made = []
    for a in todo:
        item = next(x for x in p["items"] if x["asset_id"] == a["id"])
        status = item["lands_as"]
        meta = _remap(a.get("meta") or {}, pmap, imap)
        meta["copied_from"] = {"asset_id": a["id"], "campaign_id": a["campaign_id"], "at": stamp}
        new_path = pmap.get(a.get("path") or "", "")
        if a.get("path") and not new_path:
            skipped.append(a["id"])
        if status in ("ready_for_review", "approved") and store.review_gate({**a, "path": new_path}):
            # a copy that couldn't bring its file (or breaks its limit) can't wait for review
            status = "rendered" if new_path else "planned"
        item["lands_as"] = status
        demoted = status != a["status"]
        row = {
            **a,
            "status": status,
            "path": new_path,
            "meta": meta,
            "parent_id": imap.get(int(a.get("parent_id") or 0), int(a.get("parent_id") or 0)),
            "superseded_by": [],
            "review_note": "" if demoted else a.get("review_note"),
            "reviewed_at": "" if demoted else a.get("reviewed_at"),
            "notes": (a.get("notes") or "")
            + (f"\n[copied from #{a['id']}, {a['status']} there — landed {status} here]" if demoted else ""),
        }
        new = store.clone_asset(row, target["id"], lanes.get((a["campaign_id"], int(a.get("lane_id") or 0)), 0),
                                allow_approved=operator)  # fmt: skip
        imap[a["id"]] = new["id"]
        made.append(new["id"])

    # 4. Chains that point FORWARD (superseded_by names a newer take) — now every copy has an id.
    for a in todo:
        if a.get("superseded_by"):
            # only replacements that came along: a chain never points into another campaign
            store.set_meta_paths(imap[a["id"]], superseded_by=[imap[i] for i in a["superseded_by"] if i in imap])

    _queue_catalog(made)
    return {**p, "done": made, "map": {str(k): v for k, v in imap.items() if k in {x["asset_id"] for x in p["items"]}},
            "files_copied": len(copied), "no_file": skipped, "already_copied": sorted(existing.keys() & {a["id"] for a in rows})}  # fmt: skip


def run(asset_ids: list[int], target_campaign_id: int, *, mode: str = "move", lineage: bool = True,
        operator: bool = False) -> dict[str, Any]:  # fmt: skip
    m = (mode or "").strip().lower()
    if m not in MODES:
        raise ValueError(f"mode must be move or copy — got {mode!r}")
    fn = move_assets if m == "move" else copy_assets
    return fn(asset_ids, target_campaign_id, lineage=lineage, operator=operator)


def summary(result: dict[str, Any]) -> str:
    done = result.get("done")
    verb = {"move": "Moved", "copy": "Copied"}[result["mode"]] if done is not None else f"Would {result['mode']}"
    t = result["target"]
    items = result["items"]
    sel = sum(1 for x in items if x["selected"])
    out = [
        f"{verb} {len(items)} asset(s) into campaign #{t['id']} {t['name']!r} — {sel} selected"
        + (f" + {len(items) - sel} ancestor(s) (lineage)" if len(items) > sel else "")
        + f", {result['bytes'] / 1e6:.1f} MB."
    ]
    for x in items:
        land = f" → lands {x['lands_as']}" if x["lands_as"] != x["status"] else ""
        out.append(f"- #{x['asset_id']} [{x['status']}]{land} {x['kind']} — {x['title'] or '(untitled)'}"
                   + ("" if x["selected"] else "  (ancestor)"))  # fmt: skip
    if result.get("map"):
        out.append("New ids: " + ", ".join(f"#{k}→#{v}" for k, v in result["map"].items()))
    if result.get("no_file"):
        out.append("Copied WITHOUT a file (outside the media root or missing): "
                   + ", ".join(f"#{i}" for i in result["no_file"]))  # fmt: skip
    if result.get("already_copied"):
        out.append("Already in the target (mapped to the existing copy): "
                   + ", ".join(f"#{i}" for i in result["already_copied"]))  # fmt: skip
    return "\n".join(out)
