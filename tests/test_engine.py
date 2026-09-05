"""Engine tests on the synthetic demo street (no network, no adapters)."""
from __future__ import annotations

import math
import time

import pytest
from shapely.geometry import Point

from server.engine.canopy import canopy_metrics
from server.engine.rules import apply_overrides, evaluate_site, load_rule_packs
from server.engine.shade import shade_polygons, shadow_ellipse, sun_position
from server.engine.sites import plan_sites
from server.engine.species import crown_d_at, height_at, impute_crown, load_species, species_or_default
from server.engine.synthetic import synthetic_street
from server.schemas import YEARS, PlanParams, RuleOverride

PACK_ID = "berlin_strassenbaeume_2024"


@pytest.fixture(scope="module")
def ctx():
    return synthetic_street()


@pytest.fixture(scope="module")
def pack():
    return load_rule_packs()[PACK_ID]


@pytest.fixture(scope="module")
def tilia():
    return species_or_default("tilia_cordata")


@pytest.fixture(scope="module")
def grid(ctx, pack, tilia):
    return plan_sites(ctx, PlanParams(spacing_m=8, side="both", mode="grid"), pack, tilia)


def _junction_distance(ctx, station: float) -> float:
    return min(abs(station - j.x + ctx.axis.coords[0][0]) for j in ctx.junctions)


# ----------------------------------------------------------------------------- synthetic street
def test_synthetic_city_info(ctx):
    city = ctx.city
    assert city.id == "demo" and city.name == "Demo street (offline)"
    assert city.epsg == 2056 and ctx.epsg == 2056
    assert city.center == [8.53, 47.38] and city.zoom == 16.5 and city.utc_offset_hours == 2.0
    assert city.demo_streets == ["Demo street"]
    assert city.tree_source == "synthetic" and city.geometry_source == "synthetic"
    assert len(city.basemaps) == 1 and city.basemaps[0].default
    assert "pixelkarte-grau" in city.basemaps[0].tiles[0]


def test_synthetic_geometry(ctx):
    assert ctx.length_m == pytest.approx(400.0)
    assert ctx.axis.coords[0] == (2682000.0, 1248000.0)
    assert len(ctx.existing_trees) == 3 and all(t.crown_d_m == 9.0 for t in ctx.existing_trees)
    assert len(ctx.junctions) == 3
    assert ctx.corridor.area == pytest.approx(400 * 30)
    assert not ctx.cycleways.is_empty and ctx.cycleways.length == pytest.approx(200.0)
    assert set(ctx.basis) == {"axis", "carriageway", "sidewalks", "plantable", "buildings", "cycleways",
                              "junctions", "existing_trees", "corridor"}


def test_synthetic_is_deterministic():
    a, b = synthetic_street(), synthetic_street()
    assert a.carriageway.equals(b.carriageway) and a.buildings.equals(b.buildings)
    assert [t.pt.coords[0] for t in a.existing_trees] == [t.pt.coords[0] for t in b.existing_trees]


# ----------------------------------------------------------------------------- species
def test_species_table():
    table = load_species()
    assert "tilia_cordata" in table and len(table) >= 10
    with pytest.raises(KeyError):
        species_or_default("not_a_tree")


def test_growth_curve_30y_near_mature(tilia):
    assert 0.85 <= crown_d_at(tilia, 30) / tilia.mature_crown_d_m <= 0.9
    assert 0.85 <= height_at(tilia, 30) / tilia.mature_height_m <= 0.9
    assert crown_d_at(tilia, 0) == pytest.approx(tilia.sapling_crown_d_m)
    values = [crown_d_at(tilia, y) for y in YEARS]
    assert values == sorted(values)


def test_impute_crown():
    assert impute_crown("Tilia", None, None) == 10.0
    assert impute_crown(None, "Platanus × hispanica", None) == 20.0
    assert impute_crown("Unknownus", None, None) == 8.0
    young = impute_crown("Tilia", None, 5)
    assert 1.5 < young < 10.0
    assert impute_crown("Tilia", None, 200) == pytest.approx(10.0, abs=0.1)


