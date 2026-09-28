"""Regression tests for shared import isolation, native version detection,
the fail-closed backup contract (FIX-1/FIX-16), the backup-only
SettingSync source with opt-in cloud-sync blocking (FIX-2) and the
input-services rescue path (FIX-13)."""

import ctypes
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

import backup
import capabilities
import cleaner
import config
import langlist
import mutate
import scanner
import settings
from conftest import FakeWinreg, _ensure_main, registry_modules


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


# ---------------------------------------------------------------------------
# FIX-1/FIX-16: fail-closed контракт бэкапа
#
# «Ветки нет в реестре» — НЕ отказ (skipped, бэкапить нечего); реальный сбой
# экспорта СУЩЕСТВУЮЩЕЙ ветки — отказ (failed) и отмена удаления. До правки
# оба случая попадали в один список failed, из-за чего на чистой системе
# почти каждый бэкап помечался «неполным», а настоящий отказ тонул в шуме.
# ---------------------------------------------------------------------------

_REG_HEADER = "Windows Registry Editor Version 5.00"
_PRELOAD = "Keyboard Layout\\Preload"
_SUBSTITUTES = "Keyboard Layout\\Substitutes"
_MISSING = "Keyboard Layout\\MissingBranch"
# Формат веток: (root, subkey, mode, admin_required) — как scanner.AFFECTED_BRANCHES
_TEST_BRANCHES = [("HKCU", _PRELOAD, "delete", False)]


def _fake_root_consts(fake: FakeWinreg) -> dict:
    """_ROOT_CONST на константы фейкового winreg (иначе lookup не совпадёт)."""
    return {
        "HKCU": fake.HKEY_CURRENT_USER,
        "HKU": fake.HKEY_USERS,
        "HKLM": fake.HKEY_LOCAL_MACHINE,
    }


def _write_reg_utf16(outfile, text: str) -> None:
    outfile.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))


def _export_ok(hkey_root, subkey_path, outfile) -> bool:
    """Мок reg export: валидная UTF-16 секция с заголовком."""
    _write_reg_utf16(
        outfile, _REG_HEADER + "\r\n\r\n" + f"[{hkey_root}\\{subkey_path}]\r\n"
    )
    return True


def _patch_reg(monkeypatch, fake: FakeWinreg, seed_preload: bool = True) -> None:
    """FakeWinreg во всех модулях реестрового уровня.

    FIX-10 (шаги 1-4): список берётся из ``conftest.registry_modules()``, а не
    перечисляется здесь. Вручную он устаревал при каждом переносе кода, и на
    шаге 4 новый ``settings`` выпал из подмены — тесты пошли бы в настоящий
    реестр, а контракт fail-closed проверялся бы вхолостую.
    """
    if seed_preload:
        fake.set(
            fake.HKEY_CURRENT_USER,
            _PRELOAD,
            values={"1": "00000409", "2": "00000419"},
        )
    # winreg у каждого модуля свой глобальный, а _ROOT_CONST есть не у всех.
    for module in registry_modules():
        monkeypatch.setattr(module, "winreg", fake)
        if hasattr(module, "_ROOT_CONST"):
            monkeypatch.setattr(module, "_ROOT_CONST", _fake_root_consts(fake))


class TestBranchExists:
    """cleaner._branch_exists: «ветки нет» ≠ отказ экспорта."""

    def test_missing_branch_returns_false(self, monkeypatch):
        fake = FakeWinreg()
        _patch_reg(monkeypatch, fake, seed_preload=False)
        assert cleaner._branch_exists("HKCU", _MISSING) is False

    def test_existing_branch_returns_true(self, monkeypatch):
        fake = FakeWinreg()
        _patch_reg(monkeypatch, fake)
        assert cleaner._branch_exists("HKCU", _PRELOAD) is True

    def test_access_denied_is_pessimistically_existing(self, monkeypatch):
        """Ошибка доступа трактуется как «ветка есть»: провал её экспорта
        обязан стать реальным отказом (failed), а не безобидным skip-ом."""
        fake = FakeWinreg()
        _patch_reg(monkeypatch, fake)
        fake.deny.add((fake.HKEY_CURRENT_USER, _PRELOAD))
        assert cleaner._branch_exists("HKCU", _PRELOAD) is True

    def test_unknown_root_fail_closed(self, monkeypatch):
        """Неизвестный корень: решение не берём на себя — считаем ветку
        существующей, чтобы провал экспорта не прошёл незамеченным."""
        fake = FakeWinreg()
        _patch_reg(monkeypatch, fake, seed_preload=False)
        monkeypatch.setattr(cleaner, "_ROOT_CONST", {})
        assert cleaner._branch_exists("HKXX", _MISSING) is True


class TestBackupNameReservation:
    """FIX-9: имя файла бэкапа не может достаться двум процессам."""

    def test_reserve_creates_file_and_takes_free_name(self, tmp_path):
        reserved = cleaner._reserve_backup_file(tmp_path, "bkp_1")
        assert reserved == tmp_path / "bkp_1.reg"
        assert reserved.is_file(), "резерв обязан создать файл сразу"

    def test_reserve_never_returns_existing_name(self, tmp_path):
        """Второй вызов с тем же именем обязан взять суффикс."""
        first = cleaner._reserve_backup_file(tmp_path, "bkp_1")
        second = cleaner._reserve_backup_file(tmp_path, "bkp_1")
        third = cleaner._reserve_backup_file(tmp_path, "bkp_1")
        names = {first, second, third}
        assert len(names) == 3, names
        # Сортируемость по времени сохраняется: суффикс идёт после
        # полного таймстампа, а не в начало имени.
        assert sorted(p.name for p in names) == [
            "bkp_1.reg",
            "bkp_1_1.reg",
            "bkp_1_2.reg",
        ]

    def test_reserve_does_not_overwrite_existing_backup(self, tmp_path):
        """Чужой бэкап не должен быть затёрт — это потеря данных пользователя."""
        existing = tmp_path / "bkp_1.reg"
        existing.write_text("ЧУЖОЙ БЭКАП", encoding="utf-8")
        reserved = cleaner._reserve_backup_file(tmp_path, "bkp_1")
        assert reserved != existing
        assert existing.read_text(encoding="utf-8") == "ЧУЖОЙ БЭКАП"

    def test_failed_backup_leaves_no_stub_file(self, tmp_path, monkeypatch):
        """Контракт fail-closed: отказ не оставляет пустой .reg.

        Регрессия при реализации FIX-9: резерв создавал файл ДО экспорта, и
        при отказе он оставался на диске — пользователь получил бы
        «восстановление» из пустого файла.
        """
        monkeypatch.setattr(backup, "_branch_exists", lambda root, subkey: True)
        monkeypatch.setattr(backup, "_export_single_key_via_reg", lambda *a, **k: False)
        with pytest.raises(cleaner.BackupError):
            cleaner.backup_registry(
                [{"root": "HKCU", "subkey": "Keyboard Layout\\Preload"}],
                backup_dir=str(tmp_path),
            )
        assert list(tmp_path.glob("*.reg")) == []

    def test_missing_branches_leave_no_stub_file(self, tmp_path, monkeypatch):
        """Все ветки отсутствуют → BackupError и никаких файлов."""
        monkeypatch.setattr(cleaner, "_branch_exists", lambda root, subkey: False)
        with pytest.raises(cleaner.BackupError):
            cleaner.backup_registry(
                [{"root": "HKCU", "subkey": "Keyboard Layout\\Nope"}],
                backup_dir=str(tmp_path),
            )
        assert list(tmp_path.glob("*.reg")) == []


