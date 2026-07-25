"""Core service layer — the single public API for the Shotbreak engine.

Every operation the CLI or web UI can perform is a function here.
Neither cli.py nor web/server.py imports from core/db.py, core/breakdown_engine.py,
or the LLM layer directly — they all go through this module.
"""

from __future__ import annotations

import json
from pathlib import Path

from shotbreak.core import db as _db
from shotbreak.core.fountain_parser import parse_fountain as _parse_fountain

# CLI/API pass name -> config.yaml pass_models key (they differ because
# config.yaml's keys are more descriptive than the short CLI --passes names).
_PASS_CONFIG_KEY = {
    "extract": "extract_classify",
    "coreference": "coreference_llm_stage",
    "bible": "bible",
    "scene_descriptions": "scene_descriptions",
}


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


def run_breakdown(
    project_id: int,
    passes: list[str],
    provider_overrides: dict[str, str] | None = None,
    data_dir: str | Path = "./data",
    config_path: str | Path = "config.yaml",
) -> dict:
    """Run one or more breakdown passes over a project's scenes.

    passes: any of 'extract', 'coreference' (bible/scene_descriptions are a
    later phase). provider_overrides maps pass name -> provider name,
    overriding config.yaml's pass_models for this run only. Progress is
    tracked in the breakdown_run table as it goes, so get_run_status() reflects
    a run that's still in progress from another process/thread.
    """
    from shotbreak.core import breakdown_engine as _breakdown_engine
    from shotbreak.core import config as _config

    config = _config.load_config(config_path)
    provider_overrides = provider_overrides or {}

    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)

    project = conn.execute("SELECT id FROM project WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        raise ValueError(f"No project with id {project_id}")

    scenes_total = conn.execute(
        "SELECT COUNT(*) AS c FROM scene WHERE project_id = ?", (project_id,)
    ).fetchone()["c"]

    cur = conn.execute(
        "INSERT INTO breakdown_run (project_id, passes_json, provider_overrides_json, "
        "status, scenes_total, started_at) VALUES (?, ?, ?, 'running', ?, datetime('now'))",
        (project_id, json.dumps(passes), json.dumps(provider_overrides), scenes_total),
    )
    run_id = cur.lastrowid
    conn.commit()

    all_errors: list[dict] = []
    pass_summaries: dict[str, dict] = {}
    status = "error"

    try:
        for pass_name in passes:
            provider_name = provider_overrides.get(pass_name) or _config.get_pass_provider(
                config, _PASS_CONFIG_KEY.get(pass_name, pass_name)
            )

            if pass_name == "extract":
                def on_progress(done: int, total: int, scene_id: int, _run_id: int = run_id) -> None:
                    conn.execute(
                        "UPDATE breakdown_run SET scenes_completed = ?, current_scene_id = ? WHERE id = ?",
                        (done, scene_id, _run_id),
                    )
                    conn.commit()

                summary = _breakdown_engine.run_extract_pass(
                    conn, config, project_id, provider_name,
                    breakdown_run_id=run_id, on_progress=on_progress,
                )
            elif pass_name == "coreference":
                summary = _breakdown_engine.run_coreference_pass(
                    conn, config, project_id, provider_name, breakdown_run_id=run_id,
                )
            else:
                raise ValueError(f"Unknown pass '{pass_name}'")

            pass_summaries[pass_name] = summary
            all_errors.extend(summary.get("errors", []))

        status = "complete"
    finally:
        conn.execute(
            "UPDATE breakdown_run SET status = ?, finished_at = datetime('now'), error_json = ? "
            "WHERE id = ?",
            (status, json.dumps(all_errors) if all_errors else None, run_id),
        )
        conn.commit()

    return {"run_id": run_id, "status": status, "passes": pass_summaries, "errors": all_errors}


def get_run_status(run_id: int, data_dir: str | Path = "./data") -> dict:
    """Fetch a breakdown_run's progress/status plus its cost rollup from llm_call_log."""
    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)

    row = conn.execute("SELECT * FROM breakdown_run WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise ValueError(f"No breakdown_run with id {run_id}")

    cost_row = conn.execute(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS total_cost, "
        "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens "
        "FROM llm_call_log WHERE breakdown_run_id = ?",
        (run_id,),
    ).fetchone()

    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "passes": json.loads(row["passes_json"]),
        "status": row["status"],
        "scenes_total": row["scenes_total"],
        "scenes_completed": row["scenes_completed"],
        "current_scene_id": row["current_scene_id"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "errors": json.loads(row["error_json"]) if row["error_json"] else [],
        "llm_calls": cost_row["calls"],
        "total_cost_usd": cost_row["total_cost"],
        "input_tokens": cost_row["input_tokens"],
        "output_tokens": cost_row["output_tokens"],
    }
