"""Pydantic contracts shared by the HTTP API, the agent tools, the MCP server
and the engine. Every JSON that crosses the wire is one of these models.
Geometry on the wire is always GeoJSON in WGS84 (lon, lat)."""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

Side = Literal["left", "right"]
SideChoice = Literal["both", "left", "right"]
Mode = Literal["grid", "pack"]
Verdict = Literal["valid", "conditional", "invalid"]
RuleMode = Literal["must", "should"]
Basis = Literal["measured", "estimated", "unknown"]
LayerKey = Literal["axis", "carriageway", "sidewalks", "plantable", "buildings", "cycleways",
                   "parking", "junctions", "existing_trees", "corridor"]

YEARS = (0, 5, 10, 20, 30)


class FeatureCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[dict[str, Any]] = Field(default_factory=list)


# ----------------------------------------------------------------------------- rules
class Source(BaseModel):
    title: str
    publisher: Optional[str] = None
    url: Optional[str] = None
    date: Optional[str] = None
    licence: Optional[str] = None


class Rule(BaseModel):
    id: str
    label: str
    subject: Literal["trunk", "mature_crown", "pit"]
    reference: Literal["carriageway", "cycleway", "building", "existing_tree", "junction",
                       "sidewalk_remaining_width", "plantable_surface"]
    min_distance_m: Optional[float] = None      # None for boolean rules (plantable_surface)
    mode: RuleMode
    source_ref: Optional[str] = None            # e.g. "Abschnitt 5.2"
    quote: Optional[str] = None                 # verbatim sentence from the source, if any
    description: Optional[str] = None
    assumption: bool = False                    # True = editable default, NOT from the cited standard
    enabled: bool = True
    overridden: bool = False                    # set when a session changed value/mode/enabled


class RulePack(BaseModel):
    id: str
    name: str
    jurisdiction: str
    source: Source
    notes: Optional[str] = None
    rules: list[Rule]


class RuleOverride(BaseModel):
    min_distance_m: Optional[float] = Field(None, ge=0, le=50)
    mode: Optional[RuleMode] = None
    enabled: Optional[bool] = None


# ----------------------------------------------------------------------------- species
class Species(BaseModel):
    id: str
    name_lat: str
    name_de: str
    name_en: str
    size_class: Literal["small", "medium", "large"]
    mature_crown_d_m: float
    mature_height_m: float
    sapling_crown_d_m: float = 1.5
    sapling_height_m: float = 4.0
    growth_tau_years: float = 15.0      # crown_d(t) = sapling + (mature - sapling) * (1 - exp(-t / tau))
    notes: Optional[str] = None


# ----------------------------------------------------------------------------- cities & streets
class Basemap(BaseModel):
    id: str
    label: str
    tiles: list[str]                    # XYZ or WMS templates usable by MapLibre raster source
    tile_size: int = 256
    attribution: str
    max_zoom: int = 19
    default: bool = False


class CityInfo(BaseModel):
    id: str
    name: str
    country: str
    epsg: int
    center: list[float]                 # [lon, lat]
    zoom: float = 15.5
    utc_offset_hours: float = 2.0       # summer time, used by the shade tool
    basemaps: list[Basemap]
    demo_streets: list[str]
    tree_source: str                    # human-readable attribution
    geometry_source: str


class StreetRequest(BaseModel):
    city: str
    query: Optional[str] = Field(None, max_length=120, description="street name, e.g. 'Langstrasse'")
    line: Optional[list[list[float]]] = Field(None, description="user-drawn axis [[lon,lat],...] in WGS84")
    name: Optional[str] = None
    point: Optional[tuple[Annotated[float, Field(ge=-180, lt=180, allow_inf_nan=False)],
                          Annotated[float, Field(ge=-80, le=84, allow_inf_nan=False)]]] = None

    @model_validator(mode="after")
    def exclusive_point(self):
        if self.point is not None and (self.query is not None or self.line is not None):
            raise ValueError("Provide point alone, without query or line")
        return self


class StreetStats(BaseModel):
    length_m: float
    existing_trees: int
    existing_trees_crown_imputed: int
    buildings: int
    cycleway_m: float
    junctions: int
    carriageway_m2: float
    sidewalk_m2: float
    plantable_m2: float


class StreetResponse(BaseModel):
    street_id: str
    city: str
    city_name: str
    name: str
    epsg: int
    length_m: float
    bbox: list[float]                   # [minlon, minlat, maxlon, maxlat]
    center: list[float]                 # [lon, lat]
    layers: dict[str, FeatureCollection]   # keys = LayerKey
    layer_sources: dict[str, str]       # layer -> attribution
    layer_basis: dict[str, Basis]       # layer -> measured | estimated | unknown
    stats: StreetStats
    warnings: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------------------- planning
class PlanParams(BaseModel):
    spacing_m: float = Field(8.0, ge=3, le=40)
    side: SideChoice = "both"
    species_id: str = "tilia_cordata"
    crown_diameter_m: Optional[float] = Field(None, ge=2, le=25, allow_inf_nan=False,
        description="Optional mature crown diameter for this planning scenario; not a species prediction")
    mode: Mode = "grid"
    offset_from_edge_m: float = Field(1.0, ge=0.3, le=5.0, description="carriageway edge -> trunk centre")
    pit_width_m: float = Field(2.0, ge=1.0, le=4.0, description="tree pit width across the sidewalk")
    rule_pack_id: str = "berlin_strassenbaeume_2024"
    rule_overrides: dict[str, RuleOverride] = Field(default_factory=dict)
    label: Optional[str] = Field(None, max_length=60)


