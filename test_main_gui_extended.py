"""test_main_gui_extended.py - Extended GUI tests for KeyboardLayoutCleaner.

Тестируют реальные методы main.py: диспетчеризацию очереди _process_ui_queue
и применение результатов сканирования _apply_scan_results.

Используют общие моки из conftest.py (_make_ctk, _make_gui_widgets_mock).
"""

import queue
from unittest import mock

import pytest

from conftest import _ensure_main


@pytest.fixture
def app_factory(monkeypatch):
    """Return a factory with reversible imports and no real registry access."""

    def create():
        main_mod = _ensure_main(monkeypatch)
        app = main_mod.KeyboardLayoutCleaner()
        app.after = mock.MagicMock()
        return main_mod, app

    return create


def _build_app_mocks(app_factory) -> tuple:
    main_mod, app = app_factory()
    return main_mod, app, {}


# ---------------------------------------------------------------------------
# TestUiQueueDispatch
# ---------------------------------------------------------------------------


class TestUiQueueDispatch:
    """Tests for KeyboardLayoutCleaner._post / _process_ui_queue."""

    def test_post_enqueue_calls_function(self, app_factory):
        """_post должен положить (func, args) в _ui_queue."""
        _main_mod, app, _ = _build_app_mocks(app_factory)

        called = []

        def fake_func(*a):
            called.append(a)

        app._post(fake_func, "hello", 42)
        func, args = app._ui_queue.get_nowait()
        assert func is fake_func
        assert args == ("hello", 42)

    def test_process_ui_queue_executes_tasks(self, app_factory):
        """_process_ui_queue должен выполнить все функции из очереди."""
        _main_mod, app, _ = _build_app_mocks(app_factory)

        results = []

        def add_one(v: int) -> None:
            results.append(v + 1)

        def add_two(v: int) -> None:
            results.append(v + 2)

        app._ui_queue.put((add_one, (10,)))
        app._ui_queue.put((add_two, (20,)))
        app._process_ui_queue()

        assert results == [11, 22]

    def test_process_ui_queue_handles_exceptions(self, app_factory):
        """_process_ui_queue должен корректно обрабатывать исключения."""
        _main_mod, app, _ = _build_app_mocks(app_factory)

        def always_fails() -> None:
            raise ValueError("boom")

        app._ui_queue.put((always_fails, ()))
        app._process_ui_queue()

        # _process_ui_queue вызывает self.after(...) для повторного запуска
        app.after.assert_called()

    def test_ui_queue_is_thread_safe(self, app_factory):
        """_ui_queue — экземпляр queue.Queue."""
        _main_mod, app, _ = _build_app_mocks(app_factory)

        assert isinstance(app._ui_queue, queue.Queue)
        assert hasattr(app._ui_queue, "put")
        assert hasattr(app._ui_queue, "get_nowait")


# ---------------------------------------------------------------------------
# TestApplyScanResults
# ---------------------------------------------------------------------------


class TestApplyScanResults:
    """Tests for KeyboardLayoutCleaner._apply_scan_results."""

    def test_apply_scan_results_updates_layouts_data(self, app_factory):
        """_apply_scan_results должен сохранить layouts."""
        _main_mod, app, _ = _build_app_mocks(app_factory)

        layouts: dict[str, list[dict[str, str]]] = {
            "00000409": [{"name": "English", "klid": "00000409"}],
            "00000419": [{"name": "Русский", "klid": "00000419"}],
        }

        app._render_layouts_list = mock.MagicMock()
        app.list_title_label = mock.MagicMock()
        app.current_layout_klid = ""
        app.delete_button = mock.MagicMock()
        app._render_layouts_overview = mock.MagicMock()
        app._set_status = mock.MagicMock()
        app._set_stage = mock.MagicMock()

        app._apply_scan_results(layouts)

        assert app.layouts_data is layouts
        app._render_layouts_list.assert_called_once()
        app.list_title_label.configure.assert_called_once()
        app._set_status.assert_called_once()

    def test_apply_scan_results_on_empty(self, app_factory):
        """_apply_scan_results обрабатывает пустой результат."""
        _main_mod, app, _ = _build_app_mocks(app_factory)

        app._render_layouts_list = mock.MagicMock()
        app.list_title_label = mock.MagicMock()
        app.delete_button = mock.MagicMock()
        app._render_layouts_overview = mock.MagicMock()
        app._set_status = mock.MagicMock()
        app._set_stage = mock.MagicMock()

        app._apply_scan_results({})

        app.list_title_label.configure.assert_called_once()
        app._render_layouts_overview.assert_called_once()


# ---------------------------------------------------------------------------
# TestAfterScanSuccess
# ---------------------------------------------------------------------------


