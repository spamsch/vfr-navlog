"""VATSIM data-feed client and FIR / en-route radar helpers."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from .config import VATSIM_URL
from .model import VatsimSnapshot, Waypoint
from .net import fetch


def _normalize_freq(raw: str) -> str:
    """VATSIM returns frequencies as strings like '118.300' already; just clean."""
    if not raw:
        return ""
    return raw.strip()


def fetch_vatsim(icaos: list[str], timeout: float = 6.0) -> VatsimSnapshot | None:
    """Single GET against the VATSIM data feed; pick out GND/TWR/ATIS/DEL/APP for each ICAO."""
    body = fetch(VATSIM_URL, timeout=timeout)
    if body is None:
        print("[vatsim] fetch failed: network error", file=sys.stderr)
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        print(f"[vatsim] fetch failed: {e}", file=sys.stderr)
        return None

    update_time = payload.get("general", {}).get("update_timestamp", "")
    controllers = payload.get("controllers", []) or []
    atis_stations = payload.get("atis", []) or []

    role_for_suffix = {
        "GND": "ground",
        "TWR": "tower",
        "ATIS": "atis",
        "DEL": "delivery",
        "APP": "approach",
        "CTR": "radar",
    }

    out: dict[str, dict[str, str]] = {icao.upper(): {} for icao in icaos if icao}
    atis_out: dict[str, list[str]] = {icao.upper(): [] for icao in icaos if icao}
    for entry in controllers + atis_stations:
        callsign = (entry.get("callsign") or "").upper()
        if "_" not in callsign:
            continue
        icao, _, rest = callsign.partition("_")
        if icao not in out:
            continue
        # callsigns can be EDDG_TWR, EDDG_N_TWR (split sector), EDDG_1_GND, EDDG_ATIS
        suffix = rest.rsplit("_", 1)[-1]
        role = role_for_suffix.get(suffix)
        if not role:
            continue
        freq = _normalize_freq(entry.get("frequency", ""))
        if not freq:
            continue
        out[icao].setdefault(role, freq)
        if role == "atis":
            raw_atis = entry.get("text_atis") or []
            if isinstance(raw_atis, list):
                atis_out[icao] = [str(line).strip() for line in raw_atis if line]
            elif isinstance(raw_atis, str):
                atis_out[icao] = [raw_atis.strip()]

    return VatsimSnapshot(
        fetched_at=datetime.now(timezone.utc).strftime("%H:%MZ"),
        update_time=update_time,
        frequencies=out,
        atis_text=atis_out,
    )


# ------------------------- FIR / en-route radar helpers -------------------------

_FIR_NAMES: dict[str, str] = {
    "EDGG": "Langen Radar",
    "EDWW": "Bremen Radar",
    "EDMM": "München Radar",
    "EDYY": "Berlin Radar",
}


def _german_firs_for_route(waypoints: list[Waypoint]) -> list[str]:
    """Return VATSIM FIR ICAO prefix(es) for waypoints in German airspace.

    Uses a rough latitude split: ≥53.5° → Bremen, ≤48° → Munich, else Langen.
    """
    german_wps = [wp for wp in waypoints if (wp.ident or "").upper().startswith("ED")]
    if not german_wps:
        return []
    firs: set[str] = set()
    for wp in german_wps:
        if wp.lat >= 53.5:
            firs.add("EDWW")
        elif wp.lat <= 48.0:
            firs.add("EDMM")
        else:
            firs.add("EDGG")
    return sorted(firs)


def _find_radar_online(
    vatsim: "VatsimSnapshot | None",
    fir_icaos: list[str],
) -> tuple[str, str] | None:
    """Return (station_name, frequency) for the first online CTR station, else None."""
    if not vatsim or not fir_icaos:
        return None
    for fir in fir_icaos:
        freq = vatsim.frequencies.get(fir.upper(), {}).get("radar", "")
        if freq:
            return _FIR_NAMES.get(fir.upper(), f"{fir} Radar"), freq
    return None
