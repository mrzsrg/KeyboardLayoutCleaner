r"""
test_sandbox.py — Живые (live) тесты по «Плану тестирования и безопасности».

БЕЗОПАСНОСТЬ:
  Все операции записи/удаления выполняются ТОЛЬКО в тестовом ключе
  HKCU\Software\KeyboardCleanerTest (sandbox-ключ из плана тестирования).

  Реальные ветки (Keyboard Layout, CTF, User Profile, HKU\.DEFAULT):
    - читаются только в режиме read-only (сканер, reg export бэкапа);
    - НЕ модифицируются: delete_layout вызывается с KLID "d001dead",
      которого гарантированно нет в системе (нет совпадений — нет
      удалений), а PowerShell-синхронизация при отсутствии совпадений
      возвращает NOCHANGE и НЕ вызывает Set-WinUserLanguageList.

  Реальное удаление фантомной раскладки из живого профиля выполняйте
  только в виртуальной машине (Windows Sandbox / Hyper-V / VirtualBox).

Маркировка тестов:
  @pytest.mark.gui — тесты GUI (требуют event loop, могут зависбать в CI)
  @pytest.mark.live — тесты с реальным реестром (sandbox)
"""

import contextlib
import json
import os
import re
import subprocess
import tempfile
import unittest
import winreg
from pathlib import Path
from unittest import mock

import pytest

import cleaner
import langlist
import scanner

TEST_ROOT = "Software\\KeyboardCleanerTest"
PHANTOM_KLID = "d001dead"  # заведомо несуществующая раскладка
KLID_RE = re.compile(r"^[0-9a-fA-F]{8}$")


def _create_test_tree() -> None:
    """Создать sandbox-дерево с фантомной раскладкой d001dead."""
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, TEST_ROOT + "\\Preload", 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, "1", 0, winreg.REG_SZ, "00000409")
        winreg.SetValueEx(key, "2", 0, winreg.REG_SZ, PHANTOM_KLID)
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, TEST_ROOT + "\\Substitutes", 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, PHANTOM_KLID, 0, winreg.REG_SZ, "00000409")


def _get_test_value(subpath: str, name: str):
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, TEST_ROOT + "\\" + subpath, 0, winreg.KEY_READ
        ) as key:
            return winreg.QueryValueEx(key, name)[0]
    except FileNotFoundError:
        return None


def _key_exists(subpath: str) -> bool:
    """Существует ли подключ в sandbox-корне."""
    try:
        winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, TEST_ROOT + "\\" + subpath, 0, winreg.KEY_READ
        )
        return True
    except FileNotFoundError:
        return False


def _delete_test_tree() -> None:
    """Рекурсивно удалить sandbox-ключ (в стандартном winreg нет DeleteTree)."""

    def _delete_tree_recursive(root, path):
        try:
            with winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS) as key:
                while True:
                    try:
                        sub = winreg.EnumKey(key, 0)
                    except OSError:
                        break
                    _delete_tree_recursive(root, path + "\\" + sub)
            winreg.DeleteKey(root, path)
        except FileNotFoundError:
            pass

    _delete_tree_recursive(winreg.HKEY_CURRENT_USER, TEST_ROOT)


@contextlib.contextmanager
def _no_live_language_list(detail: str = "NOCHANGE"):
    """FIX-28: подмена PS-очистки — тест не трогает список языков.

    `layout_cleaner_cleanup.ps1` внутри вызывает Set-WinUserLanguageList
    -Force, который ЗАМЕНЯЕТ список языков пользователя целиком. В песочнице
    это прикрывает флаг -Sandbox (FIX-25), но живые тесты (`@pytest.mark.live`)
    в песочницу не входят — и уходили в настоящий PowerShell по живой
    системе. `detail` повторяет то, что вернул бы скрипт: тесты проверяют
    структуру плана, а не живой вывод PowerShell.

    Подменяются ОБЕ ссылки: `cleaner._run_cleanup_script` (план удаления) и
    `langlist._run_cleanup_script` (её же вызывает
    `_sync_language_list_via_powershell` — по локальной ссылке модуля, минуя
    реэкспорт в `cleaner`).
    """
    with (
        mock.patch.object(
            cleaner,
            "_run_cleanup_script",
            return_value=(True, detail, {"tips_removed": [], "languages_removed": []}),
        ),
        mock.patch.object(
            langlist,
            "_run_cleanup_script",
            return_value=(True, detail, {"tips_removed": [], "languages_removed": []}),
        ),
    ):
        yield


class SandboxTestCase(unittest.TestCase):
    """Базовый класс: гарантированная очистка sandbox-ключа."""

    def setUp(self) -> None:
        _delete_test_tree()

    def tearDown(self) -> None:
        _delete_test_tree()