# ----------------------------------------------------------------------------- grid mode
def test_grid_count_400m_8m_both(grid):
    sites, summary = grid
    assert summary.total == 100
    assert sum(1 for s in sites if s.side == "left") == 50
    assert sum(1 for s in sites if s.side == "right") == 50
    assert {s.site_id for s in sites} == {f"{side}{4 + 8 * k:04d}" for side in "LR" for k in range(50)}
    assert summary.planted == summary.valid + summary.conditional
    assert summary.invalid == 0


def test_south_side_valid_except_near_junctions(ctx, grid):
    sites, _ = grid
    for s in sites:
        if s.side != "right":
            continue
        near_junction = _junction_distance(ctx, s.station_m) < 10.0
        if near_junction:
            assert s.verdict == "conditional" and s.props.failed_rules == ["junction"], s.site_id
        else:
            assert s.verdict == "valid", (s.site_id, s.props.failed_rules)


def test_north_side_conditional_at_pushed_building(grid):
    sites, _ = grid
    for s in sites:
        if s.side != "left":
            continue
        if 260 <= s.station_m <= 300:
            assert s.verdict == "conditional" and "building_crown" in s.props.failed_rules, s.site_id
        else:
            assert "building_crown" not in s.props.failed_rules, s.site_id


def test_north_side_existing_trees_conditional(grid):
    sites, _ = grid
    by_id = {s.site_id: s for s in sites}
    for station in (100, 108, 116):
        assert "existing_tree" in by_id[f"L{station:04d}"].props.failed_rules
    assert "existing_tree" not in by_id["L0092"].props.failed_rules


def test_side_street_crossing_is_invalid(ctx, pack, tilia):
    sites, summary = plan_sites(ctx, PlanParams(spacing_m=6, side="both", mode="grid"), pack, tilia)
    invalid = [s for s in sites if s.verdict == "invalid"]
    assert {s.site_id for s in invalid} == {"L0321", "R0321"}
    for s in invalid:
        assert set(s.props.failed_rules) & {"plantable_surface", "carriageway_edge"}
        assert s.props.edge_distance_m > 10.0     # ray ran along the side street
    assert summary.invalid == 2 and summary.must_failures == 2


def test_site_props_and_rule_results(grid, tilia):
    sites, _ = grid
    site = next(s for s in sites if s.site_id == "R0052")
    p = site.props
    assert p.verdict == "valid" and p.side == "right" and p.station_m == 52.0
    assert p.edge_distance_m == pytest.approx(3.5)
    assert p.species_id == "tilia_cordata" and p.crown_d_mature_m == tilia.mature_crown_d_m
    assert list(p.crown_d_by_year) == [str(y) for y in YEARS]
    assert p.height_by_year["30"] == pytest.approx(height_at(tilia, 30), abs=0.01)
    by_rule = {r.rule_id: r for r in p.rules}
    assert by_rule["carriageway_edge"].measured_m == pytest.approx(1.0)
    assert by_rule["cycleway"].measured_m == pytest.approx(0.5) and by_rule["cycleway"].passed
    assert by_rule["building_crown"].measured_m == pytest.approx(2.5)
    assert by_rule["plantable_surface"].passed is True and by_rule["plantable_surface"].measured_m is None
    assert by_rule["sidewalk_passage"].measured_m == pytest.approx(1.5) and by_rule["sidewalk_passage"].passed
    assert all(r.basis == "measured" for r in p.rules)
    assert by_rule["existing_tree"].assumption is True and by_rule["carriageway_edge"].assumption is False


def test_cycleway_fails_with_small_offset(ctx, pack, tilia):
    params = PlanParams(offset_from_edge_m=0.4, side="right",
                        rule_overrides={"carriageway_edge": RuleOverride(min_distance_m=0.3)})
    sites, summary = plan_sites(ctx, params, pack, tilia)
    for s in sites:
        if s.station_m < 200:
            assert "cycleway" in s.props.failed_rules and s.verdict == "conditional", s.site_id
        else:
            assert "cycleway" not in s.props.failed_rules, s.site_id
    assert summary.failures_by_rule["cycleway"] == 25


