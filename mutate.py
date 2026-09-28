"""
mutate.py — Сопоставление значений KLID и мутация реестра (FIX-10, шаг 2).

Выделено из ``cleaner.py`` по границе, доказуемой тестами: всё, что
ОПРЕДЕЛЯЕТ, какое значение реестра относится к удаляемой раскладке, и всё,
что его удаляет. Сюда же ушли резолверы веток-мишеней (включая
sandbox-варианты) и снимок/манифест (FIX-6) — манифест снимает значения
ТЕМИ ЖЕ предикатами, что удаляет, и разнесение их означало бы расхождение
«план говорит одно, удаляет другое».

Оркестрация (``delete_layout``, ``plan_layout_removal``), список языков,
SettingSync, PowerShell-адаптер и отчётность остались в ``cleaner.py``:
они вызывают сюда, но ничего не определяют сами. ``cleaner.py``
реэкспортирует перенесённые имена, поэтому публичный API не изменился.
"""

import datetime
import json
import logging
import re
import winreg
from pathlib import Path
from typing import Any

# Корни реестра живут в backup (нижний уровень). Именно `backup._ROOT_CONST`,
# а не `from backup import _ROOT_CONST`: привязка именем копировала бы
# значение, и подмена в тестах или в backup перестала бы влиять на mutate —
# тот самый «молчаливый мок», который FIX-19 и шаг 1 уже ловили дважды.
import backup
import capabilities
import layout_ids
import scanner
from config import _SANDBOX_ROOT, __version__, is_sandbox_enabled
from layout_ids import (
    METADATA_VALUE_NAMES as _METADATA_VALUE_NAMES,
)
from layout_ids import (
    TIP_KLID_RE as _TIP_KLID_RE,
)

logger = logging.getLogger("layout_cleaner")

# Fallback-словарь BCP-47 -> KLID (общий с layout_ids, FIX-8).
LAYOUT_MAP = layout_ids.LAYOUT_MAP

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
def _require_mutation(delete: bool, where: str) -> None:
    """FIX-29: при ``delete=False`` функция только читает — право не нужно.

    Иначе право пришлось бы требовать и в dry-run, который по определению
    безопасен: планирование удаления должно работать всегда, в том числе в
    тестах, которые как раз и проверяют, что план построен.
    """
    if delete:
        capabilities.require(capabilities.Capability.REGISTRY_MUTATE, where)


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



def _value_matches(
    value_name: str,
    value: object,
    variants: set[str],
    match_mode: str = "preload",
    recursive: bool = False,
    variants_hint: str = "",
) -> bool:
    """Совпадает ли значение с раскладкой — ТЕМ ЖЕ предикатом, что при удалении.

    Снимок «до удаления» (FIX-6) обязан считать ровно те значения, которые
    удалит ``_wipe_branches``. Если предикаты расходятся, дрейф плана
    сравнивает несопоставимые числа и выдаёт ложные предупреждения.

    Соответствие предикатов (по веткам):

    * ``recursive=True`` (CTF, User Profile, SettingSync\\Namespace\\Language) —
      :func:`_branch_value_matches` ровно так, как это делает
      ``_walk_branch_recursive``. Учитываются TIP-формат ``0409:00000409``,
      имена-метаданные (``FeaturesToInstall``) и BCP-47-теги.
    * ``match_mode == "substitutes"`` — единый предикат
      :func:`layout_ids.substitutes_value_matches` (имя + токены значения).
    * остальные Preload-ветки — поэлементная логика
      :func:`_clean_preload_keys`: REG_MULTI_SZ сравнивается по элементам,
      внутри элемента разбиение по ``\\``.
    """
    if recursive:
        return _branch_value_matches(
            value_name, str(value), variants, match_mode, variants_hint
        )
    if match_mode == "substitutes":
        # Прямая ветка — hex-формы без десятичного LANGID (FIX-22).
        return layout_ids.substitutes_value_matches(
            value_name, str(value), layout_ids.klid_hex_forms(variants_hint)
        )
    items = (
        [str(v) for v in value]
        if isinstance(value, (list, tuple))
        else [str(value)]
    )
    for item in items:
        item_lower = item.strip().lower()
        if item_lower in variants:
            return True
        parts = [p.strip().lower() for p in item.split("\\")]
        if set(parts) & variants:
            return True
    return False



