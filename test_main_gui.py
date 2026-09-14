"""
test_main_gui.py - GUI unit tests for KeyboardLayoutCleaner class.
Mock-based using in-memory mocks for customtkinter and unittest.mock.
Markers: @pytest.mark.gui - GUI tests (require mocked CTk and event loop)

Uses shared mocks from conftest.py:
- conftest._make_ctk() - mocked customtkinter
- conftest._make_gui_widgets_mock() - mocked gui_widgets
- conftest._ensure_main(monkeypatch) - clean main.py import
"""

import ctypes
import sys
from unittest import mock

# Import shared mocks from conftest (avoids duplication)
from conftest import _make_ctk
from conftest import _make_gui_widgets_mock as _make_gui_widgets


def _make_app(monkeypatch):
    """Helper: load main and create app instance."""
    import importlib

    monkeypatch.delitem(sys.modules, "main", raising=False)
    monkeypatch.delitem(sys.modules, "config", raising=False)
    monkeypatch.setitem(sys.modules, "customtkinter", _make_ctk())
    monkeypatch.setitem(sys.modules, "gui_widgets", _make_gui_widgets())
    os = __import__("os")
    os.environ.pop("SANDBOX_MODE", None)
    # Use reload to ensure fresh bytecode (avoids stale .pyc issues)
    main_mod = importlib.reload(__import__("main"))
    app = main_mod.KeyboardLayoutCleaner()
    return main_mod, app


def _ensure_main(monkeypatch):
    """Ensure main module is loaded with mocked dependencies."""
    import importlib

    monkeypatch.delitem(sys.modules, "main", raising=False)
    monkeypatch.delitem(sys.modules, "config", raising=False)
    monkeypatch.setitem(sys.modules, "customtkinter", _make_ctk())
    monkeypatch.setitem(sys.modules, "gui_widgets", _make_gui_widgets())
    os = __import__("os")
    os.environ.pop("SANDBOX_MODE", None)
    # Use reload to ensure fresh bytecode (avoids stale .pyc issues)
    return importlib.reload(__import__("main"))


# TestMainFunction
# ---------------------------------------------------------------------------
class TestMainFunction:
    """Tests for main() entry point."""

    def test_main_creates_app(self, monkeypatch):
        """main() creates KeyboardLayoutCleaner and starts mainloop."""
        _ensure_main(monkeypatch)
        import main

        app_instance = None

        def mock_mainloop(self):
            nonlocal app_instance
            app_instance = self

        monkeypatch.setattr(main.KeyboardLayoutCleaner, "mainloop", mock_mainloop)
        with mock.patch.object(main, "_acquire_mutex", return_value=(1, 0)):
            with mock.patch.object(main, "parse_args") as mock_parse:
                mock_parse.return_value = mock.MagicMock(sandbox=False)
                main.main()

        assert app_instance is not None
        assert isinstance(app_instance, main.KeyboardLayoutCleaner)

    def test_main_with_sandbox(self, monkeypatch):
        """main() activates sandbox when flag is set."""
        _ensure_main(monkeypatch)
        import main

        mock_scanner = mock.MagicMock()
        mock_cleaner = mock.MagicMock()
        monkeypatch.setitem(sys.modules, "scanner", mock_scanner)
        monkeypatch.setitem(sys.modules, "cleaner", mock_cleaner)

        with mock.patch.object(main, "_acquire_mutex", return_value=(1, 0)):
            with mock.patch.object(main, "parse_args") as mock_parse:
                mock_args = mock.MagicMock()
                mock_args.sandbox = True
                mock_parse.return_value = mock_args
                main.main()

                mock_scanner.activate_sandbox.assert_called_once()
                mock_cleaner.activate_sandbox.assert_called_once()


# ---------------------------------------------------------------------------
# TestKeyboardLayoutCleanerInit
# ---------------------------------------------------------------------------
class TestKeyboardLayoutCleanerInit:
    """Tests for KeyboardLayoutCleaner initialization."""

    def test_layouts_data_initialized(self, monkeypatch):
        """layouts_data initialized as empty dict."""
        _main_mod, app = _make_app(monkeypatch)
        assert isinstance(app.layouts_data, dict)
        assert len(app.layouts_data) == 0

    def test_current_layout_klid_initialized(self, monkeypatch):
        """current_layout_klid initialized as empty string."""
        _main_mod, app = _make_app(monkeypatch)
        assert app.current_layout_klid == ""

    def test_scan_button_exists(self, monkeypatch):
        """Scan button created during initialization."""
        _main_mod, app = _make_app(monkeypatch)
        assert hasattr(app, "scan_button")
        assert app.scan_button is not None

    def test_detail_text_exists(self, monkeypatch):
        """Detail text widget created."""
        _main_mod, app = _make_app(monkeypatch)
        assert hasattr(app, "detail_text")

    def test_layouts_list_frame_exists(self, monkeypatch):
        """Layouts list frame created."""
        _main_mod, app = _make_app(monkeypatch)
        assert hasattr(app, "layouts_list_frame")

    def test_stage_initialized(self, monkeypatch):
        """Stage initialized to 'start'."""
        _main_mod, app = _make_app(monkeypatch)
        assert app._stage == "start"

    def test_pulse_state_initialized(self, monkeypatch):
        """_pulse_state initialized to None."""
        _main_mod, app = _make_app(monkeypatch)
        assert hasattr(app, "_pulse_state")
        assert hasattr(app, "_pulse_job")


