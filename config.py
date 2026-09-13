"""
config.py — Центральные константы Keyboard Layout Cleaner.

Содержит:
- _SANDBOX_ROOT: корневой путь для изоляции реестра в sandbox-режиме
- SANDBOX_MODE: глобальный флаг sandbox (синхронизируется main.py)
"""

import logging
import os

logger = logging.getLogger("layout_cleaner")
if not logger.handlers:
    try:
        logger.addHandler(logging.NullHandler())
    except RecursionError:
        # Защита от рекурсии при мокировании в тестах
        pass

# ---------------------------------------------------------------------------
# Sandbox-константы
# ---------------------------------------------------------------------------
_SANDBOX_ROOT = "Software\\KeyboardCleanerTest"

# Режим песочницы — синхронизируется из main.py при старте
SANDBOX_MODE: bool = False


def is_sandbox_enabled() -> bool:
    """Проверить, активен ли sandbox-режим (через env или глобальную переменную)."""
    env_val = os.environ.get("SANDBOX_MODE", "0").strip().lower()
    return SANDBOX_MODE or env_val in ("1", "true", "yes", "y")


def enable_sandbox() -> None:
    """Активировать sandbox-режим глобально."""
    global SANDBOX_MODE
    SANDBOX_MODE = True
    os.environ["SANDBOX_MODE"] = "1"
    logger.info("Sandbox-режим активирован")


def disable_sandbox() -> None:
    """Деактивировать sandbox-режим глобально."""
    global SANDBOX_MODE
    SANDBOX_MODE = False
    os.environ.pop("SANDBOX_MODE", None)
    logger.info("Sandbox-режим деактивирован")
