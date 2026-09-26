"""
cleaner.py — Модуль бэкапа и безопасного удаления раскладок клавиатуры (Этап 2).

Реализует:
  - is_admin()       — проверка прав администратора через API WinAPI.
  - backup_registry() — экспорт веток реестра в .reg (UTF-16 LE) через subprocess.
  - delete_layout()   — удаление записей из реестра + синхронизация профиля через PowerShell.
"""

import contextlib
import datetime
import logging
import platform
import re
import subprocess
import sys
import uuid
import winreg
from pathlib import Path
from typing import Any

import applog
import capabilities
import layout_ids
from applog import get_app_dir
from applog import resource_path as resource_path
from backup import (
    _ROOT_CONST as _ROOT_CONST,
)

# FIX-10 (шаг 1): регистрационный бэкап/восстановление вынесены в
# backup.py. Имена переэкспортируются, чтобы cleaner.backup_registry и
# остальные публичные функции продолжили работать без правок вызова.
from backup import (
    REG_BACKUP_MAX_BYTES as REG_BACKUP_MAX_BYTES,
)
from backup import (
    REG_BACKUP_MIN_BYTES as REG_BACKUP_MIN_BYTES,
)
from backup import (
    BackupError as BackupError,
)
from backup import (
    _branch_exists as _branch_exists,
)
from backup import (
    _check_reg_backup_file as _check_reg_backup_file,
)
from backup import (
    _export_single_key_via_reg as _export_single_key_via_reg,
)
from backup import (
    _release_reserved_backup as _release_reserved_backup,
)
from backup import (
    _reserve_backup_file as _reserve_backup_file,
)
from backup import (
    backup_registry as backup_registry,
)
from backup import (
    get_backup_dir as get_backup_dir,
)
from backup import (
    is_admin as is_admin,
)
from backup import (
    list_backups as list_backups,
)
from backup import (
    restore_registry_backup as restore_registry_backup,
)
from config import (
    _SANDBOX_ROOT,
    TIMEOUTS,
    __version__,
    disable_sandbox,
    enable_sandbox,
    is_sandbox_enabled,
)

# FIX-10 (шаг 3): список языков и PS-адаптер вынесены в langlist.py;
# имена переэкспортируются для обратной совместимости.
from langlist import (
    _CJK_FALLBACK_KLIDS as _CJK_FALLBACK_KLIDS,
)
from langlist import (
    _backup_language_list as _backup_language_list,
)
from langlist import (
    _parse_langlist_entry as _parse_langlist_entry,
)
from langlist import (
    _parse_ps_plan as _parse_ps_plan,
)
from langlist import (
    _run_cleanup_script as _run_cleanup_script,
)
from langlist import (
    _sync_language_list_via_powershell as _sync_language_list_via_powershell,
)
from langlist import (
    _write_cjk_map_file as _write_cjk_map_file,
)
from langlist import (
    restore_language_list as restore_language_list,
)

# FIX-10 (шаг 2): предикаты сопоставления и мутация вынесены в
# mutate.py; имена переэкспортируются для обратной совместимости.
from mutate import (
    _CTF_PROFILE_VALUE_NAMES as _CTF_PROFILE_VALUE_NAMES,
)
from mutate import (
    _CTF_SUBKEY as _CTF_SUBKEY,
)
from mutate import (
    _INTL_SUBKEY as _INTL_SUBKEY,
)
from mutate import (
    _MAX_CLEAN_DEPTH as _MAX_CLEAN_DEPTH,
)
from mutate import (
    _RECURSIVE_SUBKEYS as _RECURSIVE_SUBKEYS,
)
from mutate import (
    _affected_branches as _affected_branches,
)
from mutate import (
    _branch_value_matches as _branch_value_matches,
)
from mutate import (
    _build_manifest as _build_manifest,
)
from mutate import (
    _build_sandbox_recursive_subkeys as _build_sandbox_recursive_subkeys,
)
from mutate import (
    _clean_branch_recursive as _clean_branch_recursive,
)
from mutate import (
    _clean_ctf_profiles as _clean_ctf_profiles,
)
from mutate import (
    _clean_intl_profile as _clean_intl_profile,
)
from mutate import (
    _clean_preload_keys as _clean_preload_keys,
)
from mutate import (
    _clean_substitutes_keys as _clean_substitutes_keys,
)
from mutate import (
    _collect_value_info as _collect_value_info,
)
from mutate import (
    _collect_value_info_recursive as _collect_value_info_recursive,
)
from mutate import (
    _count_snapshot_values as _count_snapshot_values,
)
from mutate import (
    _ctf_subkey as _ctf_subkey,
)
from mutate import (
    _delete_registry_value as _delete_registry_value,
)
from mutate import (
    _delete_subtree as _delete_subtree,
)
from mutate import (
    _intl_subkey as _intl_subkey,
)
from mutate import (
    _klid_hex_forms as _klid_hex_forms,
)
from mutate import (
    _klid_to_tags as _klid_to_tags,
)
from mutate import (
    _klid_variants as _klid_variants,
)
from mutate import (
    _recursive_subkeys as _recursive_subkeys,
)
from mutate import (
    _sandbox_active as _sandbox_active,
)
from mutate import (
    _snapshot_values_before_delete as _snapshot_values_before_delete,
)
from mutate import (
    _subtree_is_empty as _subtree_is_empty,
)
from mutate import (
    _value_matches as _value_matches,
)
from mutate import (
    _walk_branch_recursive as _walk_branch_recursive,
)
from mutate import (
    _walk_ctf_profiles as _walk_ctf_profiles,
)
from scanner import _get_preload_keys
from scanner import (
    check_switcher_consistency as check_switcher_consistency,
)
from scanner import (
    is_switcher_consistent as is_switcher_consistent,
)

