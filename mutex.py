"""
mutex.py — Механизм синхронизации для предотвращения параллельных запусков.

Использует Windows-named mutex для обеспечения того, что только один
экземпляр приложения может работать одновременно.

Используется в main.py для:
- Захвата мьютекса при старте (_acquire_mutex)
- Освобождения мьютекса при выходе (_release_mutex)
- Перезапуска с сохранением мьютекса (elevate)
"""

import ctypes
import logging
import os
import time
from collections.abc import Callable

from config import TIMEOUTS

logger = logging.getLogger("layout_cleaner")

# Константы Windows API
ERROR_ALREADY_EXISTS = 183
ERROR_CANCELLED = 1223  # ожидание прервано пользователем (совпадает с WinAPI)
POLL_INTERVAL_SEC = 0.3  # 300 мс между попытками
# Предел ожидания освобождения мьютекса при elevate — из config.TIMEOUTS
# (единая точка настройки таймаутов). Ожидание ВСЕГДА ограничено: если
# старый процесс завис, вызывающий код получает ERROR_ALREADY_EXISTS и
# показывает пользователю диагностику вместо бесконечного зависания.
MAX_POLL_SECONDS = TIMEOUTS["mutex_wait"]  # 60 сек
MAX_POLL_ATTEMPTS = max(1, int(MAX_POLL_SECONDS / POLL_INTERVAL_SEC))
# Через сколько секунд ожидания спрашивать пользователя (короткие задержки
# при elevate разбираются молча — диалог не мешает перезапуску).
WAIT_GRACE_SECONDS = 5

# MessageBoxW: кнопки/иконка/возврат
_MB_OKCANCEL = 0x00000001
_MB_ICONQUESTION = 0x00000020
_MB_TOPMOST = 0x00040000
_IDCANCEL = 2


def confirm_wait_dialog(seconds: int) -> bool:
    """
    Спросить пользователя, ждать ли освобождения мьютекса.

    Пока ГП нет окна (мьютекс захватывается до создания GUI), поэтому
    используется WinAPI MessageBoxW — он не создаёт tkinter-root и не
    конфликтует с последующим CTk.

    Returns
    -------
    bool
        True — продолжать ожидание, False — пользователь отменил.
    """
    try:
        result = ctypes.windll.user32.MessageBoxW(
            None,
            "Предыдущий экземпляр приложения ещё завершается.\n"
            f"Ожидать освобождения (до {seconds} сек)?\n\n"
            "«Отмена» — не запускать приложение сейчас.",
            "Keyboard Layout Cleaner",
            _MB_OKCANCEL | _MB_ICONQUESTION | _MB_TOPMOST,
        )
    except OSError:
        # Нет интерактивной сессии (служба, CI) — ждём молча.
        logger.info("Диалог ожидания мьютекса недоступен — продолжаем ожидание")
        return True
    return result != _IDCANCEL


