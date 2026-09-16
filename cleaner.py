"""
cleaner.py — Модуль бэкапа и безопасного удаления раскладок клавиатуры (Этап 2).

Реализует:
  - is_admin()       — проверка прав администратора через API WinAPI.
  - backup_registry() — экспорт веток реестра в .reg (UTF-16 LE) через subprocess.
  - delete_layout()   — удаление записей из реестра + синхронизация профиля через PowerShell.
"""

import contextlib
import ctypes
import datetime
import json
import logging
import os
import re
import subprocess
import tempfile
import winreg
from pathlib import Path
from typing import Any

import applog
import scanner
from applog import get_app_dir
from config import _SANDBOX_ROOT, disable_sandbox, enable_sandbox, is_sandbox_enabled
from scanner import LAYOUT_MAP, _get_preload_keys
from winproc import run_hidden

# ---------------------------------------------------------------------------
# Загрузка PowerShell-скриптов из файлов (scripts/*.ps1)
# ---------------------------------------------------------------------------
_PS_DIR = Path(__file__).parent / "scripts"

logger = logging.getLogger("layout_cleaner")
applog.setup_null_handler(logger)


def _load_ps1_script(name: str) -> str:
    """Загрузить PowerShell-скрипт из scripts/*.ps1.

    Parameters
    ----------
    name : str
        Имя файла без расширения (например, ``layout_cleaner_cleanup``).

    Returns
    -------
    str
        Содержимое файла.

    Raises
    ------
    FileNotFoundError
        Если файл скрипта не найден.
    """
    path = _PS_DIR / f"{name}.ps1"
    if not path.exists():
        msg = (
            f"PowerShell-скрипт не найден: {path}. "
            f"Проверьте, что папка scripts/ существует и содержит {name}.ps1."
        )
        logger.error(msg)
        raise FileNotFoundError(msg)
    content = path.read_text(encoding="utf-8")
    logger.debug("Загружен PS1-скрипт: %s (%d байт)", name, len(content))
    return content


# ---------------------------------------------------------------------------
# Режим песочницы — синхронизируется с scanner.SANDBOX_MODE через config
# ---------------------------------------------------------------------------
# SANDBOX_MODE импортируется из config для обратной совместимости


class BackupError(RuntimeError):
    """
    Бэкап реестра не создан (ни одна ветка не экспортирована).

    delete_layout обязан перехватить это исключение и ОТМЕНИТЬ удаление:
    изменять реестр без возможности отката недопустимо.
    """


# Корни реестра по имени (элементы scanner.AFFECTED_BRANCHES)
_ROOT_CONST: dict[str, int] = {
    "HKCU": winreg.HKEY_CURRENT_USER,
    "HKU": winreg.HKEY_USERS,
    "HKLM": winreg.HKEY_LOCAL_MACHINE,
}

# Ветка User Profile хранит языки BCP-47-ТЕГАМИ (en-GB), а не KLID —
# для неё есть отдельная очистка тегов (см. _clean_intl_profile)
# ВАЖНО: эти константы описывают РЕАЛЬНЫЕ ветки и никогда не мутируются.
# Sandbox-варианты вычисляются резолверами ниже (_intl_subkey, _ctf_subkey,
# _recursive_subkeys) — см. регрессию P0-1 (утечка sandbox в реальные ветки).
_INTL_SUBKEY = "Control Panel\\International\\User Profile"
_CTF_SUBKEY = "Software\\Microsoft\\CTF"

# Ветки, где данные лежат в подключах — чистятся рекурсивно
_RECURSIVE_SUBKEYS = {
    _INTL_SUBKEY,
    _CTF_SUBKEY,
    "Software\\Microsoft\\Windows\\CurrentVersion\\SettingSync\\Namespace\\Language",
}

# Максимальная глубина рекурсивной очистки (защита от аномальных деревьев).
# 6 хватает до SortOrder\AssemblyItem\<LANGID>\{GUID}\00000000 (глубина 5)
_MAX_CLEAN_DEPTH = 6


# ---------------------------------------------------------------------------
# Sandbox-осведомлённые резолверы веток
# ---------------------------------------------------------------------------
def _sandbox_active() -> bool:
    """Активен ли sandbox-режим (config-флаг или окружение)."""
    return is_sandbox_enabled()


def _affected_branches() -> list[tuple[str, str, str, bool]]:
    """Актуальный список веток — делегирует scanner.get_affected_branches().

    Единственная точка входа для delete_layout/plan_layout_removal: при
    активном sandbox возвращает sandbox-ветки, иначе — реальные.
    """
    return scanner.get_affected_branches()


def _intl_subkey() -> str:
    """Ветка User Profile с учётом sandbox."""
    if _sandbox_active():
        return _SANDBOX_ROOT + "\\" + _INTL_SUBKEY
    return _INTL_SUBKEY


def _ctf_subkey() -> str:
    """Ветка CTF с учётом sandbox."""
    if _sandbox_active():
        return _SANDBOX_ROOT + "\\" + _CTF_SUBKEY
    return _CTF_SUBKEY


def _recursive_subkeys() -> set[str]:
    """Множество рекурсивно очищаемых веток с учётом sandbox."""
    if _sandbox_active():
        return _build_sandbox_recursive_subkeys()
    return set(_RECURSIVE_SUBKEYS)


def _build_sandbox_recursive_subkeys() -> set[str]:
    """Sandbox-вариант _RECURSIVE_SUBKEYS."""
    return {
        _SANDBOX_ROOT + "\\" + _INTL_SUBKEY,
        _SANDBOX_ROOT + "\\" + _CTF_SUBKEY,
        _SANDBOX_ROOT
        + "\\"
        + "Software\\Microsoft\\Windows\\CurrentVersion\\SettingSync\\Namespace\\Language",
    }


def _paths_to_backup(
    branches: list[tuple[str, str, str, bool]], admin_privileges: bool
) -> list[dict[str, str]]:
    """Пути для бэкапа: все ветки, доступные при текущих правах."""
    return [
        {"root": root, "subkey": subkey}
        for root, subkey, _mode, admin_required in branches
        if not admin_required or admin_privileges
    ]


# Поле отчёта для РЕАЛЬНЫХ веток (delete_layout-результат)
_REPORT_FIELD: dict[tuple[str, str], str] = {
    ("HKCU", "Keyboard Layout\\Preload"): "hkcu_preload_deleted",
    ("HKCU", "Keyboard Layout\\Substitutes"): "hkcu_substitutes_deleted",
    ("HKCU", _INTL_SUBKEY): "hkcu_intl_deleted",
    ("HKCU", _CTF_SUBKEY): "hkcu_ctf_deleted",
    (
        "HKCU",
        "Software\\Microsoft\\Windows\\CurrentVersion\\SettingSync\\Namespace\\Language",
    ): "hkcu_settingsync_deleted",
    ("HKU", ".DEFAULT\\Keyboard Layout\\Preload"): "hku_default_deleted",
}


def _report_field_for(root: str, subkey: str) -> str:
    """Поле отчёта для ветки (в sandbox ключи префиксируются — маппим по суффиксу)."""
    if (root, subkey) in _REPORT_FIELD:
        return _REPORT_FIELD[(root, subkey)]
    rel = subkey
    # Сначала более длинный префикс (HKU/HKLM-ветки перенаправлены
    # в _SANDBOX_ROOT\test\...), затем общий sandbox-префикс
    for prefix in (_SANDBOX_ROOT + "\\test\\", _SANDBOX_ROOT + "\\"):
        if rel.startswith(prefix):
            rel = rel[len(prefix) :]
            break
    for (_r, sk), field in _REPORT_FIELD.items():
        if sk == rel:
            return field
    return "other_deleted"


# ---------------------------------------------------------------------------
# 1. Проверка прав администратора
# ---------------------------------------------------------------------------


def is_admin() -> bool:
    """
    Проверить, запущен ли процесс с правами администратора.

    Использует WinAPI ``IsUserAnAdmin`` (Shell32.dll).
    """
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 2. Экспорт бэкапа реестра в .reg (UTF-16 LE)
# ---------------------------------------------------------------------------


