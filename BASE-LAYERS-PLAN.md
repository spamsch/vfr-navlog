# Feature plan: pluggable base layers — chart and photo side by side

Status: proposal, 2026-07-04. Extends the per-waypoint briefing pages from
`WAYPOINT-MAP-PAGES-PLAN.md` (implemented). Endpoints marked "verified" were
tested live on 2026-07-04.

## Why

The OFM chart excerpt answers "what airspace and navaids surround this
waypoint." It does not answer "what does this place look like out the
window." An orthophoto does. The best kneeboard page shows both, side by
side: chart for context, photo for recognition.

## Research result: the DFS ICAO chart is NOT an option

Question investigated: doesn't DFS publish the official ICAO 1:500,000 chart
online? Findings, so nobody re-researches this:

- DFS lets registered users *view* the ICAO chart for free in its AIS portal
  (secais.dfs.de, VFReBulletin/WebAUP map viewer). That is a login-protected
  web application — no public tile or WMS endpoint, and scraping a
  credentialed viewer is out of the question for this tool.
- The DFS public geodata offering (dfs.de → Services → Geo data, served via
  haleconnect.com) is WMS/WFS of the *air transport network vectors* —
  navaids, routes, airspace polygons, airports. Useful data, but not the
  ICAO raster chart rendering, and turning vectors into a chart is a
  cartography project, not a feature.
- The chart itself is a commercial product (Eisenschmidt, paper + eVFR).

Consequence: openflightmaps stays the chart layer. Its aero rendering is
deliberately ICAO-chart-styled and is the closest legitimately usable public
equivalent.

## Photo sources (verified)

Priority cascade, best first:

1. **State orthophotos (DOP20), 20 cm, via WMS** — the literal out-the-window
   view; buildings, sports fields, gravel pits all recognizable.
   - Niedersachsen (verified live, no key):
     `https://opendata.lgln.niedersachsen.de/doorman/noauth/dop_wms`
     `REQUEST=GetMap&VERSION=1.3.0&LAYERS=ni_dop20&CRS=EPSG:3857&BBOX=...&WIDTH=...&HEIGHT=...&FORMAT=image/jpeg`
     — a WMS GetMap returns the exact bbox in one request, no tile
     stitching. License: CC BY 4.0 (implementer: confirm exact attribution
     string from GetCapabilities and use it verbatim).
   - Nordrhein-Westfalen: `https://www.wms.nrw.de/geobasis/wms_nw_dop`
     (layer `nw_dop_rgb`; implementer must verify layer name, CRS support,
     and license string via GetCapabilities — NRW open data, dl-de license
     family). Not yet live-tested.
   - Other Bundesländer: out of scope v1; the provider table makes adding
     one a ~5-line entry.
2. **Sentinel-2 cloudless (EOX)** — Europe-wide fallback, 10 m/px, verified
   live, no key: WMTS
   `https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2024_3857/default/g/{z}/{y}/{x}.jpg`
   (256 px tiles, EPSG:3857 — same slippy math as OFM, different tile size).
   License CC BY-NC-SA 4.0 — fine for this personal tool; attribution
   REQUIRED verbatim: "Sentinel-2 cloudless - https://s2maps.eu by EOX IT
   Services GmbH (Contains modified Copernicus Sentinel data 2024)".

Also available but NOT used as photo: BKG TopPlusOpen (verified live,
`https://sgx.geodatenzentrum.de/wmts_topplus_open/tile/1.0.0/web/default/WEBMERCATOR/{z}/{y}/{x}.png`,
256 px, open data, © BKG) — a *cartographic* alternative, more ground detail
than the OFM base. It slots into the same provider protocol as an optional
third base (`topo`); implement only if it costs nothing extra.

## Design

### Provider protocol, `vfr_navlog/baselayers.py`

```python
class BaseLayer(Protocol):
    name: str                    # "chart" | "photo-dop" | "photo-s2" | "topo"
    def attribution(self) -> str  # exact line for the page footer
    def get_image(self, lat, lon, radius_nm, min_px) -> PIL.Image | None
```

Implementations:

- `OfmChart` — wraps the existing `ofm.map_excerpt` composite (base z12 +
  aero z11 ×2). Refactor, don't duplicate: `ofm.py` keeps the tile logic,
  this class is an adapter.
