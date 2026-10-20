# How it works

What Campaign Studio does, what it deliberately doesn't, and how the recording browser is run and fenced. Start with the [README](../README.md) for the short version.

## What it does

Campaign plans and **deterministic, repeatable media production** for launching any app or
product: the planning + production half of a "Brand & Launch" agent.

- **Plans** — a campaign's goal and the math behind its target, lanes (one angle per audience),
  a shot list of assets with *who records what*, a dated schedule, a channel plan, a do-not
  list, and the decisions only the operator can make. `campaign_status` splits every open
  item into *blocked on the operator* vs *on the agent*, shaped for `show_component`.
- **Shot scripts → takes** — a declarative YAML script (viewport, colour scheme, timezone,
  locale, auth, steps) recorded by headless Chromium through Playwright: `.webm` + named
  stills + a timing log of every `mark`. Same script, same take. Redaction built in.
  `capture: device` records in device pixels instead — a 1280×800 viewport at scale 2 becomes a
  2560×1600 H.264 take (CDP screencast frames, held across idle stretches, assembled at a
  constant frame rate) so small text stays crisp without shrinking the UI.
- **Renders** — ffmpeg trims the head/tail by mark, speed-ramps dead time, crops, and encodes
  H.264 mp4 (libx264 preset slow, crf 16, yuv420p, faststart — the capture master's quality) and
  palette-optimised GIFs. Only a platform **hard** limit may step an mp4's quality down to fit
  (and the report says what it traded); a file still over one isn't called ready. A bare
  `max_bytes` never pushes a deliverable past crf 20 — see [Hard limits](limits.md#deliverable-quality). Clips are
  **continuous**: there are no interior cuts, and a ramp above 4× is flagged in the render
  report as a jump cut unless the output says `continuous: false` (a deliberate time-lapse).
  A ramp can **ease** in and out (`ease: true` or seconds) — a smooth time-remap instead of a
  speed change on one frame; an un-eased ramp of ≥2× is flagged as abrupt.
- **Failed takes keep their footage** — a take whose step fails mid-run still finalizes its
  video, `timing.json` and `failure.png`, and registers as a `captured` clip noted
  `FAILED at step N: <error>` with the marks it reached, so `campaign_render` can cut a beat
  from the footage before the failure. A failed take can't go to review itself (render the
  good part, or `allow_failed_take=true`); a run whose video couldn't be finalized or that
  ffprobe can't read counts as failed and never fills a planned asset.
- **Cards** — branded HTML templates rendered to PNG: `og-1280x640` (kept under GitHub's 1 MB
  social-preview limit), `x-card-1600x900`, `square-1080`, `title-slide-1920x1080`.
- **Montages** — many short beats (each recorded in a different app theme) + tagline cards +
  an end card, cut into one launch video: every clip normalised to one canvas (1920×1080
  canonical, 1080×1920, 1080×1080), xfade transitions plus a signature **colour wipe** in the
  next beat's theme colour, brand-font lower thirds, H.264 under a size/length limit, a poster,
  and an optional GIF. `campaign_storyboard` reviews the cut as a contact sheet first.
- **Gallery** — one console view, for browsing every asset of a campaign and approving what ships. See [The gallery](usage.md#the-gallery).

## What it won't do

**Draft-only.** It never posts to a social platform and holds no platform credentials. Every
asset has an approval state and only the operator approves (in the gallery — there is no agent
tool for it). Its one optional credential is a GitHub one (the operator's GitHub App key, or a token) for **backing up**
approved media to protoContent (see [Archive to protoContent](media-and-backup.md#archive-to-protocontent)).

**What it is not.** Copy is the [Social Studio](https://github.com/protoLabsAI/social-plugin)
plugin's job (brand kit, queue, linter, disclosure, export); browsing a site is core's
`agent_browser`. Both are referenced by tool *name* (in skills and the producer's allowlist),
never imported. Cards read Social Studio's brand-kit YAML *file* for colours/fonts/logo if it
exists — its `visual:` section (`colors: {primary, accent, background, foreground}`,
`fonts: {heading, body}`, `logo: {path, dark, light}` relative to the kit file, `wordmark`).
Missing or malformed fields fall back to this plugin's brand settings, then neutral defaults;
`logo.dark` is used on a dark card and `logo.light` on a light one; font `*_url`s are never
fetched (cards load nothing from the network).

## The out-of-process browser

**Playwright runs out of process.** The agent's own process never imports playwright. Every
take and card is rendered by `worker/pw_worker.py`, a small self-contained script run under a
Python that *has* playwright, picked in this order:

1. the **Browser worker Python** setting (`interpreter`) when set — strict: a bad value is
   reported, never silently replaced;
2. the desktop app's **managed Python runtime** (Settings ▸ Tools) — where *Install
   dependencies* puts playwright on the desktop app (the dep is declared `scope: runtime`);
3. the agent's **own Python**, on a source/venv install (never a frozen app's binary).

The job (and any bearer) goes to the worker on stdin — never argv; the worker gets an
allowlisted environment (no host credentials); its output is bounded; and if it overruns, its
whole process tree — Node driver and Chromium included — is killed. Chromium lives in
Playwright's default machine-wide cache (`~/Library/Caches/ms-playwright`,
`~/.cache/ms-playwright`, `%LOCALAPPDATA%\ms-playwright`), or `PLAYWRIGHT_BROWSERS_PATH` if
you set it; *Install Chromium* uses the same interpreter as the worker, so they always agree.

**The recording browser's guards** (enforced inside the worker). A shot script is
agent-written, so the browser it drives never reaches this plugin's own data API (on any host —
it can't open the gallery and click Approve), and a script's bearer token goes only to its
`base_url` origin, read only from an env var named `CAMPAIGN_*` or listed in the **Shot-script
bearer env vars** setting. The host's own operator/fleet token is always refused. A card page
loads nothing from the network at all.
