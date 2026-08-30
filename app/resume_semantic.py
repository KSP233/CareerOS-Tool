"""Strict, source-backed semantic classification for ambiguous resume blocks."""
from __future__ import annotations

import json

from app.resume_ingestion import DocumentBlock

_PROMPT = """Classify only the supplied resume blocks. Return JSON: {\"entities\": []}.
Every entity must include type, confidence (0-1), source_block_ids, and only
verbatim values present in those blocks. Supported types are education and
project. Never rewrite a resume, infer facts, or reference absent block IDs."""


class ResumeSemanticClassifier:
    """Optional adapter; callers must obtain consent before using external AI."""
    def __init__(self, ai): self.ai = ai

    def resolve(self, section: str, blocks: list[DocumentBlock]) -> list[dict]:
        if not blocks:
            return []
        payload = {"section": section, "blocks": [block.to_dict() for block in blocks]}
        result, _ = self.ai.generate_json("resume_semantic_parse", _PROMPT, json.dumps(payload, ensure_ascii=False), None, 0.0)
        entities = result.get("entities") if isinstance(result, dict) else None
        return [value for value in entities if isinstance(value, dict)] if isinstance(entities, list) else []
