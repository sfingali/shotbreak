"""Hierarchical settings engine.

Screenplay knowledge is hierarchical. A setting described once cascades
to all dependents. When the parent changes, children become stale.

Scopes (top-down inheritance):
  GLOBAL    — time period, season, geography, genre
  LOCATION  — per-set descriptions (the cabin, the house, the courthouse)
  GROUP     — scene groups within a location (night scenes at the cabin)

A scene's effective settings are the merge of all active scopes in order,
with narrower scopes overriding broader ones when keys collide.

The inheritance graph is a simple tree: global → location → group → scene.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Setting:
    """A named setting at a specific scope."""
    id: int = 0
    project_id: int = 0
    scope: str = "location"          # 'global', 'location', 'group'
    scope_key: str = ""              # location name, group label, or empty for global
    key: str = ""                    # the setting's name within its scope
    value: str = ""                  # the actual description
    metadata_json: str = "{}"
    created_at: str = ""
    updated_at: str = ""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Add settings tables if they don't exist (insertion-safe migration)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS setting (
            id INTEGER PRIMARY KEY,
            project_id INTEGER REFERENCES project(id),
            scope TEXT NOT NULL DEFAULT 'location',
            scope_key TEXT NOT NULL DEFAULT '',
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            metadata_json TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(project_id, scope, scope_key, key)
        );

        CREATE TABLE IF NOT EXISTS setting_dependency (
            id INTEGER PRIMARY KEY,
            project_id INTEGER REFERENCES project(id),
            parent_setting_id INTEGER REFERENCES setting(id),
            child_type TEXT NOT NULL,    -- 'scene', 'element'
            child_id INTEGER NOT NULL,   -- scene.id or element.id
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(project_id, parent_setting_id, child_type, child_id)
        );

        CREATE TABLE IF NOT EXISTS stale_tracker (
            id INTEGER PRIMARY KEY,
            project_id INTEGER REFERENCES project(id),
            target_type TEXT NOT NULL,   -- 'scene_description', 'bible'
            target_id INTEGER NOT NULL,  -- physical_description.id
            stale_reason TEXT NOT NULL,  -- description of what changed upstream
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(project_id, target_type, target_id)
        );
    """)


def upsert_setting(
    conn: sqlite3.Connection,
    project_id: int,
    scope: str,
    scope_key: str,
    key: str,
    value: str,
    metadata: dict | None = None,
) -> int:
    """Create or update a setting. Returns setting.id."""
    _ensure_schema(conn)
    meta = json.dumps(metadata or {})

    row = conn.execute(
        "SELECT id FROM setting WHERE project_id=? AND scope=? AND scope_key=? AND key=?",
        (project_id, scope, scope_key, key),
    ).fetchone()

    if row:
        conn.execute(
            "UPDATE setting SET value=?, metadata_json=?, updated_at=datetime('now') WHERE id=?",
            (value, meta, row["id"]),
        )
        setting_id = row["id"]
        is_new = False
    else:
        cur = conn.execute(
            "INSERT INTO setting (project_id, scope, scope_key, key, value, metadata_json) VALUES (?,?,?,?,?,?)",
            (project_id, scope, scope_key, key, value, meta),
        )
        setting_id = cur.lastrowid
        is_new = True

    conn.commit()

    if not is_new:
        _mark_dependents_stale(conn, project_id, setting_id, key, value)

    return setting_id


def _mark_dependents_stale(
    conn: sqlite3.Connection,
    project_id: int,
    setting_id: int,
    key: str,
    new_value: str,
) -> None:
    """When a setting changes, mark all dependent scene/element descriptions stale."""
    deps = conn.execute(
        "SELECT child_type, child_id FROM setting_dependency WHERE parent_setting_id=?",
        (setting_id,),
    ).fetchall()

    for dep in deps:
        if dep["child_type"] == "scene":
            # Find physical_descriptions for this scene
            descs = conn.execute(
                "SELECT id FROM physical_description WHERE scene_id=?",
                (dep["child_id"],),
            ).fetchall()
            for d in descs:
                conn.execute(
                    "INSERT OR IGNORE INTO stale_tracker (project_id, target_type, target_id, stale_reason) VALUES (?, 'scene_description', ?, ?)",
                    (project_id, d["id"], f"Setting '{key}' changed to: {new_value[:120]}"),
                )
        elif dep["child_type"] == "element":
            # Find bible for this element
            bibles = conn.execute(
                "SELECT id FROM physical_description WHERE element_id=? AND scene_id IS NULL AND is_current=1",
                (dep["child_id"],),
            ).fetchall()
            for b in bibles:
                conn.execute(
                    "INSERT OR IGNORE INTO stale_tracker (project_id, target_type, target_id, stale_reason) VALUES (?, 'bible', ?, ?)",
                    (project_id, b["id"], f"Setting '{key}' changed to: {new_value[:120]}"),
                )

    conn.commit()


