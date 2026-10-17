# Changelog

## Unreleased

- **Retention — the gallery prunes its intermediates (report-only until you flip it).** A
  daily background pass applies the policy: superseded files go 7 days after they were
  superseded (the row stays as a tombstone, so the retake chain still reads); rendered and
  captured intermediates go after 7 days unless something kept depends on them; planned rows
  with no file drop after 30 days. **Approved and ready-for-review assets are never deleted**;
  ready-for-review ones idle 14+ days get a **stale** badge. Dependencies are a closure over the
  parent chain, montage inputs, bookended raw masters and a take's stills — when in doubt, it
  keeps. New setting `retention_mode` (`report` default · `prune` · `disabled`, operator-only),
  new tool `campaign_prune` (dry run by default), and operator routes `GET /retention` /
  `POST /retention/prune`.

## 0.4.3

- **Gallery timestamps — see when a clip was made** (#27). Each card shows a relative time
  ("12 min ago") with the absolute local date + time on hover: the file's make time is the
  shot/render folder stamp (`YYYYMMDD-HHMMSS`, UTC), falling back to the file's mtime; a render
  also names its source take's time, and a planned row's own creation time shows when it differs.
- **Sort control:** newest first (default), oldest first, or asset #.
- **The retake chain reads by time:** "Superseded by #42 · 3 min later" on the old take, and
  "Replaces #41 · 3 min earlier" on the new one.
- `GET /api/plugins/campaign/campaigns/{id}/assets` adds `created_at`, `updated_at`, `made_at`,
  `made_from` (`stamp` | `mtime` | ``) and `take_at` per asset (no schema change — the rows
  already carried `created`/`updated`).

## 0.4.2

- **protoLabs bookends v1 are the default everywhere.** The protoLabs brand kit carries them
  (`bookends:` in `brands/protolabs/brand-kit.yaml`, with the real splash bumper
  `protoagent-splash.mp4`): splash → 300 ms fade → title card → edge sweep → content → edge sweep →
  bumper → 300 ms fade → Star CTA. `campaign_produce_video` defaults to them; montages and
  storyboards add them (`output.bookends`: auto | true | false — false for loop tiles); every
  `campaign_render` mp4 gets them (`bookends: brand | none`). Operator intro/outro settings win.
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
