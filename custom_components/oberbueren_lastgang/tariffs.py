"""Tariff database — load YAML, look up the right tariff per hour.

Tariffs live in ``<HA-config>/oberbueren_lastgang_tariffs.yaml`` as a list
of validity periods. Each period contains rates (in Rp/kWh for variable
positions, in CHF/Monat for fixed positions) excluding VAT, plus a
default VAT rate that can be overridden per-position with a ``<key>_mwst``
sibling.

The 10 paying positions on the Swiss bill are modeled as a flat registry
(see ``POSITIONS``). Each position has a ``kind`` (variable vs.
fixed_monthly), a ``tariff_split`` (whether it uses HT/NT or is flat),
and a ``stat_category`` mapping it onto one of the six cost statistic
buckets the integration imports.

Time-of-use: HT = Mon–Fri 07:00–19:00 in Europe/Zurich, NT otherwise.
Public holidays are not handled (per user decision) — a holiday on a
Wednesday at 10:00 still counts as HT.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml

_LOGGER = logging.getLogger(__name__)

# Local timezone for HT/NT determination. The upstream API serves data
# in this zone; HA hosts in CH should match. Hard-coded because this
# integration is regionally specific anyway.
_LOCAL_TZ = ZoneInfo("Europe/Zurich")

# File name (relative to the HA config directory) where users place the
# tariff YAML.
TARIFFS_FILENAME = "oberbueren_lastgang_tariffs.yaml"


PositionKind = Literal["variable", "fixed_monthly"]
TariffSplit = Literal["ht", "nt", "flat"]
StatCategory = Literal[
    "netznutzung_wirkstrom",
    "netznutzung_grundgebuehr",
    "energiebezug_wirkstrom",
    "energiebezug_zuschlaege",
    "messtarif",
]


@dataclass(frozen=True)
class PositionDef:
    """Static metadata describing one paying position on the bill."""

    key: str                   # YAML key, e.g. "abgaben.netzzuschlag"
    yaml_section: str          # Top-level YAML key
    yaml_field: str            # Sub-field within that section
    kind: PositionKind
    tariff_split: TariffSplit
    stat_category: StatCategory


# Registry of all 10 paying positions. The ordering is significant only
# for log readability — the runtime keys positions by ``key``.
POSITIONS: tuple[PositionDef, ...] = (
    PositionDef("netznutzung.wirkstrom_ht", "netznutzung", "wirkstrom_ht",
                "variable", "ht", "netznutzung_wirkstrom"),
    PositionDef("netznutzung.wirkstrom_nt", "netznutzung", "wirkstrom_nt",
                "variable", "nt", "netznutzung_wirkstrom"),
    PositionDef("netznutzung.grundgebuehr", "netznutzung", "grundgebuehr",
                "fixed_monthly", "flat", "netznutzung_grundgebuehr"),
    PositionDef("energiebezug.wirkstrom_ht", "energiebezug", "wirkstrom_ht",
                "variable", "ht", "energiebezug_wirkstrom"),
    PositionDef("energiebezug.wirkstrom_nt", "energiebezug", "wirkstrom_nt",
                "variable", "nt", "energiebezug_wirkstrom"),
    PositionDef("abgaben.sdl_swissgrid", "abgaben", "sdl_swissgrid",
                "variable", "flat", "energiebezug_zuschlaege"),
    PositionDef("abgaben.stromreserve", "abgaben", "stromreserve",
                "variable", "flat", "energiebezug_zuschlaege"),
    PositionDef("abgaben.solidarisierte_kosten", "abgaben",
                "solidarisierte_kosten", "variable", "flat",
                "energiebezug_zuschlaege"),
    PositionDef("abgaben.netzzuschlag", "abgaben", "netzzuschlag",
                "variable", "flat", "energiebezug_zuschlaege"),
    PositionDef("messtarif", "messtarif", "",
                "fixed_monthly", "flat", "messtarif"),
)


@dataclass(frozen=True)
class Position:
    """A position with its rate and effective MwSt rate (both numbers).

    For ``kind=variable`` positions ``rate`` is in Rp/kWh.
    For ``kind=fixed_monthly`` positions ``rate`` is in CHF/Monat.
    """

    rate_excl_mwst: float
    mwst_pct: float

    @property
    def mwst_factor(self) -> float:
        """Multiplier to go from excl-MwSt to incl-MwSt amount."""
        return 1.0 + self.mwst_pct / 100.0


@dataclass(frozen=True)
class TariffPeriod:
    """One row in the tariff YAML — a date range and its position rates."""

    valid_from: date
    valid_until: date | None
    mwst_default: float
    positions: dict[str, Position]   # keyed by PositionDef.key

    def covers(self, day: date) -> bool:
        if day < self.valid_from:
            return False
        if self.valid_until is not None and day > self.valid_until:
            return False
        return True


class TariffDatabase:
    """Holds all loaded periods and answers tariff lookups by datetime."""

    def __init__(self, periods: list[TariffPeriod]) -> None:
        # Sort newest first so lookup short-circuits on recent data.
        self._periods = sorted(periods, key=lambda p: p.valid_from, reverse=True)

    @property
    def is_empty(self) -> bool:
        return not self._periods

    def period_for(self, dt_utc: datetime) -> TariffPeriod | None:
        """Find the period covering ``dt_utc`` (interpreted in local time)."""
        local = dt_utc.astimezone(_LOCAL_TZ)
        day = local.date()
        for period in self._periods:
            if period.covers(day):
                return period
        return None

    def __len__(self) -> int:
        return len(self._periods)


def is_hochtarif(dt_utc: datetime) -> bool:
    """HT = Mo–Fr, 07:00–19:00 (Europe/Zurich). NT otherwise."""
    local = dt_utc.astimezone(_LOCAL_TZ)
    if local.weekday() >= 5:           # Sat=5, Sun=6
        return False
    return 7 <= local.hour < 19


def load_tariffs(config_dir: Path | str) -> TariffDatabase:
    """Read and parse the tariffs YAML. Missing file → empty database."""
    path = Path(config_dir) / TARIFFS_FILENAME
    if not path.exists():
        _LOGGER.info(
            "Tariff file %s does not exist — cost statistics will be skipped",
            path,
        )
        return TariffDatabase([])

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not raw:
        _LOGGER.warning("Tariff file %s is empty", path)
        return TariffDatabase([])
    if not isinstance(raw, list):
        raise TariffError(f"{path}: top-level must be a list of periods")

    periods = [_parse_period(p, path) for p in raw]
    _LOGGER.info("Loaded %d tariff period(s) from %s", len(periods), path)
    return TariffDatabase(periods)


def write_example_tariffs(config_dir: Path | str) -> Path:
    """Drop a commented example file at the standard location, if missing."""
    path = Path(config_dir) / TARIFFS_FILENAME
    if path.exists():
        return path

    path.write_text(_EXAMPLE_YAML, encoding="utf-8")
    _LOGGER.info("Wrote example tariff template to %s", path)
    return path


class TariffError(Exception):
    """Raised when the tariffs YAML can't be parsed."""


