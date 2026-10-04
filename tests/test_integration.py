"""Tests for FDX and Fade In parsers, breakdown engine, description engine, and exports.

All tests are pure-logic — no LLM calls, no network.
"""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from shotbreak.core import db as shotbreak_db, settings, continuity
from shotbreak.core.fdx_parser import parse_fdx, _parse_slugline
from shotbreak.core.fadein_parser import parse_fadein


# ── Fixture ──

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


# ── FDX Parser ──

class TestFdxParser:
    def test_empty_fdx(self):
        scenes = parse_fdx('<?xml version="1.0"?><FinalDraft></FinalDraft>')
        assert scenes == []

    def test_single_scene_heading(self):
        fdx = """<?xml version="1.0"?>
<FinalDraft>
  <Content>
    <Paragraph Type="Scene Heading">
      <SceneProperties Length="2/8" Page="1" Number="1"/>
      <Text>INT. KITCHEN - HOUSE - DAY</Text>
    </Paragraph>
  </Content>
</FinalDraft>"""
        scenes = parse_fdx(fdx)
        assert len(scenes) == 1
        assert scenes[0].slugline == "INT. KITCHEN - HOUSE - DAY"
        assert scenes[0].interior_exterior == "INT"
        assert scenes[0].location == "KITCHEN"
        assert scenes[0].set_name == "HOUSE"
        assert scenes[0].time_of_day == "DAY"
        assert scenes[0].scene_number == "1"
        assert scenes[0].page_eighths == 2

    def test_two_scenes_with_character(self):
        fdx = """<?xml version="1.0"?>
<FinalDraft>
  <Content>
    <Paragraph Type="Scene Heading">
      <SceneProperties Length="1/8" Page="1"/>
      <Text>EXT. STREET - DAY</Text>
    </Paragraph>
    <Paragraph Type="Action">
      <Text>A man walks.</Text>
    </Paragraph>
    <Paragraph Type="Character">
      <Text>BEN</Text>
    </Paragraph>
    <Paragraph Type="Dialogue">
      <Text>Hello.</Text>
    </Paragraph>
    <Paragraph Type="Scene Heading">
      <SceneProperties Length="2/8" Page="1"/>
      <Text>INT. OFFICE - DAY</Text>
    </Paragraph>
    <Paragraph Type="Action">
      <Text>Papers everywhere.</Text>
    </Paragraph>
  </Content>
</FinalDraft>"""
        scenes = parse_fdx(fdx)
        assert len(scenes) == 2
        assert scenes[0].slugline == "EXT. STREET - DAY"
        assert "BEN" in scenes[0].characters
        assert "Hello." in scenes[0].body_lines
        assert scenes[1].slugline == "INT. OFFICE - DAY"

    def test_flashback_detection(self):
        fdx = """<?xml version="1.0"?>
<FinalDraft>
  <Content>
    <Paragraph Type="Scene Heading">
      <SceneProperties Length="1/8" Page="1"/>
      <Text>INT. CLASSROOM (FLASHBACK) - DAY</Text>
    </Paragraph>
  </Content>
</FinalDraft>"""
        scenes = parse_fdx(fdx)
        assert scenes[0].narrative_position_hint == "flashback"


class TestFdxSceneNumbers:
    def test_scene_number_on_paragraph(self):
        # Final Draft and Fade In write the number on the Scene Heading
        # <Paragraph>, not on <SceneProperties>.
        fdx = """<?xml version="1.0"?>
<FinalDraft><Content>
  <Paragraph Type="Scene Heading" Number="12A">
    <SceneProperties Length="1/8" Page="1"/><Text>INT. ROOM - DAY</Text>
  </Paragraph>
  <Paragraph Type="Scene Heading" Number="7">
    <SceneProperties Length="1/8" Page="1" Number="99"/><Text>EXT. YARD - NIGHT</Text>
  </Paragraph>
  <Paragraph Type="Scene Heading"><Text>INT. HALL - DAY</Text></Paragraph>
</Content></FinalDraft>"""
        scenes = parse_fdx(fdx)
        assert [(s.scene_number, s.scene_number_source) for s in scenes] == [
            ("12A", "script"), ("7", "script"), ("3", "auto")]


