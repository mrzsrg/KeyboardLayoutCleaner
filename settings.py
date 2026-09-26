"""
settings.py — политика облачной синхронизации языков (FIX-10, шаг 4).

Последний кусок ``cleaner.py``, который не относится ни к бэкапу, ни к
сопоставлению значений, ни к списку языков: политика синхронизации
языковых параметров с облаком (``SettingSync\\Groups\\Language\\Enabled``).

Почему это отдельный модуль, а не «просто ещё функции в cleaner»:
облачная синхронизация — единственная подсистема, которая меняет реестр
помимо самого удаления, и она обязана быть opt-in (FIX-2). Смешанная с
очисткой реестра, она визуально выглядит частью удаления, а это ровно то
заблуждение, из-за которого блокировка однажды применялась безусловно.

Здесь же была операция ``Copy-UserInternationalSettingsToSystem``
(``sync_welcome_screen_settings``) — «применить текущий список языков к
Экрану приветствия и профилю новых пользователей». Решение владельца
26.09.2026 (Problems.MD §6, п.1): операция УДАЛЕНА, а не оставлена
отдельной командой. Проверено на живых машинах — в ``HKU\\.DEFAULT`` лежит
ровно тот же набор раскладок, что и у текущего пользователя (Win11 build
26200 и Win10 build 19045), то есть чинить там нечего; на Windows 10
командлета не существует вовсе, и приложение сообщало об этом ложным
предупреждением о необратимости и голым ``FAILED``. Возвращать нельзя:
команда переписывает весь набор international settings в ``HKU\\.DEFAULT``,
а в бэкап входит оттуда только ``HKU\\.DEFAULT\\Keyboard Layout\\Preload`` —
откат невозможен в принципе.

Резолвер ветки sandbox-осведомлённый (FIX-2c) и обращается к общему
``mutate._sandbox_active`` через МОДУЛЬ, а не через ``from ... import``:
привязка именем скопировала бы значение, и подмена в тестах перестала бы
действовать.
"""

import logging
import winreg

import capabilities
import mutate
from config import _SANDBOX_ROOT

logger = logging.getLogger("layout_cleaner")

# ---------------------------------------------------------------------------
# Backup-only источник (FIX-2): состояние облачной синхронизации языков
# ---------------------------------------------------------------------------
# Ветка SettingSync\Groups\Language хранит флажок Enabled, которым управляют
# только disable/enable_language_sync(). Она сознательно НЕ входит в матрицу
# мутаций (AFFECTED_BRANCHES): «очистка совпавших значений» здесь не нужна,
# зато её исходное состояние должно попадать в .reg-бэкап — restore_backup
# выполняет reg import и вернёт синхронизацию в прежнее состояние.
_SETTINGSYNC_GROUPS_SUBKEY = (
    "Software\\Microsoft\\Windows\\CurrentVersion\\SettingSync\\Groups\\Language"
)



def _settingsync_groups_key_path() -> str:
    """Полный путь SettingSync\\Groups\\Language с учётом sandbox (FIX-2c).

    Без резолвера live-тесты и CLI ``--sandbox`` правили бы реальное
    значение ``Enabled`` в HKCU вместо изолированной sandbox-копии.
    """
    if mutate._sandbox_active():
        return _SANDBOX_ROOT + "\\" + _SETTINGSYNC_GROUPS_SUBKEY
    return _SETTINGSYNC_GROUPS_SUBKEY



def _settingsync_groups_paths() -> list[dict[str, str]]:
    """Backup-only источники для paths_to_backup: бэкап без мутаций."""
    return [{"root": "HKCU", "subkey": _settingsync_groups_key_path()}]



def _is_language_sync_blocked() -> bool:
    """
    Проверить, заблокирована ли уже синхронизация языков с облаком.

    Returns
    -------
    bool
        True если синхронизация уже отключена, False если не определилось
        или включено.
    """
    key_path = _settingsync_groups_key_path()
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



def is_language_sync_blocked() -> bool:
    """Публичная обёртка: заблокирована ли синхронизация языков с облаком.

    Возвращает ``True`` если значение ``Enabled=0`` существует в
    ``SettingSync\\Groups\\Language``, ``False`` если ключ/значение
    отсутствуют или значение отличается от ``0``.
    """
    return _is_language_sync_blocked()



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
    # FIX-29: запись в живой HKCU пользователя.
    capabilities.require(
        capabilities.Capability.CLOUD_SYNC_POLICY, "disable_language_sync"
    )
    key_path = _settingsync_groups_key_path()
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



def enable_language_sync() -> tuple[bool, str]:
    """
    Разблокировать синхронизацию языковых параметров с облаком Microsoft.

    Обратная операция к :func:`disable_language_sync`: восстанавливает
    ``SettingSync\\Groups\\Language\\Enabled = 1`` (REG_DWORD).

    Returns
    -------
    tuple[bool, str]
        (успех, детализированное описание результата/ошибки)
    """
    # FIX-29: см. disable_language_sync.
    capabilities.require(
        capabilities.Capability.CLOUD_SYNC_POLICY, "enable_language_sync"
    )
    key_path = _settingsync_groups_key_path()
    # Проверяем текущее состояние перед записью
    if not _is_language_sync_blocked():
        logger.info("Синхронизация языков не заблокирована — пропускаем запись")
        return True, "Уже включена"

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            # 1 = Включить синхронизацию языков (значение по умолчанию)
            winreg.SetValueEx(key, "Enabled", 0, winreg.REG_DWORD, 1)
        logger.info("Синхронизация языковых параметров с облаком восстановлена.")
        return True, "Включена (Enabled = 1)"
    except PermissionError as exc:
        # Ветка заблокирована системным администратором через групповую политику
        logger.warning(
            "Не удалось включить синхронизацию языков: доступ запрещён "
            "(возможно, заблокировано групповой политикой): %s",
            exc,
        )
        return False, "Доступ запрещён (групповая политика / права администратора)"
    except OSError as exc:
        logger.warning("Не удалось включить синхронизацию языков: %s", exc)
        return False, f"Ошибка: {exc}"

