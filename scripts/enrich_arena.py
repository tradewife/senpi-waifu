#!/usr/bin/env python3
"""Enrich outputs/arena-learnings.json with strategyPatterns/anomalyDetection/selfDiagnosis."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
learn_path = ROOT / "outputs" / "arena-learnings.json"
brain_path = ROOT / "outputs" / "autonomous-brain.json"

learn = json.loads(learn_path.read_text())
brain = json.loads(brain_path.read_text())

top5 = learn.get("arenaTop5", [])
profitable = [e for e in top5 if float(e.get("totalPnl", 0)) > 0]
idle = [e for e in top5 if float(e.get("totalPnl", 0)) == 0]
losing = [e for e in top5 if float(e.get("totalPnl", 0)) < 0]

# Strategy patterns: lean vs passive vs losing
learn["strategyPatterns"] = {
    "leanExecution": {
        "members": [e["name"] for e in profitable],
        "avgROE": round(sum(float(e["roePct"]) for e in profitable) / max(len(profitable), 1), 2),
        "insight": "Only 3 of 23 entrants are profitable this week; top-3 ROE spread is tiny (3.28 vs 2.74) — a low-volatility grinding week where small consistent edge wins, not monsters.",
    },
    "passiveIdle": {
        "members": [e["name"] for e in idle],
        "insight": "Mid-table agents are flat (0 trades / 0 PnL) — waiting out the chop. Entering aggressively into this regime without an edge bleeds fees.",
    },
    "drawdown": {
        "members": [e["name"] for e in losing],
        "insight": "Bottom of top-5 is negative — even ranked agents lose this week. Position sizing must survive a negative-expectancy week.",
    },
}

# Anomaly detection
anomalies = []
for e in top5:
    roe = float(e.get("roePct", 0))
    pnl = float(e.get("totalPnl", 0))
    if roe > 0 and pnl < 0:
        anomalies.append({"name": e["name"], "flag": "positive ROE but negative PnL — denominator manipulation (withdrawals)"})
    if roe == 0 and pnl == 0 and e["rank"] <= 5:
        anomalies.append({"name": e["name"], "flag": "ranked top-5 with zero activity — passive/idle entry inflating rank in a weak week"})
learn["anomalyDetection"] = {
    "flags": anomalies,
    "note": "Week is extremely weak: best entrant only 3.28% ROE. Rankings are noise-dominated; do not over-fit to this week's leaders.",
}

# Self-diagnosis from autonomous-brain
arb = brain.get("systemHealth", {}).get("arbiter", {})
policy = brain.get("executionPolicy", {})
our = learn.get("ourStats", {})
wr = our.get("winRate", 0)
critical = None
if our.get("closes", 0) > 0 and wr == 0.0:
    critical = "0% win rate across 6 closes (-$51.11). Entry quality is the binding constraint — system is RISK_ON and firing, but every recent close lost. Review scanner scores and stop placement before adding size."

learn["selfDiagnosis"] = {
    "currentStatus": f"{policy.get('riskMode', 'UNKNOWN')} — entries allowed={policy.get('allowAutoEntry')}, blockNewEntries={policy.get('blockNewEntries')}",
    "equity": arb.get("lastEquity"),
    "peakEquity": arb.get("peakEquity"),
    "drawdownPct": arb.get("drawdownPct"),
    "heartbeatCount": arb.get("heartbeatCount"),
    "lastJidoRun": arb.get("lastJidoRun"),
    "closesTracked": our.get("closes"),
    "winRate": wr,
    "criticalIssue": critical,
}

# Extra recommendation if 0% WR
if critical:
    learn["recommendations"].insert(0, {
        "action": "review_entry_quality",
        "confidence": "high",
        "reason": critical,
        "risk": "critical",
    })

learn_path.write_text(json.dumps(learn, indent=2) + "\n")
print(f"enriched {learn_path}")
print(f"selfDiagnosis: {learn['selfDiagnosis']['currentStatus']}, equity ${learn['selfDiagnosis']['equity']}, DD {learn['selfDiagnosis']['drawdownPct']}%")
print(f"anomalies: {len(anomalies)} | recommendations: {len(learn['recommendations'])}")