class TestFdxSluglineParsing:
    def test_standard_int(self):
        r = _parse_slugline("INT. KITCHEN - HOUSE - DAY")
        assert r["interior_exterior"] == "INT"
        assert r["location"] == "KITCHEN"
        assert r["set_name"] == "HOUSE"
        assert r["time_of_day"] == "DAY"

    def test_ext_no_period(self):
        r = _parse_slugline("EXT STREET - DAY")
        assert r["interior_exterior"] == "EXT"
        assert r["location"] == "STREET"
        assert r["time_of_day"] == "DAY"

    def test_no_time_of_day(self):
        r = _parse_slugline("INT. HALLWAY")
        assert r["interior_exterior"] == "INT"
        assert r["location"] == "HALLWAY"
        assert r["time_of_day"] == ""

    def test_time_with_parenthetical(self):
        r = _parse_slugline("INT. OFFICE (FLASHBACK) - DAY")
        assert r["interior_exterior"] == "INT"
        assert r["time_of_day"] == "DAY"


# ── Fade In Parser ──

class TestFadeinParser:
    def test_empty(self):
        scenes = parse_fadein('<document type="Fade In Pro"></document>')
        assert scenes == []

    def test_single_scene(self):
        xml = """<?xml version="1.0"?>
<document type="Fade In Pro" version="1">
  <content>
    <paragraph type="scene heading" number="1">
      <text>INT. BAR - NIGHT</text>
    </paragraph>
    <paragraph type="action">
      <text>Smoke curls upward.</text>
    </paragraph>
    <paragraph type="character">
      <text>RICK</text>
    </paragraph>
    <paragraph type="dialogue">
      <text>Of all the gin joints.</text>
    </paragraph>
  </content>
</document>"""
        scenes = parse_fadein(xml)
        assert len(scenes) == 1
        assert scenes[0].slugline == "INT. BAR - NIGHT"
        assert scenes[0].scene_number == "1"
        assert "RICK" in scenes[0].characters
        assert "Smoke curls upward." in scenes[0].body_lines

    def test_two_scenes(self):
        xml = """<?xml version="1.0"?>
<document type="Fade In Pro" version="1">
  <content>
    <paragraph type="scene heading" number="1">
      <text>INT. HOUSE - DAY</text>
    </paragraph>
    <paragraph type="action"><text>A.</text></paragraph>
    <paragraph type="scene heading" number="2">
      <text>EXT. GARDEN - DAY</text>
    </paragraph>
    <paragraph type="action"><text>B.</text></paragraph>
  </content>
</document>"""
        scenes = parse_fadein(xml)
        assert len(scenes) == 2
        assert scenes[0].slugline == "INT. HOUSE - DAY"
        assert scenes[1].slugline == "EXT. GARDEN - DAY"



# ── Service-level integration (the production paths) ──

class TestServiceIntegration:
    def test_import_fdx_via_service(self, tmp_path):
        pytest.importorskip("fdx_parser")
        from shotbreak.core import db as shotbreak_db
        from shotbreak.core import service

        shotbreak_db.close_db()
        fdx_path = tmp_path / "service_import.fdx"
        fdx_path.write_text(
            """<?xml version="1.0"?>
<FinalDraft>
  <Content>
    <Paragraph Type="Scene Heading">
      <SceneProperties Length="2/8" Page="1" Number="1"/>
      <Text>INT. KITCHEN - HOUSE - DAY</Text>
    </Paragraph>
    <Paragraph Type="Action"><Text>Ben enters.</Text></Paragraph>
    <Paragraph Type="Character"><Text>BEN</Text></Paragraph>
    <Paragraph Type="Dialogue"><Text>Hello.</Text></Paragraph>
    <Paragraph Type="Scene Heading">
      <SceneProperties Length="1/8" Page="2" Number="2"/>
      <Text>EXT. STREET - NIGHT</Text>
    </Paragraph>
    <Paragraph Type="Action"><Text>He leaves.</Text></Paragraph>
  </Content>
</FinalDraft>""",
            encoding="utf-8",
        )

        result = service.import_script(
            fdx_path, project_name="fdx-service", data_dir=tmp_path
        )

        assert result["script_format"] == "fdx"
        assert result["scene_count"] == 2
        assert result["characters_found"] == 1

        conn = shotbreak_db.get_db(tmp_path / "shotbreak.db")
        scenes = conn.execute(
            "SELECT scene_number, is_montage, has_dual_dialogue "
            "FROM scene WHERE project_id = ? ORDER BY story_order",
            (result["project_id"],),
        ).fetchall()
        assert [s["scene_number"] for s in scenes] == ["1", "2"]
        assert all(s["is_montage"] == 0 for s in scenes)
        assert all(s["has_dual_dialogue"] == 0 for s in scenes)
        shotbreak_db.close_db()

    def test_export_sex_via_service(self, tmp_path):
        pytest.importorskip("pyoms")
        from shotbreak.core import db as shotbreak_db
        from shotbreak.core import service
        from shotbreak.core.mms_export import ExperimentalFormatError

        shotbreak_db.close_db()
        conn = shotbreak_db.get_db(tmp_path / "shotbreak.db")
        conn.execute("INSERT INTO project (id, name) VALUES (1, 'sex-service')")
        conn.execute(
            "INSERT INTO scene "
            "(id, project_id, scene_number, slugline, interior_exterior, location, "
            "time_of_day, page_count_eighths, raw_body) "
            "VALUES (1, 1, '1', 'INT. ROOM - DAY', 'INT', 'ROOM', 'DAY', 2, '')"
        )
        conn.execute(
            "INSERT INTO element (id, project_id, name, category, element_type) "
            "VALUES (1, 1, 'Ben', 'cast', 'character')"
        )
        conn.execute(
            "INSERT INTO element (id, project_id, name, category, element_type) "
            "VALUES (2, 1, 'Wallet', 'props', 'practical')"
        )
        conn.execute("INSERT INTO scene_element (scene_id, element_id) VALUES (1, 1)")
        conn.execute("INSERT INTO scene_element (scene_id, element_id) VALUES (1, 2)")
        conn.commit()

        result = service.export_mms(1, "sex", data_dir=tmp_path)

        assert result["bytes"].startswith(b"SSI*")
        assert b"Ben" in result["bytes"]
        assert b"Wallet" in result["bytes"]

        with pytest.raises(ExperimentalFormatError):
            service.export_mms(1, "sex", data_dir=tmp_path, strict=True)

        shotbreak_db.close_db()


