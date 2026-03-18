"""
Win rate model — simple additive.

Standard pricing produces the baseline win rate (59%).
Each pricing lever adds or subtracts linearly based on how far
it deviates from standard, capped per lever, then clamped to
[WIN_RATE_FLOOR, WIN_RATE_CEILING].

ACH uses a blend model: accelerated (bps, fast hold) + non-accelerated
(fixed fee, slow hold). Win rate impact comes from the effective cost
to the customer and how aggressively accelerated bps are pushed.
"""
from __future__ import annotations
import copy

from scipy.optimize import brentq, minimize_scalar

import config as cfg
from models.revenue_model import PricingScenario


def _blended_cc(base_rate: float, amex_rate: float) -> float:
    return (
        cfg.CC_FIXED_COMPONENT
        + base_rate * cfg.CC_BASE_VOLUME_SHARE
        + amex_rate * cfg.CC_AMEX_VOLUME_SHARE
    )


_STD = cfg.STANDARD_PRICING
_STD_CC_BLENDED = _blended_cc(_STD["cc_base_rate"], _STD["cc_amex_rate"])
_BEST_CC_BLENDED = _blended_cc(
    cfg.LEVER_BOUNDS["cc_base_rate"]["min"],
    cfg.LEVER_BOUNDS["cc_amex_rate"]["min"],
)
_WORST_CC_BLENDED = _blended_cc(
    cfg.LEVER_BOUNDS["cc_base_rate"]["max"],
    cfg.LEVER_BOUNDS["cc_amex_rate"]["max"],
)

_IMPACTS = cfg.LEVER_MAX_IMPACT


def _linear_impact(
    value: float,
    standard: float,
    best: float,
    worst: float,
    max_impact: float,
    lower_is_better: bool = True,
) -> float:
    """Compute a linear impact in [-max_impact, +max_impact].

    At ``standard`` → 0.  At ``best`` → +max_impact.  At ``worst`` → -max_impact.
    """
    if lower_is_better:
        if value <= standard:
            denom = standard - best
            if denom == 0:
                return 0.0
            return max_impact * min(1.0, (standard - value) / denom)
        else:
            denom = worst - standard
            if denom == 0:
                return 0.0
            return -max_impact * min(1.0, (value - standard) / denom)
    else:
        if value >= standard:
            denom = best - standard
            if denom == 0:
                return 0.0
            return max_impact * min(1.0, (value - standard) / denom)
        else:
            denom = standard - worst
            if denom == 0:
                return 0.0
            return -max_impact * min(1.0, (standard - value) / denom)


def _ach_win_impact(pricing: PricingScenario) -> float:
    """ACH win rate impact from the blended model.

    Three components:
    1. Accelerated bps penalty: charging high bps hurts. 0.35% is slight,
       0.49% is significant. Scaled by accel_pct.
    2. Accelerated reduction benefit: moving customers off accelerated
       gives a small win rate boost.
    3. Fixed fee penalty: $1.00-$2.50 neutral. Above $2.50 adds linear
       friction; $5.00 is very hard to pull off at portfolio level.
    """
    lb = cfg.LEVER_BOUNDS
    max_ach_impact = _IMPACTS["ach_rate"]

    accel_pct = pricing.ach_accel_pct
    accel_bps = pricing.ach_accel_bps
    fixed_fee = pricing.ach_fixed_fee
    std_accel_pct = _STD["ach_accel_pct"]
    std_accel_bps = _STD["ach_accel_bps"]

    bps_penalty = 0.0
    if accel_bps > std_accel_bps:
        bps_range = lb["ach_accel_bps"]["max"] - std_accel_bps
        if bps_range > 0:
            bps_severity = min(1.0, (accel_bps - std_accel_bps) / bps_range)
            bps_penalty = -max_ach_impact * bps_severity * accel_pct

    accel_benefit = 0.0
    if accel_pct < std_accel_pct:
        accel_benefit = max_ach_impact * 0.3 * (std_accel_pct - accel_pct)

    fee_penalty = 0.0
    fee_neutral_max = 2.50
    fee_hard_cap = 5.00
    if fixed_fee > fee_neutral_max:
        fee_range = fee_hard_cap - fee_neutral_max
        if fee_range > 0:
            fee_severity = min(1.0, (fixed_fee - fee_neutral_max) / fee_range)
            fee_penalty = -max_ach_impact * fee_severity * (1 - accel_pct)

    return bps_penalty + accel_benefit + fee_penalty


