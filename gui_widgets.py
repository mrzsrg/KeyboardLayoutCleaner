"""
gui_widgets.py — Переиспользуемые GUI-компоненты для Keyboard Layout Cleaner.

Выносит повторяющиеся виджеты из main.py в отдельные классы для:
- Упрощения тестирования
- Повторного использования
- Снижения размера main.py

Все цвета, шрифты и размеры берутся из активной темы (ui_theme), поэтому
компоненты автоматически поддерживают оба интерфейса: classic и terminal.
"""

import logging
from collections.abc import Callable
from typing import Any

import customtkinter as ctk

import i18n
import ui_theme as theme

logger = logging.getLogger("layout_cleaner")


def _t(key: str, **kwargs) -> str:
    """Локализованная строка по ключу (format без исключений)."""
    return i18n.fmt(key, **kwargs)


class TreeviewLayoutList(ctk.CTkFrame):
    """
    Виртуализированный список раскладок на базе ttk.Treeview.

    Использует встроенный ttk.Treeview вместо набора CTkButton, что обеспечивает:
    - Виртуализацию: создаются только видимые строки
    - Быстрое обновление: O(1) вместо O(n) при перерисовке
    - Меньше памяти: нет отдельного виджета на каждую раскладку

    Используется как альтернатива стандартному списку кнопок.
    Активируется через config.treeview_enabled = True.
    """

    def __init__(self, master: Any, on_layout_clicked: Callable[[str], None]) -> None:
        super().__init__(master, corner_radius=0, fg_color="transparent")
        self.on_layout_clicked = on_layout_clicked
        self._current_klid: str | None = None
        self._layouts: dict[str, list[dict]] = {}

        # Создаём Treeview
        import tkinter.ttk as ttk

        self._tree = ttk.Treeview(
            self,
            columns=("name", "type"),
            show="headings",
            selectmode="browse",
        )
        self._tree.heading("name", text=_t("list_col_name"))
        self._tree.heading("type", text=_t("list_col_type"))
        self._tree.column("name", width=300, minwidth=200, anchor="w")
        self._tree.column("type", width=100, minwidth=60, anchor="center")

        # Скроллбар
        self._scrollbar = ttk.Scrollbar(
            self, orient="vertical", command=self._tree.yview
        )
        self._tree.configure(yscrollcommand=self._scrollbar.set)

        # Связываем двойной клик и выбор
        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        self._tree.bind("<Double-1>", self._on_select)

        # Распаковка
        self._tree.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        self._scrollbar.pack(side="right", fill="y", padx=(0, 4), pady=4)

    def set_data(
        self, layouts: dict[str, list[dict]], selected_klid: str | None = None
    ) -> None:
        """Обновить данные списка. Очистить и заполнить Treeview."""
        self._layouts = layouts or {}
        self._current_klid = selected_klid

        # Очистить
        for item in self._tree.get_children():
            self._tree.delete(item)

        # Заполнить (только видимые строки создаются Treeview автоматически)
        for klid in sorted(self._layouts.keys()):
            count = len(self._layouts[klid])
            name = _build_layout_name_from_klid(klid)
            item_id = self._tree.insert("", "end", values=(name, f"x{count}"))

            # Помечаем выбранную строку
            if klid == selected_klid:
                self._tree.selection_set(item_id)
                self._tree.see(item_id)

    def get_selected_klid(self) -> str | None:
        """Возвращает KLID выбранной строки или None."""
        selected = self._tree.selection()
        if selected:
            item = selected[0]
            values = self._tree.item(item, "values")
            if values:
                # Находим KLID по имени
                for klid, _layouts in self._layouts.items():
                    name = _build_layout_name_from_klid(klid)
                    if name == values[0]:
                        self._current_klid = klid
                        return klid
        return self._current_klid

    def _on_select(self, event: Any) -> None:
        """Обработать выбор строки."""
        klid = self.get_selected_klid()
        if klid and klid in self._layouts:
            self.on_layout_clicked(klid)


