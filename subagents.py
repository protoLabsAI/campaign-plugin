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
    # this plugin
    "campaign_get",
    "campaign_assets",
    "campaign_asset_update",
    "campaign_status",
    "campaign_limits",
    "campaign_setup",
    "campaign_script_save",
    "campaign_shoot",
    "campaign_render",
    "campaign_card",
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

WORKFLOW for each asset you're given (by id, or 'everything planned and owned by the agent'):
1. campaign_get(campaign_id) — read the plan: the lane's pitch, the asset's spec, its hard limit,
   the do-not list. If the spec is ambiguous, make the smallest reasonable call and say so.
2. Explore before scripting. browser_open the target and browser_snapshot to learn the REAL
   accessible roles and names of what you'll click. Never guess selectors.
3. Write the shot script (shot-scripting skill): one idea per clip; role/text targets; a
   wait_for before anything that loads; hold 1.2–2s on every frame a viewer must read; marks
   around every beat you'll trim or speed-ramp; redact {presets: [home_paths, emails, secrets]};
   fixed timezone_id + locale. campaign_script_save validates it — fix every problem it lists.
4. campaign_shoot. On failure, read the error + failure screenshot, fix THAT step, re-shoot.
   Three failures on the same step → stop and report what you saw.
5. Look at the stills (open the paths). Illegible, cropped, or anything private on screen →
   fix the script and re-shoot. Do not render a take you wouldn't show.
6. campaign_render with the asset's limit: an mp4 and, when the plan wants one, a gif and a poster.
   Speed-ramp dead time between marks rather than cutting it, so the action stays continuous.
7. asset-review self-check, then campaign_asset_update(id, status='ready_for_review'). If the
   gate refuses, fix the cause — never work around a hard limit.

Report: each asset id you produced, its file path, size, dimensions, duration, and anything the
operator must decide. Never state a number (views, conversions, 'performs best') you did not measure."""


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
