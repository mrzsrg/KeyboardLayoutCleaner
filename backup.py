"""
backup.py — Регистрационный бэкап и восстановление (FIX-10, шаг 1).

Выделено из ``cleaner.py`` по границе, которую можно доказать тестами:
всё, что ПИШЕТ и ЧИТАЕТ ``.reg``-файл бэкапа, плюс перечисление бэкапов и
их проверка перед импортом. Мутация реестра, список языков, SettingSync и
оркестрация ``delete_layout`` остаются в ``cleaner.py`` — они опираются на
предикаты сопоставления, которые переедут лишь на следующих шагах.

`cleaner.py` реэкспортирует эти имена, поэтому ``cleaner.backup_registry``
и прочие продолжают работать: поведение не менялось, менялось только место
объявления.
"""

import contextlib
import datetime
import logging
import os
import re
import subprocess
import tempfile
import winreg
from pathlib import Path
from typing import Any

import capabilities
import winapi
from applog import get_app_dir
from applog import resource_path as resource_path
from config import TIMEOUTS
from winproc import run_hidden

logger = logging.getLogger("layout_cleaner")

# Путь к PowerShell-скриптам (FIX-20: единый источник истины для ресурсов
# рантайма — applog.resource_path).
_PS_DIR = resource_path("scripts")

# Корни реестра по имени (элементы scanner.AFFECTED_BRANCHES). Живёт здесь,
# потому что это нижний уровень: им пользуются и бэкап, и мутация.
_ROOT_CONST: dict[str, int] = {
    "HKCU": winreg.HKEY_CURRENT_USER,
    "HKU": winreg.HKEY_USERS,
    "HKLM": winreg.HKEY_LOCAL_MACHINE,
}

class BackupError(RuntimeError):
    """
    Бэкап реестра не создан (ни одна ветка не экспортирована).

    delete_layout обязан перехватить это исключение и ОТМЕНИТЬ удаление:
    изменять реестр без возможности отката недопустимо.
    """



# Санити-границы размера .reg-бэкапа перед импортом (restore_registry_backup
# и _check_reg_backup_file — единый источник значений, чтобы проверки не
# разошлись). Нижняя граница — заголовок «Windows Registry Editor Version
# 5.00» плюс хотя бы одна секция ключа; верхняя — с запасом к реальным
# бэкапам (типичный размер 10-500 КБ, полный CTF-профиль — единицы МБ).
REG_BACKUP_MIN_BYTES = 100



REG_BACKUP_MAX_BYTES = 50 * 1024 * 1024