# FIX-10 (шаг 4): SettingSync и welcome-синхронизацияция вынесены в
# settings.py; имена переэкспортируются для обратной совместимости.
from settings import (
    _SETTINGSYNC_GROUPS_SUBKEY as _SETTINGSYNC_GROUPS_SUBKEY,
)
from settings import (
    _is_language_sync_blocked as _is_language_sync_blocked,
)
from settings import (
    _settingsync_groups_key_path as _settingsync_groups_key_path,
)
from settings import (
    _settingsync_groups_paths as _settingsync_groups_paths,
)
from settings import (
    disable_language_sync as disable_language_sync,
)
from settings import (
    enable_language_sync as enable_language_sync,
)
from settings import (
    is_language_sync_blocked as is_language_sync_blocked,
)
from winproc import run_hidden

# ---------------------------------------------------------------------------
# FIX-8: реэкспорт единого резолвера идентификаторов (layout_ids).
# Имена с подчёркиванием сохранены как тонкие обёртки: на них ссылаются
# scanner.py, тесты и внешний код. Локальные копии таблиц/регулярных выражений
# удалены — расхождение копий означало «сканер нашёл, cleaner не удалил».
# ---------------------------------------------------------------------------

# Fallback-словарь BCP-47 -> KLID (используется в _branch_value_matches)
LAYOUT_MAP = layout_ids.LAYOUT_MAP

# ---------------------------------------------------------------------------
# Загрузка PowerShell-скриптов из файлов (scripts/*.ps1)
# FIX-20: путь строит applog.resource_path — один источник истины для
# ресурсов рантайма. Раньше тут был Path(__file__).parent / "scripts",
# что работало только пока файлы лежат рядом с модулями.
_PS_DIR = resource_path("scripts")

logger = logging.getLogger("layout_cleaner")
applog.setup_null_handler(logger)


# ---------------------------------------------------------------------------
# Режим песочницы — синхронизируется с scanner.SANDBOX_MODE через config
# ---------------------------------------------------------------------------
# SANDBOX_MODE импортируется из config для обратной совместимости




def _system_info() -> dict[str, str]:
    """
    Контекст системы для отчёта об операции (диагностика удалённых сбоев).

    Returns
    -------
    dict[str, str]
        ``{"windows_version", "python_version", "app_version"}``
    """
    return {
        "windows_version": platform.version(),
        "python_version": sys.version.split()[0],
        "app_version": __version__,
    }










# Санити-границы размера .reg-бэкапа объявлены рядом с _check_reg_backup_file
# (REG_BACKUP_MIN_BYTES / REG_BACKUP_MAX_BYTES) — единый источник значений.














def _paths_to_backup(
    branches: list[tuple[str, str, str, bool]], admin_privileges: bool
) -> list[dict[str, str]]:
    """Пути для бэкапа: все ветки, доступные при текущих правах."""
    return [
        {"root": root, "subkey": subkey}
        for root, subkey, _mode, admin_required in branches
        if not admin_required or admin_privileges
    ]


# ---------------------------------------------------------------------------
# FIX-6: Sidecar-манифест бэкапа + дрейф плана
# ---------------------------------------------------------------------------






















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




# ---------------------------------------------------------------------------
# 2. Экспорт бэкапа реестра в .reg (UTF-16 LE)
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# 3. Безопасное удаление раскладки
# ---------------------------------------------------------------------------




















