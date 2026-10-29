"""
Structured JSON-lines logging on the standard library (no structlog dependency).

    log = get_logger(__name__)
    log.info("order_placed", symbol="BTCUSD", size=3)

Secrets are scrubbed from every record (api keys, signatures).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SECRET_KEYS = {"api_key", "api-key", "api_secret", "secret", "signature", "password", "token"}


def _scrub(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if str(k).lower() in _SECRET_KEYS else _scrub(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    return obj


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(_scrub(extra))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class BoundLogger:
    """Tiny structlog-style adapter: log.info("event", key=value)."""

    def __init__(self, logger: logging.Logger, **bound: Any) -> None:
        self._logger = logger
        self._bound = bound

    def bind(self, **kw: Any) -> BoundLogger:
        return BoundLogger(self._logger, **{**self._bound, **kw})

    def _log(self, level: int, event: str, exc_info: bool = False, **kw: Any) -> None:
        if self._logger.isEnabledFor(level):
            self._logger.log(level, event, extra={"fields": {**self._bound, **kw}},
                             exc_info=exc_info)

    def debug(self, event: str, **kw: Any) -> None:
        self._log(logging.DEBUG, event, **kw)

    def info(self, event: str, **kw: Any) -> None:
        self._log(logging.INFO, event, **kw)

    def warning(self, event: str, **kw: Any) -> None:
        self._log(logging.WARNING, event, **kw)

    def error(self, event: str, **kw: Any) -> None:
        self._log(logging.ERROR, event, **kw)

    def exception(self, event: str, **kw: Any) -> None:
        self._log(logging.ERROR, event, exc_info=True, **kw)

    def critical(self, event: str, **kw: Any) -> None:
        self._log(logging.CRITICAL, event, **kw)


def configure_logging(log_dir: str | Path = "./logs", level: str = "INFO",
                      *, also_stdout: bool = True) -> None:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    fh = logging.handlers.TimedRotatingFileHandler(
        str(log_dir / "trader.jsonl"), when="midnight", backupCount=30, encoding="utf-8", utc=True
    )
    fh.setFormatter(_JsonFormatter())
    root.addHandler(fh)
    if also_stdout:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(_JsonFormatter())
        root.addHandler(sh)


def get_logger(name: str) -> BoundLogger:
    return BoundLogger(logging.getLogger(name))
