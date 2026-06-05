#!/usr/bin/env python3
"""
SENTINEL v1.0 — Quality Trader Convergence Scanner.

Inverted pipeline:
  1. Find assets where smart-money contribution is accelerating
  2. Check momentum events to see whether quality traders are behind the move
  3. Cross-check top trader presence as a bonus confirmation
  4. Enter only when convergence score clears the threshold

Runs every 3 minutes via APScheduler.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from phaux_common import (
    CONFIG_DIR,
    acquire_lock,
    add_pending_entry,
    current_regime_params,
    get_enabled_strategies,
    is_entries_allowed,
    is_rotation_cooled_down,
    load_json,
    load_trade_journal,
    log,
    now_iso,
    record_heartbeat,
    release_lock,
    save_json,
    send_telegram,
)

SENTINEL_CONFIG_FILE = CONFIG_DIR / "sentinel-config.json"

MAX_RANK = 40
MIN_RANK = 6
MIN_CONTRIBUTION_PCT = 2.0
MIN_CONTRIB_CHANGE_4H = 3.0
MIN_TRADER_COUNT = 25
MOMENTUM_LOOKBACK_MINUTES = 60
QUALITY_TCS = {"elite", "reliable"}
QUALITY_TRP = {"sniper", "aggressive", "balanced"}
MIN_QUALITY_TRADERS = 2
MIN_CONCENTRATION = 0.4
TOP_TRADERS_LIMIT = 30


def load_config() -> dict:
    return load_json(SENTINEL_CONFIG_FILE, default={})


def _as_market_list(result: dict) -> list[dict]:
    data = result.get("data", result)
    if isinstance(data, dict):
        data = data.get("markets", data)
    if isinstance(data, dict):
        data = data.get("markets", [])
    return data if isinstance(data, list) else []


def _as_event_list(result: dict) -> list[dict]:
    data = result.get("data", result)
    if isinstance(data, dict):
        data = data.get("events", data)
    if isinstance(data, dict):
        data = data.get("events", [])
    return data if isinstance(data, list) else []


def _as_top_trader_list(result: dict) -> list[dict]:
    data = result.get("data", result)
    if isinstance(data, dict):
        data = data.get("leaderboard", data)
    if isinstance(data, dict):
        data = data.get("data", data)
    return data if isinstance(data, list) else []


def _safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default=0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def find_rising_assets() -> list[dict]:
    """Scanner depowered — leaderboard fetch disabled. Only evaluator may call MCP."""
    return []


def check_quality_traders(asset: str) -> list[dict]:
    """Scanner depowered — momentum events disabled. Only evaluator may call MCP."""
    return []


def fetch_top_traders() -> list[dict]:
    """Scanner depowered — top traders fetch disabled. Only evaluator may call MCP."""
    return []


def check_top_trader_presence(asset: str, top_traders: list[dict]) -> list[dict]:
    appearances = []
    for trader in top_traders:
        if not isinstance(trader, dict):
            continue
        top_markets = trader.get("top_markets", trader.get("topMarkets", []))
        normalized = []
        if isinstance(top_markets, list):
            for market in top_markets:
                if isinstance(market, dict):
                    normalized.append(
                        market.get(
                            "market", market.get("asset", market.get("token", ""))
                        )
                    )
                else:
                    normalized.append(str(market))
        if asset in normalized:
            appearances.append(
                {
                    "rank": _safe_int(trader.get("rank", 999), 999),
                    "pnl": _safe_float(
                        trader.get("unrealized_pnl", trader.get("unrealizedPnl", 0))
                    ),
                }
            )
    return appearances


def score_signal(
    candidate: dict, quality_traders: list[dict], top_appearances: list[dict]
) -> tuple[int, list[str]]:
    score = 0
    reasons = []

    contrib_change = candidate["contrib_change_4h"]
    if contrib_change >= 20:
        score += 3
        reasons.append(f"SURGING_SM +{contrib_change:.1f}%")
    elif contrib_change >= 10:
        score += 2
        reasons.append(f"FAST_SM +{contrib_change:.1f}%")
    else:
        score += 1
        reasons.append(f"RISING_SM +{contrib_change:.1f}%")

    if candidate["rank"] <= 15:
        score += 2
        reasons.append(f"STRONG_RANK #{candidate['rank']}")
    elif candidate["rank"] <= 25:
        score += 1
        reasons.append(f"MID_RANK #{candidate['rank']}")

    if candidate["trader_count"] >= 100:
        score += 1
        reasons.append(f"DEEP_SM {candidate['trader_count']} traders")

    if abs(candidate["price_chg_4h"]) < 2:
        score += 1
        reasons.append(f"PRICE_LAG {candidate['price_chg_4h']:+.1f}%")

    quality_count = len(quality_traders)
    if quality_count >= 4:
        score += 5
        reasons.append(f"ELITE_CONVERGENCE {quality_count}")
    elif quality_count >= 3:
        score += 4
        reasons.append(f"STRONG_CONVERGENCE {quality_count}")
    elif quality_count >= 2:
        score += 3
        reasons.append(f"CONVERGENCE {quality_count}")

    tier2_count = sum(1 for trader in quality_traders if trader["tier"] == 2)
    if tier2_count >= 2:
        score += 2
        reasons.append(f"TIER2_DOUBLE {tier2_count}")
    elif tier2_count >= 1:
        score += 1
        reasons.append("TIER2_CONFIRMED")

    avg_concentration = (
        sum(trader["concentration"] for trader in quality_traders) / quality_count
        if quality_count
        else 0
    )
    if avg_concentration > 0.7:
        score += 1
        reasons.append(f"HIGH_CONVICTION {avg_concentration:.0%}")

    if top_appearances:
        if len(top_appearances) >= 2:
            score += 2
            reasons.append(f"TOP_CONFIRMED {len(top_appearances)}")
        else:
            score += 1
            reasons.append(f"TOP_PRESENT #{top_appearances[0]['rank']}")

    return score, reasons


def build_dsl_state(
    asset: str,
    direction: str,
    score: int,
    config: dict,
    entry_price: float,
    leverage: float,
) -> dict:
    if score >= 12:
        timeout_min, weak_peak_min, dead_weight_min, floor_roe = 60, 30, 20, -25
    elif score >= 9:
        timeout_min, weak_peak_min, dead_weight_min, floor_roe = 45, 20, 15, -22
    else:
        timeout_min, weak_peak_min, dead_weight_min, floor_roe = 35, 15, 12, -20

    dsl_cfg = config.get("dsl", {})
    return {
        "active": True,
        "asset": asset,
        "direction": direction,
        "entryPrice": entry_price,
        "leverage": leverage,
        "phase": 1,
        "lockMode": dsl_cfg.get("lockMode", "pct_of_high_water"),
        "phase2TriggerRoe": dsl_cfg.get("phase2TriggerRoe", 5),
        "highWaterPrice": entry_price,
        "highWaterRoe": 0,
        "currentTierIndex": -1,
        "currentBreachCount": 0,
        "createdAt": now_iso(),
        "highWaterUpdatedAt": now_iso(),
        "entryMode": "SENTINEL",
        "entryScore": score,
        "phase1": {
            "absoluteFloorRoe": floor_roe,
            "hardTimeoutSec": timeout_min * 60,
            "weakPeakCutSec": weak_peak_min * 60,
            "deadWeightCutMin": dead_weight_min,
        },
        "tiers": dsl_cfg.get("tiers", []),
        "stagnationTp": dsl_cfg.get(
            "stagnationTp",
            {
                "enabled": True,
                "roeMin": 10,
                "hwStaleMin": 40,
            },
        ),
        "_sentinel_version": "1.0",
    }


def count_daily_entries(strategy_key: str) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    journal = load_trade_journal()
    return sum(
        1
        for trade in journal
        if trade.get("action") == "OPEN"
        and trade.get("strategyKey") == strategy_key
        and trade.get("entrySource") == "auto-sentinel"
        and trade.get("recordedAt", "").startswith(today)
    )


def scan() -> bool:
    config = load_config()
    if not config:
        log("SENTINEL: missing config")
        return False
    if not is_entries_allowed():
        log("SENTINEL: entries not allowed — skipping scan")
        return False

    strategies = get_enabled_strategies()
    if not strategies:
        return False
    target_strategy = strategies[0]

    max_entries = config.get("risk", {}).get("maxEntriesPerDay", 5)
    if count_daily_entries(target_strategy["_key"]) >= max_entries:
        log("SENTINEL: daily entry cap reached")
        return False

    cooldown_min = config.get("risk", {}).get("cooldownMinutes", 120)
    candidates = [
        candidate
        for candidate in find_rising_assets()
        if not is_rotation_cooled_down(candidate["token"], cooldown_min)
    ]
    if not candidates:
        return False

    top_traders = fetch_top_traders()
    signals = []
    for candidate in candidates[:5]:
        quality = check_quality_traders(candidate["token"])
        matching = [
            trader
            for trader in quality
            if trader["position_direction"].upper() == candidate["direction"]
        ]
        if len(matching) < MIN_QUALITY_TRADERS:
            continue

        top_appearances = check_top_trader_presence(candidate["token"], top_traders)
        score, reasons = score_signal(candidate, matching, top_appearances)
        signals.append(
            {
                "token": candidate["token"],
                "direction": candidate["direction"],
                "score": score,
                "reasons": reasons,
                "leaderboard": candidate,
                "quality_traders": matching,
                "top_trader_appearances": len(top_appearances),
            }
        )

    min_score = config.get("entry", {}).get("minScore", 8)
    signals = [signal for signal in signals if signal["score"] >= min_score]
    signals.sort(key=lambda signal: signal["score"], reverse=True)
    if not signals:
        return False

    best = signals[0]
    budget = float(target_strategy.get("budget", 1000))
    alloc = current_regime_params().get("allocPctPerSlot", 30) / 100
    base_pct = config.get("entry", {}).get("marginPctBase", 0.25)
    if best["score"] >= 12:
        margin_pct = 0.35
    elif best["score"] >= 10:
        margin_pct = 0.30
    else:
        margin_pct = base_pct
    margin = budget * alloc * (margin_pct / base_pct)

    lev_cfg = config.get("leverage", {})
    leverage = min(
        best["leaderboard"]["max_leverage"],
        lev_cfg.get("default", 10),
        lev_cfg.get("max", 10),
    )
    if leverage < lev_cfg.get("min", 5):
        return False

    asset = best["token"]
    direction = best["direction"]
    log(f"SENTINEL: signal {asset} {direction} score={best['score']}")
    add_pending_entry(
        {
            "asset": asset,
            "direction": direction,
            "autoEntered": False,
            "strategyKey": target_strategy["_key"],
            "margin": margin,
            "leverage": leverage,
            "score": best["score"],
            "source": "sentinel",
            "mode": "SENTINEL",
            "reasons": best["reasons"],
        }
    )
    return True


def main():
    if not acquire_lock("sentinel-scanner"):
        return
    try:
        record_heartbeat("sentinel")
        scan()
    finally:
        release_lock("sentinel-scanner")


if __name__ == "__main__":
    main()