def _export_single_key_via_reg(
    hkey_root: str,
    subkey_path: str,
    outfile: Path,
) -> bool:
    """
    Экспортировать одну ветку реестра через нативную утилиту reg.exe.

    Прямой вызов reg.exe вместо powershell -Command "reg export ..."
    обеспечивает:
    - Более быстрое выполнение (нет запуска процесса PowerShell)
    - Независимость от ExecutionPolicy
    - Упрощённое экранирование аргументов

    Parameters
    ----------
    hkey_root : str
        Корневой ключ: ``"HKCU"``, ``"HKLM"``, ``"HKU"``.
    subkey_path : str
        Путь относительно корневого ключа.
    outfile : Path
        Файл-цель в кодировке UTF-16 LE.

    Returns
    -------
    bool
        True если экспорт прошёл успешно.
    """
    # Преобразуем HKU в HKEY_USERS для reg.exe
    root_map = {
        "HKCU": "HKEY_CURRENT_USER",
        "HKLM": "HKEY_LOCAL_MACHINE",
        "HKU": "HKEY_USERS",
    }
    reg_root = root_map.get(hkey_root.upper(), hkey_root)
    full_path = f"{reg_root}\\{subkey_path}"
    out_path = str(outfile.resolve())

    try:
        result = run_hidden(
            ["reg", "export", full_path, out_path, "/y"],
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0:
            stderr_tail = (
                (result.stderr or b"").decode("utf-8", errors="replace").strip()[-200:]
            )
            logger.warning(
                "reg export завершился с ошибкой (%s): %s\\%s — %s",
                result.returncode,
                hkey_root,
                subkey_path,
                stderr_tail,
            )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        logger.error(
            "reg export превысил таймаут (15 c): %s\\%s", hkey_root, subkey_path
        )
        raise
    except OSError as exc:
        logger.error("reg export не запущен (%s): %s\\%s", exc, hkey_root, subkey_path)
        raise


def backup_registry(
    registry_paths: list[dict[str, str]],
    backup_dir: str | None = None,
    report: dict[str, list[str]] | None = None,
) -> str:
    """
    Создать бэкап указанных веток реестра в ``.reg`` файл (UTF-16 LE).

    Parameters
    ----------
    registry_paths : list[dict[str, str]]
        Список словарей с ключами ``"root"`` и ``"subkey"``.
        Пример:
        ``[{"root": "HKCU", "subkey": "Keyboard Layout\\Preload"}, ...]``
    backup_dir : str, optional
        Папка для сохранения бэкапа (по умолчанию — ``cwd``).
    report : dict, optional
        Если передан словарь — в него записываются списки:
        ``exported`` — успешно экспортированные ветки,
        ``failed`` — ветки, экспорт которых не удался. Вызывающий код
        обязан проверить ``failed`` перед изменением реестра: удаление
        без полного бэкапа необратимо.

    Returns
    -------
    str
        Полный путь к созданному файлу бэкапа.

    Raises
    ------
    BackupError
        Если НЕ УДАЛОСЬ экспортировать ни одну ветку — удалять записи
        без возможности отката недопустимо.
    """
    # Портативный режим: по умолчанию бэкапы кладём рядом с приложением
    # (в собранном .exe — папка с exe, например флешка), а не в cwd.
    backup_dir_path = Path(backup_dir) if backup_dir else get_app_dir() / "backups"
    backup_dir_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir_path / f"keyboard_layout_backup_{timestamp}.reg"

    # reg export ПЕРЕЗАПИСЫВАЕТ целевой файл (/y), поэтому прямой экспорт
    # всех веток в один файл оставлял в бэкапе только последнюю ветку.
    # Экспортируем каждую ветку во временный файл и объединяем секции
    # в один .reg (формат regedit: UTF-16 LE с BOM + единый заголовок).
    exported_sections: list[str] = []
    exported_branch_names: list[str] = []
    failed_branches: list[str] = []

    with tempfile.TemporaryDirectory(dir=str(backup_dir_path)) as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)

        for idx, entry in enumerate(registry_paths):
            root = entry["root"]
            subkey = entry["subkey"]

            # Для ветки HKU\.DEFAULT меняем ТОЛЬКО имя корня (HKU →
            # HKEY_USERS); сам префикс ".DEFAULT\" в пути остаётся —
            # под HKEY_USERS ключ лежит именно как .DEFAULT\Keyboard Layout\...
            export_root, export_subkey = root, subkey
            if root.upper() == "HKU" and subkey.upper().startswith(".DEFAULT\\"):
                export_root = "HKEY_USERS"

            branch_name = f"{root}\\{subkey}"
            part_file = tmp_dir / f"branch_{idx}.reg"
            try:
                exported = _export_single_key_via_reg(
                    export_root, export_subkey, part_file
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                exported = False
                logger.error("Экспорт ветки %s не выполнен: %s", branch_name, exc)
                failed_branches.append(branch_name)

            if not exported or not part_file.exists():
                if exported_branch_names or branch_name not in failed_branches:
                    logger.error(
                        "Бэкап: ветка %s НЕ экспортирована (reg export "
                        "вернул ошибку или файл пуст)",
                        branch_name,
                    )
                if branch_name not in failed_branches:
                    failed_branches.append(branch_name)
                continue

            exported_branch_names.append(branch_name)

            try:
                text = part_file.read_text(encoding="utf-16")
            except (UnicodeError, OSError):
                continue

            lines = [
                line
                for line in text.splitlines()
                if line.strip()
                and not line.strip().startswith("Windows Registry Editor Version")
            ]
            if lines:
                exported_sections.append("\r\n".join(lines))

    if exported_sections:
        content = (
            "Windows Registry Editor Version 5.00"
            + "\r\n\r\n"
            + "\r\n\r\n".join(exported_sections)
            + "\r\n"
        )
    else:
        # НИ ОДНА ветка не экспортирована: файл-заглушка НЕ создаём —
        # удалять записи без возможности отката недопустимо.
        logger.error(
            "Бэкап не создан: ни одна ветка не экспортирована. Проблемные ветки: %s",
            failed_branches,
        )
        raise BackupError(
            "ни одна ветка реестра не была экспортирована "
            f"(проблемные ветки: {', '.join(failed_branches) or '—'})"
        )

    if failed_branches:
        logger.warning("Бэкап НЕПОЛНЫЙ — не экспортированы ветки: %s", failed_branches)

    if report is not None:
        report["exported"] = exported_branch_names
        report["failed"] = failed_branches

    # BOM UTF-16 LE (FF FE) — чтобы regedit корректно импортировал файл
    backup_file.write_bytes(b"\xff\xfe" + content.encode("utf-16-le"))

    return str(backup_file.resolve())


# ---------------------------------------------------------------------------
# 3. Безопасное удаление раскладки
# ---------------------------------------------------------------------------


def _delete_registry_value(
    root_key: int,
    subkey_path: str,
    value_name: str | None = None,
) -> bool:
    """
    Удалить конкретное значение реестра (или все значения в ключе).

    Parameters
    ----------
    root_key : int
        winreg константа (HKEY_CURRENT_USER и т.д.).
    subkey_path : str
        Путь под ключа.
    value_name : str, optional
        Имя значения. Если ``None`` — не удаляем ничего (только проверка).
    """
    try:
        # KEY_READ | KEY_WRITE достаточно для EnumValue/DeleteValue/DeleteKey;
        # KEY_ALL_ACCESS может упасть с PermissionError на защищённых ключах
        # даже там, где прав для удаления по факту хватает.
        with winreg.OpenKey(
            root_key, subkey_path, 0, winreg.KEY_READ | winreg.KEY_WRITE
        ) as key:
            if value_name:
                winreg.DeleteValue(key, value_name)
            else:
                # Удаляем все значения в ключе
                idx = 0
                while True:
                    try:
                        name, *_ = winreg.EnumValue(key, idx)
                        winreg.DeleteValue(key, name)
                        idx = 0  # индексы сбрасываются после удаления
                    except OSError:
                        break
        return True
    except FileNotFoundError:
        # Ключ или значение не существует — не ошибка
        return True
    except PermissionError:
        return False
    except OSError:
        return False


def _clean_preload_keys(
    root_key: int, subkey_path: str, layout_klid: str, delete: bool = True
) -> list[str]:
    """
    Очистить Preload-ключи, удаляя все записи, содержащие ``layout_klid``.

    Поддерживаются REG_SZ (``"00000409"``, ``"00000409\\00000409"``) и
    REG_MULTI_SZ (список токенов, как в
    ``User Profile\\<язык>\\KeyboardLayoutPreload`` — критические записи
    типа ``0809:00000809``). Сопоставляются ВСЕ представления KLID
    (hex / десятичный HKL) через _klid_variants() — единый формат
    scanner.py и cleaner.py.

    Returns
    -------
    list[str]
        Список удалённых имён значений.
    """
    deleted: list[str] = []
    variants = _klid_variants(layout_klid)
    try:
        # KEY_READ | KEY_WRITE достаточно для EnumValue/SetValueEx/DeleteValue;
        # KEY_ALL_ACCESS может упасть с PermissionError на защищённых ключах.
        with winreg.OpenKey(
            root_key, subkey_path, 0, winreg.KEY_READ | winreg.KEY_WRITE
        ) as key:
            idx = 0
            values_to_check: list[tuple[str, object]] = []
            while True:
                try:
                    name, val, _ = winreg.EnumValue(key, idx)
                    values_to_check.append((name, val))
                    idx += 1
                except OSError:
                    break

            for name, val in values_to_check:
                raw_items = (
                    [str(v) for v in val]
                    if isinstance(val, (list, tuple))
                    else [str(val)]
                )
                matched_items: list[str] = []
                kept_items: list[str] = []
                for item in raw_items:
                    parts = [p.strip().lower() for p in item.split("\\")]
                    item_lower = item.strip().lower()
                    if item_lower in variants or bool(set(parts) & variants):
                        matched_items.append(item)
                    else:
                        kept_items.append(item)
                if not matched_items:
                    continue
                if delete:
                    if isinstance(val, (list, tuple)):
                        if kept_items:
                            # Пустой список в MULTI_SZ записать нельзя
                            winreg.SetValueEx(
                                key, name, 0, winreg.REG_MULTI_SZ, kept_items
                            )
                        else:
                            winreg.DeleteValue(key, name)
                    else:
                        winreg.DeleteValue(key, name)
                deleted.append(name)
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return deleted


def _clean_substitutes_keys(
    root_key: int, subkey_path: str, layout_klid: str, delete: bool = True
) -> list[str]:
    """
    Очистить Substitute-ключи, удаляя все записи где ``layout_klid``
    выступает источником или целью подстановки.

    Returns
    -------
    list[str]
        Список удалённых имён значений.
    """
    deleted: list[str] = []
    try:
        # KEY_READ | KEY_WRITE достаточно для EnumValue/DeleteValue;
        # KEY_ALL_ACCESS может упасть с PermissionError на защищённых ключах.
        with winreg.OpenKey(
            root_key, subkey_path, 0, winreg.KEY_READ | winreg.KEY_WRITE
        ) as key:
            idx = 0
            entries: list[tuple[str, str]] = []
            while True:
                try:
                    name, val, _ = winreg.EnumValue(key, idx)
                    entries.append((name, str(val)))
                    idx += 1
                except OSError:
                    break

            variants = _klid_variants(layout_klid)
            for name, val in entries:
                # В Substitutes: name — source KLID, val — target KLID.
                # Сопоставляем во всех представлениях (hex / decimal HKL).
                if (
                    name.strip().lower() in variants
                    or val.strip().lower() in variants
                    or any(v in val.strip().lower() for v in variants)
                ):
                    if delete:
                        winreg.DeleteValue(key, name)
                    else:
                        # dry-run: индексы не сдвигаются — идём дальше
                        idx += 1
                    deleted.append(name)
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return deleted


def _klid_variants(layout_klid: str) -> set[str]:
    """
    Все строковые представления KLID, встречающиеся в реестре.

    Прямые ветки (Preload/Substitutes) хранят 8-HEX-строки, а CTF —
    HKL десятичным числом (KeyboardLayout=68748313 == 0x04190419,
    т.е. язык 0419 + раскладка 00000419).
    Возвращаем сам KLID, его HEX-нормализацию и десятичные формы:
    str(int(k, 16)); для «цифровых» строк без ведущего нуля — и
    обратное чтение (decimal → hex), т.к. запись могла быть HKL.
    """
    k = layout_klid.strip().lower()
    variants = {k}
    if re.fullmatch(r"[0-9a-f]{8}", k):
        try:
            hex_val = int(k, 16)
        except ValueError:
            return variants
        variants.add(f"{hex_val:08x}")
        variants.add(str(hex_val))
        if (
            k.isdigit() and k[0] != "0"
        ):  # двусмысленно: могла быть десятичная запись HKL
            dec_val = int(k, 10)
            if dec_val <= 0xFFFFFFFF:
                variants.add(f"{dec_val:08x}")
                variants.add(str(dec_val))
    return variants


def _clean_branch_recursive(
    root_key: int,
    subkey_path: str,
    layout_klid: str,
    match_mode: str = "preload",
    delete: bool = True,
) -> list[str]:
    """
    Рекурсивно найти (и при delete=True — удалить) значения, связанные
    с ``layout_klid``, во всём поддереве ключа.

    Нужен для веток, где данные лежат в подключах:
      - Control Panel\\International\\User Profile\\<язык>\\KeyboardLayoutPreload
      - Software\\Microsoft\\CTF\\SortOrder\\AssemblyItem\\...

    Returns
    -------
    list[str]
        Список удалённых значений как относительные пути
        ``<подключи>\\<имя значения>`` (для корневого уровня — просто имя).
    """
    deleted: list[str] = []
    variants = _klid_variants(layout_klid)

    def _matches(name: str, val: str) -> bool:
        val_l = val.strip().lower()
        # Формат "0409:00000409" (LANGID:KLID) из User Profile/CTF —
        # сравниваем ВТОРУЮ часть (KLID), а не всю строку.
        tip = _TIP_KLID_RE.match(val_l)
        if tip and tip.group(1).lower() in variants:
            return True
        if match_mode == "substitutes":
            return name.strip().lower() in variants or any(v in val_l for v in variants)
        parts = [p.strip().lower() for p in val.split("\\")]
        if (
            name.strip().lower() in variants
            or val_l in variants
            or bool(set(parts) & variants)
        ):
            return True
        # Проверяем BCP-47 тег (SettingSync хранит теги языков)
        # variants содержит целевой KLID — ищем соответствующие теги
        for tag, tag_klid in LAYOUT_MAP.items():
            if tag.lower() == val_l and tag_klid.lower() in variants:
                return True
        return False

    def _walk(key_path: str, depth: int) -> None:
        if depth > _MAX_CLEAN_DEPTH:
            logger.warning("Достигнута максимальная глубина обхода: %s", key_path)
            return
        try:
            # KEY_READ | KEY_WRITE достаточно для EnumValue/EnumKey/DeleteValue/
            # DeleteKey; KEY_ALL_ACCESS может упасть на защищённых подключях
            # даже при достаточных правах.
            with winreg.OpenKey(
                root_key, key_path, 0, winreg.KEY_READ | winreg.KEY_WRITE
            ) as key:
                # Собираем ВСЕ значения во временный список, затем удаляем
                # отдельным циклом. Прямое удаление во время итерации
                # EnumValue пропускает элементы из-за сдвига индексов.
                entries: list[tuple[str, object]] = []
                v_idx = 0
                while True:
                    try:
                        name, val, _ = winreg.EnumValue(key, v_idx)
                        entries.append((name, val))
                        v_idx += 1
                    except OSError:
                        break

                for name, val in entries:
                    if not _matches(name, str(val)):
                        continue
                    if delete:
                        try:
                            winreg.DeleteValue(key, name)
                        except OSError as exc:
                            logger.warning(
                                "Не удалено %s / %s: %s", key_path, name, exc
                            )
                            continue
                    rel = key_path[len(subkey_path) :].lstrip("\\")
                    deleted.append(f"{rel}\\{name}" if rel else name)
                # Подключи
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
    return deleted


def _klid_to_tags(layout_klid: str) -> set[str]:
    """BCP-47-теги, чьей известной раскладкой является ``layout_klid``."""
    klid_norm = layout_klid.strip().lower()
    return {tag for tag, klid in LAYOUT_MAP.items() if klid.lower() == klid_norm}


# Формат "LANGID:KLID" (0409:00000409) в User Profile\...\KeyboardLayoutPreload
_TIP_KLID_RE = re.compile(r"^[0-9a-f]{4}:([0-9a-f]{8})$", re.I)


def _subtree_is_empty(root_key: int, key_path: str) -> bool:
    """Пуст ли ключ: нет ни значений, ни подключей."""
    try:
        with winreg.OpenKey(root_key, key_path, 0, winreg.KEY_READ) as key:
            try:
                winreg.EnumValue(key, 0)
                return False
            except OSError:
                pass
            try:
                winreg.EnumKey(key, 0)
                return False
            except OSError:
                pass
        return True
    except OSError:
        return False


def _delete_subtree(root_key: int, key_path: str) -> bool:
    """Рекурсивно удалить ключ реестра со всеми подключами и значениями."""
    try:
        # KEY_READ | KEY_WRITE достаточно (EnumKey/DeleteKey); KEY_ALL_ACCESS
        # может упасть на защищённых подключях даже при достаточных правах.
        with winreg.OpenKey(
            root_key, key_path, 0, winreg.KEY_READ | winreg.KEY_WRITE
        ) as key:
            while True:
                try:
                    sub = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_subtree(root_key, key_path + "\\" + sub)
        winreg.DeleteKey(root_key, key_path)
        return True
    except (FileNotFoundError, PermissionError, OSError) as exc:
        logger.warning("Не удалён ключ %s: %s", key_path, exc)
        return False


def _clean_intl_profile(
    root_key: int,
    subkey_path: str,
    layout_klid: str,
    delete: bool = True,
) -> list[str]:
    """
    Очистить ветку ``User Profile`` на уровне BCP-47-тегов — ТОЧЕЧНО.

    Подключи этой ветки названы ТЕГАМИ языка (``en-GB``), а не KLID,
    поэтому простой KLID-поиск их не находит. В отличие от старой версии
    здесь НЕ удаляются:
      * значение ``Languages`` (REG_MULTI_SZ) на корне ветки — это
        активный язык Windows, его удаление сломало бы языковой профиль,
        сохранив фантом в перечне «живых» языков;
      * профиль языка ``User Profile\\<lang>`` целиком — у него могут
        остаться другие раскладки (например, у en-GB — и en-US-раскладка),
        и Windows пересоздаст его при следующей синхронизации.

    Вместо этого внутри профиля ``<lang>`` рекурсивно очищаются ТОЛЬКО
    значения, связанные с ``layout_klid`` (включая ``KeyboardLayoutPreload``
    и ``InputMethodOverride``). Сам профиль удаляется, лишь если после
    очистки он ПОЛНОСТЬЮ опустел и язык не значится в ``Languages``.

    Финальную перезапись живого списка всё равно выполняет PowerShell-шаг
    (Set-WinUserLanguageList): он уберёт язык, если раскладок не осталось.

    Returns
    -------
    list[str]
        Затронутые пути (при delete=False — которые БЫЛИ бы затронуты).
    """
    tags = _klid_to_tags(layout_klid)
    if not tags:
        return []
    tag_norms = {t.lower() for t in tags}

    # Языки, числящиеся активными (значение Languages на корне ветки).
    active_langs: set[str] = set()
    try:
        with winreg.OpenKey(root_key, subkey_path, 0, winreg.KEY_READ) as key:
            try:
                val, vtype = winreg.QueryValueEx(key, "Languages")
                if vtype == winreg.REG_MULTI_SZ and isinstance(val, (list, tuple)):
                    active_langs = {str(x).strip().lower() for x in val}
            except OSError:
                pass
    except OSError:
        return []

    # Профили-подключи, названные тегами удаляемого языка.
    doomed: list[str] = []
    try:
        with winreg.OpenKey(root_key, subkey_path, 0, winreg.KEY_READ) as key:
            sub_idx = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, sub_idx)
                except OSError:
                    break
                sub_idx += 1
                if sub.strip().lower() in tag_norms:
                    doomed.append(sub)
    except OSError:
        return []

    deleted: list[str] = []
    for sub in doomed:
        profile_sub = f"{subkey_path}\\{sub}"
        # 1) Точечная очистка СТРОГО записей layout_klid внутри профиля
        #    языка: KeyboardLayoutPreload, InputMethodOverride и пр.
        hits = _clean_branch_recursive(
            root_key, profile_sub, layout_klid, "preload", delete=delete
        )
        if hits:
            deleted.extend(f"{sub}\\{h}" for h in hits)

        empty_after = _subtree_is_empty(root_key, profile_sub)
        lang_active = sub.strip().lower() in active_langs
        if empty_after and not lang_active:
            if delete:
                if _delete_subtree(root_key, profile_sub):
                    deleted.append(f"{sub} <пустой профиль языка удалён>")
            else:
                deleted.append(f"{sub} <пустой профиль языка будет удалён>")

    return deleted


