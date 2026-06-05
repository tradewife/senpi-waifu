"""
emergency_stop.py — Immediate RISK_OFF command.
"""

import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "lib"))

import phaux_common as sc


@click.command("emergency-stop")
@click.option("--reason", default="Manual emergency stop", help="Reason for emergency stop.")
def emergency_stop(reason):
    """Set RISK_OFF immediately and alert via Telegram."""
    click.echo(f"EMERGENCY STOP — {sc.now_iso()}")
    click.echo(f"   Reason: {reason}")

    sc.set_risk_mode("RISK_OFF", reason, updated_by="phaux-emergency")
    click.echo("   Risk mode set to RISK_OFF")

    sc.send_telegram(f"EMERGENCY STOP\nReason: {reason}\nAll entries blocked.")
    click.echo("   Telegram alert sent")
    click.echo("\n   System is now in RISK_OFF mode. No new entries will be accepted.")
