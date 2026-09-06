"""Exploratory transfer of an observed canopy association, not a local forecast.

Tacoma 2022 hourly model, Scientific Reports (2024), Table 1:
canopy slope -0.006 C/percentage point, daytime interaction +0.001.
Daytime central cooling slope is therefore 0.005 C/percentage point.
The endpoints of the two reported marginal intervals give a sensitivity
envelope of 0 to 0.009 C/point. This is NOT a joint confidence interval and
does not account for transferring the relationship to another street/city.
"""
from __future__ import annotations

import math
from datetime import date

import shapely
from shapely.geometry import LineString

from ..adapters.base import StreetContext
from ..schemas import Species, TemperatureRequest, TemperatureResult
from .canopy import PLANTED, clip_to, crown_union, existing_crown_d
from .rules import axis_frame, flatten
from .shade import shade_polygons
from .sites import SiteGeom
from .species import crown_d_at

SOURCE_URL = "https://www.nature.com/articles/s41598-024-51921-y/tables/1"
COOLING_PER_CANOPY_POINT = 0.005
SENSITIVITY_HIGH = 0.009


def temperature_comparison(ctx: StreetContext, sites: list[SiteGeom], species: Species,
                           req: TemperatureRequest) -> TemperatureResult:
    # Reject impossible summer dates instead of silently rolling into another month.
    date(date.today().year, req.month, req.day)
    sidewalk = clip_to(ctx.sidewalks, ctx.corridor)
    if sidewalk.is_empty or sidewalk.area <= 0:
        raise ValueError("Temperature comparison needs mapped sidewalks. Choose another street or draw a section with sidewalk data.")

    # Sample actual sidewalk cross-sections along the route, at most 400 stations.
    stations = max(1, min(400, math.ceil(ctx.length_m / 10)))
    samples = []
    for i in range(stations):
        origin, (nx, ny) = axis_frame(ctx.axis, (i + 0.5) * ctx.length_m / stations)
        cross = LineString([(origin.x - nx * 15, origin.y - ny * 15),
                            (origin.x + nx * 15, origin.y + ny * 15)])
        for piece in flatten(sidewalk.intersection(cross)):
            if piece.geom_type == "LineString" and piece.length > 0.1:
                samples.append(piece.interpolate(0.5, normalized=True))
    if not samples:
        samples = [sidewalk.representative_point()]

    existing = crown_union([t.pt for t in ctx.existing_trees],
                           [existing_crown_d(t) / 2 for t in ctx.existing_trees])
    new = crown_union([s.pt for s in sites if s.verdict in PLANTED],
                      [crown_d_at(species, req.year) / 2 for s in sites if s.verdict in PLANTED])
    proposed = shapely.union_all([existing, new])
    # Match the study's local 10 m scale; do not use corridor-wide cover as a proxy.
    neighborhoods = [point.buffer(10, quad_segs=12) for point in samples]
    before = sum(existing.intersection(area).area / area.area for area in neighborhoods) / len(samples) * 100
    after = sum(proposed.intersection(area).area / area.area for area in neighborhoods) / len(samples) * 100
    added = max(0.0, after - before)
    cooling = added * COOLING_PER_CANOPY_POINT
    high = added * SENSITIVITY_HIGH
    shade_args = dict(year=req.year, month=req.month, day=req.day, hour=req.hour, include_existing=True)
    baseline_shade = shade_polygons(ctx, [], species, **shade_args)
    proposed_shade = shade_polygons(ctx, sites, species, **shade_args)
    return TemperatureResult(
        scenario_id=req.scenario_id, year=req.year, when=proposed_shade.when,
        reference_air_c=req.reference_air_c,
        proposed_air_c=round(req.reference_air_c - cooling, 2),
        proposed_air_low_c=round(req.reference_air_c - high, 2),
        proposed_air_high_c=req.reference_air_c,
        cooling_c=round(cooling, 2), cooling_low_c=0, cooling_high_c=round(high, 2),
        existing_local_canopy_pct=round(before, 1), proposed_local_canopy_pct=round(after, 1),
        existing_sidewalk_shade_pct=baseline_shade.shaded_sidewalk_pct,
        proposed_sidewalk_shade_pct=proposed_shade.shaded_sidewalk_pct,
        sample_count=len(samples), method="tacoma-daytime-association-v1", source_url=SOURCE_URL,
        limitations=[
            "Exploratory research transfer; not calibrated or validated for this street. Actual cooling can fall outside the displayed sensitivity range.",
            "The current-street temperature is your input, not a weather reading. Both cases use the same weather; tree age does not include future climate change.",
            "The range combines published coefficient endpoints; it is not a statistical confidence or forecast interval.",
            "Air comparison uses added canopy within 10 m of sampled sidewalks. Date and time affect the separate tree-shade calculation, not the air-temperature coefficient.",
            "Existing trees are held constant. Proposed amber positions are included; excluded positions are omitted. Tree growth and inventories may be estimated or incomplete.",
            "Building shade, street ventilation, humidity, soil moisture and species-specific transpiration are not modeled. This does not estimate pavement temperature or feels-like temperature.",
            *( ["This street uses illustrative demo geometry and trees."] if ctx.city.id == "demo" else [] ),
        ],
    )
