#!/usr/bin/env python3
"""
evaluate.py — Trade Evaluator command (phaux paper-trading version).

Processes queued scanner signals and executes approved trades via vulcan
paper engine. Refactored into TradeEvaluator class with process_queue() method.

Strategic Sovereignty: user-rules.json defines trade-level overrides
(fixed TP/SL ROE, partial exits, DSL params) that are injected AFTER
the 10-gate safety pipeline approves a signal. Strategic rules manage
the TRADE; the Arbiter manages the ACCOUNT. Hardcoded safety floors
(max 3 positions, XYZ ban, 7-10x leverage) cannot be bypassed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from enum import Enum

import click

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "lib"))

import phaux_common as sc
from phaux_cli.runtime import acquire_command_lock, release_command_lock
from phaux_cli.safety import evaluate_entry, GateResult


USER_RULES_FILE = sc.CONFIG_DIR / "user-rules.json"


def build_strategic_overrides(user_rules: dict) -> dict:
    """Build strategic TP/SL/partial overrides from user-rules.json.

    Strategic rules manage individual TRADE behavior (SL ROE, TP ROE,
    partial exits). They NEVER override account-level safety (max positions,
    leverage band, XYZ ban, daily loss limit). Account safety is enforced
    by the 10-gate pipeline in safety.py BEFORE this function is ever called.
    """
    overrides = {}

    fixed_tp = user_rules.get("fixed_tp_roe", {})
    if fixed_tp.get("enabled") and fixed_tp.get("tpRoePct") is not None:
        overrides["strategicTpRoe"] = float(fixed_tp["tpRoePct"])

    fixed_sl = user_rules.get("fixed_sl_roe", {})
    if fixed_sl.get("enabled") and fixed_sl.get("slRoePct") is not None:
        overrides["strategicSlRoe"] = float(fixed_sl["slRoePct"])

    partial_tp = user_rules.get("partial_tp", {})
    if partial_tp.get("enabled"):
        overrides["partialTp"] = {
            "tp1RoePct": float(partial_tp.get("tp1RoePct", 0)),
            "tp1ClosePct": float(partial_tp.get("tp1ClosePct", 50)),
            "tp2RoePct": float(partial_tp.get("tp2RoePct", 0)),
            "tp2ClosePct": float(partial_tp.get("tp2ClosePct", 25)),
        }

    partial_sl = user_rules.get("partial_sl", {})
    if partial_sl.get("enabled"):
        overrides["partialSl"] = {
            "sl1RoePct": float(partial_sl.get("sl1RoePct", 0)),
            "sl1ClosePct": float(partial_sl.get("sl1ClosePct", 50)),
            "sl2RoePct": float(partial_sl.get("sl2RoePct", 0)),
            "sl2ClosePct": float(partial_sl.get("sl2ClosePct", 25)),
        }

    dsl_override = user_rules.get("dsl_override", {})
    if dsl_override.get("enabled"):
        overrides["dslParams"] = dsl_override.get("overrides", {})

    return overrides


# Proven DSL tiers from senpi-skills (across 30+ live agents).
# consecutiveBreachesRequired=3 prevents single-wick kills.
DEFAULT_DSL_TIERS = [
    {"triggerPct": 7, "lockHwPct": 40, "consecutiveBreachesRequired": 3},
    {"triggerPct": 12, "lockHwPct": 55, "consecutiveBreachesRequired": 2},
    {"triggerPct": 15, "lockHwPct": 75, "consecutiveBreachesRequired": 2},
    {"triggerPct": 20, "lockHwPct": 85, "consecutiveBreachesRequired": 1},
]

# Default conviction tiers — Phase 1 timing scaled by entry score.
# senpi-skills v1.2: score-7 tier with tighter dead weight (8 min).
DEFAULT_CONVICTION_TIERS = [
    {
        "minScore": 6,
        "absoluteFloorRoe": -18,
        "hardTimeoutMin": 25,
        "weakPeakCutMin": 12,
        "deadWeightCutMin": 8,
    },
    {
        "minScore": 8,
        "absoluteFloorRoe": -25,
        "hardTimeoutMin": 45,
        "weakPeakCutMin": 20,
        "deadWeightCutMin": 15,
    },
    {
        "minScore": 10,
        "absoluteFloorRoe": -30,
        "hardTimeoutMin": 60,
        "weakPeakCutMin": 30,
        "deadWeightCutMin": 20,
    },
]

DEFAULT_STAGNATION_TP = {"enabled": True, "roeMin": 10, "hwStaleMin": 45}


def _resolve_conviction_tier(score: float, scanner: str) -> dict:
    """Resolve conviction tier from scanner config, falling back to defaults."""
    scanner_key = str(scanner).lower()
    config_file = sc.CONFIG_DIR / f"{scanner_key}-config.json"

    tiers = DEFAULT_CONVICTION_TIERS
    if config_file.exists():
        scanner_cfg = sc.load_json(config_file, default={})
        cfg_tiers = scanner_cfg.get("dsl", {}).get("convictionTiers")
        if cfg_tiers and isinstance(cfg_tiers, list):
            tiers = cfg_tiers

    # Select highest tier whose minScore <= entry score
    selected = tiers[-1]  # default to lowest tier
    for tier in tiers:
        if score >= tier.get("minScore", 0):
            selected = tier
    return selected


def _load_scanner_dsl_config(scanner: str) -> dict:
    """Load scanner-specific DSL config (tiers, stagnationTp, phase2)."""
    scanner_key = str(scanner).lower()
    config_file = sc.CONFIG_DIR / f"{scanner_key}-config.json"

    cfg = {}
    if config_file.exists():
        cfg = sc.load_json(config_file, default={}).get("dsl", {})

    return {
        "tiers": cfg.get("tiers", DEFAULT_DSL_TIERS),
        "stagnationTp": cfg.get("stagnationTp", DEFAULT_STAGNATION_TP),
        "lockMode": cfg.get("lockMode", "pct_of_high_water"),
        "phase2TriggerRoe": cfg.get("phase2TriggerRoe", 7),
    }


def build_dsl_state(
    asset: str,
    direction: str,
    entry_price: float,
    leverage: int,
    margin: float,
    strat_key: str,
    scanner: str,
    score: float,
    strategic: dict,
    wallet: str = "",
) -> dict:
    """Build DSL state file for a newly opened position (DSL v1.1.1 pattern).

    Uses conviction-tiered Phase 1 settings based on entry score. Includes
    wallet + strategyWalletAddress fields so the DSL runner can match state
    to positions.

    Includes strategic overrides from user-rules.json. These are
    trade-level parameters that sit above the DSL defaults but below
    the account-level safety floor.
    """
    tier = _resolve_conviction_tier(score, scanner)
    dsl_cfg = _load_scanner_dsl_config(scanner)

    dsl = {
        "active": True,
        "asset": asset,
        "direction": direction,
        "entryPrice": entry_price,
        "leverage": leverage,
        "margin": margin,
        "strategyKey": strat_key,
        "strategyWalletAddress": wallet,
        "wallet": wallet,
        "scanner": scanner,
        "entryScore": score,
        "size": None,  # Agent MUST set from clearinghouse after entry fills
        "phase": 1,
        "highWaterPrice": None,  # NOT 0 — DSL runner sets from first price update
        "highWaterRoe": None,
        "currentTierIndex": -1,
        "consecutiveBreaches": 0,
        "lockMode": dsl_cfg["lockMode"],
        "phase2TriggerRoe": dsl_cfg["phase2TriggerRoe"],
        "createdAt": sc.now_iso(),
        "highWaterUpdatedAt": sc.now_iso(),
        # Phase 1: conviction-tiered, consecutiveBreachesRequired=3 (NOT 1)
        "phase1": {
            "enabled": True,
            "retraceThreshold": 0.03,
            "consecutiveBreachesRequired": 3,  # Prevents single-wick kills
            "phase1MaxMinutes": tier.get("hardTimeoutMin", 25),
            "weakPeakCutMinutes": tier.get("weakPeakCutMin", 12),
            "deadWeightCutMin": tier.get("deadWeightCutMin", 8),
            "absoluteFloorRoe": tier.get("absoluteFloorRoe", -18),
            "weakPeakCut": {
                "enabled": True,
                "intervalInMinutes": tier.get("weakPeakCutMin", 12),
                "minValue": 3.0,
            },
        },
        # Phase 2: tiered high-water lock
        "phase2": {
            "enabled": True,
            "retraceThreshold": 0.015,
            "consecutiveBreachesRequired": 2,
        },
        # Trailing tiers (proven across 30 agents)
        "tiers": dsl_cfg["tiers"],
        # Stagnation TP: mandatory
        "stagnationTp": dsl_cfg["stagnationTp"],
        # Execution defaults
        "execution": {
            "phase1SlOrderType": "MARKET",
            "phase2SlOrderType": "MARKET",
            "breachCloseOrderType": "MARKET",
        },
        "_waifu_version": "dsl-v1.1.1",
        "_note": "Built by phaux evaluate with DSL v1.1.1 pattern. "
        "wallet + strategyWalletAddress MUST be present or DSL skips position.",
    }

    if "strategicSlRoe" in strategic:
        dsl["strategicSlRoe"] = strategic["strategicSlRoe"]
    if "strategicTpRoe" in strategic:
        dsl["strategicTpRoe"] = strategic["strategicTpRoe"]
    if "partialTp" in strategic:
        dsl["partialTp"] = strategic["partialTp"]
    if "partialSl" in strategic:
        dsl["partialSl"] = strategic["partialSl"]
    if "dslParams" in strategic:
        dsl.update(strategic["dslParams"])

    return dsl


def _compute_tp_sl_prices(
    entry_price: float,
    direction: str,
    strategic: dict,
) -> tuple[str | None, str | None]:
    """Compute absolute TP/SL prices from strategic ROE overrides.

    Returns (tp_price_str, sl_price_str) as strings suitable for vulcan,
    or (None, None) if no strategic overrides are set.
    """
    tp_price = None
    sl_price = None

    if "strategicTpRoe" in strategic and entry_price > 0:
        tp_roe = strategic["strategicTpRoe"] / 100.0  # e.g. 25 -> 0.25
        if direction.upper() == "LONG":
            tp_price = f"{entry_price * (1 + tp_roe):.4f}"
        else:
            tp_price = f"{entry_price * (1 - tp_roe):.4f}"

    if "strategicSlRoe" in strategic and entry_price > 0:
        sl_roe = abs(strategic["strategicSlRoe"]) / 100.0  # e.g. -15 -> 0.15
        if direction.upper() == "LONG":
            sl_price = f"{entry_price * (1 - sl_roe):.4f}"
        else:
            sl_price = f"{entry_price * (1 + sl_roe):.4f}"

    return tp_price, sl_price


class Recommendation(Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    MANUAL_REVIEW = "MANUAL_REVIEW"


@dataclass
class DecisionObject:
    signal: dict
    gate_result: GateResult
    recommendation: Recommendation
    reasons: List[str] = field(default_factory=list)


class TradeEvaluator:
    """
    TradeEvaluator processes pending scanner signals through the 10-gate pipeline
    and returns DecisionObjects with recommendations (APPROVE, REJECT, MANUAL_REVIEW).

    The 10-gate pipeline (safety.py) enforces account-level safety BEFORE any
    strategic overrides are applied. Strategic rules from user-rules.json only
    affect trade-level behavior (TP/SL, partial exits, DSL params).

    Execution uses vulcan paper engine (buy/sell) instead of mcporter.
    """

    PENDING_ENTRIES_FILE = sc.PENDING_ENTRIES_FILE

    MIN_SCORES = dict(sc.DEFAULT_MIN_SCORES)

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.processed: List[dict] = []
        self.remaining: List[dict] = []
        self.user_rules: dict = {}
        self.effective_min_scores: dict = {}

    def process_queue(self) -> List[DecisionObject]:
        """
        Read pending-entries.json and apply the 10 gates.
        Returns a list of DecisionObjects containing signal, gate results, and recommendation.
        """
        decisions: List[DecisionObject] = []

        # Hot-load user rules at the start of every run
        self.user_rules = sc.load_json(USER_RULES_FILE, default={})
        evaluate_rules = self.user_rules.get("evaluate", {})
        user_min_score = evaluate_rules.get("minScore")

        # Start with user-configured per-scanner min scores (from /gates_set)
        user_per_scanner = sc.load_user_min_scores()
        if user_min_score is not None:
            # Legacy global override: apply to all scanners
            self.effective_min_scores = {
                k: int(user_min_score) for k in self.MIN_SCORES
            }
            click.echo(f"[evaluate] user minScore override: {user_min_score}")
        elif user_per_scanner is not None:
            # Per-scanner overrides from safety_gates.minScores
            self.effective_min_scores = dict(self.MIN_SCORES)
            self.effective_min_scores.update(user_per_scanner)
            click.echo(f"[evaluate] user per-scanner minScores: {user_per_scanner}")
        else:
            self.effective_min_scores = dict(self.MIN_SCORES)

        strategic = build_strategic_overrides(self.user_rules)
        active_overrides = list(strategic.keys()) if strategic else []
        if active_overrides:
            click.echo(f"[evaluate] strategic overrides: {', '.join(active_overrides)}")

        pending = sc.load_json(self.PENDING_ENTRIES_FILE, default=[])
        if not pending:
            return decisions

        regime = sc.load_regime()
        mode = regime.get("riskMode", "BASELINE")

        if mode == "RISK_OFF":
            return decisions

        params = sc.current_regime_params()
        strategies = sc.get_enabled_strategies()
        if not strategies:
            return decisions

        strategy = strategies[0]
        strat_key = strategy["_key"]

        for entry in pending:
            # 10-gate safety pipeline runs FIRST (account-level: positions, leverage, XYZ, cooldown)
            gate = evaluate_entry(entry, strategy)
            recommendation = self._determine_recommendation(entry, gate, strategy)
            decision = DecisionObject(
                signal=entry,
                gate_result=gate,
                recommendation=recommendation,
                reasons=list(gate.reasons),
            )
            decisions.append(decision)

            if recommendation == Recommendation.APPROVE:
                # Strategic overrides applied AFTER gates pass (trade-level only)
                self._handle_approval(
                    entry, strategy, gate, strat_key, strategic
                )
                self.processed.append(entry)
            elif recommendation == Recommendation.MANUAL_REVIEW:
                self._handle_manual_review(entry, gate)
                self.remaining.append(entry)
            else:
                self.remaining.append(entry)

        sc.save_json(self.PENDING_ENTRIES_FILE, self.remaining)
        return decisions

    def _determine_recommendation(
        self, entry: dict, gate: GateResult, strategy: dict
    ) -> Recommendation:
        """Determine recommendation based on gate results and additional logic."""
        if not gate.approved:
            return Recommendation.REJECT

        scanner = str(
            entry.get("scanner", entry.get("source", entry.get("entryMode", "unknown")))
        ).lower()

        for s_name in self.MIN_SCORES:
            if s_name in scanner:
                scanner = s_name
                break

        score = float(entry.get("score", entry.get("totalScore", 0)))
        min_score = self.effective_min_scores.get(scanner, 6)

        if score < min_score:
            return Recommendation.REJECT

        # Hardcoded leverage band — CANNOT be overridden by strategic rules
        if gate.clamped_leverage < 7 or gate.clamped_leverage > 10:
            return Recommendation.REJECT

        # Gate 3 (phased): check local strategy config has name + enabled
        strat_name = strategy.get("name", "")
        if not strat_name or not strategy.get("enabled", False):
            return Recommendation.REJECT

        # Gate 5: check brain policy (static) instead of dynamic brain state
        brain = sc.load_brain_policy()
        blocked = brain.get("blockedScanners", []) if isinstance(brain, dict) else []
        if scanner in blocked:
            return Recommendation.REJECT

        return Recommendation.APPROVE

    def _handle_approval(
        self,
        entry: dict,
        strategy: dict,
        gate: GateResult,
        strat_key: str,
        strategic: dict,
    ):
        """Handle an approved signal - execute trade via vulcan paper engine.

        Strategic overrides (TP/SL, partial exits) are injected into the
        position params AFTER the 10-gate pipeline has already enforced
        account-level safety. These trade-level rules manage the position,
        while the Arbiter manages the account.
        """
        asset = entry.get("asset", entry.get("symbol", ""))
        direction = str(
            entry.get("direction", entry.get("side", ""))
        ).upper()
        scanner = str(
            entry.get("scanner", entry.get("source", entry.get("entryMode", "unknown")))
        ).lower()
        score = float(entry.get("score", entry.get("totalScore", 0)))
        leverage = gate.clamped_leverage
        margin = gate.effective_margin

        click.echo(
            f"  APPROVE {asset} {direction} @ {leverage}x (score={score}, scanner={scanner})"
        )

        if strategic:
            parts = []
            if "strategicSlRoe" in strategic:
                parts.append(f"SL={strategic['strategicSlRoe']}%")
            if "strategicTpRoe" in strategic:
                parts.append(f"TP={strategic['strategicTpRoe']}%")
            if "partialTp" in strategic:
                parts.append("partialTP")
            if "partialSl" in strategic:
                parts.append("partialSL")
            if parts:
                click.echo(f"    strategic: {', '.join(parts)}")

        if self.dry_run:
            click.echo(f"    DRY-RUN: would open {asset} {direction} @ {leverage}x")
            return

        try:
            notional = margin * leverage

            # Compute TP/SL prices from strategic overrides for vulcan
            # Note: vulcan needs an entry price for this, which we don't have
            # until after execution. We'll set TP/SL after fill via set_tpsl.
            tp_price = None
            sl_price = None

            with sc.acquire_trade_lock():
                if direction == "LONG":
                    resp = sc.vulcan_paper_buy(
                        asset,
                        notional_usdc=str(notional),
                    )
                else:
                    resp = sc.vulcan_paper_sell(
                        asset,
                        notional_usdc=str(notional),
                    )

            # Extract entry price from vulcan response
            if isinstance(resp, dict) and resp.get("ok"):
                data = resp.get("data", resp)
                entry_price = float(
                    data.get("fill_price", data.get("entry_price", 0))
                )
                click.echo(f"    OPENED: {asset} {direction} @ {entry_price}")

                # Set TP/SL if strategic overrides exist
                if "strategicTpRoe" in strategic or "strategicSlRoe" in strategic:
                    tp_price, sl_price = _compute_tp_sl_prices(
                        entry_price, direction, strategic
                    )
                    if tp_price or sl_price:
                        sc.vulcan_paper_set_tpsl(
                            asset, tp=tp_price, sl=sl_price
                        )
                        if tp_price:
                            click.echo(f"    TP set: {tp_price}")
                        if sl_price:
                            click.echo(f"    SL set: {sl_price}")

                sc.record_trade(
                    {
                        "action": "OPEN",
                        "asset": asset,
                        "direction": direction,
                        "leverage": leverage,
                        "marginUsd": margin,
                        "entrySource": scanner,
                        "strategyKey": strat_key,
                        "score": score,
                        "realizedPnl": 0,
                    }
                )

                # Save DSL state with strategic overrides (DSL v1.1.1 pattern)
                wallet = strategy.get("wallet", "")
                dsl = build_dsl_state(
                    asset,
                    direction,
                    entry_price,
                    leverage,
                    margin,
                    strat_key,
                    scanner,
                    score,
                    strategic,
                    wallet=wallet,
                )
                state_dir = sc.get_strategy_state_dir(strat_key)
                sc.save_json(state_dir / f"dsl-{asset}.json", dsl)

            else:
                error_msg = resp.get("error", resp.get("message", "unknown")) if isinstance(resp, dict) else str(resp)
                click.echo(f"    FAILED: {error_msg}")
        except Exception as e:
            click.echo(f"    ERROR: {e}")

    def _handle_manual_review(self, entry: dict, gate: GateResult):
        """Handle a signal requiring manual review - send Telegram notification."""
        asset = entry.get("asset", entry.get("symbol", ""))
        direction = entry.get("direction", entry.get("side", ""))
        scanner = str(
            entry.get("scanner", entry.get("source", entry.get("entryMode", "unknown")))
        ).lower()
        score = float(entry.get("score", entry.get("totalScore", 0)))

        click.echo(
            f"  MANUAL_REVIEW {asset} {direction} (score={score}, scanner={scanner})"
        )

        sc.send_telegram(
            f"📋 *MANUAL REVIEW REQUIRED*\n"
            f"Scanner: {scanner}\n"
            f"Asset: {asset}\n"
            f"Direction: {direction}\n"
            f"Score: {score}\n"
            f"Reasons: {', '.join(gate.reasons[:4])}"
        )


@click.command()
@click.option(
    "--dry-run", is_flag=True, help="Evaluate signals without executing trades."
)
def evaluate(dry_run):
    """Process pending scanner signals and execute approved trades."""
    if not acquire_command_lock("evaluate"):
        click.echo("[evaluate] Another instance running — skipping")
        return

    try:
        click.echo(
            f"[evaluate] {sc.now_iso()} starting{' (dry-run)' if dry_run else ''}"
        )

        params = sc.current_regime_params()
        regime = sc.load_regime()
        mode = regime.get("riskMode", "BASELINE")
        click.echo(f"[evaluate] regime: {mode}")

        if mode == "RISK_OFF":
            click.echo("[evaluate] RISK_OFF — skipping all entries")
            return

        evaluator = TradeEvaluator(dry_run=dry_run)
        decisions = evaluator.process_queue()

        processed_count = len(evaluator.processed)
        remaining_count = len(evaluator.remaining)

        click.echo(
            f"[evaluate] {processed_count} processed, {remaining_count} remaining"
        )

        click.echo(f"[evaluate] {sc.now_iso()} done")
    finally:
        release_command_lock("evaluate")
