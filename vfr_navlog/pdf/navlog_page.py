"""The main navlog page: header block, nav table, fuel/planning block."""
from __future__ import annotations

from ..config import UNICOM_FREQ
from ..legs import _effective_leg_alt, find_call_marker
from ..vatsim import _find_radar_online
from ..weather import _wx_ttd_cell, _wx_wind_cell
from .base import fmt_int


def _fix_line(fx) -> str:
    """One VOR cross-check line for the nav-table cell: 'HLZ 116.30 R245'."""
    if fx.overhead:
        return f"{fx.vor_ident} {fx.freq} ↑ overhead"
    line = f"{fx.vor_ident} {fx.freq} R{fx.radial:03d}"
    if fx.has_dme:
        line += f" {fx.dist_nm:.0f}nm"
    return line


def render_navlog_page(pdf, font, ctx, date_str: str) -> None:
    """Draw the first (navlog) page and its fuel/planning summary."""
    pw = pdf.w - pdf.l_margin - pdf.r_margin
    plan = ctx.plan
    aircraft = ctx.aircraft
    legs = ctx.legs
    wind = ctx.wind
    magvar = ctx.magvar
    vatsim = ctx.vatsim
    perf = aircraft.get("performance", {})
    departure = plan.waypoints[0]
    destination = plan.waypoints[-1]

    # Tower-call marker: which waypoint row to flag. Live VATSIM tower/approach
    # first, then the published standard tower frequency from apt.dat.
    call_leg_idx = find_call_marker(legs, ctx.call_tower_nm)
    dest_freqs_for_call = (
        vatsim.frequencies.get(destination.ident.upper(), {}) if vatsim else {}
    )
    std_dest_freqs = ctx.dest_info.frequencies if ctx.dest_info else {}
    call_freq = dest_freqs_for_call.get("tower") or dest_freqs_for_call.get("approach")
    call_freq_label = "TWR" if dest_freqs_for_call.get("tower") else ("APP" if dest_freqs_for_call.get("approach") else "TWR")
    if not call_freq and std_dest_freqs.get("tower"):
        call_freq = std_dest_freqs["tower"]
        call_freq_label = "TWR"
    if call_freq:
        call_text = f"→ {call_freq_label} {call_freq}"
    else:
        call_text = f"→ {destination.ident} TWR rufen"

    _note_text = (
        ctx.source_note
        or "Erzeugt aus Little Navmap .lnmpln — Werte ohne Gewähr. "
           "Vor dem Flug gegen aktuelle Briefing-Unterlagen prüfen."
    )
    # Reserve space at the bottom of each table page for the footer line.
    usable_bottom = pdf.h - pdf.b_margin - 7

    std_dep_freqs = ctx.dep_info.frequencies if ctx.dep_info else {}
    nav_y = _draw_header_block(pdf, font, pw, departure, destination, aircraft,
                              vatsim, ctx.fir_icaos, ctx.field_wx, ctx.weather, date_str,
                              std_dep_freqs, std_dest_freqs)
    cum_dist, cum_ete, cum_fuel, current_y = _draw_nav_table(
        pdf, font, pw, plan, legs, wind, magvar, perf,
        nav_y, call_leg_idx, call_text, _note_text, usable_bottom)
    _draw_fuel_block(pdf, font, pw, plan, aircraft, legs, wind, magvar, perf,
                     destination, current_y, usable_bottom,
                     cum_dist, cum_ete, cum_fuel,
                     call_leg_idx, call_freq, call_freq_label, _note_text)


