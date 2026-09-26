"""
layout_ids.py — Единый резолвер идентификаторов раскладок (FIX-8).

Модуль собирает ВСЮ таблицу знаний об идентификаторах раскладок в одном
месте, откуда её импортируют и сканер, и cleaner:

* ``LAYOUT_MAP`` — BCP-47 тег -> 8-HEX KLID (fallback, если HKLM недоступен);
* ``normalize_klid_token`` — приведение токена из CTF/User Profile к KLID;
* ``klid_variants`` — все строковые представления KLID в реестре;
* ``klid_to_tags`` / ``bcp47_to_klid`` — обратное соответствие;
* ``parse_tip`` — разбор ``InputMethodTips`` (``LANGID:KLID``);
* ``decimal_to_hkl`` / ``hkl_to_decimal`` — HKL десятичным числом;
* ``CJK_KLIDS`` — CJK-подмножество ``LAYOUT_MAP`` для PowerShell.

Причина выделения модуля: раньше нормализаторы дублировались между
``scanner.py`` (``_normalize_klid_token``, ``_TIP_KLID_RE``, ``_KLID_RE``) и
``cleaner.py`` (``_klid_variants``, ``_TIP_KLID_RE``), а CJK-словарь ещё и в
PowerShell-скрипте. Расхождение любой копии означало «сканер нашёл, cleaner
не удалил» (или наоборот), а инвариант на это не смотрел. Теперь единственный
источник — этот файл; ``scanner``/``cleaner`` держат тонкие обёртки, чтобы
не ломать существующие вызовы и тесты.
"""

import contextlib
import json
import re

# ---------------------------------------------------------------------------
# Таблицы соответствий
# ---------------------------------------------------------------------------

# Fallback-словарь BCP-47 -> KLID HEX. Используется, когда имя раскладки
# недоступно в HKLM\SYSTEM\CurrentControlSet\Control\Keyboard Layouts.
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
    # Norwegian (Bokmal)
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

# Языки, для которых InputMethodTips хранит GUID вместо KLID (CJK IME).
# Для них KLID берётся из LAYOUT_MAP по тегу языка — и в Python, и в
# PowerShell (карта передаётся параметром -CjkMapJson, FIX-8), поэтому
# литеральный словарь в .ps1 больше не нужен.
_CJK_LANGUAGE_PREFIXES = frozenset({"ja", "zh", "ko"})

# CJK-подмножество LAYOUT_MAP: тег -> KLID.
CJK_KLIDS: dict[str, str] = {
    tag: klid
    for tag, klid in LAYOUT_MAP.items()
    if tag.split("-")[0] in _CJK_LANGUAGE_PREFIXES
}

# Обратный словарь KLID -> список BCP-47 тегов (сопоставление в SettingSync)
KLID_TO_TAGS: dict[str, list[str]] = {}
for _tag, _klid in LAYOUT_MAP.items():
    KLID_TO_TAGS.setdefault(_klid.lower(), []).append(_tag.lower())

# Индекс «тег в нижнем регистре -> KLID»: теги приходят из PowerShell
# (``ru-RU``) и из реестра в произвольном регистре (``ru-ru``), а искать
# приходится оба раза. Без него один и тот же тег давал бы KLID при одном
# вызове и пустую строку при другом — тот самый класс расхождения, который
# и снимает FIX-8.
_TAG_INDEX: dict[str, str] = {tag.lower(): klid for tag, klid in LAYOUT_MAP.items()}


# ---------------------------------------------------------------------------
# Регулярные выражения
# ---------------------------------------------------------------------------

# Подлинный KLID — ровно 8 HEX-символов (отсеивает мусорные значения
# вида "1" или "ru-RU en-GB", встречающиеся в User Profile / CTF)
KLID_RE = re.compile(r"^[0-9a-f]{8}$")

# InputMethodTips имеют вид "0419:00000419" (LANGID:KLID) для обычных
# раскладок и "0411:{GUID}" — для CJK IME-раскладок (KLID не извлекается).
TIP_KLID_RE = re.compile(r"^[0-9A-Fa-f]{4}:([0-9A-Fa-f]{8})$")

# Имена значений-МЕТАДАННЫХ языкового профиля — НЕ раскладки, даже если
# содержимое выглядит как KLID. Классический пример:
#   ...\User Profile\en-US\FeaturesToInstall = 000006ff
# — это битовая маска feature-флагов, а не KLID; до этой правки сканер
# ошибочно показывал "Layout (000006ff)". Имена значений реестра не
# чувствительны к регистру — сравниваем в lowercase.
METADATA_VALUE_NAMES: frozenset[str] = frozenset(
    {
        "featurestoinstall",  # битовая маска флагов (напр. "000006ff")
        "cachedlanguagename",  # отображаемое имя языка
        "showcasing",  # опции отображения (Languages)
        "windowsoverride",  # переопределение языка интерфейса (тег, не раскладка)
    }
)

