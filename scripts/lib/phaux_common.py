"""
phaux_common.py — Shared utilities for the Phaux paper-trading system.

Handles: config loading, state read/write, Hyperliquid API calls,
Vulcan CLI paper trading, Telegram alerts, and position management.
"""

import fcntl
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path


def _load_env_file():
    """Load .env file from PHAUX_DIR or project root into os.environ."""
    env_paths = []
    phaux_dir = os.environ.get("PHAUX_DIR", "")
    if phaux_dir:
        env_paths.append(Path(phaux_dir) / ".env")
    env_paths.append(Path(__file__).parent.parent.parent / ".env")
    for env_file in env_paths:
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = val
            break


_load_env_file()


STATE_DIR = Path(os.environ.get("PHAUX_DIR", Path(__file__).parent.parent.parent))
CONFIG_DIR = STATE_DIR / "config"
POSITION_STATE_DIR = STATE_DIR / "state"
MEMORY_DIR = STATE_DIR / "memory"
OUTPUTS_DIR = STATE_DIR / "outputs"

RISK_REGIME_FILE = CONFIG_DIR / "risk-regime.json"
SCANNER_CONFIG_FILE = CONFIG_DIR / "scanner-config.json"
STRATEGIES_FILE = CONFIG_DIR / "strategies.json"
PENDING_ENTRIES_FILE = POSITION_STATE_DIR / "pending-entries.json"
SCAN_HISTORY_FILE = POSITION_STATE_DIR / "scan-history.json"
TRADE_JOURNAL_FILE = MEMORY_DIR / "trade-journal.json"
BRAIN_POLICY_FILE = CONFIG_DIR / "brain-policy.json"

