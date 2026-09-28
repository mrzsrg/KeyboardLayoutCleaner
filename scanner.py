"""
scanner.py — Модуль поиска и сопоставления раскладок клавиатуры (Этап 1).

Читает реестр Windows (HKLM, HKCU, HKU), опрашивает PowerShell для
получения Language Tags, связывает их с HEX-кодами (KLID) и возвращает
словарь найденных раскладок с путями их расположения.
"""

import logging
import re
import subprocess
import winreg
from pathlib import Path
from typing import Any

import applog
import layout_ids
from config import (
    _SANDBOX_ROOT,
    TIMEOUTS,
    disable_sandbox,
    enable_sandbox,
    is_sandbox_enabled,
)
from layout_ids import (
    KLID_RE as _KLID_RE,
)
from layout_ids import (
    LAYOUT_MAP,
    hkl_to_klid,
)
from layout_ids import (
    METADATA_VALUE_NAMES as _METADATA_VALUE_NAMES,
)
from layout_ids import (
    TIP_KLID_RE as _TIP_KLID_RE,
)
from winproc import run_hidden

logger = logging.getLogger("layout_cleaner")
applog.setup_null_handler(logger)

# ---------------------------------------------------------------------------
# FIX-8: реэкспорт единого резолвера идентификаторов (layout_ids).
# Имена с подчёркиванием сохранены как тонкие обёртки: на них ссылаются
# cleaner.py, тесты и внешний код. Единственный источник знаний — layout_ids;
# ниже НЕ должно появляться новых копий таблиц/регулярных выражений.
# ---------------------------------------------------------------------------

# Обратный словарь KLID -> теги (сопоставление в SettingSync)
_KLID_TO_TAGS = layout_ids.KLID_TO_TAGS

# Имя родительского ключа CTF (LANGID в виде 0x????????)
_CTF_LANGID_DIR_RE = layout_ids.CTF_LANGID_DIR_RE


def _normalize_klid_token(raw: str, key_path: str) -> str:
    """Привести KLID-подобный токен из CTF/User Profile к канонической 8-HEX."""
    return layout_ids.normalize_klid_token(raw, key_path)



def _safe_str(val: object) -> str:
    """
    Безопасное строковое представление значения реестра.

    REG_BINARY/REG_NONE и прочие «нестроковые» типы не должны ломать
    скан (UnicodeDecodeError/TypeError при str() от экзотических типов)
    — возвращаем repr-подобную строку, которую KLID-парсер отбросит.
    """
    try:
        return str(val).strip()
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        logger.debug("Не удалось привести значение к строке: %s", exc)
        return ""


def _reg_get_string(
    key: int, subkey_path: str, value_name: str | None = None
) -> dict[str, str]:
    """Открыть subkey и вернуть его значения или конкретный value."""
    result: dict[str, str] = {}
    try:
        with winreg.OpenKey(key, subkey_path) as parent:
            if value_name:
                try:
                    val, _ = winreg.QueryValueEx(parent, value_name)
                    return {"": str(val)}
                except FileNotFoundError:
                    return {}
            else:
                idx = 0
                while True:
                    try:
                        name, val, _ = winreg.EnumValue(parent, idx)
                        result[name] = str(val)
                        idx += 1
                    except OSError:
                        break
    except OSError as exc:
        # FileNotFoundError и PermissionError — частные случаи OSError
        logger.debug("Ветка недоступна (%s): %s", subkey_path, exc)
    return result


def _get_preload_keys(root_key: int, subkey_path: str) -> dict[str, str]:
    """Считать ключи Preload и вернуть {hex_value: hex_value}.

    Валидация KLID внутри функции (defense-in-depth): мусорные значения
    реестра («1», «ru-RU» и пр.) не покидают парсер, независимо от того,
    проверяет ли их вызывающий код.
    """
    result: dict[str, str] = {}
    values = _reg_get_string(root_key, subkey_path)
    for _idx, val in values.items():
        hex_code = val.strip().split("\\")[-1].lower() if val else ""
        if not hex_code or not _KLID_RE.match(hex_code):
            # Пустое/некорректное значение — пропускаем, чтобы не создавать
            # фантомные записи (исторически фильтр был на уровне вызовов)
            continue
        result[hex_code] = hex_code
    return result


def _scan_substitutes(root_key: int, subkey_path: str) -> dict[str, dict[str, str]]:
    """Считать ключи Substitutes и вернуть {source_klid: {"target": ...}}.

    Обе стороны пары (source и target) обязаны быть валидными KLID —
    см. комментарий в :func:`_get_preload_keys`.
    """
    result: dict[str, dict[str, str]] = {}
    values = _reg_get_string(root_key, subkey_path)
    for src, target in values.items():
        # Нормализуем регистр: имена значений Substitutes в реестре
        # встречаются и в верхнем регистре (D001DEAD), а KLID везде
        # сравниваются в нижнем
        src_norm = src.strip().lower()
        target_hex = target.strip().split("\\")[-1].lower() if target else ""
        if not (_KLID_RE.match(src_norm) and _KLID_RE.match(target_hex)):
            continue
        result[src_norm] = {"target": target_hex}
    return result


