"""Tariff database — load YAML, look up the right tariff per hour.

Tariffs live in ``<HA-config>/oberbueren_lastgang_tariffs.yaml`` as a list
of validity periods. Each period contains rates (Rp/kWh for variable
positions, CHF/Monat for fixed positions, CHF/kW/Monat for the demand
charge) excluding VAT, plus a default VAT rate that can be overridden
per-position with a ``<key>_mwst`` sibling.

The paying positions on the Swiss bill are modeled as a flat registry
(see ``POSITIONS``). Each position has a ``kind`` (variable /
fixed_monthly / power_monthly), a ``tariff_split`` (ht / nt / summer /
winter / flat), and a ``stat_category`` mapping it onto one of the cost
statistic buckets the integration imports. A period only carries the
positions that apply to it, so the pre-2027 HT/NT layout and the 2027+
Einheitstarif + seasonal + Leistungspreis layout coexist by date.

Time-of-use: HT = Mon–Fri 07:00–19:00 in Europe/Zurich, NT otherwise.
Season (2027+): summer = Apr–Sep, winter = Oct–Mar.
Public holidays are not handled (per user decision) — a holiday on a
Wednesday at 10:00 still counts as HT.
"""
from __future__ import annotations

import hashlib
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


PositionKind = Literal["variable", "fixed_monthly", "power_monthly"]
# ht / nt  → time-of-use split (Mo–Fr 07–19 = HT), used up to 2026.
# summer / winter → seasonal split (Apr–Sep = summer), used from 2027.
# flat → charged regardless of time or season.
TariffSplit = Literal["ht", "nt", "summer", "winter", "flat"]
StatCategory = Literal[
    "netznutzung_wirkstrom",
    "netznutzung_grundgebuehr",
    "netznutzung_leistung",
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


# Registry of every paying position the YAML may contain. A given tariff
# period only carries the subset that applies to it — missing positions
# are skipped silently (see ``_parse_period``), so the 2026 HT/NT block
# and the 2027 Einheitstarif + seasonal + Leistung block coexist without
# a version flag. The ordering is significant only for log readability.
POSITIONS: tuple[PositionDef, ...] = (
    # --- Netznutzung ---------------------------------------------------
    # HT/NT split (≤ 2026).
    PositionDef("netznutzung.wirkstrom_ht", "netznutzung", "wirkstrom_ht",
                "variable", "ht", "netznutzung_wirkstrom"),
    PositionDef("netznutzung.wirkstrom_nt", "netznutzung", "wirkstrom_nt",
                "variable", "nt", "netznutzung_wirkstrom"),
    # Einheitstarif — no time-of-use split (≥ 2027).
    PositionDef("netznutzung.wirkstrom", "netznutzung", "wirkstrom",
                "variable", "flat", "netznutzung_wirkstrom"),
    PositionDef("netznutzung.grundgebuehr", "netznutzung", "grundgebuehr",
                "fixed_monthly", "flat", "netznutzung_grundgebuehr"),
    # Leistungspreis — CHF per kW of the monthly peak, per month (≥ 2027).
    PositionDef("netznutzung.leistung", "netznutzung", "leistung",
                "power_monthly", "flat", "netznutzung_leistung"),
    # Combined "Netznutzung Abgaben" line (SDL + Stromreserve +
    # solidarisierte Kosten) — the 2027 Tarifblatt states it as one
    # number. Buckets with the other Abgaben/Zuschläge.
    PositionDef("netznutzung.netznutzung_abgaben", "netznutzung",
                "netznutzung_abgaben", "variable", "flat",
                "energiebezug_zuschlaege"),
    # --- Energiebezug ------------------------------------------------------
    # HT/NT split (≤ 2026).
    PositionDef("energiebezug.wirkstrom_ht", "energiebezug", "wirkstrom_ht",
                "variable", "ht", "energiebezug_wirkstrom"),
    PositionDef("energiebezug.wirkstrom_nt", "energiebezug", "wirkstrom_nt",
                "variable", "nt", "energiebezug_wirkstrom"),
    # Seasonal split (≥ 2027): summer = Apr–Sep, winter = Oct–Mar.
    PositionDef("energiebezug.wirkstrom_sommer", "energiebezug",
                "wirkstrom_sommer", "variable", "summer",
                "energiebezug_wirkstrom"),
    PositionDef("energiebezug.wirkstrom_winter", "energiebezug",
                "wirkstrom_winter", "variable", "winter",
                "energiebezug_wirkstrom"),
    # --- Abgaben (itemised, ≤ 2026 style) -------------------------------
    PositionDef("abgaben.sdl_swissgrid", "abgaben", "sdl_swissgrid",
                "variable", "flat", "energiebezug_zuschlaege"),
    PositionDef("abgaben.stromreserve", "abgaben", "stromreserve",
                "variable", "flat", "energiebezug_zuschlaege"),
    PositionDef("abgaben.solidarisierte_kosten", "abgaben",
                "solidarisierte_kosten", "variable", "flat",
                "energiebezug_zuschlaege"),
    PositionDef("abgaben.netzzuschlag", "abgaben", "netzzuschlag",
                "variable", "flat", "energiebezug_zuschlaege"),
    # --- Messtarif ---------------------------------------------------------
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

    @property
    def has_power_position(self) -> bool:
        """True if any loaded period carries a ``power_monthly`` position.

        Used to decide whether the (more expensive) month-scoped cost
        rebuild path is needed at all — pure kWh/HT-NT tariffs skip it.
        """
        power_keys = {
            pdef.key for pdef in POSITIONS if pdef.kind == "power_monthly"
        }
        return any(
            power_keys & set(p.positions) for p in self._periods
        )

    def __len__(self) -> int:
        return len(self._periods)


def is_hochtarif(dt_utc: datetime) -> bool:
    """HT = Mo–Fr, 07:00–19:00 (Europe/Zurich). NT otherwise."""
    local = dt_utc.astimezone(_LOCAL_TZ)
    if local.weekday() >= 5:           # Sat=5, Sun=6
        return False
    return 7 <= local.hour < 19


def is_summer(dt_utc: datetime) -> bool:
    """Summer = April–September (Europe/Zurich). Winter otherwise.

    The seasonal energy split introduced for 2027: Sommer 1. April – 30.
    September, Winter 1. Oktober – 31. März.
    """
    local = dt_utc.astimezone(_LOCAL_TZ)
    return 4 <= local.month <= 9


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


# Bundled with the integration.
_BUNDLED_DEFAULT_FILENAME = "default_tariffs.yaml"

# Sidecar file holding the sha256 of the bundled content we last wrote to
# the user's tariff file. Lets us tell "still our managed copy" from
# "the user edited it".
_STAMP_FILENAME = ".oberbueren_lastgang_tariffs.hash"

# sha256 of every ``default_tariffs.yaml`` the integration shipped before
# the stamp file existed (≤ v0.7.5). An untouched copy of one of these is
# still recognised as ours and gets updated in place.
_LEGACY_MANAGED_HASHES: frozenset[str] = frozenset(
    {
        # v0.2.1 … v0.7.5 (2026 HT/NT defaults, unchanged for years)
        "23bb2ee3f8dcffa38f5191371e05bb8d9310a0506b87f8a7203abeca42e2a52e",
    }
)

SyncResult = Literal[
    "created", "updated", "unchanged", "unmanaged", "user_modified"
]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bundled_tariffs_text() -> str:
    """Bundled ``default_tariffs.yaml`` text, or the embedded placeholder
    when running from a checkout that somehow lacks the file."""
    bundled = Path(__file__).parent / _BUNDLED_DEFAULT_FILENAME
    if bundled.exists():
        return bundled.read_text(encoding="utf-8")
    _LOGGER.warning(
        "Bundled %s missing — falling back to the embedded placeholder",
        _BUNDLED_DEFAULT_FILENAME,
    )
    return _EXAMPLE_YAML


def sync_managed_tariffs(config_dir: Path | str, manage: bool) -> SyncResult:
    """Keep ``<config>/oberbueren_lastgang_tariffs.yaml`` in step with the
    bundled Oberbüren defaults.

    This integration is specific to one utility, so by default the tariff
    file is *managed*: a new bundled version (e.g. a new-year price change
    shipped with an update) replaces the user's copy automatically. A
    user who wants to hand-tune the file turns the "manage tariffs"
    option off, and then this only ever creates the file when missing.

    Outcomes:

    * ``created``       — no user file existed; wrote the bundled defaults.
    * ``unchanged``     — user file already byte-identical to the bundled.
    * ``updated``       — user file was our unmodified managed copy (or a
      known older shipped default) and a newer bundled version replaced it.
    * ``unmanaged``     — ``manage`` is False; left an existing file alone.
    * ``user_modified`` — file differs from both the bundled version and
      our last managed copy → hand-edited; left untouched. The caller
      surfaces a repair notice.
    """
    cfg = Path(config_dir)
    user_path = cfg / TARIFFS_FILENAME
    stamp_path = cfg / _STAMP_FILENAME

    bundled = _bundled_tariffs_text()
    bundled_hash = _sha256(bundled)

    if not user_path.exists():
        user_path.write_text(bundled, encoding="utf-8")
        stamp_path.write_text(bundled_hash, encoding="utf-8")
        _LOGGER.info("Installed managed tariff file at %s", user_path)
        return "created"

    if not manage:
        return "unmanaged"

    current = user_path.read_text(encoding="utf-8")
    current_hash = _sha256(current)
    stamp = (
        stamp_path.read_text(encoding="utf-8").strip()
        if stamp_path.exists()
        else None
    )

    if current_hash == bundled_hash:
        if stamp != bundled_hash:
            stamp_path.write_text(bundled_hash, encoding="utf-8")
        return "unchanged"

    is_our_copy = current_hash == stamp or current_hash in _LEGACY_MANAGED_HASHES
    if is_our_copy:
        user_path.write_text(bundled, encoding="utf-8")
        stamp_path.write_text(bundled_hash, encoding="utf-8")
        _LOGGER.info(
            "Updated managed tariff file %s to the bundled version", user_path
        )
        return "updated"

    _LOGGER.warning(
        "Tariff file %s was edited — auto-update skipped. Turn off "
        "'manage tariffs' in the options to keep your edits, or delete "
        "the file to adopt the bundled version.",
        user_path,
    )
    return "user_modified"


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
# Fixed monthly fees are in CHF; the demand charge is in CHF/kW/Monat.
#
# A period carries only the positions that apply to it. Available keys:
#   netznutzung:  wirkstrom_ht / wirkstrom_nt  (Rp/kWh, HT/NT ≤ 2026)
#                 wirkstrom                    (Rp/kWh, Einheitstarif ≥ 2027)
#                 grundgebuehr                 (CHF/Monat)
#                 leistung                     (CHF/kW/Monat, Monatsspitze)
#                 netznutzung_abgaben          (Rp/kWh, kombinierte Zeile)
#   energiebezug: wirkstrom_ht / wirkstrom_nt  (Rp/kWh, HT/NT ≤ 2026)
#                 wirkstrom_sommer / wirkstrom_winter  (Rp/kWh, ≥ 2027)
#   abgaben:      sdl_swissgrid, stromreserve, solidarisierte_kosten,
#                 netzzuschlag                 (all Rp/kWh)
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
