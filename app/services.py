"""CareerOS clean-room job, document, and manual-application services."""
from __future__ import annotations

import csv, difflib, hashlib, html, json, logging, re, shutil, webbrowser
from datetime import datetime
from pathlib import Path

from app.ai_manager import AIManager
from app.database import Database
from app.pdf_export import render_cover_letter_pdf, render_resume_pdf, safe_filename
from app.prompts import COVER_LETTER_PROMPT, JOB_ANALYSIS_PROMPT, RESUME_OPTIMIZATION_PROMPT, TRANSLATION_PROMPT
from app.validators import recommendation, validate_job_analysis, validate_resume_changes
from config import get_paths, load_settings

log = logging.getLogger(__name__)
_PAY = re.compile(r"(?P<mark>CA\$|C\$|CAD\s*\$?|\$)\s*(?P<lo>\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)\s*(?:-|–|—|to)\s*(?:(?:CA\$|C\$|CAD\s*\$?|\$)\s*)?(?P<hi>\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)", re.I)

def generation_instruction(kind: str) -> str:
    note = str(load_settings().get("generation_prompts", {}).get(kind, "")).strip()
    return f"\n\nUSER-APPROVED EXTRA INSTRUCTIONS:\n{note}" if note else ""

def weighted_match_score(rule_score: int, ai_score: int, settings: dict | None = None) -> int:
    value = (settings or load_settings()).get("match_weights", {}).get("rule", 70)
    try: rule = min(100, max(0, int(value)))
    except (ValueError, TypeError): rule = 70
    return round((rule_score * rule + ai_score * (100 - rule)) / 100)

def compact_job_text(text: str) -> str:
    """Normalize copied or scraped posting text without changing its facts."""
    blocks, last_blank = [], False
    source = html.unescape(str(text or "")).replace("\r", "").replace("\\-", "-").replace("\\'", "'")
    for raw in source.split("\n"):
        line = re.sub(r"\s+", " ", raw).strip(); line = re.sub(r"^#{1,6}\s*", "", line); line = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", line)
        if not line:
            if blocks and not last_blank: blocks.append("")
            last_blank = True; continue
        if re.match(r"^[*+•-]\s+", line): line = "• " + re.sub(r"^[*+•-]\s+", "", line)
        blocks.append(line); last_blank = False
    return "\n".join(blocks).strip()

def extract_job_facts(title: str, description: str) -> dict[str, object]:
    """Extract only plainly stated dates and requirements for display; do not infer them."""
    raw = f"{title}\n{description}"; lines=[]
    for part in re.split(r"[\r\n]+", raw):
        item = re.sub(r"[*_`#]+", "", part).strip(" -:;\t")
        if 4 <= len(item) <= 420 and item not in lines: lines.append(item)
    season = re.search(r"\b(Spring|Summer|Fall|Autumn|Winter)\s+(20\d{2})\b", title, re.I)
    dated = re.search(r"\b(?:start(?:ing)? date|expected start|commenc(?:ement|ing)|begin(?:ning)?)\s*(?:is|:|-)?\s*((?:January|February|March|April|May|June|July|August|September|October|November|December)(?:\s+\d{1,2}(?:st|nd|rd|th)?)?(?:,?\s+20\d{2})?|(?:Spring|Summer|Fall|Autumn|Winter)\s+20\d{2}|20\d{2}-\d{2}-\d{2})", raw, re.I)
    start = f"{season.group(1).title()} {season.group(2)}" if season else (dated.group(1).strip() if dated else "Not stated")
    education = re.compile(r"\b(bachelor(?:'|’)?s?|master(?:'|’)?s?|ph\.?d\.?|degree|diploma|currently enrolled|engineering technology program)\b", re.I)
    other = re.compile(r"\b(?:\d+\s*(?:\+|[-–]\s*\d+)?\s+years?|must|required|eligible|clearance|citizen|work authorization|driver(?:'|’)??s? licen[cs]e|travel|on-?site|shift|bilingual|french|professional engineer|p\.eng\.|oiq|communication)\b", re.I)
    def unique(values, limit):
        seen=set(); result=[]
        for value in values:
            key=value.casefold()
            if key not in seen: result.append(value); seen.add(key)
            if len(result) == limit: break
        return result
    edu = unique([line for line in lines if education.search(line) and "privacy" not in line.casefold()], 4)
    return {"start_date":start, "education":edu, "other_requirements":unique([line for line in lines if other.search(line) and line not in edu],14)}

