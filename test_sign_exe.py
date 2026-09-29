"""Unit tests for FIX-5: sign_exe.py без недостоверного режима «ESTS».

Проверяется контракт «любой вызов ``signtool sign`` содержит селектор
сертификата»: ``/tr`` — только сервер отметки времени RFC 3161 и не заменяет
сертификат, поэтому вызов без ``/f``, ``/a`` и т.п. подписывать нечем.
"""

import json
import subprocess
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


def _ps_payload(
    status: str = "UnknownError",
    has_signer: bool = True,
    sig_type: str = "Authenticode",
    thumb: str = "CA7D5180DC99F4E652DAADC713537BE9950F0749",
    subject: str = "CN=KeyboardLayoutCleaner",
) -> str:
    """Ответ Get-AuthenticodeSignature в виде, который читает sign_exe."""
    return json.dumps(
        {
            "status": status,
            "has_signer": has_signer,
            "type": sig_type,
            "thumb": thumb,
            "subject": subject,
        }
    )


def _run_dispatching(
    sign_rc: int, ps_stdout: str, captured: list[list[str]] | None = None
):
    """Подмена subprocess.run: отдельно signtool, отдельно PowerShell."""
    envs: list[dict] = []

    def run(cmd, **kw):
        if captured is not None:
            captured.append(list(cmd))
        envs.append(kw.get("env", {}))
        if "-Command" in cmd:
            return types.SimpleNamespace(
                returncode=0, stdout=ps_stdout, stderr="", args=list(cmd)
            )
        return types.SimpleNamespace(
            returncode=sign_rc,
            stdout="",
            stderr="SignTool Error: A certificate chain processed, but "
            "terminated in a root certificate which is not trusted",
            args=list(cmd),
        )

    run.envs = envs  # type: ignore[attr-defined]
    return run


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