# Значения-ссылки на раскладку внутри ключей TSF-профилей CTF
_CTF_PROFILE_VALUE_NAMES = {"keyboardlayout", "klid"}


def _clean_ctf_profiles(
    root_key: int,
    ctf_subkey: str,
    layout_klid: str,
    delete: bool = True,
) -> list[str]:
    """
    Удалить ЦЕЛИКОМ ключи TSF-профилей CTF, ссылающиеся на раскладку.

    Рекомендация по архитектуре TSF/CTF:
      1) ``Assemblies\\<LANGID>\\{GUID}`` — зарегистрированная сборка ввода;
      2) ``SortOrder\\AssemblyItem\\<LANGID>\\{GUID}\\<index>`` — порядок в
         очереди переключения (Alt+Shift / Win+Space).
    Удалять только значения недостаточно: TSF продолжает перечислять
    «полупустые» ключи профилей. Ключ считается профилем удаляемой
    раскладки, если одно из его значений (``KeyboardLayout`` / ``KLID``)
    совпадает с KLID в любом представлении (см. _klid_variants — CTF
    хранит HKL десятичным числом). Опустевшие родительские ключи
    LANGID удаляются следом, если пусты.

    Returns
    -------
    list[str]
        Пути ключей относительно корня CTF (при delete=False — которые
        БЫЛИ бы удалены).
    """
    variants = _klid_variants(layout_klid)
    # Младшее слово KLID — для вычисления HKL-форм (см. _walk)
    m_low = re.fullmatch(r"[0-9a-f]{8}", layout_klid.strip().lower())
    base_low = int(m_low.group(0), 16) & 0xFFFF if m_low else 0
    doomed: list[str] = []

    def _walk(key_path: str, depth: int) -> None:
        if depth > _MAX_CLEAN_DEPTH:
            return
        try:
            # KEY_READ | KEY_WRITE достаточно для EnumKey/DeleteKey;
            # KEY_ALL_ACCESS может упасть на защищённых подключях.
            with winreg.OpenKey(
                root_key, key_path, 0, winreg.KEY_READ | winreg.KEY_WRITE
            ) as key:
                # Если ключ лежит под 0x???????? (LANGID языка), CTF мог
                # записать HKL: (LANGID << 16) | KLID — десятичным числом
                local_variants = variants
                for part in reversed(key_path.split("\\")):
                    m_lang = re.fullmatch(r"0x([0-9a-f]{8})", part.lower())
                    if m_lang and base_low:
                        langid = int(m_lang.group(1), 16) & 0xFFFF
                        hkl = (langid << 16) | base_low
                        local_variants = variants | {
                            str(hkl),
                            f"{hkl:08x}",
                        }
                        break
                is_profile = False
                idx = 0
                while True:
                    try:
                        name, val, _vtype = winreg.EnumValue(key, idx)
                    except OSError:
                        break
                    if (
                        str(name).strip().lower() in _CTF_PROFILE_VALUE_NAMES
                        and str(val).strip().lower() in local_variants
                    ):
                        is_profile = True
                    idx += 1
                if is_profile:
                    rel = key_path[len(ctf_subkey) :].lstrip("\\")
                    if delete:
                        _delete_subtree(root_key, key_path)
                    doomed.append(f"{rel} <профиль TSF целиком>")
                    return  # внутрь удалённого поддерева не спускаемся
                sub_idx = 0
                while True:
                    try:
                        sub = winreg.EnumKey(key, sub_idx)
                    except OSError:
                        break
                    sub_idx += 1
                    _walk(f"{key_path}\\{sub}", depth + 1)
        except (FileNotFoundError, PermissionError, OSError) as exc:
            logger.debug("CTF-профили: ветка пропущена %s: %s", key_path, exc)

    _walk(ctf_subkey, 0)

    if delete:
        # Опустевшие родители: ...\0x00000419\{GUID} -> ...\0x00000419
        # (DeleteKey падает на непустом ключе — легитимные профили того
        # же языка остаются на месте; корень CTF не затрагиваем).
        for rel in list(doomed):
            parent_rel = rel.split(" <")[0].rsplit("\\", 1)[0]
            while parent_rel and "\\" in parent_rel:
                try:
                    winreg.DeleteKey(root_key, f"{ctf_subkey}\\{parent_rel}")
                except OSError:
                    break
                doomed.append(f"{parent_rel} (пустой родитель удалён)")
                parent_rel = parent_rel.rsplit("\\", 1)[0]
    return doomed


