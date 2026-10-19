# HTTP API reference

Every route below is real: `tests/test_docs_reference.py` reads the routers by AST, joins each
path to the prefix `__init__.py` mounts its router at, and fails if a route is added, removed or
renamed without this file changing.

The plugin mounts **three routers**, at three prefixes (the host keeps one router per prefix):

| Router | Built by | Prefix | Auth |
|---|---|---|---|
| view | `api.build_view_router` | `/plugins/campaign` | **public** (`public_paths` in the manifest) |
| data | `api.build_data_router` | `/api/plugins/campaign` | the host's operator bearer (default-deny) |
| archive | `archive_api.build_archive_router` | `/api/plugins/campaign/archive` | the host's operator bearer (default-deny) |

## Public surface

An iframe navigation or a plain browser tab carries no bearer, so these two are public. The
fleet hub's `/agents/<slug>/plugins/campaign/…` proxy defers to this member's public list.

| Method | Path | What it does |
|---|---|---|
| `GET` | `/plugins/campaign/view` | The gallery page (the **Campaign Studio** rail view). Static HTML; it reads everything through the gated data routes below with the DS kit's authed fetch, and turns media into `blob:` URLs, so no media URL is public. |
| `GET` | `/plugins/campaign/open/{asset_id}` | "Open in new tab": serves one asset's file **only** for a valid 15-minute signed link (`?exp=…&sig=…`) minted by `POST /api/plugins/campaign/assets/{asset_id}/open-link`. Served `inline`, `Cache-Control: private, no-store`, `nosniff`, `Referrer-Policy: no-referrer`, sandbox CSP. 403 for an expired, forged or other-asset link, a link made before the file was re-rendered or moved, or a file outside the media folder. See [The gallery](usage.md#the-gallery). |

## Operator surface — bearer token

Approve / reject / supersede exist **only** here. There is no agent tool for them.

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:7870/api/plugins/campaign/campaigns
```

| Method | Path | What it does |
|---|---|---|
| `GET` | `/api/plugins/campaign/campaigns` | The gallery's campaign picker: each campaign's id, name, status, launch window and counts (assets, in review, approved, superseded, pruned). Superseded takes don't count as assets. Archived campaigns are hidden unless `?include_archived=true`; `archived_hidden` says how many. |
| `POST` | `/api/plugins/campaign/campaigns/{campaign_id}/archive` | Archive a campaign (`{"archived": true}`, the default) or restore it (`false`, back to the status it had). Nothing in it is touched. See [Collections](media-and-backup.md#collections--keep-the-good-ones-together-start-fresh). |
| `POST` | `/api/plugins/campaign/campaigns/{campaign_id}/collect` | Move or copy assets into this campaign: `{"asset_ids": [...], "mode": "move" \| "copy", "lineage": true, "dry_run": true}`. A **dry run by default**. Unlike `campaign_collect`, it may move an APPROVED asset, and an approved asset's copy stays approved. 404 for no such campaign, 400 for a refused request, 500 (nothing changed) on a disk error. |
| `GET` | `/api/plugins/campaign/campaigns/{campaign_id}/assets` | One campaign's lanes, every asset (status, size, dimensions, lineage, review note, `superseded_by`, `created_at` / `updated_at`, `made_at` + `made_from`, `take_at`, `has_file`, `can_review`, `pruned`, `stale`, `archived`) and the list of statuses. A path is relative to the media root, never the raw filesystem path. |
| `GET` | `/api/plugins/campaign/file/{asset_id}` | An asset's file, only when its resolved path (symlinks followed) is inside the media root; 403 otherwise, 404 with no file. Images and videos are served as themselves; anything else as an `application/octet-stream` attachment, never as a document. |
| `POST` | `/api/plugins/campaign/assets/{asset_id}/open-link` | Mint a 15-minute signed link for "Open in new tab": `{url, expires_in, exp}`. 404 for a missing, pruned or out-of-folder file; 415 for anything that isn't an image or video. |
| `POST` | `/api/plugins/campaign/assets/{asset_id}/review` | The operator's decision: `{"decision": "approve" \| "reject" \| "supersede", "note": "", "superseded_by": [ids]}`. Approve re-checks the asset's file and hard limit under the write lock and refuses (400) if it fails; `superseded_by` only goes with `supersede`, which is the only way an APPROVED asset is retired. Emits `campaign.asset_reviewed`. |
| `GET` | `/api/plugins/campaign/retention` | The last saved retention report plus a fresh dry-run plan (`?campaign_id=` narrows it; 0 = all). See [Retention](media-and-backup.md#retention). |
| `POST` | `/api/plugins/campaign/retention/prune` | `{"dry_run": true, "campaign_id": 0}` returns the plan; `{"dry_run": false}` prunes now, **whatever `retention_mode` says** (the operator is the authority the mode stands in for). Never touches an approved or ready-for-review asset. |
| `GET` | `/api/plugins/campaign/videos` | The 50 most recent `campaign_produce_video` jobs, each as below. A malformed record is skipped, never a 500. |
| `GET` | `/api/plugins/campaign/videos/{job_id}` | One video job: `status`, `stage`, `progress`, `attempt`, `gates`, `reasons`, `assets`, `downloads` (`/api/plugins/campaign/file/{asset_id}` per file), `links`, `created`, `updated`. 404 for no such job, 422 for a malformed record. See [Videos from a request](videos-from-a-request.md). |
| `GET` | `/api/plugins/campaign/archive` | The protoContent backup's status: `queued`, `waiting_retry` (asset id, attempts, next try, last error), `archived`, `approved`, and the active `auth` (`app` / `token` / `none`), never the secret. Same as `campaign_backup`. See [Archive to protoContent](media-and-backup.md#archive-to-protocontent). |
| `POST` | `/api/plugins/campaign/archive/backfill` | Queue every approved, non-superseded asset (`{"campaign_id": 0}` = every campaign): `{queued}`. |
| `POST` | `/api/plugins/campaign/archive/drain` | Run one archive pass now (upload, then one catalog commit per campaign). Synchronous, on FastAPI's threadpool. |

The routes emit `campaign.*` events on the host's bus (`campaign_changed`, `asset_reviewed`);
the event bus is never load-bearing.