def _draw_header_block(pdf, font, pw, departure, destination, aircraft,
                       vatsim, fir_icaos, field_wx, weather, date_str,
                       std_dep_freqs=None, std_dest_freqs=None) -> float:
    # ---------- header strip ----------
    pdf.set_font(font, "B", 11)
    pdf.set_xy(pdf.l_margin, pdf.t_margin)
    pdf.cell(60, 7, "Flugdurchführungsplan VFR", border=0)

    y = pdf.t_margin
    x = pdf.l_margin + 62
    fields = [
        ("Datum", date_str, 28),
        ("von", f"{departure.ident}  {departure.name}", 70),
        ("nach", f"{destination.ident}  {destination.name}", 70),
        ("LFZ-Muster", aircraft.get("type", ""), 26),
        ("LFZ-Kennz.", aircraft.get("registration", ""), 26),
    ]
    for label, value, w in fields:
        pdf.set_xy(x, y)
        pdf.set_font(font, "", 7)
        pdf.cell(w, 3, label, border="LTR")
        pdf.set_xy(x, y + 3)
        pdf.set_font(font, "B", 9)
        pdf.cell(w, 4, " " + value, border="LBR")
        x += w

    # ---------- info row (freigaben / frequencies / times) ----------
    y = pdf.t_margin + 8
    block_h = 25

    # Left: Freigaben / Wetter / Info (free text)
    fg_w = 110
    pdf.set_xy(pdf.l_margin, y)
    pdf.set_font(font, "B", 8)
    pdf.cell(fg_w, 4, " Freigaben / Wetter / Info", border="LTR")
    pdf.rect(pdf.l_margin, y + 4, fg_w, block_h - 4)

    # Middle: Frequencies (departure / destination)
    fr_x = pdf.l_margin + fg_w + 2
    fr_w = (pw - fg_w - 2) * 0.55
    pdf.set_xy(fr_x, y)
    pdf.set_font(font, "B", 8)
    if vatsim and not vatsim.empty():
        freq_title = f" Frequenzen   (fett = VATSIM live, {vatsim.fetched_at})"
    elif vatsim:
        freq_title = " Frequenzen   (Standard — VATSIM: keine Stationen online)"
    else:
        freq_title = " Frequenzen   (Standard)"
    pdf.cell(fr_w, 4, freq_title, border="LTR")

    dep_freqs = vatsim.frequencies.get(departure.ident.upper(), {}) if vatsim else {}
    dest_freqs = vatsim.frequencies.get(destination.ident.upper(), {}) if vatsim else {}
    std_dep_freqs = std_dep_freqs or {}
    std_dest_freqs = std_dest_freqs or {}

    fr_rows = [
        ("Ground", "ground", "delivery"),
        ("Tower", "tower", "approach"),
        ("Info / ATIS", "atis", None),
    ]
    col1_w = 26
    col_rest = (fr_w - col1_w) / 2
    pdf.set_xy(fr_x, y + 4)
    pdf.set_font(font, "", 7)
    pdf.cell(col1_w, 4, "", border="LR")
    pdf.cell(col_rest, 4, f" {departure.ident} (Abflug)", border="R")
    pdf.cell(col_rest, 4, f" {destination.ident} (Ziel)", border="R")
    sub_rh = (block_h - 8) / 5  # Ground + Tower + ATIS + Radar/FIS + UNICOM
    for i, (label, primary, fallback) in enumerate(fr_rows):
        ry = y + 8 + i * sub_rh
        pdf.set_xy(fr_x, ry)

        def pick(live: dict[str, str], std: dict[str, str]) -> tuple[str, bool]:
            """(value, is_live). Live VATSIM wins and prints bold; the published
            standard frequency from apt.dat fills the gap in regular weight."""
            v = live.get(primary, "")
            if not v and fallback:
                v = live.get(fallback, "")
                if v:
                    v += f" ({fallback[:3].upper()})"
            if v:
                return v, True
            return std.get(primary, ""), False

        dep_v, dep_live = pick(dep_freqs, std_dep_freqs)
        dest_v, dest_live = pick(dest_freqs, std_dest_freqs)
        pdf.set_font(font, "", 8)
        pdf.cell(col1_w, sub_rh, " " + label, border=1)
        pdf.set_font(font, "B" if dep_live else "", 8)
        pdf.cell(col_rest, sub_rh, " " + dep_v if dep_v else "", border=1)
        pdf.set_font(font, "B" if dest_live else "", 8)
        pdf.cell(col_rest, sub_rh, " " + dest_v if dest_v else "", border=1)

    # Radar / FIS row — spans dep+dest columns, highlighted in pale blue when online.
    radar_info = _find_radar_online(vatsim, fir_icaos or [])
    radar_y = y + 8 + 3 * sub_rh
    pdf.set_xy(fr_x, radar_y)
    pdf.set_font(font, "B" if radar_info else "", 8)
    if radar_info:
        radar_name, radar_freq = radar_info
        pdf.set_fill_color(225, 240, 255)
        pdf.cell(col1_w, sub_rh, f" {radar_name}", border=1, fill=True)
        pdf.cell(col_rest * 2, sub_rh, f" {radar_freq}", border=1, fill=True)
        pdf.set_fill_color(255, 255, 255)
    else:
        pdf.set_font(font, "", 8)
        pdf.cell(col1_w, sub_rh, " Radar / FIS", border=1)
        pdf.cell(col_rest * 2, sub_rh, "", border=1)

    # UNICOM row: merged across the full Frequenzen width, bold and centered.
    unicom_y = y + 8 + 4 * sub_rh
    pdf.set_xy(fr_x, unicom_y)
    pdf.set_fill_color(235, 235, 235)
    pdf.set_font(font, "B", 9)
    pdf.cell(fr_w, sub_rh,
             f"UNICOM (kein ATC):  {UNICOM_FREQ} MHz",
             border=1, align="C", fill=True)
    pdf.set_fill_color(255, 255, 255)

    # Right: Times block
    t_x = fr_x + fr_w + 2
    t_w = pw - (t_x - pdf.l_margin)
    pdf.set_xy(t_x, y)
    pdf.set_font(font, "B", 8)
    pdf.cell(t_w, 4, " Zeiten (UTC)", border="LTR")
    time_rows = [("ETD", "ATD"), ("ETA", "ATA"), ("SS", "")]
    rh = (block_h - 4) / 3
    half = t_w / 2
    for i, (a, b) in enumerate(time_rows):
        ry = y + 4 + i * rh
        pdf.set_xy(t_x, ry)
        pdf.set_font(font, "", 8)
        pdf.cell(t_w * 0.18, rh, f" {a}", border="LB" if i == 2 else "L")
        pdf.cell(half - t_w * 0.18, rh, "", border="B" if i == 2 else "")
        pdf.cell(t_w * 0.18, rh, f" {b}" if b else "", border="LB" if i == 2 else "L")
        pdf.cell(half - t_w * 0.18, rh, "", border="RB" if i == 2 else "R")

    # ---------- ATIS / Platzwetter strip: 3 rows (Abflug / Ziel / Ausweich) ----------
    y2 = y + block_h + 2
    fixed_w = 16 + 12 + 12 + 14 + 16 + 22 + 16 + 30 + 16 + 14
    atis_cols = [
        ("Platz", 16), ("Code", 12), ("RWY", 12), ("TL/FL", 14), ("Zeit UTC", 16),
        ("Wind °/kt", 22), ("Sicht", 16), ("Wolken", 30),
        ("T/Td °C", 16), ("QNH", 14),
        ("Tendenz / Bemerkungen", pw - fixed_w),
    ]
    atis_header_h = 4
    atis_row_h = 5
    pdf.set_xy(pdf.l_margin, y2)
    pdf.set_font(font, "B", 7)
    for label, w in atis_cols:
        pdf.cell(w, atis_header_h, " " + label, border="LTR")
    atis_rows = [departure.ident or "Abflug", destination.ident or "Ziel", "Ausweich"]
    # Vorbefüllung Abflug/Ziel aus VATSIM-ATIS bzw. echtem METAR; Ausweich bleibt leer.
    wx_by_row = [
        (field_wx or {}).get(departure.ident.upper()) if departure.ident else None,
        (field_wx or {}).get(destination.ident.upper()) if destination.ident else None,
        None,
    ]
    fetched_at = weather.fetched_at if weather else ""

    def _atis_cell_values(label: str, wx) -> list[str]:
        # Reihenfolge: Platz, Code, RWY, TL/FL, Zeit UTC, Wind, Sicht, Wolken, T/Td, QNH, Bemerkung
        if wx is None:
            return [" " + label] + [""] * (len(atis_cols) - 1)
        pm = wx.parsed
        zeit = wx.time_z or (fetched_at if wx.source.startswith("METAR") else "")
        return [
            " " + label,
            wx.atis_code or "",
            wx.rwy or "",
            "",                                  # TL/FL – nicht aus dem Wetter
            zeit,
            _wx_wind_cell(pm),
            "",                                  # Sicht – nur Wind/Temp/Druck
            "",                                  # Wolken – dito
            _wx_ttd_cell(pm),
            str(pm.qnh_hpa) if pm.qnh_hpa is not None else "",
            wx.source,
        ]

    pdf.set_font(font, "", 7)
    for ri, label in enumerate(atis_rows):
        ry = y2 + atis_header_h + ri * atis_row_h
        pdf.set_xy(pdf.l_margin, ry)
        values = _atis_cell_values(label, wx_by_row[ri])
        for ci, (_, w) in enumerate(atis_cols):
            cell = values[ci]
            text = (" " + cell) if cell else ""
            pdf.cell(w, atis_row_h, text, border=1)
    atis_h = atis_header_h + len(atis_rows) * atis_row_h

    return y2 + atis_h + 3