class TestBackupRegistryClassification:
    """Контракт отчёта backup_registry: exported/failed/skipped."""

    def test_missing_branch_is_skipped_not_failed(self, monkeypatch):
        """Отсутствующая ветка → skipped, бэкап всё равно создаётся."""
        fake = FakeWinreg()
        _patch_reg(monkeypatch, fake)
        monkeypatch.setattr(backup, "_export_single_key_via_reg", _export_ok)
        report: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = cleaner.backup_registry(
                [
                    {"root": "HKCU", "subkey": _PRELOAD},
                    {"root": "HKCU", "subkey": _MISSING},
                ],
                backup_dir=tmp,
                report=report,
            )
            text = Path(backup_path).read_text(encoding="utf-16")
        assert report["failed"] == []
        assert report["skipped"] == ["HKCU\\" + _MISSING]
        assert report["exported"] == ["HKCU\\" + _PRELOAD]
        assert "[HKCU\\" + _PRELOAD + "]" in text

    @pytest.mark.parametrize(
        "fail_mode",
        ["return_false", "raise_oserror", "garbage_section", "empty_section"],
        ids=["reg-export-ошибка", "reg-не-запущен", "секция-нечитаема", "секция-пуста"],
    )
    def test_failed_export_of_existing_branch_aborts(self, monkeypatch, fail_mode):
        """Сбой экспорта СУЩЕСТВУЮЩЕЙ ветки → BackupError, файл не создаётся.

        Регрессия двух дефектов сразу: (1) сбой классифицируется как failed,
        а не skipped; (2) exported не содержит ветку, чья секция не записана
        (раньше ветка попадала в exported до проверки содержимого файла).
        """
        fake = FakeWinreg()
        fake.set(fake.HKEY_CURRENT_USER, _PRELOAD, values={"1": "00000409"})
        fake.set(fake.HKEY_CURRENT_USER, _SUBSTITUTES, values={"d001dead": "00000409"})
        _patch_reg(monkeypatch, fake, seed_preload=False)

        def flaky_export(hkey_root, subkey_path, outfile):
            if subkey_path != _SUBSTITUTES:
                return _export_ok(hkey_root, subkey_path, outfile)
            if fail_mode == "return_false":
                return False
            if fail_mode == "raise_oserror":
                raise OSError("reg.exe не найден (тест)")
            if fail_mode == "garbage_section":
                # 7 байт — нечётная длина: гарантированный UnicodeDecodeError
                outfile.write_bytes(b"garbage")
                return True
            _write_reg_utf16(outfile, _REG_HEADER + "\r\n")  # секций нет
            return True

        monkeypatch.setattr(backup, "_export_single_key_via_reg", flaky_export)
        report: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            with pytest.raises(cleaner.BackupError) as excinfo:
                cleaner.backup_registry(
                    [
                        {"root": "HKCU", "subkey": _PRELOAD},
                        {"root": "HKCU", "subkey": _SUBSTITUTES},
                    ],
                    backup_dir=tmp,
                    report=report,
                )
            # Файл-заглушка не создаётся: неполный бэкап недопустим
            assert list(Path(tmp).glob("*.reg")) == []
        assert report["failed"] == ["HKCU\\" + _SUBSTITUTES]
        assert report["exported"] == ["HKCU\\" + _PRELOAD]
        assert report["skipped"] == []
        assert "Substitutes" in str(excinfo.value)

    def test_all_branches_missing_raises_but_not_failed(self, monkeypatch):
        """Все ветки отсутствуют → BackupError, но failed пуст, export не вызван."""
        fake = FakeWinreg()
        _patch_reg(monkeypatch, fake, seed_preload=False)
        export_calls: list[str] = []

        def must_not_export(hkey_root, subkey_path, outfile):
            export_calls.append(subkey_path)
            return _export_ok(hkey_root, subkey_path, outfile)

        monkeypatch.setattr(cleaner, "_export_single_key_via_reg", must_not_export)
        report: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            with pytest.raises(cleaner.BackupError) as excinfo:
                cleaner.backup_registry(
                    [{"root": "HKCU", "subkey": _MISSING}],
                    backup_dir=tmp,
                    report=report,
                )
            assert list(Path(tmp).glob("*.reg")) == []
        assert report["failed"] == []
        assert report["skipped"] == ["HKCU\\" + _MISSING]
        assert export_calls == []  # до экспорта дело не дошло
        assert "не была экспортирована" in str(excinfo.value)


