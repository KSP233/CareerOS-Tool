# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

root = Path(SPECPATH).resolve()
datas = [(str(root / "assets"), "assets")]
binaries = []
hiddenimports = [
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "pypdf", "reportlab",
    "openpyxl", "defusedxml", "cryptography", "pandas", "numpy",
]
for package in ("jobspy", "tls_client"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas; binaries += package_binaries; hiddenimports += package_hidden
hiddenimports += collect_submodules("jobspy")

a = Analysis(
    [str(root / "main.py")], pathex=[str(root)], binaries=binaries,
    datas=datas, hiddenimports=hiddenimports, hookspath=[], hooksconfig={},
    runtime_hooks=[], excludes=["markdownify"], noarchive=False, optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="CareerOS", debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False, console=False,
    icon=str(root / "assets" / "careeros.ico"),
    version=str(root / "CareerOS.version.txt"),
    uac_admin=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="CareerOS")
