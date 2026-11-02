---
name: viral-clip-craft
description: >-
  Use before planning, shooting or cutting any short clip meant to hold a stranger's attention in
  a feed — a launch teaser, a demo for social, a Reel, a Short. The craft rules that turned rejected
  clips into watchable ones: one job, action in the first seconds, a visible cause for every
  change, zooms, readable replies, captions timed from frames, short bookends, sound off, and
  measuring pacing. Triggers: "viral", "make it pop", "too boring", "punchy clip", "social clip",
  "teaser", "reel", "short", "hook", "pacing", "keep people watching", "auto-zoom", "punch-in".
tools: [campaign_render, campaign_pacing, campaign_view, campaign_shoot, campaign_script_save, campaign_montage, campaign_asset_add, campaign_asset_update]
---

# Clips people keep watching

**Nothing guarantees virality.** This skill makes a clip clear and watchable; reach depends on the
audience, the idea and the platform. Never promise views, and never state a number you didn't
measure.

**How far to trust the numbers.** Netflix's adult limits, 20 characters a second and 42 characters
a line, come from its official Timed Text Style Guide. Wistia reports that videos under a minute
average 52% engagement. Nearly every other number here is vendor-grade guidance that sources
disagree on, or an operator's review of real clips. Treat them as starting defaults, and let the
retention curves of your own posts overrule them.

## Before you shoot

1. **One job per clip.** One real thing a real person does. If the caption needs "and", it's two clips.
2. **Payoff first.** Decide the final frame, then work backward. The viewer should see the result
   land, in the view it lands in.
3. **Check what fits on screen.** At the capture size, confirm the action's title, its control and
   its landing spot all fit. If a kept card lands below the fold, change the data or the flow.
   A sticky header can make scrolling worse: try it before planning around it.
4. **Capture bigger UI, don't zoom later.** A smaller viewport at an integer scale gives larger,
   sharper text for free: **1280×800 at device scale 2** gives 2560×1600. Measure that the layout
   hasn't collapsed to its narrow mode first.
5. **Rehearse on demo data.** Run the exact steps once at the exact viewport. Rehearsal finds the
   redraw that cancels a drag, a below-the-fold landing, and timing that won't work.
6. **Order actions around the app.** If a save redraws the view 6 s later, drags go first. Give
   the view a moment after load before the first drag.

## In the take

- **A visible cause for every change.** Point, pause about 0.4 s on the control so it can be seen,
  then click or drag. **No hotkeys**: a viewer can't see a keypress, so rows just vanish.
  `campaign_script_save` warns on them; `allow_keyboard: true` is only for clips about the keyboard.
- **Something visibly changes every 2–5 s.** Sources range from 1 to 45 s; 2–5 is the working
  default for screen action. A deliberate reading hold is the exception.
- **Hold each result long enough to register**, about 1 s, unless the next action reveals it.

## The cut

- **Action within 3 s, no logo first.** Viewers decide early. `bookends: short` removes the intro.
- **Length: 15–30 s for a teaser.** Sources conflict, and longer can work for a detailed demo. A
  short clip still loses people: Wistia reports viewers of under-a-minute videos watch about half.
- **Bookends: `short`** for social (a 300 ms fade to the CTA held 2 s). The default brand bookends
  add about 11 s, which is too much on a 15 s clip.
- **Zoom on what matters.** `zooms: [{start, end, x, y, factor}]`. Screen Studio's auto-zoom follows
  clicks; it publishes no numbers. Working defaults: about 1.2× when the row and its buttons must
  both stay in frame, 1.5–2.5× for one target, up to 4× only if text stays sharp, 0.45 s ease,
  holding about 1.5 s after a click. Zooming every action wears thin: save it for the moments that
  matter, and end on one.
- **Ramp only dead time.** ≤ 4×, eased, never past something new.
- **Caption timing comes from the frames.** Find each event in the raw take, start the caption
  within 0.2 s of it, and **never before it**. Script marks and predicted waits lie. Keep to one
  short plain phrase in the viewer's own words, no tool names, at most 42 characters a line and
  20 a second. `position: top` moves a caption off something it would cover.
- **Replies must be readable.** Budget at least `characters ÷ 20` seconds for what the viewer must
  read, after it finishes arriving. Push in on it with a zoom, stop at the clip's end, and add
  `hold` to freeze the last frame. A long reply can take less if the bold words carry it, but
  don't cut away from it.
- **End on the payoff**, in its own view, not on a toast or a "Done" message.
- **Design for sound off.** Most feeds autoplay muted, and no audio should carry the meaning.
- **Vertical.** `campaign_montage` with `output: vertical` and a `focus` per beat. Check the
  safe area: the caption keeps left of the action rail and above the bottom strip.

## Check it

1. `campaign_pacing(asset_id)` on every cut. A warn is a thing to look at, not a verdict: a held
   frame while a reply is read is a fair reason to keep one.
2. Walk the clip with `campaign_view`, then do the `asset-review` self-check.
3. **Stills aren't pacing.** Clean frames can still make a boring clip. If you can't watch it in
   real time, say so, and ask the operator to.
4. After posting, **read the real retention curve**, especially the first 3 s and where it
   drops, and feed that into the next clip. It beats every number in this skill.
