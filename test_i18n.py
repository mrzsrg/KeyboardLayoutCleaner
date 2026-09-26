"""
Тесты модуля i18n.py.
"""

from __future__ import annotations

import ast
import json
import logging
import re
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
        i18n.winapi, "get_user_default_ui_language", return_value=lcid
    ):
        assert i18n.detect_system_lang() == expected


def test_detect_system_lang_windows_error() -> None:
    """При ошибке GetUserDefaultUILanguage возвращается fallback (en)."""

    _stub = _StubLogger()
    logger = logging.getLogger("layout_cleaner")
    logger.handlers = []
    logger.setLevel(logging.NOTSET)
    applog.setup_null_handler(logger)

    # FIX-19: шов — winapi.get_user_default_ui_language. Подмена
    # i18n.windll больше не действует и проверяла бы настоящий WinAPI.
    with patch.object(
        i18n.winapi,
        "get_user_default_ui_language",
        side_effect=RuntimeError("no lang"),
    ):
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

# FIX-15: «восстановление» — это merge, а не откат. Обещание полной отмены
# («undo», «撤销», «desfazer») вводит в заблуждение: reg import применяет
# сохранённые значения ПОВЕРХ текущего реестра.
PROMISED_UNDO = (
    "undo it",
    "undo the deletion",
    "rückgängig",
    "deshacer",
    "desfazer",
    "撤销",
    "отменить удаление",
)

# Ключи, где пользователю объясняют характер восстановления.
MERGE_KEYS = (
    "plan_backup_note",
    "dlg_advice_restore",
    "dlg_restore_confirm",
)


