"""OpenStreetMap helpers shared by every adapter, plus the generic "osm" adapter.

Three responsibilities live here:

* HTTP plumbing: one shared ``httpx.AsyncClient`` with the Urban Green User-Agent, a
  15-minute in-process response cache keyed by URL + parameters, Overpass with a
  mirror fallback, and Nominatim search.
* Street resolution: Nominatim picks the street, Overpass fetches every way with
  that name inside the city bbox, ``linemerge`` joins them into ONE axis (longest
  part wins, capped at 2500 m around the middle).
* Geometry estimation from OSM tags: carriageway polygons for every highway in
  the corridor, sidewalk bands, cycle lanes, building footprints, junction points
  and ``natural=tree`` nodes with crown diameters imputed when missing.

All geometry is built in the city's metric CRS (``epsg``); only the HTTP layer
speaks WGS84.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from datetime import date
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from shapely import make_valid
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge, nearest_points, substring, unary_union

from .. import crs
from ..schemas import Basemap, Basis, CityInfo
from .base import CityAdapter, ExistingTree, StreetContext, StreetNotFound, UpstreamUnavailable

USER_AGENT = "urban-green/0.1 (street-tree planning demo)"
OVERPASS_URLS = ("https://maps.mail.ru/osm/tools/overpass/api/interpreter",   # fastest public mirror in probes (0.8 s)
                 "https://overpass-api.de/api/interpreter",
                 "https://overpass.private.coffee/api/interpreter",
                 "https://overpass.kumi.systems/api/interpreter")
OVERPASS_URLS_CH = ("https://overpass.osm.ch/api/interpreter",) + OVERPASS_URLS   # Swiss extract, 0.2 s; Switzerland only
OVERPASS_TIMEOUT_S = 60.0
OVERPASS_CONCURRENCY = 2            # simultaneous Overpass requests per process (public mirrors rate-limit per IP)
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSM_ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"

HTTP_TIMEOUT_S = 30.0
OVERPASS_ATTEMPTS = 2
OVERPASS_RETRY_DELAY_S = 3.0
CACHE_TTL_S = 15 * 60.0             # in-memory
CACHE_MAX_ENTRIES = 256
DISK_CACHE_TTL_S = 7 * 24 * 3600.0  # on-disk copy of every upstream response (survives restarts, spares the mirrors)
DISK_CACHE_DIR = os.environ.get("CANOPY_CACHE_DIR", ".cache/upstream")

CORRIDOR_HALF_WIDTH_M = 15.0
CLIP_MARGIN_M = 5.0
MAX_AXIS_M = 2500.0
SIDEWALK_WIDTH_M = 2.5
PEDESTRIAN_HALF_WIDTH_M = 4.0
DEFAULT_CROWN_D_M = 8.0
AREA_BBOX_PAD_M = 35.0
AREA_BBOX_CHUNK_M = 600.0
JUNCTION_MERGE_M = 3.0
ANCHOR_TOLERANCE_M = 50.0

# Carriageway width (m) by highway class when neither `width` nor `lanes` is tagged.
DEFAULT_WIDTH_BY_CLASS: dict[str, float] = {
    "motorway": 14.0, "trunk": 12.0, "primary": 11.0, "secondary": 9.0, "tertiary": 8.0,
    "residential": 6.5, "unclassified": 6.5, "living_street": 5.0, "service": 4.0,
    "pedestrian": 0.0, "road": 6.0, "busway": 7.0,
}
DEFAULT_WIDTH_M = 6.0
# Highway classes that carry no motor carriageway at all.
NO_CARRIAGEWAY_CLASSES = frozenset({
    "pedestrian", "footway", "path", "steps", "cycleway", "bridleway", "track", "corridor",
    "platform", "elevator", "proposed", "construction", "abandoned", "razed",
})
# Classes that get sidewalks on both sides unless tagged otherwise.
SIDEWALK_DEFAULT_BOTH = frozenset({
    "primary", "secondary", "tertiary", "residential", "unclassified", "living_street",
})
# Minor ways that only count as junctions (or axis candidates) when they carry a name.
MINOR_CLASSES = frozenset({
    "footway", "path", "service", "steps", "track", "bridleway", "cycleway", "corridor",
    "platform", "elevator", "proposed", "construction",
})
CYCLE_LANE_VALUES = frozenset({"lane", "track", "opposite_lane", "opposite_track"})

BBox = tuple[float, float, float, float]  # minlon, minlat, maxlon, maxlat


# ----------------------------------------------------------------------------- cache & HTTP
_cache: dict[str, tuple[float, Any]] = {}
_client: Optional[httpx.AsyncClient] = None
_client_loop: Optional[asyncio.AbstractEventLoop] = None


def cache_key(url: str, params: dict[str, Any] | None = None, data: dict[str, Any] | None = None) -> str:
    """Stable cache key for a request: URL plus sorted query/body parameters."""
    parts = [url]
    if params:
        parts.append(urlencode(sorted(params.items())))
    if data:
        parts.append(json.dumps(data, sort_keys=True, ensure_ascii=False))
    return "|".join(parts)


def _disk_path(key: str) -> Path | None:
    """File for a cache key, or None when the disk cache is disabled (CANOPY_CACHE_DIR=)."""
    if not DISK_CACHE_DIR:
        return None
    return Path(DISK_CACHE_DIR) / (hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")


def _disk_get(key: str) -> Any | None:
    path = _disk_path(key)
    if path is None:
        return None
    try:
        if not path.exists() or time.time() - path.stat().st_mtime > DISK_CACHE_TTL_S:
            return None
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def _disk_set(key: str, value: Any) -> None:
    path = _disk_path(key)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
        tmp.replace(path)
    except (OSError, TypeError, ValueError):
        pass


def cache_get(key: str) -> Any | None:
    """Return a cached value: memory first (15 min), then the on-disk copy (7 days)."""
    hit = _cache.get(key)
    if hit is not None:
        stored_at, value = hit
        if time.monotonic() - stored_at <= CACHE_TTL_S:
            return value
        _cache.pop(key, None)
    value = _disk_get(key)
    if value is not None:
        _cache[key] = (time.monotonic(), value)
    return value


def cache_set(key: str, value: Any) -> None:
    """Store a value in memory (evicting expired/oldest entries) and on disk."""
    if len(_cache) >= CACHE_MAX_ENTRIES:
        now = time.monotonic()
        for k in [k for k, (t, _) in _cache.items() if now - t > CACHE_TTL_S]:
            _cache.pop(k, None)
        if len(_cache) >= CACHE_MAX_ENTRIES:
            oldest = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(oldest, None)
    _cache[key] = (time.monotonic(), value)
    _disk_set(key, value)


def cache_clear() -> None:
    """Drop every cached upstream response (tests)."""
    _cache.clear()


def get_client() -> httpx.AsyncClient:
    """Shared async client (30 s timeout, Urban Green User-Agent), re-created per event loop."""
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _client_loop is not loop:
        _client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, headers={"User-Agent": USER_AGENT},
                                    follow_redirects=True)
        _client_loop = loop
    return _client


async def fetch_json(url: str, *, params: dict[str, Any] | None = None,
                     data: dict[str, Any] | None = None, timeout: float = HTTP_TIMEOUT_S,
                     use_cache: bool = True) -> Any:
    """GET (or POST when ``data`` is given) a JSON document with caching.

    Raises UpstreamUnavailable on transport errors, non-2xx status or non-JSON bodies.
    """
    key = cache_key(url, params, data)
    if use_cache:
        cached = cache_get(key)
        if cached is not None:
            return cached
    client = get_client()
    try:
        if data is not None:
            resp = await client.post(url, params=params, data=data, timeout=timeout)
        else:
            resp = await client.get(url, params=params, timeout=timeout)
    except httpx.HTTPError as exc:
        raise UpstreamUnavailable(f"{url}: {exc.__class__.__name__}") from exc
    if resp.status_code >= 400:
        raise UpstreamUnavailable(f"{url}: HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise UpstreamUnavailable(f"{url}: response is not JSON") from exc
    if use_cache:
        cache_set(key, payload)
    return payload


def _is_transient(exc: UpstreamUnavailable) -> bool:
    """Rate limits and gateway errors are worth one delayed retry on the same endpoint."""
    return any(code in str(exc) for code in ("HTTP 429", "HTTP 503", "HTTP 504"))


_overpass_gate: Optional[asyncio.Semaphore] = None
_overpass_gate_loop: Optional[asyncio.AbstractEventLoop] = None


def _gate() -> asyncio.Semaphore:
    """Process-wide limit on simultaneous Overpass requests (one semaphore per event loop)."""
    global _overpass_gate, _overpass_gate_loop
    loop = asyncio.get_running_loop()
    if _overpass_gate is None or _overpass_gate_loop is not loop:
        _overpass_gate = asyncio.Semaphore(OVERPASS_CONCURRENCY)
        _overpass_gate_loop = loop
    return _overpass_gate


def overpass_urls_for(epsg: int) -> tuple[str, ...]:
    """Mirror order per CRS: Swiss streets (LV95) go to the Swiss extract first."""
    return OVERPASS_URLS_CH if epsg == 2056 else OVERPASS_URLS


async def overpass(query: str, urls: tuple[str, ...] = OVERPASS_URLS) -> dict[str, Any]:
    """Run an Overpass QL query.

    Cached responses (memory, then disk) are returned immediately. Otherwise the
    request is throttled to OVERPASS_CONCURRENCY in flight, tried on each mirror in
    turn, with one delayed retry per mirror on 429/503/504.
    """
    cached = cache_get(cache_key("overpass", data={"data": query}))
    if cached is not None:
        return cached
    errors: list[str] = []
    async with _gate():
        for url in urls:
            for attempt in range(OVERPASS_ATTEMPTS):
                try:
                    payload = await fetch_json(url, data={"data": query}, use_cache=False, timeout=OVERPASS_TIMEOUT_S)
                except UpstreamUnavailable as exc:
                    errors.append(str(exc))
                    if attempt + 1 < OVERPASS_ATTEMPTS and _is_transient(exc):
                        await asyncio.sleep(OVERPASS_RETRY_DELAY_S * (attempt + 1))
                        continue
                    break
                if not isinstance(payload, dict) or "elements" not in payload:
                    remark = payload.get("remark", "malformed response") if isinstance(payload, dict) else "malformed response"
                    errors.append(f"{url}: {remark}")
                    break
                cache_set(cache_key("overpass", data={"data": query}), payload)
                return payload
    raise UpstreamUnavailable("Overpass unavailable: " + "; ".join(errors))


async def nominatim_search(q: str, *, countrycodes: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """Nominatim free-text search (jsonv2, with geometry) -> list of result dicts."""
    params: dict[str, Any] = {"q": q, "format": "jsonv2", "polygon_geojson": 1, "limit": limit}
    if countrycodes:
        params["countrycodes"] = countrycodes
    payload = await fetch_json(NOMINATIM_URL, params=params)
    if not isinstance(payload, list):
        raise UpstreamUnavailable("Nominatim returned an unexpected payload")
    return payload


# ----------------------------------------------------------------------------- Nominatim helpers
def _normalise_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower()).replace("ß", "ss")


def pick_nominatim_street(results: list[dict[str, Any]], query_name: str | None = None) -> dict[str, Any] | None:
    """Pick the first ``osm_type=way`` + ``highway`` result, preferring an exact name match."""
    ways = [r for r in results
            if r.get("osm_type") == "way" and (r.get("category") or r.get("class")) == "highway"]
    if not ways:
        return None
    if query_name:
        wanted = _normalise_name(query_name)
        for r in ways:
            if _normalise_name(str(r.get("name") or "")) == wanted:
                return r
    return ways[0]


def nominatim_bbox(result: dict[str, Any]) -> BBox:
    """``boundingbox`` [minlat, maxlat, minlon, maxlon] -> (minlon, minlat, maxlon, maxlat)."""
    s, n, w, e = (float(v) for v in result["boundingbox"])
    return (w, s, e, n)


def pad_bbox(bbox: BBox, deg: float) -> BBox:
    """Grow a lon/lat bbox by ``deg`` degrees on every side."""
    return (bbox[0] - deg, bbox[1] - deg, bbox[2] + deg, bbox[3] + deg)


# ----------------------------------------------------------------------------- Overpass QL
def escape_ql(value: str) -> str:
    """Escape a string for use inside an Overpass QL double-quoted literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _ql_bbox(bbox: BBox) -> str:
    minlon, minlat, maxlon, maxlat = bbox
    return f"{minlat:.6f},{minlon:.6f},{maxlat:.6f},{maxlon:.6f}"