class TestDeleteLayoutBackupGuard:
    """delete_layout отменяет удаление при неполном бэкапе."""

    KLID = "d001dead"

    def _patch_env(self, monkeypatch, fake: FakeWinreg) -> None:
        """Изолированная среда: фейковый реестр, без админа, одна ветка.

        Preload засеян двумя раскладками — защита «последней раскладки»
        не срабатывает и поток доходит до шага бэкапа.
        """
        _patch_reg(monkeypatch, fake)
        monkeypatch.setattr(cleaner, "_system_info", lambda: {})
        monkeypatch.setattr(cleaner, "is_admin", lambda: False)
        monkeypatch.setattr(cleaner, "_affected_branches", lambda: list(_TEST_BRANCHES))

    def test_backuperror_aborts_before_any_mutation(self, monkeypatch):
        """BackupError из backup_registry → удаление отменено, отчёт заполнен."""
        fake = FakeWinreg()
        self._patch_env(monkeypatch, fake)

        def failing_backup(registry_paths, backup_dir=None, report=None):
            if report is not None:
                report["exported"] = []
                report["failed"] = ["HKCU\\" + _PRELOAD]
                report["skipped"] = []
            raise cleaner.BackupError("не удалось экспортировать ветки (тест)")

        monkeypatch.setattr(cleaner, "backup_registry", failing_backup)
        result = cleaner.delete_layout(self.KLID)
        assert result["success"] is False
        assert result["backup_ok"] is False
        assert result["backup_path"] == ""
        assert result["backup_failed_branches"] == ["HKCU\\" + _PRELOAD]
        assert result["backup_error"]
        assert result["langlist_backup_path"] == ""
        assert result["last_layout_guard"] is False

    def test_fail_open_report_still_aborts(self, monkeypatch):
        """Регрессия fail-open: backup_registry нарушил контракт (не бросил
        исключение), но failed не пуст — дублирующая защита в delete_layout
        обязана отменить удаление."""
        fake = FakeWinreg()
        self._patch_env(monkeypatch, fake)

        def lying_backup(registry_paths, backup_dir=None, report=None):
            if report is not None:
                report["exported"] = []
                report["failed"] = ["HKCU\\" + _PRELOAD]
                report["skipped"] = []
            return "C:\\nonexistent\\backup.reg"

        monkeypatch.setattr(cleaner, "backup_registry", lying_backup)
        result = cleaner.delete_layout(self.KLID)
        assert result["success"] is False
        assert result["backup_ok"] is False
        assert result["backup_failed_branches"] == ["HKCU\\" + _PRELOAD]
        assert result["backup_error"]
        assert result["langlist_backup_path"] == ""


# ---------------------------------------------------------------------------
# FIX-2/FIX-2c: backup-only SettingSync\Groups\Language + opt-in блокировка
# ---------------------------------------------------------------------------


class TestSettingsyncGroupsBackupOnly:
    """Groups\\Language — backup-only источник: в бэкапе, вне мутаций."""
    @pytest.fixture(autouse=True)
    def _allow_destructive(self):
        """FIX-29: тест TestSettingsyncGroupsBackupOnly работает С РЕАЛЬНЫМИ разрушительными путями.

        Право выдаётся явно, чтобы читатель теста видел: здесь тест
        действительно пишет в реестр/вызывает PowerShell. Раньше это было
        неявно — достаточно было забыть подмену, и тест писал в живую
        систему (FIX-28).
        """
        capabilities.grant(capabilities.Capability.CLOUD_SYNC_POLICY)


    def test_never_in_mutation_matrix(self):
        """Состояние синхронизации не подлежит «очистке» (только API)."""
        for _root, subkey, *_rest in scanner._ORIGINAL_AFFECTED_BRANCHES:
            assert "SettingSync\\Groups" not in subkey
        for _root, subkey, *_rest in scanner.AFFECTED_BRANCHES:
            assert "SettingSync\\Groups" not in subkey

    def test_resolver_respects_sandbox(self, monkeypatch):
        """FIX-2c: sandbox уводит путь под _SANDBOX_ROOT."""
        # FIX-10 (шаг 4): предикат живёт в mutate, и settings зовёт его как
        # mutate._sandbox_active(). Подмена cleaner._sandbox_active больше не
        # влияет: cleaner держит лишь реэкспорт.
        monkeypatch.setattr(mutate, "_sandbox_active", lambda: False)
        assert cleaner._settingsync_groups_key_path() == (
            cleaner._SETTINGSYNC_GROUPS_SUBKEY
        )
        monkeypatch.setattr(mutate, "_sandbox_active", lambda: True)
        sandboxed = cleaner._SANDBOX_ROOT + "\\" + cleaner._SETTINGSYNC_GROUPS_SUBKEY
        assert cleaner._settingsync_groups_key_path() == sandboxed
        assert cleaner._settingsync_groups_paths() == [
            {"root": "HKCU", "subkey": sandboxed}
        ]

    def test_sync_funcs_route_through_sandbox(self, monkeypatch):
        """disable/enable пишут в sandbox-копию; реальная ветка не тронута."""
        fake = FakeWinreg()
        real_sub = cleaner._SETTINGSYNC_GROUPS_SUBKEY
        fake.set(fake.HKEY_CURRENT_USER, real_sub, values={"Enabled": 1})
        _patch_reg(monkeypatch, fake, seed_preload=False)
        monkeypatch.setattr(mutate, "_sandbox_active", lambda: True)
        fake.set(
            fake.HKEY_CURRENT_USER,
            cleaner._SANDBOX_ROOT + "\\" + real_sub,
            values={"Enabled": 1},
        )

        ok, _detail = cleaner.disable_language_sync()
        assert ok
        assert cleaner._is_language_sync_blocked() is True  # sandbox-копия = 0
        monkeypatch.setattr(mutate, "_sandbox_active", lambda: False)
        assert cleaner._is_language_sync_blocked() is False  # реальная = 1

        monkeypatch.setattr(mutate, "_sandbox_active", lambda: True)
        ok, _detail = cleaner.enable_language_sync()
        assert ok
        assert cleaner._is_language_sync_blocked() is False  # sandbox-копия = 1


