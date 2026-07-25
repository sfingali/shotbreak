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
    elif suffix == ".fdx":
        script_format = "fdx"
    elif suffix == ".fadein":
        script_format = "fadein"
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
        from shotbreak.core.fdx_parser import parse_fdx as _parse_fdx

        scenes = _parse_fdx(raw_text)
    elif script_format == "fadein":
        from shotbreak.core.fadein_parser import parse_fadein as _parse_fadein

        scenes = _parse_fadein(raw_text)
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


def export_mms(
    project_id: int,
    format: str,
    data_dir: str | Path = "./data",
    strict: bool = False,
    output_path: str | Path | None = None,
) -> dict:
    """Export a project to .sex or .MMS10 (Screenwriter XML). Both formats are
    experimental — see core/mms_export.py. If output_path is given, the file
    is written and recorded in the mms_export table.

    Returns {"bytes": ..., "warnings": [...], "file_path": str | None}.
    """
    from shotbreak.core import mms_export as _mms_export

    if format not in ("sex", "mms10"):
        raise ValueError(f"Unknown MMS format '{format}' — expected 'sex' or 'mms10'")

    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)

    if format == "sex":
        data, warnings = _mms_export.export_sex(conn, project_id, strict=strict)
    else:
        data, warnings = _mms_export.export_mms10(conn, project_id, strict=strict)

    file_path = None
    if output_path is not None:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(data)
        file_path = str(output_path)
        conn.execute(
            "INSERT INTO mms_export (project_id, format, file_path) VALUES (?, ?, ?)",
            (project_id, format, file_path),
        )
        conn.commit()

    return {"bytes": data, "warnings": warnings, "file_path": file_path}


def export_fdx(project_id: int, data_dir: str | Path = "./data") -> bytes:
    """Export a project as Final Draft XML (.fdx). Best-effort schema, see
    core/exporters.py."""
    from shotbreak.core import exporters as _exporters

    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)
    return _exporters.export_fdx(conn, project_id)


def export_fadein(project_id: int, data_dir: str | Path = "./data") -> bytes:
    """Export a project as Fade In XML (.fadein). Best-effort schema, see
    core/exporters.py."""
    from shotbreak.core import exporters as _exporters

    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)
    return _exporters.export_fadein(conn, project_id)


def export_csv(project_id: int, data_dir: str | Path = "./data") -> tuple[bytes, bytes]:
    """Export a project as (scenes_csv, elements_csv) bytes."""
    from shotbreak.core import exporters as _exporters

    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)
    return _exporters.export_csv(conn, project_id)


def export_pdf(project_id: int, data_dir: str | Path = "./data") -> bytes:
    """Export per-scene PDF breakdown sheets for a project."""
    from shotbreak.core import exporters as _exporters

    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)
    return _exporters.export_pdf(conn, project_id)


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


def generate_character_bible(
    project_id: int,
    element_id: int,
    provider_name: str = "deepseek",
    data_dir: str | Path = "./data",
    config_path: str | Path = "config.yaml",
) -> dict:
    """Generate a character bible (Phase A) by reading the full character arc."""
    from shotbreak.core import config as _config
    from shotbreak.core import description_engine as _de

    cfg = _config.load_config(config_path)
    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)
    return _de.build_character_bible(conn, cfg, project_id, element_id, provider_name)


def generate_scene_descriptions(
    project_id: int,
    element_id: int,
    provider_name: str = "deepseek",
    scene_ids: list[int] | None = None,
    data_dir: str | Path = "./data",
    config_path: str | Path = "config.yaml",
) -> dict:
    """Generate per-scene physical descriptions (Phase B) for a character."""
    from shotbreak.core import config as _config
    from shotbreak.core import description_engine as _de

    cfg = _config.load_config(config_path)
    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)
    return _de.render_scene_descriptions(
        conn, cfg, project_id, element_id, provider_name, scene_ids=scene_ids,
    )


def update_setting(
    project_id: int,
    scope: str,
    scope_key: str,
    key: str,
    value: str,
    data_dir: str | Path = "./data",
) -> dict:
    """Create or update a hierarchical setting. Marks dependents as stale."""
    from shotbreak.core import settings as _settings_lib

    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)
    sid = _settings_lib.upsert_setting(conn, project_id, scope, scope_key, key, value)
    return {"setting_id": sid, "scope": scope, "scope_key": scope_key, "key": key}


def list_settings(
    project_id: int,
    scope: str | None = None,
    data_dir: str | Path = "./data",
) -> list[dict]:
    """List all settings, optionally filtered by scope."""
    from shotbreak.core import settings as _settings_lib

    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)
    return [
        {"id": s.id, "scope": s.scope, "scope_key": s.scope_key, "key": s.key, "value": s.value}
        for s in _settings_lib.get_all_settings(conn, project_id, scope=scope)
    ]


