"""
main.py — Графический интерфейс Keyboard Layout Cleaner (Этап 3).

GUI на CustomTkinter. Реализует воркфлоу из ARCHITECTURE.md без надписей
«Шаг N»: на каждом этапе интерфейс сам подсказывает дальнейшее действие —
строкой-подсказкой и пульсирующей подсветкой рекомендованного элемента:

  1. Сканирование системы (подсвечена кнопка «Сканировать систему»).
  2. Результат: кликабельный список найденных раскладок (подсвечен
     рамкой; выбор — кликом по строке с раскладкой).
  3. Детали: ВСЕ сканируемые разделы со статусом «обнаружено /
     не обнаружено» и значениями записей (подсвечена кнопка удаления).
  4. Баннер прав администратора: что будет очищено, а что пропущено;
     кнопка «Перезапустить как Администратор» видна только без прав.
  5. Удаление с подтверждением (dry-run), бэкапом .reg/.json и
     автоматическим пересканированием.
"""

import argparse
import ctypes
import logging
import os
import queue
import sys
import threading
import time
import re
from typing import Any
from tkinter import messagebox

# ---------------------------------------------------------------------------
# Логгер — создаём ДО мьютекса, т.к. _acquire_mutex использует logger
# ---------------------------------------------------------------------------
logger = logging.getLogger("layout_cleaner")

# ---------------------------------------------------------------------------
# Ранняя настройка логгера — ДО мьютекса, чтобы _acquireмог писать в лог
# ---------------------------------------------------------------------------
try:
    from applog import setup_logging as _early_setup_logging
    _early_setup_logging()
except Exception:
    pass  # Логгер не критичен для работы 

# ---------------------------------------------------------------------------
# Импорт из центрального модуля конфигурации
# ---------------------------------------------------------------------------
from config import enable_sandbox, disable_sandbox  # noqa: E402 — импорты ниже _parse_sandbox_mode намеренны (см. блок на стр. 221)

# ---------------------------------------------------------------------------
# Глобальный режим песочницы — переключается через --sandbox или env
# ---------------------------------------------------------------------------
# ВАЖНО: SANDBOX_MODE ДОЛЖНА быть вычислена ДО импорта scanner/cleaner,
# чтобы они подхватили правильное значение.


def _parse_sandbox_mode(argv: list[str] | None = None) -> tuple[bool, argparse.Namespace]:
    """Разобрать --sandbox флаг + переменную окружения.

    Returns
    -------
    tuple[bool, argparse.Namespace]
        (enabled, parsed_args) — режим песочницы и разобраные аргументы.
    """
    parser = argparse.ArgumentParser(
        prog="KeyboardLayoutCleaner",
        description="Утилита для очистки фантомных раскладок клавиатуры.",
    )
    parser.add_argument(
        "--sandbox",
        action="store_true",
        default=False,
        help="Запустить в режиме песочницы: все операции реестра изолированы "
             "в HKCU\\Software\\KeyboardCleanerTest. "
             "Также переключается переменной SANDBOX_MODE=1.",
    )
    args, _ = parser.parse_known_args(argv)

    env_val = os.environ.get("SANDBOX_MODE", "0").strip().lower()
    env_sandbox = env_val in ("1", "true", "yes", "y")
    sandbox_enabled = env_sandbox or args.sandbox

    if args.sandbox:
        os.environ["SANDBOX_MODE"] = "1"

    return sandbox_enabled, args