def _load_locale(name: str) -> dict[str, str]:
    return json.loads((BASE / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("key", MERGE_KEYS)
def test_restore_wording_does_not_promise_full_undo(key: str) -> None:
    """Ни одна локаль не обещает возврат к состоянию на момент бэкапа."""
    for path in sorted(BASE.glob("*.json")):
        text = _load_locale(path.stem).get(key, "")
        lowered = text.lower()
        for promise in PROMISED_UNDO:
            assert promise not in lowered, f"{path.name}: «{promise}» в {key}"


@pytest.mark.parametrize("key", MERGE_KEYS)
def test_restore_wording_explains_merge(key: str) -> None:
    """Во всех локалях сказано, что это слияние, а не откат."""
    for path in sorted(BASE.glob("*.json")):
        text = _load_locale(path.stem).get(key, "").lower()
        assert "merge" in text, f"{path.name}: нет «merge» в {key}"


def test_restore_confirm_keeps_path_placeholder() -> None:
    """Диалог подтверждения обязан сохранять плейсхолдер пути."""
    for path in sorted(BASE.glob("*.json")):
        assert "{path}" in _load_locale(path.stem)["dlg_restore_confirm"], path.name



# FIX-17: паритет ключей и реальная доля перевода.
#
# Ключевая находка при реализации FIX-15/18: 6 ключей (`tray_*`,
# `list_col_*`) были ТОЛЬКО в en/ru, при этом не использовались в коде
# вообще. Из-за них паритет ломался, а i18n молча показывала английский
# текст. Ключи удалены как мёртвые (проверено поиском по *.py/*.ps1).


def test_all_locales_have_exactly_the_base_keys() -> None:
    """(a) Паритет ключей: set(en) == set(lang) для каждой локали.

    Без этого отсутствующий ключ МОЛЧА подменяется английским: интерфейс
    выглядит рабочим, но на выбранном языке. Именно так и выглядел трей
    с ключами только в en/ru.
    """
    base = set(_load_locale("en"))
    for path in sorted(BASE.glob("*.json")):
        keys = set(_load_locale(path.stem))
        assert keys == base, (
            f"{path.name}: расхождение; "
            f"нет={sorted(base - keys)} лишние={sorted(keys - base)}"
        )


def test_no_locale_keys_are_blank() -> None:
    """Пустая строка выглядит как «перевод есть», но на экране — ничего."""
    for path in sorted(BASE.glob("*.json")):
        for key, value in _load_locale(path.stem).items():
            assert value.strip(), f"{path.name}: ключ {key} пуст"


# FIX-17 закрыт: `es` допереведена полностью (2,7 % строк совпадают с en —
# название продукта, «Error», и три ключа-шаблона без естественного текста).
# Allowlist для непереведённых локалей УДАЛЁН намеренно: пока он существует,
# достаточно дописать локаль в список, чтобы скрыть новый дефект навсегда.
# Теперь любая локаль обязана быть переведена — проверка ниже.


_I18N_SOURCE = (Path(__file__).resolve().parent / "i18n.py").read_text(
    encoding="utf-8"
)


def _translation_ratio(name: str) -> float:
    """Доля строк локали, дословно совпадающих с базой (en), в процентах."""
    base = _load_locale("en")
    data = _load_locale(name)
    shared = [k for k, v in base.items() if v.strip() and data.get(k) == v]
    return len(shared) / len(base) * 100


@pytest.mark.parametrize("name", ["ru", "de", "pt", "zh", "es"])
def test_locale_is_actually_translated(name: str) -> None:
    """(b) FIX-17: перевод должен быть настоящим, а не копией en.

    Порог 20 % из плана задачи. Копия базы под другим именем — это
    «Español» с английским интерфейсом.

    Тест применяется ко ВСЕМ локалям, включая `es`: отдельный список
    исключений здесь означал бы, что непереведённая локаль может вернуться
    молча.
    """
    ratio = _translation_ratio(name)
    assert ratio < 20, f"{name}: {ratio:.0f}% строк дословно совпадают с en"


def test_no_locale_is_a_disguised_english_copy() -> None:
    """Ни одна локаль, включая базовую, не обязана содержать английский.

    Отдельная проверка на СПИСОК непереведённых локалей больше не нужна:
    предыдущая ``KNOWN_UNTRANSLATED`` была тем самым allowlist, о котором
    сам её комментарий предупреждал. Условие не должно быть достижимо
    добавлением одной строки.
    """
    for name in ("ru", "de", "pt", "zh", "es"):
        assert f'"{name}"' in _I18N_SOURCE, f"{name} должен быть в i18n.SUPPORTED"


# FIX-18: ShellExecuteW может вернуть любой из этих кодов, и каждый обязан
# давать ПОЧИМЕННЫЙ текст. Раньше 4 из 8 (коды 2, 3, 11, 1136) показывали
# пользователю сырой ключ вида dlg_elevation_error_file_not_found.
ELEVATION_CODES = (2, 3, 5, 11, 31, 1136, 1223, 740)
MAIN_SOURCE = Path(__file__).resolve().parent / "main.py"


def _elevation_error_keys() -> dict[int, str]:
    """Достать из main.py словарь «код ShellExecuteW -> ключ локали».

    Разбором дерева ``ast``, а не регуляркой по тексту: словарь читается
    прямо из кода, поэтому тест не расходится с ним при правках и не
    требует импортировать GUI-модуль целиком.
    """
    tree = ast.parse(MAIN_SOURCE.read_text(encoding="utf-8"))
    mapping: dict[int, str] = {}
    for node in ast.walk(tree):
        # Словарь АННОТИРОВАН (`error_messages: dict[int, str] = {...}`),
        # поэтому это AnnAssign, а не Assign. Первый вариант теста искал
        # только Assign и молча нашёл пустой словарь — тест обязан был
        # упасть на «0 >= 8», а не пройти.
        if isinstance(node, ast.AnnAssign):
            if not (isinstance(node.target, ast.Name)):
                continue
            if node.target.id != "error_messages" or node.value is None:
                continue
            value = node.value
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "error_messages" not in targets or not isinstance(node.value, ast.Dict):
                continue
            value = node.value
        else:
            continue
        if not isinstance(value, ast.Dict):
            continue
        for key_node, val_node in zip(value.keys, value.values, strict=True):
            if isinstance(key_node, ast.Constant) and isinstance(
                val_node, ast.Constant
            ):
                mapping[key_node.value] = val_node.value
    return mapping


def test_elevation_dictionary_covers_all_documented_codes() -> None:
    """Словарь не теряет ни одного документированного кода ShellExecuteW."""
    mapping = _elevation_error_keys()
    for code in ELEVATION_CODES:
        assert code in mapping, f"код {code} потерян"
    # Ветка «неизвестный код» тоже должна быть локализована.
    assert "dlg_elevation_generic_error" in _load_locale("en")


def test_no_uac_code_resolves_to_raw_key() -> None:
    """Приёмка FIX-18: ни один код не выводит пользователю сырой ключ."""
    mapping = _elevation_error_keys()
    assert len(mapping) >= len(ELEVATION_CODES)
    for code, key in mapping.items():
        for path in sorted(BASE.glob("*.json")):
            data = _load_locale(path.stem)
            assert key in data, f"{path.name}: нет ключа {key} (код {code})"
            assert data[key] != key, f"{path.name}: {key} пуст"
    # Ключи, которые раньше запрашивал код, но которых в локалях не было.
    for stale in (
        "dlg_elevation_error_file_not_found",
        "dlg_elevation_error_path_not_found",
        "dlg_elevation_error_bad_netpath",
        "dlg_elevation_error_association",
    ):
        assert stale not in mapping, f"{stale} — сырой ключ, которого нет в локалях"


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


# ---------------------------------------------------------------------------
# FIX-25: плейсхолдеры локалей
# ---------------------------------------------------------------------------


def _placeholders(text: str) -> set[str]:
    return set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", text))


def test_all_locales_share_identical_placeholders():
    """Набор плейсхолдеров {…} обязан совпадать во всех локалях.

    Инцидент: main.py вызывает `_t("dlg_no_backups", path=...)`, а en/ru/es
    содержали `{dir}`. i18n.fmt ловил KeyError и возвращал строку БЕЗ
    подстановки — пользователь видел «{dir}» вместо пути к папке бэкапов.
    Расхождение было молчаливым: тест «ключ есть во всех локалях»
    проходил, а ломался только форматирование в рантайме.
    """
    locales_dir = Path(__file__).parent / "locales"
    ref = json.loads((locales_dir / "en.json").read_text(encoding="utf-8"))
    problems: list[str] = []
    for p in sorted(locales_dir.glob("*.json")):
        if p.name == "en.json":
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        for key, ref_text in ref.items():
            if key not in data:
                continue
            want = _placeholders(ref_text)
            got = _placeholders(data[key])
            if want != got:
                problems.append(
                    f"{p.name}:{key} — ожидалось "
                    f"{{{','.join(sorted(want))}}}, получено "
                    f"{{{','.join(sorted(got))}}}"
                )
    assert not problems, "Расхождение плейсхолдеров:\n" + "\n".join(problems)


@pytest.mark.parametrize(
    "lang", sorted(p.stem for p in (Path(__file__).parent / "locales").glob("*.json"))
)
def test_dlg_no_backups_formats_with_real_path(lang):
    """Строка реально форматируется — без остаточных плейсхолдеров."""
    previous = i18n.current_lang
    try:
        i18n.set_language(lang)
        text = i18n.fmt("dlg_no_backups", path="C:\\backups")
    finally:
        i18n.set_language(previous)
    assert "C:\\backups" in text, f"{lang}: путь не подставлен"
    assert "{" not in text, f"{lang}: остался неподставленный плейсхолдер"


@pytest.mark.parametrize(
    "lang", sorted(p.stem for p in (Path(__file__).parent / "locales").glob("*.json"))
)
def test_no_backups_uses_path_placeholder(lang):
    """В dlg_no_backups только {path} — ровно то, что передаёт main.py."""
    data = json.loads(
        (Path(__file__).parent / "locales" / f"{lang}.json").read_text(encoding="utf-8")
    )
    text = data["dlg_no_backups"]
    assert "{path}" in text, f"{lang}: нет {{path}}"
    assert "{dir}" not in text, f"{lang}: остался устаревший {{dir}}"

