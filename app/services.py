from __future__ import annotations

import difflib
import csv
import hashlib
import html
import json
import logging
import re
import shutil
import webbrowser
from datetime import datetime
from pathlib import Path

from app.ai_manager import AIManager
from app.database import Database
from app.pdf_export import render_cover_letter_pdf, render_resume_pdf, safe_filename
from app.prompts import COVER_LETTER_PROMPT, JOB_ANALYSIS_PROMPT, RESUME_OPTIMIZATION_PROMPT, TRANSLATION_PROMPT
from app.validators import recommendation, validate_job_analysis, validate_resume_changes
from config import get_paths, load_settings


logger = logging.getLogger(__name__)
_SALARY_RANGE = re.compile(
    r"(?P<currency>CA\$|C\$|CAD\s*\$?|\$)\s*(?P<low>\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)\s*"
    r"(?:-|–|—|to)\s*(?:(?:CA\$|C\$|CAD\s*\$?|\$)\s*)?(?P<high>\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)",
    re.I,
)


def generation_instruction(kind: str) -> str:
    """Return optional local user guidance without replacing the safety prompt."""
    value = load_settings().get("generation_prompts", {}).get(kind, "")
    value = str(value).strip()
    return f"\n\nUSER-APPROVED EXTRA INSTRUCTIONS:\n{value}" if value else ""


def weighted_match_score(rule_score: int, ai_score: int, settings: dict | None = None) -> int:
    """Blend repeatable rules and AI using the user's locally saved 100% split."""
    settings = settings or load_settings()
    raw_rule = settings.get("match_weights", {}).get("rule", 70)
    try:
        rule_weight = max(0, min(100, int(raw_rule)))
    except (TypeError, ValueError):
        rule_weight = 70
    return round(rule_score * rule_weight / 100 + ai_score * (100 - rule_weight) / 100)


def compact_job_text(text: str) -> str:
    """Remove scraper/Markdown spacing noise while preserving readable structure."""
    text = html.unescape(str(text or "")).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\\([\\'`*_{}\[\]()#+.!&~-])", r"\1", text)
    output: list[str] = []
    for raw_line in text.split("\n"):
        line = re.sub(r"\s+", " ", raw_line).strip()
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^\*\*(.*?)\*\*$", r"\1", line)
        line = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
        if not line:
            if output and output[-1] != "":
                output.append("")
            continue
        if re.match(r"^[*+•-]\s+", line):
            line = "• " + re.sub(r"^[*+•-]\s+", "", line)
        output.append(line)
    return "\n".join(output).strip()


def extract_job_facts(title: str, description: str) -> dict[str, object]:
    """Extract only explicit, displayable requirements from the source posting."""
    raw = f"{title}\n{description}".replace("\\-", "-").replace("\\&", "&")

    def clean(value: str) -> str:
        value = re.sub(r"[*_`#]+", "", value)
        value = re.sub(r"^[-+•\s]+", "", value)
        return re.sub(r"\s+", " ", value).strip(" -:;")

    raw_blocks = re.split(r"[\r\n]+", raw)
    lines: list[str] = []
    for block in raw_blocks:
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])", block):
            value = clean(sentence)
            if 4 <= len(value) <= 420 and value not in lines:
                lines.append(value)

    start_date = "Not stated"
    season = re.search(r"\b(Spring|Summer|Fall|Autumn|Winter)\s+(20\d{2})\b", title, re.I)
    if season:
        start_date = f"{season.group(1).title()} {season.group(2)}"
    else:
        explicit_start = re.search(
            r"\b(?:start(?:ing)? date|expected start|commenc(?:ement|ing)|begin(?:ning)?)\s*(?:is|:|-)?\s*"
            r"((?:January|February|March|April|May|June|July|August|September|October|November|December)"
            r"(?:\s+\d{1,2}(?:st|nd|rd|th)?)?(?:,?\s+20\d{2})?|(?:Spring|Summer|Fall|Autumn|Winter)\s+20\d{2}|20\d{2}-\d{2}-\d{2})",
            raw, re.I,
        )
        if explicit_start:
            start_date = clean(explicit_start.group(1))

    education_pattern = re.compile(
        r"\b(bachelor(?:'|’)?s?|master(?:'|’)?s?|ph\.?d\.?|degree|diploma|currently enrolled|"
        r"enrolled in|college or university study|engineering technology program)\b", re.I,
    )
    education = [line for line in lines if education_pattern.search(line) and "privacy" not in line.casefold()]

    requirement_section_lines: list[str] = []
    in_requirements = False
    requirement_headings = ("requirements", "about you", "qualifications", "what you bring", "what you'll bring", "what you will bring", "what you need")
    stop_headings = ("rewards", "benefits", "about us", "about the company", "additional information", "your role", "responsibilities")
    for block in raw_blocks:
        value = clean(block)
        heading = value.casefold().rstrip(":")
        if any(heading == item or heading.startswith(item + " ") for item in requirement_headings):
            in_requirements = True
            continue
        if in_requirements and any(heading == item or heading.startswith(item + " ") for item in stop_headings):
            in_requirements = False
            continue
        if in_requirements and re.match(r"^\s*[*+•-]\s+", block) and value:
            requirement_section_lines.append(value)

    other_pattern = re.compile(
        r"\b(?:minimum of\s+)?\d+\s*(?:\+|[-–]\s*\d+|to\s+\d+)?\s+years?\b|"
        r"\b(?:must|required|eligible|eligibility|security clearance|citizen(?:ship)?|work authorization|"
        r"valid driver(?:'|’)?s licen[cs]e|willing to travel|travel to|travel as necessary|onsite|on-site|"
        r"in office|office/site|shift|bilingual|proficien\w* in french|french \(spoken|professional engineer|"
        r"p\.eng\.|oiq|nuclear energy worker|radiological)\b", re.I,
    )
    other = requirement_section_lines + [line for line in lines if other_pattern.search(line)]
    other = [line for line in other if line not in education]

    def limited(values: list[str], limit: int) -> list[str]:
        result: list[str] = []
        for value in values:
            if value.casefold() not in {item.casefold() for item in result}:
                result.append(value)
            if len(result) == limit:
                break
        return result

    return {
        "start_date": start_date,
        "education": limited(education, 4),
        "other_requirements": limited(other, 14),
    }


