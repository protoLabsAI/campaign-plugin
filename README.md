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
  `capture: device` records in device pixels instead — a 1280×800 viewport at scale 2 becomes a
  2560×1600 H.264 take (CDP screencast frames, held across idle stretches, assembled at a
  constant frame rate) so small text stays crisp without shrinking the UI.
- **Renders** — ffmpeg trims the head/tail by mark, speed-ramps dead time, crops, and encodes
  H.264 mp4 (yuv420p, faststart) and palette-optimised GIFs, stepping quality down until the
  file fits its **hard** limit — and refusing to call it ready if it still doesn't. Clips are
  **continuous**: there are no interior cuts, and a ramp above 4× is flagged in the render
  report as a jump cut unless the output says `continuous: false` (a deliberate time-lapse).
  A ramp can **ease** in and out (`ease: true` or seconds) — a smooth time-remap instead of a
  speed change on one frame; an un-eased ramp of ≥2× is flagged as abrupt.
- **Failed takes keep their footage** — a take whose step fails mid-run still finalizes its
  video, `timing.json` and `failure.png`, and registers as a `captured` clip noted
  `FAILED at step N: <error>` with the marks it reached, so `campaign_render` can cut a beat
  from the footage before the failure. A failed take can't go to review itself (render the
  good part, or `allow_failed_take=true`); a run whose video couldn't be finalized or that
  ffprobe can't read counts as failed and never fills a planned asset.
- **Cards** — branded HTML templates rendered to PNG: `og-1280x640` (kept under GitHub's 1 MB
  social-preview limit), `x-card-1600x900`, `square-1080`, `title-slide-1920x1080`.
- **Montages** — many short beats (each recorded in a different app theme) + tagline cards +
  an end card, cut into one launch video: every clip normalised to one canvas (1920×1080
  canonical, 1080×1920, 1080×1080), xfade transitions plus a signature **colour wipe** in the
  next beat's theme colour, brand-font lower thirds, H.264 under a size/length limit, a poster,
  and an optional GIF. `campaign_storyboard` reviews the cut as a contact sheet first.