LOCKFILE_DIR = Path("/tmp/phaux-locks")
TRADE_LOCK_FILE = Path("/tmp/phaux-trade.lock")


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def load_json(path: Path, default=None):
    """Load a JSON file, returning `default` if missing or corrupt."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def save_json(path: Path, data, *, indent=2):
    """Atomically write JSON (write to unique .tmp then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=indent, default=str)
            f.write("\n")
        tmp.rename(path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        with open(path, "w") as f:
            json.dump(data, f, indent=indent, default=str)
            f.write("\n")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Risk regime
# ---------------------------------------------------------------------------


def load_regime() -> dict:
    """Return the full regime config."""
    return load_json(RISK_REGIME_FILE)


def load_brain_policy() -> dict:
    """Return the static brain policy config."""
    return load_json(BRAIN_POLICY_FILE, default={
        "blockedScanners": [],
        "preferredScanners": [],
        "maxSlotsCap": None,
        "maxLeverageCap": None,
        "blockNewEntries": False,
    })


def current_scanner_profile(scanner: str) -> dict:
    """Return the active scanner profile from brain policy."""
    policy = load_brain_policy()
    blocked = policy.get("blockedScanners", [])
    preferred = policy.get("preferredScanners", [])
    return {
        "blocked": scanner.lower() in blocked,
        "preferred": scanner.lower() in preferred,
    }


def current_brain_policy() -> dict:
    """Return brain policy as execution policy dict."""
    return load_brain_policy()


def _apply_brain_policy(params: dict) -> dict:
    """Overlay risk-reducing brain directives on top of regime params."""
    policy = current_brain_policy()
    if not params:
        params = {}
    effective = dict(params)

    if policy.get("blockNewEntries"):
        effective["newEntriesAllowed"] = False
        effective["autoEntryEnabled"] = False

    max_slots_cap = policy.get("maxSlotsCap")
    if isinstance(max_slots_cap, (int, float)) and "maxSlots" in effective:
        effective["maxSlots"] = min(int(effective["maxSlots"]), int(max_slots_cap))

    max_leverage_cap = policy.get("maxLeverageCap")
    if isinstance(max_leverage_cap, (int, float)) and "maxLeverageCrypto" in effective:
        effective["maxLeverageCrypto"] = min(
            float(effective["maxLeverageCrypto"]), float(max_leverage_cap)
        )

    alloc_pct_cap = policy.get("allocPctCap")
    if isinstance(alloc_pct_cap, (int, float)) and "allocPctPerSlot" in effective:
        effective["allocPctPerSlot"] = min(
            float(effective["allocPctPerSlot"]), float(alloc_pct_cap)
        )

    return effective


def current_regime_params() -> dict:
    """Return the active regime's parameter block."""
    regime = load_regime()
    mode = regime.get("riskMode", "BASELINE")
    regimes = regime.get("regimes", {})
    params = regimes.get(mode) or regimes.get("BASELINE", {})
    return _apply_brain_policy(params)


def is_entries_allowed() -> bool:
    params = current_regime_params()
    return params.get("newEntriesAllowed", False)


def is_auto_entry_enabled() -> bool:
    params = current_regime_params()
    return params.get("autoEntryEnabled", False)


def set_risk_mode(mode: str, reason: str, updated_by: str = "phaux-script"):
    """Update the risk regime mode. Only the Risk Arbiter should call this."""
    regime = load_regime()
    regime["riskMode"] = mode
    regime["updatedAt"] = now_iso()
    regime["updatedBy"] = updated_by
    regime["reason"] = reason
    save_json(RISK_REGIME_FILE, regime)


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------


def load_strategies() -> dict:
    return load_json(STRATEGIES_FILE)


def get_enabled_strategies() -> list[dict]:
    """Return list of enabled strategy dicts, each with its key injected."""
    data = load_strategies()
    result = []
    for key, strat in data.get("strategies", {}).items():
        if strat.get("enabled", True):
            strat["_key"] = key
            result.append(strat)
    return result


def get_strategy_state_dir(strategy_key: str) -> Path:
    d = POSITION_STATE_DIR / strategy_key
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_open_positions(strategy_key: str) -> list[dict]:
    """Return list of active DSL state dicts for a strategy."""
    d = get_strategy_state_dir(strategy_key)
    positions = []
    for f in d.glob("dsl-*.json"):
        state = load_json(f)
        if state and state.get("active", False):
            state["_file"] = str(f)
            positions.append(state)
    return positions


def get_all_open_positions() -> list[dict]:
    """Return all active positions across enabled strategies."""
    positions = []
    for strat in get_enabled_strategies():
        positions.extend(get_open_positions(strat["_key"]))
    return positions


def compute_roe_pct(
    entry_price: float, current_price: float, direction: str, leverage: float
) -> float:
    """Compute leverage-adjusted ROE percentage."""
    if entry_price <= 0 or leverage <= 0:
        return 0.0
    if direction.upper() == "LONG":
        pnl_pct = (current_price - entry_price) / entry_price
    else:
        pnl_pct = (entry_price - current_price) / entry_price
    return pnl_pct * leverage * 100


def _position_notional_usd(position: dict) -> float:
    margin = float(position.get("margin", 0) or 0)
    leverage = float(position.get("leverage", 0) or 0)
    if margin > 0 and leverage > 0:
        return abs(margin * leverage)
    size = float(position.get("size", 0) or 0)
    entry_price = float(position.get("entryPrice", 0) or 0)
    if size > 0 and entry_price > 0:
        return abs(size * entry_price)
    return 0.0


def directional_exposure_snapshot(
    *,
    additional_direction: str | None = None,
    additional_margin: float = 0.0,
    additional_leverage: float = 1.0,
    additional_position: bool = True,
) -> dict:
    """Summarize current and projected directional notional exposure."""
    positions = get_all_open_positions()
    long_notional = 0.0
    short_notional = 0.0

    for pos in positions:
        direction = str(pos.get("direction", "")).upper()
        notional = _position_notional_usd(pos)
        if direction == "LONG":
            long_notional += notional
        elif direction == "SHORT":
            short_notional += notional

    additional_notional = max(
        0.0, float(additional_margin or 0) * max(float(additional_leverage or 0), 1.0)
    )
    projected_long = long_notional
    projected_short = short_notional
    if additional_direction:
        if additional_direction.upper() == "LONG":
            projected_long += additional_notional
        elif additional_direction.upper() == "SHORT":
            projected_short += additional_notional

    current_total = long_notional + short_notional
    projected_total = projected_long + projected_short
    projected_open_positions = len(positions) + (
        1 if additional_notional > 0 and additional_position else 0
    )
    return {
        "currentOpenPositions": len(positions),
        "projectedOpenPositions": projected_open_positions,
        "longNotional": round(long_notional, 2),
        "shortNotional": round(short_notional, 2),
        "totalNotional": round(current_total, 2),
        "projectedLongNotional": round(projected_long, 2),
        "projectedShortNotional": round(projected_short, 2),
        "projectedTotalNotional": round(projected_total, 2),
        "projectedLongPct": round(projected_long / projected_total * 100, 2)
        if projected_total > 0
        else 0.0,
        "projectedShortPct": round(projected_short / projected_total * 100, 2)
        if projected_total > 0
        else 0.0,
    }


def check_directional_exposure_limit(
    direction: str,
    additional_margin: float,
    additional_leverage: float,
    *,
    additional_position: bool = True,
) -> tuple[bool, dict]:
    cap_pct = float(load_global_guardrails().get("directionalCapPct", 70) or 70)
    snapshot = directional_exposure_snapshot(
        additional_direction=direction,
        additional_margin=additional_margin,
        additional_leverage=additional_leverage,
        additional_position=additional_position,
    )
    offending_pct = (
        snapshot["projectedLongPct"]
        if direction.upper() == "LONG"
        else snapshot["projectedShortPct"]
    )
    snapshot["capPct"] = cap_pct
    snapshot["offendingPct"] = offending_pct

    if snapshot["projectedOpenPositions"] <= 1:
        return True, snapshot
    return offending_pct <= cap_pct, snapshot


def build_position_playbook_metadata(
    *,
    scanner: str,
    score: int | float = 0,
    margin: float = 0,
    leverage: float = 0,
    reasons: list[str] | None = None,
    sm_snapshot: dict | None = None,
    setup: dict | None = None,
) -> dict:
    scanner_key = str(scanner or "unknown").lower()
    fast_scanners = {"orca", "komodo", "sentinel", "shark", "rhino"}
    dead_weight_min = 20 if scanner_key in fast_scanners else 45
    return {
        "schemaVersion": "1.0",
        "scanner": scanner_key,
        "priority": 50,
        "entry": {
            "score": float(score or 0),
            "marginUsd": round(float(margin or 0), 2),
            "leverage": float(leverage or 0),
            "notionalUsd": round(float(margin or 0) * float(leverage or 0), 2),
        },
        "signal": {
            "reasons": list(reasons or [])[:8],
            "setup": setup or {},
        },
        "smSnapshot": sm_snapshot or {},
        "rotation": {
            "eligible": True,
            "deadWeightMin": dead_weight_min,
            "minHighWaterRoe": 2.0,
            "closeIfNegative": True,
            "priorityGap": 8,
        },
    }


def attach_position_playbook(
    dsl_state: dict,
    *,
    scanner: str,
    margin: float,
    leverage: float,
    score: int | float = 0,
    reasons: list[str] | None = None,
    sm_snapshot: dict | None = None,
    setup: dict | None = None,
) -> dict:
    playbook = build_position_playbook_metadata(
        scanner=scanner, score=score, margin=margin, leverage=leverage,
        reasons=reasons, sm_snapshot=sm_snapshot, setup=setup,
    )
    dsl_state["scanner"] = str(scanner or "unknown").lower()
    dsl_state["margin"] = round(float(margin or dsl_state.get("margin", 0) or 0), 2)
    dsl_state["notionalUsd"] = round(
        dsl_state["margin"] * float(leverage or dsl_state.get("leverage", 0) or 0), 2,
    )
    dsl_state["playbook"] = playbook
    return dsl_state


def count_open_slots(strategy: dict) -> int:
    if strategy.get("gateState", "OPEN") != "OPEN":
        return 0
    max_slots = strategy.get("maxSlots", 2)
    regime_slots = current_regime_params().get("maxSlots")
    if isinstance(regime_slots, (int, float)):
        max_slots = min(max_slots, int(regime_slots))

    policy = current_brain_policy()
    max_slots_cap = policy.get("maxSlotsCap")
    if isinstance(max_slots_cap, (int, float)):
        max_slots = min(max_slots, int(max_slots_cap))

    open_count = len(get_open_positions(strategy["_key"]))
    return max(0, max_slots - open_count)


# ---------------------------------------------------------------------------
# Pending entries queue
# ---------------------------------------------------------------------------


def load_pending_entries() -> list[dict]:
    return load_json(PENDING_ENTRIES_FILE, default=[])


def save_pending_entries(entries: list[dict]):
    save_json(PENDING_ENTRIES_FILE, entries)


def add_pending_entry(entry: dict):
    entries = load_pending_entries()
    entry["queuedAt"] = now_iso()
    policy = load_brain_policy()
    scanner = (
        entry.get("scanner")
        or entry.get("source")
        or entry.get("entryMode")
        or entry.get("mode")
        or "unknown"
    )
    scanner_key = str(scanner).lower()
    entry["brainContext"] = {
        "blockedScanner": scanner_key in policy.get("blockedScanners", []),
        "preferredScanner": scanner_key in policy.get("preferredScanners", []),
    }
    entries.append(entry)
    save_pending_entries(entries)


# ---------------------------------------------------------------------------
# Trade journal
# ---------------------------------------------------------------------------


def load_trade_journal() -> list[dict]:
    return load_json(TRADE_JOURNAL_FILE, default=[])


def record_trade(trade: dict):
    journal = load_trade_journal()
    trade["recordedAt"] = now_iso()
    journal.append(trade)
    save_json(TRADE_JOURNAL_FILE, journal)


def is_rotation_cooled_down(asset: str, cooldown_minutes: int = 45) -> bool:
    journal = load_trade_journal()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)
    for trade in reversed(journal):
        recorded = trade.get("recordedAt", "")
        if not recorded:
            continue
        try:
            trade_time = datetime.fromisoformat(recorded.replace("Z", "+00:00"))
            if trade_time < cutoff:
                break
            if trade.get("action") == "CLOSE" and trade.get("asset") == asset:
                return True
        except (ValueError, TypeError):
            continue
    return False