def evaluate_requirement(requirement: str, candidate_facts: str) -> str:
    req, facts = requirement.casefold(), candidate_facts.casefold()
    denials=(("driver",("no driver's license","do not have a driver's license")),("travel",("cannot travel","not willing to travel")),("authorization",("not authorized","require sponsorship")),("onsite",("remote only","cannot work onsite")))
    if any(flag in req and any(no in facts for no in nos) for flag,nos in denials): return "DOES_NOT_MEET"
    years=re.search(r"\b(?:minimum of\s+)?(\d+)\s*(?:\+|[-–]\s*\d+|to\s+\d+)?\s+years?",req)
    if years:
        known=[int(v) for v in re.findall(r"\b(\d+)\+?\s+years?",facts)]; return "LIKELY_MET" if known and max(known)>=int(years.group(1)) else "NOT_CONFIRMED"
    groups=[]
    if "bachelor" in req: groups.append(("bachelor","b.eng","basc"))
    if "master" in req: groups.append(("master","m.eng","msc"))
    disciplines=tuple(x for x in ("mechanical","aerospace","civil","electrical","mechatronics","software","computer") if x in req)
    if disciplines: groups.append(disciplines)
    if "french" in req: groups.append(("french",))
    if "work authorization" in req: groups.append(("work authorization","authorized to work"))
    return "LIKELY_MET" if groups and all(any(word in facts for word in group) for group in groups) else "NOT_CONFIRMED"