class TestDeleteLayoutCloudSyncOptIn:
    """FIX-2/Находка A: блокировка облака при удалении — только по согласию."""
    @pytest.fixture(autouse=True)
    def _allow_destructive(self):
        """FIX-29: тест TestDeleteLayoutCloudSyncOptIn работает С РЕАЛЬНЫМИ разрушительными путями.

        Право выдаётся явно, чтобы читатель теста видел: здесь тест
        действительно пишет в реестр/вызывает PowerShell. Раньше это было
        неявно — достаточно было забыть подмену, и тест писал в живую
        систему (FIX-28).
        """
        capabilities.grant(capabilities.Capability.CLOUD_SYNC_POLICY)


    KLID = "d001dead"

    def _harness(self, monkeypatch, fake, tmp, mock_backup=True):
        """Прогон delete_layout до отчёта без реестра/ctfmon/PowerShell."""
        _patch_reg(monkeypatch, fake)
        monkeypatch.setattr(cleaner, "_system_info", lambda: {})
        monkeypatch.setattr(cleaner, "is_admin", lambda: False)
        monkeypatch.setattr(cleaner, "_affected_branches", lambda: list(_TEST_BRANCHES))
        monkeypatch.setattr(
            cleaner, "_current_preload_klids", lambda admin: {"00000409"}
        )
        monkeypatch.setattr(cleaner, "_wipe_branches", lambda branches, result, lid: 0)
        monkeypatch.setattr(
            cleaner,
            "_sync_language_list_via_powershell",
            lambda klid: (True, "SUCCESS"),
        )
        class _NoopSuspender:
            started = False

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(cleaner, "CtfmonSuspender", _NoopSuspender)

        captured: dict = {}

        def stub_backup(registry_paths, backup_dir=None, report=None):
            captured["subkeys"] = [p["subkey"] for p in registry_paths]
            if report is not None:
                report["exported"] = ["HKCU\\" + p["subkey"] for p in registry_paths]
                report["failed"] = []
                report["skipped"] = []
            return str(tmp / "backup.reg")

        if mock_backup:
            monkeypatch.setattr(cleaner, "backup_registry", stub_backup)
        else:
            real_backup = cleaner.backup_registry

            def redirected(registry_paths, backup_dir=None, report=None):
                return real_backup(registry_paths, backup_dir=str(tmp), report=report)

            monkeypatch.setattr(cleaner, "backup_registry", redirected)
        monkeypatch.setattr(
            langlist, "_backup_language_list", lambda base: str(tmp / "langlist.json")
        )
        return captured

    def test_no_opt_in_leaves_sync_untouched(self, monkeypatch, tmp_path):
        """Галочка снята: Enabled не пишется; ветка всё равно бэкапится."""
        fake = FakeWinreg()
        fake.set(
            fake.HKEY_CURRENT_USER,
            cleaner._SETTINGSYNC_GROUPS_SUBKEY,
            values={"Enabled": 1},
        )
        captured = self._harness(monkeypatch, fake, tmp_path)
        result = cleaner.delete_layout(self.KLID)
        assert result["success"] is True
        assert result["language_sync_blocked"] is False
        assert "Пропущено" in result["language_sync_block_detail"]
        assert cleaner._is_language_sync_blocked() is False  # Enabled остался 1
        # Backup-only: ветка состояния присутствует в paths_to_backup
        assert cleaner._SETTINGSYNC_GROUPS_SUBKEY in captured["subkeys"]

    def test_opt_in_blocks_cloud_sync(self, monkeypatch, tmp_path):
        """Галочка установлена: disable выполняется (Enabled -> 0)."""
        fake = FakeWinreg()
        fake.set(
            fake.HKEY_CURRENT_USER,
            cleaner._SETTINGSYNC_GROUPS_SUBKEY,
            values={"Enabled": 1},
        )
        captured = self._harness(monkeypatch, fake, tmp_path)
        result = cleaner.delete_layout(self.KLID, block_cloud_sync=True)
        assert result["success"] is True
        assert result["language_sync_blocked"] is True
        assert cleaner._is_language_sync_blocked() is True
        assert cleaner._SETTINGSYNC_GROUPS_SUBKEY in captured["subkeys"]

    def test_backup_only_branch_lands_in_reg(self, monkeypatch, tmp_path):
        """E2E до .reg: секция Groups\\Language реально записана в бэкап."""
        fake = FakeWinreg()
        fake.set(
            fake.HKEY_CURRENT_USER,
            cleaner._SETTINGSYNC_GROUPS_SUBKEY,
            values={"Enabled": 1},
        )
        self._harness(monkeypatch, fake, tmp_path, mock_backup=False)
        monkeypatch.setattr(backup, "_export_single_key_via_reg", _export_ok)
        result = cleaner.delete_layout(self.KLID)
        assert result["success"] is True
        assert result["backup_ok"] is True
        backup_file = Path(result["backup_path"])
        assert backup_file.exists()
        text = backup_file.read_text(encoding="utf-16")
        assert "[HKCU\\" + cleaner._SETTINGSYNC_GROUPS_SUBKEY + "]" in text


# ---------------------------------------------------------------------------
# FIX-4 → решение владельца 26.09.2026: операция удалена из проекта целиком
# ---------------------------------------------------------------------------


class TestWelcomeSyncRemoved:
    """Copy-UserInternationalSettingsToSystem удалена, а не спрятана в CLI.

    Пока операция была отдельной командой, её можно было вернуть одной
    строкой в реэкспорте ``cleaner.py``. Теперь возвращать нечего: нет ни
    функции, ни права, ни PowerShell-скрипта, ни флага, ни ключа таймаута.
    Проверки на отсутствие здесь намеренно жёсткие — именно отсутствие и
    было целью решения.
    """

    KLID = "d001dead"

    def test_capability_is_gone(self):
        assert not hasattr(capabilities.Capability, "WELCOME_SYNC")
        assert "welcome_sync" not in {c.value for c in capabilities.Capability}

    def test_function_is_gone(self):
        assert not hasattr(settings, "sync_welcome_screen_settings")
        assert not hasattr(cleaner, "sync_welcome_screen_settings")

    def test_powershell_script_is_gone(self):
        script = Path(__file__).parent / "scripts" / "layout_cleaner_sync_welcome.ps1"
        assert not script.exists(), "скрипт синхронизации Экрана приветствия удалён"

    def test_cli_flag_and_timeout_are_gone(self):
        """CLI живёт под ``if __name__``, поэтому флаг проверяем по исходнику."""
        source = (Path(__file__).parent / "cleaner.py").read_text(encoding="utf-8")
        assert "--sync-welcome" not in source
        assert "sync_welcome" not in source
        assert "powershell_welcome_sync" not in config.TIMEOUTS

    def test_delete_layout_report_has_no_welcome_fields(self, monkeypatch, tmp_path):
        """Удаление по-прежнему ничего не пишет про Экран приветствия."""
        fake = FakeWinreg()
        fake.set(
            fake.HKEY_CURRENT_USER,
            cleaner._SETTINGSYNC_GROUPS_SUBKEY,
            values={"Enabled": 1},
        )
        harness = TestDeleteLayoutCloudSyncOptIn()
        harness._harness(monkeypatch, fake, tmp_path)
        # Права админа больше не имеют никакого отношения к Экрану приветствия.
        monkeypatch.setattr(cleaner, "is_admin", lambda: True)

        result = cleaner.delete_layout(self.KLID)

        assert "welcome_screen_synced" not in result
        assert "welcome_screen_sync_detail" not in result
        assert result["success"] is True

    def test_welcome_key_removed_from_en_base(self):
        """en.json — база i18n.set_language: ключ не должен вернуться из неё."""
        data = json.loads(
            (Path(__file__).parent / "locales" / "en.json").read_text(encoding="utf-8")
        )
        assert "dlg_result_welcome_sync" not in data

    def test_welcome_key_removed_from_all_locales(self):
        """Ключ удалён из всех локалей — строка не всплывёт ни на одном языке."""
        for p in sorted((Path(__file__).parent / "locales").glob("*.json")):
            data = json.loads(p.read_text(encoding="utf-8"))
            assert "dlg_result_welcome_sync" not in data, p.name