def evaluate_requirement(requirement: str, candidate_facts: str) -> str:
    """Return a conservative status based only on resume and user-confirmed facts."""
    req = requirement.casefold().replace("’", "'")
    candidate = candidate_facts.casefold().replace("’", "'")

    negative_pairs = (
        ("driver", ("no driver's license", "do not have a driver's license")),
        ("travel", ("cannot travel", "not willing to travel")),
        ("work authorization", ("not authorized to work", "require sponsorship")),
        ("citizen", ("not a canadian citizen",)),
        ("onsite", ("cannot work onsite", "remote only")),
    )
    for signal, negatives in negative_pairs:
        if signal in req and any(value in candidate for value in negatives):
            return "DOES_NOT_MEET"

    required_years = re.search(r"\b(?:minimum of\s+)?(\d+)\s*(?:\+|[-–]\s*\d+|to\s+\d+)?\s+years?\b", req)
    if required_years:
        known_years = [int(value) for value in re.findall(r"\b(\d+)\+?\s+years?\b", candidate)]
        return "LIKELY_MET" if known_years and max(known_years) >= int(required_years.group(1)) else "NOT_CONFIRMED"

    concepts: list[tuple[str, ...]] = []
    if re.search(r"\bbachelor", req): concepts.append(("bachelor", "b.eng", "basc"))
    if re.search(r"\bmaster", req): concepts.append(("master", "m.eng", "msc"))
    if re.search(r"\bdiploma\b", req): concepts.append(("diploma",))
    disciplines = [name for name in ("mechanical", "aerospace", "civil", "electrical", "mechatronics", "software", "computer") if name in req]
    if disciplines: concepts.append(tuple(disciplines))
    if "driver" in req and "licen" in req: concepts.append(("driver's license", "drivers license", "driver license"))
    if "security clearance" in req or "federal security" in req: concepts.append(("security clearance", "federal clearance"))
    if "citizen" in req: concepts.append(("canadian citizen", "canadian citizenship"))
    if "work authorization" in req or "eligible to work" in req: concepts.append(("authorized to work", "work authorization"))
    if "french" in req: concepts.append(("french",))
    if "bilingual" in req: concepts.append(("bilingual", "french"))
    if "travel" in req: concepts.append(("willing to travel", "available to travel", "can travel", "travel as necessary"))
    if "onsite" in req or "on-site" in req or "office/site" in req: concepts.append(("onsite", "on-site", "office/site", "available for site work"))
    if "professional engineer" in req or "p.eng" in req or "oiq" in req: concepts.append(("professional engineer", "p.eng", "oiq", "eligible to be registered"))
    if "nuclear energy worker" in req or "radiological" in req: concepts.append(("nuclear energy worker", "radiological"))

    if concepts:
        return "LIKELY_MET" if all(any(option in candidate for option in group) for group in concepts) else "NOT_CONFIRMED"

    technical_terms = [
        term for term in ("asme", "csa", "nfpa", "solidworks", "solid edge", "autocad", "revit", "matlab", "catia", "ansys")
        if term in req
    ]
    if technical_terms:
        return "LIKELY_MET" if all(term in candidate for term in technical_terms) else "NOT_CONFIRMED"
    return "NOT_CONFIRMED"


