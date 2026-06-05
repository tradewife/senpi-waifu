#!/usr/bin/env python3
"""
Phaux Worker — APScheduler-based cron runner for the phaux paper-trading system.

Runs all VPS cron jobs via APScheduler. No git sync, no mcporter, no Senpi MCP.
Child processes use the inherited os.environ directly (HL API and Vulcan are free/public).

Environment variables:
  PHAUX_DIR            — repo root (defaults to /app on Railway, or cwd)
  TELEGRAM_BOT_TOKEN   — optional, for trade alerts
  TELEGRAM_CHAT_ID     — optional
"""

import os
import subprocess
import sys
import datetime as _dt
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from typing import Optional

# Force line-buffered output for Railway log capture (belt-and-suspenders with PYTHONUNBUFFERED)
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(line_buffering=True)

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

STATE_DIR = Path(os.environ.get("PHAUX_DIR", os.environ.get("SENPI_WAIFU_DIR", "/app")))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_py(script: str, args: Optional[list] = None, timeout: int = 120):
    """Run a Python script from the repo, printing output."""
    cmd = ["python3", str(STATE_DIR / script)]
    if args:
        cmd.extend(args)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=os.environ,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        output = f"[TIMEOUT] {script} killed after {timeout}s"
        if e.stdout:
            output += "\n" + e.stdout.decode(errors="replace")[-500:]
        if e.stderr:
            output += "\n" + e.stderr.decode(errors="replace")[-500:]
        print(output, flush=True)
        return
    except Exception as e:
        print(f"[ERROR] {script} raised {type(e).__name__}: {e}", flush=True)
        return
    output = (result.stdout + "\n" + result.stderr).strip()
    if output:
        for line in output.split("\n"):
            print(line, flush=True)
    elif result.returncode != 0:
        print(f"[EXIT {result.returncode}] {script} (no output)", flush=True)


def run_cli(module: str, args: Optional[list] = None, timeout: int = 120):
    """Run a CLI module (e.g. phaux_cli) as a Python submodule."""
    cmd = ["python3", "-m", module]
    if args:
        cmd.extend(args)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=os.environ,
            timeout=timeout,
        )
    except Exception as e:
        print(f"[ERROR] {module} raised {type(e).__name__}: {e}", flush=True)
        return
    output = (result.stdout + "\n" + result.stderr).strip()
    if output:
        for line in output.split("\n"):
            print(line, flush=True)
    elif result.returncode != 0:
        print(f"[EXIT {result.returncode}] {module} (no output)", flush=True)


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------


def job_orca():
    run_py("scripts/vps/orca-scanner-cron.py")


def job_komodo():
    run_py("scripts/vps/komodo-scanner-cron.py")


def job_dsl():
    run_py("scripts/vps/dsl-runner.py")


def job_polar():
    run_py("scripts/vps/polar-scanner-cron.py")


def job_mantis():
    run_py("scripts/vps/mantis-scanner-cron.py")


def job_fox():
    run_py("scripts/vps/fox-scanner-cron.py")


def job_smflip():
    run_py("scripts/vps/sm-flip-cron.py")


def job_condor():
    run_py("scripts/vps/condor-scanner-cron.py")


def job_roach():
    run_py("scripts/vps/roach-scanner-cron.py")


# PAUSED: job_barracuda — BARRACUDA removed per user request
# PAUSED: job_bison     — BISON removed per user request
# PAUSED: job_shark     — SHARK paused (v1.0, -4.3% ROI)


def job_sentinel():
    run_py("scripts/vps/sentinel-scanner-cron.py")


def job_rhino():
    run_py("scripts/vps/rhino-scanner-cron.py")


def job_watchdog():
    run_py("scripts/vps/watchdog-cron.py")


def job_health():
    run_py("scripts/vps/health-check-cron.py")


def job_arena():
    run_py("scripts/vps/arena-monitor.py")


def job_regime():
    """Regime classifier — runs via phaux CLI."""
    run_cli("phaux_cli", ["regime"])


def job_arbiter():
    run_py("scripts/vps/risk-arbiter.py")


def job_reconcile():
    run_py("scripts/vps/reconcile-closes.py")


