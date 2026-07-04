# Feature plan: automatic VOR radial fixes per waypoint

Status: proposal, 2026-07-04. Companion to `REFACTORING.md`.

## The problem

VFR waypoints are identified visually and by elapsed time — both soft. The
hard method is a VOR cross-check: tune a station, set the OBS to a
pre-computed radial, and when the CDI centers you *know* you're crossing that
line. Two radials from different stations give a positive position fix.

The navlog can't do this today. There is a "VOR Info" column on the PDF, but
it's filled by hand: `collect_vor_info()` (`navlog.py:2649`) prompts for free
text per waypoint ("233 FROM"), which means computing radials yourself before
the flight — with a chart and a protractor, per waypoint, per plan. The data
to automate it is already on disk: X-Plane's `earth_nav.dat`, which
`_build_nav_index()` (`navlog.py:1917`) already parses for VOR positions and
frequencies. It currently throws away two fields the feature needs (published
range, slaved variation); more on that below.

## What the pilot needs on the kneeboard

Approaching a waypoint, at a glance:

```
HLZ 116.30  R245
DLE 115.20  R010
```

Station ident, frequency, radial. The first radial should cross the course at
a healthy angle so its CDI centering answers "am I there yet?". The second,
from a different station, turns the line into a point. If the station has DME,
the distance is a bonus check on one frequency.

## Domain rules the implementation must respect

These are the parts that are easy to get subtly wrong:

1. **A radial is the magnetic bearing FROM the station**, measured against the
   station's *slaved variation* (the calibration twist the VOR was aligned to),
   not the local magnetic variation of the waypoint and not the plan-wide
   `--magvar`. `earth_nav.dat` type-3 rows carry this value in the field the
   current parser skips (`parts[6]`; range in nm is `parts[5]`). Using plan
   magvar instead can be several degrees off near stations with old slaving —
   several degrees at 40 nm is miles of position error.
2. **Reception is line-of-sight and range-limited.** Filter candidates by the
   published range from `parts[5]`, capped at ~80 nm, and expect nothing below
   the horizon at typical VFR altitudes. Don't model terrain; the published
   range plus the cap is the right amount of effort.
3. **Geometry decides usefulness.**
   - The primary radial should cross the leg's course at ≥ 30°, ideally near
     90° (a station roughly abeam the track). A radial nearly parallel to the
     course centers slowly and answers the wrong question.
   - For a two-radial fix, the *two radials* must intersect at ≥ 30°,
     ideally near 90°. Two stations on the same side at similar bearings are
     one line of position wearing two frequencies.
   - Closer stations are more accurate: 1° of CDI ≈ distance/60 nm of lateral
     error. Prefer near over far when angles are comparable.
4. **A waypoint sitting on a VOR needs no radial.** If the waypoint is within
   ~2 nm of a station, the fix is "station passage" — print `HLZ 116.30 ↑ overhead`
   and skip the geometry.
5. **Radials are printed as three digits** (`R245`, `R010`), rounded to whole
   degrees. That's how the OBS is set; decimals are noise.

## Design

### Data model

```python
@dataclass
class RadialFix:
    vor_ident: str      # "HLZ"
    vor_name: str       # "Hehlingen" — for the reference table, not the main grid
    freq: str           # "116.30"
    radial: int         # 0–359, magnetic FROM station, slaved-variation corrected
    dist_nm: float      # station → waypoint, for the DME cross-check
    has_dme: bool
    overhead: bool = False   # waypoint is station passage

# Waypoint grows:
#   fixes: list[RadialFix] = field(default_factory=list)   # 0–2 entries
# The existing free-text vor_info stays and, when set, OVERRIDES the
# computed fixes in the PDF cell. Manual beats automatic.
```

### New module-level pieces (all pure except the file scan)

1. `load_vors(xplane_path) -> list[VorStation]` — one pass over
   `earth_nav.dat`, type-3 rows, keeping lat/lon, freq, **range (parts[5])**,
   **slaved variation (parts[6])**, ident, name. Detect co-located DME from
   type-12/13 rows with the same ident/freq. This extends what
   `_build_nav_index()` half-does; after the refactor both live in
   `vfr_navlog/xplane.py` and share the line parser.
2. `radial_from(station, lat, lon) -> int` — true bearing station→point via
   the existing `great_circle()`, minus slaved variation, normalized 0–359.