class JobService:
    def __init__(self, db: Database, ai: AIManager):
        self.db, self.ai = db, ai
        self.backfill_missing_salaries()
        self.backfill_missing_start_dates()
        self.backfill_missing_rule_scores()

    @staticmethod
    def _text(value) -> str:
        if value is None:
            return ""
        try:
            import pandas as pd
            if pd.isna(value):
                return ""
        except (ImportError, TypeError, ValueError):
            pass
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    @classmethod
    def _salary(cls, row, description: str = "") -> str:
        minimum, maximum = cls._text(row.get("min_amount")), cls._text(row.get("max_amount"))
        currency = cls._text(row.get("currency"))
        interval = cls._text(row.get("interval"))
        amount = " - ".join(x for x in (minimum, maximum) if x)
        return " ".join(x for x in (amount, currency, interval) if x) or cls._salary_from_description(description)

    @staticmethod
    def _salary_from_description(description: str) -> str:
        """Infer a displayed range only when the source API did not provide salary fields."""
        matches = list(_SALARY_RANGE.finditer(str(description or "")))
        if not matches:
            return ""
        ranges = []
        for match in matches:
            try:
                low = float(match.group("low").replace(",", "")); high = float(match.group("high").replace(",", ""))
            except ValueError:
                continue
            if high >= low:
                ranges.append((low, high, match.group("currency").upper().replace(" ", "")))
        if not ranges:
            return ""
        currency = "CA$" if any(item[2].startswith("CA") or item[2] == "C$" for item in ranges) else "$"
        minimum, maximum = min(item[0] for item in ranges), max(item[1] for item in ranges)
        format_amount = lambda value: f"{value:,.2f}".rstrip("0").rstrip(".")
        qualifier = " (varies by location)" if len(ranges) > 1 else ""
        return f"{currency}{format_amount(minimum)} - {currency}{format_amount(maximum)}{qualifier}"

    def backfill_missing_salaries(self) -> None:
        """Populate blank salary cells from saved descriptions without overwriting API data."""
        try:
            for job in self.db.jobs("TRIM(salary) = ''"):
                salary = self._salary_from_description(job.get("description", ""))
                if salary:
                    self.db.update_job(job["id"], salary=salary)
        except Exception as exc:
            logger.warning("Could not backfill salary values: %s", exc)

    def backfill_missing_start_dates(self) -> None:
        """Cache explicit start dates once so the Applications page remains lightweight."""
        try:
            for job in self.db.jobs("TRIM(start_date) = ''"):
                value = extract_job_facts(job.get("title", ""), job.get("description", ""))["start_date"]
                self.db.update_job(job["id"], start_date=value)
        except Exception as exc:
            logger.warning("Could not backfill start dates: %s", exc)

    def backfill_missing_rule_scores(self) -> None:
        """Give previously unscored jobs the same safe preliminary score as new jobs."""
        try:
            context = self._resume_for_preliminary_score() or self._default_preliminary_context()
            if not context:
                return
            for job in self.db.jobs("rule_score IS NULL"):
                description = str(job.get("description") or "")
                if description:
                    self.db.update_job(job["id"], rule_score=self._rule_score(context, f"{job.get('title', '')}\n{description}", job.get("location", ""))[0])
        except Exception as exc:
            logger.warning("Could not backfill preliminary match scores: %s", exc)

    @staticmethod
    def _resume_for_preliminary_score() -> str:
        paths = get_paths()
        imported = sorted(paths.resumes_original.glob("original-*"), key=lambda path: path.stat().st_mtime)
        configured = str(load_settings().get("resume_path", "")).strip()
        source = imported[-1] if imported else Path(configured) if configured else None
        if source and source.exists() and source.suffix.casefold() in {".txt", ".md"}:
            return source.read_text(encoding="utf-8", errors="replace")
        return ""

    @staticmethod
    def _default_preliminary_context() -> str:
        """Use only the user's saved job targets until a resume is imported."""
        return " ".join(str(query) for query in load_settings().get("search", {}).get("queries", []) if str(query).strip())

    def search(self, progress=lambda message: None) -> dict:
        try:
            from jobspy import scrape_jobs
        except ImportError as exc:
            raise RuntimeError("python-jobspy is not installed") from exc
        settings = load_settings()["search"]
        resume = self._resume_for_preliminary_score() or self._default_preliminary_context()
        added = existing = errors = 0
        total = len(settings["locations"]) * len(settings["queries"])
        index = 0
        for location in settings["locations"]:
            for query in settings["queries"]:
                index += 1
                progress(f"Searching {index}/{total}: {query} in {location}")
                try:
                    frame = scrape_jobs(
                        site_name=settings["sites"], search_term=query, location=location,
                        results_wanted=settings["results_per_search"], hours_old=settings["hours_old"],
                        country_indeed="canada", distance=settings["distance"],
                        linkedin_fetch_description=True,
                    )
                    for _, row in frame.iterrows():
                        description = self._text(row.get("description"))
                        direct_url = self._text(row.get("job_url_direct"))
                        job_url = self._text(row.get("job_url"))
                        job = {
                            "company": self._text(row.get("company")), "title": self._text(row.get("title")),
                            "location": self._text(row.get("location")), "salary": self._salary(row, description),
                            "source": self._text(row.get("site")), "url": direct_url or job_url,
                            "description": description, "date_posted": self._text(row.get("date_posted")),
                            "description_hash": hashlib.sha256(description.encode("utf-8", "ignore")).hexdigest(), "start_date": extract_job_facts(self._text(row.get("title")), description)["start_date"],
                        }
                        job_id, is_new = self.db.upsert_job(job)
                        if resume and description:
                            self.db.update_job(job_id, rule_score=self._rule_score(resume, f"{job['title']}\n{description}", job["location"])[0])
                        added += int(is_new); existing += int(not is_new)
                except Exception as exc:
                    errors += 1
                    logger.warning("Job search failed for %s in %s: %s", query, location, exc)
                    progress(f"Search warning: {query} / {location}: {exc}")
        return {"added": added, "existing": existing, "errors": errors}

    def import_file(self, source: str) -> dict:
        path = Path(source)
        if path.suffix.casefold() == ".csv":
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                records = list(csv.DictReader(handle))
        elif path.suffix.casefold() == ".json":
            records = json.loads(path.read_text(encoding="utf-8"))
            records = records if isinstance(records, list) else [records]
        else:
            raise ValueError("Import a .csv or .json job file")
        added = existing = 0
        resume = self._resume_for_preliminary_score() or self._default_preliminary_context()
        for record in records:
            if not isinstance(record, dict) or not str(record.get("title") or "").strip():
                continue
            description = self._text(record.get("description"))
            job = {key: self._text(record.get(key)) for key in ("company", "title", "location", "salary", "source", "url", "date_posted")}
            job["source"] = job["source"] or "import"
            job["description"] = description
            job["salary"] = job["salary"] or self._salary_from_description(description)
            job["description_hash"] = hashlib.sha256(description.encode("utf-8", "ignore")).hexdigest()
            job["start_date"] = extract_job_facts(job["title"], description)["start_date"]
            job_id, is_new = self.db.upsert_job(job)
            if resume and description:
                self.db.update_job(job_id, rule_score=self._rule_score(resume, f"{job['title']}\n{description}", job["location"])[0])
            added += int(is_new); existing += int(not is_new)
        return {"added": added, "existing": existing}

    def add_manual(self, values: dict) -> int:
        description = values.get("description", "")
        values["salary"] = values.get("salary") or self._salary_from_description(description)
        values["description_hash"] = hashlib.sha256(description.encode()).hexdigest()
        values["start_date"] = extract_job_facts(str(values.get("title", "")), description)["start_date"]
        job_id = self.db.upsert_job(values)[0]
        resume = self._resume_for_preliminary_score() or self._default_preliminary_context()
        if resume and description:
            self.db.update_job(job_id, rule_score=self._rule_score(resume, f"{values.get('title', '')}\n{description}", values.get("location", ""))[0])
        return job_id

    @staticmethod
    def _rule_score(resume: str, description: str, location: str) -> tuple[int, list[str], list[str]]:
        tokens = set(re.findall(r"[a-zA-Z][a-zA-Z0-9+#.-]{2,}", resume.casefold()))
        requested = list(dict.fromkeys(re.findall(r"[A-Za-z][A-Za-z0-9+#.-]{2,}", description)))
        matches = [x for x in requested if x.casefold() in tokens][:20]
        common = {"engineering", "mechanical", "aerospace", "design", "python", "matlab", "simulink", "ansys", "catia", "fusion", "cfd", "fea", "testing", "cad"}
        relevant = [x for x in requested if x.casefold() in common]
        missing = [x for x in relevant if x.casefold() not in tokens]
        skill_score = min(65, 20 + len(set(x.casefold() for x in matches)) * 3)
        senior_penalty = 25 if re.search(r"\b(senior|lead|principal|manager|[5-9]\+? years|10\+? years)\b", description, re.I) else 0
        location_score = 5 if any(x in location.casefold() for x in ("ottawa", "montreal", "montréal", "toronto", "quebec", "québec", "remote")) else 0
        return max(0, min(100, skill_score + 15 + location_score - senior_penalty)), matches, missing

    def analyze(self, job_id: int, resume: str, selected_model: str | None = None) -> dict:
        job = self.db.job(job_id)
        if not job:
            raise ValueError("Job not found")
        rule_score, rule_matches, rule_missing = self._rule_score(resume, job["description"], job["location"])
        prompt = f"RESUME:\n{resume[:12000]}\n\nJOB:\nTitle: {job['title']}\nCompany: {job['company']}\nLocation: {job['location']}\n{job['description'][:12000]}"
        analysis, model = self.ai.generate_json("job_analysis", JOB_ANALYSIS_PROMPT, prompt, selected_model)
        analysis = validate_job_analysis(analysis)
        final = weighted_match_score(rule_score, analysis["ai_score"])
        analysis["match_score"] = final
        analysis["rule_score"] = rule_score
        analysis["recommendation"] = recommendation(final)
        analysis["required_matches"] = list(dict.fromkeys(rule_matches + analysis["required_matches"]))
        analysis["missing_skills"] = list(dict.fromkeys(rule_missing + analysis["missing_skills"]))
        self.db.update_job(job_id, rule_score=rule_score, ai_score=analysis["ai_score"], match_score=final,
            match_reason=analysis["reason"], strengths_json=json.dumps(analysis["strengths"]),
            missing_json=json.dumps(analysis["missing_skills"]), risks_json=json.dumps(analysis["risks"]),
            requirements_json=json.dumps(analysis["required_matches"]), preferred_json=json.dumps(analysis["preferred_matches"]),
            recommendation=analysis["recommendation"], model_used=model)
        analysis["model_used"] = model
        return analysis

    def translate(self, job_id: int, progress=lambda message: None) -> dict:
        job = self.db.job(job_id)
        if not job:
            raise ValueError("Job not found")
        source = compact_job_text(job["description"])
        source_hash = hashlib.sha256(source.encode("utf-8", "ignore")).hexdigest()
        if job.get("translation_zh") and job.get("translation_hash") == source_hash:
            return {"translation": job["translation_zh"], "model_used": job.get("translation_model") or "cached", "cached": True}
        if not source:
            raise ValueError("Job description is empty")

        chunks: list[str] = []
        current = ""
        for paragraph in source.split("\n\n"):
            pieces = [paragraph[i:i + 6000] for i in range(0, len(paragraph), 6000)] or [""]
            for piece in pieces:
                candidate = f"{current}\n\n{piece}".strip() if current else piece
                if current and len(candidate) > 6000:
                    chunks.append(current); current = piece
                else:
                    current = candidate
        if current:
            chunks.append(current)

        translations, models = [], []
        fast_model = load_settings()["models"]["fast"]
        for index, chunk in enumerate(chunks, 1):
            progress(f"Translating {index}/{len(chunks)} with {fast_model}...")
            result, model = self.ai.generate_json("translate", TRANSLATION_PROMPT, chunk, fast_model, 0.1)
            translated = compact_job_text(result.get("translation", ""))
            if not translated:
                raise ValueError(f"Translation chunk {index} was empty")
            translations.append(translated); models.append(model)
        translation = "\n\n".join(translations)
        model_used = ", ".join(dict.fromkeys(models))
        self.db.update_job(job_id, translation_zh=translation, translation_model=model_used, translation_hash=source_hash)
        return {"translation": translation, "model_used": model_used, "cached": False}

    @staticmethod
    def open_job(job: dict) -> None:
        url = str(job.get("url") or "")
        if not url.startswith(("http://", "https://")):
            raise ValueError("Job URL is not valid")
        webbrowser.open(url)


