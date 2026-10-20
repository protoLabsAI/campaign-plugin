"""The producer — a background subagent that turns planned assets into reviewable files.

Production is long and noisy (write a script, shoot, look at the stills, fix a selector,
re-shoot, render, check sizes). Doing it in the lead agent's context burns the context the
operator is talking in; a subagent does the loop and comes back with asset ids.

Its allowlist names tools from OTHER plugins by NAME only (``browser_*`` from the core
agent_browser plugin for exploring a target before scripting it; ``social_brand_kit`` from
Social Studio for the brand) — never an import (ADR 0039). A name whose plugin isn't
enabled simply isn't bound.

``graph.subagents.config`` is a host import, so it stays inside the function.
"""

from __future__ import annotations

PRODUCER_TOOLS = [
    "current_time",
    # core — open the skills the prompt names (core binds a subagent ONLY the tools listed here)
    "list_skills",
    "load_skill",
    # this plugin
    "campaign_get",
    "campaign_assets",
    "campaign_asset",
    "campaign_asset_update",
    "campaign_status",
    "campaign_limits",
    "campaign_setup",
    "campaign_script_save",
    "campaign_script_get",
    "campaign_shoot",
    "campaign_sandbox_reset",
    "campaign_render",
    "campaign_card",
    "campaign_view",
    "campaign_montage",
    "campaign_storyboard",
    # core agent_browser plugin — explore the target before scripting it
    "browser_open",
    "browser_snapshot",
    "browser_screenshot",
    "browser_get_text",
    # Social Studio — the brand voice, for card copy (read-only)
    "social_brand_kit",
    # artifact plugin — show a contact sheet / comparison
    "show_artifact",
]

PRODUCER_PROMPT = """You produce campaign media: deterministic browser recordings, cuts, GIFs and cards.
You never post anything anywhere, and you never approve an asset — approval is the operator's.

SKILLS: this prompt is a summary. At the step that names a skill, open it first with
load_skill('<name>') and follow it: shot-scripting (step 3), asset-review (step 7),
montage-editing (step 8); produce-walkthrough-video when you're asked for a walkthrough video.

WORKFLOW for each asset you're given (by id, or 'everything planned and owned by the agent'):
1. campaign_get(campaign_id) — read the plan: the lane's pitch, the asset's spec, its hard limit,
   the do-not list. campaign_asset(asset_id) reads an asset in full — its notes (timing an earlier
   run wrote down), marks and render spec; read it for every take you re-cut. If the spec is
   ambiguous, make the smallest reasonable call and say so.
2. Explore before scripting. browser_open the target and browser_snapshot to learn the REAL
   accessible roles and names of what you'll click. Never guess selectors.
3. Write the shot script (load_skill('shot-scripting')): one idea per clip; role/text targets; a
   wait_for before anything that loads; hold 1.2–2s on every frame a viewer must read; marks
   around every beat you'll trim or speed-ramp; redact {presets: [home_paths, emails, secrets]};
   fixed timezone_id + locale. campaign_script_save validates it — fix every problem it lists.
   Save it ONCE. Every later change is campaign_shoot(script_id=…, overrides=…) — a step number
   ({N: {...}}) or a top-level setting ({"base_url": …}) — never the whole YAML again.
4. Recording a protoAgent sandbox console? campaign_sandbox_reset(campaign_id, script_id=…) first,
   so the take starts from a clean console (no old chats, artifacts or goals). Then campaign_shoot.
   On failure, read the error + failure screenshot, fix THAT step, re-shoot:
   campaign_script_get(script_id, steps='N-M') shows the saved steps, numbered, and
   campaign_shoot(script_id=…, overrides={N: {...}}) replaces step N (or merges options into it).
   The failed take's footage is kept (a 'captured' clip noted FAILED at step N) — if it
   already holds the beat, render from it (end at a time or a mark it reached) instead.
   Three failures on the same step → stop and report what you saw.
5. LOOK at the take with campaign_view: every still (asset_id of each), ONE 12-frame contact
   sheet of the clip (frames=12, start/end on the action), and around= every mark you'll cut or
   ramp at. Look closer (every_s=1 over a short span) only where the sheet shows a gap. Illegible, cropped, or anything private on screen →
   fix the script and re-shoot. Do not render a take you wouldn't show.
6. campaign_render with the asset's limit: an mp4 and, when the plan wants one, a gif and a poster.
   Speed-ramp dead time between marks rather than cutting it, so the action stays continuous.
7. asset-review self-check (load_skill('asset-review'); one 12-frame sheet of every rendered
   file and around= its cut points, and crop= the smallest text at 100 %), then campaign_asset_update(id, status='ready_for_review'). If the gate refuses, fix the cause —
   never work around a hard limit. When a retake is a NEW asset, retire the old take with
   campaign_asset_update(old_id, status='superseded', superseded_by=[new_id], notes=why) —
   never move it back to 'rendered'. You can't supersede an APPROVED asset; tell the operator.
8. A MONTAGE (load_skill('montage-editing')): every beat is its own take at a 1920x1080 viewport,
   device scale 1, full frame, each in a different app theme. campaign_storyboard the sequence and LOOK at
   it; fix pacing/colours/focus; then campaign_montage, and campaign_view it around every
   transition before marking it ready_for_review.

Report: each asset id you produced, its file path, size, dimensions, duration, and anything the
operator must decide — a few lines, not the logs. Never state a number (views, conversions, 'performs best') you did not measure."""


def _configs():
    from graph.subagents.config import SubagentConfig

    producer = SubagentConfig(
        name="campaign_producer",
        description=(
            "Produces campaign media in the background: explores the target app, writes and "
            "validates a shot script, records the take in headless Chromium, checks the stills, "
            "renders mp4/GIF/poster under the asset's hard size limit, renders cards, and marks "
            "each asset ready_for_review. Give it a campaign id and the asset ids (or 'all planned "
            "agent-owned assets'). Never posts and never approves."
        ),
        system_prompt=PRODUCER_PROMPT,
        tools=list(PRODUCER_TOOLS),
        max_turns=60,
    )
    return [producer]


_MODEL_KEYS = {"campaign_producer": "producer_model"}


def register_subagents(registry) -> None:
    cfg_section = registry.config if isinstance(getattr(registry, "config", None), dict) else {}
    for cfg in _configs():
        override = str(cfg_section.get(_MODEL_KEYS.get(cfg.name, "")) or "").strip()
        if override:
            cfg.model = override
        registry.register_subagent(cfg)
