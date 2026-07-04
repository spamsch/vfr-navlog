"""OFM tile math and excerpt tests. No network: fixture tiles + a stub fetch."""
from datetime import date
from pathlib import Path

from PIL import Image

from vfr_navlog import ofm

FIX = Path(__file__).parent / "fixtures" / "ofm"
BASE_BLOB = (FIX / "base_512.jpg").read_bytes()
AERO_BLOB = (FIX / "aero_512.png").read_bytes()


def _stub_fetch(layer, z, x, y, cycle, cache_dir=None):
    return BASE_BLOB if layer == "base" else AERO_BLOB


# --- AIRAC arithmetic ------------------------------------------------------

def test_airac_on_anchor():
    assert ofm.airac_cycle(date(2026, 1, 22)) == "2601"


def test_airac_current_verified_cycle():
    # 2606 is the cycle verified working against the live server on 2026-07-04.
    assert ofm.airac_cycle(date(2026, 7, 4)) == "2606"


def test_airac_exactly_on_a_later_cycle_date():
    # 2606 becomes effective 2026-06-11 (2601 + 5*28 days).
    assert ofm.airac_cycle(date(2026, 6, 11)) == "2606"


def test_airac_across_year_boundary():
    # 2026-01-01 falls in cycle 2513 (effective 2025-12-25).
    assert ofm.airac_cycle(date(2026, 1, 1)) == "2513"
    # The day before the 2601 anchor is still 2513.
    assert ofm.airac_cycle(date(2026, 1, 21)) == "2513"
    # First cycle of 2025.
    assert ofm.airac_cycle(date(2025, 1, 23)) == "2501"


def test_previous_cycle():
    assert ofm.previous_cycle(date(2026, 7, 4)) == "2605"
    assert ofm.previous_cycle(date(2026, 1, 22)) == "2513"


# --- Mercator tile / pixel math --------------------------------------------

def test_deg2num_known_value():
    # Berlin 52.5200 N, 13.4050 E at z11 -> slippy tile (1100, 671).
    x, y = ofm.deg2num(52.5200, 13.4050, 11)
    assert (int(x), int(y)) == (1100, 671)


def test_crop_pixel_centers_the_center():
    lat, lon, r = 52.46, 9.68, 3.0
    _, _, side = ofm._crop_origin(lat, lon, r)
    px, py = ofm.crop_pixel(lat, lon, lat, lon, r, side)
    assert abs(px - side / 2) <= 1
    assert abs(py - side / 2) <= 1


def test_crop_pixel_north_is_up():
    lat, lon, r = 52.46, 9.68, 3.0
    _, _, side = ofm._crop_origin(lat, lon, r)
    # A point due north (same lon, higher lat) lands above the centre.
    px, py = ofm.crop_pixel(lat + 0.02, lon, lat, lon, r, side)
    assert abs(px - side / 2) <= 1
    assert py < side / 2


# --- Excerpt stitching + annotation ----------------------------------------

def test_map_excerpt_crop_size_and_composite():
    img = ofm.map_excerpt(52.46, 9.68, 3.0, "2606", cache_dir=Path("/nonexistent"),
                          fetch=_stub_fetch)
    assert img is not None
    _, _, side = ofm._crop_origin(52.46, 9.68, 3.0)
    assert img.size == (side, side)
    assert img.mode == "RGB"


def test_map_excerpt_none_when_base_missing():
    def no_base(layer, z, x, y, cycle, cache_dir=None):
        return None if layer == "base" else AERO_BLOB
    img = ofm.map_excerpt(52.46, 9.68, 3.0, "2606", cache_dir=Path("/nonexistent"),
                          fetch=no_base)
    assert img is None


def test_map_excerpt_base_only_when_aero_missing():
    def base_only(layer, z, x, y, cycle, cache_dir=None):
        return BASE_BLOB if layer == "base" else None
    img = ofm.map_excerpt(52.46, 9.68, 3.0, "2606", cache_dir=Path("/nonexistent"),
                          fetch=base_only)
    assert img is not None  # ground picture alone is still a page


def test_annotate_marks_center_and_route():
    img = Image.new("RGB", (400, 400), (120, 170, 90))
    lat, lon, r = 52.46, 9.68, 3.0
    out = ofm.annotate(img, lat, lon, r, (lat - 0.02, lon), (lat + 0.02, lon))
    assert out.size == (400, 400)
    # Magenta route pixels appear along the vertical centre line.
    px = out.load()
    found = any(px[200, y][0] > 150 and px[200, y][2] > 150 and px[200, y][1] < 100
                for y in range(400))
    assert found


# --- Disk cache ------------------------------------------------------------

def test_cache_hit_skips_http(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_http(url, timeout=ofm.TILE_TIMEOUT):
        calls["n"] += 1
        return 200, BASE_BLOB

    monkeypatch.setattr(ofm, "_http_get", fake_http)
    first = ofm.fetch_tile("base", 12, 2156, 1345, "2606", cache_dir=tmp_path)
    second = ofm.fetch_tile("base", 12, 2156, 1345, "2606", cache_dir=tmp_path)
    assert first == BASE_BLOB and second == BASE_BLOB
    assert calls["n"] == 1  # second call served from disk
    assert (tmp_path / "2606" / "base" / "12" / "2156" / "1345.jpg").exists()


def test_fetch_tile_none_on_404(tmp_path, monkeypatch):
    monkeypatch.setattr(ofm, "_http_get", lambda url, timeout=ofm.TILE_TIMEOUT: (404, None))
    assert ofm.fetch_tile("base", 12, 1, 1, "9999", cache_dir=tmp_path) is None


# --- Run-level orchestration -----------------------------------------------

class _WP:
    def __init__(self, ident, lat, lon):
        self.ident, self.lat, self.lon = ident, lat, lon


class _Plan:
    def __init__(self, wps):
        self.waypoints = wps


def test_prepare_degrades_to_none_without_network(tmp_path, monkeypatch):
    # Every tile fetch fails -> every waypoint yields None, no exception, PDF safe.
    monkeypatch.setattr(ofm, "fetch_tile",
                        lambda *a, **k: None)
    plan = _Plan([_WP("EDDV", 52.46, 9.68), _WP("EDLI", 51.96, 8.54)])
    maps = ofm.prepare_waypoint_maps(plan, 3.0, date(2026, 7, 4), cache_dir=tmp_path)
    assert maps == [None, None]


def test_prepare_falls_back_to_previous_cycle(tmp_path, monkeypatch):
    # Current cycle 404s; previous cycle serves tiles -> maps carry the prev cycle.
    def fetch(layer, z, x, y, cycle, cache_dir=None):
        if cycle == "2606":
            return None
        return BASE_BLOB if layer == "base" else AERO_BLOB

    monkeypatch.setattr(ofm, "fetch_tile", fetch)
    plan = _Plan([_WP("EDDV", 52.46, 9.68), _WP("EDLI", 51.96, 8.54)])
    maps = ofm.prepare_waypoint_maps(plan, 3.0, date(2026, 7, 4), cache_dir=tmp_path)
    assert all(m is not None for m in maps)
    assert all(m.cycle == "2605" for m in maps)
