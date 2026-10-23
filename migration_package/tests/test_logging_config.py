from __future__ import annotations

import logging
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from meter_reading_mini.core.logging_config import configure_logging


@pytest.fixture
def isolated_root_logger() -> Iterator[logging.Logger]:
    root_logger = logging.getLogger()
    original_handlers = root_logger.handlers[:]
    for handler in original_handlers:
        root_logger.removeHandler(handler)
    root_logger.addHandler(logging.StreamHandler())

    try:
        yield root_logger
    finally:
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
            if handler not in original_handlers:
                handler.close()
        for handler in original_handlers:
            if handler not in root_logger.handlers:
                root_logger.addHandler(handler)


def _file_handlers(logger: logging.Logger) -> list[RotatingFileHandler]:
    return [handler for handler in logger.handlers if isinstance(handler, RotatingFileHandler)]


def _plain_stream_handlers(logger: logging.Logger) -> list[logging.StreamHandler]:
    return [handler for handler in logger.handlers if type(handler) is logging.StreamHandler]


def test_configure_logging_reuses_handlers_for_same_path(
    tmp_path: Path, isolated_root_logger: logging.Logger
) -> None:
    configure_logging(log_dir=tmp_path)
    handlers = isolated_root_logger.handlers[:]
    file_handlers = _file_handlers(isolated_root_logger)

    configure_logging(log_dir=tmp_path)

    assert len(file_handlers) == 1
    assert _file_handlers(isolated_root_logger) == file_handlers
    assert isolated_root_logger.handlers == handlers
    assert len(_plain_stream_handlers(isolated_root_logger)) == 1


def test_configure_logging_closes_old_file_handler_when_path_changes(
    tmp_path: Path, isolated_root_logger: logging.Logger
) -> None:
    first_log_dir = tmp_path / "first"
    second_log_dir = tmp_path / "second"

    configure_logging(log_dir=first_log_dir)
    old_handler = _file_handlers(isolated_root_logger)[0]
    configure_logging(log_dir=second_log_dir)

    file_handlers = _file_handlers(isolated_root_logger)
    assert len(file_handlers) == 1
    assert Path(file_handlers[0].baseFilename) == second_log_dir / "app.log"
    assert old_handler not in isolated_root_logger.handlers
    assert old_handler.stream is None
    assert len(_plain_stream_handlers(isolated_root_logger)) == 1


def test_configure_logging_does_not_add_stream_handler_to_existing_root_handlers(
    tmp_path: Path, isolated_root_logger: logging.Logger
) -> None:
    stream_handler = logging.StreamHandler()
    isolated_root_logger.addHandler(stream_handler)
    initial_stream_handler_count = len(_plain_stream_handlers(isolated_root_logger))
    try:
        configure_logging(log_dir=tmp_path)
        configure_logging(log_dir=tmp_path)

        assert isolated_root_logger.handlers.count(stream_handler) == 1
        assert len(_plain_stream_handlers(isolated_root_logger)) == initial_stream_handler_count
    finally:
        isolated_root_logger.removeHandler(stream_handler)
        stream_handler.close()
