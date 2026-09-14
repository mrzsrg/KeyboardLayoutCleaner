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

logger = logging.getLogger("layout_cleaner")

# Константы Windows API
ERROR_ALREADY_EXISTS = 183
MAX_POLL_ATTEMPTS = 200       # 200 попыток
POLL_INTERVAL_SEC = 0.3       # 300 мс между попытками
MAX_POLL_SECONDS = MAX_POLL_ATTEMPTS * POLL_INTERVAL_SEC  # 60 сек


def acquire_mutex(mutex_name: str, is_elevate: bool = False) -> tuple[int | None, int]:
    """
    Создать mutex, разрешая гонку при перезапуске (elevate).

    Args:
        mutex_name: Имя мьютекса в системе.
        is_elevate: Если True, включается режим ожидания (до 60 сек),
                    чтобы дать старому процессу завершиться при UAC-перезапуске.
                    Если False, при занятом мьютексе функция завершается
                    МГНОВЕННО (обычный случай — пользователь случайно
                    кликнул по второй копии).

    Returns:
        (handle, err):
          handle != None — успешно, новый процесс стал владельцем.
          handle is None — ERROR_ALREADY_EXISTS даже после ожидания;
                           старый процесс всё ещё держит mutex.
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
        logger.info(
            "[PID=%d] Мьютекс занят при elevate, ждём освобождения...", pid
        )

        h = None
        for attempt in range(MAX_POLL_ATTEMPTS):
            time.sleep(POLL_INTERVAL_SEC)
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
                    "[PID=%d] Ожидание мьютекса... %d сек "
                    "(попытка %d/%d)",
                    pid,
                    int(attempt * POLL_INTERVAL_SEC),
                    attempt,
                    MAX_POLL_ATTEMPTS,
                )

        if h is None:
            logger.warning(
                "[PID=%d] Не удалось захватить мьютекс после %d попыток "
                "(%.0f сек)",
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
