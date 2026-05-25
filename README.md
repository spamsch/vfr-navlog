# vfr-navlog

A small Python script that turns a [Little Navmap](https://www.littlenavmap.org/) `.lnmpln` flight plan into a printable A4-landscape VFR navlog PDF, German LBA-style. Built for simulator use with X-Plane 12 and VATSIM, but the output is real-world readable.

The `.lnmpln` format is Little Navmap's native flight-plan XML. You can build the plan in Little Navmap directly, or plan in **[Navigraph Charts](https://navigraph.com/products/charts)** and export to `.lnmpln` (File → Export → Little Navmap) — same file format either way, this script doesn't care which tool produced it.

The PDF is four pages:

1. **Navlog**: header, freigaben/wetter strip, frequency block, ATIS strip, leg-by-leg table with true course / magnetic heading / distance / groundspeed / ETE / fuel, fuel summary, planning assumptions.
2. **FIS phraseology**: dialogue-style bilingual (DE/EN) cheat-sheet for the Bremen Information FIS contact — Erstanruf, Vollmeldung, squawk/QNH readback, traffic info exchange, frequency departure — plus a variations table for workload denial, no radar contact, and POB query.
3. **CTR phraseology**: the full inbound sequence at the destination — tower initial call, position report with ATIS, CTR entry clearance via Whiskey, reporting-point call, downwind join, landing clearance, runway vacated, taxi to GA apron — plus a variations table for temporary holds, squawk assignments, and traffic sequencing.
4. **Destination briefing**: airport stammdaten, runway table with ILS LOC frequencies pulled from X-Plane's `earth_nav.dat`, communication frequencies recap, and the live VATSIM ATIS text if a controller is broadcasting.

It also fetches **live VATSIM controller frequencies** for the departure and destination airports, marks the tower-call leg, and can write an **X-Plane FMS flight plan** directly into X-Plane's `Output/FMS plans/` folder.

![placeholder — drop a screenshot here once you've generated one](docs/screenshot-page1.png)

## What it does

- Parses an `.lnmpln` (XML) plan exported from Little Navmap, or reads the active plan directly from **Navigraph Charts** (macOS only).
- Computes per-leg true course, distance, wind-corrected magnetic heading, groundspeed, time, and fuel from a JSON aircraft profile and a single wind aloft.
- Renders an LBA-style navlog using [fpdf2](https://github.com/py-pdf/fpdf2) — no external server, single PDF on disk.
- Optionally queries VATSIM's public data feed to populate the Tower / Ground / ATIS rows and the tower-call marker with live frequencies.
- Writes an X-Plane FMS v3 flight plan into `Output/FMS plans/` so you can load the route in the sim before your VATSIM session.
- Highlights the columns you actually scan in cruise (TC, MH, Dist, GS, ETE) in bold/larger type.

## Install

Python 3.10+ and [fpdf2](https://pypi.org/project/fpdf2/):

```
pip install fpdf2
```

On macOS the script registers `Arial.ttf` from `/System/Library/Fonts/Supplemental/` so umlauts render. On other platforms it falls back to core Helvetica — add a Unicode TTF to `FONT_CANDIDATES` at the top of `navlog.py` if you need it.

## Interactive mode

Run the script with no arguments for a step-by-step wizard:

```
python3 navlog.py
```

It will ask for:

1. Plan source — Little Navmap `.lnmpln` file or Navigraph Charts (live read, macOS only)
2. Aircraft JSON
3. Aircraft registration (defaults to the value in the JSON, overridable)
4. Wind aloft — type `DDD/SS` or press `M` to fetch the surface METAR from VATSIM
5. Cruise altitude in ft MSL (defaults to whatever the plan file says)
6. Magnetic variation
7. Whether to pull live VATSIM ATC frequencies
8. Whether to write an X-Plane FMS file

Tab-completion works for file paths.

## CLI

```
python3 navlog.py \
    --plan "VFR Bielefeld to Muenster.lnmpln" \
    --aircraft aircraft_c172.json \
    --wind 270/15 \
    --magvar 4E \
    --cruise-alt 2500 \
    --vatsim \
    --fms \
    --output navlog.pdf
```

### Flags

| Flag | Default | Notes |
|------|---------|-------|
| `--plan` | — | Little Navmap `.lnmpln` file. Mutually exclusive with `--navigraph`. |
| `--navigraph` | off | Read the active plan from Navigraph Charts (macOS). |
| `--aircraft` | required | JSON profile (see `aircraft_c172.json`). |
| `--registration` | from JSON | Override the aircraft registration for this run (e.g. `D-EXXX`). |
| `--wind` | `0/0` | Wind aloft `DDD/SS`, e.g. `270/15`. Applied uniformly to every leg. |
| `--cruise-alt` | from plan | Override the cruise altitude from the flight plan (feet MSL). |
| `--magvar` | `2.5E` | Magnetic variation. `4E`, `-3.5`, `2.5W` all work. |
| `--output` | auto | Output path. Defaults to `<dep>-<dest>/navlog_<date>_<type>.pdf` next to the script. |
| `--vatsim` | off | Fetch live ATC frequencies from VATSIM. One HTTPS request, fails soft. |
| `--fms` | off | Write an X-Plane FMS v3 flight plan to `Output/FMS plans/<dep>-<dest>.fms` under the X-Plane root. Falls back to the PDF's directory if `--xplane` is unset. |
| `--call-tower-nm` | `10` | Distance threshold (NM remaining) at which to flag the tower-call leg. `0` disables. |
| `--xplane` | macOS Steam default | Path to X-Plane 12 root. Used for `apt.dat`, `earth_nav.dat`, and FMS output. Pass `--xplane ""` to skip the destination-briefing page. |

## Aircraft profile

Edit `aircraft_c172.json` or write your own. Fields:

- `type`, `registration` — printed in the header and templated into the phraseology pages. The `--registration` flag or the TUI prompt overrides `registration` for a single run without editing the file.
- `performance.tas_cruise` — kt, used for navlog math.
- `performance.fuel_burn_cruise_lph`, `fuel_burn_climb_lph`, `fuel_burn_taxi_lph` — for the fuel summary.
- `fuel.capacity_usable_l`, `reserve_minutes`, `taxi_minutes`, `approach_minutes`, `alternate_minutes` — fuel block buckets.
- `mass_balance.*` — currently informational only; the script does not render a W&B page yet.

The bundled `aircraft_c172.json` uses Cessna 172S POH-typical planning numbers at 65% power. **Verify against your aircraft's POH before flying.**

## Phraseology pages

Two dedicated pages, both templated to the registration, aircraft type, departure, and destination from the plan.

**Page 2 — FIS Bremen Information.** The complete dialogue from first call to frequency departure. Rows are color-coded: pale blue for pilot transmissions, amber for FIS. Sections A and B cover the Erstanruf → Vollmeldung → squawk/QNH assignment → readback → optional traffic-info exchange → frequency departure. The variations table at the bottom covers the three situations that catch pilots off guard: workload denial (the "Squawk 7000, goodbye" response), no radar contact, and a POB query.

**Page 3 — CTR entry via Whiskey.** Everything from the initial tower call to parking at the GA apron. Section C is the clearance sequence: short call, full report with ATIS letter, entry clearance via Whiskey, readback, "report Whiskey" assignment. Section D continues from the Whiskey call through downwind, landing clearance, vacating the runway, taxi to GA, and on-stand. The variations table covers a temporary hold outside the CTR, a squawk assignment mid-sequence, and traffic sequencing all the way through to "cleared to land."

The FIS callsign is hardcoded to *Bremen Information*. For southern Germany change it to *Langen Information* or *München Information* in `render_phraseology`. The reporting-point name "Whiskey" is EDDG-specific — substitute the correct VRP name for any other destination.

## X-Plane FMS export

With `--fms` (or answering Y in the TUI), the script writes a version-3 FMS file to:

```
<xplane-root>/Output/FMS plans/<DEP>-<DEST>.fms
```

Type codes follow the X-Plane convention: `1` = airport, `3` = VOR, `2` = NDB, `11` = intersection, `28` = user waypoint. Cruise altitude is written on en-route waypoints; departure and destination get altitude `0`. Load it in X-Plane's FMS or the G1000/G530 avionics before you connect to VATSIM.

## How the tower-call marker works

The script walks legs forward from departure. The first leg whose **end** sits inside the `--call-tower-nm` threshold is the call leg. The marker is anchored at the **start** of that leg — the waypoint the pilot is leaving — so the cue appears early enough to dial the radio before the next checkpoint.

If VATSIM has the destination Tower online, the marker shows the live frequency: `→ TWR 129.805`. If only Approach is up, it falls back to APP. If neither, you get `→ EDDG TWR rufen`. Set `--call-tower-nm 0` to disable entirely.

## Destination briefing page

Page 4 reads from X-Plane's local nav data — no internet, no scraping:

- `apt.dat` from `Global Scenery/Global Airports/Earth nav data/` — elevation, transition altitude/level, IATA code, runway endpoints/width/surface. Runway length is the great-circle distance between the two endpoints.
- `earth_nav.dat` from `Custom Data/` (Navigraph) or `Resources/default data/` (Laminar) — ILS LOC entries (type 4/5) joined to runways.

The "Frequenzen & ATIS" block layers VATSIM data on top: live frequencies plus the verbatim ATIS text. If `--xplane` points somewhere without the expected files, the page degrades gracefully — comm frequencies and ATIS still appear, runway/ILS sections show a "data not found" note.

## VATSIM data

Single GET against `https://data.vatsim.net/v3/vatsim-data.json`. Callsigns of the form `<ICAO>_GND`, `<ICAO>_TWR`, `<ICAO>_ATIS`, `<ICAO>_DEL`, `<ICAO>_APP` are matched; split sectors (`EDDM_N_TWR`) are handled by suffix. If the request fails, the script emits a stderr note and continues with blank frequency cells.

## Limitations / known scope

- Single wind aloft applied uniformly. No per-leg wind, no wind gradient, no wind aloft forecast parsing.
- No mass & balance or takeoff/landing distance computation. Stations are in the JSON for future expansion.
- Magnetic variation is a single constant. Fine for Germany; less accurate across large magvar gradients.
- No MORA lookup.

PRs welcome.

## License

MIT. See `LICENSE`.