def _draw_nav_table(pdf, font, pw, plan, legs, wind, magvar, perf,
                    nav_y, call_leg_idx, call_text, _note_text, usable_bottom):
    # ---------- nav table ----------
    columns = [
        ("Waypoint",        46, "L"),
        ("VOR\nInfo",       34, "C"),
        ("Alt\nft",         12, "C"),
        ("TAS\nkt",         12, "C"),
        ("Wind\n°/kt",      16, "C"),
        ("TC\n°",           12, "C"),
        ("WCA\n°",          12, "C"),
        ("Var\n°",          10, "C"),
        ("MH\n°",           12, "C"),
        ("Dist\nNM",        14, "C"),
        ("Total\nNM",       14, "C"),
        ("GS\nkt",          12, "C"),
        ("ETE\nmin",        14, "C"),
        ("Total\nmin",      14, "C"),
        ("Fuel\nL",         12, "C"),
        ("ETO / ATO",       25, "L"),
    ]
    total_w = sum(w for _, w, _ in columns)
    if total_w > pw:
        scale = pw / total_w
        columns = [(name, w * scale, a) for name, w, a in columns]

    # Columns the pilot actively scans in cruise; printed larger + bold.
    highlight = {"TC\n°", "MH\n°", "Dist\nNM", "ETE\nmin", "GS\nkt"}
    vor_col_idx = next(i for i, (n, _, _) in enumerate(columns) if n.startswith("VOR"))

    header_h = 8
    row_h = 6.5

    def _draw_nav_header(at_y: float) -> None:
        cx = pdf.l_margin
        for col_name, col_w, _ in columns:
            pdf.set_xy(cx, at_y)
            if col_name in highlight:
                pdf.set_font(font, "B", 9)
            else:
                pdf.set_font(font, "B", 7)
            pdf.multi_cell(col_w, header_h / 2, col_name, border=1, align="C")
            cx += col_w

    _draw_nav_header(nav_y)
    current_y = nav_y + header_h

    n_rows = max(len(plan.waypoints), 10)
    cum_dist = 0.0
    cum_ete = 0.0
    cum_fuel = 0.0
    for i in range(n_rows):
        if current_y + row_h > usable_bottom:
            if i >= len(plan.waypoints):
                break  # trailing empty rows: don't overflow to a new page
            # Draw footer on the current page, then continue on a fresh one.
            pdf.set_xy(pdf.l_margin, pdf.h - pdf.b_margin - 4)
            pdf.set_font(font, "I", 6)
            pdf.cell(0, 3, _note_text, align="C")
            pdf.add_page()
            current_y = pdf.t_margin
            _draw_nav_header(current_y)
            current_y += header_h

        ry = current_y
        pdf.set_fill_color(245, 245, 245) if i % 2 == 0 else pdf.set_fill_color(255, 255, 255)

        if i == 0:
            wp = plan.waypoints[0]
            row = [
                f"{wp.ident}  {wp.name}",
                wp.vor_info or wp.freq or "",
                fmt_int(wp.alt_ft or 0) if wp.alt_ft else "",
                "", "", "", "", "", "",
                "", "", "", "", "", "", "",
            ]
        elif i < len(plan.waypoints):
            leg = legs[i - 1]
            cum_dist += leg.distance_nm
            cum_ete += leg.ete_min
            cum_fuel += leg.fuel_l
            wp = plan.waypoints[i]
            first_leg = (i == 1)
            row = [
                f"{wp.ident}  {wp.name}",
                wp.vor_info or wp.freq or "",
                fmt_int(_effective_leg_alt(plan, i)) if i < len(plan.waypoints) - 1 else fmt_int(wp.alt_ft or 0),
                fmt_int(perf.get("tas_cruise", 0)) if first_leg else "",
                f"{int(wind[0]):03d}/{int(wind[1]):02d}" if first_leg else "",
                fmt_int(leg.tc),
                f"{leg.wca:+.0f}",
                f"{magvar:+.1f}" if first_leg else "",
                fmt_int(leg.mh),
                f"{leg.distance_nm:.1f}",
                f"{cum_dist:.1f}",
                fmt_int(leg.gs_kt),
                f"{leg.ete_min:.0f}",
                f"{cum_ete:.0f}",
                f"{leg.fuel_l:.1f}",
                "",
            ]
        else:
            row = [""] * len(columns)

        # Computed VOR cross-checks for this waypoint. Manual vor_info text (put
        # into the fallback string above) overrides them; that is why we clear
        # the list when vor_info is set. Precedence: vor_info > fixes > freq.
        if i < len(plan.waypoints):
            _wp = plan.waypoints[i]
            row_fixes = [] if _wp.vor_info else _wp.fixes
        else:
            row_fixes = []

        # If this row is the tower-call marker, inject the call annotation into
        # the last (ETO / ATO) cell.
        is_call_row = (call_leg_idx is not None and i == call_leg_idx and i < len(plan.waypoints) - 1)

        cx = pdf.l_margin
        for col_idx, ((name, w, align), val) in enumerate(zip(columns, row)):
            pdf.set_xy(cx, ry)
            is_last = (col_idx == len(columns) - 1)
            if is_call_row and is_last:
                pdf.set_font(font, "B", 9)
                pdf.set_text_color(180, 0, 0)
                pdf.cell(w, row_h, " " + call_text, border=1, align="L", fill=True)
                pdf.set_text_color(0, 0, 0)
            elif col_idx == vor_col_idx and row_fixes:
                # Up to two fixes stacked in the cell at 6.5 pt.
                pdf.cell(w, row_h, "", border=1, fill=True)
                pdf.set_font(font, "", 6.5)
                line_h = row_h / 2
                for li, fx in enumerate(row_fixes[:2]):
                    pdf.set_xy(cx + 0.5, ry + li * line_h + 0.3)
                    pdf.cell(w - 1.0, line_h, _fix_line(fx), border=0, align="L")
            elif name in highlight:
                pdf.set_font(font, "B", 10)
                pdf.cell(w, row_h, " " + str(val) if val else "", border=1, align=align, fill=True)
            else:
                pdf.set_font(font, "", 8)
                pdf.cell(w, row_h, " " + str(val) if val else "", border=1, align=align, fill=True)
            cx += w

        current_y += row_h

    return cum_dist, cum_ete, cum_fuel, current_y