def acquire_mutex(
    mutex_name: str,
    is_elevate: bool = False,
    confirm_wait: Callable[[int], bool] | None = None,
) -> tuple[int | None, int]:
    """
    Создать mutex, разрешая гонку при перезапуске (elevate).

    Args:
        mutex_name: Имя мьютекса в системе.
        is_elevate: Если True, включается режим ожидания (до 60 сек),
                    чтобы дать старому процессу завершиться при UAC-перезапуске.
                    Если False, при занятом мьютексе функция завершается
                    МГНОВЕННО (обычный случай — пользователь случайно
                    кликнул по второй копии).
        confirm_wait: Необязательный колбэк ``confirm_wait(remaining_seconds)``.
                    Вызывается ОДИН раз — если мьютекс занят дольше
                    ``WAIT_GRACE_SECONDS``. Возврат False прерывает ожидание
                    (результат ``(None, ERROR_CANCELLED)``), чтобы пользователь
                    не ждал молча зависший процесс. По умолчанию None —
                    ожидание без вопросов (библиотечный режим, тесты).

    Returns:
        (handle, err):
          handle != None — успешно, новый процесс стал владельцем.
          handle is None — ERROR_ALREADY_EXISTS (мьютекс так и занят) либо
                           ERROR_CANCELLED (ожидание отменено пользователем).
    """
    pid = os.getpid()
    logger.info("[PID=%d] Попытка захвата мьютекса (is_elevate=%s)", pid, is_elevate)

    # Первичная попытка
    h = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
    err = ctypes.windll.kernel32.GetLastError()
    logger.info("[PID=%d] CreateMutexW: handle=%s, error=%d", pid, h, err)

    if err == ERROR_ALREADY_EXISTS:
        # Мьютекс уже существует — есть старый процесс.
        # Закрываем свой handle на существующий мьютекс,
        # иначе мьютекс никогда не освободится!
        ctypes.windll.kernel32.CloseHandle(h)

        # Если это НЕ перезапуск от админа — не ждём 60 секунд,
        # выходим сразу. Пользователь случайно кликнул по второй копии.
        if not is_elevate:
            logger.info(
                "[PID=%d] Мьютекс занят. Обычный запуск — мгновенный отказ.", pid
            )
            return None, ERROR_ALREADY_EXISTS

        # Только при elevate ждём, пока старый процесс полностью
        # освободит мьютекс (до 60 сек).
        logger.info("[PID=%d] Мьютекс занят при elevate, ждём освобождения...", pid)

        ask_after = max(1, int(WAIT_GRACE_SECONDS / POLL_INTERVAL_SEC))
        asked = False
        h = None
        for attempt in range(MAX_POLL_ATTEMPTS):
            time.sleep(POLL_INTERVAL_SEC)

            # Долгое ожидание — спрашиваем пользователя один раз, ждать ли.
            if not asked and confirm_wait is not None and attempt + 1 >= ask_after:
                asked = True
                elapsed = (attempt + 1) * POLL_INTERVAL_SEC
                remaining = max(1, int(MAX_POLL_SECONDS - elapsed))
                if not confirm_wait(remaining):
                    logger.info(
                        "[PID=%d] Ожидание мьютекса отменено пользователем", pid
                    )
                    return None, ERROR_CANCELLED

            # Второй параметр True — номинален здесь, так как мьютекс уже
            # существует (создан первым процессом). Нам важно только
            # GetLastError(), чтобы узнать, всё ещё ли он удерживается.
            h = ctypes.windll.kernel32.CreateMutexW(None, True, mutex_name)
            err = ctypes.windll.kernel32.GetLastError()
            if err != ERROR_ALREADY_EXISTS:
                # Успех! Старый процесс освободил мьютекс,
                # мы стали владельцами
                logger.info(
                    "[PID=%d] Мьютекс захвачен после %d попыток (%.1f сек)",
                    pid,
                    attempt + 1,
                    (attempt + 1) * POLL_INTERVAL_SEC,
                )
                break
            # Мьютекс всё ещё существует — закрываем handle и ждём
            ctypes.windll.kernel32.CloseHandle(h)
            h = None
            if attempt % 20 == 0 and attempt > 0:
                logger.info(
                    "[PID=%d] Ожидание мьютекса... %d сек (попытка %d/%d)",
                    pid,
                    int(attempt * POLL_INTERVAL_SEC),
                    attempt,
                    MAX_POLL_ATTEMPTS,
                )

        if h is None:
            logger.warning(
                "[PID=%d] Не удалось захватить мьютекс после %d попыток (%.0f сек)",
                pid,
                MAX_POLL_ATTEMPTS,
                MAX_POLL_SECONDS,
            )
            return None, err

    if err == 0:
        logger.info("[PID=%d] Мьютекс успешно захвачен", pid)
    else:
        logger.warning("[PID=%d] Ошибка создания мьютекса: %d", pid, err)

    return h, err


def release_mutex(handle: int | None) -> int:
    """
    Закрыть дескриптор мьютекса.

    Args:
        handle: дескриптор мьютекса, полученный из acquire_mutex().

    Returns:
        Код ошибки CloseHandle (0 = success).
    """
    pid = os.getpid()
    if handle and handle != 0:
        logger.info("[PID=%d] Закрытие мьютекса (handle=%d)", pid, handle)
        result = ctypes.windll.kernel32.CloseHandle(handle)
        return 0 if result else ctypes.windll.kernel32.GetLastError()
    return 0
