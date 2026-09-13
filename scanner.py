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
from typing import Any

from config import (
    SANDBOX_MODE,
    _SANDBOX_ROOT,
    disable_sandbox,
    is_sandbox_enabled,
)
from winproc import run_hidden

logger = logging.getLogger("layout_cleaner")
if not logger.handlers:
    try:
        logger.addHandler(logging.NullHandler())
    except RecursionError:
        # Защита от рекурсии при мокировании в тестах
        pass

# ---------------------------------------------------------------------------
# Fallback-словарь BCP-47 -> KLID HEX
# ---------------------------------------------------------------------------
LAYOUT_MAP: dict[str, str] = {
    # Russian
    "ru-RU": "00000419",
    # English (US)
    "en-US": "00000409",
    # English (UK)
    "en-GB": "00000809",
    # French
    "fr-FR": "0000040c",
    # German
    "de-DE": "00000407",
    # Italian
    "it-IT": "00000410",
    # Spanish
    "es-ES": "0000040a",
    # Japanese
    "ja-JP": "00000411",
    # Chinese (Simplified)
    "zh-CN": "00000804",
    # Chinese (Traditional)
    "zh-TW": "00000404",
    # Korean
    "ko-KR": "00000412",
    # Arabic
    "ar-SA": "00000401",
    # Finnish
    "fi-FI": "0000040b",
    # Ukrainian
    "uk-UA": "00020422",
    # Belarusian
    "be-BY": "00000423",
    # Estonian
    "et-EE": "0000025f",
    # Lithuanian
    "lt-LT": "00000427",
    # Latvian
    "lv-LV": "00000425",
    # Portuguese (Portugal)
    "pt-PT": "00000816",
    # Turkish
    "tr-TR": "0000041f",
    # --- Расширение fallback (стандартные раскладки: KLID = 0000+LANGID) ---
    # Armenian
    "hy-AM": "0000042b",
    # Georgian
    "ka-GE": "00000437",
    # Hebrew
    "he-IL": "0000040d",
    # Thai
    "th-TH": "0000041e",
    # Vietnamese
    "vi-VN": "0000042a",
    # Indonesian
    "id-ID": "00000421",
    # Malay
    "ms-MY": "0000043e",
    # Persian
    "fa-IR": "00000429",
    # Bulgarian
    "bg-BG": "00000402",
    # Croatian
    "hr-HR": "0000041a",
    # Czech
    "cs-CZ": "00000405",
    # Danish
    "da-DK": "00000406",
    # Dutch
    "nl-NL": "00000413",
    # Greek
    "el-GR": "00000408",
    # Hungarian
    "hu-HU": "0000040e",
    # Norwegian (Bokmål)
    "nb-NO": "00000414",
    # Polish
    "pl-PL": "00000415",
    # Portuguese (Brazil)
    "pt-BR": "00000416",
    # Romanian
    "ro-RO": "00000418",
    # Serbian (Cyrillic)
    "sr-Cyrl-RS": "00000c1a",
    # Serbian (Latin)
    "sr-Latn-RS": "0000081a",
    # Slovak
    "sk-SK": "0000041b",
    # Slovenian
    "sl-SI": "00000424",
    # Swedish
    "sv-SE": "0000041d",
}


def _safe_str(val: object) -> str:
    """
    Безопасное строковое представление значения реестра.

    REG_BINARY/REG_NONE и прочие «нестроковые» типы не должны ломать
    скан (UnicodeDecodeError/TypeError при str() от экзотических типов)
    — возвращаем repr-подобную строку, которую KLID-парсер отбросит.
    """
    try:
        return str(val).strip()
    except Exception as exc:  # noqa: BLE001 — защита от любых экзотических типов
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
    """Считать ключи Preload и вернуть {hex_value: hex_value}."""
    result: dict[str, str] = {}
    values = _reg_get_string(root_key, subkey_path)
    for _idx, val in values.items():
        hex_code = val.strip().split("\\")[-1].lower() if val else ""
        if not hex_code:
            # Пустое значение — пропускаем, чтобы не создавать фантомные записи
            continue
        result[hex_code] = hex_code
    return result


