"""Fountain screenplay parser — Fountain → ParsedScene objects.

Handles the Fountain spec plus Shotbreak-specific edge cases:
- Title page metadata
- Scene detection with optional #scene_numbers#
- INT/EXT/INT./EXT / I/E variants
- Character cues, dialogue, parentheticals
- Dual dialogue (^ marker)
- Montage detection (MONTAGE keyword, VARIOUS sluglines)
- Flashback/timeline markers in sluglines
- Missing scene numbers → auto-number
- Bold/italic, centered text, notes [[...]], boneyard /* ... */
- Page breaks (===), sections (# Section), synopses (= Synopsis)
- Multi-language slugline keyword tables
"""

from __future__ import annotations

import re
from pathlib import Path

from shotbreak.core.models import MontageBeat, ParsedScene

# ── Slugline keyword tables per language ─────────────────────────────
# Extensible; add languages as needed.

SLUGLINE_PATTERNS: dict[str, str] = {
    "en": r"^(INT\.?/?EXT\.?|EXT\.?|INT\.?|I/?E\.?)\b",
    "fr": r"^(INT\.?/?EXT\.?|EXT\.?|INT\.?)\b",
    "es": r"^(INT\.?/?EXT\.?|EXT\.?|INT\.?)\b",
    "de": r"^(INT\.?/?EXT\.?|EXT\.?|INT\.?)\b",
    "it": r"^(INT\.?/?EXT\.?|EXT\.?|INT\.?)\b",
}

DAY_NIGHT_TERMS: dict[str, list[str]] = {
    "en": ["DAY", "NIGHT", "DAWN", "DUSK", "MORNING", "AFTERNOON", "EVENING",
           "CONTINUOUS", "LATER", "MOMENTS LATER", "SAME", "MAGIC HOUR", "SUNRISE", "SUNSET"],
    "fr": ["JOUR", "NUIT", "AUBE", "CRÉPUSCULE", "MATIN", "APRÈS-MIDI", "SOIR",
           "CONTINU", "PLUS TARD"],
    "es": ["DÍA", "NOCHE", "AMANECER", "ATARDECER", "MAÑANA", "TARDE",
           "CONTINUO", "MÁS TARDE"],
    "de": ["TAG", "NACHT", "MORGEN", "ABEND", "DÄMMERUNG", "SPÄTER"],
    "it": ["GIORNO", "NOTTE", "ALBA", "TRAMONTO", "MATTINA", "POMERIGGIO",
           "SERA", "CONTINUO", "PIÙ TARDI"],
}

TRANSITIONS: set[str] = {
    "CUT TO:", "FADE IN:", "FADE OUT.", "FADE TO BLACK.", "DISSOLVE TO:",
    "SMASH CUT TO:", "MATCH CUT TO:", "JUMP CUT TO:", "IRIS IN:", "IRIS OUT.",
    "WIPE TO:", "FREEZE FRAME:", "CUT TO BLACK.", "FADE TO:",
}

NARRATIVE_MARKERS: dict[str, str] = {
    "FLASHBACK": "flashback",
    "FLASH FORWARD": "flashforward",
    "DREAM": "dream",
    "MEMORY": "memory",
    "FANTASY": "dream",
    "NIGHTMARE": "dream",
    "INTERCUT": "present",  # intercut is a technique, not a timeline shift
}

MONTAGE_KEYWORDS: set[str] = {"MONTAGE", "VARIOUS", "SEQUENCE OF SHOTS"}

# ── Helper functions ──────────────────────────────────────────────────

def _strip_formatting(text: str) -> tuple[str, bool, bool]:
    """Strip **bold**, *italic*, and _underline_ markers.
    Returns (clean_text, has_bold, has_italic).
    """
    has_bold = "**" in text
    has_italic = "*" in text
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    return text, has_bold, has_italic


def _is_slugline(line: str, lang: str = "en") -> bool:
    """Check if a line is a scene heading/slugline."""
    line = line.strip()
    if not line:
        return False
    pattern = SLUGLINE_PATTERNS.get(lang, SLUGLINE_PATTERNS["en"])
    return bool(re.match(pattern, line, re.IGNORECASE))