# ---------------------------------------------------------------------------
# Hard safety gates (non-negotiable)
# ---------------------------------------------------------------------------

DEFAULT_GLOBAL_GUARDRAILS = {
    "dailyLossLimitPct": 10,
    "catastrophicDrawdownPct": 20,
    "maxConsecutiveStopOuts": 4,
    "directionalCapPct": 70,
    "minLeverage": 7,
    "maxLeverage": 10,
    "maxPositionsTotal": 3,
    "perAssetCooldownMinutes": 120,
    "bannedAssetPrefixes": ["xyz:"],
}


def load_global_guardrails() -> dict:
    regime = load_regime()
    guardrails = regime.get("globalGuardrails", {})
    merged = dict(DEFAULT_GLOBAL_GUARDRAILS)
    merged.update({k: v for k, v in guardrails.items() if v is not None})

    user_rules = load_json(CONFIG_DIR / "user-rules.json", default={})
    user_gates = user_rules.get("safety_gates", {})
    for key in (
        "maxPositionsTotal",
        "perAssetCooldownMinutes",
        "directionalCapPct",
        "minLeverage",
        "maxLeverage",
        "bannedAssetPrefixes",
    ):
        if key in user_gates and user_gates[key] is not None:
            merged[key] = user_gates[key]

    return merged