def overpass_name_query(name: str, bbox: BBox) -> str:
    """Every ``highway`` way with exactly this name inside the bbox, with geometry."""
    return (f'[out:json][timeout:25];way["highway"]["name"="{escape_ql(name)}"]'
            f'({_ql_bbox(bbox)});out tags geom;')


def overpass_area_query(bboxes: list[BBox]) -> str:
    """Highways, buildings and trees inside the union of the given bboxes."""
    body = "".join(
        f'way["highway"]({_ql_bbox(b)});way["building"]({_ql_bbox(b)});'
        f'node["natural"="tree"]({_ql_bbox(b)});'
        f'node["highway"="crossing"]({_ql_bbox(b)});' for b in bboxes)
    return f"[out:json][timeout:25];({body});out tags geom;"


def corridor_bboxes(axis: LineString, epsg: int, *, pad_m: float = AREA_BBOX_PAD_M,
                    chunk_m: float = AREA_BBOX_CHUNK_M) -> list[BBox]:
    """Lon/lat bboxes covering the corridor, one per ~600 m chunk of the axis.

    Chunking keeps the requested area close to the corridor for diagonal streets
    instead of one large square around the whole street.
    """
    n = max(1, math.ceil(axis.length / chunk_m))
    step = axis.length / n
    boxes: list[BBox] = []
    for i in range(n):
        seg = substring(axis, i * step, (i + 1) * step)
        area = seg.buffer(pad_m)
        minlon, minlat, maxlon, maxlat = crs.to_wgs(area, epsg).bounds
        boxes.append((minlon, minlat, maxlon, maxlat))
    return boxes


