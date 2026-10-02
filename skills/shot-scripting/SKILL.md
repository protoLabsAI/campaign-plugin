---
name: shot-scripting
description: >-
  Use to write a robust Campaign Studio shot script — the declarative YAML that records a
  deterministic browser take of any web app — and to fix one that failed. Covers targets that
  survive restyles, readable holds, marks for trimming, redaction of secrets, and fixed
  timezone/locale. Triggers: "record a demo of", "capture a clip of", "shoot the flow",
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
- `mark` before and after each beat. Marks are how `campaign_render` trims and speed-ramps
  (e.g. 4× between `typed` and `result`) without guessing seconds.
- `screenshot` the frames you'll want as stills or card images.

## One idea per clip

A hero clip shows ONE thing working end to end. If the script has two "and then"s, split it.
Length targets (e.g. "15–30s hero") are intent, not a rule — whatever the plan's sourced
assumptions say.

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
Fix that ONE step and re-shoot. Same step failing three times → report to the operator with
the screenshot rather than flailing.