class ResumeService:
    def __init__(self, db: Database, ai: AIManager):
        self.db, self.ai, self.paths = db, ai, get_paths()

    def import_original(self, source: str) -> Path:
        src = Path(source)
        suffix = src.suffix.lower()
        if not src.exists() or suffix not in {".txt", ".md", ".pdf", ".docx"}:
            raise ValueError("Select a .txt, .md, .pdf, or .docx resume")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        source_copy = self.paths.resumes_original / f"source-{stamp}{suffix}"
        shutil.copy2(src, source_copy)
        if suffix in {".txt", ".md"}:
            text = src.read_text(encoding="utf-8", errors="replace")
        elif suffix == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(str(src))
            text = "\n\n".join((page.extract_text() or "").strip() for page in reader.pages)
        else:
            from docx import Document
            document = Document(str(src))
            blocks = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
            for table in document.tables:
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                    if cells:
                        blocks.append(" | ".join(cells))
            text = "\n".join(blocks)
        text = text.strip()
        if not text:
            source_copy.unlink(missing_ok=True)
            raise ValueError("No readable text was found. If this is a scanned PDF, convert it with OCR first.")
        target = self.paths.resumes_original / f"original-{stamp}.txt"
        target.write_text(text, encoding="utf-8")
        return target

    def original_path(self) -> Path | None:
        files = sorted(self.paths.resumes_original.glob("original-*.txt"), key=lambda path: path.stat().st_mtime)
        if files:
            return files[-1]
        configured_value = str(load_settings().get("resume_path", "")).strip()
        if not configured_value:
            return None
        configured = Path(configured_value)
        return configured if configured.exists() and configured.is_file() else None

    def original_text(self) -> str:
        path = self.original_path()
        if not path:
            raise FileNotFoundError("Import an original resume first")
        return path.read_text(encoding="utf-8", errors="replace")

    def supporting_context(self, limit: int = 24000) -> str:
        blocks, remaining = [], limit
        for record in reversed(self.db.supporting_documents()):
            extracted = Path(record["extracted_path"] or "")
            if record["extraction_status"] != "ready" or not extracted.exists() or remaining <= 0:
                continue
            text = extracted.read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                continue
            piece = text[:min(6000, remaining)]
            blocks.append(f"ADDITIONAL FILE: {record['original_name']}\n{piece}")
            remaining -= len(piece)
        return "\n\n".join(blocks)

    def candidate_context(self, include_resume: bool = True) -> str:
        profile = load_settings().get("profile", {})
        parts = []
        if include_resume:
            parts.append("RESUME:\n" + self.original_text())
        confirmed = "\n".join(f"{key.replace('_', ' ').title()}: {value}" for key, value in profile.items() if str(value).strip())
        if confirmed:
            parts.append("VERIFIED CANDIDATE INFORMATION:\n" + confirmed)
        extras = self.supporting_context()
        if extras:
            parts.append(extras)
        return "\n\n".join(parts)

    def imported_source_path(self) -> Path | None:
        files = sorted(self.paths.resumes_original.glob("source-*"), key=lambda path: path.stat().st_mtime)
        return files[-1] if files else self.original_path()

    def optimize(self, job: dict, selected_model: str | None = None, document_type: str = "Resume", replace_version: dict | None = None) -> dict:
        document_type = "CV" if str(document_type).upper() == "CV" else "Resume"
        original = self.original_text()
        extras = self.candidate_context(include_resume=False)
        format_instruction = "\nDOCUMENT TARGET: Create a comprehensive CV. Retain all relevant, explicitly supported experience and project detail; do not force it to one page." if document_type == "CV" else "\nDOCUMENT TARGET: Create a concise, tailored resume for this role."
        prompt = f"ORIGINAL RESUME:\n{original}\n\n{extras}\n\nJOB:\n{job['title']} at {job['company']}\n{job['description'][:12000]}{format_instruction}{generation_instruction('resume')}"
        result, model = self.ai.generate_json("resume_optimization", RESUME_OPTIMIZATION_PROMPT, prompt, selected_model, 0.2)
        result = validate_resume_changes(result, original)
        version_no = len(self.db.resume_versions()) + 1
        name = replace_version["version_name"] if replace_version else f"{'cv' if document_type == 'CV' else 'resume'}_v{version_no:03d}"
        content = original
        for change in result["changes"]:
            if change["risk"] != "HIGH" and change["original"] in content:
                content = content.replace(change["original"], change["suggested"], 1)
        target = self.paths.resumes_generated / f"{name}.pdf"
        render_resume_pdf(content, target, load_settings().get("resume_pdf", {}))
        record = {"job_id": job["id"], "version_name": name, "source_path": str(self.original_path()), "content": content, "changes_json": json.dumps(result["changes"]), "document_type": document_type, "model_used": model}
        if replace_version:
            self.paths.resumes_approved.joinpath(f"{name}.pdf").unlink(missing_ok=True)
            self.db.replace_resume_version(replace_version["id"], **record)
            version_id = replace_version["id"]
        else:
            version_id = self.db.add_resume_version(**record)
        return {**result, "version_id": version_id, "version_name": name, "document_type": document_type, "content": content, "model_used": model}

    @staticmethod
    def diff(original: str, modified: str) -> str:
        return "\n".join(difflib.unified_diff(original.splitlines(), modified.splitlines(), fromfile="Original", tofile="Modified", lineterm=""))

    def decide(self, version_id: int, approved: bool) -> Path | None:
        versions = {v["id"]: v for v in self.db.resume_versions()}
        version = versions[version_id]
        self.db.set_resume_decision(version_id, approved)
        if approved:
            target = self.paths.resumes_approved / f"{version['version_name']}.pdf"
            render_resume_pdf(version["content"], target, load_settings().get("resume_pdf", {}))
            return target
        return None

    def preview_pdf(self, content: str, name: str = "resume-preview") -> Path:
        target = self.paths.cache / f"{safe_filename(name)}.pdf"
        return render_resume_pdf(content, target, load_settings().get("resume_pdf", {}))


