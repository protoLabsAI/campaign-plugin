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
- **Renders** — ffmpeg trims the head/tail by mark, speed-ramps dead time, crops, and encodes
  H.264 mp4 (yuv420p, faststart) and palette-optimised GIFs, stepping quality down until the
  file fits its **hard** limit — and refusing to call it ready if it still doesn't. Clips are
  **continuous**: there are no interior cuts, and a ramp above 4× is flagged in the render
  report as a jump cut unless the output says `continuous: false` (a deliberate time-lapse).
- **Cards** — branded HTML templates rendered to PNG: `og-1280x640` (kept under GitHub's 1 MB
  social-preview limit), `x-card-1600x900`, `square-1080`, `title-slide-1920x1080`.
- **Montages** — many short beats (each recorded in a different app theme) + tagline cards +
  an end card, cut into one launch video: every clip normalised to one canvas (1920×1080
  canonical, 1080×1920, 1080×1080), xfade transitions plus a signature **colour wipe** in the
  next beat's theme colour, brand-font lower thirds, H.264 under a size/length limit, a poster,
  and an optional GIF. `campaign_storyboard` reviews the cut as a contact sheet first.
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
   `https://github.com/protoLabsAI/campaign-plugin`, ref `v0.3.2`. Or
   `python -m server plugin install https://github.com/protoLabsAI/campaign-plugin --ref v0.3.2`.
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
| `campaign_montage` | Cut an ordered `sequence` of clips + cards into one mp4 (see **Montages**) and register it as a `montage` asset with its poster (+ GIF) |
| `campaign_storyboard` | The montage's contact sheet — one frame per item, a swatch per transition — returned as an image, plus the cut sheet |
| `campaign_limits` | The hard-limit table, each row with its source URL and as-of date |
| `campaign_setup` | What's installed and how to fix what isn't |

Plus: skills `campaign-planning`, `shot-scripting`, `asset-review`, `montage-editing`; the `campaign_producer`
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
(selector/text/role/network idle/ms), `hold`, `scroll`, `mark`, `screenshot`, `mask`, `redact`,
`upload`.
Targets prefer accessible roles and text over CSS. Every waiting step gives up after
`step_timeout_ms` (default 15000); a step that waits on slow real work sets its own
`timeout_ms` (max 180000 — more is a validation error, never a silent clamp), all bounded by
`total_timeout_s` (default 300, max 900). Unknown step options are validation errors.
A target can add `exact: true` (whole-string match) and `nth` (0-based, or `first`/`last`).
Several matches: a `wait_for` is satisfied when ANY match is visible (text shown twice is fine
to wait on); an action (click/hover/fill/type/press/scroll/element screenshot) on an ambiguous
target fails the step with the first matches listed and how to pick one.
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
from inside frames too. The shoot browser launches with `--force-device-scale-factor` matching
`device_scale_factor`: under DPR emulation alone, xterm.js's WebGL renderer (the Terminal view)
sized its canvas at 1x and drew a blank terminal at dsf 2.
**Seed browser storage before the app boots.** An app that restores UI state from
`localStorage` (the protoAgent console keeps panel widths in `protoagent.ui`, a zustand
`persist` blob) boots at its defaults in a fresh recording browser. Seed it:

```yaml
storage:
  origin: http://localhost:7871      # optional — default: base_url's origin
  local: {protoagent.ui: {state: {panelWidths: {right: 860}}, version: 0}}   # objects → JSON
  session: {lastTab: plugins}        # strings stored as-is
init_script: "window.__demo = true;" # optional escape hatch (≤ 64 KB)
```

`storage` is written by a context init script before any page script runs, only in top-level
documents whose `location.origin` is the storage origin (≤ 200 entries, 256 KB). `init_script`
runs before page scripts in every document on the `base_url` origin only, inside a function.
Security posture: both are operator/agent-authored and trusted like the rest of the script
(which can already navigate and type anywhere); they run only inside the recording browser,
which stays fenced off this plugin's own API, and the origin check keeps them off third-party
pages and frames. Neither is a place for a credential — the script is stored in the plan.

