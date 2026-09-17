"""
test_main.py — Unit tests for main.py functions and KeyboardLayoutCleaner GUI.

Mock-based using in-memory mocks for customtkinter and unittest.mock.
Markers: @pytest.mark.gui — GUI tests (require mocked CTk and event loop)

Uses shared fixtures from conftest.py:
- _ensure_main — clean import of main with mocked dependencies
- mock_ctk / mock_gui_widgets — mocked customtkinter and gui_widgets modules
"""

# Import shared helpers from conftest (avoids duplication across test files)
from conftest import _ensure_main  # noqa: F401
