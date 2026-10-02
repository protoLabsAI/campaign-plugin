# Campaign Studio — a protoAgent plugin

Campaign plans and **deterministic, repeatable media production** for launching any app or
product: the planning + production half of a "Brand & Launch" agent.

- **Plans** — a campaign's goal and the math behind its target, lanes (one angle per audience),
  a shot list of assets with *who records what*, a dated schedule, a channel plan, a do-not
  list, and the decisions only the operator can make. `campaign_status` splits every open
  item into *blocked on the operator* vs *on the agent*, shaped for `show_component`.
- **Shot scripts → takes** — a declarative YAML script (viewport, colour scheme, timezone,
  locale, auth, steps) recorded by headless Chromium through Playwright: `.webm` + named
  stills + a timing log of every `mark`. Same script, same take. Redaction built in.
- **Renders** — ffmpeg cuts by mark, speed-ramps dead time, crops, and encodes H.264 mp4
  (yuv420p, faststart) and palette-optimised GIFs, stepping quality down until the file fits
  its **hard** limit — and refusing to call it ready if it still doesn't.
- **Cards** — branded HTML templates rendered to PNG: `og-1280x640` (kept under GitHub's 1 MB
  social-preview limit), `x-card-1600x900`, `square-1080`, `title-slide-1920x1080`.
- **Gallery** — one console view, for browsing: every clip/GIF/still/card of a campaign,
  playable, with status, size, dimensions, and the operator-only **Approve / Reject** buttons.

**Draft-only.** It never posts anywhere and holds no platform credentials. Every asset has an
approval state and only the operator approves (in the gallery — there is no agent tool for it).

**What it is not.** Copy is the [Social Studio](https://github.com/protoLabsAI/social-plugin)
plugin's job (brand kit, queue, linter, disclosure, export); browsing a site is core's
`agent_browser`. Both are referenced by tool *name* (in skills and the producer's allowlist),
never imported. Cards read Social Studio's brand-kit YAML *file* for colours/fonts/logo if it
exists — its `visual:` section (`colors: {primary, accent, background, foreground}`,
`fonts: {heading, body}`, `logo: {path, dark, light}` relative to the kit file, `wordmark`).
Missing or malformed fields fall back to this plugin's brand settings, then neutral defaults;
`logo.dark` is used on a dark card and `logo.light` on a light one; font `*_url`s are never
fetched (cards load nothing from the network).

**Playwright runs out of process.** The agent's own process never imports playwright. Every
take and card is rendered by `worker/pw_worker.py`, a small self-contained script run under a
Python that *has* playwright, picked in this order:

1. the **Browser worker Python** setting (`interpreter`) when set — strict: a bad value is
   reported, never silently replaced;
2. the desktop app's **managed Python runtime** (Settings ▸ Tools) — where *Install
   dependencies* puts playwright on the desktop app (the dep is declared `scope: runtime`);
3. the agent's **own Python**, on a source/venv install (never a frozen app's binary).

The job (and any bearer) goes to the worker on stdin — never argv; the worker gets an
allowlisted environment (no host credentials); its output is bounded; and if it overruns, its
whole process tree — Node driver and Chromium included — is killed. Chromium lives in
Playwright's default machine-wide cache (`~/Library/Caches/ms-playwright`,
`~/.cache/ms-playwright`, `%LOCALAPPDATA%\ms-playwright`), or `PLAYWRIGHT_BROWSERS_PATH` if
you set it; *Install Chromium* uses the same interpreter as the worker, so they always agree.

**The recording browser's guards** (enforced inside the worker). A shot script is
agent-written, so the browser it drives never reaches this plugin's own data API (on any host —
it can't open the gallery and click Approve), and a script's bearer token goes only to its
`base_url` origin, read only from an env var named `CAMPAIGN_*` or listed in the **Shot-script
bearer env vars** setting. The host's own operator/fleet token is always refused. A card page
loads nothing from the network at all.

## Quick start

1. **Install** (pin a tag): Settings ▸ Plugins ▸ Install from URL →
   `https://github.com/protoLabsAI/campaign-plugin`, ref `v0.2.4`. Or
   `python -m server plugin install https://github.com/protoLabsAI/campaign-plugin --ref v0.2.4`.