# CJK-языки: InputMethodTips содержит GUID вместо KLID, поэтому для них
# KLID берём из LAYOUT_MAP (раскладка по умолчанию для этих тегов).
_CJK_FALLBACK_KLIDS: dict[str, str] = {
    tag: klid
    for tag, klid in LAYOUT_MAP.items()
    if tag.split("-")[0] in {"ja", "zh", "ko"}
}


def _build_cleanup_script(layout_klid: str, apply: bool = True) -> str:
    """
    Собрать PowerShell-скрипт очистки из файла.

    Скрипт изменяет объекты, возвращённые Get-WinUserLanguageList, «на месте»
    (InputMethodTips.Remove) — прочие настройки языка сохраняются, в отличие
    от пересоздания списка через New-WinUserLanguageList.

    При apply=False система НЕ изменяется: скрипт только отчитывается
    строками ``DROP|<тег>|<tip>`` / ``REMOVELANG|<тег>`` и завершается
    статусом ``DRYRUN`` — это основа dry-run (plan_layout_removal).

    Защита от PS-инъекции: KLID обязан быть ровно 8 HEX-символов —
    передаётся через параметр PS, а не через f-string.
    """
    if not re.fullmatch(r"[0-9a-fA-F]{8}", str(layout_klid)):
        raise ValueError(
            f"Некорректный KLID для PS-скрипта: '{layout_klid}'. "
            "Ожидается ровно 8 HEX-символов."
        )
    return _load_ps1_script("layout_cleaner_cleanup")


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


