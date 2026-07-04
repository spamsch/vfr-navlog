# Refactoring plan for vfr-navlog

Status: proposal, 2026-07-04. Nothing in here is done yet.

## What we have

One file does everything. `navlog.py` is 3,211 lines and carries at least
eleven distinct concerns, glued together with section-comment banners:

| Concern | Lines (approx) | Notes |
|---|---|---|
| Nav math (great circle, wind triangle) | 79–113 | Pure functions, trivially testable |
| Plan parsing (.lnmpln XML) | 114–195 | Pure |
| VATSIM API client | 196–314 | urllib, hand-rolled |
| X-Plane data parsers (apt.dat, earth_nav.dat) | 315–477 | Line-by-line scans of huge files |
| Leg computation + hemispheric rule | 500–604 | Pure |
| PDF: navlog page + orchestration | 605–1129 | `render()` is ~450 lines |
| PDF: phraseology page | 1130–1491 | ~360 lines, German ATC scripts inline |
| PDF: destination + weather pages | 1493–1869 | ~380 lines combined |
| Navigraph Charts (leveldb via vendored ccl) | 1870–2121 | `sys.path` hack at runtime |
| Weather (METAR/TAF/ATIS fetch + parse) | 2173–2537 | Two near-parallel parsers |
| FMS/FPL export, TUI, CLI | 2122–2144, 2538–3211 | `_tui()` ~320 lines, `main()` ~215 |

`dfs_charts.py` (228 lines) is reasonable on its own but is both a standalone
script and a lazy import target of `navlog.py`, and it uses `requests` while
everything else uses `urllib` — two HTTP stacks in a two-file project.

Repo hygiene is already fine: PDFs, chart folders (`EDDV/`, `EDLI/`), `.env`,
and the vendored `ccl_chromium_reader/` are all gitignored. The problems are
structural, not clutter.

### Concrete defects worth naming

1. **`render()` takes 14 parameters** (`navlog.py:677`), half of them optional,
   threaded through from both `main()` and `_tui()`. Every new feature widens
   this signature (see `field_wx`, `dfs_charts` — the two most recent additions).
2. **`_tui()` forges an `argparse.Namespace`** to impersonate CLI args. The
   coupling has already drifted: `main()` needs
   `getattr(args, "dfs_charts", False)` (`navlog.py:3150`) because the two
   construction paths don't agree on which attributes exist.
3. **Two distance implementations**: `great_circle()` (`navlog.py:81`) and
   `_haversine_m()` (`navlog.py:353`) compute the same thing in different
   units. One of them will eventually be fixed and the other won't.
4. **Three copies of the urllib request ritual** (UA header, timeout, error
   swallowing) at `navlog.py:218`, `:2176`, `:2211` — plus a fourth stack in
   `dfs_charts.py` using `requests.Session`.
5. **`parse_metar()` and `parse_atis()`** (`navlog.py:2258`, `:2378`) both
   tokenize wind/vis/clouds/QNH with overlapping but separately-maintained
   regexes.
6. **`apt.dat` is scanned front-to-back twice per run** when the Navigraph
   path is active: `parse_airport()` (`navlog.py:361`) for the destination and
   `_airport_position()` (`navlog.py:1886`) per airport in the route. X-Plane
   12's global apt.dat is hundreds of MB; each scan is seconds of wall time,
   and both functions duplicate the same "row 1 header / row 100 runway"
   state machine.
7. **All network I/O is sequential**: VATSIM snapshot, METAR + TAF for two
   airports, ATIS lookups — each with a 6 s timeout. Worst case on a flaky
   connection is ~40 s of staring at the terminal before the PDF starts.
8. **No dependency declaration anywhere.** `fpdf2` is required, `requests` and
   `img2pdf` are required by `dfs_charts.py`, and none of them appear in a
   `pyproject.toml` or `requirements.txt`. Setting this up on a new machine is
   archaeology.
9. **No tests.** The pure core (wind triangle, hemispheric rule, METAR parser,
   FPL formatter) is exactly the kind of code that silently rots without them,
   and it's also the cheapest code in the project to test.

## Constraints

- Personal tool, one user, macOS. No backward-compat obligations beyond the
  CLI flags in the README.
- The tool must keep working after every phase. No big-bang rewrite branch
  that sits unmergeable for a week.
- There is an uncommitted change (hemispheric rule) in the working tree.
  **Commit it before any of this starts** — refactoring on top of unreviewed
  feature work is how regressions hide.