@pytest.mark.live
class TestScannerDryRun(SandboxTestCase):
    """Чек-лист п.1: сканер на живой системе (только чтение)."""

    def test_scan_returns_valid_structure(self) -> None:
        layouts = scanner.scan_keyboard_layouts()
        assert isinstance(layouts, dict)
        for klid, locations in layouts.items():
            assert re.search(KLID_RE, klid)
            assert isinstance(locations, list)
            for loc in locations:
                assert "path" in loc
                assert "value" in loc
                assert loc["path"]

    def test_hex_to_name_mapping(self) -> None:
        name = scanner.get_layout_name("00000409")
        assert "Layout (" not in name  # есть в HKLM на любой Windows
        assert scanner.get_layout_name(PHANTOM_KLID) == "Layout (" + PHANTOM_KLID + ")"

    def test_missing_branches_do_not_crash(self) -> None:
        missing = TEST_ROOT + "\\Definitely\\Missing\\Branch"
        assert scanner._reg_get_string(winreg.HKEY_CURRENT_USER, missing) == {}
        assert scanner._get_preload_keys(winreg.HKEY_CURRENT_USER, missing) == {}

    def test_powershell_language_list_parsed(self) -> None:
        entries = scanner._get_language_list_from_powershell()
        for entry in entries:
            assert re.search(KLID_RE, entry["KeyboardLayoutId"])
            assert entry["LanguageTag"]