# _TIP_KLID_RE (формат "LANGID:KLID", напр. 0409:00000409) импортирован
# из layout_ids (FIX-8) — раньше регулярное выражение дублировалось здесь
# и в scanner.py, и расхождение означало «сканер нашёл, cleaner нет».
























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
    # FIX-29: Stop-Process -Force по ЖИВОМУ процессу пользователя. Это не
    # операция с реестром, поэтому песочница её не покрывала никогда.
    capabilities.require(capabilities.Capability.INPUT_SERVICES, "_stop_ctfmon")
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
            timeout=TIMEOUTS["ctfmon_control"],
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
    # FIX-29: см. _stop_ctfmon — запуск тоже трогает живой процесс.
    capabilities.require(capabilities.Capability.INPUT_SERVICES, "_start_ctfmon")
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
            timeout=TIMEOUTS["ctfmon_control"],
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
        # Результат запуска ctfmon — для отчёта delete_layout (ctfmon_started).
        self.started: bool | None = None
        # FIX-28: True — в песочнице службы ввода не трогались (см. __enter__).
        self.skipped: bool = False

    def __enter__(self) -> "CtfmonSuspender":
        # FIX-28 (P1, песочница не изолировала ЖИВЫЕ процессы).
        #
        # Остановка ctfmon — единственное действие приложения, которое песочница
        # НЕ умела изолировать: реестр уводится под _SANDBOX_ROOT, WinRT-API
        # заблокированы флагом -Sandbox (FIX-25), а вот
        # `Stop-Process -Name ctfmon -Force` бьёт по процессам ТЕКУЩЕГО
        # пользователя. sandbox-тест (TestPhantomDeletionSimulation,
        # TestDryRunAndBackupLive) вызывал delete_layout, то есть каждый
        # полный прогон pytest УБИВАЛ живой ctfmon.exe и TextInputHost.exe.
        #
        # Это и есть наблюдавшийся пользователем симптом: во время прогонов
        # тестов пропадала русская раскладка. ctfmon владеет кешем TSF в ОЗУ;
        # его убийство заставляет Windows пересобирать кеш из реестра при
        # следующем запуске, и если Preload / User Profile / CTF\Assemblies в
        # этот момент расходятся, пересборка молча теряет раскладку — при том
        # что в Параметрах языка она остаётся «установленной».
        #
        # В песочнице останавливать нечего: мутации идут в изолированные
        # ветки, живой кеш TSF к ним отношения не имеет.
        if is_sandbox_enabled():
            self.skipped = True
            logger.info(
                "Песочница: службы ввода (ctfmon/TextInputHost) не останавливаются"
            )
            return self
        if _stop_ctfmon():
            # FIX-13: маркер «службы ввода остановлены». Если процесс будет
            # убит до __exit__ (крестик при daemon-потоке, падение, kill),
            # следующий старт приложения поднимет ctfmon по этому маркеру.
            try:
                _input_suspended_marker_path().write_text(
                    datetime.datetime.now().isoformat(timespec="seconds"),
                    encoding="utf-8",
                )
            except OSError:
                logger.warning("Не удалось создать маркер остановки служб ввода")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any | None,
    ) -> None:
        # FIX-28: в песочнице ничего не останавливалось — запускать нечего.
        # Возврат до _start_ctfmon() обязателен: иначе «восстановление» после
        # песочницы само перезапускало бы службу ввода у живого пользователя.
        if self.skipped:
            return
        self.started = _start_ctfmon()
        # Штатное завершение: маркер больше не нужен (FIX-13).
        with contextlib.suppress(OSError):
            _input_suspended_marker_path().unlink(missing_ok=True)


def _input_suspended_marker_path() -> Path:
    """Путь к файлу-маркеру «службы ввода остановлены» (FIX-13)."""
    return get_app_dir() / "input_services_suspended.marker"


def ensure_input_services_running() -> bool:
    """Аварийный запуск служб ввода, если предыдущий прогон их остановил.

    Страховка FIX-13: если процесс был убит внутри ``with CtfmonSuspender()``
    (закрытие окна при daemon-потоке, падение, kill из диспетчера задач),
    ctfmon/TextInputHost остаются остановленными. При следующем старте
    приложения маркер обнаруживается, службы запускаются, маркер снимается.

    Returns
    -------
    bool
        True — маркер найден и запуск выполнен (или попытан); False —
        маркера нет, ничего не делалось.
    """
    marker = _input_suspended_marker_path()
    if not marker.is_file():
        return False
    with contextlib.suppress(OSError):
        marker.unlink(missing_ok=True)
    logger.warning(
        "Обнаружен маркер остановки служб ввода (прошлый прогон не завершил "
        "операцию) — запускаем ctfmon"
    )
    return _start_ctfmon()








# ---------------------------------------------------------------------------
# 2b. Блокировка облачной синхронизации и синхронизация Экрана приветствия
# ---------------------------------------------------------------------------












# ---------------------------------------------------------------------------
# 2a. Восстановление из бэкапа (.reg + .langlist.json)
# ---------------------------------------------------------------------------








