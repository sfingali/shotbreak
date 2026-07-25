"""MMS (Movie Magic Scheduling) export writers — .sex and .MMS10 (DESIGN.md §3.5).

Both formats are treated as under-documented legacy formats, not solved ones.
Neither writer here has been validated against a real MMS import — see
docs/format-sex.md and docs/format-mms10.md in the open-movie-schedule project
for what is and isn't confirmed. Each writer carries a FIELD_STATUS map
distinguishing "confirmed" structure (observed in published hex dumps / vendor
documentation) from "inferred" structure (a reasonable guess at what MMS's
importer expects, never round-trip tested). `strict=True` refuses to emit
anything in the inferred category, per DESIGN.md's --strict-mms flag — for
both formats today that means strict mode can only emit the confirmed
skeleton (magic bytes + category list, or the bare XML shell), not full scene
data. Both formats are experimental until a maintainer with an MMS license
confirms a real round-trip import.
"""

from __future__ import annotations

import sqlite3
import xml.etree.ElementTree as ET
from xml.dom import minidom

# Category names confirmed present in real SEX header hex dumps (Obsolete Thor
# blog; see mms-file-formats.md). Order matches the observed dump.
CONFIRMED_MMS_CATEGORIES = [
    "Cast Members",
    "Extras",
    "Stunts",
    "Vehicles",
    "Props",
    "Special Effects",
    "Costumes",
    "Makeup",
    "Livestock",
    "Animal Handler",
    "Music",
    "Sound",
    "Set Dressing",
    "Greenery",
    "Special Equipment",
]

# shotbreak's internal element.category values -> MMS category name.
# Categories not in CONFIRMED_MMS_CATEGORIES above are shotbreak-specific
# extensions (locations, notes, vfx, weapons) with no standard MMS slot;
# they're exported as custom categories appended after the standard list.
DB_CATEGORY_TO_MMS = {
    "cast": "Cast Members",
    "background": "Extras",
    "stunts": "Stunts",
    "vehicles": "Vehicles",
    "props": "Props",
    "practical_fx": "Special Effects",
    "wardrobe": "Costumes",
    "makeup_hair": "Makeup",
    "animals": "Livestock",
    "music": "Music",
    "sound": "Sound",
    "set_dressing": "Set Dressing",
    "greenery": "Greenery",
    "special_equipment": "Special Equipment",
    "vfx": "VFX",
    "weapons": "Props",
    "locations": "Locations",
    "notes": "Notes",
}

# Confirmed-vs-inferred map for the .sex writer. Anything "inferred" is
# refused under strict=True.
SEX_FIELD_STATUS = {
    "magic_bytes": "confirmed",          # SSI*, seen in every published dump
    "category_list": "confirmed",        # null-delimited names, seen in dump
    "header_preamble_bytes": "inferred", # meaning of the 8 bytes after SSI* is unknown; zero-filled here
    "scene_record_structure": "inferred",  # no published sample shows scene data at all
}

MMS10_FIELD_STATUS = {
    "root_shape": "inferred",   # no sample .mmx/.MMS10 file has been obtained
    "scene_elements": "inferred",
    "tagged_elements": "inferred",
}


class ExperimentalFormatError(RuntimeError):
    """Raised when --strict-mms is set but part of the format is still inferred."""