def _check_reg_backup_file(path: Path) -> str:
    """
    Проверить .reg-бэкап перед импортом: существование, размер, BOM/заголовок.

    Импорт повреждённого или постороннего файла — операция, которая меняет
    реестр пользователя; поэтому проверяется не только наличие файла, но и
    его размер и первая строка (настоящий .reg всегда начинается с
    ``Windows Registry Editor Version 5.00``, файл regedit — UTF-16 LE).

    Returns
    -------
    str
        Пустая строка — файл корректен. Иначе — текст причины отказа
        (человеко-читаемый, для UI/лога).
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"не удалось прочитать файл бэкапа: {path} ({exc})"

    if size < REG_BACKUP_MIN_BYTES:
        return (
            f"файл бэкапа повреждён: размер {size} байт, ожидается минимум "
            f"{REG_BACKUP_MIN_BYTES} (заголовок .reg и хотя бы один ключ)"
        )
    if size > REG_BACKUP_MAX_BYTES:
        return (
            f"файл бэкапа слишком велик: {size} байт, ожидается не более "
            f"{REG_BACKUP_MAX_BYTES} — вероятна ошибка экспорта"
        )

    try:
        head = path.read_text(encoding="utf-16")[:120]
    except (UnicodeError, OSError) as exc:
        return f"файл бэкапа не читается как .reg (UTF-16 LE): {exc}"
    if "Windows Registry Editor Version" not in head:
        return (
            "файл бэкапа не похож на .reg (нет заголовка "
            "'Windows Registry Editor Version 5.00')"
        )
    return ""



def _branch_exists(root: str, subkey: str) -> bool:
    """
    Существует ли ветка реестра.

    Отсутствие ветки — НЕ отказ бэкапа: в ней нечего бэкапить и нечего
    удалять. Отличать этот случай от реального сбоя экспорта обязательно:
    ``reg export`` несуществующего ключа возвращает код 1 («Не удается
    найти указанный раздел или параметр в реестре»), поэтому без такой
    проверки любой бэкап на чистой системе помечался бы неполным, а
    настоящий отказ экспорта тонул бы в этом шуме.

    При любой ошибке доступа возвращается ``True`` (пессимистично):
    ветка может существовать, значит провал её экспорта должен считаться
    реальным отказом, а не «ветки нет».
    """
    key_const = _ROOT_CONST.get(root.strip().upper())
    if key_const is None:
        # Неизвестный корень — решение не берём на себя: пусть экспорт
        # провалится, и ветка попадёт в failed (fail-closed).
        return True
    try:
        with winreg.OpenKey(key_const, subkey, 0, winreg.KEY_READ):
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return True



def is_admin() -> bool:
    """
    Проверить, запущен ли процесс с правами администратора.

    Использует WinAPI ``IsUserAnAdmin`` (Shell32.dll).
    """
    try:
        return winapi.is_user_an_admin()
    except OSError:
        return False



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
            timeout=TIMEOUTS["reg_export"],
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



def _reserve_backup_file(backup_dir: Path, base_name: str) -> Path:
    """
    Атомарно занять имя файла бэкапа (FIX-9).

    Создаём пустой файл с ``O_CREAT | O_EXCL`` — операция атомарна на уровне
    ядра, поэтому два процесса, стартовавшие в одну микросекунду, НЕ могут
    получить одно имя: второй получит ``EEXIST`` и возьмёт следующий
    суффикс. Без этого ``reg export /y`` молча перезаписал бы бэкап,
    созданный другим процессом (портативный режим: два окна, два флешки).

    Суффикс ``_1``, ``_2``… сохраняет сортируемость по времени: и базовое
    имя, и суффикс идут после полного таймстампа.

    Returns
    -------
    Path
        Путь к СОЗДАННОМУ пустому файлу. Дальше `backup_registry` пишет в
        него содержимое штатным способом.
    """
    attempt = 0
    while True:
        suffix = "" if attempt == 0 else f"_{attempt}"
        candidate = backup_dir / f"{base_name}{suffix}.reg"
        try:
            # O_BINARY нужен на Windows, чтобы не зависеть от текстового
            # режима CRT (переводы строк в пустом файле не важны, но
            # поведение должно быть предсказуемым).
            fd = os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o666)
        except FileExistsError:
            attempt += 1
            # Страховка от бесконечного цикла при тысячах совпадений.
            if attempt > 1000:
                raise BackupError(
                    f"не удалось занять имя файла бэкапа в {backup_dir} "
                    f"(попыток: 1001)"
                ) from None
            continue
        else:
            os.close(fd)
            return candidate



def _release_reserved_backup(backup_file: Path) -> None:
    """Освободить зарезервированное имя при отказе бэкапа (FIX-9).

    Резерв создаёт пустой файл. Если экспорт не удался, этот файл нельзя
    оставлять: пользователь увидит «бэкап», из которого ничего не
    восстановить, — ровно тот случай, который fail-closed запрещает.
    Ошибка удаления глушится: уже сообщается о настоящей причине отказа,
    а подмена её ошибкой unlink только запутала бы.
    """
    with contextlib.suppress(OSError):
        backup_file.unlink(missing_ok=True)



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
        Папка для сохранения бэкапа. По умолчанию — ``<папка приложения>/backups``
        (портативный режим), а НЕ ``cwd``.
    report : dict, optional
        Если передан словарь — в него записываются списки:
        ``exported`` — ветки, реально попавшие в файл,
        ``failed`` — ветки, экспорт которых не удался (сбой ``reg export``,
        пустая или нечитаемая секция),
        ``skipped`` — ветки, которых нет в реестре (бэкапить нечего).
        Вызывающий код обязан считать непустой ``failed`` отказом:
        удаление без полного бэкапа необратимо.

    Returns
    -------
    str
        Полный путь к созданному файлу бэкапа.

    Raises
    ------
    BackupError
        Fail-closed: если не экспортирована хотя бы одна СУЩЕСТВУЮЩАЯ
        ветка, либо если не экспортирована ни одна ветка. Частично
        восстановимый бэкап не создаётся: иначе пользователь получит
        «восстановление», которое молча не вернёт часть состояния.
        Отсутствующие в реестре ветки отказом не считаются (см.
        ``_branch_exists``).
    """
    # Портативный режим: по умолчанию бэкапы кладём рядом с приложением
    # (в собранном .exe — папка с exe, например флешка), а не в cwd.
    backup_dir_path = Path(backup_dir) if backup_dir else get_app_dir() / "backups"
    try:
        backup_dir_path.mkdir(parents=True, exist_ok=True)
        # Тестовая запись: каталог может существовать, но быть недоступным
        # для записи (Program Files, сетевой диск, read-only носитель) —
        # падать позже, на записи .reg, значит терять понятную диагностику.
        probe = backup_dir_path / ".klc_write_test"
        probe.write_text("probe", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except PermissionError as exc:
        raise BackupError(
            f"нет прав на запись в папку бэкапов: {backup_dir_path}. "
            "Выберите другую папку или запустите приложение от имени "
            "администратора."
        ) from exc
    except OSError as exc:
        raise BackupError(
            f"не удалось подготовить папку бэкапов {backup_dir_path}: {exc}"
        ) from exc

    # FIX-9: имя включает микросекунды, но этого НЕ достаточно: два
    # процесса, стартовавших в одну микросекунду, получили бы одно имя, а
    # `reg export /y` перезаписал бы чужой файл. Поэтому файл создаётся
    # эксклюзивно (O_EXCL) — это атомарная операция ядра: выиграет ровно
    # один процесс, второй получит EEXIST и возьмёт следующий суффикс.
    # Читаемость имени (сортировка по времени) сохраняется полностью.
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base_name = f"keyboard_layout_backup_{timestamp}"
    # Резерв создаёт пустой файл. Если бэкап не состоится, резерв ОБЯЗАН быть
    # удалён — иначе появился бы ровно тот файл-заглушка, который fail-closed
    # контракт запрещает (FIX-1/FIX-16: «файл-заглушка не создаётся, если
    # не экспортирована ни одна ветка»). Освобождение имени — отдельная
    # ответственность, а не побочный эффект успеха.
    backup_file = _reserve_backup_file(backup_dir_path, base_name)

    # reg export ПЕРЕЗАПИСЫВАЕТ целевой файл (/y), поэтому прямой экспорт
    # всех веток в один файл оставлял в бэкапе только последнюю ветку.
    # Экспортируем каждую ветку во временный файл и объединяем секции
    # в один .reg (формат regedit: UTF-16 LE с BOM + единый заголовок).
    exported_sections: list[str] = []
    exported_branch_names: list[str] = []
    failed_branches: list[str] = []
    skipped_branches: list[str] = []

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

            # Ветки нет — экспортировать нечего и удалять в ней нечего:
            # это НЕ отказ бэкапа. Реальный сбой экспорта (reg export вернул
            # ошибку) попадёт в failed_branches и отменит операцию.
            if not _branch_exists(root, subkey):
                skipped_branches.append(branch_name)
                logger.info(
                    "Бэкап: ветка %s отсутствует — пропускаем (бэкапить нечего)",
                    branch_name,
                )
                continue

            part_file = tmp_dir / f"branch_{idx}.reg"
            try:
                exported = _export_single_key_via_reg(
                    export_root, export_subkey, part_file
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                exported = False
                logger.error("Экспорт ветки %s не выполнен: %s", branch_name, exc)

            if not exported or not part_file.exists():
                logger.error(
                    "Бэкап: ветка %s НЕ экспортирована (reg export вернул "
                    "ошибку или файл пуст)",
                    branch_name,
                )
                failed_branches.append(branch_name)
                continue

            try:
                text = part_file.read_text(encoding="utf-16")
            except (UnicodeError, OSError) as exc:
                # Файл создан, но секция не читается: ветка НЕ забэкаплена.
                # Раньше она всё равно попадала в exported, и отчёт врал —
                # бэкап считался полным, хотя секции в файле не было.
                logger.error(
                    "Бэкап: секцию ветки %s не удалось прочитать: %s",
                    branch_name,
                    exc,
                )
                failed_branches.append(branch_name)
                continue

            lines = [
                line
                for line in text.splitlines()
                if line.strip()
                and not line.strip().startswith("Windows Registry Editor Version")
            ]
            if not lines:
                logger.error(
                    "Бэкап: ветка %s экспортирована пустой секцией", branch_name
                )
                failed_branches.append(branch_name)
                continue

            # Ветка попадает в exported ТОЛЬКО после того, как её секция
            # реально добавлена в файл (порядок важен для fail-closed).
            exported_sections.append("\r\n".join(lines))
            exported_branch_names.append(branch_name)

    # Отчёт заполняется ДО возможных отказов: вызывающий код и тесты должны
    # видеть фактическую классификацию веток, а не только текст исключения.
    if report is not None:
        report["exported"] = exported_branch_names
        report["failed"] = failed_branches
        report["skipped"] = skipped_branches

    if failed_branches:
        # Fail-closed: бэкап, из которого нельзя восстановить все
        # СУЩЕСТВУЮЩИЕ ветки, не создаём. Иначе пользователь получит
        # «восстановление», молча не возвращающее часть состояния.
        logger.error(
            "Бэкап не создан: не экспортированы существующие ветки: %s",
            failed_branches,
        )
        _release_reserved_backup(backup_file)
        raise BackupError(
            "не удалось экспортировать существующие ветки реестра "
            f"({', '.join(failed_branches)}) — удаление отменено, чтобы не "
            "потерять данные без возможности отката"
        )

    if not exported_sections:
        # НИ ОДНА ветка не экспортирована: файл-заглушка НЕ создаём —
        # удалять записи без возможности отката недопустимо. Сюда же
        # попадает случай «все ветки отсутствуют»: удалять в них нечего.
        logger.error(
            "Бэкап не создан: ни одна ветка не экспортирована. Отсутствуют: %s",
            skipped_branches or "—",
        )
        # FIX-9: резерв имени тоже не должен пережить отказ, иначе
        # появится пустой .reg — ровно тот файл-заглушка, который здесь
        # и запрещён.
        _release_reserved_backup(backup_file)
        raise BackupError(
            "ни одна ветка реестра не была экспортирована "
            f"(отсутствуют: {', '.join(skipped_branches) or '—'})"
        )

    content = (
        "Windows Registry Editor Version 5.00"
        + "\r\n\r\n"
        + "\r\n\r\n".join(exported_sections)
        + "\r\n"
    )

    if skipped_branches:
        logger.info("Бэкап создан без отсутствующих веток: %s", skipped_branches)

    # BOM UTF-16 LE (FF FE) — чтобы regedit корректно импортировал файл
    backup_file.write_bytes(b"\xff\xfe" + content.encode("utf-16-le"))

    return str(backup_file.resolve())



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

    pattern = re.compile(r"^keyboard_layout_backup_(\d{8}_\d{6}(?:_\d{6})?)\.reg$")
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
            # Базовые 15 символов "YYYYMMDD_HHMMSS" — микросекунды не нужны
            ts = datetime.datetime.strptime(match.group(1)[:15], "%Y%m%d_%H%M%S")
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
    # FIX-29: reg import пишет в реестр мимо winreg, поэтому песочница
    # зависит только от подмены run_hidden. Право — независимый барьер.
    capabilities.require(
        capabilities.Capability.REG_IMPORT, "restore_registry_backup"
    )
    path = Path(reg_path)
    if not path.is_file():
        return {
            "ok": False,
            "detail": f"файл бэкапа не найден: {path}",
            "elevated": False,
            "needs_admin": False,
        }

    if bad := _check_reg_backup_file(path):
        return {
            "ok": False,
            "detail": bad,
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
                timeout=TIMEOUTS["reg_import_uac"],
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
            timeout=TIMEOUTS["reg_import"],
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


