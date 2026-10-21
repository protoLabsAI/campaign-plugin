# Shot scripts

A shot script is the declarative YAML a take is recorded from. This page covers the format, the steps, frames, storage seeding, uploads, retiring a retake, and the two directing rules every clip follows.

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

**Inline chat app cards (ADR 0118).** These cards mount their iframe only near the
viewport. Use `frame: {card: "section.pl-frame-card"}` to scroll the card into view
in-page before waiting for its iframe to attach and load. `card` is a CSS selector;
it defaults to `section.pl-frame-card` when only `title` is given. `title` matches
exact visible title text within the card. Optional `nth` is a non-negative, 0-based
integer selecting among matching cards (after title filtering); otherwise the card
must be unique. Card frames can nest using `frame`, just like URL/selector frames,
but cannot combine `card`/`title` with `url`/`selector` at the same level.

The worker holds the card in view through the step, re-centering it if the chat
scrolls or the layout moves, then releases the hold even if the step fails. Mounting
and loading consume the step's timeout; the action gets the remaining time.
Clicks, typing, and slider drags use the same targets as other frames:

```yaml
  - drag:
      frame: {title: "Mortgage calculator"}
      target: {role: slider, name: "Interest rate"}
      to: {dx: 120, dy: 0}
      timeout_ms: 30000
  - screenshot:
      name: mortgage-result
      frame: {card: "section.pl-frame-card", title: "Mortgage calculator", nth: 0}
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

## Retiring a retake

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