# ---------------------------------------------------------------------------
# Базовые ветки реестра для сканирования
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Единый источник истины о ветках, затрагиваемых при удалении раскладки:
# (root, subkey, scan_type, admin_required). Используется и сканером, и
# cleaner (бэкап/очистка). HKLM Keyboard Layouts сюда НЕ входит — это каталог
# имён раскладок, который приложение никогда не модифицирует.
# ---------------------------------------------------------------------------
AFFECTED_BRANCHES: list[tuple[str, str, str, bool]] = [
    ("HKCU", "Keyboard Layout\\Preload", "preload", False),
    ("HKCU", "Keyboard Layout\\Substitutes", "substitutes", False),
    ("HKCU", "Control Panel\\International\\User Profile", "preload", False),
    ("HKCU", "Software\\Microsoft\\CTF", "preload", False),
    (
        "HKCU",
        "Software\\Microsoft\\Windows\\CurrentVersion\\SettingSync\\Namespace\\Language",
        "preload",
        False,
    ),
    ("HKU", ".DEFAULT\\Keyboard Layout\\Preload", "preload", True),
]

HKCU_BRANCHES: list[tuple[str, str]] = [
    (subkey, scan_type)
    for root, subkey, scan_type, _admin in AFFECTED_BRANCHES
    if root == "HKCU"
]

HKU_BRANCHES: list[tuple[str, str]] = [
    (subkey, scan_type)
    for root, subkey, scan_type, _admin in AFFECTED_BRANCHES
    if root == "HKU"
]

HKLM_BRANCHES: list[tuple[str, str]] = [
    ("SYSTEM\\CurrentControlSet\\Control\\Keyboard Layouts", "preload"),
]

# Оригинал HKLM-веток — для deactivate_sandbox()
_ORIGINAL_HKLM_BRANCHES: list[tuple[str, str]] = list(HKLM_BRANCHES)


# ---------------------------------------------------------------------------
# Сохраняем оригинальные ветки до подмены
# ---------------------------------------------------------------------------
_ORIGINAL_AFFECTED_BRANCHES: list[tuple[str, str, str, bool]] = [
    ("HKCU", "Keyboard Layout\\Preload", "preload", False),
    ("HKCU", "Keyboard Layout\\Substitutes", "substitutes", False),
    ("HKCU", "Control Panel\\International\\User Profile", "preload", False),
    ("HKCU", "Software\\Microsoft\\CTF", "preload", False),
    (
        "HKCU",
        "Software\\Microsoft\\Windows\\CurrentVersion\\SettingSync\\Namespace\\Language",
        "preload",
        False,
    ),
    ("HKU", ".DEFAULT\\Keyboard Layout\\Preload", "preload", True),
]


def _get_sandbox_affected_branches() -> list[tuple[str, str, str, bool]]:
    """Вернуть sandbox-ветки реестра для сканирования.

    Все HKCU-ветки префаксируются `\\Software\\KeyboardCleanerTest`,
    остальные (HKU, HKLM) — перенаправляются туда же для изоляции.
    Функция идемпотентна: повторный вызов не префиксирует уже
    префиксированные ветки (защита от двойной активации).
    """
    branches: list[tuple[str, str, str, bool]] = []
    for root_name, subkey, scan_type, _admin_required in AFFECTED_BRANCHES:
        if subkey.startswith(_SANDBOX_ROOT):
            # Уже sandbox-ветка (повторная активация) — оставляем как есть
            branches.append(("HKCU", subkey, scan_type, False))
            continue
        if root_name == "HKCU":
            branches.append(("HKCU", _SANDBOX_ROOT + "\\" + subkey, scan_type, False))
        else:
            # Перенаправляем HKU/HKLM в sandbox
            branches.append(
                ("HKCU", _SANDBOX_ROOT + "\\test\\" + subkey, scan_type, False)
            )
    return branches


# Lazy evaluation: при import всегда берём «настоящие» ветки;
# sandbox-mode включается через activate_sandbox() + enable_sandbox().
def get_affected_branches() -> list[tuple[str, str, str, bool]]:
    """Публичный аксессор: актуальный список веток с учётом sandbox.

    ЕДИНАЯ точка истины для scanner, cleaner и GUI. При активном
    sandbox-режиме (флаг модуля, config.SANDBOX_MODE или переменная
    окружения SANDBOX_MODE) возвращает sandbox-ветки, иначе — реальные.
    Модули-потребители НЕ должны импортировать AFFECTED_BRANCHES напрямую:
    `from scanner import AFFECTED_BRANCHES` фиксирует список на момент
    импорта и не переключается вместе с sandbox (источник утечки P0-1).
    """
    if is_sandbox_enabled():
        return _get_sandbox_affected_branches()
    return AFFECTED_BRANCHES