def _collect_value_info(
    root_key: int,
    subkey_path: str,
    layout_klid: str,
    mode: str = "preload",
    recursive: bool = False,
) -> list[dict[str, str]]:
    """Снимок найденных значений (без удаления) для манифеста.

    ``recursive=True`` — ветка чистится рекурсивно (см. :func:`_value_matches`),
    поэтому и снимок, и обход подключей используют единый предикат
    ``_branch_value_matches``.
    """
    entries: list[dict[str, str]] = []
    variants = _klid_variants(layout_klid)
    try:
        with winreg.OpenKey(root_key, subkey_path, 0, winreg.KEY_READ) as key:
            idx = 0
            while True:
                try:
                    name, val, vtype = winreg.EnumValue(key, idx)
                    idx += 1
                except OSError:
                    break
                if _value_matches(name, val, variants, mode, recursive, layout_klid):
                    type_names = {
                        winreg.REG_SZ: "REG_SZ",
                        winreg.REG_EXPAND_SZ: "REG_EXPAND_SZ",
                        winreg.REG_DWORD: "REG_DWORD",
                        winreg.REG_MULTI_SZ: "REG_MULTI_SZ",
                        winreg.REG_BINARY: "REG_BINARY",
                    }
                    entries.append({
                        "path": subkey_path,
                        "name": name,
                        "type": type_names.get(vtype, f"0x{vtype:x}"),
                        "value": str(val),
                    })
    except OSError:
        pass
    try:
        with winreg.OpenKey(root_key, subkey_path, 0, winreg.KEY_READ) as key:
            sub_idx = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, sub_idx)
                except OSError:
                    break
                sub_idx += 1
                child_path = f"{subkey_path}\\{sub}"
                entries.extend(
                    _collect_value_info_recursive(
                        root_key, subkey_path, child_path, 0,
                        layout_klid, variants, mode, 30, recursive,
                    )
                )
    except OSError:
        pass
    return entries



def _collect_value_info_recursive(
    root_key: int, subkey_path: str, key_path: str, depth: int,
    layout_klid: str, variants: set[str], match_mode: str, max_depth: int = 30,
    recursive: bool = False,
) -> list[dict[str, str]]:
    """Рекурсивный снимок значений (без удаления)."""
    if depth > max_depth:
        return []
    entries: list[dict[str, str]] = []
    try:
        with winreg.OpenKey(root_key, key_path, 0, winreg.KEY_READ) as key:
            idx = 0
            while True:
                try:
                    name, val, vtype = winreg.EnumValue(key, idx)
                    idx += 1
                except OSError:
                    break
                if _value_matches(
                    name, val, variants, match_mode, recursive, layout_klid
                ):
                    type_names = {
                        winreg.REG_SZ: "REG_SZ",
                        winreg.REG_EXPAND_SZ: "REG_EXPAND_SZ",
                        winreg.REG_DWORD: "REG_DWORD",
                        winreg.REG_MULTI_SZ: "REG_MULTI_SZ",
                        winreg.REG_BINARY: "REG_BINARY",
                    }
                    rel = key_path[len(subkey_path):].lstrip("\\")
                    rel_path = f"{subkey_path}\\{rel}" if rel else subkey_path
                    entries.append({
                        "path": rel_path,
                        "name": name,
                        "type": type_names.get(vtype, f"0x{vtype:x}"),
                        "value": str(val),
                    })
            sub_idx = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, sub_idx)
                except OSError:
                    break
                sub_idx += 1
                entries.extend(
                    _collect_value_info_recursive(
                        root_key, subkey_path, key_path + "\\" + sub,
                        depth + 1, layout_klid, variants, match_mode, max_depth,
                        recursive,
                    )
                )
    except OSError:
        pass
    return entries