class JobService:
    def __init__(self, db: Database, ai: AIManager):
        self.db,self.ai=db,ai; self._refresh_derived_fields()

    @staticmethod
    def _value(value) -> str:
        if value is None: return ""
        try:
            import pandas as pd
            if pd.isna(value): return ""
        except (ImportError,TypeError,ValueError): pass
        return value.isoformat() if hasattr(value,"isoformat") else str(value)

    @staticmethod
    def _description_hash(text: str) -> str: return hashlib.sha256(text.encode("utf-8","ignore")).hexdigest()

    @classmethod
    def _salary_from_description(cls, description: str) -> str:
        values=[]
        for match in _PAY.finditer(str(description or "")):
            try: lo,hi=float(match["lo"].replace(",","")),float(match["hi"].replace(",",""))
            except ValueError: continue
            if hi >= lo: values.append((lo,hi,match["mark"].upper().replace(" ","")))
        if not values: return ""
        money=lambda n: f"{n:,.2f}".rstrip("0").rstrip(".")
        sign="CA$" if any(mark.startswith("CA") or mark=="C$" for _,_,mark in values) else "$"
        return f"{sign}{money(min(x[0] for x in values))} - {sign}{money(max(x[1] for x in values))}" + (" (varies by location)" if len(values)>1 else "")

    @classmethod
    def _salary(cls, row, description: str) -> str:
        low,high,unit,currency=(cls._value(row.get(k)) for k in ("min_amount","max_amount","interval","currency"))
        stated=" ".join(x for x in (" - ".join(x for x in (low,high) if x),currency,unit) if x)
        return stated or cls._salary_from_description(description)

    def _refresh_derived_fields(self) -> None:
        context=self._resume_text() or self._search_context()
        for job in self.db.jobs():
            changes={}; description=str(job.get("description") or "")
            if not str(job.get("salary") or "").strip() and description: changes["salary"]=self._salary_from_description(description)
            if not str(job.get("start_date") or "").strip() and description: changes["start_date"]=extract_job_facts(job.get("title",""),description)["start_date"]
            if job.get("rule_score") is None and context and description: changes["rule_score"]=self._rule_score(context,job.get("title","")+"\n"+description,job.get("location","")[0])
            if changes: self.db.update_job(job["id"],**changes)

    @staticmethod
    def _search_context() -> str: return " ".join(map(str,load_settings().get("search",{}).get("queries",[])))
    @staticmethod
    def _resume_text() -> str:
        paths=get_paths(); copies=sorted(paths.resumes_original.glob("original-*.txt"),key=lambda p:p.stat().st_mtime); configured=str(load_settings().get("resume_path","")).strip(); path=copies[-1] if copies else (Path(configured) if configured else None)
        return path.read_text(encoding="utf-8",errors="replace") if path and path.is_file() and path.suffix.casefold() in {".txt",".md"} else ""

    @staticmethod
    def _rule_score(candidate: str, posting: str, location: str) -> tuple[int,list[str],list[str]]:
        known=set(re.findall(r"[a-z][a-z0-9+#.-]{2,}",candidate.casefold())); requested=list(dict.fromkeys(re.findall(r"[A-Za-z][A-Za-z0-9+#.-]{2,}",posting)))
        matches=[word for word in requested if word.casefold() in known][:20]; keywords={"engineering","mechanical","aerospace","design","python","matlab","simulink","ansys","catia","fusion","cfd","fea","testing","cad"}; missing=[word for word in requested if word.casefold() in keywords and word.casefold() not in known]
        skill=min(65,20+3*len({word.casefold() for word in matches})); senior=25 if re.search(r"\b(senior|lead|principal|manager|[5-9]\+? years|10\+? years)\b",posting,re.I) else 0; location_bonus=5 if any(city in str(location).casefold() for city in ("ottawa","montreal","montréal","toronto","quebec","québec","remote")) else 0
        return max(0,min(100,skill+15+location_bonus-senior)),matches,missing

    def _store(self, raw: dict, context: str) -> bool:
        description=self._value(raw.get("description")); title=self._value(raw.get("title")); job={"company":self._value(raw.get("company")),"title":title,"location":self._value(raw.get("location")),"salary":self._value(raw.get("salary")) or self._salary(raw,description),"source":self._value(raw.get("site") or raw.get("source")),"url":self._value(raw.get("job_url_direct") or raw.get("job_url") or raw.get("url")),"description":description,"date_posted":self._value(raw.get("date_posted")),"description_hash":self._description_hash(description),"start_date":extract_job_facts(title,description)["start_date"]}
        job_id,added=self.db.upsert_job(job)
        if context and description: self.db.update_job(job_id,rule_score=self._rule_score(context,title+"\n"+description,job["location"])[0])
        return added

    def search(self, progress=lambda message: None) -> dict:
        try: from jobspy import scrape_jobs
        except ImportError as exc: raise RuntimeError("python-jobspy is not installed") from exc
        settings=load_settings()["search"]; context=self._resume_text() or self._search_context(); added=existing=errors=index=0; total=len(settings["locations"])*len(settings["queries"])
        for location in settings["locations"]:
            for query in settings["queries"]:
                index+=1; progress(f"Searching {index}/{total}: {query} in {location}")
                try:
                    frame=scrape_jobs(site_name=settings["sites"],search_term=query,location=location,results_wanted=settings["results_per_search"],hours_old=settings["hours_old"],country_indeed="canada",distance=settings["distance"],linkedin_fetch_description=True)
                    for _,row in frame.iterrows():
                        if self._store(row.to_dict(),context): added+=1
                        else: existing+=1
                except Exception as exc: errors+=1; log.warning("Search failed: %s",exc); progress(f"Search warning: {query} / {location}: {exc}")
        return {"added":added,"existing":existing,"errors":errors}

    def import_file(self, source: str) -> dict:
        path=Path(source)
        if path.suffix.casefold()==".csv":
            with path.open("r",encoding="utf-8-sig",newline="") as handle: records=list(csv.DictReader(handle))
        elif path.suffix.casefold()==".json":
            records=json.loads(path.read_text(encoding="utf-8")); records=records if isinstance(records,list) else [records]
        else: raise ValueError("Import a .csv or .json job file")
        context=self._resume_text() or self._search_context(); added=existing=0
        for record in records:
            if isinstance(record,dict) and self._value(record.get("title")):
                record={**record,"source":record.get("source") or "import"}; is_new=self._store(record,context); added += int(is_new); existing += int(not is_new)
        return {"added":added,"existing":existing}

    def add_manual(self, values: dict) -> int:
        description=str(values.get("description") or ""); record={**values,"salary":values.get("salary") or self._salary_from_description(description),"description_hash":self._description_hash(description),"start_date":extract_job_facts(str(values.get("title") or ""),description)["start_date"]}; job_id,_=self.db.upsert_job(record); context=self._resume_text() or self._search_context()
        if context and description: self.db.update_job(job_id,rule_score=self._rule_score(context,str(record.get("title"))+"\n"+description,str(record.get("location","")))[0])
        return job_id

    def analyze(self, job_id: int, resume: str, selected_model: str | None = None) -> dict:
        job=self.db.job(job_id)
        if not job: raise ValueError("Job not found")
        rule,matched,missing=self._rule_score(resume,job["title"]+"\n"+job["description"],job["location"]); payload=f"RESUME:\n{resume[:12000]}\n\nJOB:\nTitle: {job['title']}\nCompany: {job['company']}\nLocation: {job['location']}\n{job['description'][:12000]}"
        analysis,model=self.ai.generate_json("job_analysis",JOB_ANALYSIS_PROMPT,payload,selected_model); analysis=validate_job_analysis(analysis); final=weighted_match_score(rule,analysis["ai_score"]); analysis.update({"match_score":final,"rule_score":rule,"recommendation":recommendation(final),"required_matches":list(dict.fromkeys(matched+analysis["required_matches"])),"missing_skills":list(dict.fromkeys(missing+analysis["missing_skills"])),"model_used":model})
        self.db.update_job(job_id,rule_score=rule,ai_score=analysis["ai_score"],match_score=final,match_reason=analysis["reason"],strengths_json=json.dumps(analysis["strengths"]),missing_json=json.dumps(analysis["missing_skills"]),risks_json=json.dumps(analysis["risks"]),requirements_json=json.dumps(analysis["required_matches"]),preferred_json=json.dumps(analysis["preferred_matches"]),recommendation=analysis["recommendation"],model_used=model); return analysis

    def translate(self, job_id: int, progress=lambda message: None) -> dict:
        job=self.db.job(job_id)
        if not job: raise ValueError("Job not found")
        text=compact_job_text(job["description"]); digest=self._description_hash(text)
        if job.get("translation_zh") and job.get("translation_hash")==digest: return {"translation":job["translation_zh"],"model_used":job.get("translation_model") or "cached","cached":True}
        if not text: raise ValueError("Job description is empty")
        pieces=[text[i:i+6000] for i in range(0,len(text),6000)]; result=[]; models=[]; fast=load_settings()["models"]["fast"]
        for index,piece in enumerate(pieces,1):
            progress(f"Translating {index}/{len(pieces)} with {fast}..."); response,model=self.ai.generate_json("translate",TRANSLATION_PROMPT,piece,fast,0.1); translated=compact_job_text(response.get("translation",""))
            if not translated: raise ValueError(f"Translation chunk {index} was empty")
            result.append(translated); models.append(model)
        translation="\n\n".join(result); model=", ".join(dict.fromkeys(models)); self.db.update_job(job_id,translation_zh=translation,translation_model=model,translation_hash=digest); return {"translation":translation,"model_used":model,"cached":False}
    @staticmethod
    def open_job(job: dict) -> None:
        url=str(job.get("url") or "")
        if not url.startswith(("http://","https://")): raise ValueError("Job URL is not valid")
        webbrowser.open(url)