# ----------------------------------------------------------------------------- element geometry
def way_line(el: dict[str, Any], epsg: int) -> LineString | None:
    """Overpass way element (with ``geometry``) -> metric LineString, or None."""
    pts = [(p["lon"], p["lat"]) for p in el.get("geometry") or [] if "lon" in p and "lat" in p]
    if len(pts) < 2:
        return None
    line = crs.to_metric(LineString(pts), epsg)
    return line if line.length > 0 else None


def way_polygon(el: dict[str, Any], epsg: int) -> Polygon | MultiPolygon | None:
    """Closed Overpass way -> valid metric polygon, or None when it is not a ring."""
    pts = [(p["lon"], p["lat"]) for p in el.get("geometry") or [] if "lon" in p and "lat" in p]
    if len(pts) < 4 or pts[0] != pts[-1]:
        return None
    poly = make_valid(crs.to_metric(Polygon(pts), epsg))
    poly = _polygonal(poly)
    return None if poly.is_empty else poly


def node_point(el: dict[str, Any], epsg: int) -> Point | None:
    """Overpass node element -> metric Point, or None when it has no coordinates."""
    if "lat" not in el or "lon" not in el:
        return None
    return crs.to_metric(Point(el["lon"], el["lat"]), epsg)


def _polygonal(geom: BaseGeometry) -> BaseGeometry:
    """Keep only the polygonal parts of a geometry (drops slivers, lines, points)."""
    if geom.is_empty:
        return MultiPolygon()
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    parts = [g for g in getattr(geom, "geoms", []) if g.geom_type in ("Polygon", "MultiPolygon")]
    return unary_union(parts) if parts else MultiPolygon()


def _lineal(geom: BaseGeometry) -> BaseGeometry:
    """Keep only the linear parts of a geometry."""
    if geom.is_empty:
        return MultiLineString()
    if geom.geom_type in ("LineString", "MultiLineString"):
        return geom
    parts = [g for g in getattr(geom, "geoms", []) if g.geom_type in ("LineString", "MultiLineString")]
    return unary_union(parts) if parts else MultiLineString()


def _points_of(geom: BaseGeometry) -> list[Point]:
    """Every Point contained in a (multi/collection) geometry; line parts are ignored."""
    if geom.is_empty:
        return []
    if geom.geom_type == "Point":
        return [geom]
    if geom.geom_type == "MultiPoint":
        return list(geom.geoms)
    if geom.geom_type == "GeometryCollection":
        return [p for g in geom.geoms for p in _points_of(g)]
    return []


def union_polygons(geoms: list[BaseGeometry], clip: BaseGeometry | None = None) -> BaseGeometry:
    """Union polygon parts, optionally clipped; always returns a polygonal geometry."""
    if not geoms:
        return MultiPolygon()
    merged = unary_union(geoms)
    if clip is not None:
        merged = merged.intersection(clip)
    return _polygonal(make_valid(merged))


def union_lines(geoms: list[BaseGeometry], clip: BaseGeometry | None = None) -> BaseGeometry:
    """Union line parts, optionally clipped; always returns a lineal geometry."""
    if not geoms:
        return MultiLineString()
    merged = unary_union(geoms)
    if clip is not None:
        merged = merged.intersection(clip)
    return _lineal(merged)


