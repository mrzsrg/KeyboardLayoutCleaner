"""
ui_theme.py — темы оформления и яркость шрифтов Keyboard Layout Cleaner.

Два сменных интерфейса (кнопка в шапке приложения):
  classic  — исходная тема CustomTkinter: скруглённые панели, синие акценты;
  terminal — «терминальный» стиль: чёрный фон, моноширинный шрифт, плоские
             рамки, зелёные/циановые акценты (как в консоли).

Яркость шрифтов — множитель, применяемый ко всем ТЕКСТОВЫМ цветам
(dim 0.72 / normal 1.0 / bright 1.32 / ultra 1.65). Фон и рамки не
масштабируются, поэтому контраст сохраняется на любой яркости.

Выбор интерфейса и яркости сохраняется в
%LOCALAPPDATA%\\KeyboardLayoutCleaner\\ui_prefs.json
(или в профиле пользователя, если LOCALAPPDATA недоступен).

Токены темы:
  S(token) — структурный цвет/параметр (фон, рамки, радиусы — без яркости);
  C(token) — цвет ТЕКСТА (масштабируется текущей яркостью шрифтов);
  P(kind)  — пара цветов (база, акцент) для «пульсирующей» подсветки;
  N(token) — числовой параметр (радиусы, высоты, толщина рамок);
  F(token) — имя семейства шрифтов.
"""

import json
import logging
import os
from pathlib import Path

import customtkinter as ctk

logger = logging.getLogger("layout_cleaner")
if not logger.handlers:
    logger.addHandler(logging.NullHandler())

# ---------------------------------------------------------------------------
# Публичные константы
# ---------------------------------------------------------------------------

THEME_NAMES: tuple[str, ...] = ("classic", "terminal")
DEFAULT_THEME = "terminal"

# Код яркости -> множитель текстовых цветов
BRIGHTNESS_FACTORS: dict[str, float] = {
    "dim": 0.72,
    "normal": 1.0,
    "bright": 1.32,
    "ultra": 1.65,
}
DEFAULT_BRIGHTNESS = "normal"

# Токены, означающие цвет шрифта: к ним применяется яркость
TEXT_TOKENS = frozenset({
    "text", "text_dim", "section_title", "guide", "status_text",
    "ok", "err", "hint_warn", "row_text", "row_text_selected", "backup",
    "accent_text", "danger_text", "safe_text", "restart_text",
    "menu_text", "check_text",
    "tag_h1", "tag_sub", "tag_legend", "tag_dim", "tag_path", "tag_ok",
    "tag_miss", "tag_admin", "tag_blocked", "tag_warn", "tag_val",
    "tag_note", "tag_item",
})

# ---------------------------------------------------------------------------
# Определения тем
# ---------------------------------------------------------------------------

