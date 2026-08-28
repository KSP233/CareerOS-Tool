from __future__ import annotations

import json
import re
from typing import Any


class ValidationError(ValueError):
    pass


def extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if "```" in text:
        chunks = text.split("```")
        text = next((c.removeprefix("json").strip() for c in chunks if "{" in c and "}" in c), text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValidationError("AI response did not contain JSON")
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid AI JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError("AI JSON must be an object")
    return value


def validate_job_analysis(data: dict) -> dict:
    required = {"ai_score", "required_matches", "preferred_matches", "missing_skills", "strengths", "risks", "reason", "recommendation"}
    missing = required - data.keys()
    if missing:
        raise ValidationError(f"Missing fields: {', '.join(sorted(missing))}")
    data["ai_score"] = max(0, min(100, int(data["ai_score"])))
    allowed = {"EXCELLENT", "GOOD", "POSSIBLE", "WEAK", "POOR"}
    if data["recommendation"] not in allowed:
        raise ValidationError("Invalid recommendation")
    for key in ("required_matches", "preferred_matches", "missing_skills", "strengths", "risks"):
        if not isinstance(data[key], list):
            raise ValidationError(f"{key} must be a list")
    return data


def validate_resume_changes(data: dict, original: str) -> dict:
    changes = data.get("changes")
    if not isinstance(changes, list):
        raise ValidationError("Resume changes must be a list")
    original_numbers = set(re.findall(r"\b\d+(?:\.\d+)?%?\b", original))
    protected_terms = {
        "solidworks", "catia", "creo", "inventor", "autocad", "ansys", "abaqus",
        "matlab", "simulink", "python", "c++", "java", "sql", "gd&t", "cfd", "fea",
        "six sigma", "pmp", "lean", "sap", "jira", "labview",
    }
    original_folded = original.casefold()
    for change in changes:
        if not isinstance(change, dict) or not {"original", "suggested", "reason", "risk"} <= change.keys():
            raise ValidationError("Invalid resume change")
        if change["original"] not in original:
            change["risk"] = "HIGH"
            change["warning"] = "Original text was not found verbatim in the source resume."
        new_numbers = set(re.findall(r"\b\d+(?:\.\d+)?%?\b", change["suggested"])) - original_numbers
        if new_numbers:
            change["risk"] = "HIGH"
            change["warning"] = f"Potential unsupported numbers: {', '.join(sorted(new_numbers))}"
        new_terms = sorted(term for term in protected_terms if term in change["suggested"].casefold() and term not in original_folded)
        if new_terms:
            change["risk"] = "HIGH"
            change["warning"] = f"Potential unsupported skills/tools: {', '.join(new_terms)}"
    return data


def recommendation(score: int) -> str:
    if score >= 85:
        return "EXCELLENT"
    if score >= 75:
        return "GOOD"
    if score >= 60:
        return "POSSIBLE"
    if score >= 40:
        return "WEAK"
    return "POOR"
