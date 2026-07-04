"""Constants, .env loading, and default paths (X-Plane, Navigraph)."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

# Repository root: the directory that holds navlog.py, aircraft_*.json, .env and
# the vendored ccl_chromium_reader/. config.py lives one level below it.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

VATSIM_URL = "https://data.vatsim.net/v3/vatsim-data.json"
VATSIM_METAR_URL = "https://metar.vatsim.net/metar.php?id={icao}"
VATSIM_TAF_URL  = "https://metar.vatsim.net/taf.php?id={icao}"
VATSIM_UA = "navlog.py/1.0 (+local VFR planning script)"

# VATSIM convention: tune 122.800 ("UNICOM") whenever no ATC station is online
# in the airspace you're operating in.
UNICOM_FREQ = "122.800"

# Default macOS Steam install. Override via --xplane.
DEFAULT_XPLANE = Path.home() / "Library/Application Support/Steam/steamapps/common/X-Plane 12"
NAV_REL = "Custom Data/earth_nav.dat"
NAV_FALLBACK_REL = "Resources/default data/earth_nav.dat"
FIX_REL = "Resources/default data/earth_fix.dat"
APT_REL = "Global Scenery/Global Airports/Earth nav data/apt.dat"

NAVIGRAPH_LDB = Path.home() / "Library/Application Support/Navigraph Charts/Local Storage/leveldb"

SURFACE_NAMES = {
    "1": "Asphalt", "2": "Beton", "3": "Gras", "4": "Sand", "5": "Schotter",
    "12": "Trocken", "13": "Wasser", "14": "Schnee/Eis", "15": "Transparent",
}


def _load_env(path: Path) -> dict[str, str]:
    """Parse a .env file (KEY=VALUE). Ignores blank lines and # comments."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def _smart_output(env: dict[str, str], dep_icao: str, dest_icao: str, ac_type: str) -> Path:
    base = Path(env.get("NAVLOG_OUTPUT_DIR", ".")).expanduser()
    subdir = base / f"{dep_icao.upper()}-{dest_icao.upper()}"
    date_slug = datetime.now().strftime("%Y-%m-%d")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ac_type).strip("-").lower()
    filename = f"navlog_{date_slug}_{slug}.pdf" if slug else f"navlog_{date_slug}.pdf"
    return subdir / filename
