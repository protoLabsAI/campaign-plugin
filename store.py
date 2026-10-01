"""The campaign plan — campaigns, lanes, assets, milestones, decisions, shot scripts.

One SQLite file under the plugin data dir (per instance). Host-free: stdlib + ``limits``.

Asset statuses move forward through production::

    planned → scripted → captured → rendered → ready_for_review → approved | rejected

The agent may move an asset anywhere up to ``ready_for_review`` (and back, to rework a
rejected one). ``approved`` / ``rejected`` are set ONLY through :func:`review`, which only
the operator-gated gallery route calls — there is no agent tool that approves.

SQLite discipline (the shop's plugin-storage rules): a connection per call, never shared
across threads; ``busy_timeout`` BEFORE ``journal_mode=WAL``; a process lock around
writes; additive ``ALTER TABLE`` migrations on connect.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import limits
from .paths import data_dir

DB_NAME = "campaign.db"

CAMPAIGN_STATUSES = ("draft", "active", "launched", "done", "paused", "archived")
ASSET_KINDS = ("clip", "gif", "still", "card", "copy_ref")
ASSET_STATUSES = ("planned", "scripted", "captured", "rendered", "ready_for_review", "approved", "rejected")
AGENT_ASSET_STATUSES = ASSET_STATUSES[:5]
OWNERS = ("agent", "operator")
MILESTONE_STATUSES = ("todo", "doing", "done", "dropped")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created       TEXT NOT NULL,
    updated       TEXT NOT NULL,
    name          TEXT NOT NULL,
    product       TEXT NOT NULL DEFAULT '',
    goal          TEXT NOT NULL DEFAULT '',
    target_metric TEXT NOT NULL DEFAULT '',
    launch_window TEXT NOT NULL DEFAULT '',
    target_url    TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'draft',
    goal_math     TEXT NOT NULL DEFAULT '',
    assumptions   TEXT NOT NULL DEFAULT '',
    channel_plan  TEXT NOT NULL DEFAULT '',
    do_not        TEXT NOT NULL DEFAULT '',
    notes         TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS lanes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id   INTEGER NOT NULL,
    created       TEXT NOT NULL,
    updated       TEXT NOT NULL,
    name          TEXT NOT NULL,
    pitch         TEXT NOT NULL DEFAULT '',
    audience      TEXT NOT NULL DEFAULT '',
    hero_asset_id INTEGER NOT NULL DEFAULT 0,
    UNIQUE(campaign_id, name)
);
CREATE TABLE IF NOT EXISTS assets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id   INTEGER NOT NULL,
    lane_id       INTEGER NOT NULL DEFAULT 0,
    created       TEXT NOT NULL,
    updated       TEXT NOT NULL,
    kind          TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    spec          TEXT NOT NULL DEFAULT '',
    owner         TEXT NOT NULL DEFAULT 'agent',
    status        TEXT NOT NULL DEFAULT 'planned',
    path          TEXT NOT NULL DEFAULT '',
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    width         INTEGER NOT NULL DEFAULT 0,
    height        INTEGER NOT NULL DEFAULT 0,
    duration_s    REAL NOT NULL DEFAULT 0,
    limit_id      TEXT NOT NULL DEFAULT '',
    parent_id     INTEGER NOT NULL DEFAULT 0,
    script_id     INTEGER NOT NULL DEFAULT 0,
    meta          TEXT NOT NULL DEFAULT '{}',
    notes         TEXT NOT NULL DEFAULT '',
    review_note   TEXT NOT NULL DEFAULT '',
    reviewed_at   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_assets_campaign ON assets(campaign_id);
CREATE TABLE IF NOT EXISTS milestones (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id   INTEGER NOT NULL,
    created       TEXT NOT NULL,
    updated       TEXT NOT NULL,
    date          TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL,
    workstream    TEXT NOT NULL DEFAULT '',
    owner         TEXT NOT NULL DEFAULT 'agent',
    status        TEXT NOT NULL DEFAULT 'todo',
    notes         TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS decisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id    INTEGER NOT NULL,
    created        TEXT NOT NULL,
    updated        TEXT NOT NULL,
    question       TEXT NOT NULL,
    options        TEXT NOT NULL DEFAULT '[]',
    recommendation TEXT NOT NULL DEFAULT '',
    answer         TEXT NOT NULL DEFAULT '',
    answered_at    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS scripts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id   INTEGER NOT NULL,
    created       TEXT NOT NULL,
    updated       TEXT NOT NULL,
    name          TEXT NOT NULL,
    body          TEXT NOT NULL,
    UNIQUE(campaign_id, name)
);
"""