def list_stale_descriptions(
    project_id: int,
    data_dir: str | Path = "./data",
) -> list[dict]:
    """List descriptions/bibles that need re-rendering due to changed settings."""
    from shotbreak.core import settings as _settings_lib

    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)
    return _settings_lib.list_stale(conn, project_id)


def list_continuity_breaks(
    project_id: int,
    data_dir: str | Path = "./data",
    status: str | None = "open",
) -> list[dict]:
    """List continuity breaks requiring human review."""
    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)
    query = "SELECT * FROM continuity_break WHERE element_id IN (SELECT id FROM element WHERE project_id=?)"
    params: list = [project_id]
    if status:
        query += " AND status=?"
        params.append(status)
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Read-oriented functions backing the web review UI (Phase 5). Reads live here
# rather than in web/server.py for the same reason writes do: the web layer
# must stay a thin presentation layer over core.service, never touching
# core/db.py directly (see module docstring and DESIGN.md §2.2/§5.1).
# ---------------------------------------------------------------------------


def list_projects(data_dir: str | Path = "./data") -> list[dict]:
    """List all projects with scene/element counts, most recently created first."""
    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)
    rows = conn.execute(
        """SELECT p.*,
                  (SELECT COUNT(*) FROM scene WHERE project_id = p.id) AS scene_count,
                  (SELECT COUNT(*) FROM element WHERE project_id = p.id) AS element_count
           FROM project p ORDER BY p.id DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def get_project(project_id: int, data_dir: str | Path = "./data") -> dict:
    """Project detail: counts by category, and the most recent breakdown run."""
    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)
    row = conn.execute("SELECT * FROM project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise ValueError(f"No project with id {project_id}")

    project = dict(row)
    project["scene_count"] = conn.execute(
        "SELECT COUNT(*) AS c FROM scene WHERE project_id = ?", (project_id,)
    ).fetchone()["c"]
    project["element_count"] = conn.execute(
        "SELECT COUNT(*) AS c FROM element WHERE project_id = ?", (project_id,)
    ).fetchone()["c"]
    project["category_counts"] = {
        r["category"]: r["c"]
        for r in conn.execute(
            "SELECT category, COUNT(*) AS c FROM element WHERE project_id = ? GROUP BY category",
            (project_id,),
        )
    }
    last_run = conn.execute(
        "SELECT * FROM breakdown_run WHERE project_id = ? ORDER BY id DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    project["last_run"] = dict(last_run) if last_run else None
    return project


def list_scenes(
    project_id: int,
    page: int = 1,
    page_size: int = 50,
    data_dir: str | Path = "./data",
) -> dict:
    """Paginated scene list, ordered by story_order, with per-scene element counts."""
    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)

    total = conn.execute(
        "SELECT COUNT(*) AS c FROM scene WHERE project_id = ?", (project_id,)
    ).fetchone()["c"]

    page = max(page, 1)
    offset = (page - 1) * page_size
    rows = conn.execute(
        "SELECT * FROM scene WHERE project_id = ? ORDER BY story_order, id LIMIT ? OFFSET ?",
        (project_id, page_size, offset),
    ).fetchall()

    scene_ids = [r["id"] for r in rows]
    counts: dict[int, int] = {}
    if scene_ids:
        placeholders = ",".join("?" * len(scene_ids))
        for r in conn.execute(
            f"SELECT scene_id, COUNT(*) AS c FROM scene_element "
            f"WHERE scene_id IN ({placeholders}) GROUP BY scene_id",
            scene_ids,
        ):
            counts[r["scene_id"]] = r["c"]

    scenes = []
    for r in rows:
        s = dict(r)
        s["element_count"] = counts.get(r["id"], 0)
        scenes.append(s)

    page_size = page_size or 1
    return {
        "scenes": scenes,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size,
    }


def list_elements(
    project_id: int,
    category: str | None = None,
    data_dir: str | Path = "./data",
) -> list[dict]:
    """Elements for a project, optionally filtered by category, with appearance counts."""
    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)

    if category:
        rows = conn.execute(
            "SELECT * FROM element WHERE project_id = ? AND category = ? ORDER BY name",
            (project_id, category),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM element WHERE project_id = ? ORDER BY category, name",
            (project_id,),
        ).fetchall()

    element_ids = [r["id"] for r in rows]
    counts: dict[int, int] = {}
    if element_ids:
        placeholders = ",".join("?" * len(element_ids))
        for r in conn.execute(
            f"SELECT element_id, COUNT(*) AS c FROM scene_element "
            f"WHERE element_id IN ({placeholders}) GROUP BY element_id",
            element_ids,
        ):
            counts[r["element_id"]] = r["c"]

    elements = []
    for r in rows:
        e = dict(r)
        e["appearance_count"] = counts.get(r["id"], 0)
        elements.append(e)
    return elements


def get_element(project_id: int, element_id: int, data_dir: str | Path = "./data") -> dict:
    """Element detail: descriptions (bible + scene deltas), scene appearances, continuity states."""
    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)

    row = conn.execute(
        "SELECT * FROM element WHERE id = ? AND project_id = ?", (element_id, project_id)
    ).fetchone()
    if row is None:
        raise ValueError(f"No element {element_id} in project {project_id}")
    element = dict(row)

    element["descriptions"] = [
        dict(d)
        for d in conn.execute(
            "SELECT * FROM physical_description WHERE element_id = ? "
            "ORDER BY (scene_id IS NOT NULL), version DESC, id DESC",
            (element_id,),
        )
    ]

    element["appearances"] = [
        dict(a)
        for a in conn.execute(
            "SELECT se.*, s.scene_number, s.slugline, s.story_order FROM scene_element se "
            "JOIN scene s ON se.scene_id = s.id WHERE se.element_id = ? ORDER BY s.story_order",
            (element_id,),
        )
    ]

    element["continuity_states"] = [
        dict(c)
        for c in conn.execute(
            "SELECT cs.*, s.scene_number, s.story_order FROM continuity_state cs "
            "JOIN scene s ON cs.scene_id = s.id WHERE cs.element_id = ? ORDER BY s.story_order",
            (element_id,),
        )
    ]

    return element


def get_scene(project_id: int, scene_id: int, data_dir: str | Path = "./data") -> dict:
    """Scene detail: tagged elements, scene-specific description deltas, montage beats."""
    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)

    row = conn.execute(
        "SELECT * FROM scene WHERE id = ? AND project_id = ?", (scene_id, project_id)
    ).fetchone()
    if row is None:
        raise ValueError(f"No scene {scene_id} in project {project_id}")
    scene = dict(row)

    scene["elements"] = [
        dict(e)
        for e in conn.execute(
            "SELECT e.*, se.context, se.quantity, se.notes, se.ai_confidence, se.montage_beat_id "
            "FROM scene_element se JOIN element e ON se.element_id = e.id "
            "WHERE se.scene_id = ? ORDER BY e.category, e.name",
            (scene_id,),
        )
    ]

    scene["descriptions"] = [
        dict(d)
        for d in conn.execute(
            "SELECT pd.*, e.name AS element_name FROM physical_description pd "
            "JOIN element e ON pd.element_id = e.id WHERE pd.scene_id = ? ORDER BY e.name",
            (scene_id,),
        )
    ]

    scene["montage_beats"] = [
        dict(b)
        for b in conn.execute(
            "SELECT * FROM montage_beat WHERE scene_id = ? ORDER BY beat_order", (scene_id,)
        )
    ]

    return scene


def update_element(
    project_id: int,
    element_id: int,
    updates: dict,
    data_dir: str | Path = "./data",
) -> dict:
    """Edit an element's name/category/element_type from the review UI."""
    from shotbreak.core.breakdown_engine import CATEGORIES

    allowed = {"name", "category", "element_type"}
    fields = {k: v for k, v in updates.items() if k in allowed and v}
    if not fields:
        raise ValueError("No editable fields provided (allowed: name, category, element_type)")
    if "category" in fields and fields["category"] not in CATEGORIES:
        raise ValueError(f"Unknown category '{fields['category']}'")

    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)
    row = conn.execute(
        "SELECT id FROM element WHERE id = ? AND project_id = ?", (element_id, project_id)
    ).fetchone()
    if row is None:
        raise ValueError(f"No element {element_id} in project {project_id}")

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE element SET {set_clause} WHERE id = ?", (*fields.values(), element_id))
    conn.commit()

    return get_element(project_id, element_id, data_dir=data_dir)


def get_latest_breakdown_status(project_id: int, data_dir: str | Path = "./data") -> dict | None:
    """Status of a project's most recent breakdown_run — lets the UI poll a
    run it triggered without holding onto a run_id across the request that
    kicked it off in the background."""
    db_path = Path(data_dir) / "shotbreak.db"
    conn = _db.get_db(db_path)
    row = conn.execute(
        "SELECT id FROM breakdown_run WHERE project_id = ? ORDER BY id DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    if row is None:
        return None
    return get_run_status(row["id"], data_dir=data_dir)
