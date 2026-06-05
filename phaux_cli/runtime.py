"""
runtime.py — Shared CLI runtime helpers.

Wraps phaux_common utilities for CLI command use.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure scripts/lib is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

import phaux_common as sc


def acquire_command_lock(name: str) -> bool:
    """Acquire a lock for a CLI command."""
    return sc.acquire_lock(f"phaux-{name}")


def release_command_lock(name: str):
    """Release a CLI command lock."""
    sc.release_lock(f"phaux-{name}")