def restore_backup(reg_path: str | Path, json_path: str | Path = "") -> dict[str, Any]:
    """
    Полный откат удаления раскладки:
      1) импорт .reg (записи реестра);
      2) восстановление списка языков из .langlist.json (если снимок есть).

    Ключ ``success`` равен True, только если импорт прошёл, а снимок
    списка языков (когда он существует) восстановлен без ошибок.

    FIX-27: операция обёрнута в :class:`CtfmonSuspender`. ``reg import``
    перезаписывает ``CTF\\*`` и ``Preload``, а живой ``ctfmon.exe`` держит
    собственный кеш TSF в памяти и способен вернуть его поверх только что
    импортированных значений — то есть откат рапортовал бы об успехе, а
    переключатель так и остался бы без раскладки. Удаление (``delete_layout``)
    было защищено с самого начала, восстановление — нет; это была асимметрия,
    из-за которой «восстановил, а раскладка не появилась» выглядело
    невозможным. Симптом безопасно лечится перезапуском службы ввода,
    которого раньше здесь просто не было.
    """
    with CtfmonSuspender() as suspender:
        result = restore_registry_backup(reg_path)
        result["langlist_restored"] = None  # None — снимка не было
        json_file = Path(json_path) if json_path else None
        if json_file is not None and json_file.is_file():
            result["langlist_restored"] = restore_language_list(json_file)
    result["success"] = bool(result["ok"]) and result["langlist_restored"] is not False
    # Результат запуска ctfmon — для отчёта (ctfmon_restarted).
    result["ctfmon_restarted"] = suspender.started
    # FIX-27: сверка переключателя после отката. Восстановление возвращает
    # записи в реестр, но не в WinRT-список языков и не в кеш CTF, поэтому
    # «восстановлено» само по себе не означает «раскладка снова в переключателе».
    # Показываем расхождение, а не затираем его (см. scanner.check_switcher_consistency).
    try:
        switcher_report = check_switcher_consistency()
    except Exception as exc:  # noqa: BLE001 — диагностика не должна ронять откат
        logger.warning("Сверка переключателя после восстановления не удалась: %s", exc)
        switcher_report = {}
    result["switcher_check"] = switcher_report
    if not is_switcher_consistent(switcher_report):
        logger.warning(
            "После восстановления список языков и переключатель расходятся: %s",
            switcher_report,
        )
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


def _wipe_branches(
    branches_now: list[tuple[str, str, str, bool]],
    result: dict[str, Any],
    layout_id: str,
) -> int:
    """Одним проходом очистить все ветки реестра (безопасно повторять)."""
    total = 0
    for root, subkey, mode, admin_required in branches_now:
        if admin_required and not result["admin_privileges"]:
            continue
        field = _report_field_for(root, subkey)
        if subkey in _recursive_subkeys():
            deleted: list[str] = []
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


# ---------------------------------------------------------------------------
# FIX-6: Дрейф плана — обнаружение расхождений после очистки
# ---------------------------------------------------------------------------


def _count_deleted_from_report(result: dict[str, Any]) -> int:
    """Посчитать общее число удалённых значений из отчёта delete_layout."""
    fields = [
        "hkcu_preload_deleted",
        "hkcu_substitutes_deleted",
        "hkcu_intl_deleted",
        "hkcu_ctf_deleted",
        "hku_default_deleted",
        "hkcu_settingsync_deleted",
        "ctf_profiles_deleted",
        "other_deleted",
    ]
    total = 0
    for field in fields:
        items = result.get(field, [])
        if isinstance(items, list):
            total += len(items)
    return total


def _scan_remaining_preload(
    root_key: int, subkey: str, layout_klid: str, variants: set[str]
) -> list[str]:
    found: list[str] = []
    try:
        with winreg.OpenKey(root_key, subkey, 0, winreg.KEY_READ) as key:
            idx = 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(key, idx)
                    idx += 1
                except OSError:
                    break
                raw_items = (
                    [str(v) for v in val]
                    if isinstance(val, (list, tuple))
                    else [str(val)]
                )
                for item in raw_items:
                    parts = [p.strip().lower() for p in item.split("\\")]
                    item_lower = item.strip().lower()
                    if (item_lower in variants or bool(set(parts) & variants)):
                        found.append(f"{subkey}\\{name}")
                        break
    except OSError:
        pass
    return found


def _scan_remaining_substitutes(
    root_key: int, subkey: str, layout_klid: str, variants: set[str]
) -> list[str]:
    found: list[str] = []
    # Прямая ветка — hex-формы без десятичного LANGID (FIX-22): остаток
    # должен считаться ТОЧНО тем же предикатом, что и удаление.
    variants = layout_ids.klid_hex_forms(layout_klid)
    try:
        with winreg.OpenKey(root_key, subkey, 0, winreg.KEY_READ) as key:
            idx = 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(key, idx)
                    idx += 1
                except OSError:
                    break
                val_str = str(val)
                if layout_ids.substitutes_value_matches(name, val_str, variants):
                    found.append(f"{subkey}\\{name}")
    except OSError:
        pass
    return found


