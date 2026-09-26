"""
test_applog.py — Регрессия P0-3: NameError в setup_logging.

Историческая ошибка: в обработчике except вызывалось несуществующее имя
``logger`` (локальная переменная называется ``log``). При сбое
``handler.close()`` функция setup_logging() падала с NameError вместо
штатной деградации в stderr-only.
"""

import contextlib
import logging
import sys
from pathlib import Path

import pytest

import applog


class _BrokenHandler(logging.Handler):
    """Хендлер, у которого close() всегда падает."""

    def close(self) -> None:
        raise RuntimeError("close failed (регрессия P0-3)")


class TestResourcePath:
    """FIX-20: ресурсы рантайма ищутся через один хелпер.

    Приёмка: все 8 `.ps1` и все файлы локалей находятся И в dev-режиме,
    И при эмуляции собранного .exe (`sys.frozen` + `sys._MEIPASS`).
    Без второй проверки смена способа сборки или перенос файлов в подпакет
    ломали бы пути молча. Счётчик упал с 9 до 8 вместе с удалением
    скрипта синхронизации Экрана приветствия (решение владельца
    26.09.2026, Problems.MD §6 п.1).
    """

    PS1_COUNT = 8

    @staticmethod
    def _as_frozen(monkeypatch, tmp_path: Path) -> Path:
        """Эмулировать PyInstaller: данные распакованы в sys._MEIPASS."""
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        return tmp_path

    def test_dev_mode_scripts_exist(self) -> None:
        scripts = applog.resource_path("scripts")
        found = list(scripts.glob("*.ps1"))
        assert len(found) == self.PS1_COUNT, found
        # Конкретные файлы, которые вызывает cleaner, — не «вообще какие-то».
        for name in (
            "layout_cleaner_cleanup.ps1",
            "layout_cleaner_restore.ps1",
            "layout_cleaner_backup.ps1",
            "layout_cleaner_reg_import.ps1",
            "layout_cleaner_stop_ctfmon.ps1",
            "layout_cleaner_start_ctfmon.ps1",
        ):
            assert (scripts / name).is_file(), name

    def test_dev_mode_locales_exist(self) -> None:
        locales = applog.resource_path("locales")
        for name in ("en", "ru", "de", "es", "pt", "zh"):
            assert (locales / f"{name}.json").is_file(), name

    def test_frozen_mode_uses_meipass(self, monkeypatch, tmp_path: Path) -> None:
        meipass = self._as_frozen(monkeypatch, tmp_path)
        (meipass / "scripts").mkdir()
        (meipass / "locales").mkdir()
        assert applog.resource_path("scripts") == meipass / "scripts"
        assert applog.resource_path("locales") == meipass / "locales"

    def test_frozen_mode_falls_back_to_exe_dir(self, monkeypatch, tmp_path) -> None:
        """Без sys._MEIPASS (не PyInstaller) — каталог исполняемого файла."""
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        monkeypatch.setattr(sys, "executable", str(tmp_path / "app.exe"))
        assert applog.resource_path("scripts") == tmp_path / "scripts"

    def test_modules_use_the_helper(self) -> None:
        """cleaner/i18n обязаны брать пути через applog.resource_path.

        Проверяем значением, а не поиском по исходнику: если кто-то вернёт
        ``Path(__file__).parent``, тест упадёт на неправильном пути.
        """
        import cleaner
        import i18n

        assert applog.resource_path("scripts") == cleaner._PS_DIR
        assert applog.resource_path("locales") == i18n.LOCALES_DIR
        # Реальные каталоги (dev-режим) — ресурсы лежат на диске.
        assert cleaner._PS_DIR.is_dir()
        assert i18n.LOCALES_DIR.is_dir()

    def test_no_module_builds_resource_path_manually(self) -> None:
        """Ни один модуль не строит путь к данным через __file__ вручную.

        Это и есть суть FIX-20: единственный источник истины вместо
        копипасты ``Path(__file__).parent`` по модулям.

        Комментарии пропускаются: в них умышленно описано, как выглядел
        старый (неверный) вариант. Без этого фильтра тест ловил бы сам
        себя — ровно тот дефект проверки, из-за которого мок в тестах
        маскировал FIX-24.
        """
        import cleaner
        import i18n

        offenders: list[str] = []
        for module in (cleaner, i18n):
            source = Path(module.__file__).read_text(encoding="utf-8")
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "__file__" in stripped and (
                    "scripts" in stripped or "locales" in stripped
                ):
                    offenders.append(f"{module.__name__}: {stripped}")
        assert not offenders, offenders


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
