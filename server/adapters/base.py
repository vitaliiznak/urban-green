"""City adapter contract. One small class per data source turns a street name
(or a user-drawn axis) into a StreetContext: everything the engine needs, as
Shapely geometry in the city's metric CRS."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional

from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry

from ..schemas import Basis, CityInfo


@dataclass
class ExistingTree:
    id: str
    pt: Point                       # metric CRS
    species: Optional[str]          # botanical name if known
    genus: Optional[str]
    crown_d_m: Optional[float]      # measured crown diameter, None if unknown
    crown_imputed: bool             # True when crown_d_m was filled from the species table / default
    height_m: Optional[float]
    planted_year: Optional[int]
    source: str


@dataclass
class StreetContext:
    street_id: str
    city: CityInfo
    name: str
    epsg: int
    axis: LineString                # street centreline, metric CRS, single merged part
    carriageway: BaseGeometry       # (Multi)Polygon union, may be empty
    sidewalks: BaseGeometry
    plantable: BaseGeometry         # where a trunk may stand: sidewalks + verges/green, minus buildings and parking
    buildings: BaseGeometry
    cycleways: BaseGeometry         # (Multi)LineString, may be empty
    parking: BaseGeometry           # on-street bays, parking_space polygons, paved lots; may be empty
    junctions: list[Point]
    existing_trees: list[ExistingTree]
    corridor: Polygon               # analysis area: axis.buffer(15, cap_style=flat) by convention
    basis: dict[str, Basis]         # per layer key: measured | estimated | unknown
    sources: dict[str, str]         # per layer key: attribution text
    warnings: list[str] = field(default_factory=list)
    osm_way_ids: list[int] = field(default_factory=list)

    @property
    def length_m(self) -> float:
        return float(self.axis.length)


class CityAdapter(ABC):
    """Implementations: server/adapters/zurich.py."""
    info: CityInfo

    async def street_from_point(self, point: tuple[float, float]) -> StreetContext:
        raise StreetNotFound("Click a street in Zürich.")

    @abstractmethod
    async def find_street(self, query: str) -> StreetContext:
        """Resolve a street by name inside this city and build its context."""

    @abstractmethod
    async def street_from_line(self, coords_wgs: list[tuple[float, float]], name: str | None = None) -> StreetContext:
        """Build the context around a user-drawn axis (lon/lat pairs)."""


class StreetNotFound(Exception):
    pass


class UpstreamUnavailable(Exception):
    pass
