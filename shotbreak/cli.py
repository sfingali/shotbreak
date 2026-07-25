"""Shotbreak CLI — AI-powered screenplay breakdown tool."""

from pathlib import Path
from typing import Optional

import typer

from shotbreak.core import service

app = typer.Typer(
    name="shotbreak",
    help="AI-powered screenplay breakdown with physical description generation.",
    no_args_is_help=True,
)


@app.command()
def import_script(
    script_path: Path = typer.Argument(
        ..., help="Path to screenplay file (.fountain, .fdx, .pdf)", exists=True
    ),
    name: Optional[str] = typer.Option(
        None, "--name", "-n", help="Project name (defaults to filename)"
    ),
    lang: str = typer.Option("en", "--lang", "-l", help="Script language code"),
    data_dir: Path = typer.Option(
        "./data", "--data-dir", "-d", help="Directory for database and project data"
    ),
):
    """Import a screenplay and parse all scenes."""
    try:
        result = service.import_script(
            path=script_path,
            project_name=name,
            lang=lang,
            data_dir=data_dir,
        )
        typer.echo(f"✓ Imported '{result['project_name']}' ({result['script_format']})")
        typer.echo(f"  Scenes: {result['scene_count']}")
        typer.echo(f"  Characters detected: {result['characters_found']}")
        if result["flashbacks_found"]:
            typer.echo(f"  Flashbacks: {result['flashbacks_found']}")
        if result["flashforwards_found"]:
            typer.echo(f"  Flash-forwards: {result['flashforwards_found']}")
        if result["auto_numbered"]:
            typer.echo(f"  Auto-numbered scenes: {result['auto_numbered']}")
        typer.echo(f"  Database: {result['db_path']}")
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1)
    except NotImplementedError as e:
        typer.echo(f"Not yet implemented: {e}", err=True)
        raise typer.Exit(code=1)


@app.command()
def status(
    data_dir: Path = typer.Option(
        "./data", "--data-dir", "-d", help="Directory for database"
    ),
):
    """Show database status and project list."""
    from shotbreak.core import db as _db

    db_path = data_dir / "shotbreak.db"
    if not db_path.exists():
        typer.echo("No database found. Import a script first: shotbreak import SCRIPT")
        return

    conn = _db.get_db(db_path)
    projects = conn.execute("SELECT id, name, script_format, scene_count FROM project p LEFT JOIN (SELECT project_id, COUNT(*) as scene_count FROM scene GROUP BY project_id) s ON p.id = s.project_id").fetchall()

    if not projects:
        typer.echo("No projects. Import a script: shotbreak import SCRIPT")
        return

    for p in projects:
        scenes = p["scene_count"] or 0
        typer.echo(f"  [{p['id']}] {p['name']} ({p['script_format']}) — {scenes} scenes")


@app.command()
def run(
    project_id: int = typer.Argument(..., help="Project ID to run breakdown passes on"),
    passes: str = typer.Option(
        "extract,coreference", "--passes", "-p", help="Comma-separated passes: extract,coreference"
    ),
    provider: Optional[str] = typer.Option(
        None, "--provider", help="Override the configured provider for every pass in this run"
    ),
    data_dir: Path = typer.Option("./data", "--data-dir", "-d", help="Directory for database"),
    config_path: Path = typer.Option("config.yaml", "--config", "-c", help="Path to config.yaml"),
):
    """Run breakdown passes (extract, coreference) against an imported project."""
    pass_list = [p.strip() for p in passes.split(",") if p.strip()]
    overrides = {p: provider for p in pass_list} if provider else None

    typer.echo(f"Running passes {pass_list} on project {project_id}...")
    try:
        result = service.run_breakdown(
            project_id=project_id,
            passes=pass_list,
            provider_overrides=overrides,
            data_dir=data_dir,
            config_path=config_path,
        )
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1)

    for pass_name, summary in result["passes"].items():
        typer.echo(f"  [{pass_name}] {summary}")
    if result["errors"]:
        typer.echo(
            f"  {len(result['errors'])} error(s) encountered "
            f"(see breakdown_run.error_json for run_id={result['run_id']})",
            err=True,
        )

    status_info = service.get_run_status(result["run_id"], data_dir=data_dir)
    typer.echo(f"✓ Run {result['run_id']} — status: {result['status']}")
    typer.echo(f"  Scenes completed: {status_info['scenes_completed']}/{status_info['scenes_total']}")
    typer.echo(f"  LLM calls: {status_info['llm_calls']}   Cost: ${status_info['total_cost_usd']:.4f}")


