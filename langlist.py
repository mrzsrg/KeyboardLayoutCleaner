"""
langlist.py — Список языков: снимок, применение, восстановление (FIX-10, шаг 3).

Выделено из ``cleaner.py`` по границе, доказуемой тестами: всё, что
общается со списком языков пользователя и его снимком. Реестр сюда НЕ
попадает намеренно: ``Get-WinUserLanguageList``/``Set-WinUserLanguageList``
живут в профиле PowerShell, и .reg-бэкап их не покрывает — поэтому
снимок списка языков существует отдельным файлом рядом с .reg.

Вместе со списком уехал PS-адаптер (``_run_cleanup_script`` и компания):
он существует только ради списка языков, а держать его в cleaner означало бы
разделить «что делаем» и «чем это делаем» между двумя модулями.
"""

import contextlib
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import layout_ids
from applog import resource_path as resource_path
from config import TIMEOUTS, is_sandbox_enabled
from winproc import run_hidden

logger = logging.getLogger("layout_cleaner")

# Путь к PowerShell-скриптам (FIX-20: единый источник истины для ресурсов
# рантайма — applog.resource_path).
_PS_DIR = resource_path("scripts")

# CJK-языки: InputMethodTips содержит GUID вместо KLID, поэтому для них
# KLID берём из LAYOUT_MAP (раскладка по умолчанию для этих тегов).
# Карта — общая с PowerShell-скриптом: она передаётся параметром
# -CjkMapJson из layout_ids.cjk_map_json() (FIX-8), поэтому в .ps1
# больше нет второй копии словаря.
_CJK_FALLBACK_KLIDS: dict[str, str] = layout_ids.CJK_KLIDS

# FIX-25: практическая форма BCP-47 — "ru", "en-US", "zh-Hans-CN",
# "sr-Latn-RS". Склейка нескольких тегов через пробел ("ru en-US") сюда
# не попадает: именно такая строка становилась несуществующим языком в
# Set-WinUserLanguageList и уничтожала список языков пользователя.
# Регулярка продублирована в layout_cleaner_restore.ps1 ($TagPattern) —
# PowerShell остаётся последним рубежом, даже если снимок придёт в обход
# Python; round-trip-тест сверяет оба барьера.
_LANGUAGE_TAG_RE = re.compile(r"^[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*$")



def _parse_ps_plan(output: str) -> dict[str, list[str]]:
    """Разобрать строки DROP|/REMOVELANG| из вывода PS-скрипта."""
    tips: list[str] = []
    langs: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("DROP|"):
            parts = line.split("|", 2)
            if len(parts) == 3:
                tips.append(f"{parts[1]}: {parts[2]}")
        elif line.startswith("REMOVELANG|"):
            parts = line.split("|", 1)
            if len(parts) == 2:
                langs.append(parts[1])
    return {"tips_removed": tips, "languages_removed": langs}



def _write_cjk_map_file() -> str:
    """Записать CJK-карту во временный JSON-файл для PowerShell (FIX-8).

    Раньше словарь ``zh-CN/ja-JP/...`` был продублирован в .ps1; расхождение
    копий давало «сканер нашёл CJK-раскладку, очистка молча пропустила её».
    Теперь единственный источник — ``layout_ids.CJK_KLIDS``.
    """
    handle, path = tempfile.mkstemp(prefix="klc_cjk_", suffix=".json")
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        fh.write(layout_ids.cjk_map_json())
    return path



