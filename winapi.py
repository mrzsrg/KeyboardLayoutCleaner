"""
winapi.py — Единая точка объявления прототипов WinAPI (FIX-19).

Зачем модуль. До него каждая DLL открывалась через ``ctypes.windll``, где
прототипы по умолчанию — C ``int``: для ``HANDLE`` (указатель на x64) это
32-битное усечение, формально не определённое поведение. Плюс код ошибки
читался отдельным вызовом ``GetLastError()`` — а последнюю ошибку Windows
сбрасывает ЛЮБОЕ последующее обращение к ctypes, поэтому между вызовом и
чтением ошибки нельзя было вставить ничего.

Здесь оба дефекта устранены конструктивно:
  * библиотеки загружаются с ``use_last_error=True``, поэтому код ошибки
    берётся через ``ctypes.get_last_error()`` сразу после вызова и всегда
    соответствует именно ему;
  * ``argtypes``/``restype`` заданы для всех используемых функций.

Прототипы функций, работающих со структурами собственной сборки
(``RtlGetVersion``) и уже имеющих корректные прототипы на месте
(advapi32 в main.py, см. FIX-14), остаются в вызывающем модуле: перенос
структуры сюда разорвал бы её с местом создания и не дал бы выигрыша.

Обёртки (create_mutex, close_handle, …) — единственная точка входа для
остальных модулей и одновременно «шов» для тестов: подменять нужно
функцию этого модуля, а не ``ctypes.windll`` (см. комментарий в
``create_mutex``).
"""

import ctypes
from ctypes import wintypes

# ---------------------------------------------------------------------------
# Загрузка библиотек
# ---------------------------------------------------------------------------
# use_last_error=True — обязателен: без него ctypes.get_last_error() не
# имеет отношения к последней ошибке Windows.
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

# ---------------------------------------------------------------------------
# Прототипы
# ---------------------------------------------------------------------------

# --- kernel32 ---
# HANDLE — указатель; без restype=ctypes.c_void_p значение обрезалось бы
# до 32 бит на x64.
kernel32.CreateMutexW.argtypes = [
    ctypes.c_void_p,  # LPSECURITY_ATTRIBUTES
    wintypes.BOOL,  # bInitialOwner
    wintypes.LPCWSTR,  # lpName
]
kernel32.CreateMutexW.restype = wintypes.HANDLE

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.GetCurrentProcess.argtypes = []
kernel32.GetCurrentProcess.restype = wintypes.HANDLE

kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
kernel32.LocalFree.restype = wintypes.HLOCAL

kernel32.GetUserDefaultUILanguage.argtypes = []
kernel32.GetUserDefaultUILanguage.restype = wintypes.LANGID

# --- user32 ---
user32.MessageBoxW.argtypes = [
    wintypes.HWND,  # hWnd
    wintypes.LPCWSTR,  # lpText
    wintypes.LPCWSTR,  # lpCaption
    wintypes.UINT,  # uType
]
user32.MessageBoxW.restype = ctypes.c_int  # IDOK/IDCANCEL/…

# --- shell32 ---
shell32.ShellExecuteW.argtypes = [
    wintypes.HWND,  # hwnd
    wintypes.LPCWSTR,  # lpOperation
    wintypes.LPCWSTR,  # lpFile
    wintypes.LPCWSTR,  # lpParameters
    wintypes.LPCWSTR,  # lpDirectory
    ctypes.c_int,  # nShowCmd
]
shell32.ShellExecuteW.restype = wintypes.HINSTANCE

shell32.IsUserAnAdmin.argtypes = []
shell32.IsUserAnAdmin.restype = wintypes.BOOL


# ---------------------------------------------------------------------------
# Обёртки: вызов + немедленное чтение кода ошибки
# ---------------------------------------------------------------------------


def create_mutex(
    name: str, initial_owner: bool = False
) -> tuple[int | None, int]:
    """``CreateMutexW`` с корректным чтением кода ошибки.

    Returns
    -------
    tuple[int | None, int]
        ``(handle, error_code)``. ``handle is None`` — мьютекс не создан.
        ``error_code`` — код ВЫЗОВА CreateMutexW, а не «случайный» код,
        оставшийся от предыдущей операции.

    Тестовый шов: подменять ``ctypes.windll`` здесь бессмысленно — этот
    модуль держит собственные дескрипторы DLL. Тесты подменяют
    ``winapi.create_mutex`` целиком, иначе подмена ничего не затрагивала бы
    и тест «проходил», ничего не проверяя.
    """
    handle = kernel32.CreateMutexW(None, bool(initial_owner), name)
    # Сразу после вызова: любое промежуточное обращение к ctypes сбросило
    # бы last error в 0, и «ошибка выглядит как успех».
    error = ctypes.get_last_error()
    return (handle or None), error


def close_handle(handle: int) -> int:
    """``CloseHandle``; вернуть код ошибки (0 — успех)."""
    ok = kernel32.CloseHandle(handle)
    if ok:
        return 0
    return ctypes.get_last_error()


def message_box(
    text: str, caption: str, style: int, parent: int | None = None
) -> int:
    """``MessageBoxW`` — возврат идентификатора нажатой кнопки."""
    return int(user32.MessageBoxW(parent, text, caption, style))


def shell_execute(
    operation: str,
    file: str,
    parameters: str = "",
    directory: str = "",
    show_cmd: int = 1,
) -> tuple[int, int]:
    """``ShellExecuteW``; вернуть ``(result, error_code)``.

    ``SE_ERR_*`` отрицательные значения — в Windows они не сопровождаются
    кодом Win32, поэтому error_code здесь всегда 0 и вызывающий код
    разбирает ``result`` сам.
    """
    result = shell32.ShellExecuteW(
        None, operation, file, parameters, directory, show_cmd
    )
    return int(result or 0), ctypes.get_last_error()


def is_user_an_admin() -> bool:
    """``IsUserAnAdmin`` с объявленным прототипом."""
    return bool(shell32.IsUserAnAdmin())


def get_user_default_ui_language() -> int:
    """``GetUserDefaultUILanguage`` (LCID интерфейса Windows)."""
    return int(kernel32.GetUserDefaultUILanguage())