2. **Enable** it (`plugins.enabled: [campaign]`). It ships disabled.
3. **Set up media** — the setup banner walks you through it:
   - **Desktop app only:** provision the **Python runtime** first (Settings ▸ Tools, ~35 MB) —
     that's where playwright runs. The banner says so, with a button to get there.
   - **Install dependencies** installs the `playwright` package — into the managed runtime on
     the desktop app, into the agent's venv on a source install.
   - **Install Chromium** downloads Playwright's headless Chromium (~150 MB) with that same
     Python. Only ever from that button — never as a side effect of a tool call.
   - **Check again** re-probes after a fix made elsewhere (any media tool call does too).
   - **ffmpeg** is a system binary: `brew install ffmpeg` / `sudo apt install ffmpeg` /
     `winget install Gyan.FFmpeg`, or set **ffmpeg path**.
   Planning works without any of these; the media tools say exactly what's missing.
4. Ask the agent: *"Plan a launch campaign for <product> at <url>"* — then *"produce the
   agent-owned assets"* (it hands that to the `campaign_producer` subagent), then open the
   **Campaign Studio** rail view to approve or reject what came back.

## Tools

| Tool | What it does |
|---|---|
| `campaign_create` / `campaign_update` / `campaign_list` / `campaign_get` | The plan: goal, target metric, launch window, target URL, goal math, sourced assumptions, channel plan, do-not list |
| `campaign_lane` | A lane: name, pitch, audience, hero asset |
| `campaign_asset_add` / `campaign_asset_update` / `campaign_assets` | The shot list. Status `planned → scripted → captured → rendered → ready_for_review`; `ready_for_review` is refused if the file is missing or breaks its hard limit; `approved`/`rejected` are refused outright |
| `campaign_milestone` / `campaign_decision` | Dated milestones (owner agent/operator); operator decisions with options + a recommendation |
| `campaign_status` | Progress + who each open item is waiting on; includes `show_component` payloads |
| `campaign_script_save` | Validate + save a shot script (`script="template"` returns an annotated example) |
| `campaign_shoot` | Record a take (Playwright, headless Chromium, `record_video`) and register the webm + stills |
| `campaign_render` | mp4 / gif / poster outputs via ffmpeg, under a hard limit |
| `campaign_card` | Render a branded card template to PNG |
| `campaign_view` | LOOK at a still/card/poster, or frames of a clip (evenly spaced, or either side of a mark/cut), as images the model sees — downscaled JPEGs, ≤ 3 per call, contained to the campaign's dir. Needs a core with `graph.sdk.multimodal_tool_result` and a vision model; otherwise it says it couldn't show them |
| `campaign_limits` | The hard-limit table, each row with its source URL and as-of date |
| `campaign_setup` | What's installed and how to fix what isn't |

Plus: skills `campaign-planning`, `shot-scripting`, `asset-review`; the `campaign_producer`
subagent (allowlist: `campaign_*`, `browser_open/snapshot/screenshot/get_text`,
`social_brand_kit`, `show_artifact`).

## A shot script

```yaml
name: install-from-url
base_url: http://localhost:7871
viewport: {width: 1440, height: 900}
device_scale_factor: 2          # sharpens stills; the VIDEO records at CSS pixels
color_scheme: dark
timezone_id: UTC
locale: en-US
redact: {presets: [home_paths, emails, secrets]}
mask: {selectors: ['[role="alert"]'], mode: remove}   # blur | hide | remove
steps:
  - goto: /app/
  - wait_for: {role: button, name: Settings, exact: true}   # not network_idle: the console streams (SSE)
  - mark: start
  - click: {role: button, name: Settings, exact: true}
  - click: {role: tab, name: Plugins}
  - wait_for: {role: button, name: Install from URL}
  - hold: 1400
  - mark: plugins
  - click: {role: button, name: Install from URL}
  - type: {target: {role: textbox, name: plugin git URL}, text: "https://github.com/protoLabsAI/terminal-plugin", delay_ms: 45}
  - mark: typed
  - hold: 2500
  - screenshot: {name: dialog-only, target: {role: dialog, name: "Install a plugin from a git URL"}}
  - mark: end
```

