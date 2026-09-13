#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_elevation.py - Unit tests for elevation mechanism."""

import ctypes
import os
import sys
from pathlib import Path

import pytest


def _make_ctk():
    """Создать мок customtkinter для тестов."""
    import types
    ctk = types.ModuleType("customtkinter")
    for cls in ("CTk", "CTkApp", "CTkLabel", "CTkButton", "CTkFrame",
                 "CTkEntry", "CTkCheckBox", "CTkTextbox", "CTkScrollableFrame",
                 "CTkFont", "CTkOptionMenu"):
        setattr(ctk, cls, type(cls, (), {"__init__": lambda s, *a, **k: None}))
    ctk.ScalingTracker = type("S", (), {"get_window_scaling": lambda *a: 1.0})
    ctk.set_appearance_mode = lambda *a: None
    ctk.set_default_color_theme = lambda *a: None
    for fn in ("deactivate_automatic_dpi_awareness", "enable_undocked_window_scaling",
                "deactivate_automatic_high_dpi_awareness"):
        setattr(ctk, fn, lambda *a: None)
    return ctk

def _patch_main(monkeypatch, frozen=False, sandbox=False):
    """Подготовить окружение для ЧИСТОГО импорта main (регрессия P0-2).

    Историческая проблема: тесты делали ``import main``, ожидая повторного
    исполнения кода модуля, но Python кеширует модули в sys.modules —
    флаги SANDBOX_MODE оставались от предыдущего теста, и результат зависел
    от ПОРЯДКА запуска (3 «вечно падающих» теста).

    Теперь _patch_main гарантирует детерминизм:
      1) по-настоящему очищает os.environ от утечек SANDBOX_MODE
         (pop, а не monkeypatch.delenv, который «восстанавливает» утечку);
      2) удаляет main/config из sys.modules через monkeypatch.delitem —
         ``import main`` в тесте исполняет код модуля заново, а после теста
         monkeypatch возвращает исходные объекты модулей;
      3) ставит детерминированное значение SANDBOX_MODE в окружении
         ("1" или "0", без промежуточных утечек между тестами).
    """
    # 1) Реальная очистка окружения от утечек предыдущих тестов
    os.environ.pop("SANDBOX_MODE", None)
    # 2) Сброс кеша модулей: import main / config выполнится заново
    monkeypatch.delitem(sys.modules, "main", raising=False)
    monkeypatch.delitem(sys.modules, "config", raising=False)
    # 3) Мок customtkinter на время импорта main
    monkeypatch.setitem(sys.modules, "customtkinter", _make_ctk())
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    home = str(Path.home())
    monkeypatch.setenv("USERPROFILE", home)
    monkeypatch.setenv("HOME", home)
    # 4) Детерминированное окружение (без зависимости от прошлых тестов)
    monkeypatch.setenv("SANDBOX_MODE", "1" if sandbox else "0")


@pytest.fixture(autouse=True)
def _reset_sandbox_state():
    """Страховка порядкозависимости (регрессия P0-2).

    Выполняется ПОСЛЕ каждого теста файла: к этому моменту monkeypatch уже
    восстановил sys.modules, поэтому флаги сбрасываются на канонических
    экземплярах модулей. Ни один тест не оставит после себя
    SANDBOX_MODE=True ни в окружении, ни в модулях.
    """
    yield
    os.environ.pop("SANDBOX_MODE", None)
    try:
        import config as _cfg
        _cfg.SANDBOX_MODE = False
    except Exception:
        pass
    try:
        import scanner as _scn
        _scn.SANDBOX_MODE = False
    except Exception:
        pass


class TestRestartCommandLine:

    def test_non_frozen_restart(self, monkeypatch):
        _patch_main(monkeypatch, frozen=False)
        import main
        assert not main.SANDBOX_MODE
        args = main.parse_args()
        assert hasattr(args, "sandbox")

    def test_frozen_restart(self, monkeypatch):
        _patch_main(monkeypatch, frozen=True)
        import main
        assert not main.SANDBOX_MODE

    def test_sandbox_from_env(self, monkeypatch):
        _patch_main(monkeypatch, frozen=False, sandbox=True)
        import main
        assert main.SANDBOX_MODE is True

    def test_sandbox_flag_sets_env(self, monkeypatch):
        _patch_main(monkeypatch, frozen=False)
        import main
        args = main.parse_args(["--sandbox"])
        assert args.sandbox is True
        assert os.environ.get("SANDBOX_MODE") == "1"


class TestSandboxModeSync:

    def test_synced_to_scanner(self, monkeypatch):
        _patch_main(monkeypatch, sandbox=True)
        import main  # noqa: F401 — импорт исполняет код модуля (синхронизация sandbox-флага)
        import config
        assert config.SANDBOX_MODE is True

    def test_synced_to_cleaner(self, monkeypatch):
        _patch_main(monkeypatch, sandbox=True)
        import main  # noqa: F401 — импорт исполняет код модуля (синхронизация sandbox-флага)
        import config
        assert config.SANDBOX_MODE is True

    def test_non_sandbox(self, monkeypatch):
        _patch_main(monkeypatch, sandbox=False)
        import config
        assert config.SANDBOX_MODE is False


