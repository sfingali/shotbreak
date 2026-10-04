"""Fade In XML (.fadein) parser — Fade In → ParsedScene objects.

Fade In Pro XML format uses lowercase type attributes (<paragraph type="scene heading">)
and a single <text> child per paragraph. Scene headings include the scene number
as an optional "number" attribute.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from shotbreak.core.fountain_parser import _parse_slugline as fountain_parse_slugline
from shotbreak.core.models import ParsedScene

# Fade In paragraph types
SCENE_HEADING = "scene heading"
CHARACTER = "character"
DIALOGUE = "dialogue"
ACTION = "action"
TRANSITION = "transition"


def _get_text(el: ET.Element) -> str:
    """Get text from the single <text> child element."""
    child = el.find("text")
    return (child.text or "").strip() if child is not None else ""


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


def parse_fadein(text: str) -> list[ParsedScene]:
    """Parse Fade In XML text into ParsedScene objects."""
    if "<!DOCTYPE" in text.upper():
        raise ValueError("Fade In XML containing a DOCTYPE is not allowed for security reasons")
    root = ET.fromstring(text)
    # Root is <document type="Fade In Pro"> containing <content>
    content = root.find("content")
    if content is None:
        return []

    paragraphs = content.findall("paragraph")
    scenes: list[ParsedScene] = []
    current: ParsedScene | None = None
    characters: list[str] = []
    body_lines: list[str] = []
    auto_num: int = 0

    for para in paragraphs:
        ptype = (para.get("type") or "").lower()
        text = _get_text(para)
        if not text:
            continue

        if ptype == SCENE_HEADING:
            # Save previous scene
            if current is not None:
                current.characters = list(set(characters))
                current.body_lines = body_lines.copy()
                current.page_eighths = max(1, round(len(body_lines) / (55 / 8)))
                scenes.append(current)

            # Start new scene
            scene_num = para.get("number", "")
            parsed = fountain_parse_slugline(text)
            current = ParsedScene(
                scene_number=scene_num or str(auto_num + 1),
                scene_number_source="script" if scene_num else "auto",
                slugline=text,
                interior_exterior=parsed["interior_exterior"],
                location=parsed["location"],
                set_name=parsed["set_name"],
                time_of_day=parsed["time_of_day"],
                narrative_position_hint=_detect_narrative(text),
            )
            auto_num += 1
            characters = []
            body_lines = []

        elif current is not None:
            if ptype in (CHARACTER, DIALOGUE, ACTION, TRANSITION):
                body_lines.append(text)
                if ptype == CHARACTER:
                    characters.append(text)

    # Save final scene
    if current is not None:
        current.characters = list(set(characters))
        current.body_lines = body_lines.copy()
        current.page_eighths = max(1, round(len(body_lines) / (55 / 8)))
        scenes.append(current)

    return scenes


def parse_file(path: str | Path) -> list[ParsedScene]:
    """Parse a Fade In file from disk."""
    return parse_fadein(Path(path).read_text(encoding="utf-8"))