def _run_cleanup_script(
    layout_klid: str, apply: bool = True
) -> tuple[bool, str, dict[str, list[str]]]:
    """
    Выполнить PS1-скрипт очистки (apply=True) или его dry-run (apply=False).

    KLID, путь к CJK-карте и флаг apply передаются через параметры
    PowerShell, а не через интерполяцию — защита от инъекций.

    Returns
    -------
    tuple[bool, str, dict]
        (успех, человеко-читаемый статус/причина ошибки, план изменений).
    """
    empty: dict[str, list[str]] = {"tips_removed": [], "languages_removed": []}
    script_path = _PS_DIR / "layout_cleaner_cleanup.ps1"
    cjk_map_path = _write_cjk_map_file()
    cmd = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-LayoutKlid",
        layout_klid,
        "-CjkMapJson",
        cjk_map_path,
    ]
    # FIX-25: песочница. Реестр она изолирует, а Set-WinUserLanguageList —
    # WinRT-API, минующий реестр. Флаг запрещает вызов на стороне скрипта:
    # защита не зависит от того, какой путь к нему зайдёт.
    if is_sandbox_enabled():
        cmd.append("-Sandbox")
    if apply:
        cmd.append("-Apply")

    try:
        result = run_hidden(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUTS["powershell_cleanup"],
        )
    except FileNotFoundError:
        return False, "powershell не найден", dict(empty)
    except subprocess.TimeoutExpired:
        return (
            False,
            f"превышен таймаут PowerShell ({TIMEOUTS['powershell_cleanup']} с)",
            dict(empty),
        )
    except OSError as exc:
        return False, f"ошибка запуска PowerShell: {exc}", dict(empty)
    finally:
        # Временная карта не должна копиться в %TEMP%; скрипт её уже прочитал.
        with contextlib.suppress(OSError):
            os.unlink(cjk_map_path)

    output = result.stdout or ""
    if result.returncode != 0:
        stderr_tail = (result.stderr or "").strip()[-300:]
        if stderr_tail:
            detail = f"returncode={result.returncode}; stderr: {stderr_tail}"
        else:
            detail = f"returncode={result.returncode}"
        logger.warning("PS-очистка не удалась: %s", detail)
        return False, detail, dict(empty)

    if "SUCCESS" in output:
        ok, detail = True, "SUCCESS"
    elif "NOCHANGE" in output:
        ok, detail = True, "NOCHANGE"
    elif "DRYRUN" in output:
        ok, detail = True, "DRYRUN"
    elif "SANDBOX_SKIPPED" in output:
        # FIX-25: песочница. Не ошибка и не применение — план доехал до
        # конца, но реальный список языков не тронут.
        ok, detail = True, "SANDBOX_SKIPPED"
    elif "EMPTY_SKIPPED" in output:
        detail = "EMPTY_SKIPPED: список языков нельзя опустошить полностью"
        logger.warning("PS-очистка: %s", detail)
        return False, detail, _parse_ps_plan(output)
    else:
        detail = f"неожиданный вывод PowerShell: {output.strip()[:200]}"
        logger.warning("PS-очистка: %s", detail)
        return False, detail, dict(empty)

    logger.info("PS-очистка: %s", detail)
    return ok, detail, _parse_ps_plan(output)



def _sync_language_list_via_powershell(
    layout_klid: str | None = None,
) -> tuple[bool, str]:
    """
    Синхронизировать список языков через PowerShell.

    Если передан ``layout_klid`` — из списка языков удаляются
    InputMethodTips, соответствующие этому KLID, а языки, оставшиеся
    без раскладок, убираются целиком (Set-WinUserLanguageList -Force).
    Без параметра — просто переустанавливает текущий список.

    Returns
    -------
    tuple[bool, str]
        (успех, статус/причина: SUCCESS | NOCHANGE | текст ошибки).
    """
    if layout_klid:
        ok, detail, _plan = _run_cleanup_script(layout_klid, apply=True)
        return ok, detail

    script_path = _PS_DIR / "layout_cleaner_sync.ps1"
    # FIX-25: песочница не изолирует Set-WinUserLanguageList (WinRT-API
    # минует реестр), поэтому запрещаем вызов и здесь.
    extra_args = ["-Sandbox"] if is_sandbox_enabled() else []
    try:
        result = run_hidden(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                *extra_args,
            ],
            capture_output=True,
            text=True,
            timeout=TIMEOUTS["powershell_cleanup"],
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return False, f"ошибка запуска PowerShell: {exc}"
    out = result.stdout or ""
    if "SANDBOX_SKIPPED" in out:
        logger.info("Песочница: список языков не изменён")
        return True, "SANDBOX_SKIPPED"
    if result.returncode == 0 and "SUCCESS" in out:
        return True, "SUCCESS"
    return False, "не удалось переустановить список языков"



