"""
arena.py — Arena Strategy Learner command.

Studies Hyperliquid leaderboard for actionable intelligence.
Ported from scripts/waifu-arena-learner.sh.
"""

import os
import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "lib"))

import phaux_common as sc
from phaux_cli.runtime import acquire_command_lock, release_command_lock


@click.command()
@click.option("--dry-run", is_flag=True, help="Analyze without saving changes.")
def arena(dry_run):
    """Study Hyperliquid leaderboard for intelligence."""
    if not acquire_command_lock("arena"):
        click.echo("[arena] Another instance running — skipping")
        return

    try:
        _run(dry_run)
    finally:
        release_command_lock("arena")


def _arena_rpc_leaderboard(limit: int = 50):
    """Fetch live arena leaderboard via Senpi agent-tracker RPC.

    HL /info has no 'leaderboard' request type (always 422), and HL's real
    /leaderboard endpoint requires wallet-signed auth. The arena-monitor
    Supabase edge function returns live ranking data without MCP auth.
    Returns (entries, error_str); entries is a list of dicts or [].
    """
    import json as _json
    import urllib.request

    url = os.environ.get(
        "ARENA_API_URL",
        "https://ypofdvbavcdgseguddey.supabase.co/functions/v1/mcp-server",
    )
    payload = _json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_leaderboard", "arguments": {"sort_by": "roi", "limit": limit}},
    }).encode()
    try:
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = _json.loads(resp.read().decode() or "{}")
        text = body["result"]["content"][0]["text"]
        data = _json.loads(text)
    except Exception as e:
        return [], f"arena RPC failed: {e}"

    lb = data.get("leaderboard") if isinstance(data, dict) else None
    if not isinstance(lb, list):
        return [], f"arena RPC unexpected shape: {str(data)[:120]}"

    # Normalize RPC fields to the shape the analysis below expects.
    normalized = []
    for e in lb:
        if not isinstance(e, dict):
            continue
        roi = e.get("roi", e.get("roe", e.get("roePct", 0)))
        if isinstance(roi, str):
            roi = roi.rstrip("%")
        normalized.append({
            "displayName": e.get("name", e.get("displayName", e.get("ethAddress", ""))),
            "ethAddress": e.get("ethAddress", e.get("slug", "")),
            "roePct": roi,
            "roe": roi,
            "totalPnl": e.get("totalPnl", e.get("pnl", 0)),
            "pnl": e.get("totalPnl", e.get("pnl", 0)),
            "tradeCount": e.get("totalTrades", e.get("tradeCount", 0)),
        })
    return normalized, None


