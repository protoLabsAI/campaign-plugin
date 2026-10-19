# Media and backup

Where the plugin keeps what it makes, what retention deletes, what is backed up to protoContent, how to get an archived file back, and how to gather keepers into a collection. These are one story, so they live on one page.

## Where your media lives

Everything the plugin makes stays on the agent's own disk, in one folder: the **data dir**. It
is the `data_dir` setting when set, else the host's per-instance plugin store,
`<instance root>/campaign/`. On a source install that's `~/.protoagent/<instance>/campaign/`
(`~/.protoagent/default/campaign/` for the default instance). In the desktop app it's
`workspaces/<agent>/campaign/` under the app's data folder. Each agent instance has its own.

```
<data_dir>/
  campaign.db              SQLite: campaigns, lanes, assets, shot scripts, milestones, decisions
  open-link.key            the secret behind the gallery's 15-minute "Open in new tab" links
  retention-report.json    the last retention run's report
  videos/vid-<hash>.json   campaign_produce_video job records
  campaigns/<id>-<slug>/   one folder per campaign (the media root is campaigns/)
    shots/<script>-<stamp>/            takes: the video, named stills, timing.json (failure.png)
    renders/<take folder>/<stamp>/     mp4 / gif / poster cut from a take
    cards/<template>-<stamp>/          card PNGs
    montages/<name>-<stamp>/           montage mp4 + poster (+ gif)
    storyboards/<stamp>/               storyboard contact sheets
    videos/<job>-a<attempt>/           campaign_produce_video renders, contact sheet, text crop
```

