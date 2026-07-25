I read the full v1.0 spec. Below is the complete rewritten spec addressing all nine areas: physical description engine rework, pipeline efficiency, edge cases, MMS implementation plan, CLI-first architecture, data model gaps, the trademark collision, a testing strategy, and an engine-first `core/service.py` split.

---

# Shotbreak — Design Specification v2.0

**An open-source, CLI-first AI screenplay breakdown tool with two-phase physical description generation for consistent image prompting, MMS export, and multi-LLM support.**

*(Working title — see §8.1 for the naming decision and rejected alternatives. Referred to as "Breakdown Studio" in v1.0; renamed due to a trademark collision with Breakdown Services Ltd, an existing entertainment-industry company.)*

---

## 1. Overview

### 1.1 Purpose

Scenetag reads a screenplay (Fountain, FDX, PDF) and produces a full production breakdown: every scene analyzed for cast, props, wardrobe, locations, vehicles, stunts, VFX, and more. Its killer differentiator is **physical description generation** — every tagged element gets a structured, image-generator-ready physical description designed for consistent visual output across scenes, including non-linear scripts (flashbacks, time jumps). It exports to industry-standard formats (MMS via .sex/.MMS10, PDF, CSV, FDX) and supports user-provided LLM API keys for all AI features.

**This tool has no relationship to actor casting breakdowns** (the "Breakdown Services" sense of the word, used for circulating character/casting notices to agents). Scenetag is a production/AD-side scheduling breakdown tool. This ambiguity is exactly why the name changed — see §8.1.

### 1.2 Design Goals

1. **Best-in-class physical descriptions for image generation.** Descriptions must be generator-agnostic (Midjourney, DALL-E, SD, ComfyUI, Flux), scene-aware, story-order-aware (not script-order-aware — flashbacks and time jumps must not corrupt continuity), and consistent. Where a reference image exists, it is the ground truth for likeness, not the LLM's imagination.

2. **Bias-safe by construction.** The engine must never invent a character's race, ethnicity, skin tone, gender presentation, disability, or body type when the script doesn't state it — a real risk of "AI-generated visual descriptions" tools, and a correctness bug, not just an ethics concern, since an invented attribute becomes silently load-bearing for every downstream image. See §3.4.3.

3. **Open source, user-owned.** Users bring their own LLM API keys. No vendor lock-in. No cloud dependency for AI features. MIT-licensed.

4. **Industry compatibility.** Export to Movie Magic Scheduling (.sex and .MMS10), Final Draft (.fdx), CSV, PDF.

5. **Fountain-first.** Fountain is the native format. FDX imported. PDF parsed best-effort.

6. **CLI-first, engine-first.** The breakdown engine is a library (`core/`) with a thin CLI on top. A web UI is an optional, separate consumer of the same `core.service` functions — never a second place business logic lives. See §2.

### 1.3 Non-Goals for v1

- Stripboard scheduling (v2 — needs MMS round-trip first)
- Budgeting (v2 — needs rate cards, union rules, tax credit logic)
- Call sheet generation (v2)
- Multi-user collaboration (v2)
- Storyboard generation (v2 — integrate with Shot Dash)
- Mobile app
- Real-time collaboration
- Actor casting breakdowns (out of scope entirely — see §1.1)

---

## 2. Core Architecture

### 2.1 Stack

```
Core:     Python 3.11+ (stdlib where possible, minimal deps)
CLI:      Typer (thin wrapper over core.service)
Web:      FastAPI + Jinja2 + vanilla JS — OPTIONAL, calls core.service only
Data:     SQLite (via sqlite3 stdlib, WAL mode)
LLM:      httpx (async HTTP to OpenAI-compatible + Anthropic native + vision-capable endpoints)
Fountain: bespoke parser (stdlib, no deps)
PDF:      pymupdf (optional — fallback: require FDX/Fountain)
FDX:      xml.etree.ElementTree (stdlib)
Fuzzy:    rapidfuzz (co-reference deterministic stage)
```

### 2.2 Engine-First Design

**Everything the tool can do is a function in `core/service.py`.** The CLI and the web server are both *presentation layers* over this module — neither contains business logic, database writes, or LLM orchestration directly. This was a real gap in v1.0: the API design (§6) implied routes did the work, which would have made the CLI (added in this revision) a second, drifting implementation.

```python
# core/service.py — the entire public surface of the engine
def import_script(path: str, project_name: str) -> Project: ...
def diff_script_version(project_id: int, new_text: str) -> RevisionDiff: ...
def run_breakdown(project_id: int, passes: list[str], provider_overrides: dict | None = None) -> BreakdownRun: ...
def get_run_status(run_id: int) -> BreakdownRun: ...
def generate_character_bible(project_id: int, element_id: int, reference_image: bytes | None = None) -> PhysicalDescription: ...
def generate_scene_descriptions(project_id: int, element_id: int, scene_ids: list[int] | None = None) -> list[PhysicalDescription]: ...
def resolve_coreferences(project_id: int) -> CoreferenceReport: ...
def export_mms(project_id: int, format: Literal["sex", "mms10"]) -> bytes: ...
def export_pdf(project_id: int) -> bytes: ...
def export_csv(project_id: int) -> tuple[bytes, bytes]: ...
def export_fdx(project_id: int) -> bytes: ...
def estimate_cost(project_id: int, passes: list[str]) -> CostEstimate: ...
```