def job_jido():
    """Autonomous trade executor — runs via phaux CLI."""
    run_cli("phaux_cli", ["jido"])


def job_suguru_scan():
    """Suguru multi-timeframe scan — writes candidates."""
    run_py("scripts/vps/suguru.py", ["--scan-only"])


def job_suguru_decide():
    """Suguru decision engine — picks best candidate."""
    run_py("scripts/vps/suguru_decide.py")


def job_brain():
    """Static brain policy passthrough (no-op config reader)."""
    run_py("scripts/vps/autonomous-brain.py")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("=== Phaux Worker starting ===", flush=True)
    print(f"  STATE_DIR: {STATE_DIR}", flush=True)

    # Ensure required directories exist
    for subdir in ("outputs", "state", "memory"):
        (STATE_DIR / subdir).mkdir(parents=True, exist_ok=True)
    print(f"[startup] Ensured directories: outputs, state, memory under {STATE_DIR}", flush=True)

    scheduler = BlockingScheduler(
        executors={"default": ThreadPoolExecutor(8)},
        job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30},
    )

    # ORCA Dual-Mode Scanner — v1.3: every 3min (was 60s, reduced to prevent fee bleed)
    scheduler.add_job(job_orca, "interval", minutes=3, id="orca")

    # MANTIS Dual-Mode Scanner — every 90s
    scheduler.add_job(job_mantis, "interval", seconds=90, id="mantis")

    # FOX Dual-Mode Scanner — every 90s
    scheduler.add_job(job_fox, "interval", seconds=90, id="fox")

    # ROACH Striker-Only Scanner — every 90s (NEW: v1.0, Stalker disabled)
    scheduler.add_job(job_roach, "interval", seconds=90, id="roach")

    # KOMODO Momentum Scanner — every 5min (offset 1min to avoid pile-up)
    scheduler.add_job(job_komodo, "interval", minutes=5, id="komodo", seconds=60)

    # DSL High Water Runner — every 3min
    scheduler.add_job(job_dsl, "interval", minutes=3, id="dsl")

    # CONDOR Multi-Asset Hunter — every 3min, offset 1min
    scheduler.add_job(job_condor, "interval", minutes=3, id="condor", seconds=60)

    # POLAR ETH Alpha Hunter — every 3min, offset 45s
    scheduler.add_job(job_polar, "interval", minutes=3, id="polar", seconds=45)

    # PAUSED: BARRACUDA — removed
    # PAUSED: BISON      — removed
    # PAUSED: SHARK      — removed

    # SENTINEL Quality Trader Convergence — every 3min, offset 90s
    scheduler.add_job(job_sentinel, "interval", minutes=3, id="sentinel", seconds=90)

    # RHINO Momentum Pyramider — every 3min, offset 150s
    scheduler.add_job(job_rhino, "interval", minutes=3, id="rhino", seconds=150)

    # SM Flip Detector — every 5min
    scheduler.add_job(job_smflip, "interval", minutes=5, id="smflip")

    # Watchdog (margin/liq) — every 5min, offset 2min
    scheduler.add_job(job_watchdog, "interval", minutes=5, id="watchdog", seconds=120)

    # Health Check — every 10min
    scheduler.add_job(job_health, "interval", minutes=10, id="health")

    # Arena Monitor — every 15min
    scheduler.add_job(job_arena, "interval", minutes=15, id="arena")

    # Regime Classifier — every 15min, offset 5min
    scheduler.add_job(job_regime, "interval", minutes=15, id="regime", seconds=300)

    # Risk Arbiter (mechanical safety) — every 30s
    scheduler.add_job(job_arbiter, "interval", seconds=30, id="arbiter")

    # Reconcile closes — every 15min
    scheduler.add_job(job_reconcile, "interval", minutes=15, id="reconcile", seconds=30)

    # JIDO Autonomous Trade Executor — every 5min, offset 90s
    scheduler.add_job(job_jido, "interval", minutes=5, id="jido", seconds=90)

    # SUGURU Multi-TF Scan + Decide — every 30min, offset 7min
    scheduler.add_job(job_suguru_scan, "interval", minutes=30, id="suguru_scan", seconds=420)
    scheduler.add_job(job_suguru_decide, "interval", minutes=30, id="suguru_decide", seconds=450)

    # Brain Policy Passthrough — every 5min (keeps outputs/autonomous-brain.json fresh)
    scheduler.add_job(job_brain, "interval", minutes=5, id="brain", seconds=210)

    print("\nSchedule:", flush=True)
    print("  🐋 ORCA Scanner:    every 3min (v1.3)", flush=True)
    print("  🦗 MANTIS Scanner:  every 90s", flush=True)
    print("  🦊 FOX Scanner:     every 90s", flush=True)
    print("  🪳 ROACH Scanner:   every 90s (striker-only)", flush=True)
    print("  🦎 KOMODO Scanner:  every 5min", flush=True)
    print("  🦅 CONDOR Scanner:  every 3min", flush=True)
    print("  🐻‍❄️ POLAR Scanner:   every 3min", flush=True)
    print("  🛡 SENTINEL Scan:   every 3min", flush=True)
    print("  🦏 RHINO Scan:      every 3min", flush=True)
    print("  🔒 DSL HW Runner:   every 3min", flush=True)
    print("  🔄 SM Flip:         every 5min", flush=True)
    print("  👁  Watchdog:        every 5min", flush=True)
    print("  🏥 Health Check:    every 10min", flush=True)
    print("  📊 Arena Monitor:   every 15min", flush=True)
    print("  🌡  Regime Class:    every 15min", flush=True)
    print("  🚨 Risk Arbiter:    every 30s", flush=True)
    print("  🔃 Reconcile:       every 15min", flush=True)
    print("  ⚡ JIDO Executor:   every 5min", flush=True)
    print("  🔭 SUGURU Scan:     every 30min (scan + decide)", flush=True)
    print("  🧠 Brain Policy:    every 5min (config passthrough)", flush=True)
    print("  [PAUSED] 🦈 SHARK / 🎣 BARRACUDA / 🦬 BISON — removed from schedule", flush=True)
    print(f"\nWorker running — {len(scheduler.get_jobs())} jobs scheduled.\n", flush=True)
    sys.stdout.flush()

    # --- APScheduler error listener ---
    def _on_job_error(event):
        print(f"[ALERT] job {event.job_id} failed: {event.exception}", flush=True)

    def _on_job_missed(event):
        print(f"[ALERT] job {event.job_id} missed its scheduled time", flush=True)

    scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)
    scheduler.add_listener(_on_job_missed, EVENT_JOB_MISSED)

    # --- Heartbeat (1 min for faster observability) ---
    _hb_count = [0]

    def _heartbeat():
        _hb_count[0] += 1
        ts = _dt.datetime.utcnow().strftime("%H:%M:%S")
        print(f"[{ts}] heartbeat #{_hb_count[0]} — scheduler alive", flush=True)

    scheduler.add_job(
        _heartbeat,
        "interval",
        minutes=1,
        id="heartbeat",
        next_run_time=_dt.datetime.utcnow(),
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("Worker stopped.", flush=True)
    finally:
        print("[shutdown] Scheduler terminated — container exiting.", flush=True)


def start_telegram_bot():
    """Start Telegram bot polling in a daemon thread alongside the scheduler."""
    import threading

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("[startup] TELEGRAM_BOT_TOKEN not set — Telegram bot disabled", flush=True)
        return

    try:
        import asyncio
        from dashboard.telegram_bot import create_bot_application, start_polling
    except ImportError:
        print("[startup] dashboard.telegram_bot import failed — Telegram bot disabled", flush=True)
        return

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app = create_bot_application()
        if not app:
            print("[startup] Telegram bot creation returned None", flush=True)
            return
        print("[startup] Telegram bot starting (polling)...", flush=True)
        loop.run_until_complete(start_polling(app))
        loop.run_forever()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    print("[startup] Telegram bot thread launched", flush=True)


if __name__ == "__main__":
    try:
        start_telegram_bot()
        main()
    except Exception as e:
        import traceback

        print(f"[FATAL] Worker crashed: {e}", flush=True)
        traceback.print_exc()
        raise