_CLASSIC: dict = {
    # --- окно -------------------------------------------------------------
    "window_size": "900x820",
    "min_size": (820, 700),
    "bg": "#242424",
    # --- панели / списки ----------------------------------------------------
    "panel": "gray20",
    "panel_border": "gray40",
    "bw_panel": 0,
    "list_bg": "gray13",
    "list_border": "gray40",
    "bw_list": 2,
    "h_list": 120,
    "detail_bg": "gray17",
    "statusbar_bg": "gray15",
    # --- строки списка ------------------------------------------------------
    "row": "gray17",
    "row_hover": "gray28",
    "row_selected": "#1f6aa5",
    "row_selected_hover": "#3b9ae1",
    # --- кнопки -------------------------------------------------------------
    "accent_bg": "#1f6aa5",
    "accent_hover": "#3b9ae1",
    "accent_border": "#1f6aa5",
    "bw_scan": 0,
    "danger_bg": "#c0392b",
    "danger_hover": "#e74c3c",
    "danger_border": "#c0392b",
    "safe_bg": "#2c5f8a",
    "safe_hover": "#3b76a8",
    "safe_border": "#2c5f8a",
    "restart_bg": "#e74c3c",
    "restart_hover": "#c0392b",
    "restart_border": "#e74c3c",
    "bw_btn": 0,
    # --- меню / чекбокс -----------------------------------------------------
    "menu_bg": "#1f6aa5",
    "menu_btn": "#3a86c8",
    "menu_btn_hover": "#4f97d6",
    "menu_border": "#1f6aa5",
    "check_fg": "#1f6aa5",
    "check_hover": "#3b9ae1",
    "check_border": "gray60",
    # --- прогресс -----------------------------------------------------------
    "progress": "#3b9ae1",
    # --- геометрия ------------------------------------------------------------
    "radius_frame": 10,
    "radius_btn": 6,
    "radius_row": 8,
    "radius_menu": 6,
    "radius_text": 8,
    "h_scan": 42,
    "h_row": 44,
    "h_delete": 46,
    "h_restore": 36,
    # --- шрифты ---------------------------------------------------------------
    "font_ui": "Segoe UI",
    "font_mono": "Consolas",
    "mono_ui": False,  # False — обычный пропорциональный шрифт интерфейса
    "sz_banner": 17,
    "sz_restart": 15,
    "sz_hint": 14,
    "sz_guide": 16,
    "sz_list_title": 16,
    "sz_row": 16,
    "sz_details_title": 16,
    "sz_detail_text": 16,
    "sz_scan": 16,
    "sz_delete": 16,
    "sz_restore": 14,
    "sz_check": 13,
    "sz_status": 13,
    "sz_backup": 14,
    "sz_placeholder": 15,
    "sz_menu": 13,
    "sz_tool": 13,

    # --- текстовые цвета (масштабируются яркостью) -----------------------------
    "text": "#eaf2ff",
    "text_dim": "#9a9a9a",
    "section_title": "#f2f2f2",
    "guide": "#f1c40f",
    "status_text": "#8f8f8f",
    "ok": "#2ecc71",
    "err": "#e74c3c",
    "hint_warn": "#e67e22",
    "row_text": "#eaf2ff",
    "row_text_selected": "#ffffff",
    "backup": "#2ecc71",
    "accent_text": "#ffffff",
    "danger_text": "#ffffff",
    "safe_text": "#ffffff",
    "restart_text": "#ffffff",
    "menu_text": "#eaf2ff",
    "check_text": "#d0d0d0",
    # --- цвета тегов rich-text панели деталей -----------------------------------
    "tag_h1": "#ffffff",
    "tag_sub": "#b3b3b3",     # gray70
    "tag_legend": "#949494",  # gray58
    "tag_dim": "#b8b8b8",     # gray72
    "tag_path": "#eaf2ff",
    "tag_ok": "#2ecc71",
    "tag_miss": "#9e9e9e",    # gray62
    "tag_admin": "#e67e22",
    "tag_blocked": "#e74c3c",
    "tag_warn": "#f1c40f",
    "tag_val": "#9aa5b1",
    "tag_note": "#999999",    # gray60
    "tag_item": "#ffffff",
    # --- пульсирующая подсветка (база, акцент) ----------------------------------
    "pulses": {
        "scan": ("#1f6aa5", "#3b9ae1"),
        "delete": ("#c0392b", "#ff6b4a"),
        "list": ("gray40", "#3b9ae1"),
    },
}