class CoverLetterService:
    def __init__(self, db: Database, ai: AIManager, resume_service: ResumeService):
        self.db, self.ai, self.resume_service, self.paths = db, ai, resume_service, get_paths()

    def generate(self, job: dict, selected_model: str | None = None) -> dict:
        resume = self.resume_service.candidate_context()
        prompt = f"RESUME:\n{resume}\n\nJOB:\n{job['title']} at {job['company']}\n{job['description'][:12000]}{generation_instruction('cover_letter')}"
        result, model = self.ai.generate_json("cover_letter", COVER_LETTER_PROMPT, prompt, selected_model, 0.45)
        letter = str(result.get("letter", "")).strip()
        if not letter:
            raise ValueError("AI did not return a cover letter")
        return {"letter": letter, "warnings": result.get("warnings", []), "model_used": model}

    def save(self, job: dict, letter: str, model: str) -> Path:
        safe = safe_filename(f"{job['company']}_{job['title']}")
        path = self.paths.cover_letters / f"{safe}_{datetime.now():%Y%m%d-%H%M%S}.pdf"
        return self._render_and_record(job, letter, model, path)

    def _render_and_record(self, job: dict, letter: str, model: str, path: Path) -> Path:
        render_cover_letter_pdf(letter, path, job["company"], job["title"], load_settings().get("resume_pdf", {}))
        self.db.add_cover_letter(job["id"], str(path), letter, model)
        return path

    def preview_pdf(self, job: dict, letter: str) -> Path:
        return render_cover_letter_pdf(letter, self.paths.cache / "cover-letter-preview.pdf", job["company"], job["title"], load_settings().get("resume_pdf", {}))