- PDF layout is coordinate-tuned by hand. Any refactor that touches rendering
  must be verified against a reference PDF, not just against "it didn't crash."

## Target layout

```
vfr-navlog/
├── pyproject.toml            # deps, entry point: vfr-navlog = vfr_navlog.cli:main
├── navlog.py                 # 3-line shim: from vfr_navlog.cli import main; main()
├── vfr_navlog/
│   ├── __init__.py
│   ├── config.py             # constants, .env loading, default paths (X-Plane, Navigraph)
│   ├── model.py              # Waypoint, Plan, Leg, AirportInfo, Runway, IlsLoc,
│   │                         #   VatsimSnapshot, ParsedMetar, WeatherBriefing, FieldWx
│   ├── geo.py                # great_circle, apply_wind, haversine (one impl)
│   ├── legs.py               # compute_legs, _effective_leg_alt, hemispheric rule,
│   │                         #   find_call_marker
│   ├── lnmpln.py             # parse_lnmpln, parse_wind, parse_magvar
│   ├── navigraph.py          # leveldb read, _navigraph_plan, ccl import shim
│   ├── xplane.py             # apt.dat / earth_nav.dat / earth_fix.dat parsers
│   ├── net.py                # one fetch(url, timeout) helper — the only place
│   │                         #   that knows about User-Agent and error policy
│   ├── vatsim.py             # fetch_vatsim, FIR/radar helpers, frequency logic
│   ├── weather.py            # METAR/TAF/ATIS fetch, unified parser, briefing,
│   │                         #   field_weather
│   ├── dfs_charts.py         # moved as-is, ported to net.py's fetch
│   ├── exports.py            # write_fms, format_icao_fpl, collect_vor_info
│   ├── tui.py                # interactive prompts → returns RunConfig
│   ├── cli.py                # argparse → RunConfig, main() orchestration
│   └── pdf/
│       ├── __init__.py       # render(ctx: RenderContext) orchestrator
│       ├── base.py           # NavlogPDF, install_fonts, hms, fmt_int, layout consts
│       ├── navlog_page.py    # the main table page
│       ├── phraseology.py    # ATC scripts; phrase text as data, not code
│       ├── destination.py
│       ├── weather_page.py
│       └── charts.py         # _append_dfs_charts
└── tests/
    ├── test_geo.py
    ├── test_legs.py
    ├── test_weather_parse.py
    ├── test_fpl.py
    ├── test_lnmpln.py        # with a small fixture .lnmpln
    └── test_pdf_snapshot.py  # end-to-end text-content snapshot
```

Deliberately flat: one package, one sub-package for the PDF pages. This
project does not need `planning/`, `data/`, `net/` sub-packages — that's
ceremony for a codebase ten times this size.

### The two new types that make it work

```python
@dataclass
class RunConfig:          # replaces the forged argparse.Namespace
    plan_path: Path | None        # None → Navigraph source
    aircraft_path: Path
    wind: tuple[float, float]
    magvar: float
    registration: str
    cruise_alt_ft: float | None
    output: Path | None
    xplane_path: Path | None
    call_tower_nm: float
    with_dfs_charts: bool
    ...

@dataclass
class RenderContext:      # replaces render()'s 14 parameters
    plan: Plan
    aircraft: dict
    legs: list[Leg]
    wind: tuple[float, float]
    magvar: float
    vatsim: VatsimSnapshot | None
    dest_info: AirportInfo | None
    weather: WeatherBriefing | None
    field_wx: dict[str, FieldWx]
    fir_icaos: list[str]
    source_note: str
    call_tower_nm: float
    with_dfs_charts: bool
```

Both `cli.main()` and `tui` produce a `RunConfig`; a single
`build_context(config) -> RenderContext` does the fetching and computation.
`getattr(args, ..., default)` dies with the Namespace.

## Phased execution

Each phase is one or two commits and ends with the tool producing a correct
PDF. Never let a phase sprawl.

### Phase 0 — safety net (before touching structure)

1. Commit the pending hemispheric-rule change.
2. Add `pyproject.toml`: project metadata, `fpdf2`, `requests`, `img2pdf`,
   dev extras `pytest`, `pypdf`, `ruff`. Script entry point `vfr-navlog`.
3. Characterization tests against the *current* single file — import from
   `navlog` directly. Cover the pure functions:
   `great_circle`, `apply_wind`, `hemispheric_alt`, `apply_hemispheric_rule`,
   `parse_wind`, `parse_magvar`, `parse_metar`, `parse_atis`,
   `format_icao_fpl`, `hms`, `fmt_int`, `compute_legs` with a 3-waypoint
   fixture. These pin behavior before anything moves.
