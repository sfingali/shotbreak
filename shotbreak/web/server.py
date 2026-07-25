"""FastAPI review UI — a thin presentation layer over core.service.

Every route below calls exactly one core.service function (DESIGN.md §2.2,
§5.1). No route touches core/db.py, core/breakdown_engine.py, or the LLM
layer directly, and no route contains business logic beyond translating an
HTTP request into a service call and a service result into a response. This
is what keeps `web/` genuinely optional.

Configured via two environment variables, read once at import time, since
uvicorn imports this module by dotted path (`shotbreak.web.server:app`) and
can't otherwise be handed constructor args:

  SHOTBREAK_DATA_DIR    default "./data"
  SHOTBREAK_CONFIG_PATH default "config.yaml"
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from shotbreak.core import service

DATA_DIR = Path(os.environ.get("SHOTBREAK_DATA_DIR", "./data"))
CONFIG_PATH = Path(os.environ.get("SHOTBREAK_CONFIG_PATH", "config.yaml"))

_WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(_WEB_DIR / "templates"))

app = FastAPI(title="Shotbreak Review UI")
app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")


def _not_found(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class SettingUpsert(BaseModel):
    scope: str
    scope_key: str = ""
    key: str
    value: str


class BreakdownRunRequest(BaseModel):
    passes: list[str] = ["extract", "coreference"]
    provider_overrides: dict[str, str] | None = None


class ElementUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    element_type: str | None = None


class ExportRequest(BaseModel):
    format: Literal["sex", "mms10", "fdx", "fadein", "csv", "pdf"]
    strict: bool = False


# ---------------------------------------------------------------------------
# SPA
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request, project_id: int | None = None):
    """Server-rendered shell with embedded JSON for the initial view — vanilla
    JS (static/app.js) takes over navigation from there via fetch() calls
    against the /api routes below."""
    initial_data: dict = {"projects": service.list_projects(data_dir=DATA_DIR)}

    if project_id is not None:
        try:
            initial_data["project"] = service.get_project(project_id, data_dir=DATA_DIR)
            initial_data["scenes"] = service.list_scenes(project_id, data_dir=DATA_DIR)
            initial_data["elements"] = service.list_elements(project_id, data_dir=DATA_DIR)
        except ValueError:
            initial_data["project"] = None

    return templates.TemplateResponse(
        request, "index.html", {"initial_data": initial_data}
    )


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


@app.get("/api/projects")
def api_list_projects():
    return service.list_projects(data_dir=DATA_DIR)


@app.post("/api/projects")
async def api_import_project(
    file: UploadFile = File(...),
    name: str | None = Form(None),
    lang: str = Form("en"),
):
    """Upload a screenplay and import it as a new project."""
    uploads_dir = DATA_DIR / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    dest = uploads_dir / file.filename
    if dest.exists():
        dest = uploads_dir / f"{int(time.time())}_{file.filename}"
    dest.write_bytes(await file.read())

    try:
        return service.import_script(dest, project_name=name, lang=lang, data_dir=DATA_DIR)
    except (FileNotFoundError, NotImplementedError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/projects/{project_id}")
def api_get_project(project_id: int):
    try:
        return service.get_project(project_id, data_dir=DATA_DIR)
    except ValueError as e:
        raise _not_found(e)


# ---------------------------------------------------------------------------
# Scenes
# ---------------------------------------------------------------------------


@app.get("/api/projects/{project_id}/scenes")
def api_list_scenes(project_id: int, page: int = 1, page_size: int = 50):
    return service.list_scenes(project_id, page=page, page_size=page_size, data_dir=DATA_DIR)


@app.get("/api/projects/{project_id}/scenes/{scene_id}")
def api_get_scene(project_id: int, scene_id: int):
    try:
        return service.get_scene(project_id, scene_id, data_dir=DATA_DIR)
    except ValueError as e:
        raise _not_found(e)


# ---------------------------------------------------------------------------
# Elements
# ---------------------------------------------------------------------------


@app.get("/api/projects/{project_id}/elements")
def api_list_elements(project_id: int, category: str | None = None):
    return service.list_elements(project_id, category=category, data_dir=DATA_DIR)


@app.get("/api/projects/{project_id}/elements/{elem_id}")
def api_get_element(project_id: int, elem_id: int):
    try:
        return service.get_element(project_id, elem_id, data_dir=DATA_DIR)
    except ValueError as e:
        raise _not_found(e)


@app.put("/api/projects/{project_id}/elements/{elem_id}")
def api_update_element(project_id: int, elem_id: int, body: ElementUpdate):
    try:
        return service.update_element(
            project_id, elem_id, body.model_dump(exclude_none=True), data_dir=DATA_DIR
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Breakdown runs
# ---------------------------------------------------------------------------


@app.post("/api/projects/{project_id}/breakdown/run")
def api_run_breakdown(project_id: int, body: BreakdownRunRequest, background_tasks: BackgroundTasks):
    """Kicks off service.run_breakdown() in the background (Starlette runs a
    sync background task in its threadpool, so this doesn't block the event
    loop — see DESIGN.md §2.3) and returns immediately. The client polls
    GET .../breakdown/status for progress."""
    try:
        service.get_project(project_id, data_dir=DATA_DIR)
    except ValueError as e:
        raise _not_found(e)

    background_tasks.add_task(
        service.run_breakdown,
        project_id=project_id,
        passes=body.passes,
        provider_overrides=body.provider_overrides,
        data_dir=DATA_DIR,
        config_path=CONFIG_PATH,
    )
    return {"status": "started", "project_id": project_id, "passes": body.passes}


@app.get("/api/projects/{project_id}/breakdown/status")
def api_breakdown_status(project_id: int):
    status = service.get_latest_breakdown_status(project_id, data_dir=DATA_DIR)
    if status is None:
        return {"status": "none"}
    return status


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@app.get("/api/projects/{project_id}/settings")
def api_list_settings(project_id: int, scope: str | None = None):
    return service.list_settings(project_id, scope=scope, data_dir=DATA_DIR)


@app.post("/api/projects/{project_id}/settings")
def api_upsert_setting(project_id: int, body: SettingUpsert):
    return service.update_setting(
        project_id, body.scope, body.scope_key, body.key, body.value, data_dir=DATA_DIR
    )


# ---------------------------------------------------------------------------
# Continuity
# ---------------------------------------------------------------------------


@app.get("/api/projects/{project_id}/continuity/breaks")
def api_continuity_breaks(project_id: int, status: str | None = "open"):
    return service.list_continuity_breaks(project_id, data_dir=DATA_DIR, status=status)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@app.post("/api/projects/{project_id}/export")
def api_export(project_id: int, body: ExportRequest):
    output_dir = Path("./exports")
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"project_{project_id}"

    try:
        if body.format in ("sex", "mms10"):
            ext = "sex" if body.format == "sex" else "MMS10"
            out_path = output_dir / f"{stem}.{ext}"
            result = service.export_mms(
                project_id, body.format, data_dir=DATA_DIR, strict=body.strict, output_path=out_path
            )
            return {"files": [result["file_path"]], "warnings": result["warnings"]}
        if body.format == "fdx":
            data = service.export_fdx(project_id, data_dir=DATA_DIR)
            out_path = output_dir / f"{stem}.fdx"
            out_path.write_bytes(data)
            return {"files": [str(out_path)], "warnings": []}
        if body.format == "fadein":
            data = service.export_fadein(project_id, data_dir=DATA_DIR)
            out_path = output_dir / f"{stem}.fadein"
            out_path.write_bytes(data)
            return {"files": [str(out_path)], "warnings": []}
        if body.format == "csv":
            scenes_csv, elements_csv = service.export_csv(project_id, data_dir=DATA_DIR)
            scenes_path = output_dir / f"{stem}_scenes.csv"
            elements_path = output_dir / f"{stem}_elements.csv"
            scenes_path.write_bytes(scenes_csv)
            elements_path.write_bytes(elements_csv)
            return {"files": [str(scenes_path), str(elements_path)], "warnings": []}
        if body.format == "pdf":
            data = service.export_pdf(project_id, data_dir=DATA_DIR)
            out_path = output_dir / f"{stem}_breakdown.pdf"
            out_path.write_bytes(data)
            return {"files": [str(out_path)], "warnings": []}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    raise HTTPException(status_code=400, detail=f"Unknown export format '{body.format}'")

# Mount exports dir for download links
exports_dir = Path("./exports")
exports_dir.mkdir(parents=True, exist_ok=True)
app.mount("/exports", StaticFiles(directory=str(exports_dir)), name="exports")


# ---------------------------------------------------------------------------
# Descriptions
# ---------------------------------------------------------------------------


class DescriptionRequest(BaseModel):
    provider: str = "deepseek"
    scene_ids: list[int] | None = None


@app.post("/api/projects/{project_id}/elements/{elem_id}/bible")
def api_generate_bible(project_id: int, elem_id: int, body: DescriptionRequest):
    try:
        return service.generate_character_bible(
            project_id, elem_id, provider_name=body.provider,
            data_dir=DATA_DIR, config_path=CONFIG_PATH,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/projects/{project_id}/elements/{elem_id}/describe")
def api_generate_scene_descriptions(project_id: int, elem_id: int, body: DescriptionRequest):
    try:
        return service.generate_scene_descriptions(
            project_id, elem_id, provider_name=body.provider,
            scene_ids=body.scene_ids, data_dir=DATA_DIR, config_path=CONFIG_PATH,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Config / Providers
# ---------------------------------------------------------------------------


@app.get("/api/config")
def api_get_config():
    """Return the raw config YAML for editing."""
    try:
        return {"yaml": CONFIG_PATH.read_text(encoding="utf-8"), "path": str(CONFIG_PATH)}
    except FileNotFoundError:
        return {"yaml": "", "path": str(CONFIG_PATH)}


@app.post("/api/config")
def api_save_config(body: dict):
    """Save raw YAML config. Overwrites config.yaml."""
    yaml_text = body.get("yaml", "")
    if not yaml_text.strip():
        raise HTTPException(status_code=400, detail="Config cannot be empty")
    CONFIG_PATH.write_text(yaml_text, encoding="utf-8")
    return {"status": "saved", "path": str(CONFIG_PATH)}


@app.get("/api/providers")
def api_list_providers():
    """Return parsed provider list (name, kind, model) without exposing API keys."""
    import yaml
    providers = []
    default = ""
    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
        cfg = yaml.safe_load(raw) or {}
        default = cfg.get("default_provider", "")
        for name, pc in cfg.get("providers", {}).items():
            providers.append({
                "name": name,
                "kind": pc.get("kind", ""),
                "model": pc.get("model", ""),
                "base_url": pc.get("base_url", ""),
                "has_key": bool(pc.get("api_key", "") and pc["api_key"] not in ("", "placeholder")),
            })
    except Exception:
        pass
    return {"providers": providers, "default": default}
