"""The breakdown engine — Pass 1 (extract & classify) and Pass 2 (co-reference).

Pass 1 makes one LLM call per scene: tag every element present (cast, props,
wardrobe, locations, vehicles, vfx, stunts, etc.) with a category, element_type,
confidence score, in-scene context, and quantity, plus a one/two-sentence scene
synopsis — merging what DESIGN.md v1.0 ran as two separate passes into one call.

Pass 2 hands off to core/coreference.py: rapidfuzz deterministic clustering
first, LLM adjudication only for the ambiguous leftover cases.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from shotbreak.core import coreference
from shotbreak.core.llm import cost as llm_cost
from shotbreak.core.llm import provider as llm_provider

CATEGORIES = [
    "cast", "background", "stunts", "vehicles", "props", "set_dressing",
    "wardrobe", "makeup_hair", "practical_fx", "vfx", "animals", "music",
    "sound", "special_equipment", "greenery", "weapons", "locations", "notes",
]

_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "synopsis": {
            "type": "string",
            "description": "One or two sentence plain-language summary of what happens in this scene.",
        },
        "elements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The element's name exactly as it appears/is referred to in the scene text.",
                    },
                    "category": {"type": "string", "enum": CATEGORIES},
                    "element_type": {
                        "type": "string",
                        "description": "Free-form type label, e.g. 'character', 'prop', 'vehicle', 'location', 'wardrobe_item'.",
                    },
                    "confidence": {
                        "type": "number",
                        "description": (
                            "0.9+ explicitly stated in text; 0.7-0.9 strongly implied by "
                            "action/dialogue; 0.5-0.7 inferred; below 0.5 speculative."
                        ),
                    },
                    "context": {
                        "type": "string",
                        "description": "Brief note on how/why this element appears in this scene.",
                    },
                    "quantity": {"type": "integer"},
                },
                "required": ["name", "category", "element_type", "confidence", "context", "quantity"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["synopsis", "elements"],
    "additionalProperties": False,
}

_EXTRACT_SYSTEM_PROMPT = """You are an experienced assistant director doing a script breakdown pass.

For the given scene, identify every breakdown-relevant element and tag it with a category from this exact list:
{categories}

Categories:
- cast: speaking or named characters present in this scene
- background: background/extra performers (non-speaking, unnamed groups)
- stunts: stunt work, falls, fights, physical action requiring a stunt performer
- vehicles: cars, trucks, boats, aircraft — anything driven/ridden
- props: hand props, hero props, set-dressing objects a character interacts with
- set_dressing: dressing/decor items not directly handled by a character
- wardrobe: specific costume/clothing items called out or notable
- makeup_hair: special makeup, prosthetics, notable hair/hairstyle work
- practical_fx: practical effects (smoke, squibs, rain rigs, breakaway items)
- vfx: visual effects requiring post-production work
- animals: animals present or referenced as physically present
- music: source music or musical performance in-scene
- sound: notable sound design/sound effects called out in action lines
- special_equipment: cranes, rigs, underwater gear, drones, specialty camera needs
- greenery: plants, landscaping, set greenery
- weapons: firearms, blades, or other weapons
- locations: a specific named location or set worth tracking as a recurring element
- notes: anything breakdown-relevant that doesn't fit another category

