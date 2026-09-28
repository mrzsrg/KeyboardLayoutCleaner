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

Четыре требования, которые нельзя нарушать
----------------------------------------

1. **Сохранять через промежуточный файл (``reg save`` + ``copyfile``).**
   ``reg unload`` **отбрасывает** все изменения подгруженного hive. Без
   ``reg save`` файл остаётся байт в байт прежним: чистка проходит,
   журнал радостно пишет об успехе, а Windows получает ровно то, что
   лежало до запуска. Именно так выглядел инцидент на живой машине —
   ``mtime`` шаблона не менялся. Отсюда же: ``reg import`` при откате
   без ``reg save`` тоже не работает, откат шаблона был фиктивным.
   Но писать ``reg save`` надо **не в сам шаблон**: команда ``reg save
   HKU\\_KLC_DDU C:\Users\Default\NTUSER.DAT`` на живой машине дважды
   подряд отработала таймаут (120 с) — операция выглядела зависшей. Hive
   сохраняется во временный файл рядом с шаблоном, а сам шаблон
   переписывается уже после выгрузки (см. :func:`mounted`).
2. **Чистить носитель языка, а не только подключи-языки.** В шаблоне носителем
   языка являются сразу три вещи, и чистки одной из них недостаточно:
   значение ``Languages`` (REG_MULTI_SZ) в ``Control Panel\\International\\
   User Profile`` и его копии ``... System Backup``; ключи профилей
   ``<язык>`` с привязками методов ввода — значениями, ИМЯ которых равно
   TIP ``0409:00000409`` (данные — флаг установки); нумерация CTF. Из них
   Windows пересобирает ``HKU\\.DEFAULT`` при загрузке, поэтому чистки
   подключей для этого недостаточно.
3. **Порядок в шаблоне: ``Languages`` → профили.** Судьбу подключа решает
   то, значится ли его язык в ``Languages``: пока значится, профиль
   сохраняется — даже опустевшим после точечной чистки. На живой машине
   из-за обратного порядка в шаблоне оставался ``en-US`` с привязкой к
   US-раскладке, и US возвращалась после перезагрузки (FIX-37g).
4. **Говорить, когда ничего не нашлось.** Журнал, молчащий про нули,
   выглядит как успех — и стоит времени на разбор инцидента.

Ограничения, которые модуль держит намеренно
--------------------------------------------
* Правятся **перечисленные в** :data:`BRANCHES` **ветки** — точечно, а не
  «переписать ``HKU\.DEFAULT`` целиком»: предыдущая попытка проекта
  (``Copy-UserInternationalSettingsToSystem``) переписывала ``HKU\.DEFAULT``
  целиком, эти ветки не бэкапились, и откат был невозможен — операцию
  удалили.
* Hive **всегда** выгружается, даже при ошибке: оставленный подгруженным
  ``NTUSER.DAT`` — это заблокированный файл профиля по умолчанию, который
  может не отдать его следующему обновлению Windows. Раньше это было
  обещанием, а не свойством: исключение от ``reg save`` вылетало раньше
  строки выгрузки и пропускало её, так что на живой машине подгрузка
  ``HKU\\_KLC_DDU`` осталась в реестре (FIX-37e). Теперь выгрузка стоит
  в собственном ``finally`` и выполняется при любом исходе.
* В песочнице модуль — полный no-op. Шаблон общий для всей машины, трогать
  его в тестах нельзя ни при каких условиях.
* Без прав администратора — тоже no-op, с честной причиной в отчёте, а не
  с молчаливым пропуском.
"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import winreg
from pathlib import Path
from typing import TYPE_CHECKING

import capabilities
import config
import mutate
import winapi
import winproc

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger("layout_cleaner")

#: Файл профиля по умолчанию. Переопределяется переменной окружения только
#: в тестах; в бою путь фиксирован.
DDU_HIVE_PATH = r"C:\Users\Default\NTUSER.DAT"

#: Имя подгрузки. Своё, чтобы не пересечься с чужими ``reg load``;
#: префикс ``_KLC_`` делает источник опознаваемым в списке hive-ов.
MOUNT_NAME = "_KLC_DDU"

#: Суффикс промежуточного файла ``reg save`` рядом с шаблоном. Постоянный,
#: а не с PID: остаток упавшего запуска перезаписывается сам собой, а не
#: копится в ``C:\Users\Default`` мусором.
STAGING_SUFFIX = ".klc-save"

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