**File uploads.** `- upload: {target, files: [/abs/path, …], frame?}` — an
`<input type=file>` target (hidden is fine) gets the files via `set_input_files`; any other
target (a button, a drop zone) is clicked and the file chooser it opens gets them
(`expect_file_chooser`). The host checks every path at shoot time, symlinks resolved, before
the browser starts: it must be a regular file under the **`upload_dirs`** setting (empty — the
default — refuses every upload; the filesystem root, the home dir or any parent of it, and the
agent's home are ignored as too broad), not hardlinked, ≤ 50 MB (100 MB per step), never a
common key/credential file name (`.env*`, `secrets.yaml`, `.credentials.json`, `id_rsa*`,
`*.pem`, `.netrc`, browser `Cookies`, …), never inside a credential dir (`.ssh`, `.aws`,
`.gnupg`, `.config/gh`, browser profiles, …), and never inside the agent's home
(`~/.protoagent`, `$PROTOAGENT_HOME`; matched case-insensitively and by inode) other than this
plugin's own media. The worker re-checks each file without following links just before the
upload. The name lists are a backstop, not the fence: the fence is the allowlist, so point
`upload_dirs` at a folder of demo files the agent can't write into. `upload_dirs` is marked
`spawns: true`, so the agent's `set_config` can't widen it.

Re-recording into an existing asset (`asset_id`) replaces that asset's previous take: its
stills are removed (any the operator approved are kept).

Then: `campaign_render(asset_id, outputs=[{name: hero, format: mp4, start: start, end: end,
speed: [{from: start, to: plugins, factor: 2}], limit: github_attachment_video_free}, …])`.

## Directing a clip — two rules from operator review

Both come from an operator reviewing real launch clips; the skills state them for any app.

1. **Continuous action, no jump cuts** (*"too many cut frames, missing chunks of action"*).
   No cuts between the action's start and its result; compress time with speed only — a
   uniform ~1.5–2× over the run, ≤4× eased ramps only over pure dead time (typing, a spinner),
   never ramping past a moment where something new appears. A 25–35 s hero that shows
   everything beats a shorter one with gaps; hold ~2.5 s on the result. `campaign_render`
   flags a ramp above 4× by default; the asset-review self-check walks the clip ~1 frame/s and
   wants every new UI element on screen ≥ 0.5 s.
2. **Showcase the app's own views, not just its chat/command surface** (*"can't see the note
   view, only the chat screen"*). Open the view where the result lands (side panel, rail view,
   board, document, terminal) docked beside the input *before* the action, so the result
   appears live; end on that view showing the result, not on a "Done" message; crop to input +
   view. Embedded views are usually iframes — target them with `frame:`. The self-check wants
   the final ~2 s to show the result in its own view. The `shot-scripting` skill has a worked
   example (chat left, notes panel right, the agent writes a note, hold on the note).

