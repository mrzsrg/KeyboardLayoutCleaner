"""
Тесты модуля i18n.py.
"""

from __future__ import annotations

import json
import logging
import types
from pathlib import Path
from unittest.mock import patch

import pytest

import applog
import i18n


@pytest.mark.parametrize(
    ("lcid", "expected"),
    [
        (0x09, "en"),
        (0x19, "ru"),
        (0x0A, "es"),
        (0x07, "de"),
        (0x04, "zh"),
        (0x16, "pt"),
        (0x109, "en"),
        (0x119, "ru"),
        (0x9999, "en"),
    ],
)
def test_detect_system_lang_mapping(lcid: int, expected: str) -> None:
    """Определение языка по LCID с маппингом и fallback на en."""
    _stub = _StubLogger()
    logger = logging.getLogger("layout_cleaner")
    logger.handlers = []
    logger.setLevel(logging.NOTSET)
    applog.setup_null_handler(logger)

    with patch.object(
        i18n.windll.kernel32, "GetUserDefaultUILanguage", return_value=lcid
    ):
        assert i18n.detect_system_lang() == expected


def test_detect_system_lang_windows_error() -> None:
    """При ошибке GetUserDefaultUILanguage возвращается fallback (en)."""

    class FakeWindll(types.ModuleType):
        kernel32 = types.SimpleNamespace()
        kernel32.GetUserDefaultUILanguage = lambda: (_ for _ in ()).throw(
            RuntimeError("no lang")
        )

    _stub = _StubLogger()
    logger = logging.getLogger("layout_cleaner")
    logger.handlers = []
    logger.setLevel(logging.NOTSET)
    applog.setup_null_handler(logger)

    with patch.object(i18n, "windll", FakeWindll("windll")):
        assert i18n.detect_system_lang() == i18n.FALLBACK


