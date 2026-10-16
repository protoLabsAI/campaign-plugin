# Changelog

## 0.4.2

- **Edge sweep only.** The Boot Seam transition is removed (Josh's call): `transition: boot_seam`
  is now a validation error that points at `edge_sweep`, `cut` or `fade`, and it is gone from
  `transitions.json` (profile 2.0.0).
- **Brand intro:** splash → title card is a short crossfade; title card → main clip is the edge
  sweep.
- **Brand outro:** main clip → bumper is the edge sweep; bumper → CTA card is the same short
  crossfade. The final-caption guard and its timing are unchanged.
- **The bumper fade:** `bumper_fade_ms` on the intro and outro presets (default 300; 0 = a cut) —
  a dissolve on the brand easing, frame-quantized like the sweep; reduced motion makes it a cut.
  (`into_cta` overrides the outro's with any style.)
- Montages still default to `edge_sweep` between beats.

## 0.4.1

- Preflight identifies its own console through core's `graph.sdk.console_url()` (#24).

## 0.4.0

- 2K device-pixel capture (`capture: device`), the brand intro/outro, `campaign_produce_video`,
  and the signature transitions (protoContent#606), frame-exact (#16, #17, #21, #23).