def _run_cleanup_script(
    layout_klid: str, apply: bool = True
) -> tuple[bool, str, dict[str, list[str]]]:
    """
    Выполнить PS1-скрипт очистки (apply=True) или его dry-run (apply=False).

    KLID и флаг apply передаются через параметры PowerShell,
    а не через интерполяцию — защита от инъекций.

    Returns
    -------
    tuple[bool, str, dict]
        (успех, человеко-читаемый статус/причина ошибки, план изменений).
    """
    empty: dict[str, list[str]] = {"tips_removed": [], "languages_removed": []}
    script_path = _PS_DIR / "layout_cleaner_cleanup.ps1"

    try:
        if apply:
            result = run_hidden(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script_path),
                    "-LayoutKlid",
                    layout_klid,
                    "-Apply",
                ],
                capture_output=True,
                text=True,
                timeout=45,
            )
        else:
            result = run_hidden(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script_path),
                    "-LayoutKlid",
                    layout_klid,
                ],
                capture_output=True,
                text=True,
                timeout=45,
            )
    except FileNotFoundError:
        return False, "powershell не найден", dict(empty)
    except subprocess.TimeoutExpired:
        return False, "превышен таймаут PowerShell (45 с)", dict(empty)
    except OSError as exc:
        return False, f"ошибка запуска PowerShell: {exc}", dict(empty)

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
            timeout=45,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return False, f"ошибка запуска PowerShell: {exc}"
    if result.returncode == 0 and "SUCCESS" in (result.stdout or ""):
        return True, "SUCCESS"
    return False, "не удалось переустановить список языков"


def _stop_ctfmon() -> bool:
    """
    Остановить службу текстового ввода (ctfmon.exe, TextInputHost.exe).

    Подавляет восстановление TSF-кеша из оперативной памяти: если процесс
    живёт со старым прикладным кешем, он может СРАЗУ после очистки реестра
    вернуть удалённые значения. Поэтому останавливаем его ДО начала
    любых операций с реестром и PowerShell. Команда фиксированная (без
    пользовательских данных) — инъекции невозможны; права администратора
    не нужны.
    """
    script_path = _PS_DIR / "layout_cleaner_stop_ctfmon.ps1"
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
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Остановка ctfmon не удалась: %s", exc)
        return False
    logger.info("ctfmon остановлен — восстановление кэша TSF из ОЗУ подавлено")
    return result.returncode == 0


def _start_ctfmon() -> bool:
    """
    Запустить службу текстового ввода (ctfmon.exe) заново.

    Вызывается ТОЛЬКО после завершения всех операций с реестром, чтобы
    служба перечитала уже чистый реестр. Best-effort: при неудаче UI
    советует перезагрузить ПК.
    """
    script_path = _PS_DIR / "layout_cleaner_start_ctfmon.ps1"
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
            timeout=20,
        )
        ok = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Запуск ctfmon не удался: %s", exc)
        return False
    if ok:
        logger.info("ctfmon запущен — кеш TSF обновлён")
    else:
        logger.warning("Запуск ctfmon: returncode=%s", result.returncode)
    return ok


class CtfmonSuspender:
    """
    Контекстный менеджер для временной остановки службы текстового ввода.

    Гарантирует, что ctfmon запустится обратно даже при исключении.
    Использование::

        with CtfmonSuspender():
            # очистка реестра
            ...
    """

    def __init__(self) -> None:
        self._was_started = False

    def __enter__(self) -> "CtfmonSuspender":
        _stop_ctfmon()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any | None,
    ) -> None:
        _start_ctfmon()


