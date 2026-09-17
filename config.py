"""
config.py — Центральные константы Keyboard Layout Cleaner.

Содержит:
- _SANDBOX_ROOT: корневой путь для изоляции реестра в sandbox-режиме
- SANDBOX_MODE: глобальный флаг sandbox (синхронизируется main.py)
"""

import logging
import os
import threading

import applog

logger = logging.getLogger("layout_cleaner")
applog.setup_null_handler(logger)

# Версия приложения — читаем из importlib.metadata (синхронизирована с [project] version в pyproject.toml).
try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _get_version

    __version__ = _get_version("keyboard-layout-cleaner")
except (ImportError, PackageNotFoundError):
    # Fallback для PyInstaller-сборки (метаданных пакета в onedir нет);
    # держать синхронно с [project] version в pyproject.toml
    # (инвариант: test_version_matches_pyproject).
    __version__ = "1.0.4"

# ---------------------------------------------------------------------------
# Таймауты внешних процессов (секунды)
#
# Единая точка настройки: значения используются в cleaner.py, scanner.py и
# mutex.py через указанные ключи, ЛИТЕРАЛЫ timeout=<число> в этих модулях
# запрещены (регрессионный тест test_no_hardcoded_timeouts).
# ---------------------------------------------------------------------------
TIMEOUTS: dict[str, int] = {
    "reg_export": 15,  # reg export одной ветки реестра
    "reg_import": 60,  # reg import без UAC
    "reg_import_uac": 180,  # reg import с UAC (пользователь читает запрос)
    "powershell_list": 20,  # layout_cleaner_list.ps1 (скан языков)
    "powershell_backup": 45,  # layout_cleaner_backup.ps1 (снимок языков)
    "powershell_cleanup": 45,  # cleanup/sync/restore списка языков
    "powershell_welcome_sync": 45,  # Copy-UserInternationalSettingsToSystem
    "ctfmon_control": 20,  # остановка/запуск ctfmon
    "mutex_wait": 60,  # ожидание освобождения мьютекса при elevate
}

# ---------------------------------------------------------------------------
# Sandbox-константы
# ---------------------------------------------------------------------------
_SANDBOX_ROOT = "Software\\KeyboardCleanerTest"

# Режим песочницы — синхронизируется из main.py при старте
_SANDBOX_MODE: bool = False
_sandbox_lock = threading.Lock()


class _SandboxModeProxy:
    """Прокси-объект для обратной совместимости с кодом, который читает config.SANDBOX_MODE."""

    def __bool__(self) -> bool:
        with _sandbox_lock:
            return bool(_SANDBOX_MODE)

    def __repr__(self) -> str:
        # Не ссылаемся на self внутри f-string: str(self) -> __repr__ ->
        # бесконечная рекурсия. Показываем актуальное состояние флага.
        return f"<SandboxModeProxy active={is_sandbox_enabled()}>"


# Публичная переменная для обратной совместимости (только для чтения — используйте
# enable_sandbox() / disable_sandbox() для записи)
SANDBOX_MODE: object = _SandboxModeProxy()


def is_sandbox_enabled() -> bool:
    """Проверить, активен ли sandbox-режим (через env или глобальную переменную).

    Поток-безопасная версия: читает флаг через Lock.
    """
    env_val = os.environ.get("SANDBOX_MODE", "0").strip().lower()
    with _sandbox_lock:
        mode = _SANDBOX_MODE
    return mode or env_val in ("1", "true", "yes", "y")


def enable_sandbox() -> None:
    """Активировать sandbox-режим глобально.

    Поток-безопасная версия: устанавливает флаг через Lock.
    """
    global _SANDBOX_MODE
    with _sandbox_lock:
        _SANDBOX_MODE = True
    os.environ["SANDBOX_MODE"] = "1"
    logger.info("Sandbox-режим активирован")


def disable_sandbox() -> None:
    """Деактивировать sandbox-режим глобально.

    Поток-безопасная версия: сбрасывает флаг через Lock.
    """
    global _SANDBOX_MODE
    with _sandbox_lock:
        _SANDBOX_MODE = False
    os.environ.pop("SANDBOX_MODE", None)
    logger.info("Sandbox-режим деактивирован")
