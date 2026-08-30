from __future__ import annotations

import re
import unicodedata
from html import escape
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import CondPageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, HRFlowable


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
    font_family = str(options.get("font_family", ""))
    if style_name == "Classic" or font_family.casefold().startswith("times"):
        body_font, bold_font, accent = "Times-Roman", "Times-Bold", colors.HexColor("#1F2937")
    else:
        body_font, bold_font, accent = "Helvetica", "Helvetica-Bold", _NAVY
    compact = style_name == "Compact"
    spacing = max(.85, min(1.45, float(options.get("line_spacing", 1.0))))
    body_leading = (body_size + (2.4 if compact else 3.6)) * spacing
    heading_leading = (body_size + (3.5 if compact else 4.5)) * spacing
    section_before = float(options.get("section_spacing_before", 5 if compact else 8))
    section_after = float(options.get("section_spacing_after", 2 if compact else 3))
    base = getSampleStyleSheet()["BodyText"]
    return {
        "name": ParagraphStyle("ResumeName", parent=base, fontName=bold_font, fontSize=body_size + 8.5, leading=body_size + 12.5, textColor=accent, alignment=TA_CENTER, spaceAfter=4),
        "subtitle": ParagraphStyle("Subtitle", parent=base, fontName=body_font, fontSize=max(8, body_size - .2), leading=body_leading, textColor=_MUTED, alignment=TA_CENTER, spaceAfter=8 if compact else 12),
        "heading": ParagraphStyle("Section", parent=base, fontName=bold_font, fontSize=float(options.get("section_font_size", body_size + 1.1)), leading=heading_leading, textColor=accent, spaceBefore=section_before, spaceAfter=section_after),
        "body": ParagraphStyle("Body", parent=base, fontName=body_font, fontSize=body_size, leading=body_leading, textColor=colors.HexColor("#1F2937"), spaceAfter=2 if compact else 4),
        "bullet": ParagraphStyle("Bullet", parent=base, fontName=body_font, fontSize=body_size, leading=body_leading, leftIndent=14, firstLineIndent=-8, textColor=colors.HexColor("#1F2937"), spaceAfter=1 if compact else 2),
        "letter_title": ParagraphStyle("LetterTitle", parent=base, fontName=bold_font, fontSize=body_size + 6.5, leading=body_size + 10.5, textColor=accent, alignment=TA_LEFT, spaceAfter=10),
        "letter_meta": ParagraphStyle("LetterMeta", parent=base, fontName=body_font, fontSize=body_size, leading=body_leading, textColor=_MUTED, spaceAfter=12),
    }


def _document(path: Path, options: dict | None = None) -> SimpleDocTemplate:
    options = options or {}
    margin = {"Narrow": 0.50, "Standard": 0.68, "Comfortable": 0.82}.get(str(options.get("margins", "Standard")), 0.68)
    left = float(options.get("margin_left", margin)); right = float(options.get("margin_right", margin)); top = float(options.get("margin_top", margin)); bottom = float(options.get("margin_bottom", margin))
    path.parent.mkdir(parents=True, exist_ok=True)
    return SimpleDocTemplate(str(path), pagesize=LETTER, leftMargin=left * inch, rightMargin=right * inch, topMargin=top * inch, bottomMargin=bottom * inch, title=path.stem, author="CareerOS", allowSplitting=1)


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


def _unique_contact_values(info) -> list[str]:
    values = [info.location, info.phone, info.email, info.linkedin, info.github, info.website, *getattr(info, "other_lines", []), *getattr(info, "other_links", [])]
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        digits = re.sub(r"\D", "", value)
        key = f"phone:{digits[-10:]}" if len(digits) >= 10 and sum(ch.isdigit() for ch in value) >= 7 else re.sub(r"\s+", " ", value).casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _hex_color(value: str, fallback="#1F4E79"):
    try:
        return colors.HexColor(value or fallback)
    except Exception:
        return colors.HexColor(fallback)


def _activity_parts(raw: str) -> tuple[str, str]:
    # Match the DOCX renderer's common activity heading structure closely.
    parts = [part.strip() for part in str(raw or "").split("|") if part.strip()]
    if not parts:
        return "", ""
    date_index = next((i for i, part in enumerate(parts) if re.search(r"\b(?:19|20)\d{2}\b|\bPresent\b", part, re.I)), None)
    if date_index is None:
        return " | ".join(parts), ""
    return " | ".join(parts[:date_index]), " | ".join(parts[date_index:])