DEFAULT_MIN_SCORES = {
    "orca": 5,
    "mantis": 5,
    "fox": 5,
    "roach": 5,
    "komodo": 5,
    "condor": 5,
    "polar": 5,
    "sentinel": 4,
    "rhino": 4,
}


def load_user_min_scores() -> dict | None:
    user_rules = load_json(CONFIG_DIR / "user-rules.json", default={})
    user_gates = user_rules.get("safety_gates", {})
    scores = user_gates.get("minScores")
    if scores and isinstance(scores, dict):
        return {k: int(v) for k, v in scores.items() if isinstance(v, (int, float))}
    return None


def clamp_leverage(leverage: float) -> int:
    guardrails = load_global_guardrails()
    min_lev = int(guardrails.get("minLeverage", 7))
    max_lev = int(guardrails.get("maxLeverage", 10))
    return max(min_lev, min(max_lev, int(leverage)))


def is_asset_banned(asset: str) -> bool:
    guardrails = load_global_guardrails()
    prefixes = guardrails.get("bannedAssetPrefixes", ["xyz:"])
    asset_lower = str(asset).lower()
    for prefix in prefixes:
        if asset_lower.startswith(prefix.lower()):
            return True
    return False


def check_hard_cooldown(asset: str) -> bool:
    guardrails = load_global_guardrails()
    cooldown_min = int(guardrails.get("perAssetCooldownMinutes", 120))
    return is_rotation_cooled_down(asset, cooldown_min)


