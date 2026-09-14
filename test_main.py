"""
test_main.py — Unit tests for main.py functions and KeyboardLayoutCleaner GUI.

Mock-based using in-memory mocks for customtkinter and unittest.mock.
Markers: @pytest.mark.gui — GUI tests (require mocked CTk and event loop)

Uses shared fixtures from conftest.py:
- mock_ctk / mock_gui_widgets — mocked customtkinter and gui_widgets modules
- clean_import_main — clean import of main.py
"""

import os
import sys

# Import shared mocks from conftest (avoids duplication across test files)
from conftest import _make_ctk, _make_gui_widgets_mock


def _ensure_main(monkeypatch):
    """Ensure main module is loaded with mocked dependencies.

    Uses shared mocks from conftest.py.
    """
    import importlib

    monkeypatch.delitem(sys.modules, "main", raising=False)
    monkeypatch.delitem(sys.modules, "config", raising=False)
    monkeypatch.delitem(sys.modules, "scanner", raising=False)
    monkeypatch.delitem(sys.modules, "cleaner", raising=False)
    monkeypatch.setitem(sys.modules, "customtkinter", _make_ctk())
    monkeypatch.setitem(sys.modules, "gui_widgets", _make_gui_widgets_mock())
    os.environ.pop("SANDBOX_MODE", None)
    # Mock _check_windows_version so tests don't fail on version check
    monkeypatch.setattr("main._check_windows_version", lambda: (True, None))
    # Use reload to ensure fresh bytecode (avoids stale .pyc issues)
    main_mod = __import__("main")
    return importlib.reload(main_mod)
