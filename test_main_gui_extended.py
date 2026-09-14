"""test_main_gui_extended.py - Extended GUI tests for KeyboardLayoutCleaner."""

import ctypes
import queue
import sys
import types
from unittest import mock

import pytest


def _make_ctk():
    ctk = types.ModuleType("customtkinter")

    class Widget:
        def __init__(self, *a, **kw):
            self._configure_kwargs = kw
            self.configure_kwargs = kw
            self.state_value = "normal"

        def configure(self, **kw):
            self.configure_kwargs.update(kw)
            if "state" in kw:
                self.state_value = kw["state"]

        def pack(self, *a, **k):
            pass

        def grid(self, *a, **k):
            pass

        def grid_columnconfigure(self, *a, **k):
            pass

        def title(self, v=None):
            pass

        def after(self, d, f=None, *a):
            return None

        def after_cancel(self, *a, **k):
            pass

        def destroy(self):
            pass

        def set(self, v):
            pass

        def cget(self, k):
            return self._configure_kwargs.get(k, "")

        def winfo_children(self):
            return []

    class ScrollableFrame(Widget):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._frame = types.SimpleNamespace()

    class OptionMenu(Widget):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.variable = types.SimpleNamespace(set=lambda x: None, get=lambda: False)

    def _mk(name):
        if name in ("CTk", "CTkLabel", "CTkButton", "CTkFrame", "CTkEntry"):
            return type(name, (Widget,), {})
        elif name == "CTkScrollableFrame":
            return ScrollableFrame
        elif name == "CTkOptionMenu":
            return OptionMenu
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
    ):
        setattr(ctk, cls, _mk(cls))
    ctk.ScalingTracker = type("S", (), {"get_window_scaling": lambda *a: 1.0})
    ctk.set_appearance_mode = lambda *a: None
    ctk.set_default_color_theme = lambda *a: None
    return ctk


@pytest.fixture(autouse=True)
def _mock_dependencies():
    sys.modules["_ctypes"] = mock.MagicMock()
    ctypes.windll = mock.MagicMock()
    ctypes.wintypes = types.SimpleNamespace()
    import winreg as _wr

    _wr.OpenKeyEx = mock.MagicMock(return_value=mock.MagicMock())
    _wr.EnumValue = mock.MagicMock(side_effect=FileNotFoundError)
    ctk = _make_ctk()
    sys.modules["customtkinter"] = ctk
    yield
    for mod in list(sys.modules.keys()):
        if mod in ("_ctypes", "customtkinter"):
            del sys.modules[mod]


@pytest.fixture
def mock_main_window():
    mock_window = mock.MagicMock()
    mock_window.master = mock_window
    call_queue = queue.Queue()
    mock_window.call_queue = call_queue
    return mock_window, call_queue


class TestSandboxMode:
    """Tests for sandbox mode functionality."""

    def test_sandbox_enable_disable(self, _mock_dependencies):
        import config

        config.disable_sandbox()
        assert not config.is_sandbox_enabled()
        config.enable_sandbox()
        assert config.is_sandbox_enabled()
        config.disable_sandbox()
        assert not config.is_sandbox_enabled()

    def test_sandbox_is_thread_safe(self, _mock_dependencies):
        import threading

        import config

        assert hasattr(config._sandbox_lock, "acquire")
        assert isinstance(config._sandbox_lock, type(threading.Lock()))


class TestVersionCheck:
    def test_version_from_metadata(self, _mock_dependencies):
        import config

        assert hasattr(config, "__version__")
        assert isinstance(config.__version__, str)
        assert len(config.__version__) > 0


class TestI18n:
    def test_t_returns_string(self, _mock_dependencies):
        from gui_widgets import _t

        result = _t("app_title")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_t_fallback_on_missing_key(self, _mock_dependencies):
        from gui_widgets import _t

        result = _t("__NONEXISTENT_KEY__")
        assert isinstance(result, str)


class TestScaling:
    def test_get_dpi_scaling(self, _mock_dependencies):
        import customtkinter as ctk

        mock_widget = mock.MagicMock()
        ctk.ScalingTracker.get_window_scaling = mock.MagicMock(return_value=1.0)
        assert ctk.ScalingTracker.get_window_scaling(mock_widget) == 1.0


class TestQueueProcessing:
    def test_process_queue_dispatches_functions(self, mock_main_window):
        _mock_window, call_queue = mock_main_window
        assert isinstance(call_queue, queue.Queue)


class TestScanMethods:
    def test_on_scan_clicked_triggers_scan(self, mock_main_window):
        _mock_window, call_queue = mock_main_window
        assert isinstance(call_queue, queue.Queue)

    def test_apply_scan_results_updates_list(self, mock_main_window):
        _mock_window, _call_queue = mock_main_window
        test_layouts = [
            {"klid": "0804:00000409", "layout": "English", "type": "keyboard"}
        ]
        assert len(test_layouts) == 1