class TestSelfSignedSignatureVerification:
    """FIX-39: самоподписанный сертификат — это подпись без доверия.

    Измерено на живой машине 29.09.2026: ``signtool verify /pa`` для такого
    сертификата честно отвечает «цепочка прервана на недоверенном корне», а
    ``Get-AuthenticodeSignature`` при этом возвращает статус ``UnknownError``,
    отпечаток издателя и тип Authenticode — то есть подпись есть и файл не
    менялся. Подделанный файл даёт ``NotSigned``.

    Значит «доверие» и «целостность» — разные утверждения, и склеивать их в
    один зелёный статус нельзя: доверия у самоподписанного сертификата нет
    по определению, а единственный способ получить зелёный ``/pa`` — вписать
    корень в хранилище доверия раннера, что на неинтерактивной сессии
    виснет.
    """

    def test_trusted_signature_passes_strictly(self, monkeypatch, caplog):
        captured: list[list[str]] = []
        monkeypatch.setattr(
            sign_exe.subprocess, "run", _run_dispatching(0, "", captured)
        )
        with caplog.at_level("INFO"):
            ok = sign_exe.verify_signature(
                Path("fake.exe"), signtool=Path("signtool.exe")
            )
        assert ok is True
        assert len(captured) == 1, "при успешном signtool PowerShell не нужен"

    def test_self_signed_is_rejected_without_explicit_permission(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            sign_exe.subprocess, "run", _run_dispatching(1, _ps_payload())
        )
        with caplog.at_level("ERROR"):
            ok = sign_exe.verify_signature(
                Path("fake.exe"), signtool=Path("signtool.exe")
            )
        assert ok is False
        assert "не прошла проверку" in caplog.text

    def test_self_signed_passes_only_with_explicit_permission(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            sign_exe.subprocess, "run", _run_dispatching(1, _ps_payload())
        )
        with caplog.at_level("WARNING"):
            ok = sign_exe.verify_signature(
                Path("fake.exe"),
                signtool=Path("signtool.exe"),
                allow_untrusted=True,
            )
        assert ok is True
        # Отпечаток в логе: иначе «подпись есть» нечем подтвердить.
        assert "CA7D5180DC99F4E652DAADC713537BE9950F0749" in caplog.text
        assert "не доверена" in caplog.text

    def test_tampered_file_is_rejected_even_with_permission(self, monkeypatch):
        """Разрешение на «недоверенный корень» не должно лечить подделку."""
        monkeypatch.setattr(
            sign_exe.subprocess,
            "run",
            _run_dispatching(1, _ps_payload(status="NotSigned", has_signer=False)),
        )
        assert not sign_exe.verify_signature(
            Path("fake.exe"), signtool=Path("signtool.exe"), allow_untrusted=True
        )

    def test_hash_mismatch_is_rejected_even_with_permission(self, monkeypatch):
        monkeypatch.setattr(
            sign_exe.subprocess,
            "run",
            _run_dispatching(1, _ps_payload(status="HashMismatch")),
        )
        assert not sign_exe.verify_signature(
            Path("fake.exe"), signtool=Path("signtool.exe"), allow_untrusted=True
        )

    def test_broken_powershell_answer_is_rejected(self, monkeypatch):
        """Мусор в ответе PowerShell не должен читаться как «всё хорошо»."""
        monkeypatch.setattr(
            sign_exe.subprocess, "run", _run_dispatching(1, "не json вовсе")
        )
        assert not sign_exe.verify_signature(
            Path("fake.exe"), signtool=Path("signtool.exe"), allow_untrusted=True
        )

    def test_powershell_failure_is_rejected(self, monkeypatch):
        def run(cmd, **kw):
            if "-Command" in cmd:
                return types.SimpleNamespace(
                    returncode=1, stdout="", stderr="нет прав", args=list(cmd)
                )
            return _completed(1, "not trusted")

        monkeypatch.setattr(sign_exe.subprocess, "run", run)
        assert not sign_exe.verify_signature(
            Path("fake.exe"), signtool=Path("signtool.exe"), allow_untrusted=True
        )

    def test_powershell_timeout_is_rejected(self, monkeypatch):
        def run(cmd, **kw):
            if "-Command" in cmd:
                raise subprocess.TimeoutExpired(cmd, 60)
            return _completed(1, "not trusted")

        monkeypatch.setattr(sign_exe.subprocess, "run", run)
        assert not sign_exe.verify_signature(
            Path("fake.exe"), signtool=Path("signtool.exe"), allow_untrusted=True
        )

    def test_exe_path_is_passed_via_environment_not_command_string(
        self, monkeypatch, tmp_path
    ):
        """Путь к файлу — пользовательские данные, в строку команды он не идёт."""
        captured: list[list[str]] = []
        run = _run_dispatching(1, _ps_payload(), captured)
        monkeypatch.setattr(sign_exe.subprocess, "run", run)
        exe = tmp_path / "имя с кавычками и ; $() .exe"
        exe.write_bytes(b"MZ")
        sign_exe.verify_signature(
            exe, signtool=Path("signtool.exe"), allow_untrusted=True
        )
        ps_call = next(c for c in captured if "-Command" in c)
        assert str(exe) not in " ".join(ps_call)
        assert run.envs[-1]["KLC_EXE_PATH"] == str(exe)  # type: ignore[attr-defined]

    def test_cli_passes_the_permission_flag(self, monkeypatch, tmp_path):
        exe = tmp_path / "fake.exe"
        exe.write_bytes(b"MZ")
        seen: dict = {}

        def fake_verify(path, signtool, **kw):
            seen.update(kw)
            return True

        monkeypatch.setattr(
            sys,
            "argv",
            ["sign_exe.py", "--exe", str(exe), "--verify", "--allow-untrusted"],
        )
        monkeypatch.setattr(sign_exe, "find_signtool", lambda: Path("signtool.exe"))
        monkeypatch.setattr(sign_exe, "verify_signature", fake_verify)
        with pytest.raises(SystemExit) as excinfo:
            sign_exe.main()
        assert excinfo.value.code == 0
        assert seen == {"allow_untrusted": True}


class TestPowershellChoice:
    """Почему 7, а не 5.1 — измерено, а не выбрано на вкус.

    Windows PowerShell 5.1, запущенный из Python, не может загрузить модуль
    Microsoft.PowerShell.Security: «команда найдена в модуле ..., но
    загрузить модуль не удалось». Get-AuthenticodeSignature там просто
    отсутствует, и проверка подписи тихо вырождается в отказ — самый
    неприятный вид поломки. PowerShell 7 на тех же входах отвечает
    корректно.
    """

    def test_powershell7_is_preferred(self, monkeypatch):
        monkeypatch.setattr(
            sign_exe.shutil,
            "which",
            lambda name: "C:/Program Files/PowerShell/7/pwsh.exe"
            if name == "pwsh"
            else "C:/Windows/System32/powershell.exe",
        )
        assert sign_exe.find_powershell().endswith("pwsh.exe")

    def test_falls_back_to_windows_powershell(self, monkeypatch):
        monkeypatch.setattr(
            sign_exe.shutil,
            "which",
            lambda name: None if name == "pwsh" else "C:/Windows/powershell.exe",
        )
        assert sign_exe.find_powershell().endswith("powershell.exe")

    def test_absent_powershell_is_reported(self, monkeypatch):
        monkeypatch.setattr(sign_exe.shutil, "which", lambda name: None)
        with pytest.raises(FileNotFoundError) as excinfo:
            sign_exe.find_powershell()
        assert "pwsh" in str(excinfo.value)

    def test_ps7_module_paths_are_removed_for_the_51_fallback(
        self, monkeypatch, tmp_path
    ):
        """Наследованный PSModulePath от 7 указывает 5.1 на чужие модули."""
        monkeypatch.setenv(
            "PSModulePath",
            "C:/Program Files/PowerShell/7/Modules;"
            "C:/Users/me/Documents/PowerShell/Modules;"
            "C:/Windows/System32/WindowsPowerShell/v1.0/Modules",
        )
        env = sign_exe._powershell_env(tmp_path / "app.exe")
        parts = next(v for k, v in env.items() if k.upper() == "PSMODULEPATH").split(";")
        assert not any("PowerShell/7" in p or "PowerShell\\\\7" in p for p in parts)
        assert any("WindowsPowerShell" in p for p in parts)

    def test_read_uses_the_chosen_powershell(self, monkeypatch, tmp_path):
        """В дочерний процесс идёт тот интерпретатор, который нашли."""
        captured: list[list[str]] = []
        monkeypatch.setattr(sign_exe, "find_powershell", lambda: "pwsh")
        monkeypatch.setattr(
            sign_exe.subprocess, "run", _run_dispatching(0, "", captured)
        )
        sign_exe.read_authenticode_status(tmp_path / "app.exe")
        assert captured[0][0] == "pwsh"