def _load_stub(lang: str, overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Вернуть словарь, имитирующий load-файл, при необходимости с переопределениями."""
    base = json.loads((BASE / "en.json").read_text(encoding="utf-8"))
    if overrides:
        base.update(overrides)
    return base


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("en", "en"),
        ("ru", "ru"),
        ("es", "es"),
        ("de", "de"),
        ("zh", "zh"),
        ("pt", "pt"),
    ],
)
def test_set_language_known_valid(lang: str, expected: str) -> None:
    """Установка известного валидного языка."""
    _stub = _StubLogger()
    logger = logging.getLogger("layout_cleaner")
    logger.handlers = []
    logger.setLevel(logging.NOTSET)
    applog.setup_null_handler(logger)

    def fake_load(code: str) -> dict[str, str]:
        return _load_stub(code)

    with patch.object(i18n, "_load_file", side_effect=fake_load):
        result = i18n.set_language(lang)
        assert result == expected
        assert i18n.current_lang == expected
        assert i18n.tr["app_title"] == "Keyboard Layout Cleaner"  # присутствует в en


def test_set_language_unknown_fallback_to_en() -> None:
    """Неизвестный язык -> fallback на en."""
    _stub = _StubLogger()
    logger = logging.getLogger("layout_cleaner")
    logger.handlers = []
    logger.setLevel(logging.NOTSET)
    applog.setup_null_handler(logger)

    with patch.object(i18n, "_load_file", side_effect=lambda c: _load_stub(c)):
        assert i18n.set_language("xx") == i18n.FALLBACK
        assert i18n.current_lang == i18n.FALLBACK


def test_set_language_base_invalid_falls_back_gracefully() -> None:
    """Повреждённый/отсутствующий базовый en.json НЕ роняет приложение.

    Раньше _load_file(FALLBACK) бросал исключение на старте. По замечанию
    аудита чтение базы обёрнуто в try-except: при сбое используется пустой
    словарь и fallback на en (fmt() вернёт сам ключ).
    """
    logger = logging.getLogger("layout_cleaner")
    logger.handlers = []
    logger.setLevel(logging.NOTSET)
    applog.setup_null_handler(logger)

    def bad_load(_lang: str) -> dict[str, str]:
        raise ValueError("root is not an object")

    with patch.object(i18n, "_load_file", side_effect=bad_load):
        assert i18n.set_language("ru") == i18n.FALLBACK
        assert i18n.current_lang == i18n.FALLBACK


def test_set_language_file_not_found_logs_warning() -> None:
    """Если надстройка не загрузилась — предупреждение, lang = fallback."""
    logger = logging.getLogger("layout_cleaner")
    logger.handlers = []
    logger.setLevel(logging.NOTSET)
    applog.setup_null_handler(logger)

    def load_override(lang: str) -> dict[str, str]:
        if lang == i18n.FALLBACK:
            return _load_stub("en")
        raise OSError("missing file")

    import io

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s:%(message)s"))
    logger.addHandler(handler)

    with patch.object(i18n, "_load_file", side_effect=load_override):
        result = i18n.set_language("ru")
        assert result == i18n.FALLBACK
        assert i18n.current_lang == i18n.FALLBACK
        log_text = stream.getvalue()
        assert "Локаль ru не загружена" in log_text


def test_set_language_clears_tr() -> None:
    """При смене языка tr очищается и заполняется заново."""
    logger = logging.getLogger("layout_cleaner")
    logger.handlers = []
    logger.setLevel(logging.NOTSET)
    applog.setup_null_handler(logger)

    with patch.object(i18n, "_load_file", side_effect=lambda c: _load_stub(c)):
        i18n.set_language("ru")
        old_len = len(i18n.tr)
        i18n.set_language("de")
        assert len(i18n.tr) == old_len
        assert i18n.tr.get("app_title") == "Keyboard Layout Cleaner"


def test_supports_and_display_names() -> None:
    """Константы SUPPORTED и DISPLAY_NAMES."""
    assert isinstance(i18n.SUPPORTED, tuple)
    assert "en" in i18n.SUPPORTED
    assert "ru" in i18n.SUPPORTED
    assert isinstance(i18n.DISPLAY_NAMES, dict)
    assert i18n.DISPLAY_NAMES["en"] == "English"
    assert i18n.DISPLAY_NAMES["ru"] == "Русский"


def test_fmt_existing_key() -> None:
    """Корректная подстановка параметров для существующего ключа."""
    logger = logging.getLogger("layout_cleaner")
    logger.handlers = []
    logger.setLevel(logging.NOTSET)
    applog.setup_null_handler(logger)

    with patch.object(i18n, "_load_file", side_effect=lambda c: _load_stub(c)):
        i18n.set_language("en")

    assert i18n.fmt("list_title_found", count=3) == "Found layouts (3) — click a row:"


def test_fmt_missing_key_returns_key() -> None:
    """Отсутствующий ключ возвращается без изменений."""
    logger = logging.getLogger("layout_cleaner")
    logger.handlers = []
    logger.setLevel(logging.NOTSET)
    applog.setup_null_handler(logger)

    with patch.object(i18n, "_load_file", side_effect=lambda c: _load_stub(c)):
        i18n.set_language("en")

    assert i18n.fmt("nonexistent_key_xyz") == "nonexistent_key_xyz"


def test_fmt_format_error_returns_raw() -> None:
    """Если форматирование вызвало ошибку — возвращается исходный текст без исключения."""
    logger = logging.getLogger("layout_cleaner")
    logger.handlers = []
    logger.setLevel(logging.NOTSET)
    applog.setup_null_handler(logger)

    bad = {"test_bad_fmt": "value {0} {foo}"}

    def load_stub(lang: str) -> dict[str, str]:
        if lang == i18n.FALLBACK:
            return bad
        return {}

    with patch.object(i18n, "_load_file", side_effect=load_stub):
        i18n.set_language("en")

    result = i18n.fmt("test_bad_fmt", count=5)
    assert result == "value {0} {foo}"


def test_fmt_no_kwargs() -> None:
    """Без kwargs fmt возвращает строку как есть."""
    logger = logging.getLogger("layout_cleaner")
    logger.handlers = []
    logger.setLevel(logging.NOTSET)
    applog.setup_null_handler(logger)

    with patch.object(i18n, "_load_file", side_effect=lambda c: _load_stub(c)):
        i18n.set_language("en")

    assert i18n.fmt("list_title_empty") == "Found layouts: none"


BASE = Path(__file__).resolve().parent / "locales"


class _StubLogger:
    def __init__(self):
        self.warnings: list[tuple[str, ...]] = []
        self.info: list[tuple[str, ...]] = []

    def warning(self, msg: str, *args: object) -> None:
        self.warnings.append((msg, args))

    def info(self, msg: str, *args: object) -> None:
        self.info.append((msg, args))

    def error(self, msg: str, *args: object) -> None:
        pass

    def debug(self, msg: str, *args: object) -> None:
        pass
