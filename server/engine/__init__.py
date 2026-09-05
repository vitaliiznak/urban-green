"""Allee planning engine: species growth, rule evaluation, site generation,
canopy projection, shade and the synthetic demo street."""
from .canopy import canopy_metrics
from .rules import PreparedContext, apply_overrides, evaluate_site, load_rule_packs
from .shade import shade_polygons, sun_position
from .sites import SiteGeom, plan_sites
from .species import crown_d_at, height_at, impute_crown, load_species, species_or_default
from .synthetic import demo_city_info, synthetic_street

__all__ = [
    "PreparedContext",
    "SiteGeom",
    "apply_overrides",
    "canopy_metrics",
    "crown_d_at",
    "demo_city_info",
    "evaluate_site",
    "height_at",
    "impute_crown",
    "load_rule_packs",
    "load_species",
    "plan_sites",
    "shade_polygons",
    "species_or_default",
    "sun_position",
    "synthetic_street",
]
