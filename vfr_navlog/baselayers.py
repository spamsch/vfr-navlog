"""Pluggable base-layer providers: the OFM chart plus photographic imagery.

The waypoint briefing page shows two maps side by side. The left one is the OFM
chart (airspace, navaids); the right one is a photograph of the same square, so a
pilot can match "what the sectional says" against "what it looks like out the
window". This module is the photo half plus the small protocol that lets the two
sit behind one interface.

Providers implement `BaseLayer`:

    name           : "chart" | "photo-dop-ni" | "photo-s2" | …
    attribution()  : the exact licence line for the page caption
    get_image(...) : a raw RGB excerpt centred on the waypoint, or None

Photo cascade, best first: state orthophoto by coverage rectangle (DOP20, 20 cm)
→ Sentinel-2 cloudless (Europe-wide, 10 m) → None. A provider that has no data
for a point returns None and the next one is tried. WMS servers answer *outside*
their real coverage with a blank white image; `_is_blank` catches that so the
cascade continues instead of pasting a white square.

Everything above the wire is pure and unit-testable: the EPSG:3857 bbox math, the
slippy-tile crop geometry, and the blank detector. Only `_http_get` (and the
`fetch` it is passed as) touches the network, and it is resolved at call time so
tests can monkeypatch it — the same pattern `ofm.map_excerpt` uses.

Licences (verified live 2026-07-04 via GetCapabilities):
- Niedersachsen DOP20: CC BY 4.0. Quellenvermerk per the service's own example.
- Nordrhein-Westfalen DOP: Datenlizenz Deutschland Zero 2.0 (no attribution
  required, but Geobasis NRW is credited as good practice).
- EOX Sentinel-2 cloudless: CC BY-NC-SA 4.0, attribution string mandatory verbatim.
"""
from __future__ import annotations

import io
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from PIL import Image

from . import ofm
from .config import VATSIM_UA

# --- Constants -------------------------------------------------------------

CACHE_ROOT = Path.home() / ".cache" / "vfr-navlog"
IMAGERY_MAX_AGE_S = 365 * 24 * 3600     # ~1 year: photo imagery is not AIRAC-bound
HTTP_TIMEOUT = 10.0
MAX_WORKERS = 4                         # polite per-host concurrency cap
S2_TILE = 256                           # EOX WMTS tile size (vs OFM's 512)

# EPSG:3857 uses a spherical Earth of this radius (WGS84 semi-major axis).
_R = 6378137.0
_NM_M = 1852.0

Fetch = Callable[[str], "tuple[int, bytes | None]"]


@runtime_checkable
class BaseLayer(Protocol):
    """One image source for a waypoint excerpt."""
    name: str

    def attribution(self) -> str:
        """The exact licence line for the page caption."""

    def get_image(self, lat: float, lon: float, radius_nm: float,
                  min_px: int) -> Image.Image | None:
        """A raw RGB excerpt centred on (lat, lon), or None if unavailable."""


# --- HTTP + disk cache -----------------------------------------------------

def _http_get(url: str, timeout: float = HTTP_TIMEOUT) -> tuple[int, bytes | None]:
    """GET *url*. Returns (status, body); status 0 on a network error, body None
    on any non-200. Never raises."""
    req = urllib.request.Request(url, headers={"User-Agent": VATSIM_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, None


def _cached_get(path: Path, url: str, fetch: Fetch,
                max_age: float = IMAGERY_MAX_AGE_S) -> bytes | None:
    """Disk-cache-first GET. A cache file younger than *max_age* is served
    without touching the network; otherwise fetch and, on 200, store it."""
    if path.exists() and (time.time() - path.stat().st_mtime) <= max_age:
        return path.read_bytes()
    status, body = fetch(url)
    if status == 200 and body:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return body
    return None


# --- Pure geometry ---------------------------------------------------------

def merc_xy(lat: float, lon: float) -> tuple[float, float]:
    """WGS84 lat/lon to EPSG:3857 (Web Mercator) easting/northing in metres."""
    x = math.radians(lon) * _R
    y = math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0)) * _R
    return x, y


def wms_bbox_3857(lat: float, lon: float, radius_nm: float) -> tuple[float, float, float, float]:
    """EPSG:3857 bbox (minx, miny, maxx, maxy) of the square covering *radius_nm*
    ground miles around (lat, lon).

    The half-width in projected metres is radius_nm·1852 / cos(lat): Web Mercator
    stretches by 1/cos(lat), so a ground nautical mile is that many projected
    metres. This is exactly the ground square the OFM excerpt crops, so the chart
    and the photo show the same extent and the marker lands in the same place.
    """
    cx, cy = merc_xy(lat, lon)
    half = radius_nm * _NM_M / math.cos(math.radians(lat))
    return (cx - half, cy - half, cx + half, cy + half)