def test_single_side_and_evaluate_site_direct(ctx, pack, tilia):
    sites, summary = plan_sites(ctx, PlanParams(side="left", spacing_m=10), pack, tilia)
    assert summary.total == 40 and all(s.side == "left" for s in sites)
    x0, y0 = ctx.axis.coords[0]
    verdict, results, notes = evaluate_site(Point(x0 + 52, y0 - 4.5), ctx, pack, tilia, PlanParams())
    assert verdict == "valid" and len(results) == len(pack.rules) and notes == []
    verdict, results, notes = evaluate_site(Point(x0 + 52, y0 - 30), ctx, pack, tilia, PlanParams())
    assert verdict == "invalid"
    by_rule = {r.rule_id: r for r in results}
    assert by_rule["plantable_surface"].passed is False
    assert by_rule["sidewalk_passage"].passed is None and by_rule["sidewalk_passage"].note


# ----------------------------------------------------------------------------- pack mode
def test_pack_count_ge_grid_valid(ctx, pack, tilia, grid):
    _, grid_summary = grid
    sites, summary = plan_sites(ctx, PlanParams(spacing_m=8, side="both", mode="pack"), pack, tilia)
    assert summary.total >= grid_summary.valid
    assert summary.invalid == 0 and summary.planted == summary.total
    for side in ("left", "right"):
        stations = [s.station_m for s in sites if s.side == side]
        assert stations == sorted(stations)
        assert all(b - a >= 8 - 1e-6 for a, b in zip(stations, stations[1:]))
    assert summary.gaps == []


def test_pack_records_gaps(ctx, pack, tilia):
    params = PlanParams(spacing_m=8, side="right", mode="pack",
                        rule_overrides={"junction": RuleOverride(mode="must", min_distance_m=12.0)})
    sites, summary = plan_sites(ctx, params, pack, tilia)
    assert all(s.verdict != "invalid" for s in sites)
    assert all(min(s.pt.distance(j) for j in ctx.junctions) >= 12.0 - 1e-6 for s in sites)
    assert summary.gaps and all(g.reason == "junction" and g.side == "right" for g in summary.gaps)
    crossing = [g for g in summary.gaps if g.station_from_m < 320 < g.station_to_m]
    assert len(crossing) == 1 and crossing[0].station_to_m - crossing[0].station_from_m > 16


# ----------------------------------------------------------------------------- overrides
def test_apply_overrides_flips_outcomes(ctx, pack, tilia, grid):
    _, base_summary = grid
    strict = apply_overrides(pack, {"carriageway_edge": RuleOverride(min_distance_m=3.0),
                                    "ghost_rule": RuleOverride(min_distance_m=1.0)})
    rule = next(r for r in strict.rules if r.id == "carriageway_edge")
    assert rule.min_distance_m == 3.0 and rule.overridden is True
    assert all(r.overridden is False for r in pack.rules)
    assert next(r for r in pack.rules if r.id == "carriageway_edge").min_distance_m == 0.5
    _, summary = plan_sites(ctx, PlanParams(), strict, tilia)
    assert summary.invalid > base_summary.invalid and summary.invalid == summary.total
    assert summary.failures_by_rule["carriageway_edge"] == summary.total


def test_disabled_rule_is_skipped(ctx, pack, tilia):
    relaxed = apply_overrides(pack, {"junction": RuleOverride(enabled=False)})
    sites, summary = plan_sites(ctx, PlanParams(side="right"), relaxed, tilia)
    assert all("junction" not in {r.rule_id for r in s.props.rules} for s in sites)
    assert summary.valid == summary.total


