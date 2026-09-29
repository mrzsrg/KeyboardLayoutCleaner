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


class TestCiSigningStep:
    """FIX-38: подпись в релизном прогоне не должна зависеть от воли образа.

    Неподписанная публикация — самый дорогой вид «молчаливого успеха»:
    релиз выходит, хеши считаются и публикуются, README обещает проверку
    подписи, а проверить нечего. Шаг включается наличием секретов, значит
    человек подпись запросил и её надо либо поставить, либо честно упасть.
    """

    @staticmethod
    def _workflow() -> str:
        return CI_WORKFLOW.read_text(encoding="utf-8")

    def _sign_step(self) -> str:
        """Текст шага подписи: от его имени до конца блока run."""
        text = self._workflow()
        start = text.index("- name: Sign executable")
        run_at = text.index("run: |", start)
        # Конец шага - следующий `- name:` на том же отступе.
        end = text.index("\n      - name:", run_at)
        return text[run_at:end]

    def test_signing_happens_before_the_archive_is_created(self):
        """Подписанный EXE обязан попасть в ZIP, а не в уже готовый архив.

        Порядок «сначала архив, потом подпись» дал бы типичную ошибку: хеши
        посчитаны для неподписанного файла, а подпись появилась позже и
        разъехалась с ними.
        """
        text = self._workflow()
        assert text.index("- name: Sign executable") < text.index(
            "- name: Create ZIP archive"
        )

    def test_step_does_not_assume_signtool_in_path(self):
        """Windows SDK лежит в «Windows Kits\\10\\bin\\<версия>\\x64».

        Раньше шаг полагался на PATH образа runner'а. Это не свойство шага, а
        деталь версии образа: при смене образа подпись молча переставала бы
        работать.
        """
        step = self._sign_step()
        assert "Windows Kits" in step
        assert "signtool.exe" in step
        assert "$env:PATH" in step

    def test_requested_but_impossible_signing_fails_the_release(self):
        """Секреты заданы, а подписать нечем — падаем, а не публикуем молча."""
        step = self._sign_step()
        assert "exit 1" in step
        assert "Get-Command signtool.exe" in step

    def test_pfx_is_removed_even_when_the_step_stops_early(self):
        """Секрет на диске раннера не должен пережить шаг."""
        step = self._sign_step()
        assert "Remove-Item cert.pfx" in step


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