@app.command("run-status")
def run_status(
    run_id: int = typer.Argument(..., help="Breakdown run ID"),
    data_dir: Path = typer.Option("./data", "--data-dir", "-d", help="Directory for database"),
):
    """Show progress/status/cost for a breakdown run."""
    try:
        info = service.get_run_status(run_id, data_dir=data_dir)
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Run {info['id']} (project {info['project_id']}) — {info['status']}")
    typer.echo(f"  Passes: {', '.join(info['passes'])}")
    typer.echo(f"  Scenes: {info['scenes_completed']}/{info['scenes_total']}")
    typer.echo(f"  LLM calls: {info['llm_calls']}   Cost: ${info['total_cost_usd']:.4f}")
    if info["errors"]:
        typer.echo(f"  Errors: {len(info['errors'])}")


@app.command()
def export(
    project_id: int = typer.Argument(..., help="Project ID to export"),
    format: str = typer.Option(
        "sex,fdx,fadein,csv,pdf", "--format", "-f",
        help="Comma-separated formats: sex,mms10,fdx,fadein,csv,pdf",
    ),
    output_dir: Path = typer.Option(
        "./exports", "--output-dir", "-o", help="Directory to write export files to"
    ),
    strict_mms: bool = typer.Option(
        False, "--strict-mms",
        help="Refuse to emit unconfirmed/inferred fields in .sex/.MMS10 exports",
    ),
    data_dir: Path = typer.Option("./data", "--data-dir", "-d", help="Directory for database"),
):
    """Export a project's breakdown to one or more deterministic formats."""
    formats = [f.strip().lower() for f in format.split(",") if f.strip()]
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"project_{project_id}"

    for fmt in formats:
        try:
            if fmt in ("sex", "mms10"):
                ext = "sex" if fmt == "sex" else "MMS10"
                out_path = output_dir / f"{stem}.{ext}"
                result = service.export_mms(
                    project_id, fmt, data_dir=data_dir, strict=strict_mms, output_path=out_path
                )
                typer.echo(f"  [{fmt}] wrote {result['file_path']} ({len(result['bytes'])} bytes)")
                for w in result["warnings"]:
                    typer.echo(f"    ! {w}")
            elif fmt == "fdx":
                data = service.export_fdx(project_id, data_dir=data_dir)
                out_path = output_dir / f"{stem}.fdx"
                out_path.write_bytes(data)
                typer.echo(f"  [fdx] wrote {out_path} ({len(data)} bytes)")
            elif fmt == "fadein":
                data = service.export_fadein(project_id, data_dir=data_dir)
                out_path = output_dir / f"{stem}.fadein"
                out_path.write_bytes(data)
                typer.echo(f"  [fadein] wrote {out_path} ({len(data)} bytes)")
            elif fmt == "csv":
                scenes_csv, elements_csv = service.export_csv(project_id, data_dir=data_dir)
                scenes_path = output_dir / f"{stem}_scenes.csv"
                elements_path = output_dir / f"{stem}_elements.csv"
                scenes_path.write_bytes(scenes_csv)
                elements_path.write_bytes(elements_csv)
                typer.echo(f"  [csv] wrote {scenes_path} ({len(scenes_csv)} bytes)")
                typer.echo(f"  [csv] wrote {elements_path} ({len(elements_csv)} bytes)")
            elif fmt == "pdf":
                data = service.export_pdf(project_id, data_dir=data_dir)
                out_path = output_dir / f"{stem}_breakdown.pdf"
                out_path.write_bytes(data)
                typer.echo(f"  [pdf] wrote {out_path} ({len(data)} bytes)")
            else:
                typer.echo(f"  Unknown format '{fmt}', skipping", err=True)
        except Exception as e:
            typer.echo(f"  [{fmt}] Error: {e}", err=True)
            raise typer.Exit(code=1)

    typer.echo(f"✓ Exported project {project_id} to {output_dir}")


