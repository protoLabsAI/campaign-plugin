---
name: produce-walkthrough-video
description: >-
  Use when someone asks for a finished VIDEO from a plain request — "make a 60-second 2K
  walkthrough of our architecture, dark theme, end with the star CTA" — from the console chat,
  A2A or the /v1 API. The end-to-end recipe: confirm the brief in one line, make sure an Atlas
  model + tour exist (reuse, or map one with the operator's OK), validate the tour, get its
  shot script, run campaign_produce_video as a background job, report progress, and deliver the
  self-reviewed MP4 + review sheet as files. Triggers: "make a video", "walkthrough video",
  "architecture video", "record a demo of", "a 2K clip of", "produce a clip", "video of our
  architecture", "demo video for the README".
tools: [campaign_produce_video, campaign_video_status, campaign_view, campaign_assets, atlas_validate, atlas_record_script, save_file_artifact]
---

# One request → a finished walkthrough video

The job is a **finished file** in the requester's hands, not a set of instructions. You plan,
the pipeline records, renders and checks itself, and you deliver. Nobody hand-drives anything
between the request and the file — the one human gate is the operator's OK on the plan (and on
anything you'd write into their repo).

## The clip rules (Josh's, non-negotiable)

- **Continuous, no jump cuts.** One take, one stretch. Speed ramps only, never cutting action
  out. The intro CROSSFADES into the first settled frame; the outro begins after the final
  caption has been read.
- **The plugin view on camera.** The view the action lands in (Atlas, for a walkthrough) is on
  screen, live, the whole time — not a screenshot, not a mock.
- **2K, crisp text.** `quality: 2k` (2560 px wide, device-pixel capture). The 100 % crop of the
  smallest text in the review sheet must read cleanly — look at it.
- **The final caption stays up ≥ 4 s** before the outro. The pipeline refuses a cut that
  doesn't, and its gate re-checks it.

## 1. Confirm the brief — ONE line

Restate what you'll make and wait for a yes:

> "A ~85 s 2K walkthrough of the `overview` tour, dark theme, brand intro ('protoAgent in 85
> seconds') and the Star-on-GitHub outro — go?"

Pick the tour whose predicted length is closest to what was asked (atlas_record_script reports
it). Ask only what you can't infer. On A2A / `/v1` there is no one to ask mid-turn: treat the
request itself as the confirmed brief when it names the subject, length and ending, and say in
your reply what you chose.

## 2. Make sure an Atlas model + tour exist

- **Reuse first.** `atlas_validate` — a valid model with a fitting tour (e.g. `overview`) is
  the common case.
- **Nothing fits?** Mapping a model or writing a tour writes into the operator's repo: **ask
  before writing**, and get their OK on the tour script (slides, captions, length) before you
  record anything.
- **Fix before recording.** `atlas_validate` must be clean — a broken slide fails the take.

## 3. Get the shot script

`atlas_record_script(tour, base_url=<this console>, color_scheme=dark, viewport="1280x720")`.
Hand its reply to the next step **unchanged** (the pipeline reads the fenced YAML and the
predicted length out of it). Use `1280x720` for a 16:9 2560×1440 video. If the console needs a
token, pass `bearer_env` (an env var NAME), never the token.

## 4. Produce

```
campaign_produce_video(script=<the reply>, title="protoAgent in 85 seconds",
                       subtitle="How a self-hosted agent runtime fits together",
                       quality="2k", intro="brand", outro="brand")
```

It returns a job id at once. The job records in real time (≈ the predicted length + ~20 s),
measures the first settled frame and the final caption, renders a review cut (crf 18) and a
GitHub-sized variant (≤ 10 MB), then runs the gates. Same inputs again returns the same job —
don't start a second one; `force=True` only when the operator wants a re-record.

## 5. Report progress

Tell the requester it's running and roughly how long. `campaign_video_status(job_id)` gives the
stage and percent. In an API/A2A turn that must return the file, poll with
`campaign_video_status(job_id, wait_s=300)` until it ends. In the console you don't need to
wait: when the job ends, this chat gets a follow-up turn telling you to deliver.

## 6. Check, then deliver

- **`done`** — every gate passed. Still LOOK: `campaign_view` the review sheet (intro tiles,
  crossfade, walkthrough, final caption, outro) and the 100 % text crop. Then deliver:
  `save_file_artifact(path=<review mp4>, title=<title>)` and
  `save_file_artifact(path=<sheet>, title="review sheet")` (they appear in the Artifact panel
  with a Download button), and reply with the gate table and the signed links from
  `campaign_video_status` — the GitHub-sized mp4's link is the one for a README or PR.
- **`needs_review`** — a gate failed after the one automatic retry (or the cut was refused).
  Say exactly which gate and why, show the sheet, and propose the fix (a longer final-slide
  hold, a shorter bumper, a re-record). **Never present it as finished.**
- **`failed`** — the take or render broke. Quote the reason; `campaign_setup` if it's the
  browser or ffmpeg.

Nothing is approved by you: the files land `ready_for_review` in the Campaign Studio gallery,
and only the operator approves what ships.
