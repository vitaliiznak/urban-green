"""Sun position (NOAA solar calculator) and crown shadow projection.

Each crown is a sphere of diameter D whose centre sits at height h - D/2.
Under parallel sunlight its ground shadow is an ellipse displaced away from
the sun; the ellipses are unioned and clipped to the corridor for the
shaded-share metrics.
"""
from __future__ import annotations

import math
from datetime import date

import shapely
from shapely.affinity import rotate, scale, translate
from shapely.geometry import Point, Polygon
from shapely.geometry.base import BaseGeometry

from ..adapters.base import StreetContext
from ..crs import feature
from ..schemas import FeatureCollection, ShadeResult, Species
from .canopy import PLANTED, clip_to, existing_crown_d, pct
from .sites import SiteGeom
from .species import crown_d_at, height_at

MIN_ELEVATION_DEG = 5.0
MAX_DISPLACEMENT_M = 60.0
EXISTING_HEIGHT_FACTOR = 1.5
UNIT_CIRCLE = Point(0.0, 0.0).buffer(1.0, quad_segs=12)
LOW_SUN_NOTE = "sun below 5°, no usable shadows"


# --------------------------------------------------------------------------- NOAA
def _julian_day(year: int, month: int, day: int, hour_utc: float) -> float:
    """Julian day of the given UTC civil date/time (Meeus, valid for the Gregorian calendar)."""
    y, m = (year - 1, month + 12) if month <= 2 else (year, month)
    a = y // 100
    b = 2 - a + a // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + day + b - 1524.5 + hour_utc / 24.0


def _refraction_deg(elev_deg: float) -> float:
    """NOAA approximate atmospheric refraction correction (degrees)."""
    if elev_deg > 85.0:
        return 0.0
    t = math.tan(math.radians(elev_deg))
    if elev_deg > 5.0:
        corr = 58.1 / t - 0.07 / t ** 3 + 0.000086 / t ** 5
    elif elev_deg > -0.575:
        corr = 1735.0 + elev_deg * (-518.2 + elev_deg * (103.4 + elev_deg * (-12.79 + elev_deg * 0.711)))
    else:
        corr = -20.772 / t
    return corr / 3600.0


def sun_position(lat: float, lon: float, year: int, month: int, day: int, hour_local: float,
                 utc_offset_hours: float) -> tuple[float, float]:
    """Solar elevation and azimuth (degrees, azimuth clockwise from north).

    NOAA solar calculator algorithm; ``hour_local`` is civil time in a zone
    ``utc_offset_hours`` ahead of UTC. Elevation includes atmospheric refraction.
    """
    jd = _julian_day(year, month, day, hour_local - utc_offset_hours)
    jc = (jd - 2451545.0) / 36525.0
    l0 = (280.46646 + jc * (36000.76983 + 0.0003032 * jc)) % 360.0
    m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    ecc = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    m_rad = math.radians(m)
    centre = (math.sin(m_rad) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
              + math.sin(2 * m_rad) * (0.019993 - 0.000101 * jc)
              + math.sin(3 * m_rad) * 0.000289)
    omega = math.radians(125.04 - 1934.136 * jc)
    app_long = math.radians(l0 + centre - 0.00569 - 0.00478 * math.sin(omega))
    obliq0 = 23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0
    obliq = math.radians(obliq0 + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(obliq) * math.sin(app_long))
    y = math.tan(obliq / 2.0) ** 2
    l0_rad = math.radians(l0)
    eot_min = 4.0 * math.degrees(
        y * math.sin(2 * l0_rad) - 2 * ecc * math.sin(m_rad)
        + 4 * ecc * y * math.sin(m_rad) * math.cos(2 * l0_rad)
        - 0.5 * y * y * math.sin(4 * l0_rad) - 1.25 * ecc * ecc * math.sin(2 * m_rad))
    true_solar_min = (hour_local * 60.0 + eot_min + 4.0 * lon - 60.0 * utc_offset_hours) % 1440.0
    hour_angle = math.radians(true_solar_min / 4.0 - 180.0)

    lat_rad = math.radians(lat)
    cos_zenith = (math.sin(lat_rad) * math.sin(decl)
                  + math.cos(lat_rad) * math.cos(decl) * math.cos(hour_angle))
    zenith = math.acos(max(-1.0, min(1.0, cos_zenith)))
    elev = 90.0 - math.degrees(zenith)
    elev += _refraction_deg(elev)

    denom = math.cos(lat_rad) * math.sin(zenith)
    if abs(denom) < 1e-12:
        azimuth = 180.0
    else:
        cos_az = (math.sin(lat_rad) * math.cos(zenith) - math.sin(decl)) / denom
        base = math.degrees(math.acos(max(-1.0, min(1.0, cos_az))))
        azimuth = (base + 180.0) % 360.0 if hour_angle > 0 else (540.0 - base) % 360.0
    return elev, azimuth