def _get_affected_branches() -> list[tuple[str, str, str, bool]]:
    """Обратная совместимость: см. :func:`get_affected_branches`."""
    return get_affected_branches()


def _build_sandbox_hkcu() -> list[tuple[str, str]]:
    """HKCU-ветки для sandbox (без HKU)."""
    return [
        (subkey, scan_type)
        for root, subkey, scan_type, _admin in _get_sandbox_affected_branches()
        if root == "HKCU"
    ]


def _build_sandbox_hku() -> list[tuple[str, str]]:
    """HKU-ветки для sandbox (пусто — все перенаправлено в HKCU sandbox)."""
    return []


def _build_sandbox_recursive() -> set[str]:
    """Sandbox-вариант _RECURSIVE_SCAN."""
    return {
        _SANDBOX_ROOT + "\\Control Panel\\International\\User Profile",
        _SANDBOX_ROOT + "\\Software\\Microsoft\\CTF",
        _SANDBOX_ROOT
        + "\\Software\\Microsoft\\Windows\\CurrentVersion\\SettingSync\\Namespace\\Language",
    }


def activate_sandbox() -> None:
    """Включить режим песочницы: подменить константы модуля.

    Вызывается один раз при старте приложения или в тестах.
    После вызова все вызовы scan_keyboard_layouts(), delete_layout()
    и бэкапа будут работать с HKCU\\Software\\KeyboardCleanerTest
    вместо реальных веток.
    """
    global AFFECTED_BRANCHES, HKCU_BRANCHES, HKU_BRANCHES
    global HKLM_BRANCHES, _RECURSIVE_SCAN

    sandbox = _get_sandbox_affected_branches()
    AFFECTED_BRANCHES = sandbox
    HKCU_BRANCHES = _build_sandbox_hkcu()
    HKU_BRANCHES = _build_sandbox_hku()
    HKLM_BRANCHES = []  # HKLM не сканируем в sandbox
    _RECURSIVE_SCAN = _build_sandbox_recursive()
    enable_sandbox()


def deactivate_sandbox() -> None:
    """Вернуть реальные ветки реестра (используется тестами).

    Обратная операция к :func:`activate_sandbox`: восстанавливает все
    подменённые константы из оригиналов и сбрасывает флаги sandbox
    (scanner.SANDBOX_MODE, config.SANDBOX_MODE, переменная окружения).
    """
    global AFFECTED_BRANCHES, HKCU_BRANCHES, HKU_BRANCHES
    global HKLM_BRANCHES, _RECURSIVE_SCAN

    AFFECTED_BRANCHES = list(_ORIGINAL_AFFECTED_BRANCHES)
    HKCU_BRANCHES = [
        (subkey, scan_type)
        for root, subkey, scan_type, _admin in AFFECTED_BRANCHES
        if root == "HKCU"
    ]
    HKU_BRANCHES = [
        (subkey, scan_type)
        for root, subkey, scan_type, _admin in AFFECTED_BRANCHES
        if root == "HKU"
    ]
    HKLM_BRANCHES = list(_ORIGINAL_HKLM_BRANCHES)
    _RECURSIVE_SCAN = set(_ORIGINAL_RECURSIVE_SCAN)
    disable_sandbox()


# ---------------------------------------------------------------------------
# Производные списки — пересчитываются динамически
# ---------------------------------------------------------------------------

# Ветки, данные которых лежат В ПОДКЛЮЧАХ (SortOrder\AssemblyItem\...,
# User Profile\<тег>\..., SettingSync\...\...) — сканируются рекурсивно,
# а не только по прямым значениям. Зеркалит cleaner._RECURSIVE_SUBKEYS, чтобы
# отчёт «обнаружено/не обнаружено» соответствовал реальному объёму очистки.
_RECURSIVE_SCAN: set[str] = {
    "Control Panel\\International\\User Profile",
    "Software\\Microsoft\\CTF",
    "Software\\Microsoft\\Windows\\CurrentVersion\\SettingSync\\Namespace\\Language",
}

# Оригинал для deactivate_sandbox()
_ORIGINAL_RECURSIVE_SCAN: set[str] = set(_RECURSIVE_SCAN)


# ---------------------------------------------------------------------------
# Нормализация и обратные словари перенесены в layout_ids (FIX-8);
# локальные копии регулярных выражений и таблиц удалены.
# ---------------------------------------------------------------------------


