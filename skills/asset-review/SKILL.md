---
name: asset-review
description: >-
  Use before marking any Campaign Studio asset ready_for_review — the self-check that a clip,
  GIF, still or card is legible at half size, shows nothing secret, fits its hard limit, and
  loops cleanly — and when an operator rejects one. Triggers: "is it ready", "mark it ready",
  "check the render", "review the assets", "the operator rejected", "why was it rejected".
tools: [campaign_assets, campaign_asset_update, campaign_render, campaign_shoot, campaign_limits, campaign_status]
---

# Self-check before ready_for_review

The operator's review is the last gate, not the first. Hand them only things you'd ship.
`campaign_asset_update(id, status="ready_for_review")` refuses a missing file or a broken hard
limit; everything below is on you.

Open the file (or its poster / the take's stills — paths are in `campaign_assets`) and check:

1. **Legible at 50% scale.** People watch in a feed, small. Squint at it half-size: can you
   read the one thing the clip is about? If not: crop to the region in the render, record a larger viewport, or zoom the
   app before the take — not "it's fine full-screen".
2. **Nothing secret on screen.** Home paths with a real username, emails, API keys, tokens,
   internal hostnames, other people's data, notification toasts, a browser profile name. Check
   EVERY still, and scrub the clip at the marks. Found something → fix the script's
   `redact`/`mask` and re-shoot; never "it's only a frame".
3. **Under its hard limit.** The render report states size and dims and flags violations.
   `campaign_limits` is the table. Over → re-render with a tighter cut, a speed ramp over dead
   time, a crop, or a smaller width — never by dropping the limit.
4. **Loops cleanly (GIFs).** The last frame should lead back into the first: end on a held
   result frame that's close to the opening, or cut so the loop point is a natural pause.
5. **Starts on the action.** No blank page / loading spinner at the head — trim to the first
   mark. No dead air in the middle — speed-ramp it.
6. **Matches the spec and the plan's do-not list.** One idea, the lane's pitch, nothing the
   do-not list rules out (fabricated numbers, real user data).

Then mark it ready, and say in one line what the operator is approving and where it ships.

## When the operator rejects

The rejection note is in `campaign_assets` (`Review: …`) and the asset shows under "on the
agent" in `campaign_status`. Treat the note as the spec for the next take: fix the script or
render, produce a NEW asset, and mark that ready. Don't argue with the note in the asset; if
you think it's wrong, ask in chat.