def render_resume_document_pdf(document, path: Path, style, layout, document_label: str = "") -> Path:
    """Render the structured resume with the same visual grammar as the DOCX.

    This is the no-Word fallback.  It intentionally mirrors the structured
    DOCX renderer (two-column dates, inline Languages under Skills, compact
    bullets) instead of flattening ResumeDocument back to plain text.
    """
    body_size = max(8.0, min(11.0, float(getattr(style, "body_font_size", 8.7) or 8.7)))
    name_size = max(body_size + 6.0, float(getattr(style, "name_font_size", 17.5) or 17.5))
    section_size = max(body_size + 1.0, float(getattr(style, "section_font_size", body_size + 1.2) or body_size + 1.2))
    accent = _hex_color(getattr(style, "accent_color", "#1F4E79"))
    text_color = _hex_color(getattr(style, "text_color", "#1F2937"), "#1F2937")
    line_factor = max(.92, min(1.25, float(getattr(style, "line_spacing", 1.0) or 1.0)))
    leading = body_size * 1.18 * line_factor
    margin_left = float(getattr(layout, "margin_left", .58) or .58)
    margin_right = float(getattr(layout, "margin_right", .58) or .58)
    margin_top = float(getattr(layout, "margin_top", .48) or .48)
    margin_bottom = float(getattr(layout, "margin_bottom", .48) or .48)
    usable_width = LETTER[0] - (margin_left + margin_right) * inch
    hidden = set(getattr(layout, "hidden_sections", []) or [])

    base = getSampleStyleSheet()["BodyText"]
    name_style = ParagraphStyle("StructuredName", parent=base, fontName="Helvetica-Bold", fontSize=name_size, leading=name_size + 1.5, textColor=accent, alignment=TA_CENTER, spaceAfter=1)
    contact_style = ParagraphStyle("StructuredContact", parent=base, fontName="Helvetica", fontSize=max(7.3, body_size - .5), leading=max(8, leading - .7), textColor=text_color, alignment=TA_CENTER, spaceAfter=2.5)
    heading_style = ParagraphStyle("StructuredHeading", parent=base, fontName="Helvetica-Bold", fontSize=section_size, leading=section_size + 1.0, textColor=accent, spaceBefore=0, spaceAfter=0)
    body_style = ParagraphStyle("StructuredBody", parent=base, fontName="Helvetica", fontSize=body_size, leading=leading, textColor=text_color, spaceBefore=0, spaceAfter=.4)
    body_bold = ParagraphStyle("StructuredBodyBold", parent=body_style, fontName="Helvetica-Bold")
    small_style = ParagraphStyle("StructuredSmall", parent=body_style, fontSize=max(7.6, body_size - .15), leading=max(8.2, leading - .25))
    bullet_style = ParagraphStyle("StructuredBullet", parent=body_style, leftIndent=10, firstLineIndent=-6.5, bulletIndent=3, spaceAfter=.15)
    right_style = ParagraphStyle("StructuredRight", parent=body_bold, alignment=2)

    story = [Paragraph(escape(_pdf_safe(document.personal_info.full_name.strip() or "Resume")), name_style)]
    contacts = _unique_contact_values(document.personal_info)
    if contacts:
        story.append(Paragraph(escape(_pdf_safe("  |  ".join(contacts))), contact_style))
    if document_label:
        label_style = ParagraphStyle("StructuredDocumentLabel", parent=base, fontName="Helvetica-Bold", fontSize=max(10.5, body_size + 1.2), leading=max(12, body_size + 2.2), textColor=accent, alignment=TA_CENTER, spaceBefore=1.5, spaceAfter=3.0)
        story.append(Paragraph(escape(_pdf_safe(document_label)), label_style))

    def section_heading(title: str):
        story.append(Spacer(1, max(1.5, float(getattr(style, "section_spacing_before", 4.2) or 4.2))))
        story.append(Paragraph(escape(_pdf_safe(title)), heading_style))
        story.append(HRFlowable(width="100%", thickness=.7, color=accent, spaceBefore=.3, spaceAfter=max(.8, float(getattr(style, "section_spacing_after", 1.1) or 1.1))))

    def two_col(left_html: str, right_text: str, *, left_style=body_style, right_bold=True, left_ratio=.78):
        left = Paragraph(left_html, left_style)
        right = Paragraph(escape(_pdf_safe(right_text)), right_style if right_bold else ParagraphStyle("tmpRight", parent=small_style, alignment=2))
        left_ratio = max(.60, min(.90, float(left_ratio)))
        table = Table([[left, right]], colWidths=[usable_width * left_ratio, usable_width * (1 - left_ratio)], hAlign="LEFT")
        table.setStyle(TableStyle([
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING", (0,0), (-1,-1), 0), ("RIGHTPADDING", (0,0), (-1,-1), 0),
            ("TOPPADDING", (0,0), (-1,-1), 0), ("BOTTOMPADDING", (0,0), (-1,-1), 0),
        ]))
        story.append(table)

    custom = {f"custom:{section.id}": section for section in document.custom_sections}
    order = list(document.section_order)
    for key in ("experience", "education", "skills", "languages", "projects", "certifications"):
        if key not in order:
            order.append(key)
    for key in custom:
        if key not in order:
            order.append(key)

    for key in order:
        if key in hidden or key == "summary":
            continue
        if key == "education" and document.education:
            section_heading("EDUCATION")
            for item in document.education:
                degree = item.degree or item.field or item.institution
                two_col(f"<b>{escape(_pdf_safe(degree))}</b>", item.date_text)
                second = []
                if item.institution:
                    second.append(escape(_pdf_safe(item.institution)))
                concentration = item.concentration or (item.field if item.degree else "")
                if concentration:
                    label = concentration if concentration.lower().startswith("concentration") else f"Concentration: {concentration}"
                    second.append(f"<i>{escape(_pdf_safe(label))}</i>")
                two_col(" | ".join(second), item.location, left_style=small_style, right_bold=False, left_ratio=.86)
                for detail in [*item.details, *item.extra_lines]:
                    story.append(Paragraph(escape(_pdf_safe(detail)), body_style))
        elif key == "experience" and document.experience:
            section_heading("EXPERIENCE")
            for item in document.experience:
                left = " | ".join(x for x in [item.title, item.company] if x)
                two_col(f"<b>{escape(_pdf_safe(left))}</b>", item.date_text)
                if item.location:
                    story.append(Paragraph(f"<i>{escape(_pdf_safe(item.location))}</i>", small_style))
                for line in item.extra_lines:
                    story.append(Paragraph("• " + escape(_pdf_safe(line)), bullet_style))
                for bullet in item.bullets:
                    story.append(Paragraph("• " + escape(_pdf_safe(bullet.text)), bullet_style))
        elif key == "skills" and document.skills:
            section_heading("TECHNICAL SKILLS")
            for group in document.skills:
                label = f"<b><font color='{getattr(style, 'accent_color', '#1F4E79')}'>{escape(_pdf_safe(group.name))}: </font></b>" if group.name else ""
                value = ", ".join(group.items) if group.items else group.raw_text
                story.append(Paragraph(label + escape(_pdf_safe(value)), body_style))
        elif key == "languages" and document.languages:
            # Match the DOCX renderer: when Languages follows Skills it is an
            # inline skills row rather than a second large section heading.
            standalone = "skills" not in document.section_order or document.section_order.index("skills") > document.section_order.index("languages")
            if standalone:
                section_heading("LANGUAGES")
            for item in document.languages:
                value = item.language + (f" ({item.proficiency})" if item.proficiency else "")
                label = f"<b><font color='{getattr(style, 'accent_color', '#1F4E79')}'>Languages: </font></b>" if not standalone else ""
                story.append(Paragraph(label + escape(_pdf_safe(value)), body_style))
        elif key == "projects" and document.projects:
            section_heading("ENGINEERING PROJECTS")
            project_gap = max(2.2, min(4.5, float(getattr(style, "section_spacing_before", 4.2) or 4.2) * .55))
            for project_index, item in enumerate(document.projects):
                pieces = [x for x in [item.name, item.subtitle] if x]
                left = " | ".join(pieces)
                two_col(f"<b>{escape(_pdf_safe(item.name or 'Project'))}</b>" + (f" | {escape(_pdf_safe(item.subtitle))}" if item.subtitle else ""), item.date_text)
                for line in item.extra_lines:
                    story.append(Paragraph("• " + escape(_pdf_safe(line)), bullet_style))
                for bullet in item.bullets:
                    story.append(Paragraph("• " + escape(_pdf_safe(bullet.text)), bullet_style))
                if project_index < len(document.projects) - 1:
                    story.append(Spacer(1, project_gap))
        elif key == "certifications" and document.certifications:
            section_heading("CERTIFICATIONS")
            for value in document.certifications:
                story.append(Paragraph(escape(_pdf_safe(str(value))), body_style))
        elif key.startswith("custom:"):
            section = custom.get(key)
            if not section or not section.visible:
                continue
            section_heading(section.title or "ADDITIONAL INFORMATION")
            activity_gap = max(2.2, min(4.5, float(getattr(style, "section_spacing_before", 4.2) or 4.2) * .55))
            activity_started = False
            for raw in section.raw_lines:
                if _BULLET_PREFIX.match(raw):
                    story.append(Paragraph("• " + escape(_pdf_safe(_BULLET_PREFIX.sub("", raw))), bullet_style))
                else:
                    left, date = _activity_parts(raw)
                    if activity_started:
                        story.append(Spacer(1, activity_gap))
                    activity_started = True
                    if date:
                        # Bold the first activity/role segment, matching DOCX.
                        parts = [part.strip() for part in left.split(" | ") if part.strip()]
                        left_html = ""
                        if parts:
                            left_html = f"<b>{escape(_pdf_safe(parts[0]))}</b>" + (" | " + " | ".join(escape(_pdf_safe(x)) for x in parts[1:]) if len(parts) > 1 else "")
                        two_col(left_html, date)
                    else:
                        story.append(Paragraph(f"<b>{escape(_pdf_safe(left))}</b>", body_style))
            if activity_started:
                story.append(Spacer(1, max(1.5, activity_gap * .65)))

    pdf = SimpleDocTemplate(str(path), pagesize=LETTER, leftMargin=margin_left*inch, rightMargin=margin_right*inch, topMargin=margin_top*inch, bottomMargin=margin_bottom*inch, title=path.stem, author="CareerOS", allowSplitting=1)
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.build(story)
    return path