def _parse_slugline(slugline: str, lang: str = "en") -> dict[str, str]:
    """Parse a slugline into components."""
    slug = slugline.strip()

    # Extract scene number: trailing #n#, #nA#, #n-m#
    scene_num_match = re.search(r"#(\d+[A-Za-z]?(?:-\d+)?)\s*#\s*$", slug)
    scene_number = scene_num_match.group(1) if scene_num_match else ""
    if scene_num_match:
        slug = slug[: scene_num_match.start()].strip()

    # Determine INT/EXT
    ie = ""
    if re.match(r"^(INT\.?/?EXT\.?)\b", slug, re.IGNORECASE):
        ie = "INT/EXT"
    elif re.match(r"^(INT\.?)\b", slug, re.IGNORECASE):
        ie = "INT"
    elif re.match(r"^(EXT\.?)\b", slug, re.IGNORECASE):
        ie = "EXT"
    elif re.match(r"^(I/?E\.?)\b", slug, re.IGNORECASE):
        ie = "INT/EXT"

    # Remove the INT/EXT prefix
    body = re.sub(r"^(INT\.?/?EXT\.?|EXT\.?|INT\.?|I/?E\.?)\s*", "", slug, flags=re.IGNORECASE).strip()

    # Extract time of day (last element after final dash, if it's a known term)
    terms = DAY_NIGHT_TERMS.get(lang, DAY_NIGHT_TERMS["en"])
    time_of_day = ""
    parts = body.rsplit(" - ", 1)
    if len(parts) == 2:
        last = parts[1].strip().upper()
        # Check if last part matches a time-of-day term (allow parentheticals like "(FLASHBACK)")
        last_clean = re.sub(r"\(.*?\)", "", last).strip()
        if last_clean in [t.upper() for t in terms]:
            time_of_day = last
            body = parts[0].strip()

    # Parse location and set from remaining body
    # Body is: "LOCATION - SET" or "LOCATION"
    body_parts = body.rsplit(" - ", 1)
    if len(body_parts) == 2:
        location = body_parts[0].strip()
        set_name = body_parts[1].strip()
    else:
        location = body.strip()
        set_name = location

    return {
        "scene_number": scene_number,
        "interior_exterior": ie,
        "location": location,
        "set_name": set_name,
        "time_of_day": time_of_day,
    }


def _detect_narrative_position(slugline: str, body_start: str = "") -> str | None:
    """Detect flashback/dream/etc from slugline and body context."""
    combined = (slugline + " " + body_start).upper()
    for marker, position in NARRATIVE_MARKERS.items():
        if marker in combined:
            return position
    return None


