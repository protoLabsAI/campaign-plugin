"""campaign — Campaign Studio: campaign plans + deterministic, repeatable media production.

The production + planning half of a "Brand & Launch" agent, for ANY app or product: a plan
store (campaigns, lanes, a shot list of assets, milestones, operator decisions), declarative
shot scripts recorded by headless Chromium, ffmpeg cuts under hard size limits, and branded
cards. Copy belongs to Social Studio (``social``) and browser exploration to the core
``agent_browser`` plugin — both referenced by tool NAME only, never imported (ADR 0039).

Draft-only: nothing here posts to a social platform or holds a platform credential, and only
the operator approves an asset. Its one optional credential is the operator's GitHub token
(``archive_token``) for backing approved media up to protoContent (``archive.py``).

``register(registry)`` is the only place plugin code runs (ADR 0018). Host-only imports stay
inside functions so the suite runs with no protoAgent, and each contribution group is
wrapped so one failure doesn't take the rest down.
"""

from __future__ import annotations

import logging

log = logging.getLogger("protoagent.plugins.campaign")

__version__ = "0.4.3"


def _host_store(registry) -> str:
    """The host's instance-scoped plugin dir (sdk.plugin_store), or '' on older hosts / tests."""
    try:
        from graph import sdk  # host import — lazy

        return str(sdk.plugin_store(plugin_id=getattr(registry, "plugin_id", None) or "campaign"))
    except Exception:  # noqa: BLE001 — fall back to paths.py's own default
        return ""


def register(registry) -> None:
    # Imported here, not at module top: pytest imports a rootdir __init__.py without a parent
    # package, where a top-level relative import can't resolve.
    from . import brand, deps, limits, paths, shoot

    cfg = registry.config or {}

    # Paths, limits, brand, ffmpeg — before anything reads them.
    try:
        paths.configure(str(cfg.get("data_dir", "") or ""), _host_store(registry))
        for w in limits.configure(cfg.get("limit_overrides", "")):
            log.warning("[campaign] %s", w)
        deps.configure(str(cfg.get("ffmpeg_path", "") or ""), str(cfg.get("interpreter", "") or ""))
        shoot.configure(cfg.get("bearer_envs", ""), cfg.get("upload_dirs", ""))

        # registry.host's services are populated by the server AFTER register() runs, so
        # resolve host.config at call time rather than capturing a None now.
        def _host_config():
            fn = getattr(getattr(registry, "host", None), "config", None)
            return fn() if callable(fn) else None

        brand.configure(cfg, _host_config)
        from . import outro

        for w in outro.configure(cfg.get("outro", "")):
            log.warning("[campaign] %s", w)
        from . import intro  # after the outro: an intro may reuse its bumper (`bumper: outro`)

        for w in intro.configure(cfg.get("intro", "")):
            log.warning("[campaign] %s", w)
    except Exception:
        log.exception("[campaign] configuring failed")

    # Setup: the Install Chromium + Check again buttons (registered BEFORE the first report so
    # the banner's buttons already work), then the probe → setup-gap banners. Never installs on
    # its own. Probing spawns the candidate interpreter once (stdlib-only, no browser).
    def _refresh() -> None:
        deps.report(registry)

    add_step = getattr(registry, "register_setup_step", None)
    if callable(add_step):
        try:
            add_step(deps.STEP_INSTALL_CHROMIUM, lambda: deps.install_chromium(_refresh))
            add_step(deps.STEP_RECHECK, lambda: deps.recheck(_refresh))
        except Exception:
            log.exception("[campaign] registering the setup step failed")
    try:
        probe = deps.report(registry)
        log.info(
            "[campaign] setup: worker python=%s (%s) playwright=%s chromium=%s ffmpeg=%s",
            probe["python"] or "-",
            probe["python_source"] or "none",
            probe["playwright"],
            probe["chromium"],
            probe["ffmpeg"],
        )
    except Exception:
        log.exception("[campaign] setup probe failed")

    try:
        from .tools import build_tools

        registry.register_tools(build_tools(registry))
    except Exception:
        log.exception("[campaign] registering tools failed")

    try:
        from .subagents import register_subagents

        register_subagents(registry)
    except Exception:
        log.exception("[campaign] registering subagents failed")

    try:
        from .api import build_data_router, build_view_router

        registry.register_router(build_view_router(), prefix="/plugins/campaign")
        registry.register_router(build_data_router(getattr(registry, "emit", None)), prefix="/api/plugins/campaign")
    except Exception:
        log.exception("[campaign] mounting routers failed")
    try:  # the protoContent archive's operator routes — their own router, same gated prefix
        from .archive_api import build_archive_router

        registry.register_router(build_archive_router(), prefix="/api/plugins/campaign")
    except Exception:
        log.exception("[campaign] mounting the archive routes failed")

    # The library surface: the daily retention pass (report-only until the operator flips
    # retention_mode). Module-level start/stop, so a config reload's re-register is the SAME
    # surface; config is read live each tick.
    try:
        from . import surface

        def _live_cfg():  # the ONE live-config reader: tools, routes and the surface all use it
            fn = getattr(registry, "live_config", None)
            live = fn() if callable(fn) else None
            return live if isinstance(live, dict) else (registry.config or {})

        surface.configure(_live_cfg)
        add_surface = getattr(registry, "register_surface", None)
        if callable(add_surface):
            add_surface(surface.start, stop=surface.stop, name="campaign-library", reload=surface.reload)
    except Exception:
        log.exception("[campaign] registering the library surface failed")

    # skills/ is auto-discovered (ADR 0027) — no register call.
    log.info("[campaign] registered: tools, producer, gallery, library surface")
