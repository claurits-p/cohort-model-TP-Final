"""
Revenue model for payment processing.
Calculates revenue from pricing levers applied to volume forecast.
"""
from __future__ import annotations
from dataclasses import dataclass

import config as cfg
from models.volume_forecast import VolumeForecastYear
from models.cost_model import YearlyCosts, compute_yearly_costs


@dataclass
class PricingScenario:
    """A complete set of pricing levers."""
    saas_arr_discount_pct: float   # 0.0 – 0.70
    impl_fee_discount_pct: float   # 0.0 – 1.0  (1.0 = fully waived)
    cc_base_rate: float            # e.g. 0.0259
    cc_amex_rate: float            # e.g. 0.035
    ach_accel_pct: float           # 0.0 – 1.0: fraction of ACH on accelerated
    ach_accel_bps: float           # bps rate for accelerated ACH (0.10%–0.49%)
    ach_fixed_fee: float           # fixed fee for non-accelerated ACH ($2–$5)
    hold_days_cc: int

    # Derived fields populated at deal level
    saas_arr_list: float = cfg.SAAS_ARR_DEFAULT
    impl_fee_list: float = cfg.SAAS_IMPL_FEE_DEFAULT
    saas_discount_persists: bool = False

    @property
    def effective_saas_arr(self) -> float:
        return self.saas_arr_list * (1 - self.saas_arr_discount_pct)

    @property
    def effective_impl_fee(self) -> float:
        return self.impl_fee_list * (1 - self.impl_fee_discount_pct)

    @property
    def blended_hold_days_ach(self) -> float:
        a = cfg.ACH_ACCEL_HOLD["ach"]
        s = cfg.ACH_SLOW_HOLD["ach"]
        return self.ach_accel_pct * a + (1 - self.ach_accel_pct) * s

    @property
    def blended_hold_days_bank(self) -> float:
        a = cfg.ACH_ACCEL_HOLD["bank"]
        s = cfg.ACH_SLOW_HOLD["bank"]
        return self.ach_accel_pct * a + (1 - self.ach_accel_pct) * s


@dataclass
class YearlyRevenue:
    year: int
    saas_revenue: float
    impl_fee_revenue: float
    cc_revenue: float
    ach_revenue: float
    bank_network_revenue: float
    float_income: float
    total_revenue: float
    total_cost: float
    margin: float
    take_rate: float               # total_revenue / total_volume


def _ach_revenue_for_volume(
    volume: float,
    txn_count: int,
    pricing: PricingScenario,
) -> float:
    """ACH revenue from blended accelerated (bps) + non-accelerated (fixed fee)."""
    accel_vol = volume * pricing.ach_accel_pct
    slow_vol = volume * (1 - pricing.ach_accel_pct)
    slow_txns = int(txn_count * (1 - pricing.ach_accel_pct))

    accel_rev = accel_vol * pricing.ach_accel_bps
    slow_rev = slow_txns * pricing.ach_fixed_fee
    return accel_rev + slow_rev


DISCOUNT_RECOVERY_PCT = 0.75

def _saas_arr_for_year(pricing: PricingScenario, year: int) -> float:
    """SaaS ARR: full discount in Y1, 50% of discount persists in Y2-Y3, plus 7% escalator."""
    base = pricing.saas_arr_list * (cfg.SAAS_ANNUAL_ESCALATOR + 1) ** (year - 1)
    if year == 1:
        return base * (1 - pricing.saas_arr_discount_pct)
    retained_discount = pricing.saas_arr_discount_pct * (1 - DISCOUNT_RECOVERY_PCT)
    return base * (1 - retained_discount)


def _cc_blended_rate_for_year(pricing: PricingScenario, year: int) -> float:
    """CC rates revert to standard after Year 1."""
    if year == 1:
        base = pricing.cc_base_rate
        amex = pricing.cc_amex_rate
    else:
        base = cfg.CC_STANDARD_BASE_RATE
        amex = cfg.CC_STANDARD_AMEX_RATE
    return (
        cfg.CC_FIXED_COMPONENT
        + base * cfg.CC_BASE_VOLUME_SHARE
        + amex * cfg.CC_AMEX_VOLUME_SHARE
    )


def compute_yearly_revenue(
    vol: VolumeForecastYear,
    pricing: PricingScenario,
    costs: YearlyCosts,
    include_float: bool = True,
) -> YearlyRevenue:
    """Compute revenue for a single year."""
    saas_rev = _saas_arr_for_year(pricing, vol.year)
    impl_rev = pricing.effective_impl_fee if vol.year == 1 else 0.0

    blended_cc_rate = _cc_blended_rate_for_year(pricing, vol.year)
    cc_rev = vol.cc * blended_cc_rate

    ach_rev = _ach_revenue_for_volume(vol.ach, vol.ach_txn_count, pricing)
    bank_rev = 0.0

    float_income = 0.0
    if include_float:
        daily_rate = cfg.FLOAT_ANNUAL_RATE / 365
        cal = cfg.FLOAT_CALENDAR_FACTOR
        cc_float = vol.cc * daily_rate * max(pricing.hold_days_cc - 1, 0) * cal

        apct = pricing.ach_accel_pct
        accel_ach_hold = cfg.ACH_ACCEL_HOLD["ach"]
        accel_bank_hold = cfg.ACH_ACCEL_HOLD["bank"]
        slow_ach_hold = cfg.ACH_SLOW_HOLD["ach"]
        slow_bank_hold = cfg.ACH_SLOW_HOLD["bank"]

        ach_float_accel = vol.ach * apct * daily_rate * max(accel_ach_hold - 1, 0) * cal
        ach_float_slow = vol.ach * (1 - apct) * daily_rate * max(slow_ach_hold - 1, 0) * cal
        bank_float_accel = vol.bank_network * apct * daily_rate * max(accel_bank_hold - 1, 0) * cal
        bank_float_slow = vol.bank_network * (1 - apct) * daily_rate * max(slow_bank_hold - 1, 0) * cal

        float_income = cc_float + ach_float_accel + ach_float_slow + bank_float_accel + bank_float_slow

    total_rev = saas_rev + impl_rev + cc_rev + ach_rev + bank_rev + float_income
    margin = total_rev - costs.total
    take_rate = total_rev / vol.total if vol.total > 0 else 0.0

    return YearlyRevenue(
        year=vol.year,
        saas_revenue=saas_rev,
        impl_fee_revenue=impl_rev,
        cc_revenue=cc_rev,
        ach_revenue=ach_rev,
        bank_network_revenue=bank_rev,
        float_income=float_income,
        total_revenue=total_rev,
        total_cost=costs.total,
        margin=margin,
        take_rate=take_rate,
    )


def compute_three_year_financials(
    volumes: dict[int, VolumeForecastYear],
    pricing: PricingScenario,
    include_float: bool = True,
) -> dict[int, YearlyRevenue]:
    """Full 3-year financial projection for a pricing scenario."""
    results: dict[int, YearlyRevenue] = {}
    for year in [1, 2, 3]:
        vol = volumes[year]
        saas_for_cost = _saas_arr_for_year(pricing, year)
        costs = compute_yearly_costs(vol, saas_for_cost)
        results[year] = compute_yearly_revenue(vol, pricing, costs, include_float)
    return results