def _scan_remaining_recursive_inner(
    root_key: int, subkey_path: str, key_path: str, depth: int,
    variants: set[str], match_mode: str, found: list[str], max_depth: int = 30,
    layout_klid: str = "",
) -> None:
    if depth > max_depth:
        return
    try:
        with winreg.OpenKey(root_key, key_path, 0, winreg.KEY_READ) as key:
            idx = 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(key, idx)
                    idx += 1
                except OSError:
                    break
                if _value_matches(
                    name, val, variants, match_mode, True, layout_klid
                ):
                    rel = key_path[len(subkey_path):].lstrip("\\")
                    rel_path = f"{key_path}\\{name}" if rel else f"{subkey_path}\\{name}"
                    found.append(rel_path)
            sub_idx = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, sub_idx)
                except OSError:
                    break
                sub_idx += 1
                _scan_remaining_recursive_inner(
                    root_key, subkey_path,
                    key_path + "\\" + sub,
                    depth + 1, variants, match_mode, found, max_depth, layout_klid,
                )
    except OSError:
        pass


def _scan_remaining_recursive(
    root_key: int, subkey: str, layout_klid: str, variants: set[str],
    match_mode: str = "preload",
) -> list[str]:
    found: list[str] = []
    _scan_remaining_recursive_inner(
        root_key, subkey, subkey, 0, variants, match_mode, found, 30, layout_klid
    )
    return found


def _scan_remaining_intl(
    root_key: int, subkey: str, layout_klid: str, variants: set[str],
) -> list[str]:
    tags = _klid_to_tags(layout_klid)
    if not tags:
        return []
    tag_norms = {t.lower() for t in tags}
    found: list[str] = []
    try:
        with winreg.OpenKey(root_key, subkey, 0, winreg.KEY_READ) as key:
            sub_idx = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, sub_idx)
                except OSError:
                    break
                sub_idx += 1
                if sub.strip().lower() in tag_norms:
                    profile_sub = f"{subkey}\\{sub}"
                    found.extend(
                        _scan_remaining_recursive(
                            root_key, profile_sub,
                            layout_klid, variants, "preload"
                        )
                    )
    except OSError:
        pass
    return found


def _find_remaining_values(
    branches_now: list[tuple[str, str, str, bool]],
    layout_id: str,
    admin_privileges: bool,
) -> list[str]:
    """Найти значения, которые ОСТАЛИСЬ после удаления.

    Результат дедуплицируется: User Profile попадает в выборку дважды
    (рекурсивный обход ветки и точечный обход языковых профилей), и без
    дедупликации остаток завышался бы, а ``actual_removed`` — занижался.
    """
    layout_id_norm = layout_id.strip().lower()
    variants = _klid_variants(layout_id_norm)
    remaining: list[str] = []
    for root, subkey, mode, admin_required in branches_now:
        if admin_required and not admin_privileges:
            continue
        if subkey in _recursive_subkeys():
            remaining.extend(
                _scan_remaining_recursive(
                    _ROOT_CONST.get(root, 0), subkey, layout_id_norm, variants, mode
                )
            )
            if subkey == _ctf_subkey():
                # Профильные ключи CTF удаляются целиком (_clean_ctf_profiles) и
                # попадают в снимок как profile_keys — значит и в остатке они
                # должны проверяться тем же вызовом с delete=False, иначе
                # неудавшееся удаление профиля не отразилось бы в дрейфе.
                remaining.extend(
                    _clean_ctf_profiles(
                        _ROOT_CONST.get(root, 0), subkey, layout_id_norm, delete=False
                    )
                )
            if subkey == _intl_subkey():
                remaining.extend(
                    _scan_remaining_intl(
                        _ROOT_CONST.get(root, 0), subkey, layout_id_norm, variants
                    )
                )
        elif mode == "substitutes":
            remaining.extend(
                _scan_remaining_substitutes(
                    _ROOT_CONST.get(root, 0), subkey, layout_id_norm, variants
                )
            )
        else:
            remaining.extend(
                _scan_remaining_preload(
                    _ROOT_CONST.get(root, 0), subkey, layout_id_norm, variants
                )
            )
    # Дедупликация с сохранением порядка (см. докстринг): одна и та же запись
    # может быть найдена и рекурсивным обходом ветки, и профильным сканером.
    return list(dict.fromkeys(remaining))


def _detect_plan_drift(
    branches_now: list[tuple[str, str, str, bool]],
    layout_id: str,
    admin_privileges: bool,
    snapshot: dict[str, Any],
) -> list[str]:
    """Сравнить снимок ДО удаления с фактом ПОСЛЕ.

    Возвращает [planned, actual, remaining_branch\\name, ...] или [] при совпадении.
    """
    planned_total = _count_snapshot_values(snapshot)
    if planned_total == 0:
        return []

    remaining = _find_remaining_values(
        branches_now, layout_id, admin_privileges
    )
    remaining_count = len(remaining)
    actual_removed = planned_total - remaining_count

    if actual_removed != planned_total:
        drift_details: list[str] = [
            str(planned_total),
            str(actual_removed),
        ]
        drift_details.extend(remaining)
        logger.warning(
            "Дрейф плана: планировалось %d, фактически удалено %d, "
            "остаток %d. Детали: %s",
            planned_total, actual_removed, remaining_count, remaining,
        )
        return drift_details

    return []