# ---------------------------------------------------------------------------
# Канал ошибки сохранения шаблона
# ---------------------------------------------------------------------------
# «Шаблон не записан на диск» — это НЕ отсутствие удалённых значений, а отказ,
# и снаружи выглядит ровно так же: пустой список. Считать такой исход успехом
# нельзя — при следующей загрузке Windows соберёт HKU\.DEFAULT из шаблона и
# вернёт раскладку. Поэтому статус сохранения живёт отдельным каналом, и
# вызывающий (cleaner) обязан его забрать: иначе отказ не увидит никто.
_save_error = ""


def _remember_save_error(reason: str) -> None:
    """Запомнить причину сбоя сохранения шаблона (для :func:`take_save_error`)."""
    global _save_error
    _save_error = reason


def take_save_error() -> str:
    """Забрать и сбросить причину последнего сбоя сохранения шаблона.

    ``''`` — либо сохранение прошло, либо мутация не запускалась. Значение
    живёт в модуле, поэтому забирать его надо после КАЖДОЙ операции: иначе
    причина прошлой утечёт в отчёт следующей.
    """
    global _save_error
    error, _save_error = _save_error, ""
    return error


def staging_path() -> Path:
    """Путь промежуточного файла, куда ``reg save`` кладёт подгруженный hive.

    Рядом с шаблоном — значит, копирование не пересекает границу томов — и
    обязательно **не** сам шаблон: ``reg save`` в файл, из которого hive
    подгружен, не возвращается (см. :func:`save_to_staging`).
    """
    target = hive_path()
    return target.with_name(target.name + STAGING_SUFFIX)


