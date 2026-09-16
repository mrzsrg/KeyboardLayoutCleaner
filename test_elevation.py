#!/usr/bin/env python3
"""test_elevation.py - Unit tests for elevation mechanism.

Uses shared mocks from conftest.py:
- conftest._make_ctk() - mocked customtkinter
- conftest._cleanup_sandbox_state - autouse fixture (sandbox cleanup)
"""

import ctypes
import importlib
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Import shared mocks from conftest (avoids duplication)
from conftest import _make_ctk


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
    # Force reload to ensure fresh bytecode (avoids stale .pyc issues)
    import importlib

    import main as _m

    importlib.reload(_m)


# _reset_sandbox_state is now provided by conftest._cleanup_sandbox_state
# (autouse fixture with scope="function" — runs after each test)
# Local version removed to avoid duplication


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
        import config
        import main  # noqa: F401 — импорт исполняет код модуля (синхронизация sandbox-флага)

        assert bool(config.SANDBOX_MODE) is True

    def test_synced_to_cleaner(self, monkeypatch):
        _patch_main(monkeypatch, sandbox=True)
        import config
        import main  # noqa: F401 — импорт исполняет код модуля (синхронизация sandbox-флага)

        assert bool(config.SANDBOX_MODE) is True

    def test_non_sandbox(self, monkeypatch):
        _patch_main(monkeypatch, sandbox=False)
        import config

        assert bool(config.SANDBOX_MODE) is False


class TestMutexMechanism:
    def test_mutex_name_defined(self):
        import main

        assert hasattr(main, "_MUTEX_NAME")
        assert isinstance(main._MUTEX_NAME, str)
        assert len(main._MUTEX_NAME) > 0

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

    def test_sandbox_mode_proxy_repr_no_recursion(self):
        """__repr__ не должен ссылаться на self (бесконечная рекурсия).

        Регрессия: f"SandboxModeProxy({self})" -> str(self) -> __repr__ -> RecursionError.
        """
        import config

        with patch.object(config, "_SANDBOX_MODE", False):
            text = repr(config.SANDBOX_MODE)
        assert text.startswith("<SandboxModeProxy")
        assert "active=False" in text

    def test_sandbox_mode_proxy_repr_reflects_state(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "_SANDBOX_MODE", True)
        assert "active=True" in repr(config.SANDBOX_MODE)
        monkeypatch.setattr(config, "_SANDBOX_MODE", False)


class TestModuleImports:
    def _mock_ctk(self, monkeypatch):
        """Mock customtkinter using shared factory from conftest."""
        monkeypatch.setitem(sys.modules, "customtkinter", _make_ctk())

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
        import config
        import main

        main._apply_sandbox_to_modules(True)
        assert config.is_sandbox_enabled() is True
        main._apply_sandbox_to_modules(False)
        assert config.is_sandbox_enabled() is False


class TestActiveLangCache:
    """Эвристика «активного кеша» раскладки (main._active_lang_cache).

    Кеш активного языка = записи ТОЛЬКО в CTF + этот язык присутствует
    в списке языков Windows (PowerShell). Эвристика защищает живую
    раскладку от ошибочного удаления как фантома.
    """

    @staticmethod
    def _main(monkeypatch):
        if "main" not in sys.modules:
            monkeypatch.setitem(sys.modules, "customtkinter", _make_ctk())
            return importlib.import_module("main")
        return sys.modules["main"]

    def test_ctf_only_plus_active_lang_in_ps_is_cache(self, monkeypatch):
        m = self._main(monkeypatch)
        data = {
            "04190419": [
                {
                    "path": (
                        r"HKCU\Software\Microsoft\CTF\Assemblies\0x00000419"
                        r"\{34745C63-B2F0-4784-8B67-5E12C8701A31}\KeyboardLayout"
                    )
                },
            ],
            "00000419": [{"path": "PowerShell\\Get-WinUserLanguageList"}],
        }
        assert m._active_lang_cache("04190419", data) is True

    def test_registry_preload_location_means_not_cache(self, monkeypatch):
        m = self._main(monkeypatch)
        data = {
            "04190419": [
                {"path": r"HKCU\Software\Microsoft\CTF\Assemblies\{GUID}"},
                {"path": r"HKCU\Keyboard Layout\Preload\1"},
            ],
            "00000419": [{"path": "PowerShell\\Get-WinUserLanguageList"}],
        }
        assert m._active_lang_cache("04190419", data) is False

    def test_registry_substitutes_location_means_not_cache(self, monkeypatch):
        m = self._main(monkeypatch)
        data = {
            "04190419": [
                {"path": r"HKCU\Software\Microsoft\CTF\Assemblies\{GUID}"},
                {"path": r"HKCU\Keyboard Layout\Substitutes"},
            ],
            "00000419": [{"path": "PowerShell\\Get-WinUserLanguageList"}],
        }
        assert m._active_lang_cache("04190419", data) is False

    def test_no_powershell_pair_returns_false(self, monkeypatch):
        m = self._main(monkeypatch)
        data = {
            "04190419": [{"path": r"HKCU\Software\Microsoft\CTF\Assemblies\{GUID}"}]
        }
        assert m._active_lang_cache("04190419", data) is False

    def test_different_langid_in_ps_returns_false(self, monkeypatch):
        m = self._main(monkeypatch)
        data = {
            "04190419": [{"path": r"HKCU\Software\Microsoft\CTF\Assemblies\{GUID}"}],
            "00000409": [{"path": "PowerShell\\Get-WinUserLanguageList"}],
        }
        assert m._active_lang_cache("04190419", data) is False

    def test_invalid_klid_returns_false(self, monkeypatch):
        m = self._main(monkeypatch)
        assert m._active_lang_cache("zz-not-hex", {}) is False

    def test_zero_langid_returns_false(self, monkeypatch):
        m = self._main(monkeypatch)
        data = {"00000000": [{"path": r"HKCU\Software\Microsoft\CTF"}]}
        assert m._active_lang_cache("00000000", data) is False

    def test_missing_locations_returns_false(self, monkeypatch):
        m = self._main(monkeypatch)
        assert m._active_lang_cache("00000409", {}) is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
