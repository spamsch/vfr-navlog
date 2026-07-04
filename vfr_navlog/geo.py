"""Great-circle nav math and the single haversine implementation."""
from __future__ import annotations

import math

# Earth radius. The two callers want different units; both derive from the same
# central-angle core so there is one haversine, not two.
EARTH_RADIUS_NM = 3440.065
EARTH_RADIUS_M = 6371000.0


def _central_angle(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine central angle (radians) between two lat/lon points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * math.asin(math.sqrt(a))


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return EARTH_RADIUS_NM * _central_angle(lat1, lon1, lat2, lon2)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return EARTH_RADIUS_M * _central_angle(lat1, lon1, lat2, lon2)


def great_circle(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    """Returns (initial_true_course_deg, distance_nm) between two points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)

    # initial bearing
    y = math.sin(dlon) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    tc = (math.degrees(math.atan2(y, x)) + 360) % 360

    dist_nm = haversine_nm(lat1, lon1, lat2, lon2)
    return tc, dist_nm


def apply_wind(tc_deg: float, tas_kt: float, wind_from_deg: float, wind_kt: float) -> tuple[float, float, float]:
    """Returns (wca_deg, magnetic_heading_no_var, gs_kt). Caller adds magvar."""
    # wind correction angle: sin(WCA) = (W/TAS) * sin(wind_from - TC)
    rel = math.radians(wind_from_deg - tc_deg)
    if tas_kt <= 0:
        return 0.0, tc_deg, 0.0
    sin_wca = (wind_kt / tas_kt) * math.sin(rel)
    sin_wca = max(-1.0, min(1.0, sin_wca))
    wca = math.degrees(math.asin(sin_wca))
    th = tc_deg + wca
    # ground speed
    gs = math.sqrt(
        tas_kt ** 2 + wind_kt ** 2 - 2 * tas_kt * wind_kt * math.cos(rel - math.radians(wca))
    )
    return wca, th, gs
