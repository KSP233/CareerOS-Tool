from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes
from pathlib import Path

from cryptography.fernet import Fernet

from config import IS_PORTABLE_BUILD, get_paths


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


if os.name == "nt":
    _CRYPT32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CRYPT32.CryptProtectData.argtypes = [ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    _CRYPT32.CryptProtectData.restype = wintypes.BOOL
    _CRYPT32.CryptUnprotectData.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    _CRYPT32.CryptUnprotectData.restype = wintypes.BOOL
    _KERNEL32.LocalFree.argtypes = [ctypes.c_void_p]
    _KERNEL32.LocalFree.restype = ctypes.c_void_p


def _blob(data: bytes) -> tuple[_DataBlob, object]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _dpapi_protect(data: bytes) -> bytes:
    source, source_buffer = _blob(data); output = _DataBlob()
    if not _CRYPT32.CryptProtectData(ctypes.byref(source), "CareerOS API key", None, None, None, 0x1, ctypes.byref(output)):
        raise ctypes.WinError()
    try: return ctypes.string_at(output.pbData, output.cbData)
    finally:
        _KERNEL32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p)); del source_buffer


def _dpapi_unprotect(data: bytes) -> bytes:
    source, source_buffer = _blob(data); output = _DataBlob()
    if not _CRYPT32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(output)):
        raise ctypes.WinError()
    try: return ctypes.string_at(output.pbData, output.cbData)
    finally:
        _KERNEL32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p)); del source_buffer


def _legacy_key_file() -> Path:
    """Locate the old Fernet key only to read pre-DPAPI settings."""
    if IS_PORTABLE_BUILD:
        return get_paths().data_dir / ".api-key-encryption"
    return Path(__file__).resolve().parents[1] / ".api-key-encryption"


def protect_secret(value: str) -> str:
    if not value: return ""
    if os.name != "nt": raise RuntimeError("CareerOS API-key protection requires Windows DPAPI")
    return "dpapi:" + base64.urlsafe_b64encode(_dpapi_protect(value.encode("utf-8"))).decode("ascii")


def unprotect_secret(value: str) -> str:
    if not value: return ""
    if value.startswith("dpapi:"):
        if os.name != "nt": raise RuntimeError("This API key is protected for a Windows account")
        try:
            encrypted = base64.urlsafe_b64decode(value.removeprefix("dpapi:").encode("ascii"))
            return _dpapi_unprotect(encrypted).decode("utf-8")
        except Exception as exc:
            raise ValueError("The saved API key belongs to another Windows account. Enter it again in Settings.") from exc
    key_file = _legacy_key_file()
    if not key_file.is_file():
        raise ValueError("The saved legacy API key cannot be decrypted. Enter it again in Settings.")
    return Fernet(key_file.read_bytes().strip()).decrypt(value.encode("ascii")).decode("utf-8")