class TestAfterScanSuccess:
    """Tests for KeyboardLayoutCleaner._after_scan_success."""

    def test_success_enables_scan_button(self, app_factory):
        """_after_scan_success должен включить scan_button."""
        _main_mod, app, _ = _build_app_mocks(app_factory)

        app.scan_button = mock.MagicMock()
        app.restore_button = mock.MagicMock()
        app._apply_scan_results = mock.MagicMock()
        app._progress_stop = mock.MagicMock()

        layouts: dict[str, list[dict[str, str]]] = {"klid": [{"name": "en"}]}
        app._after_scan_success(layouts)

        # configure вызывается с state='normal' (может включать и другие аргументы)
        call_args = app.scan_button.configure.call_args
        assert call_args[1].get("state") == "normal"
        app._apply_scan_results.assert_called_once()

    def test_success_sets_scan_done_flag(self, app_factory):
        """_after_scan_success устанавливает _scan_done = True."""
        _main_mod, app, _ = _build_app_mocks(app_factory)

        app._scan_done = False
        app.scan_button = mock.MagicMock()
        app.restore_button = mock.MagicMock()
        app._apply_scan_results = mock.MagicMock()
        app._progress_stop = mock.MagicMock()

        layouts: dict[str, list[dict[str, str]]] = {"klid": [{"name": "en"}]}
        app._after_scan_success(layouts)

        assert app._scan_done is True


# ---------------------------------------------------------------------------
# TestVersionCheck
# ---------------------------------------------------------------------------


class TestVersionCheck:
    """Tests for version detection."""

    def test_version_from_metadata(self, app_factory):
        """app_version должен быть не пустой строкой."""
        _main_mod, _app = app_factory()

        assert hasattr(_main_mod, "app_version")
        assert isinstance(_main_mod.app_version, str)
        assert len(_main_mod.app_version) > 0


# ---------------------------------------------------------------------------
# TestLangChange
# ---------------------------------------------------------------------------


class TestLangChange:
    """Tests for KeyboardLayoutCleaner._on_lang_change."""

    def test_lang_change_calls_apply_static_texts(self, app_factory):
        """_on_lang_change должен вызвать _apply_static_texts."""
        _main_mod, app, _ = _build_app_mocks(app_factory)

        app._apply_static_texts = mock.MagicMock()
        app._on_lang_change("English")

        app._apply_static_texts.assert_called_once()

    def test_lang_change_accepts_language_code(self, app_factory):
        """_on_lang_change должен принимать строку-язык."""
        _main_mod, app, _ = _build_app_mocks(app_factory)

        app._apply_static_texts = mock.MagicMock()
        app._on_lang_change("en")
        app._apply_static_texts.assert_called()


# ---------------------------------------------------------------------------
# TestThemeToggle
# ---------------------------------------------------------------------------


class TestThemeToggle:
    """Tests for KeyboardLayoutCleaner._on_toggle_theme."""

    def test_toggle_theme_calls_rebuild(self, app_factory):
        """_on_toggle_theme должен вызвать _rebuild_ui."""
        _main_mod, app, _ = _build_app_mocks(app_factory)

        app._rebuild_ui = mock.MagicMock()
        app._view_switch_allowed = mock.MagicMock(return_value=True)

        app._on_toggle_theme()

        app._rebuild_ui.assert_called_once()

    def test_toggle_theme_respects_lock(self, app_factory):
        """_on_toggle_theme не должен перестраивать UI в busy-состоянии."""
        _main_mod, app, _ = _build_app_mocks(app_factory)

        app._rebuild_ui = mock.MagicMock()
        app._view_switch_allowed = mock.MagicMock(return_value=False)

        app._on_toggle_theme()

        app._rebuild_ui.assert_not_called()


# ---------------------------------------------------------------------------
# TestBrightnessChange
# ---------------------------------------------------------------------------


class TestBrightnessChange:
    """Tests for KeyboardLayoutCleaner._on_brightness_change."""

    def test_brightness_change_calls_rebuild(self, app_factory):
        """_on_brightness_change должен вызвать _rebuild_ui."""
        _main_mod, app, _ = _build_app_mocks(app_factory)

        app._rebuild_ui = mock.MagicMock()
        app._view_switch_allowed = mock.MagicMock(return_value=True)

        app._on_brightness_change("normal")

        app._rebuild_ui.assert_called_once()


# ---------------------------------------------------------------------------
# TestStaticTextsApply
# ---------------------------------------------------------------------------


class TestStaticTextsApply:
    """Tests for KeyboardLayoutCleaner._apply_static_texts."""

    def test_static_texts_applies_language(self, app_factory):
        """_apply_static_texts должен обновить заголовок окна."""
        _main_mod, app, _ = _build_app_mocks(app_factory)

        app.admin_banner = mock.MagicMock()
        app.admin_banner.apply_language = mock.MagicMock()
        app._apply_action_button_texts = mock.MagicMock()
        app.block_sync_checkbox = mock.MagicMock()
        app._update_admin_banner = mock.MagicMock()
        app.details_title_label = mock.MagicMock()
        app._apply_list_titles = mock.MagicMock()
        app._list_placeholder = mock.MagicMock()
        app._apply_details_panel = mock.MagicMock()
        app.backup_label = mock.MagicMock()
        app._last_backup_info = ("", "")
        app._set_stage = mock.MagicMock()

        app._apply_static_texts()

        app.admin_banner.apply_language.assert_called_once()
        app._apply_action_button_texts.assert_called_once()
        app._update_admin_banner.assert_called_once()
        app._set_stage.assert_called_once()
