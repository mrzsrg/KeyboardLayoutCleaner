"""
conftest.py - General pytest fixtures for Keyboard Layout Cleaner.
"""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

import pytest


class FakeKey:
    """Registry key handle for FakeWinreg."""

    def __init__(self, root, path):
        self._root = root
        self._path = path
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._closed = True

    def __bool__(self):
        return not self._closed


class FakeWinreg:
    """In-memory winreg for unit tests without real registry access."""

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
        return subkey.strip().rstrip("\\") if subkey else ""

    @classmethod
    def _join(cls, base_path, sub):
        sub = cls._norm(sub)
        return (
            base_path if not sub else (base_path + "\\" + sub if base_path else sub)
        )

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
        for k in kids or []:
            node["kids"].add(k)
            self.nodes.setdefault((root, self._join(path, k)), {"v": {}, "kids": set()})
        return node

    def OpenKey(self, root, subkey_path, *args, **kwargs):  # noqa: N802
        root_key, path = self._resolve(root, subkey_path)
        if (root_key, path) in self.deny:
            raise OSError("Access denied")
        if (root_key, path) not in self.nodes:
            raise FileNotFoundError(f"Key not found: {path}")
        return FakeKey(root_key, path)

    def QueryValueEx(self, root, name):  # noqa: N802
        root_key, path = self._resolve(root, "" if isinstance(root, FakeKey) else root)
        node = self.nodes.get((root_key, path))
        if not node or name not in node["v"]:
            raise FileNotFoundError(f"Value not found: {name}")
        return node["v"][name]

    def SetValueEx(self, root, name, reserved, val_type, value):  # noqa: N802
        root_key, path = self._resolve(root, "" if isinstance(root, FakeKey) else root)
        node = self.nodes.setdefault((root_key, path), {"v": {}, "kids": set()})
        node["v"][name] = (value, val_type)

    def CreateKeyEx(self, root, subkey_path, *args, **kwargs):  # noqa: N802
        root_key, path = self._resolve(root, subkey_path)
        self.nodes.setdefault((root_key, path), {"v": {}, "kids": set()})
        return FakeKey(root_key, path)

    def DeleteKey(self, root, subkey_path):  # noqa: N802
        self.nodes.pop(
            (self._resolve(root, subkey_path)[0], self._resolve(root, subkey_path)[1]),
            None,
        )

    def EnumValue(self, key, idx):  # noqa: N802
        if isinstance(key, FakeKey):
            root, path = key._root, key._path
        else:
            root, path = self._resolve(key, "")
        node = self.nodes.get((root, path))
        if node is None:
            raise OSError("No key (test)")
        items = list(node["v"].items())
        if idx >= len(items):
            raise OSError("No more data (test)")
        n, (val, vtype) = items[idx]
        return (n, val, vtype)

    def EnumKey(self, key, idx):  # noqa: N802
        if isinstance(key, FakeKey):
            root, path = key._root, key._path
        else:
            root, path = self._resolve(key, "")
        node = self.nodes.get((root, path))
        if node is None:
            raise OSError("No key (test)")
        kids = sorted(node["kids"])
        if idx >= len(kids):
            raise OSError("No more data (test)")
        return kids[idx]