class ResumeService:
    def __init__(self, db: Database, ai: AIManager): self.db,self.ai,self.paths=db,ai,get_paths()
    def import_original(self, source: str) -> Path:
        src=Path(source); suffix=src.suffix.casefold()
        if not src.is_file() or suffix not in {".txt",".md",".pdf",".docx"}: raise ValueError("Select a .txt, .md, .pdf, or .docx resume")
        stamp=datetime.now().strftime("%Y%m%d-%H%M%S-%f"); backup=self.paths.resumes_original/f"source-{stamp}{suffix}"; shutil.copy2(src,backup)
        if suffix in {".txt",".md"}: text=src.read_text(encoding="utf-8",errors="replace")
        elif suffix==".pdf":
            from pypdf import PdfReader
            text="\n\n".join((page.extract_text() or "").strip() for page in PdfReader(str(src)).pages)
        else:
            from docx import Document
            doc=Document(str(src)); text="\n".join([p.text.strip() for p in doc.paragraphs if p.text.strip()]+[" | ".join(c.text.strip() for c in row.cells if c.text.strip()) for table in doc.tables for row in table.rows])
        if not text.strip(): backup.unlink(missing_ok=True); raise ValueError("No readable text was found. If this is a scanned PDF, convert it with OCR first.")
        target=self.paths.resumes_original/f"original-{stamp}.txt"; target.write_text(text.strip(),encoding="utf-8"); return target
    def original_path(self) -> Path | None:
        copies=sorted(self.paths.resumes_original.glob("original-*.txt"),key=lambda p:p.stat().st_mtime)
        if copies: return copies[-1]
        configured=Path(str(load_settings().get("resume_path","")).strip()) if str(load_settings().get("resume_path","")).strip() else None
        return configured if configured and configured.is_file() else None
    def original_text(self) -> str:
        path=self.original_path()
        if not path: raise FileNotFoundError("Import an original resume first")
        return path.read_text(encoding="utf-8",errors="replace")
    def imported_source_path(self) -> Path | None:
        files=sorted(self.paths.resumes_original.glob("source-*"),key=lambda p:p.stat().st_mtime); return files[-1] if files else self.original_path()
    def supporting_context(self, limit: int=24000) -> str:
        parts=[]; remaining=limit
        for record in reversed(self.db.supporting_documents()):
            path=Path(record.get("extracted_path") or "")
            if record.get("extraction_status")!="ready" or not path.exists() or remaining<=0: continue
            text=path.read_text(encoding="utf-8",errors="replace").strip()[:min(6000,remaining)]
            if text: parts.append(f"ADDITIONAL FILE: {record['original_name']}\n{text}"); remaining-=len(text)
        return "\n\n".join(parts)
    def candidate_context(self, include_resume: bool=True) -> str:
        profile=load_settings().get("profile",{}); parts=[]
        if include_resume: parts.append("RESUME:\n"+self.original_text())
        confirmed="\n".join(f"{key.replace('_',' ').title()}: {value}" for key,value in profile.items() if str(value).strip())
        if confirmed: parts.append("VERIFIED CANDIDATE INFORMATION:\n"+confirmed)
        if extra:=self.supporting_context(): parts.append(extra)
        return "\n\n".join(parts)
    def optimize(self, job: dict, selected_model: str|None=None, document_type: str="Resume", replace_version: dict|None=None) -> dict:
        kind="CV" if str(document_type).upper()=="CV" else "Resume"; original=self.original_text(); extra=self.candidate_context(False); goal="Create a comprehensive CV with all explicitly supported detail." if kind=="CV" else "Create a concise tailored resume for this role."
        request=f"ORIGINAL RESUME:\n{original}\n\n{extra}\n\nJOB:\n{job['title']} at {job['company']}\n{job['description'][:12000]}\nDOCUMENT TARGET: {goal}{generation_instruction('resume')}"; result,model=self.ai.generate_json("resume_optimization",RESUME_OPTIMIZATION_PROMPT,request,selected_model,0.2); result=validate_resume_changes(result,original)
        revised=original
        for change in result["changes"]:
            if change["risk"]!="HIGH" and change["original"] in revised: revised=revised.replace(change["original"],change["suggested"],1)
        number=len(self.db.resume_versions())+1; name=replace_version["version_name"] if replace_version else f"{'cv' if kind=='CV' else 'resume'}_v{number:03d}"; record={"job_id":job["id"],"version_name":name,"source_path":str(self.original_path()),"content":revised,"changes_json":json.dumps(result["changes"]),"document_type":kind,"model_used":model}; render_resume_pdf(revised,self.paths.resumes_generated/f"{name}.pdf",load_settings().get("resume_pdf",{}))
        if replace_version: self.paths.resumes_approved.joinpath(f"{name}.pdf").unlink(missing_ok=True); self.db.replace_resume_version(replace_version["id"],**record); version_id=replace_version["id"]
        else: version_id=self.db.add_resume_version(**record)
        return {**result,"version_id":version_id,"version_name":name,"document_type":kind,"content":revised,"model_used":model}
    @staticmethod
    def diff(original: str, modified: str) -> str: return "\n".join(difflib.unified_diff(original.splitlines(),modified.splitlines(),fromfile="Original",tofile="Modified",lineterm=""))
    def decide(self, version_id: int, approved: bool) -> Path|None:
        version=next(v for v in self.db.resume_versions() if v["id"]==version_id); self.db.set_resume_decision(version_id,approved)
        if not approved: return None
        target=self.paths.resumes_approved/f"{version['version_name']}.pdf"; return render_resume_pdf(version["content"],target,load_settings().get("resume_pdf",{}))
    def preview_pdf(self, content: str, name: str="resume-preview") -> Path: return render_resume_pdf(content,self.paths.cache/f"{safe_filename(name)}.pdf",load_settings().get("resume_pdf",{}))

