"""openflightmaps (OFM) tile fetching, stitching, and per-waypoint map excerpts.

The public OFM slippy-tile API — the same one their own site and Little Navmap
themes use — serves two layers on the standard EPSG:3857 grid, 512 px tiles:

    base : opaque JPEG ground chart (towns, roads, rivers, forests)
    aero : transparent RGBA overlay (airspace, navaids, obstacles, CTR/TMZ)

We composite aero over base. Ground detail is sharpest at z12; the aero label
set is complete at z11 and thins out above it, so the excerpt uses base z12 with
aero z11 upscaled x2 (same Mercator grid, so a factor-2 upscale aligns exactly).
Both zooms live behind BASE_ZOOM / AERO_ZOOM — drop both to 11 if the upscaled
overlay looks bad.

Everything above the network boundary is pure and unit-testable: the AIRAC
arithmetic, the Mercator tile/pixel math, the crop-pixel placement, and the
annotation drawing. Only fetch_tile and prepare_waypoint_maps touch the wire.

License: OFM data is published under the OFMA General Users' License (free use
with attribution). Every rendered map page carries the attribution line with the
AIRAC cycle actually used.
"""
from __future__ import annotations

import io
import math
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import VATSIM_UA

# --- Constants -------------------------------------------------------------

TILE_API = "https://nwy-tiles-api.prod.newaydata.com/tiles/{z}/{x}/{y}.{ext}?path={cycle}/{layer}/latest"
TILE_PX = 512
BASE_ZOOM = 12          # opaque ground JPEG, sharpest legible ground detail
AERO_ZOOM = 11          # complete aero label set; upscaled x2 onto the z12 grid
TILE_TIMEOUT = 10.0     # seconds — community server, be patient but bounded
MAX_TILE_WORKERS = 4    # polite concurrency cap for a free endpoint

# EPSG:3857 equatorial circumference (WGS84 semi-major axis).
_EARTH_CIRC_M = 2 * math.pi * 6378137.0
_NM_M = 1852.0

DEFAULT_CACHE = Path.home() / ".cache" / "vfr-navlog" / "ofm"

# AIRAC anchor: cycle 2601 became effective 2026-01-22. Cycles are 28 days apart.
_AIRAC_ANCHOR = date(2026, 1, 22)
_AIRAC_ANCHOR_CYCLE = "2601"


@dataclass
class WaypointMap:
    """A finished, annotated map excerpt for one waypoint, plus the cycle used."""
    image: Image.Image
    cycle: str


# --- AIRAC cycle arithmetic (pure) -----------------------------------------

def airac_effective(today: date) -> date:
    """The effective date of the AIRAC cycle in force on *today* (pure)."""
    k = (today - _AIRAC_ANCHOR).days // 28
    return _AIRAC_ANCHOR + timedelta(days=28 * k)


def cycle_for_effective(eff: date) -> str:
    """AIRAC identifier 'YYNN' for an effective date (pure).

    NN is the ordinal of the cycle within its effective year, counted from the
    real 28-day dates — so 13- and 14-cycle years both come out right.
    """
    year_start = date(eff.year, 1, 1)
    k = (eff - _AIRAC_ANCHOR).days // 28
    k0 = math.ceil((year_start - _AIRAC_ANCHOR).days / 28)  # first cycle of eff.year
    nn = k - k0 + 1
    return f"{eff.year % 100:02d}{nn:02d}"


def airac_cycle(today: date) -> str:
    """AIRAC cycle identifier ('2606') in force on *today* (pure)."""
    return cycle_for_effective(airac_effective(today))


def previous_cycle(today: date) -> str:
    """The cycle before the one in force on *today* (publication-lag fallback)."""
    return cycle_for_effective(airac_effective(today) - timedelta(days=28))


# --- Web Mercator tile / pixel math (pure) ---------------------------------

