"""
applog.py — настройка журналирования Keyboard Layout Cleaner.

Лог пишется в файл layout_cleaner.log рядом с модулем и дублируется
в stderr. Модули scanner и cleaner используют общий логгер через
logging.getLogger("layout_cleaner"); до вызова setup_logging() у него
есть NullHandler (библиотечное поведение — без побочных эффектов).
"""

import logging
import sys
from pathlib import Path

LOG_FILE_NAME = "layout_cleaner.log"
LOGGER_NAME = "layout_cleaner"


def get_app_dir() -> Path:
    """
    Папка приложения для портативного режима.

    В собранном .exe (PyInstaller) — папка, где лежит исполняемый файл
    (например, флешка); в dev-режиме — папка с исходниками. Журнал,
    бэкапы и прочие файлы приложения создаются именно здесь, чтобы
    ничего не зависело от текущего каталога и временных папок.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_logger() -> logging.Logger:
    """Вернуть общий логгер приложения."""
    return logging.getLogger(LOGGER_NAME)


def setup_logging(log_dir: str | Path | None = None) -> Path:
    """
    Настроить логгер приложения (идемпотентно: повторный вызов
    пересоздаёт хендлеры).

    Returns
    -------
    Path
        Путь к файлу журнала. Если файл недоступен для записи,
        журналирование продолжается только в stderr.
    """
    log = logging.getLogger(LOGGER_NAME)
    log.setLevel(logging.INFO)
    for handler in list(log.handlers):
        log.removeHandler(handler)
        try:
            handler.close()
        except Exception as exc:
            # РЕГРЕССИЯ P0-3: раньше здесь было logger.debug — несуществующее
            # имя (локальная переменная называется log) → NameError вместо
            # деградации в stderr-only при сбое handler.close().
            log.debug("Не удалось закрыть хендлер лога: %s", exc)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    # В windowed-сборке (PyInstaller --noconsole) stderr отсутствует —
    # остаётся только файловый журнал.
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        log.addHandler(console)

    base = Path(log_dir) if log_dir else get_app_dir()
    log_file = base / LOG_FILE_NAME
    try:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)
    except OSError as exc:
        print(f"Не удалось открыть журнал {log_file}: {exc}", file=sys.stderr)

    return log_file