@pytest.mark.live
class TestBackupAndRestore(SandboxTestCase):
    """Чек-лист п.2: структура .reg бэкапа и восстановление (sandbox-ключ)."""

    def test_backup_structure_and_restore_roundtrip(self) -> None:
        _create_test_tree()
        with tempfile.TemporaryDirectory() as tmp_dir:
            backup_path = cleaner.backup_registry(
                [
                    {"root": "HKCU", "subkey": TEST_ROOT + "\\Preload"},
                    {"root": "HKCU", "subkey": TEST_ROOT + "\\Substitutes"},
                ],
                backup_dir=tmp_dir,
            )
            file = Path(backup_path)
            assert file.exists()
            raw = file.read_bytes()
            assert raw.startswith(b"\xff\xfe"), "нет BOM UTF-16 LE"
            text = file.read_text(encoding="utf-16")
            headers = [
                ln
                for ln in text.splitlines()
                if ln.startswith("Windows Registry Editor Version")
            ]
            assert len(headers) == 1
            # Регрессия: раньше reg export перезаписывал файл, и в бэкапе
            # оставалась только последняя ветка
            assert "[HKEY_CURRENT_USER\\" + TEST_ROOT + "\\Preload]" in text
            assert "[HKEY_CURRENT_USER\\" + TEST_ROOT + "\\Substitutes]" in text

            # Восстановление: сносим дерево и импортируем бэкап обратно
            _delete_test_tree()
            assert _get_test_value("Preload", "2") is None
            result = subprocess.run(
                ["reg", "import", backup_path], capture_output=True, timeout=30
            )
            assert result.returncode == 0
            assert _get_test_value("Preload", "2") == PHANTOM_KLID
            assert _get_test_value("Preload", "1") == "00000409"
            assert _get_test_value("Substitutes", PHANTOM_KLID) == "00000409"

    def test_backup_of_missing_branch_does_not_crash(self) -> None:
        """Ветка не существует → BackupError (файл-заглушки больше нет):
        delete_layout обязан отменить удаление, краша нет."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            with pytest.raises(cleaner.BackupError):
                cleaner.backup_registry(
                    [{"root": "HKCU", "subkey": TEST_ROOT + "\\Missing"}],
                    backup_dir=tmp_dir,
                )
            assert list(Path(tmp_dir).glob("*.reg")) == []

    def test_restore_via_cleaner_function_roundtrip(self) -> None:
        """Восстановление через restore_registry_backup (без админа, sandbox).

        Полный круг: бэкап sandbox-ветки → удаление значения →
        импорт .reg средствами приложения → значение вернулось.
        """
        _create_test_tree()
        with tempfile.TemporaryDirectory() as tmp_dir:
            backup_path = cleaner.backup_registry(
                [{"root": "HKCU", "subkey": TEST_ROOT + "\\Substitutes"}],
                backup_dir=tmp_dir,
            )
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                TEST_ROOT + "\\Substitutes",
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.DeleteValue(key, PHANTOM_KLID)
            assert _get_test_value("Substitutes", PHANTOM_KLID) is None

            result = cleaner.restore_registry_backup(backup_path)
            assert result["ok"], result.get("detail")
            assert not result["elevated"]
            assert not result["needs_admin"]
            assert _get_test_value("Substitutes", PHANTOM_KLID) == "00000409"

    def test_list_backups_finds_created_backup(self) -> None:
        """list_backups видит бэкап, созданный backup_registry в sandbox."""
        _create_test_tree()
        with tempfile.TemporaryDirectory() as tmp_dir:
            cleaner.backup_registry(
                [{"root": "HKCU", "subkey": TEST_ROOT + "\\Preload"}],
                backup_dir=tmp_dir,
            )
            items = cleaner.list_backups(tmp_dir)
            assert len(items) == 1
            assert not items[0]["has_hku"]
            assert items[0]["json_path"] == ""
            assert (
                "[HKEY_CURRENT_USER\\" + TEST_ROOT + "\\Preload]"
                in items[0]["sections"]
            )


@pytest.mark.live
class TestIntlProfileCleanup(SandboxTestCase):
    """Уровень 1c: User Profile — точечная очистка БЕЗ удаления языка.

    Мини-копия структуры User Profile в sandbox-ключе: Languages
    (REG_MULTI_SZ с тегами) + подключи-профили, названные тегами.
    Ключ в Languages = язык АКТИВЕН: профиль нельзя удалять целиком,
    чистится только привязка раскладки (InputMethodOverride и пр.).
    """

    def _build_mini_profile(self, active: bool = True) -> str:
        profile = "IntlProfile"
        langs = ["en-US", "en-GB"] if active else ["en-US"]
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            TEST_ROOT + "\\" + profile,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "Languages", 0, winreg.REG_MULTI_SZ, langs)
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            TEST_ROOT + "\\" + profile + "\\en-GB",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(
                key, "InputMethodOverride", 0, winreg.REG_SZ, "0809:00000809"
            )
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            TEST_ROOT + "\\" + profile + "\\en-US",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(
                key, "InputMethodOverride", 0, winreg.REG_SZ, "0409:00000409"
            )
        return profile

    def test_delete_mode_removes_only_target_klid(self) -> None:
        """Активный язык (en-GB в Languages): НЕ удаляем профиль целиком,
        а чистим ТОЛЬКО привязку раскладки 00000809 внутри него."""
        profile = self._build_mini_profile(active=True)
        deleted = cleaner._clean_intl_profile(
            winreg.HKEY_CURRENT_USER, TEST_ROOT + "\\" + profile, "00000809"
        )
        # Привязка KLID удалена, про запас сообщения отличаются
        assert any(d.startswith("en-GB\\") for d in deleted)
        # Профиль языка СОХРАНЁН (язык активен) и Languages не тронуты
        assert _key_exists(profile + "\\en-GB")
        assert _key_exists(profile + "\\en-US")
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            TEST_ROOT + "\\" + profile,
            0,
            winreg.KEY_READ,
        ) as key:
            langs = winreg.QueryValueEx(key, "Languages")[0]
        assert list(langs) == ["en-US", "en-GB"]

    def test_delete_removes_empty_inactive_profile(self) -> None:
        """Если en-GB НЕ активен (нет в Languages) и после точечной
        очистки профиль опустел — безопасно удаляется пустой профиль."""
        profile = self._build_mini_profile(active=False)
        cleaner._clean_intl_profile(
            winreg.HKEY_CURRENT_USER, TEST_ROOT + "\\" + profile, "00000809"
        )
        assert not _key_exists(profile + "\\en-GB")
        assert _key_exists(profile + "\\en-US")

    def test_dry_run_does_not_modify(self) -> None:
        profile = self._build_mini_profile()
        deleted = cleaner._clean_intl_profile(
            winreg.HKEY_CURRENT_USER,
            TEST_ROOT + "\\" + profile,
            "00000809",
            delete=False,
        )
        assert deleted
        assert _key_exists(profile + "\\en-GB")
        assert _key_exists(profile + "\\en-US")

    def test_unknown_klid_is_noop(self) -> None:
        profile = self._build_mini_profile()
        deleted = cleaner._clean_intl_profile(
            winreg.HKEY_CURRENT_USER, TEST_ROOT + "\\" + profile, "d001dead"
        )
        assert deleted == []
        assert _key_exists(profile + "\\en-GB")
        assert _key_exists(profile + "\\en-US")


@pytest.mark.live
class TestScannerDeepScan(SandboxTestCase):
    """Рекурсивный скан CTF/User Profile + нормализация регистра Substitutes."""

    def test_recursive_scan_finds_nested_klid(self) -> None:
        # Значение в глубоком подключе (имитация CTF SortOrder\AssemblyItem)
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            TEST_ROOT + "\\ScanRec\\A\\B\\C",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "KeyboardLayout", 0, winreg.REG_SZ, "d001dead")
        found = scanner._scan_branch_recursive(
            winreg.HKEY_CURRENT_USER, TEST_ROOT + "\\ScanRec"
        )
        assert ("A\\B\\C\\KeyboardLayout", "d001dead") in found

    def test_recursive_scan_tip_format(self) -> None:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            TEST_ROOT + "\\ScanRec2\\Sub",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(
                key, "InputMethodOverride", 0, winreg.REG_SZ, "0809:00000809"
            )
        found = scanner._scan_branch_recursive(
            winreg.HKEY_CURRENT_USER, TEST_ROOT + "\\ScanRec2"
        )
        assert ("Sub\\InputMethodOverride", "00000809") in found

    def test_recursive_scan_missing_branch(self) -> None:
        assert (
            scanner._scan_branch_recursive(
                winreg.HKEY_CURRENT_USER, TEST_ROOT + "\\Missing"
            )
            == []
        )

    def test_substitutes_case_normalized(self) -> None:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            TEST_ROOT + "\\SubsCase",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "D001DEAD", 0, winreg.REG_SZ, "00000409")
        subs = scanner._scan_substitutes(
            winreg.HKEY_CURRENT_USER, TEST_ROOT + "\\SubsCase"
        )
        assert subs.get("d001dead", {}).get("target") == "00000409"


@pytest.mark.live
class TestPermissionsSafety(SandboxTestCase):
    """Чек-лист п.3: поведение без админ-прав (без крашей)."""

    def test_is_admin_returns_bool(self) -> None:
        assert isinstance(cleaner.is_admin(), bool)

    def test_missing_or_denied_keys_are_not_fatal(self) -> None:
        assert (
            cleaner._clean_preload_keys(
                winreg.HKEY_CURRENT_USER, TEST_ROOT + "\\Missing", PHANTOM_KLID
            )
            == []
        )
        # HKU\.DEFAULT: без админ-прав — PermissionError, с правами — просто
        # нет совпадений. Оба пути обязаны вернуть [] без исключений.
        assert (
            cleaner._clean_preload_keys(
                winreg.HKEY_USERS, ".DEFAULT\\Keyboard Layout\\Preload", PHANTOM_KLID
            )
            == []
        )


@pytest.mark.live
class TestPhantomDeletionSimulation(SandboxTestCase):
    """Чек-лист п.4: симуляция удаления фантома (в sandbox-ключе)."""

    def test_phantom_removed_from_test_key(self) -> None:
        _create_test_tree()
        deleted = cleaner._clean_preload_keys(
            winreg.HKEY_CURRENT_USER, TEST_ROOT + "\\Preload", PHANTOM_KLID
        )
        assert deleted == ["2"]
        assert _get_test_value("Preload", "2") is None
        assert _get_test_value("Preload", "1") == "00000409"

        deleted_subst = cleaner._clean_substitutes_keys(
            winreg.HKEY_CURRENT_USER, TEST_ROOT + "\\Substitutes", PHANTOM_KLID
        )
        assert deleted_subst == [PHANTOM_KLID]
        assert _get_test_value("Substitutes", PHANTOM_KLID) is None

    def test_delete_layout_end_to_end_noop_on_real_system(self) -> None:
        """Полный конвейер delete_layout с фантомным KLID.

        В реальных ветках совпадений нет => ничего не удаляется; бэкап
        создаётся (read-only); PowerShell-синхронизация даёт NOCHANGE
        (Set-WinUserLanguageList НЕ вызывается, профиль не меняется).

        FIX-28: тест по замыслу «живой», но службы ввода он обязан оставить
        в покое — иначе каждый прогон pytest перезапускает TSF у реального
        пользователя и сбивает переключатель раскладок. Класс наследует
        unittest.TestCase, поэтому фикстуры pytest здесь недоступны и
        подмена делается через mock.patch (как в TestInputServicesRescue).
        """
        created_backups = []
        try:
            with (
                mock.patch.object(cleaner, "_stop_ctfmon", return_value=True),
                mock.patch.object(cleaner, "_start_ctfmon", return_value=True),
                _no_live_language_list(),
            ):
                report = cleaner.delete_layout(PHANTOM_KLID)
            if report.get("backup_path"):
                created_backups.append(report["backup_path"])
            if report.get("langlist_backup_path"):
                created_backups.append(report["langlist_backup_path"])
            # FIX-6: sidecar-манифест лежит рядом с .reg и без явного удаления
            # остаётся в backups/ навсегда (именно так там появился один
            # «сиротский» .manifest.json). Убираем и его.
            if report.get("manifest_path"):
                created_backups.append(report["manifest_path"])
            assert report["backup_path"]
            assert Path(report["backup_path"]).exists()
            assert not report["success"]  # фантом в системе отсутствует
            assert report["hkcu_preload_deleted"] == []
            assert report["hkcu_substitutes_deleted"] == []
            assert report["hkcu_ctf_deleted"] == []
            assert report["hkcu_intl_deleted"] == []
            assert report["hku_default_deleted"] == []
            # Синхронизация отработала без изменения профиля (NOCHANGE)
            assert report["power_sync"]
            assert report["power_sync_detail"] == "NOCHANGE"
            assert "langlist_backup_path" in report
        finally:
            for p in created_backups:
                with contextlib.suppress(OSError):
                    os.remove(p)


@pytest.mark.live
class TestDryRunAndBackupLive(SandboxTestCase):
    """Dry-run и JSON-бэкап списка языков на живой системе (read-only)."""

    def test_plan_layout_removal_phantom(self) -> None:
        with _no_live_language_list():
            plan = cleaner.plan_layout_removal(PHANTOM_KLID)
        assert plan["klid"] == PHANTOM_KLID
        assert "HKCU\\Keyboard Layout\\Preload" in plan["branches"]
        assert len(plan["branches"]) == 6
        # PowerShell-анализ доступен; фантома в списке языков нет
        assert plan["ps"]["available"]
        assert plan["ps"]["detail"] == "NOCHANGE"
        assert not plan["ps"]["tips_removed"]

    def test_plan_layout_removal_ignores_sandbox_tree(self) -> None:
        """План затрагивает только реальные ветки: sandbox-ключ не попадает."""
        _create_test_tree()
        with _no_live_language_list():
            plan = cleaner.plan_layout_removal(PHANTOM_KLID)
        preload = plan["branches"]["HKCU\\Keyboard Layout\\Preload"]["values"]
        assert preload == []
        assert plan["branches"].get("HKCU\\Software\\KeyboardCleanerTest") is None

    def test_backup_language_list_writes_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake = Path(tmp_dir) / "backup.reg"
            fake.write_bytes(b"\xff\xfe")
            assert fake.read_bytes() == bytes([0xFF, 0xFE])
            json_path = cleaner._backup_language_list(fake)
            assert json_path
            data = json.loads(Path(json_path).read_text(encoding="utf-8"))
            assert isinstance(data, list)


@pytest.mark.live
class TestCtfProfileCleanup(SandboxTestCase):
    """TSF-профили CTF: удаление ЦЕЛИКОМ, decimal→hex, глубина 5, dry-run."""

    PHANTOM_GUID = "{34745C63-B2F0-4784-8B67-5E12C8701A31}"
    LEGIT_GUID = "{11111111-2222-3333-4444-555555555555}"
    DEEP_GUID = "{99999999-8888-7777-6666-555555555555}"

    def _build_ctf_sim(self) -> str:
        base = TEST_ROOT + "\\CTFsim"
        # Фантомный профиль: CTF хранит KeyboardLayout как DECIMAL HKL
        # 68748313 == 0x04190419 (язык 0419 + раскладка 00000419)
        phantom = base + "\\Assemblies\\0x00000419\\" + self.PHANTOM_GUID
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, phantom, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "KeyboardLayout", 0, winreg.REG_DWORD, 68748313)
        # Легитимный сосед того же языка (0x409 → decimal 1033)
        legit = base + "\\Assemblies\\0x00000419\\" + self.LEGIT_GUID
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, legit, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "KeyboardLayout", 0, winreg.REG_DWORD, 0x00000409)
        # Запись SortOrder на ГЛУБИНЕ 5: ...\{GUID}\00000000\KLID
        deep = (
            base
            + "\\SortOrder\\AssemblyItem\\0x00000419\\"
            + self.DEEP_GUID
            + "\\00000000"
        )
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, deep, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "KLID", 0, winreg.REG_SZ, "04190419")
        return base

    def test_scanner_normalizes_decimal_hkl_and_reaches_depth5(self) -> None:
        base = self._build_ctf_sim()
        found = dict(scanner._scan_branch_recursive(winreg.HKEY_CURRENT_USER, base))
        # decimal KeyboardLayout приведён к каноническому hex-KLID
        assert (
            found.get(f"Assemblies\\0x00000419\\{self.PHANTOM_GUID}\\KeyboardLayout")
            == "04190419"
        )
        # значение на глубине 5 (...\{GUID}\00000000\KLID) теперь видно
        deep_path = (
            f"SortOrder\\AssemblyItem\\0x00000419\\{self.DEEP_GUID}\\00000000\\KLID"
        )
        assert deep_path in found
        assert found[deep_path] == "04190419"

    def test_profile_keys_deleted_entirely(self) -> None:
        base = self._build_ctf_sim()
        deleted = cleaner._clean_ctf_profiles(
            winreg.HKEY_CURRENT_USER, base, "04190419"
        )
        profiles = [d for d in deleted if "профиль TSF целиком" in d]
        assert len(profiles) == 2
        # фантомный {GUID} удалён ЦЕЛИКОМ (вместе с подключами)
        assert not _key_exists("CTFsim\\Assemblies\\0x00000419\\" + self.PHANTOM_GUID)
        assert not _key_exists(
            "CTFsim\\SortOrder\\AssemblyItem\\0x00000419\\" + self.DEEP_GUID
        )
        # легитимный сосед (KeyboardLayout=0x409) не тронут
        assert _key_exists("CTFsim\\Assemblies\\0x00000419\\" + self.LEGIT_GUID)
        # опустевшие родители удалены, а родитель с «легитимным» профилем — нет
        assert not _key_exists("CTFsim\\SortOrder\\AssemblyItem\\0x00000419")
        assert _key_exists("CTFsim\\Assemblies\\0x00000419")

    def test_dry_run_deletes_nothing(self) -> None:
        base = self._build_ctf_sim()
        found = cleaner._clean_ctf_profiles(
            winreg.HKEY_CURRENT_USER, base, "04190419", delete=False
        )
        assert len([d for d in found if "профиль TSF целиком" in d]) == 2
        assert _key_exists("CTFsim\\Assemblies\\0x00000419\\" + self.PHANTOM_GUID)
        assert _key_exists(
            "CTFsim\\SortOrder\\AssemblyItem\\0x00000419\\"
            + self.DEEP_GUID
            + "\\00000000"
        )


def _raise_oserror():
    """Заглушка конца перечисления подключей (winreg.EnumKey кидает OSError)."""
    raise OSError("no more keys")


class TestSwitcherConsistency:
    """FIX-26: сверка списка языков с панелью переключения.

    Симптом: язык виден в Параметрах языка («установлен»), но отсутствует
    в переключателе, и помогает только перезагрузка. Обе стороны читаются
    из РАЗНЫХ хранилищ, поэтому расхождение возможно и раньше было
    безымянным.
    """

    @staticmethod
    def _patch(monkeypatch, list_klids, preload_klids, ctf_names):
        monkeypatch.setattr(
            scanner,
            "_get_language_list_from_powershell",
            lambda: [
                {"LanguageTag": "xx", "KeyboardLayoutId": k} for k in list_klids
            ],
        )
        monkeypatch.setattr(
            scanner,
            "_get_preload_keys",
            lambda root, path: {k: k for k in preload_klids},
        )

        class _Key:
            def __init__(self, names):
                self._names = names
                self._idx = 0

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(
            scanner.winreg, "OpenKey", lambda *a, **k: _Key(ctf_names)
        )
        # EnumKey — функция уровня модуля winreg, а не метод ключа, поэтому
        # подменяем её отдельно, а не через объект-ключ.
        monkeypatch.setattr(
            scanner.winreg,
            "EnumKey",
            lambda key, idx: (
                ctf_names[idx] if idx < len(ctf_names) else _raise_oserror()
            ),
        )

    def test_consistent_system_reports_nothing(self, monkeypatch):
        self._patch(
            monkeypatch,
            list_klids=["00000419", "00000409"],
            preload_klids=["00000419", "00000409"],
            ctf_names=["0x00000419", "0x00000409"],
        )
        report = scanner.check_switcher_consistency()
        assert report["tips_without_preload"] == []
        assert report["preload_without_tip"] == []
        assert report["ctf_missing"] == []
        assert scanner.is_switcher_consistent(report) is True

    def test_tip_in_list_but_absent_from_preload(self, monkeypatch):
        """Язык установлен, но переключатель его не покажет."""
        self._patch(
            monkeypatch,
            list_klids=["00000419", "00000409"],
            preload_klids=["00000419"],
            ctf_names=["0x00000419"],
        )
        report = scanner.check_switcher_consistency()
        assert report["tips_without_preload"] == ["00000409"]
        assert scanner.is_switcher_consistent(report) is False

    def test_stale_preload_entry(self, monkeypatch):
        """Остаток удалённой раскладки в Preload."""
        self._patch(
            monkeypatch,
            list_klids=["00000419"],
            preload_klids=["00000419", "d001dead"],
            ctf_names=["0x00000419"],
        )
        report = scanner.check_switcher_consistency()
        assert report["preload_without_tip"] == ["d001dead"]
        assert scanner.is_switcher_consistent(report) is False

    def test_ctf_missing_is_reported_but_not_fatal(self, monkeypatch):
        """Нет ветки CTF — отмечаем, но операцию не объявляем провалом.

        CTF пересобирает система при входе; считать это поломкой значило бы
        пугать пользователя там, где Windows и так всё починит.
        """
        self._patch(
            monkeypatch,
            list_klids=["00000419", "00000409"],
            preload_klids=["00000419", "00000409"],
            ctf_names=["0x00000419"],
        )
        report = scanner.check_switcher_consistency()
        # FIX-27: полный KLID, а не «голый» LANGID «0409» — значение уходит
        # в текст диалога рядом с tips_without_preload/preload_without_tip,
        # где все остальные элементы записаны как 8-HEX KLID.
        assert report["ctf_missing"] == ["00000409"]
        # Переключатель при этом согласован: Preload в порядке.
        assert scanner.is_switcher_consistent(report) is True

    def test_ctf_missing_uses_same_format_as_other_fields(self, monkeypatch):
        """Все три поля отчёта — в одном формате KLID (FIX-27)."""
        self._patch(
            monkeypatch,
            list_klids=["00000419", "00000409"],
            preload_klids=["00000419", "00000409"],
            ctf_names=["0x00000419"],
        )
        report = scanner.check_switcher_consistency()
        for field, values in report.items():
            for value in values:
                assert scanner._KLID_RE.match(value), f"{field}: {value} не KLID"

    def test_malformed_ctf_names_ignored(self, monkeypatch):
        self._patch(
            monkeypatch,
            list_klids=["00000419"],
            preload_klids=["00000419"],
            ctf_names=["0x00000419", "junk", "0xZZZZ"],
        )
        report = scanner.check_switcher_consistency()
        assert report["ctf_missing"] == []

    def test_empty_and_missing_reports_are_consistent(self):
        assert scanner.is_switcher_consistent(None) is True
        assert scanner.is_switcher_consistent({}) is True
        assert scanner.is_switcher_consistent({"tips_without_preload": []}) is True
        assert (
            scanner.is_switcher_consistent({"preload_without_tip": ["00000409"]})
            is False
        )


class TestLanguageListSandboxIsolation:
    """FIX-25: песочница изолирует реестр, но НЕ WinRT-API списка языков.

    Set-WinUserLanguageList минует реестр, поэтому песочница его не
    изолирует. Без явного запрета sandbox-тест или CLI --sandbox
    переустановили бы РЕАЛЬНЫЙ список языков пользователя — это и есть
    причина, по которой флаги -Sandbox добавлены во все три PS-скрипта.
    """

    @staticmethod
    def _cmd(monkeypatch, sandbox: bool):
        import langlist

        captured: dict = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = list(cmd)
            return mock.Mock(returncode=0, stdout="SUCCESS", stderr="")

        monkeypatch.setattr(langlist, "run_hidden", fake_run)
        monkeypatch.setattr(langlist, "is_sandbox_enabled", lambda: sandbox)
        return captured

    def test_restore_passes_sandbox_flag(self, tmp_path, monkeypatch):
        import langlist

        jp = tmp_path / "l.json"
        jp.write_text(
            '[{"LanguageTag":"ru","InputMethodTips":["0419:00000419"]}]',
            encoding="utf-8",
        )
        captured = self._cmd(monkeypatch, True)
        langlist.restore_language_list(jp)
        assert "-Sandbox" in captured["cmd"]
        # Флаг обязан идти ПОСЛЕ -JsonPath, иначе PS1 его не увидит.
        cmd = captured["cmd"]
        assert cmd.index("-Sandbox") > cmd.index("-JsonPath")

    def test_restore_without_sandbox_has_no_flag(self, tmp_path, monkeypatch):
        import langlist

        jp = tmp_path / "l.json"
        jp.write_text(
            '[{"LanguageTag":"ru","InputMethodTips":["0419:00000419"]}]',
            encoding="utf-8",
        )
        captured = self._cmd(monkeypatch, False)
        langlist.restore_language_list(jp)
        assert "-Sandbox" not in captured["cmd"]

    def test_cleanup_passes_sandbox_flag(self, monkeypatch):
        import langlist

        captured = self._cmd(monkeypatch, True)
        langlist._run_cleanup_script("d001dead", apply=True)
        assert "-Sandbox" in captured["cmd"]

    def test_sync_passes_sandbox_flag(self, monkeypatch):
        import langlist

        captured = self._cmd(monkeypatch, True)
        langlist._sync_language_list_via_powershell()
        assert "-Sandbox" in captured["cmd"]

    @pytest.mark.parametrize(
        "script",
        [
            "layout_cleaner_restore.ps1",
            "layout_cleaner_cleanup.ps1",
            "layout_cleaner_sync.ps1",
        ],
    )
    def test_every_language_script_supports_sandbox(self, script):
        """Каждый PS-скрипт списка языков обязан принимать -Sandbox.

        Скрипт без флага упал бы с ошибкой параметров, и песочница
        «сломалась бы» громче, чем просто не сделала работу.
        """
        import langlist

        text = (langlist._PS_DIR / script).read_text(encoding="utf-8")
        assert "[switch]$Sandbox" in text
        assert "SANDBOX_SKIPPED" in text


class TestCtfmonSandboxIsolation:
    """FIX-28: песочница обязана щадить ЖИВЫЕ процессы ввода.

    Реестр песочница изолирует (_SANDBOX_ROOT), WinRT-API заблокированы
    флагом -Sandbox (FIX-25). А вот остановка ctfmon — единственное
    действие приложения, которое мимо песочницы попадало прямо в процессы
    ТЕКУЩЕГО пользователя: sandbox-тесты вызывают delete_layout, а тот
    оборачивает всю работу в CtfmonSuspender.

    Симптом, который это давало на живой машине: каждый полный прогон
    pytest убивал ctfmon.exe/TextInputHost.exe, Windows пересобирал кеш TSF,
    и при расхождении Preload / User Profile / CTF\\Assemblies раскладка
    пропадала из переключателя — ровно то, что пользователь наблюдал
    во время тестов. Реестр и WinRT при этом были изолированы, поэтому
    виновник не попадал ни в один из прежних барьеров.
    """

    @staticmethod
    def _spy(monkeypatch):
        """Подменить управление ctfmon списском вызовов."""
        calls: list[str] = []

        def _stop() -> bool:
            calls.append("stop")
            return True

        def _start() -> bool:
            calls.append("start")
            return True

        monkeypatch.setattr(cleaner, "_stop_ctfmon", _stop)
        monkeypatch.setattr(cleaner, "_start_ctfmon", _start)
        return calls

    def test_sandbox_does_not_stop_live_ctfmon(self, monkeypatch):
        calls = self._spy(monkeypatch)
        monkeypatch.setattr(cleaner, "is_sandbox_enabled", lambda: True)
        with cleaner.CtfmonSuspender() as suspender:
            assert suspender.skipped is True
        assert calls == [], f"песочница тронула живые процессы ввода: {calls}"

    def test_sandbox_does_not_restart_ctfmon_either(self, monkeypatch):
        """__exit__ обязан вернуться ДО _start_ctfmon, а не после."""
        calls = self._spy(monkeypatch)
        monkeypatch.setattr(cleaner, "is_sandbox_enabled", lambda: True)
        with cleaner.CtfmonSuspender() as suspender:
            pass
        assert suspender.started is None
        assert calls == []

    def test_sandbox_exit_skips_start_even_on_exception(self, monkeypatch):
        calls = self._spy(monkeypatch)
        monkeypatch.setattr(cleaner, "is_sandbox_enabled", lambda: True)
        with pytest.raises(RuntimeError), cleaner.CtfmonSuspender():
            raise RuntimeError("сбой внутри блока")
        assert calls == [], f"песочница перезапустила живую службу ввода: {calls}"

    def test_sandbox_writes_no_suspension_marker(self, monkeypatch, tmp_path):
        """Маркер FIX-13 поднимает ctfmon при следующем старте приложения.

        В песочнице его создавать нельзя: он заставил бы живое приложение
        перезапустить службу ввода после песочничного прогона.
        """
        self._spy(monkeypatch)
        monkeypatch.setattr(cleaner, "is_sandbox_enabled", lambda: True)
        monkeypatch.setattr(cleaner, "_input_suspended_marker_path", tmp_path / "m")
        with cleaner.CtfmonSuspender():
            pass
        assert not (tmp_path / "m").exists()

    def test_real_mode_still_stops_and_restarts(self, monkeypatch):
        """Вне песочницы поведение не меняется — это основной рабочий путь."""
        calls = self._spy(monkeypatch)
        monkeypatch.setattr(cleaner, "is_sandbox_enabled", lambda: False)
        with cleaner.CtfmonSuspender() as suspender:
            assert suspender.skipped is False
        assert calls == ["stop", "start"]
        assert suspender.started is True

    def test_real_activate_sandbox_blocks_ctfmon(self):
        """Проверка на РЕАЛЬНОМ activate_sandbox(), а не на подмене флага.

        Список тестов (test_manifest_drift.py) и локальный запуск различаются
        путём включения песочницы, поэтому тест на подменённом предикате
        не защищал бы от регрессии в реальном переключателе.
        """
        calls: list[str] = []
        orig_stop, orig_start = cleaner._stop_ctfmon, cleaner._start_ctfmon
        cleaner._stop_ctfmon = lambda: calls.append("stop") or True
        cleaner._start_ctfmon = lambda: calls.append("start") or True
        try:
            cleaner.activate_sandbox()
            try:
                import config

                assert config.is_sandbox_enabled() is True
                with cleaner.CtfmonSuspender() as suspender:
                    assert suspender.skipped is True
            finally:
                cleaner.deactivate_sandbox()
        finally:
            cleaner._stop_ctfmon, cleaner._start_ctfmon = orig_stop, orig_start
        assert calls == [], f"activate_sandbox() не защитил живые процессы: {calls}"

    def test_delete_layout_under_sandbox_leaves_ctfmon_alone(self, monkeypatch):
        """Сквозная регрессия: конвейер удаления не трогает живой ctfmon.

        Именно этот путь раньше убивал ctfmon у пользователя на каждом
        прогоне pytest.
        """
        calls = self._spy(monkeypatch)
        cleaner.activate_sandbox()
        try:
            _create_test_tree()
            cleaner.delete_layout(PHANTOM_KLID)
        finally:
            cleaner.deactivate_sandbox()
            _delete_test_tree()
        assert calls == [], f"delete_layout в песочнице убил живые процессы: {calls}"

    def test_session_guard_blocks_live_input_services(self, monkeypatch):
        """Барьер conftest.py обязан быть активен — иначе всё выше — фикция.

        Без него забытая подмена в любом новом тесте снова убьёт службу
        ввода у того, кто запустил pytest. Проверяем именно отказ: тихий
        no-op пропустил бы утечку дальше незаметно.
        """
        from conftest import LiveInputServiceLeakError

        # Никаких собственных подмен: только autouse-фикстура conftest.
        assert callable(cleaner._stop_ctfmon)
        with pytest.raises(LiveInputServiceLeakError):
            cleaner._stop_ctfmon()
        with pytest.raises(LiveInputServiceLeakError):
            cleaner._start_ctfmon()

    def test_session_guard_redirects_suspension_marker(self, tmp_path):
        """Маркер FIX-13 не должен попадать в реальный каталог приложения."""
        marker = cleaner._input_suspended_marker_path()
        assert str(marker).startswith(str(tmp_path.parent)) or "Temp" in str(marker)
        assert "KeyboardLayoutCleaner" not in str(marker)


if __name__ == "__main__":
    unittest.main(verbosity=2)