def _snapshot_values_before_delete(
    branches_now: list[tuple[str, str, str, bool]],
    layout_id: str,
    admin_privileges: bool,
) -> dict[str, Any]:
    """Снимок всех найденных до мутации значений по всем веткам.

    Использует те же предикаты, что и удаление (см. :func:`_value_matches`),
    поэтому количество значений в снимке равно количеству значений, которые
    ``_wipe_branches`` обязан удалить. Это условие корректности дрейф-детекции
    и аудита по манифесту (FIX-6).
    """
    snapshot: dict[str, Any] = {}
    recursive_subkeys = _recursive_subkeys()
    for root, subkey, mode, admin_required in branches_now:
        branch_name = f"{root}\\{subkey}"
        entry: dict[str, Any] = {
            "admin_required": admin_required,
            "skipped_no_privs": admin_required and not admin_privileges,
            "values": [],
        }
        if admin_required and not admin_privileges:
            snapshot[branch_name] = entry
            continue
        is_recursive = subkey in recursive_subkeys
        if is_recursive and subkey == _ctf_subkey():
            entry["profile_keys"] = _clean_ctf_profiles(
                backup._ROOT_CONST[root], subkey, layout_id, delete=False
            )
        # Один вызов _collect_value_info покрывает и корень ветки, и поддерево
        # (внутренний _collect_value_info_recursive): раньше для User Profile
        # значения добавлялись дважды, что завышало «плановое» число и
        # провоцировало ложный дрейф.
        entry["values"] = _collect_value_info(
            backup._ROOT_CONST[root], subkey, layout_id, mode, is_recursive
        )
        snapshot[branch_name] = entry
    return snapshot



def _build_manifest(
    layout_id: str,
    backup_path: str,
    branches_now: list[tuple[str, str, str, bool]],
    admin_privileges: bool,
    report: dict[str, list[str]],
    snapshot: dict[str, Any] | None = None,
) -> str | None:
    """Создать sidecar-манифест рядом с .reg-бэкапом.

    ``snapshot`` — снимок значений ДО мутации. Если не передан, снимок
    снимается здесь же (совместимость с вызовами из тестов), но в
    ``delete_layout`` он передаётся обязательно: иначе манифест снимал бы
    уже очищенное состояние и всегда показывал ноль значений.
    """
    try:
        backup_dir = Path(backup_path).parent
        base_name = Path(backup_path).stem
        manifest_path = backup_dir / f"{base_name}.manifest.json"

        if snapshot is None:
            snapshot = _snapshot_values_before_delete(
                branches_now, layout_id, admin_privileges
            )

        total_values = _count_snapshot_values(snapshot)

        manifest = {
            "manifest_version": "1.0",
            "app_version": __version__,
            "operation": "backup_and_delete",
            "klid": layout_id,
            "backup_file": Path(backup_path).name,
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "branch_summary": {
                "total_branches": len(branches_now),
                "exported": report.get("exported", []),
                "failed": report.get("failed", []),
                "skipped": report.get("skipped", []),
            },
            "snapshot": {
                "total_values": total_values,
                "branches": snapshot,
            },
        }

        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Манифест бэкапа: %s", manifest_path)
        return str(manifest_path.resolve())
    except Exception as exc:  # noqa: BLE001 — аудит best-effort: сбой записи
        # манифеста (диск/права/кодировка) НЕ должен отменять уже созданный
        # .reg-бэкап и удаление; пользователь получит предупреждение в логе.
        logger.warning("Не удалось создать манифест: %s", exc)
        return None



