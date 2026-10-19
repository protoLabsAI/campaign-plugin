# Videos from a request

`campaign_produce_video` turns ONE shot script into a finished, self-reviewed video with no
hand-driving — the path behind "make a 60-second 2K walkthrough of our architecture, end with
the star CTA" (the `produce-walkthrough-video` skill is the agent's recipe, from the one-line
brief to delivery):

```
campaign_produce_video(script=<atlas_record_script's reply, unchanged>, title="protoAgent in 85 seconds",
                       subtitle="How a self-hosted agent runtime fits together",
                       quality="2k", intro="brand", outro="brand")
→ "Started video job vid-3f9c…"            # returns at once
campaign_video_status(job_id="vid-3f9c…", wait_s=300)   # stage, %, gates, files, links
```

**Preflight** (before any browser starts): the script's `base_url` must be THIS agent's console
(compared with core's `sdk.console_url()` when present — on a fleet member that is the member's
own console, never the hub's — else `A2A_PUBLIC_URL` / the bound port; loopback spellings equal), its first page must answer 200, and its `bearer_env` /
`storage_state` token must pass one authenticated GET. Otherwise it refuses at once, saying
what to fix. `allow_other_console=true` is the explicit opt-in for recording another console.

It runs as a **background job** (a worker thread in the plugin; one at a time): **record** the
take (`quality: 2k` → `device_scale_factor` = 2560 ÷ viewport width + `capture: device`; an
Atlas script also gets a `last-slide` mark) → **measure** the first settled frame after `start`
(the in-point) and, for Atlas footage, when the final caption is fully up and starts fading →
**render** `[intro] → walkthrough → [outro]` twice: a review cut (crf 18) and a GitHub variant
held under `github_attachment_video_free` (10 MB) → **self-review** → **deliver**.

**Self-review gates** — all must pass before a job is `done`:

| Gate | Check |
|---|---|
| `resolution` | rendered ≥ 2560 px wide (1920 for `1080p`) |
| `final_caption` | the final caption is on screen ≥ the outro's `min_caption_s` (4 s) before the outro |
| `intro` | the intro is ≤ 6 s and crossfades in |
| `no_blank_frames` | a sample every second of the walkthrough segment: none blank / near-black (luma p95−p5 < 10 on a 64×36 sample — a dark theme with content passes) |
| `size_budget` | the GitHub variant is within `github_attachment_video_free` |
| `review_assets` | a labelled **contact sheet** (intro bumper, card, crossfade, first settled frame, eight walkthrough frames, final caption, outro) and a **100 % crop** of the densest small text are saved |

A failed gate gets **one** automatic retry where it can help — blank frames or a short
resolution re-record; an oversize GitHub variant re-renders at 24 fps. A refused cut (final
caption too short, intro too long) is a script/preset fix, so it doesn't retry. Otherwise the
job ends **`needs_review`** with the reasons, its files stay `rendered` (not offered for
review), and the chat is told not to present it as finished.

**Idempotent**: jobs live in `<data dir>/videos/vid-<hash>.json`, keyed by a hash of the
inputs. The same request returns the same job; a failed (or orphaned) job resumes, reusing a
good take; `force=True` re-records.

**Delivery** (what each channel gets today):

- **Campaign gallery** — the review mp4 + GitHub mp4 as `montage` assets (`ready_for_review`
  when every gate passed) with the sheet and the text crop as stills; the operator approves.
- **Console chat** — on a core with the `artifact.save_file` service (protoAgent#4254) the job
  saves the mp4 and the sheet into the requesting chat's Artifact panel itself. On older cores,
  the follow-up turn (`graph.sdk.run_in_session`) tells the agent to `save_file_artifact` them.
- **A2A / `/v1`** — on a core with `sdk.attach_file` (protoAgent#4255), `campaign_video_status`
  attaches the finished files to the answer: A2A FileParts, `/v1` absolute links and
  attachments. On older cores the reply carries the signed `/media/…?sig=` links (made absolute
  when the agent's base URL is known), and the agent is told the caller has no Artifact panel.
- **Links** — every file goes through the host media store (`registry.save_media`).
  `GET /api/plugins/campaign/videos/{job_id}` (bearer) returns the job's status, gates and
  `downloads` (`/api/plugins/campaign/file/{asset_id}`).
