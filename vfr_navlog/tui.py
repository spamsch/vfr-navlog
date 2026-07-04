"""Interactive setup wizard: prompts the user and returns a RunConfig."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import DEFAULT_XPLANE, PROJECT_ROOT
from .exports import _ask_fpl_fields
from .lnmpln import parse_lnmpln, parse_magvar, parse_wind
from .model import Plan, RunConfig
from .navigraph import read_navigraph_flight
from .weather import _wind_from_metar, fetch_metar


def _tui() -> RunConfig:
    """Interactive setup wizard, runs when navlog.py is called with no arguments."""
    # Enable readline tab-completion for file paths
    try:
        import glob as _glob
        import readline
        readline.set_completer_delims(" \t\n;")
        readline.parse_and_bind("tab: complete")
        readline.set_completer(
            lambda text, state: (
                _glob.glob(text.replace("~", str(Path.home())) + "*") + [None]
            )[state]
        )
    except Exception:
        pass

    B, C, G, DIM, R = "\033[1m", "\033[36m", "\033[32m", "\033[2m", "\033[0m"

    def h(title: str) -> None:
        print(f"\n{B}{C}{title}{R}")

    print(f"\n{B}VFR Navlog{R}  —  no arguments given, running interactive setup")
    print(f"{DIM}Tab-completes file paths. Press Enter to accept [defaults].{R}")

    # --- Plan source ---
    h("Plan source")
    print("  [1]  Little Navmap .lnmpln file")
    print("  [2]  Navigraph Charts  (reads live from the app, macOS only)")
    while True:
        src = input("  → [1]: ").strip() or "1"
        if src in ("1", "2"):
            break
        print("  Enter 1 or 2.")

    navigraph = src == "2"
    plan_path: Path | None = None
    _preview: Plan | None = None

    dep_icao: str | None = None
    cruise_alt_default: float = 2500.0

    if not navigraph:
        h("Flight plan file  (.lnmpln)")
        while True:
            raw = input("  Path: ").strip()
            if not raw:
                print("  (required)")
                continue
            p = Path(raw).expanduser()
            if p.exists():
                plan_path = p
                break
            print(f"  Not found: {p}")
        # Parse immediately so waypoints and cruise alt are available for later prompts.
        try:
            _preview = parse_lnmpln(plan_path)
            dep_icao = _preview.waypoints[0].ident if _preview.waypoints else None
            cruise_alt_default = _preview.cruise_alt_ft
            _wps_str = f"{_preview.waypoints[0].ident} → {_preview.waypoints[-1].ident}"
            print(f"  {G}Loaded: {_wps_str}  ({len(_preview.waypoints)} waypoints, "
                  f"cruise alt {int(cruise_alt_default)} ft){R}")
        except Exception as _exc:
            print(f"  {DIM}Warning: could not parse plan ({_exc}) — waypoints unavailable.{R}")
    else:
        # Navigraph: pre-load the live plan so waypoints are available at the altitude step.
        print(f"  {DIM}Reading active Navigraph flight plan…{R}", end="", flush=True)
        try:
            _preview = read_navigraph_flight(DEFAULT_XPLANE)
            dep_icao = _preview.waypoints[0].ident if _preview.waypoints else None
            cruise_alt_default = _preview.cruise_alt_ft
            _wps_str = f"{_preview.waypoints[0].ident} → {_preview.waypoints[-1].ident}"
            print(f"\r  {G}Loaded: {_wps_str}  ({len(_preview.waypoints)} waypoints, "
                  f"cruise alt {int(cruise_alt_default)} ft){R}")
        except BaseException:
            print(f"\r  {DIM}Could not pre-load — waypoints available after generation.{R}")

    # --- Aircraft ---
    script_dir = PROJECT_ROOT
    ac_candidates = sorted(script_dir.glob("aircraft_*.json"))
    if not ac_candidates:
        ac_candidates = sorted(Path(".").glob("aircraft_*.json"))
    ac_default = str(ac_candidates[0]) if ac_candidates else ""

    h("Aircraft JSON")
    if ac_candidates:
        for i, p in enumerate(ac_candidates):
            mark = f"  {G}← default{R}" if i == 0 else ""
            print(f"  [{i + 1}]  {p.name}{mark}")
        print("  [path]  or type a path to a different file")

    aircraft_path: Path | None = None
    while aircraft_path is None:
        hint = f" [{Path(ac_default).name}]" if ac_default else ""
        raw = input(f"  →{hint}: ").strip()
        if not raw and ac_default:
            aircraft_path = Path(ac_default)
        elif raw.isdigit() and ac_candidates:
            idx = int(raw) - 1
            if 0 <= idx < len(ac_candidates):
                aircraft_path = ac_candidates[idx]
            else:
                print(f"  Choose 1–{len(ac_candidates)}.")
        else:
            p = Path(raw).expanduser()
            if p.exists():
                aircraft_path = p
            else:
                print(f"  Not found: {p}")

    # --- Registration ---
    _ac_data: dict = {}
    try:
        _ac_data = json.loads(aircraft_path.read_text())
    except Exception:
        pass
    reg_default = _ac_data.get("registration", "")

    h("Aircraft registration")
    hint = f" [{reg_default}]" if reg_default else ""
    raw = input(f"  →{hint}: ").strip()
    registration = raw if raw else reg_default

    # --- Wind ---
    h("Wind aloft  (DDD/SS, e.g. 270/15 — or 0/0 for calm)")
    if dep_icao:
        print(f"  [M]  fetch surface wind from VATSIM METAR at {dep_icao}")
    else:
        print("  [M]  fetch surface wind from VATSIM METAR  (you'll enter an ICAO)")
    wind_str = "0/0"
    while True:
        raw = input("  → [0/0]: ").strip() or "0/0"
        if raw.upper() == "M":
            icao_q = dep_icao or input("  ICAO for METAR: ").strip().upper()
            if not icao_q:
                print("  (ICAO required)")
                continue
            print(f"  Fetching METAR for {icao_q}…", end="", flush=True)
            metar = fetch_metar(icao_q)
            if not metar:
                print(f"\n  {DIM}Could not reach VATSIM METAR — enter wind manually.{R}")
                continue
            print(f"\r  {DIM}{metar}{R}")
            wdata = _wind_from_metar(metar)
            if wdata:
                wind_str = f"{int(wdata[0]):03d}/{int(wdata[1]):02d}"
                print(f"  {G}Using wind: {wind_str}{R}  {DIM}(surface METAR — not wind aloft){R}")
                break
            else:
                print(f"  {DIM}Could not parse wind from METAR — enter manually.{R}")
                continue
        elif re.match(r"^\d{1,3}/\d{1,3}$", raw):
            wind_str = raw
            break
        else:
            print("  Format: 270/15  or  M to fetch from VATSIM METAR")

    # --- Cruise altitude ---
    h("Cruise altitude  (ft MSL)")
    while True:
        raw = input(f"  → [{int(cruise_alt_default)}]: ").strip() or str(int(cruise_alt_default))
        try:
            val = float(raw)
            if val > 0:
                cruise_alt_ft = val
                break
        except ValueError:
            pass
        print("  Enter a positive number in feet (e.g. 3500).")

    # --- Altitude changes at waypoints ---
    h("Altitude changes at waypoints  (optional)")
    _prev_wps = _preview.waypoints if _preview is not None else []
    # Coordinate-based waypoints (ident starts with a digit) get short aliases GPS1, GPS2, …
    _gps_alias: dict[str, str] = {}       # original_ident.upper() -> "GPS1"
    _alias_to_ident: dict[str, str] = {}  # "GPS1" -> original_ident.upper()
    _gps_n = 1
    for _awp in _prev_wps:
        if _awp.ident and _awp.ident[0].isdigit():
            _al = f"GPS{_gps_n}"
            _gps_alias[_awp.ident.upper()] = _al
            _alias_to_ident[_al] = _awp.ident.upper()
            _gps_n += 1
    if _prev_wps:
        print("  Waypoints in route:")
        for _wi, _wp in enumerate(_prev_wps):
            _al = _gps_alias.get(_wp.ident.upper(), "")
            _label = f"{_al}  ({_wp.ident})" if _al else _wp.ident
            print(f"    {_wi + 1:2d}.  {_label}")
    print(f"  Current cruise alt: {int(cruise_alt_ft)} ft from departure.")
    print("  Enter WP ALT pairs for step climbs/descents. Empty line to finish.")
    # Build example idents from the actual route: first interior named WP and first GPS alias.
    _ex_dep = _prev_wps[0].ident if _prev_wps else "EDDG"
    _ex_named = next(
        (wp.ident for wp in _prev_wps[1:-1] if not wp.ident[0:1].isdigit()),
        "BADGO",
    )
    _ex_named2 = next(
        (wp.ident for wp in _prev_wps[1:-1] if not wp.ident[0:1].isdigit() and wp.ident != _ex_named),
        "WLD",
    )
    _ex_gps = next(iter(_alias_to_ident), "GPS1")
    print(f"  e.g.  {_ex_dep} {_ex_named} 4500   fly 4 500 ft from {_ex_dep} to {_ex_named}, then revert")
    print(f"        {_ex_named} {_ex_named2} 15000  fly 15 000 ft from {_ex_named} to {_ex_named2}, then revert")
    print(f"        {_ex_named2} 5500         step down to 5 500 ft at {_ex_named2} (open-ended)")
    print(f"        {_ex_gps} 4500           same for a GPS fix")
    alt_profile: list[tuple[str, float]] = []
    _known_orig = {wp.ident.upper() for wp in _prev_wps}

    def _resolve(tok: str) -> str:
        t = tok.upper()
        return _alias_to_ident.get(t, t)

    def _disp(ident: str) -> str:
        return _gps_alias.get(ident.upper(), ident)

    while True:
        raw = input("  → ").strip()
        if not raw:
            break
        _parts = raw.split()
        if len(_parts) == 2:
            # WP ALT — altitude from WP onwards until the next change
            _w1, _alt_raw = _parts
            _w1 = _resolve(_w1)
            try:
                _alt_in = float(_alt_raw)
                if _alt_in <= 0:
                    raise ValueError
            except ValueError:
                print("  Altitude must be a positive number in feet.")
                continue
            if _known_orig and _w1 not in _known_orig:
                print(f"  {DIM}Warning: {_w1} not in route — adding anyway.{R}")
            alt_profile.append((_w1, _alt_in))
            print(f"  {G}From {_disp(_w1)}: {int(_alt_in):,} ft{R}")
        elif len(_parts) == 3:
            # WP1 WP2 ALT — closed segment: fly ALT from WP1, revert to cruise_alt_ft at WP2
            _w1, _w2, _alt_raw = _parts
            _w1, _w2 = _resolve(_w1), _resolve(_w2)
            try:
                _alt_in = float(_alt_raw)
                if _alt_in <= 0:
                    raise ValueError
            except ValueError:
                print("  Altitude must be a positive number in feet.")
                continue
            for _wchk in (_w1, _w2):
                if _known_orig and _wchk not in _known_orig:
                    print(f"  {DIM}Warning: {_wchk} not in route — adding anyway.{R}")
            alt_profile.append((_w1, _alt_in))
            alt_profile.append((_w2, cruise_alt_ft))  # auto-revert to base cruise alt
            print(f"  {G}From {_disp(_w1)} to {_disp(_w2)}: {int(_alt_in):,} ft  "
                  f"(→ {int(cruise_alt_ft):,} ft from {_disp(_w2)}){R}")
        else:
            print("  Format:  WP ALT          e.g. BADGO 15000")
            print("           WP1 WP2 ALT     e.g. EDDG HMM 4500")

    # --- Magnetic variation ---
    h("Magnetic variation  (e.g. 4E, 1.0W, -2.5)")
    while True:
        raw = input("  → [4E]: ").strip() or "4E"
        if re.match(r"^[+-]?\d+(\.\d+)?[EWew]?$", raw.strip()):
            magvar_str = raw
            break
        print("  Format: 2.5E  or  2.5W  or  -2.5")

    # --- VATSIM ---
    h("VATSIM  (fetch live ATC frequencies?)")
    vatsim = input("  → [y/N]: ").strip().lower() in ("y", "yes")

    # --- VOR radial fixes (automatic) ---
    h("VOR-Kreuzpeilungen automatisch berechnen?  (aus X-Plane earth_nav.dat)")
    print(f"  {DIM}Berechnet je Wegpunkt bis zu zwei VOR-Radiale zur Standlinien-Kontrolle.{R}")
    vor_fixes = input("  → [y/N]: ").strip().lower() in ("y", "yes")

    # --- VOR info per waypoint ---
    h("VOR-Informationen je Wegpunkt manuell eingeben?  (z. B. 233 FROM)")
    if vor_fixes:
        print(f"  {DIM}Enter je Wegpunkt übernimmt die berechnete Peilung; Freitext überschreibt sie.{R}")
    vor_info = input("  → [y/N]: ").strip().lower() in ("y", "yes")

    # --- DFS charts ---
    h("DFS airport charts  (append VFR charts for destination?)")
    raw = input("  → [Y/n]: ").strip().lower()
    dfs_charts = raw not in ("n", "no")

    # --- Waypoint map pages (openflightmaps) ---
    h("Wegpunkt-Kartenseiten aus openflightmaps?  (je Wegpunkt ein Kartenausschnitt)")
    print(f"  {DIM}Lädt Kartenkacheln beim ersten Lauf (Cache danach). Region Europa.{R}")
    wp_maps = input("  → [y/N]: ").strip().lower() in ("y", "yes")
    map_radius_nm = 3.0
    map_base = "both"
    chart_source = "ofm"
    if wp_maps:
        while True:
            raw = input("  Radius NM (1–5)  → [3]: ").strip() or "3"
            try:
                val = float(raw)
                if 1.0 <= val <= 5.0:
                    map_radius_nm = val
                    break
            except ValueError:
                pass
            print("  Enter a number between 1 and 5.")
        print(f"  {DIM}Basiskarte je Seite: [1] Karte + Orthofoto  [2] nur Karte  [3] nur Foto{R}")
        _bchoice = input("  → [1]: ").strip() or "1"
        map_base = {"1": "both", "2": "chart", "3": "photo"}.get(_bchoice, "both")
        if map_base != "photo":
            print(f"  {DIM}Kartenquelle: [1] openflightmaps  [2] DFS ICAO 500k "
                  f"(© DFS — nur private Nutzung){R}")
            _cchoice = input("  → [1]: ").strip() or "1"
            chart_source = {"1": "ofm", "2": "dfs"}.get(_cchoice, "ofm")

    # --- FMS ---
    h("X-Plane FMS export")
    fms_dir = DEFAULT_XPLANE / "Output" / "FMS plans"
    xp_found = DEFAULT_XPLANE.exists()
    if xp_found:
        print(f"  {DIM}Output: {fms_dir}{R}")
        fms_default = "Y"
    else:
        print(f"  {DIM}X-Plane not found at default path — FMS will go next to PDF if yes{R}")
        fms_default = "N"
    raw = input(f"  → [{fms_default}]: ").strip().lower()
    fms = raw in ("y", "yes") or (not raw and xp_found)

    # --- FPL ---
    fpl_fields = _ask_fpl_fields(B, C, G, DIM, R)

    print()

    return RunConfig(
        navigraph=navigraph,
        plan_path=plan_path,
        aircraft_path=aircraft_path,
        wind=parse_wind(wind_str),
        wind_was_default=(wind_str == "0/0"),
        magvar=parse_magvar(magvar_str),
        registration=registration,
        cruise_alt_ft=cruise_alt_ft,
        alt_profile=alt_profile,
        output=None,
        xplane_path=DEFAULT_XPLANE,
        vatsim=vatsim,
        vor_info=vor_info,
        with_dfs_charts=dfs_charts,
        call_tower_nm=10.0,
        fms=fms,
        fpl_fields=fpl_fields,
        vor_fixes=vor_fixes,
        wp_maps=wp_maps,
        map_radius_nm=map_radius_nm,
        map_base=map_base,
        chart_source=chart_source,
    )
