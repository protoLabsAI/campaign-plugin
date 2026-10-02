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
from typing import Any

from langchain_core.tools import tool

from . import cards, deps, limits, look, montage, paths, render, shoot, shotscript, store

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
        raise ValueError(f"no asset #{asset_id} in campaign {campaign_id} — campaign_assets lists them")
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
    stills = [
        s
        for s in store.list_assets(old["campaign_id"], kind="still")
        if s["parent_id"] == old["id"] and s.get("path") and old_dir and Path(s["path"]).parent == Path(old_dir)
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
    if old_dir and not kept and Path(old_dir) != Path(new_take_dir) and paths.is_contained(old_dir):
        still_used = any(
            a.get("path") and Path(a["path"]).parent == Path(old_dir) for a in store.list_assets(old["campaign_id"])
        )
        if not still_used:
            import shutil

            shutil.rmtree(old_dir, ignore_errors=True)
    return notes


def _asset_line(a: dict[str, Any]) -> str:
    dims = f" {a['width']}×{a['height']}" if a.get("width") else ""
    dur = f" {a['duration_s']:.1f}s" if a.get("duration_s") else ""
    size = f" {limits.human_bytes(a['size_bytes'])}" if a.get("size_bytes") else ""
    lim = f" limit={a['limit_id']}" if a.get("limit_id") else ""
    path = f"\n    {a['path']}" if a.get("path") else ""
    sup = ""
    if a.get("status") == "superseded":
        by = a.get("superseded_by") or []
        sup = " → replaced by " + ", ".join(f"#{i}" for i in by) if by else ""
    return (
        f"- #{a['id']} [{a['status']}] {a['kind']} — {a['title'] or '(untitled)'} (owner: {a['owner']})"
        f"{sup}{dims}{dur}{size}{lim}{path}"
    )


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
    ) -> str:
        """Move an asset through production or edit it: status planned → scripted → captured →
        rendered → ready_for_review. Mark ready_for_review only after the asset-review skill's
        self-check; the move is REFUSED if the file is missing or breaks its hard limit. You can
        never set approved or rejected — that is the operator's call in the gallery. Pass path
        to attach a file the operator recorded (it must be inside the campaign's media dir).
        status='superseded' retires a take a retake REPLACED: it leaves the operator's review
        queue and the counts without being approved or rejected, and montages/storyboards refuse
        it. Pass superseded_by=[new asset ids] and notes saying why. Allowed from any status
        except approved — an APPROVED asset can only be superseded by the operator."""
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
            a = store.update_asset(asset_id, **fields)
        except ValueError as e:
            return f"Not updated — {e}"
        _emit("asset_registered", {"campaign_id": a["campaign_id"], "asset_id": a["id"], "status": a["status"]})
        return "Updated:\n" + _asset_line(a)

    @tool
    def campaign_assets(
        campaign_id: int, status: str = "", lane: str = "", kind: str = "", include_superseded: bool = False
    ) -> str:
        """List a campaign's assets with status, owner, size, dimensions, duration and file path,
        optionally filtered by status, lane name, or kind. Superseded takes are left out unless
        include_superseded is true or status='superseded'."""
        try:
            rows = store.list_assets(campaign_id, status=status, lane_id=_lane_id(campaign_id, lane), kind=kind)
        except ValueError as e:
            return str(e)
        hidden = 0
        if not include_superseded and status != "superseded":
            hidden = sum(1 for a in rows if store.is_retired(a))
            rows = [a for a in rows if not store.is_retired(a)]
        out = "\n".join(_asset_line(a) for a in rows) or "No assets match."
        if hidden:
            out += f"\n({hidden} superseded take(s) hidden — include_superseded=true lists them)"
        return out

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

    # ── looking at the media ─────────────────────────────────────────────────
    @tool
    def campaign_view(
        campaign_id: int,
        asset_id: int = 0,
        path: str = "",
        frames: int = 3,
        around: str = "",
        max_side: int = 1280,
    ) -> str:
        """LOOK at a campaign file yourself — the image comes back to you as a picture, not a
        path. Pass asset_id (from campaign_assets) or a path inside the campaign's media dir.
        A still, card or poster comes back downscaled (long side <= max_side, 320-2048). A clip
        or GIF comes back as `frames` evenly spaced frames (1-3: one image each at full size;
        4-12: one contact sheet), and around=<mark name or seconds> adds the frame just before
        and just after that moment — use it on every cut, speed-ramp boundary and mark. At most
        3 images per call; call again for more. This is how the asset-review self-check is done:
        read every still and the cut frames for secrets, home paths, emails and usernames on
        screen (terminal and canvas text too), clutter, the wrong theme, and legibility at half
        size."""
        try:
            res = look.view(campaign_id, asset_id, path, frames=frames, around=around, max_side=max_side)
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
        """Validate and save a shot script (YAML or JSON) to a campaign, so a take can be
        re-recorded identically later. Validation reports every problem with its step number
        (unknown step, missing target, bad timezone…). Pass script='template' to get an
        annotated example to adapt. Follow the shot-scripting skill: prefer role/text targets
        over CSS classes, hold frames long enough to read, redact secrets, fix the timezone."""
        if script.strip().lower() == "template":
            return "Shot-script template (adapt it, then save):\n```yaml\n" + shotscript.example() + "\n```"
        try:
            data = shotscript.parse(script)
            if name:
                data["name"] = name
            norm = shotscript.validate(data)
            row = store.save_script(campaign_id, norm["name"], data)
        except shotscript.ScriptError as e:
            return "Not saved — fix these and resend:\n" + "\n".join(f"- {p}" for p in e.problems)
        except ValueError as e:
            return f"Not saved — {e}"
        return (
            f"Saved shot script #{row['id']} {norm['name']!r} ({len(norm['steps'])} steps). "
            f"Record it with campaign_shoot(campaign_id={campaign_id}, script_id={row['id']})."
        )

    @tool
    def campaign_shoot(
        campaign_id: int, script_id: int = 0, script: str = "", asset_id: int = 0, title: str = "", lane: str = ""
    ) -> str:
        """Record a take: run a shot script in headless Chromium (Playwright) with video on,
        and register the .webm, the named stills, and a timing log of every mark as campaign
        assets (status 'captured'). Pass script_id for a saved script, or script (YAML/JSON) to
        validate, save and run in one go. Pass asset_id to fill a planned asset with this take.
        On a failed step you get the step number, the error, and a screenshot of the page at
        that moment. Deterministic: same script, same take."""
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
            norm = shotscript.validate(data)
            if not script_id:
                script_id = store.save_script(campaign_id, norm["name"], data)["id"]
            lane_id = _lane_id(campaign_id, lane)
            previous = _own_asset(campaign_id, asset_id) if asset_id else None
        except shotscript.ScriptError as e:
            return "Script is invalid — nothing was recorded:\n" + "\n".join(f"- {p}" for p in e.problems)
        except ValueError as e:
            return str(e)

        c = store.require_campaign(campaign_id)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        out_dir = paths.unique_dir(paths.campaign_dir(campaign_id, c["name"]) / "shots", f"{norm['name']}-{stamp}")
        try:
            res = shoot.run(norm, out_dir)
        except shoot.ShootError as e:
            r = e.result
            tail = "\n".join(
                f"  {s['index']}. {s['desc']} @{s.get('t_start', 0):.1f}s"
                + (f" — ERROR {s['error']}" if s.get("error") else "")
                for s in r.get("steps", [])[-6:]
            )
            msg = f"Take failed — {e}\n\nLast steps:\n{tail or '  (none ran)'}"
            if r.get("failure_png"):
                msg += f"\n\nScreenshot at failure: {r['failure_png']}" + _embed(
                    r["failure_png"], "image/png", "failure"
                )
            msg += "\n\nFix the step (a role/text target is sturdier than CSS; add a wait_for before it) and re-shoot."
            return msg
        except Exception as e:  # noqa: BLE001 — a browser launch failure etc. should read clearly
            log.exception("[campaign] shoot failed")
            return f"The browser couldn't run: {e}\n\n{deps.brief()}"

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
            f"{limits.human_bytes(take['size_bytes'])})\n"
            f"- video: {res['video']}\n- timing log: {res['timing']}\n- marks: {marks}\n"
            f"- stills: {', '.join(f'#{i}' for i in still_ids) or 'none'}\n"
            + "".join(f"- {n}\n" for n in superseded)
            + "\n"
            "LOOK at it before rendering — campaign_view each still (asset_id) and the clip around its marks "
            "(legible? anything secret on screen?) — then cut it with "
            f"campaign_render(asset_id={take['id']}, outputs=[…])."
        )
        first = next(iter(res["screenshots"].values()), "")
        if first:
            out += _embed(first, "image/png", f"{norm['name']} still")
        return out

    # ── render ───────────────────────────────────────────────────────────────
    @tool
    def campaign_render(asset_id: int, outputs: list[dict] | str) -> str:
        """Cut a recorded take into shareable files with ffmpeg and register each as an asset
        (status 'rendered'). outputs is a list; each item has name, format (mp4 | gif | poster),
        start/end (seconds or a mark name from the take), optional speed ramps
        [{from, to, factor}] to fast-forward dead time (the clip is continuous — no interior
        cuts — and a ramp above 4x is flagged as a jump cut unless continuous: false), optional crop {x,y,width,height} (source
        px), width, fps, and limit (a hard-limit id such as github_attachment_video_free) and/or
        max_bytes. mp4 is H.264/yuv420p/faststart; when a size ceiling is set, mp4 steps CRF then
        width and gif steps fps then width until it fits. Reports final size, dimensions and
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
                lines.append(f"- {raw.get('name', '?') if isinstance(raw, dict) else raw}: FAILED — {e}")
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
            flag = f"  ⚠ {'; '.join(r['violations'])}" if r["violations"] else ""
            flag += f"  ⚠ continuity: {'; '.join(r['warnings'])}" if r["warnings"] else ""
            lines.append(
                f"- #{a['id']} {spec['name']}.{Path(r['path']).suffix.lstrip('.')} — {limits.human_bytes(r['size_bytes'])}, "
                f"{r['width']}×{r['height']}"
                + (f", {r['duration_s']:.1f}s" if r["duration_s"] else "")
                + f" ({len(r['attempts'])} attempt{'s' if len(r['attempts']) != 1 else ''}){flag}\n    {r['path']}"
            )
        if made and take["status"] in ("planned", "scripted", "captured"):
            store.update_asset(take["id"], status="rendered")
        _emit("render_finished", {"campaign_id": c["id"], "source_asset_id": take["id"], "assets": made})
        return (
            "Rendered:\n"
            + "\n".join(lines)
            + (
                "\n\nRun the asset-review self-check — campaign_view each file and its cut points — then "
                "campaign_asset_update(…, status='ready_for_review')."
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
        focus?: {x,y,w,h} source px, transition?: style | {style, duration}} or
        {card: {title, subtitle?, eyebrow?, url?, cta?, bg?, fg?, accent?, logo?: bool}, duration: s,
        zoom?: bool, theme_color?, transition?}. A clip is ONE continuous stretch: in/out trim only
        its ends, speed is one uniform factor (no ramps, no interior cuts). Transitions (into an
        item): colorwipe (default — a full-frame bar in the NEXT item's theme_color sweeps across
        and reveals it; also colorwipe_left/_up/_down), cut, or an xfade style (fade, wipeleft,
        slideleft, smoothleft, circleopen, …). output (mapping, or just a preset name): name,
        preset landscape 1920x1080 (canonical — 1920x1080 clips pass through untouched) |
        vertical 1080x1920 | square 1080x1080, fit auto|letterbox|crop, fps, transition,
        transition_duration (0.45), limit (a hard-limit id — duration and size are enforced),
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

    return [
        campaign_create,
        campaign_update,
        campaign_list,
        campaign_get,
        campaign_lane,
        campaign_asset_add,
        campaign_asset_update,
        campaign_assets,
        campaign_milestone,
        campaign_decision,
        campaign_status,
        campaign_limits,
        campaign_setup,
        campaign_script_save,
        campaign_shoot,
        campaign_render,
        campaign_card,
        campaign_view,
        campaign_montage,
        campaign_storyboard,
    ]


def _multimodal_fn():
    """Core's ``multimodal_tool_result`` (graph.sdk, #1930), or None on a core without it.

    A host import, so it stays lazy — the suite and older cores run without it."""
    try:
        from graph import sdk
    except Exception:  # noqa: BLE001 — no host (tests) or a broken import: no vision path
        return None
    fn = getattr(sdk, "multimodal_tool_result", None)
    return fn if callable(fn) else None


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
