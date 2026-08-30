from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path


SUPPORTED_RESUME_IMPORT_EXTENSIONS = {".docx"}


IS_PORTABLE_BUILD = bool(getattr(sys, "frozen", False))
PROGRAM_DIR = Path(sys.executable).resolve().parent if IS_PORTABLE_BUILD else Path(__file__).resolve().parent
# A packaged copy is intentionally self-contained: it never discovers or reuses
# the developer's existing CareerOS data, settings, or API configuration.
APP_STATE_DIR = PROGRAM_DIR / "CareerOS-data" if IS_PORTABLE_BUILD else Path(os.environ.get("LOCALAPPDATA", str(PROGRAM_DIR))) / "CareerOS"
DEFAULT_DATA_DIR = APP_STATE_DIR if IS_PORTABLE_BUILD else APP_STATE_DIR / "data"
LOCATION_FILE = APP_STATE_DIR / "data-location.json"
LEGACY_LOCATION_FILE = PROGRAM_DIR / "data-location.json"
DATABASE_FILENAME = "careeros.db"
LEGACY_DATABASE_FILENAME = "applypilot.db"

DEFAULT_SETTINGS = {
    "data_dir": str(DEFAULT_DATA_DIR),
    "language": "en",
    "ai_mode": "Auto",
    "fallback_enabled": True,
    "ollama_url": "http://127.0.0.1:11434",
    "models": {"deep": "gpt-oss:20b", "fast": "qwen3.5:9b"},
    "api": {
        "enabled": False,
        "base_url": "https://api.openai.com/v1",
        "model": "",
        "encrypted_key": "",
    },
    "resume_path": "",
    "generation_prompts": {
        "general": "",
    },
    "resume_pdf": {
        "style": "Modern",
        "font_size": 9,
        "font_family": "Helvetica",
        "line_spacing": 1.0,
        "section_spacing": "Normal",
        "margins": "Standard",
        "docx_layout": "Professional one-page",
    },
    "match_weights": {
        "rule": 70,
        "ai": 30,
    },
    "profile": {
        "first_name": "",
        "last_name": "",
        "email": "",
        "phone": "",
        "address": "",
        "city": "",
        "province": "",
        "postal_code": "",
        "linkedin_url": "",
        "portfolio_url": "",
        "skills": "",
        "education": "",
        "experience": "",
        "work_authorization": "",
        "languages": "",
        "licenses": "",
        "availability": "",
        "desired_titles": "",
        "preferred_locations": "",
        "salary_preference": "",
        "additional_facts": "",
    },
    "search": {
        "locations": ["Ottawa, ON", "Montreal, QC", "Toronto, ON", "Quebec City, QC"],
        "queries": ["Mechanical Engineer", "Junior Mechanical Engineer", "Mechanical Design Engineer", "Mechanical Engineering Intern", "Mechanical Engineering Co-op", "Aerospace Engineer", "Aerospace Engineering Intern"],
        "sites": ["indeed", "linkedin"],
        "distance": 30,
        "hours_old": 72,
        "results_per_search": 10,
    },
}


@dataclass(frozen=True)
class Paths:
    data_dir: Path
    database: Path
    settings: Path
    resumes_original: Path
    resumes_generated: Path
    resumes_approved: Path
    resumes_archive: Path
    supporting_documents: Path
    cover_letters: Path
    cache: Path
    logs: Path
    exports: Path
    backups: Path


def active_data_dir() -> Path:
    if IS_PORTABLE_BUILD:
        return DEFAULT_DATA_DIR
    for location_file in (LOCATION_FILE, LEGACY_LOCATION_FILE):
        try:
            saved = json.loads(location_file.read_text(encoding="utf-8"))
            value = str(saved.get("data_dir", "")).strip()
            if value:
                return Path(value).expanduser()
        except (OSError, ValueError, TypeError):
            pass
    return DEFAULT_DATA_DIR


