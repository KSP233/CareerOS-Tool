from __future__ import annotations

import logging
import sys

from config import ensure_directories, get_paths


def main() -> int:
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
