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


def main():
    app()


if __name__ == "__main__":
    main()