def _klid_matches_value(layout_klid: str, value: str) -> bool:
    """
    Проверить, соответствует ли значение реестра layout_klid.

    Проверяет как прямое совпадение KLID (8 HEX, десятичный HKL,
    формат InputMethodTips), так и совпадение по BCP-47 тегу через
    LAYOUT_MAP (актуально для SettingSync, где хранятся теги языков).
    """
    val_l = value.strip().lower()
    if not val_l:
        return False
    # Прямое совпадение KLID
    if _KLID_RE.match(val_l) and val_l == layout_klid:
        return True
    # Формат InputMethodTips: "0409:00000409"
    tip = _TIP_KLID_RE.match(val_l)
    if tip and tip.group(1).lower() == layout_klid:
        return True
    # Совпадение по BCP-47 тегу (SettingSync хранит теги языков)
    tags = _KLID_TO_TAGS.get(layout_klid, [])
    return val_l in tags


def _scan_branch_recursive(
    root_key: int, subkey_path: str, max_depth: int = 6
) -> list[tuple[str, str, str]]:
    """
    Рекурсивно найти в поддереве значения, содержащие KLID.

    Значения могут быть в форме ``00000409`` или ``0409:00000409``
    (LANGID:KLID, как в User Profile/CTF). REG_BINARY (SortOrder\\Language)
    осознанно не парсится — порядок в трее перестраивает
    Set-WinUserLanguageList (шаг 3 очистки).

    Returns
    -------
    list[tuple[str, str, str]]
        Тройки ``(относительный путь\\имя значения, klid для группировки,
        исходный токен как он лежит в реестре)``.

    FIX-34: klid и исходный токен разведены НАМЕРЕННО. В CTF значение
    ``KeyboardLayout`` — это HKL (0x04190419 = русская), и раньше оно
    группировалось отдельно от ``00000419``, из-за чего одна раскладка
    показывалась в списке дважды, а вторая строка оставалась без имени.
    Теперь записи собираются в одну строку по KLID, а исходный токен
    сохраняется: он и есть доказательство, и по нему видно, что именно
    Windows записала в реестр. Ничего не теряется — все разделы и все
    значения остаются видимыми в подробностях.
    """
    found: list[tuple[str, str, str]] = []

    def _walk(key_path: str, depth: int) -> None:
        if depth > max_depth:
            logger.debug("Максимальная глубина скана: %s", key_path)
            return
        try:
            with winreg.OpenKey(root_key, key_path, 0, winreg.KEY_READ) as key:
                idx = 0
                while True:
                    try:
                        name, val, _ = winreg.EnumValue(key, idx)
                    except OSError:
                        break
                    idx += 1
                    # Метаданные языкового профиля (FeaturesToInstall и пр.)
                    # НЕ являются раскладками, даже если их содержимое — 8 hex
                    # (регрессия 000006ff: битовая маска флагов ошибочно
                    # классифицировалась как KLID-кандидат).
                    if str(name).lower() in _METADATA_VALUE_NAMES:
                        continue
                    raw = str(val).strip()
                    klid = ""
                    tip_match = _TIP_KLID_RE.match(raw)
                    if tip_match:
                        klid = tip_match.group(1).lower()
                    elif _KLID_RE.match(raw) or re.fullmatch(r"[0-9]{3,10}", raw):
                        # Числовые токены (1033, 68748313) — десятичные
                        # HKL/KLID; _normalize_klid_token приводит их к
                        # каноническому 8-HEX виду (тот же формат, что у
                        # cleaner._klid_variants — сканер и очистка едины).
                        klid = _normalize_klid_token(raw, key_path)
                    else:
                        # Проверяем BCP-47 тег (SettingSync хранит теги языков)
                        raw_lower = raw.lower()
                        for tag, tag_klid in LAYOUT_MAP.items():
                            if raw_lower == tag.lower():
                                klid = tag_klid.lower()
                                break
                    if klid:
                        rel = key_path[len(subkey_path) :].lstrip("\\")
                        # HKL — это соглашение ветки CTF: там Windows пишет
                        # HKL (68748313 или 04190419), а не KLID. Сворачиваем
                        # его к KLID, иначе одна и та же раскладка попадёт в
                        # список дважды, а вторая строка останется без имени.
                        # Десятичное значение — тоже HKL, где бы ни лежало.
                        # Вне CTF 8-HEX не трогаем: фантом вида d001dead обязан
                        # сохранить собственное имя. Исходный токен уходит в
                        # подробности, поэтому запись не теряется.
                        is_hkl = bool(re.fullmatch(r"[0-9]{3,10}", raw)) or (
                            "ctf" in subkey_path.lower() and hkl_to_klid(klid) != klid
                        )
                        group = hkl_to_klid(klid) if is_hkl else klid
                        found.append((f"{rel}\\{name}" if rel else name, group, raw))
                sub_idx = 0
                while True:
                    try:
                        sub = winreg.EnumKey(key, sub_idx)
                    except OSError:
                        break
                    sub_idx += 1
                    _walk(key_path + "\\" + sub, depth + 1)
        except (FileNotFoundError, PermissionError, OSError) as exc:
            logger.debug("Ветка пропущена %s: %s", key_path, exc)

    _walk(subkey_path, 0)
    return found


