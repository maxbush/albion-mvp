"""Structured logging with file output and rotation."""

import logging, sys, json
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from src.config import settings

class SafeStreamHandler(logging.StreamHandler):
    """StreamHandler, который не падает на UnicodeEncodeError в Windows (cp1251).

    Эмодзи и другие символы вне cp1251 заменяются на '?' вместо краша stderr.
    """
    def emit(self, record):
        try:
            super().emit(record)
        except UnicodeEncodeError:
            # Падаем на форматировании — пробуем заменить неподдерживаемые символы
            try:
                msg = self.format(record)
                stream = self.stream
                # Заменяем символы вне кодировки потока
                encoded = msg.encode(stream.encoding or 'utf-8', errors='replace')
                stream.write(encoded.decode(stream.encoding or 'utf-8'))
                stream.write(self.terminator)
                self.flush()
            except Exception:
                self.handleError(record)

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


class SafeStreamHandler(logging.StreamHandler):
    """StreamHandler that pre-encodes messages to the stream's encoding to prevent
    UnicodeEncodeError and traceback spam in terminals with restricted encoding."""
    def emit(self, record):
        try:
            msg = self.format(record)
            stream = self.stream
            encoding = getattr(stream, "encoding", None) or sys.getdefaultencoding() or "utf-8"
            try:
                safe_msg = msg.encode(encoding, errors="replace").decode(encoding, errors="replace")
            except Exception:
                safe_msg = msg.encode("ascii", errors="replace").decode("ascii", errors="replace")
            stream.write(safe_msg + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)


def setup_logging():
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    # Console — human readable, safe against UnicodeEncodeError in ascii streams
    console = SafeStreamHandler(sys.stdout)
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
