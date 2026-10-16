"""The brand intro: preset validation, expansion in front of a montage (bumper → title card →
CROSSFADE into the first shot), and the guards — an intro is ≤ max_s (6 s) and never hard-cuts."""

from __future__ import annotations

import pytest
from campaign import intro, montage, outro, paths, store

LOOK = {
    "colors": {"bg": "#0a0a0c", "fg": "#ededed", "accent": "#9b87f2", "muted": "#a1a1aa"},
    "fonts": {"heading": "", "body": ""},
    "logo": "",
    "name": "",
}
CARD = {"eyebrow": "Architecture walkthrough", "title": "protoAgent", "duration": 3.0}


@pytest.fixture
def bumper(tmp_path):
    p = tmp_path / "splash.mp4"
    p.write_bytes(b"x")
    return p


@pytest.fixture
def take():
    c = store.create_campaign("Launch")
    p = paths.campaign_dir(c["id"], "Launch") / "walk.mp4"
    p.write_bytes(b"x")
    return c, store.add_asset(c["id"], "clip", "walkthrough", path=str(p), status="captured")


def _probe(path):
    return {"width": 2560, "height": 1440, "duration_s": 90.0 if "walk" in path else 4.3}


def _preset(bumper, **kw):
    return {"bumper": {"path": str(bumper), "in": 0.1, "out": 2.3}, "card": CARD, **kw}


def _plan(c, seq):
    items, _ = montage.resolve_items(c["id"], montage.parse_sequence(seq), _probe)
    opts = montage.normalize_output({"size": "2560x1440"})
    return montage.plan(items, opts, LOOK)


def test_the_preset_validates_and_reports_every_problem(bumper, tmp_path):
    assert intro.configure(_preset(bumper)) == []
    probs = "\n".join(
        intro.configure(
            {
                "bumpr": 1,
                "bumper": {"path": str(tmp_path / "nope.mp4")},
                "transition": {"style": "cut"},
                "card": {"template": "og"},
            }
        )  # fmt: skip
    )
    for bit in ("unknown key `bumpr`", "nope.mp4 doesn't exist", "never a cut", "intro.card.template"):
        assert bit in probs
    assert intro.preset() == {}
    assert (
        "a crossfade needs at least"
        in intro.configure({**_preset(bumper), "transition": {"style": "fade", "duration": 0.1}})[0]
    )
    assert (
        "fixed-length signature"
        in intro.configure({**_preset(bumper), "into_content": {"style": "edge_sweep", "duration": 1}})[0]
    )


def test_intro_is_first_needs_a_preset_and_an_override_may_not_name_a_file(bumper):
    with pytest.raises(montage.MontageError, match="no intro preset is configured"):
        montage.parse_sequence([{"intro": True}, {"clip": 1}])
    intro.configure(_preset(bumper))
    with pytest.raises(montage.MontageError, match="must be the FIRST item"):
        montage.parse_sequence([{"clip": 1}, {"intro": True}])
    with pytest.raises(montage.MontageError, match="only come from the operator's intro setting"):
        montage.parse_sequence([{"intro": {"bumper": {"path": "/etc/hosts"}}}, {"clip": 1}])
    with pytest.raises(montage.MontageError, match="needs a shot after it"):
        montage.parse_sequence([{"intro": True}])


def test_the_intro_expands_to_bumper_card_and_a_reveal_into_the_first_shot(bumper):
    intro.configure(_preset(bumper))
    items = montage.parse_sequence([{"intro": {"card": {"title": "protoAgent in 85 seconds",
                                                        "subtitle": "How it fits together"}}}, {"clip": 1, "in": 4.2}])  # fmt: skip
    bump, card, walk = items
    assert bump["fixed_path"] == str(bumper.resolve()) and (bump["in"], bump["out"]) == (0.1, 2.3)
    assert bump["transition"] is None, "the bumper opens cold"
    assert card["card"]["template"] == "title-slide" and card["card"]["title"] == "protoAgent in 85 seconds"
    assert card["card"]["eyebrow"] == "Architecture walkthrough", "the preset's card keys survive a title override"
    assert card["transition"] == {"style": "boot_seam"}, "splash → title card: a brand boundary"
    assert walk["transition"] == {"style": "edge_sweep"}, "title → walkthrough: the signature sweep, never a cut"


def test_an_intro_never_hard_cuts_into_the_first_shot(bumper):
    intro.configure(_preset(bumper))
    with pytest.raises(montage.MontageError, match="would be a hard cut"):
        montage.parse_sequence([{"intro": True}, {"clip": 1, "transition": "cut"}])


def test_guard_the_intro_is_at_most_six_seconds_and_reveals_the_shot(bumper, take):
    c, a = take
    intro.configure(_preset(bumper))
    tl = _plan(c, [{"intro": True}, {"clip": a["id"], "in": 4.2, "out": 80.0}])
    # bumper 2.2 s → (boot seam 0.4) card 3.0 s → (edge sweep 0.6) walkthrough, fully up 0.6 s later.
    assert tl["intro_s"] == pytest.approx(2.2 + 3.0 - 0.4 - 0.6 + 0.6)
    assert tl["intro_s"] <= 6.0
    into_walk = next(t for t in tl["transitions"] if t["into"] == 2)
    assert into_walk["style"] == "edge_sweep" and into_walk["frames"] == 18
    with pytest.raises(montage.MontageError, match=r"the intro runs 7\.\d\ds .* over its max_s 6s"):
        _plan(c, [{"intro": {"card": {"duration": 5.5}}}, {"clip": a["id"], "in": 4.2}])


def test_an_intro_can_reuse_the_outro_bumper(bumper):
    outro.configure({"bumper": {"path": str(bumper), "in": 0.1, "out": 2.7}, "cta": {"title": "Star it"}})
    assert intro.configure({"bumper": "outro", "card": CARD}) == []
    (bump, _card, _walk) = montage.parse_sequence([{"intro": True}, {"clip": 1}])
    assert bump["fixed_path"] == str(bumper.resolve()) and bump["out"] == 2.7


def test_booleans_are_not_seconds(bumper):
    probs = "\n".join(intro.configure({**_preset(bumper), "max_s": True}))
    assert "intro.max_s must be a number, got True" in probs
    assert "outro.beat_s must be a number, got False" in "\n".join(
        outro.configure({"beat_s": False, "cta": {"title": "x"}})
    )