# ---------------------------------------------------------------------------
# TestScanLayouts
# ---------------------------------------------------------------------------
class TestScanLayouts:
    """Tests for scan flow methods."""

    def test_on_scan_calls_scanner(self, monkeypatch):
        """_on_scan calls scanner.scan_keyboard_layouts in a thread."""
        main_mod, app = _make_app(monkeypatch)

        mock_scanner_data = {
            "04190419": [{"path": r"HKCU\Software\Microsoft\CTF\{GUID}"}],
        }
        mock_scanner = mock.MagicMock()
        mock_scanner.scan_keyboard_layouts.return_value = mock_scanner_data
        monkeypatch.setattr(
            main_mod, "scan_keyboard_layouts", mock_scanner.scan_keyboard_layouts
        )

        app._on_scan()
        import time

        time.sleep(0.5)

        # Verify scanner was called
        mock_scanner.scan_keyboard_layouts.assert_called_once()

    def test_apply_scan_results_populates_data(self, monkeypatch):
        """_apply_scan_results populates layouts_data."""
        _main_mod, app = _make_app(monkeypatch)

        mock_scan_data = {
            "04190419": [{"path": r"HKCU\Software\Microsoft\CTF\{GUID}"}],
            "04090409": [{"path": r"HKCU\Keyboard Layout\Preload\1"}],
        }
        app._apply_scan_results(mock_scan_data)

        assert len(app.layouts_data) == 2
        assert "04190419" in app.layouts_data
        assert "04090409" in app.layouts_data

    def test_render_layouts_list_creates_rows(self, monkeypatch):
        """_render_layouts_list creates row buttons for each layout."""
        _main_mod, app = _make_app(monkeypatch)

        app.layouts_data = {
            "04190419": [{"path": r"HKCU\Software\Microsoft\CTF\{GUID}"}],
            "04090409": [{"path": r"HKCU\Keyboard Layout\Preload\1"}],
        }

        mock_frame = mock.MagicMock()
        mock_frame.winfo_children.return_value = []
        app.layouts_list_frame = mock_frame
        app._layout_row_buttons = {}

        app._render_layouts_list()

        assert len(app._layout_row_buttons) == 2
        assert "04190419" in app._layout_row_buttons
        assert "04090409" in app._layout_row_buttons

    def test_render_layouts_list_empty(self, monkeypatch):
        """_render_layouts_list handles empty data."""
        _main_mod, app = _make_app(monkeypatch)

        app.layouts_data = {}
        mock_frame = mock.MagicMock()
        mock_frame.winfo_children.return_value = []
        app.layouts_list_frame = mock_frame
        app._layout_row_buttons = {}

        app._render_layouts_list()
        assert app._layout_row_buttons == {}


# ---------------------------------------------------------------------------
# TestLayoutDetails
# ---------------------------------------------------------------------------
class TestLayoutDetails:
    """Tests for layout detail display methods."""

    def test_show_layout_details_with_data(self, monkeypatch):
        """_show_layout_details shows layout details."""
        _main_mod, app = _make_app(monkeypatch)
        app.layouts_data = {
            "04190419": [{"path": r"HKCU\Software\Microsoft\CTF\{GUID}"}]
        }
        mock_text = mock.MagicMock()
        mock_text.configure = mock.MagicMock()
        mock_text.insert = mock.MagicMock()
        mock_text.delete = mock.MagicMock()
        app.detail_text = mock_text
        app._show_layout_details("04190419")
        assert mock_text.insert.called or mock_text.configure.called

    def test_show_layout_details_clears_old(self, monkeypatch):
        """_show_layout_details clears old content."""
        _main_mod, app = _make_app(monkeypatch)
        app.layouts_data = {
            "04190419": [{"path": r"HKCU\Software\Microsoft\CTF\{GUID}"}]
        }
        mock_text = mock.MagicMock()
        mock_text.configure = mock.MagicMock()
        mock_text.delete = mock.MagicMock()
        app.detail_text = mock_text
        app._show_layout_details("04190419")
        assert mock_text.delete.called