def _resolve_name(hex_code: str) -> str:
    """Человеко-читаемое название раскладки; "" если имя неизвестно.

    FIX-34: пробуем и HKL-форму, и свёрнутую KLID. Раньше ``04070407``
    искался в каталоге Windows как есть, а там лежит ``00000407``, —
    и пользователь получал «Layout (04070407)»: строка выглядела названием
    раскладки, но им не была. Теперь такая запись ищется как ``00000407``
    и получает настоящее имя.

    Пустая строка означает «имени нет» — вызывающий код подставляет
    честную пометку о неизвестной раскладке (см. main._build_layout_name).
    """
    code = hex_code.strip().lower()
    candidates = [code]
    collapsed = hkl_to_klid(code)
    if collapsed != code:
        candidates.append(collapsed)

    # 1. Каталог Windows (HKLM\...\Keyboard Layouts\<KLID> -> "Layout Text")
    for candidate in candidates:
        try:
            with (
                winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    "SYSTEM\\CurrentControlSet\\Control\\Keyboard Layouts",
                ) as layouts_key,
                winreg.OpenKey(layouts_key, candidate) as layout_key,
            ):
                name, _ = winreg.QueryValueEx(layout_key, "Layout Text")
                if name:
                    return str(name)
        except OSError:
            continue

    # 2. Fallback: ищем по LAYOUT_MAP
    for tag, mapped in LAYOUT_MAP.items():
        if mapped in candidates:
            return tag

    return ""


def get_layout_name(hex_code: str) -> str:
    """Публичное имя раскладки по HEX-коду (для GUI и внешних модулей)."""
    return _resolve_name(hex_code.strip().lower())


# ---------------------------------------------------------------------------
# PowerShell integration
# ---------------------------------------------------------------------------

# _TIP_KLID_RE, _KLID_RE и _METADATA_VALUE_NAMES импортированы из layout_ids
# (FIX-8) — единственного источника идентификаторов раскладок.


def _parse_language_entries(stdout: str) -> list[dict[str, Any]]:
    """
    Разобрать вывод PowerShell: строки формата "LanguageTag<TAB>Tips;...".

    KLID извлекается из InputMethodTips ("0419:00000419"). Если все tips
    содержат GUID вместо KLID (CJK IME), используется fallback на
    LAYOUT_MAP по тегу языка.
    """
    entries: list[dict[str, Any]] = []
    for line in stdout.strip().splitlines():
        if "\t" not in line:
            continue
        tag, _, tips_raw = line.partition("\t")
        tag = tag.strip()
        if not tag:
            continue
        klid = ""
        for tip in (t.strip() for t in tips_raw.split(";")):
            match = _TIP_KLID_RE.match(tip)
            if match:
                klid = match.group(1).lower()
                break
        if not klid and tag in LAYOUT_MAP:
            # CJK IME: tips содержат GUID вместо KLID
            klid = LAYOUT_MAP[tag].lower()
        if klid:
            entries.append({"LanguageTag": tag, "KeyboardLayoutId": klid})
    return entries