# ── Breakdown Engine (schema validation, element creation) ──

class TestBreakdownEngine:
    def test_get_or_create_element_idempotent(self, conn):
        from shotbreak.core.breakdown_engine import get_or_create_element

        conn.execute("INSERT INTO project (id, name) VALUES (1, 'test')")

        eid1 = get_or_create_element(conn, 1, "props", "Baseball Bat", "practical")
        eid2 = get_or_create_element(conn, 1, "props", "Baseball Bat", "practical")
        assert eid1 == eid2
        assert eid1 > 0

    def test_get_or_create_different_case_creates_new(self, conn):
        from shotbreak.core.breakdown_engine import get_or_create_element

        conn.execute("INSERT INTO project (id, name) VALUES (1, 'test')")

        eid1 = get_or_create_element(conn, 1, "props", "Baseball Bat", "practical")
        eid2 = get_or_create_element(conn, 1, "props", "baseball bat", "practical")
        assert eid1 != eid2

    def test_categories_constant_exists(self):
        from shotbreak.core.breakdown_engine import CATEGORIES

        assert isinstance(CATEGORIES, list)
        assert "cast" in CATEGORIES
        assert "props" in CATEGORIES
        assert "wardrobe" in CATEGORIES
        assert len(CATEGORIES) == 18


# ── Description Engine (settings cascade + bible format) ──

class TestDescriptionEngine:
    def test_bible_build_verifies_element_exists(self, conn):
        from shotbreak.core.description_engine import build_character_bible

        conn.execute("INSERT INTO project (id, name) VALUES (1, 'test')")
        conn.commit()

        # build_character_bible should raise when element doesn't exist
        with pytest.raises(ValueError):
            build_character_bible(conn, {}, 1, 999, "deepseek")

    def test_settings_cascade_global_to_location(self, conn):
        conn.execute("INSERT INTO project (id, name) VALUES (1, 'test')")
        settings.upsert_setting(conn, 1, "global", "", "season", "Winter")
        settings.upsert_setting(conn, 1, "location", "CABIN", "mood", "Cozy")

        effective = settings.get_effective_settings(conn, 1, location="CABIN")
        assert effective["season"] == "Winter"
        assert effective["mood"] == "Cozy"

    def test_settings_location_overrides_global(self, conn):
        conn.execute("INSERT INTO project (id, name) VALUES (1, 'test')")
        settings.upsert_setting(conn, 1, "global", "", "lighting", "Bright")
        settings.upsert_setting(conn, 1, "location", "BASEMENT", "lighting", "Dark")

        effective = settings.get_effective_settings(conn, 1, location="BASEMENT")
        assert effective["lighting"] == "Dark"


# ── Exports ──

