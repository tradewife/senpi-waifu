#!/usr/bin/env python3
"""
SUGURU — Multi-timeframe scanner for the phaux paper-trading system.
Pure math: HL candles across 1h/4h/1d, scores on trend/momentum/vol/volume/funding.
Writes top candidates to outputs/suguru-candidates.json. No LLM.

Usage: python3 suguru.py [--scan-only] [--dry-run] [--stale]
"""

import sys, math
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import phaux_common as sc

CORE_SYMBOLS = ["BTC", "ETH", "SOL"]
EXTENDED_SYMBOLS = ["DOGE", "XRP", "ADA", "AVAX", "LINK", "ARB", "OP", "MATIC"]
ATR_CAP_PCT = 8.0
MAX_CANDIDATES = 5
MIN_COMPOSITE = 0.40


def _closes(candles):
    return [float(c.get("c", 0)) for c in candles if float(c.get("c", 0)) > 0]


def _volumes(candles):
    return [float(c.get("v", 0)) for c in candles if float(c.get("v", 0)) > 0]


def _roc(prices, period=10):
    if len(prices) < period + 1:
        return 0.0
    return (prices[-1] - prices[-period - 1]) / prices[-period - 1] * 100


def _atr_pct(candles, period=14):
    if len(candles) < 2:
        return 0.0
    ranges = []
    for i in range(1, min(len(candles), period + 1)):
        h, l = float(candles[-i].get("h", 0)), float(candles[-i].get("l", 0))
        if h > 0 and l > 0:
            ranges.append(h - l)
    closes = _closes(candles)
    ref = closes[-1] if closes else 1.0
    return (sum(ranges) / len(ranges)) / ref * 100 if ranges and ref > 0 else 0.0


def _ema(prices, span=10):
    if len(prices) < span:
        return prices[-1] if prices else 0.0
    k = 2.0 / (span + 1)
    ema = prices[-span]
    for p in prices[-span + 1:]:
        ema = p * k + ema * (1 - k)
    return ema


def scan_symbol(symbol):
    try:
        c1h = sc.hl_get_candles(symbol, "1h", 50)
        c4h = sc.hl_get_candles(symbol, "4h", 50)
        c1d = sc.hl_get_candles(symbol, "1d", 30)
    except Exception as e:
        sc.log(f"SUGURU: candle fetch failed for {symbol}: {e}")
        return None
    if not c1h or len(c1h) < 15:
        return None
    return {"symbol": symbol, "1h": c1h, "4h": c4h, "1d": c1d}