def win_rate(pricing: PricingScenario) -> float:
    """Compute win rate using the simple additive model.

    Returns a value clamped to [WIN_RATE_FLOOR, WIN_RATE_CEILING].
    """
    lb = cfg.LEVER_BOUNDS

    saas_impact = _linear_impact(
        pricing.saas_arr_discount_pct,
        _STD["saas_arr_discount_pct"],
        lb["saas_arr_discount_pct"]["max"],
        lb["saas_arr_discount_pct"]["min"],
        _IMPACTS["saas_discount"],
        lower_is_better=False,
    )

    cc_blended = _blended_cc(pricing.cc_base_rate, pricing.cc_amex_rate)
    cc_impact = _linear_impact(
        cc_blended,
        _STD_CC_BLENDED,
        _BEST_CC_BLENDED,
        _WORST_CC_BLENDED,
        _IMPACTS["cc_rate"],
        lower_is_better=True,
    )

    ach_impact = _ach_win_impact(pricing)

    impl_impact = _linear_impact(
        pricing.impl_fee_discount_pct,
        _STD["impl_fee_discount_pct"],
        lb["impl_fee_discount_pct"]["max"],
        lb["impl_fee_discount_pct"]["min"],
        _IMPACTS["impl_discount"],
        lower_is_better=False,
    )

    total = cfg.WIN_RATE_BASELINE + saas_impact + cc_impact + ach_impact + impl_impact
    return max(cfg.WIN_RATE_FLOOR, min(cfg.WIN_RATE_CEILING, total))


# ── Backwards-compatible aliases used by cohort_engine ────────
def win_probability(pricing: PricingScenario, **kwargs) -> float:
    return win_rate(pricing)


def win_probability_uncapped(pricing: PricingScenario, **kwargs) -> float:
    return win_rate(pricing)


# ── LTV Solver: SaaS → CC → ACH blend → Impl ────────────────

def _record_ach_changes(changes, pricing, adjusted):
    """Record ACH blend changes between original and adjusted pricing."""
    if abs(adjusted.ach_accel_pct - pricing.ach_accel_pct) > 0.01:
        changes["ach_accel_pct"] = (pricing.ach_accel_pct, adjusted.ach_accel_pct)
    if abs(adjusted.ach_accel_bps - pricing.ach_accel_bps) > 1e-5:
        changes["ach_accel_bps"] = (pricing.ach_accel_bps, adjusted.ach_accel_bps)
    if abs(adjusted.ach_fixed_fee - pricing.ach_fixed_fee) > 0.01:
        changes["ach_fixed_fee"] = (pricing.ach_fixed_fee, adjusted.ach_fixed_fee)


