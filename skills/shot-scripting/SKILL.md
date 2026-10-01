---
name: shot-scripting
description: >-
  Use to write a robust Campaign Studio shot script — the declarative YAML that records a
  deterministic browser take of any web app — and to fix one that failed. Covers targets that
  survive restyles, readable holds, marks for trimming, redaction of secrets, and fixed
  timezone/locale. Triggers: "record a demo of", "capture a clip of", "shoot the flow",
  "the take failed", "re-record", "make a GIF of the app doing X".
tools: [campaign_script_save, campaign_shoot, campaign_get, browser_open, browser_snapshot, browser_screenshot]
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

A target matching several elements fails loudly (strict mode). Narrow it, or add `nth`.

## Timing — a viewer has to read it

- `wait_for` before every click on something that loads (`{text: …}`, `{role: …}`, or
  `{network_idle: true}` after a navigation). Never a bare `hold` as a "wait".
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
mask it — then CHECK the stills; redaction is a safety net, not proof.

## Auth

Never put a token in a script (it's stored in the plan). Use `auth: {bearer_env: CAMPAIGN_APP_TOKEN}`
— only env vars named `CAMPAIGN_*` (or listed in the operator's `bearer_envs` setting) are
allowed, it needs a `base_url`, and the token is sent to that origin only. If the target needs a
token and none is set up, ask the operator to create one; never reach for another secret. Or use
`auth: {storage_state: /path/state.json}` from a logged-in session.

## When a take fails

The error names the step, the cause, and a `failure.png`. Look at the screenshot first:
- element not found → the page wasn't ready (add `wait_for`) or the name differs (re-snapshot);
- several matches → narrow the target or add `nth`;
- timeout on `network_idle` → the app polls; wait for a visible element instead.
Fix that ONE step and re-shoot. Same step failing three times → report to the operator with
the screenshot rather than flailing.