def load_settings() -> dict:
    current_root = active_data_dir()
    settings_path = current_root / "data" / "settings.json"
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    settings["data_dir"] = str(current_root)
    if settings_path.exists():
        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        for key, value in saved.items():
            if isinstance(value, dict) and isinstance(settings.get(key), dict):
                settings[key].update(value)
            else:
                settings[key] = value
    return settings


def get_paths(settings: dict | None = None) -> Paths:
    settings = settings or load_settings()
    root = Path(settings.get("data_dir") or active_data_dir())
    return Paths(
        data_dir=root,
        database=root / "data" / DATABASE_FILENAME,
        settings=root / "data" / "settings.json",
        resumes_original=root / "resumes" / "original",
        resumes_generated=root / "resumes" / "generated",
        resumes_approved=root / "resumes" / "approved",
        resumes_archive=root / "resumes" / "archive",
        supporting_documents=root / "supporting_documents",
        cover_letters=root / "cover_letters",
        cache=root / "cache",
        logs=root / "logs",
        exports=root / "exports",
        backups=root / "backups",
    )


def ensure_directories(settings: dict | None = None) -> Paths:
    paths = get_paths(settings)
    for directory in (
        paths.database.parent, paths.resumes_original, paths.resumes_generated,
        paths.resumes_approved, paths.resumes_archive, paths.cover_letters,
        paths.supporting_documents, paths.cache, paths.logs, paths.exports, paths.backups,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    legacy_database = paths.database.with_name(LEGACY_DATABASE_FILENAME)
    if not paths.database.exists() and legacy_database.exists() and legacy_database.stat().st_size:
        # Copy via SQLite rather than renaming: an older database may still have a
        # WAL journal after an interrupted shutdown. The legacy file is retained as
        # a recoverable backup and is never used again by CareerOS.
        source = sqlite3.connect(legacy_database)
        destination = sqlite3.connect(paths.database)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
    if not paths.settings.exists():
        paths.settings.write_text(json.dumps(settings or DEFAULT_SETTINGS, indent=2), encoding="utf-8")
    return paths


def save_settings(settings: dict) -> None:
    paths = ensure_directories(settings)
    paths.settings.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    LOCATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCATION_FILE.write_text(json.dumps({"data_dir": str(paths.data_dir)}, indent=2), encoding="utf-8")


def migrate_data_directory(settings: dict, destination: str) -> tuple[Path, Path]:
    """Copy active user data to destination. The source is deliberately retained."""
    source = get_paths()
    target_root = Path(destination).expanduser().resolve()
    if target_root == source.data_dir.resolve():
        return source.data_dir, target_root
    target = get_paths({**settings, "data_dir": str(target_root)})
    target_root.mkdir(parents=True, exist_ok=True)
    if target.database.exists() and target.database.stat().st_size:
        raise FileExistsError(f"The selected folder already contains a CareerOS database:\n{target.database}")

    # Copy normal files first. A live SQLite database is copied through its backup API.
    for child in source.data_dir.iterdir() if source.data_dir.exists() else ():
        destination_child = target_root / child.name
        if child.resolve() == source.database.parent.resolve():
            destination_child.mkdir(parents=True, exist_ok=True)
            for item in child.iterdir():
                if item.name not in {source.database.name, source.database.name + "-wal", source.database.name + "-shm", "settings.json"}:
                    if item.is_dir():
                        shutil.copytree(item, destination_child / item.name, dirs_exist_ok=True)
                    else:
                        shutil.copy2(item, destination_child / item.name)
        elif child.is_dir():
            shutil.copytree(child, destination_child, dirs_exist_ok=True)
        else:
            shutil.copy2(child, destination_child)

    target.database.parent.mkdir(parents=True, exist_ok=True)
    if source.database.exists():
        src = sqlite3.connect(source.database)
        dst = sqlite3.connect(target.database)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    moved = json.loads(json.dumps(settings))
    moved["data_dir"] = str(target_root)
    ensure_directories(moved)
    target.settings.write_text(json.dumps(moved, indent=2), encoding="utf-8")
    LOCATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCATION_FILE.write_text(json.dumps({"data_dir": str(target_root)}, indent=2), encoding="utf-8")
    return source.data_dir, target_root