Stamps are UTC (`YYYYMMDD-HHMMSS`). `campaign.db` is the source of truth: every asset's
status, approval, review note, lineage (`parent_id`, a montage's inputs, `superseded_by`) and
file path live there. The folders only hold bytes. A file with no row, like a storyboard sheet
or anything you drop in by hand, never shows up in the gallery. It is never pruned or backed up
either. `open-link.key` is created on first use (mode 600). Delete it to revoke every
outstanding signed link; a new key is made on the next mint.

**A moved asset keeps its file where it was.** `campaign_collect` with `mode="move"` (or the
gallery's move) changes only the row's campaign. The file stays in the ORIGINAL campaign's
folder, so a collection's assets can live under another campaign's directory. That's by design
(#36, v0.6.4): every path check is media-root-wide or based on which campaign owns the row.
`mode="copy"` does copy the files into the target's folder. See [Collections](#collections--keep-the-good-ones-together-start-fresh).

**An asset's life.** `planned` → `scripted` → `captured` (a take) / `rendered` (a render,
card, montage) → `ready_for_review` → the operator decides:

- **approved**: never deleted by retention, and backed up to protoContent when the archive
  is on.
- **rejected**: kept on disk. No retention rule deletes it, and it isn't backed up.
- **superseded** (a retake replaced it, or the operator retired an approved asset): the file
  is deleted 7 days later; the row stays as a tombstone so "Superseded by #N" still reads.

Takes and renders that never reach review are deleted 7 days after their last change, unless
something kept was made from them. Retention only deletes once the operator sets
`retention_mode: prune`; the default, `report`, deletes nothing. See [Retention](#retention) for the
exact rules.

**What is protected where**

| What | On the agent's disk | Backed up to protoContent | Deleted by retention (`prune` mode) |
|---|---|---|---|
| `approved` asset: its file + a catalog entry | yes | yes, when `archive_enabled` | never |
| `ready_for_review` asset | yes | no | never (flagged stale after 14 days) |
| `rejected` / `scripted` asset | yes | no | never |
| `captured` take / `rendered` output nothing kept depends on | yes | no | file after 7 days untouched; the row too unless another row names it |
| `superseded` asset (incl. an approved one the operator retired) | yes, until pruned | only if it was archived while approved | file after 7 days; row kept |
| `planned` row with no file | row only | no | row after 30 days |
| a take's stills, a poster, a montage's GIF (assets of their own) | yes | only if that asset is itself approved | by its own status, as above |
| files with no row: a take's `timing.json`, a bookended clip's raw master, storyboards | yes | no | never |
| `campaign.db`: plans, shot scripts, lineage | yes | no; only approved assets' metadata is copied to the catalog | never |

**The plain consequence: only APPROVED assets ever leave the machine**, and only once the
archive is on (`archive_enabled` + a credential). A backup copies just the approved row's own
file, so a take's stills or a montage's poster aren't included unless they were approved too.
Takes awaiting review, renders, intermediates, shot scripts and the database (every plan and
the lineage of everything not approved) exist only on the agent's disk. If you need them kept,
back up the data dir yourself. Copy `campaign.db` while the agent is stopped: it runs in WAL
mode, so a live copy can miss recent writes. To get an archived file back, see [Getting an
archived asset back](#getting-an-archived-asset-back).

## Retention

The gallery keeps what matters and lets intermediates go (operator policy, 2026-10-16):

| Status | What happens |
|---|---|
| `approved` | **Never deleted.** |
| `ready_for_review` | **Never deleted**; flagged **stale** (gallery badge + report) after 14 days untouched |
| `superseded` | The FILE is deleted 7 days after it was superseded; the row stays as a tombstone so "Superseded by #N" still reads |
| `rendered` / `captured` | Deleted after 7 days untouched — unless anything kept depends on it. The row goes too, unless another row still names it (then it is a tombstone) |
| `planned`, no file | The row is dropped after 30 days untouched, unless another row still names it |

"Depends on" is a closure over everything that keeps its file (approved, ready for review,
rejected, scripted, and anything younger than its rule): the `parent_id` chain (render →
take, bookended clip → render, poster → render), a montage's `meta.inputs` and the clips in
its sequence, and lane hero assets. A file any keeper names — its own path, a bookended
clip's raw master (`meta.unbookended`), a take's stills or timing log — is never deleted,
whichever row it belongs to. Dependencies cross campaigns. When in doubt, it keeps.

The prune never deletes outside the campaign media root, never a symlink or a directory (it
only removes folders it emptied, never a campaign's own), and re-checks each row under the
database write lock right before acting — an approval that lands after the plan was made
wins. Tombstones are hidden in the gallery like superseded takes (**Show superseded**).

A background surface runs it **daily** in the `retention_mode` setting's mode: `report` (the
default — a dry run; nothing is deleted, the report is saved), `prune`, or `disabled`.
`retention_mode` is operator-only (core's `spawns` fence): the agent can never turn deletion
on for itself. `campaign_prune` shows the dry run any time; the operator routes are
`GET /api/plugins/campaign/retention` (the last saved report + a fresh plan) and
`POST /api/plugins/campaign/retention/prune` (`{"dry_run": false}` prunes now, whatever the
mode).

## Archive to protoContent

Approved media is backed up to [protoContent](https://github.com/protoLabsAI/protoContent)
(private), the protoLabs marketing hub — opt in with `archive_enabled` and a credential: the
**protoquinn GitHub App** (`archive_app_id` + `archive_app_private_key`, preferred) or an
`archive_token`.

* **Files** → GitHub **release assets**, one release per campaign: tag `media/<slug>`, made on
  first use, never marked *latest* (it can't displace a package release; nothing in
  protoContent's CI fires on it). Video never goes into git. Assets are content-addressed —
  `<sha256[:12]>-<file name>` — so the same bytes upload once.
* **Catalog** → `specs/media/catalog/<slug>.json` (`archive_catalog_dir`), validating against
  protoContent's `schemas/media/catalog.schema.json`: one entry per approved asset — id,
  title, lane, kind, width/height/duration, size, sha256, approved_at, review_note, the release
  asset URL, the source campaign. Append/update only. protoContent's `main` takes changes only
  through PRs, so the catalog is **batched onto one open PR per campaign** (branch
  `media-catalog/<slug>`): later approvals add commits to that PR; a new one opens only after it
  merged (a PR closed unmerged is regenerated in full). The plugin never merges anything:
  **an operator merges the catalog PR after protoContent's review gate** (protoContent's
  ADR 0009: catalog PRs are merged by a human). With app auth the PR is authored by
  `protoquinn[bot]`, but nothing relies on protoquinn auto-merging it, and in practice it
  hasn't (protoContent#622). Because a merge can still come at any moment, every catalog PR is
  complete the moment it opens: each commit is the *whole* catalog (regenerated from the local DB), one commit per campaign per
  pass, and the PR is opened only after its branch holds that commit. Approvals arriving while
  the PR is open add further whole-catalog commits; if the PR merged while a commit was landing,
  the pass notices (the PR is no longer open) and writes the catalog onto a fresh PR from `main`.
* **Flow:** every minute (while enabled) the library surface reconciles — queuing each approved
  asset with no archive record, or none for its current campaign's catalog — then drains → upload (skipped when `meta.archive.sha256` already matches: same sha256 =
  already archived) → `meta.archive = {url, sha256, at, …}` → one catalog commit per campaign
  per pass (at most 25 assets a pass). Catalog entries upsert by sha256. Failures back off
  1 min → 5 min → 30 min → 2 h → 6 h; after 10 attempts an asset parks until a backfill. An
  asset collected into another campaign is added to THAT campaign's catalog without re-uploading.
* **Backfill:** `campaign_backup(backfill=true)` or `POST /api/plugins/campaign/archive/backfill`
  queues every approved, non-superseded asset. `GET /archive` is the status;
  `POST /archive/drain` runs a pass now; `campaign_backup` shows the same status to the agent.

**Credentials — the GitHub App wins when it's configured.** `GET /archive` and
`campaign_backup` show which one is active (`auth`: `app` / `token` / `none`), never the secret.

* **GitHub App** (preferred) — protoquinn, app id `3282126`, installed org-wide on `protoLabsAI`
  (installation `121562683`, Contents + Pull requests write). Settings ▸ Campaign Studio ▸ Library:
  * `archive_app_id` = `3282126` (0 = off).
  * `archive_app_installation_id` = `121562683` — optional; 0 finds it with
    `GET /repos/{archive_repo}/installation`.
  * `archive_app_private_key` (secret) — the app's `.pem`. Paste the whole file, `-----BEGIN RSA
    PRIVATE KEY-----` through `-----END RSA PRIVATE KEY-----`, into the field: it's a one-line
    input, so the newlines are dropped, and the plugin re-wraps the key (spaces or literal `\n`
    in place of newlines also work). In `secrets.yaml` (0600, under the plugin's section) use a
    block scalar:

    ```yaml
    campaign:
      archive_app_private_key: |
        -----BEGIN RSA PRIVATE KEY-----
        MIIEow…
        -----END RSA PRIVATE KEY-----
    ```
  * …or `archive_app_private_key_path` — an absolute path to the `.pem` (chmod 600), used only
    when the secret is blank.

  Each pass signs a 9-minute RS256 JWT as the app and mints an installation access token
  **scoped to `archive_repo` only** (`repositories: ["protoContent"]`, `contents: write`,
  `pull_requests: write`) — the org-wide install never reaches another repo through this
  plugin. The token is cached until 5 minutes before it expires and re-minted once on a 401.
  Commits and the catalog PR are authored by `protoquinn[bot]` (the plugin sets no author).
  Needs `PyJWT[crypto]` in the agent's process — protoAgent already ships it (its `mcp`
  dependency requires `pyjwt[crypto]`); it's declared as an optional host dep.
* **Token** — `archive_token` (a secret in `secrets.yaml`), used only when no app id is set: a
  **fine-grained personal access token**, resource owner `protoLabsAI`, repository access
  **only `protoContent`**, with repository permissions **Contents: Read and write** (releases,
  release assets, the catalog branch and commit) and **Pull requests: Read and write** (open the
  catalog PR). *Metadata: Read* is added automatically. Nothing else — no Actions, Workflows,
  Administration or org permissions. (A classic token would need the whole `repo` scope; prefer
  the fine-grained one.)

The key, the JWT, a minted token and the PAT go only into an `Authorization` header to
`api.github.com` / `uploads.github.com` — never into a log line, an error, a URL or the status.
`archive_enabled`, `archive_repo`, `archive_catalog_dir`, `archive_app_id`,
`archive_app_installation_id` and `archive_app_private_key_path` are operator-only settings —
they decide whether, where and as whom your media goes.

### Getting an archived asset back

Restoring is a manual download. Nothing writes an archived file back into the agent's
database or media folders. You need read access to protoContent (it's private) and the
[`gh`](https://cli.github.com) CLI signed in.

1. **Find the entry in the catalog**, `specs/media/catalog/<slug>.json` on protoContent's
   `main`, one file per campaign (list them with
   `gh api repos/protoLabsAI/protoContent/contents/specs/media/catalog --jq '.[].name'`). An
   asset collected into another campaign is in that campaign's catalog too. Look it up by `id` (the asset id in the gallery), `title`, `file_name` or `sha256`:

   ```bash
   gh api repos/protoLabsAI/protoContent/contents/specs/media/catalog/protoagent-showcase.json \
     -H 'Accept: application/vnd.github.raw' |
     jq -r '.assets[] | select(.file_name == "og-1280x640.png")
            | [.id, .release_tag, .release_asset_name, .sha256] | @tsv'
   ```

   The gallery's **backed up** badge names the release tag too (hover it). Approvals still on
   the open `media-catalog/<slug>` PR aren't on `main` yet; read the file on that branch
   (`?ref=media-catalog/<slug>`).
2. **Download the release asset.** Use the entry's `release_tag` and `release_asset_name`.
   Don't rebuild them from the catalog's slug: an asset collected into another campaign keeps
   pointing at the release it was first uploaded to.

   ```bash
   gh release download media/protoagent-github-trending-launch-oct-13 \
     -R protoLabsAI/protoContent -p '9257882ac2ad-og-1280x640.png'
   ```

   Asset names are `<sha256[:12]>-<file name>`: the first 12 hex digits of the file's
   sha256, then its file name with anything outside `A-Za-z0-9._-` turned into `-` and the
   extension lowercased. The entry's `url` is the same file's browser download link (it needs
   a signed-in GitHub session with repo access).
3. **Verify it** against the entry's `sha256`:

   ```bash
   echo "<sha256>  9257882ac2ad-og-1280x640.png" | shasum -a 256 -c   # sha256sum -c on Linux
   ```

**If the agent's machine is lost**, what survives in protoContent is every archived approved
file and its catalog entry: id, title, lane, kind, dimensions, duration, size, sha256,
approval time and note, the source campaign. What's gone is everything else in the data dir:
`campaign.db` (plans, lanes, shot scripts, milestones, decisions, the lineage linking renders
to takes), every file that wasn't approved, approvals the archive hadn't uploaded yet (check
`campaign_backup` for queued or retrying ones), and any approval made while the archive was
off. A reinstalled plugin starts with an empty gallery. A downloaded file is just a file: to put
one back in the gallery, copy it into a campaign's folder and attach it to an asset with
`campaign_asset_update(asset_id, path=…)`. It comes back unapproved, so the operator approves
it again.

## Collections — keep the good ones together, start fresh

A collection is a campaign. To gather the keepers and start over:

1. `campaign_create(name="protoAgent launch — best of")`.
2. `campaign_collect(target_campaign_id=<new>, asset_ids=[…])` — a **dry run** by default:
   it lists every asset that would come, with sizes. With `lineage=true` (default) that is the
   selected assets **plus everything they were made from** (the take a render was cut from, a
   bookended clip's render, a montage's inputs), so the chain still reads and retention keeps it.
3. Run it for real (`dry_run=false`):
   * `mode="move"` re-homes the rows — same ids, same files (they stay where they are on disk,
     inside the media root), same status and approval. Lanes are matched by name in the
     target (created when missing).
   * `mode="copy"` duplicates them: files are COPIED into the target's folder (same layout),
     side files too (raw masters, a take's stills and timing log), and every id and path in the
     copies is re-pointed at the copies. Copying the same asset again maps to its existing copy.
4. `campaign_archive(old_id)` — hidden from `campaign_list` and the gallery picker (**Show
   archived** brings it back); nothing in it is touched. `archived=false` restores the status it
   had.

**Approval stays the operator's:** the agent can't move an APPROVED asset, and its copy of one
lands `ready_for_review`. The operator's route does both:
`POST /api/plugins/campaign/campaigns/<target>/collect` with
`{"asset_ids": [...], "mode": "move" | "copy", "lineage": true, "dry_run": false}` (dry run by
default), and `POST /api/plugins/campaign/campaigns/<id>/archive` with `{"archived": true}`.