def _scan_substitutes(
    root_key: int, subkey_path: str
) -> dict[str, dict[str, str]]:
    """Считать ключи Substitutes и вернуть {source_klid: {"target": ...}}."""
    result: dict[str, dict[str, str]] = {}
    values = _reg_get_string(root_key, subkey_path)
    for src, target in values.items():
        # Нормализуем регистр: имена значений Substitutes в реестре
        # встречаются и в верхнем регистре (D001DEAD), а KLID везде
        # сравниваются в нижнем
        src_norm = src.strip().lower()
        target_hex = target.strip().split("\\")[-1].lower() if target else ""
        if src_norm and target_hex:
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
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SettingSync\\Namespace\\Language", "preload", False),
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
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SettingSync\\Namespace\\Language", "preload", False),
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
    for root_name, subkey, scan_type, admin_required in AFFECTED_BRANCHES:
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
# sandbox-mode включается через scanner.SANDBOX_MODE = True + patch.
def get_affected_branches() -> list[tuple[str, str, str, bool]]:
    """Публичный аксессор: актуальный список веток с учётом sandbox.

    ЕДИНАЯ точка истины для scanner, cleaner и GUI. При активном
    sandbox-режиме (флаг модуля, config.SANDBOX_MODE или переменная
    окружения SANDBOX_MODE) возвращает sandbox-ветки, иначе — реальные.
    Модули-потребители НЕ должны импортировать AFFECTED_BRANCHES напрямую:
    `from scanner import AFFECTED_BRANCHES` фиксирует список на момент
    импорта и не переключается вместе с sandbox (источник утечки P0-1).
    """
    if SANDBOX_MODE or is_sandbox_enabled():
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
        _SANDBOX_ROOT + "\\Software\\Microsoft\\Windows\\CurrentVersion\\SettingSync\\Namespace\\Language",
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
    globals()["SANDBOX_MODE"] = True


def deactivate_sandbox() -> None:
    """Вернуть реальные ветки реестра (используется тестами).

    Обратная операция к :func:`activate_sandbox`: восстанавливает все
    подменённые константы из оригиналов и сбрасывает флаги sandbox
    (scanner.SANDBOX_MODE, config.SANDBOX_MODE, переменная окружения).
    """
    global AFFECTED_BRANCHES, HKCU_BRANCHES, HKU_BRANCHES
    global HKLM_BRANCHES, _RECURSIVE_SCAN

    AFFECTED_BRANCHES = [tuple(b) for b in _ORIGINAL_AFFECTED_BRANCHES]
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
    globals()["SANDBOX_MODE"] = False
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


# Нормализация decimal-HKL: CTF хранит KeyboardLayout десятичным числом
_CTF_LANGID_DIR_RE = re.compile(r"0x([0-9a-f]{8})")


def _normalize_klid_token(raw: str, key_path: str) -> str:
    """
    Привести KLID-подобный токен из CTF/User Profile к канонической 8-HEX форме.

    CTF хранит HKL (старшее слово — LANGID языка, младшее — KLID
    раскладки) в ДЕСЯТИЧНОМ виде: 68748313 == 0x04190419 (язык 0419 +
    раскладка 00000419); короткий decimal 1033 == 0x00000409 — тот же
    KLID. Строка из одних цифр без ведущего нуля двусмысленна, поэтому
    выбирается интерпретация (decimal → hex или чтение как hex), у которой
    старшее слово совпадает с LANGID родительского ключа ``0x????????``
    из пути; без контекста — decimal-чтение.
    """
    s = raw.strip().lower()
    if not s or len(s) > 10 or not re.fullmatch(r"[0-9a-f]+", s):
        return s
    # Символы a-f — это уже hex-форма (00000809, 04190419): возвращаем как есть
    if any(ch in "abcdef" for ch in s):
        return s
    # Только цифры. 8-значная строка с ведущим нулём — hex-KLID (00000409)
    if len(s) == 8 and s[0] == "0":
        return s
    # "0409"-стиль (4 hex-цифры с ведущим нулём) — это LANGID = KLID языка
    if len(s) >= 3 and len(s) < 8 and s[0] == "0":
        try:
            return f"{int(s, 16):08x}"
        except ValueError:
            return s
    readings = [int(s, 10)]
    if len(s) >= 8:
        readings.append(int(s, 16))
    lang = _CTF_LANGID_DIR_RE.search(key_path.lower())
    parent = int(lang.group(1), 16) & 0xFFFF if lang else None
    for cand in readings:
        if cand <= 0 or cand > 0xFFFFFFFF:
            continue
        if parent is not None and ((cand >> 16) & 0xFFFF) == parent:
            return f"{cand:08x}"
    # Без контекста — decimal-чтение (1033 → 00000409, 68748313 → 04190419)
    cand = readings[0]
    if 0 < cand <= 0xFFFFFFFF:
        return f"{cand:08x}"
    return s