@app.command()
def bible(
    project_id: int = typer.Argument(..., help="Project ID"),
    element_id: int = typer.Option(..., "--element-id", "-e", help="Element (character) ID to build bible for"),
    provider: str = typer.Option("deepseek", "--provider", help="LLM provider for bible construction"),
    data_dir: Path = typer.Option("./data", "--data-dir", "-d"),
    config_path: Path = typer.Option("config.yaml", "--config", "-c"),
):
    """Build a character bible by reading the full character arc."""
    try:
        result = service.generate_character_bible(
            project_id, element_id, provider_name=provider,
            data_dir=data_dir, config_path=config_path,
        )
        typer.echo(f"Bible for {result['character']} (bible_id={result['bible_id']})")
        typer.echo(f"  Scenes in arc: {result['scene_count']}")
        typer.echo(f"  Immutable traits: {list(result['immutable'].keys())}")
        if result["unspecified_fields"]:
            typer.echo(f"  Unspecified (needs human input): {', '.join(result['unspecified_fields'])}")
        typer.echo(f"  Tokens: {result['tokens_in']} in / {result['tokens_out']} out")
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1)


@app.command()
def describe(
    project_id: int = typer.Argument(..., help="Project ID"),
    element_id: int = typer.Option(..., "--element-id", "-e", help="Element (character) ID to generate descriptions for"),
    scene_ids: str = typer.Option(None, "--scene-ids", "-s", help="Optional comma-separated scene IDs to scope to"),
    provider: str = typer.Option("deepseek", "--provider", help="LLM provider"),
    data_dir: Path = typer.Option("./data", "--data-dir", "-d"),
    config_path: Path = typer.Option("config.yaml", "--config", "-c"),
):
    """Generate per-scene physical descriptions for a character."""
    sid_list = [int(x.strip()) for x in scene_ids.split(",")] if scene_ids else None
    try:
        result = service.generate_scene_descriptions(
            project_id, element_id, provider_name=provider,
            scene_ids=sid_list, data_dir=data_dir, config_path=config_path,
        )
        typer.echo(f"{result['element']}: {result['scenes_rendered']} rendered, "
                   f"{result.get('scenes_skipped', 0)} skipped, {result['scenes_errored']} errors")
        for err in result.get("errors", []):
            typer.echo(f"  Scene {err['scene_id']}: {err['error']}", err=True)
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1)


@app.command()
def setting(
    project_id: int = typer.Argument(..., help="Project ID"),
    scope: str = typer.Option(..., "--scope", help="Setting scope: global, location, group"),
    scope_key: str = typer.Option("", "--scope-key", help="Location name or group label"),
    key: str = typer.Option(..., "--key", "-k", help="Setting key name"),
    value: str = typer.Option(..., "--value", "-v", help="Setting value/description"),
    data_dir: Path = typer.Option("./data", "--data-dir", "-d"),
):
    """Create or update a hierarchical setting. Changes cascade to dependents."""
    try:
        result = service.update_setting(project_id, scope, scope_key, key, value, data_dir=data_dir)
        typer.echo(f"Setting [{result['setting_id']}] {result['scope']}/{result['scope_key']}/{result['key']} updated")
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1)


@app.command("list-settings")
def list_settings_cmd(
    project_id: int = typer.Argument(..., help="Project ID"),
    scope: str = typer.Option(None, "--scope", help="Filter by scope"),
    data_dir: Path = typer.Option("./data", "--data-dir", "-d"),
):
    """List hierarchical settings for a project."""
    settings_list = service.list_settings(project_id, scope=scope, data_dir=data_dir)
    if not settings_list:
        typer.echo("No settings found.")
        return
    for s in settings_list:
        typer.echo(f"  [{s['id']}] {s['scope']}/{s['scope_key']}/{s['key']}: {s['value'][:80]}")


@app.command("list-breaks")
def list_breaks_cmd(
    project_id: int = typer.Argument(..., help="Project ID"),
    status: str = typer.Option("open", "--status", help="Filter by status: open, resolved, dismissed"),
    data_dir: Path = typer.Option("./data", "--data-dir", "-d"),
):
    """List continuity breaks requiring human review."""
    breaks = service.list_continuity_breaks(project_id, data_dir=data_dir, status=status)
    if not breaks:
        typer.echo("No continuity breaks found.")
        return
    for b in breaks:
        typer.echo(f"  [{b['id']}] {b['break_type']}: {b['description'][:100]}")
    typer.echo(f"  {len(breaks)} total")


def main():
    app()


if __name__ == "__main__":
    main()