These are for standalone clips (a lane's hero, a GIF). A **montage** beat follows the same
continuity rule — one uniform speed, ≤4×, ends trimmed only — but its own length and geometry
(4–8 s on screen, 1920×1080 full frame); see the `montage-editing` skill.

## Montages

```python
campaign_montage(campaign_id, sequence, output={...}, title="", lane="", asset_id=0)
campaign_storyboard(campaign_id, sequence, output={...})
```

`sequence` is ordered; each item is a clip or a card:

```yaml
- {clip: 41, in: 1.2, out: 7.0, speed: 1.5, label: "Schedules", theme_color: "#22c55e",
   focus: {x: 1200, y: 200, w: 600, h: 600}, transition: colorwipe}
- {card: {title: "Your agent. Your data. Your way.", subtitle: "…", eyebrow: "…", url: "…",
          cta: "Star it on GitHub", bg: "#0d0f14", fg: "#f4f5f7", accent: "#7c5cff", logo: true},
   duration: 3, zoom: true, transition: {style: fade, duration: 0.6}}
```

`output` (a mapping, or just a preset name): `name`, `title`, `preset` (`landscape`
1920×1080 · `vertical` 1080×1920 · `square` 1080×1080) or `size: WxH`, `fit`
(`auto` · `letterbox` · `crop`), `fps` (30), `background`, `transition` (`colorwipe`) and
`transition_duration` (0.45 s), `limit` / `max_bytes`, `crf`, `poster`, `poster_at`, `gif`,
`gif_limit`, `gif_width`, `label_font`, `card_engine` (`auto` · `html` · `ffmpeg`),
`label_engine` (`auto` · `drawtext` · `html` · `none`). Unknown keys anywhere are refused, with
every problem listed at once.

- **Continuous footage.** A clip item is one unbroken stretch of its take: `in`/`out` trim
  only its ends; `speed` is one factor, 0.25–4×. No ramps, no interior cuts.
- **One canvas.** 1920×1080 is canonical: a 1920×1080 clip (a beat recorded at a 1920×1080
  viewport, device scale 1, full frame) passes through untouched. Any other size is
  letterboxed (or crop-filled with `fit: crop`), and the reply names it. On a vertical or
  square cut a clip is centre-cropped — or cropped to its `focus` rect (source pixels), grown
  to the canvas's shape and kept inside the frame (a focus outside the source is refused).
  Every item is normalised to the canvas's size, fps, square pixels, yuv420p and one timebase.
- **Transitions** (into an item): `colorwipe` — a full-frame bar in the NEXT item's
  `theme_color` (a card's `accent`; else the brand accent) sweeps across, covers the outgoing
  shot, then sweeps on and reveals the incoming one (two hard-edged `xfade` wipes through a
  `color` source; `colorwipe_left`/`_up`/`_down` change direction); `cut`; or an xfade style —
  `fade`, `fadeblack`, `fadewhite`, `dissolve`, `wipe*`, `slide*`, `smooth*`, `circleopen`,
  `circleclose`, `circlecrop`, `rectcrop`, `horzopen/close`, `vertopen/close`, `radial`,
  `pixelize`. A colour wipe of T covers the last T/2 of one item and the first T/2 of the next
  without shortening the cut; an xfade of T overlaps the two by T.
- **Cards** render through the card renderer (the `title-slide` template, HTML → PNG in the
  out-of-process browser, brand from the Social Studio kit's `visual:` section) and loop into
  N-second segments; `zoom: true` adds a slow 4 % push. With no browser, a plain ffmpeg
  `drawtext` card is drawn instead (and the reply says so).
- **Labels** are lower thirds — an accent bar in the clip's theme colour beside the text on a
  translucent brand-background box — drawn with `drawtext` in the brand heading font, resolved
  to a font file (a well-known sans when it isn't installed as a file). An ffmpeg built
  without `drawtext` (e.g. Homebrew's) gets the same lower third as a transparent HTML overlay;
  with neither, labels are skipped and the reply says so. Vertical labels sit above the
  bottom ~22 % where Shorts/Reels draw their own UI.
- **Output**: the graph renders once to a near-lossless master; the mp4 (H.264 High, yuv420p,
  `+faststart`) is encoded from it, stepping CRF and then a capped bitrate until it fits the
  size ceiling. A `limit` is checked twice — length/orientation/dimensions before rendering,
  size after. The poster is a PNG at the middle of the first clip (or `poster_at`); the GIF
  (`gif: true`) is kept only if it fits `gif_limit` (GitHub's 10 MB by default).
- **Approval**: a montage may use clips that are only `ready_for_review` (or earlier) — the
  reply warns, the asset's notes start `DRAFT INPUTS: …` and its `meta.unapproved_inputs`
  lists them. Rejected clips are refused.

The montage registers as a `montage` asset (the gallery plays it and filters by it) with
`meta.sequence`, the cut sheet (`meta.timeline`), the encode attempts and the engines used;
its poster and GIF are child assets.

## Hard limits, not folklore

`limits.py` is the only table of platform constraints, and it holds only *documented* limits —
GitHub's 10 MB image/GIF and free-plan video attachment ceilings, its 100 MB paid-plan video
ceiling, the repository social preview (< 1 MB, ≥ 640×320, 1280×640 recommended), and for
montages: X post video (512 MB, 0.5–140 s without Premium), X API video (≤ 1280×1024),
LinkedIn feed video (5 GB, 3 s–15 min, 256×144–4096×2304) and YouTube Shorts (vertical or
square, ≤ 3 min). Rows can carry `max_duration_s` / `min_duration_s`, `max_width` /
`max_height` and `orientations` besides bytes and minimum dimensions. Every row
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
names a shot script may use as its bearer), `upload_dirs` (folders an `upload` step may read
files from; blank refuses uploads; operator-only), `producer_model`.

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
tree). The montage tests check validation, framing and the exact filtergraph with no ffmpeg,
and a real-ffmpeg montage of synthetic solid-colour clips asserts the duration, resolution,
codec/faststart, and that frames mid-transition carry the next clip's theme colour. The
`integration` tests drive real Chromium through the real worker process: a take →
render, the fence + bearer scoping inside the worker, a card that can't phone home, a hung
browser that gets killed, and a shoot + card from a host process that is forbidden to import
playwright. CI runs both: the unit job, and an integration job that installs Chromium + ffmpeg.

**Release ritual:** bump `version` in `protoagent.plugin.yaml`, `pyproject.toml` and
`__init__.__version__` together (a test enforces lockstep), `uv lock`, and land a
`chore: release vX.Y.Z` commit on main — `.github/workflows/release.yml` tags and publishes via
`protoLabsAI/release-tools`. Never `gh release create` by hand.

## License

MIT