def _get_language_list_from_powershell() -> list[dict[str, Any]]:
    """
    Опросить PowerShell для получения списка языков пользователя.
    Возвращает список словарей с полями:
      - LanguageTag (BCP-47), e.g. "en-GB"
      - KeyboardLayoutId (KLID HEX), e.g. "00000809"
    """
    script_path = Path(__file__).parent / "scripts" / "layout_cleaner_list.ps1"
    try:
        result = run_hidden(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
            ],
            capture_output=True,
            text=True,
            timeout=TIMEOUTS["powershell_list"],
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            if "not recognized" in stderr.lower() or "не распознан" in stderr.lower():
                logger.warning(
                    "Get-WinUserLanguageList недоступен в этой сборке Windows "
                    "(командлет не распознан). PowerShell-источник пропущен — "
                    "скан продолжится по реестровым веткам. Для полного списка "
                    "обновите Windows/PowerShell или добавьте модуль LanguageList."
                )
            else:
                logger.warning(
                    "Get-WinUserLanguageList: returncode=%s, stderr=%s",
                    result.returncode,
                    stderr[:200],
                )
            return []

        return _parse_language_entries(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("PowerShell недоступен: %s", exc)
        return []


def _get_switcher_klids() -> set[str] | None:
    """FIX-31: KLID, которые переключатель показывает СЕЙЧАС.

    ``None`` — состояние прочитать не удалось (API недоступна, не
    Windows, тест). Это НЕ «раскладок нет»: вызывающий обязан отличать
    одно от другого, иначе непрочитанное состояние превратится в
    диагноз «всё сломалось».

    Замеренная на живой машине асимметрия, ради которой всё затевалось:
    удаление раскладки убирает её из ``Preload`` и из списка языков
    НЕМЕДЛЕННО, но панель продолжает показывать её до следующего входа
    в систему. Реестр хранит намерение, панель — состояние сессии.
    """
    try:
        import winapi

        hkls = winapi.get_keyboard_layout_list()
    except (AttributeError, OSError, ImportError, ValueError):
        return None
    if not hkls:
        return None
    klids: set[str] = set()
    for hkl in hkls:
        # HKL 0x04190419 -> младшее слово 0x0419 -> полный KLID 00000419.
        # Старшее слово — тип клавиатуры, к раскладке отношения не имеет.
        low = hkl & 0xFFFF
        klids.add(f"{0x0000_0000 | low:08x}")
    return klids


def get_session_layout_klids() -> set[str] | None:
    """Публичная обёртка: KLID раскладок, доступных ПРЯМО СЕЙЧАС.

    Нужна GUI, чтобы подписать строку результата скана («в списке языков»
    или «в настройках, но не переключается») — FIX-34. Вызов дешёвый:
    это прямой Win32-вызов, а не PowerShell, и в списке он делается один
    раз на все строки. ``None`` — состояние прочитать не удалось; вызывающий
    обязан отличать его от «раскладок нет» (см. :func:`_get_switcher_klids`).
    """
    return _get_switcher_klids()


def check_switcher_consistency() -> dict[str, list[str]]:
    """FIX-26: сверка списка языков с тем, что способен показать переключатель.

    Симптом, из-за которого это нужно: язык ПРОПАДАЕТ из панели
    переключения раскладок, но в Параметрах языка остаётся «установленным»,
    и помогает только перезагрузка. Причина в том, что это ДВА разных
    хранилища:

      * список языков (WinRT) — его показывает Параметры языка;
      * ``Preload`` + ``CTF\\Assemblies`` — по ним строит панель переключения.

    Операция может развести их, и раньше приложение этого не замечало:
    пользователь оставался с «призрачной» раскладкой без объяснений.

    Функция ТОЛЬКО ЧИТАЕТ. Ничего не чинит и не синхронизирует: CTF —
    кэш, который Windows пересобирает при входе, и запись в него вслепую
    опаснее, чем честная диагностика. Задача функции — показать расхождение
    и дать действие, а не сделать вид, что проблемы нет.

    Returns
    -------
    dict[str, list[str]]
        ``tips_without_preload`` — KLID из списка языков, которого нет в
        ``Preload``; переключатель не сможет его предложить;
        ``preload_without_tip`` — запись ``Preload``, которой нет ни в одном
        языке (остаток старой раскладки);
        ``ctf_missing`` — KLID языка, для которого нет ветки
        ``CTF\\Assemblies\\0x<LangID>``; ``LangID`` — младшие 4 байта
        KLID (из ``00000419`` это ``0419``), а старшие ``0000`` — тип
        клавиатуры, к языку отношения не имеют.

    Все три поля содержат ПОЛНЫЕ 8-HEX KLID в едином формате: значения
    уходят и в лог, и в текст диалога, где смешение ``0409`` и
    ``00000409`` выглядело бы как повреждённые данные.
    """
    entries = _get_language_list_from_powershell()
    list_klids: set[str] = set()
    for entry in entries:
        klid = str(entry.get("KeyboardLayoutId", "")).lower()
        if klid and _KLID_RE.match(klid):
            list_klids.add(klid)

    preload_klids = {
        klid
        for klid in _get_preload_keys(
            winreg.HKEY_CURRENT_USER, "Keyboard Layout\\Preload"
        )
        if _KLID_RE.match(klid)
    }

    # CTF\\Assemblies\\0x<LangID> — ветки языка. Ищем её по МОДУЛЬНОЙ
    # константе веток: activate_sandbox() подменяет её на изолированную,
    # и сверка в песочнице не должна читать живой CTF пользователя.
    ctf_langids: set[str] = set()
    for subkey_path, _scan_type in HKCU_BRANCHES:
        if "\\CTF" not in subkey_path:
            continue
        assemblies = subkey_path.rstrip("\\") + "\\Assemblies"
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, assemblies, 0, winreg.KEY_READ
            ) as key:
                idx = 0
                while True:
                    try:
                        name = winreg.EnumKey(key, idx)
                    except OSError:
                        break
                    idx += 1
                    m = re.match(r"^0x([0-9a-fA-F]{4,8})$", name)
                    if m:
                        ctf_langids.add(m.group(1)[-4:].lower())
        except (FileNotFoundError, PermissionError, OSError):
            continue

    result = {
        "tips_without_preload": sorted(list_klids - preload_klids),
        "preload_without_tip": sorted(preload_klids - list_klids),
        # FIX-27: возвращаем ПОЛНЫЙ KLID, а не «голый» LANGID (0409).
        # LANGID нужен только для сопоставления с веткой CTF; пользователю
        # показывается тот же KLID, что и в двух других полях, — «0409» в
        # списке раскладок не встречается и выглядит как мусор. Раньше это
        # поле уходило в лог, где несоответствие форматов было незаметно.
        "ctf_missing": sorted(
            klid for klid in list_klids if klid[-4:] not in ctf_langids
        ),
    }

    # FIX-31: сверка с тем, что панель показывает ФАКТИЧЕСКИ. Registry
    # описывает намерение, панель — состояние сессии, и расходятся они в
    # ОБЕ стороны. Прежняя проверка смотрела только на реестр и кеш, оба из
    # которых операция только что правила, — поэтому на реально сломанном
    # переключателе рапортовала «всё в порядке».
    switcher = _get_switcher_klids()
    if switcher is not None:
        intended = list_klids | preload_klids
        result["switcher_missing"] = sorted(intended - switcher)
        result["switcher_stale"] = sorted(switcher - intended)
    for key_name, values in result.items():
        if values:
            logger.warning("Сверка переключателя: %s = %s", key_name, ", ".join(values))
    return result


