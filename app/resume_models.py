"""Typed, JSON-safe resume models used by CareerOS Resume System V2."""
from __future__ import annotations

from dataclasses import MISSING, asdict, dataclass, field as dc_field
from typing import Any


@dataclass
class PersonalInfo:
    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    website: str = ""
    other_lines: list[str] = dc_field(default_factory=list)
    other_links: list[str] = dc_field(default_factory=list)


@dataclass
class ResumeBullet:
    id: str = ""
    text: str = ""
    source_refs: list[str] = dc_field(default_factory=list)
    parser_confidence: float = 0.0


@dataclass
class ExperienceItem:
    id: str = ""
    company: str = ""
    title: str = ""
    location: str = ""
    start_date: str = ""
    end_date: str = ""
    date_text: str = ""
    bullets: list[ResumeBullet] = dc_field(default_factory=list)
    extra_lines: list[str] = dc_field(default_factory=list)
    source_refs: list[str] = dc_field(default_factory=list)
    parser_confidence: float = 0.0


@dataclass
class EducationItem:
    id: str = ""
    institution: str = ""
    degree: str = ""
    field: str = ""
    concentration: str = ""
    location: str = ""
    start_date: str = ""
    end_date: str = ""
    date_text: str = ""
    details: list[str] = dc_field(default_factory=list)
    extra_lines: list[str] = dc_field(default_factory=list)
    source_refs: list[str] = dc_field(default_factory=list)
    parser_confidence: float = 0.0


@dataclass
class ProjectItem:
    id: str = ""
    name: str = ""
    subtitle: str = ""
    organization: str = ""
    role: str = ""
    location: str = ""
    start_date: str = ""
    end_date: str = ""
    date_text: str = ""
    bullets: list[ResumeBullet] = dc_field(default_factory=list)
    technologies: list[str] = dc_field(default_factory=list)
    url: str = ""
    extra_lines: list[str] = dc_field(default_factory=list)
    source_refs: list[str] = dc_field(default_factory=list)
    parser_confidence: float = 0.0


@dataclass
class SkillGroup:
    id: str = ""
    name: str = ""
    items: list[str] = dc_field(default_factory=list)
    raw_text: str = ""
    source_refs: list[str] = dc_field(default_factory=list)
    parser_confidence: float = 0.0


@dataclass
class LanguageItem:
    id: str = ""
    language: str = ""
    proficiency: str = ""
    source_refs: list[str] = dc_field(default_factory=list)
    parser_confidence: float = 0.0


@dataclass
class UnresolvedItem:
    id: str = ""
    raw_text: str = ""
    source_section: str = ""
    source_refs: list[str] = dc_field(default_factory=list)
    reason: str = ""
    candidate_types: list[str] = dc_field(default_factory=list)
    confidence: float = 0.0


@dataclass
class ResumeSection:
    id: str = ""
    section_type: str = "custom"
    title: str = ""
    raw_lines: list[str] = dc_field(default_factory=list)
    visible: bool = True


@dataclass
class ResumeDocument:
    schema_version: int = 3
    personal_info: PersonalInfo = dc_field(default_factory=PersonalInfo)
    summary: str = ""
    experience: list[ExperienceItem] = dc_field(default_factory=list)
    education: list[EducationItem] = dc_field(default_factory=list)
    projects: list[ProjectItem] = dc_field(default_factory=list)
    skills: list[SkillGroup] = dc_field(default_factory=list)
    certifications: list[str] = dc_field(default_factory=list)
    languages: list[LanguageItem] = dc_field(default_factory=list)
    custom_sections: list[ResumeSection] = dc_field(default_factory=list)
    unresolved_items: list[UnresolvedItem] = dc_field(default_factory=list)
    section_order: list[str] = dc_field(default_factory=lambda: ["experience", "education", "skills", "projects", "certifications", "languages"])
    source_text: str = ""
    source_block_ids: list[str] = dc_field(default_factory=list)
    parser_version: str = "hybrid-v1"
    semantic_classifier_version: str = "none"
    import_status: str = "REVIEW_REQUIRED"
    import_issues: list[dict[str, Any]] = dc_field(default_factory=list)


@dataclass
class ResumeStyle:
    template_id: str = "modern"
    font_family: str = "Arial"
    body_font_size: float = 9.0
    name_font_size: float = 17.5
    section_font_size: float = 10.3
    line_spacing: float = 1.0
    paragraph_spacing: float = 1.1
    section_spacing_before: float = 6.0
    section_spacing_after: float = 2.5
    bullet_spacing: float = 1.1
    text_color: str = "#1F2937"
    accent_color: str = "#1F4E79"


@dataclass
class ResumeLayout:
    page_size: str = "Letter"
    margin_top: float = 0.48
    margin_bottom: float = 0.48
    margin_left: float = 0.58
    margin_right: float = 0.58
    layout_mode: str = "single_column"
    section_order: list[str] = dc_field(default_factory=list)
    hidden_sections: list[str] = dc_field(default_factory=list)
    column_gap: float = 0.18
    left_column_ratio: float = 0.34


def resume_document_to_dict(document: ResumeDocument) -> dict[str, Any]:
    return asdict(document)


