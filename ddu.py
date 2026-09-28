r"""Default User Profile — профиль по умолчанию (``C:\Users\Default``).

Зачем этот модуль
-----------------
Приложение чистит ``HKU\.DEFAULT\Keyboard Layout\Preload`` и на этом
считало задачу выполненной. На живой машине выяснилось: Windows пересобирает
``HKU\.DEFAULT`` при **каждой загрузке** из файла профиля по умолчанию. То
есть чистка живёт ровно до перезагрузки, а дальше Windows возвращает
удалённую раскладку сам — и она снова появляется в переключателе.

Почему это не лечится «ещё одной веткой»
---------------------------------------
``HKU\.DEFAULT`` — только проекция. Настоящий источник — файл
``NTUSER.DAT`` в профиле по умолчанию. Править его можно, только подгрузив
hive (``reg load``), и тогда чистка переживает перезагрузку.

Ограничения, которые модуль держит намеренно
--------------------------------------------
* Правится **ровно одна** ветка — ``Keyboard Layout\\Preload``. Весь
  ``NTUSER.DAT`` не трогаем: предыдущая попытка проекта
  (``Copy-UserInternationalSettingsToSystem``) переписывала ``HKU\.DEFAULT``
  целиком, эти ветки не бэкапились, и откат был невозможен — операцию
  удалили.
* Hive **всегда** выгружается, даже при ошибке: оставленный подгруженным
  ``NTUSER.DAT`` — это заблокированный файл профиля по умолчанию, который
  может не отдать его следующему обновлению Windows.
* В песочнице модуль — полный no-op. Шаблон общий для всей машины, трогать
  его в тестах нельзя ни при каких условиях.
* Без прав администратора — тоже no-op, с честной причиной в отчёте, а не
  с молчаливым пропуском.
"""
from __future__ import annotations

import contextlib
import logging
import os
import winreg
from pathlib import Path
from typing import TYPE_CHECKING

import capabilities
import config
import mutate
import winapi
import winproc

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Iterator

logger = logging.getLogger("layout_cleaner")

#: Файл профиля по умолчанию. Переопределяется переменной окружения только
#: в тестах; в бою путь фиксирован.
DDU_HIVE_PATH = r"C:\Users\Default\NTUSER.DAT"

#: Имя подгрузки. Своё, чтобы не пересечься с чужими ``reg load``;
#: префикс ``_KLC_`` делает источник опознаваемым в списке hive-ов.
MOUNT_NAME = "_KLC_DDU"

#: Ветка ``Preload`` — одна из нескольких (см. :data:`BRANCHES`).
PRELOAD_SUBKEY = r"Keyboard Layout\Preload"

#: Ветки шаблона, которые приложение обязано вычистить.
#:
#: Их набор - ровно тот, что чистится у пользователя. Раньше шаблон
#: чистился только по ``Keyboard Layout\Preload``, и этого хватало лишь до
#: перезагрузки: Windows пересобирает ``HKU\.DEFAULT`` из остальных веток
#: шаблона, где раскладка оставалась. Найдено на живой машине дампом
#: шаблона: US оставался в ``Control Panel\International\User Profile``,
#: в копии ``User Profile System Backup`` и в ``Software\Microsoft\CTF``.
BRANCHES: tuple[tuple[str, str], ...] = (
    (PRELOAD_SUBKEY, "preload"),
    (r"Keyboard Layout\Substitutes", "substitutes"),
    # Именно ``...\User Profile``, а не ``...\International``:
    # ``_clean_intl_profile`` ищет подключи-языки (``en-US``) СРАЗУ под
    # переданным путём. С уровнем выше он искал ``International\en-US``,
    # которого нет, и не удалял ничего — тихо. Тот же путь, что и в
    # scanner.get_affected_branches().
    (r"Control Panel\International\User Profile", "intl"),
    # Отдельная ветка, которой нет в списке пользователя: в шаблоне она
    # тоже объявляет ``0409:00000409``, и именно из шаблона Windows
    # пересобирает HKU\.DEFAULT.
    (r"Control Panel\International\User Profile System Backup", "intl"),
    (r"Software\Microsoft\CTF", "ctf"),
)