def s2_crop_geometry(lat: float, lon: float, radius_nm: float,
                     zoom: int) -> tuple[int, int, int]:
    """(left, top, side) of the excerpt in global S2_TILE-pixel coordinates at
    *zoom*. The waypoint sits at (left + side/2, top + side/2)."""
    n = 2.0 ** zoom
    gx, gy = ofm.deg2num(lat, lon, zoom)
    cx, cy = gx * S2_TILE, gy * S2_TILE
    mpp = ofm._EARTH_CIRC_M * math.cos(math.radians(lat)) / (n * S2_TILE)
    half = radius_nm * _NM_M / mpp
    return int(round(cx - half)), int(round(cy - half)), int(round(2 * half))


def _is_blank(img: Image.Image, tol: int = 8) -> bool:
    """True if *img* is near-uniform — a WMS server's answer outside its actual
    coverage (typically pure white). A real orthophoto spans a wide range in
    every channel, so the extrema spread rules it out."""
    ex = img.convert("RGB").getextrema()
    return all(hi - lo <= tol for lo, hi in ex)


# --- Chart adapter ---------------------------------------------------------

@dataclass
class OfmChart:
    """Adapter over the existing OFM composite (base z12 + aero z11). The tile
    logic stays in ofm.py; this is only the BaseLayer face of it."""
    cycle: str
    cache_dir: Path = ofm.DEFAULT_CACHE
    name: str = "chart"

    def attribution(self) -> str:
        return f"© open flightmaps — OFMA General Users' License — AIRAC {self.cycle}"

    def get_image(self, lat: float, lon: float, radius_nm: float,
                  min_px: int) -> Image.Image | None:
        # map_excerpt already crops to the OFM excerpt side; min_px is irrelevant.
        return ofm.map_excerpt(lat, lon, radius_nm, self.cycle, cache_dir=self.cache_dir)


# --- State orthophoto (DOP20) over WMS -------------------------------------

@dataclass
class DopWms:
    """A state DOP20 orthophoto served as a single WMS GetMap.

    One GetMap returns the exact bbox — no tile stitching. Coverage is a rough
    lat/lon rectangle; a point outside it skips the provider, and a point inside
    it that the server answers with white is rejected by `_is_blank`."""
    name: str
    endpoint: str
    layer: str
    license_line: str
    coverage: tuple[float, float, float, float]   # (min_lon, min_lat, max_lon, max_lat)
    cache_dir: Path = CACHE_ROOT

    def attribution(self) -> str:
        return self.license_line

    def covers(self, lat: float, lon: float) -> bool:
        mn_lon, mn_lat, mx_lon, mx_lat = self.coverage
        return mn_lon <= lon <= mx_lon and mn_lat <= lat <= mx_lat

    def _url(self, bbox: tuple[float, float, float, float], size: int) -> str:
        minx, miny, maxx, maxy = bbox
        params = {
            "SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": "1.3.0",
            "LAYERS": self.layer, "STYLES": "", "CRS": "EPSG:3857",
            "BBOX": f"{minx},{miny},{maxx},{maxy}",
            "WIDTH": str(size), "HEIGHT": str(size), "FORMAT": "image/jpeg",
        }
        return f"{self.endpoint}?{urllib.parse.urlencode(params)}"

    def get_image(self, lat: float, lon: float, radius_nm: float, min_px: int,
                  fetch: Fetch | None = None) -> Image.Image | None:
        if not self.covers(lat, lon):
            return None
        if fetch is None:
            fetch = _http_get      # module global; resolved here so tests can patch it
        bbox = wms_bbox_3857(lat, lon, radius_nm)
        size = max(256, int(min_px))
        rb = tuple(round(v) for v in bbox)
        key = f"{self.layer}_{rb[0]}_{rb[1]}_{rb[2]}_{rb[3]}_{size}.jpg"
        path = Path(self.cache_dir) / "dop" / key
        blob = _cached_get(path, self._url(bbox, size), fetch)
        if not blob:
            return None
        try:
            img = Image.open(io.BytesIO(blob)).convert("RGB")
        except Exception:
            return None
        if _is_blank(img):
            return None
        return img


# --- Sentinel-2 cloudless over WMTS ----------------------------------------