# ---------------------------------------------------------------------------
# Hyperliquid API (public, no auth needed)
# ---------------------------------------------------------------------------


def hl_api(payload: dict, *, timeout: int = 15) -> dict:
    """POST to Hyperliquid info API. Returns parsed JSON or error dict."""
    import urllib.request
    import urllib.error

    url = os.environ.get("HL_API_URL", "https://api.hyperliquid.xyz/info")
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": f"HL HTTP {e.code}: {e.reason}", "success": False}
    except urllib.error.URLError as e:
        return {"error": f"HL URL error: {e.reason}", "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


def hl_get_candles(coin: str, interval: str = "1h", limit: int = 50) -> list[dict]:
    """Fetch OHLCV candles from Hyperliquid.

    HL API requires startTime, not limit. We compute startTime from limit.
    Interval must be a string like "1h", "4h", "1d".
    """
    interval_minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
    mins = interval_minutes.get(interval, 60)
    start_ms = int((datetime.now(timezone.utc) - timedelta(minutes=mins * limit)).timestamp() * 1000)
    resp = hl_api({"type": "candleSnapshot", "req": {"coin": coin, "interval": interval, "startTime": start_ms}})
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict) and "error" in resp:
        log(f"hl_get_candles error: {resp['error']}")
        return []
    return []


def hl_get_all_mids() -> dict:
    """Get all mid prices from Hyperliquid."""
    return hl_api({"type": "allMids"})


def hl_get_funding_rates(coin: str) -> dict:
    """Get funding history for a coin."""
    return hl_api({"type": "fundingHistory", "coin": coin})


def hl_get_orderbook(coin: str) -> dict:
    """Get L2 orderbook snapshot."""
    return hl_api({"type": "l2Book", "coin": coin})


def hl_get_user_state(user_address: str) -> dict:
    """Get user account state (positions, margin) from HL."""
    return hl_api({"type": "clearinghouseState", "user": user_address})


# ---------------------------------------------------------------------------
# Vulcan CLI (paper trading)
# ---------------------------------------------------------------------------


