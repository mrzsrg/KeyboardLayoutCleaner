"""
applog.py — настройка журналирования Keyboard Layout Cleaner.

Лог пишется в файл layout_cleaner.log рядом с модулем и дублируется
в stderr. Модули scanner и cleaner используют общий логгер через
logging.getLogger("layout_cleaner"); до вызова setup_logging() у него
есть NullHandler (библиотечное поведение — без побочных эффектов).
"""

import logging
import sys
from contextlib import suppress
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE_NAME = "layout_cleaner.log"
LOGGER_NAME = "layout_cleaner"


def make_output_safe(*, force_utf8: bool = False) -> None:
    """Не дать печати упасть из-за кодировки консоли.

    Windows печатает в кодовую страницу системы. На русской установке это
    cp1251, и русский текст печатается как есть; на «западной» (cp1252)
    любая русская буква даёт UnicodeEncodeError, и команда падает ПОСЛЕ
    того, как всё сделала: ``cleaner.py 00000419 --plan`` доходит до
    вывода отчёта и умирает на нём. У разработчика с русской Windows баг
    не воспроизводится — у пользователя с англоязычной он воспроизводится
    всегда.

    По умолчанию кодировка НЕ меняется: на cp1251 поведение остаётся
    прежним (текст читается), а на cp1252 русские буквы превращаются в
    «?» — печать не падает, и это честнее, чем портить вывод там, где он
    был правильным. Лучше потерять символ в сообщении, чем команду.

    ``force_utf8=True`` — для скриптов, чей вывод уходит в конвейер или
    файл (журнал CI, перенаправление): там UTF-8 и читаем, и переносим.

    ``errors="replace"`` — страховка для любой экзотической кодировки.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # перенаправленный/подменённый поток
            continue
        with suppress(ValueError, OSError):
            if force_utf8:
                reconfigure(encoding="utf-8", errors="replace")
            else:
                reconfigure(errors="replace")


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


def resource_path(*parts: str) -> Path:
    """
    Путь к файлу данных, поставляемому вместе с программой (FIX-20).

    Раньше модули строили такие пути как ``Path(__file__).parent / "scripts"``.
    Это работает только пока файлы лежат рядом с модулями: любая
    реструктуризация (перенос в подпакет, FIX-10/11) или смена способа
    сборки ломала бы пути МОЛЧА — приложение падало бы с «файл не найден»
    уже во время операции, а не при старте.

    Здесь один источник истины:
      * собранный .exe — ``sys._MEIPASS`` (PyInstaller распаковывает
        data-файлы туда же, независимо от onedir/onefile);
      * dev-режим — каталог модуля.

    Returns
    -------
    Path
        Путь; существование НЕ проверяется — вызывающий код решает, что
        делать с отсутствующим файлом (обычно это ошибка сборки).
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    else:
        base = Path(__file__).resolve().parent
    return base.joinpath(*parts) if parts else base


def get_logger() -> logging.Logger:
    """Вернуть общий логгер приложения."""
    return logging.getLogger(LOGGER_NAME)


def setup_null_handler(logger: logging.Logger) -> None:
    """
    Добавить NullHandler логгеру, если у него ещё нет handlers.

    Обеспечивает библиотечное поведение — без побочных эффектов (не печатает
    в stderr) до вызова setup_logging(). Повторный вызов безопасен.

    Используется в модулях scanner.py, cleaner.py, config.py, i18n.py, ui_theme.py
    вместо inline-кода с NullHandler — единый источник для всех модулей.
    """
    if not logger.handlers:
        with suppress(RecursionError):
            logger.addHandler(logging.NullHandler())


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
        except (OSError, RuntimeError):
            # РЕГРЕССИЯ P0-3: раньше здесь было logger.debug — несуществующее
            # имя (локальная переменная называется log) → NameError вместо
            # деградации в stderr-only при сбое handler.close().
            log.debug("Не удалось закрыть хендлер лога")

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
        file_handler = RotatingFileHandler(
            log_file,
            encoding="utf-8",
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
        )
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)
    except OSError:
        pass

    return log_file
