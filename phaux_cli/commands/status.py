"""
status.py — Read-only system status summary for Phaux paper trading.
"""

import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "lib"))

import phaux_common as sc


def _calc_roe(pos: dict) -> float:
    """Calculate ROE from vulcan paper position data."""
    entry = float(pos.get("entry_price", 0) or 0)
    mark = float(pos.get("mark_price", 0) or 0)
    if entry <= 0 or mark <= 0:
        return 0.0
    # Direction-aware: LONG gains when mark > entry, SHORT gains when entry > mark
    side = pos.get("side", "").upper()
    if side == "SHORT":
        return (entry - mark) / entry * 100
    return (mark - entry) / entry * 100


def _show_rules():
    rules = sc.load_json(sc.CONFIG_DIR / "user-rules.json", default={})
    if not rules:
        click.echo("No user-rules.json found.")
        return

    click.echo(f"\n{'=' * 50}")
    click.echo(f"  STRATEGIC CEILING — user-rules.json")
    click.echo(f"{'=' * 50}")

    ev = rules.get("evaluate", {})
    click.echo(f"\n📋 Evaluate (Manual):")
    click.echo(f"   minScore: {ev.get('minScore', '?')}")
    click.echo(f"   maxLeverage: {ev.get('maxLeverage', '?')}x (hardcoded 7-10x)")
    click.echo(f"   maxPositions: {ev.get('maxPositions', '?')} (hardcoded cap 3)")
    click.echo(f"   cooldown: {ev.get('cooldownMinutes', '?')}min")

    jido = rules.get("jido", {})
    click.echo(f"\n🤖 Jido (Autonomous):")
    click.echo(f"   roi_threshold: {jido.get('roi_threshold_auto', '?')}")
    click.echo(f"   minScore: {jido.get('minScore', '?')}")
    click.echo(f"   autoExecute: {jido.get('autoExecuteEnabled', '?')}")

    tp = rules.get("fixed_tp_roe", {})
    click.echo(f"\n🎯 Fixed TP: {'ON' if tp.get('enabled') else 'OFF'}")
    if tp.get("enabled") and tp.get("tpRoePct"):
        click.echo(f"   tpRoePct: {tp['tpRoePct']}%")

    sl = rules.get("fixed_sl_roe", {})
    click.echo(f"\n🛑 Fixed SL: {'ON' if sl.get('enabled') else 'OFF'}")
    if sl.get("enabled") and sl.get("slRoePct"):
        click.echo(f"   slRoePct: {sl['slRoePct']}%")

    ptp = rules.get("partial_tp", {})
    click.echo(f"\n📊 Partial TP: {'ON' if ptp.get('enabled') else 'OFF'}")
    if ptp.get("enabled"):
        click.echo(
            f"   TP1: {ptp.get('tp1RoePct', '?')}% / close {ptp.get('tp1ClosePct', '?')}%"
        )
        click.echo(
            f"   TP2: {ptp.get('tp2RoePct', '?')}% / close {ptp.get('tp2ClosePct', '?')}%"
        )

    psl = rules.get("partial_sl", {})
    click.echo(f"📉 Partial SL: {'ON' if psl.get('enabled') else 'OFF'}")
    if psl.get("enabled"):
        click.echo(
            f"   SL1: {psl.get('sl1RoePct', '?')}% / close {psl.get('sl1ClosePct', '?')}%"
        )
        click.echo(
            f"   SL2: {psl.get('sl2RoePct', '?')}% / close {psl.get('sl2ClosePct', '?')}%"
        )

    dsl = rules.get("dsl_override", {})
    click.echo(f"\n🔧 DSL Override: {'ON' if dsl.get('enabled') else 'OFF'}")

    click.echo(
        f"\n   Updated: {rules.get('updatedAt', '?')} by {rules.get('updatedBy', '?')}"
    )
    click.echo(f"\n{'=' * 50}")


@click.command()
@click.option(
    "--rules", is_flag=True, help="Show strategic parameters from user-rules.json."
)
def status(rules: bool):
    """Show current system state (read-only)."""
    if rules:
        _show_rules()
        return

    # Regime
    regime = sc.load_regime()
    mode = regime.get("riskMode", "UNKNOWN")
    reason = regime.get("reason", "")
    updated_at = regime.get("updatedAt", "?")

    click.echo(f"{'=' * 50}")
    click.echo(f"  PHAUX STATUS — {sc.now_iso()}")
    click.echo(f"{'=' * 50}")

    # Risk regime
    click.echo(f"\n📊 Regime: {mode}")
    click.echo(f"   Reason: {reason[:80]}")
    click.echo(f"   Updated: {updated_at}")

    # Effective params
    params = sc.current_regime_params()
    click.echo(f"\n⚙️  Effective params:")
    click.echo(f"   Max slots: {params.get('maxSlots', '?')}")
    click.echo(f"   Max leverage: {params.get('maxLeverageCrypto', '?')}x")
    click.echo(f"   Alloc/slot: {params.get('allocPctPerSlot', '?')}%")
    click.echo(f"   Auto-entry: {params.get('autoEntryEnabled', '?')}")
    click.echo(f"   Entries allowed: {params.get('newEntriesAllowed', '?')}")

    # Guardrails
    guardrails = sc.load_global_guardrails()
    click.echo(f"\n🛡  Guardrails:")
    click.echo(
        f"   Leverage: {guardrails.get('minLeverage', 7)}-{guardrails.get('maxLeverage', 10)}x"
    )
    click.echo(f"   Max positions: {guardrails.get('maxPositionsTotal', 3)}")
    click.echo(f"   Daily loss limit: {guardrails.get('dailyLossLimitPct', 10)}%")
    click.echo(f"   Catastrophic DD: {guardrails.get('catastrophicDrawdownPct', 20)}%")
    click.echo(f"   Cooldown: {guardrails.get('perAssetCooldownMinutes', 120)}min")

    # Open positions (from vulcan paper trading)
    positions = sc.vulcan_paper_positions()
    click.echo(f"\n📈 Open positions: {len(positions)}")
    for pos in positions:
        symbol = pos.get("symbol", "?")
        side = pos.get("side", "?")
        roe = _calc_roe(pos)
        click.echo(f"   {symbol} {side} ({roe:+.1f}% ROE)")

    # Paper account equity
    paper_status = sc.vulcan_paper_status()
    equity = paper_status.get("equity", paper_status.get("balance", "?"))
    click.echo(f"\n💰 Paper equity: {equity}")

    # Pending entries
    pending = sc.load_pending_entries()
    click.echo(f"\n📋 Pending entries: {len(pending)}")
    for entry in pending[:5]:
        asset = entry.get("asset", entry.get("symbol", "?"))
        scanner = entry.get("scanner", entry.get("source", "?"))
        click.echo(f"   {asset} via {scanner}")
    if len(pending) > 5:
        click.echo(f"   ... and {len(pending) - 5} more")

    # Stale heartbeats
    stale = sc.check_stale_heartbeats()
    if stale:
        click.echo(f"\n⚠️  Stale crons: {', '.join(stale)}")
    else:
        click.echo(f"\n✅ All mechanical crons healthy")

    # Latest report alerts
    report = sc.load_json(sc.OUTPUTS_DIR / "latest-report.json", default={})
    alerts = report.get("alerts", [])
    if alerts:
        click.echo(f"\n🚨 Active alerts:")
        for alert in alerts:
            click.echo(f"   {alert}")

    click.echo(f"\n{'=' * 50}")