def _parse_period(raw: Any, source: Path) -> TariffPeriod:
    if not isinstance(raw, dict):
        raise TariffError(f"{source}: each period must be a mapping")

    try:
        valid_from = _parse_date(raw["valid_from"])
    except KeyError:
        raise TariffError(f"{source}: period missing 'valid_from'")

    valid_until_raw = raw.get("valid_until")
    valid_until = _parse_date(valid_until_raw) if valid_until_raw else None

    mwst_default = float(raw.get("mwst_default", 8.1))

    positions: dict[str, Position] = {}
    for pdef in POSITIONS:
        rate, mwst = _resolve_position_value(raw, pdef, mwst_default, source)
        if rate is None:
            # Position is missing entirely — skip silently. Cost calc
            # for that bucket will just be 0 for this period.
            continue
        positions[pdef.key] = Position(rate_excl_mwst=rate, mwst_pct=mwst)

    return TariffPeriod(
        valid_from=valid_from,
        valid_until=valid_until,
        mwst_default=mwst_default,
        positions=positions,
    )


def _resolve_position_value(
    raw: dict[str, Any],
    pdef: PositionDef,
    mwst_default: float,
    source: Path,
) -> tuple[float | None, float]:
    """Pull rate and MwSt for one position out of the raw YAML mapping."""
    if pdef.yaml_field == "":
        # messtarif is at the top level (a scalar, not a mapping).
        rate = raw.get(pdef.yaml_section)
        mwst_override = raw.get(f"{pdef.yaml_section}_mwst")
    else:
        section = raw.get(pdef.yaml_section, {}) or {}
        if not isinstance(section, dict):
            raise TariffError(
                f"{source}: section '{pdef.yaml_section}' must be a mapping"
            )
        rate = section.get(pdef.yaml_field)
        mwst_override = section.get(f"{pdef.yaml_field}_mwst")

    if rate is None:
        return None, mwst_default
    return float(rate), float(mwst_override) if mwst_override is not None else mwst_default


def _parse_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TariffError(f"Invalid date value: {value!r}")


_EXAMPLE_YAML = """\
# Tariff configuration for oberbueren_lastgang.
#
# This file holds one or more tariff periods. Each period covers a
# date range; the integration looks up the period valid for each
# imported hour and computes cost statistics from it.
#
# All variable rates (Rp/kWh) are EXCLUDING VAT — the integration
# applies MwSt automatically using ``mwst_default`` (or a per-position
# override like ``netzzuschlag_mwst: 0`` when a position is VAT-free).
# Fixed monthly fees are in CHF.
#
# The 10 positions are split across these sections:
#   netznutzung:  wirkstrom_ht, wirkstrom_nt (Rp/kWh) + grundgebuehr (CHF/Monat)
#   energiebezug: wirkstrom_ht, wirkstrom_nt (Rp/kWh)
#   abgaben:      sdl_swissgrid, stromreserve, solidarisierte_kosten,
#                 netzzuschlag (all Rp/kWh)
#   messtarif:    scalar in CHF/Monat
#
# Add more periods as your tariffs change. Set ``valid_until: ~`` for
# the currently-active period.
- valid_from: 2026-01-01
  valid_until: ~
  mwst_default: 8.1

  netznutzung:
    wirkstrom_ht: 0.00
    wirkstrom_nt: 0.00
    grundgebuehr: 0.00

  energiebezug:
    wirkstrom_ht: 0.00
    wirkstrom_nt: 0.00

  abgaben:
    sdl_swissgrid: 0.00
    stromreserve: 0.00
    solidarisierte_kosten: 0.00
    netzzuschlag: 0.00

  messtarif: 0.00
"""
