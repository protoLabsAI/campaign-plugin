"""The agent-facing tools — every name prefixed ``campaign_``.

Thin wrappers over the host-free modules. Each docstring is what the model reads to decide
whether to reach for the tool, so it describes the JOB. (Docstrings must stay plain string
literals: an f-string leaves ``__doc__`` as None and the tool ships with no description.)

Nothing here approves an asset: approval is the operator's, in the gallery.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from langchain_core.tools import tool

from . import cards, deps, limits, look, montage, paths, produce, render, sandbox, shoot, shotscript, store
from . import intro as intromod
from . import outro as outromod

try:  # the host's graph state, injected into a tool body (the chat session id lives there)
    from langgraph.prebuilt import InjectedState
except ImportError:  # host-free suite: a plain annotation (the tool then sees state=None)
    InjectedState = "injected-state"  # type: ignore[assignment]

log = logging.getLogger("protoagent.plugins.campaign")


def _parse_obj(value: Any, what: str) -> Any:
    if isinstance(value, (dict, list)):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        import yaml

        try:
            return yaml.safe_load(text)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"{what} is neither JSON nor YAML: {e}") from e


def _output_arg(output: Any) -> Any:
    """A montage ``output``: a mapping, JSON/YAML text of one, or a bare preset name / WxH."""
    if isinstance(output, str) and (output.strip().startswith("{") or ":" in output):
        return _parse_obj(output, "output")
    return output


def _lane_id(campaign_id: int, lane: str) -> int:
    """The id of lane ``lane`` (a name, or a numeric id) in ``campaign_id`` — 0 for no lane.

    A numeric id must name a lane OF THIS CAMPAIGN: an unchecked id would file an asset under
    a lane that doesn't exist, or under another campaign's lane. A lane whose NAME is all
    digits still resolves (an id match wins)."""
    if not lane:
        return 0
    text = str(lane).strip()
    if text.isdigit() and int(text) == 0:
        return 0
    if text.isdigit():
        row = store.lane_by_id(campaign_id, int(text)) or store.lane_by_name(campaign_id, text)
        if row is None:
            raise ValueError(
                f"no lane {text} in campaign {campaign_id} — campaign_get lists its lanes; add one with campaign_lane"
            )
        return row["id"]
    row = store.lane_by_name(campaign_id, text)
    if row is None:
        raise ValueError(f"no lane named {lane!r} in campaign {campaign_id} — add it with campaign_lane first")
    return row["id"]


def _own_asset(campaign_id: int, asset_id: int) -> dict[str, Any]:
    """The asset ``asset_id`` — which must belong to ``campaign_id`` and not be approved."""
    a = store.get_asset(asset_id)
    if a is None or a["campaign_id"] != int(campaign_id):
        raise ValueError(look.not_in_campaign(asset_id, campaign_id, a))
    if a["status"] == "approved":
        raise ValueError(f"asset #{asset_id} is approved — leave asset_id off to register the new file as a new asset")
    return a


def _supersede_take(old: dict[str, Any], new_take_dir: str) -> list[str]:
    """Re-recording into an asset replaces its previous take: drop the old take's stills.

    Stills the operator APPROVED stay (their call stands) and so does the old take dir that
    holds them; every other still of the old take is removed — row and file — and the old take
    dir goes once nothing references it. Returns notes for the tool's reply.
    """
    notes: list[str] = []
    old_dir = (old.get("meta") or {}).get("take_dir") or (str(Path(old["path"]).parent) if old.get("path") else "")
    # Every campaign, not just the take's own: a collection move can leave a take's stills (or an
    # approved one) in the old campaign while the take itself lives in another.
    stills = [
        s
        for s in store.children(old["id"], kind="still")
        if s.get("path") and old_dir and Path(s["path"]).parent == Path(old_dir)
    ]
    kept = [s for s in stills if s["status"] == "approved"]
    gone = [s for s in stills if s["status"] != "approved"]
    store.delete_assets([s["id"] for s in gone])
    if gone:
        notes.append(f"superseded {len(gone)} still(s) of the previous take: " + ", ".join(f"#{s['id']}" for s in gone))
    if kept:
        notes.append(
            "kept the previous take's APPROVED still(s) "
            + ", ".join(f"#{s['id']}" for s in kept)
            + " — the operator's call stands"
        )
    if (
        old_dir
        and not kept
        and Path(old_dir) != Path(new_take_dir)
        and paths.is_contained(old_dir)
        # never the media root itself (is_contained is true for the root): a corrupt take_dir
        # must not take every campaign's media with it
        and Path(old_dir).resolve() != paths.media_root().resolve()
    ):
        still_used = store.any_file_under(old_dir)  # any row, in any campaign, with a file under it
        if not still_used:
            import shutil

            shutil.rmtree(old_dir, ignore_errors=True)
    return notes


def _failure_note(result: dict[str, Any], error: str) -> str:
    """``FAILED at step N: <error>`` — what a kept failed take says about itself."""
    n = result.get("failed_step") or 0
    step = next((s for s in result.get("steps", []) if s.get("index") == n and s.get("error")), None)
    why = f"{step['desc']} — {step['error']}" if step else error
    return f"FAILED at step {n}: {why}" if n else f"FAILED: {why}"


def _keep_failed_take(
    campaign_id: int,
    norm: dict[str, Any],
    r: dict[str, Any],
    error: str,
    script_id: int,
    lane_id: int,
    title: str,
    asset_id: int,
) -> tuple[str, int, list[int]]:
    """Register a failed take's recording — usable footage up to the failure — as a NEW
    ``captured`` clip carrying a FAILED note, its marks reached so far and its failure still.
    It never fills or supersedes ``asset_id``: a planned slot is for a clean take."""
    facts = _file_facts(r["video"])
    note = _failure_note(r, error)
    unreadable = _video_problem(r["video"])
    if unreadable and "can't be read" not in note:
        note += f" [recording unreadable: {unreadable}]"
    marks = r.get("marks") or {}
    meta = {
        "timing": r.get("timing", ""),
        "marks": marks,
        "screenshots": r.get("screenshots") or {},
        "take_dir": r.get("dir", ""),
        "failed": {
            "step": r.get("failed_step") or 0,
            "error": error,
            "failure_png": r.get("failure_png", ""),
        },
    }
    take = store.add_asset(
        campaign_id,
        "clip",
        title or f"take: {norm['name']} (failed)",
        lane_id=lane_id,
        status="captured",
        path=r["video"],
        script_id=script_id,
        meta=meta,
        notes=note,
        **{k: v for k, v in facts.items() if k != "duration_s"},
        duration_s=facts.get("duration_s") or r.get("duration_s") or 0,
    )
    still_ids = []
    for name, p in (r.get("screenshots") or {}).items():
        st = store.add_asset(
            campaign_id,
            "still",
            f"{norm['name']}: {name}",
            lane_id=take["lane_id"],
            status="captured",
            path=p,
            parent_id=take["id"],
            script_id=script_id,
            **_file_facts(p),
        )
        still_ids.append(st["id"])
    reached = ", ".join(f"{k}={v:.2f}s" for k, v in marks.items()) or "none"
    text = (
        f"Footage KEPT → asset #{take['id']} [captured], {take['duration_s']:.1f}s usable up to the failure; "
        f"marks reached: {reached}"
        + (f"; stills {', '.join(f'#{i}' for i in still_ids)}" if still_ids else "")
        + (f" (not attached to planned asset #{asset_id} — a failed take never fills a slot)" if asset_id else "")
        + f"\n- take dir: {r.get('dir') or Path(r['video']).parent} ({Path(r['video']).name}, timing.json"
        + (", failure.png" if r.get("failure_png") else "")
        + ")"
    )
    return text, take["id"], still_ids


def _clip(text: str, n: int) -> str:
    return produce.clip_line(text, n)


# ── compact replies: what a shoot/render reply carries back into the model's context ──
# A night of brandLaunch takes put ~2 kB per shoot reply into one thread that grew to 478k
# tokens (#48): the same error three times, every path twice, a coaching paragraph. Replies
# now carry the ids, ONE directory, the numbers and the next call; the full detail (the
# whole Playwright error and call log, timing log, render attempts) stays on the asset —
# campaign_asset(id) reads it back.
ERROR_PREVIEW = 300  # chars of a step error a shoot reply quotes
WARNING_PREVIEW = 300  # chars per render warning line


def _short_error(text: str) -> str:
    """A Playwright error without its ``Call log:`` tail (it restates the step), on one line."""
    head = str(text or "").split(" | Call log:", 1)[0].split("\nCall log:", 1)[0]
    return _clip(head, ERROR_PREVIEW)


def _failed_label(take: dict[str, Any]) -> str:
    """``FAILED at step N`` for a failed take — not its whole note (the error is on the asset)."""
    n = ((take.get("meta") or {}).get("failed") or {}).get("step") or 0
    return f"FAILED at step {n}" if n else "FAILED"


def _rel(path: str, base: Path) -> str:
    """``path`` relative to ``base`` when it's inside it, else the full path."""
    try:
        return str(Path(path).relative_to(base))
    except ValueError:
        return str(path)


NOTE_PREVIEW = 160  # chars of notes per row in campaign_assets(notes=true)
META_DETAIL_CAP = 4000  # chars of the remaining meta JSON campaign_asset prints


def _asset_line(a: dict[str, Any], notes: bool = False) -> str:
    dims = f" {a['width']}×{a['height']}" if a.get("width") else ""
    dur = f" {a['duration_s']:.1f}s" if a.get("duration_s") else ""
    size = f" {limits.human_bytes(a['size_bytes'])}" if a.get("size_bytes") else ""
    lim = f" limit={a['limit_id']}" if a.get("limit_id") else ""
    path = f"\n    {a['path']}" if a.get("path") else ""
    sup = ""
    if a.get("status") == "superseded":
        by = a.get("superseded_by") or []
        sup = " → replaced by " + ", ".join(f"#{i}" for i in by) if by else ""
    extra = ""
    if a.get("review_note"):
        extra += f"\n    Review: {_clip(a['review_note'], NOTE_PREVIEW)}"
    if notes and a.get("notes"):
        extra += f"\n    Notes: {_clip(a['notes'], NOTE_PREVIEW)}"
    return (
        f"- #{a['id']} [{a['status']}] {a['kind']} — {a['title'] or '(untitled)'} (owner: {a['owner']})"
        f"{sup}{dims}{dur}{size}{lim}{path}{extra}"
    )


