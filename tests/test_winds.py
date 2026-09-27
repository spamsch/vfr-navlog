"""Winds aloft from Open-Meteo: interpolation to cruise altitude and route average."""
from datetime import datetime, timezone

import pytest

from vfr_navlog import api, winds

NOW = datetime(2026, 9, 27, 14, 20, tzinfo=timezone.utc)


def _loc(elev_m=100.0, w10=(180, 5), w925=(270, 20), z925=800.0, w850=(270, 30), z850=1500.0):
    """Two hours of data; hour 14 carries the values under test, hour 13 is noise."""
    def two(v):
        return [None, v]
    hourly = {
        "time": ["2026-09-27T13:00", "2026-09-27T14:00"],
        "wind_direction_10m": two(w10[0]), "wind_speed_10m": two(w10[1]),
        "wind_direction_925hPa": two(w925[0]), "wind_speed_925hPa": two(w925[1]), "geopotential_height_925hPa": two(z925),
        "wind_direction_850hPa": two(w850[0]), "wind_speed_850hPa": two(w850[1]), "geopotential_height_850hPa": two(z850),
    }
    return {"elevation": elev_m, "hourly": hourly}


def test_interpolates_between_levels_at_the_current_hour():
    # 925 hPa at 2625 ft, 850 hPa at 4921 ft: 3773 ft is halfway → 270/25.
    mid = (800 + 1500) / 2 * winds.FT_PER_M
    assert winds.route_wind(_loc(), mid, NOW)["wind"] == "270/25"
    assert winds.route_wind(_loc(), 20000, NOW)["wind"] == "270/30"  # above the top level: top level


def test_skips_levels_below_the_ground():
    # Terrain at 1000 m: 925 hPa (800 m) is underground, so the wind comes from 10 m and 850 hPa.
    r = winds.route_wind(_loc(elev_m=1000.0, w10=(270, 10)), 1000 * winds.FT_PER_M + 33, NOW)
    assert r["wind"] == "270/10"


def test_route_average_is_a_vector_average():
    north, east = _loc(w925=(360, 20), w850=(360, 20)), _loc(w925=(90, 20), w850=(90, 20))
    r = winds.route_wind([north, east], 3500, NOW)
    assert (r["dir"], r["kt"], r["points"]) == (45, 14, 2)


def test_sample_points_keeps_ends():
    pts = [(float(i), 0.0) for i in range(25)]
    got = winds.sample_points(pts, 10)
    assert len(got) == 10 and got[0] == pts[0] and got[-1] == pts[-1]


def test_bridge_command(monkeypatch):
    seen = {}

    def fake_fetch(url, timeout=6.0, **kw):
        seen["url"] = url
        import json
        return json.dumps([_loc(), _loc()])

    monkeypatch.setattr(winds.net, "fetch", fake_fetch)
    r = api.handle({"cmd": "wind", "points": [[51.0, 7.0], [51.5, 7.5]], "alt_ft": 20000})
    assert r["ok"] and r["kt"] == 30 and r["points"] == 2
    assert "latitude=51.000%2C51.500" in seen["url"] and "wind_speed_unit=kn" in seen["url"]

    monkeypatch.setattr(winds.net, "fetch", lambda *a, **k: None)
    r = api.handle({"cmd": "wind", "points": [[51.0, 7.0]], "alt_ft": 3000})
    assert not r["ok"] and "Open-Meteo" in r["error"]


@pytest.mark.parametrize("u,v,expected", [(0.0, -10.0, (360, 10)), (0.1, 0.1, (0, 0))])
def test_dir_speed(u, v, expected):
    assert winds._dir_speed(u, v) == expected
