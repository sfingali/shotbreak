"""Final Draft XML (.fdx) parser — FDX → ParsedScene objects.

Produces the same ParsedScene dataclass as fountain_parser.py so both
parsers drop into service.import_script() interchangeably.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from shotbreak.core.models import ParsedScene

# Slugline field extraction — same logic as fountain_parser
SLUG_RE = re.compile(
    r"^(INT\.?/?EXT\.?|EXT\.?|INT\.?|I/?E\.?)\s+(.+?)\s*$", re.IGNORECASE,
)


def _parse_slugline(slug: str) -> dict[str, str]:
    """Parse a slugline into interior_exterior, location, set_name, time_of_day."""
    slug = slug.strip()
    m = SLUG_RE.match(slug)
    if not m:
        return {
            "interior_exterior": "",
            "location": slug,
            "set_name": slug,
            "time_of_day": "",
        }

    # Determine INT/EXT
    prefix = m.group(1).upper().rstrip(".")
    if "/" in prefix:
        ie = "INT/EXT"
    elif prefix == "INT":
        ie = "INT"
    elif prefix == "EXT":
        ie = "EXT"
    else:
        ie = prefix

    body = m.group(2).strip()

    # Time of day: last segment after final " - "
    time_of_day = ""
    parts = body.rsplit(" - ", 1)
    if len(parts) == 2:
        last = parts[1].strip().upper()
        # Clean parentheticals like "(FLASHBACK)"
        last_clean = re.sub(r"\(.*?\)", "", last).strip()
        known_times = {
            "DAY", "NIGHT", "DAWN", "DUSK", "MORNING", "AFTERNOON", "EVENING",
            "CONTINUOUS", "LATER", "MOMENTS LATER", "SAME", "MAGIC HOUR",
            "SUNRISE", "SUNSET",
        }
        if last_clean in known_times:
            time_of_day = last
            body = parts[0].strip()

    # Location and set name
    body_parts = body.rsplit(" - ", 1)
    if len(body_parts) == 2:
        location = body_parts[0].strip()
        set_name = body_parts[1].strip()
    else:
        location = body.strip()
        set_name = location

    return {
        "interior_exterior": ie,
        "location": location,
        "set_name": set_name,
        "time_of_day": time_of_day,
    }


def _is_character_cue(line: str) -> bool:
    """FDX Character paragraphs are always character names."""
    stripped = line.strip()
    return bool(stripped) and len(stripped) < 50 and not stripped.startswith("(")


def _estimate_page_eighths(page_length_str: str) -> int:
    """Convert FDX SceneProperties Length (like '2/8') to eighths integer."""
    try:
        return int(page_length_str.split("/")[0])
    except (ValueError, IndexError):
        return 1


def _get_text(el: ET.Element) -> str:
    """Join all <Text> children into a single string."""
    return "".join(t.text or "" for t in el.findall("Text"))


def parse_fdx(text: str) -> list[ParsedScene]:
    """Parse FDX XML text into ParsedScene objects."""
    root = ET.fromstring(text)
    content = root.find("Content")
    if content is None:
        return []

    paragraphs = content.findall("Paragraph")
    scenes: list[ParsedScene] = []

    current_slugline: str | None = None
    current_body: list[str] = []
    current_chars: list[str] = []
    current_page_eighths: int = 1
    current_narrative: str | None = None
    current_scene_num: str = ""
    auto_num: int = 0
    has_dual: bool = False

    for para in paragraphs:
        ptype = para.get("Type", "")
        text = _get_text(para).strip()
        if not text and ptype != "Scene Heading":
            continue

        if ptype == "Scene Heading":
            # Save previous scene
            if current_slugline is not None:
                auto_num += 1
                parsed_slug = _parse_slugline(current_slugline)
                scenes.append(ParsedScene(
                    scene_number=current_scene_num or str(auto_num),
                    scene_number_source="script" if current_scene_num else "auto",
                    slugline=current_slugline,
                    interior_exterior=parsed_slug["interior_exterior"],
                    location=parsed_slug["location"],
                    set_name=parsed_slug["set_name"],
                    time_of_day=parsed_slug["time_of_day"],
                    body_lines=current_body.copy(),
                    characters=list(set(current_chars)),
                    page_eighths=current_page_eighths,
                    has_dual_dialogue=has_dual,
                    narrative_position_hint=current_narrative,
                ))

            # Start new scene — capture slugline, narrative hint, scene number
            current_slugline = text
            current_narrative = _detect_narrative(text)
            current_body = []
            current_chars = []
            has_dual = False
            sp = para.find("SceneProperties")
            current_scene_num = sp.get("Number", "") if sp is not None else ""
            current_page_eighths = (
                _estimate_page_eighths(sp.get("Length", "1/8"))
                if sp is not None
                else 1
            )

        elif ptype == "Character":
            current_chars.append(text)
            current_body.append(text)

        elif ptype == "Dialogue":
            current_body.append(text)

        elif ptype in ("Action", "General", "Transition", "Shot"):
            current_body.append(text)

    # Save final scene
    if current_slugline is not None:
        parsed = _parse_slugline(current_slugline)
        auto_num += 1
        scenes.append(ParsedScene(
            scene_number=current_scene_num or str(auto_num),
            scene_number_source="script" if current_scene_num else "auto",
            slugline=current_slugline,
            interior_exterior=parsed["interior_exterior"],
            location=parsed["location"],
            set_name=parsed["set_name"],
            time_of_day=parsed["time_of_day"],
            body_lines=current_body.copy(),
            characters=list(set(current_chars)),
            page_eighths=current_page_eighths,
            has_dual_dialogue=has_dual,
            narrative_position_hint=current_narrative,
        ))

    return scenes


def _detect_narrative(slugline: str) -> str | None:
    upper = slugline.upper()
    if "FLASHBACK" in upper:
        return "flashback"
    if "FLASH FORWARD" in upper:
        return "flashforward"
    if "DREAM" in upper:
        return "dream"
    if "MEMORY" in upper:
        return "memory"
    return None


def parse_file(path: str | Path) -> list[ParsedScene]:
    """Parse an FDX file from disk."""
    return parse_fdx(Path(path).read_text(encoding="utf-8"))