- **Gallery** — one console view, for browsing: every clip/GIF/still/card of a campaign,
  playable, with status, size, dimensions, and the operator-only **Approve / Reject** buttons.
  Superseded takes are hidden (a **Show superseded** toggle brings them back, dimmed); an
  approved asset has a **Supersede…** button, the only way it can be retired.

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
   `https://github.com/protoLabsAI/campaign-plugin`, ref `v0.4.2`. Or
   `python -m server plugin install https://github.com/protoLabsAI/campaign-plugin --ref v0.4.2`.
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
| `campaign_asset_add` / `campaign_asset_update` / `campaign_assets` | The shot list. Status `planned → scripted → captured → rendered → ready_for_review`, or `superseded` (with `superseded_by`) to retire a replaced take; `ready_for_review` is refused if the file is missing or breaks its hard limit; `approved`/`rejected` are refused outright, and so is superseding an approved asset. `campaign_assets` hides superseded takes unless `include_superseded=true` |
| `campaign_milestone` / `campaign_decision` | Dated milestones (owner agent/operator); operator decisions with options + a recommendation |
| `campaign_status` | Progress + who each open item is waiting on; includes `show_component` payloads |
| `campaign_script_save` | Validate + save a shot script (`script="template"` returns an annotated example) |
| `campaign_script_get` | Read a saved shot script back as YAML, each step commented with its number |
| `campaign_shoot` | Record a take (Playwright, headless Chromium, `record_video`) and register the webm + stills. `overrides={N: …}` fixes one step of a saved script without resubmitting it — a whole step replaces step N, options merge into it — validated like any script and saved |
| `campaign_render` | mp4 / gif / poster outputs via ffmpeg, under a hard limit |
| `campaign_card` | Render a branded card template to PNG |
| `campaign_view` | LOOK at a still/card/poster, or frames of a clip (evenly spaced, `every_s` apart from `start` to `end` — paged 12 per contact sheet, the reply naming the next page's `start` — or either side of a mark/cut), as images the model sees — downscaled JPEGs, ≤ 3 per call, contained to the campaign's dir. Needs a core with `graph.sdk.multimodal_tool_result` and a vision model; otherwise it says it couldn't show them |
| `campaign_montage` | Cut an ordered `sequence` of clips + cards into one mp4 (see **Montages**) and register it as a `montage` asset with its poster (+ GIF) |
| `campaign_storyboard` | The montage's contact sheet — one frame per item, a swatch per transition — returned as an image, plus the cut sheet |
| `campaign_produce_video` | One shot script → a finished, self-reviewed video as a background job: record (2K device capture) → [brand intro] + walkthrough + [brand outro] → gates → deliver (see **Videos from a request**) |
| `campaign_video_status` | A video job's stage, progress, gate table, files and links; `wait_s` blocks until it ends |
| `campaign_limits` | The hard-limit table, each row with its source URL and as-of date |
| `campaign_setup` | What's installed and how to fix what isn't |

Plus: skills `campaign-planning`, `shot-scripting`, `asset-review`, `montage-editing`,
`produce-walkthrough-video`; the `campaign_producer`
subagent (allowlist: `campaign_*`, `browser_open/snapshot/screenshot/get_text`,
`social_brand_kit`, `show_artifact`).

## A shot script

```yaml
name: install-from-url
base_url: http://localhost:7871
viewport: {width: 1440, height: 900}
device_scale_factor: 2          # sharpens stills — and, with capture: device, the video
capture: device                 # 2880×1800 H.264 take: crisp small text, normal-size UI
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

Steps: `goto`, `click`, `focus`, `fill`, `type` (per-char delay), `press` (`repeat: N`,
`delay_ms` between), `hover`, `mouse_move` (`{x, y}` — park the pointer), `drag`
(`{target, to: {dx, dy}}` — e.g. widen a resizable dock), `wait_for`
(selector/text/role/network idle/ms), `hold`, `scroll`, `mark`, `screenshot`, `mask`, `redact`,
`upload`. `click`/`focus`/`hover`/`wait_for` take `within: <target>` to look for the target
inside one container (the Save button of ONE dialog).
Targets prefer accessible roles and text over CSS. Every waiting step gives up after
`step_timeout_ms` (default 15000); a step that waits on slow real work sets its own
`timeout_ms` (max 600000 = 10 min — more is a validation error, never a silent clamp), all bounded by
`total_timeout_s` (default 300, max 900). Unknown step options are validation errors.
A target can add `exact: true` (whole-string match) and `nth` (0-based, or `first`/`last`).
Several matches: a `wait_for` is satisfied when ANY match is visible (text shown twice is fine
to wait on); an action (click/focus/hover/drag/fill/type/press/scroll/element screenshot) on an ambiguous
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

Masks and redaction reach into every frame, including ones that load later. CSS can't reach
text drawn on a `<canvas>`, so for xterm.js terminals `redact` filters what the terminal is
**given** to draw: `Terminal.prototype.write`/`writeln` are wrapped when the xterm bundle sets
`window.Terminal` (its UMD build — what the protoAgent Terminal view loads), with the unfinished
tail of a line held back briefly so a path split across two chunks is still caught. An xterm the
hook can't reach (bundled as an ES module) has its canvases **blurred** whenever redaction is on.
Still keep secrets out of a terminal in the shot itself (`cd /tmp`, a neutral `PS1`). The request fence applies to frame loads and to requests made
from inside frames too. The shoot browser launches with `--force-device-scale-factor` matching
`device_scale_factor`: under DPR emulation alone, xterm.js's WebGL renderer (the Terminal view)
sized its canvas at 1x and drew a blank terminal at dsf 2.
**Seed browser storage before the app boots.** An app that restores UI state from
`localStorage` (the protoAgent console keeps panel widths in `protoagent.ui`, a zustand
`persist` blob) boots at its defaults in a fresh recording browser. Seed it:

```yaml
storage:
  origin: http://localhost:7871      # optional — default: base_url's origin
  local: {protoagent.ui: {state: {rightWidth: 860}, version: 14}}   # objects → JSON
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

### Retiring a retake

When a retake is registered as a NEW asset, take the old one out of the operator's queue
without approving or rejecting it:
`campaign_asset_update(old_id, status="superseded", superseded_by=[new_id], notes="why")`.
A superseded asset leaves the review queue and every count (`campaign_status`,
`campaign_list`, the gallery's campaign picker), is hidden in the gallery unless **Show
superseded** is on, and is refused as a montage/storyboard beat with an error naming its
replacement. The agent can supersede any asset that isn't approved; an APPROVED one is
refused — the operator retires it with the gallery's **Supersede…** button (the review route,
`decision: "supersede"`). Moving a superseded asset to any other status brings it back into
play and clears `superseded_by`.

Asset statuses: `planned`, `scripted`, `captured`, `rendered`, `ready_for_review`,
`approved`, `rejected` (operator only), `superseded`. `status` is plain TEXT in SQLite, so
older databases need no migration; the `superseded_by` column is added on first connect.

Then: `campaign_render(asset_id, outputs=[{name: hero, format: mp4, start: start, end: end,
speed: [{from: start, to: plugins, factor: 2, ease: true}], limit: github_attachment_video_free}, …])`.

## Directing a clip — two rules from operator review

Both come from an operator reviewing real launch clips; the skills state them for any app.

1. **Continuous action, no jump cuts** (*"too many cut frames, missing chunks of action"*).
   No cuts between the action's start and its result; compress time with speed only — a
   uniform ~1.5–2× over the run, ≤4× eased ramps (`ease: true`) only over pure dead time (typing, a spinner),
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
(`auto` · `letterbox` · `crop`), `fps` (30), `background`, `transition` (`edge_sweep`), `reduced_motion` and
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
- **The signature transition** (the default): `edge_sweep` — 600 ms; both shots stay still and
  the incoming one is revealed left → right behind a narrow brand-accent leading band (32 px at
  2560 wide) — between beats, and the one moving transition of the brand intro/outro (title card
  → main clip, main clip → bumper; splash → title and bumper → CTA are a short eased crossfade,
  `bumper_fade_ms` on the intro/outro presets — default 300, frame-quantized, 0 = a cut). The old
  `boot_seam` was removed — asking for it is a validation error pointing at `edge_sweep`, `cut` or
  `fade`. It follows one deterministic contract shared with the console (`transitions.py`, `transitions.json` —
  protoContent#606's `protolabs.brand-video/1`, checked against its analytic reference frames):
  exact easeInOutCubic, `N = round(ms·fps/1000)` frames evaluated at
  `u = k/N`, frame `k = 0` is the outgoing shot and the incoming one owns `u = 1`, geometry
  normalized to the canvas, edges antialiased by pixel coverage, the accent from the brand
  snapshot. Its length is fixed (a `duration` is refused); every item is quantized to whole
  frames so it lands frame-exact. `reduced_motion: true` (output) keeps the sweep's N-frame
  interval as a stationary hard cut at `ceil(N/2)` (no band or fade — the timeline doesn't
  move); any other moving transition becomes a plain cut; dissolves stay.
- **Other transitions** (into an item): `colorwipe` — a full-frame bar in the NEXT item's
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
  lists them. Rejected clips are refused, and so are superseded ones (the error names the
  replacement take to use instead).

The montage registers as a `montage` asset (the gallery plays it and filters by it) with
`meta.sequence`, the cut sheet (`meta.timeline`), the encode attempts and the engines used;
its poster and GIF are child assets.

### The video end-card recipe (brand outro)

Not a new component — a **recipe** composed from what's already here: the last shot's beat, a
recorded bumper, and the `cta-slide` card drawn in the brand's own look. A brand kit's
`visual.card` says how its end cards are drawn (anything unset keeps the generic Campaign look):

```yaml
visual:
  fonts:
    heading: Geist
    body: Geist
    mono: Geist Mono
    files:                     # EMBEDDED as data: URIs — same face on every machine, nothing fetched;
      - {family: Geist, path: fonts/Geist-Variable.woff2, weight: "100 900"}   # a card whose bundled
      - {family: Geist Mono, path: fonts/GeistMono-Variable.woff2, weight: "100 900"}  # face didn't paint FAILS
  card:
    button: outline            # outline = bordered rectangle, no fill, no glow (| fill: the accent pill)
    background: subtle         # subtle = one 8 % accent wash, no grid (| glow | plain)
    title_weight: 600          # weight stops; sizes stay canvas-relative (video-readable)
    eyebrow_weight: 500
    button_weight: 500
    body_weight: 440
    radius_px: 4               # at 1280 px wide — scaled to the canvas (2560 → 8 px)
    border_px: 1               # doubled for video (2560 → 4 px)
```

**protoLabs** ships as a checked-in snapshot, `brands/protolabs/brand-kit.yaml`, GENERATED from
the `@protolabsai/design` tokens (pinned: `ds-tokens.json` + `ds-version.txt`, the mapping in
`scripts/gen_brand_protolabs.py`; the suite fails if the kit drifts from the tokens) with Geist /
Geist Mono bundled under their SIL OFL licence (`brands/protolabs/fonts/OFL.txt`). Point
**Brand kit file** at it. Regenerate after a DS bump:
`python scripts/gen_brand_protolabs.py --design <path to @protolabsai/design>`.

#### The brand outro

One ending, set up once and appended to any montage with a last item `{outro: true}`:
**the beat** (the last shot's final frame frozen — its last caption held, faded, then the clean
shot held), **a bumper** (a recorded clip — e.g. the product's real boot splash, shot with
`capture: device`) and **a CTA card** (the `cta-slide` template: logo, eyebrow, title, a big
accent button, the URL — rendered at the canvas size, so `size: 2560x1440` gives a 2K card).
The operator sets it in the **Brand outro** setting (YAML; operator-only, since the bumper
names a local file):

```yaml
caption_hold_s: 2.5       # the last caption stays up this much longer (frozen frame)
min_caption_s: 4.0        # REFUSE a cut that shows the final caption for less than this
caption_fade_s: 0.5       # then the caption fades (the clip's `caption.region` is painted over)…
beat_s: 2.0               # …and the clean shot holds before the bumper
transition: edge_sweep    # main clip → bumper (the default)
bumper_fade_ms: 300       # bumper → CTA card: a short crossfade on the brand easing (0 = a cut)
bumper: {path: ~/media/protoagent-splash.mp4, in: 0.1, out: 2.7}
cta: {template: cta-slide, eyebrow: "Open source · MIT", title: "Star protoAgent on GitHub",
      cta: "★  Star on GitHub", url: github.com/protoLabsAI/protoAgent, duration: 4.5}
```

The clip before the outro says where its final caption is — `caption: {at: <source s when the
last caption is fully up>, region: {x, y, w, h}, color: "#0a0a0c"}` (source pixels). With `at`,
the montage checks `(out − at) / speed + caption_hold_s ≥ min_caption_s` and refuses a cut that
trims into the last caption's reading time (the cut sheet reports `caption_visible_s`). Footage
that ends on its OWN hold + caption fade + beat (Atlas's record mode does, before `tour-done`)
sets `caption.until` (when the caption starts fading) and the preset's `caption_hold_s`,
`caption_fade_s` and `beat_s` to 0: the cut is at the end mark and only the guard remains. Footage
is never cut to make room for the beat — the frame is frozen instead. `{outro: {…}}` overrides
any key for one cut (its bumper may name a campaign `clip`, never a file `path`).

#### The brand intro

The mirror of the outro, prepended with a FIRST item `{intro: true}`: **a bumper** (the same
live boot splash, ~2 s), **a title card** (the `title-slide` template in the same card recipe —
embedded Geist, the DS snapshot, the bordered look — eyebrow, title, subtitle, ~3 s; the splash
crossfades into it — `bumper_fade_ms`, default 300 ms on the brand easing, 0 = a cut), then the
**edge sweep** into the first shot. Never a hard cut into the shot:
`into_content` must be `edge_sweep` (the default) or a crossfade (`fade`, `dissolve`,
`fadeblack`, `fadewhite`, ≥ 0.3 s; a `duration: 0` is refused), and the montage REFUSES an
intro that runs longer than `max_s` (default 6 s, measured until the first shot is fully up —
the cut sheet reports `intro_s`). The **Brand intro** setting (YAML; operator-only):

```yaml
bumper: {path: ~/media/protoagent-splash.mp4, in: 0.1, out: 2.3}   # or `bumper: outro` — reuse the outro's file
card: {template: title-slide, eyebrow: "Architecture walkthrough", title: "protoAgent", duration: 3.0}
bumper_fade_ms: 300       # splash → title card: a short eased crossfade (0 = a cut)
into_content: edge_sweep  # title card → the first shot (the default)
max_s: 6
```

`{intro: {card: {title: "protoAgent in 85 seconds", subtitle: "…"}}}` sets one cut's title
(merged into the preset's card). Start the first shot on a SETTLED frame — `in` past any
opening motion; `campaign_produce_video` measures it from the footage.

## Videos from a request

`campaign_produce_video` turns ONE shot script into a finished, self-reviewed video with no
hand-driving — the path behind "make a 60-second 2K walkthrough of our architecture, end with
the star CTA" (the `produce-walkthrough-video` skill is the agent's recipe, from the one-line
brief to delivery):

```
campaign_produce_video(script=<atlas_record_script's reply, unchanged>, title="protoAgent in 85 seconds",
                       subtitle="How a self-hosted agent runtime fits together",
                       quality="2k", intro="brand", outro="brand")
→ "Started video job vid-3f9c…"            # returns at once
campaign_video_status(job_id="vid-3f9c…", wait_s=300)   # stage, %, gates, files, links
```

**Preflight** (before any browser starts): the script's `base_url` must be THIS agent's console
(compared with core's `sdk.console_url()` when present — on a fleet member that is the member's
own console, never the hub's — else `A2A_PUBLIC_URL` / the bound port; loopback spellings equal), its first page must answer 200, and its `bearer_env` /
`storage_state` token must pass one authenticated GET. Otherwise it refuses at once, saying
what to fix. `allow_other_console=true` is the explicit opt-in for recording another console.

It runs as a **background job** (a worker thread in the plugin; one at a time): **record** the
take (`quality: 2k` → `device_scale_factor` = 2560 ÷ viewport width + `capture: device`; an
Atlas script also gets a `last-slide` mark) → **measure** the first settled frame after `start`
(the in-point) and, for Atlas footage, when the final caption is fully up and starts fading →
**render** `[intro] → walkthrough → [outro]` twice: a review cut (crf 18) and a GitHub variant
held under `github_attachment_video_free` (10 MB) → **self-review** → **deliver**.

**Self-review gates** — all must pass before a job is `done`:

| Gate | Check |
|---|---|
| `resolution` | rendered ≥ 2560 px wide (1920 for `1080p`) |
| `final_caption` | the final caption is on screen ≥ the outro's `min_caption_s` (4 s) before the outro |
| `intro` | the intro is ≤ 6 s and crossfades in |
| `no_blank_frames` | a sample every second of the walkthrough segment: none blank / near-black (luma p95−p5 < 10 on a 64×36 sample — a dark theme with content passes) |
| `size_budget` | the GitHub variant is within `github_attachment_video_free` |
| `review_assets` | a labelled **contact sheet** (intro bumper, card, crossfade, first settled frame, eight walkthrough frames, final caption, outro) and a **100 % crop** of the densest small text are saved |

A failed gate gets **one** automatic retry where it can help — blank frames or a short
resolution re-record; an oversize GitHub variant re-renders at 24 fps. A refused cut (final
caption too short, intro too long) is a script/preset fix, so it doesn't retry. Otherwise the
job ends **`needs_review`** with the reasons, its files stay `rendered` (not offered for
review), and the chat is told not to present it as finished.

**Idempotent**: jobs live in `<data dir>/videos/vid-<hash>.json`, keyed by a hash of the
inputs. The same request returns the same job; a failed (or orphaned) job resumes, reusing a
good take; `force=True` re-records.

**Delivery** (what each channel gets today):

- **Campaign gallery** — the review mp4 + GitHub mp4 as `montage` assets (`ready_for_review`
  when every gate passed) with the sheet and the text crop as stills; the operator approves.
- **Console chat** — on a core with the `artifact.save_file` service (protoAgent#4254) the job
  saves the mp4 and the sheet into the requesting chat's Artifact panel itself. On older cores,
  the follow-up turn (`graph.sdk.run_in_session`) tells the agent to `save_file_artifact` them.
- **A2A / `/v1`** — on a core with `sdk.attach_file` (protoAgent#4255), `campaign_video_status`
  attaches the finished files to the answer: A2A FileParts, `/v1` absolute links and
  attachments. On older cores the reply carries the signed `/media/…?sig=` links (made absolute
  when the agent's base URL is known), and the agent is told the caller has no Artifact panel.
- **Links** — every file goes through the host media store (`registry.save_media`).
  `GET /api/plugins/campaign/videos/{job_id}` (bearer) returns the job's status, gates and
  `downloads` (`/api/plugins/campaign/file/{asset_id}`).

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
files from; blank refuses uploads; operator-only), `outro` (the brand outro — see **The brand
outro**; operator-only), `intro` (the brand intro — see **The brand intro**; operator-only),
`producer_model`.

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
