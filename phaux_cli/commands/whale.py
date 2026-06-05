"""
whale.py — Whale Index Manager command.

Daily copy-trade portfolio review and rebalance using Hyperliquid user stats.
Ported from scripts/waifu-whale-index.sh.
"""

import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "lib"))

import phaux_common as sc
from phaux_cli.runtime import acquire_command_lock, release_command_lock


def _score_trader(t: dict) -> float:
    """Score a trader candidate using weighted formula.

    HL vault/leaderboard entries may have different field names than Senpi.
    Fields used: pnl, roe, volume, accountValue.
    """
    pnl = float(t.get("pnl", t.get("totalPnl", 0)))
    roe = float(t.get("roe", t.get("roePct", 0)))
    volume = float(t.get("volume", 0))
    account_value = float(t.get("accountValue", 0))

    # Normalize to 0-100 range
    pnl_score = min(max(pnl / 100, 0), 100) if pnl > 0 else 0
    roe_score = min(max(roe, 0), 100)
    vol_score = min(volume / 1_000_000 * 10, 100)  # Scale: $1M vol = 10 pts
    size_score = min(account_value / 100_000 * 10, 100)  # Scale: $100k = 10 pts

    return 0.35 * pnl_score + 0.25 * roe_score + 0.20 * vol_score + 0.20 * size_score


@click.command()
@click.option("--dry-run", is_flag=True, help="Analyze without saving changes.")
def whale(dry_run):
    """Daily copy-trade portfolio review and rebalance."""
    if not acquire_command_lock("whale"):
        click.echo("[whale] Another instance running — skipping")
        return

    try:
        _run(dry_run)
    finally:
        release_command_lock("whale")


def _run(dry_run: bool):
    click.echo(f"[whale] {sc.now_iso()} starting{' (dry-run)' if dry_run else ''}")

    state_path = sc.OUTPUTS_DIR / "whale-index-state.json"
    state = sc.load_json(state_path, default={
        "slots": [], "watchlist": {}, "notes": [],
        "budget": 1000, "riskTolerance": "conservative", "targetSlots": 2,
    })
    state["updatedAt"] = sc.now_iso()

    # Discover top traders via HL vaults/leaderboard
    traders_resp = sc.hl_api({"type": "topVaults", "limit": 50})

    # Parse response — can be a list or a dict with nested data
    if isinstance(traders_resp, list):
        top = traders_resp
    elif isinstance(traders_resp, dict):
        if traders_resp.get("error"):
            click.echo(f"  Top vaults API error: {traders_resp['error']}")
            top = []
        else:
            top = traders_resp.get("vaults", traders_resp.get("data", traders_resp.get("entries", [])))
            if not isinstance(top, list):
                top = []
    else:
        top = []

    if not top:
        click.echo("  No trader data available — skipping")
        state["notes"].append(f"{sc.now_iso()}: No vault/leaderboard data available")
        if not dry_run:
            sc.save_json(state_path, state)
        return

    click.echo(f"  Discovery returned {len(top)} traders/vaults")

    # Filter by minimum performance thresholds
    risk = state.get("riskTolerance", "conservative")
    min_pnl_thresholds = {
        "conservative": 1000,
        "moderate": 500,
        "aggressive": 0,
    }
    min_pnl = min_pnl_thresholds.get(risk, 1000)

    allowed = [
        t for t in top
        if isinstance(t, dict) and float(t.get("pnl", t.get("totalPnl", 0))) >= min_pnl
    ]
    click.echo(f"  After risk filter ({risk}, min PnL ${min_pnl}): {len(allowed)} candidates")

    # Score and sort
    scored = [(_score_trader(t), t) for t in allowed]
    scored.sort(key=lambda x: x[0], reverse=True)

    # Exclude already-active traders
    active_addresses = {s.get("traderAddress", "") for s in state.get("slots", [])}
    new_candidates = [
        (s, t) for s, t in scored
        if t.get("address", t.get("ethAddress", t.get("vaultAddress", ""))) not in active_addresses
    ]

    # Monitor existing slots
    for slot in state.get("slots", []):
        addr = slot.get("traderAddress", "")
        trader = next(
            (t for t in top if t.get("address", t.get("ethAddress", t.get("vaultAddress", ""))) == addr),
            None,
        )
        if not trader:
            slot["status"] = "WATCH"
            slot["watchCount"] = slot.get("watchCount", 0) + 1
            click.echo(f"  SLOT {addr[:12]}...: WATCH (trader not found)")
            continue

        rank = next(
            (i + 1 for i, t in enumerate(top)
             if t.get("address", t.get("ethAddress", t.get("vaultAddress", ""))) == addr),
            99,
        )
        slot["lastRank"] = rank
        slot["lastCheckedAt"] = sc.now_iso()

        if rank <= 50:
            slot["status"] = "HOLD"
            slot["watchCount"] = 0
            click.echo(f"  SLOT {addr[:12]}...: HOLD (rank {rank})")
        elif rank <= 75:
            slot["watchCount"] = slot.get("watchCount", 0) + 1
            slot["status"] = "WATCH" if slot["watchCount"] >= 2 else "HOLD"
            click.echo(f"  SLOT {addr[:12]}...: {slot['status']} (rank {rank})")
        else:
            slot["watchCount"] = slot.get("watchCount", 0) + 1
            slot["status"] = "WATCH"
            click.echo(f"  SLOT {addr[:12]}...: WATCH (rank {rank})")

    # Fill empty slots
    target_slots = state.get("targetSlots", 2)
    active_count = sum(1 for s in state.get("slots", []) if s.get("status") in ("HOLD", "WATCH"))
    empty_slots = target_slots - active_count

    if empty_slots > 0 and new_candidates:
        click.echo(f"  Filling {min(empty_slots, len(new_candidates))} empty slot(s)")
        for score, trader in new_candidates[:empty_slots]:
            addr = trader.get("address", trader.get("ethAddress", trader.get("vaultAddress", "")))
            new_slot = {
                "traderAddress": addr,
                "traderLabel": trader.get("label", trader.get("vaultName", "UNKNOWN")),
                "status": "HOLD",
                "watchCount": 0,
                "createdAt": sc.now_iso(),
                "lastCheckedAt": sc.now_iso(),
                "lastRank": next(
                    (i + 1 for i, t in enumerate(top)
                     if t.get("address", t.get("ethAddress", t.get("vaultAddress", ""))) == addr),
                    99,
                ),
                "score": round(score, 1),
                "pnl": trader.get("pnl", trader.get("totalPnl", 0)),
                "roe": trader.get("roe", trader.get("roePct", 0)),
                "accountValue": trader.get("accountValue", 0),
            }
            state["slots"].append(new_slot)
            click.echo(f"    Added: {addr[:12]}... (score={score:.1f})")

    click.echo(f"  Portfolio: {len(state['slots'])} slots")

    if dry_run:
        click.echo("  DRY-RUN: state not saved")
        return

    sc.save_json(state_path, state)
    click.echo(f"[whale] {sc.now_iso()} done")
