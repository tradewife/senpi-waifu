#!/usr/bin/env python3
"""Refresh arena-learnings.json run metadata for the 2026-08-24 cron re-verification."""
import json
import datetime

p = "/home/kt/projects/phaux/outputs/arena-learnings.json"
with open(p) as f:
    data = json.load(f)

now = datetime.datetime.now(datetime.timezone.utc)
now_s = now.strftime("%Y-%m-%dT%H:%M:%SZ")

meta = data.get("meta", {})
meta["cliRunAt"] = now_s
meta["cliExit"] = "FAILED"
meta["cliError"] = "Leaderboard API error: HL HTTP 422: Unprocessable Entity"
meta["cliErrorDetail"] = (
    "Re-verified " + now.strftime("%Y-%m-%d") + ": arena.py hl_api({\"type\":\"leaderboard\"}) -> HL /info has no "
    "'leaderboard' request type. HL /leaderboard endpoint requires wallet-signed auth. "
    "CLI returns early without writing learnings.")
meta["senpiMCP"] = "UNAUTHENTICATED"
meta["senpiMCPDetail"] = (
    "RE-VERIFIED " + now_s + " (this run): user_get_me -> 401 UNAUTHENTICATED; leaderboard_get_top -> 401 UNAUTHORIZED; "
    "leaderboard_get_status -> 401 UNAUTHORIZED. Auth outage started 2026-08-21/22 STILL ONGOING day 4. "
    "Live arena analysis impossible until Senpi API token regenerated at senpi.ai.")
meta["dataFreshness"] = "STALE (last successful enrichment 2026-05-21; no live data obtainable this run)"
meta["liveData"] = False
meta["mcpEnrichment"] = False
data["meta"] = meta
data["generatedAt"] = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")

with open(p, "w") as f:
    json.dump(data, f, indent=2)

print("updated generatedAt:", data["generatedAt"])
print("updated meta.senpiMCP:", meta["senpiMCP"])
print("recommendations preserved:", len(data["recommendations"]))