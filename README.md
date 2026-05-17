# vfr-navlog

A small Python script that turns a [Little Navmap](https://www.littlenavmap.org/) `.lnmpln` flight plan into a printable A4-landscape VFR navlog PDF, German LBA-style. Built for simulator use with X-Plane 12 and VATSIM, but the output is real-world readable.

The `.lnmpln` format is Little Navmap's native flight-plan XML. You can build the plan in Little Navmap directly, or plan in **[Navigraph Charts](https://navigraph.com/products/charts)** and export to `.lnmpln` (File → Export → Little Navmap) — same file format either way, this script doesn't care which tool produced it.

The PDF is three pages:

1. **Navlog**: header, freigaben/wetter strip, frequency block, ATIS strip, leg-by-leg table with true course / magnetic heading / distance / groundspeed / ETE / fuel, fuel summary, planning assumptions.
2. **Phraseology**: bilingual (German / English) VFR radio calls templated to your flight — UNICOM departure, Bremen Information FIS contact, destination tower inbound, reporting points, landing readback, Mayday — plus a "what you'll hear from the destination tower" reference table.
3. **Destination briefing**: airport stammdaten, runway table with ILS LOC frequencies pulled from X-Plane's `earth_nav.dat`, communication frequencies recap, and the live VATSIM ATIS text if a controller is broadcasting.

It also fetches **live VATSIM controller frequencies** for the departure and destination airports, and marks the leg where you should call the destination tower based on a configurable distance threshold.

![placeholder — drop a screenshot here once you've generated one](docs/screenshot-page1.png)

## What it does

- Parses an `.lnmpln` (XML) plan exported from Little Navmap.
- Computes per-leg true course, distance, wind-corrected magnetic heading, groundspeed, time, and fuel from a JSON aircraft profile and a single wind aloft.
- Renders an LBA-style navlog using [fpdf2](https://github.com/py-pdf/fpdf2) — no external server, single PDF on disk.
- Optionally queries VATSIM's public data feed to populate the Tower / Ground / ATIS rows and the tower-call marker with live frequencies. Falls back to `→ <ICAO> TWR rufen` if no controller is online.
- Emits a UNICOM 122.800 MHz call-out in case the destination is uncontrolled.
- Highlights the columns you actually scan in cruise (TC, MH, Dist, GS, ETE) in bold/larger type. Planning constants (TAS, Wind, Var) recede.
- Adds a phraseology cheat-sheet as page 2 with the registration, aircraft type, and live tower frequency baked into the calls.

## Install

Python 3.10+ and [fpdf2](https://pypi.org/project/fpdf2/):

```
pip install fpdf2
```

On macOS the script will register `Arial.ttf` from `/System/Library/Fonts/Supplemental/` so umlauts and em-dashes render. On other platforms it falls back to core Helvetica (Latin-1 only) — drop a Unicode TTF into the `FONT_CANDIDATES` list at the top of `navlog.py` if you need it.

## Quick start

```
python3 navlog.py \
    --plan "VFR Bielefeld to Muenster.lnmpln" \
    --aircraft aircraft_sr22.json \
    --wind 270/15 \
    --magvar 4E \
    --vatsim \
    --output navlog.pdf
```

Opens at `navlog.pdf`. Two pages.

## CLI flags

| Flag | Default | Notes |
|------|---------|-------|
| `--plan` | required | Little Navmap `.lnmpln` file. |
| `--aircraft` | required | JSON profile (see `aircraft_sr22.json`). |
| `--wind` | `0/0` | Wind aloft `DDD/SS`, e.g. `270/15`. Applied uniformly to every leg. |
| `--magvar` | `2.5E` | Magnetic variation. `4E`, `-3.5`, `2.5W` all work. |
| `--output` | `navlog.pdf` | Output file. |
| `--vatsim` | off | Fetch live ATC frequencies from VATSIM. One HTTPS request, fails soft. |
| `--call-tower-nm` | `10` | Distance threshold (NM remaining) at which to flag the tower-call leg. `0` disables. |
| `--xplane` | macOS Steam default | Path to X-Plane 12 root. Used to read `apt.dat` (runway lengths/surface) and `earth_nav.dat` (ILS LOC frequencies) for the destination briefing page. Pass `--xplane ""` to skip the page. |

## Aircraft profile

Edit `aircraft_sr22.json` or write your own. Fields:

- `type`, `registration` — printed in the header and templated into the phraseology page.
- `performance.tas_cruise` — kt, used for navlog math.
- `performance.fuel_burn_cruise_lph`, `fuel_burn_climb_lph`, `fuel_burn_taxi_lph` — for the fuel summary.
- `fuel.capacity_usable_l`, `reserve_minutes`, `taxi_minutes`, `approach_minutes`, `alternate_minutes` — fuel block buckets.
- `mass_balance.*` — currently informational only; the script does not render a W&B page (yet).

The bundled SR22 file uses Cirrus G3 NA POH-typical planning numbers. **Verify against your aircraft's POH** before flying.

## How the tower-call marker works

The script walks legs from departure forward. The first leg whose **end** sits inside the `--call-tower-nm` threshold is the leg the call should happen on. The marker is anchored on the **start** of that leg — the waypoint the pilot is leaving — so the cue lights up early enough to dial the radio and rehearse the call before the next checkpoint.

If VATSIM has the destination Tower online, the marker prints the live frequency: `→ TWR 129.805`. If only Approach is up, it falls back to APP. If neither, you get the prompt without a frequency: `→ EDDG TWR rufen`.

Set `--call-tower-nm 0` to disable entirely (e.g., for uncontrolled destinations where the UNICOM bar is the relevant cue).

## Phraseology page

Templated for **VFR in Germany**, specifically the Bremen FIR. The FIS callsign is hardcoded to *Bremen Information* — for southern Germany flights, change it to *Langen Information* or *München Information* at the top of `render_phraseology`.

Notes on the German VFR phraseology I checked against:

- **POB** ("Personen an Bord") goes into the FIS position report, not the tower call. The destination tower will ask separately if it wants it — the "What you may hear from the Tower" table includes that case.
- **Landing readback** is just runway designator + callsign per ICAO Annex 10. Wind is informational and not required in the readback.
- **Reporting points** ("Pflichtmeldepunkte") are airport-specific — substitute the actual point name (Whiskey, Sierra etc.) when you fly.

## Destination briefing page

Page 3 reads from X-Plane's local nav data — no internet, no scraping. The script looks at:

- `apt.dat` from `Global Scenery/Global Airports/Earth nav data/` (bundled with X-Plane 12) — airport elevation, transition altitude/level, IATA code, runway endpoints/width/surface. Runway length is great-circle distance between the two endpoints.
- `earth_nav.dat` from `Custom Data/` (Navigraph) or, if absent, `Resources/default data/` (Laminar) — ILS LOC entries (type 4 / 5) joined to runways. CAT-I / CAT-III / LOC-only are passed through verbatim.

The "Frequenzen & ATIS" block then layers VATSIM data on top: live frequencies for the controllers on duty + the verbatim **ATIS text** from the controller's broadcast. The ATIS text is genuinely useful — it tells you the runway in use, current QNH, ATIS letter, and any approach restrictions before you've even tuned the frequency.

If `--xplane` points somewhere without the expected files, the page degrades to whatever it can find: comm frequencies and ATIS text still appear, runway/ILS sections show a "data not found" line.

## VATSIM data

The script makes a single GET against `https://data.vatsim.net/v3/vatsim-data.json` with a polite User-Agent, parses the controller list, and matches callsigns of the form `<ICAO>_GND`, `<ICAO>_TWR`, `<ICAO>_ATIS`, `<ICAO>_DEL`, `<ICAO>_APP`. Split sectors (`EDDM_N_TWR`) are handled by suffix.

The output is a snapshot from the moment the script ran; the title says `(VATSIM live, HH:MMZ)` so you know how fresh it is. Re-run the script before you connect if you want the latest state.

If the request fails (offline, DNS, timeout, JSON change), the script emits a stderr note and continues with blank frequency cells. UNICOM and the call-marker fallback still render.

## Limitations / known scope

- Single wind aloft, applied uniformly. No per-leg wind. Fine for a short VFR hop, less useful for a 200 NM cross-country.
- No mass & balance or takeoff/landing distance computation (page 2 of the original LBA form). Stations are in the JSON for future expansion.
- Magnetic variation is a single constant for the whole route. Acceptable for Germany; not great if you fly across magvar gradients.
- No MORA (Minimum Off-Route Altitude) lookup. The column was removed; if you want it back you need to feed it from somewhere (X-Plane's grid MORA in `Custom Data` would be one source).
- No real-weather METAR ingestion. Wind comes from the CLI.

PRs welcome on any of the above.

## License

MIT. See `LICENSE`.
