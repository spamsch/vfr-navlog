# VFR Navlog app

Desktop planner and kneeboard for flying VFR without GPS — pilotage, dead reckoning and VOR radials. Built with Tauri 2, SvelteKit (static SPA), TypeScript and Leaflet.

Route resolution, leg math, VOR data and PDF export all run in the `vfr_navlog` Python package next to this folder, through its JSON bridge (`python -m vfr_navlog.api`). The app needs that checkout and its `.venv` (see the main README); set `VFR_NAVLOG_ROOT` if the app lives elsewhere.

## Run

Prerequisites: Node, Rust (rustup, stable), and on Windows the MSVC build tools and WebView2.

```
cd app
npm install
npm run tauri dev
```

`npm test` runs the radial-tracking unit tests; `npm run check` type-checks.

## What it does

**Plan**
- Paste a route (e.g. copied from Navigraph Charts), pick aircraft, cruise altitude, wind and variation, resolve. *Live* next to the wind loads the current real-world wind at cruise altitude (Open-Meteo forecast for this hour, averaged along the route, true direction) and re-resolves.
- Per-leg altitude; a warning with the semicircular-rule altitude where a leg does not comply (click to apply, or tick *Semicircular rule* to apply it automatically).
- Notes per waypoint, and landmark pins dropped on the map. Each pin is attached to its leg with left/right of track, NM and minutes from the leg start.
- VOR radials: pick a VOR (list or map) to see, per leg, the radials at both ends and whether the leg can be flown on a radial — OBS set to the leg course, needle within half scale (5°) all the way. Each leg has NAV1 and NAV2: pick a station for each, or let *auto* suggest one (NAV1: the best-aligned radial; NAV2: the nearest other station to the waypoint). A station not along the leg is set to its radial over the waypoint (FROM), so the needle centres on arrival.
- Map: openflightmaps chart, satellite imagery (online only) or no background, minute ticks, ±10° lines, VORs with radial spokes, flyable legs in green.
- *Reset to start* clears the flight log and shows the whole route.
- *Export PDF* prints notes, landmarks, VOR setting and your leg altitudes into the navlog; *Cache charts* stores the chart tiles along the route for offline use.

**Fly** (dark kneeboard)
- Top to bottom: MH, altitude and GS in huge type; NAV1 and NAV2 with a big course and the station/frequency, plus the radial and DME you should read right now at the DR position; the notes for the waypoint ahead; NAV1 and NAV2 for the next leg, to set up before the turn.
- Below: the leg, countdown to the next waypoint and ETO, *Over waypoint now*, climb/descend warnings, checkpoints with due times.
- Dead-reckoning position on the map.
- 1-in-60 helper: off-track distance → heading back to the waypoint or to parallel.
- Destination: live VATSIM frequencies and ATIS, published frequencies, ILS per runway, runways, METAR with QNH — opens by itself on the tower-call leg and refreshes every 3 minutes.

**Airport charts** (German aerodromes, ED/ET)
- After a route is resolved, the departure and destination charts are downloaded from the DFS AIP (BasicVFR visual approach charts plus the aerodrome, ground movement and parking charts from BasicIFR) into `~/.cache/vfr-navlog/charts/<ICAO>/`. The cache is re-checked at most weekly and re-downloaded only when DFS publishes a new issue; offline, the cached set is used.
- *Airport charts* in the Plan view and *Charts* in the Fly view's destination box open a viewer: airport tabs, page list, zoom 1–4× (+/−), drag to pan, ←/→ to flip pages, Esc to close.
- DFS charts are for personal flight preparation only; they stay in the local cache and are never committed.

**X-Plane kneeboard** (in-sim window, FlyWithLua NG+)
- *Send to X-Plane* writes the plan to `<X-Plane>/Output/vfr-navlog/kneeboard_plan.lua` and installs `xplane/vfr_kneeboard.lua` into FlyWithLua's Scripts folder (reload Lua scripts once after the first install). After the first send, plan edits re-send automatically.
- Same order as the Fly view: MH / altitude / GS, NAV1 and NAV2 (with *Tune* buttons that set frequency and course), notes, the next leg's NAV1 and NAV2, then timer and the rest.
- Bind `FlyWithLua/vfr_kneeboard/toggle` to a key or joystick button; `FlyWithLua/vfr_kneeboard/over_waypoint` logs "over the next waypoint now". Also under Plugins › FlyWithLua › Macros, together with *reset window position*.
- Shows the current leg with a timer on sim time (pauses with the sim), times over waypoints with revised ETAs, checkpoints, notes, climb/descend warnings, the leg's VOR with **Tune NAV1/NAV2** (sets frequency and OBS) and the radial/DME expected now, and the destination frequencies/ILS/ATIS as of the last send.
- Text is ASCII only (FlyWithLua's font has no umlauts); the in-sim flight log is separate from the app's Fly view.
- **VFR Charts** window (kneeboard *Charts* button or `FlyWithLua/vfr_kneeboard/charts_toggle`; `charts_next` / `charts_prev` for stick buttons): the departure's airport charts before and on the first leg, the destination's from the tower-call leg on. Page flipping, zoom that keeps the view centre, drag to pan, mouse wheel, *Pop out* to move it to another monitor. Pages are sent as JPEG (≤ 2048 px) and load on first view.

Plans save as `.vfrplan.json` (Ctrl+S / Ctrl+O), including the flight log; the working plan is also autosaved locally.

Charts: © openflightmaps (OFMA General Users' License), cached under `~/.cache/vfr-navlog/ofm`, shared with the PDF renderer.