# table → {column: decl} for columns added after v0.1.0 (none yet). Applied on connect so an
# upgrade never strands an operator's existing plan.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {}

_WRITE_LOCK = threading.Lock()


def db_path() -> Path:
    return data_dir() / DB_NAME


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")  # BEFORE the WAL switch — that takes locks too
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA)
    for table, cols in _ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    return conn


@contextmanager
def _write():
    with _WRITE_LOCK:
        conn = connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()


def _read(sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return [_row(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    for key, default in (("options", []), ("meta", {})):
        if key in d:
            try:
                d[key] = json.loads(d[key] or json.dumps(default))
            except json.JSONDecodeError:
                d[key] = default
    if "spec" in d and isinstance(d["spec"], str) and d["spec"].startswith(("{", "[")):
        try:
            d["spec"] = json.loads(d["spec"])
        except json.JSONDecodeError:
            pass
    if "body" in d and isinstance(d.get("body"), str):
        try:
            d["body"] = json.loads(d["body"])
        except json.JSONDecodeError:
            pass
    return d


def _choice(value: str, allowed: tuple[str, ...], what: str) -> str:
    v = (value or "").strip().lower()
    if v not in allowed:
        raise ValueError(f"{what} must be one of {', '.join(allowed)} — got {value!r}")
    return v


def _spec_text(spec: Any) -> str:
    if isinstance(spec, (dict, list)):
        return json.dumps(spec)
    return str(spec or "")


# ── campaigns ────────────────────────────────────────────────────────────────
CAMPAIGN_FIELDS = (
    "name",
    "product",
    "goal",
    "target_metric",
    "launch_window",
    "target_url",
    "status",
    "goal_math",
    "assumptions",
    "channel_plan",
    "do_not",
    "notes",
)


def create_campaign(name: str, **fields: Any) -> dict[str, Any]:
    if not (name or "").strip():
        raise ValueError("a campaign needs a name")
    data = {k: str(fields.get(k) or "") for k in CAMPAIGN_FIELDS if k != "name"}
    data["status"] = _choice(data.get("status") or "draft", CAMPAIGN_STATUSES, "status")
    now = _now()
    cols = ["created", "updated", "name", *data.keys()]
    with _write() as conn:
        cur = conn.execute(
            f"INSERT INTO campaigns ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            (now, now, name.strip(), *data.values()),
        )
        cid = cur.lastrowid
    return get_campaign(cid)  # type: ignore[return-value]


def update_campaign(campaign_id: int, **fields: Any) -> dict[str, Any]:
    if get_campaign(campaign_id) is None:
        raise ValueError(f"no campaign with id {campaign_id}")
    changes = {k: str(v) for k, v in fields.items() if k in CAMPAIGN_FIELDS and v is not None}
    if "status" in changes:
        changes["status"] = _choice(changes["status"], CAMPAIGN_STATUSES, "status")
    if "name" in changes and not changes["name"].strip():
        raise ValueError("a campaign needs a name")
    if changes:
        sets = ", ".join(f"{k} = ?" for k in changes)
        with _write() as conn:
            conn.execute(
                f"UPDATE campaigns SET {sets}, updated = ? WHERE id = ?", (*changes.values(), _now(), int(campaign_id))
            )
    return get_campaign(campaign_id)  # type: ignore[return-value]


def get_campaign(campaign_id: int) -> dict[str, Any] | None:
    rows = _read("SELECT * FROM campaigns WHERE id = ?", (int(campaign_id),))
    return rows[0] if rows else None


def list_campaigns(include_archived: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM campaigns"
    if not include_archived:
        sql += " WHERE status != 'archived'"
    return _read(sql + " ORDER BY id DESC")


def require_campaign(campaign_id: int) -> dict[str, Any]:
    c = get_campaign(campaign_id)
    if c is None:
        raise ValueError(f"no campaign with id {campaign_id} — campaign_list shows the ids")
    return c


# ── lanes ────────────────────────────────────────────────────────────────────
def upsert_lane(
    campaign_id: int,
    name: str,
    pitch: str | None = None,
    audience: str | None = None,
    hero_asset_id: int | None = None,
    lane_id: int = 0,
) -> dict[str, Any]:
    require_campaign(campaign_id)
    now = _now()
    with _write() as conn:
        if lane_id:
            row = conn.execute(
                "SELECT * FROM lanes WHERE id = ? AND campaign_id = ?", (lane_id, campaign_id)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM lanes WHERE campaign_id = ? AND name = ?", (campaign_id, name.strip())
            ).fetchone()
        if row is None:
            if lane_id:
                raise ValueError(f"no lane {lane_id} in campaign {campaign_id}")
            if not (name or "").strip():
                raise ValueError("a lane needs a name")
            cur = conn.execute(
                "INSERT INTO lanes (campaign_id, created, updated, name, pitch, audience, hero_asset_id)"
                " VALUES (?,?,?,?,?,?,?)",
                (campaign_id, now, now, name.strip(), pitch or "", audience or "", int(hero_asset_id or 0)),
            )
            lane_id = cur.lastrowid
        else:
            lane_id = row["id"]
            new_name = (name or row["name"]).strip()
            clash = conn.execute(
                "SELECT id FROM lanes WHERE campaign_id = ? AND name = ? AND id != ?", (campaign_id, new_name, lane_id)
            ).fetchone()
            if clash is not None:
                raise ValueError(f"campaign {campaign_id} already has a lane named {new_name!r} (lane {clash['id']})")
            conn.execute(
                "UPDATE lanes SET name = ?, pitch = ?, audience = ?, hero_asset_id = ?, updated = ? WHERE id = ?",
                (
                    new_name,
                    row["pitch"] if pitch is None else pitch,
                    row["audience"] if audience is None else audience,
                    row["hero_asset_id"] if hero_asset_id is None else int(hero_asset_id),
                    now,
                    lane_id,
                ),
            )
    return _read("SELECT * FROM lanes WHERE id = ?", (lane_id,))[0]


def lanes(campaign_id: int) -> list[dict[str, Any]]:
    return _read("SELECT * FROM lanes WHERE campaign_id = ? ORDER BY id", (int(campaign_id),))


def lane_by_name(campaign_id: int, name: str) -> dict[str, Any] | None:
    rows = _read("SELECT * FROM lanes WHERE campaign_id = ? AND name = ?", (int(campaign_id), (name or "").strip()))
    return rows[0] if rows else None


# ── assets ───────────────────────────────────────────────────────────────────
def add_asset(
    campaign_id: int,
    kind: str,
    title: str = "",
    *,
    lane_id: int = 0,
    spec: Any = "",
    owner: str = "agent",
    status: str = "planned",
    path: str = "",
    size_bytes: int = 0,
    width: int = 0,
    height: int = 0,
    duration_s: float = 0.0,
    limit_id: str = "",
    parent_id: int = 0,
    script_id: int = 0,
    meta: dict | None = None,
    notes: str = "",
) -> dict[str, Any]:
    require_campaign(campaign_id)
    kind = _choice(kind, ASSET_KINDS, "kind")
    owner = _choice(owner or "agent", OWNERS, "owner")
    status = _choice(status or "planned", AGENT_ASSET_STATUSES, "status")
    if limit_id and limits.get(limit_id) is None:
        raise ValueError(f"unknown limit {limit_id!r} — campaign_limits lists them")
    now = _now()
    with _write() as conn:
        cur = conn.execute(
            "INSERT INTO assets (campaign_id, lane_id, created, updated, kind, title, spec, owner, status, path,"
            " size_bytes, width, height, duration_s, limit_id, parent_id, script_id, meta, notes)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                campaign_id,
                int(lane_id or 0),
                now,
                now,
                kind,
                title,
                _spec_text(spec),
                owner,
                status,
                str(path or ""),
                int(size_bytes or 0),
                int(width or 0),
                int(height or 0),
                float(duration_s or 0),
                limit_id or "",
                int(parent_id or 0),
                int(script_id or 0),
                json.dumps(meta or {}),
                notes,
            ),
        )
        aid = cur.lastrowid
    return get_asset(aid)  # type: ignore[return-value]


ASSET_UPDATABLE = (
    "title",
    "lane_id",
    "spec",
    "owner",
    "status",
    "path",
    "size_bytes",
    "width",
    "height",
    "duration_s",
    "limit_id",
    "script_id",
    "meta",
    "notes",
)


# What the agent may still touch on an asset the operator APPROVED: its notes. Everything
# else — status, file, dims, limit, spec — is what was approved, and stays that way.
APPROVED_MUTABLE = ("notes",)


def update_asset(asset_id: int, **fields: Any) -> dict[str, Any]:
    """Agent-side update. Refuses approve/reject and enforces the ready-for-review gate.

    An APPROVED asset is frozen for the agent (only ``notes`` may change): it can't be moved
    back to an earlier status, re-pointed at another file, or have its limit swapped. Every
    check runs against the row as it is INSIDE the write lock, so an operator approval that
    lands between the agent's read and its write can't be overwritten.
    """
    changes = {k: v for k, v in fields.items() if k in ASSET_UPDATABLE and v is not None}
    if "status" in changes:
        wanted = str(changes["status"]).strip().lower()
        if wanted in ("approved", "rejected"):
            raise ValueError("only the operator approves or rejects — mark it ready_for_review and ask them")
        changes["status"] = _choice(wanted, AGENT_ASSET_STATUSES, "status")
    if "owner" in changes:
        changes["owner"] = _choice(changes["owner"], OWNERS, "owner")
    if changes.get("limit_id") and limits.get(changes["limit_id"]) is None:
        raise ValueError(f"unknown limit {changes['limit_id']!r} — campaign_limits lists them")
    if "spec" in changes:
        changes["spec"] = _spec_text(changes["spec"])
    if "meta" in changes:
        changes["meta"] = json.dumps(changes["meta"] or {})
    with _write() as conn:
        cur = conn.execute("SELECT * FROM assets WHERE id = ?", (int(asset_id),)).fetchone()
        if cur is None:
            raise ValueError(f"no asset with id {asset_id}")
        row = _row(cur)
        if row["status"] == "approved":
            frozen = sorted(k for k in changes if k not in APPROVED_MUTABLE)
            if frozen:
                raise ValueError(
                    f"asset #{asset_id} is approved — the agent can't change its {', '.join(frozen)}. "
                    "Register the new take as a new asset instead of changing what was approved"
                )
        merged = {**row, **changes}
        if changes.get("status") == "ready_for_review":
            problems = review_gate(merged)
            if problems:
                raise ValueError("not ready for review: " + "; ".join(problems))
        if changes:
            sets = ", ".join(f"{k} = ?" for k in changes)
            conn.execute(
                f"UPDATE assets SET {sets}, updated = ? WHERE id = ?", (*changes.values(), _now(), int(asset_id))
            )
    return get_asset(asset_id)  # type: ignore[return-value]


def delete_assets(asset_ids: list[int]) -> int:
    """Remove asset rows (never an approved one). Returns how many went."""
    ids = [int(i) for i in asset_ids]
    if not ids:
        return 0
    with _write() as conn:
        cur = conn.execute(
            f"DELETE FROM assets WHERE status != 'approved' AND id IN ({', '.join('?' * len(ids))})", ids
        )
        return cur.rowcount


def review_gate(asset: dict[str, Any]) -> list[str]:
    """Why an asset can't be offered for review ([] = it can)."""
    if asset.get("kind") == "copy_ref":
        return []
    problems: list[str] = []
    p = str(asset.get("path") or "")
    if not p:
        return ["it has no file yet — render or capture it first"]
    f = Path(p)
    if not f.is_file():
        return [f"its file is missing ({p})"]
    if asset.get("limit_id"):
        problems += limits.check(
            asset["limit_id"],
            size=f.stat().st_size,
            width=asset.get("width") or None,
            height=asset.get("height") or None,
            fmt=f.suffix.lstrip("."),
        )
    return problems


def review(asset_id: int, decision: str, note: str = "") -> dict[str, Any]:
    """OPERATOR-only: approve or reject. Called from the gated gallery route, never a tool."""
    decision = (decision or "").strip().lower()
    status = {"approve": "approved", "approved": "approved", "reject": "rejected", "rejected": "rejected"}.get(decision)
    if status is None:
        raise ValueError("decision must be approve or reject")
    with _write() as conn:
        # Gate the row as it is under the lock — not a copy read before an agent write landed.
        cur = conn.execute("SELECT * FROM assets WHERE id = ?", (int(asset_id),)).fetchone()
        if cur is None:
            raise ValueError(f"no asset with id {asset_id}")
        if status == "approved":
            problems = review_gate(_row(cur))
            if problems:
                raise ValueError("can't approve: " + "; ".join(problems))
        now = _now()
        conn.execute(
            "UPDATE assets SET status = ?, review_note = ?, reviewed_at = ?, updated = ? WHERE id = ?",
            (status, note or "", now, now, int(asset_id)),
        )
    return get_asset(asset_id)  # type: ignore[return-value]


def get_asset(asset_id: int) -> dict[str, Any] | None:
    rows = _read("SELECT * FROM assets WHERE id = ?", (int(asset_id),))
    return rows[0] if rows else None


def list_assets(campaign_id: int = 0, *, status: str = "", lane_id: int = 0, kind: str = "") -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM assets WHERE 1=1", []
    if campaign_id:
        sql += " AND campaign_id = ?"
        args.append(int(campaign_id))
    if status:
        sql += " AND status = ?"
        args.append(status)
    if lane_id:
        sql += " AND lane_id = ?"
        args.append(int(lane_id))
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    return _read(sql + " ORDER BY id", tuple(args))


# ── milestones ───────────────────────────────────────────────────────────────
def upsert_milestone(
    campaign_id: int,
    title: str = "",
    *,
    milestone_id: int = 0,
    date: str | None = None,
    workstream: str | None = None,
    owner: str | None = None,
    status: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    require_campaign(campaign_id)
    if owner is not None:
        owner = _choice(owner, OWNERS, "owner")
    if status is not None:
        status = _choice(status, MILESTONE_STATUSES, "status")
    now = _now()
    with _write() as conn:
        if milestone_id:
            row = conn.execute(
                "SELECT * FROM milestones WHERE id = ? AND campaign_id = ?", (milestone_id, campaign_id)
            ).fetchone()
            if row is None:
                raise ValueError(f"no milestone {milestone_id} in campaign {campaign_id}")
            conn.execute(
                "UPDATE milestones SET title = ?, date = ?, workstream = ?, owner = ?, status = ?, notes = ?,"
                " updated = ? WHERE id = ?",
                (
                    title or row["title"],
                    row["date"] if date is None else date,
                    row["workstream"] if workstream is None else workstream,
                    row["owner"] if owner is None else owner,
                    row["status"] if status is None else status,
                    row["notes"] if notes is None else notes,
                    now,
                    milestone_id,
                ),
            )
        else:
            if not (title or "").strip():
                raise ValueError("a milestone needs a title")
            cur = conn.execute(
                "INSERT INTO milestones (campaign_id, created, updated, date, title, workstream, owner, status, notes)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    campaign_id,
                    now,
                    now,
                    date or "",
                    title,
                    workstream or "",
                    owner or "agent",
                    status or "todo",
                    notes or "",
                ),
            )
            milestone_id = cur.lastrowid
    return _read("SELECT * FROM milestones WHERE id = ?", (milestone_id,))[0]


def milestones(campaign_id: int) -> list[dict[str, Any]]:
    return _read("SELECT * FROM milestones WHERE campaign_id = ? ORDER BY date, id", (int(campaign_id),))


# ── decisions ────────────────────────────────────────────────────────────────
def upsert_decision(
    campaign_id: int,
    question: str = "",
    *,
    decision_id: int = 0,
    options: list[str] | None = None,
    recommendation: str | None = None,
    answer: str | None = None,
) -> dict[str, Any]:
    require_campaign(campaign_id)
    now = _now()
    with _write() as conn:
        if decision_id:
            row = conn.execute(
                "SELECT * FROM decisions WHERE id = ? AND campaign_id = ?", (decision_id, campaign_id)
            ).fetchone()
            if row is None:
                raise ValueError(f"no decision {decision_id} in campaign {campaign_id}")
            new_answer = row["answer"] if answer is None else answer
            conn.execute(
                "UPDATE decisions SET question = ?, options = ?, recommendation = ?, answer = ?, answered_at = ?,"
                " updated = ? WHERE id = ?",
                (
                    question or row["question"],
                    row["options"] if options is None else json.dumps(list(options)),
                    row["recommendation"] if recommendation is None else recommendation,
                    new_answer,
                    (now if answer else row["answered_at"]) if new_answer else "",
                    now,
                    decision_id,
                ),
            )
        else:
            if not (question or "").strip():
                raise ValueError("a decision needs a question")
            cur = conn.execute(
                "INSERT INTO decisions (campaign_id, created, updated, question, options, recommendation, answer,"
                " answered_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    campaign_id,
                    now,
                    now,
                    question,
                    json.dumps(list(options or [])),
                    recommendation or "",
                    answer or "",
                    now if answer else "",
                ),
            )
            decision_id = cur.lastrowid
    return _read("SELECT * FROM decisions WHERE id = ?", (decision_id,))[0]


def decisions(campaign_id: int) -> list[dict[str, Any]]:
    return _read("SELECT * FROM decisions WHERE campaign_id = ? ORDER BY id", (int(campaign_id),))


# ── shot scripts ─────────────────────────────────────────────────────────────
def save_script(campaign_id: int, name: str, body: dict[str, Any]) -> dict[str, Any]:
    require_campaign(campaign_id)
    if not (name or "").strip():
        raise ValueError("a shot script needs a name")
    now = _now()
    with _write() as conn:
        conn.execute(
            "INSERT INTO scripts (campaign_id, created, updated, name, body) VALUES (?,?,?,?,?)"
            " ON CONFLICT(campaign_id, name) DO UPDATE SET body = excluded.body, updated = excluded.updated",
            (campaign_id, now, now, name.strip(), json.dumps(body)),
        )
    return _read("SELECT * FROM scripts WHERE campaign_id = ? AND name = ?", (campaign_id, name.strip()))[0]


def get_script(script_id: int) -> dict[str, Any] | None:
    rows = _read("SELECT * FROM scripts WHERE id = ?", (int(script_id),))
    return rows[0] if rows else None


def scripts(campaign_id: int) -> list[dict[str, Any]]:
    return _read("SELECT * FROM scripts WHERE campaign_id = ? ORDER BY id", (int(campaign_id),))


# ── the progress summary ─────────────────────────────────────────────────────
def status_summary(campaign_id: int, today: str = "") -> dict[str, Any]:
    """Who is each open item waiting on — the operator or the agent — plus counts."""
    camp = require_campaign(campaign_id)
    today = today or datetime.now(UTC).date().isoformat()
    assets = list_assets(campaign_id)
    lane_names = {ln["id"]: ln["name"] for ln in lanes(campaign_id)}
    counts = {s: 0 for s in ASSET_STATUSES}
    for a in assets:
        counts[a["status"]] = counts.get(a["status"], 0) + 1

    on_operator: list[dict[str, str]] = []
    on_agent: list[dict[str, str]] = []

    def _label(a: dict[str, Any]) -> str:
        lane = lane_names.get(a["lane_id"], "")
        return f"#{a['id']} {a['kind']} — {a['title'] or '(untitled)'}" + (f" [{lane}]" if lane else "")

    for d in decisions(campaign_id):
        if not d["answer"]:
            rec = f" (recommend: {d['recommendation']})" if d["recommendation"] else ""
            on_operator.append({"item": f"decision #{d['id']}: {d['question']}{rec}", "why": "needs an answer"})
    for a in assets:
        if a["status"] == "ready_for_review":
            on_operator.append({"item": _label(a), "why": "awaiting approval in the gallery"})
        elif a["status"] == "rejected":
            note = f": {a['review_note']}" if a["review_note"] else ""
            on_agent.append({"item": _label(a), "why": f"rejected — rework{note}"})
        elif a["status"] != "approved":
            who = on_operator if a["owner"] == "operator" else on_agent
            who.append({"item": _label(a), "why": f"{a['status']} — {a['owner']} records/produces it"})
    for m in milestones(campaign_id):
        if m["status"] in ("done", "dropped"):
            continue
        overdue = bool(m["date"]) and m["date"] < today
        why = f"{m['status']}, due {m['date'] or 'undated'}" + (" — OVERDUE" if overdue else "")
        (on_operator if m["owner"] == "operator" else on_agent).append(
            {"item": f"milestone: {m['title']}" + (f" ({m['workstream']})" if m["workstream"] else ""), "why": why}
        )

    total = len(assets)
    done = counts.get("approved", 0)
    return {
        "campaign": {k: camp[k] for k in ("id", "name", "status", "goal", "target_metric", "launch_window")},
        "counts": counts,
        "progress": {"approved": done, "total": total},
        "on_operator": on_operator,
        "on_agent": on_agent,
    }
