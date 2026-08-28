from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from config import get_paths


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, company TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,
 location TEXT NOT NULL DEFAULT '', salary TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
 url TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', requirements_json TEXT NOT NULL DEFAULT '[]',
 preferred_json TEXT NOT NULL DEFAULT '[]', date_found TEXT NOT NULL, date_posted TEXT,
 rule_score INTEGER, ai_score INTEGER, match_score INTEGER, match_reason TEXT NOT NULL DEFAULT '',
 strengths_json TEXT NOT NULL DEFAULT '[]', missing_json TEXT NOT NULL DEFAULT '[]', risks_json TEXT NOT NULL DEFAULT '[]',
 recommendation TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'New', model_used TEXT,
 description_hash TEXT NOT NULL DEFAULT '', start_date TEXT NOT NULL DEFAULT '', translation_zh TEXT NOT NULL DEFAULT '',
 translation_model TEXT, translation_hash TEXT NOT NULL DEFAULT '', UNIQUE(url)
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_identity ON jobs(company, title, location);
CREATE TABLE IF NOT EXISTS resume_versions (
 id INTEGER PRIMARY KEY, job_id INTEGER, version_name TEXT NOT NULL, source_path TEXT,
 content TEXT NOT NULL, changes_json TEXT NOT NULL DEFAULT '[]', document_type TEXT NOT NULL DEFAULT 'Resume', model_used TEXT,
 created_at TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0, rejected INTEGER NOT NULL DEFAULT 0,
 FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE TABLE IF NOT EXISTS cover_letters (
 id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, path TEXT NOT NULL, content TEXT NOT NULL,
 model_used TEXT, created_at TEXT NOT NULL, FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE TABLE IF NOT EXISTS supporting_documents (
 id INTEGER PRIMARY KEY, original_name TEXT NOT NULL, stored_path TEXT NOT NULL,
 extracted_path TEXT, extraction_status TEXT NOT NULL DEFAULT 'stored',
 character_count INTEGER NOT NULL DEFAULT 0, added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applications (
 id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL UNIQUE, status TEXT NOT NULL,
 applied_at TEXT, previous_status TEXT NOT NULL DEFAULT 'New', resume_version_id INTEGER, cover_letter_id INTEGER, notes TEXT NOT NULL DEFAULT '',
 FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE TABLE IF NOT EXISTS ai_requests (
 id INTEGER PRIMARY KEY, task TEXT NOT NULL, model TEXT NOT NULL, elapsed_ms INTEGER NOT NULL,
 success INTEGER NOT NULL, created_at TEXT NOT NULL, error TEXT
);
"""


class Database:
    def __init__(self, path: Path | None = None):
        self.path = path or get_paths().database
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
            migrations = {
                "translation_zh": "TEXT NOT NULL DEFAULT ''",
                "translation_model": "TEXT",
                "translation_hash": "TEXT NOT NULL DEFAULT ''",
                "start_date": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            resume_columns = {row[1] for row in conn.execute("PRAGMA table_info(resume_versions)")}
            if "document_type" not in resume_columns:
                conn.execute("ALTER TABLE resume_versions ADD COLUMN document_type TEXT NOT NULL DEFAULT 'Resume'")
            application_columns = {row[1] for row in conn.execute("PRAGMA table_info(applications)")}
            if "previous_status" not in application_columns:
                conn.execute("ALTER TABLE applications ADD COLUMN previous_status TEXT NOT NULL DEFAULT 'New'")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def backup(self) -> Path | None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        target = get_paths().backups / f"careeros-{datetime.now():%Y%m%d-%H%M%S}.db"
        shutil.copy2(self.path, target)
        return target

    def upsert_job(self, job: dict) -> tuple[int, bool]:
        now = datetime.now().astimezone().isoformat()
        url = str(job.get("url") or "").strip()
        identity = (str(job.get("company") or "").strip(), str(job.get("title") or "").strip(), str(job.get("location") or "").strip())
        with self.connect() as conn:
            row = conn.execute("SELECT id, description_hash, salary, start_date FROM jobs WHERE url=? OR (company=? AND title=? AND location=?)", (url, *identity)).fetchone()
            if row:
                if job.get("description_hash") and job["description_hash"] != row["description_hash"]:
                    salary = str(job.get("salary") or "").strip() or row["salary"]
                    start_date = str(job.get("start_date") or "").strip() or row["start_date"]
                    conn.execute("UPDATE jobs SET description=?, description_hash=?, date_posted=?, salary=?, start_date=? WHERE id=?", (job.get("description", ""), job["description_hash"], job.get("date_posted"), salary, start_date, row["id"]))
                return row["id"], False
            cur = conn.execute("INSERT INTO jobs(company,title,location,salary,source,url,description,date_found,date_posted,description_hash,start_date) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (*identity, job.get("salary", ""), job.get("source", ""), url, job.get("description", ""), now, job.get("date_posted"), job.get("description_hash", ""), job.get("start_date", "")))
            return cur.lastrowid, True

    def jobs(self, where: str = "1=1", params: tuple = ()) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY COALESCE(match_score,-1) DESC, date_found DESC", params)]

    def job(self, job_id: int) -> dict | None:
        rows = self.jobs("id=?", (job_id,))
        return rows[0] if rows else None

    def update_job(self, job_id: int, **fields) -> None:
        allowed = {"status", "salary", "start_date", "rule_score", "ai_score", "match_score", "match_reason", "strengths_json", "missing_json", "risks_json", "recommendation", "model_used", "requirements_json", "preferred_json", "translation_zh", "translation_model", "translation_hash"}
        values = {k: v for k, v in fields.items() if k in allowed}
        if not values:
            return
        with self.connect() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in values)} WHERE id=?", (*values.values(), job_id))

    def add_resume_version(self, **record) -> int:
        with self.connect() as conn:
            cur = conn.execute("INSERT INTO resume_versions(job_id,version_name,source_path,content,changes_json,document_type,model_used,created_at) VALUES(?,?,?,?,?,?,?,?)", (record.get("job_id"), record["version_name"], record.get("source_path"), record["content"], record.get("changes_json", "[]"), record.get("document_type", "Resume"), record.get("model_used"), datetime.now().astimezone().isoformat()))
            return cur.lastrowid

    def replace_resume_version(self, version_id: int, **record) -> None:
        """Replace a selected local draft in place and return it to review state."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE resume_versions SET job_id=?, source_path=?, content=?, changes_json=?, document_type=?, model_used=?, created_at=?, approved=0, rejected=0 WHERE id=?",
                (record.get("job_id"), record.get("source_path"), record["content"], record.get("changes_json", "[]"), record.get("document_type", "Resume"), record.get("model_used"), datetime.now().astimezone().isoformat(), version_id),
            )

    def resume_versions(self) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT r.*, j.company, j.title FROM resume_versions r LEFT JOIN jobs j ON j.id=r.job_id ORDER BY r.id DESC")]

    def set_resume_decision(self, version_id: int, approved: bool) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE resume_versions SET approved=?, rejected=? WHERE id=?", (int(approved), int(not approved), version_id))

    def remove_resume_version(self, version_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM resume_versions WHERE id=?", (version_id,)).fetchone()
            if row:
                conn.execute("DELETE FROM resume_versions WHERE id=?", (version_id,))
            return dict(row) if row else None

    def add_cover_letter(self, job_id: int, path: str, content: str, model: str) -> int:
        with self.connect() as conn:
            return conn.execute("INSERT INTO cover_letters(job_id,path,content,model_used,created_at) VALUES(?,?,?,?,?)", (job_id, path, content, model, datetime.now().astimezone().isoformat())).lastrowid

    def cover_letters(self) -> list[dict]:
        with self.connect() as conn:
            query = """
                SELECT c.*, j.company, j.title FROM cover_letters c
                LEFT JOIN jobs j ON j.id=c.job_id ORDER BY c.id DESC
            """
            return [dict(row) for row in conn.execute(query)]

    def remove_cover_letter(self, cover_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM cover_letters WHERE id=?", (cover_id,)).fetchone()
            if row:
                conn.execute("DELETE FROM cover_letters WHERE id=?", (cover_id,))
            return dict(row) if row else None

    def add_supporting_document(self, original_name: str, stored_path: str, extracted_path: str | None, status: str, character_count: int) -> int:
        with self.connect() as conn:
            return conn.execute(
                "INSERT INTO supporting_documents(original_name,stored_path,extracted_path,extraction_status,character_count,added_at) VALUES(?,?,?,?,?,?)",
                (original_name, stored_path, extracted_path, status, character_count, datetime.now().astimezone().isoformat()),
            ).lastrowid

    def supporting_documents(self) -> list[dict]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM supporting_documents ORDER BY id DESC")]

    def remove_supporting_document(self, document_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM supporting_documents WHERE id=?", (document_id,)).fetchone()
            if row:
                conn.execute("DELETE FROM supporting_documents WHERE id=?", (document_id,))
            return dict(row) if row else None

    def mark_applied(self, job_id: int) -> None:
        now = datetime.now().astimezone().isoformat()
        with self.connect() as conn:
            job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            previous = (job["status"] if job and job["status"] != "Applied" else "New")
            conn.execute("INSERT INTO applications(job_id,status,applied_at,previous_status) VALUES(?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET status=excluded.status, applied_at=excluded.applied_at", (job_id, "Applied", now, previous))
            conn.execute("UPDATE jobs SET status='Applied' WHERE id=?", (job_id,))

    def unmark_applied(self, job_id: int) -> bool:
        with self.connect() as conn:
            record = conn.execute("SELECT previous_status FROM applications WHERE job_id=?", (job_id,)).fetchone()
            if not record:
                return False
            conn.execute("DELETE FROM applications WHERE job_id=?", (job_id,))
            conn.execute("UPDATE jobs SET status=? WHERE id=? AND status='Applied'", (record["previous_status"] or "New", job_id))
            return True

    def application_rows(self) -> list[dict]:
        query = """
        SELECT j.*,
          a.applied_at,
          (SELECT version_name FROM resume_versions r WHERE r.job_id=j.id AND r.approved=1 ORDER BY r.id DESC LIMIT 1) AS resume_version,
          (SELECT path FROM cover_letters c WHERE c.job_id=j.id ORDER BY c.id DESC LIMIT 1) AS cover_letter_path
        FROM jobs j LEFT JOIN applications a ON a.job_id=j.id
        ORDER BY COALESCE(a.applied_at, j.date_found) DESC
        """
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(query)]

    def record_ai(self, task: str, model: str, elapsed_ms: int, success: bool, error: str = "") -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO ai_requests(task,model,elapsed_ms,success,created_at,error) VALUES(?,?,?,?,?,?)", (task, model, elapsed_ms, int(success), datetime.now().astimezone().isoformat(), error[:500]))
