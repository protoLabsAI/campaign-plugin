"""The hard-limit table: every row sourced and dated; overrides merge; checks are exact."""

from __future__ import annotations

from campaign import limits


def test_every_default_row_has_a_source_url_and_an_as_of_date():
    for key, row in limits.DEFAULT_LIMITS.items():
        assert row["source"].startswith("https://"), key
        assert len(row["as_of"]) == 10, key
        assert any(row.get(k) for k in ("max_bytes", "max_duration_s", "max_width")), f"{key} states no hard number"


def test_video_platform_rows_check_duration_dimensions_and_orientation():
    assert limits.check("x_video", size=50_000_000, width=1920, height=1080, fmt="mp4", duration=60) == []
    assert any("140s maximum" in p for p in limits.check("x_video", size=1, duration=150))
    assert any("0.5s minimum" in p for p in limits.check("x_video", size=1, duration=0.2))
    assert any("1280px maximum" in p for p in limits.check("x_api_video", size=1, width=1920, height=1080))
    assert limits.check("youtube_shorts", size=1, width=1080, height=1920, duration=45) == []
    assert limits.check("youtube_shorts", size=1, width=1080, height=1080, duration=45) == []
    probs = limits.check("youtube_shorts", size=1, width=1920, height=1080, duration=200)
    assert any("landscape" in p for p in probs) and any("180s maximum" in p for p in probs)
    assert limits.orientation(1080, 1920) == "vertical" and limits.orientation(10, 10) == "square"
    assert "`x_video`" in limits.brief() and "0.5–140s" in limits.brief()


def test_documented_github_numbers():
    assert limits.get("github_attachment_image")["max_bytes"] == 10_000_000
    assert limits.get("github_attachment_video_free")["max_bytes"] == 10_000_000
    og = limits.get("github_social_preview")
    assert og["max_bytes"] == 1_000_000 and (og["min_width"], og["min_height"]) == (640, 320)


def test_check_reports_size_format_and_dimension_violations():
    assert limits.check("github_social_preview", size=900_000, width=1280, height=640, fmt="png") == []
    probs = limits.check("github_social_preview", size=1_200_000, width=600, height=300, fmt="webp")
    assert len(probs) == 4
    assert any("over the 1.00 MB" in p for p in probs)
    assert limits.check("nope", size=1) and "unknown limit" in limits.check("nope", size=1)[0]


def test_overrides_merge_add_and_mark_rows():
    warnings = limits.configure(
        "github_attachment_video_free: {max_bytes: 100000000, as_of: '2026-10-01'}\n"
        "my_cdn: {label: CDN clip, max_bytes: 5000000, source: 'https://cdn.test/docs'}"
    )
    assert warnings == []
    assert limits.get("github_attachment_video_free")["max_bytes"] == 100_000_000
    assert limits.get("github_attachment_video_free")["source"].startswith("https://docs.github.com")
    assert limits.get("my_cdn")["overridden"] is True
    assert "*(override)*" in limits.brief()


def test_bad_overrides_warn_and_fall_back():
    assert limits.configure("{{nope")
    assert limits.get("github_social_preview")["max_bytes"] == 1_000_000
    assert limits.configure("- a list")


def test_no_soft_norms_live_in_the_table():
    # A guard against drift: only hard caps / dims here — never "ideal length" style keys.
    soft = {"ideal_seconds", "sweet_spot", "best_length", "recommended_length"}
    for row in limits.DEFAULT_LIMITS.values():
        assert not soft & set(row)