class DduError(RuntimeError):
    """Не удалось подгрузить/выгрузить/изменить профиль по умолчанию."""


def hive_path() -> Path:
    """Путь к файлу профиля по умолчанию.

    Переопределяется переменной окружения ``KLC_DDU_HIVE`` — только чтобы
    тесты работали на файле-заглушке, а не на настоящем шаблоне машины.
    """
    return Path(os.environ.get("KLC_DDU_HIVE", DDU_HIVE_PATH))


def mounted_subkey() -> str:
    """Путь к ветке Preload в подгруженном виде (без имени корня)."""
    return f"{MOUNT_NAME}\\{PRELOAD_SUBKEY}"


def mounted_branch(subkey: str) -> str:
    """Путь к произвольной ветке шаблона в подгруженном виде."""
    return f"{MOUNT_NAME}\\{subkey}"


def is_hive_file_present() -> bool:
    """Файл профиля по умолчанию существует."""
    return hive_path().is_file()


def is_blocked_reason() -> str:
    """Причина, по которой шаблон трогать нельзя, или ``""`` если можно.

    Пустая строка означает «можно работать» — вызывающий код обязан
    проверять именно её, а не ``is_available``: молчаливый пропуск здесь
    означал бы возврат раскладки при следующей перезагрузке без единого
    слова пользователю.
    """
    if config.is_sandbox_enabled():
        return "песочница: профиль по умолчанию общий для машины, не трогаем"
    try:
        admin = winapi.is_user_an_admin()
    except OSError:
        admin = False
    if not admin:
        return "нет прав администратора"
    if not is_hive_file_present():
        return f"файл профиля по умолчанию не найден: {hive_path()}"
    return ""



