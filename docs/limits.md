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