_TERMINAL: dict = {
    # --- окно -------------------------------------------------------------
    "window_size": "1080x860",
    "min_size": (980, 720),
    "bg": "#0b0d0b",
    # --- панели / списки ----------------------------------------------------
    "panel": "#161a16",
    "panel_border": "#3c423c",
    "bw_panel": 1,
    "list_bg": "#0e120e",
    "list_border": "#a8b2a8",
    "bw_list": 2,
    "h_list": 130,
    "detail_bg": "#0d110d",
    "statusbar_bg": "#101410",
    # --- строки списка ------------------------------------------------------
    "row": "#0e120e",
    "row_hover": "#18251a",
    "row_selected": "#1fb6ad",
    "row_selected_hover": "#25cfc5",
    # --- кнопки: плоские, рамка цветом акцента, текст — акцентом ---------------
    "accent_bg": "#0e1626",
    "accent_hover": "#17305a",
    "accent_border": "#4a9eff",
    "bw_scan": 2,
    "danger_bg": "#160d0d",
    "danger_hover": "#331414",
    "danger_border": "#c04545",
    "safe_bg": "#0e1a24",
    "safe_hover": "#16324a",
    "safe_border": "#3fa7d6",
    "restart_bg": "#1a1108",
    "restart_hover": "#33200e",
    "restart_border": "#e08a3c",
    "bw_btn": 1,
    # --- меню / чекбокс -----------------------------------------------------
    "menu_bg": "#111411",
    "menu_btn": "#1d231d",
    "menu_btn_hover": "#2a332a",
    "menu_border": "#4a544a",
    "check_fg": "#1fb66d",
    "check_hover": "#179a5b",
    "check_border": "#5a6a5a",
    # --- прогресс -----------------------------------------------------------
    "progress": "#39d353",

    # --- геометрия: плоский «терминал» — без скруглений --------------------------
    "radius_frame": 0,
    "radius_btn": 0,
    "radius_row": 0,
    "radius_menu": 0,
    "radius_text": 0,
    "h_scan": 44,
    "h_row": 34,
    "h_delete": 38,
    "h_restore": 32,
    # --- шрифты: всё моноширинное ------------------------------------------------
    "font_ui": "Consolas",
    "font_mono": "Consolas",
    "mono_ui": True,
    "sz_banner": 15,
    "sz_restart": 14,
    "sz_hint": 13,
    "sz_guide": 14,
    "sz_list_title": 15,
    "sz_row": 14,
    "sz_details_title": 15,
    "sz_detail_text": 14,
    "sz_scan": 15,
    "sz_delete": 15,
    "sz_restore": 13,
    "sz_check": 12,
    "sz_status": 12,
    "sz_backup": 13,
    "sz_placeholder": 14,
    "sz_menu": 12,
    "sz_tool": 12,
    # --- текстовые цвета (масштабируются яркостью) -----------------------------
    "text": "#d8f0d8",
    "text_dim": "#8fa58f",
    "section_title": "#39d353",
    "guide": "#39d353",
    "status_text": "#74c98a",
    "ok": "#39d353",
    "err": "#ff5c5c",
    "hint_warn": "#ffab52",
    "row_text": "#42e868",
    "row_text_selected": "#062018",
    "backup": "#39d353",
    "accent_text": "#cfe6ff",
    "danger_text": "#ff5c5c",
    "safe_text": "#7fd4ff",
    "restart_text": "#ffab52",
    "menu_text": "#bfe8c6",
    "check_text": "#bfe8c6",
    # --- цвета тегов rich-text панели деталей -----------------------------------
    "tag_h1": "#eaffea",
    "tag_sub": "#9adf9a",
    "tag_legend": "#6a9a6a",
    "tag_dim": "#6a9a6a",
    "tag_path": "#79c0ff",
    "tag_ok": "#3ef06a",
    "tag_miss": "#e7c545",
    "tag_admin": "#ffb454",
    "tag_blocked": "#ff6b6b",
    "tag_warn": "#ffd24a",
    "tag_val": "#8fa5b1",
    "tag_note": "#6f8f6f",
    "tag_item": "#eaffea",
    # --- пульсирующая подсветка (база, акцент) ----------------------------------
    "pulses": {
        "scan": ("#0e1626", "#1d3a66"),
        "delete": ("#160d0d", "#5a1a1a"),
        "list": ("#a8b2a8", "#39d353"),
    },
}

_THEMES: dict[str, dict] = {"classic": _CLASSIC, "terminal": _TERMINAL}

# ---------------------------------------------------------------------------
# Состояние + сохранение выбора
# ---------------------------------------------------------------------------

_theme: str = DEFAULT_THEME
_brightness: str = DEFAULT_BRIGHTNESS


