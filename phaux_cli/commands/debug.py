"""
debug.py — Local debug tools for Phaux paper trading.

Usage:
    phaux debug logs [--lines N] [--filter TEXT]
    phaux debug status
"""

import os
import sys
from pathlib import Path

import click

PROJECT_ROOT = Path(os.environ.get("PHAUX_DIR", Path(__file__).parent.parent.parent))

sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "lib"))

import phaux_common as sc

LOG_FILE = PROJECT_ROOT / "worker.log"


@click.group()
def debug():
    """Debug tools — local logs and system health."""


@debug.command()
@click.option("--lines", "-n", default=50, show_default=True, help="Number of lines to show.")
@click.option("--filter", "grep_filter", default=None, help="Filter output (e.g. 'ORCA', 'error').")
def logs(lines, grep_filter):
    """Show recent log entries from worker.log or stdout."""
    if LOG_FILE.exists():
        all_lines = LOG_FILE.read_text().splitlines()
        selected = all_lines[-lines:] if len(all_lines) > lines else all_lines
        for line in selected:
            if grep_filter is None or grep_filter.lower() in line.lower():
                click.echo(line)
    else:
        click.echo(f"No log file found at {LOG_FILE}")
        click.echo("Logs are written to stdout when running via worker.py")


@debug.command()
def status():
    """Local system health — heartbeats, stale crons, arbiter state."""
    click.echo(f"{'=' * 55}")
    click.echo("  PHAUX LOCAL SYSTEM HEALTH")
    click.echo(f"{'=' * 55}")

    health = sc.load_json(sc.OUTPUTS_DIR / "health-state.json", default={})
    if health:
        click.echo(f"\n📊 Health state:")
        for key, val in health.items():
            click.echo(f"   {key}: {val}")
    else:
        click.echo("\n📊 Health state: no data")

    heartbeats = sc.load_json(sc.OUTPUTS_DIR / "cron-heartbeats.json", default={})
    if heartbeats:
        click.echo(f"\n💓 Heartbeats ({len(heartbeats)} crons):")
        for name, ts in sorted(heartbeats.items()):
            click.echo(f"   {name:20s} {ts}")
    else:
        click.echo("\n💓 Heartbeats: no data")

    stale = sc.check_stale_heartbeats()
    if stale:
        click.echo(f"\n⚠️  Stale crons: {', '.join(stale)}")
    else:
        click.echo("\n✅ All mechanical crons healthy")

    arbiter = sc.load_json(sc.OUTPUTS_DIR / "arbiter-state.json", default={})
    if arbiter:
        equity = arbiter.get("lastEquity", "?")
        peak = arbiter.get("peakEquity", "?")
        click.echo(f"\n📈 Arbiter state:")
        click.echo(f"   Last equity: {equity}")
        click.echo(f"   Peak equity: {peak}")
    else:
        click.echo("\n📈 Arbiter state: no data")

    click.echo(f"\n{'=' * 55}")
