from __future__ import annotations

import logging
import json
import sys
import tempfile
from pathlib import Path

from config import ensure_directories, get_paths


def self_test() -> int:
    """Offline packaged-runtime check that does not create or read user data."""
    from app.database import Database
    from app.docx_export import render_cv_letter_docx
    from app.pdf_export import render_cv_letter_pdf
    import PySide6, cryptography, docx, openpyxl, pypdf, reportlab
    from app import safe_markdownify
    sys.modules["markdownify"] = safe_markdownify
    import jobspy.util as jobspy_util
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    icon = root / "assets" / "careeros.ico"
    with tempfile.TemporaryDirectory(prefix="careeros-self-test-") as directory:
        temp = Path(directory); database = Database(temp / "test.db")
        self_check = database.connection()
        with self_check as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        render_cv_letter_docx("Dear Hiring Manager,\n\nTest body.\n\nSincerely,\nCareerOS", temp / "letter.docx", {"full_name":"CareerOS"}, "Example", "Test", options={})
        render_cv_letter_pdf("Dear Hiring Manager,\n\nTest body.\n\nSincerely,\nCareerOS", temp / "letter.pdf", {"full_name":"CareerOS"}, "Example", "Test", options={})
        safe_jobspy = jobspy_util.md is safe_markdownify.markdownify and jobspy_util.markdown_converter("<h9999999>Safe</h9999999>") == "Safe"
        result = {"ok": integrity == "ok" and icon.is_file() and safe_jobspy, "database": integrity, "icon": icon.is_file(), "safe_jobspy": safe_jobspy, "frozen": bool(getattr(sys, "frozen", False))}
    print(json.dumps(result))
    return 0 if result["ok"] else 1


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    ensure_directories()
    logging_format = "%(asctime)s %(levelname)s %(message)s"
    try:
        logging.basicConfig(filename=get_paths().logs / "careeros.log", level=logging.INFO, format=logging_format)
    except OSError:
        # A previous instance can temporarily hold the Windows log file. Logging must not prevent the UI from opening.
        logging.basicConfig(stream=sys.stderr, level=logging.INFO, format=logging_format)
    logging.info("CareerOS starting; auto apply is not implemented")
    from app.gui import run_gui
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