# Обратный словарь KLID → список BCP-47 тегов (для сопоставления в SettingSync)
_KLID_TO_TAGS: dict[str, list[str]] = {}
for _tag, _klid in LAYOUT_MAP.items():
    _KLID_TO_TAGS.setdefault(_klid.lower(), []).append(_tag.lower())


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
) -> list[tuple[str, str]]:
    """
    Рекурсивно найти в поддереве значения, содержащие KLID.

    Значения могут быть в форме ``00000409`` или ``0409:00000409``
    (LANGID:KLID, как в User Profile/CTF). REG_BINARY (SortOrder\\Language)
    осознанно не парсится — порядок в трее перестраивает
    Set-WinUserLanguageList (шаг 3 очистки).

    Returns
    -------
    list[tuple[str, str]]
        Пары ``(относительный путь\\имя значения, klid)``.
    """
    found: list[tuple[str, str]] = []

    def _walk(key_path: str, depth: int) -> None:
        if depth > max_depth:
            logger.debug("Максимальная глубина скана: %s", key_path)
            return
        try:
            with winreg.OpenKey(
                root_key, key_path, 0, winreg.KEY_READ
            ) as key:
                idx = 0
                while True:
                    try:
                        name, val, _ = winreg.EnumValue(key, idx)
                    except OSError:
                        break
                    raw = str(val).strip()
                    klid = ""
                    tip_match = _TIP_KLID_RE.match(raw)
                    if tip_match:
                        klid = tip_match.group(1).lower()
                    elif _KLID_RE.match(raw) or re.fullmatch(
                        r"[0-9]{3,10}", raw
                    ):
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
                        rel = key_path[len(subkey_path):].lstrip("\\")
                        found.append(
                            (f"{rel}\\{name}" if rel else name, klid)
                        )
                    idx += 1
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
    """Получить человеко-читаемое название для HEX-кода раскладки."""
    # 1. Попробовать через HKLM (Layout Text)
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            "SYSTEM\\CurrentControlSet\\Control\\Keyboard Layouts",
        ) as layouts_key:
            with winreg.OpenKey(layouts_key, hex_code) as layout_key:
                name, _ = winreg.QueryValueEx(layout_key, "Layout Text")
                return name
    except FileNotFoundError:
        pass
    except OSError:
        pass

    # 2. Fallback: ищем по LAYOUT_MAP
    for tag, code in LAYOUT_MAP.items():
        if code == hex_code:
            return tag

    return f"Layout ({hex_code})"


def get_layout_name(hex_code: str) -> str:
    """Публичное имя раскладки по HEX-коду (для GUI и внешних модулей)."""
    return _resolve_name(hex_code.strip().lower())


# ---------------------------------------------------------------------------
# PowerShell integration
# ---------------------------------------------------------------------------


# InputMethodTips имеют вид "0419:00000419" (LANGID:KLID) для обычных
# раскладок и "0411:{GUID}" — для CJK IME-раскладок.
_TIP_KLID_RE = re.compile(r"^[0-9A-Fa-f]{4}:([0-9A-Fa-f]{8})$")


