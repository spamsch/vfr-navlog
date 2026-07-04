from pathlib import Path

import pytest

from vfr_navlog.xplane import airport_positions, parse_airport, scan_airports

FIXTURES = Path(__file__).parent / "fixtures"
APT = FIXTURES / "mini_apt.dat"


def test_scan_airports_single_pass_multiple_icaos():
    got = scan_airports(APT, {"EDDV", "EDLI"})
    assert set(got) == {"EDDV", "EDLI"}

    eddv = got["EDDV"]
    assert eddv.name == "Hannover Test"
    assert eddv.elevation_ft == 183
    assert eddv.city == "Hannover"
    assert eddv.transition_alt == "5000"
    assert eddv.iata == "HAJ"
    assert len(eddv.runways) == 1
    assert eddv.runways[0].ident_a == "09L"
    assert eddv.runways[0].ident_b == "27R"
    assert eddv.runways[0].surface == "Asphalt"
    # position = mean of the two runway endpoints
    assert eddv.lat == pytest.approx((52.46 + 52.47) / 2)
    assert eddv.lon == pytest.approx((9.68 + 9.69) / 2)

    edli = got["EDLI"]
    assert edli.city == "Bielefeld"
    assert edli.runways[0].surface == "Gras"


def test_scan_airports_missing_returns_empty():
    assert scan_airports(APT, {"ZZZZ"}) == {}
    assert scan_airports(Path("/nonexistent"), {"EDDV"}) == {}


def test_parse_airport_wrapper():
    info = parse_airport(APT, "eddv")
    assert info is not None and info.icao == "EDDV"
    assert parse_airport(APT, "ZZZZ") is None


def test_airport_positions():
    pos = airport_positions(APT, {"EDDV", "EDLI"})
    assert pos["EDDV"] == pytest.approx(((52.46 + 52.47) / 2, (9.68 + 9.69) / 2))
    assert "EDLI" in pos


def test_scan_airports_parses_standard_frequencies():
    got = scan_airports(APT, {"EDDV", "EDLI"})

    # EDDV mixes 1050-series (kHz) and a legacy 55 row (10 kHz units).
    assert got["EDDV"].frequencies == {
        "atis": "127.970",
        "ground": "121.775",
        "tower": "129.805",
        "approach": "119.18",
    }
    # EDLI has both a legacy and a 1050-series tower row — the 8.33-capable
    # 1050 row wins regardless of file order.
    assert got["EDLI"].frequencies["tower"] == "118.355"
