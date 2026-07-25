"""Two-phase physical description engine.

Phase A — Bible Construction:
  One LLM call per character reading the ENTIRE character arc plus inherited
  settings. Produces a canonical bible: immutable traits, mutable baseline,
  unspecified_fields (explicitly refused to invent), era candidates.

Phase B — Per-Scene Rendering:
  For each scene in story_order within its thread. Input: bible + inherited
  settings + previous continuity state + current scene text. Output: visible
  attributes, deltas, new continuity state.

Tiered inference model:
  explicit  — stated in text           → is_inferred=0
  implied   — strongly inferable        → is_inferred=1
  archetypal default — refuse to generate → bias_flag=1, continuity_break row
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from shotbreak.core import continuity as _continuity
from shotbreak.core import settings as _settings
from shotbreak.core.llm import provider as llm_provider


BIBLE_SCHEMA = {
    "type": "object",
    "properties": {
        "immutable": {
            "type": "object",
            "description": "Traits that do not change across the film",
            "properties": {
                "build": {"type": "string"},
                "face": {"type": "string"},
                "eye_color": {"type": "string"},
                "hair_color": {"type": "string"},
                "distinguishing_marks": {"type": "string"},
                "age_range": {"type": "string"},
                "default_silhouette": {"type": "string"},
            },
        },
        "mutable_baseline": {
            "type": "object",
            "description": "Defaults that may change per scene",
            "properties": {
                "default_wardrobe": {"type": "string"},
                "default_hair": {"type": "string"},
                "default_movement": {"type": "string"},
            },
        },
        "unspecified_fields": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Demographic/physical attributes the script does NOT state. The LLM MUST list these rather than silently invent them.",
        },
        "era_candidates": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Detected time jumps that might require a separate appearance variant.",
        },
        "confidence": {
            "type": "number",
            "description": "Overall confidence in the bible, 0-1.",
        },
    },
    "required": ["immutable", "mutable_baseline", "unspecified_fields", "era_candidates", "confidence"],
}


SCENE_DESCRIPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "visible_this_scene": {
            "type": "object",
            "properties": {
                "clothing": {"type": "string"},
                "props_on_person": {"type": "string"},
                "hair_makeup": {"type": "string"},
                "tattoos_scars": {"type": "string"},
                "movement_style": {"type": "string"},
            },
        },
        "scene_context": {
            "type": "object",
            "properties": {
                "lighting": {"type": "string"},
                "environment": {"type": "string"},
                "emotional_state": {"type": "string"},
            },
        },
        "continuity_changes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "What changed since the last scene this character appeared in.",
        },
        "new_continuity_state": {
            "type": "object",
            "description": "Key/value pairs of current state to carry forward.",
        },
        "inference_tier": {
            "type": "string",
            "enum": ["explicit", "implied", "archetypal_default"],
            "description": "How much of this description is stated vs inferred vs guessed.",
        },
        "full_composite_prompt": {
            "type": "string",
            "description": "A single prompt suitable for feeding to an image generator for this character in this scene.",
        },
    },
    "required": ["visible_this_scene", "scene_context", "continuity_changes", "new_continuity_state", "inference_tier"],
}


def _settings_context(conn: sqlite3.Connection, project_id: int, location: str | None) -> str:
    """Format inherited settings as prompt context."""
    effective = _settings.get_effective_settings(conn, project_id, location=location)
    if not effective:
        return ""
    lines = ["INHERITED SETTINGS (apply to this scene):"]
    for k, v in effective.items():
        lines.append(f"  - {k}: {v}")
    return "\n".join(lines)


def build_character_bible(
    conn: sqlite3.Connection,
    config: dict,
    project_id: int,
    element_id: int,
    provider_name: str,
) -> dict:
    """Phase A: Build the character bible by reading the full character arc.

    Returns dict with bible_id, bible_json, llm_tokens.
    """

    # Get character info
    el = conn.execute(
        "SELECT name, category FROM element WHERE id=? AND project_id=?",
        (element_id, project_id),
    ).fetchone()
    if not el:
        raise ValueError(f"Element {element_id} not found in project {project_id}")
    character_name = el["name"]

    # Gather ALL scenes this character appears in (full arc)
    scene_rows = conn.execute("""
        SELECT s.slugline, s.raw_body, s.story_order, s.narrative_position,
               s.location, s.time_of_day
        FROM scene s
        JOIN scene_element se ON se.scene_id = s.id
        WHERE se.element_id = ? AND s.project_id = ?
        ORDER BY s.story_order
    """, (element_id, project_id)).fetchall()

    if not scene_rows:
        raise ValueError(f"Character '{character_name}' has no tagged scenes")

    # Build full-arc text
    arc_parts = []
    all_locations = set()
    for r in scene_rows:
        nar = f" [{r['narrative_position']}]" if r['narrative_position'] and r['narrative_position'] != 'present' else ""
        arc_parts.append(
            f"SCENE {r['story_order']}{nar}: {r['slugline']}\n{r['raw_body']}"
        )
        if r["location"]:
            all_locations.add(r["location"])

    full_arc = "\n\n".join(arc_parts)

    # Collect inherited settings from all locations this character appears in
    settings_text_parts = []
    for loc in sorted(all_locations):
        ctx = _settings_context(conn, project_id, loc)
        if ctx:
            settings_text_parts.append(ctx)
    settings_text = "\n\n".join(settings_text_parts)

    # Phase A: Bible Construction
    system = (
        "You are a character designer and continuity supervisor for film production. "
        "Your job is to build a character bible from the COMPLETE arc of a character "
        "across the entire screenplay.\n\n"
        "CRITICAL RULES:\n"
        "1. Only describe what is explicitly stated or strongly implied in the text.\n"
        "2. If an attribute is NOT stated (race, specific age, height, exact eye color, etc.), "
        "LIST IT IN 'unspecified_fields' — DO NOT invent it. An invented attribute becomes "
        "silently load-bearing for every downstream image generation.\n"
        "3. Distinguish immutable traits (won't change) from mutable baselines (can change per scene).\n"
        "4. Note any time jumps or era shifts that might require a different appearance.\n"
        "5. Be specific and visual — use photographic language: materials, colors, fit, condition."
    )

    user = (
        f"CHARACTER: {character_name}\n\n"
        f"{settings_text}\n\n"
        f"FULL CHARACTER ARC (all scenes, in story order):\n\n{full_arc}\n\n"
        f"Build the character bible."
    )

    response = llm_provider.complete(
        config, provider_name, system, user, max_tokens=4096, json_schema=BIBLE_SCHEMA,
    )

    bible_data = json.loads(response.text)

    # Write bible as physical_description (scene_id=NULL = bible entry)
    conn.execute("UPDATE physical_description SET is_current=0 WHERE element_id=? AND scene_id IS NULL AND is_current=1", (element_id,))

    cur = conn.execute(
        """INSERT INTO physical_description
           (element_id, scene_id, description_type, content, is_inferred, bias_flag, version, is_approved, is_current, generation_metadata)
           VALUES (?, NULL, 'bible', ?, 0, 0, 1, 0, 1, ?)""",
        (
            element_id,
            json.dumps(bible_data),
            json.dumps({"provider": provider_name, "model": config.get("providers", {}).get(provider_name, {}).get("model"), "tokens_in": response.input_tokens, "tokens_out": response.output_tokens}),
        ),
    )
    bible_id = cur.lastrowid
    conn.commit()

    # Write unspecified_fields as continuity_breaks requiring human review
    for field in bible_data.get("unspecified_fields", []):
        conn.execute(
            "INSERT INTO continuity_break (element_id, scene_id, break_type, description) VALUES (?, NULL, 'inferred_pending_review', ?)",
            (element_id, f"Bible: unspecified_field '{field}' — no script basis. Must be provided by human before image generation."),
        )
    conn.commit()

    return {
        "bible_id": bible_id,
        "character": character_name,
        "scene_count": len(scene_rows),
        "unspecified_fields": bible_data.get("unspecified_fields", []),
        "immutable": bible_data.get("immutable", {}),
        "tokens_in": response.input_tokens,
        "tokens_out": response.output_tokens,
    }


def render_scene_descriptions(
    conn: sqlite3.Connection,
    config: dict,
    project_id: int,
    element_id: int,
    provider_name: str,
    scene_ids: list[int] | None = None,
) -> dict:
    """Phase B: Generate per-scene physical descriptions in story order.

    If scene_ids is None, renders all scenes the character appears in.
    Skips scenes that already have a current description (idempotent).
    """

    # Get bible
    bible_row = conn.execute(
        "SELECT id, content FROM physical_description WHERE element_id=? AND scene_id IS NULL AND is_current=1",
        (element_id,),
    ).fetchone()

    if not bible_row:
        raise ValueError(f"No bible found for element {element_id}. Run bible construction first.")

    bible = bible_row["content"]
    bible_id = bible_row["id"]

    # Get character name
    el = conn.execute("SELECT name FROM element WHERE id=?", (element_id,)).fetchone()

    # Get scenes in story_order within each thread
    query = """
        SELECT s.id, s.slugline, s.raw_body, s.story_order, s.narrative_position,
               s.location, s.time_of_day, s.story_thread_id,
               COALESCE(st.label, 'present') AS thread_label
        FROM scene s
        JOIN scene_element se ON se.scene_id = s.id
        LEFT JOIN story_thread st ON st.id = s.story_thread_id
        WHERE se.element_id = ? AND s.project_id = ?
    """
    params: list = [element_id, project_id]
    if scene_ids:
        placeholders = ",".join("?" * len(scene_ids))
        query += f" AND s.id IN ({placeholders})"
        params.extend(scene_ids)
    query += " ORDER BY COALESCE(s.story_thread_id, 1), s.story_order"

    scene_rows = conn.execute(query, params).fetchall()

    if not scene_rows:
        return {"element": el["name"] if el else str(element_id), "scenes_rendered": 0, "errors": []}

    # Walk continuity to get state from previous scenes
    walks = _continuity.walk_element_continuity(conn, element_id)
    _continuity.write_continuity_states(conn, element_id, walks)

    results = []
    errors = []
    for row in scene_rows:
        try:
            # Skip if already has a current description for this scene
            existing = conn.execute(
                "SELECT id FROM physical_description WHERE element_id=? AND scene_id=? AND is_current=1",
                (element_id, row["id"]),
            ).fetchone()
            if existing:
                results.append({"scene_id": row["id"], "description_id": existing["id"], "status": "skipped (existing)"})
                continue

            # Gather settings context for this scene's location
            settings_ctx = _settings_context(conn, project_id, row["location"])

            # Get previous continuity state for this element+thread
            prev_state_row = conn.execute(
                """SELECT state_json FROM continuity_state
                   WHERE element_id=? AND scene_id < ? AND story_thread_id=?
                   ORDER BY scene_id DESC LIMIT 1""",
                (element_id, row["id"], row["story_thread_id"] or 1),
            ).fetchone()
            prev_state_text = prev_state_row["state_json"] if prev_state_row else "{}"

            system = (
                "You are a scene continuity supervisor. Given a character bible, "
                "inherited settings, and the previous continuity state, describe what "
                "the character looks like in THIS scene.\n\n"
                "RULES:\n"
                "1. Use the bible for immutable traits — never contradict it.\n"
                "2. Inherited settings describe the environment — use them.\n"
                "3. Track what changed from the previous state.\n"
                "4. If nothing in the script supports a visual detail, DO NOT invent it. "
                "Use 'archetypal_default' tier and set bias_flag.\n"
                "5. The full_composite_prompt should be a single ready-to-use image "
                "generation prompt combining all visible details."
            )

            nar = f" [{row['narrative_position']}]" if row['narrative_position'] and row['narrative_position'] != 'present' else ""
            user = (
                f"CHARACTER: {el['name']}  SCENE: {row['slugline']}{nar}\n"
                f"LOCATION: {row['location'] or 'unknown'}  TIME: {row['time_of_day'] or 'unknown'}\n\n"
                f"CHARACTER BIBLE:\n{bible}\n\n"
                f"{settings_ctx}\n\n"
                f"PREVIOUS CONTINUITY STATE:\n{prev_state_text}\n\n"
                f"SCENE TEXT:\n{row['raw_body']}"
            )

            response = llm_provider.complete(
                config, provider_name, system, user,
                max_tokens=4096, json_schema=SCENE_DESCRIPTION_SCHEMA,
            )
            data = json.loads(response.text)

            is_inferred = 1 if data.get("inference_tier") == "implied" else 0
            bias_flag = 1 if data.get("inference_tier") == "archetypal_default" else 0

            cur = conn.execute(
                """INSERT INTO physical_description
                   (element_id, scene_id, bible_version_id, description_type, content,
                    is_inferred, bias_flag, version, is_current, is_approved, generation_metadata)
                   VALUES (?, ?, ?, 'scene_description', ?, ?, ?, 1, 1, 0, ?)""",
                (
                    element_id, row["id"], bible_id,
                    json.dumps(data),
                    is_inferred, bias_flag,
                    json.dumps({"provider": provider_name, "model": config.get("providers", {}).get(provider_name, {}).get("model", ""), "tokens_in": response.input_tokens, "tokens_out": response.output_tokens}),
                ),
            )

            # Update continuity state
            new_state = data.get("new_continuity_state", {})
            conn.execute(
                """INSERT INTO continuity_state
                   (element_id, scene_id, story_thread_id, state_json, changed_from_previous)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    element_id, row["id"],
                    row["story_thread_id"] or 1,
                    json.dumps(new_state),
                    json.dumps(data.get("continuity_changes", [])),
                ),
            )

            if bias_flag:
                conn.execute(
                    "INSERT INTO continuity_break (element_id, scene_id, break_type, description) VALUES (?, ?, 'inferred_pending_review', ?)",
                    (element_id, row["id"], f"Scene {row['id']}: archetypal_default inference tier — human review required."),
                )

            conn.commit()
            results.append({"scene_id": row["id"], "description_id": cur.lastrowid, "status": "rendered"})

        except Exception as exc:
            errors.append({"scene_id": row["id"], "error": str(exc)})

    return {
        "element": el["name"] if el else str(element_id),
        "scenes_rendered": len([r for r in results if r["status"] == "rendered"]),
        "scenes_skipped": len([r for r in results if "skipped" in r["status"]]),
        "scenes_errored": len(errors),
        "errors": errors,
    }