def _backup_language_list(backup_file: Path) -> str:
    """
    Экспортировать текущий список языков в JSON рядом с .reg-бэкапом.

    Реестровый бэкап не покрывает изменения Set-WinUserLanguageList,
    поэтому перед любой модификацией списка языков его снимок сохраняется
    в ``<бэкап>.langlist.json`` — его можно восстановить функцией
    :func:`restore_language_list`.
    """
    script_path = _PS_DIR / "layout_cleaner_backup.ps1"
    json_file = backup_file.with_suffix(".langlist.json")
    try:
        result = run_hidden(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-OutputPath",
                str(json_file),
            ],
            capture_output=True,
            text=True,
            timeout=TIMEOUTS["powershell_backup"],
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Не удалось экспортировать список языков: %s", exc)
        return ""
    if result.returncode != 0:
        logger.warning(
            "Экспорт списка языков: returncode=%s, stderr=%s",
            result.returncode,
            (result.stderr or "").strip()[:200],
        )
        return ""
    if "SUCCESS" not in (result.stdout or ""):
        logger.warning(
            "Экспорт списка языков: PS-скрипт не сообщил об успехе (stdout=%r)",
            (result.stdout or "")[:200],
        )
        return ""
    # JSON пишет сам PS-скрипт в -OutputPath; читаем файл напрямую —
    # не зависим от чистоты stdout (предупреждения PowerShell не сломают парсинг).
    if not json_file.is_file():
        logger.warning("PS-скрипт не создал файл %s", json_file)
        return ""
    logger.info("Список языков сохранён: %s", json_file)
    return str(json_file)



def _parse_langlist_entry(item: Any) -> dict[str, Any] | None:
    """Разобрать один элемент JSON-снимка списка языков (FIX-3).

    Возвращает dict для PS1-скрипта; ``None`` — файл отклоняется целиком
    (политика «без частичного восстановления»).
    """
    if not isinstance(item, dict):
        logger.warning("Некорректный элемент JSON-бэкапа")
        return None
    raw_tag = item.get("LanguageTag", "")
    # FIX-25: НЕскалярный LanguageTag — это дефект снимка, а не формат,
    # который надо починить. Такой файл получал прежний backup.ps1
    # (`@(Get-WinUserLanguageList)`): LanguageTag = ["ru", "en-US"].
    # Код молча брал ПЕРВЫЙ тег, из-за чего снимок доходил до
    # Set-WinUserLanguageList как ОДИН язык "ru" с раскладками обоих
    # языков, и -Force заменял список языков пользователя. «Починка» здесь
    # маскировала потерю языка; теперь файл отклоняется целиком.
    if isinstance(raw_tag, (list, tuple)):
        logger.warning(
            "LanguageTag=%r — массив тегов означает испорченный снимок "
            "(по одному объекту на язык); файл отклонён целиком",
            raw_tag,
        )
        return None
    if not isinstance(raw_tag, str) or not raw_tag.strip():
        logger.warning("Отсутствует корректный LanguageTag")
        return None
    tag = raw_tag.strip()
    # FIX-25: тег должен быть BCP-47. Раньше "ru en-US" (склейка тегов) и
    # другие мусорные значения проходили в PowerShell, где становились
    # несуществующим языком. Теперь отказ до вызова WinRT.
    if not _LANGUAGE_TAG_RE.match(tag):
        logger.warning(
            "Недопустимый LanguageTag=%r (ожидается BCP-47 вида 'ru'/'en-US'); "
            "файл отклонён целиком",
            tag,
        )
        return None
    raw_tips = item.get("InputMethodTips") or []
    # Единичная раскладка может прийти строкой или числом, а не списком.
    if isinstance(raw_tips, (str, int)):
        raw_tips = [raw_tips]
    if not isinstance(raw_tips, list) or any(
        not isinstance(t, (str, int)) or isinstance(t, bool) for t in raw_tips
    ):
        logger.warning("Некорректный InputMethodTips")
        return None
    tips = [str(t) for t in raw_tips]
    # Принимаем KLID-форму (0409:00000409) и GUID-форму TSF (одинарную или
    # двойную: 0411:{GUID}{GUID}).
    tip_re = re.compile(r"^\d{4}:(?:[0-9a-fA-F]{8}|(?:\{[0-9A-Fa-f-]{36}\}){1,2})$")
    bad = next((t for t in tips if not tip_re.match(t)), None)
    if bad is not None:
        logger.warning(
            "TIP неверного формата: %r — файл отклонён целиком "
            "(без частичного восстановления)",
            bad,
        )
        return None

    def _opt_bool(key: str) -> bool | None:
        val = item.get(key)
        if isinstance(val, bool):
            return val
        if isinstance(val, int) and not isinstance(val, bool) and val in (0, 1):
            return bool(val)
        if val is not None:
            logger.warning(
                "Некорректное %s=%r (ожидается 0/1) — поле проигнорировано",
                key,
                val,
            )
        return None

    entry: dict[str, Any] = {"LanguageTag": tag, "InputMethodTips": tips}
    # FIX-3: пропуск сквозь Python изменяемых свойств WinUserLanguage.
    # Старые снимки (без полей) восстанавливаются как раньше — дефолты
    # New-WinUserLanguageList; новые — round-trip возвращает настройки.
    handwriting = _opt_bool("Handwriting")
    if handwriting is not None:
        entry["Handwriting"] = handwriting
    spellchecking = _opt_bool("Spellchecking")
    if spellchecking is not None:
        entry["Spellchecking"] = spellchecking
    return entry