class TestSignTimeoutIsExplained:
    """Недоступный сервер отметки времени не должен выглядеть как стектрейс."""

    def test_pfx_timeout_names_the_timestamper(self, monkeypatch, caplog):
        def run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 60)

        monkeypatch.setattr(sign_exe.subprocess, "run", run)
        with caplog.at_level("ERROR"):
            ok = sign_exe.sign_with_pfx(
                Path("fake.exe"), Path("cert.pfx"), "secret",
                tsa_url="https://timestamp.digicert.com",
                signtool=Path("signtool.exe"),
            )
        assert ok is False
        assert "таймаут" in caplog.text
        assert "tsa-url" in caplog.text

    def test_store_timeout_is_explained(self, monkeypatch, caplog):
        def run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 60)

        monkeypatch.setattr(sign_exe.subprocess, "run", run)
        with caplog.at_level("ERROR"):
            ok = sign_exe.sign_with_store(
                Path("fake.exe"), tsa_url="https://timestamp.digicert.com",
                signtool=Path("signtool.exe"),
            )
        assert ok is False
        assert "таймаут" in caplog.text


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

    def test_self_signed_certificate_is_detected(self):
        """Отличать самоподписанный сертификат надо по Subject = Issuer."""
        step = self._sign_step()
        assert "Get-PfxCertificate" in step
        assert "Subject -eq" in step
        assert "Issuer" in step

    def test_untrusted_verification_is_announced_not_hidden(self):
        """Недоверенный корень — это предупреждение в логе, а не «успех».

        Иначе релиз выглядит подписанным «по-человечески», а пользователь
        получает ровно то же самое, что и без подписи: окно SmartScreen с
        «Неизвестный издатель», только без пояснений, почему.
        """
        step = self._sign_step()
        assert "::warning" in step
        assert "Неизвестный издатель" in step
        assert "--allow-untrusted" in step

    def test_step_never_touches_the_trust_store(self):
        """Рутовый сертификат в хранилище доверия — ловушка для раннера.

        Проверено на живой машине 29.09.2026: `certutil -addstore -user Root`
        на неинтерактивной сессии не возвращает управление и ждёт диалог
        подтверждения. Такой шаг в релизном прогоне означал бы зависшую
        сборку, а не доверенную подпись.

        Проверяются только исполняемые строки: объяснение «почему так не
        делаем» в комментарии обязано остаться, иначе следующий человек
        снова предложит это «просто добавить».
        """
        step = self._sign_step()
        code = "\n".join(
            line for line in step.splitlines() if not line.strip().startswith("#")
        )
        assert "certutil" not in code
        assert "addstore" not in code
        assert "-user Root" not in code
        # А объяснение в комментарии на месте.
        assert "certutil" in step

    def test_certificate_is_read_without_prompting(self):
        """Шаг не должен ждать ввода пароля, которого на раннере нет.

        ``Get-PfxCertificate -FilePath`` спрашивает пароль PFX интерактивно.
        На раннере ответа не будет, и шаг подписи простоит до таймаута job'а —
        релиз останется без архива. Проверено на живой машине 29.09.2026:
        тот же вызов печатает «Введите пароль:» и не возвращает управление.
        """
        step = self._sign_step()
        code = "\n".join(
            line for line in step.splitlines() if not line.strip().startswith("#")
        )
        assert "Get-PfxCertificate" not in code
        assert "X509Certificate2" in code

    def test_thumbprint_and_expiry_are_logged(self):
        """Кем подписано и до какого — это и есть доказательство подписи."""
        step = self._sign_step()
        assert "Thumbprint" in step
        assert "NotAfter" in step


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