4. PDF snapshot test: run end-to-end on a fixture `.lnmpln` with fixed wind
   and all network features off, extract page text with `pypdf`, compare to a
   stored snapshot. This catches content regressions; layout stays a manual
   eyeball check (keep one reference PDF and diff visually once per phase).
5. Add `ruff` config; fix only what it flags automatically. No manual style
   churn in this phase.

### Phase 1 — mechanical split (no behavior change)

Move code into the package layout above, in dependency order so each commit
imports cleanly:

1. `config.py`, `model.py`, `geo.py` (fold `_haversine_m` into `geo.py`,
   delete the duplicate, express `great_circle` distance via the single
   haversine core).
2. `lnmpln.py`, `legs.py`, `xplane.py`, `navigraph.py`.
3. `net.py`, `vatsim.py`, `weather.py`, `exports.py`; move `dfs_charts.py`
   into the package and re-point its imports.
4. `pdf/` split: `base.py` first, then one file per page function. `render()`
   moves as-is (still ugly) into `pdf/__init__.py`.
5. `cli.py` + `tui.py`; `navlog.py` becomes the shim.
6. Update tests to import from `vfr_navlog.*`. Snapshot must be byte-identical
   in extracted text.

Rule for this phase: **cut and paste only.** No renames beyond module paths,
no signature changes, no cleverness. Every diff should be reviewable as
"same code, new home."

### Phase 2 — interface repair

Now that things have homes, fix the seams:

1. Introduce `RunConfig`; make `tui.py` and `cli.py` both produce it. Delete
   the Namespace forgery and every `getattr(args, ...)`.
2. Introduce `RenderContext`; collapse `render()`'s signature. Then split
   `render()`'s ~450-line body: table-row building, header block, fuel block,
   and page orchestration become separate functions inside `navlog_page.py`.
3. Unify HTTP on `net.fetch()` (urllib is fine; drop the `requests` dependency
   from `dfs_charts.py` unless its session/redirect handling turns out to
   matter — check before porting).
4. Merge the METAR and ATIS token parsers into one tokenizer with two entry
   points. The tests from Phase 0 make this safe.
5. In `phraseology.py`, pull the German radio scripts out of imperative
   `pdf.cell()` sequences into a data structure (list of labeled exchanges),
   rendered by one small loop. Editing phraseology should mean editing text,
   not reading fpdf calls.

### Phase 3 — performance

Ordered by measured pain, not by fun:

1. **Single-pass apt.dat scan.** One function walks apt.dat once and answers
   for a *set* of ICAOs: full `AirportInfo` for the destination, position for
   every route airport. Kills the duplicate state machine and the second
   multi-hundred-MB scan. This is the biggest wall-clock win.
2. **Parallel network fetches.** `ThreadPoolExecutor(max_workers=6)` over the
   independent calls (VATSIM snapshot, 2× METAR, 2× TAF, ATIS). All results
   are already optional-typed, so a failed future just yields `None`. Turns
   worst-case ~40 s of serial timeouts into one 6 s window.
3. Optional, only if startup still feels slow: cache the apt.dat byte offsets
   per ICAO in a JSON sidecar keyed by apt.dat mtime. Skip unless (1) and (2)
   leave it annoying.

### Phase 4 — finish

1. README: update usage for the installed `vfr-navlog` entry point; keep
   `python3 navlog.py` working via the shim and say so.
2. Delete dead code the split exposed (there will be some — orphaned helpers
   near the old section banners are the usual suspects).
3. One full manual run: TUI path and CLI path, Navigraph source and .lnmpln
   source, with charts, against the reference PDF.

## Explicitly not doing

- **No async rewrite.** Threads over six blocking calls is enough forever.
- **No plugin architecture for PDF pages.** There are five pages; a function
  call per page is the right abstraction.
- **No wrapper layer over fpdf.** The coordinate math is the domain; hiding
  it behind an abstraction makes layout tuning worse.
- **No un-vendoring of ccl_chromium_reader.** It's not on PyPI; the runtime
  path shim moves into `navigraph.py` and stays. Documenting the clone step
  in the README is the whole fix.
- **No i18n.** The PDF is German because the pilot is German.

## Order of risk

Phases 0–1 are near-zero risk (tests first, then pure moves). Phase 2 step 4
(parser merge) and Phase 3 step 1 (apt.dat unification) are the two changes
that can actually break output — both are covered by the Phase 0 tests, and
both should be their own commits so a bad one reverts clean.