def asset_detail(a: dict[str, Any]) -> str:
    """One asset in full — notes, review note, family, and the meta (marks, render spec) an agent
    recorded — so a re-cut or a background producer can pick up where the last run stopped."""
    c = store.get_campaign(a["campaign_id"])
    out = [_asset_line(a), f"- campaign: #{a['campaign_id']}" + (f" {c['name']}" if c else "")]
    if a.get("lane_id"):
        ln = store.lane_by_id(a["campaign_id"], a["lane_id"])
        out.append(f"- lane: {ln['name'] if ln else a['lane_id']}")
    if a.get("spec"):
        out.append(f"- spec: {a['spec']}")
    if a.get("parent_id"):
        parent = store.get_asset(a["parent_id"])
        where = (
            f" (in campaign {parent['campaign_id']})" if parent and parent["campaign_id"] != a["campaign_id"] else ""
        )
        out.append(f"- made from: #{a['parent_id']}{where}" + ("" if parent else " (no longer exists)"))
    kids = store.children(a["id"])
    if kids:
        out.append(
            "- made from it: "
            + ", ".join(
                f"#{k['id']} {k['kind']}"
                + (f" (in campaign {k['campaign_id']})" if k["campaign_id"] != a["campaign_id"] else "")
                for k in kids
            )
        )
    if a.get("script_id"):
        out.append(f"- shot script: #{a['script_id']} (campaign_script_get)")
    if a.get("reviewed_at"):
        out.append(f"- reviewed: {a['reviewed_at']}")
    out.append("\n## Notes\n" + (a.get("notes") or "(none)"))
    if a.get("review_note"):
        out.append("\n## Review note\n" + a["review_note"])
    meta = dict(a.get("meta") or {})
    marks = meta.pop("marks", None) or {}
    if marks:
        out.append("\n## Marks (seconds in the recorded take)")
        for k, v in marks.items():
            try:
                out.append(f"- {k}: {float(v):.2f}s")
            except (TypeError, ValueError):
                out.append(f"- {k}: {v}")
    spec = meta.pop("spec", None)
    if spec:
        out.append("\n## Render spec\n" + json.dumps(spec, ensure_ascii=False, default=str))
    for key, label in (("warnings", "Warnings"), ("violations", "Limit violations")):
        items = meta.pop(key, None)
        if items:
            out.append(f"\n## {label}")
            out += [f"- {i}" for i in (items if isinstance(items, list) else [items])]
    if meta:
        rest = json.dumps(meta, ensure_ascii=False, default=str, sort_keys=True)
        if len(rest) > META_DETAIL_CAP:
            rest = rest[:META_DETAIL_CAP] + f"… ({len(rest) - META_DETAIL_CAP} more chars)"
        out.append("\n## Other meta\n" + rest)
    return "\n".join(out)


def campaign_brief(campaign_id: int) -> str:
    c = store.require_campaign(campaign_id)
    out = [f"# Campaign #{c['id']}: {c['name']}  ({c['status']})"]
    for label, key in (
        ("Product", "product"),
        ("Target URL", "target_url"),
        ("Goal", "goal"),
        ("Target metric", "target_metric"),
        ("Launch window", "launch_window"),
    ):
        if c[key]:
            out.append(f"- **{label}:** {c[key]}")
    for label, key in (
        ("Goal & math", "goal_math"),
        ("Assumptions", "assumptions"),
        ("Channel plan", "channel_plan"),
        ("Do not", "do_not"),
        ("Notes", "notes"),
    ):
        if c[key]:
            out.append(f"\n## {label}\n{c[key]}")
    lanes = store.lanes(campaign_id)
    out.append("\n## Lanes")
    out += [
        f"- lane {ln['id']} **{ln['name']}** — {ln['pitch']}"
        + (f" (audience: {ln['audience']})" if ln["audience"] else "")
        + (f" · hero asset #{ln['hero_asset_id']}" if ln["hero_asset_id"] else "")
        for ln in lanes
    ] or ["(none yet)"]
    out.append("\n## Assets")
    out += [_asset_line(a) for a in store.list_assets(campaign_id)] or ["(none yet)"]
    out.append("\n## Milestones")
    out += [
        f"- #{m['id']} {m['date'] or '(undated)'} [{m['status']}] {m['title']} — {m['workstream'] or 'general'}, owner {m['owner']}"
        for m in store.milestones(campaign_id)
    ] or ["(none yet)"]
    out.append("\n## Decisions")
    decisions = store.decisions(campaign_id)
    for d in decisions:
        opts = " / ".join(d["options"]) if d["options"] else ""
        ans = f" → **{d['answer']}**" if d["answer"] else " → (open)"
        rec = f" (recommend: {d['recommendation']})" if d["recommendation"] else ""
        out.append(f"- #{d['id']} {d['question']} [{opts}]{rec}{ans}")
    if not decisions:
        out.append("(none yet)")
    scripts = store.scripts(campaign_id)
    if scripts:
        out.append("\n## Shot scripts")
        out += [f"- script #{s['id']} {s['name']} ({len(s['body'].get('steps', []))} steps)" for s in scripts]
        out.append("(campaign_script_get(script_id) shows a script's YAML, steps numbered)")
    return "\n".join(out)