# ---------------------------------------------------------------------------
# FIX-13: страховка остановленных служб ввода (маркер + аварийный запуск)
# ---------------------------------------------------------------------------
class TestInputServicesRescue:
    """Маркер «службы остановлены» и ensure_input_services_running (FIX-13)."""

    @pytest.fixture(autouse=True)
    def _marker_to_tmp(self, monkeypatch, tmp_path):
        """Маркер направляем во временный каталог — не трогаем реальный app dir."""
        marker = tmp_path / "input_services_suspended.marker"
        monkeypatch.setattr(cleaner, "_input_suspended_marker_path", lambda: marker)
        self.marker = marker

    def test_no_marker_is_noop(self):
        """Без маркера — ничего не делаем и ctfmon не трогаем."""
        with mock.patch.object(cleaner, "_start_ctfmon") as start:
            assert cleaner.ensure_input_services_running() is False
        start.assert_not_called()

    def test_marker_triggers_rescue_and_is_removed(self):
        """Маркер найден → ctfmon запускается, маркер снимается (идемпотентность)."""
        self.marker.write_text("2026-09-18T10:00:00", encoding="utf-8")
        with mock.patch.object(cleaner, "_start_ctfmon", return_value=True) as start:
            assert cleaner.ensure_input_services_running() is True
        start.assert_called_once()
        assert not self.marker.exists()

    def test_marker_removed_even_if_start_fails(self):
        """Сбой _start_ctfmon не оставляет маркер: иначе каждый следующий
        старт будет повторять аварийный запуск (зацикливание попыток).
        Возвращается результат запуска (False), но маркер уже снят."""
        self.marker.write_text("x", encoding="utf-8")
        with mock.patch.object(cleaner, "_start_ctfmon", return_value=False):
            assert cleaner.ensure_input_services_running() is False
        assert not self.marker.exists()

    def test_suspender_writes_and_clears_marker(self):
        """CtfmonSuspender: __enter__ кладёт маркер после успешной остановки,
        __exit__ снимает его после штатного запуска ctfmon."""
        with (
            mock.patch.object(cleaner, "_stop_ctfmon", return_value=True),
            mock.patch.object(cleaner, "_start_ctfmon", return_value=True),
            cleaner.CtfmonSuspender(),
        ):
            assert self.marker.exists()
        assert not self.marker.exists()

    def test_suspender_no_marker_when_stop_failed(self):
        """Остановка не удалась — маркера нет (нечего «восстанавливать»)."""
        with (
            mock.patch.object(cleaner, "_stop_ctfmon", return_value=False),
            mock.patch.object(cleaner, "_start_ctfmon", return_value=True) as start,
            cleaner.CtfmonSuspender(),
        ):
            assert not self.marker.exists()
        # __exit__ всё равно пытается запустить ctfmon (лучше лишний старт,
        # чем остановленный ввод)
        start.assert_called_once()


class TestCloseDuringOperation:
    """Закрытие окна во время операции блокируется (FIX-13)."""

    def test_on_close_blocked_during_operation(self, monkeypatch):
        main = _ensure_main(monkeypatch)
        window = main.KeyboardLayoutCleaner.__new__(main.KeyboardLayoutCleaner)
        window._operation_in_progress = True
        warned = []

        monkeypatch.setattr(
            main,
            "messagebox",
            type(
                "MB",
                (),
                {"showwarning": staticmethod(lambda *a, **k: warned.append(a))},
            ),
        )
        exited = []
        monkeypatch.setattr(window, "_release_mutex_and_exit", lambda: exited.append(1))
        window._on_close()
        assert warned  # пользователю показали предупреждение
        assert not exited  # окно НЕ закрылось во время операции

    def test_on_close_exits_when_idle(self, monkeypatch):
        main = _ensure_main(monkeypatch)
        window = main.KeyboardLayoutCleaner.__new__(main.KeyboardLayoutCleaner)
        window._operation_in_progress = False
        monkeypatch.setattr(window, "_release_mutex_and_exit", lambda: None)
        # Нет исключений — штатный путь закрытия при простое.
        window._on_close()

    def test_close_blocked_key_in_locales(self):
        """Строка предупреждения присутствует во всех локалях."""
        for p in sorted((Path(__file__).parent / "locales").glob("*.json")):
            data = json.loads(p.read_text(encoding="utf-8"))
            assert data.get("dlg_close_blocked"), p.name
def _raise_no_powershell(*args, **kwargs):
    """Заглушка: любой вызов PowerShell в юнит-тесте — ошибка теста."""
    raise FileNotFoundError("powershell отключён в тесте")