Steps: `goto`, `click`, `fill`, `type` (per-char delay), `press`, `hover`, `wait_for`
(selector/text/role/network idle/ms), `hold`, `scroll`, `mark`, `screenshot`, `mask`, `redact`.
Targets prefer accessible roles and text over CSS. Every waiting step gives up after
`step_timeout_ms` (default 15000); a step that waits on slow real work sets its own
`timeout_ms` (max 180000 — more is a validation error, never a silent clamp), all bounded by
`total_timeout_s` (default 300, max 900). Unknown step options are validation errors.
`network_idle` never settles on an app that holds SSE/websockets open — wait for an element. Tokens never go in a script: use
`auth: {bearer_env: CAMPAIGN_APP_TOKEN}` (a `CAMPAIGN_*` env var, or one named in the
`bearer_envs` setting; sent to the `base_url` origin only) or `auth: {storage_state: path}`.
**Plugin views are iframes.** A target inside one adds `frame:` — `{url: /plugins/terminal/view}`
(substring; a glob on the whole URL with `*`), `{selector: "iframe[title='Terminal']"}`, or both,
nesting one level (`frame: {…, frame: {…}}`). The frame is waited for within the step's timeout:

```yaml
  - wait_for: {text: connected, frame: {url: /plugins/terminal/view}, timeout_ms: 30000}
  - type: {target: {selector: textarea, frame: /plugins/terminal/view}, text: "ls", delay_ms: 45}
  - screenshot: {name: terminal, frame: /plugins/terminal/view}   # frame only = the iframe element
```

Masks and redaction reach into every frame, including ones that load later — but not into text
drawn on a `<canvas>` (xterm.js): keep secrets out of a canvas terminal in the shot itself
(`cd /tmp`, a neutral `PS1`). The request fence applies to frame loads and to requests made
from inside frames too.
Re-recording into an existing asset (`asset_id`) replaces that asset's previous take: its
stills are removed (any the operator approved are kept).

Then: `campaign_render(asset_id, outputs=[{name: hero, format: mp4, start: start, end: end,
speed: [{from: start, to: plugins, factor: 2}], limit: github_attachment_video_free}, …])`.

## Hard limits, not folklore

`limits.py` is the only table of platform constraints, and it holds only *documented* limits —
GitHub's 10 MB image/GIF and free-plan video attachment ceilings, its 100 MB paid-plan video
ceiling, and the repository social preview (< 1 MB, ≥ 640×320, 1280×640 recommended). Every row
carries its `source` URL and `as_of` date; bytes are counted as decimal MB to be safe.
Override or add rows with the **Hard-limit overrides** setting. Soft norms (ideal clip length,
"best" aspect) are deliberately absent — the agent researches them and writes them into the
plan as sourced, dated assumptions.

## Settings

`data_dir` (blank = the host's per-instance plugin store), `ffmpeg_path`, `interpreter` (a
Python with playwright for the browser worker; blank = managed runtime, else the agent's own —
named so core's agent self-config fence refuses agent writes to it), `brand_kit_path`
(a Social Studio kit to read; blank auto-detects), `brand_name` / `brand_colors` /
`brand_fonts` / `brand_logo` (fallbacks), `limit_overrides`, `bearer_envs` (extra env-var
names a shot script may use as its bearer), `producer_model`.

## Development

```bash
uv sync                                  # dev env incl. playwright
uv run python -m playwright install chromium   # only for the integration tests
uv run pytest -q                         # unit + integration (integration skips without Chromium/ffmpeg)
uv run ruff check . && uv run ruff format --check .
```

The suite is host-free: it bootstraps the plugin as a synthetic package (`tests/conftest.py`),
runs the REAL worker code in-process against a fake `sync_playwright()` (the job still
round-trips through JSON), fakes ffmpeg at its runner, and exercises the subprocess machinery
for real (interpreter probe, stdin-only secrets, bounded output, kill of an overrunning process
tree). The `integration` tests drive real Chromium through the real worker process: a take →
render, the fence + bearer scoping inside the worker, a card that can't phone home, a hung
browser that gets killed, and a shoot + card from a host process that is forbidden to import
playwright. CI runs both: the unit job, and an integration job that installs Chromium + ffmpeg.

**Release ritual:** bump `version` in `protoagent.plugin.yaml`, `pyproject.toml` and
`__init__.__version__` together (a test enforces lockstep), `uv lock`, and land a
`chore: release vX.Y.Z` commit on main — `.github/workflows/release.yml` tags and publishes via
`protoLabsAI/release-tools`. Never `gh release create` by hand.

## License

MIT
