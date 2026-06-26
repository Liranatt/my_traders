"""Configuration constants and the policy / simulator dataclasses for the Portfolio Layer."""
from __future__ import annotations

from dataclasses import dataclass

# --- The contract anchor -----------------------------------------------------------------
# Canonical dataset: the completed run with 1,733 valid candidates (2024-07-29 -> 2026-05-21).
# This MUST match the run Agent A uses to build data/candidates.parquet. Locked here so the
# DB-built mock is reproducible; surfaced loudly at runtime so it can be confirmed with Agent A.
DEFAULT_RUN_ID = "339084de-3ab2-4548-b06b-350feff0a376"
CANDIDATES_PARQUET = "data/candidates.parquet"

# --- Chronological split (MODEL_PLAN Stage 3): 10-trading-day embargo between segments -----
# Candidates whose t_theta falls in an embargo gap (2026-01-01..01-09, 2026-04-01..04-09) are
# dropped so a train trade's 10-day-forward outcome can't leak into val/test.
TRAIN_END = "2025-12-31"
VAL_START = "2026-01-10"
VAL_END = "2026-03-31"
TEST_START = "2026-04-10"

TRADING_DAYS_PER_YEAR = 252

# Archetypes removed to form the `pre_drop_earnings_defense` universe (task B3).
# NOTE: Agent A owns the authoritative boolean in the contract; this is B's stand-in until the
# parquet ships. `company_quarterly_earnings_beat` = the earnings peer-dump worlds flagged as
# weak in WORKING_WITH_LIRAN. (No standalone "defense" archetype exists in this run; the spurious
# defense mappings live inside geopolitics worlds and can't be split out from archetype alone —
# flagged for Agent A.)
PRE_DROP_ARCHETYPES = ("company_quarterly_earnings_beat",)


@dataclass(frozen=True)
class PolicyConfig:
    """The rule-based Portfolio Layer v1 (B1). ~3 knobs per the overfitting guardrail."""

    # ENTRY — confirmation band (swept in B1).
    entry_strong_probability: float = 0.65   # crossing >= this enters immediately
    entry_confirm_floor: float = 0.55        # weaker crossings must hold above this ...
    entry_confirmation_days: int = 2         # ... for this many trading days, then enter

    # EXIT toolbox (swept in B1). Exactly one stop_type plus the always-available exits below.
    #   none | fixed | fixed_trailing | close_vol_trailing
    stop_type: str = "close_vol_trailing"
    stop_level: float = 0.05                 # fixed/fixed_trailing: return frac; close_vol: k*sigma
    vol_lookback: int = 10                   # trading days for the close-vol trailing stop
    stop_basis: str = "spread"               # apply the stop to the hedged spread | the raw asset

    use_polymarket_exit: bool = True         # sell when prob crosses back below theta_out
    theta_out: float = 0.50                  # swept in {0.50, 0.55}
    use_resolution_exit: bool = True         # out 1 trading day before t_e (no fixed 10d cap)
    use_rf_flip: bool = False                # exit if Agent A re-scores & flips (off until enabled)

    # --- Added exit rules (each tested SEPARATELY; all default off) -----------------------
    # A. Take-profit: exit when the position reaches a favorable target. mode 'fixed' uses
    #    take_profit_level; mode 'rf' uses the RF's predicted magnitude |alpha_score| per trade.
    use_take_profit: bool = False
    take_profit_level: float = 0.06
    take_profit_mode: str = "fixed"          # fixed | rf
    # B. Peak give-back ratchet: once up >= ratchet_arm, exit on ratchet_giveback retrace from peak.
    use_peak_ratchet: bool = False
    ratchet_arm: float = 0.04
    ratchet_giveback: float = 0.02
    # C. Pre-event exit: out pre_event_days trading days before the resolution (skip the gap).
    use_pre_event_exit: bool = False
    pre_event_days: int = 1
    # D. Momentum-stall: exit if no new favorable high in stall_days trading days.
    use_stall_exit: bool = False
    stall_days: int = 3
    # E. Probability-ceiling: exit when the Polymarket prob reaches prob_ceiling (info diffused).
    use_prob_ceiling_exit: bool = False
    prob_ceiling: float = 0.85

    def label(self) -> str:
        stop = self.stop_type if self.stop_type == "none" else f"{self.stop_type}@{self.stop_level:g}"
        poly = f"poly<{self.theta_out:g}" if self.use_polymarket_exit else "no-poly"
        extra = []
        if self.use_take_profit:
            extra.append(f"TP-{self.take_profit_mode}@{self.take_profit_level:g}" if self.take_profit_mode == "fixed" else f"TP-rf")
        if self.use_peak_ratchet:
            extra.append(f"ratchet{self.ratchet_arm:g}/{self.ratchet_giveback:g}")
        if self.use_pre_event_exit:
            extra.append(f"pre_event{self.pre_event_days}d")
        if self.use_stall_exit:
            extra.append(f"stall{self.stall_days}d")
        if self.use_prob_ceiling_exit:
            extra.append(f"prob>={self.prob_ceiling:g}")
        tail = (", " + "+".join(extra)) if extra else ""
        return f"strong>={self.entry_strong_probability:g}/conf{self.entry_confirmation_days}d, {stop}, {poly}{tail}"


@dataclass(frozen=True)
class SimConfig:
    """Book mechanics + experiment selectors (B2 objective, B3 universe, alpha mock mode)."""

    run_id: str = DEFAULT_RUN_ID
    book: float = 100_000.0
    position_fraction: float = 0.10          # constant notional per position = book * fraction
    max_positions: int = 10                  # cap so gross <= book (constant size, no leverage)
    sector_cap: int = 4                      # max concurrent positions sharing a sector ETF

    alpha_mode: str = "parquet"              # parquet | oracle | random | long_only | current_ml
    universe: str = "all"                    # all | pre_drop  (task B3)
    objective: str = "sharpe"                # sharpe | calmar | return_maxdd  (task B2)
    maxdd_cap: float = 0.20                  # return_maxdd: maximize net return s.t. maxDD <= cap
    seed: int = 7
    hedge: bool = True                       # False => raw long (no sector-ETF short leg)
    min_relevance: float = 0.0               # only trade candidates with final relevance >= this
