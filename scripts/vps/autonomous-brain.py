#!/usr/bin/env python3
"""
Autonomous Brain — no-op config passthrough.

Reads config/brain-policy.json and copies it to outputs/autonomous-brain.json
for backward compatibility with consumers. No LLM / Hermes / AI logic.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from phaux_common import (
    acquire_lock,
    release_lock,
    log,
    now_iso,
    load_json,
    save_json,
    record_heartbeat,
    CONFIG_DIR,
    OUTPUTS_DIR,
)

POLICY_FILE = CONFIG_DIR / "brain-policy.json"
OUTPUT_FILE = OUTPUTS_DIR / "autonomous-brain.json"


def main():
    if not acquire_lock("autonomous-brain"):
        return
    try:
        record_heartbeat("brain")
        policy = load_json(POLICY_FILE, default={})
        policy.setdefault("generatedAt", now_iso())
        save_json(OUTPUT_FILE, policy)
        log(f"Brain passthrough: mode={policy.get('mode', 'unknown')}")
    finally:
        release_lock("autonomous-brain")


if __name__ == "__main__":
    main()