def _build_layout_name_from_klid(klid: str) -> str:
    """Получить человеко-читаемое имя раскладки из KLID (упрощённая версия)."""
    parts = klid.split(":")
    if len(parts) == 2:
        return f"KLID: {klid}"
    return f"KLID: {klid}"


# ---------------------------------------------------------------------------
# Системный трей (notification area icon)
# ---------------------------------------------------------------------------
class SystemTray:
    """
    Иконка в системном трее с контекстным меню.

    Позволяет:
    - Сворачивать приложение в трей вместо закрытия
    - Быстро показывать/скрывать окно
    - Выходить через контекстное меню

    Зависимости: pystray, Pillow (установить: pip install pystray pillow)
    Активируется через config.system_tray_enabled = True.
    """

    def __init__(self, app) -> None:
        """
        Инициализировать системный трей.

        Args:
            app: экземпляр KeyboardLayoutCleaner (для show/hide/quit)
        """
        self.app = app
        self._icon = None
        self._pystray = None

        try:
            import pystray
            from PIL import Image

            self._pystray = pystray
            self._Image = Image
        except ImportError:
            logger.warning(
                "pystray или Pillow не установлены — системный трей недоступен. "
                "Установите: pip install pystray pillow"
            )
            return

        # Создаём иконку (16x16 пикселей, серый квадрат)
        icon = self._create_default_icon()

        # Меню трей
        menu = pystray.Menu(
            pystray.MenuItem(
                _t("tray_show"),
                self._on_show,
                default=True,
            ),
            pystray.MenuItem(_t("tray_hide"), self._on_hide),
            pystray.MenuItem(_t("tray_restart"), self._on_restart),
            pystray.MenuItem(_t("tray_quit"), self._on_quit),
        )

        self._icon = self._pystray.Icon(
            "keyboard-layout-cleaner",
            icon,
            menu=menu,
            title="Keyboard Layout Cleaner",
        )

    def _create_default_icon(self):
        """Создать простую иконку по умолчанию (белый квадрат)."""
        try:
            import os

            icon_path = os.path.join(os.path.dirname(__file__), "assets", "icon.ico")
            if os.path.exists(icon_path):
                return self._Image.open(icon_path).resize((16, 16))
        except OSError:
            pass
        # Fallback: белый квадрат
        return self._Image.new("RGB", (16, 16), color="white")

    def _on_show(self, icon=None, item=None):
        """Показать главное окно."""
        self.app.after(0, self.app._show_window)

    def _on_hide(self, icon=None, item=None):
        """Скрыть главное окно."""
        self.app.after(0, self.app._hide_window)

    def _on_restart(self, icon=None, item=None):
        """Перезапустить как администратор."""
        self.app.after(0, self.app._restart_as_admin)

    def _on_quit(self, icon=None, item=None):
        """Закрыть приложение."""
        self.app.after(0, lambda: self.app.quit_from_tray())

    def run(self) -> None:
        """Запустить трей-иконку (блокирующий вызов)."""
        if self._icon:
            self._icon.run()

    def stop(self) -> None:
        """Остановить трей-иконку."""
        if self._icon:
            self._icon.stop()
            self._icon = None