# ----------------------------------------------------------------------------- canopy
def test_canopy_monotonic_and_existing_cover(ctx, grid, tilia):
    sites, _ = grid
    result = canopy_metrics(ctx, sites, tilia)
    assert result.corridor_area_m2 == pytest.approx(12000.0)
    assert 0 < result.sidewalk_area_m2 < result.street_area_m2 < result.corridor_area_m2
    assert result.existing_cover_corridor_pct > 0
    assert [y.year for y in result.years] == list(YEARS)
    cover = [y.cover_corridor_pct for y in result.years]
    assert cover == sorted(cover) and cover[-1] > cover[0]
    for y in result.years:
        assert y.new_crown_area_m2 <= y.total_crown_area_m2 <= result.corridor_area_m2
        assert 0 <= y.cover_street_pct <= 100 and 0 <= y.sidewalk_under_crown_pct <= 100
    assert result.years[0].total_crown_area_m2 >= result.corridor_area_m2 * result.existing_cover_corridor_pct / 100 - 1


def test_canopy_excludes_invalid_sites(ctx, pack, tilia):
    strict = apply_overrides(pack, {"carriageway_edge": RuleOverride(min_distance_m=3.0)})
    sites, _ = plan_sites(ctx, PlanParams(), strict, tilia)
    result = canopy_metrics(ctx, sites, tilia, years=[30])
    assert result.years[0].new_crown_area_m2 == 0.0
    assert result.years[0].cover_corridor_pct == result.existing_cover_corridor_pct


# ----------------------------------------------------------------------------- shade
def test_sun_position_reference_cases():
    elev, az = sun_position(0.0, 0.0, 2026, 3, 20, 12.0, 0.0)
    assert elev > 85
    _, az_morning = sun_position(0.0, 0.0, 2026, 3, 20, 6.5, 0.0)
    assert abs(az_morning - 90) < 3
    elev_night, _ = sun_position(47.38, 8.53, 2026, 7, 15, 23.0, 2.0)
    assert elev_night < 0


def test_shade_july_afternoon(ctx, grid, tilia):
    sites, _ = grid
    result = shade_polygons(ctx, sites, tilia, year=30, month=7, day=15, hour=15.0, include_existing=True)
    assert 40 <= result.sun_elevation_deg <= 60
    assert 220 <= result.sun_azimuth_deg <= 260
    assert result.when.endswith("-07-15 15:00 local")
    kinds = [f["properties"]["kind"] for f in result.shadows.features]
    assert kinds.count("new") == 100 and kinds.count("existing") == 3
    assert 0 < result.shaded_sidewalk_pct <= 100 and 0 < result.shaded_street_pct <= 100
    assert 0 < result.shaded_corridor_pct <= 100
    ellipse = shadow_ellipse(Point(0, 0), 10.0, 18.0, result.sun_elevation_deg, result.sun_azimuth_deg)
    assert ellipse.centroid.x > 0 and ellipse.centroid.y > 0            # displaced to the north-east
    expected = math.pi * 5.0 * 5.0 / math.sin(math.radians(result.sun_elevation_deg))
    assert ellipse.area == pytest.approx(expected, rel=0.05)


def test_shade_low_sun_is_empty(ctx, grid, tilia):
    sites, _ = grid
    result = shade_polygons(ctx, sites, tilia, year=30, month=7, day=15, hour=22.0, include_existing=False)
    assert result.sun_elevation_deg < 5
    assert result.shadows.features == [] and result.shaded_corridor_pct == 0.0
    assert "sun below" in result.when


def test_shade_without_existing(ctx, grid, tilia):
    sites, _ = grid
    result = shade_polygons(ctx, sites, tilia, year=0, month=7, day=15, hour=15.0, include_existing=False)
    assert all(f["properties"]["kind"] == "new" for f in result.shadows.features)
    assert result.shaded_corridor_pct < 10


# ----------------------------------------------------------------------------- performance
def test_plan_sites_long_street_is_fast(pack, tilia):
    long_ctx = synthetic_street(length_m=1300)
    start = time.perf_counter()
    sites, summary = plan_sites(long_ctx, PlanParams(spacing_m=8, side="both", mode="grid"), pack, tilia)
    elapsed = time.perf_counter() - start
    assert summary.total == len(sites) == 2 * 162
    assert elapsed < 2.0
