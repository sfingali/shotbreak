"""SQLite database layer — schema, migrations, connection management."""

import sqlite3
import threading
from pathlib import Path

SCHEMA_VERSION = 1

# Global writer connection (one per process, WAL mode)
_WRITER: sqlite3.Connection | None = None
_WRITER_LOCK = threading.Lock()


def _migrations() -> list[str]:
    """Return ordered list of migration SQL statements (v0 → vN)."""
    return [
        # v1: full schema
        """
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        INSERT OR IGNORE INTO config (key, value) VALUES ('schema_version', '1');

        CREATE TABLE IF NOT EXISTS project (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            script_path TEXT,
            script_format TEXT DEFAULT 'fountain',
            script_language TEXT DEFAULT 'en',
            output_language TEXT DEFAULT 'en',
            scene_numbers_locked INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS script_version (
            id INTEGER PRIMARY KEY,
            project_id INTEGER REFERENCES project(id),
            version_num INTEGER NOT NULL,
            raw_text TEXT NOT NULL,
            scene_count INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS story_thread (
            id INTEGER PRIMARY KEY,
            project_id INTEGER REFERENCES project(id),
            label TEXT NOT NULL,
            is_primary INTEGER DEFAULT 0,
            story_date_marker TEXT,
            parent_thread_id INTEGER REFERENCES story_thread(id)
        );

        CREATE TABLE IF NOT EXISTS scene (
            id INTEGER PRIMARY KEY,
            project_id INTEGER REFERENCES project(id),
            script_version_id INTEGER REFERENCES script_version(id),
            scene_number TEXT NOT NULL,
            scene_number_source TEXT DEFAULT 'script',
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
            narrative_position TEXT DEFAULT 'present',
            story_thread_id INTEGER REFERENCES story_thread(id),
            story_order INTEGER,
            story_date_marker TEXT,
            breakdown_status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS montage_beat (
            id INTEGER PRIMARY KEY,
            scene_id INTEGER REFERENCES scene(id),
            beat_order INTEGER NOT NULL,
            beat_text TEXT NOT NULL,
            location_hint TEXT
        );

        CREATE TABLE IF NOT EXISTS element (
            id INTEGER PRIMARY KEY,
            project_id INTEGER REFERENCES project(id),
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            element_type TEXT DEFAULT 'practical',
            metadata_json TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS character_relationship (
            id INTEGER PRIMARY KEY,
            element_id_a INTEGER REFERENCES element(id),
            element_id_b INTEGER REFERENCES element(id),
            relationship_type TEXT NOT NULL,
            notes TEXT,
            ai_confidence REAL,
            is_approved INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS reference_image (
            id INTEGER PRIMARY KEY,
            element_id INTEGER REFERENCES element(id),
            file_path TEXT NOT NULL,
            role TEXT NOT NULL,
            caption TEXT,
            uploaded_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS scene_element (
            id INTEGER PRIMARY KEY,
            scene_id INTEGER REFERENCES scene(id),
            element_id INTEGER REFERENCES element(id),
            montage_beat_id INTEGER REFERENCES montage_beat(id),
            context TEXT,
            quantity INTEGER DEFAULT 1,
            notes TEXT,
            ai_confidence REAL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS physical_description (
            id INTEGER PRIMARY KEY,
            element_id INTEGER REFERENCES element(id),
            scene_id INTEGER,
            bible_version_id INTEGER REFERENCES physical_description(id),
            description_type TEXT NOT NULL,
            content TEXT NOT NULL,
            prompt_variant TEXT,
            generation_metadata TEXT,
            is_inferred INTEGER DEFAULT 0,
            bias_flag INTEGER DEFAULT 0,
            version INTEGER DEFAULT 1,
            superseded_by INTEGER REFERENCES physical_description(id),
            is_approved INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS continuity_state (
            id INTEGER PRIMARY KEY,
            element_id INTEGER REFERENCES element(id),
            scene_id INTEGER REFERENCES scene(id),
            story_thread_id INTEGER REFERENCES story_thread(id),
            state_json TEXT NOT NULL,
            changed_from_previous TEXT,
            is_aging_checkpoint INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS tag_library (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            keywords TEXT NOT NULL,
            language TEXT DEFAULT 'en',
            preset_type TEXT DEFAULT 'canon'
        );

        CREATE TABLE IF NOT EXISTS breakdown_run (
            id INTEGER PRIMARY KEY,
            project_id INTEGER REFERENCES project(id),
            passes_json TEXT NOT NULL,
            provider_overrides_json TEXT,
            status TEXT DEFAULT 'queued',
            scenes_total INTEGER,
            scenes_completed INTEGER DEFAULT 0,
            current_scene_id INTEGER,
            started_at TEXT,
            finished_at TEXT,
            error_json TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS llm_call_log (
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

        CREATE TABLE IF NOT EXISTS human_eval_sample (
            id INTEGER PRIMARY KEY,
            physical_description_id INTEGER REFERENCES physical_description(id),
            reviewer TEXT,
            verdict TEXT,
            edited_content TEXT,
            edit_distance REAL,
            notes TEXT,
            reviewed_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS element_alias (
            id INTEGER PRIMARY KEY,
            element_id INTEGER REFERENCES element(id),
            raw_mention TEXT NOT NULL,
            match_method TEXT NOT NULL,
            confidence REAL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS continuity_break (
            id INTEGER PRIMARY KEY,
            element_id INTEGER REFERENCES element(id),
            scene_id INTEGER REFERENCES scene(id),
            break_type TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT DEFAULT 'open',
            resolution_notes TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            resolved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS mms_export (
            id INTEGER PRIMARY KEY,
            project_id INTEGER REFERENCES project(id),
            format TEXT NOT NULL,
            file_path TEXT,
            exported_at TEXT DEFAULT (datetime('now'))
        );
        """,
    ]


def get_db(db_path: str | Path) -> sqlite3.Connection:
    """Get or create the writer connection. Thread-safe, WAL mode."""
    global _WRITER
    db_path = Path(db_path)

    with _WRITER_LOCK:
        if _WRITER is not None:
            return _WRITER

        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row

        # Run migrations
        conn.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        cur = conn.execute("SELECT value FROM config WHERE key = 'schema_version'")
        row = cur.fetchone()
        current = int(row["value"]) if row else 0

        migrations = _migrations()
        for i in range(current, len(migrations)):
            conn.executescript(migrations[i])

        _WRITER = conn
        return conn


def close_db() -> None:
    """Close the writer connection (for cleanup)."""
    global _WRITER
    with _WRITER_LOCK:
        if _WRITER:
            _WRITER.close()
            _WRITER = None
