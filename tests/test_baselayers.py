"""Base-layer providers: coverage routing, cascade, blank detection, WMS bbox
math, and the S2 stitcher. No network — a stub _http_get branches on URL."""
import io
from datetime import date
from pathlib import Path

from PIL import Image

from vfr_navlog import baselayers as bl
from vfr_navlog import ofm

FIX = Path(__file__).parent / "fixtures" / "ofm"


def _jpeg(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), color).save(buf, format="JPEG")
    return buf.getvalue()


def _photo_jpeg(size=(256, 256)) -> bytes:
    """A wide-range, non-uniform image standing in for an orthophoto."""
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = ((x * 7) % 256, (y * 5 + 40) % 256, (x + y) % 256)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _half_white_jpeg(size=(256, 256)) -> bytes:
    """A border-straddling WMS answer: real imagery on one half, white fill on
    the other — what a state server returns for a bbox reaching across its
    border (seen live: NI at Minden, right on the NI/NRW line)."""
    img = Image.open(io.BytesIO(_photo_jpeg(size))).convert("RGB")
    img.paste((255, 255, 255), (0, 0, size[0] // 2, size[1]))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


PHOTO_BLOB = _photo_jpeg()          # a real, wide-range JPEG
WHITE_BLOB = _jpeg((255, 255, 255))
HALF_WHITE_BLOB = _half_white_jpeg()
S2_TILE_BLOB = _photo_jpeg()


# --- Pure geometry ---------------------------------------------------------

def test_wms_bbox_3857_hand_computed():
    # lat 52, lon 9, radius 3 NM. EPSG:3857 metres, computed independently.
    minx, miny, maxx, maxy = bl.wms_bbox_3857(52.0, 9.0, 3.0)
    assert abs(minx - 992850.977) < 0.01
    assert abs(miny - 6791101.014) < 0.01
    assert abs(maxx - 1010899.857) < 0.01
    assert abs(maxy - 6809149.894) < 0.01
    # Symmetric about the Mercator centre of (52, 9).
    cx, cy = bl.merc_xy(52.0, 9.0)
    assert abs((minx + maxx) / 2 - cx) < 1e-6
    assert abs((miny + maxy) / 2 - cy) < 1e-6


def test_s2_crop_geometry_centers_waypoint():
    lat, lon, r, z = 52.48, 9.32, 3.0, 13
    left, top, side = bl.s2_crop_geometry(lat, lon, r, z)
    gx, gy = ofm.deg2num(lat, lon, z)
    cx, cy = gx * bl.S2_TILE, gy * bl.S2_TILE
    assert abs((left + side / 2) - cx) <= 1
    assert abs((top + side / 2) - cy) <= 1
    assert side > 0


def test_is_blank_detects_uniform_white():
    assert bl._is_blank(Image.new("RGB", (300, 300), (255, 255, 255)))
    assert not bl._is_blank(Image.open(io.BytesIO(PHOTO_BLOB)))


def test_insufficient_coverage_half_white_rejected():
    # Border-straddling answer: 50% white fill >> the 25% threshold.
    assert bl._insufficient_coverage(Image.open(io.BytesIO(HALF_WHITE_BLOB)))
    # Fully blank still caught (fast path).
    assert bl._insufficient_coverage(Image.new("RGB", (300, 300), (255, 255, 255)))
    # A real photo — even one with some white buildings — passes.
    photo = Image.open(io.BytesIO(PHOTO_BLOB)).convert("RGB")
    photo.paste((255, 255, 255), (0, 0, 40, 40))  # ~2.4% white roofs
    assert not bl._insufficient_coverage(photo)


# --- WMS provider ----------------------------------------------------------

def _dop_ni(tmp_path):
    return bl.DopWms(
        name="photo-dop-ni",
        endpoint="https://opendata.lgln.niedersachsen.de/doorman/noauth/dop_wms",
        layer="ni_dop20", license_line="NI-LINE",
        coverage=(6.40, 51.05, 11.85, 54.20), cache_dir=tmp_path)


def test_dop_skips_outside_coverage(tmp_path):
    ni = _dop_ni(tmp_path)
    # Copenhagen is outside NI — no network attempt, straight None.
    calls = {"n": 0}

    def fetch(url, timeout=bl.HTTP_TIMEOUT):
        calls["n"] += 1
        return 200, PHOTO_BLOB

    assert ni.get_image(55.68, 12.57, 3.0, 800, fetch=fetch) is None
    assert calls["n"] == 0


def test_dop_returns_image_and_caches(tmp_path):
    ni = _dop_ni(tmp_path)
    calls = {"n": 0}

    def fetch(url, timeout=bl.HTTP_TIMEOUT):
        calls["n"] += 1
        assert "REQUEST=GetMap" in url and "CRS=EPSG%3A3857" in url
        return 200, PHOTO_BLOB

    img1 = ni.get_image(53.0, 10.0, 3.0, 800, fetch=fetch)
    img2 = ni.get_image(53.0, 10.0, 3.0, 800, fetch=fetch)
    assert img1 is not None and img2 is not None
    assert calls["n"] == 1  # second served from disk cache


def test_dop_blank_white_is_none(tmp_path):
    ni = _dop_ni(tmp_path)
    img = ni.get_image(53.0, 10.0, 3.0, 800,
                       fetch=lambda url, timeout=bl.HTTP_TIMEOUT: (200, WHITE_BLOB))
    assert img is None


def test_dop_partial_border_fill_is_none(tmp_path):
    # Half imagery, half white border fill (the live Minden case) → rejected.
    ni = _dop_ni(tmp_path)
    img = ni.get_image(53.0, 10.0, 3.0, 800,
                       fetch=lambda url, timeout=bl.HTTP_TIMEOUT: (200, HALF_WHITE_BLOB))
    assert img is None


# --- Sentinel-2 stitcher ---------------------------------------------------

def test_sentinel2_stitches_square(tmp_path):
    s2 = bl.Sentinel2(cache_dir=tmp_path)
    img = s2.get_image(55.68, 12.57, 3.0, 900,
                       fetch=lambda url, timeout=bl.HTTP_TIMEOUT: (200, S2_TILE_BLOB))
    assert img is not None
    _, _, side = bl.s2_crop_geometry(55.68, 12.57, 3.0, s2.zoom)
    assert img.size == (side, side)
    assert img.mode == "RGB"


def test_sentinel2_none_when_all_tiles_missing(tmp_path):
    s2 = bl.Sentinel2(cache_dir=tmp_path)
    img = s2.get_image(55.68, 12.57, 3.0, 900,
                       fetch=lambda url, timeout=bl.HTTP_TIMEOUT: (404, None))
    assert img is None


# --- Cascade routing -------------------------------------------------------

def _route_fetch():
    """Stub _http_get that mimics live coverage: NI/NRW serve photos inside their
    own areas, EOX serves everywhere; each records what was hit."""
    hits = {"ni": 0, "nrw": 0, "s2": 0}

    def fetch(url, timeout=bl.HTTP_TIMEOUT):
        if "niedersachsen" in url:
            hits["ni"] += 1
            return 200, PHOTO_BLOB
        if "wms.nrw.de" in url:
            hits["nrw"] += 1
            return 200, PHOTO_BLOB
        if "tiles.maps.eox.at" in url:
            hits["s2"] += 1
            return 200, S2_TILE_BLOB
        return 404, None

    return fetch, hits


def test_cascade_ni_point_uses_dop_ni(tmp_path, monkeypatch):
    fetch, hits = _route_fetch()
    monkeypatch.setattr(bl, "_http_get", fetch)
    provs = bl.photo_providers(tmp_path)
    # 53.0/10.0 is NI-only (below NRW's northern edge).
    img, attr = bl._build_photo(53.0, 10.0, 3.0, 800, provs)
    assert img is not None
    assert "LGLN" in attr or hits["ni"] > 0
    assert hits["nrw"] == 0 and hits["s2"] == 0


def test_cascade_nrw_point_uses_dop_nrw(tmp_path, monkeypatch):
    fetch, hits = _route_fetch()
    monkeypatch.setattr(bl, "_http_get", fetch)
    provs = bl.photo_providers(tmp_path)
    # 50.9/7.0 (Köln) is south of NI coverage → NRW serves it.
    img, attr = bl._build_photo(50.9, 7.0, 3.0, 800, provs)
    assert img is not None
    assert "Geobasis NRW" in attr
    assert hits["s2"] == 0


def test_cascade_denmark_falls_to_sentinel2(tmp_path, monkeypatch):
    fetch, hits = _route_fetch()
    monkeypatch.setattr(bl, "_http_get", fetch)
    provs = bl.photo_providers(tmp_path)
    img, attr = bl._build_photo(55.68, 12.57, 3.0, 800, provs)  # Copenhagen
    assert img is not None
    assert "Sentinel-2 cloudless" in attr
    assert hits["s2"] > 0


def test_cascade_blank_dop_falls_through(tmp_path, monkeypatch):
    # NI answers white (outside its real data); cascade must reach Sentinel-2.
    def fetch(url, timeout=bl.HTTP_TIMEOUT):
        if "niedersachsen" in url:
            return 200, WHITE_BLOB
        if "tiles.maps.eox.at" in url:
            return 200, S2_TILE_BLOB
        return 404, None

    monkeypatch.setattr(bl, "_http_get", fetch)
    provs = bl.photo_providers(tmp_path)
    img, attr = bl._build_photo(53.0, 10.0, 3.0, 800, provs)  # NI-only point
    assert img is not None
    assert "Sentinel-2 cloudless" in attr


def test_cascade_partial_dop_falls_through_to_next_state(tmp_path, monkeypatch):
    # The Minden regression: a border point inside both rectangles; NI answers
    # half imagery / half white fill, NRW has the full picture. The cascade must
    # reject NI's partial answer and land on NRW.
    def fetch(url, timeout=bl.HTTP_TIMEOUT):
        if "niedersachsen" in url:
            return 200, HALF_WHITE_BLOB
        if "wms.nrw.de" in url:
            return 200, PHOTO_BLOB
        return 404, None

    monkeypatch.setattr(bl, "_http_get", fetch)
    provs = bl.photo_providers(tmp_path)
    img, attr = bl._build_photo(52.29, 8.92, 3.0, 800, provs)  # Minden: NI∩NRW
    assert img is not None
    assert "Geobasis NRW" in attr


# --- Orchestration ---------------------------------------------------------

class _WP:
    def __init__(self, ident, lat, lon):
        self.ident, self.lat, self.lon = ident, lat, lon


class _Plan:
    def __init__(self, wps):
        self.waypoints = wps


def _both_fetch_ofm(monkeypatch):
    base = PHOTO_BLOB
    aero = (FIX / "aero_512.png").read_bytes()
    monkeypatch.setattr(ofm, "fetch_tile",
                        lambda layer, z, x, y, cycle, cache_dir=None:
                        base if layer == "base" else aero)


def test_map_base_chart_never_calls_photo(tmp_path, monkeypatch):
    _both_fetch_ofm(monkeypatch)

    def no_photo(url, timeout=bl.HTTP_TIMEOUT):
        raise AssertionError(f"photo endpoint hit in chart-only mode: {url}")

    monkeypatch.setattr(bl, "_http_get", no_photo)
    plan = _Plan([_WP("EDDV", 52.46, 9.68), _WP("EDLI", 51.96, 8.54)])
    res = bl.prepare_waypoint_layers(plan, 3.0, date(2026, 7, 4),
                                     map_base="chart", cache_dir=tmp_path)
    assert all(r is not None for r in res)
    assert all(r.chart is not None and r.photo is None for r in res)


def test_prepare_both_pairs_chart_and_photo(tmp_path, monkeypatch):
    _both_fetch_ofm(monkeypatch)
    fetch, _ = _route_fetch()
    monkeypatch.setattr(bl, "_http_get", fetch)
    plan = _Plan([_WP("EDDV", 52.46, 9.68), _WP("EDLI", 51.96, 8.54)])
    res = bl.prepare_waypoint_layers(plan, 3.0, date(2026, 7, 4),
                                     map_base="both", cache_dir=tmp_path)
    assert all(r is not None for r in res)
    for r in res:
        assert r.chart is not None and r.photo is not None
        assert r.chart.size == r.photo.size  # aligned, same annotation grid
        assert r.chart_attribution and "open flightmaps" in r.chart_attribution
        assert r.photo_attribution


# --- DFS ICAO chart (opt-in chart source) -----------------------------------

def _png(rgba=None, size=256):
    if rgba is not None:
        img = Image.new("RGBA", (size, size), rgba)
    else:
        # Chart-ish tile with real dynamic range so the blank detector passes.
        img = Image.new("RGBA", (size, size))
        img.putdata([(x % 256, y % 256, (x + y) % 256, 255)
                     for y in range(size) for x in range(size)])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


CHART_PNG = _png()                              # opaque, wide-range chart tile
FILLER_PNG = _png((0, 0, 0, 0))                 # transparent filler outside coverage


def test_dfs_icao_stitches_and_flattens_alpha(tmp_path):
    dfs = bl.DfsIcaoChart(cache_dir=tmp_path)
    img = dfs.get_image(52.4, 9.7, 3.0, 900,
                        fetch=lambda url, timeout=bl.HTTP_TIMEOUT: (200, CHART_PNG))
    assert img is not None
    _, _, side = bl.s2_crop_geometry(52.4, 9.7, 3.0, dfs.zoom)
    assert img.size == (side, side)
    assert img.mode == "RGB"


def test_dfs_icao_transparent_filler_is_none(tmp_path):
    # Outside chart coverage every tile is transparent filler → flattens to a
    # uniform white square → provider degrades to None instead of framing it.
    dfs = bl.DfsIcaoChart(cache_dir=tmp_path)
    img = dfs.get_image(48.0, 2.0, 3.0, 900,
                        fetch=lambda url, timeout=bl.HTTP_TIMEOUT: (200, FILLER_PNG))
    assert img is None


def test_dfs_icao_tiles_are_cached(tmp_path):
    dfs = bl.DfsIcaoChart(cache_dir=tmp_path)
    calls = {"n": 0}

    def fetch(url, timeout=bl.HTTP_TIMEOUT):
        calls["n"] += 1
        return 200, CHART_PNG

    dfs.get_image(52.4, 9.7, 3.0, 900, fetch=fetch)
    first = calls["n"]
    assert first > 0
    dfs.get_image(52.4, 9.7, 3.0, 900, fetch=fetch)
    assert calls["n"] == first          # second run fully served from disk
    assert any((tmp_path / "dfs_icao500").rglob("*.png"))
