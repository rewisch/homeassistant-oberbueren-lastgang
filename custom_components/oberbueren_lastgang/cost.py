"""Per-hour cost computation.

Given a list of hourly kWh measurements and a TariffDatabase, produce
the same list of hours expanded with cost figures for each of the five
cost categories (plus a ``total`` rollup):

  * ``netznutzung_wirkstrom``     — kWh × HT/NT- bzw. Einheitstarif × MwSt
  * ``netznutzung_grundgebuehr``  — Fixkosten anteilig pro Stunde des Monats
  * ``netznutzung_leistung``      — Monatsspitze (kW) × Leistungspreis ×
                                    MwSt, komplett auf die Spitzenstunde
  * ``energiebezug_wirkstrom``    — kWh × HT/NT- bzw. Sommer/Winter × MwSt
  * ``energiebezug_zuschlaege``   — kWh × Σ(Zuschlagspositionen × ihrer MwSt)
  * ``messtarif``                 — Fixkosten anteilig pro Stunde des Monats
  * ``total``                     — Σ obiger sechs

Rates in the YAML are in Rappen per kWh (variable) or CHF per month
(fixed); we convert to CHF per kWh internally so all sums end up in CHF.
"""
from __future__ import annotations

import calendar
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .const import COST_CATEGORY_KEYS, COST_TOTAL_KEY
from .tariffs import (
    POSITIONS,
    Position,
    PositionDef,
    TariffDatabase,
    TariffPeriod,
    is_hochtarif,
    is_summer,
)

# YAML key of the monthly-peak Leistungspreis position.
_LEISTUNG_KEY = "netznutzung.leistung"

_LOGGER = logging.getLogger(__name__)
_LOCAL_TZ = ZoneInfo("Europe/Zurich")

# Conversion: positions defined in Rp/kWh → CHF/kWh.
_RP_TO_CHF = 0.01


def compute_hourly_costs(
    hourly_kwh: list[tuple[datetime, float]],
    tariffs: TariffDatabase,
    hourly_peak_kw: dict[datetime, float] | None = None,
) -> dict[str, list[tuple[datetime, float]]]:
    """Expand hourly kWh into per-category hourly cost (CHF, incl. MwSt).

    The returned dict has one entry per category plus ``total`` — each
    value is a list of ``(hour_utc, cost_chf)`` tuples in the same order
    as the input. Hours that fall outside any tariff period (e.g. before
    the user's earliest valid_from) are recorded as 0.0 cost across all
    categories rather than being dropped, so the chronological alignment
    with the kWh series is preserved.

    ``hourly_peak_kw`` maps each ``hour_utc`` to the highest 15-minute
    average power (kW) observed in that hour. When given (and the tariff
    period has a ``power_monthly`` Leistungspreis), the monthly demand
    charge — ``month_peak_kw × rate × MwSt`` — is added in full to the
    single hour that set that calendar month's peak, and zero to every
    other hour. Callers that pass only a partial month get a provisional
    lump; a later month-scoped rebuild over the full month corrects it.
    """
    out: dict[str, list[tuple[datetime, float]]] = {
        key: [] for key in (*COST_CATEGORY_KEYS, COST_TOTAL_KEY)
    }
    if tariffs.is_empty:
        # No tariffs configured → emit zeros so callers can still write
        # statistics (they'll be flat lines, which is honest).
        for hour_utc, _ in hourly_kwh:
            for key in (*COST_CATEGORY_KEYS, COST_TOTAL_KEY):
                out[key].append((hour_utc, 0.0))
        return out

    missing_period_logged = False

    for hour_utc, kwh in hourly_kwh:
        period = tariffs.period_for(hour_utc)
        if period is None:
            if not missing_period_logged:
                _LOGGER.warning(
                    "No tariff period covers %s — emitting zero cost for "
                    "uncovered hours (further occurrences silenced).",
                    hour_utc.isoformat(),
                )
                missing_period_logged = True
            for key in (*COST_CATEGORY_KEYS, COST_TOTAL_KEY):
                out[key].append((hour_utc, 0.0))
            continue

        per_category = _compute_hour(hour_utc, kwh, period)
        total = sum(per_category.values())
        for key in COST_CATEGORY_KEYS:
            out[key].append((hour_utc, per_category[key]))
        out[COST_TOTAL_KEY].append((hour_utc, total))

    if hourly_peak_kw:
        _inject_leistung_lumps(out, hourly_peak_kw, tariffs)

    return out