def _drop(path: Path) -> None:
    """Убрать промежуточный файл. Его отсутствие — не проблема."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Промежуточный файл %s не удалён: %s", path, exc)


def save_to_staging() -> Path | None:
    """Сохранить подгруженный hive во временный файл (``reg save``).

    Без этого выгрузка отменяет всю работу. ``reg unload`` по
    документации **отбрасывает** все изменения, сделанные в подгруженном
    hive: файл ``NTUSER.DAT`` остаётся байт в байт прежним.

    На живой машине это выглядело как «приложение отработало, а US вернулась
    после перезагрузки»: чистка проходила, в логе были строки об успехе, но
    mtime шаблона не менялся — Windows получала ровно то, что лежало там
    до запуска.

    Именно во временный файл, а не в сам ``NTUSER.DAT``: на живой машине
    ``reg save`` в подгруженный источник дважды подряд отработал таймаут
    (120 с) — операция выглядела зависшей, а подгрузка осталась висеть и
    держать файл шаблона занятым. Сам шаблон переписывается отдельно и уже
    после выгрузки, см. :func:`commit_staging`.

    Вызывается строго из :func:`mounted` и только при ``save=True``.

    Returns
    -------
    Path | None
        Путь к сохранённому hive; ``None`` — не сохранилось (причина в
        журнале и в :func:`take_save_error`, промежуточного файла не остаётся).
    """
    staged = staging_path()
    # Остаток прошлого упавшего запуска: reg save в уже существующий файл не
    # пишет и возвращает ошибку без внятного объяснения.
    _drop(staged)
    try:
        result = _reg(
            ["save", f"HKU\\{MOUNT_NAME}", str(staged)],
            timeout=config.TIMEOUTS["reg_save"],
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Именно этот случай разорвал операцию на живой машине: исключение
        # вылетело из блока finally и пропустило выгрузку подгрузки.
        _drop(staged)
        _remember_save_error(f"reg save не вернулся: {exc}")
        logger.error("Профиль по умолчанию НЕ сохранён: reg save: %s", exc)
        return None
    if result.returncode != 0:
        _drop(staged)
        _remember_save_error(
            f"reg save вернул код {result.returncode}: "
            f"{result.stderr or result.stdout or 'без вывода'}"
        )
        logger.error(
            "Профиль по умолчанию НЕ сохранён (%s): %s - правки будут потеряны",
            staged,
            result.stderr or result.returncode,
        )
        return None
    return staged


def commit_staging(staged: Path) -> bool:
    """Переписать шаблон сохранённым hive — только после выгрузки.

    Именно ``copyfile``, а не ``os.replace``: файл перезаписывается по месту,
    поэтому у шаблона остаются прежние права доступа и владелец. У файла,
    созданного ``reg save``, свои права — подмена отдала бы профиль по
    умолчанию не тем, кому он должен быть доступен.

    Пустой файл не принимается: пустой шаблон хуже нетронутого, он ломает
    создание учётных записей на всей машине.
    """
    try:
        size = staged.stat().st_size
    except OSError as exc:
        _drop(staged)
        _remember_save_error(f"промежуточный файл недоступен: {exc}")
        logger.error("Профиль по умолчанию НЕ сохранён: %s", exc)
        return False
    if size == 0:
        _drop(staged)
        _remember_save_error(f"reg save создал пустой файл {staged}")
        logger.error(
            "Профиль по умолчанию НЕ сохранён: %s пуст - пустой шаблон "
            "заблокировал бы создание учётных записей",
            staged,
        )
        return False
    target = hive_path()
    try:
        shutil.copyfile(staged, target)
    except OSError as exc:
        # Сюда попадает и «файл занят» — то есть подгрузка не снята.
        _remember_save_error(f"{target} не перезаписан: {exc}")
        logger.error(
            "Профиль по умолчанию не перезаписан (%s): %s. Если файл занят - "
            "подгрузка не снята, снимите её вручную: reg unload HKU\\%s",
            target,
            exc,
            MOUNT_NAME,
        )
        return False
    finally:
        _drop(staged)
    logger.info("Профиль по умолчанию сохранён на диск: %s (%d байт)", target, size)
    return True


@contextlib.contextmanager
def mounted(*, save: bool = False) -> Iterator[None]:
    """Подгрузить hive профиля по умолчанию и **обязательно** выгрузить.

    ``save=True`` дополнительно сохраняет hive в файл перед выгрузкой.
    Обязателен для любой мутации: правки без ``reg save`` не переживают
    ``reg unload`` (см. :func:`save_to_staging`). Выгрузка для чтения
    (``export_preload``) сохранять не должна — она не меняет ничего.

    Порядок: ``reg save`` во временный файл → ``reg unload`` → перезапись
    самого шаблона. Шаблон переписывается **после** выгрузки: пока hive
    подгружен, файл остаётся занятым.

    Выгрузка стоит в собственном ``finally``. Раньше исключение от
    ``reg save`` вылетало раньше строки выгрузки и пропускало её, так что
    на живой машине подгрузка ``HKU\\_KLC_DDU`` осталась в реестре и держала
    файл шаблона занятым (FIX-37e).

    Имя подгрузки занято — сначала пробуем выгрузить: ``_KLC_DDU`` зарезервирован
    за этим модулем, и оставшийся после сбоя процессов mount принадлежит нам
    же. Чужие подгрузки не трогаем: там другое имя.

    Право ``REGISTRY_MUTATE`` требуется здесь же (FIX-29), а не только внутри
    ``mutate._clean_preload_keys``: подгрузка hive, ``reg save`` и ``reg
    import`` сами по себе разрушительны, и защита обязана стоять там, где
    операция, а не там, где её когда-то позвали.
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
    staged: Path | None = None
    try:
        try:
            yield
        finally:
            # Сохранение ПЕРЕД выгрузкой: обратный порядок недопустим — после
            # unload подгрузки уже нечего сохранять. Отдельный finally, чтобы
            # сбой сохранения не отменил выгрузку ниже.
            if save:
                staged = save_to_staging()
    finally:
        # Выгрузка обязана произойти при ЛЮБОМ исходе, включая исключение
        # внутри тела и сбой reg save: незакрытый hive блокирует файл профиля
        # по умолчанию — общий для машины.
        unload = _reg(["unload", f"HKU\\{MOUNT_NAME}"])
        if unload.returncode != 0:
            logger.error(
                "Не удалось выгрузить %s: %s - файл профиля по умолчанию может "
                "остаться заблокированным. Снимите подгрузку вручную от имени "
                "администратора: reg unload HKU\\%s",
                MOUNT_NAME,
                unload.stderr or unload.returncode,
                MOUNT_NAME,
            )
        else:
            logger.info("Профиль по умолчанию выгружен")
        # Шаблон перезаписывается ПОСЛЕ выгрузки: пока hive подгружен, файл
        # занят и копирование упало бы с «файл занят».
        if staged is not None:
            commit_staging(staged)


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
    """Удалить записи KLID из всех веток шаблона (FIX-37).

    Логика та же, что у пользовательской ветки, — переиспользуются
    функции ``mutate._clean_*``, чтобы правила сопоставления KLID
    (hex / десятичный HKL, MULTI_SZ) и правила точечной чистки
    профиля языка не разошлись между пользователем и шаблонами.

    Одно исключение — ``mutate._clean_intl_languages``. Живой пользователь
    не позволяет трогать значение ``Languages`` (это его активный язык,
    его переписывает PowerShell), а в шаблоне носителем языка является
    именно оно, и никакого PowerShell-шага там нет. Функция вызывается
    только здесь; см. её докстринг.

    Подгрузка обязана завершаться сохранением в файл (см. :func:`mounted`):
    без него выгрузка отменяет правки и шаблон остаётся прежним.

    Статус сохранения возвращается **отдельно**, через
    :func:`take_save_error`: пустой список deleted одинаков для «в шаблоне
    ничего не было» и «правки не дошли до диска», а второе — это отказ, при
    котором раскладка вернётся после перезагрузки. Вызывающий обязан
    забрать этот статус и показать его пользователю.
    """
    # Операция начинается с чистого состояния: сбой прошлой не должен
    # попасть в отчёт этой.
    take_save_error()
    reason = is_blocked_reason()
    if reason:
        logger.info("Профиль по умолчанию не чистится: %s", reason)
        return []
    deleted: list[str] = []
    try:
        # save=True: без `reg save` выгрузка отменит все правки шаблона.
        with mounted(save=True):
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
                    # ПОРЯДОК ВАЖЕН (FIX-37g): сначала значение `Languages`,
                    # потом профили. `_clean_intl_profile` решает судьбу
                    # подключа по тому, значится ли его язык в `Languages`;
                    # при обратном порядке en-US ещё числился активным, и
                    # опустевший ключ `User Profile\en-US` оставался в
                    # шаблоне — вместе с привязкой к US-раскладке.
                    #
                    # Значение `Languages` — отдельная правка: профилей-языков
                    # (`en-US`) в шаблоне нет, есть только сам список, и из
                    # него Windows собирает HKU\.DEFAULT при загрузке.
                    deleted += mutate._clean_intl_languages(
                        root, path, layout_klid, True
                    )
                    deleted += mutate._clean_intl_profile(
                        root, path, layout_klid, True
                    )
                elif mode == "ctf":
                    mutate._clean_ctf_profiles(root, path, layout_klid, True)
                    deleted += mutate._clean_branch_recursive(
                        root, path, layout_klid, "preload", True
                    )
                # Лог по КАЖДОЙ ветке, включая нули. Молчание при «0»
                # и стоило инцидента: по журналу операция выглядела
                # успешной, а раскладка возвращалась после перезагрузки.
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
            "Профиль по умолчанию очищен: %s (всего значений: %d)",
            layout_klid,
            len(deleted),
        )
    else:
        logger.warning(
            "В шаблоне профиля по умолчанию не найдено значений %s - "
            "проверьте Control Panel\\International\\User Profile",
            layout_klid,
        )
    return deleted