Rules:
1. Tag only what is actually in THIS scene's text — do not invent elements from other scenes.
2. Use confidence tiers: 0.9+ explicitly stated; 0.7-0.9 strongly implied; 0.5-0.7 inferred; below 0.5 speculative.
3. Never invent a character's race, ethnicity, skin tone, gender presentation, disability, or body type — if not stated in text, omit that detail entirely rather than guessing.
4. Use the name exactly as it appears in the scene text (e.g. a character cue's name), so later processing can match mentions across scenes.
5. Respond with JSON only, matching the given schema."""


@dataclass
class ExtractionResult:
    synopsis: str
    elements: list[dict] = field(default_factory=list)


def extract_scene(
    config: dict,
    provider_name: str,
    slugline: str,
    raw_body: str,
    *,
    interior_exterior: str | None = None,
    time_of_day: str | None = None,
    is_montage: bool = False,
    has_dual_dialogue: bool = False,
) -> tuple[ExtractionResult, llm_provider.LLMResponse]:
    """Pass 1: one LLM call, tag every element in the scene + synopsis."""
    system = _EXTRACT_SYSTEM_PROMPT.format(categories=", ".join(CATEGORIES))

    flags = []
    if is_montage:
        flags.append("This scene is a MONTAGE — it may contain several distinct mini-beats.")
    if has_dual_dialogue:
        flags.append("This scene has dual (side-by-side) dialogue — two characters speaking simultaneously.")
    flags_text = ("\n" + "\n".join(flags)) if flags else ""

    user = (
        f"SLUGLINE: {slugline}\n"
        f"INT/EXT: {interior_exterior or 'unknown'}  TIME: {time_of_day or 'unknown'}"
        f"{flags_text}\n\n"
        f"SCENE TEXT:\n{raw_body}"
    )

    response = llm_provider.complete(
        config, provider_name, system, user, max_tokens=4096, json_schema=_EXTRACT_SCHEMA
    )
    data = json.loads(response.text)
    return ExtractionResult(synopsis=data.get("synopsis", ""), elements=data.get("elements", [])), response


def get_or_create_element(
    conn: sqlite3.Connection, project_id: int, category: str, name: str, element_type: str
) -> int:
    """Exact-name upsert within (project, category) — coreference resolves
    spelling/casing variants afterward, so this stays a literal match."""
    row = conn.execute(
        "SELECT id FROM element WHERE project_id = ? AND category = ? AND name = ?",
        (project_id, category, name),
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO element (project_id, name, category, element_type) VALUES (?, ?, ?, ?)",
        (project_id, name, category, element_type),
    )
    return cur.lastrowid


def run_extract_pass(
    conn: sqlite3.Connection,
    config: dict,
    project_id: int,
    provider_name: str,
    *,
    breakdown_run_id: int | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> dict:
    """Run Pass 1 over every scene in the project. Commits after each scene so
    a crash mid-run doesn't lose completed work. Returns a summary dict.
    """
    scenes = conn.execute(
        "SELECT id, slugline, raw_body, interior_exterior, time_of_day, is_montage, "
        "has_dual_dialogue FROM scene WHERE project_id = ? ORDER BY story_order, id",
        (project_id,),
    ).fetchall()

    summary = {"scenes_total": len(scenes), "scenes_completed": 0, "errors": []}

    for i, scene in enumerate(scenes, start=1):
        try:
            result, response = extract_scene(
                config,
                provider_name,
                scene["slugline"],
                scene["raw_body"],
                interior_exterior=scene["interior_exterior"],
                time_of_day=scene["time_of_day"],
                is_montage=bool(scene["is_montage"]),
                has_dual_dialogue=bool(scene["has_dual_dialogue"]),
            )
        except Exception as exc:  # noqa: BLE001 — record and move on to the next scene
            llm_cost.log_error(
                conn,
                project_id=project_id,
                pass_name="extract_classify",
                provider=provider_name,
                model=config.get("providers", {}).get(provider_name, {}).get("model", provider_name),
                error=str(exc),
                breakdown_run_id=breakdown_run_id,
                scene_id=scene["id"],
            )
            summary["errors"].append({"scene_id": scene["id"], "error": str(exc)})
            conn.commit()
            if on_progress:
                on_progress(i, len(scenes), scene["id"])
            continue

        conn.execute(
            "UPDATE scene SET synopsis = ?, breakdown_status = 'complete' WHERE id = ?",
            (result.synopsis, scene["id"]),
        )

        for elem in result.elements:
            element_id = get_or_create_element(
                conn, project_id, elem["category"], elem["name"], elem["element_type"]
            )
            conn.execute(
                "INSERT INTO scene_element (scene_id, element_id, context, quantity, ai_confidence) "
                "VALUES (?, ?, ?, ?, ?)",
                (scene["id"], element_id, elem.get("context"), elem.get("quantity", 1), elem.get("confidence")),
            )

        llm_cost.log_response(
            conn,
            config,
            response,
            project_id=project_id,
            pass_name="extract_classify",
            provider_name=provider_name,
            breakdown_run_id=breakdown_run_id,
            scene_id=scene["id"],
        )

        summary["scenes_completed"] += 1
        conn.commit()

        if on_progress:
            on_progress(i, len(scenes), scene["id"])

    return summary


def run_coreference_pass(
    conn: sqlite3.Connection,
    config: dict,
    project_id: int,
    provider_name: str,
    *,
    breakdown_run_id: int | None = None,
) -> dict:
    """Run Pass 2: hybrid co-reference resolution across all categories."""
    coref_cfg = config.get("coreference", {})
    result = coreference.resolve_project_coreferences(
        conn,
        config,
        project_id,
        provider_name,
        auto_merge_threshold=coref_cfg.get("fuzzy_auto_merge_threshold", 92),
        candidate_threshold=coref_cfg.get("fuzzy_candidate_threshold", 75),
    )

    for response in result["llm_calls"]:
        llm_cost.log_response(
            conn,
            config,
            response,
            project_id=project_id,
            pass_name="coreference",
            provider_name=provider_name,
            breakdown_run_id=breakdown_run_id,
        )
    conn.commit()

    return {
        "categories_processed": result["categories_processed"],
        "fuzzy_merges": result["fuzzy_merges"],
        "llm_merges": result["llm_merges"],
        "llm_calls_made": len(result["llm_calls"]),
    }
