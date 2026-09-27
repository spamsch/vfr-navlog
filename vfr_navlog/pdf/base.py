"""Shared PDF primitives: the document subclass, font install, small formatters."""
from __future__ import annotations

from pathlib import Path

from fpdf import FPDF

# One Arial set per platform (macOS, Windows); the first complete set wins.
FONT_SETS = [
    [
        ("/System/Library/Fonts/Supplemental/Arial.ttf", ""),
        ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "B"),
        ("/System/Library/Fonts/Supplemental/Arial Italic.ttf", "I"),
        ("/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf", "BI"),
    ],
    [
        ("C:/Windows/Fonts/arial.ttf", ""),
        ("C:/Windows/Fonts/arialbd.ttf", "B"),
        ("C:/Windows/Fonts/ariali.ttf", "I"),
        ("C:/Windows/Fonts/arialbi.ttf", "BI"),
    ],
]


def install_fonts(pdf: FPDF) -> str:
    """Register a Unicode TTF if available; otherwise fall back to core Helvetica."""
    for candidates in FONT_SETS:
        if all(Path(p).exists() for p, _ in candidates):
            for path, style in candidates:
                pdf.add_font("NavFont", style, path)
            return "NavFont"
    return "Helvetica"


class NavlogPDF(FPDF):
    def __init__(self):
        super().__init__(orientation="L", unit="mm", format="A4")
        self.set_auto_page_break(False)
        self.set_margins(8, 8, 8)
        self.add_page()


def hms(minutes: float) -> str:
    if minutes <= 0:
        return ""
    total_sec = int(round(minutes * 60))
    h, rem = divmod(total_sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_int(x: float, width: int = 0) -> str:
    return f"{int(round(x)):>{width}d}"