def _inject_leistung_lumps(
    out: dict[str, list[tuple[datetime, float]]],
    hourly_peak_kw: dict[datetime, float],
    tariffs: TariffDatabase,
) -> None:
    """Add each calendar month's demand charge to its peak hour in-place.

    ``out`` is the dict built by ``compute_hourly_costs`` — parallel
    ``(hour_utc, chf)`` lists. We locate the hour that set every month's
    15-minute peak, compute ``peak_kw × Leistungspreis × MwSt`` from the
    tariff period covering that hour, and fold it into both the
    ``netznutzung_leistung`` bucket and ``total`` at that index.
    """
    idx_by_hour = {
        hour_utc: i
        for i, (hour_utc, _) in enumerate(out[COST_TOTAL_KEY])
    }

    by_month: dict[tuple[int, int], list[datetime]] = {}
    for hour_utc in idx_by_hour:
        local = hour_utc.astimezone(_LOCAL_TZ)
        by_month.setdefault((local.year, local.month), []).append(hour_utc)

    for hours in by_month.values():
        peak_kw, peak_hour = max(
            (hourly_peak_kw.get(h, 0.0), h) for h in hours
        )
        if peak_kw <= 0.0:
            continue
        period = tariffs.period_for(peak_hour)
        if period is None:
            continue
        pos = period.positions.get(_LEISTUNG_KEY)
        if pos is None:
            continue
        # rate is CHF per kW per month; peak_kw in kW → CHF (incl. MwSt).
        amount = peak_kw * pos.rate_excl_mwst * pos.mwst_factor
        i = idx_by_hour[peak_hour]
        for key in ("netznutzung_leistung", COST_TOTAL_KEY):
            h, v = out[key][i]
            out[key][i] = (h, v + amount)


def _compute_hour(
    hour_utc: datetime,
    kwh: float,
    period: TariffPeriod,
) -> dict[str, float]:
    """Costs for a single hour, broken down per category."""
    ht = is_hochtarif(hour_utc)
    summer = is_summer(hour_utc)
    local = hour_utc.astimezone(_LOCAL_TZ)
    monthly_share = 1.0 / _hours_in_month(local.year, local.month)

    bucket: dict[str, float] = {key: 0.0 for key in COST_CATEGORY_KEYS}

    for pdef in POSITIONS:
        position = period.positions.get(pdef.key)
        if position is None:
            continue
        bucket[pdef.stat_category] += _position_cost(
            pdef, position, kwh, ht, summer, monthly_share
        )

    return bucket


def _position_cost(
    pdef: PositionDef,
    position: Position,
    kwh: float,
    ht: bool,
    summer: bool,
    monthly_share: float,
) -> float:
    """CHF (incl. MwSt) this position contributes to a single hour."""
    incl = position.rate_excl_mwst * position.mwst_factor

    if pdef.kind == "fixed_monthly":
        # ``incl`` is CHF/Monat, distribute across hours of this month.
        return incl * monthly_share

    if pdef.kind == "power_monthly":
        # Demand charge depends on the whole month's peak — it is added
        # to the peak hour separately in _inject_leistung_lumps().
        return 0.0

    # variable: incl is Rp/kWh, multiply by kWh and convert Rp→CHF.
    chf_per_kwh = incl * _RP_TO_CHF

    # Time-of-use / seasonal gate. Flat positions charge unconditionally.
    if pdef.tariff_split == "ht" and not ht:
        return 0.0
    if pdef.tariff_split == "nt" and ht:
        return 0.0
    if pdef.tariff_split == "summer" and not summer:
        return 0.0
    if pdef.tariff_split == "winter" and summer:
        return 0.0

    return chf_per_kwh * kwh


def _hours_in_month(year: int, month: int) -> int:
    """Whole hours in a calendar month, ignoring DST jumps.

    March/October differ by ±1 hour due to DST, but spreading the fixed
    monthly fee across nominal 24-hour days is what users actually expect
    on a bill. Off-by-one hour per year is below the rounding noise.
    """
    days = calendar.monthrange(year, month)[1]
    return days * 24


