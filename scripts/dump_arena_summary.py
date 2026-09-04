#!/usr/bin/env python3
"""Dump enriched arena learnings summary (cron-safe, no heredoc)."""
import json

d = json.load(open("outputs/arena-learnings.json"))
meta = {k: d.get(k) for k in ("generatedAt", "totalEntrants", "ourStats",
                              "arenaTop5AvgRoe", "arenaTop5AvgPnl")}
print(json.dumps(meta, indent=1))
for e in d.get("arenaTop5", []):
    print(e.get("rank"), e.get("name"), "ROE=" + str(e.get("roePct")),
          "PnL=" + str(e.get("totalPnl")), "pattern=" + str(e.get("pattern", "")))
print(json.dumps(d.get("selfDiagnosis"), indent=1))
print(json.dumps(d.get("anomalyDetection"), indent=1)[:800])
for r in d.get("recommendations", []):
    print("-", r.get("confidence"), r.get("action"), str(r.get("reason", ""))[:140])
