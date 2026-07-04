"""X-Plane nav-data parsers: apt.dat, earth_nav.dat, earth_fix.dat."""
from __future__ import annotations

from pathlib import Path

from .config import APT_REL, FIX_REL, NAV_FALLBACK_REL, NAV_REL, SURFACE_NAMES
from .geo import haversine_m
from .model import AirportInfo, IlsLoc, Plan, Runway


def parse_airport(apt_path: Path, icao: str) -> AirportInfo | None:
    if not apt_path.exists():
        return None
    icao_upper = icao.upper()
    info: AirportInfo | None = None
    in_target = False
    try:
        with open(apt_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                parts = line.split()
                if not parts:
                    continue
                row = parts[0]
                if row == "1" and len(parts) >= 5:
                    # New airport header. Finish previous if it was ours.
                    if in_target:
                        break
                    if parts[4].upper() == icao_upper:
                        info = AirportInfo(
                            icao=icao_upper,
                            name=" ".join(parts[5:]),
                            elevation_ft=float(parts[1]),
                        )
                        in_target = True
                    else:
                        in_target = False
                elif in_target and info is not None:
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
                        except (ValueError, IndexError):
                            continue
    except OSError:
        return None
    return info


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


def load_destination_info(plan: Plan, xplane_path: Path) -> AirportInfo | None:
    if not plan.waypoints:
        return None
    dest = plan.waypoints[-1]
    if dest.type.upper() != "AIRPORT":
        return None
    apt_path = xplane_path / APT_REL
    info = parse_airport(apt_path, dest.ident)
    if info is None:
        info = AirportInfo(icao=dest.ident.upper(), name=dest.name or dest.ident,
                           elevation_ft=dest.alt_ft or 0.0)

    nav_path = xplane_path / NAV_REL
    if not nav_path.exists():
        nav_path = xplane_path / NAV_FALLBACK_REL
    info.ils_locs = parse_ils_locs(nav_path, info.icao)
    return info


def _airport_position(apt_path: Path, icao: str) -> tuple[float, float] | None:
    """Approximate airport lat/lon by averaging its runway endpoints from apt.dat."""
    if not apt_path.exists():
        return None
    icao_upper = icao.upper()
    in_target = False
    lats: list[float] = []
    lons: list[float] = []
    try:
        with open(apt_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if not parts:
                    continue
                if parts[0] == "1" and len(parts) >= 5:
                    if in_target:
                        break
                    in_target = (parts[4].upper() == icao_upper)
                elif in_target and parts[0] == "100" and len(parts) >= 20:
                    try:
                        lats += [float(parts[9]), float(parts[18])]
                        lons += [float(parts[10]), float(parts[19])]
                    except (ValueError, IndexError):
                        pass
    except OSError:
        return None
    if not lats:
        return None
    return sum(lats) / len(lats), sum(lons) / len(lons)


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