# ---------------------------------------------------------------------------
# TestOnLayoutClicked
# ---------------------------------------------------------------------------
class TestOnLayoutClicked:
    """Tests for _on_layout_clicked method."""

    def test_sets_selection(self, monkeypatch):
        """_on_layout_clicked sets selected layout."""
        _main_mod, app = _make_app(monkeypatch)
        app.layouts_data = {"04190419": [{"path": "test"}]}
        mock_text = mock.MagicMock()
        app.detail_text = mock_text
        mock_btn = mock.MagicMock()
        app.delete_button = mock_btn
        app._on_layout_clicked("04190419")
        assert app.current_layout_klid == "04190419"

    def test_activates_delete_button(self, monkeypatch):
        """_on_layout_clicked activates delete button."""
        _main_mod, app = _make_app(monkeypatch)
        app.layouts_data = {"04190419": []}
        mock_btn = mock.MagicMock()
        app.delete_button = mock_btn
        app._on_layout_clicked("04190419")
        mock_btn.configure.assert_any_call(state="normal")

    def test_ignores_unknown_klid(self, monkeypatch):
        """_on_layout_clicked ignores unknown KLID."""
        _main_mod, app = _make_app(monkeypatch)
        app.layouts_data = {"04190419": []}
        mock_btn = mock.MagicMock()
        app.delete_button = mock_btn
        app._on_layout_clicked("FFFFFFFF")
        mock_btn.configure.assert_not_called()


# ---------------------------------------------------------------------------
# TestActiveLangCache
# ---------------------------------------------------------------------------
class TestActiveLangCache:
    """Tests for _active_lang_cache function."""

    def test_cache_true(self, monkeypatch):
        """_active_lang_cache returns True for active cache."""
        _ensure_main(monkeypatch)
        import main

        data = {
            "04190419": [
                {
                    "path": r"HKCU\Software\Microsoft\CTF\Assemblies\0x00000419\{GUID}\KeyboardLayout"
                }
            ],
            "00000419": [{"path": r"PowerShell\Get-WinUserLanguageList"}],
        }
        assert main._active_lang_cache("04190419", data) is True

    def test_cache_false_preload(self, monkeypatch):
        """_active_lang_cache returns False with Preload."""
        _ensure_main(monkeypatch)
        import main

        data = {
            "04190419": [
                {"path": r"HKCU\Software\Microsoft\CTF\Assemblies\{GUID}"},
                {"path": r"HKCU\Keyboard Layout\Preload\1"},
            ],
            "00000419": [{"path": r"PowerShell\Get-WinUserLanguageList"}],
        }
        assert main._active_lang_cache("04190419", data) is False

    def test_cache_no_ps_pair(self, monkeypatch):
        """_active_lang_cache returns False without PS pair."""
        _ensure_main(monkeypatch)
        import main

        data = {
            "04190419": [{"path": r"HKCU\Software\Microsoft\CTF\Assemblies\{GUID}"}]
        }
        assert main._active_lang_cache("04190419", data) is False

    def test_cache_invalid_klid(self, monkeypatch):
        """_active_lang_cache returns False for invalid KLID."""
        _ensure_main(monkeypatch)
        import main

        assert main._active_lang_cache("zz-not-hex", {}) is False

    def test_cache_zero_langid(self, monkeypatch):
        """_active_lang_cache returns False for zero langid."""
        _ensure_main(monkeypatch)
        import main

        data = {"00000000": [{"path": r"HKCU\Software\Microsoft\CTF"}]}
        assert main._active_lang_cache("00000000", data) is False


# ---------------------------------------------------------------------------
# TestLayoutUtils
# ---------------------------------------------------------------------------
class TestLayoutUtils:
    """Tests for layout utility functions."""

    def test_build_name_with_name(self, monkeypatch):
        """_build_layout_name returns name with KLID."""
        _ensure_main(monkeypatch)
        import main

        with mock.patch.object(main, "get_layout_name", return_value="Russian"):
            assert main._build_layout_name("04190419") == "Russian (04190419)"

    def test_build_name_no_name(self, monkeypatch):
        """_build_layout_name falls back to default name."""
        _ensure_main(monkeypatch)
        import main

        with mock.patch.object(main, "get_layout_name", return_value=""):
            result = main._build_layout_name("d001dead")
            assert "d001dead" in result


