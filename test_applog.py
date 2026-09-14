"""
test_applog.py — Регрессия P0-3: NameError в setup_logging.

Историческая ошибка: в обработчике except вызывалось несуществующее имя
``logger`` (локальная переменная называется ``log``). При сбое
``handler.close()`` функция setup_logging() падала с NameError вместо
штатной деградации в stderr-only.
"""

import contextlib
import logging

import pytest

import applog


class _BrokenHandler(logging.Handler):
    """Хендлер, у которого close() всегда падает."""

    def close(self) -> None:
        raise RuntimeError("close failed (регрессия P0-3)")


def _restore_handlers(log: logging.Logger, saved: list[logging.Handler]) -> None:
    """Вернуть логгеру исходный набор хендлеров (без утечек файла)."""
    for handler in list(log.handlers):
        log.removeHandler(handler)
        with contextlib.suppress(Exception):
            handler.close()
    for handler in saved:
        log.addHandler(handler)


class TestSetupLogging:
    """setup_logging() не должен падать ни при каких состояниях хендлеров."""

    def setup_method(self) -> None:
        self.log = logging.getLogger(applog.LOGGER_NAME)
        self._saved = list(self.log.handlers)

    def teardown_method(self) -> None:
        _restore_handlers(self.log, self._saved)

    def test_broken_handler_close_does_not_raise(self) -> None:
        """Сбой close() у хендлера не роняет setup_logging (регрессия P0-3)."""
        self.log.addHandler(_BrokenHandler())
        # До фикса здесь поднимался NameError: logger не определён в applog
        log_file = applog.setup_logging()
        assert log_file.name == applog.LOG_FILE_NAME
        # логгер остался рабочим: хендлеры заменены (stderr + файл ≤ 2)
        assert 1 <= len(self.log.handlers) <= 2

    def test_setup_logging_is_idempotent(self) -> None:
        """Повторный вызов заменяет хендлеры, а не накапливает их."""
        applog.setup_logging()
        count_after_first = len(self.log.handlers)
        applog.setup_logging()
        assert len(self.log.handlers) == count_after_first
        applog.setup_logging()
        assert len(self.log.handlers) == count_after_first

    def test_custom_log_dir(self, tmp_path) -> None:
        """Журнал пишется в указанный каталог."""
        log_file = applog.setup_logging(log_dir=tmp_path)
        assert log_file.parent == tmp_path
        assert log_file.exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
