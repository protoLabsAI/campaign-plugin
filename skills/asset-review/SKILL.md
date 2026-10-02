---
name: asset-review
description: >-
  Use before marking any Campaign Studio asset ready_for_review — the self-check that a clip,
  GIF, still or card is legible at half size, shows nothing secret, fits its hard limit, and
  loops cleanly — and when an operator rejects one. Triggers: "is it ready", "mark it ready",
  "check the render", "review the assets", "the operator rejected", "why was it rejected".
tools: [campaign_view, campaign_assets, campaign_asset_update, campaign_render, campaign_shoot, campaign_limits, campaign_status]
---

# Self-check before ready_for_review

The operator's review is the last gate, not the first. Hand them only things you'd ship.
`campaign_asset_update(id, status="ready_for_review")` refuses a missing file or a broken hard
limit; everything below is on you.

## Look first — with `campaign_view`, never from the path

You can only review what you have SEEN. `campaign_view` returns the picture to you; a file path
or a render report is not a review. Before `ready_for_review` you must have viewed:

- **every still** of the take — `campaign_view(campaign_id, asset_id=<still id>)` for each
  (the take's stills are listed in `campaign_assets` with `kind=still`), plus every card and
  poster you're handing over;
- **the clip's flow** — `campaign_view(campaign_id, asset_id=<clip id>, frames=6)` (one
  contact sheet; use `frames=3` for full-size frames when text must be read);
- **both sides of every cut** — `around=<mark>` on the take for each mark you trimmed or
  speed-ramped at (`around` also takes seconds in the rendered file), so a jump never lands on
  a half-drawn dialog, a spinner, or a frame with something private in it.

At most 3 images come back per call — call again rather than skipping. If `campaign_view`
says vision isn't available, you have NOT reviewed the asset: say so in the ready note and ask
the operator to look in the gallery.

## What to check in every image

1. **Legible at 50% scale.** People watch in a feed, small. Squint at it half-size: can you
   read the one thing the clip is about? `campaign_view(…, max_side=640)` shows you roughly
   that size. If not: crop to the region in the render, record a larger viewport, or zoom the
   app before the take — not "it's fine full-screen".
2. **Nothing secret on screen.** Read every line of text in frame, including text drawn on
   a canvas or in a terminal pane (redaction only rewrites the page's DOM text — it can't touch
   canvas/terminal text): home paths with a real username (`/Users/<name>`, `C:\Users\…`),
   emails, usernames and handles, API keys, tokens, `.env` values, internal hostnames and IPs,
   other people's data, notification toasts, a browser profile name. Check EVERY still and both
   sides of every cut. Found something → fix the script's `redact`/`mask` (or the terminal's
   contents) and re-shoot; never "it's only a frame".
3. **No clutter.** Stray toasts, open menus, devtools, debug banners, empty states, a half-loaded
   page or a spinner at a cut, the pointer parked on the text.
4. **The right theme.** The `color_scheme` the plan asks for, the brand's look on cards, and no
   mixed light/dark panels.
5. **Under its hard limit.** The render report states size and dims and flags violations.
   `campaign_limits` is the table. Over → re-render with a tighter cut, a speed ramp over dead
   time, a crop, or a smaller width — never by dropping the limit.
6. **Loops cleanly (GIFs).** The last frame should lead back into the first: end on a held
   result frame that's close to the opening, or cut so the loop point is a natural pause.
7. **Starts on the action.** No blank page / loading spinner at the head — trim to the first
   mark. No dead air in the middle — speed-ramp it.
8. **Matches the spec and the plan's do-not list.** One idea, the lane's pitch, nothing the
   do-not list rules out (fabricated numbers, real user data).

Then mark it ready, and say in one line what the operator is approving, where it ships, and
which images you viewed (still ids, frames, cuts).

## When the operator rejects

The rejection note is in `campaign_assets` (`Review: …`) and the asset shows under "on the
agent" in `campaign_status`. Treat the note as the spec for the next take: fix the script or
render, produce a NEW asset, and mark that ready. Don't argue with the note in the asset; if
you think it's wrong, ask in chat.
