"""X-Plane nav-data parsers: apt.dat, earth_nav.dat, earth_fix.dat."""
from __future__ import annotations

from pathlib import Path

from .config import APT_REL, FIX_REL, NAV_FALLBACK_REL, NAV_REL, SURFACE_NAMES
from .geo import haversine_m
from .model import AirportInfo, IlsLoc, Plan, Runway, VorStation

# apt.dat comm-frequency rows: legacy 50-series (freq in 10 kHz units) and the
# 8.33-kHz-capable 1050-series (freq in kHz). 51/1051 (UNICOM) and 56/1056
# (departure) are not rendered anywhere, so they are not collected.
_FREQ_ROLES = {
    "50": ("atis", 100), "52": ("delivery", 100), "53": ("ground", 100),
    "54": ("tower", 100), "55": ("approach", 100),
    "1050": ("atis", 1000), "1052": ("delivery", 1000), "1053": ("ground", 1000),
    "1054": ("tower", 1000), "1055": ("approach", 1000),
}


def scan_airports(apt_path: Path, icaos) -> dict[str, AirportInfo]:
    """Single front-to-back pass over apt.dat answering for a *set* of ICAOs.

    Returns {ICAO: AirportInfo} for each requested code found. Each AirportInfo
    carries its runways, its published comm frequencies (.frequencies, the
    real-world fallback when no VATSIM station is online), and an averaged
    runway-endpoint position (.lat/.lon).
    Replaces the two duplicate state machines (parse_airport / _airport_position);
    X-Plane's global apt.dat is hundreds of MB, so we walk it once and stop as
    soon as every requested airport has been read.
    """
    wanted = {i.upper() for i in icaos if i}
    found: dict[str, AirportInfo] = {}
    if not apt_path.exists() or not wanted:
        return found

    remaining = set(wanted)
    info: AirportInfo | None = None
    lats: list[float] = []
    lons: list[float] = []

    def _finish(cur: AirportInfo | None) -> None:
        if cur is not None and lats:
            cur.lat = sum(lats) / len(lats)
            cur.lon = sum(lons) / len(lons)

    try:
        with open(apt_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if not parts:
                    continue
                row = parts[0]
                if row == "1" and len(parts) >= 5:
                    # New airport header: close out the previous target block.
                    if info is not None:
                        _finish(info)
                        remaining.discard(info.icao)
                        if not remaining:
                            info = None
                            break
                    icao_upper = parts[4].upper()
                    if icao_upper in wanted and icao_upper not in found:
                        info = AirportInfo(
                            icao=icao_upper,
                            name=" ".join(parts[5:]),
                            elevation_ft=float(parts[1]),
                        )
                        found[icao_upper] = info
                        lats = []
                        lons = []
                    else:
                        info = None
                elif info is not None:
                    if row == "1302" and len(parts) >= 3:
                        key, val = parts[1], " ".join(parts[2:])
                        if key == "city":
                            info.city = val
                        elif key == "transition_alt":
                            info.transition_alt = val
                        elif key == "transition_level":
                            info.transition_level = val
                        elif key == "iata_code":
                            info.iata = val
                    elif row in _FREQ_ROLES and len(parts) >= 2:
                        role, div = _FREQ_ROLES[row]
                        try:
                            raw = int(parts[1])
                        except ValueError:
                            continue
                        mhz = f"{raw / 100:.2f}" if div == 100 else f"{raw / 1000:.3f}"
                        # 1050-series carries 8.33 kHz precision — let it win
                        # over a legacy row; legacy only fills gaps.
                        if div == 1000 or role not in info.frequencies:
                            info.frequencies[role] = mhz
                    elif row == "100" and len(parts) >= 26:
                        # 100 width surface ... end1_ident lat lon ... end2_ident lat lon ...
                        try:
                            width = float(parts[1])
                            surface = parts[2]
                            ident_a = parts[8]
                            lat_a = float(parts[9])
                            lon_a = float(parts[10])
                            ident_b = parts[17]
                            lat_b = float(parts[18])
                            lon_b = float(parts[19])
                            length_m = haversine_m(lat_a, lon_a, lat_b, lon_b)
                            info.runways.append(Runway(
                                ident_a=ident_a, ident_b=ident_b,
                                surface=SURFACE_NAMES.get(surface, surface),
                                width_m=width, length_m=length_m,
                            ))
                            lats += [lat_a, lat_b]
                            lons += [lon_a, lon_b]
                        except (ValueError, IndexError):
                            continue
        _finish(info)  # close the final block if we hit EOF mid-airport
    except OSError:
        return found
    return found


def parse_airport(apt_path: Path, icao: str) -> AirportInfo | None:
    """AirportInfo for a single ICAO (thin wrapper over the single-pass scan)."""
    return scan_airports(apt_path, {icao}).get(icao.upper())


def airport_positions(apt_path: Path, icaos) -> dict[str, tuple[float, float]]:
    """Averaged runway-endpoint position per ICAO, in one pass."""
    return {
        icao: (info.lat, info.lon)
        for icao, info in scan_airports(apt_path, icaos).items()
        if info.lat is not None and info.lon is not None
    }


def parse_ils_locs(nav_path: Path, icao: str) -> list[IlsLoc]:
    if not nav_path.exists():
        return []
    icao_upper = icao.upper()
    out: list[IlsLoc] = []
    try:
        with open(nav_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if icao_upper not in line:
                    continue
                parts = line.split()
                if len(parts) < 11:
                    continue
                if parts[0] not in {"4", "5"}:  # ILS LOC or LOC-only
                    continue
                if parts[8].upper() != icao_upper:
                    continue
                try:
                    freq_raw = int(parts[4])
                except ValueError:
                    continue
                runway = parts[10]
                ident = parts[7]
                type_desc = " ".join(parts[11:]) if len(parts) > 11 else ""
                out.append(IlsLoc(
                    runway=runway, ident=ident,
                    freq_mhz=freq_raw / 100.0,
                    type_desc=type_desc,
                ))
    except OSError:
        return []
    return out


def load_airport_infos(plan: Plan, xplane_path: Path) -> tuple[AirportInfo | None, AirportInfo | None]:
    """(departure, destination) AirportInfo from one apt.dat pass.

    The departure info exists for its published comm frequencies (the standard-
    frequency fallback in the navlog's frequency block); only the destination
    gets the ILS join and the briefing page.
    """
    if not plan.waypoints:
        return None, None
    dep = plan.waypoints[0]
    dest = plan.waypoints[-1]
    wanted = {wp.ident for wp in (dep, dest) if wp.type.upper() == "AIRPORT"}
    found = scan_airports(xplane_path / APT_REL, wanted)

    dep_info = found.get(dep.ident.upper()) if dep.type.upper() == "AIRPORT" else None

    dest_info: AirportInfo | None = None
    if dest.type.upper() == "AIRPORT":
        dest_info = found.get(dest.ident.upper())
        if dest_info is None:
            dest_info = AirportInfo(icao=dest.ident.upper(), name=dest.name or dest.ident,
                                    elevation_ft=dest.alt_ft or 0.0)
        nav_path = xplane_path / NAV_REL
        if not nav_path.exists():
            nav_path = xplane_path / NAV_FALLBACK_REL
        dest_info.ils_locs = parse_ils_locs(nav_path, dest_info.icao)
    return dep_info, dest_info


def load_destination_info(plan: Plan, xplane_path: Path) -> AirportInfo | None:
    return load_airport_infos(plan, xplane_path)[1]


def load_vors(xplane_path: Path) -> list[VorStation]:
    """Every VOR in earth_nav.dat, keeping the two fields _build_nav_index drops.

    earth_nav.dat type-3 (VOR) rows (X-Plane 1150 layout):
        3  lat  lon  elev  freq  range  slaved_var  ident  terminal  region  name...
    freq is in 10s of kHz (10850 → 108.50 MHz); range is the published reception
    range in nm; slaved_var is the magnetic variation the station was calibrated
    to (East positive) — a radial is measured against *this*, not the plan magvar.
    The two tokens after the ident are the terminal-area id ("ENRT" for enroute)
    and the ICAO region; the human name follows.

    Co-located DME is detected from type-12 (DME of a VOR/ILS) and type-13
    (standalone DME) rows whose ident+freq match a VOR. Standalone DME with no
    matching VOR produces no station (there is no OBS to set).

    Returns [] if the file is absent or unreadable — the caller degrades, never
    fails the run.
    """
    nav_path = xplane_path / NAV_REL
    if not nav_path.exists():
        nav_path = xplane_path / NAV_FALLBACK_REL
    if not nav_path.exists():
        return []

    vors: list[VorStation] = []
    dme_keys: set[tuple[str, str]] = set()
    try:
        with open(nav_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 8 or parts[0] not in {"3", "12", "13"}:
                    continue
                try:
                    raw_freq = int(parts[4])
                except ValueError:
                    continue
                freq_str = f"{raw_freq / 100:.2f}"
                ident = parts[7]
                if parts[0] in {"12", "13"}:
                    dme_keys.add((ident, freq_str))
                    continue
                # type 3 — a VOR
                if len(parts) < 9:
                    continue
                try:
                    lat = float(parts[1])
                    lon = float(parts[2])
                    range_nm = float(parts[5])
                    slaved_var = float(parts[6])
                except ValueError:
                    continue
                # Skip the terminal-area id and ICAO region to get the plain name.
                name = " ".join(parts[10:]) if len(parts) >= 11 else " ".join(parts[8:])
                # Pure TACANs are military: a civil VOR receiver gets no azimuth
                # from them, so they cannot serve as a radial fix. VORTACs keep
                # their VOR part and stay usable.
                upper_name = name.upper()
                if "TACAN" in upper_name and "VORTAC" not in upper_name:
                    continue
                vors.append(VorStation(
                    ident=ident,
                    name=name,
                    freq=freq_str,
                    lat=lat, lon=lon,
                    range_nm=range_nm,
                    slaved_var=slaved_var,
                ))
    except OSError:
        return []

    for v in vors:
        if (v.ident, v.freq) in dme_keys:
            v.has_dme = True
    return vors


def _build_nav_index(xplane_path: Path, idents: set[str]) -> dict[str, list[tuple[float, float, str]]]:
    """Resolve navaid/fix idents to all (lat, lon, freq_str) candidates from X-Plane's nav and fix databases.

    Returns a list of candidates per ident so the caller can pick the geographically nearest one
    when the same ident exists in multiple countries (e.g. VOR WLD).
    """
    result: dict[str, list[tuple[float, float, str]]] = {ident: [] for ident in idents}
    if not idents:
        return result

    # earth_nav.dat — VOR (type 3, freq in 10s of kHz → MHz) and NDB (type 2, freq in kHz)
    nav_path = xplane_path / NAV_REL
    if not nav_path.exists():
        nav_path = xplane_path / NAV_FALLBACK_REL
    if nav_path.exists():
        try:
            with open(nav_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 9 or parts[0] not in {"2", "3"}:
                        continue
                    ident = parts[7]
                    if ident in idents:
                        try:
                            lat, lon = float(parts[1]), float(parts[2])
                            raw_freq = int(parts[4])
                            if parts[0] == "3":
                                freq_str = f"{raw_freq / 100:.2f}"
                            else:
                                freq_str = str(raw_freq)
                            result[ident].append((lat, lon, freq_str))
                        except ValueError:
                            pass
        except OSError:
            pass

    # earth_fix.dat — named waypoints/intersections (no frequency)
    remaining = {ident for ident, candidates in result.items() if not candidates}
    if remaining:
        fix_path = xplane_path / FIX_REL
        if fix_path.exists():
            try:
                with open(fix_path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) < 3:
                            continue
                        ident = parts[2]
                        if ident in remaining:
                            try:
                                result[ident].append((float(parts[0]), float(parts[1]), ""))
                            except ValueError:
                                pass
            except OSError:
                pass

    return result
