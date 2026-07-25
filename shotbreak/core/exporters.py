"""Deterministic export formats — FDX, Fade In, CSV, PDF (DESIGN.md §3.6).

No LLM calls. Everything here reads tagged elements already in the database
and formats them. FDX and Fade In schemas are widely used but neither is
byte-verified against a real Final Draft / Fade In import in this codebase —
treat both as best-effort, readable XML rather than guaranteed round-trip
compatible.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import xml.etree.ElementTree as ET
from xml.dom import minidom

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

CATEGORY_LABELS = {
    "cast": "Cast",
    "background": "Background/Extras",
    "stunts": "Stunts",
    "vehicles": "Vehicles",
    "props": "Props",
    "set_dressing": "Set Dressing",
    "wardrobe": "Wardrobe",
    "makeup_hair": "Makeup/Hair",
    "practical_fx": "Practical FX",
    "vfx": "VFX",
    "animals": "Animals",
    "music": "Music",
    "sound": "Sound",
    "special_equipment": "Special Equipment",
    "greenery": "Greenery",
    "weapons": "Weapons",
    "locations": "Locations",
    "notes": "Notes",
}


def _fetch_scenes(conn: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, scene_number, slugline, interior_exterior, location, set_name, "
        "time_of_day, page_count_eighths, synopsis FROM scene "
        "WHERE project_id = ? ORDER BY story_order, id",
        (project_id,),
    ).fetchall()


def _fetch_scene_elements(conn: sqlite3.Connection, project_id: int) -> dict[int, list[sqlite3.Row]]:
    """scene_id -> list of element rows (id, name, category) tagged to that scene."""
    rows = conn.execute(
        "SELECT se.scene_id, e.id, e.name, e.category FROM scene_element se "
        "JOIN element e ON e.id = se.element_id "
        "JOIN scene s ON s.id = se.scene_id "
        "WHERE s.project_id = ? ORDER BY se.scene_id, e.category, e.name",
        (project_id,),
    ).fetchall()
    out: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        out.setdefault(row["scene_id"], []).append(row)
    return out


def _eighths_to_page_str(eighths: int) -> str:
    eighths = eighths or 0
    whole, rem = divmod(eighths, 8)
    if rem == 0:
        return f"{whole}" if whole else "0"
    return f"{whole} {rem}/8" if whole else f"{rem}/8"


def _project_name(conn: sqlite3.Connection, project_id: int) -> str:
    row = conn.execute("SELECT name FROM project WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise ValueError(f"No project with id {project_id}")
    return row["name"]


# ── FDX export (Final Draft XML) ──────────────────────────────────────────


def export_fdx(conn: sqlite3.Connection, project_id: int) -> bytes:
    """Best-effort Final Draft XML export: scene headings as Scene Heading
    Paragraphs (with SceneProperties carrying the page length), and a
    document-level TagData block listing every tagged element and the
    scenes it appears in.
    """
    scenes = _fetch_scenes(conn, project_id)
    scene_elements = _fetch_scene_elements(conn, project_id)

    root = ET.Element("FinalDraft", {"DocumentType": "Script", "Template": "No", "Version": "1"})
    content = ET.SubElement(root, "Content")

    for scene in scenes:
        heading = ET.SubElement(content, "Paragraph", {"Type": "Scene Heading", "Number": scene["scene_number"]})
        ET.SubElement(
            heading,
            "SceneProperties",
            {
                "Length": _eighths_to_page_str(scene["page_count_eighths"]),
                "Page": scene["scene_number"],
            },
        )
        ET.SubElement(heading, "Text").text = scene["slugline"]

        if scene["synopsis"]:
            action = ET.SubElement(content, "Paragraph", {"Type": "Action"})
            ET.SubElement(action, "Text").text = scene["synopsis"]

        for elem in scene_elements.get(scene["id"], []):
            if elem["category"] == "cast":
                char = ET.SubElement(content, "Paragraph", {"Type": "Character"})
                ET.SubElement(char, "Text").text = elem["name"]

    tag_data = ET.SubElement(root, "TagData")
    elements_by_id: dict[int, dict] = {}
    for scene in scenes:
        for elem in scene_elements.get(scene["id"], []):
            entry = elements_by_id.setdefault(
                elem["id"], {"name": elem["name"], "category": elem["category"], "scenes": []}
            )
            entry["scenes"].append(scene["scene_number"])

    for elem_id, entry in elements_by_id.items():
        tag = ET.SubElement(
            tag_data,
            "Tag",
            {"Id": str(elem_id), "Category": CATEGORY_LABELS.get(entry["category"], entry["category"]), "Name": entry["name"]},
        )
        for scene_number in entry["scenes"]:
            ET.SubElement(tag, "Scene", {"Number": scene_number})

    rough = ET.tostring(root, encoding="unicode")
    pretty = minidom.parseString(rough).toprettyxml(indent="  ")
    return pretty.encode("utf-8")


# ── Fade In export (.fadein) ──────────────────────────────────────────────


def export_fadein(conn: sqlite3.Connection, project_id: int) -> bytes:
    """Best-effort Fade In XML export: scene headings and character cues as
    paragraphs, plus a shotbreak-specific <tags> extension carrying tagged
    elements per scene (Fade In has no native breakdown-tagging schema, so
    this is a documented extension, not a native Fade In feature).
    """
    scenes = _fetch_scenes(conn, project_id)
    scene_elements = _fetch_scene_elements(conn, project_id)

    root = ET.Element("document", {"type": "Fade In Pro", "version": "1"})
    content = ET.SubElement(root, "content")

    for scene in scenes:
        heading = ET.SubElement(content, "paragraph", {"type": "Scene Heading", "number": scene["scene_number"]})
        ET.SubElement(heading, "text").text = scene["slugline"]

        if scene["synopsis"]:
            action = ET.SubElement(content, "paragraph", {"type": "Action"})
            ET.SubElement(action, "text").text = scene["synopsis"]

        for elem in scene_elements.get(scene["id"], []):
            if elem["category"] == "cast":
                char = ET.SubElement(content, "paragraph", {"type": "Character"})
                ET.SubElement(char, "text").text = elem["name"]

    tags_el = ET.SubElement(root, "tags", {"extension": "shotbreak-breakdown-tags"})
    for scene in scenes:
        elems = scene_elements.get(scene["id"], [])
        if not elems:
            continue
        scene_tags = ET.SubElement(tags_el, "scene", {"number": scene["scene_number"]})
        for elem in elems:
            ET.SubElement(
                scene_tags, "tag", {"category": CATEGORY_LABELS.get(elem["category"], elem["category"]), "name": elem["name"]}
            )

    rough = ET.tostring(root, encoding="unicode")
    pretty = minidom.parseString(rough).toprettyxml(indent="  ")
    return pretty.encode("utf-8")


# ── CSV export ──────────────────────────────────────────────────────────


def export_scenes_csv(conn: sqlite3.Connection, project_id: int) -> bytes:
    scenes = _fetch_scenes(conn, project_id)
    scene_elements = _fetch_scene_elements(conn, project_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["scene_number", "slugline", "location", "time_of_day", "page_count", "characters"])
    for scene in scenes:
        characters = [
            e["name"] for e in scene_elements.get(scene["id"], []) if e["category"] == "cast"
        ]
        writer.writerow(
            [
                scene["scene_number"],
                scene["slugline"],
                scene["location"] or "",
                scene["time_of_day"] or "",
                _eighths_to_page_str(scene["page_count_eighths"]),
                "; ".join(characters),
            ]
        )
    return buf.getvalue().encode("utf-8")


def export_elements_csv(conn: sqlite3.Connection, project_id: int) -> bytes:
    elements = conn.execute(
        "SELECT id, name, category FROM element WHERE project_id = ? ORDER BY category, name",
        (project_id,),
    ).fetchall()
    scenes_by_element: dict[int, list[str]] = {}
    for row in conn.execute(
        "SELECT se.element_id, s.scene_number FROM scene_element se "
        "JOIN scene s ON s.id = se.scene_id WHERE s.project_id = ? "
        "ORDER BY s.story_order, s.id",
        (project_id,),
    ):
        scenes_by_element.setdefault(row["element_id"], []).append(row["scene_number"])

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["element_name", "category", "scenes_list"])
    for elem in elements:
        writer.writerow(
            [
                elem["name"],
                CATEGORY_LABELS.get(elem["category"], elem["category"]),
                "; ".join(scenes_by_element.get(elem["id"], [])),
            ]
        )
    return buf.getvalue().encode("utf-8")


def export_csv(conn: sqlite3.Connection, project_id: int) -> tuple[bytes, bytes]:
    """Returns (scenes_csv_bytes, elements_csv_bytes)."""
    return export_scenes_csv(conn, project_id), export_elements_csv(conn, project_id)


# ── PDF breakdown sheets ──────────────────────────────────────────────────


def export_pdf(conn: sqlite3.Connection, project_id: int) -> bytes:
    """One breakdown sheet per scene: header block + element grid by category."""
    project_name = _project_name(conn, project_id)
    scenes = _fetch_scenes(conn, project_id)
    scene_elements = _fetch_scene_elements(conn, project_id)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("BreakdownTitle", parent=styles["Heading1"], fontSize=14, spaceAfter=4)
    meta_style = ParagraphStyle("BreakdownMeta", parent=styles["Normal"], fontSize=10, spaceAfter=2)
    header_cell_style = ParagraphStyle("HeaderCell", parent=styles["Normal"], fontSize=9, textColor=colors.white)
    cell_style = ParagraphStyle("Cell", parent=styles["Normal"], fontSize=9)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        title=f"{project_name} — Breakdown Sheets",
    )

    story = []
    for i, scene in enumerate(scenes):
        story.append(Paragraph(f"Scene {scene['scene_number']}", title_style))
        story.append(Paragraph(scene["slugline"], meta_style))
        story.append(
            Paragraph(
                f"INT/EXT: {scene['interior_exterior'] or '—'}   "
                f"Location: {scene['location'] or '—'}   "
                f"Time: {scene['time_of_day'] or '—'}   "
                f"Pages: {_eighths_to_page_str(scene['page_count_eighths'])}",
                meta_style,
            )
        )
        if scene["synopsis"]:
            story.append(Paragraph(f"Synopsis: {scene['synopsis']}", meta_style))
        story.append(Spacer(1, 0.15 * inch))

        elems = scene_elements.get(scene["id"], [])
        by_category: dict[str, list[str]] = {}
        for e in elems:
            by_category.setdefault(CATEGORY_LABELS.get(e["category"], e["category"]), []).append(e["name"])

        table_data = [[Paragraph("Category", header_cell_style), Paragraph("Elements", header_cell_style)]]
        if by_category:
            for cat_name in sorted(by_category):
                table_data.append(
                    [Paragraph(cat_name, cell_style), Paragraph(", ".join(sorted(by_category[cat_name])), cell_style)]
                )
        else:
            table_data.append([Paragraph("—", cell_style), Paragraph("No elements tagged", cell_style)])

        table = Table(table_data, colWidths=[1.8 * inch, 4.7 * inch])
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#333333")),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f4f4")]),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(table)

        if i != len(scenes) - 1:
            story.append(PageBreak())

    doc.build(story)
    return buf.getvalue()