def _reg(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return winproc.run_hidden(["reg", *args], timeout=timeout, check=False)


def _is_mounted() -> bool:
    """Подгружен ли hive прямо сейчас (не нами — тоже считается)."""
    return _reg(["query", f"HKU\\{MOUNT_NAME}"]).returncode == 0


@contextlib.contextmanager
def mounted() -> Iterator[None]:
    """Подгрузить hive профиля по умолчанию и **обязательно** выгрузить.

    Имя подгрузки занято — сначала пробуем выгрузить: ``_KLC_DDU`` зарезервирован
    за этим модулем, и оставшийся после сбоя процессов mount принадлежит нам
    же. Чужие подгрузки не трогаем: там другое имя.

    Право ``REGISTRY_MUTATE`` требуется здесь же (FIX-29), а не только внутри
    ``mutate._clean_preload_keys``: подгрузка hive и ``reg import`` сами по
    себе разрушительны, и защита обязана стоять там, где операция, а не там,
    где её когда-то позвали.
    """
    capabilities.require(capabilities.Capability.REGISTRY_MUTATE, "ddu.mounted")
    if _is_mounted():
        logger.warning(
            "Подгрузка %s уже была в реестре (вероятно, осталась после сбоя) "
            "- выгружаем её перед повторной загрузкой",
            MOUNT_NAME,
        )
        _reg(["unload", f"HKU\\{MOUNT_NAME}"])

    result = _reg(["load", f"HKU\\{MOUNT_NAME}", str(hive_path())], timeout=60)
    if result.returncode != 0:
        raise DduError(
            f"не удалось подгрузить профиль по умолчанию "
            f"({hive_path()}): {result.stderr or result.returncode}"
        )
    try:
        yield
    finally:
        # Выгрузка обязана произойти при ЛЮБОМ исходе, включая исключение
        # внутри тела: незакрытый hive блокирует файл профиля по умолчанию.
        unload = _reg(["unload", f"HKU\\{MOUNT_NAME}"])
        if unload.returncode != 0:
            logger.error(
                "Не удалось выгрузить %s: %s - файл профиля по умолчанию может "
                "остаться заблокированным",
                MOUNT_NAME,
                unload.stderr or unload.returncode,
            )
        else:
            logger.info("Профиль по умолчанию выгружен")


def export_preload(dest: Path) -> bool:
    """Выгрузить ``Preload`` профиля по умолчанию в ``.reg``.

    ``True`` — секция выгружена и пригодна для отката. ``False`` — ветки нет
    либо подгрузка не удалась; тогда откатывать будет нечего, и это должно
    быть видно, а не проглочено.
    """
    reason = is_blocked_reason()
    if reason:
        logger.info("Профиль по умолчанию не выгружается: %s", reason)
        return False
    try:
        with mounted():
            # Выгружается поддерево целиком, а не одна ветка: список веток,
            # которые чистит wipe_preload, может разойтись с тем, что
            # выгружено, и тогда откат окажется неполным ровно там, где
            # была чистка. Шаблон весит ~256 КБ, полный дамп уместен.
            result = _reg(
                ["export", f"HKU\\{MOUNT_NAME}", str(dest), "/y"], timeout=60
            )
    except DduError as exc:
        logger.error("Профиль по умолчанию не выгружен: %s", exc)
        return False
    if result.returncode != 0 or not dest.exists():
        logger.error(
            "reg export профиля по умолчанию не выполнен: %s",
            result.stderr or result.returncode,
        )
        return False
    logger.info("Preload профиля по умолчанию выгружен: %s", dest)
    return True


def wipe_preload(layout_klid: str) -> list[str]:
    """Удалить записи KLID из всех веток шаблона (FIX-37).      Логика та же, что у пользовательской ветки, — переиспользуются     функции ``mutate._clean_*``, чтобы правила сопоставления KLID     (hex / десятичный HKL, MULTI_SZ) и правила точечной чистки     профиля языка не разошлись между пользователем и шаблонами.
    (hex / десятичный HKL, MULTI_SZ) не разошлись между ветками.
    """
    reason = is_blocked_reason()
    if reason:
        logger.info("Профиль по умолчанию не чистится: %s", reason)
        return []
    deleted: list[str] = []
    try:
        with mounted():
            root = winreg.HKEY_USERS
            for subkey, mode in BRANCHES:
                path = f"{MOUNT_NAME}\\{subkey}"
                before = len(deleted)
                if mode == "preload":
                    deleted += mutate._clean_preload_keys(
                        root, path, layout_klid, True
                    )
                elif mode == "substitutes":
                    deleted += mutate._clean_substitutes_keys(
                        root, path, layout_klid, True
                    )
                elif mode == "intl":
                    deleted += mutate._clean_intl_profile(
                        root, path, layout_klid, True
                    )
                elif mode == "ctf":
                    mutate._clean_ctf_profiles(root, path, layout_klid, True)
                    deleted += mutate._clean_branch_recursive(
                        root, path, layout_klid, "preload", True
                    )
                if len(deleted) > before:
                    logger.info(
                        "Очистка профиля по умолчанию / %s: удалено значений: %d",
                        subkey,
                        len(deleted) - before,
                    )
    except DduError as exc:
        logger.error("Профиль по умолчанию не очищен: %s", exc)
        return []
    if deleted:
        logger.info(
            "Очистка профиля по умолчанию / %s: удалено значений: %d",
            PRELOAD_SUBKEY,
            len(deleted),
        )
    return deleted


def restore_preload(reg_file: Path) -> bool:
    """Вернуть ``Preload`` профиля по умолчанию из выгрузки.

    Импорт — merge, как и у пользовательской ветки: значения, появившиеся
    после удаления, сохраняются.
    """
    reason = is_blocked_reason()
    if reason:
        logger.info("Профиль по умолчанию не восстанавливается: %s", reason)
        return False
    if not reg_file.exists():
        logger.info("Нет выгрузки профиля по умолчанию: %s", reg_file)
        return False
    try:
        with mounted():
            result = _reg(["import", str(reg_file)], timeout=60)
    except DduError as exc:
        logger.error("Профиль по умолчанию не восстановлен: %s", exc)
        return False
    if result.returncode != 0:
        logger.error(
            "reg import профиля по умолчанию не выполнен: %s",
            result.stderr or result.returncode,
        )
        return False
    logger.info("Preload профиля по умолчанию восстановлен из %s", reg_file)
    return True
