---
name: shot-scripting
description: >-
  Use to write a robust Campaign Studio shot script — the declarative YAML that records a
  deterministic browser take of any web app — and to fix one that failed. Covers targets that
  survive restyles, readable holds, continuous action with no jump cuts, staging the result in
  the app's own view (not just its chat/command surface), marks, redaction of secrets, fixed
  timezone/locale, pre-seeded browser storage, and file uploads. Triggers: "record a demo of", "capture a clip of", "shoot the flow",
  "the take failed", "re-record", "make a GIF of the app doing X".
tools: [campaign_script_save, campaign_shoot, campaign_view, campaign_get, browser_open, browser_snapshot, browser_screenshot]
---

# Shot scripts

A take is only as repeatable as its script. The same script must produce the same clip next
week, after a restyle, on another machine. Start from `campaign_script_save(campaign_id,
"template")`.

## Explore first

`browser_open(url)` then `browser_snapshot()` — the snapshot lists real accessible roles and
names. Script against those. Never guess a selector from memory of what the app "probably" uses.

## Targets, most to least robust

1. `{role: button, name: "Install from URL"}` — what a screen reader sees; survives restyles.
2. `{label: "Repository URL"}` / `{placeholder: "https://…"}` — form fields.
3. `{text: "Plugins"}` — visible copy (add `exact: true` if it's a substring of other text).
4. `{test_id: …}` — when the app has them.
5. `selector: "css"` — last resort. Never a generated class name (`.css-1x2y3z`).

In a `type` step, `text:` is what gets typed — to target an element by its text, write
`target: {text: "Search"}`.

### When text appears more than once

Results often show up twice — "3 passed" in a bold summary AND in an inline code span.

- **A wait is fine with that.** `wait_for: {text: "3 passed"}` is satisfied as soon as ANY
  match is visible (`state: hidden` waits until none is). Don't add `nth` just to wait.
- **An action needs exactly one** (click, hover, fill, type, press, scroll, an element
  screenshot). An ambiguous action target fails the step, and the error lists the first few
  matches (`1) <strong> '3 passed'; 2) <code> 'pytest -q: 3 passed in 0.4s'`). Pick one:
  `exact: true` (whole-string, case-sensitive — drops the code span here), `nth: 0` / `nth: 1`
  / `nth: first` / `nth: last` (0-based, DOM order), or a narrower `role` + `name` / selector.
  Prefer `exact` or a role over `nth` — an index breaks when the page adds a match.

A failed take may already have done things (sent a prompt to an agent, started a job) — the
app keeps going after the shoot dies. So get targets right BEFORE the take: re-snapshot the
page in the state the step will see it and check every action target names one element.

## Inside an iframe — protoAgent plugin views

A protoAgent console **plugin view** (the Terminal rail view, Notes, any `/plugins/<id>/view`)
is an `<iframe>`. A target inside one says which frame with `frame:` — on the step or on the
target — or the step searches only the top page and never finds it:

```yaml
- click: {role: button, name: Terminal}                       # opens the rail view
- wait_for: {text: "connected", frame: {url: /plugins/terminal/view}, timeout_ms: 30000}
- click: {selector: ".xterm", frame: {url: /plugins/terminal/view}}
- type: {target: {selector: textarea, frame: /plugins/terminal/view}, text: "ls", delay_ms: 45}
- press: {key: Enter, selector: textarea, frame: /plugins/terminal/view}
- screenshot: {name: terminal, frame: /plugins/terminal/view}  # just the iframe
```

- `frame: {url: …}` — a **substring** of the frame's URL; with `*`/`?`/`[` it's a **glob on
  the whole URL** (`"*/plugins/*/view*"`). A bare string is a `url`.
- `frame: {selector: "iframe[title='Terminal']"}` — the `<iframe>` element in its parent.
  Give both to be exact; two frames matching is an error, never a guess.
- `frame: {url: …, frame: {url: …}}` — one level deeper (max 2).
- The frame is **waited for** within the step's `timeout_ms` (a view's iframe attaches after
  the console renders). `wait_for: {frame: …}` with no target waits for the frame alone;
  `screenshot` with only a `frame` shoots the iframe element; `scroll` with only a `frame`
  scrolls inside it. A frame that never shows up fails the step and lists the frames the page
  does have — use one of those URLs.
