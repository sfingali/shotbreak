"""Data model dataclasses matching the SQLite schema (DESIGN.md §2.6)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field


# ── ParsedScene (parser output, pre-database) ─────────────────────────

@dataclass
class ParsedScene:
    scene_number: str                # "1", "12A"
    scene_number_source: str = "script"  # 'script' | 'auto'
    slugline: str = ""               # "INT. MASTER BEDROOM - HOUSE - NIGHT"
    interior_exterior: str = ""      # "INT", "EXT", "INT/EXT"
    location: str = ""               # "MASTER BEDROOM"
    set_name: str = ""               # "HOUSE"
    time_of_day: str = ""            # "NIGHT", "DAY", "DAWN", "DUSK"
    body_lines: list[str] = field(default_factory=list)
    characters: list[str] = field(default_factory=list)
    page_eighths: int = 0
    has_dual_dialogue: bool = False
    is_montage: bool = False
    montage_beats: list[MontageBeat] = field(default_factory=list)
    narrative_position_hint: str | None = None
    story_date_marker: str | None = None


@dataclass
class MontageBeat:
    beat_order: int
    beat_text: str
    location_hint: str | None = None


# ── Database model dataclasses ────────────────────────────────────────

@dataclass
class Project:
    id: int = 0
    name: str = ""
    script_path: str | None = None
    script_format: str = "fountain"  # 'fountain', 'fdx', 'pdf'
    script_language: str = "en"
    output_language: str = "en"
    scene_numbers_locked: bool = False
    created_at: str = ""
    updated_at: str = ""


@dataclass
class ScriptVersion:
    id: int = 0
    project_id: int = 0
    version_num: int = 1
    raw_text: str = ""
    scene_count: int = 0
    created_at: str = ""


@dataclass
class StoryThread:
    id: int = 0
    project_id: int = 0
    label: str = ""
    is_primary: bool = False
    story_date_marker: str | None = None
    parent_thread_id: int | None = None


@dataclass
class Scene:
    id: int = 0
    project_id: int = 0
    script_version_id: int = 0
    scene_number: str = ""
    scene_number_source: str = "script"
    slugline: str = ""
    interior_exterior: str | None = None
    location: str | None = None
    set_name: str | None = None
    time_of_day: str | None = None
    page_start: float = 0.0
    page_end: float = 0.0
    page_count_eighths: int = 0
    raw_body: str = ""
    synopsis: str | None = None
    has_dual_dialogue: bool = False
    is_montage: bool = False
    narrative_position: str = "present"
    story_thread_id: int | None = None
    story_order: int | None = None
    story_date_marker: str | None = None
    breakdown_status: str = "pending"
    created_at: str = ""


@dataclass
class Element:
    id: int = 0
    project_id: int = 0
    name: str = ""
    category: str = ""               # cast, props, wardrobe, etc.
    element_type: str = "practical"  # character, prop, location, etc.
    metadata_json: str = "{}"
    created_at: str = ""


@dataclass
class SceneElement:
    id: int = 0
    scene_id: int = 0
    element_id: int = 0
    montage_beat_id: int | None = None
    context: str | None = None
    quantity: int = 1
    notes: str | None = None
    ai_confidence: float | None = None
    created_at: str = ""


@dataclass
class PhysicalDescription:
    id: int = 0
    element_id: int = 0
    scene_id: int | None = None       # None = bible entry
    bible_version_id: int | None = None
    description_type: str = ""        # 'bible','appearance','clothing',...
    content: str = ""
    prompt_variant: str | None = None
    generation_metadata: str | None = None
    is_inferred: bool = False
    bias_flag: bool = False
    version: int = 1
    superseded_by: int | None = None
    is_approved: bool = False
    created_at: str = ""


@dataclass
class ContinuityState:
    id: int = 0
    element_id: int = 0
    scene_id: int = 0
    story_thread_id: int | None = None
    state_json: str = "{}"
    changed_from_previous: str | None = None
    is_aging_checkpoint: bool = False
    created_at: str = ""


@dataclass
class CharacterRelationship:
    id: int = 0
    element_id_a: int = 0
    element_id_b: int = 0
    relationship_type: str = ""
    notes: str | None = None
    ai_confidence: float | None = None
    is_approved: bool = False


@dataclass
class ReferenceImage:
    id: int = 0
    element_id: int = 0
    file_path: str = ""
    role: str = ""                    # 'casting_reference','mood_board','locked_design'
    caption: str | None = None
    uploaded_at: str = ""


@dataclass
class BreakdownRun:
    id: int = 0
    project_id: int = 0
    passes_json: str = "[]"
    provider_overrides_json: str | None = None
    status: str = "queued"
    scenes_total: int = 0
    scenes_completed: int = 0
    current_scene_id: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error_json: str | None = None
    created_at: str = ""


@dataclass
class LLMCallLog:
    id: int = 0
    breakdown_run_id: int | None = None
    project_id: int | None = None
    pass_name: str = ""
    provider: str = ""
    model: str = ""
    scene_id: int | None = None
    element_id: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    had_reference_image: bool = False
    error: str | None = None
    created_at: str = ""


@dataclass
class ContinuityBreak:
    id: int = 0
    element_id: int = 0
    scene_id: int = 0
    break_type: str = ""              # 'contradiction','inferred_pending_review','consensus_disagreement'
    description: str = ""
    status: str = "open"
    resolution_notes: str | None = None
    created_at: str = ""
    resolved_at: str | None = None


@dataclass
class ElementAlias:
    id: int = 0
    element_id: int = 0
    raw_mention: str = ""
    match_method: str = ""           # 'exact','fuzzy','alias_table','llm'
    confidence: float | None = None
    created_at: str = ""


@dataclass
class MMSExport:
    id: int = 0
    project_id: int = 0
    format: str = ""                  # 'sex','mms10'
    file_path: str | None = None
    exported_at: str = ""


@dataclass
class TagLibrary:
    id: int = 0
    name: str = ""
    category: str = ""
    keywords: str = "[]"              # JSON array
    language: str = "en"
    preset_type: str = "canon"        # 'canon','user'
