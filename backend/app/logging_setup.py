"""Small JSON log sink for the Hub's own events, excluding exception payloads."""

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False)


def install_log_handler(data_dir: Path) -> logging.Handler:
    path = data_dir / "logs" / "app.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
    path.chmod(0o600)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("app")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return handler


def remove_log_handler(handler: logging.Handler) -> None:
    logging.getLogger("app").removeHandler(handler)
    handler.close()