def build_schedule_ir(conn: sqlite3.Connection, project_id: int) -> dict:
    """Build the Open Scheduling Exchange JSON intermediate representation
    (see open-movie-schedule/spec/open-scheduling-exchange.md) from the DB.
    """
    project = conn.execute("SELECT * FROM project WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        raise ValueError(f"No project with id {project_id}")

    elements = conn.execute(
        "SELECT id, name, category FROM element WHERE project_id = ? ORDER BY id", (project_id,)
    ).fetchall()
    element_by_id = {e["id"]: e for e in elements}

    scenes = conn.execute(
        "SELECT id, scene_number, interior_exterior, location, time_of_day, "
        "page_count_eighths, synopsis FROM scene WHERE project_id = ? ORDER BY story_order, id",
        (project_id,),
    ).fetchall()

    scene_elements: dict[int, list[int]] = {}
    for row in conn.execute(
        "SELECT scene_id, element_id FROM scene_element se "
        "JOIN scene s ON s.id = se.scene_id WHERE s.project_id = ?",
        (project_id,),
    ):
        scene_elements.setdefault(row["scene_id"], []).append(row["element_id"])

    mms_categories: list[str] = list(CONFIRMED_MMS_CATEGORIES)
    seen_categories = set(mms_categories)
    for e in elements:
        mms_cat = DB_CATEGORY_TO_MMS.get(e["category"], e["category"].replace("_", " ").title())
        if mms_cat not in seen_categories:
            mms_categories.append(mms_cat)
            seen_categories.add(mms_cat)

    ir_elements = [
        {
            "id": f"e{e['id']}",
            "name": e["name"],
            "category": DB_CATEGORY_TO_MMS.get(e["category"], e["category"].replace("_", " ").title()),
        }
        for e in elements
    ]

    ir_scenes = []
    for s in scenes:
        ir_scenes.append(
            {
                "scene_number": s["scene_number"],
                "slugline": {
                    "interior_exterior": s["interior_exterior"] or "",
                    "location": s["location"] or "",
                    "time_of_day": s["time_of_day"] or "",
                },
                "page_count_eighths": s["page_count_eighths"] or 0,
                "synopsis": s["synopsis"] or "",
                "elements": [
                    {"element_id": f"e{eid}"} for eid in scene_elements.get(s["id"], [])
                    if eid in element_by_id
                ],
            }
        )

    return {
        "metadata": {"title": project["name"], "production_company": ""},
        "categories": [
            {"name": c, "type": "standard" if c in CONFIRMED_MMS_CATEGORIES else "custom"}
            for c in mms_categories
        ],
        "elements": ir_elements,
        "scenes": ir_scenes,
    }


# ── .sex writer ─────────────────────────────────────────────────────────

_MAGIC = b"SSI*"


def render_sex(ir: dict, strict: bool = False) -> tuple[bytes, list[str]]:
    """Render the Open Scheduling Exchange IR to a best-effort .sex file.

    Returns (file_bytes, warnings). Structure beyond the magic bytes and
    category list is inferred, not confirmed against a real MMS import —
    see SEX_FIELD_STATUS and docs/format-sex.md.
    """
    warnings: list[str] = []

    if strict:
        inferred = [k for k, v in SEX_FIELD_STATUS.items() if v == "inferred"]
        raise ExperimentalFormatError(
            "--strict-mms refuses .sex export: the following fields are still "
            f"inferred, not confirmed against a real MMS import: {', '.join(inferred)}. "
            "Re-run without --strict-mms to get a best-effort (experimental) file, "
            "or supply real sample .sex files to confirm the structure first "
            "(see docs/format-sex.md)."
        )

    warnings.append(
        "EXPERIMENTAL: .sex scene-record structure is inferred, not confirmed against "
        "a real MMS import. Header preamble bytes (offset 0x04-0x0D) are zero-filled "
        "placeholders — their real meaning is unknown. Manual QA in MMS required "
        "before trusting this file."
    )

    out = bytearray()
    out += _MAGIC
    out += b"\x00\x00\x00\x00"  # header preamble (inferred/unknown meaning) — zero-filled
    out += b"\x00\x00\x00\x00"
    out += b"\x00\x00"

    for cat in ir["categories"]:
        out += cat["name"].encode("ascii", errors="replace")
        out += b"\x00"
    out += b"\x00"  # double-null terminates the category block

    elements_by_id = {e["id"]: e for e in ir["elements"]}

    for scene in ir["scenes"]:
        out += b"SCENE\x00"
        out += scene["scene_number"].encode("ascii", errors="replace") + b"\x00"
        out += scene["slugline"]["interior_exterior"].encode("ascii", errors="replace") + b"\x00"
        out += scene["slugline"]["location"].encode("ascii", errors="replace") + b"\x00"
        out += scene["slugline"]["time_of_day"].encode("ascii", errors="replace") + b"\x00"
        out += str(scene["page_count_eighths"]).encode("ascii") + b"\x00"
        out += scene["synopsis"].encode("ascii", errors="replace") + b"\x00"

        by_category: dict[str, list[str]] = {}
        for ref in scene["elements"]:
            elem = elements_by_id.get(ref["element_id"])
            if elem is None:
                continue
            by_category.setdefault(elem["category"], []).append(elem["name"])

        for cat_name, names in by_category.items():
            out += cat_name.encode("ascii", errors="replace") + b"\x00"
            for name in names:
                out += name.encode("ascii", errors="replace") + b"\x00"
            out += b"\x00"  # empty name terminates this category's element list

        out += b"\x00"  # empty category name terminates the scene record
        out += b"\n"

    return bytes(out), warnings


def export_sex(conn: sqlite3.Connection, project_id: int, strict: bool = False) -> tuple[bytes, list[str]]:
    ir = build_schedule_ir(conn, project_id)
    return render_sex(ir, strict=strict)


# ── .MMS10 (Screenwriter XML) writer ─────────────────────────────────────


def render_mms10(ir: dict, strict: bool = False) -> tuple[bytes, list[str]]:
    """Render the IR to a best-effort .MMS10 (Screenwriter XML) file.

    No sample .mmx/.MMS10 file has been obtained (see docs/format-mms10.md) —
    every element and attribute name here is a best guess at the documented
    Screenwriter XML shape, not a confirmed schema. `strict=True` refuses to
    emit it at all, since the whole schema is inferred.
    """
    warnings: list[str] = []

    if strict:
        raise ExperimentalFormatError(
            "--strict-mms refuses .MMS10 export: the Screenwriter XML schema has not "
            "been confirmed against a real sample (no .mmx/.MMS10 file obtained — see "
            "docs/format-mms10.md). Re-run without --strict-mms for a best-effort "
            "(experimental) file."
        )

    warnings.append(
        "EXPERIMENTAL: .MMS10 (Screenwriter XML) schema is inferred, not confirmed "
        "against a real sample file. Manual QA in MMS required before trusting this file."
    )

    root = ET.Element(
        "ScreenwriterDocument",
        {
            "Application": "shotbreak",
            "FormatStatus": "experimental-inferred-schema",
        },
    )

    header = ET.SubElement(root, "Header")
    ET.SubElement(header, "Title").text = ir["metadata"]["title"]
    ET.SubElement(header, "ProductionCompany").text = ir["metadata"]["production_company"]

    categories_el = ET.SubElement(root, "Categories")
    for cat in ir["categories"]:
        ET.SubElement(categories_el, "Category", {"Name": cat["name"], "Type": cat["type"]})

    elements_el = ET.SubElement(root, "Elements")
    for elem in ir["elements"]:
        ET.SubElement(
            elements_el, "Element", {"Id": elem["id"], "Name": elem["name"], "Category": elem["category"]}
        )

    scenes_el = ET.SubElement(root, "Scenes")
    for scene in ir["scenes"]:
        scene_el = ET.SubElement(scenes_el, "Scene", {"Number": scene["scene_number"]})
        ET.SubElement(
            scene_el,
            "SceneHeading",
            {
                "InteriorExterior": scene["slugline"]["interior_exterior"],
                "Location": scene["slugline"]["location"],
                "TimeOfDay": scene["slugline"]["time_of_day"],
            },
        )
        ET.SubElement(scene_el, "PageCount", {"Eighths": str(scene["page_count_eighths"])})
        if scene["synopsis"]:
            ET.SubElement(scene_el, "Synopsis").text = scene["synopsis"]
        tagged_el = ET.SubElement(scene_el, "TaggedElements")
        for ref in scene["elements"]:
            ET.SubElement(tagged_el, "ElementRef", {"Id": ref["element_id"]})

    rough = ET.tostring(root, encoding="unicode")
    pretty = minidom.parseString(rough).toprettyxml(indent="  ")
    return pretty.encode("utf-8"), warnings


def export_mms10(conn: sqlite3.Connection, project_id: int, strict: bool = False) -> tuple[bytes, list[str]]:
    ir = build_schedule_ir(conn, project_id)
    return render_mms10(ir, strict=strict)
