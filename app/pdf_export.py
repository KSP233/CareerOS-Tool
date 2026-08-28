from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import CondPageBreak, Paragraph, SimpleDocTemplate, Spacer


_NAVY = colors.HexColor("#16324F")
_MUTED = colors.HexColor("#5D6B7A")
_PDF_TRANSLATION = str.maketrans({
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2026": "...", "\u00a0": " ",
    "\u2022": "-", "\u25cf": "-", "\u25aa": "-", "\u25e6": "-",
})
_BULLET_PREFIX = re.compile(r"^\s*(?:[-*]|[^\w\s])\s+")


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")[:90] or "document"


def _pdf_safe(value: str) -> str:
    """Keep ReportLab's built-in Helvetica output predictable on Windows PDF viewers."""
    normalized = unicodedata.normalize("NFKC", value).translate(_PDF_TRANSLATION)
    return normalized.encode("cp1252", errors="replace").decode("cp1252")


def _styles(options: dict | None = None) -> dict[str, ParagraphStyle]:
    options = options or {}
    style_name = str(options.get("style", "Modern"))
    try:
        body_size = max(8.0, min(12.0, float(options.get("font_size", 9))))
    except (TypeError, ValueError):
        body_size = 9.0
    if style_name == "Classic":
        body_font, bold_font, accent = "Times-Roman", "Times-Bold", colors.HexColor("#1F2937")
    else:
        body_font, bold_font, accent = "Helvetica", "Helvetica-Bold", _NAVY
    compact = style_name == "Compact"
    body_leading = body_size + (2.4 if compact else 3.6)
    heading_leading = body_size + (3.5 if compact else 4.5)
    base = getSampleStyleSheet()["BodyText"]
    return {
        "name": ParagraphStyle("ResumeName", parent=base, fontName=bold_font, fontSize=body_size + 8.5, leading=body_size + 12.5, textColor=accent, alignment=TA_CENTER, spaceAfter=4),
        "subtitle": ParagraphStyle("Subtitle", parent=base, fontName=body_font, fontSize=max(8, body_size - .2), leading=body_leading, textColor=_MUTED, alignment=TA_CENTER, spaceAfter=8 if compact else 12),
        "heading": ParagraphStyle("Section", parent=base, fontName=bold_font, fontSize=body_size + 1.1, leading=heading_leading, textColor=accent, spaceBefore=5 if compact else 8, spaceAfter=2 if compact else 3),
        "body": ParagraphStyle("Body", parent=base, fontName=body_font, fontSize=body_size, leading=body_leading, textColor=colors.HexColor("#1F2937"), spaceAfter=2 if compact else 4),
        "bullet": ParagraphStyle("Bullet", parent=base, fontName=body_font, fontSize=body_size, leading=body_leading, leftIndent=14, firstLineIndent=-8, textColor=colors.HexColor("#1F2937"), spaceAfter=1 if compact else 2),
        "letter_title": ParagraphStyle("LetterTitle", parent=base, fontName=bold_font, fontSize=body_size + 6.5, leading=body_size + 10.5, textColor=accent, alignment=TA_LEFT, spaceAfter=10),
        "letter_meta": ParagraphStyle("LetterMeta", parent=base, fontName=body_font, fontSize=body_size, leading=body_leading, textColor=_MUTED, spaceAfter=12),
    }


def _document(path: Path, options: dict | None = None) -> SimpleDocTemplate:
    options = options or {}
    margin = {"Narrow": 0.50, "Standard": 0.68, "Comfortable": 0.82}.get(str(options.get("margins", "Standard")), 0.68)
    path.parent.mkdir(parents=True, exist_ok=True)
    return SimpleDocTemplate(str(path), pagesize=LETTER, leftMargin=margin * inch, rightMargin=margin * inch, topMargin=margin * inch, bottomMargin=margin * inch, title=path.stem, author="CareerOS", allowSplitting=1)


def _is_heading(line: str) -> bool:
    value = line.strip().lstrip("#").strip().rstrip(":")
    return bool(value) and len(value) <= 52 and (value.isupper() or value.casefold() in {"education", "experience", "skills", "projects", "summary", "certifications", "work experience", "technical skills", "languages"})


def render_resume_pdf(content: str, path: Path, options: dict | None = None) -> Path:
    styles = _styles(options); raw_lines = [line.strip() for line in content.replace("\r", "").split("\n")]
    lines = [(_pdf_safe(_BULLET_PREFIX.sub("", line)) if _BULLET_PREFIX.match(line) else _pdf_safe(line)).strip() for line in raw_lines]
    bullet_lines = [bool(_BULLET_PREFIX.match(line)) for line in raw_lines]
    first = next((line for line in lines if line), "Resume")
    consumed = False; story = [Paragraph(escape(first), styles["name"])]
    for line, is_bullet in zip(lines, bullet_lines):
        if not line or (not consumed and line == first):
            if line == first: consumed = True
            continue
        if _is_heading(line):
            story.append(CondPageBreak(0.45 * inch)); story.append(Paragraph(escape(line.lstrip("#").strip().rstrip(":")), styles["heading"]))
        elif is_bullet:
            story.append(Paragraph("- " + escape(line), styles["bullet"]))
        else:
            story.append(Paragraph(escape(line), styles["body"]))
    document = _document(path, options); document.build(story)
    return path


def render_cover_letter_pdf(letter: str, path: Path, company: str, title: str, options: dict | None = None) -> Path:
    styles = _styles(options); story = [Paragraph("Cover Letter", styles["letter_title"])]
    story.append(Paragraph(escape(_pdf_safe(title)) + "<br/>" + escape(_pdf_safe(company)), styles["letter_meta"]))
    for raw in re.split(r"\n\s*\n", _pdf_safe(letter).strip()):
        paragraph = " ".join(line.strip() for line in raw.splitlines() if line.strip())
        if paragraph:
            story.append(Paragraph(escape(paragraph), styles["body"]))
            story.append(Spacer(1, 3))
    document = _document(path, options); document.build(story)
    return path
