# Montages and branding

Cutting many short beats and cards into one launch video, and the brand intro and outro that bookend every clip by default.

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
`transition_duration` (0.45 s), `limit` / `max_bytes`, `crf` (16), `poster`, `poster_at`, `gif`,
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
- **Output**: the graph renders once to a near-lossless master; the mp4 (H.264 High, preset
  slow, yuv420p, `+faststart`) is encoded from it at `crf` 16 (deliverable quality). Under a
  platform `limit` it steps CRF and then a capped bitrate until it fits, warning when that went
  past crf 20; a bare `max_bytes` stops at crf 20 and warns instead
  (see [Deliverable quality](limits.md#deliverable-quality)). A `limit` is checked twice — length/orientation/dimensions before rendering,
  size after. The poster is a PNG at the middle of the first clip (or `poster_at`); the GIF
  (`gif: true`) is kept only if it fits `gif_limit` (GitHub's 10 MB by default).
- **Approval**: a montage may use clips that are only `ready_for_review` (or earlier) — the
  reply warns, the asset's notes start `DRAFT INPUTS: …` and its `meta.unapproved_inputs`
  lists them. Rejected clips are refused, and so are superseded ones (the error names the
  replacement take to use instead).

The montage registers as a `montage` asset (the gallery plays it and filters by it) with
`meta.sequence`, the cut sheet (`meta.timeline`), the encode attempts and the engines used;
its poster and GIF are child assets.

## protoLabs bookends v1 — the default intro + outro

Every clip Campaign produces is bookended by default, the way Josh approved it (vfr-edge-fade,
2026-10-16):

**intro** — the console's real boot-splash bumper → a 300 ms fade (brand easing) → the title card →
an **edge sweep** into the content; **outro** — the content (its final caption held ≥ 4 s, guarded)
→ an **edge sweep** into the bumper → a 300 ms fade → the "Star protoAgent on GitHub" end card.

The recipe lives in the protoLabs brand kit (`brands/protolabs/brand-kit.yaml` → `bookends:`,
generated with the DS snapshot; the bumper is `brands/protolabs/protoagent-splash.mp4`), so with
**Brand kit file** pointed at it, it applies with nothing else configured. The operator's own
**Brand intro** / **Brand outro** settings win over it. Where it applies:

- **campaign_produce_video** — `intro: brand`, `outro: brand` are the defaults.
- **campaign_montage / campaign_storyboard** — `output.bookends` (default `auto`: whenever the brand
  has bookends) wraps the sequence: the intro's title is `output.title` (required while bookends
  apply), the outro follows the last clip. A sequence that already starts with `{intro: …}` /
  ends with `{outro: …}` is left alone. **`bookends: false`** for README loop tiles, short
  GIF-style loops and other cuts that must not have them; `true` makes them required.
- **campaign_render** — each mp4 output takes `bookends: brand` (the default for a full clip) or
  `none`; the plain render is wrapped in the bookends at its own size (title = the output's
  `title`, else the asset's) at the render's own `crf` (16 unless the output names one), and
  the asset points at the bookended file. GIFs and posters never
  get them.

## The video end-card recipe (brand outro)

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

### The brand outro

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

### The brand intro

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
