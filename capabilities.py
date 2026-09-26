"""
capabilities.py — Права на разрушительные операции (FIX-29).

Зачем. До FIX-29 тесты могли дойти до живых разрушительных путей, и защита
от этого держалась на памяти разработчика: «не забудь подменить
``cleaner._run_cleanup_script``». Забыли дважды — у пользователя пропала
русская раскладка. ``CtfmonSuspender`` вообще останавливал ``ctfmon``
настоящим ``Stop-Process -Force``, а live-тесты по замыслу идут по живой
системе (см. комментарий в ``.github/workflows/ci-cd.yml``: sandbox
исказил бы plan/delete API и сломал бы assertions).

Поэтому песочницей это не решается: она меняет КУДА пишет реестр, но не
запрещает ПИСАТЬ, и ничего не знает про процессы и про WinRT-API списка
языков. Нужен отдельный механизм — права (capability).

Модель намеренно простая и ОТКАЗО-ЗАКРЫТАЯ (deny by default):

  * права по умолчанию НЕ выданы ни у кого — пустой набор;
  * ``require()`` стоит ВНУТРИ каждой разрушительной функции, а не в
    тесте. Забыть его нельзя, иначе функция останется без защиты;
  * выдаёт права только production-код (точка входа приложения или CLI);
  * тесты получают права только явной фикстурой ``granted``.

Почему не счётчик/токен, а простой набор. Разрушительные пути зовут
друг друга (``delete_layout`` -> ``_sync_language_list_via_powershell`` ->
``_run_cleanup_script``), и вложенные ``granted`` должны работать как
вложенные ``with``. Счётчик «уровней» с этим справляется, но даёт
ложное чувство защиты: у глубокого вызова право всё равно своё, а не
переданное сверху. Достаточно факта «у этого процесса есть право», а
кто именно его выдал — вопрос для отладки, а не для безопасности.

Отличие от песочницы принципиально: песочница — про ВЕТКУ реестра
(куда писать), capability — про ПРАВО (можно ли вообще). Они не заменяют
друг друга, поэтому песочница и остаётся: в песочнице те же разрушительные
функции безопасны, потому что пишут в изолированный ключ.
"""

import contextlib
import threading
from collections.abc import Iterator
from enum import Enum
from typing import Any


class Capability(str, Enum):
    """Отдельные виды разрушительного доступа.

    Их несколько не для красоты: ``LANGUAGE_LIST`` опаснее остальных
    (WinRT-API минует реестр, и песочница его не покрывает — см. FIX-25),
    а ``WELCOME_SYNC`` необратим в принципе (FIX-4). Право на одно не
    даёт права на другое: тесту, которому нужно подменить PowerShell, не
    нужно разрешение останавливать ctfmon.
    """

    #: Удаление/перезапись значений реестра (Preload, Substitutes, CTF, User Profile).
    REGISTRY_MUTATE = "registry_mutate"
    #: ``reg import`` — запись в реестр мимо winreg, через reg.exe.
    REG_IMPORT = "reg_import"
    #: WinRT-API списка языков: Set-WinUserLanguageList -Force ЗАМЕНЯЕТ
    #: список целиком, песочницей не изолируется.
    LANGUAGE_LIST = "language_list"
    #: Остановка/запуск ctfmon.exe и TextInputHost.exe (Stop-Process -Force).
    INPUT_SERVICES = "input_services"
    #: Политика облачной синхронизации языков (SettingSync\Groups\Language).
    CLOUD_SYNC_POLICY = "cloud_sync_policy"
    #: Copy-UserInternationalSettingsToSystem — переписывает HKU\.DEFAULT,
    #: откат невозможен.
    WELCOME_SYNC = "welcome_sync"


class CapabilityDeniedError(RuntimeError):
    """Разрушительная операция без выданного права."""


_lock = threading.Lock()
_granted: set[Capability] = set()


def grant(*caps: Capability) -> None:
    """Выдать права. Вызывает ТОЛЬКО production-код (входная точка)."""
    with _lock:
        _granted.update(caps)


def revoke(*caps: Capability) -> None:
    """Отозвать права."""
    with _lock:
        _granted.difference_update(caps)


def revoke_all() -> None:
    """Снять все права. Нужно тестам: состояние процесса общее для сессии."""
    with _lock:
        _granted.clear()


def is_granted(cap: Capability) -> bool:
    with _lock:
        return cap in _granted


def granted_set() -> set[Capability]:
    with _lock:
        return set(_granted)


def require(cap: Capability, where: Any = None) -> None:
    """Проверить право или упасть. Ставится в начале разрушительной функции.

    Parameters
    ----------
    cap
        Требуемое право.
    where
        Для сообщения об ошибке: обычно имя функции. Не обязателен, но
        делает падение в тесте самодостаточным — иначе пришлось бы искать
        в traceback, какой именно из десяти вызовов не имел права.
    """
    if is_granted(cap):
        return
    place = f"{where}: " if where else ""
    raise CapabilityDeniedError(
        f"{place}требуется право {cap.value!r}, а у процесса его нет. "
        "Права выдаёт только production-код в точке входа "
        "(main.main() / CLI) — тесты получают их фикстурой granted(). "
        "Если это тест, который должен работать с живой системой, "
        "добавьте granted(capabilities.Capability.%s)."
        % cap.name
    )


@contextlib.contextmanager
def granted(*caps: Capability) -> Iterator[None]:
    """Временно выдать права (только для тестов и внутренних нужд)."""
    missing = [c for c in caps if not is_granted(c)]
    grant(*caps)
    try:
        yield
    finally:
        # Отзываем только те, которых ДО контекста не было: иначе вложенный
        # granted() погасил бы право, выданное снаружи.
        revoke(*missing)


#: Права, достаточные для обычной работы приложения в реальном режиме.
#: Песочница их не отменяет: она меняет адреса, но операции те же.
APP_CAPABILITIES = frozenset(
    {
        Capability.REGISTRY_MUTATE,
        Capability.REG_IMPORT,
        Capability.LANGUAGE_LIST,
        Capability.INPUT_SERVICES,
        Capability.CLOUD_SYNC_POLICY,
    }
)
