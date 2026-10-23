"""Shared logging configuration for the PC app."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_FILE_NAME = "app.log"
LOG_FILE_MAX_BYTES = 50 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 10
_NOISY_LOGGERS = {
	"alembic": logging.WARNING,
	"httpcore": logging.WARNING,
	"httpx": logging.WARNING,
}


def get_default_log_dir() -> Path:
	"""Return the canonical directory for application log files."""

	return Path(__file__).resolve().parents[2] / "logs"


def get_app_log_file_path(*, log_dir: str | Path | None = None) -> Path:
	"""Return the full path to the rotating application log file."""

	resolved_log_dir = Path(log_dir) if log_dir is not None else get_default_log_dir()
	return resolved_log_dir / LOG_FILE_NAME


def configure_logging(*, log_dir: str | Path | None = None) -> None:
	"""Configure application logging with a rotating file handler."""

	log_format = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"

	logging.basicConfig(
		format=log_format,
		level=logging.INFO,
	)

	resolved_log_dir = Path(log_dir) if log_dir is not None else get_default_log_dir()
	resolved_log_dir.mkdir(parents=True, exist_ok=True)
	log_file = get_app_log_file_path(log_dir=resolved_log_dir)

	root_logger = logging.getLogger()
	if any(
		isinstance(handler, RotatingFileHandler) and Path(getattr(handler, "baseFilename", "")).resolve() == log_file.resolve()
		for handler in root_logger.handlers
	):
		return

	file_handler = RotatingFileHandler(
		log_file,
		encoding="utf-8",
		maxBytes=LOG_FILE_MAX_BYTES,
		backupCount=LOG_FILE_BACKUP_COUNT,
	)
	file_handler.setFormatter(logging.Formatter(log_format))
	file_handler.setLevel(logging.INFO)
	root_logger.addHandler(file_handler)

	for logger_name, level in _NOISY_LOGGERS.items():
		logging.getLogger(logger_name).setLevel(level)


__all__ = [
	"LOG_FILE_BACKUP_COUNT",
	"LOG_FILE_MAX_BYTES",
	"get_app_log_file_path",
	"get_default_log_dir",
	"configure_logging",
]