def _fake_proc(returncode=0, stdout="", stderr=""):
    """Create a CompletedProcess object for subprocess.run mocking."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _tmp_dir():
    """Create a temp directory for tests."""
    return Path(tempfile.mkdtemp(prefix="klc_test_"))


def _write_json(data, tmp_dir=None):
    """Write JSON to a temp file and return path."""
    if tmp_dir is None:
        tmp_dir = _tmp_dir()
    path = tmp_dir / "test_data.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _make_ctk():  # noqa: C901 - тестовый мок customtkinter: намеренно всеобъемлющий
    """Create a mock customtkinter module with full widget support.

    Includes configure() state tracking, grid/pack, textbox tags,
    optionmenu/switch variables, and progressbar methods.
    """
    ctk = types.ModuleType("customtkinter")

    class Widget:
        """Base widget mock with configure tracking and geometry methods."""

        def __init__(self, *args, **kwargs):
            self._configure_kwargs = kwargs
            self.configure_kwargs = kwargs
            self.state_value = "normal"

        def configure(self, **kwargs):
            self.configure_kwargs.update(kwargs)
            if "state" in kwargs:
                self.state_value = kwargs["state"]

        def pack(self, *args, **kwargs):
            pass

        def pack_propagate(self, *args, **kwargs):
            pass

        def grid(self, *args, **kwargs):
            pass

        def grid_columnconfigure(self, *args, **kwargs):
            pass

        def grid_rowconfigure(self, *args, **kwargs):
            pass

        def title(self, value=None):
            pass

        def geometry(self, value=None):
            pass

        def minsize(self, *a, **k):
            pass

        def maxsize(self, *a, **k):
            pass

        def after(self, delay, func=None, *args):
            return None

        def after_cancel(self, *a, **k):
            pass

        def mainloop(self):
            pass

        def iconbitmap(self, *a, **k):
            pass

        def destroy(self):
            pass

        def set(self, value):
            pass

        def cget(self, key):
            return self._configure_kwargs.get(key, "")

        def winfo_children(self):
            return []

        def withdraw(self):
            pass

        def focus_force(self):
            pass

    class ScrollableFrame(Widget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._frame = types.SimpleNamespace()

    class Textbox(Widget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._tags = {}
            self._textbox = mock.MagicMock()

        def tag_config(self, name, **kwargs):
            self._tags[name] = kwargs

        def insert(self, *args, **kwargs):
            pass

        def delete(self, *args, **kwargs):
            pass

        def see(self, *args, **kwargs):
            pass

    class OptionMenu(Widget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.variable = types.SimpleNamespace(set=lambda x: None, get=lambda: "")

    class Progressbar(Widget):
        def start(self, *a, **k):
            pass

        def stop(self, *a, **k):
            pass

        def step(self, *a, **k):
            pass

    class Checkbox(Widget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.variable = types.SimpleNamespace(set=lambda x: None, get=lambda: False)

    class Font(Widget):
        def __init__(self, family="", size=10, weight="normal", slant="roman"):
            super().__init__()
            self.family, self.size, self.weight, self.slant = (
                family,
                size,
                weight,
                slant,
            )

    def _mk(name):
        if name == "CTk":
            return type("CTk", (Widget,), {})
        elif name == "CTkScrollableFrame":
            return ScrollableFrame
        elif name == "CTkTextbox":
            return Textbox
        elif name == "CTkOptionMenu":
            return OptionMenu
        elif name == "CTkProgressbar":
            return Progressbar
        elif name == "CTkSwitch":
            return Checkbox
        elif name == "CTkFont":
            return Font
        else:
            return type(name, (Widget,), {})

    for cls in (
        "CTk",
        "CTkLabel",
        "CTkButton",
        "CTkFrame",
        "CTkEntry",
        "CTkScrollableFrame",
        "CTkFont",
        "CTkOptionMenu",
        "CTkSwitch",
        "CTkProgressbar",
        "CTkTextbox",
        "CTkCheckBox",
    ):
        setattr(ctk, cls, _mk(cls))

    ctk.ScalingTracker = type("S", (), {"get_window_scaling": lambda *a: 1.0})
    ctk.set_appearance_mode = lambda *a: None
    ctk.set_default_color_theme = lambda *a: None
    for fn in (
        "deactivate_automatic_dpi_awareness",
        "enable_undocked_window_scaling",
        "deactivate_automatic_high_dpi_awareness",
    ):
        setattr(ctk, fn, lambda *a: None)

    return ctk


def _make_gui_widgets_mock():
    """Create a mock gui_widgets module for main.py tests.

    Uses MagicMock for sub-widgets so methods like configure/pack/grid
    are available without explicit definition.
    """
    gw = types.ModuleType("gui_widgets")

    class MockStatusBar:
        def __init__(self, *a, **k):
            self.label = mock.MagicMock()
            self.progress_bar = mock.MagicMock()
            self.progress_start = lambda: None
            self.progress_stop = lambda: None
            self.set_status = lambda x: None

        def pack(self, *a, **k):
            pass

        def pack_propagate(self, *a, **k):
            pass

    class MockActionButtons:
        def __init__(
            self, *a, on_delete=None, on_restore=None, on_toggle_sync=None, **k
        ):
            self.delete_btn = mock.MagicMock()
            self.restore_btn = mock.MagicMock()
            self.block_sync_checkbox = mock.MagicMock()

        def pack(self, *a, **k):
            pass

    class MockAdminBanner:
        def __init__(
            self,
            *a,
            on_restart_admin=None,
            on_lang_change=None,
            on_toggle_theme=None,
            on_brightness_change=None,
            **k,
        ):
            self.icon_label = mock.MagicMock()
            self.status_label = mock.MagicMock()
            self.restart_btn = mock.MagicMock()
            self.lang_menu = mock.MagicMock()
            self.update_status = lambda x: None

        def pack(self, *a, **k):
            pass

    gw.StatusBar = MockStatusBar
    gw.ActionButtons = MockActionButtons
    gw.AdminBanner = MockAdminBanner
    return gw


def _ensure_main(monkeypatch):
    """Load main.py with mocked dependencies and module cache clearing."""
    import importlib

    monkeypatch.delitem(sys.modules, "main", raising=False)
    monkeypatch.delitem(sys.modules, "config", raising=False)
    monkeypatch.setitem(sys.modules, "customtkinter", _make_ctk())
    monkeypatch.setitem(sys.modules, "gui_widgets", _make_gui_widgets_mock())
    os.environ.pop("SANDBOX_MODE", None)
    # Use reload to ensure fresh bytecode (avoids stale .pyc issues)
    main_mod = __import__("main")
    return importlib.reload(main_mod)


# Pytest fixtures
@pytest.fixture(autouse=True)
def _cleanup_sandbox_state():
    """Auto-cleanup SANDBOX_MODE after each test."""
    yield
    os.environ.pop("SANDBOX_MODE", None)
    try:
        import config as _cfg

        _cfg.disable_sandbox()
    except (ImportError, AttributeError):
        pass


@pytest.fixture
def mock_ctk(monkeypatch):
    """Provide a mock customtkinter module and inject into sys.modules.

    Returns the mock module instance for assertions.
    """
    ctk_mock = _make_ctk()
    monkeypatch.setitem(sys.modules, "customtkinter", ctk_mock)
    return ctk_mock


@pytest.fixture
def mock_gui_widgets(monkeypatch):
    """Provide a mock gui_widgets module and inject into sys.modules.

    Returns the mock module instance for assertions.
    """
    gw_mock = _make_gui_widgets_mock()
    monkeypatch.setitem(sys.modules, "gui_widgets", gw_mock)
    return gw_mock


@pytest.fixture
def fake_winreg(monkeypatch):
    """Create in-memory FakeWinreg and inject into scanner/cleaner."""
    fw = FakeWinreg()
    monkeypatch.setattr("scanner.winreg", fw)
    monkeypatch.setattr("cleaner.winreg", fw)
    return fw


@pytest.fixture
def fake_proc_factory():
    """Factory for fake subprocess.CompletedProcess."""
    return _fake_proc


@pytest.fixture
def tmp_test_dir():
    """Temp directory for test files. Automatically cleaned."""
    path = _tmp_dir()
    yield path
    import shutil

    with contextlib.suppress(OSError):
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def mock_logger(monkeypatch):
    """Create a mock logger for tests."""
    logger = mock.MagicMock()
    monkeypatch.setattr("logging.getLogger", lambda name: logger)
    return logger


@pytest.fixture
def clean_import_main(monkeypatch):
    """Fixture for clean import of main.py."""
    return lambda: _ensure_main(monkeypatch)


@pytest.fixture
def patched_winreg(monkeypatch, fake_winreg):
    """Replace winreg in scanner/cleaner with FakeWinreg."""
    monkeypatch.setattr("winreg", fake_winreg)
    monkeypatch.setattr("scanner.winreg", fake_winreg)
    monkeypatch.setattr("cleaner.winreg", fake_winreg)
    return fake_winreg
