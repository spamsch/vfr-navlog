"""Exercise cli.run() end-to-end with all network calls stubbed.

Covers the parallel-fetch path (ThreadPoolExecutor over vatsim + METAR/TAF)
and the weather/field-weather assembly without touching the network.
"""
from pathlib import Path

import pypdf

import vfr_navlog.cli as cli
from vfr_navlog.config import PROJECT_ROOT
from vfr_navlog.model import RunConfig, VatsimSnapshot

FIXTURES = Path(__file__).parent / "fixtures"


def _config(out: Path) -> RunConfig:
    return RunConfig(
        navigraph=False,
        plan_path=FIXTURES / "sample.lnmpln",
        aircraft_path=PROJECT_ROOT / "aircraft_c172.json",
        wind=(0.0, 0.0),
        wind_was_default=True,
        magvar=3.0,
        registration=None,
        cruise_alt_ft=None,
        alt_profile=[],
        output=out,
        xplane_path=None,
        vatsim=True,
        vor_info=False,
        with_dfs_charts=False,
        call_tower_nm=10.0,
        fms=False,
        fpl_fields=None,
    )


def test_run_parallel_fetch_stubbed(tmp_path, monkeypatch):
    snapshot = VatsimSnapshot(
        fetched_at="10:00Z", update_time="",
        frequencies={"EDDV": {"tower": "118.000", "ground": "121.900"}, "EDLI": {}},
        atis_text={},
    )
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(cli, "fetch_vatsim", lambda icaos, timeout=6.0: snapshot)
    monkeypatch.setattr(cli, "fetch_metar",
                        lambda icao, timeout=6.0: f"{icao} 271012KT 9999 FEW030 14/09 Q1018")
    monkeypatch.setattr(cli, "fetch_taf", lambda icao, timeout=6.0: f"TAF {icao} 2712/2812 27010KT")

    out = tmp_path / "navlog.pdf"
    cli.run(_config(out))

    assert out.exists()
    reader = pypdf.PdfReader(str(out))
    # navlog page + 2 phraseology + weather briefing page
    assert len(reader.pages) >= 4
    text = "\n".join(p.extract_text() for p in reader.pages)
    assert "Wetterbriefing" in text


def test_run_no_vatsim_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: None)
    cfg = _config(tmp_path / "n2.pdf")
    cfg.vatsim = False
    cli.run(cfg)
    assert (tmp_path / "n2.pdf").exists()