# Имя родительского ключа CTF содержит LANGID в виде 0x????????.
CTF_LANGID_DIR_RE = re.compile(r"0x([0-9a-f]{8})")


# ---------------------------------------------------------------------------
# Нормализация и варианты KLID
# ---------------------------------------------------------------------------


def normalize_klid_token(raw: str, key_path: str = "") -> str:
    """
    Привести KLID-подобный токен из CTF/User Profile к канонической 8-HEX форме.

    CTF хранит HKL (старшее слово — LANGID языка, младшее — KLID
    раскладки) в ДЕСЯТИЧНОМ виде: 68748313 == 0x04190419 (язык 0419 +
    раскладка 00000419); короткий decimal 1033 == 0x00000409 — тот же
    KLID. Строка из одних цифр без ведущего нуля двусмысленна, поэтому
    выбирается интерпретация (decimal -> hex или чтение как hex), у которой
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
    lang = CTF_LANGID_DIR_RE.search(key_path.lower())
    parent = int(lang.group(1), 16) & 0xFFFF if lang else None
    for cand in readings:
        if cand <= 0 or cand > 0xFFFFFFFF:
            continue
        if parent is not None and ((cand >> 16) & 0xFFFF) == parent:
            return f"{cand:08x}"
    # Без контекста — decimal-чтение (1033 -> 00000409, 68748313 -> 04190419)
    cand = readings[0]
    if 0 < cand <= 0xFFFFFFFF:
        return f"{cand:08x}"
    return s


def klid_hex_forms(layout_klid: str) -> set[str]:
    """
    Представления KLID, безопасные для прямых веток реестра (FIX-22).

    Preload/Substitutes хранят 8-HEX-строки, поэтому «голый» десятичный
    LANGID здесь не нужен и вреден: ``str(int(k, 16))`` даёт ``1033`` для
    ``00000409`` и ``1049`` для ``00000419``, а в Substitutes KLID лежит в
    ИМЕНИ значения. Значение-фантом вида ``d0011049`` содержало бы подстроку
    ``1049`` и удалялось бы вместе с целевой раскладкой.

    Сохранена обработка десятичного HKL на входе (критерий FIX-22): ``67699721``
    — это HKL ``0x04090409``, поэтому добавляется его hex-прочтение.
    """
    k = layout_klid.strip().lower()
    forms = {k}
    if not KLID_RE.match(k):
        return forms
    try:
        hex_val = int(k, 16)
    except ValueError:
        return forms
    forms.add(f"{hex_val:08x}")
    if k.isdigit() and k[0] != "0":
        # Ввод неоднозначен: все цифры без ведущего нуля могут быть десятичной
        # записью HKL. HKL = (LANGID << 16) | KLID, поэтому восстанавливаем
        # ОБЕ формы: полный HKL в hex и канонический KLID из младшего слова
        # (67699721 == 0x04090409 -> KLID 00000409).
        dec_val = int(k, 10)
        if 0 < dec_val <= 0xFFFFFFFF:
            forms.add(f"{dec_val:08x}")
            forms.add(f"{dec_val & 0xFFFF:08x}")
    return forms


def klid_variants(layout_klid: str) -> set[str]:
    """
    Все представления KLID, включая десятичную форму HKL (для CTF).

    Отличие от :func:`klid_hex_forms` — одно намеренное дополнение:
    ``str(int(k, 16))``, то есть запись HKL десятичным числом. CTF хранит
    ``KeyboardLayout`` именно так (``68748313`` == ``0x04190419``), и без
    этого очистка перестала бы находить такие записи. Сравнение в CTF
    идёт ТОЧНО (без поиска подстроки), поэтому лишних совпадений здесь
    не возникает — в отличие от Substitutes, где KLID стоит в имени.

    Прямые ветки (Preload/Substitutes) используют :func:`klid_hex_forms`.
    """
    forms = klid_hex_forms(layout_klid)
    k = layout_klid.strip().lower()
    if KLID_RE.match(k):
        with contextlib.suppress(ValueError):
            forms.add(str(int(k, 16)))
    return forms


# Разделители, по которым значение реестра дробится на токены. Сравнение
# токенов ЦЕЛИКОМ вместо поиска подстроки (FIX-22): иначе вариант «1049»
# совпал бы с «d0011049» и удалил чужую запись.
_VALUE_TOKEN_SEPARATORS = re.compile(r"[\\:;,]")


def value_tokens(value: str) -> set[str]:
    """Токены значения реестра в нижнем регистре (пустые отброшены)."""
    return {
        token.strip().lower()
        for token in _VALUE_TOKEN_SEPARATORS.split(value)
        if token.strip()
    }


def substitutes_value_matches(
    value_name: str, value: str, forms: set[str]
) -> bool:
    """ЕДИНЫЙ предикат сопоставления для ветки ``Keyboard Layout\\Substitutes``.

    В Substitutes KLID лежит в ИМЕНИ значения, а в значении — тоже KLID
    (цель подстановки), иногда в составной форме (``0409:00000409``,
    ``00000409\\0000040c``). Сравниваем имя целиком и каждый ТОКЕН значения
    целиком.

    Раньше одна и та же логика была написана в четырёх местах
    (``_value_matches``, ``_clean_substitutes_keys``, ``_branch_value_matches``,
    ``_scan_remaining_substitutes``) и различалась: в ``_branch_value_matches``
    не было проверки точного совпадения значения, а везде искалась подстрока.
    Расхождение предикатов означает «план говорит одно, дрейф считает другое» —
    именно тот класс дефекта, который закрывает FIX-7.
    """
    if value_name.strip().lower() in forms:
        return True
    return bool(value_tokens(value) & forms)


def klid_matches_value(layout_klid: str, value: str) -> bool:
    """
    Проверить, соответствует ли значение реестра ``layout_klid``.

    Проверяет как прямое совпадение KLID (8 HEX, десятичный HKL, формат
    InputMethodTips), так и совпадение по BCP-47 тегу через LAYOUT_MAP
    (актуально для SettingSync, где хранятся теги языков).
    """
    val_l = value.strip().lower()
    if not val_l:
        return False
    if KLID_RE.match(val_l) and val_l == layout_klid:
        return True
    tip = TIP_KLID_RE.match(val_l)
    if tip and tip.group(1).lower() == layout_klid:
        return True
    return val_l in KLID_TO_TAGS.get(layout_klid, [])


# ---------------------------------------------------------------------------
# BCP-47 <-> KLID
# ---------------------------------------------------------------------------


def klid_to_tags(layout_klid: str) -> set[str]:
    """BCP-47-теги, чьей известной раскладкой является ``layout_klid``."""
    klid_norm = layout_klid.strip().lower()
    return {tag for tag, klid in LAYOUT_MAP.items() if klid.lower() == klid_norm}


def bcp47_to_klid(tag: str) -> str:
    """KLID по BCP-47-тегу (пустая строка, если тег неизвестен).

    Регистр не важен: теги приходят и из PowerShell (``ru-RU``), и из
    реестра в произвольном регистре, а искать приходится оба раза.
    """
    return _TAG_INDEX.get(tag.strip().lower(), "")


def cjk_klids() -> dict[str, str]:
    """Копия CJK-карты: безопасна для передачи наружу (JSON/PowerShell)."""
    return dict(CJK_KLIDS)


def cjk_map_json() -> str:
    """CJK-карта в формате JSON — единственный источник для PowerShell."""
    return json.dumps(CJK_KLIDS, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# TIP и HKL
# ---------------------------------------------------------------------------


def parse_tip(tip: str) -> str:
    """
    KLID из InputMethodTips вида ``0409:00000409``.

    Для CJK-раскладок tip имеет вид ``0411:{GUID}`` — KLID не извлекается
    (возвращается пустая строка), вызывающая сторона использует LAYOUT_MAP
    по тегу языка.
    """
    match = TIP_KLID_RE.match(tip.strip())
    return match.group(1).lower() if match else ""


def decimal_to_hkl(langid: int, base_low: int) -> int:
    """HKL = (LANGID << 16) | младшее слово KLID (как это пишет CTF)."""
    return (langid << 16) | (base_low & 0xFFFF)


def hkl_to_decimal(hkl: int) -> tuple[int, int]:
    """Разложить HKL на пару ``(LANGID, младшее слово)``."""
    return (hkl >> 16) & 0xFFFF, hkl & 0xFFFF


def klid_low_word(layout_klid: str) -> int:
    """Младшее слово KLID (0, если токен не является KLID)."""
    match = KLID_RE.match(layout_klid.strip().lower())
    return int(match.group(0), 16) & 0xFFFF if match else 0


def hkl_string_forms(langid: int, base_low: int) -> set[str]:
    """Строковые формы HKL для конкретного LANGID (десятичная и 8-HEX)."""
    hkl = decimal_to_hkl(langid, base_low)
    return {str(hkl), f"{hkl:08x}"}
