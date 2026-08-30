"""Structural safety checks and constrained AI patches for ResumeDocument.

The generated DOCX/PDF is a projection of this model.  It is never used as
the source of truth for a subsequent AI generation.
"""
from __future__ import annotations

import copy
import re

from app.resume_models import ResumeBullet, ResumeDocument
from app.validators import ValidationError


_HEADINGS = re.compile(r"(?:^|\n)\s*(?:summary|education|experience|projects?|skills?|certifications?|languages?)\s*:?\s*(?:$|\n)", re.I)


class ResumeIntegrityValidator:
    """Reject malformed/duplicated structured resumes before persistence."""

    def validate(self, document: ResumeDocument) -> None:
        if not document.personal_info.full_name.strip():
            raise ValidationError("Resume personal information is missing a name")
        seen: set[str] = set()

        def unique(value: str, label: str) -> None:
            if not value:
                raise ValidationError(f"Resume {label} is missing its stable ID")
            if value in seen:
                raise ValidationError(f"Duplicate resume ID: {value}")
            seen.add(value)

        def safe_text(value: str, label: str) -> None:
            if _HEADINGS.search(value or ""):
                raise ValidationError(f"Suspicious section heading in {label}")

        for collection, label in ((document.experience, "experience"), (document.education, "education"), (document.projects, "project"), (document.skills, "skill group"), (document.custom_sections, "custom section")):
            for item in collection:
                unique(item.id, label)
                # Display labels such as the SkillGroup name may legitimately be
                # "SKILLS"; inspect only content-bearing values for accidental
                # embedded document structure.
                for value in (getattr(item, "company", ""), getattr(item, "title", ""), getattr(item, "institution", ""), getattr(item, "degree", ""), getattr(item, "raw_text", "")):
                    safe_text(str(value), label)
                bullets = getattr(item, "bullets", [])
                texts: set[str] = set()
                for bullet in bullets:
                    unique(bullet.id, "bullet")
                    safe_text(bullet.text, "bullet")
                    folded = re.sub(r"\s+", " ", bullet.text).casefold().strip()
                    if not folded:
                        raise ValidationError("Resume contains an empty bullet")
                    if folded in texts:
                        raise ValidationError(f"Duplicate bullet in {label}")
                    texts.add(folded)
        if len(document.custom_sections) != len({section.id for section in document.custom_sections}):
            raise ValidationError("Duplicate custom section")

    def report(self, document: ResumeDocument) -> dict:
        """Return semantic diagnostics suitable for the import-review UI."""
        issues: list[dict] = []
        try:
            self.validate(document)
        except ValidationError as exc:
            return {"status": "FAIL", "issues": [{"code": "STRUCTURAL_INVALID", "severity": "error", "entity_id": "", "message": str(exc)}]}
        for item in document.education:
            if not any((item.institution, item.degree, item.field, item.details)):
                issues.append({"code": "EDUCATION_EMPTY", "severity": "error", "entity_id": item.id, "message": "Education entity has no institution, degree, field, or details."})
            if item.institution and re.search(r"\b(bachelor|master|ph\.?d|diploma)\b", item.institution, re.I):
                issues.append({"code": "EDUCATION_DEGREE_IN_INSTITUTION", "severity": "error", "entity_id": item.id, "message": "Education institution contains degree-like content."})
            if item.degree and re.search(r"\b(university|college|institute|school)\b", item.degree, re.I):
                issues.append({"code": "EDUCATION_INSTITUTION_IN_DEGREE", "severity": "error", "entity_id": item.id, "message": "Education degree contains institution-like content."})
        if "projects" in document.section_order and not document.projects:
            issues.append({"code": "PROJECTS_UNRESOLVED", "severity": "warning", "entity_id": "", "message": "A Projects section was detected but no safe project boundaries were found."})
        if not document.languages:
            for group in document.skills:
                if re.search(r"\blanguages?\s*:", group.raw_text, re.I):
                    issues.append({"code": "LANGUAGE_INSIDE_SKILLS", "severity": "warning", "entity_id": group.id, "message": "Language data remains inside a Skills group."})
        for group in document.skills:
            if any(item.count("(") != item.count(")") for item in group.items):
                issues.append({"code": "MALFORMED_SKILL_ITEM", "severity": "error", "entity_id": group.id, "message": "A skill has unbalanced parentheses."})
        known_refs = set(document.source_block_ids)
        ownership: dict[str, set[str]] = {}
        def track(owner: str, refs: list[str]) -> None:
            for ref in refs:
                if known_refs and ref not in known_refs:
                    issues.append({"code": "INVALID_SOURCE_REF", "severity": "error", "entity_id": owner, "message": f"Unknown source block reference: {ref}"})
                ownership.setdefault(ref, set()).add(owner)
        for label, entities in (("education", document.education), ("experience", document.experience), ("project", document.projects), ("skills", document.skills)):
            for entity in entities:
                track(f"{label}:{entity.id}", list(getattr(entity, "source_refs", []) or []))
                for bullet in getattr(entity, "bullets", []) or []:
                    track(f"{label}:{entity.id}", list(getattr(bullet, "source_refs", []) or []))
        for ref, owners in ownership.items():
            families = {owner.split(":", 1)[0] for owner in owners}
            if len(families) > 1:
                issues.append({"code": "SUSPICIOUS_SOURCE_OWNERSHIP", "severity": "warning", "entity_id": ref, "message": "One source block belongs to unrelated resume entities."})
        for item in document.unresolved_items:
            critical = item.source_section.casefold() in {"education", "projects", "experience"}
            issues.append({"code": "UNRESOLVED_IMPORT_BLOCK", "severity": "error" if critical else "warning", "entity_id": item.id, "message": item.reason or "Imported content needs review."})
        if any(issue["severity"] == "error" for issue in issues): status = "REVIEW_REQUIRED"
        elif document.unresolved_items: status = "REVIEW_REQUIRED"
        elif issues: status = "PASS_WITH_WARNINGS"
        else: status = "PASS"
        return {"status": status, "issues": issues}


