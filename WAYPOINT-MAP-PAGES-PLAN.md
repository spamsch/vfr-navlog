# Feature plan: per-waypoint briefing pages with openflightmaps excerpts

Status: proposal, 2026-07-04. Companion to `VOR-FIXES-PLAN.md` (implemented)
and `REFACTORING.md` (implemented). Facts below marked "verified" were tested
against the live tile server on 2026-07-04.

## The problem

Radial fixes tell you *when* you cross a waypoint; they don't help you
*recognize* it. For that you want the picture: the highway junction, the
river bend, the town edge, the mast — with the surrounding airspace. Today
the navlog has no per-waypoint visual at all. The goal: one A4-landscape page
per waypoint with a detailed chart excerpt (2–4 nm radius, waypoint centered
and marked, route line drawn through) plus that waypoint's VOR fix data, in
flight order, right after the nav table.

## Data source (verified)

openflightmaps (OFM) publishes AIRAC-updated VFR charts. Alongside the
GeoTIFF/MBTiles downloads on openflightmaps.org/downloads, there is a public
slippy-tile API — the same one their own site and Little Navmap themes use:

```
https://nwy-tiles-api.prod.newaydata.com/tiles/{z}/{x}/{y}.png?path={cycle}/aero/latest
https://nwy-tiles-api.prod.newaydata.com/tiles/{z}/{x}/{y}.jpg?path={cycle}/base/latest
```

Verified 2026-07-04 with cycle `2606` near Hannover (z11 x1078 y672):

- Tiles are **512×512**, EPSG:3857 (standard slippy scheme).
- **`aero` is a transparent RGBA overlay**: pink airspace tint, navaid boxes
  (ident, frequency, Morse), obstacles with elevations, CTR/TMZ boundaries.
  No ground detail. It must be composited over `base`.
- **`base` is an opaque JPEG ground chart**: towns (yellow), roads, railways,
  rivers, forests — ICAO-chart drawing style, ideal for visual recognition.
- **Zoom levels**: z11 carries the full aero label set (verified: complete
  NDB info box "HW 358 HANNOVER" with Morse). z12 still draws airspace lines
  and obstacles but drops most labels. z13 is effectively blank (1.2 KB
  gray+alpha tile). So z11 is the canonical max-detail aero rendering; z12
  base is available for sharper ground imagery.
- `{cycle}` is the AIRAC cycle (e.g. `2606`). 13 cycles/year, 28 days,
  anchor: 2601 became effective 2026-01-22.

**License**: OFM data is published under the OFMA General Users' License —
free use with attribution ("similar idea to OSM", their FAQ). Consequences:
every generated map page carries an attribution line
(`© open flightmaps — OFMA General Users' License — AIRAC {cycle}`), and this
stays a personal, non-redistributed tool. Coverage is regional (Germany plus
a set of European regions), not worldwide — out-of-coverage waypoints must
degrade gracefully.

## Geometry and resolution math

At latitude φ, a 512 px tile at zoom z spans `40075017·cos φ / 2^z` meters;
per-pixel resolution is that /512. Around lat 52 (northern Germany):

| zoom | m/px | 3 nm radius (11.1 km diameter) in px |
|---|---|---|
| 11 | ~23.3 | ~477 px |
| 12 | ~11.6 | ~955 px |

477 px stretched to a ~13 cm map is ~93 dpi — legible but soft in print.
Therefore the composite strategy:

**Base at z12, aero at z11 upscaled ×2.** Both are the same Mercator grid, so
a factor-2 upscale aligns exactly (z11 pixel (x,y) → z12 pixels (2x,2y)).
Result: ground detail at ~187 dpi, aero overlay slightly soft but complete
(all labels present). Implement behind a single constant
(`AERO_ZOOM = 11`, `BASE_ZOOM = 12`); if the upscaled overlay looks bad in
the review PDF, dropping both to z11 is a one-line change.

Tile fetch count: a 3 nm radius square at z12 spans ≤ 3×3 tiles, plus ≤ 2×2
at z11 for aero → ≤ 13 tiles per waypoint, minus cache hits (adjacent
waypoints share tiles; a straight route reuses heavily).

## Design

### New module `vfr_navlog/ofm.py`

All tile logic in one place:

1. `airac_cycle(today) -> str` — pure. 28-day arithmetic from the 2026-01-22
   anchor of cycle 2601 (works backward and forward across year boundaries).
2. `fetch_tile(layer, z, x, y, cycle) -> bytes | None` — via `net.fetch`
   semantics (10 s timeout, UA, None on failure). **Disk cache** keyed
   `{cache_dir}/{cycle}/{layer}/{z}/{x}/{y}.{ext}` under
   `~/.cache/vfr-navlog/ofm/`; the cycle key makes stale-chart reuse
   impossible and lets old cycles be deleted wholesale. On HTTP 404 for the
   computed cycle, retry once with the previous cycle (publication lag),
   remember the working cycle for the run.