Both `cli.py` and `web/server.py` import only from `core.service`. Neither touches `core/db.py`, `core/breakdown_engine.py`, or the LLM provider layer directly. This makes the web UI genuinely optional (deleting `web/` doesn't break anything) and makes the CLI the reference implementation, not an afterthought.

### 2.3 Process Model

The CLI runs breakdown passes synchronously in the foreground by default (`scenetag breakdown run` blocks and prints progress), or `--background` to detach and poll. The optional web server wraps the same `core.service.run_breakdown()` call in a background asyncio task and polls `breakdown_run` for the SPA to display progress — the FastAPI layer adds no new orchestration logic, just a queue and a status endpoint.

**SQLite concurrency note (unaddressed in v1.0):** `sqlite3` is synchronous. Running it under `asyncio` without care serializes on the GIL and can raise `SQLITE_BUSY` under concurrent CLI + web access. Fix: WAL mode enabled at connection time, a single dedicated writer connection per process (`core/db.py` enforces this), and all reads/writes from the web layer go through `asyncio.to_thread()`. If both a CLI run and a web-triggered run target the same project concurrently, the second acquires an advisory lock row in `config` and fails fast with a clear "run already in progress" error rather than corrupting state.

### 2.4 File Layout

```
scenetag/
├── core/
│   ├── __init__.py
│   ├── service.py            # THE public API — CLI and web both call only this
│   ├── models.py              # dataclasses
│   ├── db.py                  # SQLite schema, WAL setup, connection management
│   ├── fountain_parser.py
│   ├── fdx_parser.py
│   ├── pdf_parser.py
│   ├── breakdown_engine.py    # merged extract+classify pass, orchestration
│   ├── coreference.py         # hybrid deterministic (rapidfuzz/embeddings) + LLM adjudication
│   ├── description_engine.py  # two-phase physical description generation
│   ├── timeline.py            # story_thread / story_order / aging-checkpoint resolution
│   ├── mms_export.py          # .sex and .MMS10 writers
│   ├── exporters.py           # PDF, CSV, FDX
│   ├── llm/
│   │   ├── provider.py        # provider abstraction, capability flags incl. vision
│   │   └── cost.py            # token/cost accounting, writes llm_call_log
│   └── testing/
│       ├── cassettes/         # recorded LLM req/response fixtures (VCR-style)
│       └── golden_scripts/    # golden-script regression fixtures + expected breakdowns
├── cli.py                     # `scenetag` entry point (Typer)
├── web/                       # OPTIONAL — delete this dir and the tool still works
│   ├── server.py               # FastAPI routes, each one calls exactly one core.service fn
│   ├── templates/
│   └── static/
├── config.yaml.example
└── README.md
```

### 2.5 LLM Provider Model

```yaml
providers:
  openai:
    api_key: "${OPENAI_API_KEY}"
    base_url: "https://api.openai.com/v1"
    model: "gpt-4o"
    supports_vision: true
    cost_per_1k_input: 0.0025
    cost_per_1k_output: 0.01
  anthropic:
    api_key: "${ANTHROPIC_API_KEY}"
    model: "claude-sonnet-5"
    supports_vision: true
    cost_per_1k_input: 0.003
    cost_per_1k_output: 0.015
  deepseek:
    api_key: "${DEEPSEEK_API_KEY}"
    base_url: "https://api.deepseek.com/v1"
    model: "deepseek-chat"
    supports_vision: false
    cost_per_1k_input: 0.00027
    cost_per_1k_output: 0.0011
  local:
    api_key: "not-needed"
    base_url: "http://localhost:8080/v1"
    model: "llama-3.1-70b"
    supports_vision: false
    cost_per_1k_input: 0.0
    cost_per_1k_output: 0.0

default_provider: "openai"
```

All providers speak OpenAI-compatible chat completions except Anthropic (native Messages API via `httpx`). `supports_vision` gates whether reference-image conditioning (§3.4.2) is available for that provider/model — the engine refuses to silently drop an attached reference image; it errors out and tells the user to pick a vision-capable provider for that pass. `cost_per_1k_*` fields feed the cost estimator (§9 below, §5) and `llm_call_log` (§2.6).

Environment variable interpolation via `${VAR}` syntax.

### 2.6 Database Schema (SQLite)

```sql
-- Projects
CREATE TABLE project (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    script_path TEXT,
    script_format TEXT,          -- 'fountain', 'fdx', 'pdf'
    script_language TEXT DEFAULT 'en',   -- locale of the script text itself
    output_language TEXT DEFAULT 'en',   -- language breakdown labels/descriptions are generated in
    scene_numbers_locked INTEGER DEFAULT 0,  -- once true, inserted scenes get letter suffixes (12A), never renumber
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Script versions (for diff-aware re-analysis)
CREATE TABLE script_version (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES project(id),
    version_num INTEGER NOT NULL,
    raw_text TEXT NOT NULL,
    scene_count INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Story threads (present-day, flashback A, flashback B, dream sequence, ...)
CREATE TABLE story_thread (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES project(id),
    label TEXT NOT NULL,             -- 'present', 'flashback: college years', 'dream sequence 1'
    is_primary INTEGER DEFAULT 0,    -- the 'present' thread continuity resumes from
    story_date_marker TEXT,          -- free text: "1998", "three years earlier"
    parent_thread_id INTEGER REFERENCES story_thread(id)  -- what thread continuity resumes into on return
);

-- Parsed scenes
CREATE TABLE scene (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES project(id),
    script_version_id INTEGER REFERENCES script_version(id),
    scene_number TEXT NOT NULL,             -- "1", "12A", etc.
    scene_number_source TEXT DEFAULT 'script',  -- 'script' | 'auto' (see missing-scene-number handling, §3.1.5)
    slugline TEXT NOT NULL,
    interior_exterior TEXT,
    location TEXT,
    set_name TEXT,
    time_of_day TEXT,
    page_start REAL,
    page_end REAL,
    page_count_eighths INTEGER,
    raw_body TEXT NOT NULL,
    synopsis TEXT,
    has_dual_dialogue INTEGER DEFAULT 0,
    is_montage INTEGER DEFAULT 0,
    narrative_position TEXT DEFAULT 'present',  -- 'present','flashback','flashforward','dream','memory'
    story_thread_id INTEGER REFERENCES story_thread(id),
    story_order INTEGER,             -- chronological order WITHIN its thread; drives continuity walk, NOT scene_number
    story_date_marker TEXT,          -- scene-local override of thread's date, e.g. "THREE YEARS LATER" card
    breakdown_status TEXT DEFAULT 'pending',   -- 'pending','processing','complete','reviewed'
    created_at TEXT DEFAULT (datetime('now'))
);

-- Montage sub-beats (a montage scene bundles several mini-scenes under one slugline)
CREATE TABLE montage_beat (
    id INTEGER PRIMARY KEY,
    scene_id INTEGER REFERENCES scene(id),
    beat_order INTEGER NOT NULL,
    beat_text TEXT NOT NULL,
    location_hint TEXT
);

-- Elements (the core breakdown entity)
CREATE TABLE element (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES project(id),
    name TEXT NOT NULL,
    category TEXT NOT NULL,          -- cast, props, wardrobe, vehicles, stunts, vfx,
                                     -- special_equipment, animals, music, sound,
                                     -- set_dressing, makeup_hair, greenery, weapons,
                                     -- practical_fx, notes
    element_type TEXT DEFAULT 'practical',  -- 'character','prop','location','wardrobe','vehicle', etc.
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);

-- Character relationships (missing in v1.0 — needed for bible generation context
-- and for the review UI, e.g. "Ben is Jack's father", "Marie and Ben are married")
CREATE TABLE character_relationship (
    id INTEGER PRIMARY KEY,
    element_id_a INTEGER REFERENCES element(id),
    element_id_b INTEGER REFERENCES element(id),
    relationship_type TEXT NOT NULL,   -- 'parent_of','spouse_of','sibling_of','antagonist_of', free text allowed
    notes TEXT,
    ai_confidence REAL,
    is_approved INTEGER DEFAULT 0
);

-- Reference images (headshots, mood boards, locked designs — used to condition
-- description generation and override inference for stated-vs-unspecified attributes)
CREATE TABLE reference_image (
    id INTEGER PRIMARY KEY,
    element_id INTEGER REFERENCES element(id),
    file_path TEXT NOT NULL,
    role TEXT NOT NULL,     -- 'casting_reference','mood_board','locked_design'
    caption TEXT,
    uploaded_at TEXT DEFAULT (datetime('now'))
);

-- Scene-Element junction
CREATE TABLE scene_element (
    id INTEGER PRIMARY KEY,
    scene_id INTEGER REFERENCES scene(id),
    element_id INTEGER REFERENCES element(id),
    montage_beat_id INTEGER REFERENCES montage_beat(id),  -- NULL unless tagged to a specific montage beat
    context TEXT,
    quantity INTEGER DEFAULT 1,
    notes TEXT,
    ai_confidence REAL,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Physical descriptions (THE HARD PROBLEM) — now versioned, and split bible/scene
CREATE TABLE physical_description (
    id INTEGER PRIMARY KEY,
    element_id INTEGER REFERENCES element(id),
    scene_id INTEGER,                -- NULL = bible entry; set = scene-specific delta
    bible_version_id INTEGER REFERENCES physical_description(id),  -- which bible version this scene description was generated against
    description_type TEXT NOT NULL,  -- 'bible','appearance','clothing','props_on_person',
                                     -- 'tattoos','hair_makeup','movement_style',
                                     -- 'lighting_context','environment_context','full_composite'
    content TEXT NOT NULL,
    prompt_variant TEXT,             -- 'midjourney','dalle3','sdxl','flux','generic'
    generation_metadata TEXT,        -- provider, model, prompt hash, reference_image_id used (if any), timestamp
    is_inferred INTEGER DEFAULT 0,   -- true if not explicitly stated in script text
    bias_flag INTEGER DEFAULT 0,     -- true if the lint pass (§3.4.3) detected an unstated protected attribute — forces human review
    version INTEGER DEFAULT 1,
    superseded_by INTEGER REFERENCES physical_description(id),
    is_approved INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Continuity tracking — walks story_order within a story_thread, not scene_number
CREATE TABLE continuity_state (
    id INTEGER PRIMARY KEY,
    element_id INTEGER REFERENCES element(id),
    scene_id INTEGER REFERENCES scene(id),
    story_thread_id INTEGER REFERENCES story_thread(id),
    state_json TEXT NOT NULL,
    changed_from_previous TEXT,
    is_aging_checkpoint INTEGER DEFAULT 0,  -- true if this state change was gated as a bible-level aging update (§3.4.4)
    created_at TEXT DEFAULT (datetime('now'))
);

-- Tag library presets
CREATE TABLE tag_library (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    keywords TEXT NOT NULL,
    language TEXT DEFAULT 'en',
    preset_type TEXT DEFAULT 'canon'   -- 'canon','user'
);

-- Breakdown runs — referenced throughout the v1.0 API (§5.2 "GET .../breakdown/status")
-- but never actually defined in v1.0's schema. Fixed here.
CREATE TABLE breakdown_run (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES project(id),
    passes_json TEXT NOT NULL,           -- e.g. ["extract","coreference","bible","scene_descriptions"]
    provider_overrides_json TEXT,
    status TEXT DEFAULT 'queued',        -- 'queued','running','complete','error','cancelled'
    scenes_total INTEGER,
    scenes_completed INTEGER DEFAULT 0,
    current_scene_id INTEGER,
    started_at TEXT,
    finished_at TEXT,
    error_json TEXT,                     -- [{scene_id, error}] for partial failures
    created_at TEXT DEFAULT (datetime('now'))
);

-- LLM cost/usage tracking (missing in v1.0 despite cost estimates being promised in the UI)
CREATE TABLE llm_call_log (
    id INTEGER PRIMARY KEY,
    breakdown_run_id INTEGER REFERENCES breakdown_run(id),
    project_id INTEGER REFERENCES project(id),
    pass_name TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    scene_id INTEGER,
    element_id INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    latency_ms INTEGER,
    had_reference_image INTEGER DEFAULT 0,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Human eval samples (testing strategy, §9.3)
CREATE TABLE human_eval_sample (
    id INTEGER PRIMARY KEY,
    physical_description_id INTEGER REFERENCES physical_description(id),
    reviewer TEXT,
    verdict TEXT,             -- 'approve','reject','edited'
    edited_content TEXT,
    edit_distance REAL,
    notes TEXT,
    reviewed_at TEXT DEFAULT (datetime('now'))
);

-- MMS export tracking
CREATE TABLE mms_export (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES project(id),
    format TEXT NOT NULL,     -- 'sex','mms10'
    file_path TEXT,
    exported_at TEXT DEFAULT (datetime('now'))
);

-- Config / settings
CREATE TABLE config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

---

## 3. Core Features — Detailed Design

### 3.1 Script Import & Parsing

#### 3.1.1 Fountain Parser (`fountain_parser.py`)

Handles title page metadata, scene detection (`INT.`/`EXT.`/`INT./EXT.` with optional `#n#` scene numbers), character cues, dialogue, parentheticals, transitions, centered text, bold/italic, notes `[[...]]`, boneyard `/* ... */`, page breaks `===`, sections/synopses.

```python
@dataclass
class ParsedScene:
    scene_number: str
    scene_number_source: str      # 'script' | 'auto'
    slugline: str
    interior_exterior: str
    location: str
    set_name: str
    time_of_day: str
    body_lines: list[str]
    characters: list[str]
    page_eighths: int
    has_dual_dialogue: bool
    is_montage: bool
    narrative_position_hint: str | None   # deterministic guess from slugline/action text
    story_date_marker: str | None
```

#### 3.1.2 Dual Dialogue

Fountain marks the second column with `^` immediately after the character cue; FDX wraps both in `<DualDialogue>`. The parser must attribute each dialogue block to its correct character and must **not** double-count page eighths for the side-by-side block (a common bug: naive line-counting treats two parallel columns as sequential text and inflates page count). `scene.has_dual_dialogue` is set so the element-extraction prompt (§3.3) knows to expect interleaved speaker turns rather than a single linear conversation.

#### 3.1.3 Montages

A montage slugline (`INT./EXT. VARIOUS - MONTAGE`, or a scene explicitly containing `MONTAGE:` with dashed sub-beats) is parsed into one `scene` row plus N `montage_beat` rows — each beat is the text span between dash/bullet markers, with a best-effort `location_hint` pulled from any embedded mini-slugline. `scene_element` tagging attaches to the specific beat where possible (`montage_beat_id` set) so a costume that appears in beat 3 of a five-beat montage isn't reported as present for the whole montage's page range.

#### 3.1.4 Flashbacks / Non-Linear Structure

Deterministic detection first: regex against slugline and immediately-following action text for markers (`FLASHBACK`, `FLASHBACK TO:`, `X YEARS EARLIER`, `X YEARS LATER`, `INTERCUT WITH`, `DREAM SEQUENCE`, `— PRESENT DAY —`). These populate `narrative_position_hint` and `story_date_marker` at parse time, with no LLM call. Ambiguous cases (a scene that reads like a memory but has no explicit marker) are left `None` and resolved during Pass 1 (§3.3) by the LLM, with a human-override control in the CLI/UI (`scenetag scene set-thread --scene 45 --thread flashback-college`). See §3.4.4 for how this drives continuity.

#### 3.1.5 Missing Scene Numbers

Unnumbered Fountain scripts are auto-numbered sequentially at import (`scene_number_source = 'auto'`). Once a project's scene numbers are locked for production (`project.scene_numbers_locked = 1`, typically after the first MMS export — locking numbers is standard AD practice), any scene inserted between two existing scenes on a later revision gets a lettered suffix (`12A`) rather than triggering a renumber, matching industry convention and avoiding invalidating an already-circulated MMS schedule.

#### 3.1.6 Multi-Language Scripts

`project.script_language` drives which locale's slugline keyword table the parser uses (`INT./EXT.` equivalents, day/night terms, transition words) — these are config-driven regex tables per language, not hardcoded English strings as in v1.0. `project.output_language` is independent: a French-language script can still produce English-language breakdown labels and physical descriptions for an English-speaking crew (or vice versa). LLM prompts in every pass include an explicit `Respond in {output_language}.` instruction, and the tag library (§7) carries per-language keyword sets.

#### 3.1.7 FDX Parser (`fdx_parser.py`)

`xml.etree.ElementTree` over `<Paragraph Type="Scene Heading">`, `<Paragraph Type="Character">`, `<Paragraph Type="Dialogue">`, `<DualDialogue>`, `SceneProperties` (exact page lengths — more accurate than Fountain's estimate), `TagData` (pre-tagged elements, imported directly).

#### 3.1.8 PDF Parser (`pdf_parser.py`)

Optional, via `pymupdf`. Best-effort heuristic reconstruction; users are warned PDF is lossy and FDX/Fountain is strongly preferred.

### 3.2 Script Revision Handling

Unchanged from v1.0's approach, with one addition: revision diffing must also re-run `timeline.py`'s thread/story-order resolution when scenes are inserted or deleted, since `story_order` is a project-wide sequence, not per-scene metadata — deleting a flashback scene can shift every subsequent scene's continuity anchor within that thread, not just the deleted scene's neighbors.

1. Parse new version
2. Match against previous version by scene number + fuzzy text
3. Classify: unchanged (skip re-breakdown) / modified (re-tag) / new (full breakdown) / deleted (archive)
4. Preserve manual edits and approved descriptions on unchanged scenes
5. Recompute `story_order` for affected threads
6. Flag continuity implications for human review

### 3.3 The Breakdown Engine (`breakdown_engine.py`)

#### 3.3.1 Pipeline (Efficiency Rework)

v1.0 ran five LLM passes, one of which (scene detection/metadata) re-derives information the deterministic parser already produces exactly — that pass is removed entirely. Co-reference resolution ran as a single LLM call over the full element list, which doesn't scale and burns tokens on unambiguous cases a string-match would resolve for free. Revised pipeline:

```
PASS 0: PARSING (deterministic, no LLM)
  Slugline, INT/EXT, location, set, time, page count, character cues, dual-dialogue
  flags, montage beats, narrative-position hints — all from fountain_parser/fdx_parser.
  This replaces v1.0's "Pass 1: Scene Detection & Metadata" LLM call outright.

PASS 1: EXTRACT & CLASSIFY (one LLM call per scene — merges v1.0's Pass 2 + Pass 4)
  Input:  Scene text + Pass 0 metadata
  Output: Tagged elements per category, WITH element_type classification and
          confidence scores in the same call, plus scene synopsis.
  Model:  Capable tier (GPT-4o, Claude Sonnet, DeepSeek V4)

PASS 2: CO-REFERENCE RESOLUTION (hybrid — replaces v1.0's all-LLM Pass 3)
  Stage A (deterministic, no LLM): normalize case/punctuation, exact-match dedup,
    rapidfuzz token-sort-ratio clustering — auto-merge above a high threshold (e.g. 92).
  Stage B (embeddings, cheap/local): candidate clusters between medium thresholds
    (e.g. 75-92) get an embedding similarity pass to narrow ambiguous groups further.
  Stage C (LLM, only for what's left): remaining ambiguous clusters — typically a small
    fraction of the full element list — sent to the LLM for adjudication with full
    scene context. In practice this cuts co-reference LLM token spend by 80-90% versus
    v1.0's every-element approach.
  Model:  Capable, but on a small input

PASS 3: PHYSICAL DESCRIPTIONS — two-phase, see §3.4
```

Users still choose which passes to run and which model per pass; the cost estimator (§5.2, §9) now reflects the hybrid co-reference savings.

#### 3.3.2 Element Categories

Unchanged from v1.0 (cast, background, stunts, vehicles, props, set_dressing, wardrobe, makeup_hair, practical_fx, vfx, animals, music, sound, special_equipment, greenery, weapons, vehicles_picture, notes).

#### 3.3.3 Confidence Scoring

Unchanged banding (0.9+ explicit, 0.7-0.9 strongly implied, 0.5-0.7 inferred, <0.5 speculative/flagged), now also feeding `is_inferred` on `physical_description` rows (§3.4.3).

### 3.4 Physical Description Engine (`description_engine.py`)

This is the most substantially reworked section of the spec. v1.0 generated a fresh "appearance" description independently for every scene, with no anchor — nothing prevented the LLM from silently drifting a character's face or build across 278 separate calls, and nothing prevented it from inventing unstated demographic attributes. Both are fixed by moving to a two-phase design.

#### 3.4.1 Two-Phase Design: Character Bible, Then Per-Scene

**Phase A — Character Bible (once per recurring element, after Passes 1-2 complete).**
The LLM receives every scene excerpt in which the character appears, ordered by `story_order` within the primary story thread (not script order — see §3.4.4), plus any `reference_image` rows for that element, and produces one canonical global record: build, face, hair (color/texture/length), distinguishing marks, established age bracket — attributes that should not silently change scene to scene. Stored as `physical_description` with `scene_id = NULL`, `description_type = 'bible'`, `version = 1`.

**Phase B — Per-Scene Deltas (after Phase A, walked in `story_order`).**
For each scene, the LLM receives: the bible (fixed anchor, quoted verbatim, not regenerated), the `continuity_state` inherited from the *previous scene in the same story thread*, the scene text, and optionally the reference image again for hero characters. Output is a **delta only** — clothing, props on person, hair/makeup state changes, lighting context, environment context, emotional state — plus the new `continuity_state`. `bible_version_id` on the row pins exactly which bible version it was generated against, so a later bible revision (§3.4.4) doesn't silently invalidate scene descriptions without a visible link.

This is both cheaper (smaller per-scene prompts — fixed attributes aren't re-derived 278 times) and more correct (there is one place, not 278, where "what does Ben look like" is decided).

#### 3.4.2 Reference Image Conditioning

`reference_image` (§2.6) stores casting references, mood boards, or locked character designs, tied to an element and tagged by `role`. When a vision-capable provider is configured (`supports_vision: true`, §2.5) and a reference image exists for the element, both Phase A and Phase B attach it with an explicit instruction:

> "Use the attached reference image as ground truth for facial features, skin tone, hair, and build. The script text governs clothing, props, and scene action only — do not let the image override wardrobe or action described in the text."

The image ID used is recorded in `generation_metadata` for every description it influenced, so a later swap of the reference image (e.g., after casting is confirmed) can be traced to exactly which descriptions need regeneration.

#### 3.4.3 First-Appearance / Bias Gate

Every Phase A and Phase B prompt enforces a strict three-tier rule, and it is the load-bearing fix for a real correctness/ethics gap in v1.0 (which had no guardrail against the LLM inventing physical details on first appearance):

1. **STATED** — explicitly in the script text → include normally.
2. **IMPLIED** — strongly implied by action or dialogue (e.g., "he winces, favoring his left leg" → limp) → include, `is_inferred = 1`, confidence tier attached.
3. **UNSPECIFIED PROTECTED ATTRIBUTES** — race, ethnicity, skin tone, gender identity/presentation, disability, body type/weight — **never invented.** If not stated in text and no `reference_image` exists, the field is left `"unspecified"`, not defaulted to an assumption.

A post-generation lint pass (regex/keyword match against a demographic-terms list, run in `description_engine.py` before the row is written) checks every description for terms in tier 3 that aren't traceable to either the source scene text or an attached reference image. Any hit sets `bias_flag = 1` and blocks `is_approved` from being set programmatically — it routes to mandatory human review in the description editor (§4.2) or `scenetag review` CLI queue, regardless of the model's own confidence score. A reference image, when present, is the authorized override for tier 3 (§3.4.2) — its use is what lets a hero character get a fully specified bible without inventing anything.

#### 3.4.4 Story-Order Continuity, Flashbacks, and Aging

**Continuity walks `story_order` within a `story_thread`, not `scene_number`.** A flashback scene showing a character 20 years younger is not "the next state after" whatever the present-day scene before it in script order happened to show — it belongs to its own thread with its own continuity chain, seeded fresh (or from an earlier bible version if one exists for that period) rather than inheriting the present-day chain. When a thread with `parent_thread_id` set returns to its parent (e.g., the story cuts back to "present"), the engine resumes the parent thread's *own* last state — it does not carry forward anything from the flashback.

**Aging checkpoints:** `story_thread.story_date_marker` / `scene.story_date_marker` (populated deterministically at parse time from cues like "THREE YEARS LATER," §3.1.4) are compared against the character's last appearance in the same thread. If the elapsed interval exceeds a configurable threshold (default: >1 year for adults, >90 days for characters flagged as children under 12 in the bible), the engine raises an **aging checkpoint** rather than silently carrying the old bible forward: Phase B explicitly asks whether aging-relevant attributes (hairstyle, added scars, gray hair, a child's growth) should change. Because aging is a bible-level fact, not a scene-local costume change, an accepted aging checkpoint writes a **new bible version** (`version += 1`, `superseded_by` chains the old row) rather than mutating scene deltas — and requires human approval before it takes effect, since this is exactly the kind of change worth a human's eyes.

#### 3.4.5 Description Schema

```python
@dataclass
class ElementDescription:
    element_id: int
    scene_id: int | None            # None = bible entry
    bible_version_id: int | None    # which bible version this delta was generated against

    appearance: str | None          # bible-level only
    clothing: str | None
    props_on_person: str | None
    hair_makeup: str | None
    tattoos_scars: str | None       # bible-level; scene deltas note only visibility changes
    movement_style: str | None      # bible-level

    lighting_context: str | None
    environment_context: str | None
    emotional_state: str | None

    is_inferred: bool
    bias_flag: bool

    prompt_variants: dict[str, str]
    full_composite: str | None
```

#### 3.4.6 Multi-Model Consensus (Optional)

Unchanged from v1.0: for hero elements, run Phase A/B through multiple providers and diff outputs; disagreements surface for review. Now also useful as a bias-gate cross-check — a tier-3 attribute one model infers and another omits is a strong signal to flag regardless of the lint pass.

#### 3.4.7 Prompt Template (Phase B, revised)

```
You are a production designer and continuity supervisor.

CHARACTER BIBLE (fixed — do not contradict, do not re-derive):
{bible_content}

REFERENCE IMAGE: {attached if present; instruct model per §3.4.2}

CONTINUITY STATE INHERITED FROM PREVIOUS SCENE IN THIS STORY THREAD ({thread_label}):
{continuity_state}

CURRENT SCENE ({narrative_position}, story_order={story_order}):
{scene_text}

RULES:
1. Only describe what changed from the inherited state, plus this scene's context
   (lighting, environment, emotional state).
2. STATED / IMPLIED / UNSPECIFIED tiering is mandatory — see bias gate (§3.4.3).
   Never fill an UNSPECIFIED protected attribute; write "unspecified".
3. If this scene is flagged as an aging checkpoint, explicitly address whether
   aging-relevant attributes should change; do not silently carry the bible forward.
4. Output JSON matching the ElementDescription delta schema.
```

---

### 3.5 MMS Export (`mms_export.py`)

MMS export is treated as an under-documented legacy format problem, not a spec-lookup problem — v1.0 underestimated this. Concrete phased plan:

**Phase 1 — `.sex` (text-based Scheduling Export)**
1. Acquire real sample `.sex` files (Movie Magic Scheduling trial/demo export, or community-shared samples referenced from `ggvfx/film-breakdown-assistant`).
2. **Differential reverse engineering**: export the same minimal script twice, changing exactly one field between exports (e.g., one extra prop tag), and diff the byte output to isolate field boundaries and delimiters. Repeat per category/field until the record structure is pinned down, not guessed from partial documentation.
3. Build the writer against the confirmed structure; keep an internal "confirmed vs. inferred" map per field.
4. **Round-trip fixture tests**: `tests/fixtures/mms/*.sex` (redacted real samples) checked byte-for-byte against writer output for the same input project.
5. **Manual QA gate**: an actual import into Movie Magic Scheduling (or its demo) is required before the writer is marked "supported" rather than "experimental." A `--strict-mms` CLI flag refuses to emit fields that are still in the "inferred" map, forcing best-effort mode to be opt-in and visible.

**Phase 2 — `.MMS10` (Screenwriter XML, `.mmx`)**
Same differential approach, easier because XML diffs cleanly. Acquire a Screenwriter trial export, diff two controlled exports to confirm schema/namespace, document the confirmed subset internally, gate behind the same "confirmed vs. inferred" + `--strict-mms` mechanism, and require the same manual round-trip QA gate before calling it supported.

Both formats ship marked **experimental** in the README until a maintainer with an actual MMS license confirms end-to-end import; this is a correction to v1.0, which implied both formats were a documentation lookup away from being solid.

### 3.6 Other Exports (`exporters.py`)

Unchanged from v1.0: PDF breakdown sheets (WeasyPrint/ReportLab), scenes/elements CSV, tagged FDX.

---

## 4. CLI (Primary Interface)

```
scenetag import script.fountain --project "The Waif"
scenetag revise script_v2.fountain --project 1
scenetag breakdown run --project 1 --passes extract,coreference,bible,scene_descriptions
scenetag breakdown status --run 7
scenetag breakdown cancel --run 7
scenetag scene set-thread --scene 45 --thread flashback-college     # human override, §3.1.4
scenetag bible generate --project 1 --element Ben --reference-image ben_headshot.jpg
scenetag bible approve --description-id 12
scenetag review sample --project 1 --n 20      # human eval loop, §9.3
scenetag export mms --project 1 --format sex -o the_waif.sex
scenetag export mms --project 1 --format mms10 --strict-mms -o the_waif.MMS10
scenetag export pdf --project 1
scenetag cost estimate --project 1 --passes bible,scene_descriptions
scenetag serve                                  # launches optional web UI
scenetag test cassette                           # §9.1
scenetag test golden                             # §9.2
```

Every command is a direct call into `core.service` (§2.2) — the CLI has no logic of its own beyond argument parsing and output formatting.

---

## 5. Web UI (Optional)

### 5.1 Philosophy

Single-page app, vanilla JS, no build step, server-rendered HTML with embedded JSON. **Every route in `web/server.py` calls exactly one `core.service` function.** If a feature needs new logic, that logic goes in `core/`, never in a route handler — this is what makes the web UI genuinely optional rather than the thing that happens to also expose a CLI.

### 5.2 Views

Largely unchanged from v1.0's mockups (project dashboard, breakdown grid, element detail, description editor), with two additions:

- **Description editor** now shows the bible/scene-delta split explicitly (bible attributes are read-only in the scene view, with a "this is inherited — edit the bible to change it" affordance), the reference image if attached, and a highlighted bias-gate warning banner when `bias_flag = 1`, blocking the approve button until a reviewer explicitly overrides it.
- **Timeline view** (new): a per-project view of `story_thread`s with scenes plotted by `story_order`, so a human can sanity-check flashback/present-day continuity boundaries and correct `narrative_position` misclassifications before running Phase A/B.

### 5.3 AI Run Interface

Same as v1.0's mockup, with cost estimate now sourced from `llm_call_log` history + `cost_per_1k_*` config rather than a hardcoded guess, and pass list reflecting the merged pipeline (§3.3.1: Extract & Classify, Co-reference [hybrid], Bible, Scene Descriptions).

---

## 6. API Design (Web UI Only — Thin Wrapper)

Each route below is a 1:1 call into the matching `core.service` function (§2.2); this section exists to document the HTTP surface, not new behavior.

```
GET  /api/projects
POST /api/projects
GET  /api/projects/{id}
DELETE /api/projects/{id}

POST /api/projects/{id}/script
GET  /api/projects/{id}/script/versions

GET  /api/projects/{id}/scenes
GET  /api/projects/{id}/scenes/{scene_id}
PUT  /api/projects/{id}/scenes/{scene_id}
PUT  /api/projects/{id}/scenes/{scene_id}/thread      # narrative_position / story_thread override

GET  /api/projects/{id}/elements
GET  /api/projects/{id}/elements/{elem_id}
PUT  /api/projects/{id}/elements/{elem_id}
POST /api/projects/{id}/elements

GET  /api/projects/{id}/relationships                  # character_relationship
POST /api/projects/{id}/relationships

POST /api/projects/{id}/elements/{elem_id}/reference-images
GET  /api/projects/{id}/elements/{elem_id}/reference-images

POST /api/projects/{id}/elements/{elem_id}/bible        # Phase A
POST /api/projects/{id}/elements/{elem_id}/descriptions # Phase B
PUT  /api/projects/{id}/descriptions/{desc_id}
POST /api/projects/{id}/descriptions/{desc_id}/approve  # blocked if bias_flag unresolved

POST /api/projects/{id}/breakdown/run
GET  /api/projects/{id}/breakdown/status?run_id=
POST /api/projects/{id}/breakdown/cancel

GET  /api/projects/{id}/continuity
GET  /api/projects/{id}/continuity/{elem_id}
GET  /api/projects/{id}/timeline                        # story_thread + scenes by story_order

GET  /api/projects/{id}/cost                            # llm_call_log rollup
POST /api/projects/{id}/cost/estimate

POST /api/projects/{id}/export/mms
POST /api/projects/{id}/export/pdf
POST /api/projects/{id}/export/csv
POST /api/projects/{id}/export/fdx

GET  /api/config
PUT  /api/config
POST /api/config/test

GET  /api/tag-library
POST /api/tag-library

GET  /api/projects/{id}/review/sample                    # human eval queue, §9.3
POST /api/review/{sample_id}
```

---

## 7. Tag Library System

Unchanged core design from v1.0 (feature film, TV drama, limited series, indie short, commercial, documentary presets), with `tag_library.language` added so keyword sets are per-locale (§3.1.6) rather than English-only.

---

## 8. Naming & Open Source Plan

### 8.1 Name Collision

**"Breakdown Studio" collides with Breakdown Services Ltd**, an established entertainment-industry company (operators of Breakdown Express) that distributes casting breakdowns to talent agencies — a different meaning of "breakdown" (the casting sense) but the same industry vertical, the same target users (production companies, ADs), and close enough branding that confusion is likely, plus real trademark risk given they operate in the exact space this tool's users work in.

**Alternatives considered:**

| Name | Notes |
|---|---|
| **Scenetag** *(recommended)* | Describes what the tool does (tags elements per scene), no existing collision found in the screenwriting/production-software space, works as both product name and CLI verb-object. |
| Fountainhead | Nice tie to the Fountain format; risks confusion with the Ayn Rand novel and unrelated existing software of the same name. |
| Slugline Studio | Direct collision with Slugline, an existing screenwriting app. Rejected outright. |
| Elementale | Unique, evokes "elements," but doesn't read clearly as a breakdown tool. |
| Covera | Too close to "coverage," a distinct existing term (script coverage = analyst notes on a spec script) — would cause a different confusion. |

Recommendation: **Scenetag**, used throughout this document. Repo, package, and CLI binary all use this name. This is a naming decision, not a legal opinion — a trademark search/clearance is still worth a real check before public launch, but Scenetag doesn't share the obvious-collision problem "Breakdown Studio" does.

### 8.2 License

MIT License, unchanged rationale from v1.0.

### 8.3 Repository

Codeberg preferred, mirrored to GitHub for discoverability. Repo name: `scenetag`.

### 8.4 Community

CONTRIBUTING.md, issue templates, a discussion area for tag-library and cassette-fixture sharing, example configs per LLM provider.

---

## 9. Testing Strategy

v1.0 had no testing section at all despite being an LLM-dependent application where correctness is genuinely hard to pin down — this is the biggest structural gap in the original spec, fixed here.

### 9.1 Cassette Tests

Record real LLM request/response pairs once (VCR-style, via a custom `httpx` transport recorder or `vcrpy`) into `core/testing/cassettes/*.yaml`, with API keys redacted before commit. CI replays these with no network access and no per-run API cost. Cassette tests validate **plumbing**: prompt formatting, JSON-schema parsing of responses, retry/error handling, cost-log accounting — not output quality, since a cassette is frozen. Every pass (extract/classify, co-reference LLM stage, bible, scene descriptions) gets cassette coverage for both success and malformed-response paths.

### 9.2 Golden-Script Regression

A small set (3-5) of real or purpose-authored screenplays, checked into `core/testing/golden_scripts/`, each with a hand-verified expected breakdown — element lists, categories, and confidence bands (not exact prose, since LLM output isn't byte-stable). Regression runs assert element recall/precision against the golden set within tolerance, required-category coverage, and schema validity on every generated description. Run twice: against replayed cassettes on every CI run (catches code regressions), and against live models on a slower cadence (nightly/weekly) to catch **model drift** — a provider silently changing model behavior underneath a pinned model name, which cassette-only testing can't detect.

### 9.3 Human Eval Loop

Automated tests can't judge whether a physical description is actually good, or whether the bias gate (§3.4.3) is being applied sensibly rather than mechanically. `scenetag review sample --project 1 --n 20` pulls a weighted sample (oversampling first-appearances and anything with `bias_flag = 1`) into a review queue; reviewers approve, reject, or edit. Results land in `human_eval_sample`, tracked over time as approval rate and edit-distance-from-original — the actual quality metric for this application, distinct from the plumbing-correctness cassette tests and the recall/precision golden-script tests.

---

## 10. Configuration & Environment

### 10.1 Config File (`config.yaml`)

```yaml
host: "127.0.0.1"
port: 8190
data_dir: "./data"

providers:
  openai:
    api_key: "${OPENAI_API_KEY}"
    model: "gpt-4o"
    supports_vision: true
    cost_per_1k_input: 0.0025
    cost_per_1k_output: 0.01
  anthropic:
    api_key: "${ANTHROPIC_API_KEY}"
    model: "claude-sonnet-5"
    supports_vision: true
    cost_per_1k_input: 0.003
    cost_per_1k_output: 0.015
  deepseek:
    api_key: "${DEEPSEEK_API_KEY}"
    model: "deepseek-chat"
    supports_vision: false
    cost_per_1k_input: 0.00027
    cost_per_1k_output: 0.0011

default_provider: "openai"

breakdown:
  parallelism: 3
  confidence_threshold: 0.5
  max_retries: 3
  timeout_seconds: 120

pass_models:
  extract_classify: "openai"
  coreference_llm_stage: "deepseek"     # small input, cheap model is fine
  bible: "anthropic"                    # needs vision for reference images
  scene_descriptions: "anthropic"

descriptions:
  generate_prompt_variants: true
  variants: ["midjourney", "dalle3", "flux", "sdxl"]
  auto_approve_above: 0.9
  bias_gate_blocks_auto_approve: true    # non-negotiable default; document how to override, don't silently allow it

timeline:
  adult_aging_threshold_years: 1
  child_aging_threshold_days: 90

mms:
  default_format: "sex"
  strict_mode: false   # --strict-mms CLI flag maps here

language:
  script_language: "en"
  output_language: "en"
```

### 10.2 `.env`

```
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
DEEPSEEK_API_KEY=sk-...
```

---

## 11. Implementation Phases

Testing infrastructure moved earlier (Phase 1) since cassette fixtures need to exist before pipeline work can be tested in CI at all — v1.0 put testing last, after everything else was already built untested.

**Phase 1: Foundation + Test Harness (Week 1-2)**
`core/db.py` schema (full, including `breakdown_run`, timeline, reference images, cost log from day one — not bolted on later), Fountain/FDX parsers with dual-dialogue/montage/missing-scene-number/multi-language handling, cassette-recording harness, `cli.py` skeleton calling `core.service` stubs.

**Phase 2: Core Breakdown (Week 2-4)**
LLM provider abstraction (incl. vision capability flags, cost logging), merged Extract & Classify pass, hybrid co-reference (deterministic + embeddings + LLM adjudication), golden-script fixtures for this stage.

**Phase 3: Descriptions (Week 4-6)**
Two-phase description engine (bible then scene deltas), reference-image conditioning, bias/first-appearance gate + lint pass, timeline/story-order/aging-checkpoint logic, versioning.

**Phase 4: Exports (Week 5-7)**
`.sex` via differential reverse engineering + fixture round-trip tests, `.MMS10` same approach, PDF/CSV/FDX exports.

**Phase 5: Web UI + Polish (Week 7-9)**
`web/server.py` as a thin wrapper (built last, deliberately, to force the core/service boundary to actually hold), timeline view, description editor bias-flag UI, tag library presets, human eval loop tooling, documentation.

---

## 12. Risks and Open Questions

1. **MMS10/`.sex` format confirmation** — mitigated by the differential-reverse-engineering plan (§3.5) and the explicit experimental/strict-mode split, but still gated on getting real sample files and a manual round-trip QA pass before either format can be called "supported."
2. **Description generation cost/quality at scale** — mitigated by the two-phase bible/delta design (smaller per-scene prompts) and hybrid co-reference, but a full breakdown with bible + scene deltas for a 278-scene script with 5+ recurring characters is still a real per-project cost; `llm_call_log` and the cost estimator exist specifically to keep this visible before a run, not after.
3. **Bias-gate false negatives** — the lint pass is keyword/regex based and will miss paraphrased or indirect demographic inference; the human eval loop (§9.3) oversamples first-appearances specifically to catch what the lint pass doesn't.
4. **Reference image licensing/consent** — uploaded casting references or actor headshots may carry usage restrictions the tool has no way to verify; document clearly in the UI that reference image upload is the user's responsibility, not the tool's to police.
5. **Story-order/thread inference reliability** — deterministic detection (§3.1.4) handles explicit markers well but will miss unmarked flashbacks/memories; the timeline view (§5.2) and CLI override exist because this is expected to need human correction, not because the heuristic is expected to be complete.
6. **PDF parsing reliability** — unchanged from v1.0: best-effort only, FDX/Fountain strongly recommended.
7. **Fountain spec edge cases** — unchanged concern; per-project parser settings and error reporting for unparseable constructs.
8. **Model drift** — a provider changing a named model's behavior underneath a pinned model string could silently degrade description quality; this is why golden-script regression runs against live models on a schedule, not just against cassettes (§9.2).

---

## 13. Appendix: Physical Description Examples

### 13.1 Character Bible (Phase A) — BEN

```json
{
  "character": "Ben",
  "bible_version": 1,
  "reference_image_id": 4,
  "bible": {
    "appearance": "Tall, lean build. Angular face with strong jaw. Dark eyes. Short black hair, slightly disheveled. Late 30s.",
    "tattoos_scars": "Full-sleeve tattoos on both arms — geometric and tribal patterns in faded black ink. Hidden under long sleeves in most scenes; a visual reveal element.",
    "movement_style": "Cautious, deliberate. Tactical training evident in how he clears rooms. Protective stance around family.",
    "build": "Lean but functional — not gym-built.",
    "race_ethnicity_skin_tone": "unspecified — not stated in script text; reference_image_id 4 supplied as ground truth for rendering, see generation_metadata"
  },
  "generation_metadata": {
    "provider": "anthropic", "model": "claude-sonnet-5",
    "reference_image_id": 4, "bias_lint": "pass — no unstated tier-3 terms present"
  }
}
```

### 13.2 Scene Delta (Phase B) — BEN, Scene 2 (story_order within "present" thread)

```json
{
  "element": "Ben",
  "scene_id": 2,
  "bible_version_id": 1,
  "story_thread": "present",
  "story_order": 2,
  "clothing": "Same long-sleeve dark shirt and jeans as scene 1. Added work boots.",
  "props_on_person": "Baseball bat in right hand. Flashlight.",
  "lighting_context": "Dark corridor, flashlight beam cutting through darkness.",
  "emotional_state": "Tactical, controlled but on edge.",
  "continuity_changes": ["Added boots", "Picked up baseball bat", "Picked up flashlight"],
  "is_aging_checkpoint": false,
  "bias_flag": false
}
```

### 13.3 Flashback Thread Example — BEN, "Flashback: Service Years" thread

```json
{
  "element": "Ben",
  "story_thread": "flashback: service years",
  "parent_thread": "present",
  "story_date_marker": "roughly 12 years earlier",
  "bible_delta_note": "This thread does NOT inherit continuity_state from the present-day thread. A separate bible check is triggered: is a 12-year-earlier appearance materially different (shorter hair, no visible tattoos yet, younger face)? Flagged as an aging checkpoint requiring human approval before generating this thread's own bible variant, rather than reusing bible_version 1 unmodified.",
  "requires_human_review": true
}
```

### 13.4 Element Description — BASEBALL BAT (unchanged structure from v1.0, still correct)

```json
{
  "element": "Baseball Bat",
  "category": "props",
  "element_type": "hero_prop",
  "global_description": {
    "appearance": "Wooden baseball bat, regulation size. Dark stained wood, worn grip tape, scuff marks — not pristine.",
    "significance": "Ben's home-defense weapon; a visual signifier of preparedness and paranoia. Appears scenes 1-2, then set down."
  }
}
```

---

*Design spec v2.0 — 25 July 2026. Supersedes v1.0 ("Breakdown Studio").*