def is_switcher_consistent(report: dict[str, list[str]] | None) -> bool:
    """Есть ли расхождение, способное скрыть раскладку из переключателя.

    FIX-31: учитываются и «нет в панели», и «в панели, но не в реестре».
    Второе раньше считалось нормой, а на деле означает устаревший профиль
    сессии: раскладка удалена из настроек, но продолжает висеть в
    переключателе до следующего входа в систему.
    """
    if not report:
        return True
    return not any(
        report.get(key)
        for key in (
            "tips_without_preload",
            "preload_without_tip",
            "switcher_missing",
            "switcher_stale",
        )
    )


# ---------------------------------------------------------------------------
# Основной сканер
# ---------------------------------------------------------------------------


def _scan_hkcu_branches(ensure_layout, add_location) -> None:
    """Сканировать ветки HKCU (Preload / Substitutes / рекурсивные)."""
    for subkey_path, scan_type in HKCU_BRANCHES:
        if scan_type == "preload":
            for klid in _get_preload_keys(winreg.HKEY_CURRENT_USER, subkey_path):
                if not _KLID_RE.match(klid):
                    continue
                add_location(klid, f"HKCU\\{subkey_path}", klid)
        elif scan_type == "substitutes":
            subs = _scan_substitutes(winreg.HKEY_CURRENT_USER, subkey_path)
            for src_klid, info in subs.items():
                if not _KLID_RE.match(src_klid):
                    continue
                target = info.get("target", src_klid)
                add_location(
                    src_klid,
                    f"HKCU\\{subkey_path}",
                    f"source={src_klid} -> target={target}",
                )
                if _KLID_RE.match(target):
                    ensure_layout(target)

        # Ветки, где данные лежат в подключах (CTF, User Profile):
        # рекурсивный скан поддерева в дополнение к прямым значениям
        if subkey_path in _RECURSIVE_SCAN:
            for rel, klid, form in _scan_branch_recursive(
                winreg.HKEY_CURRENT_USER, subkey_path
            ):
                add_location(klid, f"HKCU\\{subkey_path}", f"{rel}={form}", form)


def _scan_hklm_branches(add_location) -> None:
    """Сканировать ветки HKLM (каталог раскладок — только Preload)."""
    for subkey_path, scan_type in HKLM_BRANCHES:
        if scan_type == "preload":
            values = _reg_get_string(winreg.HKEY_LOCAL_MACHINE, subkey_path)
            for name, value in values.items():
                if not name:
                    continue
                klid = name.strip().lower()
                if not _KLID_RE.match(klid):
                    continue
                add_location(klid, f"HKLM\\{subkey_path}", f"{name}={value}")


def _scan_hku_branches(add_location) -> None:
    """Сканировать ветки HKU (.DEFAULT Preload)."""
    for subkey_path, scan_type in HKU_BRANCHES:
        if scan_type == "preload":
            for klid in _get_preload_keys(winreg.HKEY_USERS, subkey_path):
                if not _KLID_RE.match(klid):
                    continue
                add_location(klid, f"HKU\\{subkey_path}", klid)