def _bullets(values: list[dict[str, Any]] | None) -> list[ResumeBullet]:
    return [ResumeBullet(id=str(value.get("id", "")), text=str(value.get("text", "")), source_refs=list(value.get("source_refs") or []), parser_confidence=float(value.get("parser_confidence", 0) or 0)) for value in values or [] if isinstance(value, dict)]


def resume_document_from_dict(value: dict[str, Any] | None) -> ResumeDocument:
    value = value or {}
    person = value.get("personal_info") if isinstance(value.get("personal_info"), dict) else {}
    document = ResumeDocument(schema_version=int(value.get("schema_version", 1) or 1), personal_info=PersonalInfo(**{key: person.get(key, [] if key == "other_lines" else "") for key in PersonalInfo.__dataclass_fields__}))
    for key in ("summary", "certifications", "section_order", "source_text", "source_block_ids", "parser_version", "semantic_classifier_version", "import_status", "import_issues"):
        if key in value:
            setattr(document, key, value[key] if key in {"certifications", "languages", "section_order", "source_block_ids", "import_issues"} else str(value[key] or ""))
    document.experience = [ExperienceItem(id=str(item.get("id", "")), company=str(item.get("company", "")), title=str(item.get("title", "")), location=str(item.get("location", "")), start_date=str(item.get("start_date", "")), end_date=str(item.get("end_date", "")), date_text=str(item.get("date_text", "")), bullets=_bullets(item.get("bullets")), extra_lines=list(item.get("extra_lines") or []), source_refs=list(item.get("source_refs") or []), parser_confidence=float(item.get("parser_confidence", 0) or 0)) for item in value.get("experience", []) if isinstance(item, dict)]
    document.education = [EducationItem(id=str(item.get("id", "")), institution=str(item.get("institution", "")), degree=str(item.get("degree", "")), field=str(item.get("field", "")), concentration=str(item.get("concentration", "")), location=str(item.get("location", "")), start_date=str(item.get("start_date", "")), end_date=str(item.get("end_date", "")), date_text=str(item.get("date_text", "")), details=list(item.get("details") or []), extra_lines=list(item.get("extra_lines") or []), source_refs=list(item.get("source_refs") or []), parser_confidence=float(item.get("parser_confidence", 0) or 0)) for item in value.get("education", []) if isinstance(item, dict)]
    document.projects = [ProjectItem(id=str(item.get("id", "")), name=str(item.get("name", "")), subtitle=str(item.get("subtitle", "")), organization=str(item.get("organization", "")), role=str(item.get("role", "")), location=str(item.get("location", "")), start_date=str(item.get("start_date", "")), end_date=str(item.get("end_date", "")), date_text=str(item.get("date_text", "")), bullets=_bullets(item.get("bullets")), technologies=list(item.get("technologies") or []), url=str(item.get("url", "")), extra_lines=list(item.get("extra_lines") or []), source_refs=list(item.get("source_refs") or []), parser_confidence=float(item.get("parser_confidence", 0) or 0)) for item in value.get("projects", []) if isinstance(item, dict)]
    document.skills = [SkillGroup(id=str(item.get("id", "")), name=str(item.get("name", "")), items=list(item.get("items") or []), raw_text=str(item.get("raw_text", "")), source_refs=list(item.get("source_refs") or []), parser_confidence=float(item.get("parser_confidence", 0) or 0)) for item in value.get("skills", []) if isinstance(item, dict)]
    document.languages = [LanguageItem(id=str(item.get("id", "")), language=str(item.get("language", "")), proficiency=str(item.get("proficiency", "")), source_refs=list(item.get("source_refs") or []), parser_confidence=float(item.get("parser_confidence", 0) or 0)) if isinstance(item, dict) else LanguageItem(id=f"language-{index}", language=str(item)) for index, item in enumerate(value.get("languages", []), 1)]
    document.custom_sections = [ResumeSection(id=str(item.get("id", "")), section_type=str(item.get("section_type", "custom")), title=str(item.get("title", "")), raw_lines=list(item.get("raw_lines") or []), visible=bool(item.get("visible", True))) for item in value.get("custom_sections", []) if isinstance(item, dict)]
    document.unresolved_items = [UnresolvedItem(id=str(item.get("id", "")), raw_text=str(item.get("raw_text", "")), source_section=str(item.get("source_section", "")), source_refs=list(item.get("source_refs") or []), reason=str(item.get("reason", "")), candidate_types=list(item.get("candidate_types") or []), confidence=float(item.get("confidence", 0) or 0)) for item in value.get("unresolved_items", []) if isinstance(item, dict)]
    return document


def _defaults(cls, value: dict[str, Any] | None) -> dict[str, Any]:
    source = value or {}; result = {}
    for key, definition in cls.__dataclass_fields__.items():
        if key in source: result[key] = source[key]
        elif definition.default is not MISSING: result[key] = definition.default
        else: result[key] = definition.default_factory()
    return result


def resume_style_to_dict(style: ResumeStyle) -> dict[str, Any]: return asdict(style)
def resume_style_from_dict(value: dict[str, Any] | None) -> ResumeStyle: return ResumeStyle(**_defaults(ResumeStyle, value))
def resume_layout_to_dict(layout: ResumeLayout) -> dict[str, Any]: return asdict(layout)
def resume_layout_from_dict(value: dict[str, Any] | None) -> ResumeLayout: return ResumeLayout(**_defaults(ResumeLayout, value))