def link_setting_to_child(
    conn: sqlite3.Connection,
    project_id: int,
    setting_id: int,
    child_type: str,
    child_id: int,
) -> None:
    """Record that a child (scene or element) inherits from this setting."""
    _ensure_schema(conn)
    conn.execute(
        "INSERT OR IGNORE INTO setting_dependency (project_id, parent_setting_id, child_type, child_id) VALUES (?,?,?,?)",
        (project_id, setting_id, child_type, child_id),
    )
    conn.commit()


def get_effective_settings(
    conn: sqlite3.Connection,
    project_id: int,
    location: str | None = None,
    group: str | None = None,
    scene_id: int | None = None,
) -> dict[str, str]:
    """Assemble all inherited settings for a scene, narrower scopes overriding broader.

    Order: global → location (if known) → group (if known). Returns dict of key→value.
    """
    _ensure_schema(conn)

    effective: dict[str, str] = {}

    # Global scope
    for row in conn.execute(
        "SELECT key, value FROM setting WHERE project_id=? AND scope='global' ORDER BY key",
        (project_id,),
    ):
        effective[row["key"]] = row["value"]

    # Location scope
    if location:
        for row in conn.execute(
            "SELECT key, value FROM setting WHERE project_id=? AND scope='location' AND scope_key=? ORDER BY key",
            (project_id, location),
        ):
            effective[row["key"]] = row["value"]

    # Group scope
    if group:
        for row in conn.execute(
            "SELECT key, value FROM setting WHERE project_id=? AND scope='group' AND scope_key=? ORDER BY key",
            (project_id, group),
        ):
            effective[row["key"]] = row["value"]

    return effective


def get_setting(
    conn: sqlite3.Connection,
    project_id: int,
    scope: str,
    scope_key: str,
    key: str,
) -> Setting | None:
    """Get a single setting by composite key."""
    row = conn.execute(
        "SELECT * FROM setting WHERE project_id=? AND scope=? AND scope_key=? AND key=?",
        (project_id, scope, scope_key, key),
    ).fetchone()
    if not row:
        return None
    return Setting(**{k: row[k] for k in row.keys()})


def list_stale(
    conn: sqlite3.Connection,
    project_id: int,
) -> list[dict]:
    """List all stale descriptions/bibles that need re-rendering."""
    rows = conn.execute(
        "SELECT * FROM stale_tracker WHERE project_id=? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def clear_stale(
    conn: sqlite3.Connection,
    project_id: int,
    target_type: str | None = None,
    target_id: int | None = None,
) -> None:
    """Remove stale markers after re-rendering."""
    if target_type and target_id:
        conn.execute(
            "DELETE FROM stale_tracker WHERE project_id=? AND target_type=? AND target_id=?",
            (project_id, target_type, target_id),
        )
    else:
        conn.execute("DELETE FROM stale_tracker WHERE project_id=?", (project_id,))
    conn.commit()


def get_all_settings(
    conn: sqlite3.Connection,
    project_id: int,
    scope: str | None = None,
) -> list[Setting]:
    """List all settings, optionally filtered by scope."""
    if scope:
        rows = conn.execute(
            "SELECT * FROM setting WHERE project_id=? AND scope=? ORDER BY scope_key, key",
            (project_id, scope),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM setting WHERE project_id=? ORDER BY scope, scope_key, key",
            (project_id,),
        ).fetchall()
    return [Setting(**{k: r[k] for k in r.keys()}) for r in rows]