def _draw_fuel_block(pdf, font, pw, plan, aircraft, legs, wind, magvar, perf,
                     destination, current_y, usable_bottom,
                     cum_dist, cum_ete, cum_fuel,
                     call_leg_idx, call_freq, call_freq_label, _note_text):
    # ---------- fuel summary ----------
    fuel = aircraft.get("fuel", {})
    burn = perf.get("fuel_burn_cruise_lph", 60)
    burn_climb = perf.get("fuel_burn_climb_lph", 80)
    burn_taxi = perf.get("fuel_burn_taxi_lph", 15)

    taxi_l = burn_taxi * fuel.get("taxi_minutes", 12) / 60
    climb_l = burn_climb * 5 / 60
    approach_l = burn * fuel.get("approach_minutes", 10) / 60
    reserve_l = burn * fuel.get("reserve_minutes", 30) / 60
    alternate_l = burn * fuel.get("alternate_minutes", 0) / 60
    trip_l = cum_fuel
    min_required = taxi_l + climb_l + trip_l + approach_l + alternate_l + reserve_l

    # If the summary block won't fit on the current page, start a fresh one.
    SUMMARY_H = 65  # conservative: fuel header + 10 lines + signature rect
    if current_y + 4 + SUMMARY_H > usable_bottom:
        pdf.set_xy(pdf.l_margin, pdf.h - pdf.b_margin - 4)
        pdf.set_font(font, "I", 6)
        pdf.cell(0, 3, _note_text, align="C")
        pdf.add_page()
        fuel_y = pdf.t_margin + 2
    else:
        fuel_y = current_y + 4
    fuel_x = pdf.l_margin
    fuel_w = 95
    pdf.set_xy(fuel_x, fuel_y)
    pdf.set_font(font, "B", 8)
    pdf.cell(fuel_w, 5, " Kraftstoffberechnung", border=1)
    pdf.ln(5)
    pdf.set_font(font, "", 8)
    fuel_lines = [
        ("Anlassen / Rollen", taxi_l),
        ("Steigflug", climb_l),
        ("Reiseflug (Trip)", trip_l),
        ("An- / Abflug", approach_l),
        ("Ausweichflugplatz", alternate_l),
        ("Reserve (30 min)", reserve_l),
        ("Mindest-Kraftstoffbedarf", min_required),
        ("Kraftstoff-Vorrat", fuel.get("capacity_usable_l", 0)),
        ("Extra-Kraftstoff", fuel.get("capacity_usable_l", 0) - min_required),
    ]
    for label, val in fuel_lines:
        pdf.set_xy(fuel_x, pdf.get_y())
        pdf.cell(fuel_w * 0.65, 5, " " + label, border=1)
        pdf.cell(fuel_w * 0.35, 5, f" {val:7.1f} L", border=1, align="R")
        pdf.ln(5)

    capacity = fuel.get("capacity_usable_l", 0)
    max_flight_min = (capacity / burn) * 60 if burn > 0 else 0
    safe_min = max(0, max_flight_min - 30)
    pdf.set_xy(fuel_x, pdf.get_y() + 1)
    pdf.set_font(font, "B", 8)
    pdf.cell(fuel_w * 0.65, 5, " Sichere Flugzeit (max − 30 min)", border=1)
    pdf.cell(fuel_w * 0.35, 5, f" {int(safe_min // 60)}:{int(safe_min % 60):02d}", border=1, align="R")

    # ---------- planning assumptions ----------
    foot_x = fuel_x + fuel_w + 4
    foot_y = fuel_y
    pdf.set_xy(foot_x, foot_y)
    pdf.set_font(font, "B", 8)
    pdf.cell(80, 5, " Planungsannahmen", border=1)
    pdf.ln(5)
    if call_leg_idx is not None and call_leg_idx < len(plan.waypoints) - 1:
        call_wp = plan.waypoints[call_leg_idx]
        remaining_from_call = sum(l.distance_nm for l in legs[call_leg_idx:])
        freq_part = f" {call_freq}" if call_freq else ""
        call_summary = (
            f"Ruf {destination.ident} {call_freq_label}{freq_part} ab {call_wp.ident} "
            f"({remaining_from_call:.0f} NM)"
        )
    else:
        call_summary = f"Ruf {destination.ident} TWR vor CTR/Meldepunkt"

    items = [
        f"Wind: {int(wind[0]):03d}°/{int(wind[1])} kt (uniform alle Schenkel)",
        f"Magnetische Variation: {magvar:+.1f}°",
        f"AIRAC-Zyklus: {plan.cycle or 'n/a'}",
        f"TAS Reise: {perf.get('tas_cruise', '-')} kt",
        f"Kraftstoffverbrauch: {burn:.1f} L/h",
        f"Gesamtstrecke: {cum_dist:.1f} NM",
        f"Gesamtzeit (Reise): {int(cum_ete)} min",
        f"Gesamtkraftstoff (Trip): {trip_l:.1f} L",
        call_summary,
    ]
    pdf.set_font(font, "", 8)
    for line in items:
        pdf.set_xy(foot_x, pdf.get_y())
        pdf.cell(80, 5, " " + line, border=1)
        pdf.ln(5)

    # signature / remarks block
    sig_x = foot_x + 84
    pdf.set_xy(sig_x, foot_y)
    pdf.set_font(font, "B", 8)
    sig_w = pdf.l_margin + pw - sig_x
    pdf.cell(sig_w, 5, " Bemerkungen / Unterschrift Pilot", border=1)
    pdf.rect(sig_x, foot_y + 5, sig_w, 45)

    # bottom note on the final navlog page
    pdf.set_xy(pdf.l_margin, pdf.h - pdf.b_margin - 4)
    pdf.set_font(font, "I", 6)
    pdf.cell(0, 3, _note_text, align="C")
