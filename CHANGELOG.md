# Changelog

## 0.6.0

- **The protoContent archive can act as a GitHub App (protoquinn) instead of a personal token.**
  New settings `archive_app_id` (0 = off), `archive_app_installation_id` (0 = discovered from
  `archive_repo`) and `archive_app_private_key_path`, and a new secret `archive_app_private_key`
  (the app's PEM — a one-line paste from Settings is re-wrapped, so dropped newlines don't
  matter). When an app id is set the app wins over `archive_token`. Each pass signs an RS256
  JWT and mints an installation token **scoped to the archive repo only** (Contents + Pull
  requests write), cached until 5 minutes before expiry and re-minted once on a 401. Commits and
  the catalog PR are authored by `protoquinn[bot]`. The key, JWT and minted token are redacted
  from every error, log line and status. `PyJWT[crypto]` (already shipped by protoAgent via
  `mcp`) is declared as an optional host dep.
- **`GET /archive` and `campaign_backup` show the active auth mode** — `app`, `token` or `none`.
- **Catalog PRs are safe to auto-merge.** protoquinn may merge a catalog PR as soon as
  protoContent's CI is green, so each PR opens only after its branch holds the whole catalog,
  and if a PR merges while a later commit is landing, the pass reopens a fresh PR with the full
  catalog instead of stranding the new entries on a merged branch.

## 0.5.1

- **The gallery shows which approved assets are backed up.** An approved asset whose file is on its
  campaign's protoContent release and listed in the catalog gets a **backed up** badge. Hovering
  shows the release and when it was uploaded. The asset API returns this as `archived` (`at`,
  `release_tag`); it's `null` until the catalog PR has the entry.

## 0.5.0

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
- **Collections — gather the keepers, start fresh.** `campaign_collect` moves or copies assets
  into another campaign **with their lineage** (the takes and renders they were made from, a
  montage's inputs), dry run first. Move keeps ids, files and approvals; copy duplicates the
  files into the target's folder and re-points every id and path. `campaign_archive` hides a
  campaign from `campaign_list` and the gallery picker (**Show archived**) and restores its old
  status on the way back. The agent can't move an approved asset and its copy of one lands for
  review; the operator's `POST /campaigns/{id}/collect` and `/archive` routes do both.
  `GET /campaigns` now leaves archived campaigns out unless `include_archived=true`.
- **Approved media is backed up to protoContent.** A background pass finds every approved asset
  that isn't backed up yet (no hook needed — it reconciles each minute while enabled), uploads the file as a release asset (release `media/<campaign>`, never "latest",
  content-addressed so the same bytes upload once), records `meta.archive = {url, sha256, at}`,
  and batches the campaign's catalog (`specs/media/catalog/<slug>.json`) onto ONE open PR per
  campaign in protoContent — never merged by the plugin. Retries with backoff; idempotent by
  sha256 (catalog entries upsert by sha256); `campaign_backup(backfill=true)` / `POST /archive/backfill`
  queue every approved asset.
  New settings `archive_enabled`, `archive_repo`, `archive_catalog_dir`, and the secret
  `archive_token` (fine-grained PAT on protoContent only: Contents RW + Pull requests RW); all
  operator-only. Off by default.

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
