"""Structured logging with file output and rotation."""

import logging, sys, json
from datetime import datetime, timezone, timezone
from logging.handlers import RotatingFileHandler
from src.config import settings

class JSONFormatter(logging.Formatter):
    """Output logs as JSON lines for machine parsing."""
    def format(self, record):
        obj = {
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(obj, ensure_ascii=False)

def setup_logging():
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    # Console — human readable
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(console)

    # File — JSON structured (1 MB per file, keep 5)
    try:
        fh = RotatingFileHandler("albion.log", maxBytes=1_000_000, backupCount=5)
        fh.setLevel(level)
        fh.setFormatter(JSONFormatter())
        root.addHandler(fh)
    except Exception as e:
        console.handle(logging.makeLogRecord({
            "name": __name__, "level": logging.WARNING,
            "msg": f"Could not create log file: {e}",
        }))

    # Quiet down libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