# ----------------------------------------------------------------------------- tag parsing
_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def parse_length(value: Any) -> float | None:
    """First number in a tag value ("6.5", "7 m", "5,5") as metres; None when absent."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    m = _NUMBER_RE.search(str(value))
    if not m:
        return None
    num = float(m.group(0).replace(",", "."))
    if "'" in str(value) or "ft" in str(value).lower():
        num *= 0.3048
    return num


def parse_year(value: Any) -> int | None:
    """Four-digit year from a tag or property ("1999", "2012-04-01", 1999)."""
    if value is None:
        return None
    m = re.search(r"(1[6-9]\d{2}|20\d{2})", str(value))
    return int(m.group(1)) if m else None


def parse_int(value: Any) -> int | None:
    """Leading integer in a tag value ("2", "2;3" -> 2)."""
    if value is None:
        return None
    m = re.match(r"\s*(\d+)", str(value))
    return int(m.group(1)) if m else None


def carriageway_width(tags: dict[str, Any]) -> float:
    """Estimated carriageway width: ``width`` > ``lanes*3.0`` > default by class; 0 = none."""
    hw = str(tags.get("highway") or "")
    if hw in NO_CARRIAGEWAY_CLASSES:
        return 0.0
    width = parse_length(tags.get("width"))
    if width is not None and 2.0 <= width <= 40.0:
        return width
    lanes = parse_int(tags.get("lanes"))
    if lanes is not None and 1 <= lanes <= 12:
        return lanes * 3.0
    return DEFAULT_WIDTH_BY_CLASS.get(hw.removesuffix("_link"), DEFAULT_WIDTH_M)


def _sidewalk_flag(value: Any) -> bool | None:
    if value is None:
        return None
    return str(value) not in ("no", "none")


def sidewalk_sides(tags: dict[str, Any]) -> tuple[bool, bool]:
    """(left, right) sidewalk presence from ``sidewalk``/``sidewalk:*`` tags or class default."""
    hw = str(tags.get("highway") or "")
    if hw == "pedestrian":
        return True, True
    both = _sidewalk_flag(tags.get("sidewalk:both"))
    left = _sidewalk_flag(tags.get("sidewalk:left"))
    right = _sidewalk_flag(tags.get("sidewalk:right"))
    generic = str(tags.get("sidewalk") or "")
    if generic:
        if generic == "left":
            left, right = True, False if right is None else right
        elif generic == "right":
            left, right = False if left is None else left, True
        else:
            flag = _sidewalk_flag(generic)
            left = flag if left is None else left
            right = flag if right is None else right
    if both is not None:
        left = both if left is None else left
        right = both if right is None else right
    if left is None and right is None:
        default = hw in SIDEWALK_DEFAULT_BOTH
        return default, default
    return bool(left), bool(right)


def cycle_lane_sides(tags: dict[str, Any]) -> tuple[bool, bool]:
    """(left, right) presence of a cycle lane/track mapped as a tag on the road itself."""
    def has(value: Any) -> bool:
        return str(value or "") in CYCLE_LANE_VALUES

    generic = str(tags.get("cycleway") or "")
    both = has(tags.get("cycleway:both"))
    left = has(tags.get("cycleway:left")) or both
    right = has(tags.get("cycleway:right")) or both
    if generic in ("lane", "track"):
        left = right = True
    elif generic in ("opposite_lane", "opposite_track"):
        left = True
    return left, right


def is_minor_way(tags: dict[str, Any]) -> bool:
    """Footways, paths, service roads etc. without a name are not real streets."""
    return str(tags.get("highway") or "") in MINOR_CLASSES and not tags.get("name")


def is_axis_candidate(tags: dict[str, Any]) -> bool:
    """Ways that may form a street axis: no separately mapped sidewalks, no plazas."""
    if str(tags.get("area") or "") == "yes":
        return False
    if str(tags.get("footway") or "") in ("sidewalk", "crossing"):
        return False
    return str(tags.get("highway") or "") not in MINOR_CLASSES


# ----------------------------------------------------------------------------- axis merging
def merge_axis(lines: list[LineString], warnings: list[str], anchor: Point | None = None) -> LineString:
    """Join way geometries into one LineString.

    When the ways do not form a single chain the longest part wins (preferring
    parts within 50 m of ``anchor`` when one is given) and a warning is added.
    The result is capped at 2500 m around its middle.
    """
    lines = [ln for ln in lines if ln is not None and not ln.is_empty and ln.length > 0]
    if not lines:
        raise StreetNotFound("no way geometry to build an axis from")
    merged = linemerge(MultiLineString(lines)) if len(lines) > 1 else lines[0]
    if merged.geom_type == "MultiLineString":
        parts = sorted(merged.geoms, key=lambda g: g.length, reverse=True)
        chosen = parts[0]
        if anchor is not None:
            near = [p for p in parts if p.distance(anchor) <= ANCHOR_TOLERANCE_M]
            if near:
                chosen = near[0]
        warnings.append(f"Street consists of {len(parts)} disconnected parts; "
                        f"using the longest ({chosen.length:.0f} m)")
        merged = chosen
    return cap_axis(LineString(merged.coords), warnings)


def cap_axis(axis: LineString, warnings: list[str], max_m: float = MAX_AXIS_M) -> LineString:
    """Cut an over-long axis to the ``max_m`` metres around its middle."""
    if axis.length <= max_m:
        return axis
    mid = axis.length / 2.0
    capped = substring(axis, mid - max_m / 2.0, mid + max_m / 2.0)
    warnings.append(f"Street is {axis.length:.0f} m long; planning the middle {max_m:.0f} m")
    return LineString(capped.coords)


# ----------------------------------------------------------------------------- trees
def impute_crown_d(genus: str | None, species_name: str | None, age_years: int | None) -> float:
    """Crown diameter from the engine's species table; 8 m when the engine is unavailable."""
    try:
        from ..engine.species import impute_crown
    except Exception:
        return DEFAULT_CROWN_D_M
    try:
        value = float(impute_crown(genus, species_name, age_years))
    except Exception:
        return DEFAULT_CROWN_D_M
    return value if math.isfinite(value) and value > 0 else DEFAULT_CROWN_D_M


