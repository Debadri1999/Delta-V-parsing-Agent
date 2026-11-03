"""Logging configuration. Logs stay local and never contain FHX content above DEBUG."""

from __future__ import annotations

import logging
import sys
from pathlib import Path


def setup_logging(log_file: Path | None = None, verbose: bool = False) -> None:
    """Configure console (and optional file) logging.

    Args:
        log_file: Optional path of a run log.
        verbose: Log at DEBUG instead of INFO.
    """
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to cp1252; model text is Unicode
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    for noisy in ("httpx", "httpcore", "openai", "fontTools", "fpdf"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("fontTools.subset").setLevel(logging.ERROR)
