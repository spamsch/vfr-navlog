"""Weather briefing page (METAR/TAF two-column + VFR assessment)."""
from __future__ import annotations

from fpdf import FPDF

from ..model import ParsedMetar, VatsimSnapshot, WeatherBriefing
from ..vatsim import _find_radar_online


def render_weather_page(pdf: FPDF, font: str, briefing: WeatherBriefing, fir_icaos: list[str] | None = None, vatsim: "VatsimSnapshot | None" = None) -> None:
    pdf.add_page()
    pw   = pdf.w - pdf.l_margin - pdf.r_margin
    GAP  = 3.0
    cw   = (pw - GAP) / 2          # width of each column
    col_xs = [pdf.l_margin, pdf.l_margin + cw + GAP]

    C_HDR   = (210, 220, 235)
    C_SEC   = (232, 232, 232)
    C_VFR   = (210, 238, 210)
    C_MVFR  = (255, 243, 200)
    C_IFR   = (255, 215, 215)
    C_RADAR = (225, 240, 255)
    LH = 4.0   # line height

    # ── title ─────────────────────────────────────────────────────────────────
    radar_info = _find_radar_online(vatsim, fir_icaos or [])
    radar_str  = f"  ·  {radar_info[0]} {radar_info[1]}" if radar_info else ""
    pdf.set_xy(pdf.l_margin, pdf.t_margin)
    pdf.set_font(font, "B", 12)
    pdf.cell(pw, 7,
             f"Wetterbriefing  ·  {briefing.dep_icao} → {briefing.dest_icao}"
             f"  ({briefing.fetched_at}){radar_str}",
             align="C")
    content_y = pdf.t_margin + 8

    # ── two-column weather sections ───────────────────────────────────────────
    col_data = [
        ("Abflug",   briefing.dep_icao,  briefing.dep_metar_raw,  briefing.dep_taf_raw,  briefing.dep_metar),
        ("Ziel",     briefing.dest_icao, briefing.dest_metar_raw, briefing.dest_taf_raw, briefing.dest_metar),
    ]

    def _metar_rows(p: ParsedMetar) -> list[tuple[str, str]]:
        if p.wind_kt is not None:
            w = ("VRB" if p.wind_vrb else f"{p.wind_dir:03d}°") + f" / {p.wind_kt} kt"
            if p.wind_gust_kt:
                w += f"  G{p.wind_gust_kt}"
        else:
            w = "—"
        if p.cavok:
            vis_str  = "CAVOK"
            ceil_str = "CAVOK"
        else:
            vis_str  = f"{p.vis_m} m" if p.vis_m is not None else "—"
            ceil_str = f"{p.ceiling_ft} ft" if p.ceiling_ft else "—"
        def _tc(v):
            return f"{v:+.0f} °C" if v is not None else "—"
        return [
            ("Wind",     w),
            ("Sicht",    vis_str),
            ("Decke",    ceil_str),
            ("Wolken",   "  ".join(p.clouds) if p.clouds else ("CAVOK" if p.cavok else "—")),
            ("T / Td",   f"{_tc(p.temp_c)} / {_tc(p.dewpoint_c)}"),
            ("QNH",      f"{p.qnh_hpa} hPa" if p.qnh_hpa else "—"),
            ("Wetter",   "  ".join(p.phenomena) if p.phenomena else "—"),
        ]

    final_ys: list[float] = []

    for col_idx, (label, icao, metar_raw, taf_raw, parsed) in enumerate(col_data):
        x = col_xs[col_idx]
        y = content_y

        # Column header
        pdf.set_fill_color(*C_HDR)
        pdf.set_font(font, "B", 10)
        pdf.set_xy(x, y)
        pdf.cell(cw, 6, f"  {icao}  ·  {label}", border=1, fill=True)
        y += 6

        # METAR header
        pdf.set_fill_color(*C_SEC)
        pdf.set_font(font, "B", 8)
        pdf.set_xy(x, y)
        pdf.cell(cw, 5, "  METAR", border=1, fill=True)
        y += 5

        # METAR raw text
        pdf.set_fill_color(255, 255, 255)
        pdf.set_font(font, "", 7.5)
        pdf.set_xy(x, y)
        if metar_raw:
            pdf.multi_cell(cw, LH, metar_raw, border="LBR")
            y = pdf.get_y()
        else:
            pdf.cell(cw, 7, "  Nicht verfügbar", border=1)
            y += 7

        # Parsed METAR key values
        if parsed:
            for row_label, row_val in _metar_rows(parsed):
                pdf.set_xy(x, y)
                pdf.set_font(font, "B", 7.5)
                pdf.cell(cw * 0.28, LH + 0.5, f" {row_label}", border=1)
                pdf.set_font(font, "", 8)
                pdf.cell(cw * 0.72, LH + 0.5, f" {row_val}", border=1)
                y += LH + 0.5

        y += 2  # visual gap before TAF

        # TAF header
        pdf.set_fill_color(*C_SEC)
        pdf.set_font(font, "B", 8)
        pdf.set_xy(x, y)
        pdf.cell(cw, 5, "  TAF", border=1, fill=True)
        y += 5

        # TAF raw text — limit to 15 lines to avoid page overflow
        pdf.set_fill_color(255, 255, 255)
        pdf.set_font(font, "", 7.5)
        pdf.set_xy(x, y)
        if taf_raw:
            lines = taf_raw.splitlines()
            if len(lines) > 15:
                lines = lines[:15] + ["[…]"]
            display = "\n".join(lines)
            pdf.multi_cell(cw, LH, display, border="LBR")
            y = pdf.get_y()
        else:
            pdf.cell(cw, 7, "  Nicht verfügbar", border=1)
            y += 7

        final_ys.append(y)

    # ── VFR assessment strip ──────────────────────────────────────────────────
    assess_y = max(final_ys) + 4

    pdf.set_fill_color(*C_SEC)
    pdf.set_font(font, "B", 9)
    pdf.set_xy(pdf.l_margin, assess_y)
    pdf.cell(pw, 5.5, "  VFR-Beurteilung  ·  Meteorological go/no-go assessment", border=1, fill=True)
    assess_y += 5.5

    STATUS_COLORS = {"VFR": C_VFR, "MVFR": C_MVFR, "IFR": C_IFR}
    STATUS_LABELS = {
        "VFR":  "VFR  OK",
        "MVFR": "MVFR  !",
        "IFR":  "IFR  XX",
    }
    assess_rows = [
        (briefing.dep_icao,  briefing.dep_metar),
        (briefing.dest_icao, briefing.dest_metar),
    ]
    col_defs = [
        ("Platz",   pw * 0.08),
        ("Status",  pw * 0.10),
        ("Wind",    pw * 0.18),
        ("Sicht",   pw * 0.12),
        ("Decke",   pw * 0.12),
        ("QNH hPa", pw * 0.10),
        ("Wetter",  pw * 0.30),
    ]
    # header row
    pdf.set_fill_color(*C_SEC)
    pdf.set_font(font, "B", 8)
    pdf.set_xy(pdf.l_margin, assess_y)
    for col_name, col_w in col_defs:
        pdf.cell(col_w, 5, f" {col_name}", border=1, fill=True)
    assess_y += 5

    for icao, parsed in assess_rows:
        status = parsed.vfr_status() if parsed else "—"
        color  = STATUS_COLORS.get(status, (245, 245, 245))
        pdf.set_fill_color(*color)
        pdf.set_font(font, "B" if status in ("IFR", "MVFR") else "", 8)
        pdf.set_xy(pdf.l_margin, assess_y)

        if parsed:
            if parsed.wind_kt is not None:
                w = ("VRB" if parsed.wind_vrb else f"{parsed.wind_dir:03d}°") + f"/{parsed.wind_kt}kt"
                if parsed.wind_gust_kt:
                    w += f" G{parsed.wind_gust_kt}"
            else:
                w = "—"
            vis_s  = "CAVOK" if parsed.cavok else (f"{parsed.vis_m} m" if parsed.vis_m else "—")
            ceil_s = "CAVOK" if parsed.cavok else (f"{parsed.ceiling_ft} ft" if parsed.ceiling_ft else "—")
            qnh_s  = str(parsed.qnh_hpa) if parsed.qnh_hpa else "—"
            wx_s   = "  ".join(parsed.phenomena) if parsed.phenomena else "—"
        else:
            w = vis_s = ceil_s = qnh_s = wx_s = "—"
            status = "—"

        values = [icao, STATUS_LABELS.get(status, status), w, vis_s, ceil_s, qnh_s, wx_s]
        for (_, col_w), val in zip(col_defs, values):
            pdf.cell(col_w, 5.5, f" {val}", border=1, fill=True)
        assess_y += 5.5

    # VFR minimums note
    pdf.set_fill_color(255, 255, 255)
    pdf.set_xy(pdf.l_margin, assess_y + 1)
    pdf.set_font(font, "I", 6.5)
    pdf.cell(pw, 3.5,
             "VFR: Decke ≥ 3000 ft + Sicht ≥ 5 km  ·  MVFR: Decke 1500–3000 ft oder Sicht 3–5 km  ·  "
             "IFR: Decke < 1500 ft oder Sicht < 3 km  ·  Grenzwerte Deutschland TMZ/CTR: AIP ENR 1.2",
             border=0, align="C")

    # Radar strip (if online)
    if radar_info:
        radar_y = assess_y + 5.5
        pdf.set_fill_color(*C_RADAR)
        pdf.set_font(font, "B", 8)
        pdf.set_xy(pdf.l_margin, radar_y)
        pdf.cell(pw, 5,
                 f"  En-route Radar:  {radar_info[0]}  ·  {radar_info[1]} MHz  (VATSIM live)",
                 border=1, fill=True, align="L")

    # Footer
    pdf.set_y(pdf.h - pdf.b_margin - 4)
    pdf.set_font(font, "I", 6)
    pdf.cell(0, 3,
             f"METAR/TAF via VATSIM weather proxy ({briefing.fetched_at}). "
             "Nicht für echten Flugbetrieb — offizielle Briefing-Quellen (DWD, Autobrief) verwenden.",
             align="C")