class PlanRequest(PlanParams):
    street_id: str


class RuleResult(BaseModel):
    rule_id: str
    label: str
    mode: RuleMode
    required_m: Optional[float]
    measured_m: Optional[float]
    passed: Optional[bool]              # None = could not be evaluated (layer missing)
    basis: Basis
    assumption: bool = False
    note: Optional[str] = None


class SiteProps(BaseModel):
    """properties of every Point feature in ScenarioResponse.sites"""
    site_id: str
    station_m: float                    # distance along the axis
    side: Side
    verdict: Verdict
    edge_distance_m: Optional[float]    # measured trunk -> carriageway edge
    rules: list[RuleResult]
    failed_rules: list[str]
    species_id: str
    crown_d_mature_m: float
    height_mature_m: float
    crown_d_by_year: dict[str, float]   # {"0": 1.5, "5": .., "10": .., "20": .., "30": ..}
    height_by_year: dict[str, float]


class Gap(BaseModel):
    side: Side
    station_from_m: float
    station_to_m: float
    reason: str


class PlanSummary(BaseModel):
    total: int
    valid: int
    conditional: int
    invalid: int
    planted: int                        # valid + conditional
    must_failures: int
    should_failures: int
    failures_by_rule: dict[str, int]
    gaps: list[Gap] = Field(default_factory=list)


class CanopyYear(BaseModel):
    year: int
    new_crown_area_m2: float
    total_crown_area_m2: float          # new + existing, union, clipped to corridor
    cover_corridor_pct: float
    cover_street_pct: float             # share of carriageway+sidewalk area under crowns
    sidewalk_under_crown_pct: float


class CanopyResult(BaseModel):
    corridor_area_m2: float
    street_area_m2: float
    sidewalk_area_m2: float
    existing_cover_corridor_pct: float
    years: list[CanopyYear]


class ScenarioResponse(BaseModel):
    scenario_id: str
    street_id: str
    label: str
    created_at: str
    params: PlanParams
    rules_used: RulePack
    species: Species
    sites: FeatureCollection            # Point features, properties = SiteProps
    summary: PlanSummary
    canopy: CanopyResult


class CanopyRequest(BaseModel):
    scenario_id: str
    years: list[int] = Field(default_factory=lambda: list(YEARS))


class ShadeRequest(BaseModel):
    scenario_id: str
    year: int = Field(30, ge=0, le=60)
    month: int = Field(7, ge=1, le=12)
    day: int = Field(15, ge=1, le=31)
    hour: float = Field(15.0, ge=0, le=24, description="local civil time")
    include_existing: bool = True


class ShadeResult(BaseModel):
    scenario_id: str
    year: int
    when: str                           # "2026-07-15 15:00 local"
    sun_elevation_deg: float
    sun_azimuth_deg: float
    shadows: FeatureCollection          # Polygon features {kind: "new"|"existing", site_id?}
    shaded_sidewalk_pct: float
    shaded_street_pct: float
    shaded_corridor_pct: float


class CompareRequest(BaseModel):
    scenario_ids: list[str] = Field(min_length=1, max_length=6)


class TemperatureRequest(BaseModel):
    scenario_id: str
    year: int = Field(30, ge=0, le=30)
    reference_air_c: float = Field(30, ge=0, le=55, allow_inf_nan=False)
    month: int = Field(7, ge=6, le=8)
    day: int = Field(15, ge=1, le=31)
    hour: float = Field(15, ge=6, le=18, allow_inf_nan=False)


class TemperatureResult(BaseModel):
    scenario_id: str
    year: int
    when: str
    reference_air_c: float
    proposed_air_c: float
    proposed_air_low_c: float
    proposed_air_high_c: float
    cooling_c: float
    cooling_low_c: float
    cooling_high_c: float
    existing_local_canopy_pct: float
    proposed_local_canopy_pct: float
    existing_sidewalk_shade_pct: float
    proposed_sidewalk_shade_pct: float
    sample_count: int
    method: str
    source_url: str
    limitations: list[str]


class CompareRow(BaseModel):
    scenario_id: str
    street_id: str
    street_name: str
    city: str
    city_name: str
    tree_source: str
    label: str
    spacing_m: float
    side: SideChoice
    species_id: str
    crown_diameter_m: float
    mode: Mode
    planted: int
    valid: int
    conditional: int
    invalid: int
    new_crown_area_30_m2: float
    cover_corridor_pct_30: float
    cover_street_pct_30: float
    sidewalk_under_crown_pct_30: float


class CompareResponse(BaseModel):
    rows: list[CompareRow]
    best_by_cover: Optional[str] = None


# ----------------------------------------------------------------------------- config & agent
class AgentInfo(BaseModel):
    enabled: bool
    provider: Optional[str] = None
    model: Optional[str] = None
    turns_per_hour: int
    example_prompts: list[str]


class ConfigResponse(BaseModel):
    cities: list[CityInfo]
    rule_packs: list[RulePack]
    species: list[Species]
    defaults: PlanParams
    agent: AgentInfo
    version: str


class AgentRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    street_id: Optional[str] = None
    scenario_id: Optional[str] = None


class QuotaInfo(BaseModel):
    session_id: str
    turns_remaining_hour: int
    turns_remaining_day: int
