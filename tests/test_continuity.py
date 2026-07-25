"""Tests for continuity state machine and settings engine."""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from shotbreak.core import continuity, settings, db as shotbreak_db


@pytest.fixture
def conn():
    """Fresh SQLite connection with schema for each test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    shotbreak_db._WRITER = None
    c = shotbreak_db.get_db(path)
    yield c
    c.close()
    shotbreak_db._WRITER = None
    Path(path).unlink(missing_ok=True)


def _seed_scenes(conn, project_id=1, thread_id=1):
    """Insert test scenes: 3 present-day + 1 flashback with an element."""
    conn.execute("INSERT INTO project (id, name) VALUES (?, 'test')", (project_id,))
    conn.execute("INSERT INTO story_thread (id, project_id, label, is_primary) VALUES (?,?, 'present', 1)", (thread_id, project_id))
    fb_thread_id = thread_id + 1
    conn.execute("INSERT INTO story_thread (id, project_id, label, is_primary) VALUES (?,?, 'flashback-war', 0)", (fb_thread_id, project_id))

    scenes = [
        (1, "INT. ROOM - DAY #1#", 1, thread_id, "He enters wearing a black coat."),
        (2, "INT. HALLWAY - NIGHT #2#", 2, thread_id, "He removes the coat. Bruises visible on his arms."),
        (3, "EXT. STREET - DAY #3#", 3, thread_id, "He walks quickly. Still no coat."),
        (4, "EXT. BATTLEFIELD (FLASHBACK) - DAY #4#", 1, fb_thread_id, "Younger. Military uniform. No bruises."),
    ]
    for sid, slug, order, tid, body in scenes:
        conn.execute(
            "INSERT INTO scene (id, project_id, scene_number, slugline, story_order, story_thread_id, raw_body, narrative_position) VALUES (?,?,?,?,?,?,?,?)",
            (sid, project_id, str(sid), slug, order, tid, body, "flashback" if sid == 4 else "present"),
        )

    conn.execute(
        "INSERT INTO element (id, project_id, name, category, element_type) VALUES (?,?, 'Ben', 'cast', 'character')",
        (1, project_id),
    )
    for sid in [1, 2, 3, 4]:
        conn.execute(
            "INSERT INTO scene_element (scene_id, element_id, context) VALUES (?,?, 'test')",
            (sid, 1),
        )
    conn.commit()
    return thread_id, fb_thread_id


class TestContinuityWalk:
    def test_walks_scenes_in_story_order(self, conn):
        _seed_scenes(conn)
        walks = continuity.walk_element_continuity(conn, element_id=1)
        assert len(walks) == 2

        present = [w for w in walks if w.thread_label == "present"][0]
        flashback = [w for w in walks if w.thread_label == "flashback-war"][0]

        assert len(present.states) == 3
        assert present.states[0]["story_order"] == 1
        assert present.states[1]["story_order"] == 2
        assert present.states[2]["story_order"] == 3

        s0 = json.loads(present.states[0]["state_json"])
        assert s0["_first_appearance"] is True

        assert len(flashback.states) == 1
        assert flashback.states[0]["story_order"] == 1

    def test_threads_do_not_mix(self, conn):
        _seed_scenes(conn)
        walks = continuity.walk_element_continuity(conn, element_id=1)
        flashback = [w for w in walks if w.thread_label == "flashback-war"][0]
        assert flashback.states[0]["scene_id"] == 4
        s0 = json.loads(flashback.states[0]["state_json"])
        assert s0["_first_appearance"] is True

    def test_empty_element(self, conn):
        conn.execute("INSERT INTO project (id, name) VALUES (1, 'test')")
        conn.execute("INSERT INTO element (id, project_id, name, category, element_type) VALUES (1,1,'Ghost','cast','character')")
        walks = continuity.walk_element_continuity(conn, element_id=1)
        assert walks == []


class TestSettings:
    def test_upsert_and_get(self, conn):
        conn.execute("INSERT INTO project (id, name) VALUES (1, 'test')")
        sid = settings.upsert_setting(conn, 1, "location", "CABIN", "description", "Cedar-siding browned like old pennies")
        assert sid > 0
        s = settings.get_setting(conn, 1, "location", "CABIN", "description")
        assert s is not None
        assert "Cedar-siding" in s.value

    def test_effective_settings_merge(self, conn):
        conn.execute("INSERT INTO project (id, name) VALUES (1, 'test')")
        settings.upsert_setting(conn, 1, "global", "", "season", "Autumn")
        settings.upsert_setting(conn, 1, "global", "", "period", "Modern day")
        settings.upsert_setting(conn, 1, "location", "CABIN", "description", "Cedar-siding, old pennies brown")
        effective = settings.get_effective_settings(conn, 1, location="CABIN")
        assert effective["season"] == "Autumn"
        assert effective["period"] == "Modern day"
        assert effective["description"] == "Cedar-siding, old pennies brown"

    def test_location_overrides_global(self, conn):
        conn.execute("INSERT INTO project (id, name) VALUES (1, 'test')")
        settings.upsert_setting(conn, 1, "global", "", "lighting", "Neutral")
        settings.upsert_setting(conn, 1, "location", "BASEMENT", "lighting", "Dark, single bare bulb")
        effective = settings.get_effective_settings(conn, 1, location="BASEMENT")
        assert effective["lighting"] == "Dark, single bare bulb"

    def test_update_marks_dependents_stale(self, conn):
        conn.execute("INSERT INTO project (id, name) VALUES (1, 'test')")
        sid = settings.upsert_setting(conn, 1, "location", "CABIN", "mood", "Cozy")
        settings.link_setting_to_child(conn, 1, sid, "scene", 31)
        conn.execute("INSERT INTO element (id, project_id, name, category, element_type) VALUES (1,1,'Ben','cast','character')")
        conn.execute("INSERT INTO physical_description (id, element_id, scene_id, description_type, content, is_current) VALUES (1,1,31,'scene_description','{}',1)")
        settings.upsert_setting(conn, 1, "location", "CABIN", "mood", "Ominous")
        stale = settings.list_stale(conn, 1)
        assert len(stale) == 1
        assert stale[0]["target_type"] == "scene_description"
        assert stale[0]["target_id"] == 1


class TestTimeJumps:
    def test_parse_years_numeric(self):
        assert continuity._parse_years("12 YEARS EARLIER") == 12
        assert continuity._parse_years("3 years later") == 3

    def test_parse_years_named(self):
        assert continuity._parse_years("TEN YEARS LATER") == 10
        assert continuity._parse_years("FIVE YEARS EARLIER") == 5
        assert continuity._parse_years("THREE YEARS BEFORE") == 3

    def test_parse_years_months_not_years(self):
        assert continuity._parse_years("6 months later") == 0

    def test_parse_years_none(self):
        assert continuity._parse_years("CONTINUOUS") is None
        assert continuity._parse_years("MOMENTS LATER") is None