class AdminBanner(ctk.CTkFrame):
    """
    Баннер статуса администратора с кнопкой перезапуска.

    Строка 1: статус прав + «Перезапустить как Админ» + язык интерфейса.
    Строка 2: выбор интерфейса (classic/terminal) и яркости шрифтов.
    """

    def __init__(
        self,
        master: Any,
        on_restart_admin: Callable[[], None],
        on_lang_change: Callable[[str], None],
        on_toggle_theme: Callable[[], None],
        on_brightness_change: Callable[[str], None],
    ) -> None:
        super().__init__(
            master, corner_radius=theme.N("radius_frame"), fg_color="transparent"
        )

        self.on_restart_admin = on_restart_admin
        self.on_lang_change = on_lang_change
        self.on_toggle_theme = on_toggle_theme
        self.on_brightness_change = on_brightness_change

        # --- Строка 1: статус + перезапуск + язык ---------------------------
        row1 = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        row1.pack(fill="x")

        # Иконка
        self.icon_label = ctk.CTkLabel(
            row1, text="", font=ctk.CTkFont(size=22, weight="bold")
        )
        self.icon_label.pack(side="left", padx=(0, 10))

        # Текст статуса
        self.status_label = ctk.CTkLabel(
            row1,
            text=_t("banner_checking"),
            font=theme.body_font(theme.N("sz_banner"), "bold"),
        )
        self.status_label.pack(side="left", padx=(0, 14))

        # Кнопка "Перезапустить как Админ"
        self.restart_btn = ctk.CTkButton(
            row1,
            text=_t("btn_restart_admin"),
            command=self._on_restart,
            fg_color=theme.S("restart_bg"),
            hover_color=theme.S("restart_hover"),
            border_width=theme.N("bw_btn"),
            border_color=theme.S("restart_border"),
            text_color=theme.C("restart_text"),
            corner_radius=theme.N("radius_btn"),
            height=38,
            width=320,
            font=theme.body_font(theme.N("sz_restart"), "bold"),
            state="disabled",
        )
        self.restart_btn.pack(side="left")

        # Переключатель языка
        self.lang_menu = self._make_menu(
            row1,
            values=list(i18n.DISPLAY_NAMES.values()),
            width=140,
            command=self.on_lang_change,
        )
        self.lang_menu.set(i18n.DISPLAY_NAMES.get(i18n.current_lang, "English"))
        self.lang_menu.pack(side="right")

        # --- Строка 2: интерфейс + яркость шрифтов ---------------------------
        row2 = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        row2.pack(fill="x", pady=(6, 0))

        # Кнопка смены интерфейса (подпись = куда переключаемся)
        target = "terminal" if theme.current_theme() == "classic" else "classic"
        self.ui_mode_btn = ctk.CTkButton(
            row2,
            text=_t(f"ui_mode_btn_{target}"),
            command=self.on_toggle_theme,
            width=190,
            height=30,
            fg_color=theme.S("menu_bg"),
            hover_color=theme.S("menu_btn_hover"),
            border_width=theme.N("bw_btn"),
            border_color=theme.S("menu_border"),
            text_color=theme.C("menu_text"),
            corner_radius=theme.N("radius_btn"),
            font=theme.body_font(theme.N("sz_tool")),
        )
        self.ui_mode_btn.pack(side="left")

        # Меню яркости шрифтов: код -> локализованная подпись
        self._brightness_labels: dict[str, str] = {
            code: _t(f"brightness_{code}") for code in theme.BRIGHTNESS_FACTORS
        }
        self.brightness_menu = self._make_menu(
            row2,
            values=list(self._brightness_labels.values()),
            width=210,
            # наружу отдаём КОД яркости, а не подпись
            command=lambda label: self.on_brightness_change(self._label_to_code(label)),
        )
        self.brightness_menu.set(self._brightness_labels[theme.current_brightness()])
        self.brightness_menu.pack(side="left", padx=(10, 0))

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _make_menu(
        self, master: Any, values: list[str], width: int, command: Callable
    ) -> ctk.CTkOptionMenu:
        """Выпадающее меню в стиле активной темы."""
        return ctk.CTkOptionMenu(
            master,
            values=values,
            width=width,
            height=30,
            fg_color=theme.S("menu_bg"),
            button_color=theme.S("menu_btn"),
            button_hover_color=theme.S("menu_btn_hover"),
            text_color=theme.C("menu_text"),
            dropdown_fg_color=theme.S("menu_bg"),
            dropdown_hover_color=theme.S("menu_btn_hover"),
            dropdown_text_color=theme.C("menu_text"),
            corner_radius=theme.N("radius_menu"),
            font=theme.body_font(theme.N("sz_menu")),
            dropdown_font=theme.body_font(theme.N("sz_menu")),
            command=command,
        )

    def _label_to_code(self, label: str) -> str:
        """Обратное преобразование подписи яркости в код уровня."""
        for code, lab in self._brightness_labels.items():
            if lab == label:
                return code
        return theme.current_brightness()

    def apply_language(self) -> None:
        """Обновить подписи переключателей после смены языка интерфейса."""
        target = "terminal" if theme.current_theme() == "classic" else "classic"
        self.ui_mode_btn.configure(text=_t(f"ui_mode_btn_{target}"))
        self._brightness_labels = {
            code: _t(f"brightness_{code}") for code in theme.BRIGHTNESS_FACTORS
        }
        self.brightness_menu.configure(values=list(self._brightness_labels.values()))
        self.brightness_menu.set(self._brightness_labels[theme.current_brightness()])
        self.lang_menu.set(i18n.DISPLAY_NAMES.get(i18n.current_lang, "English"))

    def _on_restart(self) -> None:
        """Обработчик нажатия кнопки перезапуска."""
        self.restart_btn.configure(state="disabled", text=_t("btn_restart_admin_busy"))
        self.on_restart_admin()

    def update_status(self, is_admin: bool) -> None:
        """Обновить отображение статуса администратора."""
        if is_admin:
            self.icon_label.configure(text="🛡️")
            self.status_label.configure(
                text=_t("banner_admin_ok"), text_color=theme.C("ok")
            )
            self.restart_btn.pack_forget()
        else:
            self.icon_label.configure(text="⚠️")
            self.status_label.configure(
                text=_t("banner_admin_no"), text_color=theme.C("err")
            )
            self.restart_btn.pack(side="left")
            self.restart_btn.configure(state="normal", text=_t("btn_restart_admin"))

    def set_language(self, lang_code: str) -> None:
        """Установить язык интерфейса."""
        name = i18n.DISPLAY_NAMES.get(lang_code, "English")
        self.lang_menu.set(name)

    def disable_restart(self) -> None:
        """Отключить кнопку перезапуска."""
        self.restart_btn.configure(state="disabled")

    def enable_restart(self) -> None:
        """Включить кнопку перезапуска."""
        self.restart_btn.configure(state="normal", text=_t("btn_restart_admin"))