def genus_from_species(species: str | None) -> str | None:
    """First word of a botanical name ("Platanus x hispanica" -> "Platanus")."""
    if not species:
        return None
    first = species.strip().split()[0] if species.strip() else ""
    return first.capitalize() if first.isalpha() else None


def tree_from_node(el: dict[str, Any], epsg: int) -> ExistingTree | None:
    """``natural=tree`` node -> ExistingTree with an imputed crown when none is tagged."""
    pt = node_point(el, epsg)
    if pt is None:
        return None
    tags = el.get("tags") or {}
    species = tags.get("species") or tags.get("taxon") or None
    genus = tags.get("genus") or genus_from_species(species)
    crown = parse_length(tags.get("diameter_crown"))
    if crown is not None and crown <= 0:
        crown = None
    planted = parse_year(tags.get("start_date") or tags.get("planted_date"))
    age = date.today().year - planted if planted else None
    imputed = crown is None
    return ExistingTree(
        id=f"osm-{el.get('id')}", pt=pt, species=species, genus=genus,
        crown_d_m=impute_crown_d(genus, species, age) if imputed else crown,
        crown_imputed=imputed, height_m=parse_length(tags.get("height")),
        planted_year=planted, source="osm")


def trees_from_elements(elements: list[dict[str, Any]], epsg: int, clip: BaseGeometry) -> list[ExistingTree]:
    """All ``natural=tree`` nodes inside ``clip`` as ExistingTree records."""
    trees: list[ExistingTree] = []
    for el in elements:
        if el.get("type") != "node" or (el.get("tags") or {}).get("natural") != "tree":
            continue
        tree = tree_from_node(el, epsg)
        if tree is not None and clip.contains(tree.pt):
            trees.append(tree)
    return trees


# ----------------------------------------------------------------------------- layers from OSM
@dataclass
class OsmLayers:
    """Street layers estimated from Overpass elements (metric CRS)."""
    carriageway: BaseGeometry
    sidewalks: BaseGeometry
    plantable: BaseGeometry
    buildings: BaseGeometry
    cycleways: BaseGeometry
    junctions: list[Point]
    trees: list[ExistingTree]
    axis_way_ids: list[int]
    half_width_m: float
    warnings: list[str] = field(default_factory=list)