class SupportingDocumentService:
    """Stores any user-selected file, extracting local text where supported."""
    TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".xml", ".html", ".htm", ".rtf", ".log", ".ini", ".py", ".c", ".cpp", ".h"}

    def __init__(self, db: Database):
        self.db, self.paths = db, get_paths()

    def _extract(self, source: Path) -> tuple[str, str]:
        suffix = source.suffix.lower()
        if suffix in self.TEXT_EXTENSIONS:
            text = source.read_text(encoding="utf-8", errors="replace")
            if suffix == ".rtf":
                text = re.sub(r"\\[a-z]+-?\d* ?", "", text).replace("{", "").replace("}", "")
            return text.strip(), "ready"
        if suffix == ".pdf":
            from pypdf import PdfReader
            return "\n\n".join((page.extract_text() or "").strip() for page in PdfReader(str(source)).pages).strip(), "ready"
        if suffix == ".docx":
            from docx import Document
            document = Document(str(source)); blocks = [p.text.strip() for p in document.paragraphs if p.text.strip()]
            for table in document.tables:
                blocks.extend(" | ".join(cell.text.strip() for cell in row.cells if cell.text.strip()) for row in table.rows)
            return "\n".join(blocks).strip(), "ready"
        if suffix == ".xlsx":
            from openpyxl import load_workbook
            book = load_workbook(str(source), read_only=True, data_only=True); blocks = []
            for sheet in book.worksheets:
                blocks.append(f"SHEET: {sheet.title}")
                for row in sheet.iter_rows(values_only=True):
                    values = [str(value) for value in row if value is not None and str(value).strip()]
                    if values: blocks.append(" | ".join(values))
            return "\n".join(blocks).strip(), "ready"
        return "", "stored_unreadable"

    def import_file(self, source: str) -> dict:
        src = Path(source)
        if not src.exists() or not src.is_file():
            raise ValueError("Select a file")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        destination = self.paths.supporting_documents / f"{stamp}-{safe_filename(src.stem)}{src.suffix.lower()}"
        shutil.copy2(src, destination)
        try:
            text, status = self._extract(src)
        except Exception as exc:
            logger.warning("Could not extract supporting document %s: %s", src.name, type(exc).__name__)
            text, status = "", "stored_unreadable"
        extracted_path = None
        if text:
            extracted = self.paths.supporting_documents / f"{stamp}-{safe_filename(src.stem)}.extracted.txt"
            extracted.write_text(text, encoding="utf-8")
            extracted_path = str(extracted)
        elif status == "ready":
            status = "stored_unreadable"
        document_id = self.db.add_supporting_document(src.name, str(destination), extracted_path, status, len(text))
        return {"id": document_id, "name": src.name, "status": status, "characters": len(text)}

    def remove(self, document_id: int) -> None:
        record = self.db.remove_supporting_document(document_id)
        if not record:
            return
        for value in (record.get("stored_path"), record.get("extracted_path")):
            if value:
                Path(value).unlink(missing_ok=True)
