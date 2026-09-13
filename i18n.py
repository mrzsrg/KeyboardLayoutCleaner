"""
i18n.py — мини-слой локализации Keyboard Layout Cleaner.

Файлы переводов: locales/<код>.json (en — базовый, поверх него
накладывается выбранный язык; отсутствующие ключи автоматически
берутся из базы). Язык по умолчанию определяется по интерфейсу
Windows (GetUserDefaultUILanguage).
"""

import json
import logging
from ctypes import windll
from pathlib import Path

logger = logging.getLogger("layout_cleaner")
if not logger.handlers:
    logger.addHandler(logging.NullHandler())

SUPPORTED = ("en", "ru", "es", "de", "zh", "pt")
FALLBACK = "en"
LOCALES_DIR = Path(__file__).resolve().parent / "locales"

# Первичный язык LCID (младший байт) -> код локали приложения
_PRIMARY_TO_LANG = {
    0x09: "en",  # English
    0x19: "ru",  # Russian
    0x0A: "es",  # Spanish
    0x07: "de",  # German
    0x04: "zh",  # Chinese
    0x16: "pt",  # Portuguese
}

# Отображаемые названия для переключателя в UI
DISPLAY_NAMES = {
    "en": "English",
    "ru": "Русский",
    "es": "Español",
    "de": "Deutsch",
    "zh": "简体中文",
    "pt": "Português",
}

tr: dict[str, str] = {}
current_lang: str = FALLBACK


def detect_system_lang() -> str:
    """Определить язык интерфейса Windows (с fallback на en)."""
    try:
        lang_id = int(windll.kernel32.GetUserDefaultUILanguage())
        return _PRIMARY_TO_LANG.get(lang_id & 0xFF, FALLBACK)
    except Exception:  # noqa: BLE001
        return FALLBACK


def _load_file(lang: str) -> dict[str, str]:
    path = LOCALES_DIR / f"{lang}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: корень должен быть JSON-объектом")
    return data


def set_language(lang: str) -> str:
    """
    Загрузить локаль: en — база, поверх — выбранный язык.
    Возвращает фактически установленный код (с fallback на en).
    """
    global current_lang
    lang = lang if lang in SUPPORTED else FALLBACK
    strings = _load_file(FALLBACK)
    if lang != FALLBACK:
        try:
            strings.update(_load_file(lang))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Локаль %s не загружена (%s) — используется en", lang, exc
            )
            lang = FALLBACK
    tr.clear()
    tr.update(strings)
    current_lang = lang
    logger.info("Язык интерфейса: %s", lang)
    return lang


def fmt(key: str, **kwargs) -> str:
    """Строка по ключу с подстановкой параметров (без исключений)."""
    text = tr.get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось отформатировать строку %s: %s", key, exc)
    return text