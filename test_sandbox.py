r"""
test_sandbox.py — Живые (live) тесты по «Плану тестирования и безопасности».

БЕЗОПАСНОСТЬ:
  Все операции записи/удаления выполняются ТОЛЬКО в тестовом ключе
  HKCU\Software\TestLayoutCleaner (sandbox-ключ из плана тестирования).

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

import json
import os
import re
import subprocess
import tempfile
import unittest
import winreg
from pathlib import Path

import pytest

import cleaner
import scanner

TEST_ROOT = "Software\\TestLayoutCleaner"
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
        self.assertIsInstance(layouts, dict)
        for klid, locations in layouts.items():
            self.assertRegex(klid, KLID_RE)
            self.assertIsInstance(locations, list)
            for loc in locations:
                self.assertIn("path", loc)
                self.assertIn("value", loc)
                self.assertTrue(loc["path"])

    def test_hex_to_name_mapping(self) -> None:
        name = scanner.get_layout_name("00000409")
        self.assertNotIn("Layout (", name)  # есть в HKLM на любой Windows
        self.assertEqual(
            scanner.get_layout_name(PHANTOM_KLID), "Layout (" + PHANTOM_KLID + ")"
        )

    def test_missing_branches_do_not_crash(self) -> None:
        missing = TEST_ROOT + "\\Definitely\\Missing\\Branch"
        self.assertEqual(
            scanner._reg_get_string(winreg.HKEY_CURRENT_USER, missing), {}
        )
        self.assertEqual(
            scanner._get_preload_keys(winreg.HKEY_CURRENT_USER, missing), {}
        )

    def test_powershell_language_list_parsed(self) -> None:
        entries = scanner._get_language_list_from_powershell()
        for entry in entries:
            self.assertRegex(entry["KeyboardLayoutId"], KLID_RE)
            self.assertTrue(entry["LanguageTag"])


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
            self.assertTrue(file.exists())
            raw = file.read_bytes()
            self.assertTrue(raw.startswith(b"\xff\xfe"), "нет BOM UTF-16 LE")
            text = file.read_text(encoding="utf-16")
            headers = [
                ln
                for ln in text.splitlines()
                if ln.startswith("Windows Registry Editor Version")
            ]
            self.assertEqual(len(headers), 1)
            # Регрессия: раньше reg export перезаписывал файл, и в бэкапе
            # оставалась только последняя ветка
            self.assertIn(
                "[HKEY_CURRENT_USER\\" + TEST_ROOT + "\\Preload]", text
            )
            self.assertIn(
                "[HKEY_CURRENT_USER\\" + TEST_ROOT + "\\Substitutes]", text
            )

            # Восстановление: сносим дерево и импортируем бэкап обратно
            _delete_test_tree()
            self.assertIsNone(_get_test_value("Preload", "2"))
            result = subprocess.run(
                ["reg", "import", backup_path], capture_output=True, timeout=30
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(_get_test_value("Preload", "2"), PHANTOM_KLID)
            self.assertEqual(_get_test_value("Preload", "1"), "00000409")
            self.assertEqual(_get_test_value("Substitutes", PHANTOM_KLID), "00000409")

    def test_backup_of_missing_branch_does_not_crash(self) -> None:
        """Ветка не существует → BackupError (файл-заглушки больше нет):
        delete_layout обязан отменить удаление, краша нет."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaises(cleaner.BackupError):
                cleaner.backup_registry(
                    [{"root": "HKCU", "subkey": TEST_ROOT + "\\Missing"}],
                    backup_dir=tmp_dir,
                )
            self.assertEqual(list(Path(tmp_dir).glob("*.reg")), [])

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
            self.assertIsNone(_get_test_value("Substitutes", PHANTOM_KLID))

            result = cleaner.restore_registry_backup(backup_path)
            self.assertTrue(result["ok"], result.get("detail"))
            self.assertFalse(result["elevated"])
            self.assertFalse(result["needs_admin"])
            self.assertEqual(
                _get_test_value("Substitutes", PHANTOM_KLID), "00000409"
            )

    def test_list_backups_finds_created_backup(self) -> None:
        """list_backups видит бэкап, созданный backup_registry в sandbox."""
        _create_test_tree()
        with tempfile.TemporaryDirectory() as tmp_dir:
            cleaner.backup_registry(
                [{"root": "HKCU", "subkey": TEST_ROOT + "\\Preload"}],
                backup_dir=tmp_dir,
            )
            items = cleaner.list_backups(tmp_dir)
            self.assertEqual(len(items), 1)
            self.assertFalse(items[0]["has_hku"])
            self.assertEqual(items[0]["json_path"], "")
            self.assertIn(
                "[HKEY_CURRENT_USER\\" + TEST_ROOT + "\\Preload]",
                items[0]["sections"],
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
            winreg.SetValueEx(
                key, "Languages", 0, winreg.REG_MULTI_SZ, langs
            )
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
        self.assertTrue(
            any(d.startswith("en-GB\\") for d in deleted)
        )
        # Профиль языка СОХРАНЁН (язык активен) и Languages не тронуты
        self.assertTrue(_key_exists(profile + "\\en-GB"))
        self.assertTrue(_key_exists(profile + "\\en-US"))
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            TEST_ROOT + "\\" + profile,
            0,
            winreg.KEY_READ,
        ) as key:
            langs = winreg.QueryValueEx(key, "Languages")[0]
        self.assertEqual(list(langs), ["en-US", "en-GB"])

    def test_delete_removes_empty_inactive_profile(self) -> None:
        """Если en-GB НЕ активен (нет в Languages) и после точечной
        очистки профиль опустел — безопасно удаляется пустой профиль."""
        profile = self._build_mini_profile(active=False)
        cleaner._clean_intl_profile(
            winreg.HKEY_CURRENT_USER, TEST_ROOT + "\\" + profile, "00000809"
        )
        self.assertFalse(_key_exists(profile + "\\en-GB"))
        self.assertTrue(_key_exists(profile + "\\en-US"))

    def test_dry_run_does_not_modify(self) -> None:
        profile = self._build_mini_profile()
        deleted = cleaner._clean_intl_profile(
            winreg.HKEY_CURRENT_USER,
            TEST_ROOT + "\\" + profile,
            "00000809",
            delete=False,
        )
        self.assertTrue(deleted)
        self.assertTrue(_key_exists(profile + "\\en-GB"))
        self.assertTrue(_key_exists(profile + "\\en-US"))

    def test_unknown_klid_is_noop(self) -> None:
        profile = self._build_mini_profile()
        deleted = cleaner._clean_intl_profile(
            winreg.HKEY_CURRENT_USER, TEST_ROOT + "\\" + profile, "d001dead"
        )
        self.assertEqual(deleted, [])
        self.assertTrue(_key_exists(profile + "\\en-GB"))
        self.assertTrue(_key_exists(profile + "\\en-US"))


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
            winreg.SetValueEx(
                key, "KeyboardLayout", 0, winreg.REG_SZ, "d001dead"
            )
        found = scanner._scan_branch_recursive(
            winreg.HKEY_CURRENT_USER, TEST_ROOT + "\\ScanRec"
        )
        self.assertIn(("A\\B\\C\\KeyboardLayout", "d001dead"), found)

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
        self.assertIn(("Sub\\InputMethodOverride", "00000809"), found)

    def test_recursive_scan_missing_branch(self) -> None:
        self.assertEqual(
            scanner._scan_branch_recursive(
                winreg.HKEY_CURRENT_USER, TEST_ROOT + "\\Missing"
            ),
            [],
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
        self.assertEqual(subs.get("d001dead", {}).get("target"), "00000409")


@pytest.mark.live
class TestPermissionsSafety(SandboxTestCase):
    """Чек-лист п.3: поведение без админ-прав (без крашей)."""

    def test_is_admin_returns_bool(self) -> None:
        self.assertIsInstance(cleaner.is_admin(), bool)

    def test_missing_or_denied_keys_are_not_fatal(self) -> None:
        self.assertEqual(
            cleaner._clean_preload_keys(
                winreg.HKEY_CURRENT_USER, TEST_ROOT + "\\Missing", PHANTOM_KLID
            ),
            [],
        )
        # HKU\.DEFAULT: без админ-прав — PermissionError, с правами — просто
        # нет совпадений. Оба пути обязаны вернуть [] без исключений.
        self.assertEqual(
            cleaner._clean_preload_keys(
                winreg.HKEY_USERS, ".DEFAULT\\Keyboard Layout\\Preload", PHANTOM_KLID
            ),
            [],
        )


@pytest.mark.live
class TestPhantomDeletionSimulation(SandboxTestCase):
    """Чек-лист п.4: симуляция удаления фантома (в sandbox-ключе)."""

    def test_phantom_removed_from_test_key(self) -> None:
        _create_test_tree()
        deleted = cleaner._clean_preload_keys(
            winreg.HKEY_CURRENT_USER, TEST_ROOT + "\\Preload", PHANTOM_KLID
        )
        self.assertEqual(deleted, ["2"])
        self.assertIsNone(_get_test_value("Preload", "2"))
        self.assertEqual(_get_test_value("Preload", "1"), "00000409")

        deleted_subst = cleaner._clean_substitutes_keys(
            winreg.HKEY_CURRENT_USER, TEST_ROOT + "\\Substitutes", PHANTOM_KLID
        )
        self.assertEqual(deleted_subst, [PHANTOM_KLID])
        self.assertIsNone(_get_test_value("Substitutes", PHANTOM_KLID))

    def test_delete_layout_end_to_end_noop_on_real_system(self) -> None:
        """Полный конвейер delete_layout с фантомным KLID.

        В реальных ветках совпадений нет => ничего не удаляется; бэкап
        создаётся (read-only); PowerShell-синхронизация даёт NOCHANGE
        (Set-WinUserLanguageList НЕ вызывается, профиль не меняется).
        """
        created_backups = []
        try:
            report = cleaner.delete_layout(PHANTOM_KLID)
            if report.get("backup_path"):
                created_backups.append(report["backup_path"])
            if report.get("langlist_backup_path"):
                created_backups.append(report["langlist_backup_path"])
            self.assertTrue(report["backup_path"])
            self.assertTrue(Path(report["backup_path"]).exists())
            self.assertFalse(report["success"])  # фантом в системе отсутствует
            self.assertEqual(report["hkcu_preload_deleted"], [])
            self.assertEqual(report["hkcu_substitutes_deleted"], [])
            self.assertEqual(report["hkcu_ctf_deleted"], [])
            self.assertEqual(report["hkcu_intl_deleted"], [])
            self.assertEqual(report["hku_default_deleted"], [])
            # Синхронизация отработала без изменения профиля (NOCHANGE)
            self.assertTrue(report["power_sync"])
            self.assertEqual(report["power_sync_detail"], "NOCHANGE")
            self.assertIn("langlist_backup_path", report)
        finally:
            for p in created_backups:
                try:
                    os.remove(p)
                except OSError:
                    pass




@pytest.mark.live
class TestDryRunAndBackupLive(SandboxTestCase):
    """Dry-run и JSON-бэкап списка языков на живой системе (read-only)."""

    def test_plan_layout_removal_phantom(self) -> None:
        plan = cleaner.plan_layout_removal(PHANTOM_KLID)
        self.assertEqual(plan["klid"], PHANTOM_KLID)
        self.assertIn("HKCU\\Keyboard Layout\\Preload", plan["branches"])
        self.assertEqual(len(plan["branches"]), 6)
        # PowerShell-анализ доступен; фантома в списке языков нет
        self.assertTrue(plan["ps"]["available"])
        self.assertEqual(plan["ps"]["detail"], "NOCHANGE")
        self.assertFalse(plan["ps"]["tips_removed"])

    def test_plan_layout_removal_ignores_sandbox_tree(self) -> None:
        """План затрагивает только реальные ветки: sandbox-ключ не попадает."""
        _create_test_tree()
        plan = cleaner.plan_layout_removal(PHANTOM_KLID)
        preload = plan["branches"]["HKCU\\Keyboard Layout\\Preload"]["values"]
        self.assertEqual(preload, [])
        self.assertIsNone(plan["branches"].get("HKCU\\Software\\TestLayoutCleaner"))

    def test_backup_language_list_writes_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake = Path(tmp_dir) / "backup.reg"
            fake.write_bytes(b"\\xff\\xfe")
            json_path = cleaner._backup_language_list(fake)
            self.assertTrue(json_path)
            data = json.loads(Path(json_path).read_text(encoding="utf-8"))
            self.assertIsInstance(data, list)


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
            winreg.SetValueEx(
                key, "KeyboardLayout", 0, winreg.REG_DWORD, 68748313
            )
        # Легитимный сосед того же языка (0x409 → decimal 1033)
        legit = base + "\\Assemblies\\0x00000419\\" + self.LEGIT_GUID
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, legit, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(
                key, "KeyboardLayout", 0, winreg.REG_DWORD, 0x00000409
            )
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
        found = dict(
            scanner._scan_branch_recursive(winreg.HKEY_CURRENT_USER, base)
        )
        # decimal KeyboardLayout приведён к каноническому hex-KLID
        self.assertEqual(
            found.get(
                "Assemblies\\0x00000419\\%s\\KeyboardLayout"
                % self.PHANTOM_GUID
            ),
            "04190419",
        )
        # значение на глубине 5 (...\{GUID}\00000000\KLID) теперь видно
        deep_path = (
            "SortOrder\\AssemblyItem\\0x00000419\\%s\\00000000\\KLID"
            % self.DEEP_GUID
        )
        self.assertIn(deep_path, found)
        self.assertEqual(found[deep_path], "04190419")

    def test_profile_keys_deleted_entirely(self) -> None:
        base = self._build_ctf_sim()
        deleted = cleaner._clean_ctf_profiles(
            winreg.HKEY_CURRENT_USER, base, "04190419"
        )
        profiles = [d for d in deleted if "профиль TSF целиком" in d]
        self.assertEqual(len(profiles), 2)
        # фантомный {GUID} удалён ЦЕЛИКОМ (вместе с подключами)
        self.assertFalse(
            _key_exists("CTFsim\\Assemblies\\0x00000419\\" + self.PHANTOM_GUID)
        )
        self.assertFalse(
            _key_exists(
                "CTFsim\\SortOrder\\AssemblyItem\\0x00000419\\"
                + self.DEEP_GUID
            )
        )
        # легитимный сосед (KeyboardLayout=0x409) не тронут
        self.assertTrue(
            _key_exists("CTFsim\\Assemblies\\0x00000419\\" + self.LEGIT_GUID)
        )
        # опустевшие родители удалены, а родитель с «легитимным» профилем — нет
        self.assertFalse(
            _key_exists("CTFsim\\SortOrder\\AssemblyItem\\0x00000419")
        )
        self.assertTrue(_key_exists("CTFsim\\Assemblies\\0x00000419"))

    def test_dry_run_deletes_nothing(self) -> None:
        base = self._build_ctf_sim()
        found = cleaner._clean_ctf_profiles(
            winreg.HKEY_CURRENT_USER, base, "04190419", delete=False
        )
        self.assertEqual(
            len([d for d in found if "профиль TSF целиком" in d]), 2
        )
        self.assertTrue(
            _key_exists("CTFsim\\Assemblies\\0x00000419\\" + self.PHANTOM_GUID)
        )
        self.assertTrue(
            _key_exists(
                "CTFsim\\SortOrder\\AssemblyItem\\0x00000419\\"
                + self.DEEP_GUID
                + "\\00000000"
            )
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