_SANDBOX_MODE, _parsed_args = _parse_sandbox_mode()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Разобрать командную строку."""
    if argv is None:
        return _parsed_args
    _, args = _parse_sandbox_mode(argv)
    return args


def _apply_sandbox_to_modules(sandbox_enabled: bool) -> None:
    """Синхронизировать SANDBOX_MODE в модулях scanner/cleaner."""
    import config as _cfg
    _cfg.SANDBOX_MODE = sandbox_enabled
    if sandbox_enabled:
        enable_sandbox()
    else:
        disable_sandbox()


# Раннее определение SANDBOX_MODE до импорта scanner/cleaner
SANDBOX_MODE: bool = _SANDBOX_MODE

if sys.platform != "win32":
    if sys.stderr:
        sys.stderr.write("Keyboard Layout Cleaner работает только под Windows.\n")
    else:
        messagebox.showerror("Ошибка", "Keyboard Layout Cleaner работает только под Windows.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Защита от параллельных запусков (named mutex)
# ---------------------------------------------------------------------------
_MUTEX_NAME = r"Global\KeyboardLayoutCleaner_{A3F8B2C1-7D4E-4A9B-8C6F-1E2D3F4A5B6C}"


def _acquire_mutex() -> tuple[int | None, int]:
    """
    Создать mutex, разрешая гонку при перезапуске (elevate).

    Возвращает (handle, err):
      handle != None — успешно, новый процесс стал владельцем.
      handle is None — ERROR_ALREADY_EXISTS даже после ожидания;
                       старый процесс всё ещё держит mutex.
    """
    pid = os.getpid()
    logger.info("[PID=%d] Попытка захвата мьютекса", pid)

    # Первичная попытка
    h = ctypes.windll.kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    err = ctypes.windll.kernel32.GetLastError()
    logger.info("[PID=%d] CreateMutexW: handle=%s, error=%d", pid, h, err)

    if err == 183:  # ERROR_ALREADY_EXISTS
        # Мьютекс уже существует — есть старый процесс.
        # Закрываем свой handle на существующий мьютекс,
        # иначе мьютекс никогда не освободится!
        logger.info("[PID=%d] Мьютекс занят (ERROR_ALREADY_EXISTS), закрываем handle %s и ждём...", pid, h)
        ctypes.windll.kernel32.CloseHandle(h)

        # Ждём, пока старый процесс полностью освободит мьютекс.
        # Даём до 60 секунд (200 попыток × 300 мс).
        h = None
        for attempt in range(200):
            time.sleep(0.3)
            h = ctypes.windll.kernel32.CreateMutexW(None, True, _MUTEX_NAME)
            err = ctypes.windll.kernel32.GetLastError()
            if err != 183:
                # Успех! Старый процесс освободил мьютекс, мы стали владельцами
                logger.info("[PID=%d] Мьютекс захвачен после %d попыток (%.1f сек)", pid, attempt + 1, (attempt + 1) * 0.3)
                break
            # Мьютекс всё ещё существует — закрываем handle и ждём
            ctypes.windll.kernel32.CloseHandle(h)
            h = None
            if attempt % 20 == 0 and attempt > 0:
                logger.info("[PID=%d] Ожидание мьютекса... %d сек (попытка %d/200)", pid, int(attempt * 0.3), attempt)
        else:
            logger.error("[PID=%d] Таймаут ожидания мьютекса (60 сек, 200 попыток)", pid)

    return h, err


# NOTE: Мьютекс НЕ захватывается на уровне модуля — это мешает тестам.
# Захват происходит в main() перед запуском GUI.

# ---------------------------------------------------------------------------
# Проверка версии Windows (требуется Windows 10 64-bit или новее)
# ---------------------------------------------------------------------------
def _check_windows_version():
    """Возвращает (True, version_info) если версия Windows >= 10 (build 10240), иначе (False, None)."""
    try:
        ntdll = ctypes.windll.ntdll
        # RtlGetVersion заполняет RTL_OSVERSIONINFOW
        class RTL_OSVERSIONINFOW(ctypes.Structure):
            _fields_ = [
                ("dwOSVersionInfoSize", ctypes.c_ulong),
                ("dwMajorVersion", ctypes.c_ulong),
                ("dwMinorVersion", ctypes.c_ulong),
                ("dwBuildNumber", ctypes.c_ulong),
                ("dwPlatformId", ctypes.c_ulong),
                ("szCSDVersion", ctypes.c_wchar * 128),
            ]
        ver = RTL_OSVERSIONINFOW()
        ver.dwOSVersionInfoSize = ctypes.sizeof(ver)
        ret = ntdll.RtlGetVersion(ctypes.byref(ver), ctypes.byref(ctypes.c_int()))
        if ret != 0:  # NTSTATUS success
            return False, None
        is_ok = ver.dwMajorVersion >= 10 and ver.dwBuildNumber >= 10240
        return is_ok, ver
    except Exception:
        return False, None

is_ok, ver = _check_windows_version()
if not is_ok:
    if sys.stderr:
        sys.stderr.write(
            "Ошибка: требуется Windows 10 (build 10240) или новее.\n"
        )
    else:
        if ver is not None:
            version_str = "Ваша версия: %d.%d (build %d)" % (
                ver.dwMajorVersion, ver.dwMinorVersion, ver.dwBuildNumber
            )
        else:
            version_str = "Ваша версия: неизвестна"
        messagebox.showerror(
            "Ошибка",
            "Требуется Windows 10 (build 10240) или новее.\n\n" + version_str,
        )
    sys.exit(1)

# noqa: E402 — эти импорты НАМЕРЕННО стоят не в начале файла: sandbox-флаг
# должен быть вычислен (_parse_sandbox_mode выше) до импорта scanner/cleaner.
import customtkinter as ctk  # noqa: E402

from applog import get_logger, get_app_dir  # noqa: E402
from cleaner import (  # noqa: E402
    delete_layout,
    disable_language_sync,
    get_backup_dir,
    is_admin,
    list_backups,
    plan_layout_removal,
    restore_backup,
)
from scanner import (  # noqa: E402
    get_affected_branches,
    get_layout_name,
    scan_keyboard_layouts,
)
from gui_widgets import ActionButtons, AdminBanner, StatusBar  # noqa: E402

# Синхронизируем SANDBOX_MODE в модулях scanner/cleaner
# (они импортируются выше — здесь уже доступны)
_apply_sandbox_to_modules(SANDBOX_MODE)

import i18n  # noqa: E402
import ui_theme as theme  # noqa: E402

# Язык интерфейса: определяется по системе, можно переключить в UI
i18n.set_language(i18n.detect_system_lang())


def _t(key: str, **kwargs) -> str:
    """Локализованная строка по ключу (format без исключений)."""
    return i18n.fmt(key, **kwargs)


# ---------------------------------------------------------------------------
# Глобальные настройки CustomTkinter
# ---------------------------------------------------------------------------
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

# ---------------------------------------------------------------------------
# Подсветка «рекомендованного действия» и общие константы UI
# ---------------------------------------------------------------------------
# Цвета подсветки берутся из активной темы (ui_theme.P): пары
# (базовый, акцент) для попеременного «пульсирования» элемента, с которым
# пользователю нужно взаимодействовать прямо сейчас.
PULSE_INTERVAL_MS = 600
# Период опроса очереди результатов фоновых потоков, мс
POLL_INTERVAL_MS = 100

# Путь к каталогу имён раскладок (только справка, никогда не удаляется)
HKLM_CATALOG_PATH = "HKLM\\SYSTEM\\CurrentControlSet\\Control\\Keyboard Layouts"
# Пояснение к каталогу для панели деталей
HKLM_CATALOG_NOTE = (
    "системный справочник названий — существует у всех, очистке не подлежит"
)
# Псевдо-раздел с данными Get-WinUserLanguageList
PS_SECTION_PATH = "PowerShell\\Get-WinUserLanguageList"


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _build_layout_name(klid: str) -> str:
    """Сформировать строку отображения раскладки: Имя (KLID)."""
    # Динамическое имя из HKLM ("Layout Text") + fallback на LAYOUT_MAP
    name = get_layout_name(klid)
    if name and not name.endswith(f"({klid})"):
        return f"{name} ({klid})"
    return f"Layout ({klid})"


# ---------------------------------------------------------------------------
# Главное приложение
# ---------------------------------------------------------------------------

def _active_lang_cache(klid: str, layouts_data: dict) -> bool:
    """
    Похоже ли, что записи раскладки — это КЕШ АКТИВНОЙ раскладки, а не фантом.

    Признаки: все записи найдены только в CTF (нет записей в Preload /
    Substitutes / User Profile / PowerShell-списке), при этом язык
    присутствует в списке языков Windows по данным последнего скана.
    Язык KLID: для канонической формы «0000XXXX» — младшее слово; для
    HKL-формы «XXXXYYYY» (например, 04190419 — язык 0419 + раскладка
    00000419) — старшее слово. Такой кеш Windows пересоздаёт для
    активного языка — удаление не изменит список языков.
    """

    def _lang_of(klid_str: str) -> int:
        k = int(str(klid_str), 16)
        hi, lo = (k >> 16) & 0xFFFF, k & 0xFFFF
        return hi if hi else lo

    try:
        langid = _lang_of(klid)
    except (ValueError, TypeError):
        return False
    if langid == 0:
        return False
    locs = layouts_data.get(klid, [])
    if not locs:
        return False
    if any("CTF" not in str(loc.get("path", "")).upper() for loc in locs):
        return False
    for other_klid, other_locs in layouts_data.items():
        if other_klid == klid:
            continue
        for loc in other_locs:
            if "powershell" in str(loc.get("path", "")).lower():
                try:
                    if _lang_of(other_klid) == langid:
                        return True
                except (ValueError, TypeError):
                    continue
    return False


class KeyboardLayoutCleaner(ctk.CTk):
    """Основной класс GUI приложения."""

    def __init__(self) -> None:
        super().__init__()

        self.title(_t("app_title"))
        self.geometry("900x780")
        self.minsize(820, 660)

        # Данные
        self.layouts_data: dict[str, list[dict[str, str]]] = {}
        self.current_layout_klid: str = ""
        self._scan_done: bool = False
        self._last_backup_info: tuple[str, str] = ("", "")

        # Состояние процесса перезапуска (защита от двойного нажатия)
        self._is_elevating: bool = False

        # Ссылки на виджеты (будут инициализированы после создания)
        self.admin_label: ctk.CTkLabel | None = None
        self.admin_hint_label: ctk.CTkLabel | None = None
        self.admin_icon_label: ctk.CTkLabel | None = None
        self.guide_label: ctk.CTkLabel | None = None
        self.list_title_label: ctk.CTkLabel | None = None
        self.layouts_list_frame: ctk.CTkScrollableFrame | None = None
        self._layout_row_buttons: dict[str, ctk.CTkButton] = {}
        self._list_placeholder: ctk.CTkLabel | None = None
        self.detail_text: ctk.CTkTextbox | None = None
        self.scan_button: ctk.CTkButton | None = None
        self.delete_button: ctk.CTkButton | None = None
        self.backup_label: ctk.CTkLabel | None = None
        self.details_title_label: ctk.CTkLabel | None = None
        self.status_label: ctk.CTkLabel | None = None

        # Переиспользуемые компоненты (gui_widgets)
        self.admin_banner: AdminBanner | None = None
        self.actions: ActionButtons | None = None
        self.status_bar: StatusBar | None = None

        # Состояние подсветки рекомендуемого действия
        self._stage: str = "start"
        self._pulse_job: str | None = None
        self._pulse_state: dict[str, Any] | None = None

        # Потокобезопасная доставка результатов фоновых потоков в UI:
        # вызывать tkinter из другого потока нельзя (блокируется/падает)
        self._ui_queue: queue.Queue[tuple[Any, tuple[Any, ...]]] = queue.Queue()

        self._build_ui()
        # Цикл опроса очереди результатов (UI-поток)
        self.after(POLL_INTERVAL_MS, self._process_ui_queue)

    # ------------------------------------------------------------------
    # UI Layout
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """Собрать весь интерфейс (стиль — из активной темы ui_theme)."""

        # -------- Окно: фон и габариты по теме --------
        self.configure(fg_color=theme.S("bg"))
        self.minsize(*theme.S("min_size"))
        self.geometry(theme.S("window_size"))

        # -------- Верхний баннер: Admin Status + переключатели вида --------
        self.admin_banner = AdminBanner(
            self,
            on_restart_admin=self._restart_as_admin,
            on_lang_change=self._on_lang_change,
            on_toggle_theme=self._on_toggle_theme,
            on_brightness_change=self._on_brightness_change,
        )
        self.admin_banner.pack(fill="x", padx=20, pady=(15, 5))

        # Ссылки на виджеты баннера для обратной совместимости
        self.admin_icon_label = self.admin_banner.icon_label
        self.admin_label = self.admin_banner.status_label
        self.admin_btn = self.admin_banner.restart_btn
        self.lang_menu = self.admin_banner.lang_menu

        # Пояснение, что именно доступно при текущих правах
        self.admin_hint_label = ctk.CTkLabel(
            self,
            text="",
            font=theme.body_font(theme.N("sz_hint")),
            justify="left",
            wraplength=860,
        )
        self.admin_hint_label.pack(fill="x", padx=24, pady=(0, 2))

        self._update_admin_banner()

        # -------- Строка-подсказка: что делать дальше --------
        self.guide_label = ctk.CTkLabel(
            self,
            text="",
            font=theme.body_font(theme.N("sz_guide"), "bold"),
            text_color=theme.C("guide"),
        )
        self.guide_label.pack(fill="x", padx=24, pady=(0, 4))

        # -------- Сканирование системы --------
        scan_frame = ctk.CTkFrame(
            self, corner_radius=theme.N("radius_frame"), fg_color="transparent"
        )
        scan_frame.pack(fill="x", padx=20, pady=(5, 10))

        self.scan_button = ctk.CTkButton(
            scan_frame,
            text=_t("btn_scan"),
            command=self._on_scan,
            height=theme.N("h_scan"),
            corner_radius=theme.N("radius_btn"),
            font=theme.body_font(theme.N("sz_scan"), "bold"),
            fg_color=theme.S("accent_bg"),
            hover_color=theme.S("accent_hover"),
            border_width=theme.N("bw_scan"),
            border_color=theme.S("accent_border"),
            text_color=theme.C("accent_text"),
        )
        self.scan_button.pack(fill="x", ipady=2)

        # -------- Список найденных раскладок (выбор кликом по строке) --------
        list_frame = ctk.CTkFrame(
            self, corner_radius=theme.N("radius_frame"), fg_color="transparent"
        )
        list_frame.pack(fill="x", padx=20, pady=(5, 5))

        self.list_title_label = ctk.CTkLabel(
            list_frame,
            text=_t("list_placeholder_before_scan"),
            font=theme.body_font(theme.N("sz_list_title"), "bold"),
            text_color=theme.C("section_title"),
        )
        self.list_title_label.pack(anchor="w", padx=(5, 0), pady=(5, 2))

        self.layouts_list_frame = ctk.CTkScrollableFrame(
            list_frame,
            height=theme.N("h_list"),
            corner_radius=theme.N("radius_frame"),
            fg_color=theme.S("list_bg"),
            border_width=theme.N("bw_list"),
            border_color=theme.S("list_border"),
        )
        self.layouts_list_frame.pack(fill="x")

        # Плейсхолдер внутри списка (до первого сканирования)
        self._list_placeholder = ctk.CTkLabel(
            self.layouts_list_frame,
            text=_t("list_placeholder_after_scan"),
            font=theme.body_font(theme.N("sz_placeholder")),
            text_color=theme.C("text_dim"),
        )
        self._list_placeholder.pack(padx=10, pady=18)

        # -------- Панель деталей --------
        # ВАЖНО: pack() вызывается в конце метода — после кнопок действий
        # и статус-бара, которые привязаны к низу окна. Так панель деталей
        # забирает только остаток места и сжимается при уменьшении окна,
        # а кнопки «Удалить раскладку»/«Восстановить из бэкапа» всегда видны.
        detail_frame = ctk.CTkFrame(
            self,
            corner_radius=theme.N("radius_frame"),
            fg_color=theme.S("panel"),
            border_width=theme.N("bw_panel"),
            border_color=theme.S("panel_border"),
        )

        self.details_title_label = ctk.CTkLabel(
            detail_frame,
            text=_t("details_title"),
            font=theme.body_font(theme.N("sz_details_title"), "bold"),
            text_color=theme.C("section_title"),
        )
        self.details_title_label.pack(anchor="w", padx=(5, 0), pady=(5, 0))

        self.detail_text = ctk.CTkTextbox(
            detail_frame,
            wrap="word",
            font=theme.body_font(theme.N("sz_detail_text")),
            text_color=theme.C("text"),
            fg_color=theme.S("detail_bg"),
            state="disabled",
            corner_radius=theme.N("radius_text"),
            # Явная стартовая высота: без неё виджет запрашивает дефолтные
            # ~200px и раздувает требуемую высоту окна (панель деталей
            # «резиновая» — expand=True растянет её до свободного места)
            height=110,
        )
        self.detail_text.pack(
            fill="both", expand=True, padx=5, pady=(2, 5), ipady=5
        )
        self._config_detail_tags()
        self._render_initial_hint()

        # -------- Нижние блоки: пакуются СНИЗУ ВВЕРХ (side="bottom") --------
        # Порядок упаковки = порядок снизу вверх: статус-бар — у самой
        # нижней кромки, над ним метка бэкапа, над ней кнопки действий.
        # Благодаря этому при любой высоте окна (вплоть до минимальной)
        # «Удалить раскладку» и «Восстановить из бэкапа» остаются на экране.

        # Статус-бар с прогресс-индикатором (gui_widgets.StatusBar)
        self.status_bar = StatusBar(self)
        self.status_bar.pack(fill="x", side="bottom", padx=0, pady=0)
        self.status_bar.pack_propagate(False)
        # Алиасы для обратной совместимости с остальным кодом main.py
        self.status_label = self.status_bar.label
        self.progress_bar = self.status_bar.progress_bar
        self._progress_active = False

        # Метка бэкапа
        self.backup_label = ctk.CTkLabel(
            self,
            text="",
            font=theme.body_font(theme.N("sz_backup")),
            text_color=theme.C("backup"),
            justify="center",
        )
        self.backup_label.pack(side="bottom", fill="x", padx=20, pady=(0, 5))

        # Панель действий: удаление, блокировка облака, восстановление
        # Переиспользуемый компонент gui_widgets.ActionButtons
        self.actions = ActionButtons(
            self,
            on_delete=self._on_delete_layout,
            on_restore=self._on_restore_backup,
            on_toggle_sync=self._on_toggle_block_sync,
        )
        self.actions.pack(side="bottom", fill="x", padx=20, pady=(5, 10))
        # Алиасы для обратной совместимости с остальным кодом main.py
        self.delete_button = self.actions.delete_btn
        self.restore_button = self.actions.restore_btn
        self.block_sync_checkbox = self.actions.block_sync_checkbox

        # -------- Панель деталей: забирает весь ОСТАТОК места --------
        detail_frame.pack(fill="both", expand=True, padx=20, pady=(5, 5))

        # Стартовое состояние: подсвечиваем кнопку сканирования
        self._set_stage(self._stage)

    # ------------------------------------------------------------------
    # Admin banner
    # ------------------------------------------------------------------

    def _update_admin_banner(self) -> None:
        """Обновить баннер статуса администратора (делегирует AdminBanner)."""
        if not self.admin_banner:
            return
        self.admin_banner.update_status(is_admin())
        # Пояснение к баннеру — остаётся в main.py (отдельный виджет)
        if self.admin_hint_label:
            if is_admin():
                self.admin_hint_label.configure(
                    text=_t("banner_admin_ok_hint"),
                    text_color=theme.C("ok"),
                )
            else:
                self.admin_hint_label.configure(
                    text=_t("banner_admin_no_hint"),
                    text_color=theme.C("hint_warn"),
                )

    # ------------------------------------------------------------------
    # Переключение языка интерфейса
    # ------------------------------------------------------------------

    def _on_lang_change(self, choice: str) -> None:
        """Сменить язык и перевести все статические элементы."""
        names = list(i18n.DISPLAY_NAMES.values())
        if choice in names:
            codes = list(i18n.DISPLAY_NAMES.keys())
            i18n.set_language(codes[names.index(choice)])
        self._apply_static_texts()

    def _apply_static_texts(self) -> None:
        """Перевести статические элементы (при смене языка)."""
        self.title(_t("app_title"))
        # Подписи переключателей вида (интерфейс/яркость) в шапке
        if self.admin_banner:
            self.admin_banner.apply_language()
        # ⏳-состояния не трогаем: временный текст вернётся сам по завершении
        if self.scan_button and not str(
            self.scan_button.cget("text")
        ).startswith("⏳"):
            self.scan_button.configure(
                text=_t("btn_rescan" if self._scan_done else "btn_scan")
            )
        for widget, key in (
            (self.delete_button, "btn_delete"),
            (self.restore_button, "btn_restore"),
            (self.admin_btn, "btn_restart_admin"),
        ):
            if widget and not str(widget.cget("text")).startswith("⏳"):
                widget.configure(text=_t(key))
        if self.block_sync_checkbox:
            self.block_sync_checkbox.configure(text=_t("chk_block_cloud_sync"))
        self._update_admin_banner()
        if self.details_title_label:
            self.details_title_label.configure(text=_t("details_title"))
        if self.list_title_label:
            if not self._scan_done:
                self.list_title_label.configure(
                    text=_t("list_placeholder_before_scan")
                )
            elif self.layouts_data:
                self.list_title_label.configure(
                    text=_t("list_title_found", count=len(self.layouts_data))
                )
            else:
                self.list_title_label.configure(text=_t("list_title_empty"))
        # Строки списка пересоздаются с учётом новой локали
        if self.layouts_data:
            self._render_layouts_list()
        # Плейсхолдер списка (виден только до первого сканирования)
        if self._list_placeholder:
            try:
                self._list_placeholder.configure(
                    text=_t("list_placeholder_after_scan")
                )
            except Exception:  # noqa: BLE001
                pass
        # Панель деталей: перерисовать текущее состояние
        if not self.layouts_data:
            self._render_initial_hint()
        elif self.current_layout_klid in self.layouts_data:
            self._show_layout_details(self.current_layout_klid)
        else:
            self._render_layouts_overview()
        # Метка последних бэкапов (пути не локализуются)
        if self.backup_label and any(self._last_backup_info):
            reg_path, lang_path = self._last_backup_info
            parts = []
            if reg_path:
                parts.append(_t("dlg_backup_registry", path=reg_path))
            if lang_path:
                parts.append(_t("dlg_backup_langlist", path=lang_path))
            self.backup_label.configure(text="\n".join(parts))
        # Строка-подсказка этапа (текст берётся из локали заново)
        self._set_stage(self._stage)

    # ------------------------------------------------------------------
    # Смена интерфейса (classic/terminal) и яркости шрифтов
    # ------------------------------------------------------------------

    def _view_switch_allowed(self) -> bool:
        """Переключать вид нельзя во время фоновой операции."""
        return self._stage != "busy" and not self._progress_active

    def _on_toggle_theme(self) -> None:
        """Переключить интерфейс: terminal ↔ classic (с пересборкой UI)."""
        if not self._view_switch_allowed():
            self._set_status(_t("status_busy_wait"))
            return
        theme.set_theme(
            "terminal" if theme.current_theme() == "classic" else "classic"
        )
        self._rebuild_ui()

    def _on_brightness_change(self, code: str) -> None:
        """Сменить яркость шрифтов (code из ui_theme.BRIGHTNESS_FACTORS)."""
        if not self._view_switch_allowed():
            self._set_status(_t("status_busy_wait"))
            return
        theme.set_brightness(code)
        self._rebuild_ui()

    def _rebuild_ui(self) -> None:
        """
        Пересобрать интерфейс с сохранением состояния (данные скана,
        выбранная раскладка, этап подсказки, тексты ⏳-состояний вернутся
        сами по завершении операций).
        """
        self._stop_pulse()
        self._pulse_state = None
        self._pulse_job = None
        for child in self.winfo_children():
            try:
                child.destroy()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Виджет не удалось уничтожить при пересборке UI: %s", exc)
        self._build_ui()
        self._apply_static_texts()
        # Восстановить доступность кнопки восстановления после скана
        if self._scan_done and self.restore_button:
            self.restore_button.configure(state="normal")

    # ------------------------------------------------------------------
    # Подсветка рекомендуемого действия (этапы работы)
    # ------------------------------------------------------------------

    _STAGE_HINTS: dict[str, str] = {
        "start": "stage_hint_start",
        "choose": "stage_hint_choose",
        "delete": "stage_hint_delete",
        "busy": "stage_hint_busy",
    }

    def _set_stage(self, stage: str) -> None:
        """Установить этап: строка-подсказка + подсветка нужного элемента."""
        self._stage = stage
        self._stop_pulse()
        if self.guide_label:
            self.guide_label.configure(text=_t(self._STAGE_HINTS.get(stage, "")))
        if stage == "start":
            if self.scan_button:
                self.scan_button.configure(state="normal")
                self._start_pulse("button", self.scan_button, theme.P("scan"))
        elif stage == "choose":
            if self.layouts_list_frame:
                self._start_pulse("border", self.layouts_list_frame, theme.P("list"))
        elif stage == "delete":
            if self.delete_button:
                self._start_pulse("button", self.delete_button, theme.P("delete"))

    # ------------------------------------------------------------------
    # Прогресс-бар для длительных операций
    # ------------------------------------------------------------------

    def _progress_start(self) -> None:
        """Запустить анимированный прогресс-бар (indeterminate mode)."""
        self._progress_active = True
        if self.status_bar:
            self.status_bar.progress_start()

    def _progress_stop(self) -> None:
        """Остановить прогресс-бар."""
        self._progress_active = False
        if self.status_bar:
            self.status_bar.progress_stop()

    def _start_pulse(
        self, kind: str, widget: Any, colors: tuple[str, str]
    ) -> None:
        """Запустить попеременную подсветку элемента (базовый ↔ акцент)."""
        self._pulse_state = {
            "kind": kind,
            "widget": widget,
            "colors": colors,
            "i": 0,
        }
        self._pulse_tick()

    def _pulse_tick(self) -> None:
        """Один такт подсветки (перепланируется через after)."""
        state = self._pulse_state
        if not state:
            return
        widget = state["widget"]
        try:
            state["i"] = (state["i"] + 1) % 2
            color = state["colors"][state["i"]]
            if state["kind"] == "button":
                widget.configure(fg_color=color)
            else:
                widget.configure(border_color=color)
        except Exception as exc:  # noqa: BLE001
            # Виджет мог быть уничтожен — тихо прекращаем подсветку
            logger.debug("Пульсация прекращена (виджет уничтожен): %s", exc)
            self._pulse_state = None
            self._pulse_job = None
            return
        self._pulse_job = self.after(PULSE_INTERVAL_MS, self._pulse_tick)

    def _stop_pulse(self) -> None:
        """Остановить подсветку и вернуть элементу базовый цвет."""
        state = self._pulse_state
        if state:
            try:
                if state["kind"] == "button":
                    state["widget"].configure(fg_color=state["colors"][0])
                else:
                    state["widget"].configure(border_color=state["colors"][0])
            except Exception as exc:  # noqa: BLE001
                logger.debug("Не удалось сбросить цвет пульсации: %s", exc)
        self._pulse_state = None
        if self._pulse_job:
            try:
                self.after_cancel(self._pulse_job)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Не удалось отменить задачу пульсации: %s", exc)
            self._pulse_job = None

    def _post(self, func: Any, *args: Any) -> None:
        """Положить UI-задачу от фонового потока в очередь (потокобезопасно)."""
        self._ui_queue.put((func, args))

    def _process_ui_queue(self) -> None:
        """Обработать результаты фоновых потоков в UI-потоке."""
        try:
            while True:
                func, args = self._ui_queue.get_nowait()
                try:
                    func(*args)
                except Exception:
                    logger.exception("Ошибка обработки UI-задачи %s", func)
        except queue.Empty:
            pass
        self.after(POLL_INTERVAL_MS, self._process_ui_queue)

    # ------------------------------------------------------------------
    # Сканирование
    # ------------------------------------------------------------------

    def _on_scan(self) -> None:
        """Запустить сканирование в фоновом потоке."""
        self._set_stage("busy")
        if self.scan_button:
            self.scan_button.configure(state="disabled", text=_t("btn_scan_busy"))
        if self.restore_button:
            self.restore_button.configure(state="disabled")
        self._set_status(_t("status_scanning"))
        self._progress_start()

        def _scan_thread() -> None:
            try:
                layouts = scan_keyboard_layouts()
                # layouts_data обновляется только в UI-потоке (через очередь)
                self._post(self._after_scan_success, layouts)
            except Exception as exc:
                self._post(self._after_scan_error, str(exc))

        threading.Thread(target=_scan_thread, daemon=True).start()

    def _apply_scan_results(self, layouts: dict[str, list[dict[str, str]]]) -> None:
        """Обновить GUI данными сканирования (без модальных окон)."""
        self.layouts_data = layouts
        self._render_layouts_list()
        if self.list_title_label:
            self.list_title_label.configure(
                text=(
                    _t("list_title_found", count=len(layouts))
                    if layouts
                    else _t("list_title_empty")
                )
            )
        if self.current_layout_klid in self.layouts_data:
            # Ранее выбранная раскладка ещё на месте (обновление данных
            # после удаления) — сразу показываем её разделы
            self._show_layout_details(self.current_layout_klid)
            if self.delete_button:
                self.delete_button.configure(state="normal")
            self._set_stage("delete")
        else:
            self.current_layout_klid = ""
            if self.delete_button:
                self.delete_button.configure(state="disabled")
            self._render_layouts_overview()
            self._set_stage("choose")
        self._set_status(_t("status_found_count", count=len(layouts)))

    def _after_scan_success(self, layouts: dict[str, list[dict[str, str]]]) -> None:
        """Обработка успешного завершения сканирования."""
        self._progress_stop()
        if self.scan_button:
            self.scan_button.configure(
                text=_t("btn_rescan"), state="normal"
            )
        self._scan_done = True
        if self.restore_button:
            self.restore_button.configure(state="normal")
        self._apply_scan_results(layouts)

        # Модальное окно — только если раскладок не найдено (неожиданный итог)
        if not layouts:
            messagebox.showinfo(
                _t("dlg_scan_result_title"),
                _t("dlg_scan_empty"),
            )

    def _after_scan_error(self, error_msg: str) -> None:
        """Обработка ошибки сканирования."""
        self._progress_stop()
        if self.scan_button:
            self.scan_button.configure(text=_t("btn_scan"), state="normal")
        if self.restore_button:
            self.restore_button.configure(state="normal")
        self._set_status(_t("status_scan_error"))
        self._set_stage("start")
        messagebox.showerror(
            _t("dlg_error_title"),
            _t("dlg_scan_failed", error=error_msg),
        )

    # ------------------------------------------------------------------
    # Выбор раскладки и детали
    # ------------------------------------------------------------------

    def _render_layouts_list(self) -> None:
        """Построить кликабельный список найденных раскладок."""
        if not self.layouts_list_frame:
            return
        # Пересоздаём строки списка (включая плейсхолдер)
        for child in self.layouts_list_frame.winfo_children():
            child.destroy()
        self._layout_row_buttons.clear()
        for klid in sorted(self.layouts_data):
            count = len(self.layouts_data[klid])
            row = ctk.CTkButton(
                self.layouts_list_frame,
                text=_t(
                    theme.row_text_key(),
                    name=_build_layout_name(klid),
                    count=count,
                ),
                anchor="w",
                height=theme.N("h_row"),
                corner_radius=theme.N("radius_row"),
                font=theme.body_font(theme.N("sz_row")),
                fg_color=theme.S("row"),
                hover_color=theme.S("row_hover"),
                text_color=theme.C("row_text"),
                command=lambda k=klid: self._on_layout_clicked(k),
            )
            row.pack(fill="x", padx=8, pady=4)
            self._layout_row_buttons[klid] = row
        if not self.layouts_data:
            self._list_placeholder = ctk.CTkLabel(
                self.layouts_list_frame,
                text=_t("list_placeholder_empty"),
                font=theme.body_font(theme.N("sz_placeholder")),
                text_color=theme.C("text_dim"),
            )
            self._list_placeholder.pack(padx=10, pady=18)
        self._highlight_selected_row()

    def _highlight_selected_row(self) -> None:
        """Подсветить строку выбранной раскладки акцентным цветом."""
        for klid, row in self._layout_row_buttons.items():
            if klid == self.current_layout_klid:
                row.configure(
                    fg_color=theme.S("row_selected"),
                    hover_color=theme.S("row_selected_hover"),
                    text_color=theme.C("row_text_selected"),
                )
            else:
                row.configure(
                    fg_color=theme.S("row"),
                    hover_color=theme.S("row_hover"),
                    text_color=theme.C("row_text"),
                )

    def _on_layout_clicked(self, klid: str) -> None:
        """Обработать клик по строке списка раскладок."""
        if not self.detail_text or not self.delete_button:
            return
        if klid not in self.layouts_data:
            return

        self.current_layout_klid = klid
        self._highlight_selected_row()
        self._show_layout_details(klid)

        # Активируем кнопку удаления и подсвечиваем её
        self.delete_button.configure(state="normal")
        self._set_stage("delete")

    def _config_detail_tags(self) -> None:
        """Настроить стили rich-text панели деталей (шрифты и цвета)."""
        tb = self.detail_text
        if not tb:
            return
        # CTk автоматически масштабирует шрифты обычных виджетов под DPI
        # экрана, но шрифты тегов внутреннего tkinter.Text мы применяем
        # сами — поэтому умножаем их на тот же коэффициент, иначе при
        # 125–150% DPI панель деталей выглядит мельче остального интерфейса.
        try:
            scale = float(ctk.ScalingTracker.get_window_scaling(self))
        except Exception:
            scale = 1.0
        if scale <= 0:
            scale = 1.0
        self._detail_font_scale = scale

        def px(n: int) -> int:
            return max(9, round(n * scale))

        ui, mono = theme.F("font_ui"), theme.F("font_mono")
        if theme.S("mono_ui"):
            # Терминальный стиль: вся панель — моноширинным шрифтом
            ui = mono
        tags: dict[str, tuple[ctk.CTkFont, str]] = {
            # Заголовки и подписи
            "h1": (ctk.CTkFont(family=ui, size=px(19), weight="bold"), theme.C("tag_h1")),
            "sub": (ctk.CTkFont(family=ui, size=px(14)), theme.C("tag_sub")),
            "legend": (ctk.CTkFont(family=ui, size=px(12)), theme.C("tag_legend")),
            "dim": (ctk.CTkFont(family=ui, size=px(13)), theme.C("tag_dim")),
            # Пути разделов реестра
            "path": (
                ctk.CTkFont(family=mono, size=px(15), weight="bold"),
                theme.C("tag_path"),
            ),
            # Статусы обнаружения
            "ok": (ctk.CTkFont(family=ui, size=px(15), weight="bold"), theme.C("tag_ok")),
            "miss": (
                ctk.CTkFont(family=ui, size=px(14), slant="italic"),
                theme.C("tag_miss"),
            ),
            # Права администратора и предупреждения
            "admin": (
                ctk.CTkFont(family=ui, size=px(15), weight="bold"),
                theme.C("tag_admin"),
            ),
            "blocked": (
                ctk.CTkFont(family=ui, size=px(15), weight="bold"),
                theme.C("tag_blocked"),
            ),
            "warn": (
                ctk.CTkFont(family=ui, size=px(15), weight="bold"),
                theme.C("tag_warn"),
            ),
            # Значения записей и примечания
            "val": (ctk.CTkFont(family=mono, size=px(14)), theme.C("tag_val")),
            "note": (
                ctk.CTkFont(family=ui, size=px(13), slant="italic"),
                theme.C("tag_note"),
            ),
            # Заголовки в сводке сканирования
            "item": (
                ctk.CTkFont(family=ui, size=px(17), weight="bold"),
                theme.C("tag_item"),
            ),
        }
        for name, (font, color) in tags.items():
            # Цвет — через публичный API customtkinter
            tb.tag_config(name, foreground=color)
            # Шрифт — публичный tag_config запрещает 'font' (incompatible
            # with scaling), поэтому применяем его во внутренний tkinter
            # Text напрямую; при изменении внутреннего API — тихо пропускаем.
            # Безопасная проверка наличия внутреннего виджета (hasattr)
            # предотвращает сбой GUI при обновлении customtkinter.
            if hasattr(tb, "_textbox"):
                tb._textbox.tag_config(name, font=font)  # type: ignore[attr-defined]
            else:
                logger.debug(
                    "tag font not applied (no _textbox): %s", name
                )

    def _show_layout_details(self, klid: str) -> None:
        """
        Показать для выбранной раскладки ВСЕ сканируемые разделы
        со статусом «обнаружено / не обнаружено» и значениями записей.
        Оформление задаётся тегами из _config_detail_tags().
        """
        tb = self.detail_text
        if not tb:
            return

        # При смене DPI/масштабирования экрана применяем теги заново
        try:
            scale = float(ctk.ScalingTracker.get_window_scaling(self))
        except Exception:
            scale = 1.0
        if scale != getattr(self, "_detail_font_scale", None):
            self._config_detail_tags()

        tb.configure(state="normal")
        tb.delete("1.0", "end")

        locations = self.layouts_data.get(klid, [])
        by_path: dict[str, list[str]] = {}
        for loc in locations:
            by_path.setdefault(loc.get("path", ""), []).append(
                loc.get("value", "")
            )

        admin = is_admin()
        layout_name = _build_layout_name(klid)
        divider = "─" * 56

        # Оптимизация: копим (текст, тег), сливаем соседние одинаковые теги
        # и вставляем единым проходом — меньше вызовов Tk, без фризов UI
        # на больших списках разделов.
        chunks: list[tuple[str, str]] = [
            (_t("details_layout", name=layout_name) + "\n", "h1"),
            (
                _t("details_klid", klid=klid, count=len(locations)) + "\n",
                "sub",
            ),
        ]
        if _active_lang_cache(klid, self.layouts_data):
            chunks.append((_t("details_active_cache") + "\n\n", "warn"))
        chunks.append((divider + "\n", "dim"))
        chunks.append(
            (
                _t("legend_found")
                + "      "
                + _t("legend_not_found")
                + "\n",
                "legend",
            )
        )
        chunks.append((divider + "\n\n", "dim"))

        # Все разделы, которые сканирует приложение, + PowerShell-список
        # языков и справочный каталог имён HKLM
        # Sandbox-aware: в sandbox-режиме показываются sandbox-ветки —
        # те же пути, что возвращает сканер (см. регрессию P0-1)
        rows: list[tuple[str, bool, str]] = [
            (f"{root}\\{subkey}", admin_required, "")
            for root, subkey, _mode, admin_required in get_affected_branches()
        ]
        rows.append((PS_SECTION_PATH, False, ""))
        rows.append((HKLM_CATALOG_PATH, False, HKLM_CATALOG_NOTE))

        hku_blocked = False
        for path, admin_required, note in rows:
            values = by_path.get(path, [])
            mark = "[+]" if values else "[ ]"
            mark_tag = "ok" if values else "miss"
            if values:
                status, status_tag = _t("status_found", count=len(values)), "ok"
            else:
                status, status_tag = _t("status_not_found"), "miss"

            chunks.append((f"{mark} ", mark_tag))
            chunks.append((f"{path}\n", "path"))
            chunks.append(("        " + _t("label_status"), "dim"))
            chunks.append((status, status_tag))
            if note:
                chunks.append((f"  ({note})", "note"))
            chunks.append(("\n", "dim"))
            if admin_required and not admin:
                # Предупреждение — только когда прав нет. При запуске от
                # администратора раздел ведёт себя как обычный, и лишние
                # напоминания не показываем.
                chunks.append(("        " + _t("label_needs_admin"), "admin"))
                chunks.append(
                    (_t("label_skipped_no_admin") + "\n", "blocked")
                )
                if values:
                    hku_blocked = True
            # Значения — ЕДИНЫЙ chunk с тегом val: 7 вставок → 1
            val_lines = [
                "            " + _t("details_value", value=v)
                for v in values[:6]
            ]
            if len(values) > 6:
                val_lines.append(
                    "            "
                    + _t("details_more", count=len(values) - 6)
                )
            if val_lines:
                chunks.append(("\n".join(val_lines) + "\n", "val"))
            chunks.append(("\n", "dim"))

        if hku_blocked:
            chunks.append((_t("warn_title") + "\n", "warn"))
            chunks.append((_t("warn_hku") + "\n\n", "admin"))

        # Единый проход вставки: соседние одинаковые теги сливаются —
        # число вызовов Tk снижается в ~2 раза без потери оформления.
        merged_text: list[str] = []
        merged_tag = ""
        for text, tag in chunks:
            if tag == merged_tag:
                merged_text.append(text)
                continue
            if merged_text:
                tb.insert("end", "".join(merged_text), merged_tag)
            merged_text, merged_tag = [text], tag
        if merged_text:
            tb.insert("end", "".join(merged_text), merged_tag)
        tb.configure(state="disabled")

    def _render_initial_hint(self) -> None:
        """Стартовый текст панели деталей (до первого сканирования)."""
        tb = self.detail_text
        if not tb:
            return
        tb.configure(state="normal")
        tb.delete("1.0", "end")
        tb.insert("end", _t("hint_welcome_title") + "\n", "h1")
        tb.insert(
            "end",
            _t("hint_welcome_scan") + "\n\n",
            "sub",
        )
        tb.insert(
            "end",
            _t("hint_welcome_details") + "\n",
            "sub",
        )
        tb.insert("end", "\n" + _t("hint_legend"), "legend")
        tb.insert("end", _t("legend_found"), "ok")
        tb.insert("end", "      ", "legend")
        tb.insert("end", _t("legend_not_found"), "miss")
        if not is_admin():
            # Пометка о правах нужна только тем, кто запустил без админа
            tb.insert("end", "      " + _t("legend_needs_admin"), "admin")
        tb.configure(state="disabled")

    def _render_layouts_overview(self) -> None:
        """Сводка в панели деталей: что найдено и что делать дальше."""
        tb = self.detail_text
        if not tb:
            return
        tb.configure(state="normal")
        tb.delete("1.0", "end")
        if not self.layouts_data:
            tb.insert("end", _t("overview_empty_title") + "\n", "h1")
            tb.insert("end", _t("overview_empty_hint"), "sub")
        else:
            tb.insert(
                "end",
                _t("overview_title", count=len(self.layouts_data)) + "\n\n",
                "h1",
            )
            tb.insert(
                "end",
                _t("overview_pick"),
                "sub",
            )
            tb.insert(
                "end",
                _t("overview_pick2"),
                "sub",
            )
            tb.insert("end", _t("overview_pick3"), "sub")
        tb.configure(state="disabled")

    # ------------------------------------------------------------------
    # Удаление раскладки
    # ------------------------------------------------------------------

    def _on_delete_layout(self) -> None:
        """Сначала dry-run план удаления, затем подтверждение и удаление."""
        if not self.current_layout_klid:
            return

        klid = self.current_layout_klid
        # Валидация KLID — защита от некорректных данных
        if not re.fullmatch(r"[0-9a-f]{8}", klid):
            self._set_status(_t("status_delete_error"))
            messagebox.showerror(
                _t("dlg_error_title"),
                _t("dlg_invalid_klid", klid=klid),
            )
            return

        self._set_stage("busy")
        if self.delete_button:
            self.delete_button.configure(state="disabled", text=_t("btn_analyze_busy"))
        self._set_status(_t("status_analyzing"))

        def _plan_thread() -> None:
            try:
                plan = plan_layout_removal(klid)
                self._post(self._after_plan, plan)
            except Exception as exc:
                self._post(self._after_delete_error, str(exc))

        threading.Thread(target=_plan_thread, daemon=True).start()

    def _after_plan(self, plan: dict[str, Any]) -> None:
        """Показать план удаления и спросить подтверждение."""
        layout_name = _build_layout_name(plan["klid"])
        admin_status = (
            _t("plan_rights_full")
            if plan["admin_privileges"]
            else _t("plan_rights_limited")
        )
        confirm = messagebox.askyesno(
            _t("dlg_confirm_delete_title"),
            self._format_plan(plan, layout_name, admin_status),
        )
        if not confirm:
            if self.delete_button:
                self.delete_button.configure(
                    text=_t("btn_delete"), state="normal"
                )
            if self.restore_button:
                self.restore_button.configure(state="normal")
            self._set_status(_t("status_delete_cancelled"))
            self._set_stage("delete")
            return

        klid = plan["klid"]
        if self.delete_button:
            self.delete_button.configure(state="disabled", text=_t("btn_delete_busy"))
        if self.restore_button:
            self.restore_button.configure(state="disabled")
        self.backup_label.configure(text="")
        self._set_status(_t("status_deleting"))
        self._progress_start()

        def _delete_thread() -> None:
            try:
                report = delete_layout(klid)
                self._post(self._after_delete, report)
            except Exception as exc:
                self._post(self._after_delete_error, str(exc))

        threading.Thread(target=_delete_thread, daemon=True).start()

    def _format_plan(
        self, plan: dict[str, Any], layout_name: str, admin_status: str
    ) -> str:
        """Сформировать человеко-читаемый текст плана удаления."""
        lines = [
            _t("plan_layout", name=layout_name),
            _t("plan_klid", klid=plan["klid"]),
            _t("plan_rights", status=admin_status),
            "",
            _t("plan_will_delete"),
        ]
        found_any = False
        for branch, info in plan["branches"].items():
            if info.get("admin_required") and not plan["admin_privileges"]:
                lines.append(_t("plan_skipped_no_admin", branch=branch))
                continue
            values = info.get("values", [])
            profile_keys = info.get("profile_keys", [])
            if values or profile_keys:
                found_any = True
                lines.append(f"  • {branch}:")
                for val in values[:10]:
                    lines.append(_t("plan_value_line", value=val))
                if len(values) > 10:
                    lines.append(_t("plan_value_more", count=len(values) - 10))
                for pkey in profile_keys[:6]:
                    lines.append(_t("plan_profile_line", path=pkey))
                if len(profile_keys) > 6:
                    lines.append(
                        _t("plan_value_more", count=len(profile_keys) - 6)
                    )
        if not found_any:
            lines.append(_t("plan_no_matches"))

        ps = plan.get("ps", {})
        lines.append("")
        if ps.get("languages_removed"):
            lines.append(
                _t("plan_langs_removed", langs=", ".join(ps["languages_removed"]))
            )
        elif ps.get("tips_removed"):
            lines.append(
                _t("plan_tips_removed", tips="; ".join(ps["tips_removed"][:8]))
            )
        elif ps.get("available"):
            lines.append(_t("plan_ps_nochange"))
        else:
            lines.append(
                _t("plan_ps_unavailable", detail=ps.get("detail", ""))
            )
        if plan.get("ctfmon_restart"):
            lines.append(_t("plan_ctfmon"))
        if _active_lang_cache(plan["klid"], self.layouts_data):
            lines.append("")
            lines.append(_t("plan_active_cache"))
        if plan.get("last_layout_guard"):
            lines.append("")
            lines.append(_t("plan_last_layout"))
        lines.append("")
        lines.append(_t("plan_backup_note"))
        return "\n".join(lines)

    def _after_delete(self, report: dict[str, Any]) -> None:
        """Обработка результата удаления."""
        self._progress_stop()
        if self.delete_button:
            self.delete_button.configure(
                text=_t("btn_delete"), state="normal"
            )
        if self.restore_button:
            self.restore_button.configure(state="normal")

        backup_path = report.get("backup_path", "")
        lang_backup = report.get("langlist_backup_path", "")
        total_deleted = (
            len(report.get("hkcu_preload_deleted", []))
            + len(report.get("hkcu_substitutes_deleted", []))
            + len(report.get("hkcu_ctf_deleted", []))
            + len(report.get("hkcu_intl_deleted", []))
            + len(report.get("hku_default_deleted", []))
            + len(report.get("ctf_profiles_deleted", []))
        )

        power_sync = report.get("power_sync", False)
        sync_detail = report.get("power_sync_detail", "")

        # Защита последней раскладки: отчёт раньше остального текста
        if report.get("last_layout_guard"):
            self._set_status(_t("status_last_layout"))
            messagebox.showwarning(
                _t("dlg_last_layout_title"),
                _t("dlg_last_layout"),
            )
            self._refresh_after_delete()
            return

        # Провал бэкапа: удаление было отменено ДО изменения реестра —
        # объясняем пользователю причину и способ обхода.
        if report.get("backup_ok") is False:
            self._set_status(_t("status_backup_failed"))
            messagebox.showerror(
                _t("dlg_error_title"),
                _t("dlg_backup_failed", error=report.get("backup_error", "")),
            )
            self._refresh_after_delete()
            return

        # Сообщение о результате
        if report.get("success"):
            msg = (
                _t("dlg_result_done")
                + "\n\n"
                + _t("dlg_result_deleted", count=total_deleted)
                + "\n"
                + _t("dlg_result_sync_prefix")
                + ("✓ " + sync_detail if power_sync else "✗ " + sync_detail)
            )

            # Индикатор блокировки облачной синхронизации
            sync_blocked = report.get("language_sync_blocked", False)
            sync_block_detail = report.get("language_sync_block_detail", "")
            msg += (
                "\n"
                + _t("dlg_result_cloud_sync")
                + ("✓ " + sync_block_detail if sync_blocked else "✗ " + sync_block_detail)
            )

            # Индикатор синхронизации Экрана приветствия
            welcome_synced = report.get("welcome_screen_synced", False)
            welcome_detail = report.get("welcome_screen_sync_detail", "")
            msg += (
                "\n"
                + _t("dlg_result_welcome_sync")
                + ("✓ " + welcome_detail if welcome_synced else "✗ " + welcome_detail)
            )

            partial = report.get("backup_failed_branches", [])
            if partial:
                msg += "\n" + _t(
                    "dlg_result_backup_partial", branches=", ".join(partial)
                )
            if power_sync:
                msg += (
                    _t("dlg_advice_logoff")
                )
            else:
                msg += (
                    _t("dlg_advice_ps_failed")
                )
            retry = report.get("retry_cleaned", 0)
            if retry:
                msg += _t("dlg_advice_ctfmon", count=retry)
            profiles = len(report.get("ctf_profiles_deleted", []))
            if profiles:
                msg += "\n" + _t("dlg_result_ctf_profiles", count=profiles)
            ctfmon = report.get("ctfmon_restarted")
            if ctfmon is True:
                msg += "\n" + _t("dlg_result_ctfmon_ok")
            elif ctfmon is False:
                msg += "\n" + _t("dlg_result_ctfmon_fail")
            msg += "\n\n" + _t("dlg_advice_restore")
            self._set_status(_t("status_deleted_count", count=total_deleted))
            logger.info(
                "Удаление %s: записей=%d, sync=%s, cloud_block=%s, welcome=%s",
                report.get("klid", "?"),
                total_deleted,
                sync_detail,
                sync_block_detail,
                welcome_detail,
            )
        else:
            msg = (
                _t(
                    "dlg_delete_failed_nosync",
                    detail=sync_detail or "нет данных",
                )
            )
            self._set_status(_t("status_delete_failed"))
            logger.warning("Удаление не удалось: %s", report)

        messagebox.showinfo(_t("dlg_result_title"), msg)

        # Показываем пути к бэкапам
        label_lines = []
        if backup_path:
            label_lines.append(_t("dlg_backup_registry", path=backup_path))
        if lang_backup:
            label_lines.append(_t("dlg_backup_langlist", path=lang_backup))
        if label_lines:
            self.backup_label.configure(text="\n".join(label_lines))

        # Обновляем данные — пересканируем в фоновом потоке
        self._refresh_after_delete()

    def _after_delete_error(self, error_msg: str) -> None:
        """Обработка ошибки удаления."""
        self._progress_stop()
        if self.delete_button:
            self.delete_button.configure(
                text=_t("btn_delete"), state="normal"
            )
        if self.restore_button:
            self.restore_button.configure(state="normal")
        self._set_status(_t("status_delete_error"))
        self._set_stage("delete")
        messagebox.showerror(
            _t("dlg_error_title"),
            _t("dlg_delete_failed", error=error_msg),
        )

    def _on_toggle_block_sync(self) -> None:
        """Обработчик переключения галочки блокировки облачной синхронизации."""
        if self.block_sync_checkbox.get():
            ok, detail = disable_language_sync()
            if ok:
                self._set_status(_t("status_sync_blocked"))
            else:
                # Показываем детализированную причину ошибки
                self._set_status(detail)
                self.block_sync_checkbox.deselect()
        else:
            self._set_status(_t("status_sync_unblocked"))

    # ------------------------------------------------------------------
    # Восстановление из бэкапа
    # ------------------------------------------------------------------

    def _on_restore_backup(self) -> None:
        """Открыть диалог выбора и восстановления бэкапа."""
        try:
            backups = list_backups()
        except Exception as exc:
            logger.exception("Не удалось получить список бэкапов")
            messagebox.showerror(
                _t("dlg_error_title"),
                _t("dlg_backups_read_error", error=exc),
            )
            return
        if not backups:
            messagebox.showinfo(
                _t("dlg_restore_title"),
                _t("dlg_no_backups", path=get_backup_dir()),
            )
            return
        self._show_restore_dialog(backups)

    def _show_restore_dialog(self, backups: list[dict[str, Any]]) -> None:
        """Окно: список бэкапов + предпросмотр содержимого."""
        dialog = ctk.CTkToplevel(self)
        dialog.title(_t("dlg_restore_title"))
        dialog.geometry("700x480")
        dialog.configure(fg_color=theme.S("panel"))
        dialog.transient(self)

        def _grab() -> None:
            # Окно могло ещё не отрисоваться — пробуем безопасно
            try:
                dialog.grab_set()
            except Exception:
                pass

        dialog.after(120, _grab)

        ctk.CTkLabel(
            dialog,
            text=_t("dlg_restore_pick"),
            font=theme.body_font(theme.N("sz_details_title"), "bold"),
            text_color=theme.C("section_title"),
        ).pack(anchor="w", padx=16, pady=(14, 4))

        options = [
            b["name"]
            + _t("restore_option_sections", count=len(b["sections"]))
            + (_t("restore_option_langlist") if b["json_path"] else "")
            for b in backups
        ]

        def selected() -> dict[str, Any]:
            value = combo.get()
            return backups[options.index(value)] if value in options else backups[0]

        preview = ctk.CTkTextbox(
            dialog,
            wrap="word",
            font=theme.mono_font(13),
            fg_color=theme.S("detail_bg"),
            text_color=theme.C("text"),
        )
        preview.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        preview.configure(state="disabled")

        def render_preview(_value: str | None = None) -> None:
            b = selected()
            lines = [_t("dlg_restore_file", path=b["reg_path"]), "", _t("dlg_restore_sections")]
            if b["sections"]:
                lines += [f"  • {s}" for s in b["sections"][:12]]
                if len(b["sections"]) > 12:
                    lines.append(_t("dlg_restore_more_sections", count=len(b["sections"]) - 12))
            else:
                lines.append(_t("dlg_restore_no_sections"))
            lines.append("")
            lines.append(
                _t("dlg_restore_langlist_yes")
                if b["json_path"]
                else _t("dlg_restore_langlist_no")
            )
            if b["has_hku"] and not is_admin():
                lines.append(
                    _t("dlg_restore_uac_note")
                )
            preview.configure(state="normal")
            preview.delete("1.0", "end")
            preview.insert("1.0", "\n".join(lines))
            preview.configure(state="disabled")

        combo = ctk.CTkComboBox(
            dialog,
            values=options,
            state="readonly",
            width=430,
            fg_color=theme.S("menu_bg"),
            button_color=theme.S("menu_btn"),
            button_hover_color=theme.S("menu_btn_hover"),
            text_color=theme.C("menu_text"),
            dropdown_fg_color=theme.S("menu_bg"),
            dropdown_hover_color=theme.S("menu_btn_hover"),
            dropdown_text_color=theme.C("menu_text"),
            command=render_preview,
        )
        combo.set(options[0])
        combo.pack(anchor="w", padx=16, pady=(0, 8))
        render_preview()

        btns = ctk.CTkFrame(dialog, fg_color="transparent")
        btns.pack(fill="x", padx=16, pady=(0, 14))

        def do_restore() -> None:
            b = selected()
            confirm = (
                _t("dlg_restore_confirm", path=b["reg_path"])
            )
            if b["json_path"]:
                confirm += _t("dlg_restore_confirm_langlist")
            if b["has_hku"] and not is_admin():
                confirm += _t("dlg_restore_confirm_uac")
            confirm += _t("dlg_restore_confirm_overwrite")
            if not messagebox.askyesno(_t("dlg_restore_confirm_title"), confirm, parent=dialog):
                return
            dialog.destroy()
            self._start_restore(b)

        ctk.CTkButton(
            btns,
            text=_t("btn_restore_confirm"),
            fg_color=theme.S("safe_bg"),
            hover_color=theme.S("safe_hover"),
            border_width=theme.N("bw_btn"),
            border_color=theme.S("safe_border"),
            text_color=theme.C("safe_text"),
            corner_radius=theme.N("radius_btn"),
            height=40,
            font=theme.body_font(theme.N("sz_restart"), "bold"),
            command=do_restore,
        ).pack(side="left", expand=True, fill="x", padx=(0, 6))
        ctk.CTkButton(
            btns,
            text=_t("btn_cancel"),
            fg_color=theme.S("menu_btn"),
            hover_color=theme.S("menu_btn_hover"),
            text_color=theme.C("menu_text"),
            corner_radius=theme.N("radius_btn"),
            height=40,
            command=dialog.destroy,
        ).pack(side="left", expand=True, fill="x", padx=(6, 0))

    def _start_restore(self, backup: dict[str, Any]) -> None:
        """Выполнить восстановление в фоновом потоке."""
        self._set_status(_t("status_restoring"))
        if self.restore_button:
            self.restore_button.configure(
                state="disabled", text=_t("btn_restore_busy")
            )
        if self.scan_button:
            self.scan_button.configure(state="disabled")
        if self.delete_button:
            self.delete_button.configure(state="disabled")

        reg_path = backup["reg_path"]
        json_path = backup["json_path"]

        def _restore_thread() -> None:
            try:
                result = restore_backup(reg_path, json_path)
                self._post(self._after_restore, result, backup)
            except Exception as exc:
                self._post(self._after_restore_error, str(exc))

        threading.Thread(target=_restore_thread, daemon=True).start()

    def _after_restore(
        self, result: dict[str, Any], backup: dict[str, Any]
    ) -> None:
        """Обработка результата восстановления."""
        self._enable_after_restore()
        lang_ok = result.get("langlist_restored")
        if result.get("ok") and lang_ok is not False:
            msg = _t("dlg_restore_done")
            if result.get("elevated"):
                msg += _t("dlg_restore_done_uac")
            if lang_ok:
                msg += _t("dlg_restore_done_langlist")
            msg += _t("dlg_restore_advice")
            self._set_status(_t("status_restore_done"))
            logger.info(
                "Восстановление из %s: успех", backup.get("reg_path", "?")
            )
            messagebox.showinfo(_t("dlg_result_title"), msg)
        else:
            detail = result.get("detail") or (
                _t("dlg_restore_failed_langlist")
                if lang_ok is False
                else _t("dlg_restore_see_log")
            )
            self._set_status(_t("status_restore_failed"))
            logger.warning("Восстановление не удалось: %s", result)
            messagebox.showerror(
                _t("dlg_error_title"), _t("dlg_restore_failed", detail=detail)
            )
        # Пересканируем, чтобы панель отразила актуальное состояние
        self._refresh_after_delete()

    def _after_restore_error(self, error_msg: str) -> None:
        """Обработка ошибки восстановления."""
        self._enable_after_restore()
        self._set_status(_t("status_restore_error"))
        messagebox.showerror(
            _t("dlg_error_title"),
            _t("dlg_restore_error", error=error_msg),
        )

    def _enable_after_restore(self) -> None:
        """Вернуть кнопкам рабочее состояние после восстановления."""
        if self.restore_button:
            self.restore_button.configure(
                state="normal", text=_t("btn_restore")
            )
        if self.scan_button:
            self.scan_button.configure(state="normal")
        if self.delete_button:
            self.delete_button.configure(state="normal")

    def _refresh_after_delete(self) -> None:
        """Пересканировать в фоновом потоке и обновить данные (без модалок)."""
        self._set_stage("busy")
        if self.scan_button:
            self.scan_button.configure(state="disabled", text=_t("btn_rescan_busy"))
        self._set_status(_t("status_refreshing"))

        def _refresh_thread() -> None:
            try:
                layouts = scan_keyboard_layouts()
                self._post(self._apply_scan_results, layouts)
                self._post(self._finalize_refresh, layouts)
            except Exception as exc:
                self._post(self._refresh_error, str(exc))

        threading.Thread(target=_refresh_thread, daemon=True).start()

    def _finalize_refresh(self, layouts: dict[str, list[dict[str, str]]]) -> None:
        """Завершение обновления: кнопки, статус, сброс выбора."""
        if self.scan_button:
            self.scan_button.configure(
                text=_t("btn_rescan"), state="normal"
            )
        self._scan_done = True
        # Итоговое сообщение: удалённая раскладка исчезла из списка?
        if self.current_layout_klid not in layouts:
            self._set_status(_t("status_layout_removed"))
        else:
            self._set_status(_t("status_refreshed"))

    def _refresh_error(self, error_msg: str) -> None:
        """Ошибка пересканирования — больше не «тихая»: лог и статус."""
        if self.scan_button:
            self.scan_button.configure(
                text=_t("btn_rescan"), state="normal"
            )
        self._scan_done = True
        self._set_status(_t("status_refresh_error"))
        self._set_stage("choose")
        logger.warning("Пересканирование после удаления не удалось: %s", error_msg)


    # ------------------------------------------------------------------
    # Перезапуск как администратор
    # ------------------------------------------------------------------

    def _restart_as_admin(self) -> None:
        """Перезапустить приложение с правами администратора через UAC."""
        if not self.admin_btn:
            return

        # Защита от двойного нажатия — если процесс уже идёт, игнорируем.
        if getattr(self, "_is_elevating", False):
            return
        self._is_elevating = True

        pid = os.getpid()
        logger.info("[PID=%d] Начат процесс перезапуска от имени администратора", pid)

        self.admin_btn.configure(
            state="disabled", text=_t("btn_restart_admin_busy")
        )

        result_code = None
        try:
            # Определяем параметры для elevated процесса
            # ВАЖНО: передаём --sandbox если включён, чтобы новый процесс
            # тоже работал в песочнице
            sandbox_flag = " --sandbox" if SANDBOX_MODE else ""

            if getattr(sys, "frozen", False):
                # Сборка exe: перезапускаем сам исполняемый файл
                target = sys.executable
                params = sandbox_flag
            else:
                # Явное оборачивание пути в двойные кавычки — корректно
                # обрабатывает пробелы и спецсимволы в пути к скрипту.
                target = sys.executable
                params = f'"{os.path.abspath(__file__)}"{sandbox_flag}'

            logger.info("[PID=%d] ShellExecuteW: target=%s, params=%s", pid, target, params)

            # ShellExecuteW возвращает >32 при успехе и код ошибки (<=32)
            # при неудаче — например, если пользователь отклонил запрос UAC.
            result_code = ctypes.windll.shell32.ShellExecuteW(
                None,
                "runas",
                target,
                params,
                None,
                1,
            )

            logger.info("[PID=%d] ShellExecuteW вернул: %d", pid, result_code)

            if result_code <= 32:
                # Не закрываем приложение — сообщаем и продолжаем работу
                logger.warning("[PID=%d] ShellExecuteW не удался (код=%d)", pid, result_code)
                self._is_elevating = False
                self.admin_btn.configure(
                    state="normal", text=_t("btn_restart_admin")
                )
                self._set_status(_t("status_elevation_cancelled"))
                self._show_elevation_error(result_code)
                return

            # Запуск успешен — новый процесс уже запущен.
            # Закрываем мьютекс и завершаем работу.
            logger.info("[PID=%d] Запуск успешен, закрываем мьютекс и выходим", pid)
            self._release_mutex_and_exit()

        except Exception as exc:
            logger.error("[PID=%d] Ошибка при перезапуске: %s", pid, exc)
            self._is_elevating = False
            self.admin_btn.configure(
                state="normal", text=_t("btn_restart_admin")
            )
            self._set_status(_t("status_elevation_error"))
            messagebox.showerror(
                _t("dlg_error_title"),
                _t("dlg_elevation_error", error=exc),
            )

    def _release_mutex_and_exit(self) -> None:
        """
        Закрыть мьютекс и завершить процесс.

        Вынесено в отдельный метод для гарантированного закрытия мьютекса
        при любом сценарии выхода.
        """
        global _mutex_handle
        pid = os.getpid()

        logger.info("[PID=%d] Закрываем мьютекс и завершаем процесс...", pid)

        try:
            # Закрываем дескриптор мьютекса
            if _mutex_handle and _mutex_handle != 0:
                ctypes.windll.kernel32.CloseHandle(_mutex_handle)
                _mutex_handle = None
                logger.info("[PID=%d] Мьютекс закрыт", pid)
        except Exception as exc:
            logger.error("[PID=%d] Ошибка при закрытии мьютекса: %s", pid, exc)

        try:
            # Уничтожаем GUI
            self.destroy()
            logger.info("[PID=%d] GUI уничтожен", pid)
        except Exception as exc:
            logger.error("[PID=%d] Ошибка при уничтожении GUI: %s", pid, exc)

        # Чистое завершение
        logger.info("[PID=%d] Завершаем процесс (sys.exit(0))", pid)
        sys.exit(0)

    @staticmethod
    def _show_elevation_error(result_code: int) -> None:
        """Показать понятное сообщение об ошибке UAC по коду ShellExecuteW."""
        _ERROR_MESSAGES: dict[int, str] = {
            2: "dlg_elevation_error_file_not_found",
            3: "dlg_elevation_error_path_not_found",
            5: "dlg_elevation_access_denied",
            11: "dlg_elevation_error_association",
            31: "dlg_elevation_no_assoc",
            1136: "dlg_elevation_error_bad_netpath",
        }
        if result_code in _ERROR_MESSAGES:
            key = _ERROR_MESSAGES[result_code]
            msg = _t(key)
        else:
            msg = _t("dlg_elevation_generic_error", code=result_code)
        messagebox.showwarning(
            _t("dlg_elevation_title"),
            msg,
        )


    # ------------------------------------------------------------------
    # Утилиты
    # ------------------------------------------------------------------

    def _set_status(self, message: str) -> None:
        """Установить текст статус-бара."""
        if self.status_bar:
            self.status_bar.set_status(message)
        elif self.status_label:
            self.status_label.configure(text=message)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def main() -> None:
    """Запустить приложение."""
    args = parse_args()

    # Активируем песочницу если нужно
    if args.sandbox or SANDBOX_MODE:
        import cleaner
        import scanner

        scanner.activate_sandbox()
        cleaner.activate_sandbox()

    # Захватываем мьютекс — защита от параллельных запусков
    global _mutex_handle, _last_error
    _mutex_handle, _last_error = _acquire_mutex()
    logger.info("Итог: handle=%s, error=%d", _mutex_handle, _last_error)

    if _last_error == 183:
        # Старый процесс так и не освободил mutex за отведённое время.
        logger.error("Не удалось захватить мьютекс за 60 секунд")
        if sys.stderr:
            sys.stderr.write(
                "Ошибка: приложение уже запущено.\n"
                "Если это не так, перезагрузите компьютер.\n"
            )
        else:
            try:
                from tkinter import messagebox
                messagebox.showerror(
                    "Ошибка",
                    "Приложение уже запущено.\n\n"
                    "Если это не так, перезагрузите компьютер.",
                )
            except Exception:
                pass
        sys.exit(1)

    if _mutex_handle == 0 or _mutex_handle is None:
        logger.error("CreateMutexW вернул NULL (error=%d)", _last_error)
        if sys.stderr:
            sys.stderr.write(
                "Ошибка: не удалось создать mutex (GetLastError=%d).\n" % _last_error
            )
        else:
            try:
                from tkinter import messagebox
                messagebox.showerror(
                    "Ошибка",
                    "Не удалось создать mutex (GetLastError=%d)." % _last_error,
                )
            except Exception:
                pass
        sys.exit(1)

    logger.info("Мьютекс успешно захвачен, приложение запущено")
    get_logger().info("Приложение запущено (журнал: %s)", get_app_dir() / "layout_cleaner.log")
    app = KeyboardLayoutCleaner()
    app.mainloop()


if __name__ == "__main__":
    main()
