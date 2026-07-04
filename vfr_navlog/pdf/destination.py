"""Destination airport briefing page."""
from __future__ import annotations

from fpdf import FPDF

from ..config import UNICOM_FREQ
from ..fixes import morse
from ..model import AirportInfo, VatsimSnapshot


def render_destination_page(
    pdf: FPDF, font: str, info: AirportInfo,
    vatsim: VatsimSnapshot | None, navaids=None,
) -> None:
    pdf.add_page()
    pw = pdf.w - pdf.l_margin - pdf.r_margin

    # Title
    pdf.set_xy(pdf.l_margin, pdf.t_margin)
    pdf.set_font(font, "B", 14)
    pdf.cell(pw, 8, f"Zielflugplatz {info.icao}  ·  Destination airport", align="C")
    pdf.ln(9)
    pdf.set_font(font, "I", 8)
    pdf.cell(pw, 4, info.name + (f"  ·  {info.city}" if info.city else ""), align="C")
    pdf.ln(7)

    # ---------- Airport basics ----------
    pdf.set_font(font, "B", 9)
    pdf.set_fill_color(225, 225, 225)
    pdf.cell(pw, 5, "  Flugplatz-Stammdaten  ·  Airport data", border=1, fill=True)
    pdf.ln(5)

    basics = [
        ("ICAO",            info.icao),
        ("IATA",            info.iata or "—"),
        ("Name",            info.name),
        ("Ort / City",      info.city or "—"),
        ("Elevation",       f"{int(info.elevation_ft)} ft"),
        ("TA / TL",         f"{info.transition_alt or '—'} ft / FL {info.transition_level or '—'}"),
    ]
    half = pw / 2
    label_w = 32
    val_w = half - label_w
    pdf.set_font(font, "", 9)
    for i in range(0, len(basics), 2):
        row = basics[i:i + 2]
        for label, value in row:
            pdf.set_font(font, "B", 8)
            pdf.cell(label_w, 5, " " + label, border=1)
            pdf.set_font(font, "", 9)
            pdf.cell(val_w, 5, " " + str(value), border=1)
        if len(row) == 1:
            pdf.cell(half, 5, "", border=0)
        pdf.ln(5)
    pdf.ln(2)

    # ---------- Runways + ILS LOC ----------
    pdf.set_font(font, "B", 9)
    pdf.set_fill_color(210, 220, 235)
    pdf.cell(pw, 5, "  Pisten & ILS-Frequenzen  ·  Runways & ILS LOC frequencies", border=1, fill=True)
    pdf.ln(5)

    cols = [
        ("RWY",          22, "C"),
        ("Belag",        24, "L"),
        ("Länge",        22, "R"),
        ("Breite",       18, "R"),
        ("ILS Ident",    24, "C"),
        ("ILS Freq",     24, "C"),
        ("Typ",          pw - 22 - 24 - 22 - 18 - 24 - 24, "L"),
    ]
    pdf.set_font(font, "B", 8)
    pdf.set_fill_color(240, 240, 240)
    for name, w, _ in cols:
        pdf.cell(w, 5, " " + name, border=1, fill=True)
    pdf.ln(5)

    # Build rows: one per runway end, joined with its ILS LOC if any.
    ils_by_rwy = {ils.runway: ils for ils in info.ils_locs}
    pdf.set_font(font, "", 9)
    if info.runways:
        for rwy in info.runways:
            for end in (rwy.ident_a, rwy.ident_b):
                ils = ils_by_rwy.get(end)
                values = [
                    end,
                    rwy.surface,
                    f"{int(round(rwy.length_m))} m",
                    f"{int(round(rwy.width_m))} m",
                    ils.ident if ils else "—",
                    f"{ils.freq_mhz:.3f}" if ils else "—",
                    ils.type_desc if ils else "—",
                ]
                for (_, w, align), val in zip(cols, values):
                    pdf.cell(w, 5, " " + str(val), border=1, align=align)
                pdf.ln(5)
    else:
        pdf.cell(pw, 5, "  Keine Pistendaten gefunden (X-Plane apt.dat fehlt). Pfad mit --xplane setzen.",
                 border=1, align="C")
        pdf.ln(5)
    pdf.ln(2)

    # ---------- Comm frequencies (recap + ATIS text) ----------
    pdf.set_font(font, "B", 9)
    pdf.set_fill_color(225, 225, 225)
    suffix = f"  (fett = VATSIM live, {vatsim.fetched_at})" if vatsim else "  (Standard)"
    pdf.cell(pw, 5, "  Frequenzen & ATIS  ·  Communication frequencies" + suffix, border=1, fill=True)
    pdf.ln(5)

    live = vatsim.frequencies.get(info.icao, {}) if vatsim else {}
    std = info.frequencies or {}
    role_labels = [
        ("Ground",    "ground"),
        ("Tower",     "tower"),
        ("Approach",  "approach"),
        ("Delivery",  "delivery"),
        ("ATIS",      "atis"),
        ("UNICOM",    None),
    ]
    pdf.set_font(font, "", 9)
    label_w2 = 30
    val_w2 = pw / 3 - label_w2
    pdf.set_x(pdf.l_margin)
    col = 0
    row_y = pdf.get_y()
    for label, key in role_labels:
        is_live = False
        if key is None:
            value = f"{UNICOM_FREQ} (kein ATC)"
        else:
            # Live VATSIM station bold; published standard frequency otherwise.
            value = live.get(key, "")
            is_live = bool(value)
            if not value:
                value = std.get(key, "—") or "—"
        x = pdf.l_margin + col * (label_w2 + val_w2)
        pdf.set_xy(x, row_y)
        pdf.set_font(font, "B", 8)
        pdf.cell(label_w2, 5, " " + label, border=1)
        pdf.set_font(font, "B" if is_live else "", 9)
        pdf.cell(val_w2, 5, " " + value, border=1)
        col += 1
        if col == 3:
            col = 0
            row_y += 5
    if col != 0:
        # fill remaining slots in current row
        for _ in range(3 - col):
            x = pdf.l_margin + col * (label_w2 + val_w2)
            pdf.set_xy(x, row_y)
            pdf.cell(label_w2 + val_w2, 5, "", border=0)
            col += 1
        row_y += 5
    pdf.set_y(row_y + 1)

    # Live ATIS text, if present
    atis_lines = vatsim.atis_text.get(info.icao) if vatsim else None
    if atis_lines:
        pdf.set_font(font, "B", 8)
        pdf.set_fill_color(245, 245, 220)
        pdf.cell(pw, 4.5, "  ATIS-Text (live)", border=1, fill=True)
        pdf.ln(4.5)
        pdf.set_font(font, "", 8.5)
        atis_block = "\n".join(atis_lines)
        pdf.multi_cell(pw, 4, atis_block, border="LBR")
    else:
        pdf.set_font(font, "I", 8)
        pdf.cell(pw, 4.5, "  Kein ATIS-Text verfügbar (VATSIM ATIS offline oder --vatsim nicht gesetzt).",
                 border=1, align="C")
        pdf.ln(4.5)

    # ---------- Navaid-Referenz (Morse) ----------
    # One line per distinct VOR used for the route's radial fixes. You identify a
    # VOR by its Morse before trusting it; putting the pattern here closes that loop.
    if navaids:
        pdf.ln(2)
        pdf.set_font(font, "B", 9)
        pdf.set_fill_color(225, 235, 225)
        pdf.cell(pw, 5, "  Navaid-Referenz  ·  VOR identification (Morse)", border=1, fill=True)
        pdf.ln(5)
        nav_cols = [
            ("Ident", 20, "C"),
            ("Name", pw - 20 - 22 - 16 - 60, "L"),
            ("Freq", 22, "C"),
            ("DME", 16, "C"),
            ("Morse", 60, "L"),
        ]
        pdf.set_font(font, "B", 8)
        pdf.set_fill_color(240, 240, 240)
        for name, w, _ in nav_cols:
            pdf.cell(w, 5, " " + name, border=1, fill=True)
        pdf.ln(5)
        for fx in navaids:
            values = [
                fx.vor_ident,
                fx.vor_name,
                fx.freq,
                "DME" if fx.has_dme else "—",
                morse(fx.vor_ident),
            ]
            for (_, w, align), val in zip(nav_cols, values):
                pdf.set_font(font, "" if align != "C" else "B", 8)
                pdf.cell(w, 5, " " + str(val), border=1, align=align)
            pdf.ln(5)

    # Footer
    pdf.set_y(pdf.h - pdf.b_margin - 4)
    pdf.set_font(font, "I", 6)
    pdf.cell(0, 3,
             "Pisten- und ILS-Daten aus X-Plane apt.dat / earth_nav.dat. Vor dem Flug aktuelle AIP / ATIS prüfen.",
             align="C")