class StatusBar(ctk.CTkFrame):
    """
    Статус-бар с прогресс-индикатором.

    Отображает текущий статус операции и анимированный прогресс-бар
    для длительных операций.
    """

    def __init__(self, master: Any) -> None:
        super().__init__(
            master,
            corner_radius=0,
            fg_color=theme.S("statusbar_bg"),
            height=32,
        )

        # Текст статуса
        self.label = ctk.CTkLabel(
            self,
            text=_t("status_ready"),
            font=theme.body_font(theme.N("sz_status")),
            text_color=theme.C("status_text"),
        )
        self.label.pack(side="left", padx=10, pady=4)

        # Прогресс-бар (indeterminate mode)
        self.progress_bar = ctk.CTkProgressBar(
            self,
            mode="indeterminate",
            progress_color=theme.S("progress"),
            height=4,
        )
        self.progress_bar.pack(side="bottom", fill="x", padx=0, pady=0)
        self.progress_bar.set(0)
        self._progress_active = False

    def set_status(self, text: str, color: str | None = None) -> None:
        """Установить текст статуса (цвет по умолчанию — из темы)."""
        self.label.configure(text=text, text_color=color or theme.C("status_text"))

    def progress_start(self) -> None:
        """Запустить анимированный прогресс-бар."""
        self._progress_active = True
        self.progress_bar.set(0)
        self.progress_bar.start()

    def progress_stop(self) -> None:
        """Остановить прогресс-бар."""
        self._progress_active = False
        self.progress_bar.stop()
        self.progress_bar.set(0)

    @property
    def is_progress_active(self) -> bool:
        """Активен ли прогресс-бар."""
        return self._progress_active


