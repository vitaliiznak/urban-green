"""Projection helpers. The engine works in a local metric CRS per city
(EPSG:2056 for Zürich, EPSG:25833 for Berlin, UTM elsewhere); the API boundary
converts to/from WGS84 (EPSG:4326, lon/lat) so the browser never sees metres."""
from __future__ import annotations

import math
from functools import lru_cache

from pyproj import Transformer
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform


@lru_cache(maxsize=32)
def _tf(src: int, dst: int) -> Transformer:
    return Transformer.from_crs(f"EPSG:{src}", f"EPSG:{dst}", always_xy=True)


def to_metric(geom: BaseGeometry, epsg: int) -> BaseGeometry:
    """WGS84 lon/lat geometry -> metric CRS `epsg`."""
    return transform(_tf(4326, epsg).transform, geom)


def to_wgs(geom: BaseGeometry, epsg: int) -> BaseGeometry:
    """Metric CRS `epsg` geometry -> WGS84 lon/lat."""
    return transform(_tf(epsg, 4326).transform, geom)


def utm_epsg(lon: float, lat: float) -> int:
    """UTM zone EPSG code for a lon/lat (used by the generic OSM adapter)."""
    zone = int(math.floor((lon + 180) / 6)) + 1
    return (32600 if lat >= 0 else 32700) + zone


def feature(geom: BaseGeometry, epsg: int, props: dict | None = None, precision: int = 7) -> dict:
    """Metric geometry -> GeoJSON Feature in WGS84 with rounded coordinates."""
    g = to_wgs(geom, epsg)
    return {"type": "Feature", "geometry": _round(mapping(g), precision), "properties": props or {}}


def _round(obj, p):
    if isinstance(obj, dict):
        return {k: _round(v, p) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round(v, p) for v in obj]
    if isinstance(obj, float):
        return round(obj, p)
    return obj
