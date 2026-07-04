"""METAR / TAF / ATIS fetching and parsing, plus the weather briefing."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .config import VATSIM_METAR_URL, VATSIM_TAF_URL
from .model import FieldWx, ParsedMetar, VatsimSnapshot, WeatherBriefing
from .net import fetch


def fetch_metar(icao: str, timeout: float = 6.0) -> str | None:
    """Fetch a raw METAR from VATSIM's METAR endpoint. Returns None on failure."""
    body = fetch(VATSIM_METAR_URL.format(icao=icao.upper()), timeout=timeout)
    if body is None:
        return None
    text = body.strip()
    return text if text else None


def _wind_from_metar(metar: str) -> tuple[float, float] | None:
    """Extract (direction_deg, speed_kt) from a raw METAR string.

    Returns None when wind is missing or can't be parsed.
    Variable-direction wind (VRB) is treated as calm (000).
    """
    # Standard: dddssKT or dddssGggKT
    m = re.search(r"\b(\d{3})(\d{2,3})(?:G\d{2,3})?KT\b", metar)
    if m:
        return float(m.group(1)) % 360, float(m.group(2))
    # MPS variant (rare at VATSIM, convert to knots)
    m = re.search(r"\b(\d{3})(\d{2,3})(?:G\d{2,3})?MPS\b", metar)
    if m:
        return float(m.group(1)) % 360, round(float(m.group(2)) * 1.944)
    # Variable
    m = re.search(r"\bVRB(\d{2,3})KT\b", metar)
    if m:
        return 0.0, float(m.group(1))
    return None


def fetch_taf(icao: str, timeout: float = 6.0) -> str | None:
    """Fetch a raw TAF from VATSIM's TAF endpoint. Returns None on failure."""
    body = fetch(VATSIM_TAF_URL.format(icao=icao.upper()), timeout=timeout)
    if body is None:
        return None
    text = body.strip()
    return text if text else None


def _parse_temp(s: str) -> float:
    return -float(s[1:]) if s.startswith("M") else float(s)


# ------------------------- shared METAR-group tokenizers -------------------------
#
# METAR and ATIS text carry the same coded groups for wind, temperature and
# pressure. These extractors are the single source of truth for both parse_metar
# and parse_atis; each entry point layers its own extras (METAR: vis/clouds/
# phenomena; ATIS: verbose free-text fallbacks) around them.

_WIND_GROUP_RE = re.compile(r"\b(VRB|\d{3})(\d{2,3})(?:G(\d{2,3}))?(KT|MPS)\b")


def _tok_wind_group(pm: ParsedMetar, text: str, mod360: bool = False) -> bool:
    """Set wind fields from a coded wind group (dddssKT / VRBssKT / dddssMPS).

    Returns True if a group matched. `mod360` wraps the direction into 0–359
    (ATIS convention); METAR keeps the reported direction verbatim.
    """
    m = _WIND_GROUP_RE.search(text)
    if not m:
        return False
    kt = int(m.group(2))
    gust = int(m.group(3)) if m.group(3) else None
    if m.group(4) == "MPS":
        kt = int(round(kt * 1.944))
        gust = int(round(gust * 1.944)) if gust else None
    pm.wind_kt = kt
    pm.wind_gust_kt = gust
    if m.group(1) == "VRB":
        pm.wind_vrb = True
    else:
        pm.wind_dir = int(m.group(1)) % 360 if mod360 else int(m.group(1))
    return True


def _tok_temp_group(pm: ParsedMetar, text: str) -> None:
    """Set temp/dewpoint from a coded 'tt/dd' group (M-prefixed = negative)."""
    m = re.search(r"\b(M?\d{2})/(M?\d{2})\b", text)
    if m:
        try:
            pm.temp_c = _parse_temp(m.group(1))
            pm.dewpoint_c = _parse_temp(m.group(2))
        except ValueError:
            pass


def _qnh_from_q(text: str) -> int | None:
    m = re.search(r"\bQ(\d{4})\b", text)
    return int(m.group(1)) if m else None


def _qnh_from_a(text: str) -> int | None:
    """Altimeter setting A#### (inHg × 100) converted to hPa."""
    m = re.search(r"\bA(\d{4})\b", text)
    return int(round(int(m.group(1)) * 0.338639)) if m else None