class CoverLetterService:
    def __init__(self, db:Database, ai:AIManager, resume_service:ResumeService): self.db,self.ai,self.resume_service,self.paths=db,ai,resume_service,get_paths()
    def generate(self, job:dict, selected_model:str|None=None) -> dict:
        request=f"CANDIDATE:\n{self.resume_service.candidate_context()}\n\nJOB:\n{job['title']} at {job['company']}\n{job['description'][:12000]}{generation_instruction('cover_letter')}"; result,model=self.ai.generate_json("cover_letter",COVER_LETTER_PROMPT,request,selected_model,0.45); letter=str(result.get("letter") or "").strip()
        if not letter: raise ValueError("AI did not return a cover letter")
        return {"letter":letter,"warnings":result.get("warnings",[]),"model_used":model}
    def _render_and_record(self, job, letter, model, path): render_cover_letter_pdf(letter,path,job["company"],job["title"],load_settings().get("resume_pdf",{})); self.db.add_cover_letter(job["id"],str(path),letter,model); return path
    def save(self, job, letter, model): return self._render_and_record(job,letter,model,self.paths.cover_letters/f"{safe_filename(f'{job['company']}_{job['title']}')}_{datetime.now():%Y%m%d-%H%M%S}.pdf")
    def preview_pdf(self,job,letter): return render_cover_letter_pdf(letter,self.paths.cache/"cover-letter-preview.pdf",job["company"],job["title"],load_settings().get("resume_pdf",{}))