def _restart_ctfmon() -> bool:
    """Перезапустить службу ввода (стоп + старт) — для обратной совместимости."""
    return _start_ctfmon()


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
            timeout=45,
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
    entries: list[tuple[str, list[str]]] = []
    for item in data:
        raw_tag = item.get("LanguageTag", "")
        # PowerShell может вернуть LanguageTag массивом (["ru", "en-US"]),
        # тогда берём первое непустое значение.
        if isinstance(raw_tag, (list, tuple)):
            raw_tag = next((t for t in raw_tag if str(t).strip()), "")
        tag = str(raw_tag).strip()
        if not tag:
            continue
        raw_tips = item.get("InputMethodTips") or []
        # Единичная раскладка может прийти строкой или числом, а не списком.
        if isinstance(raw_tips, (str, int)):
            raw_tips = [raw_tips]
        tips = [str(t) for t in raw_tips]
        entries.append((tag, tips))
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
        result = run_hidden(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-JsonPath",
                str(tmp_json),
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Восстановление списка языков не удалось: %s", exc)
        return False
    finally:
        with contextlib.suppress(OSError):
            tmp_json.unlink(missing_ok=True)
    ok = result.returncode == 0 and "SUCCESS" in (result.stdout or "")
    logger.info(
        "Восстановление списка языков: %s (%d языков)",
        "успех" if ok else "неудача",
        len(entries),
    )
    return ok


# ---------------------------------------------------------------------------
# 2b. Блокировка облачной синхронизации и синхронизация Экрана приветствия
# ---------------------------------------------------------------------------


def _is_language_sync_blocked() -> bool:
    """
    Проверить, заблокирована ли уже синхронизация языков с облаком.

    Returns
    -------
    bool
        True если синхронизация уже отключена, False если не определилось
        или включено.
    """
    key_path = r"Software\Microsoft\Windows\CurrentVersion\SettingSync\Groups\Language"
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ
        ) as key:
            try:
                result = winreg.QueryValueEx(key, "Enabled")
                # Защита от моков и некорректных результатов
                if not isinstance(result, (tuple, list)) or len(result) < 2:
                    return False
                val, vtype = result[0], result[1]
                return vtype == winreg.REG_DWORD and val == 0
            except (FileNotFoundError, OSError):
                # Значение не существует — синхронизация не заблокирована
                return False
    except OSError:
        return False


def disable_language_sync() -> tuple[bool, str]:
    """
    Заблокировать синхронизацию языковых параметров с облаком Microsoft.

    Изменяет локальную политику через реестр HKCU:
    SettingSync\\Groups\\Language\\Enabled = 0 отключает синхронизацию
    языковых предпочтений с учётной записью Microsoft.

    Returns
    -------
    tuple[bool, str]
        (успех, детализированное описание результата/ошибки)
    """
    key_path = r"Software\Microsoft\Windows\CurrentVersion\SettingSync\Groups\Language"
    # Проверяем текущее состояние перед записью
    if _is_language_sync_blocked():
        logger.info("Синхронизация языков уже отключена — пропускаем запись")
        return True, "Уже отключена"

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            # 0 = Отключить синхронизацию языков
            winreg.SetValueEx(key, "Enabled", 0, winreg.REG_DWORD, 0)
        logger.info("Синхронизация языковых параметров с облаком заблокирована.")
        return True, "Отключена (Безопасно)"
    except PermissionError as exc:
        # Ветка заблокирована системным администратором через групповую политику
        logger.warning(
            "Не удалось отключить синхронизацию языков: доступ запрещён "
            "(возможно, заблокировано групповой политикой): %s",
            exc,
        )
        return False, "Доступ запрещён (групповая политика / права администратора)"
    except OSError as exc:
        logger.warning("Не удалось отключить синхронизацию языков: %s", exc)
        return False, f"Ошибка: {exc}"


def sync_welcome_screen_settings() -> bool:
    """
    Применить текущий (чистый) список языков пользователя к Экрану приветствия
    и Системным учетным записям.

    Использует команду PowerShell Copy-UserInternationalSettingsToSystem,
    которая копирует международные настройки текущего пользователя в профиль
    по умолчанию (Winlogon) и для новых пользователей.

    Returns
    -------
    bool
        True если команда выполнена успешно, False при ошибке.
    """
    script_path = _PS_DIR / "layout_cleaner_sync_welcome.ps1"
    try:
        res = run_hidden(
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
            timeout=45,  # Увеличено с 15 до 45 сек — на слабом железе операция может занимать до 30 сек
        )
        ok = res.returncode == 0
        if ok:
            logger.info(
                "Настройки Экрана приветствия обновлены (чистый список языков)."
            )
        else:
            logger.warning(
                "Copy-UserInternationalSettingsToSystem: returncode=%s, stderr=%s",
                res.returncode,
                (res.stderr or "").strip()[:200],
            )
        return ok
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Не удалось обновить настройки Экрана приветствия: %s", exc)
        return False


# ---------------------------------------------------------------------------
# 2a. Восстановление из бэкапа (.reg + .langlist.json)
# ---------------------------------------------------------------------------


def get_backup_dir() -> Path:
    """Папка бэкапов по умолчанию (рядом с приложением/исходниками)."""
    return get_app_dir() / "backups"