# ---------------------------------------------------------------------------
# TestApplySandboxToModules
# ---------------------------------------------------------------------------
class TestApplySandboxToModules:
    """Tests for _apply_sandbox_to_modules function."""

    def test_enable_sandbox(self, monkeypatch):
        """_apply_sandbox_to_modules(True) enables sandbox in config."""
        _ensure_main(monkeypatch)
        import config as cfg
        import main

        cfg.disable_sandbox()
        main._apply_sandbox_to_modules(True)
        assert cfg.is_sandbox_enabled() is True
        main._apply_sandbox_to_modules(False)

    def test_disable_sandbox(self, monkeypatch):
        """_apply_sandbox_to_modules(False) disables sandbox."""
        _ensure_main(monkeypatch)
        import config as cfg
        import main

        cfg.enable_sandbox()
        main._apply_sandbox_to_modules(False)
        assert cfg.is_sandbox_enabled() is False

    def test_sets_env_var(self, monkeypatch):
        """_apply_sandbox_to_modules sets SANDBOX_MODE env var."""
        _ensure_main(monkeypatch)
        import main

        os = __import__("os")
        os.environ.pop("SANDBOX_MODE", None)
        main._apply_sandbox_to_modules(True)
        assert os.environ.get("SANDBOX_MODE") == "1"
        main._apply_sandbox_to_modules(False)


# ---------------------------------------------------------------------------
# TestParseArgs
# ---------------------------------------------------------------------------
class TestParseArgs:
    """Tests for parse_args function."""

    def test_with_sandbox(self, monkeypatch):
        """parse_args with --sandbox."""
        _ensure_main(monkeypatch)
        import main

        args = main.parse_args(["--sandbox"])
        assert args.sandbox is True

    def test_no_sandbox_default(self, monkeypatch):
        """parse_args without --sandbox defaults to False."""
        _ensure_main(monkeypatch)
        import main

        args = main.parse_args([])
        assert args.sandbox is False


# ---------------------------------------------------------------------------
# TestAcquireMutex
# ---------------------------------------------------------------------------
class TestAcquireMutex:
    """Tests for _acquire_mutex function."""

    def test_success(self, monkeypatch):
        """_acquire_mutex successfully creates mutex."""
        _ensure_main(monkeypatch)
        import main

        mock_kernel32 = mock.MagicMock()
        mock_kernel32.CreateMutexW.return_value = 42
        mock_kernel32.GetLastError.return_value = 0
        monkeypatch.setattr(ctypes.windll, "kernel32", mock_kernel32)
        handle, error = main._acquire_mutex()
        assert handle == 42
        assert error == 0

    def test_already_exists_with_elevate(self, monkeypatch):
        """_acquire_mutex handles ERROR_ALREADY_EXISTS при is_elevate=True."""
        _ensure_main(monkeypatch)
        import main
        import mutex

        mock_kernel32 = mock.MagicMock()
        mock_kernel32.CreateMutexW.return_value = 42
        mock_kernel32.GetLastError.return_value = 183
        monkeypatch.setattr(ctypes.windll, "kernel32", mock_kernel32)
        monkeypatch.setattr(mutex.time, "sleep", lambda *a, **k: None)
        mock_kernel32.CreateMutexW.side_effect = [42, 42]
        mock_kernel32.GetLastError.side_effect = [183, 0]
        handle, error = main._acquire_mutex(is_elevate=True)
        assert handle == 42
        assert error == 0


# ---------------------------------------------------------------------------
# TestRunAsAdmin
# ---------------------------------------------------------------------------
class TestRunAsAdmin:
    """Tests for _restart_as_admin method."""

    def test_calls_shellexecute(self, monkeypatch):
        """_restart_as_admin calls ShellExecuteW."""
        main_mod, app = _make_app(monkeypatch)
        mock_shell32 = mock.MagicMock()
        mock_shell32.ShellExecuteW.return_value = 42
        monkeypatch.setattr(main_mod.ctypes.windll, "shell32", mock_shell32)
        with mock.patch.object(main_mod.sys, "exit"):
            with mock.patch.object(main_mod.threading, "Thread"):
                app._restart_as_admin()
        mock_shell32.ShellExecuteW.assert_called()


# ---------------------------------------------------------------------------
# TestConstants
# ---------------------------------------------------------------------------
class TestConstants:
    """Tests for main.py constants."""

    def test_pulse_interval(self, monkeypatch):
        _ensure_main(monkeypatch)
        import main

        assert isinstance(main.PULSE_INTERVAL_MS, int)
        assert main.PULSE_INTERVAL_MS > 0

    def test_poll_interval(self, monkeypatch):
        _ensure_main(monkeypatch)
        import main

        assert isinstance(main.POLL_INTERVAL_MS, int)
        assert main.POLL_INTERVAL_MS > 0

    def test_mutex_name(self, monkeypatch):
        _ensure_main(monkeypatch)
        import main

        assert isinstance(main._MUTEX_NAME, str)
        assert "Global" in main._MUTEX_NAME

    def test_hklm_path(self, monkeypatch):
        _ensure_main(monkeypatch)
        import main

        assert isinstance(main.HKLM_CATALOG_PATH, str)
        assert "Keyboard Layouts" in main.HKLM_CATALOG_PATH

    def test_version(self, monkeypatch):
        _ensure_main(monkeypatch)
        import main

        assert isinstance(main.app_version, str)
        assert len(main.app_version) > 0
