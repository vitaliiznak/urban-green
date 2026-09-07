"""City adapter registry: ``ADAPTERS`` maps a city id to its adapter instance and
``get_adapter`` looks one up (KeyError when unknown). The public product is
Switzerland (Zürich). The offline "demo" adapter stays for tests."""
from __future__ import annotations

from ..schemas import Basemap, CityInfo
from .base import CityAdapter, ExistingTree, StreetContext, StreetNotFound, UpstreamUnavailable
from .osm import build_osm_layers, context_from_osm_layers, drawn_axis
from .serialize import serialize_street
from .zurich import ZurichAdapter

DEMO_EPSG = 2056
DEMO_INFO = CityInfo(
    id="demo", name="Demo street (offline)", country="CH", epsg=DEMO_EPSG, center=[8.53, 47.38],
    zoom=16, utc_offset_hours=2.0,
    basemaps=[
        Basemap(id="grey", label="Grey map (swisstopo)",
                tiles=["https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-grau/default/current/3857/{z}/{x}/{y}.jpeg"],
                attribution="© swisstopo", max_zoom=18, default=True),
    ],
    demo_streets=["Demo street"],
    tree_source="Synthetic demo data (no network)", geometry_source="Synthetic demo data (no network)")


class DemoAdapter(CityAdapter):
    """Deterministic synthetic street; never touches the network."""

    info = DEMO_INFO

    async def find_street(self, query: str) -> StreetContext:
        """The engine's synthetic 400 m street, named after the query."""
        from ..engine.synthetic import synthetic_street

        ctx = synthetic_street(name=(query or "").strip() or "Demo street")
        ctx.city = self.info
        return ctx

    async def street_from_line(self, coords_wgs: list[tuple[float, float]], name: str | None = None) -> StreetContext:
        """Estimated default geometry (6.5 m carriageway, 2.5 m sidewalks) around a drawn axis."""
        warnings: list[str] = []
        axis = drawn_axis(coords_wgs, DEMO_EPSG, warnings)
        layers = build_osm_layers(axis, {"elements": []}, DEMO_EPSG)
        ctx = context_from_osm_layers(self.info, DEMO_EPSG, name or "Drawn street", axis, layers, warnings)
        ctx.warnings.append("Offline demo city: geometry estimated from the drawn line only")
        return ctx


ADAPTERS: dict[str, CityAdapter] = {
    "zurich": ZurichAdapter(),
    "demo": DemoAdapter(),
}
PUBLIC_CITIES = ("zurich",)


def get_adapter(city_id: str) -> CityAdapter:
    """Adapter for a city id; raises KeyError for unknown ids."""
    try:
        return ADAPTERS[city_id]
    except KeyError:
        raise KeyError(f"unknown city '{city_id}'; known: {', '.join(ADAPTERS)}") from None


__all__ = [
    "ADAPTERS", "PUBLIC_CITIES", "CityAdapter", "DemoAdapter", "ExistingTree",
    "StreetContext", "StreetNotFound", "UpstreamUnavailable", "ZurichAdapter", "get_adapter",
    "serialize_street",
]