# Подлинный KLID — ровно 8 HEX-символов (отсеивает мусорные значения
# вида "1" или "ru-RU en-GB", встречающиеся в User Profile / CTF)
_KLID_RE = re.compile(r"^[0-9a-f]{8}$")


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
    script = r"""
$langs = Get-WinUserLanguageList;
foreach ($l in $langs) {
    $tips = @($l.InputMethodTips) -join ';';
    Write-Output ("{0}`t{1}" -f $l.LanguageTag, $tips);
}
"""
    try:
        result = run_hidden(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            logger.warning(
                "Get-WinUserLanguageList: returncode=%s, stderr=%s",
                result.returncode,
                (result.stderr or "").strip()[:200],
            )
            return []

        return _parse_language_entries(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("PowerShell недоступен: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Основной сканер
# ---------------------------------------------------------------------------


def scan_keyboard_layouts() -> dict[str, list[dict[str, str]]]:
    """
    Просканировать реестр Windows (HKCU, HKLM, HKU) и PowerShell,
    собрать все найденные раскладки клавиатуры и их пути.

    Returns:
        Словарь вида::

            {
                "<KLID_HEX>": [
                    {"path": "HKCU\\...", "value": "..."},
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

    def _add_location(klid: str, path: str, value: str) -> None:
        """Добавить путь в locations."""
        _ensure_layout(klid)
        layout_map[klid]["locations"].append({"path": path, "value": value})

    # ------------------------------------------------------------------
    # 1. Сканирование HKCU веток
    # ------------------------------------------------------------------
    for subkey_path, scan_type in HKCU_BRANCHES:
        if scan_type == "preload":
            for klid in _get_preload_keys(winreg.HKEY_CURRENT_USER, subkey_path):
                if not _KLID_RE.match(klid):
                    continue
                _add_location(klid, f"HKCU\\{subkey_path}", klid)
        elif scan_type == "substitutes":
            subs = _scan_substitutes(winreg.HKEY_CURRENT_USER, subkey_path)
            for src_klid, info in subs.items():
                if not _KLID_RE.match(src_klid):
                    continue
                target = info.get("target", src_klid)
                _add_location(
                    src_klid,
                    f"HKCU\\{subkey_path}",
                    f"source={src_klid} -> target={target}",
                )
                if _KLID_RE.match(target):
                    _ensure_layout(target)

        # Ветки, где данные лежат в подключах (CTF, User Profile):
        # рекурсивный скан поддерева в дополнение к прямым значениям
        if subkey_path in _RECURSIVE_SCAN:
            for rel, klid in _scan_branch_recursive(
                winreg.HKEY_CURRENT_USER, subkey_path
            ):
                _add_location(
                    klid, f"HKCU\\{subkey_path}", f"{rel}={klid}"
                )

    # ------------------------------------------------------------------
    # 2. Сканирование HKLM веток
    # ------------------------------------------------------------------
    for subkey_path, scan_type in HKLM_BRANCHES:
        if scan_type == "preload":
            values = _reg_get_string(winreg.HKEY_LOCAL_MACHINE, subkey_path)
            for name, value in values.items():
                if name:
                    klid = name.strip().lower()
                    if not _KLID_RE.match(klid):
                        continue
                    if klid:
                        _add_location(
                            klid,
                            f"HKLM\\{subkey_path}",
                            f"{name}={value}",
                        )

    # ------------------------------------------------------------------
    # 3. Сканирование HKU веток
    # ------------------------------------------------------------------
    for subkey_path, scan_type in HKU_BRANCHES:
        if scan_type == "preload":
            for klid in _get_preload_keys(winreg.HKEY_USERS, subkey_path):
                if not _KLID_RE.match(klid):
                    continue
                _add_location(klid, f"HKU\\{subkey_path}", klid)

    # ------------------------------------------------------------------
    # 4. PowerShell: Get-WinUserLanguageList
    # ------------------------------------------------------------------
    pw_langs = _get_language_list_from_powershell()
    for entry in pw_langs:
        klid = entry.get("KeyboardLayoutId", "")
        tag = entry.get("LanguageTag", "")
        if klid:
            # LAYOUT_MAP намеренно не мутируется во время скана:
            # запись в глобальный словарь из рабочего потока = гонка с GUI
            _ensure_layout(klid)
            layout_map[klid]["locations"].append(
                {
                    "path": "PowerShell\\Get-WinUserLanguageList",
                    "value": f"LanguageTag={tag}, KLID={klid}",
                }
            )

    # ------------------------------------------------------------------
    # Сортировка результатов по klid
    # ------------------------------------------------------------------
    sorted_map: dict[str, list[dict[str, str]]] = {}
    for klid in sorted(layout_map.keys()):
        sorted_map[klid] = [
            {"path": loc["path"], "value": loc["value"]}
            for loc in layout_map[klid]["locations"]
        ]

    logger.info("Сканирование завершено: найдено %d раскладок", len(sorted_map))
    return sorted_map


# ---------------------------------------------------------------------------
# CLI entry-point для ручного тестирования
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    layouts = scan_keyboard_layouts()
    print(f"Found {len(layouts)} keyboard layout(s):\n")
    for klid in layouts:
        name = _resolve_name(klid)
        print(f"  [{klid}] {name}")
        for loc in layouts[klid]:
            print(f"    -> {loc['path']}")
            print(f"       value: {loc['value']}")
