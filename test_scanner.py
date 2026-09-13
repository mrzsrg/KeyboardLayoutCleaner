"""
test_scanner.py - Unit tests for scanner / cleaner / main (registry-free).

Mock-based using in-memory FakeWinreg and unittest.mock.
Markers: @pytest.mark.gui, @pytest.mark.live
"""
import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import pytest

import config
import cleaner
import scanner
import sys
import winproc

class FakeKey:
    """Registry key handle."""
    def __init__(self, root, path):
        self._root = root
        self._path = path
        self._closed = False
    def __enter__(self): return self
    def __exit__(self, *exc):
        self._closed = True
        return False
    def __bool__(self): return True

class FakeWinreg:
    """In-memory winreg for unit tests."""
    HKEY_CLASSES_ROOT = 0
    HKEY_CURRENT_USER = 1
    HKEY_LOCAL_MACHINE = 2
    HKEY_USERS = 3
    HKEY_CURRENT_CONFIG = 5
    KEY_READ = 0x20019
    KEY_WRITE = 0x20000
    KEY_ALL_ACCESS = 0xF003F
    REG_SZ = 1
    REG_BINARY = 3
    REG_NONE = 0
    REG_DWORD = 4
    REG_DWORD_BIG_ENDIAN = 5
    REG_EXPAND_SZ = 2
    REG_LINK = 6
    REG_MULTI_SZ = 7
    REG_QWORD = 11
    REG_QWORD_LITTLE_ENDIAN = 11

    def __init__(self):
        self.nodes = {}
        self.deny = set()

    @staticmethod
    def _norm(subkey):
        s = subkey.strip() if subkey else ""
        return s.rstrip("\\")

    @classmethod
    def _join(cls, base_path, sub):
        sub = cls._norm(sub)
        if not sub:
            return base_path
        sep = "\\"
        return base_path + sep + sub if base_path else sub

    def _infer_type(self, val):
        if isinstance(val, (list, tuple)):
            return self.REG_MULTI_SZ
        if isinstance(val, bool):
            return self.REG_DWORD
        if isinstance(val, int):
            return self.REG_DWORD
        if isinstance(val, bytes):
            return self.REG_BINARY
        return self.REG_SZ

    def _resolve(self, root, subkey_path):
        if isinstance(root, FakeKey):
            return root._root, self._join(root._path, subkey_path)
        return root, self._norm(subkey_path)

    def set(self, root, path, values=None, kids=None):
        """Create/overwrite a node, creating parent nodes."""
        path = self._norm(path)
        sep = "\\"
        sub_parts = path.split(sep) if path else []
        cur = ""
        for p in sub_parts:
            cur = p if not cur else cur + sep + p
            self.nodes.setdefault((root, cur), {"v": {}, "kids": set()})
            if sep in cur:
                parent = cur.rsplit(sep, 1)[0]
                pnode = self.nodes.get((root, parent))
                if pnode is not None:
                    pnode["kids"].add(p)
        node = self.nodes.setdefault((root, path), {"v": {}, "kids": set()})
        for name, val in (values or {}).items():
            node["v"][name] = (val, self._infer_type(val))
        for k in (kids or []):
            node["kids"].add(k)
            child = self._join(path, k)
            self.nodes.setdefault((root, child), {"v": {}, "kids": set()})
        return node

    def OpenKey(self, root, subkey_path, *args, **kwargs):
        root_key, path = self._resolve(root, subkey_path)
        if path in self.deny or f"{root_key}:" + path in self.deny:
            raise PermissionError(f"Access denied (test): {path}")
        node = self.nodes.get((root_key, path))
        if node is None:
            raise FileNotFoundError(f"No key (test): {path}")
        return FakeKey(root_key, path)

    def CreateKey(self, root, subkey_path, *args, **kwargs):
        root_key, path = self._resolve(root, subkey_path)
        sep = chr(92) + chr(92)
        sub_parts = path.split(sep) if path else []
        cur = ""
        for p in sub_parts:
            cur = p if not cur else cur + sep + p
            if (root_key, cur) not in self.nodes:
                self.nodes[(root_key, cur)] = {"v": {}, "kids": set()}
                if sep in cur:
                    parent = cur.rsplit(sep, 1)[0]
                    if (root_key, parent) in self.nodes:
                        pn = self.nodes[(root_key, parent)]
                        pn["kids"].add(p)
        self.nodes.setdefault((root_key, path), {"v": {}, "kids": set()})
        if path and sep in path:
            parent = path.rsplit(sep, 1)[0]
            name = path.rsplit(sep, 1)[1]
            if (root_key, parent) in self.nodes:
                self.nodes[(root_key, parent)]["kids"].add(name)
        return FakeKey(root_key, path)

    CreateKeyEx = CreateKey
    CloseKey = staticmethod(lambda key: None)
    FlushKey = staticmethod(lambda key: None)
    ExpandEnvironmentStrings = staticmethod(lambda s: s)

    def _node(self, key):
        if isinstance(key, FakeKey):
            return self.nodes.get((key._root, key._path))
        return None

    def QueryValueEx(self, key, name):
        node = self._node(key)
        if node is None or name not in node["v"]:
            raise FileNotFoundError(name)
        return node["v"][name]

    def SetValueEx(self, key, name, reserved, vtype, val):
        node = self._node(key)
        if node is None:
            raise FileNotFoundError(name)
        node["v"][name] = (val, vtype)

    def SetValue(self, key, name, vtype, val):
        self.SetValueEx(key, name, 0, vtype, val)

    def DeleteValue(self, key, name):
        node = self._node(key)
        if node is None or name not in node["v"]:
            raise OSError(f"No value (test): {name}")
        del node["v"][name]

    def EnumValue(self, key, idx):
        node = self._node(key)
        if node is None:
            raise OSError("No key (test)")
        items = list(node["v"].items())
        if idx >= len(items):
            raise OSError("No more data (test)")
        n, (val, vtype) = items[idx]
        return (n, val, vtype)

    def EnumKey(self, key, idx):
        node = self._node(key)
        if node is None:
            raise OSError("No key (test)")
        kids = sorted(node["kids"])
        if idx >= len(kids):
            raise OSError("No more data (test)")
        return kids[idx]

    def DeleteKey(self, root, subkey_path, *args, **kwargs):
        root_key, path = self._resolve(root, subkey_path)
        node = self.nodes.get((root_key, path))
        if node is None:
            raise FileNotFoundError(path)
        if node["v"] or node["kids"]:
            raise OSError("Key not empty (test)")
        del self.nodes[(root_key, path)]
        sep = chr(92) + chr(92)
        if sep in path:
            parent = path.rsplit(sep, 1)[0]
            name = path.rsplit(sep, 1)[1]
            pnode = self.nodes.get((root_key, parent))
            if pnode is not None:
                pnode["kids"].discard(name)
        elif path:
            pnode = self.nodes.get((root_key, ""))
            if pnode is not None:
                pnode["kids"].discard(path)