- `DopWms` — one class, instantiated per state from a small table:
  `(name, endpoint, layer, license_line, coverage_bbox)`. Coverage is a
  rough lat/lon rectangle per state — enough to route requests; a waypoint
  in neither rectangle skips DOP. Requests `image/jpeg`, EPSG:3857, computed
  bbox, WIDTH/HEIGHT ≥ min_px (use ~1500 px for a 3 nm radius → ~7 m/px —
  well below DOP native resolution but sharp far beyond print needs, and a
  reasonable JPEG size per page). Blank-white responses (WMS answering
  outside actual coverage) must be detected (near-uniform image → treat as
  None).
- `Sentinel2` — slippy stitcher at z13/z14 (256 px tiles; reuse the mercator
  math from `ofm.py`, parameterized by tile size). Fallback when DOP has no
  coverage or fails.

Photo cascade per waypoint: DOP (by coverage) → Sentinel-2 → None.

Caching: extend the existing disk cache; key by provider
(`~/.cache/vfr-navlog/{provider}/...`). DOP GetMap responses keyed by
rounded bbox + size; S2 tiles by layer-year/z/x/y; OFM unchanged (by AIRAC
cycle). DOP/S2 imagery is not AIRAC-bound — cap cache age at ~1 year via a
mtime check rather than a cycle key.

### Page layout: side by side

The waypoint page (currently: VOR block left ~60 mm, one ~185 mm map right)
becomes:

- Header strip: unchanged.
- **VOR fix strip**: moves from a left column to a full-width horizontal
  band under the header (the fix lines are short; render them side by side,
  large). Frees the full width for maps.
- **Two maps side by side, ~115×115 mm each**: left = chart (OFM composite),
  right = photo. Same center, same radius, same scale — the eye can jump
  between them. Marker, route line, and 1 nm scale bar drawn on BOTH
  (`ofm.annotate` already does this; reuse as-is on the photo image). The
  aero overlay goes on the chart ONLY — the photo stays clean; that
  cleanliness is its entire value.
- Per-map attribution caption directly under each map (chart: OFM line with
  AIRAC cycle; photo: the provider's exact license line).
- Degrade: photo unavailable → chart-only page in the current full-width
  layout (no half-empty page). Chart unavailable but photo present →
  photo-only, same treatment. Neither → skip page (existing rule).

### Wiring

- `--map-base both|chart|photo` (default `both`), only meaningful with
  `--wp-maps`. TUI: extend the existing wp-maps prompt with the base choice,
  default `both`.
- `RunConfig.map_base`; image preparation stays in `cli.run()`'s fetch stage
  (now up to two images per waypoint, still under the existing
  ThreadPoolExecutor; keep tile-courtesy cap at 4 concurrent per host).
- `RenderContext`: per-waypoint `(chart_img | None, photo_img | None,
  photo_attribution | None)` instead of the single image.

## What this does NOT do

- No DFS ICAO raster (see research above), no scraping of the AIS portal.
- No aero overlay on the photo, no photo brightness "enhancement".
- No per-state DOP rollout beyond NI + NRW in v1.
- No change to any page outside the waypoint pages; `--wp-maps` off remains
  byte-identical output.

## Testing

- Provider selection: coverage-rectangle routing (NI point → DOP-NI, NRW
  point → DOP-NRW, Denmark point → Sentinel-2), cascade on provider failure,
  blank-white DOP detection (uniform fixture image → None → cascade).
- WMS bbox math: lat/lon + radius → EPSG:3857 bbox, hand-computed values.
- Sentinel-2 stitcher: 256 px fixture tiles, marker centered (reuses the
  OFM fixture pattern).
- Layout: stubbed providers → pypdf asserts both attributions present on a
  both-page, chart-only fallback renders full-width, `--map-base chart`
  never calls the photo provider.
- Existing suite untouched and green; PDF snapshot (flag off) byte-identical.
- Manual QA artifact: real-network sample PDF, same route as the wp-maps
  sample, `--map-base both` — reviewer eyeballs chart/photo alignment (same
  landmark at the same relative position in both maps).

## Sequencing

Build on top of the merged waypoint-pages feature. Roughly four commits:
provider protocol + DOP/S2 providers with tests; ofm adapter + cascade +
cache; page layout rework; wiring + docs. The one real risk is WMS endpoint
drift (state services get reorganized) — hence GetCapabilities verification
at implementation time and per-provider graceful failure forever after.
