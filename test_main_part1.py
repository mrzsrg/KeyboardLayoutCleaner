# ---------------------------------------------------------------------------
# Import _ensure_main from test_main module
# ---------------------------------------------------------------------------
import sys
from unittest import mock

if "test_main" not in sys.modules:
    __import__("test_main")
from test_main import _ensure_main


# ---------------------------------------------------------------------------
# TestSandboxModeParsing
# ---------------------------------------------------------------------------
class TestSandboxModeParsing:
    """Тесты для _parse_sandbox_mode (main.py, стр. 61-90)."""

    def test_no_args_returns_false(self, monkeypatch):
        _ensure_main(monkeypatch)
        import main

        enabled, _args = main._parse_sandbox_mode()
        assert enabled is False

    def test_sandbox_flag(self, monkeypatch):
        _ensure_main(monkeypatch)
        import main

        enabled, _args = main._parse_sandbox_mode(["--sandbox"])
        assert enabled is True

    def test_env_var_true(self, monkeypatch):
        _ensure_main(monkeypatch)
        import os

        import main

        os.environ["SANDBOX_MODE"] = "1"
        enabled, _ = main._parse_sandbox_mode()
        assert enabled is True
        os.environ.pop("SANDBOX_MODE", None)

    def test_env_var_false(self, monkeypatch):
        _ensure_main(monkeypatch)
        import os

        import main

        os.environ["SANDBOX_MODE"] = "0"
        enabled, _ = main._parse_sandbox_mode()
        assert enabled is False
        os.environ.pop("SANDBOX_MODE", None)

    def test_args_sandbox_overrides_env(self, monkeypatch):
        _ensure_main(monkeypatch)
        import os

        import main

        os.environ["SANDBOX_MODE"] = "0"
        enabled, args = main._parse_sandbox_mode(["--sandbox"])
        assert enabled is True
        assert args.sandbox is True
        os.environ.pop("SANDBOX_MODE", None)


# ---------------------------------------------------------------------------
# TestBuildLayoutName
# ---------------------------------------------------------------------------
class TestBuildLayoutName:
    """Тесты для _build_layout_name (main.py, стр. 292-298)."""

    def test_builds_name_with_klid(self, monkeypatch):
        _ensure_main(monkeypatch)
        import main

        with mock.patch.object(main, "get_layout_name", return_value="Russian"):
            name = main._build_layout_name("04190419")
            assert name == "Russian (04190419)"

    def test_falls_back_to_layout_when_no_name(self, monkeypatch):
        _ensure_main(monkeypatch)
        import main

        with mock.patch.object(main, "get_layout_name", return_value=""):
            name = main._build_layout_name("d001dead")
            assert name == "Layout (d001dead)"

    def test_already_has_klid(self, monkeypatch):
        _ensure_main(monkeypatch)
        import main

        with mock.patch.object(
            main, "get_layout_name", return_value="Russian (04190419)"
        ):
            name = main._build_layout_name("04190419")
            assert name == "Russian (04190419)"


# ---------------------------------------------------------------------------
# TestConstants
# ---------------------------------------------------------------------------
class TestConstants:
    """Проверка глобальных констант main.py."""

    def test_pulse_interval_defined(self, monkeypatch):
        _ensure_main(monkeypatch)
        import main

        assert isinstance(main.PULSE_INTERVAL_MS, int)
        assert main.PULSE_INTERVAL_MS > 0

    def test_poll_interval_defined(self, monkeypatch):
        _ensure_main(monkeypatch)
        import main

        assert isinstance(main.POLL_INTERVAL_MS, int)
        assert main.POLL_INTERVAL_MS > 0

    def test_mutex_name_defined(self, monkeypatch):
        _ensure_main(monkeypatch)
        import main

        assert isinstance(main._MUTEX_NAME, str)
        assert "Global" in main._MUTEX_NAME

    def test_hklm_catalog_path_defined(self, monkeypatch):
        _ensure_main(monkeypatch)
        import main

        assert isinstance(main.HKLM_CATALOG_PATH, str)
        assert "Keyboard Layouts" in main.HKLM_CATALOG_PATH


# ---------------------------------------------------------------------------
# TestApplySandboxToModules
# ---------------------------------------------------------------------------
class TestApplySandboxToModules:
    """Тесты для _apply_sandbox_to_modules (main.py, стр. 104-111)."""

    def test_enable_sandbox(self, monkeypatch):
        _ensure_main(monkeypatch)
        import config as _cfg
        import main

        _cfg.disable_sandbox()
        main._apply_sandbox_to_modules(True)
        assert _cfg.is_sandbox_enabled() is True
        main._apply_sandbox_to_modules(False)

    def test_disable_sandbox(self, monkeypatch):
        _ensure_main(monkeypatch)
        import config as _cfg
        import main

        _cfg.enable_sandbox()
        main._apply_sandbox_to_modules(False)
        assert _cfg.is_sandbox_enabled() is False