# --------------------------------------------------------------------------- shadows
def shadow_ellipse(trunk: Point, crown_d: float, height: float, elev_deg: float,
                   azimuth_deg: float) -> Polygon:
    """Ground shadow of a spherical crown as an ellipse displaced away from the sun."""
    r = crown_d / 2.0
    centre_h = max(height - r, r)
    elev = math.radians(elev_deg)
    displacement = min(centre_h / math.tan(elev), MAX_DISPLACEMENT_M)
    along = r / math.sin(elev)
    direction = azimuth_deg + 180.0
    dx, dy = math.sin(math.radians(direction)), math.cos(math.radians(direction))
    ellipse = scale(UNIT_CIRCLE, xfact=along, yfact=r, origin=(0.0, 0.0))
    ellipse = rotate(ellipse, 90.0 - direction, origin=(0.0, 0.0))
    return translate(ellipse, trunk.x + displacement * dx, trunk.y + displacement * dy)


def _when(year: int, month: int, day: int, hour: float) -> str:
    hh = int(hour)
    mm = int(round((hour - hh) * 60))
    if mm == 60:
        hh, mm = hh + 1, 0
    return f"{year:04d}-{month:02d}-{day:02d} {hh:02d}:{mm:02d} local"


def _existing_height(crown_d: float, height_m: float | None) -> float:
    return float(height_m) if height_m else EXISTING_HEIGHT_FACTOR * crown_d


def shade_polygons(ctx: StreetContext, sites: list[SiteGeom], sp: Species, *, year: int, month: int,
                   day: int, hour: float, include_existing: bool, scenario_id: str = "") -> ShadeResult:
    """Shadows cast by the planted sites (and existing trees) at a local time.

    ``year`` is the growth year of the scenario (crown size), the sun is
    computed for ``month``/``day`` of the current calendar year at
    ``ctx.city.center`` with the city's UTC offset. Returns per-tree shadow
    features (WGS84) and the shaded share of sidewalk, street and corridor.
    ``scenario_id`` is passed through for the caller's convenience.
    """
    lon, lat = ctx.city.center[0], ctx.city.center[1]
    calendar_year = date.today().year
    elev, azimuth = sun_position(lat, lon, calendar_year, month, day, hour, ctx.city.utc_offset_hours)
    when = _when(calendar_year, month, day, hour)
    base = dict(scenario_id=scenario_id, year=int(year), sun_elevation_deg=round(elev, 1),
                sun_azimuth_deg=round(azimuth, 1))
    if elev < MIN_ELEVATION_DEG:
        return ShadeResult(**base, when=f"{when} ({LOW_SUN_NOTE})", shadows=FeatureCollection(),
                           shaded_sidewalk_pct=0.0, shaded_street_pct=0.0, shaded_corridor_pct=0.0)

    polygons: list[BaseGeometry] = []
    features: list[dict] = []
    crown_d, height = crown_d_at(sp, year), height_at(sp, year)
    for site in sites:
        if site.verdict not in PLANTED:
            continue
        poly = shadow_ellipse(site.pt, crown_d, height, elev, azimuth)
        polygons.append(poly)
        features.append(feature(poly, ctx.epsg, {"kind": "new", "site_id": site.site_id}))
    if include_existing:
        for tree in ctx.existing_trees:
            d = existing_crown_d(tree)
            poly = shadow_ellipse(tree.pt, d, _existing_height(d, tree.height_m), elev, azimuth)
            polygons.append(poly)
            features.append(feature(poly, ctx.epsg, {"kind": "existing", "tree_id": tree.id}))

    corridor = ctx.corridor
    shaded = clip_to(shapely.union_all(polygons) if polygons else None, corridor)
    sidewalk = clip_to(ctx.sidewalks, corridor)
    street = clip_to(shapely.union_all([g for g in (ctx.carriageway, ctx.sidewalks)
                                      if g is not None and not g.is_empty]), corridor)
    return ShadeResult(
        **base,
        when=when,
        shadows=FeatureCollection(features=features),
        shaded_sidewalk_pct=pct(shaded.intersection(sidewalk).area, sidewalk.area),
        shaded_street_pct=pct(shaded.intersection(street).area, street.area),
        shaded_corridor_pct=pct(shaded.area, corridor.area),
    )