- Use `agent_browser` on the view's own URL (`/plugins/<id>/view`) to snapshot its roles/names.

**Masks and redaction reach into every frame** — current ones and ones that load later — so a
top-level `mask`/`redact` covers plugin views too. **But CSS can't touch text drawn on a
`<canvas>`**: the terminal (xterm.js) paints its text, so neither `mask` selectors nor
`redact` patterns can change what it shows. Keep secrets off a canvas terminal *in the shot
itself*: `cd /tmp` (not your home dir) before you start, set a neutral prompt
(`export PS1='$ '`), never `cat` an env file or print a token, and `clear` before the beat that
matters. Or `mask` the whole terminal element (`.xterm`) for a beat you can't keep clean.

## Seed browser storage before the app boots

Many apps restore UI state from `localStorage` while they start — panel widths, the open tab,
a dismissed onboarding tour. A fresh recording browser has none of it, so the app boots at its
defaults (the protoAgent console docks panels at 360 px instead of the 860 you wanted). Clicking
and dragging to fix that on camera wastes the clip. Seed it instead:

```yaml
base_url: http://localhost:7871
storage:
  # origin: http://localhost:7871     # optional — defaults to base_url's origin
  local:
    protoagent.ui: {state: {panelWidths: {right: 860}}, version: 0}   # an object → JSON
    onboarding.done: "1"                                                # a string → as-is
  session: {lastTab: plugins}
```

- Use it when the look of the FIRST frame depends on persisted state. Read the real key and
  shape first: `browser_open` the app, set the UI the way you want it, then read
  `localStorage` (or ask the operator) — copy the JSON, don't guess it. For a zustand `persist`
  store the value is `{state: {…}, version: N}`; a wrong `version` makes the app discard it.
- It's written before ANY page script runs, only in top-level pages on that one origin; every
  `goto`/reload there starts from the seed. Other origins never see it.
- Need more set-up than key/values (a feature flag on `window`, a stubbed `Date`)? Use
  `init_script: "window.__demo = true;"` — JavaScript run before page scripts in every page on
  the `base_url` origin only, inside a function (assign `window.x` for globals), ≤ 64 KB. It's
  stored in the plan like the rest of the script: never put a token in it.
- Not for auth — that's `auth:` (below).

## File uploads

```yaml
- upload: {role: button, name: "Attach files", files: [/Users/me/demo-assets/screenshot.png]}
- upload: {target: {text: "Drop files here"}, files: /Users/me/demo-assets/data.csv}
- upload: {selector: "input[type=file]", files: [/Users/me/demo-assets/a.png, /Users/me/demo-assets/b.png]}
- wait_for: {text: "screenshot.png"}
```

- Target the **button or drop zone the viewer sees** — it's clicked and the file chooser it
  opens gets the files (the native dialog never shows in the video). A hidden
  `<input type=file>` works too (files set directly, nothing clicked); use it when the
  control only accepts a real drag-and-drop.
- `files` are absolute paths and must sit under a folder the operator listed in the plugin's
  **Upload folders** (`upload_dirs`) setting. It's empty by default, so uploads are refused
  until the operator sets one — ask them for a demo-assets folder and put the files there.
  You can't change that setting yourself.
- Refused even inside an allowed folder: anything that isn't a regular file, over 50 MB
  (100 MB per step), key/credential files (`.env`, `secrets.yaml`, `id_rsa`, `*.pem`, …),
  credential dirs (`.ssh`, `.aws`, …), and anything in the agent's home (`~/.protoagent`)
  except this plugin's own media. Upload only demo files made for the shot — never a real
  customer file.
- A target that is neither a file input nor opens a chooser fails the step with what to
  target instead.

## Timing — a viewer has to read it

- `wait_for` before every click on something that loads (`{text: …}`, `{role: …}`). Never a
  bare `hold` as a "wait".
- **Don't wait for `network_idle` on an app that streams.** The protoAgent console (and any
  chat UI, dashboard or live log) keeps SSE/websocket connections open, so `networkidle` never
  settles and the step just times out. Navigate with the default `wait_until: load` and then
  `wait_for` the specific element you need.
