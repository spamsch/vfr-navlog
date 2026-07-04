"""Navigraph Charts integration: read the active plan from Electron localStorage."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from .config import APT_REL, NAVIGRAPH_LDB, PROJECT_ROOT
from .geo import haversine_m
from .model import Plan, Waypoint
from .xplane import _airport_position, _build_nav_index


def _decode_dms(token: str) -> tuple[float, float] | None:
    """Parse a Navigraph/ICAO DMS coordinate like '520630N0075118E' → (lat, lon)."""
    m = re.match(r'^(\d{2})(\d{2})(\d{2})([NS])(\d{3})(\d{2})(\d{2})([EW])$', token)
    if not m:
        return None
    lat = int(m.group(1)) + int(m.group(2)) / 60 + int(m.group(3)) / 3600
    if m.group(4) == 'S':
        lat = -lat
    lon = int(m.group(5)) + int(m.group(6)) / 60 + int(m.group(7)) / 3600
    if m.group(8) == 'W':
        lon = -lon
    return lat, lon


def _navigraph_plan(data: dict, xplane_path: Path | None) -> Plan:
    """Convert a parsed Navigraph flightSelectedFlight JSON to a Plan."""
    routestring = data.get("routestring", "")
    cruise_alt = float(data.get("cruisingAltitude") or 2500)

    # Tokenise; drop routing keywords
    tokens = [t for t in routestring.split() if t.upper() not in {"DCT", "N/A"}]
    if not tokens:
        sys.exit("Navigraph plan has an empty routestring.")

    # Strip runway suffix from both ends (airports can carry e.g. EDLI-RW11)
    tokens[0] = tokens[0].split("-")[0]
    tokens[-1] = tokens[-1].split("-")[0]

    # Navigraph sometimes stores the routestring reversed relative to the title.
    # Detect via "X to Y" in the title and flip if needed.
    title = data.get("title", "")
    m_title = re.match(r'^(\w+)\s+to\s+(\w+)$', title.strip(), re.IGNORECASE)
    if m_title:
        expected_origin = m_title.group(1).upper()
        if tokens[0].upper() != expected_origin and tokens[-1].upper() == expected_origin:
            tokens = list(reversed(tokens))

    # Sort tokens into inline lat/lon vs. named idents that need lookup
    named_idents: set[str] = {t for t in tokens if _decode_dms(t) is None}

    apt_pos: dict[str, tuple[float, float, str]] = {}
    nav_candidates: dict[str, list[tuple[float, float, str]]] = {}

    if xplane_path:
        apt_path = xplane_path / APT_REL
        for ident in named_idents:
            p = _airport_position(apt_path, ident)
            if p:
                apt_pos[ident] = (p[0], p[1], "")
        unresolved = named_idents - set(apt_pos)
        if unresolved:
            nav_candidates = _build_nav_index(xplane_path, unresolved)

    # Compute route centroid from inline DMS coordinates and resolved airports so we can
    # pick the geographically nearest candidate when a navaid ident appears in multiple countries.
    ref_coords: list[tuple[float, float]] = [c for t in tokens if (c := _decode_dms(t)) is not None]
    ref_coords += [(p[0], p[1]) for p in apt_pos.values()]
    if ref_coords:
        ref_lat = sum(c[0] for c in ref_coords) / len(ref_coords)
        ref_lon = sum(c[1] for c in ref_coords) / len(ref_coords)
    else:
        ref_lat, ref_lon = 0.0, 0.0

    nav_pos: dict[str, tuple[float, float, str]] = {}
    for ident, candidates in nav_candidates.items():
        if not candidates:
            continue
        if len(candidates) == 1:
            nav_pos[ident] = candidates[0]
        else:
            nav_pos[ident] = min(candidates, key=lambda c: haversine_m(c[0], c[1], ref_lat, ref_lon))
            chosen = nav_pos[ident]
            print(
                f"[navigraph] {ident}: {len(candidates)} candidates in nav db — "
                f"picked ({chosen[0]:.2f}, {chosen[1]:.2f}), nearest to route centroid "
                f"({ref_lat:.2f}, {ref_lon:.2f})",
                file=sys.stderr,
            )

    all_pos = {**apt_pos, **nav_pos}

    waypoints: list[Waypoint] = []
    for i, tok in enumerate(tokens):
        coords = _decode_dms(tok)
        if coords:
            waypoints.append(Waypoint(name="", ident=tok, type="USER", lat=coords[0], lon=coords[1]))
        else:
            entry = all_pos.get(tok)
            if entry is None:
                print(f"[navigraph] could not resolve {tok!r} — skipped", file=sys.stderr)
                continue
            lat, lon, freq_str = entry
            is_airport = (i == 0 or i == len(tokens) - 1)
            waypoints.append(Waypoint(
                name="", ident=tok,
                type="AIRPORT" if is_airport else "VOR",
                lat=lat, lon=lon,
                freq=freq_str or None,
            ))

    if len(waypoints) < 2:
        sys.exit("Navigraph plan: fewer than 2 waypoints could be resolved.")

    return Plan(
        waypoints=waypoints,
        cruise_alt_ft=cruise_alt,
        flightplan_type=data.get("rules", "VFR"),
        cycle="Navigraph",
        created=data.get("updatedAt", ""),
    )


def read_navigraph_flight(xplane_path: Path | None) -> Plan:
    """Read the active flight plan from Navigraph Charts' Electron localStorage."""
    import shutil
    import tempfile

    # ccl_chromium_reader is not on PyPI; look for it in the project root or in $HOME
    for candidate in [PROJECT_ROOT / "ccl_chromium_reader", Path.home() / "ccl_chromium_reader"]:
        if candidate.exists():
            sys.path.insert(0, str(candidate))
            break

    try:
        from ccl_chromium_reader import ccl_chromium_localstorage
    except ImportError:
        sys.exit(
            "ccl_chromium_reader not found. Install it next to navlog.py:\n"
            "  pip install brotli\n"
            "  pip install 'ccl_simplesnappy @ git+https://github.com/cclgroupltd/ccl_simplesnappy.git'\n"
            "  git clone --depth 1 https://github.com/cclgroupltd/ccl_chromium_reader.git"
        )

    if not NAVIGRAPH_LDB.exists():
        sys.exit(f"Navigraph Charts LevelDB not found at {NAVIGRAPH_LDB}")

    # Copy to a temp dir — LevelDB is exclusively locked while Navigraph is open
    tmp = Path(tempfile.mkdtemp())
    try:
        shutil.copytree(NAVIGRAPH_LDB, tmp / "ldb")
        with ccl_chromium_localstorage.LocalStoreDb(tmp / "ldb") as ls:
            best = None
            for rec in ls.iter_all_records():
                if rec.script_key == "flightSelectedFlight" and rec.is_live and rec.value:
                    if best is None or rec.leveldb_seq_number > best.leveldb_seq_number:
                        best = rec
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if best is None:
        sys.exit("No active flight plan found in Navigraph Charts.")

    data = json.loads(best.value)
    title = data.get("title", "?")
    rules = data.get("rules", "?")
    print(f"[navigraph] {title}  ({rules})")
    return _navigraph_plan(data, xplane_path)