3. `map_excerpt(lat, lon, radius_nm) -> PIL.Image | None` — computes the z12
   pixel box centered on (lat, lon), fetches the covering base tiles (z12)
   and aero tiles (z11), stitches, upscales aero ×2, alpha-composites, crops
   to the requested square. Pillow is already a transitive dependency
   (fpdf2 requires it); promote it to a declared direct dependency.
4. `annotate(img, ...) -> PIL.Image` — draws on the composite:
   - center marker: open circle + crosshair (open so the feature itself
     stays visible),
   - the route: line from previous waypoint through center to next waypoint,
     clipped to the image, distinct color (magenta, the classic course-line
     color; verify it doesn't vanish against the pink CTR tint — fall back
     to black outline halo),
   - a 1 nm scale bar, bottom-left, and a "N↑" hint top-right (tiles are
     north-up by construction).
   All drawing in pixel space with the same Mercator math as the crop —
   pure functions, unit-testable without network.
5. Concurrency: fetch a page's tiles with `ThreadPoolExecutor(max_workers=4)`
   — polite to a free community server; the disk cache does the real work
   from the second run on.

### New page renderer `vfr_navlog/pdf/waypoint_pages.py`

One page per waypoint, in route order, inserted **directly after the nav
table page(s)** (kneeboard order: table → waypoint pages → destination →
weather → phraseology → DFS charts). Layout per page:

- **Header strip**: `WP 3/7  ·  OSN  Osnabrück`, altitude at this waypoint,
  inbound MH/dist/ETE from the previous leg, outbound MH to the next — the
  numbers you want next to the picture, duplicated from the table on purpose.
- **Left column (~60 mm)**: the VOR fix block — the waypoint's `RadialFix`
  entries rendered large (ident, freq, radial, DME distance, Morse), the
  manual `vor_info` text if set, and the overhead case verbatim from the nav
  table cell. Below it: lat/lon in degrees-minutes (for the GPS cross-check).
- **Right (~185×185 mm)**: the annotated map excerpt, `--map-radius-nm`
  radius (default 3, accepted range 1–5, i.e. the requested 2–4 plus margin).
- **Footer**: the OFM attribution line with the AIRAC cycle actually used.

Departure and destination airports get pages too (the departure-area picture
is the initial-orientation aid; the destination page complements the DFS
charts, which are aerodrome plates, not area charts).

### Wiring

- `RunConfig`: `wp_maps: bool` (default False), `map_radius_nm: float`.
- CLI: `--wp-maps`, `--map-radius-nm N` (only meaningful with `--wp-maps`).
- TUI: one yes/no prompt next to the existing DFS-charts question, radius
  prompt with default 3.
- `RenderContext`: carries the prepared per-waypoint images (or None per
  waypoint) — fetching happens in `cli.run()`'s existing fetch stage
  alongside the other network work, NOT inside the PDF renderer.
- Degrade rules: no network / tile fetch fails / out of OFM coverage
  (aero tile blank AND base 404) → skip that waypoint's page, one stderr
  note per run, never fail the PDF. A blank-aero-but-good-base page is
  still rendered (ground picture alone is worth it).

### What this does NOT do

- No GeoTIFF/MBTiles download pipeline. The regional GeoTIFF zips are
  hundreds of MB per cycle and would need re-downloading every 28 days; the
  tile API with a disk cache fetches only the ~dozen tiles per waypoint
  actually needed. If OFM ever retires the tile endpoint, MBTiles is the
  fallback design.
- No terrain/hillshade layer, no route corridor pages, no multi-scale
  insets. One scale, one page, per waypoint.
- No offline guarantee on first run — this feature is network-bound by
  nature; the cache makes reruns offline-capable for a cycle.

## Testing

- Pure math, no network: tile x/y from lat/lon (known values), pixel
  position of a lat/lon within a stitched image, `airac_cycle` across year
  boundary and exactly on a cycle date, scale-bar length math.
- `map_excerpt`/`annotate` against tiny fixture tiles (solid-color 512×512
  PNGs/JPEGs in `tests/fixtures/ofm/`) with `fetch_tile` monkeypatched:
  assert crop size, marker at center pixel, route line endpoints.
- Cache behavior: second call does not invoke the (stubbed) HTTP layer.
- PDF: flag off → existing snapshot byte-identical. Flag on with stubbed
  tiles → pypdf text contains the header strip, VOR block, and attribution.
- One manual visual QA artifact (not a test): generate a real-network sample
  PDF for a short real-coordinate route and eyeball marker placement against
  a known landmark. The reviewer does this before accepting.

## Sequencing

Single feature branch of work on top of current main, roughly four commits:
ofm.py + math tests; page renderer; CLI/TUI/RenderContext wiring + degrade
paths; docs (README section incl. license note). Estimated ~350 lines of
logic plus tests. The PDF snapshot needs no re-baseline (flag off changes
nothing).

Risks, honestly: the tile endpoint is community infrastructure with no SLA —
hence cache + graceful skip, never a hard dependency; and the ×2 aero
upscale is a taste call that the review PDF either confirms or reverts to
all-z11.