def _count_snapshot_values(snapshot: dict[str, Any]) -> int:
    """Посчитать значения в снимке."""
    total = 0
    for entry in snapshot.values():
        total += len(entry.get("values", []))
        total += len(entry.get("profile_keys", []))
    return total



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
    # FIX-29: право на мутацию реестра. Проверка ЗДЕСЬ, а не у вызывающих:
    # это единственная точка, через которую проходят все DeleteValue.
    if value_name is not None:
        capabilities.require(
            capabilities.Capability.REGISTRY_MUTATE, "_delete_registry_value"
        )
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
    # FIX-29: мутация требует права; при delete=False только чтение.
    _require_mutation(delete, '_clean_preload_keys')
    # Прямая ветка: hex-формы без «голого» десятичного LANGID. Иначе
    # значение "1049" в Preload удалялось бы при чистке 00000419 (FIX-22).
    variants = _klid_hex_forms(layout_klid)
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
    # FIX-29: мутация требует права; при delete=False только чтение.
    _require_mutation(delete, '_clean_substitutes_keys')
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

            variants = _klid_hex_forms(layout_klid)
            for name, val in entries:
                # В Substitutes: name — source KLID, val — target KLID.
                # Единый предикат и hex-формы (FIX-22): подстрочный поиск
                # удалял чужие записи вида d0011049 при удалении 00000419.
                if layout_ids.substitutes_value_matches(name, val, variants):
                    if delete:
                        winreg.DeleteValue(key, name)
                    deleted.append(name)
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return deleted



def _klid_variants(layout_klid: str) -> set[str]:
    """
    Все представления KLID, включая десятичную форму HKL.

    Используется для РЕКУРСИВНЫХ веток (CTF хранит HKL десятичным числом).
    Прямые ветки (Preload/Substitutes) берут :func:`_klid_hex_forms` — без
    «голого» десятичного LANGID (FIX-22). Реализация в layout_ids (FIX-8).
    """
    return layout_ids.klid_variants(layout_klid)



def _klid_hex_forms(layout_klid: str) -> set[str]:
    """
    Представления KLID для прямых веток (Preload/Substitutes) — FIX-22.

    Без десятичного LANGID: «1049» совпало бы подстрокой с фантомной
    записью ``d0011049`` и удалило её вместе с целевой раскладкой.
    """
    return layout_ids.klid_hex_forms(layout_klid)