def split_elements(data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Overpass payload -> (highway ways, building ways, tree nodes)."""
    highways: list[dict[str, Any]] = []
    buildings: list[dict[str, Any]] = []
    trees: list[dict[str, Any]] = []
    for el in data.get("elements") or []:
        tags = el.get("tags") or {}
        if el.get("type") == "way":
            if "highway" in tags:
                highways.append(el)
            if "building" in tags:
                buildings.append(el)
        elif el.get("type") == "node" and tags.get("natural") == "tree":
            trees.append(el)
    return highways, buildings, trees


def select_axis_ways(axis: LineString, highways: list[tuple[dict[str, Any], LineString]],
                     way_ids: list[int]) -> list[tuple[dict[str, Any], LineString]]:
    """The ways that make up the axis: by id when known, else the ways running along it."""
    if way_ids:
        wanted = set(way_ids)
        return [(el, ln) for el, ln in highways if el.get("id") in wanted]
    band = axis.buffer(6.0)
    picked: list[tuple[dict[str, Any], LineString]] = []
    for el, ln in highways:
        if not is_axis_candidate(el.get("tags") or {}):
            continue
        inside = ln.intersection(band).length
        if inside >= 0.6 * ln.length or inside >= 30.0:
            picked.append((el, ln))
    return picked


def _sidewalk_band(line: LineString, tags: dict[str, Any], width: float) -> BaseGeometry:
    """Estimated sidewalk band beside one way, respecting left/right tags."""
    left, right = sidewalk_sides(tags)
    if not (left or right):
        return MultiPolygon()
    if width <= 0:
        return line.buffer(PEDESTRIAN_HALF_WIDTH_M, cap_style="flat")
    outer = width / 2.0 + SIDEWALK_WIDTH_M
    band = line.buffer(outer, cap_style="flat")
    if left and right:
        return band
    side_sign = 1.0 if left else -1.0
    side = line.buffer(side_sign * (outer + 1.0), single_sided=True)
    return band.intersection(side)


def _cycle_offsets(line: LineString, tags: dict[str, Any], width: float) -> list[BaseGeometry]:
    """Cycle lanes tagged on the road itself, as lines at the carriageway edge."""
    left, right = cycle_lane_sides(tags)
    offset = max(width / 2.0, 0.5)
    lines: list[BaseGeometry] = []
    if left:
        lines.append(line.offset_curve(offset))
    if right:
        lines.append(line.offset_curve(-offset))
    return [ln for ln in lines if not ln.is_empty]


def find_junctions(axis: LineString, highways: list[tuple[dict[str, Any], LineString]],
                   exclude_ids: set[int], clip: BaseGeometry) -> list[Point]:
    """Points where other streets touch or cross the axis, merged within 3 m, by station."""
    found: list[Point] = []
    for el, ln in highways:
        tags = el.get("tags") or {}
        crossing = tags.get("footway") == "crossing" or tags.get("cycleway") == "crossing"
        if el.get("id") in exclude_ids or (is_minor_way(tags) and not crossing):
            continue
        axis_layers = {str((other.get("tags") or {}).get("layer", "0"))
                       for other, line in highways if other.get("id") in exclude_ids and line.distance(ln) <= 0.5}
        if axis_layers and str(tags.get("layer", "0")) not in axis_layers:
            continue
        inter = ln.intersection(axis)
        pts = _points_of(inter)
        if not pts and inter.is_empty and ln.distance(axis) <= 0.5:
            pts = [nearest_points(axis, ln)[0]]
        found.extend(p for p in pts if clip.contains(p))
    found.sort(key=axis.project)
    merged: list[Point] = []
    for p in found:
        if not merged or all(p.distance(q) > JUNCTION_MERGE_M for q in merged):
            merged.append(p)
    return merged


def build_osm_layers(axis: LineString, data: dict[str, Any], epsg: int, *,
                     axis_way_ids: list[int] | None = None,
                     corridor: BaseGeometry | None = None) -> OsmLayers:
    """Estimate every street layer from an Overpass area payload around ``axis``."""
    corridor = corridor if corridor is not None else axis.buffer(CORRIDOR_HALF_WIDTH_M, cap_style="flat")
    clip = corridor.buffer(CLIP_MARGIN_M)
    warnings: list[str] = []
    highway_els, building_els, tree_els = split_elements(data)
    highways = [(el, ln) for el in highway_els if (ln := way_line(el, epsg)) is not None]
    highways = [(el, ln) for el, ln in highways if ln.distance(axis) <= CORRIDOR_HALF_WIDTH_M + AREA_BBOX_PAD_M]

    axis_ways = select_axis_ways(axis, highways, list(axis_way_ids or []))
    axis_ids = [el["id"] for el, _ in axis_ways]
    if axis_ways:
        widths = [carriageway_width(el.get("tags") or {}) for el, _ in axis_ways]
        half_width = max(widths) / 2.0
    else:
        half_width = DEFAULT_WIDTH_BY_CLASS["residential"] / 2.0
        warnings.append("No OpenStreetMap street found along the axis; using default widths "
                        "(6.5 m carriageway, 2.5 m sidewalks)")

    carriageway_parts: list[BaseGeometry] = []
    for el, ln in highways:
        w = carriageway_width(el.get("tags") or {})
        if w > 0:
            carriageway_parts.append(ln.buffer(w / 2.0))
    if not axis_ways:
        carriageway_parts.append(axis.buffer(half_width, cap_style="flat"))
    carriageway = union_polygons(carriageway_parts, clip)

    buildings = union_polygons(
        [poly for el in building_els if (poly := way_polygon(el, epsg)) is not None], clip)

    sidewalk_parts: list[BaseGeometry] = []
    cycle_parts: list[BaseGeometry] = []
    if axis_ways:
        for el, ln in axis_ways:
            tags = el.get("tags") or {}
            w = carriageway_width(tags)
            sidewalk_parts.append(_sidewalk_band(ln, tags, w))
            cycle_parts.extend(_cycle_offsets(ln, tags, w))
    else:
        sidewalk_parts.append(axis.buffer(half_width + SIDEWALK_WIDTH_M, cap_style="flat"))
    sidewalks = union_polygons(sidewalk_parts, clip).difference(carriageway).difference(buildings)
    sidewalks = _polygonal(make_valid(sidewalks))

    for el, ln in highways:
        if (el.get("tags") or {}).get("highway") == "cycleway":
            cycle_parts.append(ln)
    cycleways = union_lines(cycle_parts, clip)

    plantable = _polygonal(make_valid(sidewalks.difference(buildings).difference(carriageway)))
    junctions = find_junctions(axis, highways, set(axis_ids), clip)
    for el in data.get("elements") or []:
        if el.get("type") == "node" and (el.get("tags") or {}).get("highway") == "crossing":
            if el.get("lon") is None or el.get("lat") is None:
                continue
            pt = crs.to_metric(Point(el["lon"], el["lat"]), epsg)
            if clip.covers(pt) and axis.distance(pt) <= 3.0 and all(pt.distance(p) > JUNCTION_MERGE_M for p in junctions):
                junctions.append(pt)
    trees = trees_from_elements(tree_els, epsg, clip)
    return OsmLayers(carriageway=carriageway, sidewalks=sidewalks, plantable=plantable,
                     buildings=buildings, cycleways=cycleways, junctions=junctions, trees=trees,
                     axis_way_ids=axis_ids, half_width_m=half_width, warnings=warnings)


# ----------------------------------------------------------------------------- context assembly
def street_id_for(city_id: str, name: str, axis: LineString) -> str:
    """Deterministic id for a street: city + short hash of name and axis geometry."""
    digest = hashlib.sha1(name.encode("utf-8") + axis.wkb).hexdigest()[:10]
    return f"{city_id}-{digest}"


def assemble_context(*, city: CityInfo, epsg: int, name: str, axis: LineString,
                     carriageway: BaseGeometry, sidewalks: BaseGeometry, plantable: BaseGeometry,
                     buildings: BaseGeometry, cycleways: BaseGeometry, junctions: list[Point],
                     trees: list[ExistingTree], basis: dict[str, Basis], sources: dict[str, str],
                     warnings: list[str], way_ids: list[int]) -> StreetContext:
    """Build a StreetContext with the conventional corridor and every layer key filled."""
    corridor = axis.buffer(CORRIDOR_HALF_WIDTH_M, cap_style="flat")
    full_basis: dict[str, Basis] = {"axis": "measured", "corridor": "measured"}
    full_basis.update(basis)
    full_sources = {"axis": OSM_ATTRIBUTION, "corridor": OSM_ATTRIBUTION}
    full_sources.update(sources)
    return StreetContext(
        street_id=street_id_for(city.id, name, axis), city=city, name=name, epsg=epsg, axis=axis,
        carriageway=carriageway, sidewalks=sidewalks, plantable=plantable, buildings=buildings,
        cycleways=cycleways, junctions=junctions, existing_trees=trees, corridor=corridor,
        basis=full_basis, sources=full_sources, warnings=list(warnings), osm_way_ids=list(way_ids))


def context_from_osm_layers(city: CityInfo, epsg: int, name: str, axis: LineString,
                            layers: OsmLayers, warnings: list[str]) -> StreetContext:
    """StreetContext where every layer comes from OSM estimation."""
    basis: dict[str, Basis] = {
        "carriageway": "estimated", "sidewalks": "estimated", "plantable": "estimated",
        "buildings": "measured", "cycleways": "estimated", "junctions": "measured",
        "existing_trees": "estimated",
    }
    sources = {key: OSM_ATTRIBUTION for key in basis}
    return assemble_context(
        city=city, epsg=epsg, name=name, axis=axis, carriageway=layers.carriageway,
        sidewalks=layers.sidewalks, plantable=layers.plantable, buildings=layers.buildings,
        cycleways=layers.cycleways, junctions=layers.junctions, trees=layers.trees, basis=basis,
        sources=sources, warnings=[*warnings, *layers.warnings], way_ids=layers.axis_way_ids)


# ----------------------------------------------------------------------------- resolution
@dataclass
class ResolvedAxis:
    """Outcome of resolving a street name: merged axis plus provenance."""
    axis: LineString
    name: str
    way_ids: list[int]
    warnings: list[str]


def drawn_axis(coords_wgs: list[tuple[float, float]], epsg: int, warnings: list[str]) -> LineString:
    """User-drawn lon/lat pairs -> metric axis (capped at 2500 m); StreetNotFound when degenerate."""
    pts = [(float(lon), float(lat)) for lon, lat in coords_wgs]
    if len(pts) < 2:
        raise StreetNotFound("A drawn street needs at least two points")
    axis = crs.to_metric(LineString(pts), epsg)
    if axis.length < 1.0:
        raise StreetNotFound("The drawn street is shorter than one metre")
    return cap_axis(LineString(axis.coords), warnings)


async def resolve_street_axis(street: str, *, city_bbox: BBox, epsg: int,
                              countrycodes: str | None = None, city_label: str = "") -> ResolvedAxis:
    """Street name -> one merged axis via Nominatim (name pick) and Overpass (all ways)."""
    street = re.sub(r"\s+", " ", street or "").strip()
    if not street:
        raise StreetNotFound("Empty street name")
    warnings: list[str] = []
    q = f"{street}, {city_label}" if city_label else street
    try:
        results = await nominatim_search(q, countrycodes=countrycodes)
    except UpstreamUnavailable as exc:
        results = []
        warnings.append(f"Nominatim unavailable ({exc}); matched the name directly in OpenStreetMap")
    hit = pick_nominatim_street(results, street)
    anchor: Point | None = None
    name = street
    if hit is not None:
        name = str(hit.get("name") or street)
        anchor = crs.to_metric(Point(float(hit["lon"]), float(hit["lat"])), epsg)
    else:
        named = next((r for r in results
                      if (r.get("category") or r.get("class")) == "highway" and r.get("name")), None)
        if named is not None:
            name = str(named["name"])
    candidates = [name] if _normalise_name(name) == _normalise_name(street) else [name, street]
    ways: list[dict[str, Any]] = []
    for candidate in candidates:
        payload = await overpass(overpass_name_query(candidate, city_bbox), urls=overpass_urls_for(epsg))
        ways = [el for el in payload.get("elements") or [] if el.get("type") == "way"]
        if ways:
            name = candidate
            break
    if not ways:
        raise StreetNotFound(f"No street named '{street}' found" + (f" in {city_label}" if city_label else ""))
    primary = [el for el in ways if is_axis_candidate(el.get("tags") or {})] or ways
    pairs = [(el, ln) for el in primary if (ln := way_line(el, epsg)) is not None]
    axis = merge_axis([ln for _, ln in pairs], warnings, anchor)
    used = [el["id"] for el, ln in pairs if ln.distance(axis) <= 1.0]
    return ResolvedAxis(axis=axis, name=name, way_ids=used, warnings=warnings)


async def resolve_street_point(point: tuple[float, float], *, epsg: int,
                               city_bbox: BBox | None = None) -> ResolvedAxis:
    """Snap a click to an actual road within 30 m; retain the clicked section."""
    lon, lat = point
    if city_bbox and not (city_bbox[0] <= lon <= city_bbox[2] and city_bbox[1] <= lat <= city_bbox[3]):
        raise StreetNotFound("Click inside the selected city, or choose Anywhere (OSM).")
    anchor = crs.to_metric(Point(lon, lat), epsg)
    bbox = crs.to_wgs(anchor.buffer(40), epsg).bounds
    query = f'[out:json][timeout:25];way["highway"]({_ql_bbox(bbox)});out tags geom;'
    data = await overpass(query, urls=overpass_urls_for(epsg))
    candidates = []
    for el in data.get("elements") or []:
        tags = el.get("tags") or {}
        if el.get("type") != "way" or not tags.get("highway") or not is_axis_candidate(tags):
            continue
        line = way_line(el, epsg)
        if line is not None and line.length >= 1 and line.distance(anchor) <= 30:
            candidates.append((el, line))
    if not candidates:
        raise StreetNotFound("No mapped street within 30 m. Zoom in and click the street centreline.")
    seed, axis = min(candidates, key=lambda pair: (pair[1].distance(anchor), pair[0]["id"]))
    name = (seed.get("tags") or {}).get("name") or "Unnamed street"
    warnings: list[str] = []
    pairs = [(seed, axis)]
    if name != "Unnamed street":
        # Query locally, avoiding distant streets that happen to share a name.
        try:
            nearby = await overpass(overpass_name_query(name, crs.to_wgs(anchor.buffer(MAX_AXIS_M), epsg).bounds),
                                    urls=overpass_urls_for(epsg))
            pairs += [(el, line) for el in nearby.get("elements") or []
                      if el.get("type") == "way" and el.get("id") != seed["id"]
                      and is_axis_candidate(el.get("tags") or {})
                      and (line := way_line(el, epsg)) is not None]
            merged = linemerge([line for _, line in pairs])
            parts = list(merged.geoms) if isinstance(merged, MultiLineString) else [merged]
            snapped = axis.interpolate(axis.project(anchor))
            containing = [part for part in parts if part.distance(snapped) < 0.01]
            if containing:
                axis = max(containing, key=lambda part: part.intersection(pairs[0][1]).length)
        except UpstreamUnavailable:
            warnings.append("Only the clicked OpenStreetMap section is available; neighbouring sections could not be loaded.")
    if axis.length > MAX_AXIS_M:
        start = min(max(0, axis.project(anchor) - MAX_AXIS_M / 2), axis.length - MAX_AXIS_M)
        axis = substring(axis, start, start + MAX_AXIS_M)
        warnings.append("Analysis limited to 2.5 km around the clicked position.")
    # Substring interpolation may differ by tiny floating-point offsets.
    ids = [el["id"] for el, line in pairs if line.intersection(axis.buffer(0.01, cap_style="flat")).length > 0.1]
    return ResolvedAxis(axis=axis, name=name, way_ids=ids, warnings=warnings)


async def fetch_area(axis: LineString, epsg: int) -> dict[str, Any]:
    """Overpass payload with every highway, building and tree around the axis corridor."""
    return await overpass(overpass_area_query(corridor_bboxes(axis, epsg)), urls=overpass_urls_for(epsg))


async def osm_street_context(city: CityInfo, epsg: int, axis: LineString, name: str,
                             way_ids: list[int], warnings: list[str]) -> StreetContext:
    """Full StreetContext from OSM alone (generic adapter, Berlin geometry)."""
    data = await fetch_area(axis, epsg)
    layers = build_osm_layers(axis, data, epsg, axis_way_ids=way_ids)
    return context_from_osm_layers(city, epsg, name, axis, layers, warnings)


# ----------------------------------------------------------------------------- generic adapter
OSM_INFO = CityInfo(
    id="osm", name="Anywhere (OpenStreetMap)", country="", epsg=3857,
    center=[8.5417, 47.3769], zoom=12, utc_offset_hours=2.0,
    basemaps=[
        Basemap(id="light", label="CARTO light", tiles=["https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png"],
                attribution="© OpenStreetMap contributors © CARTO", max_zoom=19, default=True),
        Basemap(id="osm", label="OpenStreetMap", tiles=["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
                attribution="© OpenStreetMap contributors", max_zoom=19),
    ],
    demo_streets=["Rue de Rivoli, Paris", "Karl-Marx-Allee, Berlin", "Marktgasse, Winterthur"],
    tree_source=OSM_ATTRIBUTION, geometry_source=OSM_ATTRIBUTION)


def split_street_city(query: str) -> tuple[str, str]:
    """"street, city" -> (street, city); raises StreetNotFound without a comma."""
    if "," not in (query or ""):
        raise StreetNotFound("Use 'street, city' (for example 'Marktgasse, Winterthur')")
    street, _, city = query.rpartition(",")
    street, city = street.strip(), city.strip()
    if not street or not city:
        raise StreetNotFound("Use 'street, city' (for example 'Marktgasse, Winterthur')")
    return street, city


class OsmAdapter(CityAdapter):
    """Any street OpenStreetMap covers, in the local UTM zone."""

    info = OSM_INFO

    async def street_from_point(self, point: tuple[float, float]) -> StreetContext:
        epsg = crs.utm_epsg(*point)
        resolved = await resolve_street_point(point, epsg=epsg)
        return await self._build(resolved.axis, resolved.name, resolved.way_ids, resolved.warnings, epsg)

    async def _city_bbox(self, city: str, fallback: dict[str, Any] | None) -> BBox:
        """Bounding box of the city from Nominatim; else 3 km around the street hit."""
        try:
            results = await nominatim_search(city, limit=1)
        except UpstreamUnavailable:
            results = []
        if results and results[0].get("boundingbox"):
            return nominatim_bbox(results[0])
        if fallback is not None and fallback.get("boundingbox"):
            return pad_bbox(nominatim_bbox(fallback), 0.03)
        raise StreetNotFound(f"Unknown city '{city}'")

    async def find_street(self, query: str) -> StreetContext:
        """Resolve "street, city" anywhere and build its context from OSM."""
        street, city = split_street_city(query)
        results = await nominatim_search(f"{street}, {city}")
        hit = pick_nominatim_street(results, street)
        anchor_hit = hit or (results[0] if results else None)
        bbox = await self._city_bbox(city, anchor_hit)
        centre_lon = (bbox[0] + bbox[2]) / 2.0
        centre_lat = (bbox[1] + bbox[3]) / 2.0
        if hit is not None:
            centre_lon, centre_lat = float(hit["lon"]), float(hit["lat"])
        epsg = crs.utm_epsg(centre_lon, centre_lat)
        resolved = await resolve_street_axis(street, city_bbox=bbox, epsg=epsg, city_label=city)
        return await self._build(resolved.axis, f"{resolved.name}, {city}", resolved.way_ids,
                                 resolved.warnings, epsg)

    async def street_from_line(self, coords_wgs: list[tuple[float, float]], name: str | None = None) -> StreetContext:
        """Context around a user-drawn axis; CRS = UTM zone of its first point."""
        if not coords_wgs:
            raise StreetNotFound("A drawn street needs at least two points")
        lon, lat = float(coords_wgs[0][0]), float(coords_wgs[0][1])
        epsg = crs.utm_epsg(lon, lat)
        warnings: list[str] = []
        axis = drawn_axis(coords_wgs, epsg, warnings)
        return await self._build(axis, name or "Drawn street", [], warnings, epsg)

    async def _build(self, axis: LineString, name: str, way_ids: list[int],
                     warnings: list[str], epsg: int) -> StreetContext:
        mid = crs.to_wgs(axis.interpolate(0.5, normalized=True), epsg)
        city = self.info.model_copy(update={"epsg": epsg, "center": [round(mid.x, 6), round(mid.y, 6)]})
        return await osm_street_context(city, epsg, axis, name, way_ids, warnings)
