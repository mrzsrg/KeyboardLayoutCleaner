import contextlib
import os
import subprocess
import tempfile
import time

import pytest

_MUTEX_NAME = r"Global\TestKLC_FullRestart_{B4G9C3D2-8E5F-4B9C-9D7G-2F3E4G5H6I7J}"


def _create_script(name, content):
    """Создать временный Python скрипт."""
    path = os.path.join(tempfile.gettempdir(), name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


class TestMutexFullRestart:
    """Тест полного сценария перезапуска с двух процессов."""

    def test_two_process_restart_scenario(self):
        """
        Имитирует реальный сценарий:
        1. Старый процесс создаёт мьютекс
        2. Старый процесс "запускает" новый процесс (имитация ShellExecuteW)
        3. Старый процесс закрывает мьютекс и завершается
        4. Новый процесс успешно захватывает мьютекс
        """
        old_script = f'''
import ctypes
import time
import sys

MUTEX_NAME = r"{_MUTEX_NAME}"

# Создаём мьютекс (как при старте приложения)
h = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
err = ctypes.windll.kernel32.GetLastError()
print(f"OLD: mutex created, handle={{h}}, error={{err}}", flush=True)

if err != 0:
    print("OLD: failed to create mutex", flush=True)
    sys.exit(1)

# Имитация: ждём немного (GUI работает)
time.sleep(1.5)

# Имитация перезапуска: закрываем мьютекс и выходим
print("OLD: closing mutex and exiting", flush=True)
ctypes.windll.kernel32.CloseHandle(h)
time.sleep(1.0)  # Даём время ядру на обработку
print("OLD: exited", flush=True)
'''

        new_script = f'''
import ctypes
import time
import sys

MUTEX_NAME = r"{_MUTEX_NAME}"

# Новый процесс пытается создать мьютекс
print("NEW: trying to create mutex", flush=True)
h = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
err = ctypes.windll.kernel32.GetLastError()
print(f"NEW: handle={{h}}, error={{err}}", flush=True)

if err == 183:
    print("NEW: ERROR_ALREADY_EXISTS, closing handle", flush=True)
    ctypes.windll.kernel32.CloseHandle(h)

    # Поллинг с таймаутом 10 секунд
    acquired = False
    for i in range(50):
        time.sleep(0.2)
        h2 = ctypes.windll.kernel32.CreateMutexW(None, True, MUTEX_NAME)
        err2 = ctypes.windll.kernel32.GetLastError()
        if err2 != 183:
            print(f"NEW: acquired mutex on attempt {{i+1}}", flush=True)
            h = h2
            err = err2
            acquired = True
            break
        ctypes.windll.kernel32.CloseHandle(h2)

    if not acquired:
        print("NEW: TIMEOUT - could not acquire mutex", flush=True)
        sys.exit(1)

if err == 0:
    print("NEW: SUCCESS - continuing work", flush=True)
    # Очистка
    ctypes.windll.kernel32.CloseHandle(h)
else:
    print(f"NEW: FAILED with error {{err}}", flush=True)
    sys.exit(1)
'''

        old_path = _create_script("klc_test_old.py", old_script)
        new_path = _create_script("klc_test_new.py", new_script)

        try:
            # Запускаем старый процесс
            old_proc = subprocess.Popen(
                ["python", old_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            # Даём старому процессу время создать мьютекс
            time.sleep(0.5)

            # Запускаем новый процесс
            new_proc = subprocess.Popen(
                ["python", new_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            # Ждём завершения обоих процессов
            old_stdout, _ = old_proc.communicate(timeout=15)
            new_stdout, _ = new_proc.communicate(timeout=15)

            # Проверяем результаты
            assert old_proc.returncode == 0, f"Old process failed: {old_stdout}"
            assert new_proc.returncode == 0, f"New process failed: {new_stdout}"
            assert "NEW: SUCCESS" in new_stdout, (
                "New process should have acquired mutex"
            )

        finally:
            # Очистка файлов
            for path in [old_path, new_path]:
                with contextlib.suppress(OSError):
                    os.remove(path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
