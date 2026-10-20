# Changelog

## 0.8.0

Less of the campaign tools' output in the agent's context (#48). brandLaunch's night of takes
grew one chat thread from 64k to 478k prompt tokens (132.5M in all), partly because of these
tools' replies and of how the skills told the agent to use them.

- **`campaign_shoot` / `campaign_render` / `campaign_video_status` reply with a short summary.**
  A take names its asset id, its take dir once (files relative to it), duration, size, marks and
  still ids. A failed take quotes its error once, without Playwright's call log, plus the three
  steps before it and the `overrides={N: …}` call that fixes it. A render names its render dir
  once, one line per output and one line per warning. A video job's status shows one line per
  gate and file under one directory and the last 3 log lines; `verbose=true` shows the whole
  log. The full error, timing log, failure still and render attempts stay on the asset
  (`campaign_asset`). Measured on representative replies: shoot 862 → 622 chars, failed shoot
  with a real Playwright error 1,990 → 931, render 1,340 → 774, video status 2,122 → 1,459.
- **Save a script once, iterate by id.** `campaign_shoot(overrides=…)` now also takes top-level
  settings (`{"base_url": …, "color_scheme": …}`) besides step numbers, so one saved script
  records another sandbox or theme without the YAML being resent. `campaign_script_save` says
  "Unchanged" (and points at overrides) when the same script is saved again, and
  `campaign_script_get(steps="20-30")` shows only the steps being fixed.
- **New `campaign_sandbox_reset`.** It clears a protoAgent sandbox console's chat sessions,
  artifacts and goals (and optionally sets its theme preset) between takes, replacing the
  hand-written `browser_eval` cleanup. It only targets a console a saved script records, uses
  that script's auth, runs in the browser worker, and refuses a non-loopback console, the
  operator's :7870 / :7871 instances and this agent's own console before sending anything.
  The producer subagent has it.
- **Skills keep the main thread small.** One asset per `task(campaign_producer)`, a fresh
  sandbox per clip, one 12-frame contact sheet per review pass (1 s pages only over a span the
  sheet flags), and save-once / iterate-by-id in shot-scripting. See
  [Keeping the agent's context small](docs/usage.md#keeping-the-agents-context-small).

## 0.7.1

- **Deliverables render at crf 16, the capture master's quality.** `campaign_render` defaulted
  to crf 23, the brand bookends and montages to crf 20, and `campaign_produce_video`'s review
  cut to crf 18, so a crisp 2K take was re-encoded softer at every step. Every deliverable mp4
  (render, bookends, the produce review cut, montages) now defaults to crf 16 with libx264
  `-preset slow`, yuv420p and `+faststart`. The bookends wrap a render at that render's own crf.
  An explicit `crf` still wins.
- **A size ceiling no longer degrades a deliverable silently.** A bare `max_bytes` with no
  platform limit only steps CRF up to 20 at full width. If the file is still over, it is kept at
  that quality with a `kept deliverable quality …` warning instead of being pushed to crf 27+.
  It is not a violation. A platform `limit` that caps bytes (GitHub's 10 MB) may still step CRF
  and width or bitrate down to fit, but any step past crf 20 is reported as
  `quality traded to fit …`. The render report's flag is now `⚠ notes:` (it was
  `⚠ continuity:`), since it carries these too. See [Deliverable quality](docs/limits.md#deliverable-quality).

## 0.7.0

- **The producer can open the skills its prompt cites** (#42). `campaign_producer`'s prompt told
  it to follow the shot-scripting, asset-review and montage-editing skills, but core binds a
  subagent only the tools it names, and `load_skill` wasn't one of them. It now has `load_skill`
  and `list_skills`, and the prompt says to `load_skill('<name>')` at the step that names each
  skill. A test checks that every skill the prompt cites exists under `skills/`.
- **`campaign_view` takes a `crop`** (#43). `crop={x, y, width, height}` in source pixels (the
  shape `campaign_render` already takes) cuts out a region before any scaling, so a caption bar
  or a corner badge of a 2560×1440 or 3840×2160 master comes back at 1:1 pixels. With a crop,
  `max_side` defaults to the crop's long side and may go up to 4096; the reply says whether the
  image is shown at 100 % or how far it was scaled. A crop outside the frame is refused with the
  frame's size. Contact sheets of a crop stay bounded at 2048 px. The asset-review skill now
  asks for a 100 % crop of the smallest text.
- **Asset notes and metadata can be read back** (#44). New `campaign_asset(asset_id)` prints one
  asset in full: notes and the operator's review note (untruncated), the asset it was made from
  and the ones made from it (in any campaign), marks, render spec, warnings and the rest of its
  metadata (capped). The producer has it. `campaign_assets` now shows the review note the
  asset-review skill already pointed to, and `notes=true` adds a one-line preview of each
  asset's notes.
- **The wrong-campaign error names the campaign that holds the asset.** `campaign_view`,
  `campaign_shoot`, `campaign_card` and `campaign_asset` now say "no asset #865 in campaign 2 —
  it's in campaign 1 (Name); pass campaign_id=1" instead of only "no asset #865 in campaign 2".

## 0.6.4

- **`campaign_view` can look at an asset that was moved in from another campaign.** Moving assets
  (`campaign_collect` / the gallery's move) leaves each file where it was, so a moved row's file
  sits under the OLD campaign's folder, and `campaign_view` refused it as "not a file inside this
  campaign's media dir". That meant an agent couldn't review or re-cut a moved take. A file is now
  accepted when it is inside this campaign's folder, or inside any campaign folder under the media
  root **and** named by an asset of this campaign (its own file or a side file). Anything outside
  the media root, a symlink that escapes it, and the old campaign's other files are still refused.
  The gallery, retention and the archive already checked against the media root, so they were
  unaffected. Already-moved assets work with no migration.

## 0.6.3

- **Gallery images have "Open in new tab", like images in chat.** It's the third hover button,
  after Download and View fullscreen, with the chat's icon and label. The gallery's previews are
  private to the view, so the button asks the agent for a **15-minute signed link** to that one
  file and opens it in a new browser tab. In the desktop app it opens in your system browser,
  the same as a chat image. It works through the fleet hub too
  (`/agents/<slug>/plugins/campaign/open/…`). If your browser blocks the pop-up, the card says so
  and shows the link to click.
  - New gated route `POST /api/plugins/campaign/assets/{id}/open-link` returns
    `{url, expires_in, exp}`. It answers 404 for a missing, pruned or out-of-folder file, and
    415 for anything that isn't an image or video.
  - New public route `GET /plugins/campaign/open/{id}?exp=…&sig=…` (added to `public_paths`)
    serves the file `inline` with `Cache-Control: private, no-store`, `nosniff`,
    `Referrer-Policy: no-referrer` and the gallery's sandbox CSP. It refuses with 403 any link
    that is expired, forged, made for another asset, or made before the file was re-rendered or
    moved, and any file outside the media folder.
  - The signature is an HMAC over the asset id, the expiry and the file (its path, size, mtime
    and inode). The key is a random per-instance secret in `<data_dir>/open-link.key` (mode 600),
    created atomically. Deleting that file revokes every outstanding link, with no restart.
  - The route opens the file once (never following a final symlink), checks that descriptor,
    and streams exactly those bytes. Swapping the file for a symlink mid-request can't serve
    anything else.

## 0.6.2

- **Gallery images open like images in chat.** A still, card or GIF in the gallery now has the
  same hover buttons as an image in chat, **Download** and **View fullscreen**, and opens in the
  same Lightbox (Escape, the backdrop or × closes it). Clicking the image itself opens it too.
  It uses the design system's own image and Lightbox styles, so it looks the same as chat. There's
  no "Open in new tab": the gallery's files are private to the view and can't be opened outside it.

## 0.6.1

- **The archive's operator routes are actually served.** `GET /api/plugins/campaign/archive`
  (status), `POST …/archive/backfill` and `POST …/archive/drain` were registered as a second router at
  the same prefix as the data routes. The host keeps one router per prefix, so since 0.5.0 it
  dropped them (logging "registered a second router … dropped") and they returned 404. The
  background backup pass was unaffected. They now mount at their own prefix with the same URLs. The
  test registry drops a duplicate prefix the way the host does, so this can't regress silently.

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