def parse_metar(raw: str) -> ParsedMetar:
    result = ParsedMetar(raw=raw)

    _tok_wind_group(result, raw, mod360=False)

    # CAVOK
    if "CAVOK" in raw:
        result.cavok = True
        result.vis_m = 9999
    else:
        # Visibility: standalone 4-digit group or 9999
        m = re.search(r"(?<!\d)(\d{4})(?!\d)", raw)
        if m:
            result.vis_m = int(m.group(1))
        # Clouds
        for cm in re.finditer(r"\b(FEW|SCT|BKN|OVC)(\d{3})(CB|TCU)?", raw):
            height_ft = int(cm.group(2)) * 100
            label = cm.group(1) + cm.group(2) + (cm.group(3) or "")
            result.clouds.append(label)
            if cm.group(1) in ("BKN", "OVC"):
                if result.ceiling_ft is None or height_ft < result.ceiling_ft:
                    result.ceiling_ft = height_ft

    # Temp / Dewpoint
    _tok_temp_group(result, raw)

    # QNH
    q = _qnh_from_q(raw)
    if q is not None:
        result.qnh_hpa = q
    else:
        a = _qnh_from_a(raw)
        if a is not None:
            result.qnh_hpa = a

    # Significant weather phenomena
    phenom_re = re.compile(
        r"\b(\+|-|VC)?(?:MI|BC|PR|DR|BL|SH|TS|FZ)?"
        r"(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PO|SQ|FC|SS|DS)\b"
    )
    result.phenomena = [pm.group(0) for pm in phenom_re.finditer(raw)]

    return result


def briefing_from_raws(dep_icao: str, dest_icao: str,
                       dep_m: str | None, dest_m: str | None,
                       dep_t: str | None, dest_t: str | None,
                       fetched_at: str) -> WeatherBriefing:
    """Assemble a WeatherBriefing from already-fetched raw METAR/TAF text.

    Separated from fetch_weather_briefing so the caller can fetch the four
    endpoints in parallel and still build the briefing the same way.
    """
    return WeatherBriefing(
        dep_icao=dep_icao.upper(),
        dest_icao=dest_icao.upper(),
        dep_metar_raw=dep_m,
        dest_metar_raw=dest_m,
        dep_taf_raw=dep_t,
        dest_taf_raw=dest_t,
        dep_metar=parse_metar(dep_m) if dep_m else None,
        dest_metar=parse_metar(dest_m) if dest_m else None,
        fetched_at=fetched_at,
    )


def fetch_weather_briefing(dep_icao: str, dest_icao: str) -> WeatherBriefing:
    fetched_at = datetime.now(timezone.utc).strftime("%H:%MZ")
    dep_m  = fetch_metar(dep_icao)
    dest_m = fetch_metar(dest_icao)
    dep_t  = fetch_taf(dep_icao)
    dest_t = fetch_taf(dest_icao)
    return briefing_from_raws(dep_icao, dest_icao, dep_m, dest_m, dep_t, dest_t, fetched_at)


# ------------------------- field weather (ATIS / METAR) -------------------------
#
# Quelle pro Platz: zuerst der VATSIM-ATIS-Text der online ATIS-Station (falls
# vorhanden und parsebar), sonst echtes METAR als Fallback. Der Fallback liefert
# bewusst nur Wind, Temperatur und Druck – Sicht/Wolken bleiben leer.

def parse_atis(lines: list[str]) -> ParsedMetar:
    """Extrahiert Wind, Temperatur und QNH aus dem freien VATSIM-ATIS-Text.

    ATIS-Text variiert stark je vACC. Strategie: erst nach eingebetteten
    METAR-Gruppen suchen (viele vACCs hängen das rohe METAR an), dann nach der
    ausgeschriebenen ATIS-Sprache (WIND 250 DEG 8 KT, QNH 1018, TEMP 14 DP 09).
    Sicht/Wolken werden bewusst nicht geparst – der ATIS-Fließtext ist dafür zu
    uneinheitlich, und gefragt sind nur Wind/Temp/Druck.
    """
    text = " ".join(lines).upper()
    pm = ParsedMetar(raw=text)

    # --- Wind: erst METAR-Gruppe (dddssKT / dddssMPS / VRBssKT), dann verbose ---
    if not _tok_wind_group(pm, text, mod360=True):
        m = re.search(r"\bWIND\b[^0-9]{0,8}(\d{3})[^0-9]{0,10}?(\d{1,3})\s*(?:KT|KNOT)", text)
        if m:
            pm.wind_dir = int(m.group(1)) % 360
            pm.wind_kt = int(m.group(2))
        else:
            m = re.search(r"\bWIND\b[^0-9]{0,4}(?:VRB|VARIABLE)[^0-9]{0,8}(\d{1,3})\s*(?:KT|KNOT)", text)
            if m:
                pm.wind_vrb = True
                pm.wind_kt = int(m.group(1))
        if pm.wind_kt is not None:
            g = re.search(r"\b(?:GUST\w*|MAX(?:IMUM)?)[^0-9]{0,6}(\d{1,3})", text)
            if g:
                pm.wind_gust_kt = int(g.group(1))

    # --- QNH: METAR-Gruppe (Qdddd / Adddd) oder verbose (QNH 1018) ---
    q = _qnh_from_q(text)
    if q is not None:
        pm.qnh_hpa = q
    else:
        m = re.search(r"\bQNH\b[^0-9]{0,6}(\d{3,4})", text)
        if m:
            pm.qnh_hpa = int(m.group(1))
        else:
            a = _qnh_from_a(text)   # inHg → hPa
            if a is not None:
                pm.qnh_hpa = a

    # --- Temp / Taupunkt: METAR-Gruppe (tt/dd) oder verbose (TEMP 14 DP 09) ---
    _tok_temp_group(pm, text)
    if pm.temp_c is None:
        m = re.search(r"\bTEMP\w*[^0-9M]{0,4}(M?\d{1,2}).{0,14}?(?:DEW\w*|DP|DEWPOINT)[^0-9M]{0,4}(M?\d{1,2})", text)
        if m:
            try:
                pm.temp_c = _parse_temp(m.group(1))
                pm.dewpoint_c = _parse_temp(m.group(2))
            except ValueError:
                pass
        else:
            m = re.search(r"\bTEMP\w*[^0-9M]{0,4}(M?\d{1,2})", text)
            if m:
                try:
                    pm.temp_c = _parse_temp(m.group(1))
                except ValueError:
                    pass

    return pm


