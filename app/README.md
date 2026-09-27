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
- Paste a route (e.g. copied from Navigraph Charts), pick aircraft, cruise altitude, wind and variation, resolve.
- Per-leg altitude; a warning with the semicircular-rule altitude where a leg does not comply (click to apply, or tick *Semicircular rule* to apply it automatically).
- Notes per waypoint, and landmark pins dropped on the map. Each pin is attached to its leg with left/right of track, NM and minutes from the leg start.
- VOR radials: pick a VOR (list or map) to see, per leg, the radials at both ends and whether the leg can be flown on a radial — OBS set to the leg course, needle within half scale (5°) all the way. Assign a VOR per leg, or let *auto* suggest the best-aligned one.
- Map: openflightmaps chart, minute ticks, ±10° lines, VORs with radial spokes, flyable legs in green.
- *Reset to start* clears the flight log and shows the whole route.
- *Export PDF* prints notes, landmarks, VOR setting and your leg altitudes into the navlog; *Cache charts* stores the chart tiles along the route for offline use.

**Fly** (dark kneeboard)
- Current leg in large type: MH, altitude, GS, distance, countdown to the next waypoint, ETO.
- *Over waypoint now* logs the time; ETAs are revised from the last leg's actual time.
- Dead-reckoning position on the map; checkpoints with due times; climb/descend warnings.
- NAV box: frequency, OBS and TO/FROM, plus the radial and DME you should read right now at the DR position.
- 1-in-60 helper: off-track distance → heading back to the waypoint or to parallel.
- Destination: live VATSIM frequencies and ATIS, published frequencies, ILS per runway, runways, METAR with QNH — opens by itself on the tower-call leg and refreshes every 3 minutes.

**X-Plane kneeboard** (in-sim window, FlyWithLua NG+)
- *Send to X-Plane* writes the plan to `<X-Plane>/Output/vfr-navlog/kneeboard_plan.lua` and installs `xplane/vfr_kneeboard.lua` into FlyWithLua's Scripts folder (reload Lua scripts once after the first install). After the first send, plan edits re-send automatically.
- Bind `FlyWithLua/vfr_kneeboard/toggle` to a key or joystick button; `FlyWithLua/vfr_kneeboard/over_waypoint` logs "over the next waypoint now". Also under Plugins › FlyWithLua › Macros, together with *reset window position*.
- Shows the current leg with a timer on sim time (pauses with the sim), times over waypoints with revised ETAs, checkpoints, notes, climb/descend warnings, the leg's VOR with **Tune NAV1/NAV2** (sets frequency and OBS) and the radial/DME expected now, and the destination frequencies/ILS/ATIS as of the last send.
- Text is ASCII only (FlyWithLua's font has no umlauts); the in-sim flight log is separate from the app's Fly view.

Plans save as `.vfrplan.json` (Ctrl+S / Ctrl+O), including the flight log; the working plan is also autosaved locally.

Charts: © openflightmaps (OFMA General Users' License), cached under `~/.cache/vfr-navlog/ofm`, shared with the PDF renderer.