def restore_language_list(json_path: str | Path) -> bool:
    """
    Восстановить список языков из JSON-бэкапа (_backup_language_list).

    Используется для отката изменений, сделанных Set-WinUserLanguageList:
    реестровый .reg не содержит InputMethodTips.
    """
    path = Path(json_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Не удалось прочитать %s: %s", path, exc)
        return False
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        logger.warning(
            "JSON-бэкап %s должен содержать массив языков, получено %s",
            path,
            type(data).__name__,
        )
        return False
    # TIP-валидация (FIX-3): restore раньше доверял содержимому файла целиком,
    # хотя cleanup-скрипт валидирует KLID регуляркой. Принимаем KLID-форму
    # (0409:00000409) и GUID-форму TSF (одинарную или двойную:
    # 0411:{GUID}{GUID}); некорректный TIP отклоняет файл ЦЕЛИКОМ
    # (без частичного восстановления).
    entries: list[dict[str, Any]] = []
    for item in data:
        entry = _parse_langlist_entry(item)
        if entry is None:
            return False
        entries.append(entry)
    if not entries:
        logger.warning("В %s нет корректных записей языков", path)
        return False

    # Пишем JSON-бэкап во временный файл для PS1-скрипта.
    # Используем безопасный mkstemp (не mktemp — TOCTOU / Bandit B306).
    import tempfile

    fd, tmp_name = tempfile.mkstemp(suffix=".json", prefix="klc_restore_")
    os.close(fd)
    tmp_json = Path(tmp_name)
    try:
        tmp_json.write_text(json.dumps(entries), encoding="utf-8")
        script_path = _PS_DIR / "layout_cleaner_restore.ps1"
        # FIX-25: песочница не изолирует Set-WinUserLanguageList, поэтому
        # запрещаем вызов и на уровне вызова, и внутри скрипта.
        restore_cmd = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            "-JsonPath",
            str(tmp_json),
        ]
        if is_sandbox_enabled():
            restore_cmd.append("-Sandbox")
        result = run_hidden(
            restore_cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUTS["powershell_cleanup"],
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Восстановление списка языков не удалось: %s", exc)
        return False
    finally:
        with contextlib.suppress(OSError):
            tmp_json.unlink(missing_ok=True)
    # FIX-25: песочница отвечает SANDBOX_SKIPPED — реальный список не
    # тронут. Для вызывающего это успех (мы ничего не сломали), но
    # сообщаем честно, что восстановление НЕ применялось.
    if "SANDBOX_SKIPPED" in (result.stdout or ""):
        logger.info(
            "Восстановление списка языков: песочница, список не изменён "
            "(%d языков в снимке)",
            len(entries),
        )
        return True
    ok = result.returncode == 0 and "SUCCESS" in (result.stdout or "")
    if not ok and "INVALID_TAG" in (result.stdout or ""):
        logger.warning(
            "Снимок списка языков отклонён как испорченный — реальный "
            "список языков НЕ изменён"
        )
    logger.info(
        "Восстановление списка языков: %s (%d языков)",
        "успех" if ok else "неудача",
        len(entries),
    )
    return ok

