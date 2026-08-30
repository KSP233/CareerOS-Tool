"""Bounded compatibility shim for JobSpy's optional Markdown conversion.

CareerOS requests HTML descriptions and performs its own bounded conversion,
so JobSpy should never need this function.  Keeping the small compatibility
surface makes an unexpected upstream call safe without bundling the vulnerable
python-markdownify release forced by JobSpy 1.1.82.
"""
from __future__ import annotations


def markdownify(value: object, *args, **kwargs) -> str:
    del args, kwargs
    # Import lazily to avoid a module cycle while app.services is loading.
    from app.services import compact_job_text
    return compact_job_text(str(value or ""))
