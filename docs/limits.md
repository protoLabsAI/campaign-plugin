# Hard limits, not folklore

`limits.py` is the only table of platform constraints, and it holds only *documented* limits —
GitHub's 10 MB image/GIF and free-plan video attachment ceilings, its 100 MB paid-plan video
ceiling, the repository social preview (< 1 MB, ≥ 640×320, 1280×640 recommended), and for
montages: X post video (512 MB, 0.5–140 s without Premium), X API video (≤ 1280×1024),
LinkedIn feed video (5 GB, 3 s–15 min, 256×144–4096×2304) and YouTube Shorts (vertical or
square, ≤ 3 min). Rows can carry `max_duration_s` / `min_duration_s`, `max_width` /
`max_height` and `orientations` besides bytes and minimum dimensions. Every row
carries its `source` URL and `as_of` date; bytes are counted as decimal MB to be safe.
Override or add rows with the **Hard-limit overrides** setting. Soft norms (ideal clip length,
"best" aspect) are deliberately absent — the agent researches them and writes them into the
plan as sourced, dated assumptions.

## Deliverable quality

Every deliverable mp4 — `campaign_render`, the brand bookends around it, `campaign_produce_video`'s
review cut, and montages — is H.264 (libx264 `-preset slow`), yuv420p, `+faststart`, at
**crf 16**: the same quality as the `capture: device` master, so a 2K take's smallest text
stays crisp at 100 %. Pass `crf` to choose a smaller file on purpose.

A byte ceiling never degrades a deliverable silently:

| Ceiling | What the mp4 does |
|---|---|
| none | one encode at the output's `crf` (16) |
| a platform `limit` that caps bytes (e.g. `github_attachment_video_free`) | steps CRF, then width (render) or a capped bitrate (montage), until it fits; any step past crf 20 or below full width comes back in `warnings` (`quality traded to fit …`); still over → a violation, not ready |
| a bare `max_bytes` (no platform limit) | steps CRF up to crf 20 at full width and stops; still over → kept at that quality with a `kept deliverable quality …` warning, not a violation |

`produce_video`'s GitHub variant is the one routinely size-limited output (10 MB). X's 512 MB
cap is no constraint at these lengths. GIFs keep their own fps/width ladder, and posters are PNG.
