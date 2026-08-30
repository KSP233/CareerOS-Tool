"""CareerOS persistence layer.

This is a clean-room implementation based on CareerOS's local-review data
contract: jobs are review records, drafts are versioned, and applications are manual.
"""
from __future__ import annotations

import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from config import get_paths

_DDL = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, company TEXT NOT NULL DEFAULT '', title TEXT NOT NULL, location TEXT NOT NULL DEFAULT '', salary TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', requirements_json TEXT NOT NULL DEFAULT '[]', preferred_json TEXT NOT NULL DEFAULT '[]', date_found TEXT NOT NULL, date_posted TEXT, rule_score INTEGER, ai_score INTEGER, match_score INTEGER, match_reason TEXT NOT NULL DEFAULT '', strengths_json TEXT NOT NULL DEFAULT '[]', missing_json TEXT NOT NULL DEFAULT '[]', risks_json TEXT NOT NULL DEFAULT '[]', recommendation TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'New', model_used TEXT, description_hash TEXT NOT NULL DEFAULT '', start_date TEXT NOT NULL DEFAULT '', translation_zh TEXT NOT NULL DEFAULT '', translation_model TEXT, translation_hash TEXT NOT NULL DEFAULT '', UNIQUE(url));
CREATE UNIQUE INDEX IF NOT EXISTS jobs_identity ON jobs(company, title, location);
CREATE TABLE IF NOT EXISTS resume_versions (id INTEGER PRIMARY KEY, job_id INTEGER, parent_version_id INTEGER, generation_job_id INTEGER, version_name TEXT NOT NULL, source_path TEXT, content TEXT NOT NULL, changes_json TEXT NOT NULL DEFAULT '[]', document_type TEXT NOT NULL DEFAULT 'Resume', document_json TEXT NOT NULL DEFAULT '', style_json TEXT NOT NULL DEFAULT '', layout_json TEXT NOT NULL DEFAULT '', template_version TEXT NOT NULL DEFAULT 'v2', layout_version TEXT NOT NULL DEFAULT 'v2', model_used TEXT, created_at TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0, rejected INTEGER NOT NULL DEFAULT 0, FOREIGN KEY(job_id) REFERENCES jobs(id));
CREATE TABLE IF NOT EXISTS cover_letters (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, path TEXT NOT NULL, content TEXT NOT NULL, model_used TEXT, created_at TEXT NOT NULL, FOREIGN KEY(job_id) REFERENCES jobs(id));
CREATE TABLE IF NOT EXISTS supporting_documents (id INTEGER PRIMARY KEY, original_name TEXT NOT NULL, stored_path TEXT NOT NULL, extracted_path TEXT, extraction_status TEXT NOT NULL DEFAULT 'stored', character_count INTEGER NOT NULL DEFAULT 0, added_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS applications (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL UNIQUE, status TEXT NOT NULL, applied_at TEXT, previous_status TEXT NOT NULL DEFAULT 'New', resume_version_id INTEGER, cover_letter_id INTEGER, notes TEXT NOT NULL DEFAULT '', FOREIGN KEY(job_id) REFERENCES jobs(id));
CREATE TABLE IF NOT EXISTS ai_requests (id INTEGER PRIMARY KEY, task TEXT NOT NULL, model TEXT NOT NULL, elapsed_ms INTEGER NOT NULL, success INTEGER NOT NULL, created_at TEXT NOT NULL, error TEXT);
"""

def _now() -> str: return datetime.now().astimezone().isoformat()

class Database:
    """Transaction-oriented repository for the local CareerOS database."""
    def __init__(self, path: Path | None = None):
        self.path = Path(path or get_paths().database); self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db: db.executescript(_DDL); self._upgrade(db)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15); db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON"); yield db; db.commit()
        finally: db.close()
    connect = connection

    @staticmethod
    def _upgrade(db: sqlite3.Connection) -> None:
        columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
        for name, declaration in {"translation_zh":"TEXT NOT NULL DEFAULT ''", "translation_model":"TEXT", "translation_hash":"TEXT NOT NULL DEFAULT ''", "start_date":"TEXT NOT NULL DEFAULT ''"}.items():
            if name not in columns: db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
        versions = {row[1] for row in db.execute("PRAGMA table_info(resume_versions)")}
        for name, declaration in {"document_type":"TEXT NOT NULL DEFAULT 'Resume'", "document_json":"TEXT NOT NULL DEFAULT ''", "style_json":"TEXT NOT NULL DEFAULT ''", "layout_json":"TEXT NOT NULL DEFAULT ''", "parent_version_id":"INTEGER", "generation_job_id":"INTEGER", "template_version":"TEXT NOT NULL DEFAULT 'v2'", "layout_version":"TEXT NOT NULL DEFAULT 'v2'"}.items():
            if name not in versions: db.execute(f"ALTER TABLE resume_versions ADD COLUMN {name} {declaration}")
        applications = {row[1] for row in db.execute("PRAGMA table_info(applications)")}
        if "previous_status" not in applications: db.execute("ALTER TABLE applications ADD COLUMN previous_status TEXT NOT NULL DEFAULT 'New'")

    def backup(self) -> Path | None:
        if not self.path.exists() or not self.path.stat().st_size: return None
        target = get_paths().backups / f"careeros-{datetime.now():%Y%m%d-%H%M%S-%f}.db"; target.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(self.path); destination = sqlite3.connect(target)
        try: source.backup(destination)
        finally: destination.close(); source.close()
        return target

    def upsert_job(self, job: dict) -> tuple[int, bool]:
        """Use canonical URL first, then company/title/location for an incomplete source URL."""
        item = {key: str(job.get(key) or "").strip() for key in ("company","title","location","salary","source","url","description","description_hash","start_date")}
        if not item["title"]: raise ValueError("A job title is required")
        with self.connection() as db:
            old = db.execute("SELECT id,description_hash,salary,start_date FROM jobs WHERE (url<>'' AND url=?) OR (company=? AND title=? AND location=?) LIMIT 1", (item["url"],item["company"],item["title"],item["location"])).fetchone()
            if old:
                if item["description_hash"] and item["description_hash"] != old["description_hash"]:
                    db.execute("UPDATE jobs SET description=?,description_hash=?,date_posted=?,salary=?,start_date=? WHERE id=?", (item["description"],item["description_hash"],job.get("date_posted"),item["salary"] or old["salary"],item["start_date"] or old["start_date"],old["id"]))
                return int(old["id"]), False
            created = db.execute("INSERT INTO jobs(company,title,location,salary,source,url,description,date_found,date_posted,description_hash,start_date) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (item["company"],item["title"],item["location"],item["salary"],item["source"],item["url"],item["description"],_now(),job.get("date_posted"),item["description_hash"],item["start_date"])); return int(created.lastrowid), True

    def jobs(self) -> list[dict]:
        with self.connection() as db: return [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY COALESCE(match_score,rule_score,-1) DESC,date_found DESC")]
    def job(self, job_id: int) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(); return dict(row) if row else None
    def update_job(self, job_id: int, **changes) -> None:
        permitted={"status","salary","start_date","rule_score","ai_score","match_score","match_reason","strengths_json","missing_json","risks_json","recommendation","model_used","requirements_json","preferred_json","translation_zh","translation_model","translation_hash"}; values={k:v for k,v in changes.items() if k in permitted}
        if values:
            # Identifiers are selected exclusively from the fixed permitted set.
            with self.connection() as db: db.execute(f"UPDATE jobs SET {', '.join(f'{key}=?' for key in values)} WHERE id=?", (*values.values(),job_id))  # nosec B608

    def clear_job_scores(self) -> None:
        """Invalidate every score when the active resume changes or disappears."""
        with self.connection() as db:
            db.execute("""UPDATE jobs SET
                rule_score=NULL, ai_score=NULL, match_score=NULL,
                match_reason='', strengths_json='[]', missing_json='[]',
                risks_json='[]', requirements_json='[]', preferred_json='[]',
                recommendation='', model_used=NULL
            """)

    def add_resume_version(self, **r) -> int:
        values=(r.get("job_id"),r.get("parent_version_id"),r.get("generation_job_id"),r["version_name"],r.get("source_path"),r["content"],r.get("changes_json","[]"),r.get("document_type","Resume"),r.get("document_json", ""),r.get("style_json", ""),r.get("layout_json", ""),r.get("template_version", "v2"),r.get("layout_version", "v2"),r.get("model_used"),_now())
        with self.connection() as db: return int(db.execute("INSERT INTO resume_versions(job_id,parent_version_id,generation_job_id,version_name,source_path,content,changes_json,document_type,document_json,style_json,layout_json,template_version,layout_version,model_used,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",values).lastrowid)
    def replace_resume_version(self, version_id: int, **r) -> None:
        with self.connection() as db: db.execute("UPDATE resume_versions SET job_id=?,generation_job_id=?,source_path=?,content=?,changes_json=?,document_type=?,document_json=?,style_json=?,layout_json=?,template_version=?,layout_version=?,model_used=?,created_at=?,approved=0,rejected=0 WHERE id=?",(r.get("job_id"),r.get("generation_job_id"),r.get("source_path"),r["content"],r.get("changes_json","[]"),r.get("document_type","Resume"),r.get("document_json", ""),r.get("style_json", ""),r.get("layout_json", ""),r.get("template_version", "v2"),r.get("layout_version", "v2"),r.get("model_used"),_now(),version_id))

    def update_resume_version(self, version_id: int, **changes) -> None:
        allowed = {"content", "document_json", "style_json", "layout_json", "template_version", "layout_version"}; values = {key: value for key, value in changes.items() if key in allowed}
        if values:
            # Identifiers are selected exclusively from the fixed allowed set.
            with self.connection() as db: db.execute(f"UPDATE resume_versions SET {', '.join(f'{key}=?' for key in values)} WHERE id=?", (*values.values(), version_id))  # nosec B608
    def resume_versions(self) -> list[dict]:
        with self.connection() as db: return [dict(row) for row in db.execute("SELECT r.*,j.company,j.title FROM resume_versions r LEFT JOIN jobs j ON j.id=r.job_id ORDER BY r.id DESC")]
    def set_resume_decision(self, version_id: int, approved: bool) -> None:
        with self.connection() as db: db.execute("UPDATE resume_versions SET approved=?,rejected=? WHERE id=?",(int(approved),int(not approved),version_id))
    def remove_resume_version(self, version_id: int) -> dict | None:
        with self.connection() as db:
            row=db.execute("SELECT * FROM resume_versions WHERE id=?",(version_id,)).fetchone()
            if row: db.execute("DELETE FROM resume_versions WHERE id=?",(version_id,))
            return dict(row) if row else None
    def add_cover_letter(self, job_id: int, path: str, content: str, model: str) -> int:
        with self.connection() as db: return int(db.execute("INSERT INTO cover_letters(job_id,path,content,model_used,created_at) VALUES(?,?,?,?,?)",(job_id,path,content,model,_now())).lastrowid)
    def cover_letters(self) -> list[dict]:
        with self.connection() as db: return [dict(row) for row in db.execute("SELECT c.*,j.company,j.title FROM cover_letters c LEFT JOIN jobs j ON j.id=c.job_id ORDER BY c.id DESC")]
    def remove_cover_letter(self, cover_id: int) -> dict | None:
        with self.connection() as db:
            row=db.execute("SELECT * FROM cover_letters WHERE id=?",(cover_id,)).fetchone()
            if row: db.execute("DELETE FROM cover_letters WHERE id=?",(cover_id,))
            return dict(row) if row else None
    def add_supporting_document(self, name: str, stored: str, extracted: str | None, status: str, characters: int) -> int:
        with self.connection() as db: return int(db.execute("INSERT INTO supporting_documents(original_name,stored_path,extracted_path,extraction_status,character_count,added_at) VALUES(?,?,?,?,?,?)",(name,stored,extracted,status,characters,_now())).lastrowid)
    def supporting_documents(self) -> list[dict]:
        with self.connection() as db: return [dict(row) for row in db.execute("SELECT * FROM supporting_documents ORDER BY id DESC")]
    def remove_supporting_document(self, document_id: int) -> dict | None:
        with self.connection() as db:
            row=db.execute("SELECT * FROM supporting_documents WHERE id=?",(document_id,)).fetchone()
            if row: db.execute("DELETE FROM supporting_documents WHERE id=?",(document_id,))
            return dict(row) if row else None
    def mark_applied(self, job_id: int) -> None:
        with self.connection() as db:
            job=db.execute("SELECT status FROM jobs WHERE id=?",(job_id,)).fetchone(); before=job["status"] if job and job["status"] != "Applied" else "New"
            db.execute("INSERT INTO applications(job_id,status,applied_at,previous_status) VALUES(?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,applied_at=excluded.applied_at",(job_id,"Applied",_now(),before)); db.execute("UPDATE jobs SET status='Applied' WHERE id=?",(job_id,))
    def unmark_applied(self, job_id: int) -> bool:
        with self.connection() as db:
            row=db.execute("SELECT previous_status FROM applications WHERE job_id=?",(job_id,)).fetchone()
            if not row: return False
            db.execute("DELETE FROM applications WHERE job_id=?",(job_id,)); db.execute("UPDATE jobs SET status=? WHERE id=? AND status='Applied'",(row["previous_status"] or "New",job_id)); return True
    def application_rows(self) -> list[dict]:
        query="SELECT j.*,a.applied_at,(SELECT version_name FROM resume_versions r WHERE r.job_id=j.id AND r.approved=1 AND r.document_type='Resume' ORDER BY r.id DESC LIMIT 1) AS resume_version,(SELECT version_name FROM resume_versions r WHERE r.job_id=j.id AND r.approved=1 AND r.document_type='CV' ORDER BY r.id DESC LIMIT 1) AS cv_version FROM jobs j LEFT JOIN applications a ON a.job_id=j.id ORDER BY COALESCE(a.applied_at,j.date_found) DESC"
        with self.connection() as db: return [dict(row) for row in db.execute(query)]

    def merge_from(self, source_path: str | Path) -> dict[str, int | str]:
        """Merge another CareerOS database without importing its Settings/API key.

        Job ids are remapped before dependent records are inserted. Files are
        copied only when their saved path is inside the source data directory;
        a tampered database cannot make the merge copy arbitrary user files.
        """
        source_path = Path(source_path).expanduser().resolve()
        if not source_path.is_file(): raise FileNotFoundError("Select an existing CareerOS .db file")
        if source_path == self.path.resolve(): raise ValueError("Choose a different CareerOS database")
        source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True, timeout=15); source.row_factory = sqlite3.Row
        created_files: list[Path] = []
        stats: dict[str, int | str] = {"jobs":0, "jobs_existing":0, "drafts":0, "drafts_existing":0, "applications":0, "supporting_documents":0, "backup":""}
        source_root = source_path.parent.parent.resolve()
        target_paths = get_paths()

        def table_exists(name: str) -> bool:
            return source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

        def source_rows(name: str) -> list[dict]:
            # name is supplied only by this method from its fixed table list.
            return [dict(row) for row in source.execute(f'SELECT * FROM "{name}"')] if table_exists(name) else []  # nosec B608

        def contained_file(value: object) -> Path | None:
            if not str(value or "").strip(): return None
            candidate = Path(str(value)).expanduser().resolve()
            return candidate if candidate.is_file() and (candidate == source_root or source_root in candidate.parents) else None

        def unique_target(folder: Path, name: str) -> Path:
            folder.mkdir(parents=True, exist_ok=True); candidate = folder / name; index = 2
            while candidate.exists():
                candidate = folder / f"{Path(name).stem}-merged-{index}{Path(name).suffix}"; index += 1
            return candidate

        def copy_source_file(value: object, folder: Path) -> str:
            candidate = contained_file(value)
            if candidate is None: return ""
            target = unique_target(folder, candidate.name); shutil.copy2(candidate, target); created_files.append(target)
            return str(target)

        backup = self.backup(); stats["backup"] = str(backup or "")
        try:
            if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("The selected database failed SQLite integrity_check")
            if not table_exists("jobs"):
                raise ValueError("The selected file is not a CareerOS database")
            with self.connection() as target:
                target_columns = {name:{row[1] for row in target.execute(f'PRAGMA table_info("{name}")')} for name in ("jobs","resume_versions","cover_letters","supporting_documents","applications")}
                job_map: dict[int, int] = {}
                for row in source_rows("jobs"):
                    old_id = int(row["id"]); url = str(row.get("url") or "").strip()
                    existing = target.execute("SELECT * FROM jobs WHERE (url<>'' AND url=?) OR (company=? AND title=? AND location=?) LIMIT 1", (url, str(row.get("company") or ""), str(row.get("title") or ""), str(row.get("location") or ""))).fetchone()
                    if existing:
                        new_id = int(existing["id"]); stats["jobs_existing"] += 1
                        fill = {key:value for key,value in row.items() if key in target_columns["jobs"] and key not in {"id","status"} and (existing[key] is None or str(existing[key]).strip() in {"", "[]"}) and value not in (None,"","[]")}
                        if fill: target.execute(f"UPDATE jobs SET {', '.join(f'{key}=?' for key in fill)} WHERE id=?", (*fill.values(), new_id))  # nosec B608 - columns intersect target schema
                    else:
                        values = {key:value for key,value in row.items() if key in target_columns["jobs"] and key != "id"}
                        values.setdefault("date_found", _now()); values.setdefault("title", "Imported job")
                        cursor = target.execute(f"INSERT INTO jobs({', '.join(values)}) VALUES({', '.join('?' for _ in values)})", tuple(values.values()))  # nosec B608 - columns intersect target schema
                        new_id = int(cursor.lastrowid); stats["jobs"] += 1
                    job_map[old_id] = new_id

                version_map: dict[int, int] = {}; pending_parents: list[tuple[int, object]] = []
                for row in source_rows("resume_versions"):
                    old_id = int(row["id"]); mapped_job = job_map.get(int(row["job_id"])) if row.get("job_id") is not None else None
                    document_type = str(row.get("document_type") or "Resume")
                    duplicate = target.execute("SELECT id FROM resume_versions WHERE job_id IS ? AND document_type=? AND content=? LIMIT 1", (mapped_job, document_type, str(row.get("content") or ""))).fetchone()
                    if duplicate:
                        version_map[old_id] = int(duplicate["id"]); stats["drafts_existing"] += 1; continue
                    base_name = str(row.get("version_name") or f"merged_{old_id}"); name = base_name; index = 2
                    while target.execute("SELECT 1 FROM resume_versions WHERE version_name=?", (name,)).fetchone(): name=f"{base_name}_merged{index:02d}"; index+=1
                    source_copy = copy_source_file(row.get("source_path"), target_paths.resumes_original)
                    if source_copy:
                        old_source = contained_file(row.get("source_path"))
                        for suffix in (".json", ".blocks.json"):
                            sidecar = old_source.with_suffix(suffix) if old_source else None
                            if sidecar and sidecar.is_file() and (sidecar == source_root or source_root in sidecar.parents):
                                side_target = Path(source_copy).with_suffix(suffix); shutil.copy2(sidecar, side_target); created_files.append(side_target)
                    values = {key:value for key,value in row.items() if key in target_columns["resume_versions"] and key not in {"id","parent_version_id"}}
                    values.update(job_id=mapped_job, generation_job_id=job_map.get(int(row["generation_job_id"])) if row.get("generation_job_id") is not None else mapped_job, version_name=name, source_path=source_copy, document_type=document_type)
                    values.setdefault("created_at", _now()); values.setdefault("content", "")
                    cursor = target.execute(f"INSERT INTO resume_versions({', '.join(values)}) VALUES({', '.join('?' for _ in values)})", tuple(values.values()))  # nosec B608 - columns intersect target schema
                    new_id = int(cursor.lastrowid); version_map[old_id] = new_id; pending_parents.append((new_id, row.get("parent_version_id"))); stats["drafts"] += 1
                    for folder_name, destination in (("generated", target_paths.resumes_generated), ("approved", target_paths.resumes_approved)):
                        source_folder = source_root / "resumes" / folder_name
                        for suffix in (".pdf", ".docx"):
                            file = source_folder / f"{base_name}{suffix}"
                            if file.is_file():
                                target_file = destination / f"{name}{suffix}"; target_file.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(file, target_file); created_files.append(target_file)
                for new_id, old_parent in pending_parents:
                    if old_parent is not None and int(old_parent) in version_map: target.execute("UPDATE resume_versions SET parent_version_id=? WHERE id=?", (version_map[int(old_parent)], new_id))

                cover_map: dict[int, int] = {}
                for row in source_rows("cover_letters"):
                    old_id=int(row["id"]); mapped_job=job_map.get(int(row["job_id"]))
                    if mapped_job is None: continue
                    duplicate=target.execute("SELECT id FROM cover_letters WHERE job_id=? AND content=? LIMIT 1",(mapped_job,str(row.get("content") or ""))).fetchone()
                    if duplicate: cover_map[old_id]=int(duplicate["id"]); continue
                    values={key:value for key,value in row.items() if key in target_columns["cover_letters"] and key!="id"}; values["job_id"]=mapped_job; values["path"]=copy_source_file(row.get("path"),target_paths.cover_letters); values.setdefault("created_at",_now())
                    cursor=target.execute(f"INSERT INTO cover_letters({', '.join(values)}) VALUES({', '.join('?' for _ in values)})",tuple(values.values()))  # nosec B608 - columns intersect target schema
                    cover_map[old_id]=int(cursor.lastrowid)

                for row in source_rows("supporting_documents"):
                    duplicate=target.execute("SELECT 1 FROM supporting_documents WHERE original_name=? AND character_count=? LIMIT 1",(str(row.get("original_name") or ""),int(row.get("character_count") or 0))).fetchone()
                    if duplicate: continue
                    stored=copy_source_file(row.get("stored_path"),target_paths.supporting_documents); extracted=copy_source_file(row.get("extracted_path"),target_paths.supporting_documents)
                    if not stored: continue
                    values={key:value for key,value in row.items() if key in target_columns["supporting_documents"] and key!="id"}; values.update(stored_path=stored,extracted_path=extracted or None); values.setdefault("added_at",_now())
                    target.execute(f"INSERT INTO supporting_documents({', '.join(values)}) VALUES({', '.join('?' for _ in values)})",tuple(values.values()))  # nosec B608 - columns intersect target schema
                    stats["supporting_documents"] += 1

                for row in source_rows("applications"):
                    mapped_job=job_map.get(int(row["job_id"]))
                    if mapped_job is None: continue
                    values={key:value for key,value in row.items() if key in target_columns["applications"] and key!="id"}; values["job_id"]=mapped_job
                    if row.get("resume_version_id") is not None: values["resume_version_id"]=version_map.get(int(row["resume_version_id"]))
                    if row.get("cover_letter_id") is not None: values["cover_letter_id"]=cover_map.get(int(row["cover_letter_id"]))
                    columns=list(values); assignments=", ".join(f"{key}=excluded.{key}" for key in columns if key!="job_id")
                    target.execute(f"INSERT INTO applications({', '.join(columns)}) VALUES({', '.join('?' for _ in columns)}) ON CONFLICT(job_id) DO UPDATE SET {assignments}",tuple(values.values()))  # nosec B608 - columns intersect target schema
                    stats["applications"] += 1
        except Exception:
            for path in reversed(created_files):
                try: path.unlink(missing_ok=True)
                except OSError: pass
            raise
        finally:
            source.close()
        return stats

    def record_ai(self, task: str, model: str, elapsed_ms: int, success: bool, error: str = "") -> None:
        with self.connection() as db: db.execute("INSERT INTO ai_requests(task,model,elapsed_ms,success,created_at,error) VALUES(?,?,?,?,?,?)",(task,model,elapsed_ms,int(success),_now(),error[:500]))