@pytest.fixture
def fake_winreg(monkeypatch):
    """Inject FakeWinreg into scanner and cleaner."""
    fw = FakeWinreg()
    monkeypatch.setattr(scanner, "winreg", fw)
    monkeypatch.setattr(cleaner, "winreg", fw)
    return fw


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
            fake_winreg.HKEY_CURRENT_USER, "Software\\Test",
            values={"foo": "bar", "baz": "qux"},
        )
        with _install_fake_winreg(fake_winreg):
            result = scanner._reg_get_string(
                scanner.winreg.HKEY_CURRENT_USER, "Software\\Test"
            )
        assert result == {"foo": "bar", "baz": "qux"}

    def test_specific_value(self, fake_winreg):
        fake_winreg.set(
            fake_winreg.HKEY_CURRENT_USER, "Software\\Test",
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
            fake_winreg.HKEY_CURRENT_USER, "Software\\Test",
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
            fake_winreg.HKEY_CURRENT_USER, "Keyboard Layout\\Preload",
            values={"1": "00000409", "2": "00000419"},
        )
        with _install_fake_winreg(fake_winreg):
            result = scanner._get_preload_keys(
                scanner.winreg.HKEY_CURRENT_USER, "Keyboard Layout\\Preload"
            )
        assert result == {"00000409": "00000409", "00000419": "00000419"}

    def test_empty_values_skipped(self, fake_winreg):
        fake_winreg.set(
            fake_winreg.HKEY_CURRENT_USER, "Keyboard Layout\\Preload",
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
            fake_winreg.HKEY_CURRENT_USER, "Keyboard Layout\\Substitutes",
            values={"00000419": "00000409"},
        )
        with _install_fake_winreg(fake_winreg):
            result = scanner._scan_substitutes(
                scanner.winreg.HKEY_CURRENT_USER, "Keyboard Layout\\Substitutes"
            )
        assert result == {"00000419": {"target": "00000409"}}

    def test_case_normalized(self, fake_winreg):
        fake_winreg.set(
            fake_winreg.HKEY_CURRENT_USER, "Keyboard Layout\\Substitutes",
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
            fake_winreg.HKEY_CURRENT_USER, "T\\A\\B\\C",
            values={"KeyboardLayout": "00000419"},
        )
        with _install_fake_winreg(fake_winreg):
            found = scanner._scan_branch_recursive(
                scanner.winreg.HKEY_CURRENT_USER, "T"
            )
        assert any(v == "00000419" for _, v in found)

    def test_tip_format(self, fake_winreg):
        fake_winreg.set(
            fake_winreg.HKEY_CURRENT_USER, "T2\\Sub",
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
            fake_winreg.HKEY_CURRENT_USER, "T3\\Allowed",
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
        monkeypatch.setattr(scanner.subprocess, "run",
                          _run_mock(returns=mock_result))
        entries = scanner._get_language_list_from_powershell()
        assert len(entries) == 1
        assert entries[0]["LanguageTag"] == "en-US"
        assert entries[0]["KeyboardLayoutId"] == "00000409"

    def test_empty_on_failure(self, monkeypatch):
        monkeypatch.setattr(scanner.subprocess, "run",
                          _run_mock(returns=_proc(returncode=1)))
        assert scanner._get_language_list_from_powershell() == []

    def test_timeout_returns_empty(self, monkeypatch):
        def raise_timeout(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="pw", timeout=15)
        monkeypatch.setattr(scanner.subprocess, "run",
                          mock.MagicMock(side_effect=raise_timeout))
        assert scanner._get_language_list_from_powershell() == []


class TestScanKeyboardLayouts:
    """scan_keyboard_layouts."""

    def test_empty_registry(self, fake_winreg, monkeypatch):
        monkeypatch.setattr(scanner, "winreg", fake_winreg)
        monkeypatch.setattr(scanner.subprocess, "run",
                          _run_mock(returns=_proc(stdout="")))
        assert scanner.scan_keyboard_layouts() == {}

    def test_finds_preload_layout(self, fake_winreg, monkeypatch):
        fake_winreg.set(
            fake_winreg.HKEY_CURRENT_USER, "Keyboard Layout\\Preload",
            values={"1": "00000409"},
        )
        monkeypatch.setattr(scanner, "winreg", fake_winreg)
        monkeypatch.setattr(scanner.subprocess, "run",
                          _run_mock(returns=_proc(stdout="")))
        result = scanner.scan_keyboard_layouts()
        assert "00000409" in result
        locs = result["00000409"]
        assert any("Preload" in loc["path"] for loc in locs)

    def test_finds_powershell_layout(self, fake_winreg, monkeypatch):
        monkeypatch.setattr(scanner, "winreg", fake_winreg)
        monkeypatch.setattr(
            scanner.subprocess, "run",
            _run_mock(returns=_proc(stdout="en-US\ten-US:00000409"))
        )
        result = scanner.scan_keyboard_layouts()
        assert "00000409" in result
        locs = result["00000409"]
        assert any("PowerShell" in loc["path"] for loc in locs)

    def test_invalid_klid_filtered(self, fake_winreg, monkeypatch):
        fake_winreg.set(
            fake_winreg.HKEY_CURRENT_USER, "Keyboard Layout\\Preload",
            values={"1": "not_a_klid", "2": "00000409"},
        )
        monkeypatch.setattr(scanner, "winreg", fake_winreg)
        monkeypatch.setattr(scanner.subprocess, "run",
                          _run_mock(returns=_proc(stdout="")))
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
        fake_winreg.set(
            fake_winreg.HKEY_CURRENT_USER, "EmptyKey"
        )
        with _install_fake_winreg(fake_winreg):
            result = cleaner._subtree_is_empty(
                cleaner.winreg.HKEY_CURRENT_USER, "EmptyKey"
            )
        assert result is True

    def test_non_empty_subtree(self, fake_winreg):
        fake_winreg.set(
            fake_winreg.HKEY_CURRENT_USER, "NonEmpty",
            values={"foo": "bar"}
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
            assert root == "HKCU"          # HKU/HKLM перенаправлены в HKCU sandbox
            assert admin_required is False  # права админа в sandbox не нужны

    def test_accessor_returns_real_branches_after_deactivate(self):
        scanner.activate_sandbox()
        scanner.deactivate_sandbox()
        branches = [tuple(b) for b in scanner.get_affected_branches()]
        assert ("HKCU", "Keyboard Layout\\Preload", "preload", False) in branches
        assert ("HKU", ".DEFAULT\\Keyboard Layout\\Preload", "preload", True) in branches

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
        assert all(
            s.startswith(sandbox) for s in cleaner._recursive_subkeys()
        )
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
        assert cleaner._report_field_for(
            "HKCU", sandbox + "\\Keyboard Layout\\Preload"
        ) == "hkcu_preload_deleted"
        assert cleaner._report_field_for(
            "HKCU", sandbox + "\\Keyboard Layout\\Substitutes"
        ) == "hkcu_substitutes_deleted"
        assert cleaner._report_field_for(
            "HKCU", sandbox + "\\Control Panel\\International\\User Profile"
        ) == "hkcu_intl_deleted"
        assert cleaner._report_field_for(
            "HKCU", sandbox + "\\Software\\Microsoft\\CTF"
        ) == "hkcu_ctf_deleted"
        assert cleaner._report_field_for(
            "HKCU", sandbox + "\\test\\.DEFAULT\\Keyboard Layout\\Preload"
        ) == "hku_default_deleted"
        # реальные ветки маппятся как раньше
        assert cleaner._report_field_for(
            "HKCU", "Keyboard Layout\\Preload"
        ) == "hkcu_preload_deleted"

    def test_backup_paths_are_sandbox_only(self):
        scanner.activate_sandbox()
        cleaner.activate_sandbox()
        paths = cleaner._paths_to_backup(cleaner._affected_branches(), admin_privileges=True)
        assert paths
        for entry in paths:
            assert entry["subkey"].startswith(config._SANDBOX_ROOT)
            assert entry["root"] == "HKCU"

    def test_backup_paths_real_mode_unchanged(self):
        scanner.deactivate_sandbox()
        cleaner.deactivate_sandbox()
        paths = cleaner._paths_to_backup(cleaner._affected_branches(), admin_privileges=False)
        subkeys = {e["subkey"] for e in paths}
        assert "Keyboard Layout\\Preload" in subkeys
        # без админ-прав ветка HKU\\.DEFAULT не бэкапится
        assert ".DEFAULT\\Keyboard Layout\\Preload" not in subkeys
        paths_admin = cleaner._paths_to_backup(
            cleaner._affected_branches(), admin_privileges=True
        )
        assert ".DEFAULT\\Keyboard Layout\\Preload" in {e["subkey"] for e in paths_admin}


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
        winproc.run_hidden(
            ["powershell", "-NoProfile", "-Command", "x"], timeout=5
        )
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
        assert entries and entries[0]["LanguageTag"] == "en-US"
        assert recorded, "run_hidden должен вызывать общий subprocess.run"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