def build_tools(registry):
    """Construct the tool list. ``registry`` is used for best-effort events + media embeds."""

    def _emit(topic: str, data: dict) -> None:
        try:
            registry.emit(topic, data)
        except Exception:  # noqa: BLE001 — the event bus is chrome, never load-bearing
            pass

    def _embed(path: str, mime: str, title: str) -> str:
        """Inline an image in chat via the core media store, when the host offers it."""
        save = getattr(registry, "save_media", None)
        if not callable(save):
            return ""
        try:
            ref = save(path, mime, {"source": "campaign", "title": title})
            url = getattr(ref, "url", None) or (ref.get("url") if isinstance(ref, dict) else None)
            return f"\n\n![{title}]({url})" if url else ""
        except Exception:  # noqa: BLE001 — the path in the text is the real answer
            return ""

    # ── plans ────────────────────────────────────────────────────────────────
    @tool
    def campaign_create(
        name: str,
        product: str = "",
        goal: str = "",
        target_metric: str = "",
        launch_window: str = "",
        target_url: str = "",
        goal_math: str = "",
        assumptions: str = "",
        channel_plan: str = "",
        do_not: str = "",
        notes: str = "",
    ) -> str:
        """Start a campaign plan for any product or app: its name, the product, the goal, the
        ONE metric that says it worked, the launch window, and the target URL the media will be
        captured from. Put the reasoning in goal_math (how the target number was derived — never
        invent a baseline; say what is assumed) and assumptions (each sourced and dated). Use
        channel_plan for where each lane ships and do_not for the lines this campaign won't
        cross. Returns the new campaign id. Follow the campaign-planning skill for a brief → plan."""
        try:
            c = store.create_campaign(
                name,
                product=product,
                goal=goal,
                target_metric=target_metric,
                launch_window=launch_window,
                target_url=target_url,
                goal_math=goal_math,
                assumptions=assumptions,
                channel_plan=channel_plan,
                do_not=do_not,
                notes=notes,
            )
        except ValueError as e:
            return f"Not created — {e}"
        paths.campaign_dir(c["id"], c["name"])
        _emit("campaign_changed", {"campaign_id": c["id"]})
        return (
            f"Created campaign #{c['id']} {c['name']!r}. Media will live in {paths.campaign_dir(c['id'], c['name'])}."
        )

    @tool
    def campaign_update(
        campaign_id: int,
        name: str | None = None,
        product: str | None = None,
        goal: str | None = None,
        target_metric: str | None = None,
        launch_window: str | None = None,
        target_url: str | None = None,
        status: str | None = None,
        goal_math: str | None = None,
        assumptions: str | None = None,
        channel_plan: str | None = None,
        do_not: str | None = None,
        notes: str | None = None,
    ) -> str:
        """Change any field of a campaign plan — only the fields you pass are touched. status is
        one of draft, active, launched, done, paused, archived. Use it to record revised goal
        math, newly researched assumptions (with source + date), or the channel plan."""
        try:
            c = store.update_campaign(
                campaign_id,
                name=name,
                product=product,
                goal=goal,
                target_metric=target_metric,
                launch_window=launch_window,
                target_url=target_url,
                status=status,
                goal_math=goal_math,
                assumptions=assumptions,
                channel_plan=channel_plan,
                do_not=do_not,
                notes=notes,
            )
        except ValueError as e:
            return f"Not updated — {e}"
        _emit("campaign_changed", {"campaign_id": c["id"]})
        return f"Updated campaign #{c['id']} ({c['status']})."

    @tool
    def campaign_list(include_archived: bool = False) -> str:
        """List campaigns with their ids, status, launch window and asset progress. Start here
        when the operator refers to 'the launch' or 'the campaign' without an id."""
        rows = store.list_campaigns(include_archived=include_archived)
        if not rows:
            return "No campaigns yet. Create one with campaign_create."
        out = []
        for c in rows:
            assets = [a for a in store.list_assets(c["id"]) if not store.is_retired(a)]
            approved = sum(1 for a in assets if a["status"] == "approved")
            out.append(
                f"- #{c['id']} {c['name']} [{c['status']}] {c['launch_window'] or ''} — "
                f"{approved}/{len(assets)} assets approved"
            )
        return "\n".join(out)

    @tool
    def campaign_archive(campaign_id: int, archived: bool = True) -> str:
        """Archive a campaign — it disappears from campaign_list and the gallery's campaign picker
        (both can still show archived ones), with every asset, file and approval left exactly as
        it is — or bring it back (archived=false) to the status it had before. Use it when the
        operator wants a fresh start: collect the keepers into a new campaign
        (campaign_collect), then archive the old one."""
        try:
            c = store.set_archived(campaign_id, archived)
        except ValueError as e:
            return f"Not changed — {e}"
        _emit("campaign_changed", {"campaign_id": c["id"], "status": c["status"]})
        return f"Campaign #{c['id']} {c['name']!r} is now {c['status']}."

    @tool
    def campaign_collect(
        target_campaign_id: int,
        asset_ids: list[int],
        mode: str = "move",
        lineage: bool = True,
        dry_run: bool = True,
    ) -> str:
        """Gather assets into another campaign (a collection) — e.g. the best takes of a launch
        into a fresh campaign. mode='move' re-homes the rows (same ids, same files, same status);
        mode='copy' duplicates them, copying their files into the target's folder. lineage=true
        (default) brings everything they were made from too (the take a render was cut from, a
        montage's inputs), so the chain still reads and retention keeps it. dry_run=true (the
        default) only lists what would move/copy, with sizes — show the operator that list and
        get their OK first. APPROVED assets: the agent can't move them (the operator's route
        can), and the agent's copy of one lands as ready_for_review for the operator to
        re-approve. Create the target first with campaign_create."""
        from . import library

        try:
            ids = [int(str(i).lstrip("#")) for i in (asset_ids or [])]
            if dry_run:
                out = library.plan(ids, target_campaign_id, mode=mode, lineage=lineage)
            else:
                out = library.run(ids, target_campaign_id, mode=mode, lineage=lineage)
        except ValueError as e:
            return f"Not collected — {e}"
        if not dry_run:
            _emit("campaign_changed", {"campaign_id": target_campaign_id})
        return library.summary(out) + (
            "\n\n(dry run — nothing moved; call again with dry_run=false)" if dry_run else ""
        )

    @tool
    def campaign_get(campaign_id: int) -> str:
        """Read a whole campaign plan: goal and math, assumptions, channel plan, do-not list,
        lanes, every asset with its status/size/path, milestones, open decisions, and the saved
        shot scripts. Read this before changing a plan or producing anything for it."""
        try:
            return campaign_brief(campaign_id)
        except ValueError as e:
            return str(e)

    @tool
    def campaign_lane(
        campaign_id: int,
        name: str,
        pitch: str | None = None,
        audience: str | None = None,
        hero_asset_id: int | None = None,
        lane_id: int = 0,
    ) -> str:
        """Add or update a lane — one angle of the campaign with its own pitch, audience, and
        hero asset (e.g. 'install in one click' for plugin authors). Matching is by name within
        the campaign (or pass lane_id to rename). Only the fields you pass are changed."""
        try:
            ln = store.upsert_lane(campaign_id, name, pitch, audience, hero_asset_id, lane_id)
        except ValueError as e:
            return f"Not saved — {e}"
        _emit("campaign_changed", {"campaign_id": campaign_id})
        return f"Lane {ln['id']} {ln['name']!r} saved."

    @tool
    def campaign_asset_add(
        campaign_id: int,
        kind: str,
        title: str,
        lane: str = "",
        spec: str = "",
        owner: str = "agent",
        limit_id: str = "",
        notes: str = "",
    ) -> str:
        """Add a planned asset to the shot list. kind is clip, gif, still, card, montage, or copy_ref (a
        pointer to copy owned by the Social Studio queue — put its post id in spec). spec says
        what it shows and how (the beat, length intent, aspect, what must be legible). owner is
        who records it: 'agent' for browser captures, 'operator' for anything needing a human
        (a phone video, a voiceover). limit_id names a hard limit it must meet (campaign_limits)."""
        try:
            a = store.add_asset(
                campaign_id,
                kind,
                title,
                lane_id=_lane_id(campaign_id, lane),
                spec=spec,
                owner=owner,
                limit_id=limit_id,
                notes=notes,
            )
        except ValueError as e:
            return f"Not added — {e}"
        _emit("asset_registered", {"campaign_id": campaign_id, "asset_id": a["id"], "status": a["status"]})
        return f"Added asset #{a['id']} ({a['kind']}, planned, owner {a['owner']})."

    @tool
    def campaign_asset_update(
        asset_id: int,
        status: str | None = None,
        title: str | None = None,
        lane: str | None = None,
        spec: str | None = None,
        owner: str | None = None,
        limit_id: str | None = None,
        path: str | None = None,
        notes: str | None = None,
        superseded_by: list[int] | None = None,
        allow_failed_take: bool = False,
    ) -> str:
        """Move an asset through production or edit it: status planned → scripted → captured →
        rendered → ready_for_review. Mark ready_for_review only after the asset-review skill's
        self-check; the move is REFUSED if the file is missing or breaks its hard limit. You can
        never set approved or rejected — that is the operator's call in the gallery. Pass path
        to attach a file the operator recorded (it must be inside the campaign's media dir).
        status='superseded' retires a take a retake REPLACED: it leaves the operator's review
        queue and the counts without being approved or rejected, and montages/storyboards refuse
        it. Pass superseded_by=[new asset ids] and notes saying why. Allowed from any status
        except approved — an APPROVED asset can only be superseded by the operator. A take kept
        from a FAILED shoot is refused for ready_for_review (render the good part instead)
        unless allow_failed_take=true says the raw take itself is the deliverable."""
        try:
            row = store.get_asset(asset_id)
            if row is None:
                return f"No asset with id {asset_id}."
            fields: dict[str, Any] = {
                "status": status,
                "title": title,
                "spec": spec,
                "owner": owner,
                "limit_id": limit_id,
                "notes": notes,
                "superseded_by": superseded_by,
            }
            if lane is not None:
                fields["lane_id"] = _lane_id(row["campaign_id"], lane)
            if path is not None:
                if path and not paths.is_contained(path):
                    return (
                        f"Not updated — {path} is outside the campaign media dir ({paths.media_root()}). "
                        "Copy the file in first, so the gallery can serve it."
                    )
                fields["path"] = str(Path(path).expanduser().resolve()) if path else ""
                if path:
                    fields.update(_file_facts(fields["path"]))
            a = store.update_asset(asset_id, allow_failed_take=bool(allow_failed_take), **fields)
        except ValueError as e:
            return f"Not updated — {e}"
        _emit("asset_registered", {"campaign_id": a["campaign_id"], "asset_id": a["id"], "status": a["status"]})
        return "Updated:\n" + _asset_line(a)

    @tool
    def campaign_assets(
        campaign_id: int,
        status: str = "",
        lane: str = "",
        kind: str = "",
        include_superseded: bool = False,
        notes: bool = False,
    ) -> str:
        """List a campaign's assets with status, owner, size, dimensions, duration, file path and
        the operator's review note, optionally filtered by status, lane name, or kind. notes=true
        adds a one-line preview of each asset's notes; campaign_asset(asset_id) shows one asset in
        full (notes, marks, render spec). Superseded takes are left out unless include_superseded
        is true or status='superseded'."""
        try:
            rows = store.list_assets(campaign_id, status=status, lane_id=_lane_id(campaign_id, lane), kind=kind)
        except ValueError as e:
            return str(e)
        hidden = 0
        if not include_superseded and status != "superseded":
            hidden = sum(1 for a in rows if store.is_retired(a))
            rows = [a for a in rows if not store.is_retired(a)]
        out = "\n".join(_asset_line(a, notes=bool(notes)) for a in rows) or "No assets match."
        if hidden:
            out += f"\n({hidden} superseded take(s) hidden — include_superseded=true lists them)"
        return out

    @tool
    def campaign_asset(asset_id: int, campaign_id: int = 0) -> str:
        """Read ONE asset in full: its notes and the operator's review note (never truncated), the
        asset it was made from and the ones made from it (in any campaign), its marks, its render
        spec, warnings, and the rest of its recorded metadata. Use it to recover what an earlier
        run wrote down (caption timing, a ramp recipe) before a re-cut. Pass campaign_id to check
        the asset belongs to that campaign — when it doesn't, the reply names the one it's in."""
        a = store.get_asset(asset_id)
        if a is None:
            return f"No asset with id {asset_id}."
        if campaign_id and a["campaign_id"] != int(campaign_id):
            msg = look.not_in_campaign(asset_id, campaign_id, a)
            return msg[0].upper() + msg[1:]
        return asset_detail(a)

    @tool
    def campaign_milestone(
        campaign_id: int,
        title: str = "",
        date: str | None = None,
        workstream: str | None = None,
        owner: str | None = None,
        status: str | None = None,
        notes: str | None = None,
        milestone_id: int = 0,
    ) -> str:
        """Add a dated milestone to the schedule (or update one by milestone_id): what happens,
        on which date (YYYY-MM-DD), in which workstream (capture, render, copy, launch …), and
        whose it is — 'agent' or 'operator'. status: todo, doing, done, dropped."""
        try:
            m = store.upsert_milestone(
                campaign_id,
                title,
                milestone_id=milestone_id,
                date=date,
                workstream=workstream,
                owner=owner,
                status=status,
                notes=notes,
            )
        except ValueError as e:
            return f"Not saved — {e}"
        _emit("campaign_changed", {"campaign_id": campaign_id})
        return f"Milestone #{m['id']} {m['title']!r} ({m['date'] or 'undated'}, {m['owner']}, {m['status']})."

    @tool
    def campaign_decision(
        campaign_id: int,
        question: str = "",
        options: list[str] | None = None,
        recommendation: str | None = None,
        answer: str | None = None,
        decision_id: int = 0,
    ) -> str:
        """Record a decision the OPERATOR must make — the question, the options, and your
        recommendation with its reason. When the operator answers in chat, record their answer
        here (pass decision_id + answer); never answer one yourself. Open decisions show up in
        campaign_status as blocked on the operator."""
        try:
            d = store.upsert_decision(
                campaign_id,
                question,
                decision_id=decision_id,
                options=options,
                recommendation=recommendation,
                answer=answer,
            )
        except ValueError as e:
            return f"Not saved — {e}"
        _emit("campaign_changed", {"campaign_id": campaign_id})
        return f"Decision #{d['id']} saved ({'answered' if d['answer'] else 'open'})."

    @tool
    def campaign_status(campaign_id: int) -> str:
        """A progress summary of one campaign: asset counts by status, and every open item
        split into what is blocked on the OPERATOR (decisions, approvals, assets they record,
        their milestones) versus on the AGENT. Returns markdown plus a JSON block shaped for
        show_component — render it with show_component('keyvalue', …) and ('table', …)."""
        try:
            s = store.status_summary(campaign_id)
        except ValueError as e:
            return str(e)
        c = s["campaign"]
        lines = [
            f"**{c['name']}** [{c['status']}] — {s['progress']['approved']}/{s['progress']['total']} assets approved"
            + (f" ({s['superseded']} superseded, not counted)" if s.get("superseded") else ""),
            "",
            "Blocked on the operator:",
            *([f"- {i['item']} — {i['why']}" for i in s["on_operator"]] or ["- nothing"]),
            "",
            "On the agent:",
            *([f"- {i['item']} — {i['why']}" for i in s["on_agent"]] or ["- nothing"]),
        ]
        counts = ", ".join(f"{k}: {v}" for k, v in s["counts"].items() if v)
        components = {
            "keyvalue": {
                "title": f"{c['name']} — progress",
                "items": [
                    {"label": "Approved", "value": f"{s['progress']['approved']} / {s['progress']['total']}"},
                    {"label": "Waiting on operator", "value": str(len(s["on_operator"]))},
                    {"label": "Waiting on agent", "value": str(len(s["on_agent"]))},
                    {"label": "By status", "value": counts or "—"},
                    {"label": "Launch window", "value": c["launch_window"] or "—"},
                ],
            },
            "table": {
                "title": "Open items",
                "columns": ["Blocked on", "Item", "Why"],
                "rows": [["operator", i["item"], i["why"]] for i in s["on_operator"]]
                + [["agent", i["item"], i["why"]] for i in s["on_agent"]],
            },
        }
        return "\n".join(lines) + "\n\nshow_component payloads:\n```json\n" + json.dumps(components, indent=1) + "\n```"

    @tool
    def campaign_limits() -> str:
        """The hard platform limits this plugin enforces (e.g. GitHub's 10 MB attachment
        ceiling, the 1 MB social-preview image), each with its source URL and the date it was
        checked. Only hard, documented limits live here — soft norms (ideal clip length, best
        aspect) must be researched and written into the plan as dated assumptions."""
        return limits.brief()

    @tool
    def campaign_setup() -> str:
        """Check what media production needs on this machine — a Python with the playwright
        package (the browser runs out of process), its headless Chromium, and ffmpeg/ffprobe —
        and say exactly how to fix anything missing. Installing is the operator's call (setup
        banner buttons); this never installs."""
        deps.recheck(lambda: deps.report(registry))
        return deps.brief()

    def _live_cfg() -> dict:
        from . import surface  # register() points it at registry.live_config()

        return surface.config()

    @tool
    def campaign_backup(backfill: bool = False, campaign_id: int = 0) -> str:
        """The protoContent backup of APPROVED media: how many approved assets are archived, how
        many wait in the queue, and any that are retrying (with the error). Every approval the
        operator makes is queued automatically; backfill=true also queues every approved asset
        that is already there (campaign_id 0 = all campaigns) — already-archived files are
        recognised by their sha256 and never upload twice. Files go up as GitHub release assets on
        protoContent and each campaign's catalog is batched onto ONE open PR there (merged by the
        operator or protoContent's QA bot once CI is green). Runs in the background while the
        operator has archive_enabled on and a credential set (the GitHub App, or an
        archive_token); this tool never uploads anything itself."""
        from . import archive

        s = archive.settings(_live_cfg())
        queued = archive.backfill(campaign_id) if backfill else []
        st = archive.status(_live_cfg())
        out = [f"Archived: {st['archived']} of {st['approved']} approved asset(s); {st['queued']} queued."]
        if backfill:
            out.append(f"Queued {len(queued)} approved asset(s) for the backup.")
        for r in st["waiting_retry"][:20]:
            out.append(f"- #{r['asset_id']}: retry {r['attempts']} at {r['next_at']} — {r['last_error']}")
        why = archive.problems(s)
        out.append(
            f"Not running: {'; '.join(why)} — the operator sets these in Settings ▸ Campaign Studio ▸ Library."
            if why
            else f"Running: every minute → {s['repo']} (release media/<campaign>, catalog in {s['catalog_dir']}/), "
            f"as {'the GitHub App' if s['auth'] == 'app' else 'the archive token'}."
        )
        return "\n".join(out)

    @tool
    def campaign_prune(campaign_id: int = 0, dry_run: bool = True) -> str:
        """Retention for the asset gallery: list what the prune would delete, with sizes, by
        rule — superseded files after 7 days (the row stays as a tombstone), rendered/captured
        intermediates after 7 days unless something kept depends on them, planned rows with no
        file after 30 days — plus ready-for-review assets gone stale (14+ days, never deleted).
        APPROVED and READY-FOR-REVIEW assets are never pruned. campaign_id 0 = every campaign.
        dry_run=true (the default) touches nothing. dry_run=false deletes for real, and only works
        once the operator has set retention_mode to 'prune' in Settings; until then a daily
        report-only run is all that happens. Use it to answer 'how much space would cleanup
        free' or to clean up after the operator has turned pruning on."""
        from . import retention

        if dry_run:
            return retention.summary({**retention.plan(campaign_id=campaign_id), "dry_run": True})
        m = retention.mode(_live_cfg())
        if m != "prune":
            return (
                f"Not pruned — retention_mode is {m!r}. Deleting is the operator's switch: they set "
                "retention_mode to 'prune' in Settings ▸ Campaign Studio (or prune from the gallery's "
                "operator route). Here is what it WOULD do:\n\n"
                + retention.summary({**retention.plan(campaign_id=campaign_id), "dry_run": True})
            )
        report = retention.execute(campaign_id=campaign_id)
        _emit("campaign_changed", {"campaign_id": campaign_id, "pruned": len(report["done"])})
        return retention.summary(report)

    # ── looking at the media ─────────────────────────────────────────────────
    @tool
    def campaign_view(
        campaign_id: int,
        asset_id: int = 0,
        path: str = "",
        frames: int = 3,
        around: str = "",
        max_side: int = 0,
        every_s: float = 0,
        start: str | float = "",
        end: str | float = "",
        crop: dict | str = "",
    ) -> str:
        """LOOK at a campaign file yourself — the image comes back to you as a picture, not a
        path. Pass asset_id (from campaign_assets) or a path inside the campaign's media dir.
        A still, card or poster comes back downscaled (long side <= max_side, 320-2048, default
        1280). crop={x, y, width, height} in SOURCE pixels (the shape campaign_render takes)
        cuts out that region first, so it comes back at 100 % — with a crop, max_side defaults to
        the crop's long side and may go to 4096; the reply says the scale shown. That is how the
        smallest text of a 2560- or 3840-wide master is checked at full size (a caption strip, a
        corner badge); keep a crop's long side to ~1500 px or less, since some vision models
        shrink larger images. A crop must lie inside the frame. A clip
        or GIF comes back as `frames` evenly spaced frames (1-3: one image each at full size;
        4-12: one contact sheet), and around=<mark name or seconds> adds the frame just before
        and just after that moment — use it on every cut, speed-ramp boundary and mark.
        start/end (seconds or a mark name) limit the frames to that span. every_s=1 takes a
        frame every second from start instead of `frames` — up to 12 per call (one contact
        sheet); a longer span is paged and the reply names the NEXT PAGE's start — use it on a
        span a sheet flagged, not to page a whole clip. A review pass is ONE 12-frame sheet
        (frames=12) plus around= each cut: every image stays in the conversation. At most 3
        images per call (around's two + one sheet). This is how the asset-review self-check is done:
        read every still and the cut frames for secrets, home paths, emails and usernames on
        screen (terminal and canvas text too), clutter, the wrong theme, and legibility at half
        size."""
        try:
            res = look.view(
                campaign_id,
                asset_id,
                path,
                frames=frames,
                around=around,
                max_side=max_side,
                every_s=every_s,
                start=start,
                end=end,
                crop=crop,
            )
        except (look.LookError, render.RenderError, ValueError) as e:
            return f"Can't show it — {e}"
        text = look.caption(res)
        envelope = _multimodal_fn()
        if envelope is None:
            return (
                text + "\n\nVision isn't available: this protoAgent core has no multimodal tool results "
                "(graph.sdk.multimodal_tool_result), so the images above could not be shown to you. You have "
                "NOT reviewed this file — ask the operator to look at it in the Campaign Studio gallery."
            )
        try:
            return envelope(text, images=[{"b64": i["b64"], "mime": i["mime"]} for i in res["images"]])
        except ValueError as e:  # core enforces its own caps; say so instead of failing the call
            return f"{text}\n\nThe host refused the images ({e}) — try a smaller max_side or fewer frames."

    # ── shot scripts ─────────────────────────────────────────────────────────
    @tool
    def campaign_script_save(campaign_id: int, script: str, name: str = "") -> str:
        """Validate and save a NEW shot script (YAML or JSON) to a campaign, so a take can be
        re-recorded identically later. Save a script ONCE, then iterate on it by id:
        campaign_shoot(script_id=…, overrides={N: step-change, "base_url": …}) fixes a step or
        points the same script at another sandbox / theme without resending it — never paste the
        whole YAML again for a small change. Validation reports every problem with its step
        number (unknown step, missing target, bad timezone…). Pass script='template' to get an
        annotated example to adapt. Follow the shot-scripting skill: prefer role/text targets
        over CSS classes, hold frames long enough to read, redact secrets, fix the timezone."""
        if script.strip().lower() == "template":
            return "Shot-script template (adapt it, then save):\n```yaml\n" + shotscript.example() + "\n```"
        try:
            data = shotscript.parse(script)
            if name:
                data["name"] = name
            norm = shotscript.validate(data)
            prior = next((r for r in store.scripts(campaign_id) if r["name"] == norm["name"]), None)
            if prior is not None and prior["body"] == data:
                return (
                    f"Unchanged — shot script #{prior['id']} {norm['name']!r} already holds exactly this. "
                    f"Record it with campaign_shoot(campaign_id={campaign_id}, script_id={prior['id']}); change "
                    "it with overrides=…, not by saving it again."
                )
            row = store.save_script(campaign_id, norm["name"], data)
        except shotscript.ScriptError as e:
            return "Not saved — fix these and resend:\n" + "\n".join(f"- {p}" for p in e.problems)
        except ValueError as e:
            return f"Not saved — {e}"
        return (
            f"Saved shot script #{row['id']} {norm['name']!r} ({len(norm['steps'])} steps"
            + (", replacing the earlier version" if prior is not None else "")
            + f"). Record it with campaign_shoot(campaign_id={campaign_id}, script_id={row['id']}); "
            "iterate with overrides=… on that id."
        )

    @tool
    def campaign_script_get(script_id: int, steps: str = "") -> str:
        """Read a saved shot script back as YAML — every step commented with its number (the
        numbers validation errors and failed takes use). steps='20-30' (or '27') shows only
        those steps plus the settings, so fixing one step doesn't pull the whole script into
        context. Use it to fix a script without rewriting it from memory: change one step with
        campaign_shoot(script_id=…, overrides={N: …}). campaign_get lists a campaign's scripts
        by id."""
        row = store.get_script(script_id)
        if row is None:
            return f"No shot script #{script_id} — campaign_get(campaign_id) lists a campaign's scripts."
        try:
            span = _step_span(steps, len(row["body"].get("steps") or []))
        except ValueError as e:
            return str(e)
        return (
            f"Shot script #{row['id']} {row['name']!r} (campaign {row['campaign_id']}, saved {row['updated']}"
            + (f"; steps {span[0]}–{span[1]} of {len(row['body'].get('steps') or [])}" if span else "")
            + f"):\n```yaml\n{_script_yaml(row['body'], span)}```"
        )

    @tool
    def campaign_shoot(
        campaign_id: int,
        script_id: int = 0,
        script: str = "",
        asset_id: int = 0,
        title: str = "",
        lane: str = "",
        overrides: dict | str = "",
    ) -> str:
        """Record a take: run a shot script in headless Chromium (Playwright) with video on,
        and register the .webm (or, with `capture: device`, a device-pixel H.264 .mp4 — e.g.
        2560×1600 from a 1280×800 viewport at device_scale_factor 2, for crisp small text), the
        named stills, and a timing log of every mark as campaign assets (status 'captured'). Pass script_id for a saved script, or script (YAML/JSON) to
        validate, save and run in one go. Pass asset_id to fill a planned asset with this take.
        THE ITERATION PATH: save a script once, then re-shoot it by script_id with overrides —
        never resend the whole script. overrides={N: change} fixes step N (1-based, as errors
        number them): a whole step ({"click": {"role": "button", "name": "Save"}}) replaces it,
        options ({"timeout_ms": 90000}) are merged into it; a top-level setting
        ({"base_url": "http://127.0.0.1:7944", "color_scheme": "light"}) replaces that setting, so
        one script records another sandbox or theme. The patched script is validated like any
        other and SAVED (same name), so the take stays re-recordable. The reply is a short
        summary (asset ids, the take dir, marks); campaign_asset(id) has the full detail.
        On a failed step you get the step number, the error, and a screenshot of the page at
        that moment — and the recording up to the failure is KEPT as a new 'captured' clip noted
        'FAILED at step N: …' with the marks it reached, so campaign_render can still cut a beat
        from it. Deterministic: same script, same take."""
        if (reason := deps.need_browser()) is not None:
            return reason
        try:
            store.require_campaign(campaign_id)
            if script_id:
                row = store.get_script(script_id)
                if row is None or row["campaign_id"] != int(campaign_id):
                    return f"No shot script #{script_id} in campaign {campaign_id}."
                data = row["body"]
            else:
                if not script.strip():
                    return "Pass script_id (a saved script) or script (YAML/JSON)."
                data = shotscript.parse(script)
            changed: list[int | str] = []
            if overrides not in (None, "", {}):
                data, changed = shotscript.apply_overrides(data, _parse_obj(overrides, "overrides"))
            norm = shotscript.validate(data)
            if not script_id:
                script_id = store.save_script(campaign_id, norm["name"], data)["id"]
            elif changed:
                store.save_script(campaign_id, row["name"], data)
            lane_id = _lane_id(campaign_id, lane)
            previous = _own_asset(campaign_id, asset_id) if asset_id else None
        except shotscript.ScriptError as e:
            what = "Script (with your overrides) is invalid" if overrides not in (None, "", {}) else "Script is invalid"
            return f"{what} — nothing was recorded or saved:\n" + "\n".join(f"- {p}" for p in e.problems)
        except ValueError as e:
            return str(e)

        c = store.require_campaign(campaign_id)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        out_dir = paths.unique_dir(paths.campaign_dir(campaign_id, c["name"]) / "shots", f"{norm['name']}-{stamp}")
        try:
            res = shoot.run(norm, out_dir)
        except shoot.ShootError as e:
            r = e.result
            n = r.get("failed_step") or 0
            before = [s for s in r.get("steps", []) if not s.get("error")][-3:]
            msg = f"Take failed — {_short_error(str(e))}"
            if before:
                msg += "\n- steps before it: " + " · ".join(
                    f"{s['index']}. {_clip(s['desc'], 60)} @{s.get('t_start', 0):.1f}s" for s in before
                )
            kept_id = 0
            if r.get("video") and Path(r["video"]).is_file():
                kept, kept_id, still_ids = _keep_failed_take(
                    campaign_id, norm, r, str(e), script_id, lane_id, title, asset_id
                )
                _emit("shoot_finished", {"campaign_id": campaign_id, "asset_id": kept_id, "stills": still_ids})
                msg += "\n" + kept
            elif r.get("failure_png"):
                msg += f"\n- failure still: {r['failure_png']}"
            fix = f"{{{n}: {{...}}}}" if n else "{N: {...}}"
            msg += (
                "\nNext: fix the step (a role/text target, a wait_for before it, its own timeout_ms) with "
                f"campaign_shoot(campaign_id={campaign_id}, script_id={script_id}, overrides={fix}) — don't resend "
                "the script."
            )
            if kept_id:
                msg += (
                    f" If the kept footage already holds the beat, cut it: campaign_render(asset_id={kept_id}, …) "
                    f"ending at a reached mark. Full error + timing log: campaign_asset({kept_id})."
                )
            if r.get("failure_png"):
                msg += _embed(r["failure_png"], "image/png", "failure")
            return msg
        except Exception as e:  # noqa: BLE001 — a browser launch failure etc. should read clearly
            log.exception("[campaign] shoot failed")
            return f"The browser couldn't run: {e}\n\n{deps.brief()}"

        problem = _video_problem(res.get("video", ""))
        if problem:
            # A take that "passed" but whose recording ffprobe can't read is not a good take —
            # never let it fill a planned slot or pass for one.
            r = {**res, "failed_step": res.get("failed_step") or 0}
            err = f"the recording can't be read ({problem}) — it may be truncated"
            kept, take_id, still_ids = _keep_failed_take(campaign_id, norm, r, err, script_id, lane_id, title, asset_id)
            _emit("shoot_finished", {"campaign_id": campaign_id, "asset_id": take_id, "stills": still_ids})
            return (
                f"Take failed — {err}\n{kept}\n"
                f"Next: re-shoot it — campaign_shoot(campaign_id={campaign_id}, script_id={script_id})."
            )
        facts = _file_facts(res["video"]) if res.get("video") else {}
        meta = {
            "timing": res["timing"],
            "marks": res["marks"],
            "screenshots": res["screenshots"],
            "take_dir": res["dir"],
        }
        fields = dict(
            path=res.get("video", ""),
            status="captured",
            script_id=script_id,
            meta=meta,
            **{k: v for k, v in facts.items() if k != "duration_s"},
            duration_s=facts.get("duration_s") or res["duration_s"],
        )
        superseded: list[str] = []
        if asset_id:
            try:
                take = store.update_asset(asset_id, **fields)
            except ValueError as e:
                return f"Recorded to {res['dir']} but couldn't attach it to asset #{asset_id}: {e}"
            if previous and previous.get("path") and previous["path"] != take["path"]:
                superseded = _supersede_take(previous, res["dir"])
        else:
            take = store.add_asset(campaign_id, "clip", title or f"take: {norm['name']}", lane_id=lane_id, **fields)
        still_ids = []
        for name, p in res["screenshots"].items():
            st = store.add_asset(
                campaign_id,
                "still",
                f"{norm['name']}: {name}",
                lane_id=take["lane_id"],
                status="captured",
                path=p,
                parent_id=take["id"],
                script_id=script_id,
                **_file_facts(p),
            )
            still_ids.append(st["id"])
        _emit("shoot_finished", {"campaign_id": campaign_id, "asset_id": take["id"], "stills": still_ids})
        marks = ", ".join(f"{k}={v:.2f}s" for k, v in res["marks"].items()) or "none"
        out = (
            f"Recorded take → asset #{take['id']} ({take['duration_s']:.1f}s, {take['width']}×{take['height']}, "
            f"{limits.human_bytes(take['size_bytes'])}) from script #{script_id}\n"
            f"- take dir: {res['dir']} ({_rel(res.get('video', ''), Path(res['dir']))}, timing.json)\n"
            f"- marks: {marks}\n"
            + (f"- capture: {res['capture_note']}\n" if res.get("capture_note") else "")
            + f"- stills: {', '.join(f'#{i}' for i in still_ids) or 'none'}\n"
            + "".join(f"- {n}\n" for n in superseded)
            + f"Next: LOOK (campaign_view each still + ONE 12-frame sheet of the clip), then "
            f"campaign_render(asset_id={take['id']}, outputs=[…]). Re-take: campaign_shoot(script_id={script_id}, "
            "overrides=…), never the whole script again."
        )
        first = next(iter(res["screenshots"].values()), "")
        if first:
            out += _embed(first, "image/png", f"{norm['name']} still")
        return out

    @tool
    def campaign_sandbox_reset(
        campaign_id: int,
        script_id: int = 0,
        base_url: str = "",
        clear: list[str] | str = "",
        theme: str = "",
    ) -> str:
        """Reset a protoAgent SANDBOX console between takes — delete its chat sessions, artifacts
        and goals so the next take starts clean (clear= any of sessions, artifacts, goals;
        default all three), and optionally set its theme preset (theme='gruvbox', in the script's
        color_scheme). Use it instead of hand-written browser_eval cleanup. The target is the
        console a saved shot script records: script_id (its base_url), or base_url matching one
        of the campaign's scripts, or — with neither — the one console its scripts share. It
        REFUSES, before sending anything, a console that isn't on this machine, the operator's
        :7870 / :7871 instances, and this agent's own console (and refuses when it can't tell
        which console is its own). Requests use the script's own auth (bearer_env /
        storage_state). Returns one line: what was cleared and anything left."""
        if (reason := deps.need_browser()) is not None:
            return reason
        try:
            res = sandbox.reset(campaign_id, script_id=script_id, base_url=base_url, clear=clear, theme=theme)
        except sandbox.SandboxError as e:
            return f"Not reset — {e}"
        except ValueError as e:
            return f"Not reset — {e}"
        except shoot.WorkerError as e:
            return f"Not reset — the browser worker couldn't run: {e}"
        return sandbox.describe(res)

    # ── render ───────────────────────────────────────────────────────────────
    @tool
    def campaign_render(asset_id: int, outputs: list[dict] | str) -> str:
        """Cut a recorded take into shareable files with ffmpeg and register each as an asset
        (status 'rendered'). outputs is a list; each item has name, format (mp4 | gif | poster),
        start/end (seconds or a mark name from the take — for a failed take, only a mark it
        reached), optional speed ramps [{from, to, factor, ease}] to fast-forward dead time
        (ease: true or seconds accelerates into and out of the ramp smoothly instead of
        switching speed on one frame; the clip is continuous — no interior cuts — and a ramp
        above 4x is flagged as a jump cut unless continuous: false, an un-eased one of 2x+ as
        abrupt), optional crop {x,y,width,height} (source
        px), width, fps, and limit (a hard-limit id such as github_attachment_video_free) and/or
        max_bytes, and bookends (mp4 only: brand — the default, protoLabs bookends v1: splash bumper →
        fade → title card → edge sweep → the clip → edge sweep → bumper → fade → Star CTA, titled by
        the output's title — or none for a bare clip). mp4 is H.264 (preset slow)/yuv420p/faststart at crf 16 —
        deliverable quality — unless you pass crf. A platform limit that caps bytes steps an mp4's CRF then
        width until it fits and warns when that went past crf 20; a bare max_bytes only goes to crf 20 and,
        if still over, keeps the quality and warns instead. gif steps fps then width until it fits. Reports final size, dimensions and
        duration, and anything still over a hard limit (which then can't be marked ready)."""
        if (reason := deps.need_ffmpeg()) is not None:
            return reason
        take = store.get_asset(asset_id)
        if take is None:
            return f"No asset with id {asset_id}."
        if not take.get("path") or not Path(take["path"]).is_file():
            return f"Asset #{asset_id} has no recorded file to render from."
        try:
            specs = _parse_obj(outputs, "outputs")
        except ValueError as e:
            return str(e)
        if isinstance(specs, dict):
            specs = [specs]
        if not isinstance(specs, list) or not specs:
            return "outputs must be a non-empty list of output specs."
        if len(specs) > render.MAX_OUTPUTS:
            return f"{len(specs)} outputs in one call is too many — at most {render.MAX_OUTPUTS}; split the job."
        marks = (take.get("meta") or {}).get("marks") or {}
        failed = (take.get("meta") or {}).get("failed") or None
        try:
            source = render.probe(take["path"])
        except render.RenderError as e:
            return str(e)
        c = store.require_campaign(take["campaign_id"])
        out_dir = paths.unique_dir(
            paths.campaign_dir(c["id"], c["name"]) / "renders" / Path(take["path"]).parent.name,
            datetime.now(UTC).strftime("%Y%m%d-%H%M%S"),
        )
        lines, made = [], []
        for raw in specs:
            try:
                spec = render.normalize_output(raw, marks, source["duration_s"])
                r = render.render_output(take["path"], out_dir, spec, marks=marks, source=source)
            except render.RenderError as e:
                hint = ""
                if failed and "neither seconds nor a mark" in str(e):
                    hint = (
                        f" — this take {_failed_label(take)}, so marks after that step were never "
                        "reached: end at a time (seconds) or a mark it did reach"
                    )
                lines.append(f"- {raw.get('name', '?') if isinstance(raw, dict) else raw}: FAILED — {e}{hint}")
                continue
            kind = {"mp4": "clip", "gif": "gif", "poster": "still"}[spec["format"]]
            notes = ("VIOLATES: " + "; ".join(r["violations"])) if r["violations"] else ""
            a = store.add_asset(
                c["id"],
                kind,
                spec["title"] or f"{Path(take['path']).stem}: {spec['name']}",
                lane_id=take["lane_id"],
                status="rendered",
                path=r["path"],
                size_bytes=r["size_bytes"],
                width=r["width"],
                height=r["height"],
                duration_s=r["duration_s"],
                limit_id=spec["limit"],
                parent_id=take["id"],
                meta={"spec": raw, "attempts": r["attempts"], "violations": r["violations"], "warnings": r["warnings"]},
                notes=notes,
            )
            made.append(a["id"])
            size_notes: list[str] = []
            booked = _bookend_render(c, a, spec, r, out_dir, size_notes) if spec["bookends"] == "brand" else ""
            if booked.startswith("/"):
                a = store.get_asset(a["id"])
                r = {**r, "path": a["path"], "size_bytes": a["size_bytes"], "width": a["width"],
                     "height": a["height"], "duration_s": a["duration_s"],
                     "warnings": [*r["warnings"], *(f"bookends: {n}" for n in size_notes)]}  # fmt: skip
            elif booked:
                r = {**r, "warnings": [*r["warnings"], booked]}
            lines.append(
                f"- #{a['id']} {_rel(r['path'], out_dir)} — {limits.human_bytes(r['size_bytes'])}, "
                f"{r['width']}×{r['height']}" + (f", {r['duration_s']:.1f}s" if r["duration_s"] else "")
            )
            lines += [f"    ⚠ VIOLATES: {_clip(v, WARNING_PREVIEW)}" for v in r["violations"]]
            lines += [f"    ⚠ {_clip(w, WARNING_PREVIEW)}" for w in r["warnings"]]
        if made and take["status"] in ("planned", "scripted", "captured"):
            store.update_asset(take["id"], status="rendered")
        _emit("render_finished", {"campaign_id": c["id"], "source_asset_id": take["id"], "assets": made})
        return (
            (
                f"Rendering from a FAILED take ({_failed_label(take)}) — only footage up to the failure exists.\n"
                if failed
                else ""
            )
            + (f"Rendered into {out_dir}:\n" if made else "Rendered:\n")
            + "\n".join(lines)
            + (
                "\nNext: asset-review — campaign_view each file (one 12-frame sheet + around= each cut), then "
                "campaign_asset_update(…, status='ready_for_review'). Attempts + specs: campaign_asset(id)."
                if made
                else ""
            )
        )

    # ── cards ────────────────────────────────────────────────────────────────
    @tool
    def campaign_card(
        campaign_id: int,
        template: str,
        data: dict | str,
        size: str = "",
        limit: str = "",
        lane: str = "",
        asset_id: int = 0,
    ) -> str:
        """Render a branded card to PNG from a bundled template: og-1280x640 (social preview,
        held under GitHub's 1 MB limit), x-card-1600x900, square-1080, title-slide-1920x1080
        (an end frame for clips). data supplies title, subtitle, eyebrow, url, footer, and an
        optional image (a still's file path, e.g. a take's still or a cropped poster) with image_fit 'cover' (fill the frame, default) or 'contain' (keep the image's own aspect) — plus colors/fonts/logo/brand to
        override the brand look, which otherwise comes from the Social Studio brand kit file or
        this plugin's brand settings. size overrides the template's WxH. Registers a 'card'
        asset (or fills asset_id)."""
        if (reason := deps.need_browser()) is not None:
            return reason
        try:
            c = store.require_campaign(campaign_id)
            payload = _parse_obj(data, "data") or {}
            if not isinstance(payload, dict):
                return "data must be a mapping (title, subtitle, url, image, …)."
            if payload.get("image") and not Path(str(payload["image"])).expanduser().is_file():
                return f"data.image {payload['image']} doesn't exist."
            if template not in cards.templates():
                return f"Not rendered — unknown template {template!r} — bundled: {', '.join(cards.templates())}"
            lane_id = _lane_id(campaign_id, lane)
            if asset_id:
                _own_asset(campaign_id, asset_id)
            out = (
                paths.unique_dir(
                    paths.campaign_dir(c["id"], c["name"]) / "cards",
                    f"{template}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}",
                )
                / template
            )
            r = cards.render(template, payload, out, size=size, limit=limit or None)
        except ValueError as e:
            return f"Not rendered — {e}"
        except Exception as e:  # noqa: BLE001 — a browser failure should read clearly
            log.exception("[campaign] card failed")
            return f"The browser couldn't render the card: {e}\n\n{deps.brief()}"
        fields = dict(
            path=r["path"],
            status="rendered",
            size_bytes=r["size_bytes"],
            width=r["width"],
            height=r["height"],
            limit_id=r["limit"],
            meta={
                "template": template,
                "data": {k: v for k, v in payload.items() if k != "image"},
                "attempts": r["attempts"],
            },
            notes=("VIOLATES: " + "; ".join(r["violations"])) if r["violations"] else "",
        )
        try:
            if asset_id:
                a = store.update_asset(asset_id, **fields)
            else:
                a = store.add_asset(campaign_id, "card", payload.get("title") or template, lane_id=lane_id, **fields)
        except ValueError as e:
            return f"Rendered to {r['path']} but couldn't register it — {e}"
        _emit("asset_registered", {"campaign_id": campaign_id, "asset_id": a["id"], "status": a["status"]})
        flag = f"\n⚠ {'; '.join(r['violations'])}" if r["violations"] else ""
        mime = "image/jpeg" if r["format"] in ("jpg", "jpeg") else "image/png"
        return (
            f"Card → asset #{a['id']}: {r['width']}×{r['height']} {r['format']}, {limits.human_bytes(r['size_bytes'])}"
            f" (brand from {r['brand_source']})\n{r['path']}{flag}" + _embed(r["path"], mime, template)
        )

    # ── montage ──────────────────────────────────────────────────────────────
    def _sequence(sequence: Any) -> list:
        seq = _parse_obj(sequence, "sequence")
        if not isinstance(seq, list):
            raise montage.MontageError("sequence must be a list of {clip: …} / {card: …} items")
        return seq

    def _problems(e: montage.MontageError) -> str:
        return "\n".join(f"- {p}" for p in e.problems)

    @tool
    def campaign_montage(
        campaign_id: int,
        sequence: list | str,
        output: dict | str = "",
        title: str = "",
        lane: str = "",
        asset_id: int = 0,
    ) -> str:
        """Cut many short clips + title cards into ONE launch montage mp4 with colour-keyed
        transitions, and register it as a 'montage' asset (+ its poster, + a GIF if asked and it
        fits). sequence is an ordered list of items:
        {clip: asset_id, in?: s, out?: s, speed?: 0.25-4, label?: 'lower third', theme_color?: '#hex',
        focus?: {x,y,w,h} source px, hold?: s (freeze the last frame), caption?: {at, region, color},
        transition?: style | {style, duration}} or
        {card: {title, subtitle?, eyebrow?, url?, cta?, bg?, fg?, accent?, logo?: bool,
        template?: title-slide|cta-slide}, duration: s, zoom?: bool, theme_color?, transition?} — and,
        LAST, {outro: true} to append the operator's brand outro (beat on the last clip, bumper,
        CTA card; it refuses a cut that trims into the last clip's final caption). A clip is ONE continuous stretch: in/out trim only
        its ends, speed is one uniform factor (no ramps, no interior cuts). Transitions (into an
        item): edge_sweep (default — the 600 ms signature: the next shot revealed left → right
        behind a brand-accent band; fixed-length, no duration) — colorwipe (a bar in the NEXT item's
        theme_color; also colorwipe_left/_up/_down), cut, or an xfade style (fade, wipeleft,
        slideleft, smoothleft, circleopen, …); output reduced_motion: true makes moving ones cuts. output (mapping, or just a preset name): name,
        preset landscape 1920x1080 (canonical — 1920x1080 clips pass through untouched) |
        vertical 1080x1920 | square 1080x1080, fit auto|letterbox|crop, fps, transition,
        transition_duration (0.45), bookends (auto — the default: protoLabs bookends v1 wrap the cut, title
        = output.title; false for README loop tiles / GIF-style loops), limit (a hard-limit id — duration and size are enforced),
        max_bytes, gif, poster_at. Unknown keys and missing/foreign/rejected/superseded clips are refused;
        clips not yet approved are allowed but the montage is marked DRAFT INPUTS. Run
        campaign_storyboard first to review the cut. Follow the montage-editing skill."""
        try:
            c = store.require_campaign(campaign_id)
            seq = _sequence(sequence)
            raw_out = _output_arg(output)
            opts = montage.normalize_output(raw_out)
            # Beats first (superseded, rejected, missing …) — they need no ffmpeg.
            montage.check_items(campaign_id, montage.parse_sequence(seq))
            if (reason := deps.need_ffmpeg()) is not None:
                return reason
            lane_id = _lane_id(campaign_id, lane)
            if asset_id:
                target = _own_asset(campaign_id, asset_id)
                if target["kind"] != "montage":
                    return f"Not rendered — asset #{asset_id} is a {target['kind']}; fill a planned montage asset or leave asset_id off."
            out_dir = paths.unique_dir(
                paths.campaign_dir(c["id"], c["name"]) / "montages",
                f"{opts['name']}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}",
            )
        except montage.MontageError as e:
            return "Not rendered — fix these and resend:\n" + _problems(e)
        except ValueError as e:
            return f"Not rendered — {e}"
        try:
            r = montage.render_montage(campaign_id, seq, raw_out, out_dir)
        except montage.MontageError as e:
            shutil.rmtree(out_dir, ignore_errors=True)
            return "Not rendered — fix these and resend:\n" + _problems(e)
        except (render.RenderError, shoot.WorkerError) as e:
            return f"The montage didn't render — {e}"
        notes = []
        if r["violations"]:
            notes.append("VIOLATES: " + "; ".join(r["violations"]))
        if r["unapproved"]:
            notes.append(
                "DRAFT INPUTS: built from unapproved "
                + ", ".join(f"#{i}" for i in r["unapproved"])
                + " — get those approved (or swap them) before this ships"
            )
        meta = {
            "sequence": seq,
            "output": raw_out or {},
            "timeline": r["timeline"],
            "attempts": r["attempts"],
            "engines": r["engines"],
            "warnings": r["warnings"],
            "violations": r["violations"],
            "inputs": r["inputs"],
            "unapproved_inputs": r["unapproved"],
        }
        fields = dict(
            path=r["path"],
            status="rendered",
            size_bytes=r["size_bytes"],
            width=r["width"],
            height=r["height"],
            duration_s=r["duration_s"],
            limit_id=r["opts"]["limit"],
            meta=meta,
            notes=" ".join(notes),
        )
        try:
            if asset_id:
                a = store.update_asset(asset_id, **fields)
            else:
                a = store.add_asset(
                    campaign_id, "montage", title or r["opts"]["title"] or r["opts"]["name"], lane_id=lane_id, **fields
                )
        except ValueError as e:
            return f"Rendered to {r['path']} but couldn't register it — {e}"
        extra = []
        if r["poster"]:
            p = store.add_asset(
                campaign_id, "still", f"{a['title']}: poster", lane_id=a["lane_id"], status="rendered",
                path=r["poster"], parent_id=a["id"], **_file_facts(r["poster"]),
            )  # fmt: skip
            extra.append(f"- poster → asset #{p['id']}: {r['poster']}")
        if r["gif"]:
            g = store.add_asset(
                campaign_id, "gif", f"{a['title']}: gif", lane_id=a["lane_id"], status="rendered",
                path=r["gif"]["path"], parent_id=a["id"], limit_id=r["opts"]["gif_limit"],
                **_file_facts(r["gif"]["path"]),
            )  # fmt: skip
            extra.append(f"- gif → asset #{g['id']}: {limits.human_bytes(g['size_bytes'])}, {r['gif']['path']}")
        _emit("render_finished", {"campaign_id": campaign_id, "source_asset_id": 0, "assets": [a["id"]]})
        lines = [
            f"Montage → asset #{a['id']}: {r['width']}×{r['height']} mp4, {r['duration_s']:.2f}s "
            f"(planned {r['expected_duration_s']:.2f}s), {limits.human_bytes(r['size_bytes'])} "
            f"({len(r['attempts'])} encode attempt{'s' if len(r['attempts']) != 1 else ''})",
            r["path"],
            *extra,
            "",
            "Cut sheet:",
            montage.cut_sheet(r["timeline"], r["expected_duration_s"]),
        ]
        if r["violations"]:
            lines += ["", "⚠ " + "; ".join(r["violations"])]
        if r["unapproved"]:
            lines += [
                "",
                "⚠ DRAFT — built from inputs the operator hasn't approved: "
                + ", ".join(f"#{i}" for i in r["unapproved"]),
            ]
        if r["warnings"]:
            lines += ["", "Notes:", *(f"- {w}" for w in r["warnings"] if "not approved" not in w)]
        lines += [
            "",
            f"LOOK at it before offering it: campaign_view(asset_id={a['id']}, frames=12) and around= each transition "
            "time above — then campaign_asset_update(…, status='ready_for_review').",
        ]
        out = "\n".join(lines)
        if r["poster"]:
            out += _embed(r["poster"], "image/png", f"{a['title']} poster")
        return out

    @tool
    def campaign_storyboard(campaign_id: int, sequence: list | str, output: dict | str = "") -> str:
        """Review a montage cut BEFORE rendering it: a contact sheet comes back to you as a
        picture — one frame per item (a clip's middle frame, framed exactly as the montage will
        frame it; each card rendered), in order, with a swatch between items for the transition
        (the colour wipe's colour, an xfade's real midpoint, a thin bar for a cut) — plus the cut
        sheet: every item's start time, length after speed, and each transition's moment.
        Same sequence and output as campaign_montage; it validates them identically (unknown
        keys, missing/rejected/superseded clips, transitions longer than the items, a hard limit the runtime breaks).
        Check: one idea per beat, theme colours that differ beat to beat, tagline cadence, the
        end card last, and a focus that keeps the subject in a vertical/square cut."""
        try:
            c = store.require_campaign(campaign_id)
            seq = _sequence(sequence)
            raw_out = _output_arg(output)
            opts = montage.normalize_output(raw_out)
            # Beats first (superseded, rejected, missing …) — they need no ffmpeg.
            montage.check_items(campaign_id, montage.parse_sequence(seq))
            if (reason := deps.need_ffmpeg()) is not None:
                return reason
            dst = (
                paths.unique_dir(
                    paths.campaign_dir(c["id"], c["name"]) / "storyboards", datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
                )
                / f"{opts['name']}-storyboard.png"
            )
            b = montage.storyboard(campaign_id, seq, raw_out, dst)
        except montage.MontageError as e:
            return "Can't storyboard it — fix these and resend:\n" + _problems(e)
        except (render.RenderError, shoot.WorkerError, ValueError) as e:
            return f"Can't storyboard it — {e}"
        g = b["grid"]
        text = (
            f"Storyboard — {len(b['timeline'])} items, {b['total']:.2f}s at {b['opts']['width']}×{b['opts']['height']} "
            f"({b['opts']['preset']}). Read the sheet left → right, top → bottom ({g['columns']} items a row); "
            "the narrow tile after an item is the transition into the next.\n"
            f"{b['path']}\n\nCut sheet:\n{montage.cut_sheet(b['timeline'], b['total'])}"
            + ("\n\nNotes:\n" + "\n".join(f"- {w}" for w in b["warnings"]) if b["warnings"] else "")
        )
        ff = deps.ffmpeg()
        envelope = _multimodal_fn()
        if envelope is None or not ff:
            return (
                text + "\n\nVision isn't available on this core, so the sheet couldn't be shown to you — "
                "ask the operator to look at it." + _embed(b["path"], "image/png", "storyboard")
            )
        import base64
        import tempfile

        with tempfile.TemporaryDirectory(prefix="campaign-board-") as td:
            try:
                data = look._encode(
                    render._default_runner, ff, ["-i", b["path"]], look._scale(2048, 2048), Path(td) / "board.jpg"
                )
            except look.LookError as e:
                return f"{text}\n\nCouldn't encode the sheet for viewing ({e})."
        try:
            return envelope(text, images=[{"b64": base64.b64encode(data).decode(), "mime": "image/jpeg"}])
        except ValueError as e:
            return f"{text}\n\nThe host refused the image ({e})."

    # ── one request → a finished video ───────────────────────────────────────
    def _session(state: Any) -> str:
        if isinstance(state, dict):
            return str(state.get("session_id") or "")
        return ""

    def _notify(session_id: str, job_id: str, text: str) -> None:
        try:
            from graph import sdk  # host import — lazy
        except Exception:  # noqa: BLE001 — no host: the status tool is the only channel
            return
        fn = getattr(sdk, "run_in_session", None)
        if callable(fn):
            fn(session_id, text, job_id=f"campaign-{job_id}")

    def _sdk_fn(name: str):
        try:
            from graph import sdk  # host import — lazy
        except Exception:  # noqa: BLE001 — no host (tests, an in-process driver)
            return None
        fn = getattr(sdk, name, None)
        return fn if callable(fn) else None

    def _artifact_save_file():
        """core's `artifact.save_file` service (protoAgent#4254), resolved at CALL time — None on
        an older core or with the artifact plugin off (the follow-up turn then saves the files)."""
        svc = _sdk_fn("service")
        return svc("artifact.save_file") if svc else None

    def _hooks() -> produce.Hooks:
        save = getattr(registry, "save_media", None)
        return produce.Hooks(
            save_media=save if callable(save) else None,
            notify=_notify,
            emit=_emit,
            save_artifact=_artifact_save_file,
        )

    def _attach(job: dict) -> int:
        """Attach the finished files to THIS turn's answer (core `sdk.attach_file`, #4255): an
        A2A caller gets FileParts, a /v1 caller absolute links + attachments. 0 on older cores."""
        attach = _sdk_fn("attach_file")
        d = job.get("delivery") or {}
        if attach is None or job["status"] != "done":
            return 0
        n = 0
        stem = paths.slug(job["inputs"]["title"], "video")[:40]
        for key, name in (("final", f"{stem}.mp4"), ("gh", f"{stem}-gh.mp4"), ("sheet", f"{stem}-sheet.png")):
            url = (d.get("links") or {}).get(key)
            if not url:
                continue
            try:
                n += 1 if attach(url, name=name) else 0
            except Exception:  # noqa: BLE001 — the links in the text are still the answer
                pass
        return n

    @tool
    def campaign_produce_video(
        script: str,
        title: str,
        quality: str = "2k",
        intro: str = "brand",
        outro: str = "brand",
        subtitle: str = "",
        eyebrow: str = "Architecture walkthrough",
        campaign_id: int = 0,
        caption_region: dict | str = "",
        force: bool = False,
        allow_other_console: bool = False,
        state: Annotated[Any, InjectedState] = None,
    ) -> str:
        """Produce a FINISHED video from one shot script, end to end, as a background job:
        record the take → render [brand intro] + the walkthrough + [brand outro] → self-review →
        deliver. Returns a job id at once; follow it with campaign_video_status(job_id).
        script: the YAML (or the whole reply) from atlas_record_script, or any Campaign shot
        script. title: the video's title (the intro card + file names). quality: 2k (2560 px wide
        device-pixel capture — crisp small text) or 1080p. intro / outro: 'brand' (the operator's
        Brand intro / Brand outro presets — boot-splash bumper fading to a title card that edge-sweeps into
        the first SETTLED frame; final caption ≥ 4 s, bumper, Star-on-GitHub card) or 'none'.
        subtitle / eyebrow: the intro card's other lines. caption_region {x,y,w,h} (device px)
        overrides where the final caption is measured (Atlas footage is detected).
        PREFLIGHT, before any browser starts: the script's base_url must be THIS agent's console
        (allow_other_console=True only when recording another console is intended), its page must
        answer 200, and its bearer_env / storage_state token must pass the console's auth — else
        it refuses at once with what to fix.
        Self-review gates before delivery: resolution, final caption ≥ 4 s, intro ≤ 6 s, no
        blank frames in the walkthrough, the GitHub variant ≤ 10 MB, plus a contact sheet and a
        100 % crop of the smallest text. One automatic retry where it helps; otherwise the job
        ends needs_review with the reasons — never present that as finished. Same inputs again →
        the same job (force=True re-records). Follow the produce-walkthrough-video skill."""
        if (reason := deps.need_browser()) is not None:
            return reason
        if (reason := deps.need_ffmpeg()) is not None:
            return reason
        region = None
        if caption_region not in (None, "", {}):
            try:
                region = {k: int(v) for k, v in _parse_obj(caption_region, "caption_region").items()}
            except (ValueError, AttributeError, TypeError) as e:
                return f"caption_region must be {{x, y, w, h}} in device pixels — {e}"
        try:
            job = produce.start(
                script, title=title, quality=quality, intro=intro, outro=outro, subtitle=subtitle,
                eyebrow=eyebrow, campaign_id=campaign_id, region=region, session_id=_session(state),
                force=force, allow_other_console=allow_other_console, hooks=_hooks(),
            )  # fmt: skip
        except (produce.ProduceError, ValueError) as e:
            return f"Not started — {e}"
        if job.get("reused"):
            return (
                f"REUSED an existing job: this exact request was already made — job {job['id']} is "
                f"{job['status']}; nothing was re-recorded (force=True re-records it). Say so when you reply."
                "\n\n" + produce.summary(job)
            )
        pred = job.get("predicted_s")
        return (
            f"Started video job {job['id']} — {title!r} at {quality}"
            + (f" (~{pred:g} s of footage; recording runs in real time, then ~1–2 min of rendering)" if pred else "")
            + ". It runs in the background: report progress with campaign_video_status(job_id="
            f"{job['id']!r}, wait_s=…). When it ends, this chat gets a follow-up turn to deliver the files."
            + ("\nNotes: " + "; ".join(job["notes"]) if job.get("notes") else "")
        )

    @tool
    def campaign_video_status(job_id: str = "", wait_s: int = 0, verbose: bool = False) -> str:
        """Progress of a campaign_produce_video job: stage (recording → measuring → rendering →
        reviewing → delivering), percent, the self-review gate table (PASS/FAIL each) and, once
        it ends, the files (review mp4, GitHub-sized mp4, contact sheet, 100 % text crop) with
        their asset ids and signed download links. wait_s (≤ 600) blocks until the job ends or
        that many seconds pass — use it from an API/A2A turn that must return the finished file.
        No job_id lists recent jobs. Status 'needs_review' means a gate failed: say so and why —
        never present that video as finished. The reply is compact (one line per gate and file,
        the last 3 log lines); verbose=true adds the whole job log."""
        if not job_id:
            jobs = produce.list_jobs(10)
            if not jobs:
                return "No video jobs yet — start one with campaign_produce_video."
            return "Recent video jobs:\n" + "\n".join(
                f"- {j['id']} {j['inputs']['title']!r}: {j['status']} ({j.get('stage')}, "
                f"{round(produce.progress(j) * 100)}%)"
                for j in jobs
            )
        import time as _time

        deadline = _time.monotonic() + max(0, min(int(wait_s or 0), 600))
        job = produce.load(job_id)
        while job and job["status"] not in produce.TERMINAL and _time.monotonic() < deadline:
            _time.sleep(2)
            job = produce.load(job_id)
        if job is None:
            return f"No video job {job_id!r} — campaign_video_status() lists recent ones."
        out = produce.summary(job, verbose=bool(verbose))
        if (n := _attach(job)) > 0:
            out += f"\n\n{n} file(s) attached to this reply (a remote caller receives them as files)."
        sheet = (job.get("delivery") or {}).get("files", {}).get("sheet")
        if job["status"] in produce.TERMINAL and sheet:
            out += _embed(sheet, "image/png", f"{job['inputs']['title']} — review sheet")
        return out

    return [
        campaign_create,
        campaign_update,
        campaign_list,
        campaign_get,
        campaign_lane,
        campaign_asset_add,
        campaign_asset_update,
        campaign_assets,
        campaign_asset,
        campaign_milestone,
        campaign_decision,
        campaign_status,
        campaign_limits,
        campaign_setup,
        campaign_script_save,
        campaign_script_get,
        campaign_shoot,
        campaign_sandbox_reset,
        campaign_render,
        campaign_card,
        campaign_view,
        campaign_montage,
        campaign_storyboard,
        campaign_produce_video,
        campaign_video_status,
        campaign_prune,
        campaign_archive,
        campaign_collect,
        campaign_backup,
    ]


