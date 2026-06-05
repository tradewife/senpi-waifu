#!/usr/bin/env python3
"""
suguru_decide.py — Pure math decision engine for suguru candidates.
Weighted scoring: trend 40%, momentum 30%, volume 15%, funding 15%.
Writes suguru-recommendation.json. No LLM.

Usage: python3 suguru_decide.py [--dry-run]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import phaux_common as sc

WEIGHTS = {"trend": 0.40, "momentum": 0.30, "volume": 0.15, "funding": 0.15}
MIN_SCORE = 0.50
DEFAULT_LEV = 8
CAND_FILE = sc.OUTPUTS_DIR / "suguru-candidates.json"
REC_FILE = sc.OUTPUTS_DIR / "suguru-recommendation.json"


def norm(raw, cap):
    return min(max(raw, 0.0), cap) / cap if cap > 0 else 0.0


def score_candidate(c):
    t = norm(c.get("trend_score", 0), 3.0)
    m = norm(c.get("momentum_score", 0), 5.0)
    v = norm(c.get("volume_score", 0), 3.0)
    f = norm(c.get("funding_score", 0) + 1.0, 3.0)
    return t * WEIGHTS["trend"] + m * WEIGHTS["momentum"] + v * WEIGHTS["volume"] + f * WEIGHTS["funding"]


def reasoning(c, ds):
    td = c.get("trend_dir", {})
    align = "aligned" if len(set(td.values())) == 1 else "mixed"
    return f"decision={ds:.2f}; trend {align} ({'/'.join(td.values())}); roc_1h={c.get('roc_1h',0):.2f}%; atr={c.get('atr_1h_pct',0):.1f}%"


def main():
    data = sc.load_json(CAND_FILE)
    if not data or not data.get("candidates"):
        sc.log("suguru-decide: no candidates")
        sc.save_json(REC_FILE, {"timestamp": sc.now_iso(), "recommendation": "SKIP", "reasoning": "No scan data."})
        return

    candidates = data["candidates"]
    scored = sorted([(score_candidate(c), c) for c in candidates], key=lambda x: x[0], reverse=True)
    top_score, top = scored[0]

    if "--dry-run" in sys.argv:
        sc.log(f"suguru-decide DRY-RUN: {top['asset']} {top['direction']} score={top_score:.3f}")
        return

    rec = {"timestamp": sc.now_iso(), "candidates_count": len(candidates)}
    if top_score >= MIN_SCORE:
        rec.update({"recommendation": "TRADE", "asset": top["asset"], "direction": top["direction"],
                     "confidence": round(top_score, 2), "leverage": DEFAULT_LEV,
                     "reasoning": reasoning(top, top_score), "trade_params": {"gss": DEFAULT_LEV}})
    else:
        rec.update({"recommendation": "SKIP",
                     "reasoning": f"Top score {top_score:.2f} < threshold {MIN_SCORE}. Best: {top['asset']} {top['direction']}"})

    sc.save_json(REC_FILE, rec)
    sc.log(f"suguru-decide: {rec['recommendation']} {rec.get('asset','')} (score={top_score:.3f})")

    # Write TRADE recommendation to pending entries queue for Jido to pick up
    if rec.get("recommendation") == "TRADE":
        entry = {
            "asset": rec["asset"],
            "symbol": rec["asset"],
            "direction": rec["direction"],
            "side": rec["direction"],
            "scanner": "suguru",
            "source": "suguru",
            "score": int(rec.get("confidence", 0.5) * 10),
            "totalScore": int(rec.get("confidence", 0.5) * 10),
            "leverage": rec.get("leverage", DEFAULT_LEV),
            "entryMode": "suguru",
        }
        sc.add_pending_entry(entry)
        sc.log(f"suguru-decide: queued {rec['asset']} {rec['direction']} for execution")


if __name__ == "__main__":
    main()
