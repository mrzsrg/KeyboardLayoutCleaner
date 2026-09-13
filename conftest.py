"""
conftest.py — общие fixtures и моки для тестов Keyboard Layout Cleaner.

Предоставляет:
- Моки winreg (OpenKey, QueryValueEx, EnumValue, EnumKey)
- Моки subprocess.run для PowerShell
- Моки ctypes.windll.shell32.IsUserAnAdmin
- Утилиты для настройки моков
"""

import ctypes

from unittest.mock import MagicMock

import pytest


class MockRegKey:
    """Мок-ключ реестра с поддержкой EnumValue/EnumKey/QueryValueEx."""

    def __init__(self, values=None, subkeys=None):
        self._values = values or {}
        self._subkeys = subkeys or []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def QueryValueEx(self, value_name):
        """Вернуть значение и тип (REG_SZ = 1 по умолчанию)."""
        val = self._values.get(value_name, "")
        # Если значение не найдено — возвращаем пустые значения, а не падаем
        if val is None:
            return ("", 1)
        return (val, 1)

    def EnumValue(self, idx):
        if idx >= len(self._values):
            raise OSError("No more values")
        items = list(self._values.items())
        name, val = items[idx]
        return (name, val, 1)

    def EnumKey(self, idx):
        if idx >= len(self._subkeys):
            raise OSError("No more subkeys")
        return self._subkeys[idx]


def create_mock_winreg(key_values=None, key_subkeys=None):
    """Создать мок winreg с заданными значениями."""
    mock = MagicMock()
    mock_key_instances = []

    def _open_key(root, subkey_path, *args, **kwargs):
        path = subkey_path if isinstance(subkey_path, str) else ""
        key = MockRegKey(
            values=key_values.get(path, {}) if key_values else {},
            subkeys=key_subkeys.get(path, []) if key_subkeys else [],
        )
        mock_key_instances.append(key)
        mock_key = MagicMock()
        mock_key.__enter__.return_value = key
        mock_key.__exit__.return_value = None
        return mock_key

    mock.OpenKey.side_effect = _open_key
    mock.HKEY_CURRENT_USER = ctypes.HKEY_CURRENT_USER
    mock.HKEY_LOCAL_MACHINE = ctypes.HKEY_LOCAL_MACHINE
    mock.HKEY_USERS = ctypes.HKEY_USERS
    mock.KEY_READ = 0x20019
    mock.KEY_WRITE = 0x20000
    mock.REG_SZ = 1
    mock.REG_MULTI_SZ = 7
    mock.REG_DWORD = 4
    return mock, mock_key_instances


def create_mock_subprocess_run(returncode=0, stdout="", stderr=""):
    """Создать мок subprocess.run для PowerShell-вызовов."""
    mock_result = MagicMock()
    mock_result.returncode = returncode
    mock_result.stdout = stdout
    mock_result.stderr = stderr
    mock_run = MagicMock()
    mock_run.return_value = mock_result
    return mock_run


def mock_is_admin(is_admin_flag=True):
    """Создать мок для IsUserAnAdmin."""
    mock = MagicMock()
    mock.IsUserAnAdmin.return_value = is_admin_flag
    return mock


@pytest.fixture
def mock_winreg():
    """Фикстура: пустой мок winreg."""
    mock, _ = create_mock_winreg()
    return mock


@pytest.fixture
def mock_winreg_with_values():
    """Фикстура: мок winreg с предзаполненными значениями."""
    key_values = {
        r"Keyboard Layout\Preload": {
            "1": "00000409\00000409",
            "2": "00000419\00000419",
        },
        r"SYSTEM\CurrentControlSet\Control\Keyboard Layouts\00000419": {
            "Layout Text": "Russian",
        },
    }
    mock, _ = create_mock_winreg(key_values=key_values)
    return mock


@pytest.fixture
def mock_subprocess_run():
    """Фикстура: успешный мок subprocess.run (PowerShell)."""
    return create_mock_subprocess_run(
        returncode=0,
        stdout="en-GB\t0809:00000809\nru-RU\t0419:00000419\n",
    )


@pytest.fixture
def mock_is_admin_true():
    """Фикстура: мок IsUserAnAdmin, возвращающий True."""
    return mock_is_admin(True)


@pytest.fixture
def mock_is_admin_false():
    """Фикстура: мок IsUserAnAdmin, возвращающий False."""
    return mock_is_admin(False)


def setup_winreg_mocks(mock_winreg, preload=None, substitutes=None):
    """Быстрая настройка моков winreg для тестов сканера."""
    if preload:
        mock_winreg.OpenKey.return_value.__enter__.return_value.EnumValue.side_effect = [
            (name, val, 1) for name, val in preload.items()
        ] + [OSError]

    if substitutes:
        mock_winreg.OpenKey.return_value.__enter__.return_value.EnumValue.side_effect = [
            (name, val, 1) for name, val in substitutes.items()
        ] + [OSError]