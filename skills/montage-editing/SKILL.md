---
name: montage-editing
description: >-
  Use to plan, storyboard and cut a launch MONTAGE in Campaign Studio — many short clips of
  different app workflows, each recorded in a different app theme, cut together with colour
  wipes keyed to each beat's theme colour, plus tagline cards and an end card with the CTA.
  Covers recording beats for a montage (1920x1080, full frame), pacing, one idea per beat,
  theme colour per beat, tagline cadence, the end card, and vertical/square cuts with focus.
  Triggers: "make a montage", "launch video", "sizzle reel", "cut these clips together",
  "a reel of the app", "trailer", "vertical cut for shorts/reels".
tools: [campaign_montage, campaign_storyboard, campaign_view, campaign_assets, campaign_shoot, campaign_render, campaign_card, campaign_asset_update, campaign_limits, campaign_get]
---

# Cutting a launch montage

A montage sells *range*: in 30–60 seconds the viewer sees the app do six different things, each
in its own colour, and leaves with one line and one URL. Every beat is a real, continuous
recording — the montage only trims the ends, sets one speed, and joins beats with transitions.

## 1. Record the beats for a montage

- **One take per beat, one idea per take.** "Schedules a job", "Delegates to Claude Code",
  "Dark theme, plugin view open" — if you need *and* to describe it, it's two beats.
- **Canonical geometry: `viewport: {width: 1920, height: 1080}`, `device_scale_factor: 1`,
  full frame, no crop.** Every beat then shares one geometry: 1920×1080 clips pass straight
  through the landscape cut untouched (no scaling, no letterbox) and transitions line up pixel
  for pixel. Anything else is letterboxed and the montage reply names it — re-record it.
- **A different app theme per beat** (different accent + background). Note each beat's accent
  as its `theme_color` — the colour wipe INTO that beat sweeps in that colour, so the cut
  itself announces "new theme, new idea".
- Mark the beat's best stretch (`mark: start` / `mark: end`) and hold 1–2 s on the payoff so
  the trim has room. Redact and fix the timezone as for any take (shot-scripting skill).

## 2. Write the sequence

```yaml
- {card: {title: "Your agent. Your data. Your way.", accent: "#7c5cff"}, duration: 2.5}
- {clip: 41, in: 1.2, out: 7.0, label: "Schedules", theme_color: "#22c55e"}
- {clip: 44, in: 0.5, out: 9.5, speed: 1.5, label: "Delegates to Claude Code", theme_color: "#f97316"}
- {clip: 47, out: 6.0, label: "Your own plugins", theme_color: "#06b6d4", transition: smoothleft}
- {card: {title: "Runs on your machine.", subtitle: "Your keys. Your models."}, duration: 2}
- {clip: 52, in: 2.0, out: 8.0, label: "Any model", theme_color: "#e11d48"}
- {card: {title: "protoAgent", url: "github.com/protoLabsAI/protoAgent", cta: "Star it on GitHub",
          logo: true}, duration: 3.5, zoom: true}
```

- **Pacing: 4–8 s on screen per beat** (after speed). Under ~3 s the viewer can't read what
  happened; over ~8 s the montage turns into a demo. Aim for 6–9 beats in a 45–60 s cut.
- **Continuous footage only.** `in`/`out` trim the ENDS; `speed` is one uniform factor
  (≤ 4×; 1.25–2× tightens typing without looking fast-forwarded). There are no interior cuts —
  if a beat needs one, it is two beats, or ramp it with `campaign_render` first and montage
  the rendered clip.
- **Labels**: 2–4 words, the capability not the click ("Schedules", not "Clicking the
  schedule button"). Not every beat needs one; never more than one line.
- **Tagline cadence**: open with the promise card, then one short card every 2–3 beats
  (a 3-part line split across the reel works well: "Your agent." … "Your data." … "Your
  way."), 1.5–2.5 s each. Cards are breaths — never two in a row except before the end card.
- **End card last, 3–4 s**: the wordmark (`logo: true`), the repo URL (`url`), one CTA
  (`cta`). `zoom: true` gives it a slow 4 % push so it doesn't feel frozen.
- **Transitions**: the default `colorwipe` (0.45 s) between beats; `fade` into and out of
  cards reads calmer; `cut` for a deliberate hard beat. Don't mix more than two styles in one
  reel. A transition can't eat a beat — the tool refuses ones longer than the beat allows.

## 3. Storyboard, LOOK, then render

1. `campaign_storyboard(campaign_id, sequence, output)` — you get the contact sheet as a
   picture plus the cut sheet. Check: every beat frames its subject; neighbouring theme colours
   differ; cards are legible; the total length is what the plan wants.
2. Fix and re-storyboard until it reads. It's cheap; the full render is not.
3. `campaign_montage(campaign_id, sequence, output={name: launch, limit: x_video})`. It refuses
   unknown keys, missing, rejected or superseded clips (the error names the replacement — swap
   it in), and a cut that breaks the limit's length; clips not
   approved yet are allowed but the montage is marked **DRAFT INPUTS** — get them approved.
4. `campaign_view(asset_id=<montage>, frames=12)` and `around=<each transition time>` from the
   cut sheet. Then the asset-review self-check and `ready_for_review`.

## 4. Vertical (Shorts/Reels) and square cuts

- `output: vertical` (1080×1920) or `square` (1080×1080), same sequence. Without `focus`, a
  1920×1080 beat is centre-cropped to the canvas shape.
- Give each beat a **`focus: {x, y, w, h}`** in SOURCE pixels (of the 1920×1080 take) around
  what matters — the plugin view, the result panel, the toast. The crop grows that area to the
  canvas's shape around its centre and stays inside the frame; a focus that runs outside the
  source is refused. Use `campaign_view` on the take's still to read off the coordinates.
- Vertical labels sit above the bottom ~22 % (the platform's caption and buttons live there);
  keep titles short — a vertical card is narrow.
- `youtube_shorts` as the limit enforces vertical/square and ≤ 3 min; check `campaign_limits`
  for the platform you're cutting for.

## Never

- Never present a DRAFT-INPUTS montage as shippable — say which inputs still need approval.
- Never speed a beat past 4× or splice the middle out of one: it reads as a jump cut.
- Never invent a number for a card ("10× faster") the plan doesn't source.
