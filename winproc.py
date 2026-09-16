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
from pathlib import Path

# CREATE_NO_WINDOW — флаг создания процесса из WinAPI
CREATE_NO_WINDOW = 0x08000000

# Интерпретаторы PowerShell: их вывод декодируем как UTF-8 (наши .ps1-скрипты
# принудительно ставят [Console]::OutputEncoding = UTF8). Иначе кириллица в
# stderr/логе превращается в мусор: система декодирует OEM-байты CP866
# кодовой страницей cp1251 (проверено на живой системе).
_PS_EXECUTABLES = frozenset({"powershell", "powershell.exe", "pwsh", "pwsh.exe"})


def run_hidden(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run без консольного окна дочернего процесса (Windows).

    Семантика аргументов совпадает с subprocess.run; уже переданный
    creationflags объединяется (OR) с CREATE_NO_WINDOW. На не-Windows
    платформах флаг не передаётся (значение в WinAPI не определено).

    Декодирование текстового вывода (``text=True``):

    * для PowerShell-интерпретаторов подставляется ``encoding="utf-8"``
      (наши скрипты выставляют UTF-8 console output — иначе кириллица в
      stderr декодируется системной кодовой страницей и портится);
    * всегда добавляется ``errors="replace"`` — сбой декодирования одного
      байта не должен превращаться в UnicodeDecodeError и терять
      диагностику дочернего процесса.

    Явно переданные вызывающим кодом ``encoding``/``errors`` имеют приоритет.
    """
    if os.name == "nt":
        flags = kwargs.pop("creationflags", 0) | CREATE_NO_WINDOW
        kwargs["creationflags"] = flags

    # Текстовый режим запрошен явно (text=True) или неявно (encoding/errors).
    # При text=False добавлять encoding нельзя — subprocess.run поднимет
    # ValueError.
    if kwargs.get("text") or kwargs.get("encoding"):
        kwargs.setdefault("errors", "replace")
        executable = Path(cmd[0]).name.lower() if cmd else ""
        if executable in _PS_EXECUTABLES:
            kwargs.setdefault("encoding", "utf-8")

    return subprocess.run(cmd, **kwargs)