def _run_vulcan(args: list[str], *, timeout: int = 30) -> dict:
    """Run a vulcan CLI command, parse JSON output. Returns parsed JSON or error dict."""
    cmd = ["vulcan"] + args + ["-o", "json", "-y"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return {"error": result.stderr.strip() or f"exit {result.returncode}", "success": False}
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "success": False}
    except json.JSONDecodeError:
        return {"error": f"invalid json: {(result.stdout or '')[:200]}", "success": False}
    except FileNotFoundError:
        return {"error": "vulcan CLI not found — install with: cargo install vulcan", "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


def vulcan_get_ticker(symbol: str) -> dict:
    """Get current market ticker from Phoenix via Vulcan."""
    resp = _run_vulcan(["market", "ticker", symbol])
    if isinstance(resp, dict) and resp.get("ok"):
        return resp.get("data", resp)
    return resp


def vulcan_get_candles(symbol: str, interval: str = "1h", limit: int = 50) -> list[dict]:
    """Get OHLCV candles from Phoenix via Vulcan."""
    resp = _run_vulcan(["market", "candles", symbol, "--interval", interval, "--limit", str(limit)])
    if isinstance(resp, dict) and resp.get("ok"):
        data = resp.get("data", [])
        if isinstance(data, list):
            return data
        return data.get("candles", [])
    return []


def vulcan_paper_status() -> dict:
    """Get paper trading account status."""
    resp = _run_vulcan(["paper", "status"])
    if isinstance(resp, dict) and resp.get("ok"):
        return resp.get("data", resp)
    return resp


def vulcan_paper_positions() -> list[dict]:
    """Get all open paper positions."""
    resp = _run_vulcan(["paper", "positions"])
    if isinstance(resp, dict) and resp.get("ok"):
        data = resp.get("data", resp)
        if isinstance(data, dict):
            return data.get("positions", [])
        if isinstance(data, list):
            return data
    return []


def vulcan_paper_fills(limit: int = 50) -> list[dict]:
    """Get recent paper fills."""
    resp = _run_vulcan(["paper", "fills", "--limit", str(limit)])
    if isinstance(resp, dict) and resp.get("ok"):
        data = resp.get("data", resp)
        if isinstance(data, dict):
            return data.get("fills", [])
        if isinstance(data, list):
            return data
    return []


def vulcan_paper_buy(
    symbol: str,
    *,
    notional_usdc: str | None = None,
    tokens: str | None = None,
    tp: str | None = None,
    sl: str | None = None,
) -> dict:
    """Open a paper LONG position."""
    args = ["paper", "buy", symbol]
    if notional_usdc:
        args.extend(["--notional-usdc", notional_usdc])
    if tokens:
        args.extend(["--tokens", tokens])
    if tp:
        args.extend(["--tp", tp])
    if sl:
        args.extend(["--sl", sl])
    return _run_vulcan(args)


def vulcan_paper_sell(
    symbol: str,
    *,
    notional_usdc: str | None = None,
    tokens: str | None = None,
    tp: str | None = None,
    sl: str | None = None,
) -> dict:
    """Open a paper SHORT position or close a LONG."""
    args = ["paper", "sell", symbol]
    if notional_usdc:
        args.extend(["--notional-usdc", notional_usdc])
    if tokens:
        args.extend(["--tokens", tokens])
    if tp:
        args.extend(["--tp", tp])
    if sl:
        args.extend(["--sl", sl])
    return _run_vulcan(args)


def vulcan_paper_cancel(symbol: str, order_id: str | None = None) -> dict:
    """Cancel a paper order."""
    args = ["paper", "cancel", symbol]
    if order_id:
        args.append(order_id)
    return _run_vulcan(args)


def vulcan_paper_set_tpsl(
    symbol: str,
    *,
    tp: str | None = None,
    sl: str | None = None,
) -> dict:
    """Set take-profit and/or stop-loss on a paper position."""
    args = ["paper", "set-tpsl", symbol]
    if tp:
        args.extend(["--tp", tp])
    if sl:
        args.extend(["--sl", sl])
    return _run_vulcan(args)


def vulcan_paper_close(symbol: str, *, tokens: str | None = None) -> dict:
    """Close a paper position by placing opposite side order."""
    resp = _run_vulcan(["paper", "positions"])
    if isinstance(resp, dict) and resp.get("ok"):
        positions = resp.get("data", {}).get("positions", [])
        for pos in positions:
            if pos.get("symbol", "").upper() == symbol.upper():
                side = pos.get("side", "").lower()
                size_tokens = tokens or str(pos.get("size_tokens", 0))
                if side == "long":
                    return vulcan_paper_sell(symbol, tokens=size_tokens)
                elif side == "short":
                    return vulcan_paper_buy(symbol, tokens=size_tokens)
        return {"error": f"No open position for {symbol}", "success": False}
    return resp


def vulcan_paper_init(balance: float = 1000.0) -> dict:
    """Initialize paper account with starting balance."""
    return _run_vulcan(["paper", "init", "--balance", str(balance)])


# ---------------------------------------------------------------------------
# Merged market data (dual source: HL + Vulcan/Phoenix)
# ---------------------------------------------------------------------------


def get_merged_market_data(symbol: str, interval: str = "1h", limit: int = 50) -> dict:
    """Fetch market data from both HL and Vulcan, merge into unified view."""
    hl_candles = hl_get_candles(symbol, interval, limit)
    vx_candles = vulcan_get_candles(symbol, interval, limit)
    ticker = vulcan_get_ticker(symbol)

    # Use Vulcan ticker for live price (Phoenix execution venue)
    live_price = None
    if isinstance(ticker, dict):
        live_price = float(ticker.get("mark_price", ticker.get("mid_price", 0)) or 0)
    if not live_price or live_price <= 0:
        # Fallback to HL mid prices
        mids = hl_get_all_mids()
        if isinstance(mids, dict):
            live_price = float(mids.get(symbol, 0) or 0)

    return {
        "symbol": symbol,
        "livePrice": live_price,
        "ticker": ticker if isinstance(ticker, dict) else {},
        "hlCandles": hl_candles,
        "vxCandles": vx_candles,
        "source": "merged",
    }


# ---------------------------------------------------------------------------
# Atomic trade locking
# ---------------------------------------------------------------------------


@contextmanager
def acquire_trade_lock():
    TRADE_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TRADE_LOCK_FILE, "w") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Locking (prevent cron overlap)