def solve_multi_lever_for_target_win_rate(
    pricing: PricingScenario,
    target_wp: float,
    wp_params: dict,
) -> dict | None:
    """Find lever adjustments to hit target_wp.

    Priority: SaaS discount → CC rates → ACH blend → Impl fee.
    Returns dict with adjusted pricing and changes, or None.
    """
    adjusted = copy.copy(pricing)
    changes = {}
    lb = cfg.LEVER_BOUNDS

    current = win_rate(adjusted)
    if current >= target_wp:
        return {"pricing": adjusted, "changes": changes}

    # 1) SaaS discount
    saas_lo = adjusted.saas_arr_discount_pct
    saas_hi = lb["saas_arr_discount_pct"]["max"]
    if saas_hi > saas_lo:
        def _wr_saas(d):
            p = copy.copy(adjusted)
            p.saas_arr_discount_pct = d
            return win_rate(p) - target_wp

        if _wr_saas(saas_hi) >= 0:
            try:
                result = brentq(_wr_saas, saas_lo, saas_hi, xtol=1e-4)
                changes["saas_arr_discount_pct"] = (pricing.saas_arr_discount_pct, result)
                adjusted.saas_arr_discount_pct = result
                return {"pricing": adjusted, "changes": changes}
            except ValueError:
                pass

        adjusted.saas_arr_discount_pct = saas_hi
        if saas_hi > pricing.saas_arr_discount_pct:
            changes["saas_arr_discount_pct"] = (pricing.saas_arr_discount_pct, saas_hi)

    if win_rate(adjusted) >= target_wp:
        return {"pricing": adjusted, "changes": changes}

    # 2) CC base + AMEX together
    cc_hi = adjusted.cc_base_rate
    cc_lo = lb["cc_base_rate"]["min"]
    amex_hi = adjusted.cc_amex_rate
    amex_lo = lb["cc_amex_rate"]["min"]

    if cc_hi > cc_lo or amex_hi > amex_lo:
        def _wr_cc(t):
            p = copy.copy(adjusted)
            frac = max(0.0, min(1.0, t))
            if cc_hi > cc_lo:
                p.cc_base_rate = cc_hi - frac * (cc_hi - cc_lo)
            if amex_hi > amex_lo:
                p.cc_amex_rate = amex_hi - frac * (amex_hi - amex_lo)
            return win_rate(p) - target_wp

        if _wr_cc(1.0) >= 0:
            try:
                result = brentq(_wr_cc, 0.0, 1.0, xtol=1e-5)
                new_base = cc_hi - result * (cc_hi - cc_lo) if cc_hi > cc_lo else cc_hi
                new_amex = amex_hi - result * (amex_hi - amex_lo) if amex_hi > amex_lo else amex_hi
                if abs(new_base - pricing.cc_base_rate) > 1e-5:
                    changes["cc_base_rate"] = (pricing.cc_base_rate, new_base)
                if abs(new_amex - pricing.cc_amex_rate) > 1e-5:
                    changes["cc_amex_rate"] = (pricing.cc_amex_rate, new_amex)
                adjusted.cc_base_rate = new_base
                adjusted.cc_amex_rate = new_amex
                return {"pricing": adjusted, "changes": changes}
            except ValueError:
                pass

        adjusted.cc_base_rate = cc_lo if cc_hi > cc_lo else cc_hi
        adjusted.cc_amex_rate = amex_lo if amex_hi > amex_lo else amex_hi
        if cc_lo < pricing.cc_base_rate:
            changes["cc_base_rate"] = (pricing.cc_base_rate, cc_lo)
        if amex_lo < pricing.cc_amex_rate:
            changes["cc_amex_rate"] = (pricing.cc_amex_rate, amex_lo)

    if win_rate(adjusted) >= target_wp:
        return {"pricing": adjusted, "changes": changes}

    # 3) ACH blend — shift accel_pct within bounds, with default fixed fee
    accel_min = lb["ach_accel_pct"]["min"]
    accel_max = lb["ach_accel_pct"]["max"]
    adjusted.ach_fixed_fee = lb["ach_fixed_fee"]["default"]
    best_ach_wr = win_rate(adjusted)
    best_ach_pct = adjusted.ach_accel_pct

    steps = [accel_min + i * 0.05 for i in range(int((accel_max - accel_min) / 0.05) + 1)]
    for test_pct in steps:
        p = copy.copy(adjusted)
        p.ach_accel_pct = test_pct
        wr = win_rate(p)
        if wr > best_ach_wr or (wr == best_ach_wr and test_pct < best_ach_pct):
            best_ach_wr = wr
            best_ach_pct = test_pct

    adjusted.ach_accel_pct = best_ach_pct
    _record_ach_changes(changes, pricing, adjusted)

    if win_rate(adjusted) >= target_wp:
        return {"pricing": adjusted, "changes": changes}

    # 4) Impl fee discount — increase toward 100%
    impl_hi = lb["impl_fee_discount_pct"]["max"]
    if adjusted.impl_fee_discount_pct < impl_hi:
        def _wr_impl(d):
            p = copy.copy(adjusted)
            p.impl_fee_discount_pct = d
            return win_rate(p) - target_wp

        if _wr_impl(impl_hi) >= 0:
            try:
                result = brentq(_wr_impl, adjusted.impl_fee_discount_pct, impl_hi, xtol=1e-4)
                if abs(result - pricing.impl_fee_discount_pct) > 1e-4:
                    changes["impl_fee_discount_pct"] = (pricing.impl_fee_discount_pct, result)
                adjusted.impl_fee_discount_pct = result
                return {"pricing": adjusted, "changes": changes}
            except ValueError:
                pass

        adjusted.impl_fee_discount_pct = impl_hi
        if abs(impl_hi - pricing.impl_fee_discount_pct) > 1e-4:
            changes["impl_fee_discount_pct"] = (pricing.impl_fee_discount_pct, impl_hi)

    final = win_rate(adjusted)
    if final >= target_wp - 0.005:
        return {"pricing": adjusted, "changes": changes}

    return None


