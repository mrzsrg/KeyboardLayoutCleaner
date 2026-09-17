import ctypes
import time

import pytest


def _create_mutex(name, initial_owner=False):
    """Создать мьютекс и вернуть (handle, error)."""
    h = ctypes.windll.kernel32.CreateMutexW(None, initial_owner, name)
    err = ctypes.windll.kernel32.GetLastError()
    return h, err


def _close_mutex(handle):
    """Закрыть дескриптор мьютекса."""
    if handle != 0:
        ctypes.windll.kernel32.CloseHandle(handle)


class TestMutexRestartScenario:
    """Тест сценария перезапуска приложения от имени администратора."""

    def test_mutex_restart_scenario(self):
        """
        Симуляция перезапуска:
        1. Старый процесс создаёт мьютекс
        2. Новый процесс получает ERROR_ALREADY_EXISTS
        3. Старый процесс закрывает мьютекс
        4. Новый процесс закрывает свой дескриптор существующего мьютекса
        5. Новый процесс успешно создаёт новый мьютекс
        """
        mutex_name = r"Local\TestMutexRestart_{A3F8B2C1-7D4E-4A9B-8C6F-1E2D3F4A5B6C}"

        # Шаг 1: Старый процесс создаёт мьютекс
        h_old, err_old = _create_mutex(mutex_name)
        assert err_old == 0, "Старый процесс должен был создать мьютекс"
        assert h_old != 0, "Handle должен быть ненулевым"

        # Шаг 2: Новый процесс пытается создать мьютекс
        h_new, err_new = _create_mutex(mutex_name)
        assert err_new == 183, "Новый процесс должен получить ERROR_ALREADY_EXISTS"
        assert h_new != 0, "Handle всё равно должен быть получен"

        # Шаг 3: Старый процесс закрывает свой дескриптор
        _close_mutex(h_old)

        # Шаг 4: Новый процесс закрывает свой дескриптор существующего мьютекса
        # БЕЗ ЭТОГО ШАГА МЬЮТЕКС НЕ ОСВОБОДИТСЯ!
        _close_mutex(h_new)

        # Даём ядру Windows время на обработку
        time.sleep(0.5)

        # Шаг 5: Новый процесс теперь может создать новый мьютекс
        h_final, err_final = _create_mutex(mutex_name, initial_owner=True)
        assert err_final == 0, "Новый процесс должен был создать мьютекс"
        assert h_final != 0, "Handle должен быть ненулевым"

        # Очистка
        _close_mutex(h_final)

    def test_mutex_without_close_fails(self):
        """
        Демонстрация проблемы: если новый процесс не закрывает handle,
        мьютекс не освобождается.
        """
        mutex_name = r"Local\TestMutexNoClose_{A3F8B2C1-7D4E-4A9B-8C6F-1E2D3F4A5B6C}"

        # Старый процесс создаёт мьютекс
        h_old, _ = _create_mutex(mutex_name)

        # Новый процесс получает ERROR_ALREADY_EXISTS
        h_new, err_new = _create_mutex(mutex_name)
        assert err_new == 183

        # Старый процесс закрывает свой дескриптор
        _close_mutex(h_old)

        # Новый процесс НЕ закрывает свой дескриптор (ошибка!)
        # Пробуем создать мьютекс снова
        time.sleep(0.5)
        h_try, err_try = _create_mutex(mutex_name)

        # Мьютекс всё ещё существует, потому что h_new всё ещё открыт
        assert err_try == 183, "Мьютекс должен был остаться занятым"

        # Очистка
        _close_mutex(h_new)
        _close_mutex(h_try)

    def test_mutex_acquire_with_polling(self):
        """
        Тест правильного алгоритма ожидания освобождения мьютекса.
        Использует тот же подход, что должен быть в main.py.
        """
        mutex_name = r"Local\TestMutexPolling_{A3F8B2C1-7D4E-4A9B-8C6F-1E2D3F4A5B6C}"

        # Старый процесс создаёт мьютекс
        h_old, _ = _create_mutex(mutex_name)

        # Новый процесс получает ERROR_ALREADY_EXISTS
        h_new, err = _create_mutex(mutex_name)
        assert err == 183

        # Старый процесс закрывает свой дескриптор
        _close_mutex(h_old)

        # Новый процесс ждёт и пытается создать мьютекс
        acquired = False
        for _ in range(20):
            time.sleep(0.15)
            # Закрываем старый handle перед каждой попыткой
            _close_mutex(h_new)
            h_new, err = _create_mutex(mutex_name, initial_owner=True)
            if err == 0:
                acquired = True
                break

        assert acquired, "Новый процесс должен был захватить мьютекс"
        _close_mutex(h_new)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