3. `select_fixes(wp, inbound_course, stations, max_fixes=2) -> list[RadialFix]`
   — the scoring function. Candidate filter: in range, not overhead-case.
   Score = f(crossing angle with course, distance, DME bonus). Primary =
   best score with crossing angle ≥ 30°. Secondary = best remaining station
   whose radial intersects the primary's at ≥ 30°. Pure, unit-testable, and
   the only place the geometry heuristics live.
4. `attach_vor_fixes(plan, stations) -> None` — walks waypoints 1..n, passes
   each waypoint's inbound leg course, fills `wp.fixes`. Skips the departure
   airport (you know where you are) but includes the destination (a radial
   confirming field identification is genuinely useful).

### PDF output

The current `VOR\nInfo` column (`navlog.py:884`) is 24 mm — fits one fix,
not two. Changes:

- Widen to ~34 mm, taking the width from `Waypoint` (56 → 46 mm; idents plus
  names still fit — verify against the longest fixture name).
- Render up to two fixes stacked in the cell at font size ~6.5:
  `HLZ 116.30 R245` / `DLE 115.20 R010`. Row height (6.5 mm) already
  accommodates two small lines; if it gets cramped, bump `row_h` for rows that
  carry two fixes rather than shrinking the font further.
- Precedence per waypoint: manual `vor_info` text > computed fixes > `wp.freq`
  (the current fallback chain at `navlog.py:956` gains one link).
- **Optional second deliverable, recommended:** a "Navaid-Referenz" block on
  the destination or a spare corner of page 1 — one line per distinct station
  used anywhere in the plan: ident, name, frequency, DME yes/no, Morse code.
  You identify a VOR by its Morse before trusting it; putting the dot-dash
  pattern on the kneeboard closes that loop. Morse table is 26 entries of
  static data.

### TUI / CLI

- CLI: `--vor-fixes` (flag, default off to keep current output stable; flip
  the default later if it earns it). Requires a resolvable `--xplane` path;
  degrade with a stderr note if nav data is missing, never fail the run.
- TUI: in the existing VOR prompt flow, show the computed fixes as the
  default answer per waypoint — Enter accepts, typing free text overrides.
  This keeps `collect_vor_info()`'s contract (manual entry) while making the
  default the computed truth instead of an empty string.
- FMS/FPL export, phraseology, weather pages: untouched.

### What this does NOT do

- **No NDB fixes** in v1. QDMs from NDBs are a plausible fallback where VOR
  coverage is thin, but ADF workflow is different enough (no OBS, relative
  bearing) that it's a separate feature with its own output format.
- **No altitude-aware reception modeling.** Published range + 80 nm cap.
- **No tracking guidance** (flying a radial as a course). This is
  cross-checks only; the route already has magnetic headings.
- **No real-world AIRAC guarantee.** Radials come from X-Plane's nav data,
  which is what the sim's receivers use — correct for simming. For real-world
  use, the footer disclaimer already covers "check against current charts";
  a decommissioned VOR in stale nav data is the realistic hazard.

## Testing

All the interesting logic is pure:

- `radial_from`: fixture station with known slaved variation; assert against
  hand-computed radials, including the wraparound cases (359/000) and a
  southern-hemisphere-style negative variation.
- `select_fixes`: synthetic geometries — station abeam (should win), station
  dead ahead (should lose to abeam), two stations at 10° separation (second
  must be rejected), everything out of range (empty result), waypoint on
  station (overhead).
- `load_vors`: 10-line `earth_nav.dat` fixture covering VOR, VOR-DME,
  DME-only, and a malformed line.
- PDF snapshot (from the refactoring plan's Phase 0) re-baselined once, since
  the column widths change.

## Sequencing against the refactoring plan

The clean order is: refactoring **Phase 0** (tests + pyproject), **Phase 1**
(package split), then this feature lands as `vfr_navlog/fixes.py` +
extensions to `xplane.py`, `model.py`, `pdf/navlog_page.py`, `tui.py`,
`cli.py` — small diffs in files that each own one concern.

If the itch strikes before the refactor: the feature is implementable in
today's single file as one new section (`# --- VOR radial fixes ---`) with
the four functions above, ~200 lines. It's self-contained enough to move
cleanly during Phase 1. What should NOT happen is doing it mid-refactor.

Estimated size either way: ~200 lines of logic + ~40 lines of PDF changes +
tests. One sitting for the core, a second for the Morse reference block and
TUI defaults.
