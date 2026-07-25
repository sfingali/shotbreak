"""Core service layer — the single public API for the Shotbreak engine.

Every operation the CLI or web UI can perform is a function here.
Neither cli.py nor web/server.py imports from core/db.py, core/breakdown_engine.py,
or the LLM layer directly — they all go through this module.
"""

from __future__ import annotations

from pathlib import Path

from shotbreak.core import db as _db
from shotbreak.core.fountain_parser import parse_fountain as _parse_fountain


def import_script(
    path: str | Path,
    project_name: str | None = None,
    lang: str = "en",
    data_dir: str | Path = "./data",
) -> dict:
    """Import a screenplay, create a project, and parse all scenes into the database.

    Returns a dict with project info and scene count.
    """
    path = Path(path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Script not found: {path}")

    # Determine format
    suffix = path.suffix.lower()
    if suffix in (".fountain",):
        script_format = "fountain"
    elif suffix in (".fdx", ".xml"):
        script_format = "fdx"
    elif suffix == ".pdf":
        script_format = "pdf"
    else:
        script_format = "fountain"  # best guess

    # Read raw text
    raw_text = path.read_text(encoding="utf-8")

    # Parse scenes
    if script_format == "fountain":
        scenes = _parse_fountain(raw_text, lang=lang)
    elif script_format == "fdx":
        raise NotImplementedError("FDX parser not yet implemented")
    else:
        raise NotImplementedError("PDF parser not yet implemented")

    # Database
    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)

    project_name = project_name or path.stem

    cur = conn.execute(
        "INSERT INTO project (name, script_path, script_format, script_language) VALUES (?, ?, ?, ?)",
        (project_name, str(path), script_format, lang),
    )
    project_id = cur.lastrowid

    conn.execute(
        "INSERT INTO script_version (project_id, version_num, raw_text, scene_count) VALUES (?, 1, ?, ?)",
        (project_id, raw_text, len(scenes)),
    )
    version_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Create primary story thread and insert scenes
    conn.execute(
        "INSERT INTO story_thread (project_id, label, is_primary) VALUES (?, 'present', 1)",
        (project_id,),
    )
    thread_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    for order, sc in enumerate(scenes, start=1):
        cur = conn.execute(
            """INSERT INTO scene
               (project_id, script_version_id, scene_number, scene_number_source,
                slugline, interior_exterior, location, set_name, time_of_day,
                page_count_eighths, raw_body, has_dual_dialogue, is_montage,
                narrative_position, story_thread_id, story_order, story_date_marker)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id, version_id,
                sc.scene_number, sc.scene_number_source,
                sc.slugline, sc.interior_exterior, sc.location, sc.set_name,
                sc.time_of_day, sc.page_eighths,
                "\n".join(sc.body_lines),
                int(sc.has_dual_dialogue), int(sc.is_montage),
                sc.narrative_position_hint or "present",
                thread_id, order,
                sc.story_date_marker,
            ),
        )
        scene_id = cur.lastrowid

        # Insert montage beats
        for beat in sc.montage_beats:
            conn.execute(
                "INSERT INTO montage_beat (scene_id, beat_order, beat_text, location_hint) VALUES (?, ?, ?, ?)",
                (scene_id, beat.beat_order, beat.beat_text, beat.location_hint),
            )

    conn.commit()

    return {
        "project_id": project_id,
        "project_name": project_name,
        "script_format": script_format,
        "scene_count": len(scenes),
        "characters_found": len(set(c for s in scenes for c in s.characters)),
        "flashbacks_found": sum(1 for s in scenes if s.narrative_position_hint == "flashback"),
        "flashforwards_found": sum(1 for s in scenes if s.narrative_position_hint == "flashforward"),
        "auto_numbered": sum(1 for s in scenes if s.scene_number_source == "auto"),
        "db_path": str(db_path),
    }