def _step_span(raw: Any, total: int) -> tuple[int, int] | None:
    """``"20-30"`` / ``"27"`` → (first, last), 1-based and clamped; ``""`` → None (all)."""
    text = str(raw or "").strip()
    if not text:
        return None
    parts = [p.strip() for p in text.replace("–", "-").split("-", 1)]
    try:
        lo, hi = int(parts[0]), int(parts[-1])
    except ValueError:
        raise ValueError(f"steps must be 'N' or 'N-M' (1..{total}), got {raw!r}") from None
    lo, hi = max(1, min(lo, hi)), min(total, max(lo, hi))
    if lo > total:
        raise ValueError(f"steps {raw!r} is past the end — the script has {total} steps")
    return lo, hi


def _script_yaml(body: dict[str, Any], span: tuple[int, int] | None = None) -> str:
    """A saved script as YAML that round-trips (``shotscript.parse`` reads it back to ``body``),
    each step preceded by a ``# step N`` comment. With ``span``, only those steps (an excerpt —
    it doesn't round-trip)."""
    import yaml

    def dump(obj: Any, flow: bool | None = None) -> str:
        return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=100, default_flow_style=flow)

    # Top-level keys in block style; each step's body inline-where-small, as scripts are written.
    out = "".join(
        dump({k: v}, flow=None if isinstance(v, (dict, list)) else False) for k, v in body.items() if k != "steps"
    )
    steps = body.get("steps")
    if isinstance(steps, list):
        out += "steps:\n"
        for i, st in enumerate(steps, start=1):
            if span and not span[0] <= i <= span[1]:
                continue
            nested = isinstance(st, dict) and any(isinstance(v, (dict, list)) for v in st.values())
            item = dump([st], flow=None if nested else False).rstrip("\n").replace("\n", "\n  ")
            out += f"  # step {i}\n  {item}\n"
    elif steps is not None:
        out += dump({"steps": steps})
    return out


