"""
main.py — Click CLI group and command registration.

Usage:
    phaux evaluate       Process pending scanner signals and execute approved trades
    phaux regime         Classify macro market regime (RISK_ON / BASELINE / RISK_OFF)
    phaux review         Portfolio review — check risk rails, write report
    phaux howl           Nightly 10-pillar self-improvement analysis
    phaux whale          Daily copy-trade portfolio review and rebalance
    phaux arena          Study Hyperliquid leaderboard for intelligence
    phaux status         Show current system state (read-only)
    phaux emergency-stop Set RISK_OFF immediately
"""

import click

from phaux_cli.commands.evaluate import evaluate
from phaux_cli.commands.jido import jido
from phaux_cli.commands.regime import regime
from phaux_cli.commands.review import review
from phaux_cli.commands.howl import howl
from phaux_cli.commands.whale import whale
from phaux_cli.commands.arena import arena
from phaux_cli.commands.status import status
from phaux_cli.commands.emergency_stop import emergency_stop
from phaux_cli.commands.debug import debug
from phaux_cli.commands.config import config


@click.group()
@click.version_option(version="2.0.0", prog_name="phaux")
def cli():
    """Phaux — paper-trading perpetual futures system."""


cli.add_command(evaluate)
cli.add_command(jido)
cli.add_command(regime)
cli.add_command(review)
cli.add_command(howl)
cli.add_command(whale)
cli.add_command(arena)
cli.add_command(status)
cli.add_command(emergency_stop)
cli.add_command(debug)
cli.add_command(config)
