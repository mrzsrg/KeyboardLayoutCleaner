"""Unit tests for FIX-5: sign_exe.py без недостоверного режима «ESTS».

Проверяется контракт «любой вызов ``signtool sign`` содержит селектор
сертификата»: ``/tr`` — только сервер отметки времени RFC 3161 и не заменяет
сертификат, поэтому вызов без ``/f``, ``/a`` и т.п. подписывать нечем.
"""

import sys
import types
from pathlib import Path
from unittest import mock

import pytest

import sign_exe

REPO_ROOT = Path(__file__).parent
SIGN_SOURCE = (REPO_ROOT / "sign_exe.py").read_text(encoding="utf-8")
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci-cd.yml"
# Селекторы сертификата для `signtool sign` (MS Learn: SignTool sign).
CERT_SELECTORS = ("/a", "/f", "/c", "/k", "/n", "/sha1")


def _completed(returncode: int = 0, stderr: str = "") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        returncode=returncode, stdout="", stderr=stderr, args=[]
    )


class TestEstsModeRemoved:
    def test_module_has_no_sign_with_ests(self):
        assert not hasattr(sign_exe, "sign_with_ests")

    def test_source_has_no_ests_mentions(self):
        assert "ests" not in SIGN_SOURCE.lower()

    def test_ci_workflow_does_not_use_ests(self):
        assert "--ests" not in CI_WORKFLOW.read_text(encoding="utf-8")

    def test_parser_rejects_ests(self, monkeypatch):
        monkeypatch.setattr(
            sys, "argv", ["sign_exe.py", "--exe", "fake.exe", "--ests"]
        )
        with pytest.raises(SystemExit):
            sign_exe.main()


class TestSignCommandsAlwaysHaveCertSelector:
    def test_pfx_mode_selects_certificate(self, monkeypatch):
        captured: list[list[str]] = []
        monkeypatch.setattr(
            sign_exe.subprocess,
            "run",
            lambda cmd, **kw: captured.append(cmd) or _completed(),
        )
        ok = sign_exe.sign_with_pfx(
            Path("fake.exe"), Path("cert.pfx"), "secret",
            tsa_url="https://timestamp.digicert.com", signtool=Path("signtool.exe"),
        )
        assert ok is True
        cmd = captured[0]
        assert cmd[1] == "sign"
        assert "/f" in cmd
        assert "/p" in cmd
        assert "/tr" in cmd  # отметка времени как дополнение к сертификату

    def test_store_mode_selects_certificate(self, monkeypatch):
        captured: list[list[str]] = []
        monkeypatch.setattr(
            sign_exe.subprocess,
            "run",
            lambda cmd, **kw: captured.append(cmd) or _completed(),
        )
        ok = sign_exe.sign_with_store(
            Path("fake.exe"), tsa_url="https://timestamp.digicert.com",
            signtool=Path("signtool.exe"),
        )
        assert ok is True
        cmd = captured[0]
        assert cmd[1] == "sign"
        assert "/a" in cmd
        assert any(sel in cmd for sel in CERT_SELECTORS)

    def test_store_mode_without_tsa_still_signs(self, monkeypatch):
        captured: list[list[str]] = []
        monkeypatch.setattr(
            sign_exe.subprocess,
            "run",
            lambda cmd, **kw: captured.append(cmd) or _completed(),
        )
        assert sign_exe.sign_with_store(
            Path("fake.exe"), signtool=Path("signtool.exe")
        )
        assert "/tr" not in captured[0]

    def test_verify_does_not_sign(self, monkeypatch):
        captured: list[list[str]] = []
        monkeypatch.setattr(
            sign_exe.subprocess,
            "run",
            lambda cmd, **kw: captured.append(cmd) or _completed(),
        )
        assert sign_exe.verify_signature(Path("fake.exe"), signtool=Path("signtool.exe"))
        assert captured[0][1] == "verify"
        assert "sign" not in captured[0]


class TestCliErrorMessages:
    def _argv(self, tmp_path, *extra):
        exe = tmp_path / "fake.exe"
        exe.write_bytes(b"MZ")
        return ["sign_exe.py", "--exe", str(exe), *extra]

    def test_no_method_reports_supported_ways(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setattr(sys, "argv", self._argv(tmp_path))
        monkeypatch.setattr(sign_exe, "find_signtool", lambda: Path("signtool.exe"))
        with caplog.at_level("ERROR"), pytest.raises(SystemExit) as excinfo:
            sign_exe.main()
        assert excinfo.value.code == 1
        message = caplog.text
        assert "--pfx" in message
        assert "--store" in message
        assert "--verify" in message

    def test_pfx_without_password_is_rejected(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setattr(sys, "argv", self._argv(tmp_path, "--pfx", "cert.pfx"))
        monkeypatch.setattr(sign_exe, "find_signtool", lambda: Path("signtool.exe"))
        with caplog.at_level("ERROR"), pytest.raises(SystemExit):
            sign_exe.main()
        assert "--store" in caplog.text

    def test_missing_signtool_is_reported(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setattr(sys, "argv", self._argv(tmp_path, "--store"))
        monkeypatch.setattr(
            sign_exe, "find_signtool",
            mock.Mock(side_effect=FileNotFoundError("signtool.exe не найден в PATH")),
        )
        with caplog.at_level("ERROR"), pytest.raises(SystemExit) as excinfo:
            sign_exe.main()
        assert excinfo.value.code == 1
        assert "signtool" in caplog.text
