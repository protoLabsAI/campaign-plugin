# Agent tools reference

The tools Campaign Studio gives the agent. They are ordinary LangChain tools, so any agent on
the instance can call them. None of them approves or rejects an asset, posts anywhere, or turns
deletion on: those are the operator's (the gallery, the review route and the `retention_mode`
setting).

`tests/test_docs_reference.py` fails if a tool is added, renamed or removed without this file
changing.

| Tool | What it does |
|---|---|
| `campaign_create` / `campaign_update` / `campaign_list` / `campaign_get` | The plan: goal, target metric, launch window, target URL, goal math, sourced assumptions, channel plan, do-not list |
| `campaign_collect` / `campaign_archive` | Collections: move or copy assets (with their lineage) into another campaign; archive a campaign (hidden by default) or bring it back (see [Collections](media-and-backup.md#collections--keep-the-good-ones-together-start-fresh)) |
| `campaign_lane` | A lane: name, pitch, audience, hero asset |
| `campaign_asset_add` / `campaign_asset_update` / `campaign_assets` | The shot list. Status `planned → scripted → captured → rendered → ready_for_review`, or `superseded` (with `superseded_by`) to retire a replaced take; `ready_for_review` is refused if the file is missing or breaks its hard limit; `approved`/`rejected` are refused outright, and so is superseding an approved asset. `campaign_assets` hides superseded takes unless `include_superseded=true`, shows the operator's review note, and previews each asset's notes with `notes=true` |
| `campaign_asset` | One asset in full: its notes and review note (untruncated), the asset it was made from and the ones made from it (in any campaign), its marks, render spec, warnings and other metadata. Asked with the wrong `campaign_id`, it names the campaign that holds the asset |
| `campaign_milestone` / `campaign_decision` | Dated milestones (owner agent/operator); operator decisions with options + a recommendation |
| `campaign_status` | Progress + who each open item is waiting on; includes `show_component` payloads |
| `campaign_script_save` | Validate + save a NEW shot script (`script="template"` returns an annotated example). Replies with a one-line confirmation, never the YAML; saving an identical script again says "Unchanged" and points at its id |
| `campaign_script_get` | Read a saved shot script back as YAML, each step commented with its number; `steps="20-30"` shows only those steps (plus the settings) |
| `campaign_shoot` | Record a take (Playwright, headless Chromium, `record_video`) and register the webm + stills. **The iteration path:** re-shoot a saved script by `script_id` with `overrides` — `{N: …}` fixes step N (a whole step replaces it, options merge into it), a top-level setting (`{"base_url": …, "color_scheme": …}`) replaces that setting — validated like any script and saved, so the YAML is never resent. Replies with a short summary (asset id, take dir, duration/size, marks, still ids; a failure's error once, without its call log); the full error, timing log and failure still stay on the asset (`campaign_asset`) |
| `campaign_sandbox_reset` | Clear a protoAgent **sandbox** console between takes — its chat sessions, artifacts and goals (`clear=`), and optionally its theme preset (`theme=`). Targets only a console a saved script records (`script_id`, or a `base_url` matching one), with that script's auth, through the browser worker. Refuses a non-loopback console, the operator's :7870 / :7871 instances, and this agent's own console (or an unknown own console) before sending anything |
| `campaign_render` | mp4 / gif / poster outputs via ffmpeg, under a hard limit, with optional timed `captions` (see [campaign_render specs](#campaign_render-specs)). Replies with one line per output (asset id, file under one render dir, size, dims, duration), a captions line and one line per warning; attempts, specs and the captions used stay on the asset |
| `campaign_card` | Render a branded card template to PNG |
| `campaign_view` | LOOK at a still/card/poster, or frames of a clip (evenly spaced, `every_s` apart from `start` to `end` — paged 12 per contact sheet, the reply naming the next page's `start` — or either side of a mark/cut), as images the model sees — downscaled JPEGs, ≤ 3 per call; `crop={x,y,width,height}` in source pixels cuts out a region first so it comes back at 100 % (how a 2560- or 3840-wide master's smallest text is checked), contained to the campaign's dir. Needs a core with `graph.sdk.multimodal_tool_result` and a vision model; otherwise it says it couldn't show them |
| `campaign_montage` | Cut an ordered `sequence` of clips + cards into one mp4 (see [Montages](montages-and-branding.md#montages)) and register it as a `montage` asset with its poster (+ GIF) |
| `campaign_storyboard` | The montage's contact sheet — one frame per item, a swatch per transition — returned as an image, plus the cut sheet |
| `campaign_produce_video` | One shot script → a finished, self-reviewed video as a background job: record (2K device capture) → [brand intro] + walkthrough + [brand outro] → gates → deliver (see [Videos from a request](videos-from-a-request.md)) |
| `campaign_video_status` | A video job's stage, progress, gate table (one line per gate), files (one line each, under one directory) and links, and the last 3 log lines; `wait_s` blocks until it ends; `verbose=true` adds the whole job log |
| `campaign_limits` | The hard-limit table, each row with its source URL and as-of date |
| `campaign_setup` | What's installed and how to fix what isn't |
| `campaign_backup` | The protoContent backup of approved media: archived / queued / retrying; `backfill=true` queues every approved asset (see [Archive to protoContent](media-and-backup.md#archive-to-protocontent)) |
| `campaign_prune` | Retention: what the prune would delete, with sizes, by rule (dry run by default); `dry_run=false` deletes only once the operator set `retention_mode: prune` (see [Retention](media-and-backup.md#retention)) |

## campaign_render specs

Each item of `outputs` is a mapping with these keys — **and only these**. A key it doesn't know
(at the top level, in a `speed` ramp, a `crop` or a caption) is refused before anything renders,
naming the key, the allowed set and the likely intent (`'ramps' → did you mean 'speed'?`).

| Key | |
|---|---|
| `name`, `format` | required; `format` is `mp4`, `gif` or `poster` |
| `title` | the asset's title (and the bookends' title card) |
| `start`, `end` | seconds of the take or a mark name |
| `speed` | ramps `[{from, to, factor, ease}]` — source seconds or marks |
| `continuous` | default `true`: warn on ramps that read as jump cuts |
| `crop` | `{x, y, width, height}` in source pixels |
| `width`, `fps`, `crf` | output size, frame rate, quality (crf 16 default) |
| `limit`, `max_bytes` | a hard-limit id (`campaign_limits`) and/or a byte ceiling |
| `at` | a poster's frame |
| `bookends` | mp4: `brand` (default) or `none` |
| `captions` | mp4/gif: timed lower thirds — below |

**`captions`** is a list of `{text, start, end}` or `{text, at}`:

```json
"captions": [
  {"text": "Ask the agent for a feature spec", "start": 0.5, "end": "form_shown"},
  {"text": "It builds the spec form; send it", "at": "form_shown"},
  {"text": "A card lands on the board", "at": "card_created"}
]
```

- `start` / `end` / `at` are **source** seconds or marks — the take's clock, the same one the
  `speed` marks use. They're mapped through the ramps onto the output, so a caption stays on its
  action however the ramps change. `{text, at}` ends when the next caption starts (or the clip ends).
- One caption on screen at a time (overlapping `start`–`end` ranges are refused). Each stays up at
  least **1.8 s of output**; a caption a ramp squeezes shorter is held longer (the next one waits)
  and the reply says so in a warning.
- Text wraps to at most **2 lines** at the output's width (shorter on 9:16, which also keeps to the
  vertical safe area); longer text is refused with how many characters fit.
- Captions are burned into the clip only: the brand bookends wrap the captioned clip, so the cards
  never carry one. The take is never modified.
- The asset records `meta.captions` (the list, in source seconds — pass it back as `captions` to
  re-render) and `meta.caption_timing` (each caption's output window).

## Skills

`campaign-planning`, `shot-scripting`, `asset-review`, `montage-editing`,
`produce-walkthrough-video` (in `skills/`, auto-discovered by the host).

## The producer subagent

`campaign_producer` records, renders and self-checks the agent-owned assets of a plan. Its model
is the `producer_model` setting (blank = the host's aux-model routing). Its allowlist
(`subagents.py`, `PRODUCER_TOOLS`) is:

- `current_time`
- this plugin's production tools only: `campaign_get`, `campaign_assets`,
  `campaign_asset`, `campaign_asset_update`, `campaign_status`, `campaign_limits`, `campaign_setup`,
  `campaign_script_save`, `campaign_script_get`, `campaign_shoot`, `campaign_sandbox_reset`, `campaign_render`,
  `campaign_card`, `campaign_view`, `campaign_montage`, `campaign_storyboard`. Not the plan
  editors, collections, the archive, retention or `campaign_produce_video`.
- core `agent_browser`'s `browser_open`, `browser_snapshot`, `browser_screenshot`,
  `browser_get_text`, to explore a target before scripting it;
- Social Studio's `social_brand_kit` (read-only) and the artifact plugin's `show_artifact`.

The other plugins' tools are named, never imported; a name whose plugin isn't enabled simply
isn't bound.