@dataclass
class Sentinel2:
    """EOX Sentinel-2 cloudless, stitched from 256 px WMTS tiles. Europe-wide
    fallback when no state DOP covers the waypoint."""
    year: str = "2024"
    zoom: int = 13
    cache_dir: Path = CACHE_ROOT
    name: str = "photo-s2"

    @property
    def layer(self) -> str:
        return f"s2cloudless-{self.year}_3857"

    def attribution(self) -> str:
        return (f"Sentinel-2 cloudless - https://s2maps.eu by EOX IT Services GmbH "
                f"(Contains modified Copernicus Sentinel data {self.year})")

    def _tile_url(self, z: int, x: int, y: int) -> str:
        return (f"https://tiles.maps.eox.at/wmts/1.0.0/{self.layer}"
                f"/default/g/{z}/{y}/{x}.jpg")

    def _tile(self, z: int, x: int, y: int, fetch: Fetch) -> bytes | None:
        path = Path(self.cache_dir) / "s2" / self.layer / str(z) / str(x) / f"{y}.jpg"
        return _cached_get(path, self._tile_url(z, x, y), fetch)

    def get_image(self, lat: float, lon: float, radius_nm: float, min_px: int,
                  fetch: Fetch | None = None) -> Image.Image | None:
        if fetch is None:
            fetch = _http_get
        z = self.zoom
        n = 2 ** z
        left, top, side = s2_crop_geometry(lat, lon, radius_nm, z)
        if side <= 0:
            return None
        tx0, tx1 = left // S2_TILE, (left + side - 1) // S2_TILE
        ty0, ty1 = top // S2_TILE, (top + side - 1) // S2_TILE
        cols, rows = tx1 - tx0 + 1, ty1 - ty0 + 1
        canvas = Image.new("RGB", (cols * S2_TILE, rows * S2_TILE), (255, 255, 255))
        coords = [(tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            blobs = list(ex.map(lambda c: self._tile(z, c[0] % n, c[1], fetch), coords))

        any_tile = False
        for (tx, ty), blob in zip(coords, blobs):
            if not blob:
                continue
            try:
                tile = Image.open(io.BytesIO(blob)).convert("RGB")
            except Exception:
                continue
            canvas.paste(tile, ((tx - tx0) * S2_TILE, (ty - ty0) * S2_TILE))
            any_tile = True
        if not any_tile:
            return None
        ox, oy = left - tx0 * S2_TILE, top - ty0 * S2_TILE
        return canvas.crop((ox, oy, ox + side, oy + side))


# --- Provider table --------------------------------------------------------

# Coverage rectangles are padded a touch beyond each service's advertised
# EX_GeographicBoundingBox so a border waypoint still routes to DOP first; a
# blank-white answer at the true edge falls through to the next provider.
def dop_providers(cache_dir: Path = CACHE_ROOT) -> list[DopWms]:
    return [
        DopWms(
            name="photo-dop-ni",
            endpoint="https://opendata.lgln.niedersachsen.de/doorman/noauth/dop_wms",
            layer="ni_dop20",
            license_line=("Orthofoto: LGLN (2024) Creative Commons Namensnennung "
                          "– 4.0 International (CC BY 4.0)"),
            coverage=(6.40, 51.05, 11.85, 54.20),
            cache_dir=cache_dir,
        ),
        DopWms(
            name="photo-dop-nrw",
            endpoint="https://www.wms.nrw.de/geobasis/wms_nw_dop",
            layer="nw_dop_rgb",
            license_line=("Orthofoto: © Geobasis NRW (2024) — Datenlizenz "
                          "Deutschland Zero 2.0 (dl-de/zero-2-0)"),
            coverage=(5.70, 50.10, 9.55, 52.65),
            cache_dir=cache_dir,
        ),
    ]


def photo_providers(cache_dir: Path = CACHE_ROOT) -> list[BaseLayer]:
    """The photo cascade in priority order: state DOP → Sentinel-2."""
    return [*dop_providers(cache_dir), Sentinel2(cache_dir=cache_dir)]


# --- Per-waypoint result ---------------------------------------------------

@dataclass
class WaypointLayers:
    """Finished, annotated images for one waypoint plus their caption lines.

    Both images (when present) are the same pixel size and share centre, radius
    and scale, so the eye can jump between chart and photo. `chart` carries the
    aero overlay; `photo` is deliberately clean."""
    chart: Image.Image | None = None
    photo: Image.Image | None = None
    chart_attribution: str | None = None
    photo_attribution: str | None = None

    def empty(self) -> bool:
        return self.chart is None and self.photo is None


# --- Cascade + annotation orchestration ------------------------------------

def _resolve_cycle(dep_lat: float, dep_lon: float, today: date, cache_dir: Path) -> str:
    """The OFM AIRAC cycle to use, falling back to the previous one when the
    current cycle is not published yet (same rule as ofm.prepare_waypoint_maps)."""
    cycle = ofm.airac_cycle(today)
    if not ofm._probe(dep_lat, dep_lon, cycle, cache_dir):
        prev = ofm.previous_cycle(today)
        if ofm._probe(dep_lat, dep_lon, prev, cache_dir):
            print(f"[wp-maps] AIRAC {cycle} not published yet — using {prev}", file=sys.stderr)
            cycle = prev
    return cycle


def _build_photo(lat: float, lon: float, radius_nm: float, min_px: int,
                 providers: list[BaseLayer]) -> tuple[Image.Image | None, str | None]:
    """First provider in the cascade that returns an image wins."""
    for prov in providers:
        img = prov.get_image(lat, lon, radius_nm, min_px)
        if img is not None:
            return img, prov.attribution()
    return None, None


def prepare_waypoint_layers(plan, radius_nm: float, today: date,
                            map_base: str = "both",
                            cache_dir: Path = CACHE_ROOT) -> list[WaypointLayers | None]:
    """Fetch, annotate, and pair chart + photo for every waypoint, in route order.

    `map_base` selects which halves to build: "both", "chart", or "photo". A
    waypoint with neither image yields None so the renderer skips its page. Both
    images are resized to the OFM excerpt side and annotated with the same
    marker/route/scale, so they overlay perfectly; the aero overlay is on the
    chart only.
    """
    wps = plan.waypoints
    if not wps:
        return []

    want_chart = map_base in ("both", "chart")
    want_photo = map_base in ("both", "photo")

    ofm_cache = Path(cache_dir) / "ofm"
    cycle = _resolve_cycle(wps[0].lat, wps[0].lon, today, ofm_cache) if want_chart else ""
    chart_provider = OfmChart(cycle=cycle, cache_dir=ofm_cache) if want_chart else None
    # Photo providers are constructed only when needed — "--map-base chart" must
    # never touch the photo endpoints.
    photo_provs = photo_providers(cache_dir) if want_photo else []

    which = " + ".join([p for p, on in (("chart", want_chart), ("photo", want_photo)) if on])
    print(f"[wp-maps] building {which} layers for {len(wps)} waypoint(s)"
          + (f", AIRAC {cycle}" if want_chart else "") + "…")

    results: list[WaypointLayers | None] = []
    n = len(wps)
    for i, wp in enumerate(wps):
        side = ofm._crop_origin(wp.lat, wp.lon, radius_nm)[2]
        min_px = max(1200, side)
        prev_ll = (wps[i - 1].lat, wps[i - 1].lon) if i > 0 else None
        next_ll = (wps[i + 1].lat, wps[i + 1].lon) if i < n - 1 else None

        chart_img = chart_attr = None
        if chart_provider is not None:
            raw = chart_provider.get_image(wp.lat, wp.lon, radius_nm, min_px)
            if raw is not None:
                chart_img = ofm.annotate(_fit(raw, side), wp.lat, wp.lon, radius_nm,
                                         prev_ll, next_ll)
                chart_attr = chart_provider.attribution()

        photo_img = photo_attr = None
        if photo_provs:
            raw, attr = _build_photo(wp.lat, wp.lon, radius_nm, min_px, photo_provs)
            if raw is not None:
                photo_img = ofm.annotate(_fit(raw, side), wp.lat, wp.lon, radius_nm,
                                         prev_ll, next_ll)
                photo_attr = attr

        wl = WaypointLayers(chart_img, photo_img, chart_attr, photo_attr)
        if wl.empty():
            print(f"[wp-maps] {wp.ident}: no chart or photo coverage — skipping page",
                  file=sys.stderr)
            results.append(None)
        else:
            if want_photo and photo_img is None:
                print(f"[wp-maps] {wp.ident}: no photo coverage — chart only",
                      file=sys.stderr)
            results.append(wl)
    return results


def _fit(img: Image.Image, side: int) -> Image.Image:
    """Resize to a square of *side* px so chart and photo share the annotation
    coordinate space (ofm.annotate maps route points onto the OFM excerpt grid)."""
    if img.width == side and img.height == side:
        return img.convert("RGB")
    return img.convert("RGB").resize((side, side), Image.LANCZOS)