def restore_preload(reg_file: Path) -> bool:
    """Вернуть ``Preload`` профиля по умолчанию из выгрузки.

    Импорт — merge, как и у пользовательской ветки: значения, появившиеся
    после удаления, сохраняются.

    ``False`` означает «шаблон не восстановлен» — в том числе если импорт
    прошёл, а записать hive в файл не удалось: сообщать об успехе при
    неизменённом шаблоне значило бы отправить пользователя проверять
    результат только после перезагрузки.
    """
    take_save_error()
    reason = is_blocked_reason()
    if reason:
        logger.info("Профиль по умолчанию не восстанавливается: %s", reason)
        return False
    if not reg_file.exists():
        logger.info("Нет выгрузки профиля по умолчанию: %s", reg_file)
        return False
    try:
        # save=True: импорт без сохранения в файл отменяется выгрузкой, и
        # откат шаблона получался фиктивным — вернулось бы всё, кроме шаблона.
        with mounted(save=True):
            result = _reg(["import", str(reg_file)], timeout=60)
    except DduError as exc:
        logger.error("Профиль по умолчанию не восстановлен: %s", exc)
        return False
    save_error = take_save_error()
    if result.returncode != 0:
        logger.error(
            "reg import профиля по умолчанию не выполнен: %s",
            result.stderr or result.returncode,
        )
        return False
    if save_error:
        logger.error(
            "Шаблон профиля по умолчанию импортирован, но НЕ записан в файл "
            "(%s) - откат не переживёт перезагрузку",
            save_error,
        )
        return False
    logger.info("Preload профиля по умолчанию восстановлен из %s", reg_file)
    return True
