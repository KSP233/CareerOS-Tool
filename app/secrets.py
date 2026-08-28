from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet

from config import IS_PORTABLE_BUILD, get_paths


def _key_file() -> Path:
    """Keep an API-key cipher local to the user's data, never in the app bundle."""
    if IS_PORTABLE_BUILD:
        return get_paths().data_dir / ".api-key-encryption"
    return Path(__file__).resolve().parents[1] / ".api-key-encryption"


def _cipher() -> Fernet:
    key_file = _key_file()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    if not key_file.exists():
        key_file.write_bytes(Fernet.generate_key())
    return Fernet(key_file.read_bytes().strip())


def protect_secret(value: str) -> str:
    return _cipher().encrypt(value.encode("utf-8")).decode("ascii") if value else ""


def unprotect_secret(value: str) -> str:
    return _cipher().decrypt(value.encode("ascii")).decode("utf-8") if value else ""