def _multimodal_fn():
    """Core's ``multimodal_tool_result`` (graph.sdk, #1930), or None on a core without it.

    A host import, so it stays lazy — the suite and older cores run without it."""
    try:
        from graph import sdk
    except Exception:  # noqa: BLE001 — no host (tests) or a broken import: no vision path
        return None
    fn = getattr(sdk, "multimodal_tool_result", None)
    return fn if callable(fn) else None


def _bookend_render(
    c: dict[str, Any],
    a: dict[str, Any],
    spec: dict[str, Any],
    r: dict[str, Any],
    out_dir: Path,
    size_notes: list[str] | None = None,
) -> str:
    """Wrap a freshly rendered mp4 in the brand's bookends (protoLabs bookends v1) — a montage of
    [intro, the clip, outro] at the clip's own size and the render's own crf (deliverable quality,
    16, unless the spec chose one) — and point the asset at it. Returns the new path, '' when the
    brand has no bookends, or why it couldn't (the plain render is kept). What a size ceiling cost
    the bookended file is appended to ``size_notes``."""
    if not (intromod.preset() and outromod.preset()):
        return ""
    title = spec["title"] or a["title"]
    opts = {"name": f"{spec['name']}-bookends", "title": title, "size": f"{r['width'] // 2 * 2}x{r['height'] // 2 * 2}",
            "fps": spec["fps"] or 30, "poster": False, "limit": spec["limit"] or "", "max_bytes": spec["max_bytes"],
            "crf": spec["crf"]}  # fmt: skip
    try:
        m = montage.render_montage(c["id"], [{"clip": a["id"]}], opts, out_dir / "bookends")
    except (montage.MontageError, render.RenderError) as e:
        return f"bookends skipped — {e}"
    if size_notes is not None:
        size_notes += [w for w in m.get("warnings") or [] if w.startswith(("quality traded", "kept deliverable"))]
    store.update_asset(a["id"], path=m["path"], size_bytes=m["size_bytes"], width=m["width"], height=m["height"],
                       duration_s=m["duration_s"], meta={**(a.get("meta") or {}), "bookends": montage.BOOKENDS_RECIPE,
                       "unbookended": r["path"]})  # fmt: skip
    return m["path"]


def _video_problem(path: str) -> str | None:
    """Why a recorded video isn't usable — missing, or unreadable by ffprobe — or None.
    Without ffprobe there's no way to tell, so it isn't flagged here (render will be)."""
    if not path or not Path(path).is_file():
        return "no video file was written"
    if not deps.ffprobe():
        return None
    try:
        info = render.probe(path)
    except Exception as e:  # noqa: BLE001
        return f"ffprobe: {e}"
    if not info.get("width") or not info.get("duration_s"):
        return "ffprobe finds no video frames in it"
    return None


def _file_facts(path: str) -> dict[str, Any]:
    """size/dims/duration for a file — ffprobe when available, size alone otherwise."""
    p = Path(path)
    if not p.is_file():
        return {}
    facts: dict[str, Any] = {"size_bytes": p.stat().st_size}
    if deps.ffprobe():
        try:
            info = render.probe(p)
            facts.update(width=info["width"], height=info["height"], duration_s=info["duration_s"])
        except Exception:  # noqa: BLE001 — size alone is still useful
            pass
    elif p.suffix.lower() == ".png":
        import struct

        with p.open("rb") as fh:
            head = fh.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", head[16:24])
            facts.update(width=w, height=h)
    return facts
