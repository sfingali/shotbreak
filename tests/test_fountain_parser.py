"""Tests for the Shotbreak Fountain parser."""

from shotbreak.core.fountain_parser import (
    _detect_narrative_position,
    _estimate_page_eighths,
    _is_character_cue,
    _is_slugline,
    _parse_slugline,
    parse_fountain,
)


class TestSluglineDetection:
    def test_standard_int(self):
        assert _is_slugline("INT. MASTER BEDROOM - HOUSE - NIGHT")

    def test_standard_ext(self):
        assert _is_slugline("EXT. BEACH - DAY")

    def test_int_ext_combined(self):
        assert _is_slugline("INT./EXT. CAR - DAY")

    def test_no_period_variant(self):
        assert _is_slugline("INT CAR - DAY")

    def test_not_a_slugline(self):
        assert not _is_slugline("Ben stands up.")
        assert not _is_slugline("BEN")
        assert not _is_slugline("")


class TestSluglineParsing:
    def test_full_slugline_with_number(self):
        result = _parse_slugline("INT. MASTER BEDROOM - HOUSE - NIGHT #1#")
        assert result["scene_number"] == "1"
        assert result["interior_exterior"] == "INT"
        assert result["location"] == "MASTER BEDROOM"
        assert result["set_name"] == "HOUSE"
        assert result["time_of_day"] == "NIGHT"

    def test_slugline_with_numbered_scene_letter(self):
        result = _parse_slugline("INT. KITCHEN - DAY #12A#")
        assert result["scene_number"] == "12A"

    def test_slugline_no_number(self):
        result = _parse_slugline("INT. BATHROOM - DAY")
        assert result["scene_number"] == ""
        assert result["location"] == "BATHROOM"
        assert result["time_of_day"] == "DAY"

    def test_slugline_no_set(self):
        result = _parse_slugline("EXT. FOREST - DAY")
        assert result["location"] == "FOREST"
        assert result["set_name"] == "FOREST"

    def test_flashback_marker_in_slugline(self):
        result = _parse_slugline("INT. CORNER OFFICE (FLASHBACK) - DAY #257#")
        assert result["scene_number"] == "257"
        assert result["time_of_day"] == "DAY"  # parenthetical stripped from time-of-day


class TestNarrativeDetection:
    def test_flashback_in_slugline(self):
        assert _detect_narrative_position("INT. OFFICE (FLASHBACK) - DAY") == "flashback"

    def test_dream_in_slugline(self):
        assert _detect_narrative_position("INT. ROOM - NIGHT", "DREAM SEQUENCE") == "dream"

    def test_no_narrative_marker(self):
        assert _detect_narrative_position("INT. KITCHEN - DAY", "Ben cooks.") is None


class TestCharacterCue:
    def test_standard_cue(self):
        assert _is_character_cue("BEN")

    def test_cue_with_extension(self):
        assert _is_character_cue("MARIE (O.S.)")
        assert _is_character_cue("BEN (V.O.)")

    def test_cue_with_dual_dialogue(self):
        assert _is_character_cue("MARIE ^")

    def test_not_a_cue(self):
        assert not _is_character_cue("Ben stands up.")
        assert not _is_character_cue("")
        assert not _is_character_cue("This is way too long to be a character cue honestly")


class TestPageEighths:
    def test_empty_body(self):
        assert _estimate_page_eighths([]) == 1

    def test_short_scene(self):
        lines = ["BEN", "Hello.", "Marie", "Hi."]
        assert 1 <= _estimate_page_eighths(lines) <= 2

    def test_longer_scene(self):
        lines = ["Line " + str(i) for i in range(100)]
        eighths = _estimate_page_eighths(lines)
        assert 8 <= eighths <= 20


class TestParseFountain:
    def test_basic_script(self):
        text = """INT. ROOM - DAY #1#

Ben enters. He looks around.

BEN
Hello.

MARIE
Hi there.

INT. KITCHEN - NIGHT #2#

They cook dinner.

CUT TO:"""
        scenes = parse_fountain(text)
        assert len(scenes) == 2
        assert scenes[0].scene_number == "1"
        assert scenes[0].interior_exterior == "INT"
        assert scenes[0].location == "ROOM"
        assert scenes[0].time_of_day == "DAY"
        assert "BEN" in scenes[0].characters  # stored in uppercase as-in-script
        assert "MARIE" in scenes[0].characters
        assert scenes[1].scene_number == "2"
        assert scenes[1].time_of_day == "NIGHT"

    def test_script_with_no_numbers(self):
        text = """INT. ROOM - DAY

Ben enters.

INT. KITCHEN - NIGHT

Ben cooks."""
        scenes = parse_fountain(text)
        assert len(scenes) == 2
        assert scenes[0].scene_number_source == "auto"
        assert scenes[0].scene_number == "A1"
        assert scenes[1].scene_number == "A2"

    def test_flashback_detection(self):
        text = """INT. OFFICE (FLASHBACK) - DAY #10#

Ben works."""
        scenes = parse_fountain(text)
        assert len(scenes) == 1
        assert scenes[0].narrative_position_hint == "flashback"

    def test_title_page_skipped(self):
        text = """Title: My Script
Author: Me

INT. ROOM - DAY #1#

Ben sits."""
        scenes = parse_fountain(text)
        assert len(scenes) == 1
        assert scenes[0].scene_number == "1"

    def test_boneyard_skipped(self):
        text = """INT. ROOM - DAY #1#

Ben enters.
/* This is a boneyard comment
that spans multiple lines */
Ben exits."""
        scenes = parse_fountain(text)
        assert len(scenes) == 1
        body = "\n".join(scenes[0].body_lines)
        assert "boneyard" not in body.lower()

    def test_notes_kept_in_body(self):
        text = """INT. ROOM - DAY #1#

Ben enters [[this is a production note]]."""
        scenes = parse_fountain(text)
        assert len(scenes) == 1
        body = "\n".join(scenes[0].body_lines)
        assert "[[" in body
