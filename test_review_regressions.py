"""Regression tests for shared import isolation and native version detection."""

import ctypes
import sys
from unittest import mock

import pytest

from conftest import FakeWinreg, _ensure_main


def test_fake_registry_accepts_integer_roots():
    registry = FakeWinreg()
    registry.SetValueEx(registry.HKEY_CURRENT_USER, "value", 0, registry.REG_SZ, "ok")
    assert registry.QueryValueEx(registry.HKEY_CURRENT_USER, "value") == (
        "ok",
        registry.REG_SZ,
    )
    with pytest.raises(FileNotFoundError):
        registry.QueryValueEx(registry.HKEY_LOCAL_MACHINE, "missing")


def test_shared_import_restores_modules_and_environment(monkeypatch):
    names = ("main", "config", "scanner", "cleaner", "customtkinter", "gui_widgets")
    missing = object()
    before = {name: sys.modules.get(name, missing) for name in names}
    monkeypatch.setenv("SANDBOX_MODE", "1")
    with monkeypatch.context() as isolated:
        main = _ensure_main(isolated)
        assert main.is_ok
        assert not main.SANDBOX_MODE
    import os

    assert os.environ["SANDBOX_MODE"] == "1"
    for name in names:
        assert sys.modules.get(name, missing) is before[name]


@pytest.mark.parametrize(
    ("status", "major", "build", "expected"),
    [(0, 10, 19045, True), (0, 6, 9600, False), (-1, 10, 19045, False)],
)
def test_rtl_get_version_signature(monkeypatch, status, major, build, expected):
    main = _ensure_main(monkeypatch)

    def query(pointer):
        pointer._obj.dwMajorVersion = major
        pointer._obj.dwBuildNumber = build
        return status

    with mock.patch.object(
        ctypes.windll.ntdll, "RtlGetVersion", side_effect=query
    ) as api:
        supported, _version = main._check_windows_version()
        assert supported is expected
        api.assert_called_once()
        assert len(api.call_args.args) == 1
        assert len(api.argtypes) == 1
        assert api.restype is ctypes.c_long