def score_symbol(data):
    sym = data["symbol"]
    closes_1h = _closes(data["1h"])
    closes_4h = _closes(data["4h"]) if data["4h"] else []
    closes_1d = _closes(data["1d"]) if data["1d"] else []
    volumes_1h = _volumes(data["1h"])
    if len(closes_1h) < 15:
        return None

    atr_1h = _atr_pct(data["1h"], 14)
    if atr_1h > ATR_CAP_PCT:
        return None

    dir_1h = "LONG" if closes_1h[-1] > _ema(closes_1h, 10) else "SHORT"
    dir_4h = "LONG" if closes_4h and closes_4h[-1] > _ema(closes_4h, 10) else "SHORT"
    dir_1d = "LONG" if closes_1d and closes_1d[-1] > _ema(closes_1d, 10) else "SHORT"
    dirs = [dir_1h, dir_4h, dir_1d]
    trend_score = 3.0 if len(set(dirs)) == 1 else (1.0 if dirs.count(dir_1h) >= 2 else 0.0)

    roc_1h = abs(_roc(closes_1h, 5))
    roc_4h = abs(_roc(closes_4h, 5)) if len(closes_4h) > 6 else 0.0
    roc_1d = abs(_roc(closes_1d, 3)) if len(closes_1d) > 4 else 0.0
    momentum_score = min((roc_1h * 0.5 + roc_4h * 0.3 + roc_1d * 0.2) / 3.0, 5.0)

    vol_avg = sum(volumes_1h[-10:]) / 10 if len(volumes_1h) >= 10 else 1
    vol_recent = sum(volumes_1h[-3:]) / 3 if len(volumes_1h) >= 3 else 0
    volume_score = min(vol_recent / vol_avg if vol_avg > 0 else 1.0, 3.0)

    funding_score = 0.0
    try:
        fr_data = sc.hl_get_funding_rates(sym)
        if isinstance(fr_data, list) and fr_data:
            fr = float(fr_data[-1].get("fundingRate", 0))
            fr_dir = "SHORT" if fr > 0.0001 else ("LONG" if fr < -0.0001 else None)
            if fr_dir == dir_1h:
                funding_score = 2.0
            elif fr_dir and fr_dir != dir_1h:
                funding_score = -1.0
    except Exception:
        pass

    raw = trend_score * 0.3 + momentum_score * 0.3 + volume_score * 0.2 + (funding_score + 1.0) * 0.2
    composite = min(max(raw / 3.0, 0.0), 1.0)
    return composite, {
        "asset": sym, "direction": dir_1h, "composite_score": round(composite, 4),
        "trend_score": round(trend_score, 2), "momentum_score": round(momentum_score, 2),
        "volume_score": round(volume_score, 2), "funding_score": round(funding_score, 2),
        "atr_1h_pct": round(atr_1h, 2), "roc_1h": round(roc_1h, 3),
        "roc_4h": round(roc_4h, 3), "vol_ratio": round(volume_score, 2),
        "trend_dir": {"1h": dir_1h, "4h": dir_4h, "1d": dir_1d},
    }


def check_stale():
    now = datetime.now(timezone.utc)
    cancelled = []
    for f in sc.POSITION_STATE_DIR.rglob("dsl-*-suguru.json"):
        dsl = sc.load_json(f)
        if not dsl or not dsl.get("active"):
            continue
        try:
            dt = datetime.fromisoformat(dsl.get("createdAt", "").replace("Z", "+00:00"))
            age_min = (now - dt).total_seconds() / 60
        except (ValueError, TypeError):
            age_min = 0
        if age_min > 120:
            dsl["active"] = False
            dsl["closedAt"] = sc.now_iso()
            dsl["closeReason"] = f"suguru_stale:age {int(age_min)}min"
            sc.save_json(f, dsl)
            cancelled.append(dsl.get("asset", "?"))
            sc.send_telegram(f"🗑 SUGURU STALE: {dsl.get('asset', '?')} ({int(age_min)}min)")
    return cancelled


def main():
    if not sc.acquire_lock("suguru"):
        return
    dry_run = "--dry-run" in sys.argv
    try:
        sc.record_heartbeat("suguru")
        universe = CORE_SYMBOLS + EXTENDED_SYMBOLS
        sc.log(f"SUGURU: scanning {len(universe)} assets")

        raw = [scan_symbol(s) for s in universe]
        raw = [d for d in raw if d]

        candidates = []
        for d in raw:
            r = score_symbol(d)
            if r and r[0] >= MIN_COMPOSITE:
                candidates.append(r[1])
        candidates.sort(key=lambda c: c["composite_score"], reverse=True)
        candidates = candidates[:MAX_CANDIDATES]
        sc.log(f"SUGURU: {len(candidates)} candidates from {len(raw)} scanned")

        output = {"timestamp": sc.now_iso(), "regime": sc.load_regime().get("riskMode", "UNKNOWN"),
                  "universe_scanned": len(raw), "candidates": candidates}

        if dry_run:
            for c in candidates:
                sc.log(f"SUGURU DRY-RUN: {c['direction']} {c['asset']} score={c['composite_score']:.2f}")
            return

        sc.save_json(sc.OUTPUTS_DIR / "suguru-candidates.json", output)
        sc.log(f"SUGURU: wrote {len(candidates)} candidates")
    finally:
        sc.release_lock("suguru")


if __name__ == "__main__":
    if "--stale" in sys.argv:
        check_stale()
    else:
        main()