def deg2num(lat: float, lon: float, z: int) -> tuple[float, float]:
    """Fractional slippy-tile coordinates (x, y) for a lat/lon at zoom z."""
    n = 2.0 ** z
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def _base_px(lat: float, lon: float) -> tuple[float, float]:
    """Global pixel coordinates on the BASE_ZOOM 512 px grid."""
    x, y = deg2num(lat, lon, BASE_ZOOM)
    return x * TILE_PX, y * TILE_PX


def m_per_px(lat: float, z: int) -> float:
    """Ground resolution (metres per pixel) at latitude *lat*, zoom *z*, 512 px tiles."""
    return _EARTH_CIRC_M * math.cos(math.radians(lat)) / (2.0 ** z * TILE_PX)


def radius_px(lat: float, radius_nm: float) -> float:
    """Half-width of the excerpt in BASE_ZOOM pixels for a given nm radius."""
    return radius_nm * _NM_M / m_per_px(lat, BASE_ZOOM)


def crop_pixel(lat: float, lon: float, center_lat: float, center_lon: float,
               radius_nm: float, side_px: int) -> tuple[float, float]:
    """Pixel position of (lat, lon) inside a *side_px* crop centred on
    (center_lat, center_lon). The centre maps to ~(side_px/2, side_px/2)."""
    cx, cy = _base_px(center_lat, center_lon)
    half = radius_px(center_lat, radius_nm)
    left = round(cx - half)
    top = round(cy - half)
    px, py = _base_px(lat, lon)
    return px - left, py - top


def _crop_origin(center_lat: float, center_lon: float, radius_nm: float) -> tuple[int, int, int]:
    """(left, top, side) of the excerpt in global BASE_ZOOM pixels."""
    cx, cy = _base_px(center_lat, center_lon)
    half = radius_px(center_lat, radius_nm)
    side = int(round(2 * half))
    left = int(round(cx - half))
    top = int(round(cy - half))
    return left, top, side


# --- Tile fetch (network + disk cache) -------------------------------------

