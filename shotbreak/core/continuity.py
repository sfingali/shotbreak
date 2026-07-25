"""Continuity state machine.

Walks scenes in story_order within each story_thread to maintain a running
state dict per element+thread pair. Handles:

- Time jumps   → aging checkpoints, writes character_era rows
- Contradictions → continuity_break rows  
- Flashback threads → separate continuity lines from present-day thread
- Settings inheritance → delegates to core/settings.py for cascade context
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass


@dataclass
class ContinuityWalk:
    """Result of walking an element's scenes to collect continuity state."""
    element_id: int
    thread_id: int
    thread_label: str
    states: list[dict]  # [{scene_id, story_order, state_json, changes, is_checkpoint}]


TIME_JUMP_PATTERNS = [
    (re.compile(r"(\d+)\s+(YEARS?|MONTHS?|WEEKS?|DAYS?)\s+(EARLIER|LATER|BEFORE|AGO)", re.IGNORECASE), None),
    (re.compile(r"(THREE|FIVE|TEN|TWENTY|A\s+FEW)\s+(YEARS?|MONTHS?)\s+(EARLIER|LATER)", re.IGNORECASE), None),
]

TIME_JUMP_YEAR_MAP = {
    "A FEW": 2, "THREE": 3, "FIVE": 5, "TEN": 10, "TWENTY": 20,
}


def _parse_years(text: str) -> int | None:
    """Extract approximate years from a time-jump marker string."""
    text_upper = text.upper()
    # Numeric: "12 YEARS EARLIER"
    m = re.search(r"(\d+)\s+YEARS?", text_upper)
    if m:
        return int(m.group(1))
    # Named: "THREE YEARS LATER"
    for word, years in TIME_JUMP_YEAR_MAP.items():
        if word in text_upper:
            return years
    # Months: "6 MONTHS EARLIER" → < 1 year
    if re.search(r"MONTHS?", text_upper):
        return 0
    return None


def walk_element_continuity(
    conn: sqlite3.Connection,
    element_id: int,
    time_jump_threshold_years: int = 2,
) -> list[ContinuityWalk]:
    """Walk all scenes for an element, grouped by story_thread, in story_order.

    Returns one ContinuityWalk per thread the element appears in.
    Flashback threads get their own walk, isolated from the present-day thread.
    """
    rows = conn.execute("""
        SELECT s.id AS scene_id, s.story_order, s.story_date_marker,
               s.narrative_position, s.slugline, s.raw_body,
               COALESCE(st.label, 'present') AS thread_label,
               COALESCE(s.story_thread_id, 1) AS thread_id
        FROM scene s
        JOIN scene_element se ON se.scene_id = s.id
        LEFT JOIN story_thread st ON st.id = s.story_thread_id
        WHERE se.element_id = ?
          AND s.project_id = (SELECT project_id FROM element WHERE id = ?)
        ORDER BY COALESCE(s.story_thread_id, 1), s.story_order
    """, (element_id, element_id)).fetchall()

    if not rows:
        return []

    # Group by thread
    threads: dict[int, list] = {}
    for r in rows:
        threads.setdefault(r["thread_id"], []).append(r)

    walks: list[ContinuityWalk] = []
    for thread_id, thread_rows in threads.items():
        states: list[dict] = []
        # Last non-null date marker seen so far in this thread — most scenes
        # carry no marker at all (only explicit transition scenes do, per
        # DESIGN.md §3.1.4), so a jump must be measured against the last
        # *seen* marker, not the literal previous scene's (usually empty) one.
        last_marker_date: str | None = None

        for i, row in enumerate(thread_rows):
            state: dict = {}
            changes: list[str] = []
            is_checkpoint = False

            # Detect time jumps from story_date_marker
            current_date = row["story_date_marker"]
            if current_date and last_marker_date:
                combined = f"{current_date} {last_marker_date}"
                years = _parse_years(combined)
                if years is not None and years >= time_jump_threshold_years:
                    is_checkpoint = True
                    changes.append(f"Time jump: ~{years} years ({current_date})")

            # Seed initial state from the scene's raw text for first appearance
            if i == 0:
                state = {"_first_appearance": True, "_thread_label": thread_rows[0]["thread_label"]}

            state["_scene_id"] = row["scene_id"]
            state["_story_order"] = row["story_order"]
            state["_slugline"] = row["slugline"]

            states.append({
                "scene_id": row["scene_id"],
                "story_order": row["story_order"],
                "state_json": json.dumps(state),
                "changes": json.dumps(changes),
                "is_checkpoint": is_checkpoint,
            })

            if current_date:
                last_marker_date = current_date

        walks.append(ContinuityWalk(
            element_id=element_id,
            thread_id=thread_id,
            thread_label=thread_rows[0]["thread_label"],
            states=states,
        ))

    return walks


def write_continuity_states(
    conn: sqlite3.Connection,
    element_id: int,
    walks: list[ContinuityWalk],
) -> int:
    """Persist continuity states and write character_era + continuity_break rows.

    Returns count of rows written.
    """
    count = 0
    for walk in walks:
        for s in walk.states:
            conn.execute(
                """INSERT INTO continuity_state
                   (element_id, scene_id, story_thread_id, state_json,
                    changed_from_previous, is_aging_checkpoint)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    element_id,
                    s["scene_id"],
                    walk.thread_id,
                    s["state_json"],
                    s["changes"],
                    int(s["is_checkpoint"]),
                ),
            )
            count += 1

            if s["is_checkpoint"]:
                write_aging_checkpoint(conn, element_id, walk.thread_id, s["scene_id"], s["changes"])

    conn.commit()
    return count


def write_aging_checkpoint(
    conn: sqlite3.Connection,
    element_id: int,
    story_thread_id: int,
    scene_id: int,
    changes_json: str | list,
) -> int:
    """Write a character_era row for an aging checkpoint. Returns the new row id.

    requires_review defaults to 1 (DB default) — per DESIGN.md §3.4.4, an aging
    checkpoint must be human-approved before it takes effect as a new bible version.
    """
    changes = json.loads(changes_json) if isinstance(changes_json, str) else changes_json
    label = changes[0] if changes else "Aging checkpoint"

    cur = conn.execute(
        """INSERT INTO character_era (element_id, story_thread_id, era_label, scene_id)
           VALUES (?, ?, ?, ?)""",
        (element_id, story_thread_id, label, scene_id),
    )
    return cur.lastrowid


def detect_continuity_breaks(
    conn: sqlite3.Connection,
    element_id: int,
    new_state: dict,
    previous_state: dict,
    scene_id: int,
) -> list[dict]:
    """Compare new state to previous and flag contradictions.

    Returns list of break dicts, each written as a continuity_break row.
    """
    breaks: list[dict] = []

    # Compare key fields that should not change without cause
    immutable_keys = ["build", "eye_color", "hair_color", "height", "face_shape"]
    for key in immutable_keys:
        prev_val = previous_state.get(key)
        new_val = new_state.get(key)
        if prev_val and new_val and prev_val != new_val:
            desc = f"Immutable trait '{key}' changed from '{prev_val}' to '{new_val}' at scene {scene_id}"
            conn.execute(
                "INSERT INTO continuity_break (element_id, scene_id, break_type, description) VALUES (?, ?, 'contradiction', ?)",
                (element_id, scene_id, desc),
            )
            breaks.append({"key": key, "previous": prev_val, "new": new_val})

    conn.commit()
    return breaks