# ---------------------------------------------------------------------------


def acquire_lock(name: str) -> bool:
    LOCKFILE_DIR.mkdir(parents=True, exist_ok=True)
    lockfile = LOCKFILE_DIR / f"{name}.lock"
    if lockfile.exists():
        age = time.time() - lockfile.stat().st_mtime
        if age < 60:
            return False
        log(f"Stale lock for {name} ({age:.0f}s old), removing")
    lockfile.write_text(str(os.getpid()))
    return True


def release_lock(name: str):
    lockfile = LOCKFILE_DIR / f"{name}.lock"
    lockfile.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Cron heartbeat monitoring
# ---------------------------------------------------------------------------

HEARTBEAT_FILE = OUTPUTS_DIR / "cron-heartbeats.json"


def record_heartbeat(cron_name: str):
    heartbeats = load_json(HEARTBEAT_FILE, default={})
    heartbeats[cron_name] = now_iso()
    save_json(HEARTBEAT_FILE, heartbeats)


def check_stale_heartbeats(
    max_stale_minutes: dict[str, int] | None = None,
) -> list[str]:
    defaults = {
        "orca": 12, "mantis": 8, "fox": 8, "roach": 8,
        "komodo": 15, "condor": 12, "polar": 12, "rhino": 12,
        "sentinel": 12, "dsl-runner": 12, "sm-flip": 15,
        "watchdog": 15, "risk-arbiter": 3, "arena": 35,
    }
    if max_stale_minutes:
        defaults.update(max_stale_minutes)

    heartbeats = load_json(HEARTBEAT_FILE, default={})
    now = datetime.now(timezone.utc)
    stale = []

    for cron_name, max_min in defaults.items():
        last_run = heartbeats.get(cron_name)
        if not last_run:
            continue
        try:
            last_time = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
            if (now - last_time).total_seconds() > max_min * 60:
                stale.append(cron_name)
        except (ValueError, TypeError):
            continue

    return stale


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def send_telegram(message: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        import urllib.request

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps(
            {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
        ).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"Telegram send failed: {e}")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr)
