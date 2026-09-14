"""
winproc.py — Запуск дочерних процессов без «мигающих» консольных окон.

Приложение собирается как windowed (PyInstaller, console=False): каждый
вызов subprocess.run с консольной утилитой (powershell.exe, reg.exe)
открывает видимое чёрное окно консоли поверх GUI. Флаг CREATE_NO_WINDOW
(0x08000000) подавляет окно, не меняя семантику stdout/stderr/returncode.
В консольном запуске (терминал, CI, тесты) флаг безвреден.

Все subprocess.run приложения ДОЛЖНЫ идти через run_hidden() — это
проверяется регрессионным тестом
test_scanner.py::TestWinprocHelper::test_scanner_and_cleaner_go_through_winproc.

Важно для моков в тестах: run_hidden вызывает subprocess.run по атрибуту
модуля (не «from subprocess import run»), поэтому
monkeypatch.setattr(scanner.subprocess, "run", ...) продолжает перехват.
"""

import os
import subprocess

# CREATE_NO_WINDOW — флаг создания процесса из WinAPI
CREATE_NO_WINDOW = 0x08000000


def run_hidden(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run без консольного окна дочернего процесса (Windows).

    Семантика аргументов совпадает с subprocess.run; уже переданный
    creationflags объединяется (OR) с CREATE_NO_WINDOW. На не-Windows
    платформах флаг не передаётся (значение в WinAPI не определено).
    """
    if os.name == "nt":
        flags = kwargs.pop("creationflags", 0) | CREATE_NO_WINDOW
        kwargs["creationflags"] = flags
    return subprocess.run(cmd, **kwargs)