class ActionButtons(ctk.CTkFrame):
    """
    Панель кнопок действий: удаление, восстановление, блокировка синхронизации.
    """

    def __init__(
        self,
        master: Any,
        on_delete: Callable[[], None],
        on_restore: Callable[[], None],
        on_toggle_sync: Callable[[], None],
    ) -> None:
        super().__init__(
            master, corner_radius=theme.N("radius_frame"), fg_color="transparent"
        )

        self.on_delete = on_delete
        self.on_restore = on_restore
        self.on_toggle_sync = on_toggle_sync

        # Кнопка удаления
        self.delete_btn = ctk.CTkButton(
            self,
            text=_t("btn_delete"),
            fg_color=theme.S("danger_bg"),
            hover_color=theme.S("danger_hover"),
            border_width=theme.N("bw_btn"),
            border_color=theme.S("danger_border"),
            text_color=theme.C("danger_text"),
            corner_radius=theme.N("radius_btn"),
            height=theme.N("h_delete"),
            font=theme.body_font(theme.N("sz_delete"), "bold"),
            state="disabled",
            command=self._on_delete,
        )
        self.delete_btn.pack(fill="x")

        # Галочка блокировки облачной синхронизации
        self.block_sync_checkbox = ctk.CTkCheckBox(
            self,
            text=_t("chk_block_cloud_sync"),
            font=theme.body_font(theme.N("sz_check")),
            fg_color=theme.S("check_fg"),
            hover_color=theme.S("check_hover"),
            border_color=theme.S("check_border"),
            text_color=theme.C("check_text"),
            corner_radius=theme.N("radius_btn"),
            command=self._on_toggle_sync,
        )
        self.block_sync_checkbox.pack(fill="x", pady=(6, 0))
        self.block_sync_checkbox.select()

        # Кнопка восстановления
        self.restore_btn = ctk.CTkButton(
            self,
            text=_t("btn_restore"),
            fg_color=theme.S("safe_bg"),
            hover_color=theme.S("safe_hover"),
            border_width=theme.N("bw_btn"),
            border_color=theme.S("safe_border"),
            text_color=theme.C("safe_text"),
            corner_radius=theme.N("radius_btn"),
            height=theme.N("h_restore"),
            font=theme.body_font(theme.N("sz_restore")),
            command=self._on_restore,
        )
        self.restore_btn.pack(fill="x", pady=(6, 0))

    def _on_delete(self) -> None:
        """Обработчик нажатия кнопки удаления."""
        self.delete_btn.configure(state="disabled", text=_t("btn_delete_busy"))
        self.on_delete()

    def _on_restore(self) -> None:
        """Обработчик нажатия кнопки восстановления."""
        self.on_restore()

    def _on_toggle_sync(self) -> None:
        """Обработчик переключения галочки блокировки."""
        self.on_toggle_sync()

    def set_delete_enabled(self, enabled: bool) -> None:
        """Включить/выключить кнопку удаления."""
        self.delete_btn.configure(
            state="normal" if enabled else "disabled",
            text=_t("btn_delete"),
        )

    def set_delete_busy(self, busy: bool) -> None:
        """Установить состояние занятости кнопки удаления."""
        self.delete_btn.configure(
            state="disabled",
            text=_t("btn_delete_busy" if busy else "btn_delete"),
        )

    def set_restore_enabled(self, enabled: bool) -> None:
        """Включить/выключить кнопку восстановления."""
        self.restore_btn.configure(state="normal" if enabled else "disabled")

    def set_block_sync(self, enabled: bool) -> None:
        """Установить состояние галочки блокировки синхронизации."""
        if enabled:
            self.block_sync_checkbox.select()
        else:
            self.block_sync_checkbox.deselect()
