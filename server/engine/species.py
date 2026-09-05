"""Species table and the crown growth model.

Everything here is data-driven from ``rules/species.json``: mature sizes,
sapling sizes and the time constant of the saturating growth curve

    size(t) = sapling + (mature - sapling) * (1 - exp(-t / tau))

are read from the file, never hard-coded.
"""
from __future__ import annotations

import json
import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from ..schemas import Species

DEFAULT_SAPLING_CROWN_D_M = 1.5
IMPUTE_TAU_YEARS = 15.0


def rules_dir() -> Path:
    """Directory holding the rule packs and the species table.

    ``CANOPY_RULES_DIR`` overrides the default ``<project>/rules`` so a
    container can mount its own packs without touching the code.
    """
    override = os.environ.get("CANOPY_RULES_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "rules"


@lru_cache(maxsize=1)
def _species_file() -> dict:
    with (rules_dir() / "species.json").open(encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def _species_table() -> dict[str, Species]:
    return {row["id"]: Species(**row) for row in _species_file()["species"]}


def load_species() -> dict[str, Species]:
    """All species keyed by id, parsed once from ``rules/species.json``."""
    return dict(_species_table())


def species_or_default(species_id: str) -> Species:
    """Species by id. Raises ``KeyError`` naming the unknown id."""
    try:
        return _species_table()[species_id]
    except KeyError:
        known = ", ".join(sorted(_species_table()))
        raise KeyError(f"unknown species '{species_id}' (known: {known})") from None


def _growth_fraction(year: float, tau: float) -> float:
    """Share of the sapling-to-mature growth completed after ``year`` years."""
    t = max(float(year), 0.0)
    if tau <= 0:
        return 1.0
    return 1.0 - math.exp(-t / tau)


def crown_d_at(sp: Species, year: float) -> float:
    """Crown diameter (m) of a tree of species ``sp`` planted ``year`` years ago."""
    frac = _growth_fraction(year, sp.growth_tau_years)
    return sp.sapling_crown_d_m + (sp.mature_crown_d_m - sp.sapling_crown_d_m) * frac


def height_at(sp: Species, year: float) -> float:
    """Tree height (m) after ``year`` years, same curve as the crown."""
    frac = _growth_fraction(year, sp.growth_tau_years)
    return sp.sapling_height_m + (sp.mature_height_m - sp.sapling_height_m) * frac


def genus_crown_table() -> dict[str, float]:
    """Typical mature crown diameter per genus from the species file."""
    return dict(_species_file().get("genus_crown_d_m", {}))


def default_crown_d() -> float:
    """Fallback mature crown diameter for unknown genera."""
    return float(_species_file().get("default_crown_d_m", 8.0))


def _genus_key(genus: Optional[str], species_name: Optional[str]) -> Optional[str]:
    """Normalised genus name from an explicit genus or the first word of a botanical name."""
    for candidate in (genus, species_name):
        tokens = [t for t in (candidate or "").replace("×", " ").split() if t.lower() != "x"]
        if tokens:
            return tokens[0].capitalize()
    return None


def impute_crown(genus: Optional[str], species_name: Optional[str], age_years: Optional[int]) -> float:
    """Estimate the crown diameter of an existing tree with no measured crown.

    The genus table gives the mature diameter (default when unknown); when the
    age is known the value is scaled back along the growth curve with tau 15.
    """
    table = genus_crown_table()
    key = _genus_key(genus, species_name)
    mature = table.get(key, default_crown_d()) if key else default_crown_d()
    if age_years is None:
        return round(mature, 1)
    frac = _growth_fraction(age_years, IMPUTE_TAU_YEARS)
    return round(DEFAULT_SAPLING_CROWN_D_M + (mature - DEFAULT_SAPLING_CROWN_D_M) * frac, 1)
