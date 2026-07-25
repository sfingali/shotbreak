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


def main():
    app()


if __name__ == "__main__":
    main()
