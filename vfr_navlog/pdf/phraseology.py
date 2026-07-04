"""Phraseology pages: FIS/Radar en route and CTR entry via Whiskey."""
from __future__ import annotations

from fpdf import FPDF

from ..model import Plan, VatsimSnapshot
from ..vatsim import _find_radar_online


def render_phraseology(pdf: FPDF, font: str, plan: Plan, aircraft: dict, vatsim: VatsimSnapshot | None, fir_icaos: list[str] | None = None) -> None:
    """Two-page phraseology: (1) FIS / Radar en route, (2) CTR entry via Whiskey."""
    dep = plan.waypoints[0]
    dest = plan.waypoints[-1]
    reg = aircraft.get("registration", "D-XXXX")
    ac_type = aircraft.get("type", "C172")

    def clean_name(s: str) -> str:
        return s.replace("- ", "-").strip(" -")

    dest_name = clean_name(dest.name or dest.ident).split()[0] or dest.ident

    tower_freq = ""
    if vatsim:
        tower_freq = vatsim.frequencies.get(dest.ident.upper(), {}).get("tower", "")
    tower_on = f" auf {tower_freq}" if tower_freq else ""

    # ── layout constants (computed once, used in all helpers via closure) ─────
    pw     = pdf.w - pdf.l_margin - pdf.r_margin
    ROLE_W = 14.0
    DE_W   = (pw - ROLE_W) / 2
    EN_W   = pw - ROLE_W - DE_W
    LH     = 4.0          # line height for dialogue rows

    SIT_W  = 52.0
    DE_V   = (pw - SIT_W) / 2
    EN_V   = pw - SIT_W - DE_V

    # colour palette
    C_PILOT = (235, 245, 255)   # pilot rows: pale blue
    C_ATC   = (255, 248, 230)   # ATC rows:   pale amber
    C_NOTE  = (252, 252, 220)   # info notes: pale yellow
    C_SEC   = (225, 225, 225)   # section headers
    C_HDR   = (210, 220, 235)   # variation table header
    C_COL   = (242, 242, 242)   # column label row

    # ── helpers ───────────────────────────────────────────────────────────────

    def note_box(text: str) -> None:
        pdf.set_font(font, "I", 7.5)
        pdf.set_fill_color(*C_NOTE)
        pdf.multi_cell(pw, 3.8, text, border=1, fill=True)
        pdf.ln(3)

    def section_bar(title: str, with_cols: bool = True) -> None:
        pdf.set_font(font, "B", 9)
        pdf.set_fill_color(*C_SEC)
        pdf.cell(pw, 5, "  " + title, border=1, fill=True)
        pdf.ln(5)
        if with_cols:
            pdf.set_fill_color(*C_COL)
            pdf.set_font(font, "B", 7.5)
            pdf.cell(ROLE_W, 4.5, "",           border=1, fill=True)
            pdf.cell(DE_W,   4.5, "  Deutsch",  border=1, fill=True)
            pdf.cell(EN_W,   4.5, "  English",  border=1, fill=True)
            pdf.ln(4.5)

    def drow(role: str, de: str, en: str, atc: bool = False) -> None:
        """One dialogue row: role chip | German text | English text."""
        bg = C_ATC if atc else C_PILOT
        y0 = pdf.get_y()
        pdf.set_fill_color(*bg)

        pdf.set_xy(pdf.l_margin, y0)
        pdf.set_font(font, "B", 7.5)
        pdf.multi_cell(ROLE_W, LH, role, border="LBR", align="C", fill=True)
        h0 = pdf.get_y() - y0

        pdf.set_xy(pdf.l_margin + ROLE_W, y0)
        pdf.set_font(font, "", 8.5)
        pdf.multi_cell(DE_W, LH, de, border="BR", fill=True)
        h1 = pdf.get_y() - y0

        pdf.set_xy(pdf.l_margin + ROLE_W + DE_W, y0)
        pdf.set_font(font, "I", 8.5)
        pdf.multi_cell(EN_W, LH, en, border="BR", fill=True)
        h2 = pdf.get_y() - y0

        pdf.set_y(y0 + max(h0, h1, h2))

    def vbar(title: str) -> None:
        pdf.ln(2)
        pdf.set_font(font, "B", 8.5)
        pdf.set_fill_color(*C_HDR)
        pdf.cell(pw, 5, "  " + title, border=1, fill=True)
        pdf.ln(5)
        pdf.set_fill_color(*C_COL)
        pdf.set_font(font, "B", 7.5)
        pdf.cell(SIT_W, 4, "  Situation", border=1, fill=True)
        pdf.cell(DE_V,  4, "  Deutsch",   border=1, fill=True)
        pdf.cell(EN_V,  4, "  English",   border=1, fill=True)
        pdf.ln(4)

    def vrow(sit: str, de: str, en: str) -> None:
        y0 = pdf.get_y()
        pdf.set_fill_color(255, 255, 255)
        pdf.set_xy(pdf.l_margin, y0)
        pdf.set_font(font, "B", 7.5)
        pdf.multi_cell(SIT_W, LH, sit, border="LBR")
        h0 = pdf.get_y() - y0
        pdf.set_xy(pdf.l_margin + SIT_W, y0)
        pdf.set_font(font, "", 8)
        pdf.multi_cell(DE_V, LH, de, border="BR")
        h1 = pdf.get_y() - y0
        pdf.set_xy(pdf.l_margin + SIT_W + DE_V, y0)
        pdf.set_font(font, "I", 8)
        pdf.multi_cell(EN_V, LH, en, border="BR")
        h2 = pdf.get_y() - y0
        pdf.set_y(y0 + max(h0, h1, h2))

    def page_footer(text: str) -> None:
        pdf.set_y(pdf.h - pdf.b_margin - 4)
        pdf.set_font(font, "I", 6)
        pdf.cell(0, 3, text, align="C")

    # ── PAGE 1: FIS / en-route Radar ─────────────────────────────────────────
    radar_info = _find_radar_online(vatsim, fir_icaos or [])
    radar_name = radar_info[0] if radar_info else None
    radar_freq = radar_info[1] if radar_info else None

    pdf.add_page()

    pdf.set_xy(pdf.l_margin, pdf.t_margin)
    pdf.set_font(font, "B", 13)
    if radar_name:
        page1_title = f"Sprechgruppen VFR  ·  1: {radar_name}  ({radar_freq})"
    else:
        page1_title = "Sprechgruppen VFR  ·  1: FIS Bremen Information"
    pdf.cell(pw, 7, page1_title, align="C")
    pdf.ln(8)
    pdf.set_font(font, "I", 8)
    pdf.cell(pw, 4,
             f"Templated für {reg} ({ac_type}), {dep.ident} → {dest.ident}.  "
             "[Eckige Klammern] vor jedem Spruch anpassen.",
             align="C")
    pdf.ln(6)

    if radar_name:
        note_box(
            f"{radar_name} ONLINE — {radar_freq} MHz (VATSIM live).  "
            "Radardienst: aktive Staffelung möglich. "
            "Erstanruf: nur Rufzeichen — nach Rückfrage Vollmeldung mit Position, Höhe, POB. "
            "Squawk wie angewiesen (kein automatisches 7000)."
        )
    else:
        note_box(
            "Bremen Information (Langen Center) = Fluginformationsdienst, keine Staffelung. "
            "Erstanruf: nur Rufzeichen — erst nach Rückfrage die Vollmeldung abgeben. "
            "POB immer nennen. Squawk VFR = 7000. Frequenz: AIP / Streckenkarte prüfen."
        )

    section_bar("A · Erstkontakt & Vollmeldung  ·  Initial contact & full position report")

    drow("PILOT",
         f"Bremen Information, {reg}.",
         f"Bremen Information, {reg}.")

    drow("FIS",
         f"{reg}, Bremen Information, bitte melden.",
         f"{reg}, Bremen Information, go ahead.",
         atc=True)

    drow("PILOT",
         f"{reg}, {ac_type}, VFR von {dep.ident} nach {dest.ident}, "
         f"[Position, z. B. 10 km nördlich Osnabrück], [2500 Fuß], "
         "[2] Personen an Bord, erbitte Verkehrsinformationen.",
         f"{reg}, {ac_type}, VFR from {dep.ident} to {dest.ident}, "
         f"[position, e.g. 10 km north of Osnabrück], [2500 feet], "
         "[2] persons on board, request traffic information.")

    drow("FIS",
         f"{reg}, identifiziert, [2500 Fuß], QNH [1018], Squawk [7631], "
         "Verkehrsinformationen soweit möglich.",
         f"{reg}, identified, [2500 feet], QNH [1018], squawk [7631], "
         "traffic information workload permitting.",
         atc=True)

    drow("PILOT",
         f"QNH [1018], Squawk [7631], {reg}.",
         f"QNH [1018], squawk [7631], {reg}.")

    section_bar("B · Verkehrsinformation & Frequenzverlassen  ·  Traffic info & leaving FIS",
                with_cols=False)

    drow("FIS",
         f"{reg}, Verkehr, [Cessna 172, 12 Uhr, 4 Meilen], entgegenkommend, [2500 Fuß], "
         "melden Sie Verkehr in Sicht.",
         f"{reg}, traffic, [Cessna 172, 12 o'clock, 4 miles], opposite direction, [2500 feet], "
         "report traffic in sight.",
         atc=True)

    drow("PILOT",
         f"Verkehr in Sicht / nicht in Sicht, {reg}.",
         f"Traffic in sight / not in sight, {reg}.")

    drow("PILOT",
         f"{reg}, erbitte Verlassen der Frequenz.",
         f"{reg}, request frequency change.")

    drow("FIS",
         f"{reg}, Frequenzwechsel genehmigt, Squawk VFR, auf Wiederhören.",
         f"{reg}, frequency change approved, squawk 7000, goodbye.",
         atc=True)

    drow("PILOT",
         f"Squawk VFR, {reg}, auf Wiederhören.",
         f"Squawk 7000, {reg}, goodbye.")

    vbar("Mögliche FIS-Antworten  ·  Possible FIS responses")

    vrow("Hohe Arbeitsbelastung\nWorkload denial",
         f"{reg}, aufgrund hoher Arbeitsbelastung kein Fluginformationsdienst möglich. "
         f"Squawk 7000, auf Wiederhören.\n→  Verstanden, Squawk 7000, {reg}.",
         f"{reg}, unable to provide FIS due to high workload. "
         f"Squawk 7000, goodbye.\n→  Roger, squawk 7000, {reg}.")

    vrow("Kein Radarkontakt\nNo radar contact",
         f"{reg}, kein Radarkontakt. Bitte Position genauer angeben.\n"
         f"→  {reg}, [5 km westlich Mast Steinkimmen], Kurs [120 Grad].",
         f"{reg}, no radar contact. Say position more precisely.\n"
         f"→  {reg}, [5 km west of Steinkimmen mast], heading [120].")

    vrow("POB-Nachfrage\nPOB query",
         f"{reg}, wie viele Personen an Bord?\n→  [2] Personen an Bord, {reg}.",
         f"{reg}, persons on board?\n→  [2] persons on board, {reg}.")

    page_footer(
        "FIS = Fluginformationsdienst — keine Staffelung, keine Separierung. "
        "Frequenzwechsel erst nach Genehmigung. Squawk 7000 beim Verlassen. "
        "Quelle: DFS Sprechfunkverfahren / VATSIM Germany KB."
    )

    # ── PAGE 2: CTR entry via Whiskey ─────────────────────────────────────────
    pdf.add_page()

    pdf.set_xy(pdf.l_margin, pdf.t_margin)
    pdf.set_font(font, "B", 13)
    pdf.cell(pw, 7,
             f"Sprechgruppen VFR  ·  2: CTR-Einflug {dest.ident} über Meldepunkt Whiskey",
             align="C")
    pdf.ln(8)
    pdf.set_font(font, "I", 8)
    pdf.cell(pw, 4,
             f"{reg} ({ac_type}), {dep.ident} → {dest.ident}, "
             f"Einflug über Whiskey, Piste [25]{tower_on}.  [Eckige Klammern] anpassen.",
             align="C")
    pdf.ln(6)

    note_box(
        "Vorher: ATIS abhören, Buchstaben und QNH notieren, max. Einflughöhe beachten (oft 2000 ft). "
        "Erstanruf ca. 10–15 NM vor der CTR-Grenze: nur Rufzeichen — dann warten! "
        "Vollmeldung mit ATIS-Buchstabe erst nach Rückfrage."
    )

    section_bar("C · Erstkontakt Tower & Einflugfreigabe  ·  Initial call & CTR entry clearance")

    drow("PILOT",
         f"{dest_name} Tower, {reg}.",
         f"{dest_name} Tower, {reg}.")

    drow("TWR",
         f"{reg}, {dest_name} Tower, bitte melden.",
         f"{reg}, {dest_name} Tower, go ahead.",
         atc=True)

    drow("PILOT",
         f"{reg}, {ac_type}, VFR von {dep.ident}, [15 km nordwestlich Whiskey], [2000 Fuß], "
         "Information [Alpha] erhalten, erbitte Einflug über Whiskey zur Landung.",
         f"{reg}, {ac_type}, VFR from {dep.ident}, [15 km northwest of Whiskey], [2000 feet], "
         "information [Alpha] received, request entry via Whiskey for landing.")

    drow("TWR",
         f"{reg}, fliegen Sie in die Kontrollzone über Whiskey, "
         "QNH [1018], erwarten Sie Piste [25].",
         f"{reg}, enter the control zone via Whiskey, "
         "QNH [1018], expect runway [25].",
         atc=True)

    drow("PILOT",
         f"Einflug über Whiskey, QNH [1018], Piste [25], {reg}.",
         f"Entering via Whiskey, QNH [1018], runway [25], {reg}.")

    drow("TWR",
         f"{reg}, melden Sie Whiskey.",
         f"{reg}, report Whiskey.",
         atc=True)

    drow("PILOT",
         f"Melde Whiskey, {reg}.",
         f"Wilco, {reg}.")

    section_bar("D · Am Meldepunkt Whiskey bis GA-Vorfeld  ·  At Whiskey through to GA apron",
                with_cols=False)

    drow("PILOT",
         f"{reg}, Whiskey, [2000 Fuß].",
         f"{reg}, Whiskey, [2000 feet].")

    drow("TWR",
         f"{reg}, fliegen Sie in den [rechten] Gegenanflug Piste [25].",
         f"{reg}, join [right] downwind runway [25].",
         atc=True)

    drow("PILOT",
         f"[Rechter] Gegenanflug Piste [25], {reg}.",
         f"[Right] downwind runway [25], {reg}.")

    drow("TWR",
         f"{reg}, Wind [250 Grad, 8 Knoten], Piste [25], Landung frei.",
         f"{reg}, wind [250 degrees, 8 knots], runway [25], cleared to land.",
         atc=True)

    drow("PILOT",
         f"Piste [25], Landung frei, {reg}.",
         f"Runway [25], cleared to land, {reg}.")

    drow("PILOT",
         f"{reg}, Piste [25] verlassen über [Alpha].",
         f"{reg}, runway [25] vacated via [Alpha].")

    drow("TWR",
         f"{reg}, rollen Sie zum GA-Vorfeld über [Alpha, Bravo], Squawk Standby.",
         f"{reg}, taxi to GA apron via [Alpha, Bravo], squawk standby.",
         atc=True)

    drow("PILOT",
         f"GA-Vorfeld über [Alpha, Bravo], Squawk Standby, {reg}.",
         f"GA apron via [Alpha, Bravo], squawk standby, {reg}.")

    drow("PILOT",
         f"{reg}, Parkposition erreicht, auf Wiederhören.",
         f"{reg}, on stand, goodbye.")

    vbar("Mögliche Tower-Antworten  ·  Possible tower responses")

    vrow("Einflug vorübergehend\nnicht möglich",
         f"{reg}, können Sie [5 Minuten] außerhalb der CTR warten?\n"
         f"→  Warte außerhalb CTR, {reg}.",
         f"{reg}, can you hold outside the CTR for [5 minutes]?\n"
         f"→  Holding outside CTR, {reg}.")

    vrow("Squawk-Zuweisung\nSquawk assignment",
         f"Squawk [7023].\n→  Squawk [7023], {reg}.",
         f"Squawk [7023].\n→  Squawk [7023], {reg}.")

    vrow("Sequenzierung hinter\nVerkehr  ·  Sequencing",
         f"{reg}, Verkehr voraus, [Piper auf Endanflug Piste 25], Verkehr in Sicht?\n"
         f"→  Verkehr in Sicht, {reg}.\n"
         f"TWR: Folgen Sie dem Verkehr, Piste [25], Landung frei.",
         f"{reg}, traffic ahead, [Piper on final runway 25], traffic in sight?\n"
         f"→  Traffic in sight, {reg}.\n"
         "TWR: Follow traffic, runway [25], cleared to land.")

    page_footer(
        f"CTR-Einflug nur mit ausdrücklicher Freigabe. ATIS vor Erstkontakt abhören. "
        f"Meldepunkt Whiskey ist {dest.ident}-spezifisch — Bezeichnung vor jedem Flug im Chart prüfen."
    )
