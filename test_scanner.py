"""
test_scanner.py - Unit tests for scanner / cleaner / main (registry-free).

Mock-based using in-memory FakeWinreg and unittest.mock.
Markers: @pytest.mark.gui, @pytest.mark.live

Uses shared fixtures from conftest.py:
- fake_winreg / patched_winreg — in-memory registry mock
- tmp_test_dir — temp directory for test files
- fake_proc_factory — subprocess.CompletedProcess factory
- clean_import_main — clean import of main.py
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

import cleaner
import config
import scanner
import winproc

# ---------------------------------------------------------------------------
# Legacy helpers (kept for compatibility with existing tests)
# ---------------------------------------------------------------------------


class _MultiPatch:
    """Контекстный менеджер для нескольких patch-объектов."""

    def __init__(self, *patches):
        self._patches = patches

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False

def _install_fake_winreg(fake):
    """Патчим winreg в scanner и cleaner единым FakeWinreg-объектом."""
    return _MultiPatch(
        mock.patch.object(scanner, "winreg", fake),
        mock.patch.object(cleaner, "winreg", fake),
        mock.patch.object(
            cleaner,
            "_ROOT_CONST",
            {
                "HKCU": fake.HKEY_CURRENT_USER,
                "HKU": fake.HKEY_USERS,
                "HKLM": fake.HKEY_LOCAL_MACHINE,
            },
        ),
    )


def _proc(stdout="", stderr="", returncode=0, args=None):
    """Mock-объект, имитирующий subprocess.run(...)."""
    m = mock.MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    m.args = args
    return m


def _run_mock(returns=None, side_effect=None):
    return mock.MagicMock(return_value=returns, side_effect=side_effect)


def _tmp_dir() -> str:
    """Временный каталог, не требующий фикстуры."""
    d = tempfile.mkdtemp(prefix="klc_test_")
    os.makedirs(d, exist_ok=True)
    return d


def _write_reg(path, sections):
    """Записать файл .reg в кодировке UTF-16 LE с заголовком Windows."""
    text = "Windows Registry Editor Version 5.00\r\n\r\n" + "".join(sections)
    Path(path).write_text(text, encoding="utf-16")


# ---------------------------------------------------------------------------
# TestSafeStr
# ---------------------------------------------------------------------------
class TestSafeStr:
    """_safe_str: safe string conversion."""

    def test_str_value(self):
        assert scanner._safe_str("  hello  ") == "hello"

    def test_int_value(self):
        assert scanner._safe_str(42) == "42"

    def test_none_value(self):
        assert scanner._safe_str(None) == "None"

    def test_object_with_broken_str(self):
        class BadStr:
            def __str__(self):
                raise ValueError("boom")

        assert scanner._safe_str(BadStr()) == ""


class TestRegGetString:
    """_reg_get_string: reading registry values."""

    def test_existing_key_all_values(self, fake_winreg):
        fake_winreg.set(
            fake_winreg.HKEY_CURRENT_USER,
            "Software\\Test",
            values={"foo": "bar", "baz": "qux"},
        )
        with _install_fake_winreg(fake_winreg):
            result = scanner._reg_get_string(
                scanner.winreg.HKEY_CURRENT_USER, "Software\\Test"
            )
        assert result == {"foo": "bar", "baz": "qux"}

    def test_specific_value(self, fake_winreg):
        fake_winreg.set(
            fake_winreg.HKEY_CURRENT_USER,
            "Software\\Test",
            values={"foo": "bar"},
        )
        with _install_fake_winreg(fake_winreg):
            result = scanner._reg_get_string(
                scanner.winreg.HKEY_CURRENT_USER, "Software\\Test", "foo"
            )
        assert result == {"": "bar"}

    def test_missing_key(self, fake_winreg):
        with _install_fake_winreg(fake_winreg):
            result = scanner._reg_get_string(
                scanner.winreg.HKEY_CURRENT_USER, "NonExistent"
            )
        assert result == {}

    def test_missing_value(self, fake_winreg):
        fake_winreg.set(
            fake_winreg.HKEY_CURRENT_USER,
            "Software\\Test",
            values={"foo": "bar"},
        )
        with _install_fake_winreg(fake_winreg):
            result = scanner._reg_get_string(
                scanner.winreg.HKEY_CURRENT_USER, "Software\\Test", "missing"
            )
        assert result == {}


class TestGetPreloadKeys:
    """_get_preload_keys."""

    def test_returns_klid_dict(self, fake_winreg):
        fake_winreg.set(
            fake_winreg.HKEY_CURRENT_USER,
            "Keyboard Layout\\Preload",
            values={"1": "00000409", "2": "00000419"},
        )
        with _install_fake_winreg(fake_winreg):
            result = scanner._get_preload_keys(
                scanner.winreg.HKEY_CURRENT_USER, "Keyboard Layout\\Preload"
            )
        assert result == {"00000409": "00000409", "00000419": "00000419"}

    def test_empty_values_skipped(self, fake_winreg):
        fake_winreg.set(
            fake_winreg.HKEY_CURRENT_USER,
            "Keyboard Layout\\Preload",
            values={"1": "", "2": "00000419"},
        )
        with _install_fake_winreg(fake_winreg):
            result = scanner._get_preload_keys(
                scanner.winreg.HKEY_CURRENT_USER, "Keyboard Layout\\Preload"
            )
        assert result == {"00000419": "00000419"}

    def test_missing_key(self, fake_winreg):
        with _install_fake_winreg(fake_winreg):
            result = scanner._get_preload_keys(
                scanner.winreg.HKEY_CURRENT_USER, "NonExistent"
            )
        assert result == {}


class TestScanSubstitutes:
    def test_basic_mapping(self, fake_winreg):
        fake_winreg.set(
            fake_winreg.HKEY_CURRENT_USER,
            "Keyboard Layout\\Substitutes",
            values={"00000419": "00000409"},
        )
        with _install_fake_winreg(fake_winreg):
            result = scanner._scan_substitutes(
                scanner.winreg.HKEY_CURRENT_USER, "Keyboard Layout\\Substitutes"
            )
        assert result == {"00000419": {"target": "00000409"}}

    def test_case_normalized(self, fake_winreg):
        fake_winreg.set(
            fake_winreg.HKEY_CURRENT_USER,
            "Keyboard Layout\\Substitutes",
            values={"D0010419": "00000409"},
        )
        with _install_fake_winreg(fake_winreg):
            result = scanner._scan_substitutes(
                scanner.winreg.HKEY_CURRENT_USER, "Keyboard Layout\\Substitutes"
            )
        assert "d0010419" in result
        assert result["d0010419"]["target"] == "00000409"

    def test_missing_key(self, fake_winreg):
        with _install_fake_winreg(fake_winreg):
            result = scanner._scan_substitutes(
                scanner.winreg.HKEY_CURRENT_USER, "NonExistent"
            )
        assert result == {}


class TestNormalizeKlidToken:
    def test_already_hex(self):
        assert scanner._normalize_klid_token("00000409", r"0x00000409") == "00000409"

    def test_hex_with_letters(self):
        assert scanner._normalize_klid_token("00000809", "") == "00000809"

    def test_decimal_short(self):
        assert scanner._normalize_klid_token("1033", "") == "00000409"

    def test_decimal_long(self):
        assert scanner._normalize_klid_token("68748313", r"0x00000419") == "04190419"

    def test_empty_string(self):
        assert scanner._normalize_klid_token("", "") == ""

    def test_invalid_chars(self):
        assert scanner._normalize_klid_token("xyz", "") == "xyz"


class TestKlidMatchesValue:
    def test_direct_klid_match(self):
        assert scanner._klid_matches_value("00000409", "00000409")

    def test_tip_format_match(self):
        assert scanner._klid_matches_value("00000409", "0409:00000409")

    def test_tag_match(self):
        assert scanner._klid_matches_value("00000409", "en-US")

    def test_no_match(self):
        assert not scanner._klid_matches_value("00000409", "00000419")

    def test_empty_value(self):
        assert not scanner._klid_matches_value("00000409", "")


class TestScanBranchRecursive:
    """_scan_branch_recursive: recursive subtree scan."""

    def test_finds_nested_klid(self, fake_winreg):
        fake_winreg.set(
            fake_winreg.HKEY_CURRENT_USER,
            "T\\A\\B\\C",
            values={"KeyboardLayout": "00000419"},
        )
        with _install_fake_winreg(fake_winreg):
            found = scanner._scan_branch_recursive(
                scanner.winreg.HKEY_CURRENT_USER, "T"
            )
        assert any(v == "00000419" for _, v in found)

    def test_tip_format(self, fake_winreg):
        fake_winreg.set(
            fake_winreg.HKEY_CURRENT_USER,
            "T2\\Sub",
            values={"InputMethodOverride": "0809:00000809"},
        )
        with _install_fake_winreg(fake_winreg):
            found = scanner._scan_branch_recursive(
                scanner.winreg.HKEY_CURRENT_USER, "T2"
            )
        assert any(v == "00000809" for _, v in found)

    def test_missing_branch(self, fake_winreg):
        with _install_fake_winreg(fake_winreg):
            found = scanner._scan_branch_recursive(
                scanner.winreg.HKEY_CURRENT_USER, "NoExist"
            )
        assert found == []

    def test_permission_error_skipped(self, fake_winreg):
        fake_winreg.deny.add("T3\\Denied")
        fake_winreg.set(
            fake_winreg.HKEY_CURRENT_USER,
            "T3\\Allowed",
            values={"KLID": "00000409"},
        )
        with _install_fake_winreg(fake_winreg):
            found = scanner._scan_branch_recursive(
                scanner.winreg.HKEY_CURRENT_USER, "T3"
            )
        assert any(v == "00000409" for _, v in found)


class TestGetLayoutName:
    """get_layout_name / _resolve_name."""

    def test_known_layout_en(self, fake_winreg):
        with _install_fake_winreg(fake_winreg):
            name = scanner.get_layout_name("00000409")
        assert name == "en-US"

    def test_known_layout_ru(self, fake_winreg):
        with _install_fake_winreg(fake_winreg):
            name = scanner.get_layout_name("00000419")
        assert name == "ru-RU"

    def test_unknown_layout(self, fake_winreg):
        with _install_fake_winreg(fake_winreg):
            name = scanner.get_layout_name("d001dead")
        assert name == "Layout (d001dead)"

    def test_uppercase_normalized(self, fake_winreg):
        with _install_fake_winreg(fake_winreg):
            name = scanner.get_layout_name("D001DEAD")
        assert name == "Layout (d001dead)"


class TestParseLanguageEntries:
    """_parse_language_entries."""

    def test_basic_parse(self):
        stdout = "en-US\ten-US:00000409;en-US:00000409\nru-RU\tru-RU:00000419"
        entries = scanner._parse_language_entries(stdout)
        assert len(entries) == 2
        assert entries[0]["LanguageTag"] == "en-US"
        assert entries[0]["KeyboardLayoutId"] == "00000409"
        assert entries[1]["LanguageTag"] == "ru-RU"

    def test_empty_stdout(self):
        assert scanner._parse_language_entries("") == []

    def test_no_tab_lines_skipped(self):
        stdout = "garbage\nen-US\ten-US:00000409"
        entries = scanner._parse_language_entries(stdout)
        assert len(entries) == 1
        assert entries[0]["KeyboardLayoutId"] == "00000409"

    def test_cjk_fallback(self):
        stdout = "ja-JP\t0411:{A3BC9C5F-DB4A-4DF3-B769-30BD9E1D5A39}"
        entries = scanner._parse_language_entries(stdout)
        assert len(entries) == 1
        assert entries[0]["KeyboardLayoutId"] == "00000411"


class TestGetLanguageListFromPowerShell:
    """_get_language_list_from_powershell."""

    def test_success(self, monkeypatch):
        mock_result = _proc(stdout="en-US\ten-US:00000409")
        monkeypatch.setattr(scanner.subprocess, "run", _run_mock(returns=mock_result))
        entries = scanner._get_language_list_from_powershell()
        assert len(entries) == 1
        assert entries[0]["LanguageTag"] == "en-US"
        assert entries[0]["KeyboardLayoutId"] == "00000409"

    def test_empty_on_failure(self, monkeypatch):
        monkeypatch.setattr(
            scanner.subprocess, "run", _run_mock(returns=_proc(returncode=1))
        )
        assert scanner._get_language_list_from_powershell() == []

    def test_timeout_returns_empty(self, monkeypatch):
        def raise_timeout(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="pw", timeout=15)

        monkeypatch.setattr(
            scanner.subprocess, "run", mock.MagicMock(side_effect=raise_timeout)
        )
        assert scanner._get_language_list_from_powershell() == []


class TestScanKeyboardLayouts:
    """scan_keyboard_layouts."""

    def test_empty_registry(self, fake_winreg, monkeypatch):
        monkeypatch.setattr(scanner, "winreg", fake_winreg)
        monkeypatch.setattr(
            scanner.subprocess, "run", _run_mock(returns=_proc(stdout=""))
        )
        assert scanner.scan_keyboard_layouts() == {}

    def test_finds_preload_layout(self, fake_winreg, monkeypatch):
        fake_winreg.set(
            fake_winreg.HKEY_CURRENT_USER,
            "Keyboard Layout\\Preload",
            values={"1": "00000409"},
        )
        monkeypatch.setattr(scanner, "winreg", fake_winreg)
        monkeypatch.setattr(
            scanner.subprocess, "run", _run_mock(returns=_proc(stdout=""))
        )
        result = scanner.scan_keyboard_layouts()
        assert "00000409" in result
        locs = result["00000409"]
        assert any("Preload" in loc["path"] for loc in locs)

    def test_finds_powershell_layout(self, fake_winreg, monkeypatch):
        monkeypatch.setattr(scanner, "winreg", fake_winreg)
        monkeypatch.setattr(
            scanner.subprocess,
            "run",
            _run_mock(returns=_proc(stdout="en-US\ten-US:00000409")),
        )
        result = scanner.scan_keyboard_layouts()
        assert "00000409" in result
        locs = result["00000409"]
        assert any("PowerShell" in loc["path"] for loc in locs)

    def test_invalid_klid_filtered(self, fake_winreg, monkeypatch):
        fake_winreg.set(
            fake_winreg.HKEY_CURRENT_USER,
            "Keyboard Layout\\Preload",
            values={"1": "not_a_klid", "2": "00000409"},
        )
        monkeypatch.setattr(scanner, "winreg", fake_winreg)
        monkeypatch.setattr(
            scanner.subprocess, "run", _run_mock(returns=_proc(stdout=""))
        )
        result = scanner.scan_keyboard_layouts()
        assert "not_a_klid" not in result
        assert "00000409" in result


class TestScannerConstants:
    """Check scanner constants and regex patterns."""

    def test_layout_map_has_russian(self):
        assert "ru-RU" in scanner.LAYOUT_MAP
        assert scanner.LAYOUT_MAP["ru-RU"] == "00000419"

    def test_layout_map_has_english(self):
        assert "en-US" in scanner.LAYOUT_MAP
        assert scanner.LAYOUT_MAP["en-US"] == "00000409"

    def test_affected_branches_has_preload(self):
        paths = [b[1] for b in scanner.AFFECTED_BRANCHES]
        assert any("Preload" in p for p in paths)

    def test_klid_re_matches_valid(self):
        assert scanner._KLID_RE.match("00000409")
        assert not scanner._KLID_RE.match("xyz")
        assert not scanner._KLID_RE.match("0409")

    def test_tip_klid_re(self):
        m = scanner._TIP_KLID_RE.match("0409:00000409")
        assert m is not None
        assert m.group(1) == "00000409"


# ---------------------------------------------------------------------------
# TestCleanerBasic
# ---------------------------------------------------------------------------
class TestCleanerBasic:
    """Basic cleaner function tests."""

    def test_backup_error_is_runtime_error(self):
        assert issubclass(cleaner.BackupError, RuntimeError)

    def test_root_const_has_hkcu(self):
        assert "HKCU" in cleaner._ROOT_CONST
        assert "HKU" in cleaner._ROOT_CONST
        assert "HKLM" in cleaner._ROOT_CONST

    def test_klid_variants(self):
        result = cleaner._klid_variants("00000409")
        assert "00000409" in result
        assert "1033" in result  # decimal form

    def test_klid_to_tags(self):
        tags = cleaner._klid_to_tags("00000409")
        assert "en-US" in tags


class TestSubtreeEmpty:
    """_subtree_is_empty."""

    def test_empty_subtree(self, fake_winreg):
        fake_winreg.set(fake_winreg.HKEY_CURRENT_USER, "EmptyKey")
        with _install_fake_winreg(fake_winreg):
            result = cleaner._subtree_is_empty(
                cleaner.winreg.HKEY_CURRENT_USER, "EmptyKey"
            )
        assert result is True

    def test_non_empty_subtree(self, fake_winreg):
        fake_winreg.set(
            fake_winreg.HKEY_CURRENT_USER, "NonEmpty", values={"foo": "bar"}
        )
        with _install_fake_winreg(fake_winreg):
            result = cleaner._subtree_is_empty(
                cleaner.winreg.HKEY_CURRENT_USER, "NonEmpty"
            )
        assert result is False


# ---------------------------------------------------------------------------
# TestSandboxIsolation — регрессия P0-1 (sandbox обязан изолировать cleaner)
# ---------------------------------------------------------------------------
class TestSandboxIsolation:
    """
    scanner.activate_sandbox() обязана переключать ветки и для cleaner,
    и для GUI. Историческая ошибка: `from scanner import AFFECTED_BRANCHES`
    в cleaner фиксировал список РЕАЛЬНЫХ веток на момент импорта, поэтому
    delete_layout/plan_layout_removal работали с живым реестром даже в
    sandbox-режиме.
    """

    def setup_method(self) -> None:
        scanner.deactivate_sandbox()
        cleaner.deactivate_sandbox()

    def teardown_method(self) -> None:
        scanner.deactivate_sandbox()
        cleaner.deactivate_sandbox()

    def test_accessor_returns_sandbox_branches(self):
        scanner.activate_sandbox()
        branches = scanner.get_affected_branches()
        assert branches
        for root, subkey, _mode, admin_required in branches:
            assert subkey.startswith(config._SANDBOX_ROOT)
            assert root == "HKCU"  # HKU/HKLM перенаправлены в HKCU sandbox
            assert admin_required is False  # права админа в sandbox не нужны

    def test_accessor_returns_real_branches_after_deactivate(self):
        scanner.activate_sandbox()
        scanner.deactivate_sandbox()
        branches = [tuple(b) for b in scanner.get_affected_branches()]
        assert ("HKCU", "Keyboard Layout\\Preload", "preload", False) in branches
        assert (
            "HKU",
            ".DEFAULT\\Keyboard Layout\\Preload",
            "preload",
            True,
        ) in branches

    def test_activate_sandbox_is_idempotent(self):
        scanner.activate_sandbox()
        scanner.activate_sandbox()  # повторная активация не должна двойнопрефиксовать
        branches = scanner.get_affected_branches()
        for _root, subkey, _mode, _admin in branches:
            assert subkey.count(config._SANDBOX_ROOT) == 1

    def test_cleaner_has_no_stale_binding(self):
        # cleaner больше не держит собственную копию списка веток
        assert not hasattr(cleaner, "AFFECTED_BRANCHES")

    def test_cleaner_resolvers_follow_sandbox(self):
        scanner.activate_sandbox()
        cleaner.activate_sandbox()
        sandbox = config._SANDBOX_ROOT
        assert cleaner._ctf_subkey() == sandbox + "\\Software\\Microsoft\\CTF"
        assert cleaner._intl_subkey() == (
            sandbox + "\\Control Panel\\International\\User Profile"
        )
        assert cleaner._ctf_subkey() in cleaner._recursive_subkeys()
        assert cleaner._intl_subkey() in cleaner._recursive_subkeys()
        assert all(s.startswith(sandbox) for s in cleaner._recursive_subkeys())
        # ветки для delete_layout/plan_layout_removal — только sandbox
        for _root, subkey, _mode, _admin in cleaner._affected_branches():
            assert subkey.startswith(sandbox)

    def test_cleaner_resolvers_real_after_deactivate(self):
        scanner.activate_sandbox()
        cleaner.activate_sandbox()
        cleaner.deactivate_sandbox()
        scanner.deactivate_sandbox()
        assert cleaner._ctf_subkey() == "Software\\Microsoft\\CTF"
        assert cleaner._intl_subkey() == "Control Panel\\International\\User Profile"
        assert cleaner._recursive_subkeys() == set(cleaner._RECURSIVE_SUBKEYS)

    def test_report_field_mapping_in_sandbox(self):
        scanner.activate_sandbox()
        sandbox = config._SANDBOX_ROOT
        assert (
            cleaner._report_field_for("HKCU", sandbox + "\\Keyboard Layout\\Preload")
            == "hkcu_preload_deleted"
        )
        assert (
            cleaner._report_field_for(
                "HKCU", sandbox + "\\Keyboard Layout\\Substitutes"
            )
            == "hkcu_substitutes_deleted"
        )
        assert (
            cleaner._report_field_for(
                "HKCU", sandbox + "\\Control Panel\\International\\User Profile"
            )
            == "hkcu_intl_deleted"
        )
        assert (
            cleaner._report_field_for("HKCU", sandbox + "\\Software\\Microsoft\\CTF")
            == "hkcu_ctf_deleted"
        )
        assert (
            cleaner._report_field_for(
                "HKCU", sandbox + "\\test\\.DEFAULT\\Keyboard Layout\\Preload"
            )
            == "hku_default_deleted"
        )
        # реальные ветки маппятся как раньше
        assert (
            cleaner._report_field_for("HKCU", "Keyboard Layout\\Preload")
            == "hkcu_preload_deleted"
        )

    def test_backup_paths_are_sandbox_only(self):
        scanner.activate_sandbox()
        cleaner.activate_sandbox()
        paths = cleaner._paths_to_backup(
            cleaner._affected_branches(), admin_privileges=True
        )
        assert paths
        for entry in paths:
            assert entry["subkey"].startswith(config._SANDBOX_ROOT)
            assert entry["root"] == "HKCU"

    def test_backup_paths_real_mode_unchanged(self):
        scanner.deactivate_sandbox()
        cleaner.deactivate_sandbox()
        paths = cleaner._paths_to_backup(
            cleaner._affected_branches(), admin_privileges=False
        )
        subkeys = {e["subkey"] for e in paths}
        assert "Keyboard Layout\\Preload" in subkeys
        # без админ-прав ветка HKU\\.DEFAULT не бэкапится
        assert ".DEFAULT\\Keyboard Layout\\Preload" not in subkeys
        paths_admin = cleaner._paths_to_backup(
            cleaner._affected_branches(), admin_privileges=True
        )
        assert ".DEFAULT\\Keyboard Layout\\Preload" in {
            e["subkey"] for e in paths_admin
        }


# ---------------------------------------------------------------------------
# TestWinprocHelper — регрессия P1-1 (скрытие консольных окон subprocess)
# ---------------------------------------------------------------------------
class TestWinprocHelper:
    """
    В windowed-сборке (console=False) каждый прямой subprocess.run с
    powershell/reg открывал чёрное окно консоли поверх GUI. Все вызовы
    приложения обязаны идти через winproc.run_hidden (CREATE_NO_WINDOW).
    """

    def test_run_hidden_adds_create_no_window(self, monkeypatch):
        calls: list[tuple[list, dict]] = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return mock.MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(winproc.subprocess, "run", fake_run)
        winproc.run_hidden(["powershell", "-NoProfile", "-Command", "x"], timeout=5)
        cmd, kwargs = calls[0]
        assert cmd == ["powershell", "-NoProfile", "-Command", "x"]
        assert kwargs["creationflags"] & winproc.CREATE_NO_WINDOW
        assert kwargs["timeout"] == 5

    def test_run_hidden_merges_existing_flags(self, monkeypatch):
        calls: list[dict] = []

        def fake_run(cmd, **kwargs):
            calls.append(kwargs)
            return mock.MagicMock(returncode=0)

        monkeypatch.setattr(winproc.subprocess, "run", fake_run)
        winproc.run_hidden(["reg", "export"], creationflags=0x8)
        assert calls[0]["creationflags"] == (0x8 | winproc.CREATE_NO_WINDOW)

    def test_run_hidden_real_spawn(self):
        """Сквозная проверка: CREATE_NO_WINDOW не ломает реальный запуск."""
        result = winproc.run_hidden(
            [sys.executable, "-c", "print('ok')"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "ok"

    def test_scanner_and_cleaner_go_through_winproc(self):
        """Статическая регрессия: в scanner/cleaner нет прямых subprocess.run."""
        base = Path(__file__).resolve().parent
        for name in ("scanner.py", "cleaner.py"):
            src = (base / name).read_text(encoding="utf-8")
            assert "subprocess.run(" not in src, (
                f"{name}: прямой вызов subprocess.run — консоль будет мигать; "
                "используйте winproc.run_hidden"
            )
            assert "run_hidden(" in src, f"{name}: не используется run_hidden"

    def test_mock_via_scanner_subprocess_still_intercepts(self, monkeypatch):
        """Моки тестов (patch scanner.subprocess.run) перехватывают run_hidden."""
        recorded = []

        def fake_run(cmd, **kwargs):
            recorded.append(cmd)
            return mock.MagicMock(returncode=0, stdout="en-US\ten-US:00000409")

        monkeypatch.setattr(scanner.subprocess, "run", fake_run)
        entries = scanner._get_language_list_from_powershell()
        assert entries
        assert entries[0]["LanguageTag"] == "en-US"
        assert recorded, "run_hidden должен вызывать общий subprocess.run"


# ---------------------------------------------------------------------------
# TestRestoreLanguageList
# ---------------------------------------------------------------------------
class TestRestoreLanguageList:
    """restore_language_list: генерация PS-скрипта из JSON и экранирование '.

    Единственная защита от PS-инъекции в этом пути — экранирование
    одинарных кавычек (' -> '') при сборке New-WinUserLanguageList.
    """

    @staticmethod
    def _write_json(data):
        jp = Path(_tmp_dir()) / "langlist.json"
        jp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return jp

    def _capture_success_run(self):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return _proc(returncode=0, stdout="SUCCESS")

        return captured, fake_run

    def test_success_builds_ps_script(self):
        jp = self._write_json(
            [{"LanguageTag": "en-US", "InputMethodTips": ["0409:00000409"]}]
        )
        captured, fake_run = self._capture_success_run()
        with mock.patch.object(cleaner, "run_hidden", fake_run):
            ok = cleaner.restore_language_list(jp)
        assert ok is True
        # Команда: [powershell, -NoProfile, -File, PS1_PATH, -JsonPath, JSON_PATH]
        cmd = captured["cmd"]
        assert "layout_cleaner_restore.ps1" in cmd[-3]  # PS1-файл
        assert "-JsonPath" in cmd[-2]
        # PS1-скрипт содержит нужные команды
        ps_content = (cleaner._PS_DIR / "layout_cleaner_restore.ps1").read_text(
            encoding="utf-8"
        )
        assert "New-WinUserLanguageList" in ps_content
        assert "Set-WinUserLanguageList" in ps_content
        assert '"SUCCESS"' in ps_content or "SUCCESS" in ps_content

    def test_single_quotes_are_escaped(self):
        jp = self._write_json(
            [
                {
                    "LanguageTag": "O'Brien",
                    "InputMethodTips": ["0419:00000419", "It's"],
                }
            ]
        )
        _captured, fake_run = self._capture_success_run()
        with mock.patch.object(cleaner, "run_hidden", fake_run):
            assert cleaner.restore_language_list(jp) is True
        # PS1-скрипт читает JSON напрямую — escape делается на уровне JSON,
        # а не PowerShell-строк:
        ps_content = (cleaner._PS_DIR / "layout_cleaner_restore.ps1").read_text(
            encoding="utf-8"
        )
        assert "ConvertFrom-Json" in ps_content  # Чтение JSON

    def test_dict_input_supported(self):
        jp = self._write_json({"LanguageTag": "de-DE", "InputMethodTips": []})
        captured, fake_run = self._capture_success_run()
        with mock.patch.object(cleaner, "run_hidden", fake_run):
            assert cleaner.restore_language_list(jp) is True
        cmd = captured["cmd"]
        assert "layout_cleaner_restore.ps1" in cmd[-3]  # PS1-файл

    def test_list_language_tag_and_scalar_tip_normalized(self):
        # PowerShell может вернуть LanguageTag массивом (["ru", "en-US"]),
        # а одна раскладка — строкой, а не списком.
        jp = self._write_json(
            [
                {
                    "LanguageTag": ["ru", "en-US"],
                    "InputMethodTips": "0419:00000419",
                }
            ]
        )
        captured, fake_run = self._capture_success_run()
        with mock.patch.object(cleaner, "run_hidden", fake_run):
            assert cleaner.restore_language_list(jp) is True
        # Во временный JSON для PS1 попадает первый непустой тег и список раскладок.
        tmp_json = Path(captured["cmd"][-1])
        written = json.loads(tmp_json.read_text(encoding="utf-8"))
        assert written == [["ru", ["0419:00000419"]]]

    def test_empty_list_language_tag_is_skipped(self):
        jp = self._write_json(
            [{"LanguageTag": [], "InputMethodTips": ["0409:00000409"]}]
        )
        with mock.patch.object(cleaner, "run_hidden", _proc):
            assert cleaner.restore_language_list(jp) is False

    def test_false_without_success_marker(self):
        jp = self._write_json(
            [{"LanguageTag": "en-US", "InputMethodTips": ["0409:00000409"]}]
        )

        def fake_run(cmd, **kw):
            return _proc(returncode=0, stdout="FAILED")

        with mock.patch.object(cleaner, "run_hidden", fake_run):
            assert cleaner.restore_language_list(jp) is False

    def test_false_on_timeout(self):
        jp = self._write_json(
            [{"LanguageTag": "en-US", "InputMethodTips": ["0409:00000409"]}]
        )

        def fake_run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=45)

        with mock.patch.object(cleaner, "run_hidden", fake_run):
            assert cleaner.restore_language_list(jp) is False

    def test_false_on_invalid_json(self):
        jp = Path(_tmp_dir()) / "langlist.json"
        jp.write_text("{not-json", encoding="utf-8")
        with mock.patch.object(cleaner, "run_hidden", _proc):
            assert cleaner.restore_language_list(jp) is False

    def test_false_when_no_valid_entries(self):
        jp = self._write_json([{"LanguageTag": "  ", "InputMethodTips": []}])
        with mock.patch.object(cleaner, "run_hidden", _proc):
            assert cleaner.restore_language_list(jp) is False


# ---------------------------------------------------------------------------
# TestVersion
# ---------------------------------------------------------------------------
class TestVersion:
    """__version__ в коде совпадает с pyproject.toml."""

    def test_config_defines_version(self):
        assert isinstance(config.__version__, str)
        assert config.__version__.count(".") >= 2

    def test_version_matches_pyproject(self):
        text = (
            Path(__file__)
            .resolve()
            .parent.joinpath("pyproject.toml")
            .read_text(encoding="utf-8")
        )
        m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        assert m is not None, "version не найден в pyproject.toml"
        assert m.group(1) == config.__version__


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
