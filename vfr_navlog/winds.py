"""Current real-world wind at cruise altitude along the route (Open-Meteo, no key).

Open-Meteo's forecast gives wind at 10 m and at pressure levels together with
each level's geopotential height. Per route point the wind is interpolated
(as a vector) to the cruise altitude at the current UTC hour; the points are
then vector-averaged into one wind for the navlog. Directions are true, like
METAR wind and the navlog's wind input.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from urllib.parse import urlencode

from . import net

API = "https://api.open-meteo.com/v1/forecast"
LEVELS_HPA = (1000, 925, 850, 700, 600)
MAX_POINTS = 10
FT_PER_M = 3.28084


def _uv(direction: float, speed: float) -> tuple[float, float]:
    """Wind FROM *direction* → vector the air moves along (east, north)."""
    r = math.radians(direction)
    return -speed * math.sin(r), -speed * math.cos(r)


def _dir_speed(u: float, v: float) -> tuple[int, int]:
    speed = math.hypot(u, v)
    if speed < 0.5:
        return 0, 0
    direction = math.degrees(math.atan2(-u, -v)) % 360
    return round(direction) % 360 or 360, round(speed)


def sample_points(points: list[tuple[float, float]], limit: int = MAX_POINTS) -> list[tuple[float, float]]:
    """At most *limit* points, spread evenly and always keeping both ends."""
    if len(points) <= limit:
        return list(points)
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


def wind_at(loc: dict, hour: int, alt_ft: float) -> tuple[float, float] | None:
    """(u, v) at *alt_ft* MSL for one Open-Meteo location at hourly index *hour*."""
    h = loc["hourly"]
    elev_ft = float(loc.get("elevation") or 0.0) * FT_PER_M
    profile: list[tuple[float, float, float]] = []  # (height ft, u, v)
    if h.get("wind_speed_10m") and h["wind_speed_10m"][hour] is not None:
        profile.append((elev_ft + 33, *_uv(h["wind_direction_10m"][hour], h["wind_speed_10m"][hour])))
    for p in LEVELS_HPA:
        try:
            z, d, s = h[f"geopotential_height_{p}hPa"][hour], h[f"wind_direction_{p}hPa"][hour], h[f"wind_speed_{p}hPa"][hour]
        except (KeyError, IndexError):
            continue
        if None in (z, d, s) or z * FT_PER_M < elev_ft:  # level below the ground here
            continue
        profile.append((z * FT_PER_M, *_uv(d, s)))
    if not profile:
        return None
    profile.sort()
    if alt_ft <= profile[0][0]:
        return profile[0][1:]
    for (z0, u0, v0), (z1, u1, v1) in zip(profile, profile[1:]):
        if alt_ft <= z1:
            t = (alt_ft - z0) / (z1 - z0) if z1 > z0 else 0.0
            return u0 + (u1 - u0) * t, v0 + (v1 - v0) * t
    return profile[-1][1:]


def route_wind(data: list[dict] | dict, alt_ft: float, now: datetime | None = None) -> dict:
    """Average wind at *alt_ft* over the Open-Meteo locations, for the current hour."""
    locs = data if isinstance(data, list) else [data]
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:00")
    us, vs = [], []
    for loc in locs:
        times = loc["hourly"]["time"]
        hour = times.index(stamp) if stamp in times else min(max(now.hour, 0), len(times) - 1)
        w = wind_at(loc, hour, alt_ft)
        if w:
            us.append(w[0])
            vs.append(w[1])
    if not us:
        raise ValueError("no wind data for the route")
    direction, speed = _dir_speed(sum(us) / len(us), sum(vs) / len(vs))
    return {
        "wind": f"{direction:03d}/{speed}",
        "dir": direction,
        "kt": speed,
        "alt_ft": round(alt_ft),
        "valid": now.strftime("%H:00Z"),
        "points": len(us),
        "source": "Open-Meteo forecast",
    }


def fetch_route_wind(points: list[tuple[float, float]], alt_ft: float, timeout: float = 10.0) -> dict:
    pts = sample_points(points)
    if not pts:
        raise ValueError("no route points")
    hourly = ["wind_speed_10m", "wind_direction_10m"]
    for p in LEVELS_HPA:
        hourly += [f"wind_speed_{p}hPa", f"wind_direction_{p}hPa", f"geopotential_height_{p}hPa"]
    query = urlencode({
        "latitude": ",".join(f"{lat:.3f}" for lat, _ in pts),
        "longitude": ",".join(f"{lon:.3f}" for _, lon in pts),
        "hourly": ",".join(hourly),
        "wind_speed_unit": "kn",
        "timezone": "GMT",
        "forecast_days": 1,
    })
    body = net.fetch(f"{API}?{query}", timeout=timeout)
    if body is None:
        raise ConnectionError("Open-Meteo not reachable")
    return route_wind(json.loads(body), alt_ft)