class TestExports:
    def setup_project(self, conn):
        conn.execute("INSERT INTO project (id, name) VALUES (1, 'test')")
        conn.execute("INSERT INTO scene (id, project_id, scene_number, slugline, interior_exterior, location, time_of_day, raw_body, page_count_eighths) VALUES (1,1,'1','INT. ROOM - DAY','INT','ROOM','DAY','Hello world',2)")
        conn.execute("INSERT INTO scene (id, project_id, scene_number, slugline, interior_exterior, location, time_of_day, raw_body, page_count_eighths) VALUES (2,1,'2','EXT. STREET - NIGHT','EXT','STREET','NIGHT','Goodbye',1)")
        conn.execute("INSERT INTO element (id, project_id, name, category, element_type) VALUES (1,1,'Ben','cast','character')")
        conn.execute("INSERT INTO element (id, project_id, name, category, element_type) VALUES (2,1,'Baseball Bat','props','practical')")
        conn.commit()

    def test_fdx_export_produces_xml(self, conn):
        from shotbreak.core.exporters import export_fdx

        self.setup_project(conn)
        result = export_fdx(conn, 1)
        assert b"<FinalDraft" in result
        assert b"INT. ROOM - DAY" in result

    def test_fadein_export_produces_xml(self, conn):
        from shotbreak.core.exporters import export_fadein

        self.setup_project(conn)
        result = export_fadein(conn, 1)
        assert b"Fade In Pro" in result
        assert b"INT. ROOM - DAY" in result

    def test_csv_export_has_scenes_and_elements(self, conn):
        from shotbreak.core.exporters import export_csv

        self.setup_project(conn)
        scenes_csv, elements_csv = export_csv(conn, 1)
        assert b"scene_number" in scenes_csv
        assert b"INT. ROOM" in scenes_csv
        assert b"name" in elements_csv
        assert b"Ben" in elements_csv

    def test_sex_export_has_header(self, conn):
        from shotbreak.core.mms_export import export_sex

        self.setup_project(conn)
        result_bytes, warnings = export_sex(conn, 1)
        assert b"SSI*" in result_bytes
        assert b"ROOM" in result_bytes  # location field, null-separated


# ── Continuity (already tested in test_continuity.py, add edge case) ──

class TestContinuityEdgeCases:
    def test_single_time_jump(self, conn):
        conn.execute("INSERT INTO project (id, name) VALUES (1, 'test')")
        conn.execute("INSERT INTO story_thread (id, project_id, label, is_primary) VALUES (1,1,'present',1)")
        conn.execute("INSERT INTO element (id, project_id, name, category, element_type) VALUES (1,1,'Ben','cast','character')")
        conn.execute("INSERT INTO scene (id, project_id, scene_number, slugline, raw_body, story_order, story_thread_id, story_date_marker) VALUES (1,1,'1','INT. ROOM - DAY','',1,1,'PRESENT')")
        conn.execute("INSERT INTO scene (id, project_id, scene_number, slugline, raw_body, story_order, story_thread_id, story_date_marker) VALUES (2,1,'2','INT. ROOM - DAY','',2,1,'TEN YEARS LATER')")
        conn.execute("INSERT INTO scene_element (scene_id, element_id) VALUES (1,1)")
        conn.execute("INSERT INTO scene_element (scene_id, element_id) VALUES (2,1)")
        conn.commit()

        walks = continuity.walk_element_continuity(conn, element_id=1)
        assert len(walks) == 1
        states = walks[0].states
        assert len(states) == 2
        assert not states[0]["is_checkpoint"]
        assert states[1]["is_checkpoint"]


# ── Web API (integration) ──

try:
    from fastapi.testclient import TestClient
    from shotbreak.web.server import app
    HAS_FASTAPI = True
except ModuleNotFoundError:
    HAS_FASTAPI = False


@pytest.mark.skipif(not HAS_FASTAPI, reason="fastapi not installed in test venv")
class TestWebApi:
    def test_index_returns_html(self):
        client = TestClient(app)
        response = client.get("/")
        assert response.status_code == 200
        assert "Shotbreak" in response.text

    def test_list_projects(self):
        client = TestClient(app)
        response = client.get("/api/projects")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_config_routes(self):
        client = TestClient(app)
        response = client.get("/api/config")
        assert response.status_code == 200
        assert "yaml" in response.json()

    def test_providers_route(self):
        client = TestClient(app)
        response = client.get("/api/providers")
        assert response.status_code == 200
        data = response.json()
        assert "providers" in data
        assert "default" in data