class TestMutexMechanism:

    def test_mutex_name_defined(self):
        import main
        assert hasattr(main, "_MUTEX_NAME")
        assert isinstance(main._MUTEX_NAME, str) and len(main._MUTEX_NAME) > 0

    def test_mutex_create_and_close(self):
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, "TestMutexUnit")
        assert handle != 0
        ctypes.windll.kernel32.CloseHandle(handle)

    def test_mutex_error_already_exists(self):
        h1 = ctypes.windll.kernel32.CreateMutexW(None, False, "TestMutexUnit2")
        assert h1 != 0
        h2 = ctypes.windll.kernel32.CreateMutexW(None, False, "TestMutexUnit2")
        err = ctypes.windll.kernel32.GetLastError()
        assert err == 183
        ctypes.windll.kernel32.CloseHandle(h1)
        ctypes.windll.kernel32.CloseHandle(h2)

    def test_mutex_polling(self):
        import time
        h = ctypes.windll.kernel32.CreateMutexW(None, False, "TestMutexPoll")
        assert h != 0
        ctypes.windll.kernel32.CloseHandle(h)
        time.sleep(0.05)
        test_h = ctypes.windll.kernel32.CreateMutexW(None, False, "TestMutexPoll")
        err = ctypes.windll.kernel32.GetLastError()
        ctypes.windll.kernel32.CloseHandle(test_h)
        assert err != 183

    def test_mutex_acquire_after_release(self):
        """Тест: новый процесс может захватить мьютекс после освобождения старым."""
        import time
        # Создаём мьютекс (имитация старого процесса)
        h1 = ctypes.windll.kernel32.CreateMutexW(None, False, "TestMutexElevate")
        assert h1 != 0

        # Новый процесс пытается создать мьютекс — получает ERROR_ALREADY_EXISTS
        h2 = ctypes.windll.kernel32.CreateMutexW(None, False, "TestMutexElevate")
        err = ctypes.windll.kernel32.GetLastError()
        assert err == 183  # ERROR_ALREADY_EXISTS

        # Старый процесс освобождает мьютекс
        ctypes.windll.kernel32.CloseHandle(h1)
        ctypes.windll.kernel32.CloseHandle(h2)

        # Даём ядру Windows время на обработку
        time.sleep(0.1)

        # Теперь новый процесс может создать мьютекс
        h3 = ctypes.windll.kernel32.CreateMutexW(None, True, "TestMutexElevate")
        err = ctypes.windll.kernel32.GetLastError()
        assert err != 183  # Мьютекс успешно создан
        assert h3 != 0
        ctypes.windll.kernel32.CloseHandle(h3)


class TestSandboxConfig:

    def test_sandbox_root_defined(self):
        from config import _SANDBOX_ROOT
        assert isinstance(_SANDBOX_ROOT, str)
        assert len(_SANDBOX_ROOT) > 0

    def test_is_sandbox_enabled_env(self, monkeypatch):
        from config import is_sandbox_enabled
        monkeypatch.setenv("SANDBOX_MODE", "1")
        assert is_sandbox_enabled() is True

    def test_is_sandbox_enabled_env_false(self, monkeypatch):
        from config import is_sandbox_enabled
        monkeypatch.setenv("SANDBOX_MODE", "0")
        assert is_sandbox_enabled() is False


class TestModuleImports:

    def _mock_ctk(self, monkeypatch):
        import types
        ctk = types.ModuleType("customtkinter")
        for cls in ("CTk", "CTkApp", "CTkLabel", "CTkButton", "CTkFrame",
                     "CTkEntry", "CTkCheckBox", "CTkTextbox", "CTkScrollableFrame",
                     "CTkFont", "CTkOptionMenu"):
            setattr(ctk, cls, type(cls, (), {"__init__": lambda s, *a, **k: None}))
        ctk.ScalingTracker = type("S", (), {"get_window_scaling": lambda *a: 1.0})
        ctk.set_appearance_mode = lambda *a: None
        for fn in ("deactivate_automatic_dpi_awareness", "enable_undocked_window_scaling",
                    "deactivate_automatic_high_dpi_awareness"):
            setattr(ctk, fn, lambda *a: None)
        monkeypatch.setitem(sys.modules, "customtkinter", ctk)

    def test_imports_cleaner(self, monkeypatch):
        self._mock_ctk(monkeypatch)
        import main
        assert hasattr(main, "delete_layout")

    def test_imports_scanner(self, monkeypatch):
        self._mock_ctk(monkeypatch)
        import main
        assert hasattr(main, "scan_keyboard_layouts")

    def test_apply_sandbox_to_modules(self, monkeypatch):
        self._mock_ctk(monkeypatch)
        import main
        import config
        main._apply_sandbox_to_modules(True)
        assert config.SANDBOX_MODE is True
        main._apply_sandbox_to_modules(False)
        assert config.SANDBOX_MODE is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