def _run(dry_run: bool):
    click.echo(f"[arena] {sc.now_iso()} starting{' (dry-run)' if dry_run else ''}")

    # Fetch live leaderboard from arena RPC (HL /info has no leaderboard type).
    leaderboard, rpc_err = _arena_rpc_leaderboard(50)
    if rpc_err:
        # Keep the legacy HL call as a last-resort path (will 422, then it's a no-op).
        leaderboard = sc.hl_api({"type": "leaderboard", "limit": 50})
        if isinstance(leaderboard, dict) and leaderboard.get("error"):
            click.echo(f"  Leaderboard API error: {leaderboard.get('error', 'Unknown error')} ({rpc_err})")
            return
        if isinstance(leaderboard, dict):
            leaderboard = leaderboard.get("leaderboard", leaderboard.get("entries", leaderboard.get("data", [])))

    # Our stats
    journal = sc.load_trade_journal()
    our_closes = [t for t in journal if t.get("action") == "CLOSE"]
    our_wins = [t for t in our_closes if float(t.get("realizedPnl", 0)) > 0]
    our_wr = len(our_wins) / len(our_closes) * 100 if our_closes else 0
    our_pnl = sum(float(t.get("realizedPnl", 0)) for t in our_closes)

    click.echo(f"  Our stats: {len(our_closes)} closes, {our_wr:.1f}% WR, ${our_pnl:,.2f} PnL")

    # Parse leaderboard response — HL ranking returns a list of entries
    # Each entry: { "ethAddress": "...", "displayName": "...", "pnl": ..., "roe": ..., "volume": ... }
    if isinstance(leaderboard, list):
        top_traders = leaderboard
    elif isinstance(leaderboard, dict):
        top_traders = leaderboard.get("leaderboard", leaderboard.get("entries", leaderboard.get("data", [])))
        if not isinstance(top_traders, list):
            top_traders = []
    else:
        top_traders = []

    recommendations = []

    if not top_traders:
        click.echo("  No leaderboard data available")
        learnings = {
            "generatedAt": sc.now_iso(),
            "recommendations": [],
            "note": "No leaderboard data available",
        }
        if not dry_run:
            sc.save_json(sc.OUTPUTS_DIR / "arena-learnings.json", learnings)
        return

    click.echo(f"  Leaderboard: {len(top_traders)} entrants")

    # Top 5 stats — HL entries have roe, pnl (or similar fields)
    top5 = top_traders[:5]
    avg_top5_roe = sum(float(t.get("roe", t.get("roePct", 0))) for t in top5) / len(top5) if top5 else 0
    avg_top5_pnl = sum(float(t.get("pnl", t.get("totalPnl", 0))) for t in top5) / len(top5) if top5 else 0

    click.echo(f"  Top 5 avg ROE: {avg_top5_roe:.2f}% | avg PnL: ${avg_top5_pnl:.2f}")
    for i, t in enumerate(top5[:3]):
        name = t.get("displayName", t.get("ethAddress", "?"))
        if len(name) > 16:
            name = name[:16]
        click.echo(f"    #{i + 1} {name} ROE={t.get('roe', t.get('roePct', '0'))}% PnL=${t.get('pnl', t.get('totalPnl', '0'))}")

    # Generate recommendations
    if our_wr < 40 and len(our_closes) >= 10:
        recommendations.append({
            "action": "tighten_scores",
            "confidence": "high",
            "reason": f"Win rate {our_wr:.0f}% < 40% across {len(our_closes)} trades. Tighten entry scores.",
            "risk": "reducing",
        })

    if our_wr > 55 and len(our_closes) >= 10:
        recommendations.append({
            "action": "slightly_loosen",
            "confidence": "medium",
            "reason": f"Win rate {our_wr:.0f}% is strong. Could capture more edge (requires manual approval).",
            "risk": "increasing",
        })

    # Compare our PnL vs leaderboard leaders
    if avg_top5_pnl > 0 and our_pnl < avg_top5_pnl:
        recommendations.append({
            "action": "study_top_strategies",
            "confidence": "medium",
            "reason": f"Leaderboard top 5 avg PnL ${avg_top5_pnl:.2f} vs ours ${our_pnl:.2f}. Study their patterns.",
            "risk": "neutral",
        })

    if len(our_closes) > 50 and our_pnl < 0:
        recommendations.append({
            "action": "reduce_frequency",
            "confidence": "high",
            "reason": f"{len(our_closes)} trades with negative PnL. Over-trading detected.",
            "risk": "reducing",
        })

    recommendations.append({
        "action": "reminder_max_leverage",
        "confidence": "absolute",
        "reason": "Max leverage is 10x. Never increase. Proven across 22 agents.",
        "risk": "rule",
    })

    # Save learnings
    learnings = {
        "generatedAt": sc.now_iso(),
        "totalEntrants": len(top_traders),
        "ourStats": {
            "closes": len(our_closes),
            "winRate": round(our_wr, 1),
            "totalPnl": round(our_pnl, 2),
        },
        "arenaTop5AvgRoe": round(avg_top5_roe, 2),
        "arenaTop5AvgPnl": round(avg_top5_pnl, 2),
        "arenaTop5": [
            {
                "rank": i + 1,
                "name": t.get("displayName", t.get("ethAddress", "")),
                "ethAddress": t.get("ethAddress", ""),
                "roePct": t.get("roe", t.get("roePct")),
                "totalPnl": t.get("pnl", t.get("totalPnl")),
            }
            for i, t in enumerate(top5)
        ],
        "recommendations": recommendations,
        "appliedChanges": [],
    }

    click.echo(f"  Recommendations: {len(recommendations)}")
    for r in recommendations:
        click.echo(f"    [{r['confidence']}] {r['action']}: {r['reason'][:80]}")

    if dry_run:
        click.echo("  DRY-RUN: learnings not saved")
        return

    sc.save_json(sc.OUTPUTS_DIR / "arena-learnings.json", learnings)
    click.echo(f"[arena] {sc.now_iso()} done")