def render_cv_document_pdf(document, path: Path, style, layout) -> Path:
    """Fallback PDF renderer for a Curriculum Vitae."""
    return render_resume_document_pdf(document, path, style, layout, document_label="CURRICULUM VITAE")


def render_cv_letter_pdf(letter: str, path: Path, candidate: dict, company: str, title: str, job_location: str = "", date_text: str = "", options: dict | None = None) -> Path:
    """Fallback renderer matching the DOCX application-letter layout."""
    styles = _styles({**(options or {}), "font_size": max(9.5, float((options or {}).get("font_size", 10.5)))})
    name = _pdf_safe(str(candidate.get("full_name") or "Candidate"))
    contacts = [candidate.get("location"), candidate.get("phone"), candidate.get("email"), candidate.get("linkedin")]
    contact = " | ".join(_pdf_safe(str(value).strip()) for value in contacts if str(value or "").strip())
    story = [Paragraph(escape(name), styles["letter_title"])]
    if contact:
        story.append(Paragraph(escape(contact), styles["letter_meta"]))
    if date_text:
        story.append(Paragraph(escape(_pdf_safe(date_text)), styles["body"])); story.append(Spacer(1, 8))
    recipient = "<br/>".join(escape(_pdf_safe(str(value).strip())) for value in (company, job_location) if str(value or "").strip())
    if recipient:
        story.append(Paragraph(recipient, styles["body"]))
    story.append(Spacer(1, 8)); story.append(Paragraph("<b>Re: " + escape(_pdf_safe(title)) + "</b>", styles["body"])); story.append(Spacer(1, 7))
    for raw in re.split(r"\n\s*\n", _pdf_safe(letter).strip()):
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if lines:
            story.append(Paragraph("<br/>".join(escape(line) for line in lines), styles["body"])); story.append(Spacer(1, 3))
    document = _document(path, {**(options or {}), "font_size": max(9.5, float((options or {}).get("font_size", 10.5)))})
    document.build(story)
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