def _detect_date_marker(slugline: str, body_start: str = "") -> str | None:
    """Detect time shift markers like 'THREE YEARS LATER'."""
    combined = slugline + " " + body_start
    patterns = [
        r"(\d+\s+(?:YEARS?|MONTHS?|WEEKS?|DAYS?|HOURS?)\s+(?:EARLIER|LATER|BEFORE|AGO))",
        r"((?:THREE|FIVE|TEN|TWENTY|A\s+FEW)\s+(?:YEARS?|MONTHS?|WEEKS?|DAYS?)\s+(?:EARLIER|LATER))",
    ]
    for p in patterns:
        m = re.search(p, combined, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _estimate_page_eighths(body_lines: list[str]) -> int:
    """Estimate page length in eighths from body text.
    Rough heuristic: ~55 lines per page, so eighths = ceil(lines / (55/8)).
    """
    if not body_lines:
        return 1
    # Count non-empty lines
    line_count = sum(1 for l in body_lines if l.strip())
    eighths = max(1, round(line_count / (55 / 8)))
    return min(eighths, 64)  # cap at 8 pages


def _is_character_cue(line: str) -> bool:
    """Check if a line is a character cue (all caps, not too long)."""
    stripped = line.strip()
    if not stripped:
        return False
    # Must be uppercase, possibly with (O.S.), (V.O.), (CONT'D), ^ for dual dialogue
    cue = re.sub(r"\^$", "", stripped).strip()
    return bool(re.match(r"^[A-Z][A-Z0-9\s\.']{1,40}$", cue))


def _is_transition(line: str) -> bool:
    """Check if a line is a transition."""
    stripped = line.strip().rstrip(".")
    return stripped.upper() + ":" in TRANSITIONS or stripped.upper() in TRANSITIONS


def _is_parenthetical(line: str) -> bool:
    """Check if a line is a parenthetical (wryly)."""
    return line.strip().startswith("(") and line.strip().endswith(")")


def _is_centered(line: str) -> bool:
    """Check if a line is centered text: > text <"""
    return line.strip().startswith(">") and line.strip().endswith("<")


# ── Main parser ───────────────────────────────────────────────────────

def parse_fountain(text: str, lang: str = "en") -> list[ParsedScene]:
    """Parse Fountain text into ParsedScene objects.

    Returns a list of scenes in script order.
    """
    lines = text.split("\n")
    scenes: list[ParsedScene] = []
    current_body: list[str] = []
    current_slugline: str = ""
    current_chars: list[str] = []
    in_scene = False
    auto_num = 0
    body_start_line = ""  # first non-blank action line after slugline, for context

    # Dual dialogue tracking
    dual_dialogue = False

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Skip title page (everything before first slugline)
        if not in_scene:
            if _is_slugline(line, lang):
                in_scene = True
                # fall through to process this slugline
            else:
                i += 1
                continue

        # Check for new slugline
        if _is_slugline(line, lang) and stripped not in ("",):
            # Save previous scene
            if current_slugline:
                parsed = _parse_slugline(current_slugline, lang)
                eighths = _estimate_page_eighths(current_body)
                is_montage = any(kw in current_slugline.upper() for kw in MONTAGE_KEYWORDS)
                nar_pos = _detect_narrative_position(current_slugline, body_start_line)
                date_marker = _detect_date_marker(current_slugline, body_start_line)

                if parsed["scene_number"]:
                    scene_num = parsed["scene_number"]
                    scene_num_src = "script"
                else:
                    auto_num += 1
                    scene_num = f"A{auto_num}"
                    scene_num_src = "auto"

                # Detect montage beats
                montage_beats = _extract_montage_beats(current_body) if is_montage else []

                scenes.append(ParsedScene(
                    scene_number=scene_num,
                    scene_number_source=scene_num_src,
                    slugline=current_slugline,
                    interior_exterior=parsed["interior_exterior"],
                    location=parsed["location"],
                    set_name=parsed["set_name"],
                    time_of_day=parsed["time_of_day"],
                    body_lines=current_body[:],
                    characters=list(set(current_chars)),
                    page_eighths=eighths,
                    has_dual_dialogue=dual_dialogue,
                    is_montage=is_montage,
                    montage_beats=montage_beats,
                    narrative_position_hint=nar_pos,
                    story_date_marker=date_marker,
                ))

            # Start new scene
            current_slugline = stripped
            current_body = []
            current_chars = []
            dual_dialogue = False
            body_start_line = ""
            i += 1
            continue

        # Inside a scene — classify each line
        if not stripped:
            current_body.append(line)
            i += 1
            continue

        # Skip boneyard blocks
        if stripped.startswith("/*"):
            # Skip until */
            while i < len(lines) and "*/" not in lines[i]:
                i += 1
            i += 1
            continue

        # Single-line boneyard
        if stripped.startswith("/*") and "*/" in stripped:
            i += 1
            continue

        # Notes — keep in body but skip for analysis
        if stripped.startswith("[["):
            current_body.append(line)
            i += 1
            continue

        # Section headers
        if stripped.startswith("#"):
            current_body.append(line)
            i += 1
            continue

        # Synopsis
        if stripped.startswith("="):
            current_body.append(line)
            i += 1
            continue

        # Page break
        if stripped == "===":
            current_body.append(line)
            i += 1
            continue

        # Centered text
        if _is_centered(stripped):
            current_body.append(line)
            i += 1
            continue

        # Transition
        if _is_transition(stripped):
            current_body.append(line)
            i += 1
            continue

        # Dual dialogue marker on character cue
        is_dual = stripped.endswith("^")
        if is_dual:
            dual_dialogue = True

        # Character cue
        if _is_character_cue(stripped):
            cue = stripped.rstrip("^").strip()
            current_chars.append(cue)
            current_body.append(line)
            i += 1
            continue

        # Parenthetical
        if _is_parenthetical(stripped):
            current_body.append(line)
            i += 1
            continue

        # Dialogue or action — track first non-empty action line for context
        if not body_start_line and not _is_character_cue(stripped) and not _is_parenthetical(stripped):
            body_start_line = stripped

        current_body.append(line)
        i += 1

    # Save final scene
    if current_slugline:
        parsed = _parse_slugline(current_slugline, lang)
        eighths = _estimate_page_eighths(current_body)
        is_montage = any(kw in current_slugline.upper() for kw in MONTAGE_KEYWORDS)
        nar_pos = _detect_narrative_position(current_slugline, body_start_line)
        date_marker = _detect_date_marker(current_slugline, body_start_line)
        auto_num += 1
        scene_num = parsed["scene_number"] or str(auto_num)
        scene_num_src = "script" if parsed["scene_number"] else "auto"
        montage_beats = _extract_montage_beats(current_body) if is_montage else []

        scenes.append(ParsedScene(
            scene_number=scene_num,
            scene_number_source=scene_num_src,
            slugline=current_slugline,
            interior_exterior=parsed["interior_exterior"],
            location=parsed["location"],
            set_name=parsed["set_name"],
            time_of_day=parsed["time_of_day"],
            body_lines=current_body[:],
            characters=list(set(current_chars)),
            page_eighths=eighths,
            has_dual_dialogue=dual_dialogue,
            is_montage=is_montage,
            montage_beats=montage_beats,
            narrative_position_hint=nar_pos,
            story_date_marker=date_marker,
        ))

    return scenes


def _extract_montage_beats(body_lines: list[str]) -> list[MontageBeat]:
    """Extract montage sub-beats from body text.
    Looks for dash/bullet-prefixed lines or mini-sluglines within a montage.
    """
    beats: list[MontageBeat] = []
    order = 0
    for line in body_lines:
        stripped = line.strip()
        # Beat markers: lines starting with dash or bullet
        if stripped.startswith("-") and len(stripped) > 2:
            order += 1
            beat_text = stripped[1:].strip()
            # Try to extract location hint from bold/italic markers or ALL CAPS prefix
            loc_hint = None
            loc_match = re.match(r"^([A-Z][A-Z\s\.]+?)\s*[-—–]\s*", beat_text)
            if loc_match:
                loc_hint = loc_match.group(1).strip()
                # Verify it looks like a location (not a character name)
                if len(loc_hint.split()) > 5:
                    loc_hint = None
            beats.append(MontageBeat(beat_order=order, beat_text=beat_text, location_hint=loc_hint))
    return beats


def parse_file(path: str | Path, lang: str = "en") -> list[ParsedScene]:
    """Parse a Fountain file from disk."""
    text = Path(path).read_text(encoding="utf-8")
    return parse_fountain(text, lang)
