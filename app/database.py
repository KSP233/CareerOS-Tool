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
CREATE TABLE IF NOT EXISTS resume_versions (id INTEGER PRIMARY KEY, job_id INTEGER, version_name TEXT NOT NULL, source_path TEXT, content TEXT NOT NULL, changes_json TEXT NOT NULL DEFAULT '[]', document_type TEXT NOT NULL DEFAULT 'Resume', model_used TEXT, created_at TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0, rejected INTEGER NOT NULL DEFAULT 0, FOREIGN KEY(job_id) REFERENCES jobs(id));
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
        if "document_type" not in versions: db.execute("ALTER TABLE resume_versions ADD COLUMN document_type TEXT NOT NULL DEFAULT 'Resume'")
        applications = {row[1] for row in db.execute("PRAGMA table_info(applications)")}
        if "previous_status" not in applications: db.execute("ALTER TABLE applications ADD COLUMN previous_status TEXT NOT NULL DEFAULT 'New'")

    def backup(self) -> Path | None:
        if not self.path.exists() or not self.path.stat().st_size: return None
        target = get_paths().backups / f"careeros-{datetime.now():%Y%m%d-%H%M%S}.db"; shutil.copy2(self.path, target); return target

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

    def jobs(self, where: str = "1=1", params: tuple = ()) -> list[dict]:
        with self.connection() as db: return [dict(row) for row in db.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY COALESCE(match_score,rule_score,-1) DESC,date_found DESC", params)]
    def job(self, job_id: int) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(); return dict(row) if row else None
    def update_job(self, job_id: int, **changes) -> None:
        permitted={"status","salary","start_date","rule_score","ai_score","match_score","match_reason","strengths_json","missing_json","risks_json","recommendation","model_used","requirements_json","preferred_json","translation_zh","translation_model","translation_hash"}; values={k:v for k,v in changes.items() if k in permitted}
        if values:
            with self.connection() as db: db.execute(f"UPDATE jobs SET {', '.join(f'{key}=?' for key in values)} WHERE id=?", (*values.values(),job_id))

    def add_resume_version(self, **r) -> int:
        values=(r.get("job_id"),r["version_name"],r.get("source_path"),r["content"],r.get("changes_json","[]"),r.get("document_type","Resume"),r.get("model_used"),_now())
        with self.connection() as db: return int(db.execute("INSERT INTO resume_versions(job_id,version_name,source_path,content,changes_json,document_type,model_used,created_at) VALUES(?,?,?,?,?,?,?,?)",values).lastrowid)
    def replace_resume_version(self, version_id: int, **r) -> None:
        with self.connection() as db: db.execute("UPDATE resume_versions SET job_id=?,source_path=?,content=?,changes_json=?,document_type=?,model_used=?,created_at=?,approved=0,rejected=0 WHERE id=?",(r.get("job_id"),r.get("source_path"),r["content"],r.get("changes_json","[]"),r.get("document_type","Resume"),r.get("model_used"),_now(),version_id))
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
        query="SELECT j.*,a.applied_at,(SELECT version_name FROM resume_versions r WHERE r.job_id=j.id AND r.approved=1 ORDER BY r.id DESC LIMIT 1) AS resume_version,(SELECT path FROM cover_letters c WHERE c.job_id=j.id ORDER BY c.id DESC LIMIT 1) AS cover_letter_path FROM jobs j LEFT JOIN applications a ON a.job_id=j.id ORDER BY COALESCE(a.applied_at,j.date_found) DESC"
        with self.connection() as db: return [dict(row) for row in db.execute(query)]
    def record_ai(self, task: str, model: str, elapsed_ms: int, success: bool, error: str = "") -> None:
        with self.connection() as db: db.execute("INSERT INTO ai_requests(task,model,elapsed_ms,success,created_at,error) VALUES(?,?,?,?,?,?)",(task,model,elapsed_ms,int(success),_now(),error[:500]))