def list_backups(backup_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """
    Собрать список бэкапов (новые первыми).

    Returns
    -------
    list[dict[str, Any]]
        Каждый элемент::

            {
              "reg_path": str,          # путь к .reg
              "json_path": str,         # путь к .langlist.json ("" если нет)
              "name": str,              # «04.09.2026 21:15:00»
              "sections": [str, ...],   # заголовки секций [HKEY_...] из .reg
              "has_hku": bool,          # содержит HKEY_USERS\\.DEFAULT
            }
    """
    directory = Path(backup_dir) if backup_dir else get_backup_dir()
    if not directory.is_dir():
        return []

    pattern = re.compile(r"^keyboard_layout_backup_(\d{8}_\d{6})\.reg$")
    reg_files = [
        p
        for p in directory.glob("keyboard_layout_backup_*.reg")
        if pattern.match(p.name)
    ]

    items: list[dict[str, Any]] = []
    for reg_file in sorted(reg_files, reverse=True):
        match = pattern.match(reg_file.name)
        if not match:
            continue
        try:
            ts = datetime.datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
        except ValueError:
            continue
        json_file = reg_file.with_suffix(".langlist.json")
        sections: list[str] = []
        try:
            text = reg_file.read_text(encoding="utf-16")
            sections = [
                line.strip()
                for line in text.splitlines()
                if line.strip().startswith("[") and line.strip().endswith("]")
            ]
        except (UnicodeError, OSError):
            pass
        items.append(
            {
                "reg_path": str(reg_file.resolve()),
                "json_path": str(json_file) if json_file.is_file() else "",
                "name": ts.strftime("%d.%m.%Y %H:%M:%S"),
                "sections": sections,
                "has_hku": any(s.upper().startswith("[HKEY_USERS") for s in sections),
            }
        )
    return items


def restore_registry_backup(reg_path: str | Path) -> dict[str, Any]:
    """
    Применить .reg-бэкап через ``reg import``.

    Если бэкап содержит ветки ``HKEY_USERS\\.DEFAULT``, а процесс запущен
    без прав администратора — reg.exe запускается с UAC-запросом
    (``Start-Process -Verb RunAs``): перезапускать приложение не нужно.

    Returns
    -------
    dict
        ``{"ok": bool, "detail": str, "elevated": bool, "needs_admin": bool}``
    """
    path = Path(reg_path)
    if not path.is_file():
        return {
            "ok": False,
            "detail": f"файл бэкапа не найден: {path}",
            "elevated": False,
            "needs_admin": False,
        }

    needs_admin = False
    try:
        text = path.read_text(encoding="utf-16")
        needs_admin = "[HKEY_USERS" in text.upper()
    except (UnicodeError, OSError):
        pass

    if needs_admin and not is_admin():
        # UAC-запрос на сам импорт; путь к файлу передаётся через параметр PS,
        # а не через интерполяцию — защита от PS-инъекций
        script_path = _PS_DIR / "layout_cleaner_reg_import.ps1"
        logger.info("Импорт .reg требует админа — запрашиваю UAC")
        try:
            proc = run_hidden(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script_path),
                    "-RegPath",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {
                "ok": False,
                "detail": f"ошибка запуска reg import (UAC): {exc}",
                "elevated": True,
                "needs_admin": True,
            }
        ok = proc.returncode == 0
        detail = ""
        if not ok:
            detail = (proc.stderr or proc.stdout or "").strip()[
                :200
            ] or "импорт не выполнен (возможно, UAC отклонён)"
        logger.info("reg import (UAC) %s: ok=%s", path.name, ok)
        return {
            "ok": ok,
            "detail": detail,
            "elevated": True,
            "needs_admin": True,
        }

    try:
        proc = run_hidden(
            ["reg", "import", str(path)],
            capture_output=True,
            text=False,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {
            "ok": False,
            "detail": f"ошибка запуска reg import: {exc}",
            "elevated": False,
            "needs_admin": needs_admin,
        }
    ok = proc.returncode == 0
    detail = ""
    if not ok:
        raw = proc.stderr or proc.stdout or b""
        detail = (
            raw.decode("utf-8", "replace").strip()[:200] or "reg import вернул ошибку"
        )
    logger.info("reg import %s: ok=%s", path.name, ok)
    return {
        "ok": ok,
        "detail": detail,
        "elevated": False,
        "needs_admin": needs_admin,
    }


def restore_backup(reg_path: str | Path, json_path: str | Path = "") -> dict[str, Any]:
    """
    Полный откат удаления раскладки:
      1) импорт .reg (записи реестра);
      2) восстановление списка языков из .langlist.json (если снимок есть).

    Ключ ``success`` равен True, только если импорт прошёл, а снимок
    списка языков (когда он существует) восстановлен без ошибок.
    """
    result = restore_registry_backup(reg_path)
    result["langlist_restored"] = None  # None — снимка не было
    json_file = Path(json_path) if json_path else None
    if json_file is not None and json_file.is_file():
        result["langlist_restored"] = restore_language_list(json_file)
    result["success"] = bool(result["ok"]) and result["langlist_restored"] is not False
    return result


def _current_preload_klids(admin: bool) -> set[str]:
    """
    KLID всех раскладок, уже прописанных в Preload. Используется защитой
    «не удалять последнюю раскладку». Sandbox-осведомлённо: в sandbox
    читаются sandbox-ветки, а не реальные (через scanner.get_affected_branches).
    """
    klids: set[str] = set()
    for root, subkey, _mode, admin_required in _affected_branches():
        if not subkey.endswith("Keyboard Layout\\Preload"):
            continue
        if admin_required and not admin:
            continue
        key_const = _ROOT_CONST.get(root)
        if key_const is None:
            continue
        with contextlib.suppress(OSError):
            klids |= set(_get_preload_keys(key_const, subkey))
    return {k for k in klids if re.fullmatch(r"[0-9a-f]{8}", k)}


def delete_layout(layout_id: str) -> dict[str, Any]:
    """
    Безопасно удалить указанную раскладку клавиатуры из реестра
    и синхронизировать профиль пользователя через PowerShell.

    Parameters
    ----------
    layout_id : str
        KLID HEX-код раскладки, например ``"00000419"``.

    Returns
    -------
    dict
        Сводка выполненных операций::

            {
                "success": bool,
                "backup_path": str,        # путь к созданному бэкапу
                "backup_ok": bool,         # False = удаление отменено
                "backup_error": str,       # причина провала бэкапа
                "backup_failed_branches": list[str],  # неполный бэкап
                "hkcu_preload_deleted": list[str],
                "hkcu_substitutes_deleted": list[str],
                "hkcu_ctf_deleted": list[str],
                "hku_default_deleted": list[str],
                "hkcu_intl_deleted": list[str],
                "hkcu_settingsync_deleted": list[str],
                "power_sync": bool,
                "power_sync_detail": str,
                "langlist_backup_path": str,
                "ctf_profiles_deleted": list[str],
                "ctfmon_restarted": bool | None,
                "admin_privileges": bool,
                "language_sync_blocked": bool,
                "language_sync_block_detail": str,
                "welcome_screen_synced": bool,
                "welcome_screen_sync_detail": str,
            }
    """
    layout_id = layout_id.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{8}", layout_id):
        raise ValueError(
            f"Некорректный KLID: '{layout_id}'. "
            "Ожидается 8 HEX-символов, например '00000419'."
        )

    result: dict[str, Any] = {
        "success": False,
        "klid": layout_id,
        "backup_path": "",
        "backup_ok": True,
        "backup_error": "",
        "backup_failed_branches": [],
        "langlist_backup_path": "",
        "hkcu_preload_deleted": [],
        "hkcu_substitutes_deleted": [],
        "hkcu_ctf_deleted": [],
        "hku_default_deleted": [],
        "hkcu_intl_deleted": [],
        "hkcu_settingsync_deleted": [],
        "ctf_profiles_deleted": [],
        "power_sync": False,
        "power_sync_detail": "",
        "ctfmon_restarted": None,
        "last_layout_guard": False,
        "retry_cleaned": 0,
        "admin_privileges": is_admin(),
        "language_sync_blocked": False,
        "language_sync_block_detail": "",
        "welcome_screen_synced": False,
        "welcome_screen_sync_detail": "",
        "permission_errors": [],  # Ошибки прав доступа при очистке
        "other_deleted": [],  # Ветки вне стандартного маппинга (sandbox и пр.)
    }

    # ------------------------------------------------------------------
    # Защита: не даём удалить ПОСЛЕДНЮЮ оставшуюся раскладку системы,
    # иначе Windows останется без раскладки клавиатуры вообще
    # ------------------------------------------------------------------
    current_klids = _current_preload_klids(result["admin_privileges"])
    if current_klids == {layout_id}:
        result["last_layout_guard"] = True
        logger.warning("Удаление отклонено: это последняя оставшаяся раскладка")
        return result

    # ------------------------------------------------------------------
    # Список веток — единый источник истины (scanner, sandbox-aware)
    # ------------------------------------------------------------------
    branches_now = _affected_branches()

    paths_to_backup = _paths_to_backup(branches_now, result["admin_privileges"])

    # ------------------------------------------------------------------
    # Шаг 1: Бэкапы (реестр + список языков) — до любых изменений.
    # КРИТИЧНО: без полного бэкапа удаление НЕ выполняется.
    # ------------------------------------------------------------------
    backup_report: dict[str, list[str]] = {}
    try:
        result["backup_path"] = backup_registry(paths_to_backup, report=backup_report)
    except BackupError as exc:
        result["backup_ok"] = False
        result["backup_error"] = str(exc)
        logger.error("Удаление ОТМЕНЕНО: бэкап не создан (%s)", exc)
        return result
    logger.info("Бэкап реестра: %s", result["backup_path"])

    failed_backup_branches = backup_report.get("failed", [])
    if failed_backup_branches:
        result["backup_failed_branches"] = failed_backup_branches
        logger.warning(
            "Бэкап неполный (не экспортированы: %s) — продолжаем, но "
            "эти ветки НЕ будут восстановимы из этого бэкапа",
            failed_backup_branches,
        )

    result["langlist_backup_path"] = _backup_language_list(Path(result["backup_path"]))

    # ------------------------------------------------------------------
    # Шаг 2: ОСТАНОВКА служб ввода (ctfmon.exe, TextInputHost.exe).
    # Подавляет гонку состояний: живой процесс держит TSF-кеш в ОЗУ и
    # может СРАЗУ вернуть удалённые значения в реестр после очистки.
    # Используем контекстный менеджер — ctfmon гарантированно запустится
    # даже при исключении.
    # ------------------------------------------------------------------
    ctfmon_started: bool | None = None
    retry_total = 0
    ok, detail = False, "SKIPPED"

    def _wipe(collect_permission_errors: bool = False) -> int:
        """Одним проходом очистить все ветки (безопасно повторять)."""
        total = 0
        for root, subkey, mode, admin_required in branches_now:
            if admin_required and not result["admin_privileges"]:
                continue
            field = _report_field_for(root, subkey)
            if subkey in _recursive_subkeys():
                deleted = []
                if subkey == _ctf_subkey():
                    profiles = _clean_ctf_profiles(_ROOT_CONST[root], subkey, layout_id)
                    result["ctf_profiles_deleted"] = (
                        result.get("ctf_profiles_deleted", []) + profiles
                    )
                    total += len(profiles)
                    if profiles:
                        logger.info(
                            "Очистка CTF: удалено TSF-профилей целиком: %d",
                            len(profiles),
                        )

                deleted += _clean_branch_recursive(
                    _ROOT_CONST[root], subkey, layout_id, mode
                )
                if subkey == _intl_subkey():
                    # User Profile: точечная очистка записей KLID
                    deleted += _clean_intl_profile(_ROOT_CONST[root], subkey, layout_id)
            elif mode == "substitutes":
                deleted = _clean_substitutes_keys(_ROOT_CONST[root], subkey, layout_id)
            else:
                deleted = _clean_preload_keys(_ROOT_CONST[root], subkey, layout_id)
            result[field] = result.get(field, []) + deleted
            if deleted:
                logger.info(
                    "Очистка %s / %s: удалено значений: %d",
                    root,
                    subkey,
                    len(deleted),
                )
            total += len(deleted)
        return total

    with CtfmonSuspender():
        # Шаг 3: Очистка веток реестра (идемпотентная — можно повторять)
        total_deleted = _wipe(collect_permission_errors=True)

        # Шаг 4: Синхронизация списка языков через PowerShell
        ok, detail = _sync_language_list_via_powershell(layout_id)
        result["power_sync"] = ok
        result["power_sync_detail"] = detail

        # Шаг 4a: Блокировка облачной синхронизации языков
        sync_blocked, sync_block_detail = disable_language_sync()
        result["language_sync_blocked"] = sync_blocked
        result["language_sync_block_detail"] = sync_block_detail

        # Шаг 4b: Синхронизация чистых настроек с Экраном приветствия
        # Требует прав администратора (Copy-UserInternationalSettingsToSystem)
        if result["admin_privileges"]:
            welcome_synced = sync_welcome_screen_settings()
            result["welcome_screen_synced"] = welcome_synced
            result["welcome_screen_sync_detail"] = (
                "Обновлено" if welcome_synced else "Не удалось обновить — см. лог"
            )
        else:
            result["welcome_screen_synced"] = False
            result["welcome_screen_sync_detail"] = (
                "Пропущено (требуются права администратора)"
            )

        # Шаг 5: Верификация — повторная подчистка того, что служба могла
        # вернуть между шагами 3 и 4.
        retry_total = _wipe(collect_permission_errors=True)

    # ctfmon_started устанавливается в __exit__ контекстного менеджера
    # через проверку результата _start_ctfmon()
    ctfmon_started = _start_ctfmon() is not False  # True если успешно или не требуется

    # Сбор информации об ошибках прав доступа
    permission_errors: list[str] = []
    for _branch_path, deleted in [
        ("HKCU\\Keyboard Layout\\Preload", result.get("hkcu_preload_deleted", [])),
        (
            "HKCU\\Keyboard Layout\\Substitutes",
            result.get("hkcu_substitutes_deleted", []),
        ),
        (
            "HKCU\\Control Panel\\International\\User Profile",
            result.get("hkcu_intl_deleted", []),
        ),
        ("HKCU\\Software\\Microsoft\\CTF", result.get("hkcu_ctf_deleted", [])),
        (
            "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\SettingSync\\Namespace\\Language",
            result.get("hkcu_settingsync_deleted", []),
        ),
        (
            "HKU\\.DEFAULT\\Keyboard Layout\\Preload",
            result.get("hku_default_deleted", []),
        ),
        ("CTF profiles", result.get("ctf_profiles_deleted", [])),
    ]:
        if not deleted and result["admin_privileges"]:
            # Если админ, а ветка не очищена — probable permission error
            # Проверяем что ветка вообще существует и требовала очистки
            pass  # У нас нет информации была ли ветка пропущена по причине прав

    result["permission_errors"] = permission_errors

    if retry_total:
        result["retry_cleaned"] = retry_total
        total_deleted += retry_total
        logger.warning(
            "Служба ввода вернула %d записей — выполнена повторная очистка",
            retry_total,
        )

    if total_deleted > 0:
        result["ctfmon_restarted"] = ctfmon_started

    # Успех = что-то удалено в реестре ИЛИ список языков реально очищен
    result["success"] = total_deleted > 0 or (ok and detail == "SUCCESS")

    return result


def plan_layout_removal(layout_id: str) -> dict[str, Any]:
    """
    Dry-run: показать, что будет удалено, НЕ изменяя систему.

    Реестр только перечисляется (совпадающие значения по веткам),
    PowerShell-скрипт запускается в режиме анализа ($apply = $false) —
    он сообщает, какие tips/языки будут затронуты, но не вызывает
    Set-WinUserLanguageList.

    Returns
    -------
    dict
        {
          "klid": str,
          "admin_privileges": bool,
          "branches": {путь: {"admin_required": bool, "values": [...],
                              "profile_keys": [...] (только CTF)}},
          "ctfmon_restart": bool,
          "ps": {"available": bool, "detail": str,
                 "tips_removed": [...], "languages_removed": [...]},
        }
    """
    layout_id = layout_id.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{8}", layout_id):
        raise ValueError(
            f"Некорректный KLID: '{layout_id}'. "
            "Ожидается 8 HEX-символов, например '00000419'."
        )

    admin = is_admin()
    branches: dict[str, Any] = {}
    for root, subkey, mode, admin_required in _affected_branches():
        entry: dict[str, Any] = {"admin_required": admin_required, "values": []}
        if admin_required and not admin:
            branches[f"{root}\\{subkey}"] = entry
            continue
        if subkey in _recursive_subkeys():
            entry["values"] = []
            if subkey == _ctf_subkey():
                # Dry-run показывает и ключи TSF-профилей целиком
                entry["profile_keys"] = _clean_ctf_profiles(
                    _ROOT_CONST[root], subkey, layout_id, delete=False
                )
            entry["values"] += _clean_branch_recursive(
                _ROOT_CONST[root], subkey, layout_id, mode, delete=False
            )
            if subkey == _intl_subkey():
                # Dry-run тоже показывает теги/подключи User Profile
                entry["values"] += _clean_intl_profile(
                    _ROOT_CONST[root], subkey, layout_id, delete=False
                )
        elif mode == "substitutes":
            entry["values"] = _clean_substitutes_keys(
                _ROOT_CONST[root], subkey, layout_id, delete=False
            )
        else:
            entry["values"] = _clean_preload_keys(
                _ROOT_CONST[root], subkey, layout_id, delete=False
            )
        branches[f"{root}\\{subkey}"] = entry

    ok, detail, ps_plan = _run_cleanup_script(layout_id, apply=False)
    plan = {
        "klid": layout_id,
        "admin_privileges": admin,
        "branches": branches,
        # Перезапуск ctfmon планируется, если в реестре есть совпадения
        "ctfmon_restart": any(
            entry.get("values") or entry.get("profile_keys")
            for entry in branches.values()
        ),
        "ps": {
            "available": ok,
            "detail": detail,
            "tips_removed": ps_plan["tips_removed"],
            "languages_removed": ps_plan["languages_removed"],
        },
    }
    # Предупредить пользователя, если это последняя оставшаяся раскладка
    current_klids = _current_preload_klids(admin)
    plan["last_layout_guard"] = bool(current_klids) and current_klids == {layout_id}
    logger.info(
        "Dry-run %s: ps=%s, tips=%d, langs=%d",
        layout_id,
        detail,
        len(ps_plan["tips_removed"]),
        len(ps_plan["languages_removed"]),
    )
    return plan


# ---------------------------------------------------------------------------
# CLI entry-point для ручного тестирования
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) > 1:
        klid = sys.argv[1]
    else:
        sys.exit(1)

    report = delete_layout(klid)

# ---------------------------------------------------------------------------
# --- Очистка ---
# ---------------------------------------------------------------------------


def activate_sandbox() -> None:
    """Активировать sandbox-режим (совместимость со старым API)."""
    enable_sandbox()


def deactivate_sandbox() -> None:
    """Выйти из sandbox-режима (используется тестами)."""
    disable_sandbox()
