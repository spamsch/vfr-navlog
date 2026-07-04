#!/usr/bin/env python3
"""
LBA-inspired VFR navlog generator.

Reads a Little Navmap .lnmpln plan plus an aircraft JSON config, applies
a single wind aloft and a magnetic variation, and writes an A4-landscape
PDF you can print and clip to your kneeboard.

Usage:
    python3 navlog.py \
        --plan "/path/to/plan.lnmpln" \
        --aircraft aircraft_sr22.json \
        --wind 270/15 \
        --magvar 2.5E \
        --output navlog.pdf
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from fpdf import FPDF

# --- Phase 1 facade: names below now live in the vfr_navlog package. This file
#     re-exports them so `from navlog import X` and the code still in this module
#     keep working until the split completes. ---
from vfr_navlog.config import (  # noqa: E402,F401
    APT_REL,
    DEFAULT_XPLANE,
    FIX_REL,
    NAV_FALLBACK_REL,
    NAV_REL,
    NAVIGRAPH_LDB,
    SURFACE_NAMES,
    UNICOM_FREQ,
    VATSIM_METAR_URL,
    VATSIM_TAF_URL,
    VATSIM_UA,
    VATSIM_URL,
    _load_env,
    _smart_output,
)
from vfr_navlog.geo import apply_wind, great_circle  # noqa: E402,F401
from vfr_navlog.geo import haversine_m as _haversine_m  # noqa: E402,F401
from vfr_navlog.model import (  # noqa: E402,F401
    AirportInfo,
    FieldWx,
    IlsLoc,
    Leg,
    ParsedMetar,
    Plan,
    Runway,
    VatsimSnapshot,
    Waypoint,
    WeatherBriefing,
)
from vfr_navlog.legs import (  # noqa: E402,F401
    _effective_leg_alt,
    apply_hemispheric_rule,
    compute_legs,
    find_call_marker,
    hemispheric_alt,
)
from vfr_navlog.lnmpln import parse_lnmpln, parse_magvar, parse_wind  # noqa: E402,F401
from vfr_navlog.navigraph import (  # noqa: E402,F401
    _airport_position,
    _build_nav_index,
    _decode_dms,
    _navigraph_plan,
    read_navigraph_flight,
)
from vfr_navlog.xplane import (  # noqa: E402,F401
    load_destination_info,
    parse_airport,
    parse_ils_locs,
)
from vfr_navlog.vatsim import (  # noqa: E402,F401
    _find_radar_online,
    _german_firs_for_route,
    _normalize_freq,
    fetch_vatsim,
)
from vfr_navlog.weather import (  # noqa: E402,F401
    _wind_from_metar,
    _wx_ttd_cell,
    _wx_wind_cell,
    fetch_metar,
    fetch_taf,
    fetch_weather_briefing,
    field_weather,
    parse_atis,
    parse_metar,
)
from vfr_navlog.exports import (  # noqa: E402,F401
    _ask_fpl_fields,
    collect_vor_info,
    format_icao_fpl,
    write_fms,
)
from vfr_navlog.pdf import (  # noqa: E402,F401
    render,
    render_destination_page,
    render_phraseology,
    render_weather_page,
)
from vfr_navlog.pdf.base import (  # noqa: E402,F401
    FONT_CANDIDATES,
    NavlogPDF,
    fmt_int,
    hms,
    install_fonts,
)
from vfr_navlog.pdf.charts import _append_dfs_charts  # noqa: E402,F401

# --- Phase 1 facade: the interactive wizard and CLI entry point now live in
#     vfr_navlog.tui / vfr_navlog.cli. navlog.py stays as a thin re-export so
#     `from navlog import main` and `python3 navlog.py` keep working. ---
from vfr_navlog.cli import main  # noqa: E402,F401
from vfr_navlog.tui import _tui  # noqa: E402,F401


if __name__ == "__main__":
    main()