def delete_layout(layout_id: str, block_cloud_sync: bool = False) -> dict[str, Any]:
    """
    Безопасно удалить указанную раскладку клавиатуры из реестра
    и синхронизировать профиль пользователя через PowerShell.

    Parameters
    ----------
    layout_id : str
        KLID HEX-код раскладки, например ``"00000419"``.
    block_cloud_sync : bool
        FIX-2 (opt-in): блокировать ли облачную синхронизацию языков
        (``SettingSync\\Groups\\Language\\Enabled = 0``). По умолчанию False —
        шаг выполнялся безусловно и менял реестр даже у пользователя,
        который блокировку не запрашивал (Находка A). Состояние ветки в
        любом случае попадает в .reg-бэкап, поэтому restore его вернёт.

    Returns
    -------
    dict
        Сводка выполненных операций::

            {
                "success": bool,
                "klid": str,               # нормализованный KLID (lowercase)
                "backup_path": str,        # путь к созданному бэкапу
                "backup_ok": bool,         # False = удаление отменено
                "backup_error": str,       # причина отмены по бэкапу
                "backup_failed_branches": list[str],   # реальные сбои экспорта
                "backup_skipped_branches": list[str],  # ветки, которых нет в реестре
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
                "last_layout_guard": bool,  # сработала защита последней раскладки
                "retry_cleaned": int,       # сколько повторных чисток CTF сделано
                # FIX-26: расхождение списка языков и панели переключения.
                # {"tips_without_preload": [...], "preload_without_tip": [...],
                #  "ctf_missing": [...]}; пустой dict — всё согласовано.
                "switcher_check": dict,
                "admin_privileges": bool,
                "language_sync_blocked": bool,
                "language_sync_block_detail": str,
                "operation_id": str,      # короткий id операции (для логов)
                "started_at": str,        # ISO-время старта
                "completed_at": str,      # ISO-время завершения ("" — прервано)
                "system_info": dict,      # windows/python/app версии
                "error": str,             # текст исключения ("" — без ошибок)
            }

    Защита «последняя раскладка» проверяется ПОСЛЕ остановки службы ввода
    (внутри :class:`CtfmonSuspender`), а не до бэкапа: ранняя проверка
    оставляла окно гонки с живым TSF-кешем.

    Автоматический откат частично удалённых значений СОЗНАТЕЛЬНО не
    выполняется: перед удалением всегда создаётся .reg-бэкап, а сама
    очистка идемпотентна (``_wipe_branches`` вызывается дважды), поэтому
    откат при исключении не даёт выигрыша, но создаёт риск записать в
    реестр устаревший снимок. Вместо отката исключение на середине
    операции перехватывается: отчёт возвращается с ``success=False``,
    текстом в ``error`` и путями бэкапов — восстановление выполняет явная
    операция :func:`restore_backup` по ``backup_path``.
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
        "operation_id": uuid.uuid4().hex[:8],
        "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "completed_at": "",
        "system_info": _system_info(),
        "error": "",
        "backup_path": "",
        "backup_ok": True,
        "backup_error": "",
        "backup_failed_branches": [],
        "backup_skipped_branches": [],
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
        "other_deleted": [],  # Ветки вне стандартного маппинга (sandbox и пр.)
        "plan_drift": [],  # FIX-6: [planned, actual, remaining...] или []
        "manifest_path": "",  # FIX-6: путь к sidecar-манифесту
    }

    # ------------------------------------------------------------------
    # Ранний отказ без бэкапа и остановки ввода. Проверяем повторно перед
    # записью: другую раскладку могли удалить, пока создавался бэкап.
    # ------------------------------------------------------------------
    if _current_preload_klids(result["admin_privileges"]) == {layout_id}:
        result["last_layout_guard"] = True
        result["completed_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        return result

    # ------------------------------------------------------------------
    # Список веток — единый источник истины (scanner, sandbox-aware)
    # ------------------------------------------------------------------
    branches_now = _affected_branches()

    # FIX-2: + backup-only источник SettingSync\Groups\Language — restore
    # (reg import) вернёт исходное состояние блокировки облака.
    paths_to_backup = (
        _paths_to_backup(branches_now, result["admin_privileges"])
        + _settingsync_groups_paths()
    )

    # ------------------------------------------------------------------
    # Шаг 1: Бэкапы (реестр + список языков) — до любых изменений.
    # КРИТИЧНО (fail-closed): либо бэкап покрывает все СУЩЕСТВУЮЩИЕ ветки,
    # либо удаление не выполняется. Отсутствие ветки ошибкой не считается
    # (в ней нечего бэкапить и нечего удалять), сбой экспорта существующей
    # ветки — считается и отменяет операцию.
    # ------------------------------------------------------------------
    backup_report: dict[str, list[str]] = {}
    try:
        result["backup_path"] = backup_registry(paths_to_backup, report=backup_report)
    except BackupError as exc:
        result["backup_ok"] = False
        result["backup_error"] = str(exc)
        result["backup_failed_branches"] = backup_report.get("failed", [])
        logger.error("Удаление ОТМЕНЕНО: бэкап не создан (%s)", exc)
        return result
    logger.info("Бэкап реестра: %s", result["backup_path"])

    failed_backup_branches = backup_report.get("failed", [])
    if failed_backup_branches:
        # Защита от fail-open: ``backup_registry`` уже обязан был бросить
        # BackupError, но решение дублируется здесь, чтобы будущее изменение
        # контракта не превратилось в удаление без возможности отката.
        result["backup_ok"] = False
        result["backup_failed_branches"] = failed_backup_branches
        result["backup_error"] = (
            "бэкап неполный, не экспортированы ветки: "
            + ", ".join(failed_backup_branches)
        )
        logger.error(
            "Удаление ОТМЕНЕНО: неполный бэкап (не экспортированы: %s)",
            failed_backup_branches,
        )
        return result

    result["backup_skipped_branches"] = backup_report.get("skipped", [])
    if result["backup_skipped_branches"]:
        # Не предупреждение, а информация: ветки отсутствуют, бэкапить и
        # удалять в них нечего. Раньше этот случай выдавался за неполный
        # бэкап, из-за чего настоящий отказ экспорта было не отличить от шума.
        logger.info(
            "Бэкап: ветки отсутствуют в реестре (бэкапить нечего): %s",
            result["backup_skipped_branches"],
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
    total_deleted = 0
    ok, detail = False, "SKIPPED"
    snapshot: dict[str, Any] | None = None  # FIX-6

    with CtfmonSuspender() as suspender:
        # Повторная проверка сокращает окно гонки, но не устраняет его:
        # остановка ctfmon не блокирует записи из системных Параметров.
        current_klids = _current_preload_klids(result["admin_privileges"])
        if current_klids == {layout_id}:
            result["last_layout_guard"] = True
            result["completed_at"] = datetime.datetime.now().isoformat(
                timespec="seconds"
            )
            logger.warning(
                "Удаление отклонено (проверка после остановки службы ввода): "
                "это последняя оставшаяся раскладка"
            )
            # return внутри with: __exit__ запустит ctfmon обратно.
            return result

        # Шаг 3a..5 выполняются под try/except: если что-то падает на середине,
        # пользователь ДОЛЖЕН получить отчёт с путями к бэкапу, а не только
        # трассировку в логе. Откат значений здесь сознательно не делается
        # (см. докстринг delete_layout): очистка идемпотентна, а .reg-бэкап
        # уже создан — восстановление выполняет restore_backup.
        try:
            # FIX-6: снимок ДО удаления — план для дрейф-детекции и манифеста
            snapshot = _snapshot_values_before_delete(
                branches_now, layout_id, result["admin_privileges"]
            )

            # FIX-6: манифест пишется сразу после снимка и ДО мутации, чтобы
            # (а) содержать состояние «как было», а не «как стало», и
            # (б) пережить падение на середине операции — он и есть аудит.
            manifest_path = _build_manifest(
                layout_id,
                result["backup_path"],
                branches_now,
                result["admin_privileges"],
                backup_report,
                snapshot,
            )
            if manifest_path:
                result["manifest_path"] = manifest_path

            # Шаг 3a: Очистка веток реестра (идемпотентная — можно повторять)
            total_deleted = _wipe_branches(branches_now, result, layout_id)

            # Шаг 4: Синхронизация списка языков через PowerShell
            ok, detail = _sync_language_list_via_powershell(layout_id)
            result["power_sync"] = ok
            result["power_sync_detail"] = detail

            # Шаг 4a: Блокировка облачной синхронизации языков — только
            # opt-in (FIX-2, Находка A): раньше disable выполнялся безусловно
            # и менял Enabled даже у пользователя, снявшего галочку.
            if block_cloud_sync:
                sync_blocked, sync_block_detail = disable_language_sync()
            else:
                sync_blocked = False
                sync_block_detail = "Пропущено: блокировка не запрошена"
            result["language_sync_blocked"] = sync_blocked
            result["language_sync_block_detail"] = sync_block_detail

            # FIX-4: шаг синхронизации Экрана приветствия удалён из проекта
            # полностью (решение владельца 26.09.2026, Problems.MD §6 п.1) —
            # вместе с отдельной командой CLI. Причина та же:
            # Copy-UserInternationalSettingsToSystem перезаписывает HKU\.DEFAULT
            # и профиль новых пользователей (весь набор international
            # settings), эти ветки не бэкапятся и откат невозможен.

            # Шаг 5: Верификация — повторная подчистка того, что служба могла
            # вернуть между шагами 3 и 4.
            retry_total = _wipe_branches(branches_now, result, layout_id)
        except Exception as exc:
            # Осознанно широкий except: сбой любой подсистемы (winreg, reg.exe,
            # PowerShell, кодеки) на середине операции не должен превращаться в
            # «немой» краш фонового потока. Отчёт с бэкапом и текстом ошибки
            # информативнее трассировки: пользователь сможет откатиться.
            result["success"] = False
            result["error"] = f"{type(exc).__name__}: {exc}"
            result["completed_at"] = datetime.datetime.now().isoformat(
                timespec="seconds"
            )
            logger.exception(
                "Удаление прервано исключением (частичные изменения возможны, "
                "бэкап: %s)",
                result["backup_path"],
            )

    # Результат запуска ctfmon фиксируется в __exit__ контекстного менеджера
    # (CtfmonSuspender) — служба уже запущена там, повторный вызов не нужен.
    ctfmon_started = suspender.started

    # FIX-6: дрейф-детекция — сравнение снимка ДО мутации с фактом ПОСЛЕ.
    # Выполняется вне try/except: отчёт о расхождении нужен даже тогда, когда
    # операция упала на середине и `result["error"]` уже заполнен.
    if snapshot is not None:
        result["plan_drift"] = _detect_plan_drift(
            branches_now, layout_id, result["admin_privileges"], snapshot
        )

    if retry_total:
        result["retry_cleaned"] = retry_total
        total_deleted += retry_total
        logger.warning(
            "Служба ввода вернула %d записей — выполнена повторная очистка",
            retry_total,
        )

    if total_deleted > 0:
        result["ctfmon_restarted"] = ctfmon_started

    # FIX-26: сверка переключателя. Операция может развести список языков
    # (его показывают Параметры языка) и Preload/CTF (по ним строится панель
    # переключения). Пользователь тогда видит «установленный» язык, которого
    # в переключателе нет, и лечится перезагрузкой. Молчаливый выход — худший
    # вариант: считаем операцию успешной и ОТЧЁТНО говорим о расхождении.
    # Только чтение — CTF пересобирает система при входе, писать в него вслепую
    # опаснее, чем показать правду.
    try:
        switcher_report = check_switcher_consistency()
    except Exception as exc:  # noqa: BLE001 — диагностика не должна ронять операцию
        logger.warning("Сверка переключателя не удалась: %s", exc)
        switcher_report = {}
    result["switcher_check"] = switcher_report
    if not is_switcher_consistent(switcher_report):
        logger.warning(
            "После операции список языков и переключатель расходятся: %s",
            switcher_report,
        )

    # Успех = что-то удалено в реестре ИЛИ список языков реально очищен
    result["success"] = not result["error"] and (
        total_deleted > 0 or (ok and detail == "SUCCESS")
    )
    result["completed_at"] = datetime.datetime.now().isoformat(timespec="seconds")

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
# Очистка (FIX-21: перенесено ВЫШЕ блока CLI `if __name__ == "__main__"`)
#
# Раньше эти функции объявлялись ПОСЛЕ него. Само по себе это работало,
# потому что CLI зовёт enable_sandbox из config, но объявление ниже точки
# входа означает, что любой новый вызов из CLI потребовал бы функции,
# определённой позже по файлу. Порядок объявлений теперь соответствует
# порядку использования.
# ---------------------------------------------------------------------------


def activate_sandbox() -> None:
    """Активировать sandbox-режим (совместимость со старым API)."""
    enable_sandbox()


def deactivate_sandbox() -> None:
    """Выйти из sandbox-режима (используется тестами)."""
    disable_sandbox()


# ---------------------------------------------------------------------------
# CLI entry-point для ручного тестирования
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    from config import enable_sandbox

    parser = argparse.ArgumentParser(
        description="Keyboard Layout Cleaner — CLI для удаления раскладок клавиатуры."
    )
    parser.add_argument(
        "klid",
        nargs="?",
        default=None,
        help="KLID раскладки, например 00000419",
    )
    parser.add_argument(
        "--plan", action="store_true", help="dry-run: показать план без изменений"
    )
    parser.add_argument(
        "--sandbox", action="store_true", help="работать в sandbox-режиме"
    )
    parser.add_argument(
        "--block-cloud-sync",
        action="store_true",
        help="блокировать облачную синхронизацию языков (opt-in)",
    )
    parser.add_argument("--json", action="store_true", help="вывести отчёт в JSON")
    args = parser.parse_args()

    # FIX-29: CLI — вторая точка входа, права выдаются здесь же.
    capabilities.grant(*capabilities.APP_CAPABILITIES)

    if args.sandbox:
        enable_sandbox()

    if not args.klid:
        parser.error("укажите KLID раскладки")

    if args.plan:
        report = plan_layout_removal(args.klid)
    else:
        report = delete_layout(args.klid, block_cloud_sync=args.block_cloud_sync)

    if args.json:
        import json as _json

        print(_json.dumps(report, ensure_ascii=False, indent=2, default=str))  # noqa: T201
    else:
        for key, value in report.items():
            print(f"{key}: {value}")  # noqa: T201