def apply_resume_operations(document: ResumeDocument, operations: object) -> tuple[ResumeDocument, list[dict]]:
    """Apply only explicit, same-owner content patches; never move entities."""
    if not isinstance(operations, list):
        raise ValidationError("Resume AI operations must be a list")
    working = copy.deepcopy(document)
    changes: list[dict] = []
    experiences = {item.id: item for item in working.experience}
    projects = {item.id: item for item in working.projects}
    skills = {item.id: item for item in working.skills}
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValidationError("Resume AI operation must be an object")
        kind = str(operation.get("operation") or "").strip()
        entity_type = str(operation.get("entity_type") or "").strip()
        entity_id = str(operation.get("entity_id") or "").strip()

        if kind == "replace_bullet":
            value = str(operation.get("value") or "").strip()
            if not value or _HEADINGS.search(value):
                raise ValidationError("Resume AI operation contains suspicious structural text")
            owners = experiences if entity_type == "experience" else projects if entity_type == "project" else None
            if owners is None or entity_id not in owners:
                raise ValidationError("Resume AI operation references an unknown or wrong entity")
            bullet_id = str(operation.get("bullet_id") or "")
            bullet = next((item for item in owners[entity_id].bullets if item.id == bullet_id), None)
            if bullet is None:
                raise ValidationError("Resume AI operation references an unknown bullet")
            old = bullet.text
            bullet.text = value
        elif kind == "replace_skill_items":
            if entity_type != "skills" or entity_id not in skills or not isinstance(operation.get("items"), list):
                raise ValidationError("Invalid skills operation")
            values = [str(item).strip() for item in operation["items"] if str(item).strip()]
            if (
                not values
                or len(values) != len(dict.fromkeys(item.casefold() for item in values))
                or any(_HEADINGS.search(item) for item in values)
            ):
                raise ValidationError("Invalid or duplicate skills operation")
            # Preserve the exact source-facing skill line so original-DOCX
            # rendering can replace it in place instead of rebuilding a section.
            old = skills[entity_id].raw_text or f"{skills[entity_id].name}: {', '.join(skills[entity_id].items)}"
            skills[entity_id].items = values
            skills[entity_id].raw_text = f"{skills[entity_id].name}: {', '.join(values)}"
            value = skills[entity_id].raw_text
        else:
            raise ValidationError("Unsupported resume AI operation")
        changes.append({
            "operation": kind,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "bullet_id": str(operation.get("bullet_id") or ""),
            "original": old,
            "suggested": value,
            "reason": str(operation.get("reason") or "Structured content rewrite"),
            "risk": "LOW",
        })
    ResumeIntegrityValidator().validate(working)
    return working, changes


def apply_resume_operations_best_effort(document: ResumeDocument, operations: object) -> tuple[ResumeDocument, list[dict], list[dict]]:
    """Apply independent AI patches while rejecting only invalid individual operations.

    The strict ``apply_resume_operations`` function remains the validator used by
    tests and other callers that require all-or-nothing behavior.  Generation
    uses this tolerant wrapper so one hallucinated/unsupported entity id cannot
    discard otherwise safe edits.
    """
    if not isinstance(operations, list):
        raise ValidationError("Resume AI operations must be a list")
    working = copy.deepcopy(document)
    accepted: list[dict] = []
    rejected: list[dict] = []
    for index, operation in enumerate(operations):
        try:
            working, changes = apply_resume_operations(working, [operation])
            accepted.extend(changes)
        except ValidationError as exc:
            rejected.append({
                "index": index,
                "operation": operation if isinstance(operation, dict) else {"raw": repr(operation)},
                "reason": str(exc),
            })
    ResumeIntegrityValidator().validate(working)
    return working, accepted, rejected