class TestScanCleanParity:
    """FIX-7: инвариант «сканер и cleaner видят одно и то же».

    Одно и то же дерево FakeWinreg прогоняется через scan_keyboard_layouts()
    и через plan_layout_removal()/_wipe_branches(): каждая ветка, в которой
    сканер нашёл раскладку, обязана присутствовать в плане/отчёте очистки.
    Тест краснеет при расхождении таблиц/нормализаторов между модулями
    (scanner._normalize_klid_token / _TIP_KLID_RE / LAYOUT_MAP <->
    cleaner._klid_variants / _TIP_KLID_RE / HKL-пересчёт).
    """
    @pytest.fixture(autouse=True)
    def _allow_destructive(self):
        """FIX-29: тест TestScanCleanParity работает С РЕАЛЬНЫМИ разрушительными путями.

        Право выдаётся явно, чтобы читатель теста видел: здесь тест
        действительно пишет в реестр/вызывает PowerShell. Раньше это было
        неявно — достаточно было забыть подмену, и тест писал в живую
        систему (FIX-28).
        """
        capabilities.grant(capabilities.Capability.REGISTRY_MUTATE,
            capabilities.Capability.LANGUAGE_LIST)

    @pytest.fixture(autouse=True)
    def _no_admin_by_default(self, monkeypatch):
        """Права администратора не должны зависеть от того, под кем запущен pytest.

        На сервере GitHub тесты идут от администратора, локально — обычно нет.
        При ``is_admin() == True`` в плане появляется ветка ``HKU\\.DEFAULT``,
        и ``_plan_hits(BR_PRELOAD)`` начинает видеть два значения «1» вместо
        одного — тест падал на CI и проходил локально по одной причине.
        Тесты, которым права нужны, ставят подмену сами.
        """
        monkeypatch.setattr(cleaner, "is_admin", lambda: False)


    BR_PRELOAD = "Keyboard Layout\\Preload"
    BR_SUBST = "Keyboard Layout\\Substitutes"
    BR_INTL = "Control Panel\\International\\User Profile"
    BR_CTF = "Software\\Microsoft\\CTF"
    BR_SYNC = (
        "Software\\Microsoft\\Windows\\CurrentVersion"
        "\\SettingSync\\Namespace\\Language"
    )
    BR_HKU = ".DEFAULT\\Keyboard Layout\\Preload"

    def _build_tree(self, fake: FakeWinreg, layout: str) -> None:
        """Раскладка во всех источниках + «посторонний шум» (другой язык)."""
        other = "0000040c"  # fr-FR — чужая раскладка, её трогать нельзя
        fake.set(fake.HKEY_CURRENT_USER, self.BR_PRELOAD,
                 values={"1": layout, "2": other})
        fake.set(fake.HKEY_CURRENT_USER, self.BR_SUBST,
                 values={layout: "00000409", other: "0000040c"})
        fake.set(
            fake.HKEY_CURRENT_USER,
            self.BR_INTL,
            values={"Languages": ["en-US", "ru-RU"]},  # активные языки
        )
        fake.set(fake.HKEY_CURRENT_USER, self.BR_INTL + "\\en-US",
                 values={"KeyboardLayoutPreload": "0409:00000409"})
        fake.set(fake.HKEY_CURRENT_USER, self.BR_INTL + "\\ru-RU",
                 values={"KeyboardLayoutPreload": f"0419:{layout}"})
        # CTF: HKL = (LANGID << 16) | base_low, десятичное значение
        langid = int(layout[4:], 16)  # LANGID из младшей части KLID
        base_low = int(layout[4:], 16)  # base_low = KLID
        hkl_dec = str((langid << 16) | base_low)
        ctf_key = (
            f"{self.BR_CTF}\\SortOrder\\AssemblyItem\\0x{langid:08x}\\{{E}}\\0"
        )
        fake.set(fake.HKEY_CURRENT_USER, ctf_key,
                 values={"KeyboardLayout": hkl_dec})
        # CTF SortOrder/Language — числовые имена значений (00000000, ...) →
        # содержат KLID как строку. Это отдельная ветка (не AssemblyItem).
        fake.set(
            fake.HKEY_CURRENT_USER,
            self.BR_CTF + "\\SortOrder\\Language",
            values={"00000000": layout},
        )
        # SettingSync хранит BCP-47-тег
        fake.set(fake.HKEY_CURRENT_USER, self.BR_SYNC,
                 values={"ru-RU": layout})
        fake.set(fake.HKEY_USERS, self.BR_HKU, values={"1": layout})

    @staticmethod
    def _patch(monkeypatch, fake: FakeWinreg) -> None:
        """FakeWinreg во всех модулях + заглушки PowerShell.

        FIX-10 (шаги 2 и 4). На шаге 2 пропуск ``mutate`` стоил access
        violation: в НАСТОЯЩИЙ winreg уходили фейковые корни 1/2/3. На шаге 4
        тем же способом выпал бы ``settings``. Поэтому список берётся из
        ``conftest.registry_modules()``, а не набирается вручную: вручную он
        устаревает при каждом переносе и об этом не сообщает.
        """
        for module in registry_modules():
            monkeypatch.setattr(module, "winreg", fake)
            if hasattr(module, "_ROOT_CONST"):
                monkeypatch.setattr(module, "_ROOT_CONST", _fake_root_consts(fake))
        # PowerShell недоступен: скан-источник и PS-скрипты не запускаются
        monkeypatch.setattr(scanner, "run_hidden", _raise_no_powershell)
        monkeypatch.setattr(langlist, "run_hidden", _raise_no_powershell)

    @staticmethod
    def _plan_hits(plan: dict, branch_marker: str) -> list[str]:
        """Значения/ключи из плана для веток, чей путь содержит marker."""
        out: list[str] = []
        for path, entry in plan["branches"].items():
            if branch_marker in path:
                out.extend(entry.get("values") or [])
                out.extend(entry.get("profile_keys") or [])
        return out

    @staticmethod
    def _wipe_fields(report: dict) -> list[str]:
        out: list[str] = []
        for field in (
            "hkcu_preload_deleted",
            "hkcu_substitutes_deleted",
            "hkcu_intl_deleted",
            "hkcu_ctf_deleted",
            "hku_default_deleted",
        ):
            out.extend(report.get(field) or [])
        return out

    def test_scanner_finds_layout_in_all_sources(self, monkeypatch):
        fake = FakeWinreg()
        self._build_tree(fake, "00000419")
        self._patch(monkeypatch, fake)
        scan = scanner.scan_keyboard_layouts()
        assert "00000419" in scan, "сканер не нашёл раскладку из дерева"
        paths = "\n".join(loc["path"] for loc in scan["00000419"])
        for marker in (
            "Preload",
            "Substitutes",
            "User Profile",
            "CTF",
            "SettingSync",
        ):
            assert marker in paths, f"сканер пропустил ветку: {marker}"

    def test_parity_plan_matches_scan(self, monkeypatch):
        """Каждая ветка со скан-совпадениями подтверждается планом cleaner."""
        fake = FakeWinreg()
        self._build_tree(fake, "00000419")
        self._patch(monkeypatch, fake)
        # HKU\.DEFAULT требует админ-прав: мокаем, чтобы план охватил все 5 веток
        monkeypatch.setattr(cleaner, "is_admin", lambda: True)
        scan = scanner.scan_keyboard_layouts()
        plan = cleaner.plan_layout_removal("00000419")

        # Сканер находит 00000419 в 4 ветках HKCU (+ HKU при админ-правах)
        # HKU\.DEFAULT содержит тот же суффикс "Preload", поэтому BR_PRELOAD
        # пересекается с ним — отбрасываем HKU-записи из HKCU-проверки
        preload_hits = []
        for path, entry in plan["branches"].items():
            if self.BR_PRELOAD in path and not path.startswith("HKU"):
                preload_hits.extend(entry.get("values") or [])
        assert preload_hits == ["1"]
        assert self._plan_hits(plan, self.BR_SUBST) == ["00000419"]
        # User Profile: TIP-запись ru-RU; en-US не затронут
        intl_hits = self._plan_hits(plan, self.BR_INTL)
        assert "ru-RU\\KeyboardLayoutPreload" in intl_hits
        assert not any("en-US" in v for v in intl_hits)
        # SettingSync: BCP-47-тег
        assert self._plan_hits(plan, self.BR_SYNC) == ["ru-RU"]
        # HKU .DEFAULT (админ-контекст)
        assert "1" in self._plan_hits(plan, self.BR_HKU)

        # HKL-форма той же раскладки (FIX-34). Запись CTF лежит в реестре
        # как 68748313 = 0x04190419, и РАНЬШЕ сканер выдавал её отдельной
        # строкой «04190419» — второй раз та же раскладка, без имени.
        # Теперь она входит в строку 00000419, а исходный код сохранён.
        assert "04190419" not in scan, "HKL-форма снова отделилась в свою строку"
        hkl_records = [loc for loc in scan["00000419"] if "AssemblyItem" in loc["value"]]
        assert hkl_records, "сканер потерял HKL-запись CTF"
        hkl_dec = str((0x419 << 16) | 0x419)
        assert [loc["form"] for loc in hkl_records] == [hkl_dec], hkl_records
        # Паритет: план для канонического KLID покрывает и эту запись —
        # иначе объединение строк было бы враньём (нашли, но не почистили).
        assert any("AssemblyItem" in h for h in self._plan_hits(plan, self.BR_CTF))
        # HKL-форма остаётся рабочим входом для планирования
        plan_hkl = cleaner.plan_layout_removal("04190419")
        assert self._plan_hits(plan_hkl, self.BR_CTF), plan_hkl["branches"]
        # При этом HKL-план не трогает чужие ветки (симметрия скан/план)
        assert not self._plan_hits(plan_hkl, self.BR_PRELOAD)

    def test_parity_wipe_matches_scan(self, monkeypatch, tmp_path):
        """Реальная очистка удаляет всё, что нашёл сканер, и ничего больше."""
        fake = FakeWinreg()
        self._build_tree(fake, "00000419")
        self._patch(monkeypatch, fake)

        monkeypatch.setattr(cleaner, "_system_info", lambda: {})
        monkeypatch.setattr(cleaner, "is_admin", lambda: True)
        monkeypatch.setattr(
            langlist, "_sync_language_list_via_powershell",
            lambda klid: (True, "SUCCESS"),
        )

        class _NoopSuspender:
            started = False

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(cleaner, "CtfmonSuspender", _NoopSuspender)
        monkeypatch.setattr(
            langlist, "_backup_language_list", lambda base: ""
        )
        monkeypatch.setattr(
            cleaner, "backup_registry",
            lambda paths, backup_dir=None, report=None: str(tmp_path / "b.reg"),
        )

        result = cleaner.delete_layout("00000419")

        assert result["error"] == "", result["error"]
        assert result["success"] is True
        # Дрейфа нет: cleaner удалил ровно то, что нашёл сканер
        assert result["plan_drift"] == [], result["plan_drift"]
        assert result["hkcu_preload_deleted"] == ["1"]
        assert result["hkcu_substitutes_deleted"] == ["00000419"]
        # FIX-37h: помимо значений, которые нашёл сканер, убирается САМ ЯЗЫК —
        # ru остался без раскладок, поэтому он вычеркнут из Languages, а его
        # профиль удалён. en-US в списке остаётся: он не удаляемая раскладка.
        assert result["hkcu_intl_deleted"] == [
            "ru-RU\\KeyboardLayoutPreload",
            "Languages en-US|ru-RU -> en-US",
            "ru-RU <профиль языка удалён>",
        ]
        assert result["hkcu_settingsync_deleted"] == ["ru-RU"]
        assert result["hku_default_deleted"] == ["1"]
        assert result["hkcu_ctf_deleted"] == ["SortOrder\\Language\\00000000"]
        assert any(
            "SortOrder\\AssemblyItem" in p and "профиль TSF" in p
            for p in result["ctf_profiles_deleted"]
        ), result["ctf_profiles_deleted"]

        # Скан после очистки: целевой KLID исчез ВМЕСТЕ с его HKL-записью
        # (FIX-34: раньше проверяли отдельную строку «04190419»)
        scan_after = scanner.scan_keyboard_layouts()
        assert "00000419" not in scan_after
        assert "04190419" not in scan_after
        hkl_dec = str((0x419 << 16) | 0x419)
        assert not [
            loc for locs in scan_after.values() for loc in locs
            if loc.get("form") == hkl_dec
        ], "HKL-запись CTF пережила очистку"
        # Чужие значения уцелели
        assert "0000040c" in scan_after
        assert "00000409" in scan_after

    def test_parity_catches_scanner_matcher_drift(self, monkeypatch):
        """Подмена TIP-механики только в scanner → паритет нарушается."""
        fake = FakeWinreg()
        self._build_tree(fake, "00000419")
        self._patch(monkeypatch, fake)
        no_match = type("NoMatch", (), {"match": staticmethod(lambda s: None)})
        monkeypatch.setattr(scanner, "_TIP_KLID_RE", no_match)
        scan = scanner.scan_keyboard_layouts()
        # Сканер «ослеп» к TIP-записи User Profile: было 4 ветки
        paths = "\n".join(loc["path"] for loc in scan.get("00000419", []))
        assert "User Profile" not in paths, scan

    def test_parity_catches_cleaner_matcher_drift(self, monkeypatch):
        """Подмена TIP-механики только в cleaner → план теряет запись."""
        fake = FakeWinreg()
        self._build_tree(fake, "00000419")
        self._patch(monkeypatch, fake)
        no_match = type("NoMatch", (), {"match": staticmethod(lambda s: None)})
        # FIX-10 (шаг 2): предикат очистки живёт в mutate — подменять надо
        # его. Подмена cleaner._TIP_KLID_RE после переноса не действовала бы
        # (cleaner только реэкспортирует), и «дрейф» оказался бы недоказуем.
        monkeypatch.setattr(mutate, "_TIP_KLID_RE", no_match)
        plan = cleaner.plan_layout_removal("00000419")
        # План «ослеп» к TIP-записи, хотя сканер её находит
        assert not self._plan_hits(plan, self.BR_INTL)
        # Остальные механизмы cleaner продолжают работать
        assert self._plan_hits(plan, self.BR_PRELOAD) == ["1"]
        assert self._plan_hits(plan, self.BR_CTF)

    def test_parity_no_false_positive_for_absent_layout(self, monkeypatch):
        """Критерий приёмки FIX-7: для НЕ существующей в дереве раскладки
        обе стороны дают ноль — ни сканер, ни план.

        Без этой половины инварианта тест краснеет только на «пропуске»:
        слишком широкий матчинг (например, подстрочный поиск) даёт лишние
        совпадения, а паритет scan↔plan этого не замечает — обе стороны
        одинаково неправы.
        """
        fake = FakeWinreg()
        self._build_tree(fake, "00000419")
        self._patch(monkeypatch, fake)
        monkeypatch.setattr(cleaner, "is_admin", lambda: True)

        absent = "00000809"  # en-GB в дереве отсутствует
        assert absent not in scanner.scan_keyboard_layouts()
        plan = cleaner.plan_layout_removal(absent)
        assert not self._plan_hits(plan, self.BR_PRELOAD)
        assert not self._plan_hits(plan, self.BR_SUBST)
        assert not self._plan_hits(plan, self.BR_INTL)
        assert not self._plan_hits(plan, self.BR_CTF)
        assert not self._plan_hits(plan, self.BR_SYNC)

    def test_parity_klid_variants_and_normalizer_agree(self, monkeypatch):
        """HKL-формы: scanner._normalize_klid_token и cleaner._klid_variants
        обязаны приводить одно и то же значение к одному и тому же KLID.

        Это ядро дублирования knowledge из FIX-7: два независимых
        нормализатора — источник расхождения «сканер нашёл, cleaner нет».
        """
        cases = [
            # десятичный HKL, записанный в CTF
            ("68748313", r"...\0x00000419", "04190419"),
            ("67699721", r"...\0x00000409", "04090409"),
            # короткий десятичный LANGID
            ("1033", "", "00000409"),
            # уже канонический hex
            ("00000809", "", "00000809"),
        ]
        for raw, key_path, expected in cases:
            normalized = scanner._normalize_klid_token(raw, key_path)
            assert normalized == expected, (raw, normalized)
            # cleaner обязан уметь сопоставить исходное значение (а не только
            # канонический hex) — иначе очистка не найдёт то, что нашёл сканер.
            assert normalized in cleaner._klid_variants(expected), (
                raw, normalized, cleaner._klid_variants(expected)
            )

    def test_parity_catches_klid_variants_drift(self, monkeypatch):
        """Подмена набора вариантов ТОЛЬКО в cleaner → план слепнет.

        Регрессия FIX-7: раньше подмена одного нормализатора не делала
        тесты красными, потому что паритет проверялся лишь на TIP-механике.

        Дерево строится отдельно: обычное (не профильное) значение CTF с
        десятичным HKL. Профильные ключи TSF исключены намеренно — их
        отбор идёт через HKL, вычисленный из имени ключа ``0x????????``,
        и от ``_klid_variants`` не зависит.
        """
        fake = FakeWinreg()
        hkl_decimal = "68748313"  # 0x04190419 — язык 0419 + раскладка 00000419
        fake.set(
            fake.HKEY_CURRENT_USER,
            self.BR_CTF + "\\SortOrder\\Language",
            values={"00000001": hkl_decimal},
        )
        self._patch(monkeypatch, fake)

        # Нормальный режим: сканер и план видят запись. Сканер кладёт её в
        # строку KLID (FIX-34), и план по этому же KLID обязан её покрыть —
        # именно это и проверяет тест: обе стороны говорят об одном KLID.
        scan = scanner.scan_keyboard_layouts()
        assert scan.get("00000419"), scan
        assert scan["00000419"][0]["form"] == hkl_decimal
        plan_ok = cleaner.plan_layout_removal("00000419")
        assert self._plan_hits(plan_ok, self.BR_CTF), plan_ok["branches"]

        # Подмена предикатов очистки → расхождение: сканер находит, план нет.
        # С FIX-34 запись в CTF опознаётся ДВУМЯ независимыми способами:
        # набором вариантов и сверкой HKL по младшему слову. Поэтому дрейф
        # воспроизводится только когда сломаны оба — иначе тест проверял бы
        # несуществующий дефект. Смысл теста прежний: паритет способен
        # падать, когда очистка слепнет.
        monkeypatch.setattr(
            mutate, "_klid_variants", lambda klid: {klid.strip().lower()}
        )
        monkeypatch.setattr(
            mutate.layout_ids, "hkl_matches_klid", lambda value, klids: False
        )
        plan_blind = cleaner.plan_layout_removal("00000419")
        assert not self._plan_hits(plan_blind, self.BR_CTF)
        assert scanner.scan_keyboard_layouts().get("00000419")

    def test_parity_metadata_names_shared_between_modules(self):
        """Фильтр метаданных языкового профиля — тоже общее знание.

        FIX-10 (шаг 2): предикаты сопоставления живут в mutate.
        """
        assert scanner._METADATA_VALUE_NAMES is mutate._METADATA_VALUE_NAMES