def _branch_value_matches(
    name: str,
    val: str,
    variants: set[str],
    match_mode: str,
    variants_hint: str = "",
) -> bool:
    """Совпадает ли значение реестра с одним из представлений KLID.

    ``variants_hint`` — исходный KLID; нужен только для ветки
    ``substitutes``, где набор форм берётся строже (FIX-22).
    """
    # Метаданные языкового профиля (FeaturesToInstall="000006ff" и пр.) не
    # являются раскладками — пропускаем их по имени значения (см. scanner).
    name_l = str(name).strip().lower()
    if name_l in _METADATA_VALUE_NAMES:
        return False
    val_l = val.strip().lower()
    # Формат "0409:00000409" (LANGID:KLID) — сравниваем ВТОРУЮ часть
    # (KLID), а не всю строку. Такой вид встречается в ДВУХ местах, и раньше
    # распознавался только первый:
    #   * в ТЕЛЕ значения (KeyboardLayoutPreload = "0409:00000409");
    #   * в ИМЕНИ значения — так хранятся установленные методы ввода в
    #     `Control Panel\International\User Profile\<язык>`: имя
    #     "0409:00000409", тип REG_DWORD, данные 0x1. На живой машине из-за
    #     этого в шаблоне профиля по умолчанию остался `en-US` с привязкой к
    #     US-раскладке, и US возвращалась после перезагрузки (FIX-37g).
    for token in (val_l, name_l):
        tip = _TIP_KLID_RE.match(token)
        if tip and tip.group(1).lower() in variants:
            return True
    if match_mode == "substitutes":
        # Прямая ветка — hex-формы без десятичного LANGID (FIX-22). Здесь
        # Substitutes рекурсивно не обходятся, но предикат обязан совпадать
        # с _clean_substitutes_keys, иначе план и удаление разойдутся.
        return layout_ids.substitutes_value_matches(
            name, val_l, layout_ids.klid_hex_forms(variants_hint)
        )
    # HKL-формы в CTF (FIX-34). Сканер показывает такую запись в строке её
    # KLID, значит и план обязан находить её по этому же KLID — иначе строка
    # врёт: «запись найдена», а удаление её не тронет. Проверка по младшему
    # слову: 0x04190419 и 0x04090419 — одна и та же раскладка, разные типы
    # клавиатуры. Прямые ветки сюда не попадают (в Preload/Substitutes HKL
    # не пишется), а substitutes выше уже вышел.
    if layout_ids.hkl_matches_klid(val_l, variants):
        return True
    parts = [p.strip().lower() for p in val.split("\\")]
    if (
        name_l in variants
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



def _walk_branch_recursive(
    root_key: int,
    subkey_path: str,
    key_path: str,
    depth: int,
    variants: set[str],
    match_mode: str,
    delete: bool,
    deleted: list[str],
    layout_klid: str = "",
) -> None:
    """Рекурсивный обход поддерева: пометить (и удалить) значения KLID.

    ``layout_klid`` передаётся отдельно от ``variants``: для прямой ветки
    Substitutes набор форм строже (FIX-22), и пересчитать его из ``variants``
    невозможно — десятичный LANGID уже смешан с hex-формами.
    """
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
                if not _branch_value_matches(
                    name, str(val), variants, match_mode, layout_klid
                ):
                    continue
                if delete:
                    try:
                        winreg.DeleteValue(key, name)
                    except OSError as exc:
                        logger.warning("Не удалено %s / %s: %s", key_path, name, exc)
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
                _walk_branch_recursive(
                    root_key,
                    subkey_path,
                    key_path + "\\" + sub,
                    depth + 1,
                    variants,
                    match_mode,
                    delete,
                    deleted,
                    layout_klid,
                )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        logger.debug("Ветка пропущена %s: %s", key_path, exc)



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
    # FIX-29: мутация требует права; при delete=False только чтение.
    _require_mutation(delete, '_clean_branch_recursive')
    variants = _klid_variants(layout_klid)
    _walk_branch_recursive(
        root_key,
        subkey_path,
        subkey_path,
        0,
        variants,
        match_mode,
        delete,
        deleted,
        layout_klid,
    )
    return deleted



def _klid_to_tags(layout_klid: str) -> set[str]:
    """BCP-47-теги, чьей известной раскладкой является ``layout_klid``."""
    return layout_ids.klid_to_tags(layout_klid)



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
    # FIX-29: отдельный примитив, минующий _delete_registry_value.
    capabilities.require(
        capabilities.Capability.REGISTRY_MUTATE, "_delete_subtree"
    )
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
    # FIX-29: мутация требует права; при delete=False только чтение.
    _require_mutation(delete, '_clean_intl_profile')
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



def _clean_intl_languages(
    root_key: int,
    subkey_path: str,
    layout_klid: str,
    delete: bool = True,
) -> list[str]:
    """Убрать теги языка из значения ``Languages`` (REG_MULTI_SZ).

    Зачем отдельная от ``_clean_intl_profile``

    ------------------------------------
    ``_clean_intl_profile`` сознательно НЕ трогает ``Languages``: у живого
    пользователя это его активный язык, и значение переписывает шаг
    ``Set-WinUserLanguageList``. В шаблоне профиля по умолчанию такого
    шага нет. Там ``Languages`` — единственный носитель языка: Windows
    пересобирает из него ``HKU\\.DEFAULT`` при **каждой** загрузке.

    Проверено дампом живого шаблона: пока в нём лежит ``en-US``, US
    возвращается в ``HKU\\.DEFAULT\\Keyboard Layout\\Preload`` и в
    ``...\\CTF\\SortOrder\\Language`` после каждой перезагрузки, сколько бы
    веток ``Preload`` ни вычистили. Чистка подключей языка этого не
    покрывает: подключа ``en-US`` в шаблоне нет вовсе, есть только
    значение ``Languages``.

    Функция узкая и вызывается **только** для шаблона — см.
    ``ddu.wipe_preload``. Для живого пользователя она бы вырезала
    активный язык, поэтому здесь её звать нельзя.

    Returns
    -------
    list[str]
        Описание изменений (при delete=False — которые БЫЛИ бы внесены).
    """
    tags = _klid_to_tags(layout_klid)
    if not tags:
        return []
    tag_norms = {t.strip().lower() for t in tags}
    # FIX-29: мутация требует права; при delete=False только чтение.
    _require_mutation(delete, "_clean_intl_languages")
    access = winreg.KEY_READ | winreg.KEY_WRITE if delete else winreg.KEY_READ
    try:
        with winreg.OpenKey(root_key, subkey_path, 0, access) as key:
            try:
                value, vtype = winreg.QueryValueEx(key, "Languages")
            except OSError:
                return []
            if vtype != winreg.REG_MULTI_SZ or not isinstance(
                value, (list, tuple)
            ):
                return []
            kept = [
                str(x) for x in value if str(x).strip().lower() not in tag_norms
            ]
            if len(kept) == len(value):
                return []
            if delete:
                if kept:
                    winreg.SetValueEx(
                        key, "Languages", 0, winreg.REG_MULTI_SZ, kept
                    )
                else:
                    # Пустой MULTI_SZ — мусор: значение убирается целиком.
                    winreg.DeleteValue(key, "Languages")
            report = f"Languages {'|'.join(map(str, value))} -> " + (
                '|'.join(kept) if kept else "<удалено>"
            )
            return [report]
    except (FileNotFoundError, PermissionError, OSError) as exc:
        logger.debug("Languages: ветка пропущена %s: %s", subkey_path, exc)
        return []



def _clean_intl_language_if_unused(
    root_key: int,
    intl_subkey: str,
    preload_subkey: str,
    layout_klid: str,
    delete: bool = True,
) -> list[str]:
    """FIX-37h: убрать ЯЗЫК из профиля, если раскладок не осталось.

    Живой прогон 28.09.2026 (пользователь с ``ru`` + ``en-US``): раскладка
    уходила из ``Preload`` и CTF, но значение ``Languages`` оставалось
    ``ru|en-US``, а ключ ``User Profile\\en-US`` — на месте. Windows считает
    язык установленным и возвращает раскладку в переключатель при каждой
    загрузке; в журнале это видно как ``switcher_stale = 00000409``.

    Почему не хватило PowerShell-шага (``Set-WinUserLanguageList``): к его
    моменту реестр был уже вычищен, ``Get-WinUserLanguageList`` не видел
    подсказки ``0409:00000409`` и отвечал ``NOCHANGE`` — язык не убирался.
    Поэтому шаг синхронизации перенесён ДО чистки веток, но опираться
    только на него нельзя: он молчит и при других сбоях, а молчание
    выглядит как успех.

    Условие удаления языка — в ``Preload`` не осталось НИ ОДНОЙ другой
    раскладки того же языка. Сравнение по BCP-47-тегам, а не по префиксу
    KLID: ``00000419`` и ``00000409`` не «одна LANGID» (у KLID старшие
    четыре символа — нули), и по подписи каталога ``00000411`` — это
    Japanese, а не вторая американская раскладка (проверено на живой
    машине: ``Layout Text`` в ``Keyboard Layouts\\00000411``).

    Неопознанные раскладки (тех, которых нет в ``LAYOUT_MAP``) удалению
    языка НЕ мешают: у них может быть любая страна, а молчаливый отказ
    означал бы, что языковой профиль остаётся и раскладка вернётся — ровно
    тот инцидент, который чинится. Такие KLID попадают в журнал, чтобы
    решение можно было проверить, а их раскладки пользователя при этом
    остаются на месте.

    Если ``Preload`` недоступен, язык не трогаем: отсутствие доказательства
    — не повод удалять.

    Returns
    -------
    list[str]
        Описание изменений (при delete=False — которые БЫЛИ бы внесены).
    """
    tags = _klid_to_tags(layout_klid)
    if not tags:
        return []
    # FIX-29: мутация требует права; при delete=False только чтение.
    _require_mutation(delete, "_clean_intl_language_if_unused")

    target = layout_klid.strip().lower()
    tag_norms = {t.strip().lower() for t in tags}
    siblings: list[str] = []
    unknown: list[str] = []
    try:
        with winreg.OpenKey(root_key, preload_subkey, 0, winreg.KEY_READ) as pkey:
            idx = 0
            while True:
                try:
                    _name, value, _vtype = winreg.EnumValue(pkey, idx)
                except OSError:
                    break
                idx += 1
                token = str(value).strip().lower()
                if not re.fullmatch(r"[0-9a-f]{8}", token) or token == target:
                    continue
                token_tags = {t.lower() for t in _klid_to_tags(token)}
                if not token_tags:
                    unknown.append(token)
                elif token_tags & tag_norms:
                    siblings.append(token)
    except OSError as exc:
        logger.debug(
            "Preload %s недоступен (%s) — язык %s оставлен",
            preload_subkey,
            exc,
            sorted(tag_norms),
        )
        return []

    if unknown:
        logger.info(
            "В Preload есть раскладки вне таблицы (%s) — язык по ним не судим",
            ", ".join(sorted(unknown)),
        )
    if siblings:
        logger.info(
            "Язык %s остаётся: в Preload есть его другие раскладки: %s",
            sorted(tag_norms),
            ", ".join(sorted(siblings)),
        )
        return []

    deleted = _clean_intl_languages(root_key, intl_subkey, layout_klid, delete)

    # Порядок FIX-37g повторён: сначала Languages, потом профили — иначе
    # опустевший профиль языка сохранился бы как «активный».
    for tag in sorted(tags):
        profile = f"{intl_subkey}\\{tag}"
        try:
            with winreg.OpenKey(root_key, profile, 0, winreg.KEY_READ):
                pass
        except OSError:
            continue
        if delete:
            if _delete_subtree(root_key, profile):
                deleted.append(f"{tag} <профиль языка удалён>")
        else:
            deleted.append(f"{tag} <профиль языка будет удалён>")
    return deleted


# Значения-ссылки на раскладку внутри ключей TSF-профилей CTF
_CTF_PROFILE_VALUE_NAMES = {"keyboardlayout", "klid"}



def _walk_ctf_profiles(
    root_key: int,
    ctf_subkey: str,
    key_path: str,
    depth: int,
    variants: set[str],
    base_low: int,
    delete: bool,
    doomed: list[str],
) -> None:
    """Рекурсивный обход CTF: пометить (и удалить) ключи профилей раскладки."""
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
                    # HKL = (LANGID << 16) | младшее слово KLID (layout_ids)
                    local_variants = variants | layout_ids.hkl_string_forms(
                        langid, base_low
                    )
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
                _walk_ctf_profiles(
                    root_key,
                    ctf_subkey,
                    f"{key_path}\\{sub}",
                    depth + 1,
                    variants,
                    base_low,
                    delete,
                    doomed,
                )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        logger.debug("CTF-профили: ветка пропущена %s: %s", key_path, exc)



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
    # FIX-29: мутация требует права; при delete=False только чтение.
    _require_mutation(delete, "_clean_ctf_profiles")
    # Младшее слово KLID — для вычисления HKL-форм (layout_ids, FIX-8)
    base_low = layout_ids.klid_low_word(layout_klid)
    doomed: list[str] = []

    _walk_ctf_profiles(
        root_key, ctf_subkey, ctf_subkey, 0, variants, base_low, delete, doomed
    )

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