def _scan_powershell_languages(add_location) -> None:
    """Добавить раскладки из Get-WinUserLanguageList (PowerShell)."""
    pw_langs = _get_language_list_from_powershell()
    for entry in pw_langs:
        klid = entry.get("KeyboardLayoutId", "")
        tag = entry.get("LanguageTag", "")
        if not klid:
            continue
        # add_location, а не прямая правка layout_map: LAYOUT_MAP намеренно
        # не мутируется во время скана (запись в глобальный словарь из
        # рабочего потока = гонка с GUI), а запись обязана получить поле
        # form — по нему подробности группируют записи по исходной форме.
        add_location(
            klid,
            "PowerShell\\Get-WinUserLanguageList",
            f"LanguageTag={tag}, KLID={klid}",
            klid,
        )


def scan_keyboard_layouts() -> dict[str, list[dict[str, str]]]:
    """
    Просканировать реестр Windows (HKCU, HKLM, HKU) и PowerShell,
    собрать все найденные раскладки клавиатуры и их пути.

    FIX-34: ключ словаря — канонический KLID раскладки, а не сырой токен
    из реестра. Одна раскладка = одна строка, сколько бы раз Windows ни
    записала её в разные ветки. Сырой код не выбрасывается: он лежит в
    поле ``form`` и показывается в подробностях, поэтому «откуда взялась
    запись» остаётся проверяемым.

    Returns:
        Словарь вида::

            {
                "<KLID_HEX>": [
                    {
                        "path": "HKCU\\...",
                        "value": "...",
                        "form": "исходный токен из реестра",
                    },
                    ...
                ],
                ...
            }
    """
    layout_map: dict[str, dict[str, Any]] = {}

    def _ensure_layout(klid: str) -> None:
        """Создать запись для klid если её нет."""
        if klid not in layout_map:
            layout_map[klid] = {
                "name": _resolve_name(klid),
                "locations": [],
            }

    def _add_location(
        klid: str, path: str, value: str, form: str | None = None
    ) -> None:
        """Добавить путь в locations, запомнив исходную форму токена."""
        _ensure_layout(klid)
        layout_map[klid]["locations"].append(
            {"path": path, "value": value, "form": form or klid}
        )

    _scan_hkcu_branches(_ensure_layout, _add_location)
    _scan_hklm_branches(_add_location)
    _scan_hku_branches(_add_location)
    if not is_sandbox_enabled():
        # В sandbox PowerShell-источник не изолирован: Get-WinUserLanguageList
        # вернул бы ЖИВОЙ список пользователя (мимо sandbox-ключа) — пропускаем.
        _scan_powershell_languages(_add_location)

    # ------------------------------------------------------------------
    # Сортировка результатов по klid
    # ------------------------------------------------------------------
    sorted_map: dict[str, list[dict[str, str]]] = {}
    for klid in sorted(layout_map.keys()):
        sorted_map[klid] = [
            {
                "path": loc["path"],
                "value": loc["value"],
                "form": loc["form"],
            }
            for loc in layout_map[klid]["locations"]
        ]

    logger.info("Сканирование завершено: найдено %d раскладок", len(sorted_map))
    return sorted_map


# ---------------------------------------------------------------------------
# CLI entry-point для ручного тестирования
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(
        description="Сканировать реестр Windows на предмет раскладок клавиатуры."
    )
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="сканировать sandbox-ключ (HKCU\\Software\\KeyboardCleanerTest) "
        "вместо живого реестра",
    )
    parser.add_argument("--json", action="store_true", help="вывести результат в JSON")
    args = parser.parse_args()

    if args.sandbox:
        # Ровно activate_sandbox(), а НЕ enable_sandbox():
        # scan_keyboard_layouts() итерирует МОДУЛЬНЫЕ константы
        # HKCU_BRANCHES/HKU_BRANCHES/_RECURSIVE_SCAN (см. _scan_hkcu_branches),
        # которые подменяет только activate_sandbox(). enable_sandbox() ставил
        # лишь флаг в config — CLI молча сканировал живой реестр вопреки
        # --help. GUI не страдал: main() вызывает activate_sandbox().
        activate_sandbox()

    layouts = scan_keyboard_layouts()
    if args.json:
        print(_json.dumps(layouts, ensure_ascii=False, indent=2))  # noqa: T201
    else:
        for klid in sorted(layouts):
            name = _resolve_name(klid)
            locations = layouts[klid]
            print(f"{klid}  {name}  ({len(locations)} мест)")  # noqa: T201
            for loc in locations:
                print(f"    {loc['path']}: {loc['value']}")  # noqa: T201