def _http_get(url: str, timeout: float = TILE_TIMEOUT) -> tuple[int, bytes | None]:
    """GET *url*. Returns (status, body). status 0 on a network/timeout error;
    body is None on any non-200. Never raises."""
    req = urllib.request.Request(url, headers={"User-Agent": VATSIM_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, TimeoutError):
        return 0, None


def fetch_tile(layer: str, z: int, x: int, y: int, cycle: str,
               cache_dir: Path = DEFAULT_CACHE, timeout: float = TILE_TIMEOUT) -> bytes | None:
    """Fetch one tile, disk cache first. Returns the encoded bytes, or None.

    Cache key: {cache_dir}/{cycle}/{layer}/{z}/{x}/{y}.{ext}. The cycle in the
    path makes stale-chart reuse impossible and lets old cycles be deleted
    wholesale. A cache hit never touches the network.
    """
    ext = "jpg" if layer == "base" else "png"
    path = Path(cache_dir) / cycle / layer / str(z) / str(x) / f"{y}.{ext}"
    if path.exists():
        return path.read_bytes()
    url = TILE_API.format(z=z, x=x, y=y, ext=ext, cycle=cycle, layer=layer)
    status, body = _http_get(url, timeout=timeout)
    if status == 200 and body:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return body
    return None


# --- Excerpt stitching -----------------------------------------------------

def _stitch(layer: str, z: int, left_px: int, top_px: int, side_px: int,
            cycle: str, cache_dir: Path, fetch) -> tuple[Image.Image, int, int, bool]:
    """Fetch and stitch the tiles of *layer*/*z* covering the pixel box
    [left_px, left_px+side_px) x [top_px, top_px+side_px).

    Returns (canvas, canvas_left_px, canvas_top_px, any_tile). The canvas is
    RGBA; missing tiles are left transparent."""
    tx0, tx1 = left_px // TILE_PX, (left_px + side_px - 1) // TILE_PX
    ty0, ty1 = top_px // TILE_PX, (top_px + side_px - 1) // TILE_PX
    cols, rows = tx1 - tx0 + 1, ty1 - ty0 + 1
    canvas = Image.new("RGBA", (cols * TILE_PX, rows * TILE_PX), (0, 0, 0, 0))

    coords = [(tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]
    with ThreadPoolExecutor(max_workers=MAX_TILE_WORKERS) as ex:
        blobs = list(ex.map(lambda c: fetch(layer, z, c[0], c[1], cycle, cache_dir=cache_dir), coords))

    any_tile = False
    for (tx, ty), blob in zip(coords, blobs):
        if not blob:
            continue
        try:
            tile = Image.open(io.BytesIO(blob)).convert("RGBA")
        except Exception:
            continue
        canvas.paste(tile, ((tx - tx0) * TILE_PX, (ty - ty0) * TILE_PX))
        any_tile = True
    return canvas, tx0 * TILE_PX, ty0 * TILE_PX, any_tile


def map_excerpt(lat: float, lon: float, radius_nm: float, cycle: str,
                cache_dir: Path = DEFAULT_CACHE, fetch=fetch_tile) -> Image.Image | None:
    """A composited, cropped OFM excerpt centred on (lat, lon), or None.

    None means no ground coverage (all base tiles missing) — out of the OFM
    region or a hard network failure. A missing aero layer is not fatal: the
    ground picture alone is worth a page.
    """
    left, top, side = _crop_origin(lat, lon, radius_nm)
    if side <= 0:
        return None

    base_canvas, base_left, base_top, base_any = _stitch(
        "base", BASE_ZOOM, left, top, side, cycle, cache_dir, fetch)
    if not base_any:
        return None
    composite = base_canvas.convert("RGBA")

    # Aero z11: one z11 pixel covers two BASE_ZOOM pixels. Work in z11 pixel
    # space, then upscale x2 onto the base grid.
    aero_left, aero_top = left // 2, top // 2
    aero_side = side // 2 + 2  # +2 px slack so the x2 result covers the crop
    aero_canvas, a_left11, a_top11, aero_any = _stitch(
        "aero", AERO_ZOOM, aero_left, aero_top, aero_side, cycle, cache_dir, fetch)
    if aero_any:
        aero_up = aero_canvas.resize(
            (aero_canvas.width * 2, aero_canvas.height * 2), Image.NEAREST)
        ox = a_left11 * 2 - base_left
        oy = a_top11 * 2 - base_top
        composite.paste(aero_up, (ox, oy), aero_up)

    crop = composite.crop((left - base_left, top - base_top,
                           left - base_left + side, top - base_top + side))
    return crop.convert("RGB")


# --- Annotation (pure pixel drawing) ---------------------------------------

def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/System/Library/Fonts/Arial Bold.ttf"):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def annotate(img: Image.Image, lat: float, lon: float, radius_nm: float,
             prev_ll: tuple[float, float] | None,
             next_ll: tuple[float, float] | None) -> Image.Image:
    """Draw the route line, centre marker, scale bar and north hint on *img*.

    All placement uses crop_pixel with the same Mercator math as the excerpt,
    so this is a pure function of its inputs and unit-testable without network.
    """
    img = img.convert("RGB")
    side = img.width
    draw = ImageDraw.Draw(img)

    def to_px(pt: tuple[float, float]) -> tuple[float, float]:
        return crop_pixel(pt[0], pt[1], lat, lon, radius_nm, side)

    ccx, ccy = side / 2.0, side / 2.0
    magenta = (208, 0, 208)
    lw = max(2, round(side / 320))
    halo = lw + 4

    # Route line: previous -> centre -> next, clipped by the raster bounds.
    segments = []
    if prev_ll is not None:
        segments.append((to_px(prev_ll), (ccx, ccy)))
    if next_ll is not None:
        segments.append(((ccx, ccy), to_px(next_ll)))
    for a, b in segments:
        draw.line([a, b], fill=(255, 255, 255), width=halo)   # white halo
    for a, b in segments:
        draw.line([a, b], fill=magenta, width=lw)

    # Centre marker: open circle + four ticks, so the feature stays visible.
    r = max(7, round(side / 60))
    for col, wd in (((255, 255, 255), 4), ((0, 0, 0), 2)):
        draw.ellipse([ccx - r, ccy - r, ccx + r, ccy + r], outline=col, width=wd)
        gap = r + 2
        tick = r + max(6, round(side / 90))
        draw.line([(ccx, ccy - gap), (ccx, ccy - tick)], fill=col, width=wd)
        draw.line([(ccx, ccy + gap), (ccx, ccy + tick)], fill=col, width=wd)
        draw.line([(ccx - gap, ccy), (ccx - tick, ccy)], fill=col, width=wd)
        draw.line([(ccx + gap, ccy), (ccx + tick, ccy)], fill=col, width=wd)

    # Scale bar: exactly 1 NM, bottom-left, on a white plate.
    nm_px = _NM_M / m_per_px(lat, BASE_ZOOM)
    m = max(10, round(side / 45))            # margin
    bh = max(4, round(side / 130))           # bar thickness
    x0, y1 = m, side - m
    x1, y0 = m + nm_px, y1 - bh
    fnt = _font(max(11, round(side / 42)))
    draw.rectangle([x0 - 3, y0 - 16, x1 + 3, y1 + 3], fill=(255, 255, 255))
    draw.rectangle([x0, y0, x1, y1], fill=(0, 0, 0))
    draw.text((x0, y0 - 15), "1 NM", fill=(0, 0, 0), font=fnt)

    # North hint, top-right (tiles are north-up by construction).
    nx, ny = side - m, m
    draw.line([(nx - 5, ny + 16), (nx - 5, ny)], fill=(0, 0, 0), width=3)
    draw.polygon([(nx - 5, ny - 4), (nx - 10, ny + 5), (nx, ny + 5)], fill=(0, 0, 0))
    draw.text((nx - 24, ny + 2), "N", fill=(0, 0, 0), font=fnt)

    return img


# --- Run-level orchestration (network) -------------------------------------

def _probe(lat: float, lon: float, cycle: str, cache_dir: Path) -> bool:
    """True if the base tile under (lat, lon) exists for *cycle*."""
    bx, by = _base_px(lat, lon)
    tx, ty = int(bx // TILE_PX), int(by // TILE_PX)
    return fetch_tile("base", BASE_ZOOM, tx, ty, cycle, cache_dir=cache_dir) is not None


def prepare_waypoint_maps(plan, radius_nm: float, today: date,
                          cache_dir: Path = DEFAULT_CACHE) -> list[WaypointMap | None]:
    """Fetch and annotate one excerpt per waypoint, in route order.

    Resolves the working AIRAC cycle once (falling back to the previous cycle on
    a publication-lag 404), then builds a page image per waypoint. Waypoints with
    no coverage yield None and a single stderr note; the PDF is never failed.
    """
    wps = plan.waypoints
    if not wps:
        return []

    cycle = airac_cycle(today)
    dep = wps[0]
    if not _probe(dep.lat, dep.lon, cycle, cache_dir):
        prev = previous_cycle(today)
        if _probe(dep.lat, dep.lon, prev, cache_dir):
            print(f"[wp-maps] AIRAC {cycle} not published yet — using {prev}", file=sys.stderr)
            cycle = prev

    print(f"[wp-maps] fetching OFM excerpts for {len(wps)} waypoint(s), AIRAC {cycle}…")
    maps: list[WaypointMap | None] = []
    for i, wp in enumerate(wps):
        img = map_excerpt(wp.lat, wp.lon, radius_nm, cycle, cache_dir=cache_dir)
        if img is None:
            print(f"[wp-maps] {wp.ident}: no OFM coverage — skipping page", file=sys.stderr)
            maps.append(None)
            continue
        prev_ll = (wps[i - 1].lat, wps[i - 1].lon) if i > 0 else None
        next_ll = (wps[i + 1].lat, wps[i + 1].lon) if i < len(wps) - 1 else None
        img = annotate(img, wp.lat, wp.lon, radius_nm, prev_ll, next_ll)
        maps.append(WaypointMap(image=img, cycle=cycle))
    return maps
