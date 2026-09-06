"""Click resolution uses nearby road geometry, never an arbitrary drawn line."""
import pytest
from pydantic import ValidationError
from shapely.geometry import LineString, Point

from server import crs
from server.adapters import osm
from server.adapters.base import StreetNotFound
from server.schemas import StreetRequest

EPSG = 2056
ORIGIN = crs.to_metric(Point(8.53, 47.38), EPSG)


def way(id, coords, **tags):
    line = crs.to_wgs(LineString([(ORIGIN.x + x, ORIGIN.y + y) for x, y in coords]), EPSG)
    return {"id": id, "type": "way", "tags": {"highway": "residential", "name": "Test street", **tags},
            "geometry": [{"lon": x, "lat": y} for x, y in line.coords]}


@pytest.mark.asyncio
async def test_click_chooses_nearest_road_and_connected_section(monkeypatch):
    seed = way(1, [(-50, 5), (50, 5)])
    continuation = way(2, [(50, 5), (180, 5)])
    disconnected = way(3, [(-100, 100), (1000, 100)])
    sidewalk = way(4, [(-50, 0), (50, 0)], highway="footway", footway="sidewalk")
    queries = []
    async def overpass(query, **kwargs):
        queries.append(query)
        return {"elements": [seed, sidewalk] if len(queries) == 1 else [seed, continuation, disconnected]}
    monkeypatch.setattr(osm, "overpass", overpass)
    result = await osm.resolve_street_point((8.53, 47.38), epsg=EPSG)
    assert result.name == "Test street"
    assert set(result.way_ids) == {1, 2}
    assert result.axis.length == pytest.approx(230, abs=.01)
    assert result.axis.distance(ORIGIN) == pytest.approx(5, abs=.01)


@pytest.mark.asyncio
async def test_empty_click_does_not_select_distant_road(monkeypatch):
    async def overpass(*args, **kwargs):
        return {"elements": [way(1, [(-100, 31), (100, 31)])]}
    monkeypatch.setattr(osm, "overpass", overpass)
    with pytest.raises(StreetNotFound, match="within 30 m"):
        await osm.resolve_street_point((8.53, 47.38), epsg=EPSG)


@pytest.mark.asyncio
async def test_long_street_keeps_clicked_end_when_capped(monkeypatch):
    road = way(1, [(-20, 0), (6000, 0)])
    async def overpass(*args, **kwargs):
        return {"elements": [road]}
    monkeypatch.setattr(osm, "overpass", overpass)
    result = await osm.resolve_street_point((8.53, 47.38), epsg=EPSG)
    assert result.axis.length == pytest.approx(2500)
    assert result.axis.distance(ORIGIN) < .01
    assert result.way_ids == [1]


@pytest.mark.asyncio
async def test_click_outside_city_rejected_before_network():
    with pytest.raises(StreetNotFound, match="inside the selected city"):
        await osm.resolve_street_point((13.4, 52.5), epsg=EPSG, city_bbox=(8.44, 47.32, 8.63, 47.43))


@pytest.mark.parametrize("point", [[8.53], [8.53, 47.38, 0], [181, 47], [8, 90], [float('nan'), 47]])
def test_click_coordinate_validation(point):
    with pytest.raises(ValidationError):
        StreetRequest(city="zurich", point=point)


def test_click_cannot_be_mixed_with_another_selection():
    with pytest.raises(ValidationError):
        StreetRequest(city="zurich", point=[8.53, 47.38], query="Langstrasse")