# ── Top Line Optimizer: maximize 3-year cohort revenue ────────

def optimize_topline_pricing(
    pricing: PricingScenario,
    deals_to_pricing: int,
    volumes: dict,
    quarterly_churn: float = 0.02,
) -> tuple[PricingScenario, dict, float]:
    """Maximize total 3-year cohort revenue using all levers.

    CC at min, impl fee at max. Sweeps ACH blend (accel_pct × accel_bps ×
    fixed_fee) and SaaS discount jointly to find revenue-maximizing combo.

    Returns (optimized_pricing, lever_changes, achieved_win_rate).
    """
    from models.revenue_model import compute_three_year_financials

    lb = cfg.LEVER_BOUNDS

    def _retention_factor(year):
        r = 1 - quarterly_churn
        qs = (year - 1) * 4
        qe = year * 4
        return sum(r ** q for q in range(qs, qe)) / 4

    a_min = lb["ach_accel_pct"]["min"]
    a_max = lb["ach_accel_pct"]["max"]
    accel_pcts = [a_min + i * 0.05 for i in range(int((a_max - a_min) / 0.05) + 1)]
    b_min = lb["ach_accel_bps"]["min"]
    b_max = lb["ach_accel_bps"]["max"]
    accel_bps_options = [b_min + i * 0.0005 for i in range(int((b_max - b_min) / 0.0005) + 1)]
    f_min = lb["ach_fixed_fee"]["min"]
    f_max = lb["ach_fixed_fee"]["max"]
    fixed_fee_options = [f_min + i * 0.50 for i in range(int((f_max - f_min) / 0.50) + 1)]

    best_revenue = float("-inf")
    best_adjusted = None

    for apct in accel_pcts:
        for abps in accel_bps_options:
            for ffee in fixed_fee_options:
                base = copy.copy(pricing)
                base.cc_base_rate = lb["cc_base_rate"]["min"]
                base.cc_amex_rate = lb["cc_amex_rate"]["min"]
                base.hold_days_cc = lb["hold_days_cc"]["max"]
                base.impl_fee_discount_pct = lb["impl_fee_discount_pct"]["max"]
                base.ach_accel_pct = apct
                base.ach_accel_bps = abps
                base.ach_fixed_fee = ffee

                def _neg_revenue(saas_d, _base=base):
                    _base.saas_arr_discount_pct = saas_d
                    wp = win_rate(_base)
                    deals = deals_to_pricing * wp
                    yearly = compute_three_year_financials(volumes, _base, include_float=True)
                    return -sum(
                        yearly[y].total_revenue * deals * _retention_factor(y)
                        for y in [1, 2, 3]
                    )

                result = minimize_scalar(
                    _neg_revenue,
                    bounds=(lb["saas_arr_discount_pct"]["min"], lb["saas_arr_discount_pct"]["max"]),
                    method="bounded",
                )

                revenue = -result.fun
                if revenue > best_revenue:
                    best_revenue = revenue
                    best_adjusted = copy.copy(base)
                    best_adjusted.saas_arr_discount_pct = result.x

    adjusted = best_adjusted
    changes = {}

    if abs(adjusted.saas_arr_discount_pct - pricing.saas_arr_discount_pct) > 1e-4:
        changes["saas_arr_discount_pct"] = (pricing.saas_arr_discount_pct, adjusted.saas_arr_discount_pct)
    if abs(adjusted.cc_base_rate - pricing.cc_base_rate) > 1e-5:
        changes["cc_base_rate"] = (pricing.cc_base_rate, adjusted.cc_base_rate)
    if abs(adjusted.cc_amex_rate - pricing.cc_amex_rate) > 1e-5:
        changes["cc_amex_rate"] = (pricing.cc_amex_rate, adjusted.cc_amex_rate)
    _record_ach_changes(changes, pricing, adjusted)
    if adjusted.hold_days_cc != pricing.hold_days_cc:
        changes["hold_days_cc"] = (pricing.hold_days_cc, adjusted.hold_days_cc)
    if abs(adjusted.impl_fee_discount_pct - pricing.impl_fee_discount_pct) > 1e-4:
        changes["impl_fee_discount_pct"] = (pricing.impl_fee_discount_pct, adjusted.impl_fee_discount_pct)

    achieved = win_rate(adjusted)
    return adjusted, changes, achieved