def _prefs_path() -> Path:
    """Путь к файлу настроек вида (создаётся при первом сохранении)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "KeyboardLayoutCleaner" / "ui_prefs.json"


def load_prefs() -> dict:
    """Прочитать сохранённые настройки вида (устойчиво к ошибкам)."""
    try:
        data = json.loads(_prefs_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as exc:  # noqa: BLE001
        logger.debug("Не удалось загрузить настройки вида: %s", exc)
    return {}


def _save_prefs() -> None:
    """Сохранить текущие настройки вида (ошибки не критичны)."""
    try:
        path = _prefs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"theme": _theme, "brightness": _brightness},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось сохранить настройки вида: %s", exc)


def _load_initial() -> None:
    """Загрузить сохранённый выбор при старте приложения."""
    global _theme, _brightness
    prefs = load_prefs()
    t = prefs.get("theme")
    if t in _THEMES:
        _theme = t
    b = prefs.get("brightness")
    if b in BRIGHTNESS_FACTORS:
        _brightness = b


_load_initial()


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------


def current_theme() -> str:
    """Активный интерфейс: 'classic' или 'terminal'."""
    return _theme


def set_theme(name: str) -> None:
    """Установить интерфейс по имени (валидация + сохранение)."""
    global _theme
    if name in _THEMES:
        _theme = name
        _save_prefs()
    else:
        logger.warning("Неизвестная тема: %s", name)


def current_brightness() -> str:
    """Активный уровень яркости шрифтов: 'dim'/'normal'/'bright'/'ultra'."""
    return _brightness


def set_brightness(code: str) -> None:
    """Установить уровень яркости шрифтов (валидация + сохранение)."""
    global _brightness
    if code in BRIGHTNESS_FACTORS:
        _brightness = code
        _save_prefs()
    else:
        logger.warning("Неизвестный уровень яркости: %s", code)


def brightness_factor() -> float:
    """Множитель яркости текстовых цветов (1.0 — обычная)."""
    return BRIGHTNESS_FACTORS.get(_brightness, 1.0)


def scale_color(color: str, factor: float) -> str:
    """
    Масштабировать яркость HEX-цвета (#rrggbb).

    factor > 1 — к белому, factor < 1 — к чёрному.
    Не-HEX значения (имена Tk) возвращаются без изменений.
    """
    if (
        not isinstance(color, str)
        or not color.startswith("#")
        or len(color) != 7
    ):
        return color
    try:
        r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    except ValueError:
        return color

    def _ch(c: int) -> int:
        if factor >= 1.0:
            return round(c + (255 - c) * (factor - 1.0))
        return round(c * factor)

    return "#{:02x}{:02x}{:02x}".format(
        *(min(255, max(0, _ch(x))) for x in (r, g, b))
    )


def _T() -> dict:
    """Активный словарь темы."""
    return _THEMES[_theme]


def S(token: str):
    """Структурный параметр темы (фон, рамка, радиус) — без масштабирования."""
    return _T()[token]


def C(token: str) -> str:
    """Цвет шрифта по токену — с учётом текущей яркости шрифтов."""
    color = _T()[token]
    if token in TEXT_TOKENS:
        return scale_color(color, brightness_factor())
    return color



def P(kind: str) -> tuple:
    """Пара цветов (база, акцент) для пульсирующей подсветки элемента."""
    return tuple(_T()["pulses"][kind])


def N(token: str):
    """Числовой параметр темы (радиусы, высоты, толщина рамок)."""
    return _T()[token]


def F(token: str) -> str:
    """Имя семейства шрифтов по токену ('font_ui' / 'font_mono')."""
    return _T()[token]


def body_font(size: int, weight: str | None = None, slant: str | None = None):
    """
    Шрифт основного текста интерфейса.

    В терминальной теме всё моноширинное, в классической — Segoe UI.
    """
    family = F("font_mono") if _T()["mono_ui"] else F("font_ui")
    return ctk.CTkFont(
        family=family,
        size=size,
        weight=weight or "normal",
        slant=slant or "roman",
    )


def mono_font(size: int, weight: str | None = None):
    """Моноширинный шрифт (значения, пути, коды)."""
    return ctk.CTkFont(
        family=F("font_mono"), size=size, weight=weight or "normal"
    )


def row_text_key() -> str:
    """Ключ локализации строки списка раскладок для активной темы."""
    return "list_row_terminal" if _T()["mono_ui"] else "list_row"