- **Slow real work gets its own `timeout_ms`.** Every waiting step gives up after
  `step_timeout_ms` (script-wide, default 15000). When the screen shows something that takes
  real time — an agent run (often 17–45s), a build, an install — give THAT step its own
  ceiling: `- wait_for: {text: "Run complete", timeout_ms: 90000}`. The per-step max is
  180000 (3 min); asking for more fails validation — wait for an intermediate sign of progress
  first and split the wait. Every step is also bounded by what's left of `total_timeout_s`
  (default 300, max 900), so raise that for a long take. Speed-ramp the wait in the render.
- `hold: 1200`–`2000` on every frame a viewer must read (a dialog, a result). Too short is the
  most common reason a clip is useless.
- `type` with `delay_ms: 35–60` for realism; `fill` when the typing isn't the point.
- `mark` before and after each beat. Marks are how `campaign_render` sets the clip's start/end
  and speed-ramps dead time (e.g. ≤4× between `typed` and `sent`) without guessing seconds.
- `screenshot` the frames you'll want as stills or card images.

## One idea per clip

A hero clip shows ONE thing working end to end. If the script has two "and then"s, split it.
Length targets are intent, not a rule — whatever the plan's sourced assumptions say — but see
the next section before you cut for length.

## Continuous action — no jump cuts

Operator feedback on real launch clips: *"too many cut frames, missing chunks of action."* A
viewer can't follow what they didn't see happen, and a cut inside the action reads as a glitch
or as faked. So:

- **No hard cuts between the action's start and its result.** Trim only the head (before the
  action starts) and the tail (after the held result). Script one take that runs straight
  through; never stitch beats from different points of a take.
- **Compress time with speed only.** A uniform ~1.5–2× over the whole run keeps it brisk and
  still readable. Ramp harder (eased, **≤4×**) only over *pure dead time* — typing, a spinner,
  a progress bar with nothing new on screen. `campaign_render` flags a ramp above 4× as a jump cut
  (a `continuity` warning in its report) unless the output says `continuous: false` — reserve
  that for a deliberate time-lapse; a flagged ramp is a re-render, not something to ship. A uniform
  speed-up is one ramp over the whole run (`{from: start, to: end, factor: 1.5}`); ramps can't
  overlap, so to combine it with a faster dead-time ramp split the run into adjacent ramps
  (`start→typed` at 3, `typed→end` at 1.5).
- **Never ramp past a moment where something new appears.** A panel opening, a row landing, a
  status changing, text streaming in — those play at the run speed. Put a `mark` right before
  each so the ramp ends there.
- **Longer beats gappy.** A 25–35 s hero that shows everything beats a 15 s one with holes. If
  it's over a size limit, crop or drop width/fps — don't cut the action.
- **Hold ~2.5 s on the result** (`hold: 2500` after it lands) so it can be read before the
  clip ends or loops.

