import shutil
from pathlib import Path

import pytest

from vfr_navlog import api
from vfr_navlog.config import APT_REL, NAV_REL

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def xplane(tmp_path: Path) -> Path:
    for src, rel in (("mini_apt.dat", APT_REL), ("mini_nav.dat", NAV_REL)):
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / src, dest)
    return tmp_path


@pytest.fixture(autouse=True)
def _offline_cycle(monkeypatch):
    # resolve() probes the OFM tile server for the AIRAC cycle; keep tests offline.
    import vfr_navlog.baselayers as bl
    monkeypatch.setattr(bl, "_resolve_cycle", lambda *a, **k: "2610")


def test_aircraft_lists_profiles():
    resp = api.handle({"cmd": "aircraft"})
    assert resp["ok"]
    files = {a["file"] for a in resp["aircraft"]}
    assert {"aircraft_c172.json", "aircraft_sr22.json"} <= files


def test_resolve_returns_waypoints_and_legs(xplane):
    resp = api.handle({
        "cmd": "resolve", "route": "EDDV 520000N0090000E DLE EDLI",
        "aircraft": "aircraft_c172.json", "cruise_alt_ft": 3000, "wind": "270/10",
        "xplane": str(xplane),
    })
    assert resp["ok"], resp
    assert [w["ident"] for w in resp["waypoints"]] == ["EDDV", "520000N0090000E", "DLE", "EDLI"]
    assert len(resp["legs"]) == 3
    leg = resp["legs"][0]
    assert leg["from"] == "EDDV" and leg["distance_nm"] > 0 and leg["gs_kt"] > 0
    assert resp["ofm_cycle"] == "2610"
    assert resp["wind"] == [270.0, 10.0]
    # mini_nav.dat has HLZ (52.5N 9.0E) and DLE (51.8N 8.2E) near this route.
    idents = [v["ident"] for v in resp["vors"]]
    assert "DLE" in idents and "HLZ" in idents
    dle = next(v for v in resp["vors"] if v["ident"] == "DLE")
    assert dle["freq"] == "115.20" and dle["var"] == -2.0 and dle["dme"] is True
    assert dle["route_dist_nm"] < 1.0  # DLE is a route waypoint


def test_resolve_keeps_cruise_alt_without_hemispheric_rule(xplane):
    base = {"cmd": "resolve", "route": "EDDV 520000N0090000E DLE EDLI",
            "aircraft": "aircraft_c172.json", "cruise_alt_ft": 3200, "xplane": str(xplane)}
    plain = api.handle({**base, "hemispheric": False})
    assert {leg["alt_ft"] for leg in plain["legs"]} == {3200}
    ruled = api.handle(base)
    assert all(leg["alt_ft"] in (3500, 4500) for leg in ruled["legs"])


def test_field_combines_vatsim_and_weather(monkeypatch):
    from vfr_navlog.model import VatsimSnapshot

    snap = VatsimSnapshot(
        fetched_at="10:00Z", update_time="",
        frequencies={"EDDK": {"tower": "124.980", "atis": "132.130"}, "EDGG": {"radar": "127.925"}},
        atis_text={"EDDK": ["KOELN INFO B", "RWY 14L"]},
    )
    monkeypatch.setattr(api, "fetch_vatsim", lambda icaos, timeout=6.0: snap)
    monkeypatch.setattr(api, "fetch_metar", lambda icao, timeout=6.0: "EDDK 271020Z 15008KT 9999 FEW040 28/10 Q1016")
    monkeypatch.setattr(api, "fetch_taf", lambda icao, timeout=6.0: None)
    resp = api.handle({"cmd": "field", "icao": "eddk", "firs": ["EDGG"]})
    assert resp["ok"], resp
    assert resp["freqs"]["tower"] == "124.980"
    assert resp["atis"] == ["KOELN INFO B", "RWY 14L"]
    assert resp["radar"] == {"name": "Langen Radar", "freq": "127.925"}
    assert resp["qnh_hpa"] == 1016 and resp["vfr_status"] == "VFR"
    assert resp["taf"] is None


def test_field_offline_fails_soft(monkeypatch):
    monkeypatch.setattr(api, "fetch_vatsim", lambda icaos, timeout=6.0: None)
    monkeypatch.setattr(api, "fetch_metar", lambda icao, timeout=6.0: None)
    monkeypatch.setattr(api, "fetch_taf", lambda icao, timeout=6.0: None)
    resp = api.handle({"cmd": "field", "icao": "EDDK"})
    assert resp["ok"] and resp["freqs"] == {} and resp["atis"] == [] and resp["metar"] is None


def test_errors_come_back_as_json(xplane):
    resp = api.handle({"cmd": "resolve", "route": "XXXX YYYY", "aircraft": "aircraft_c172.json",
                       "xplane": str(xplane)})
    assert resp["ok"] is False and resp["error"]
    assert api.handle({"cmd": "nope"})["ok"] is False