def _atis_meta(lines: list[str]) -> dict:
    """ATIS-Kennung (Buchstabe), aktive RWY und Beobachtungszeit aus dem Text."""
    text = " ".join(lines).upper()
    meta: dict = {}
    m = (re.search(r"\bATIS\b(?:\s+\w+)?\s+(?:INFO\w*\s+)?([A-Z])\b", text)
         or re.search(r"\bINFORMATION\s+([A-Z])", text))
    if m:
        meta["atis_code"] = m.group(1)
    m = re.search(r"\bRWY?\s*(\d{2}[LRC]?)", text)
    if m:
        meta["rwy"] = m.group(1)
    m = re.search(r"\b(\d{4})Z\b", text)
    if m:
        meta["time_z"] = m.group(1) + "Z"
    return meta


def field_weather(icao: str, vatsim: "VatsimSnapshot | None",
                  briefing: "WeatherBriefing | None") -> FieldWx | None:
    """Wetter für einen Platz: VATSIM-ATIS bevorzugt, sonst echtes METAR.

    Fällt auf METAR zurück, wenn keine ATIS-Station online ist *oder* der
    ATIS-Text keinen Wind/Temp/QNH hergibt. Der METAR-Fallback wird auf
    Wind/Temp/Druck beschränkt (Sicht/Wolken bleiben leer).
    """
    icao = icao.upper()

    # 1) VATSIM ATIS
    lines = vatsim.atis_text.get(icao) if vatsim else None
    if lines:
        pm = parse_atis(lines)
        if pm.wind_kt is not None or pm.qnh_hpa is not None or pm.temp_c is not None:
            return FieldWx(icao, "VATSIM ATIS", pm, **_atis_meta(lines))

    # 2) Echtes METAR (aus dem bereits geholten Briefing, sonst nachladen)
    pm = None
    if briefing:
        if icao == briefing.dep_icao:
            pm = briefing.dep_metar
        elif icao == briefing.dest_icao:
            pm = briefing.dest_metar
    if pm is None:
        raw = fetch_metar(icao)
        pm = parse_metar(raw) if raw else None
    if pm is not None:
        # nur Wind/Temp/Druck verwenden
        slim = ParsedMetar(
            raw=pm.raw,
            wind_dir=pm.wind_dir, wind_kt=pm.wind_kt,
            wind_gust_kt=pm.wind_gust_kt, wind_vrb=pm.wind_vrb,
            temp_c=pm.temp_c, dewpoint_c=pm.dewpoint_c, qnh_hpa=pm.qnh_hpa,
        )
        return FieldWx(icao, "METAR (real)", slim)

    return None


def _wx_wind_cell(pm: ParsedMetar) -> str:
    if pm.wind_kt is None:
        return ""
    if pm.wind_vrb or pm.wind_dir is None:
        head = "VRB"
    else:
        head = f"{pm.wind_dir:03d}"
    s = f"{head}/{pm.wind_kt:02d}"
    if pm.wind_gust_kt:
        s += f"G{pm.wind_gust_kt}"
    return s


def _wx_ttd_cell(pm: ParsedMetar) -> str:
    if pm.temp_c is None:
        return ""
    s = f"{int(round(pm.temp_c))}"
    if pm.dewpoint_c is not None:
        s += f"/{int(round(pm.dewpoint_c))}"
    return s