These lengths are for a standalone clip (a lane's hero, a GIF). Recording a **beat for a
montage**? The same continuity rule holds, but the `montage-editing` skill owns its length
(4–8 s on screen), geometry (1920×1080, full frame) and theme-per-beat — follow it there.

## Showcase the app's own views, not just the input

Operator feedback: *"can't see the note view, only the chat screen, so we don't see the note
being added."* Most apps have a surface where you ask (a chat, a command bar, a form) and a
surface where the result lives (a side panel, a rail view, a board, a document, a terminal, a
list). A clip of only the input surface ending on "Done" proves nothing. So:

- **Before triggering the action, open the view where the result lands**, docked beside the
  input (split view, side panel), so the result appears live on camera as it happens. `wait_for`
  the view to be ready *before* the action step.
- **End on that view showing the result**, not on a "Done"/"Saved" message in the input.
- **Crop to input + view** in the render — the two panes, not the whole window's chrome.
- **Embedded views are often iframes** (in protoAgent, every plugin view is) — target inside
  them with `frame:` (see below). `wait_for` the new item inside the frame so the take fails
  loudly if the result never shows up there.

### Worked example — chat on the left, notes panel on the right

```yaml
base_url: http://localhost:7870
viewport: {width: 1440, height: 900}
color_scheme: dark
timezone_id: UTC
locale: en-US
redact: {presets: [home_paths, emails, secrets]}
steps:
  - goto: /
  - wait_for: {role: textbox, name: Message}
  # 1. Stage the result's view FIRST, docked beside the chat.
  - click: {role: button, name: Notes}
  - wait_for: {frame: {url: /plugins/notes/view}, timeout_ms: 20000}
  - wait_for: {text: Notes, frame: /plugins/notes/view}
  - hold: 800
  - mark: start
  # 2. The action — typing is dead time, so it may be ramped (≤4×) in the render.
  - type: {target: {role: textbox, name: Message}, text: "Save a note: ship the beta on Friday", delay_ms: 40}
  - mark: typed
  - press: {key: Enter, target: {role: textbox, name: Message}}
  - mark: sent
  # 3. The result lands IN THE VIEW — wait for it there, not for "Done" in the chat.
  - wait_for: {text: "ship the beta on Friday", frame: /plugins/notes/view, timeout_ms: 90000}
  - mark: result
  # 4. Hold on the note so it can be read.
  - hold: 2500
  - screenshot: {name: note-landed}
  - mark: end
```

Render it continuous — no interior cuts, ramp only the typing, crop to the two panes:

```yaml
outputs:
  - {name: hero, format: mp4, start: start, end: end,
     speed: [{from: start, to: typed, factor: 3}],   # typing only; nothing new appears
     crop: {x: 0, y: 56, width: 1440, height: 844}, limit: github_attachment_video_free}
```

The agent's reply streaming in and the note appearing (`sent` → `result`) play at 1×: that's
the moment the clip exists to show. (As a montage beat the same script works — record it at
1920×1080 per `montage-editing` and give the beat a `focus` on the notes panel for vertical cuts.)

## Determinism

- Always set `timezone_id` and `locale` — clocks and dates otherwise leak the machine's.
- Set `color_scheme` explicitly (dark/light) to match the plan.
- The VIDEO is recorded at the viewport's CSS pixels — `device_scale_factor` sharpens the
  `screenshot` stills only (Playwright's screencast doesn't upscale). So pick the viewport for
  the clip: 1280×800 is a sensible default; for a sharper clip of one region, record a larger
  viewport and `crop` to the region in `campaign_render`. Mobile takes: e.g. 390×844.

## Redaction — before the first frame

```yaml
redact: {presets: [home_paths, emails, secrets]}   # /Users/<name> → ~, emails, API keys/JWTs
mask: {selectors: [".account-menu", "[data-private]"], mode: blur}
```

`mode`: `blur` (default), `hide` (keeps its space), or `remove` (collapses it — good for
banners and toasts that aren't part of the story). Top-level `redact`/`mask` apply from the first frame; a `- mask:` step applies from that point.
If the app shows tokens, paths, emails, internal hostnames or customer data anywhere in frame,
mask it — then LOOK at the stills with `campaign_view`; redaction is a safety net, not proof
(it can't reach text drawn on a canvas, e.g. a terminal — see *Inside an iframe* above).

## Auth

Never put a token in a script (it's stored in the plan). Use `auth: {bearer_env: CAMPAIGN_APP_TOKEN}`
— only env vars named `CAMPAIGN_*` (or listed in the operator's `bearer_envs` setting) are
allowed, it needs a `base_url`, and the token is sent to that origin only. If the target needs a
token and none is set up, ask the operator to create one; never reach for another secret. Or use
`auth: {storage_state: /path/state.json}` from a logged-in session.

## When a take fails

The error names the step, the cause, and a `failure.png`. Look at the screenshot first:
- element not found → the page wasn't ready (add `wait_for`) or the name differs (re-snapshot);
- `matches N elements: 1) … 2) …` → an action's target is ambiguous: `exact: true`, a
  narrower role/selector, or `nth` (the listed order is the `nth` order);
- element not found but you can SEE it → it's inside an iframe (a plugin view): add `frame:`;
- timeout on `network_idle` → the app polls or streams (SSE); wait for a visible element instead;
- `Timeout 15000ms exceeded` on something that is just slow → give that step `timeout_ms`.
- panels/tabs at the wrong size or state in the first frame → seed it with `storage:`;
- `upload … refused` → the file isn't under `upload_dirs` (or is a credential) — move a demo
  file into the allowed folder, or ask the operator; never work around it.
Fix that ONE step and re-shoot. Same step failing three times → report to the operator with
the screenshot rather than flailing.