class SupportingDocumentService:
    TEXT_EXTENSIONS={".txt",".md",".csv",".json",".yaml",".yml",".xml",".html",".htm",".rtf",".log",".ini",".py",".c",".cpp",".h"}
    def __init__(self,db:Database): self.db,self.paths=db,get_paths()
    def _extract(self,source:Path)->tuple[str,str]:
        suffix=source.suffix.casefold()
        if suffix in self.TEXT_EXTENSIONS: return source.read_text(encoding="utf-8",errors="replace").strip(),"ready"
        if suffix==".pdf":
            from pypdf import PdfReader
            return "\n\n".join((p.extract_text() or "").strip() for p in PdfReader(str(source)).pages).strip(),"ready"
        if suffix==".docx":
            from docx import Document
            doc=Document(str(source)); return "\n".join(p.text.strip() for p in doc.paragraphs if p.text.strip()),"ready"
        if suffix==".xlsx":
            from openpyxl import load_workbook
            book=load_workbook(str(source),read_only=True,data_only=True); return "\n".join(" | ".join(str(x) for x in row if x is not None) for sheet in book.worksheets for row in sheet.iter_rows(values_only=True) if any(x is not None for x in row)),"ready"
        return "","stored_unreadable"
    def import_file(self,source:str)->dict:
        src=Path(source)
        if not src.is_file(): raise ValueError("Select a file")
        stamp=datetime.now().strftime("%Y%m%d-%H%M%S-%f"); saved=self.paths.supporting_documents/f"{stamp}-{safe_filename(src.stem)}{src.suffix.casefold()}"; shutil.copy2(src,saved)
        try: text,status=self._extract(src)
        except Exception: text,status="","stored_unreadable"
        extracted=None
        if text: target=self.paths.supporting_documents/f"{stamp}-{safe_filename(src.stem)}.extracted.txt"; target.write_text(text,encoding="utf-8"); extracted=str(target)
        elif status=="ready": status="stored_unreadable"
        ident=self.db.add_supporting_document(src.name,str(saved),extracted,status,len(text)); return {"id":ident,"name":src.name,"status":status,"characters":len(text)}
    def remove(self,document_id:int)->None:
        row=self.db.remove_supporting_document(document_id)
        if row:
            for value in (row.get("stored_path"),row.get("extracted_path")):
                if value: Path(value).unlink(missing_ok=True)
