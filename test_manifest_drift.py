"""
test_manifest_drift.py — Unit-тесты FIX-6: sidecar-манифест и дрейф плана.
"""

import contextlib
import json
import os
import tempfile
import types
from pathlib import Path
from typing import ClassVar
from unittest import mock

import pytest

import backup
import capabilities
import cleaner
import langlist
from conftest import FakeWinreg, _ensure_main, fake_root_consts, registry_modules


class _MultiPatch:
    def __init__(self, *patches):
        self._patches = patches

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


def _install_fake_winreg(fake):
    """Подменить winreg во всех модулях, работающих с реестром.

    FIX-10 (шаги 1-4): модули берутся из ``conftest.registry_modules()``.
    Перечисление вручную устаревало при каждом переносе кода, и на шаге 4
    новый ``settings`` выпал — и ``disable_language_sync()`` ушёл бы в
    настоящий реестр машины, на которой шли тесты.
    """
    roots = fake_root_consts(fake)
    patches = []
    for module in registry_modules():
        patches.append(mock.patch.object(module, "winreg", fake))
        if hasattr(module, "_ROOT_CONST"):
            patches.append(mock.patch.object(module, "_ROOT_CONST", roots))
    return _MultiPatch(*patches)


def _tmp_dir():
    return tempfile.mkdtemp(prefix="klc_test_")


class TestManifestGeneration:
    """_build_manifest: creates sidecar .manifest.json."""

    def test_build_manifest_creates_json(self):
        fake = FakeWinreg()
        klid = "00000419"
        branches_now = [
            ("HKCU", "Keyboard Layout\\Preload", "preload", False),
            ("HKCU", "Keyboard Layout\\Substitutes", "substitutes", False),
        ]
        report = {
            "exported": ["HKCU\\Keyboard Layout\\Preload"],
            "failed": [],
            "skipped": ["HKCU\\Keyboard Layout\\Substitutes"],
        }
        tmp_dir = Path(_tmp_dir())
        with _install_fake_winreg(fake):
            fake.set(
                fake.HKEY_CURRENT_USER, "Keyboard Layout\\Preload", values={"3": klid}
            )
            manifest_path = cleaner._build_manifest(
                klid, str(tmp_dir / "backup.reg"),
                branches_now, False, report,
            )
            assert manifest_path is not None
            manifest_file = tmp_dir / "backup.manifest.json"
            assert manifest_file.is_file()
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
            assert data["manifest_version"] == "1.0"
            assert data["operation"] == "backup_and_delete"
            assert data["klid"] == klid
            assert data["backup_file"] == "backup.reg"
            assert "created_at" in data
            assert "snapshot" in data
            assert "branch_summary" in data

    def test_build_manifest_returns_none_on_failure(self):
        fake = FakeWinreg()
        klid = "00000419"
        branches_now = [("HKCU", "Keyboard Layout\\Preload", "preload", False)]
        report = {"exported": [], "failed": [], "skipped": []}
        with _install_fake_winreg(fake):
            manifest_path = cleaner._build_manifest(
                klid, os.path.join(_tmp_dir(), "missing-dir", "backup.reg"),
                branches_now, False, report,
            )
            assert manifest_path is None

    def test_build_manifest_uses_provided_snapshot(self):
        """Снимок ДО мутации передаётся снаружи — манифест не снимает состояние
        повторно (иначе после удаления он всегда показывал бы нули)."""
        fake = FakeWinreg()
        klid = "00000419"
        branches_now = [("HKCU", "Keyboard Layout\\Preload", "preload", False)]
        pre_mutation = {
            "HKCU\\Keyboard Layout\\Preload": {
                "admin_required": False,
                "values": [{"name": "2", "type": "REG_SZ", "value": klid}],
            }
        }
        tmp_dir = Path(_tmp_dir())
        with _install_fake_winreg(fake):
            # В реестре значения уже нет — как после успешного удаления.
            manifest_path = cleaner._build_manifest(
                klid, str(tmp_dir / "backup.reg"),
                branches_now, False, {"exported": [], "failed": [], "skipped": []},
                pre_mutation,
            )
            data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            assert data["snapshot"]["total_values"] == 1


class TestDetectPlanDrift:
    """_detect_plan_drift: compares plan vs actual state."""

    def test_no_drift_all_removed(self):
        fake = FakeWinreg()
        klid = "00000419"
        branches_now = [("HKCU", "Keyboard Layout\\Preload", "preload", False)]
        snapshot = {
            "HKCU\\Keyboard Layout\\Preload": {
                "admin_required": False,
                "values": [{"name": "3", "type": "REG_SZ", "value": klid}],
            }
        }
        with _install_fake_winreg(fake), mock.patch.object(
            cleaner, "_settingsync_groups_paths", return_value=[]
        ):
                drift = cleaner._detect_plan_drift(branches_now, klid, False, snapshot)
                assert drift == []

    def test_drift_detected_when_value_remaining(self):
        fake = FakeWinreg()
        klid = "00000419"
        branches_now = [("HKCU", "Keyboard Layout\\Preload", "preload", False)]
        snapshot = {
            "HKCU\\Keyboard Layout\\Preload": {
                "admin_required": False,
                "values": [
                    {"name": "3", "type": "REG_SZ", "value": klid},
                    {"name": "4", "type": "REG_SZ", "value": "00001234"},
                ],
            }
        }
        fake.set(fake.HKEY_CURRENT_USER, "Keyboard Layout\\Preload", values={"3": klid})
        with _install_fake_winreg(fake), mock.patch.object(
            cleaner, "_settingsync_groups_paths", return_value=[]
        ):
                drift = cleaner._detect_plan_drift(branches_now, klid, False, snapshot)
                assert len(drift) > 0
                assert drift[0] == "2"
                assert drift[1] == "1"
                assert "3" in drift[2]


class TestSnapshotValuesBeforeDelete:
    """_snapshot_values_before_delete: snapshots values before deletion."""

    def test_snapshot_captures_preload_values(self):
        fake = FakeWinreg()
        klid = "00000419"
        branches_now = [("HKCU", "Keyboard Layout\\Preload", "preload", False)]
        fake.set(
            fake.HKEY_CURRENT_USER, "Keyboard Layout\\Preload",
            values={"3": klid, "4": "00000409"},
        )
        with _install_fake_winreg(fake), mock.patch.object(
            cleaner, "_settingsync_groups_paths", return_value=[]
        ):
                snapshot = cleaner._snapshot_values_before_delete(branches_now, klid, False)
                assert "HKCU\\Keyboard Layout\\Preload" in snapshot
                entry = snapshot["HKCU\\Keyboard Layout\\Preload"]
                assert len(entry["values"]) == 1

    def test_snapshot_excludes_non_matching_values(self):
        fake = FakeWinreg()
        target_klid = "00000419"
        branches_now = [("HKCU", "Keyboard Layout\\Preload", "preload", False)]
        fake.set(fake.HKEY_CURRENT_USER, "Keyboard Layout\\Preload", values={"3": target_klid})
        with _install_fake_winreg(fake), mock.patch.object(
            cleaner, "_settingsync_groups_paths", return_value=[]
        ):
                snapshot = cleaner._snapshot_values_before_delete(branches_now, target_klid, False)
                entry = snapshot["HKCU\\Keyboard Layout\\Preload"]
                assert len(entry["values"]) == 1
                assert entry["values"][0]["value"] == target_klid

    def test_snapshot_empty_branch(self):
        fake = FakeWinreg()
        klid = "00000419"
        branches_now = [("HKCU", "Keyboard Layout\\Preload", "preload", False)]
        with _install_fake_winreg(fake), mock.patch.object(
            cleaner, "_settingsync_groups_paths", return_value=[]
        ):
                snapshot = cleaner._snapshot_values_before_delete(branches_now, klid, False)
                assert "HKCU\\Keyboard Layout\\Preload" in snapshot
                assert len(snapshot["HKCU\\Keyboard Layout\\Preload"]["values"]) == 0


class TestCountSnapshotValues:
    """_count_snapshot_values: counts planned values from snapshot."""

    def test_count_multiple_values(self):
        snapshot = {
            "HKCU\\Keyboard Layout\\Preload": {
                "admin_required": False,
                "values": [
                    {"name": "3", "type": "REG_SZ", "value": "00000419"},
                    {"name": "4", "type": "REG_SZ", "value": "00000419"},
                ],
            },
            "HKCU\\Keyboard Layout\\Substitutes": {
                "admin_required": False,
                "values": [{"name": "00000419", "type": "REG_SZ", "value": "00000409"}],
            },
        }
        assert cleaner._count_snapshot_values(snapshot) == 3

    def test_count_with_profile_keys(self):
        snapshot = {
            "HKCU\\Software\\Microsoft\\CTF\\Layouts": {
                "admin_required": False,
                "values": [],
                "profile_keys": ["00000419", "00000419-00000409"],
            },
        }
        assert cleaner._count_snapshot_values(snapshot) == 2

    def test_empty_snapshot(self):
        assert cleaner._count_snapshot_values({}) == 0

    def test_count_single_preload(self):
        snapshot = {
            "HKCU\\Keyboard Layout\\Preload": {
                "admin_required": False,
                "values": [{"name": "3", "type": "REG_SZ", "value": "00000419"}],
            },
        }
        assert cleaner._count_snapshot_values(snapshot) == 1

class TestRecursiveRemainingScan:
    """_scan_remaining_recursive / _find_remaining_values на рекурсивных ветках.

    Регрессия FIX-6: обход вызывал _scan_remaining_recursive_inner с неверным
    набором аргументов (``variants`` уезжал в ``match_mode``), из-за чего
    ЛЮБОЙ реальный прогон delete_layout падал с TypeError уже ПОСЛЕ мутации
    реестра — отчёт терялся, а пользователь видел исключение в потоке.
    """

    def test_ctf_branch_returns_remaining_value(self):
        fake = FakeWinreg()
        klid = "00000419"
        fake.set(
            fake.HKEY_CURRENT_USER,
            "Software\\Microsoft\\CTF\\SortOrder\\AssemblyItem\\00000000",
            values={"KeyboardLayout": klid},
        )
        with _install_fake_winreg(fake):
            found = cleaner._scan_remaining_recursive(
                fake.HKEY_CURRENT_USER,
                "Software\\Microsoft\\CTF",
                klid,
                cleaner._klid_variants(klid),
                "preload",
            )
        assert found == [
            "Software\\Microsoft\\CTF\\SortOrder\\AssemblyItem\\00000000\\KeyboardLayout"
        ]

    def test_find_remaining_values_walks_ctf_branch(self):
        """Значение в SortOrder\\Language: ветка не профильная, обход обязателен."""
        fake = FakeWinreg()
        klid = "00000419"
        fake.set(
            fake.HKEY_CURRENT_USER,
            "Software\\Microsoft\\CTF\\SortOrder\\Language",
            values={"00000000": klid},
        )
        with _install_fake_winreg(fake):
            remaining = cleaner._find_remaining_values(
                [("HKCU", "Software\\Microsoft\\CTF", "preload", False)], klid, False
            )
        assert len(remaining) == 1
        assert remaining[0].endswith("SortOrder\\Language\\00000000")

    def test_find_remaining_values_reports_ctf_profile_key(self):
        """Профильные ключи CTF проверяются тем же вызовом, что и снимок."""
        fake = FakeWinreg()
        klid = "00000419"
        fake.set(
            fake.HKEY_CURRENT_USER,
            TestDeleteLayoutManifestIntegration.CTF_PROFILE,
            values={"KeyboardLayout": 68748313},  # 0x04190419 = langid 0419 + KLID 0419
        )
        with _install_fake_winreg(fake):
            remaining = cleaner._find_remaining_values(
                [("HKCU", "Software\\Microsoft\\CTF", "preload", False)], klid, False
            )
        assert len(remaining) == 1
        assert "профиль TSF" in remaining[0]

    def test_remaining_values_are_deduplicated(self):
        """User Profile обходится дважды (ветка + языковой профиль) — остаток
        не должен дублироваться, иначе actual_removed занижается."""
        fake = FakeWinreg()
        klid = "00000419"
        profile = "Control Panel\\International\\User Profile"
        fake.set(fake.HKEY_CURRENT_USER, profile, values={"Languages": ["ru"]})
        fake.set(fake.HKEY_CURRENT_USER, profile + "\\ru", values={"0419:00000419": klid})
        with _install_fake_winreg(fake):
            remaining = cleaner._find_remaining_values(
                [("HKCU", profile, "preload", False)], klid, False
            )
        assert len(remaining) == len(set(remaining)) == 1




class TestValueMatchesParity:
    """_value_matches обязан повторять предикат УДАЛЕНИЯ (иначе ложный дрейф)."""

    def test_recursive_branch_uses_branch_value_matches(self):
        variants = cleaner._klid_variants("00000419")
        # TIP-значение «в теле» значения (KeyboardLayoutPreload = "0419:00000419")
        assert cleaner._value_matches(
            "KeyboardLayoutPreload", "0419:00000419", variants, "preload", True
        ) is cleaner._branch_value_matches(
            "KeyboardLayoutPreload", "0419:00000419", variants, "preload"
        )

    def test_recursive_branch_skips_metadata_names(self):
        assert not cleaner._value_matches(
            "FeaturesToInstall", "00000419", {"00000419"}, "preload", True
        )

    def test_preload_multisz_items_are_compared_elementwise(self):
        """REG_MULTI_SZ из Preload: элемент сравнивается целиком (как в
        _clean_preload_keys), а не как строковое представление списка."""
        variants = cleaner._klid_variants("00000419")
        assert cleaner._value_matches(
            "KeyboardLayoutPreload", ["00000409", "00000419"], variants, "preload", False
        )
        assert not cleaner._value_matches(
            "KeyboardLayoutPreload", ["00000409"], variants, "preload", False
        )

    def test_substitutes_matches_like_deletion(self):
        """Substitutes: сравнение по токенам, а не по подстроке (FIX-22)."""
        variants = cleaner._klid_variants("00000409")
        # Точное совпадение значения-цели подстановки.
        assert cleaner._value_matches(
            "d0010419", "00000409", variants, "substitutes", False, "00000409"
        )
        # Составное значение: токен сравнивается ЦЕЛИКОМ.
        assert cleaner._value_matches(
            "d0010419", "0409:00000409", variants, "substitutes", False, "00000409"
        )
        # Чужая цель не трогается.
        assert not cleaner._value_matches(
            "d0010419", "0000040c", variants, "substitutes", False, "00000409"
        )

    def test_substitutes_does_not_match_on_substring(self):
        """Регрессия FIX-22: «1049» — часть строки «d0011049», но не KLID.

        Раньше в набор вариантов входил «голый» десятичный LANGID (1049 для
        00000419), а сопоставление искало подстроку. Фантомная раскладка
        d0011049 попадала под удаление 00000419.
        """
        variants = cleaner._klid_variants("00000419")
        assert not cleaner._value_matches(
            "d0011049", "0000040c", variants, "substitutes", False, "00000419"
        )

    def test_decimal_langid_is_not_a_klid_form(self):
        """«Голый» LANGID не публикуется как форма KLID (FIX-22)."""
        assert "1033" not in cleaner._klid_hex_forms("00000409")
        assert "1049" not in cleaner._klid_hex_forms("00000419")
        # В полном наборе (для CTF) десятичная форма HKL остаётся.
        assert "1033" in cleaner._klid_variants("00000409")

    def test_decimal_hkl_input_still_resolves(self):
        """Критерий FIX-22: десятичный HKL 67699721 == 0x04090409."""
        forms = cleaner._klid_hex_forms("67699721")
        assert "04090409" in forms
        assert "67699721" in forms

    def test_recursive_branch_still_matches_decimal_hkl(self):
        """CTF хранит HKL десятичным числом — рекурсивный обход обязан его видеть."""
        variants = cleaner._klid_variants("04190419")
        assert cleaner._branch_value_matches(
            "KeyboardLayout", "68748313", variants, "preload", "04190419"
        )


class TestSubstitutesAntiRegression:
    """Тест-антирегрессия FIX-22 на настоящей очистке ветки Substitutes.

    Критерий из плана: значение `1033`/`1049` в Preload/Substitutes/Intl
    НЕ удаляется при удалении `00000409`/`00000419`, а десятичная форма HKL
    обрабатывается по-прежнему.
    """
    @pytest.fixture(autouse=True)
    def _allow_destructive(self):
        """FIX-29: тест TestSubstitutesAntiRegression работает С РЕАЛЬНЫМИ разрушительными путями.

        Право выдаётся явно, чтобы читатель теста видел: здесь тест
        действительно пишет в реестр/вызывает PowerShell. Раньше это было
        неявно — достаточно было забыть подмену, и тест писал в живую
        систему (FIX-28).
        """
        capabilities.grant(capabilities.Capability.REGISTRY_MUTATE)


    SUBST = "Keyboard Layout\\Substitutes"
    PRELOAD = "Keyboard Layout\\Preload"

    def _fake(self):
        fake = FakeWinreg()
        # Фантомная раскладка d0011049: содержит подстроку "1049".
        fake.set(
            fake.HKEY_CURRENT_USER,
            self.SUBST,
            values={"d0011049": "0000040c", "00000419": "00000409"},
        )
        # Точное значение "1049" как LANGID — тоже не раскладка 00000419.
        fake.set(fake.HKEY_CURRENT_USER, self.PRELOAD, values={"1": "1049"})
        return fake

    def test_phantom_with_langid_substring_survives(self):
        fake = self._fake()
        with _install_fake_winreg(fake):
            deleted = cleaner._clean_substitutes_keys(
                fake.HKEY_CURRENT_USER, self.SUBST, "00000419"
            )
        assert "d0011049" not in deleted
        # Целевая раскладка удалена.
        assert "00000419" in deleted

    def test_bare_langid_in_preload_survives(self):
        fake = self._fake()
        with _install_fake_winreg(fake):
            deleted = cleaner._clean_preload_keys(
                fake.HKEY_CURRENT_USER, self.PRELOAD, "00000419"
            )
        assert deleted == []

    def test_decimal_hkl_form_still_deleted(self):
        """Вход как десятичный HKL 67699721 == 0x04090409 (en-US) обязан работать.

        HKL = (LANGID << 16) | KLID, поэтому из него восстанавливается
        канонический KLID младшего слова: 0x0409 -> 00000409.
        """
        fake = FakeWinreg()
        # Подстановка С ЦЕЛЬЮ en-US — её значение совпадает с KLID из HKL.
        # Подстановка с целью fr-FR — посторонняя, остаётся.
        fake.set(
            fake.HKEY_CURRENT_USER,
            self.SUBST,
            values={"0000040c": "00000409", "0000040c2": "00000419"},
        )
        with _install_fake_winreg(fake):
            deleted = cleaner._clean_substitutes_keys(
                fake.HKEY_CURRENT_USER, self.SUBST, "67699721"
            )
        assert "0000040c" in deleted
        assert "0000040c2" not in deleted


class TestDeleteLayoutManifestIntegration:
    """E2E delete_layout (FakeWinreg): манифест до мутации, дрейфа нет."""
    @pytest.fixture(autouse=True)
    def _allow_destructive(self):
        """FIX-29: тест TestDeleteLayoutManifestIntegration работает С РЕАЛЬНЫМИ разрушительными путями.

        Право выдаётся явно и ПОЛНО: delete_layout дёргает реестр, PowerShell
        со списком языков и политику синхронизации. Перечисление всех прав
        здесь — это же и список того, что этот тест способен сломать.
        """
        capabilities.grant(
            capabilities.Capability.REGISTRY_MUTATE,
            capabilities.Capability.LANGUAGE_LIST,
            capabilities.Capability.CLOUD_SYNC_POLICY,
            capabilities.Capability.REG_IMPORT,
        )



    KLID = "00000419"
    PRELOAD = "Keyboard Layout\\Preload"
    CTF = "Software\\Microsoft\\CTF"
    # 68748313 == 0x04190419: LANGID 0x0419 из имени ключа + low word KLID 0x0419
    CTF_PROFILE = (
        "Software\\Microsoft\\CTF\\SortOrder\\AssemblyItem\\0x00000419"
        "\\{11111111-2222-3333-4444-555555555555}\\00000000"
    )
    CTF_PROFILE_HKL = 68748313
    BRANCHES: ClassVar[list] = [
        ("HKCU", PRELOAD, "preload", False),
        ("HKCU", CTF, "preload", False),
    ]

    def _patch_env(self, monkeypatch, tmp, tmp_path):
        monkeypatch.setattr(cleaner, "_system_info", lambda: {})
        monkeypatch.setattr(cleaner, "is_admin", lambda: False)
        monkeypatch.setattr(cleaner, "_affected_branches", lambda: list(self.BRANCHES))
        monkeypatch.setattr(cleaner, "_current_preload_klids", lambda admin: {"00000409"})
        monkeypatch.setattr(
            langlist, "_sync_language_list_via_powershell", lambda klid: (True, "SUCCESS")
        )
        # FIX-28 (P1): `cleaner` импортирует `_sync_language_list_via_powershell`
        # ИМЕНЕМ, поэтому подмена только `langlist._sync_...` на путь
        # delete_layout не действовала — он звал НАСТОЯЩИЙ
        # langlist._run_cleanup_script, а тот запускал layout_cleaner_cleanup.ps1
        # с -Apply по ЖИВОМУ списку языков. С KLID = 00000419 это удаляло
        # русскую раскладку у пользователя, запустившего тесты. Подменяем
        # оба края: точку входа в cleaner и сам вызов скрипта.
        monkeypatch.setattr(
            cleaner,
            "_sync_language_list_via_powershell",
            lambda klid: (True, "SUCCESS"),
        )
        monkeypatch.setattr(
            langlist,
            "_run_cleanup_script",
            lambda klid, apply=True: (
                True,
                "SUCCESS",
                {"tips_removed": [f"0419:{klid}"], "languages_removed": []},
            ),
        )
        monkeypatch.setattr(
            langlist, "_backup_language_list", lambda base: str(tmp / "langlist.json")
        )

        class _NoopSuspender:
            started = False

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(cleaner, "CtfmonSuspender", _NoopSuspender)
        backup_file = tmp / "backup.reg"

        def stub_backup(registry_paths, backup_dir=None, report=None):
            backup_file.write_text(
                "Windows Registry Editor Version 5.00", encoding="utf-16"
            )
            if report is not None:
                report["exported"] = ["HKCU\\" + p["subkey"] for p in registry_paths]
                report["failed"] = []
                report["skipped"] = []
            return str(backup_file)

        monkeypatch.setattr(cleaner, "backup_registry", stub_backup)

    def _seed(self, fake):
        fake.set(
            fake.HKEY_CURRENT_USER, self.PRELOAD,
            values={"1": "00000409", "2": self.KLID},
        )
        fake.set(
            fake.HKEY_CURRENT_USER, self.CTF_PROFILE,
            values={"KeyboardLayout": self.CTF_PROFILE_HKL},
        )




    def test_full_delete_reports_no_drift_and_keeps_manifest(self, monkeypatch, tmp_path):
        fake = FakeWinreg()
        self._seed(fake)
        observed: dict = {}
        real_wipe = cleaner._wipe_branches

        def wipe_spy(branches, result, layout_id):
            path = result.get("manifest_path", "")
            if path and "snapshot_at_wipe" not in observed:
                observed["manifest_at_wipe"] = path
                with open(path, encoding="utf-8") as fh:
                    observed["snapshot_at_wipe"] = json.loads(fh.read())
            return real_wipe(branches, result, layout_id)

        with _install_fake_winreg(fake):
            self._patch_env(monkeypatch, tmp_path, tmp_path)
            monkeypatch.setattr(cleaner, "_wipe_branches", wipe_spy)
            result = cleaner.delete_layout(self.KLID)

        assert result["error"] == ""
        assert result["success"] is True
        assert result["plan_drift"] == []
        assert result["manifest_path"]
        # Манифест существует УЖЕ в момент мутации и содержит состояние ДО неё:
        # 1 значение в Preload + 1 профильный ключ CTF.
        assert observed["manifest_at_wipe"] == result["manifest_path"]
        assert observed["snapshot_at_wipe"]["snapshot"]["total_values"] == 2
        preload_left = fake.nodes[(fake.HKEY_CURRENT_USER, self.PRELOAD)]["v"]
        assert set(preload_left) == {"1"}

    def test_drift_reported_when_wipe_is_noop(self, monkeypatch, tmp_path):
        """Служба ввода вернула значение / сбой удаления → дрейф в отчёте."""
        fake = FakeWinreg()
        self._seed(fake)
        with _install_fake_winreg(fake):
            self._patch_env(monkeypatch, tmp_path, tmp_path)
            monkeypatch.setattr(cleaner, "_wipe_branches", lambda branches, result, lid: 0)
            result = cleaner.delete_layout(self.KLID)

        assert result["plan_drift"]
        assert result["plan_drift"][0] == "2"  # планировалось 2 записи
        assert result["plan_drift"][1] == "0"  # фактически удалено 0
        remaining = result["plan_drift"][2:]
        assert len(remaining) == 2
        assert any("Preload" in item for item in remaining)
        assert any("профиль TSF" in item for item in remaining)

    def test_manifest_written_next_to_reg_backup(self, monkeypatch, tmp_path):
        fake = FakeWinreg()
        self._seed(fake)
        with _install_fake_winreg(fake):
            self._patch_env(monkeypatch, tmp_path, tmp_path)
            result = cleaner.delete_layout(self.KLID)
        manifest = Path(result["manifest_path"])
        assert manifest.name == "backup.manifest.json"
        assert manifest.parent == Path(result["backup_path"]).parent
        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert data["manifest_version"] == "1.0"
        assert data["klid"] == self.KLID
        assert data["branch_summary"]["failed"] == []


class TestRestoreWorksWithoutManifest:
    """Приёмка FIX-6: `.reg` — самостоятельный путь восстановления.

    Манифест — аудит, а не условие отката: удаление sidecar-файла (пользователь
    мог перенести только `.reg`, архив мог достаться без него) не должно
    ломать восстановление.
    """
    @pytest.fixture(autouse=True)
    def _allow_destructive(self):
        """FIX-29: тест TestRestoreWorksWithoutManifest работает С РЕАЛЬНЫМИ разрушительными путями.

        Право выдаётся явно, чтобы читатель теста видел: здесь тест
        действительно пишет в реестр/вызывает PowerShell. Раньше это было
        неявно — достаточно было забыть подмену, и тест писал в живую
        систему (FIX-28).
        """
        capabilities.grant(capabilities.Capability.REG_IMPORT)


    @pytest.fixture(autouse=True)
    def _no_live_ctfmon(self, monkeypatch):
        """FIX-28: restore_backup оборачивает откат в CtfmonSuspender.

        Раньше эти два теста подменяли только backup.run_hidden, и
        `cleaner._stop_ctfmon` уходил в НАСТОЯЩИЙ PowerShell: каждый прогон
        pytest убивал живой ctfmon/TextInputHost у машины, на которой шли
        тесты. Побочно рушился и кеш TSF — раскладка пропадала из
        переключателя при «установленном» языке.
        """
        monkeypatch.setattr(cleaner, "_stop_ctfmon", lambda: True)
        monkeypatch.setattr(cleaner, "_start_ctfmon", lambda: True)

    def test_restore_backup_ignores_missing_manifest(self, tmp_path, monkeypatch):
        reg = tmp_path / "backup.reg"
        reg.write_bytes(
            b"\xff\xfe" + (
                "Windows Registry Editor Version 5.00\r\n\r\n"
                '[HKEY_CURRENT_USER\\Keyboard Layout\\Preload]\r\n'
                '"1"="00000419"\r\n'
            ).encode("utf-16-le")
        )
        calls: list[list[str]] = []
        # FIX-10 (шаг 1): reg import выполняет backup.restore_registry_backup,
        # поэтому подменять надо ЕГО run_hidden. Подмена cleaner.run_hidden
        # после переноса не действовала бы и тест пошёл бы в настоящий
        # reg import — восстановление применялось бы к живому реестру.
        monkeypatch.setattr(
            backup,
            "run_hidden",
            lambda cmd, **kw: calls.append(cmd)
            or types.SimpleNamespace(returncode=0, stdout="", stderr=""),
        )
        monkeypatch.setattr(backup, "is_admin", lambda: True)

        result = cleaner.restore_backup(reg)

        assert result["ok"] is True, result["detail"]
        assert any("import" in str(part) for part in calls[0]), calls
        # Манифест не читается: удаление sidecar-файла ничего не меняет.
        assert not (tmp_path / "backup.manifest.json").exists()

    def test_restore_backup_uses_manifest_when_present(self, tmp_path, monkeypatch):
        """Наличие манифеста не должно ломать тот же сценарий."""
        reg = tmp_path / "backup.reg"
        reg.write_bytes(
            b"\xff\xfe" + (
                "Windows Registry Editor Version 5.00\r\n\r\n"
                '[HKEY_CURRENT_USER\\Keyboard Layout\\Preload]\r\n'
                '"1"="00000419"\r\n'
            ).encode("utf-16-le")
        )
        (tmp_path / "backup.manifest.json").write_text(
            json.dumps({"klid": "00000419"}), encoding="utf-8"
        )
        monkeypatch.setattr(
            backup,
            "run_hidden",
            lambda cmd, **kw: types.SimpleNamespace(
                returncode=0, stdout="", stderr=""
            ),
        )
        monkeypatch.setattr(backup, "is_admin", lambda: True)

        assert cleaner.restore_backup(reg)["ok"] is True


class TestRestoreVerifiesSwitcher:
    """FIX-27: откат возвращает реестр, а не весь переключатель.

    ``reg import`` восстанавливает ``Preload``/``CTF``/``User Profile``, но
    список языков живёт в WinRT, а панель переключения дополнительно зависит
    от кеша ``ctfmon``. Поэтому «восстановлено» ≠ «раскладка в переключателе».
    Раньше диалог говорил «Восстановление завершено» и добавлял безусловный
    совет «выйдите и войдите» — независимо от того, было расхождение или нет.
    """
    @pytest.fixture(autouse=True)
    def _allow_destructive(self):
        """FIX-29: тест TestRestoreVerifiesSwitcher работает С РЕАЛЬНЫМИ разрушительными путями.

        Право выдаётся явно, чтобы читатель теста видел: здесь тест
        действительно пишет в реестр/вызывает PowerShell. Раньше это было
        неявно — достаточно было забыть подмену, и тест писал в живую
        систему (FIX-28).
        """
        capabilities.grant(capabilities.Capability.REG_IMPORT)


    @staticmethod
    @contextlib.contextmanager
    def _stub_scripts(monkeypatch, order: list[str]):
        """Подменить PS-вызовы ctfmon и ``reg import``, маркер — во временный каталог.

        ``run_hidden`` подменяется в ДВУХ модулях намеренно: ctfmon-скрипты
        запускает cleaner, а ``reg import`` — backup.restore_registry_backup.
        Мок только в cleaner оставил бы тест выполнять НАСТОЯЩИЙ reg.exe по
        подставному пути (тест проходил бы случайно и зависел бы от прав).
        """
        monkeypatch.setattr(cleaner, "_PS_DIR", Path("."))
        monkeypatch.setattr(cleaner, "run_hidden", lambda *a, **k: mock.Mock(returncode=0))
        monkeypatch.setattr(
            backup,
            "run_hidden",
            lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
        )
        monkeypatch.setattr(backup, "is_admin", lambda: True)
        monkeypatch.setattr(cleaner, "_stop_ctfmon", lambda: order.append("stop") or True)
        monkeypatch.setattr(cleaner, "_start_ctfmon", lambda: order.append("start") or True)
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "input_services_suspended.marker"
            monkeypatch.setattr(cleaner, "_input_suspended_marker_path", lambda: marker)
            yield

    @staticmethod
    def _reg(tmp_path):
        """Валидный .reg-заглушка: BOM + заголовок + одна секция.

        Формат скопирован из соседних тестов намеренно: ``_check_reg_backup_file``
        отсекает файлы меньше ``REG_BACKUP_MIN_BYTES`` и без заголовка
        «Windows Registry Editor Version», поэтому огрызок из одной строки
        приводил бы к отказу по размеру — и тест проверял бы не откат,
        а валидатор файла.
        """
        reg = tmp_path / "b.reg"
        reg.write_bytes(
            b"\xff\xfe"
            + (
                "Windows Registry Editor Version 5.00\r\n\r\n"
                "[HKEY_CURRENT_USER\\Keyboard Layout\\Preload]\r\n"
                '"1"="00000419"\r\n'
            ).encode("utf-16-le")
        )
        return reg

    def test_restore_restarts_input_service(self, monkeypatch, tmp_path):
        """reg import перезаписывает CTF — ctfmon обязан быть остановлен.

        Живой ctfmon держит кеш TSF в памяти и может вернуть его поверх
        только что импортированных значений: откат рапортовал бы об успехе,
        а переключатель остался бы без раскладки. Удаление защищено с
        начала, восстановление — нет (эта асимметрия и породила симптом).
        """
        order: list[str] = []
        with self._stub_scripts(monkeypatch, order):
            cleaner.restore_backup(self._reg(tmp_path))
        assert order == ["stop", "start"], order

    def test_input_service_started_even_when_reg_import_fails(
        self, monkeypatch, tmp_path
    ):
        """Ошибка импорта не должна оставить систему без ввода (FIX-13)."""
        order: list[str] = []
        with self._stub_scripts(monkeypatch, order):
            monkeypatch.setattr(
                cleaner,
                "restore_registry_backup",
                lambda p: {
                    "ok": False,
                    "detail": "boom",
                    "elevated": False,
                    "needs_admin": False,
                },
            )
            result = cleaner.restore_backup(self._reg(tmp_path))
        assert result["ok"] is False
        assert order == ["stop", "start"], order

    def test_ctfmon_result_is_reported(self, monkeypatch, tmp_path):
        """Пользователь должен видеть, перезапустилась ли служба ввода."""
        with self._stub_scripts(monkeypatch, []):
            monkeypatch.setattr(cleaner, "_start_ctfmon", lambda: False)
            result = cleaner.restore_backup(self._reg(tmp_path))
        assert result["ctfmon_restarted"] is False

    def test_restore_reports_switcher_mismatch(self, monkeypatch, tmp_path):
        """Отчёт отката несёт сверку — иначе расхождение останется невидимым."""
        with self._stub_scripts(monkeypatch, []):
            monkeypatch.setattr(
                cleaner,
                "check_switcher_consistency",
                lambda: {"ctf_missing": ["00000409"]},
            )
            result = cleaner.restore_backup(self._reg(tmp_path))
        assert result["switcher_check"] == {"ctf_missing": ["00000409"]}

    def test_restore_survives_broken_diagnostics(self, monkeypatch, tmp_path):
        """Сбой сверки не должен превращать успешный откат в ошибку."""
        with self._stub_scripts(monkeypatch, []):
            monkeypatch.setattr(
                cleaner,
                "check_switcher_consistency",
                lambda: (_ for _ in ()).throw(RuntimeError("diag down")),
            )
            result = cleaner.restore_backup(self._reg(tmp_path))
        assert result["success"] is True
        assert result["switcher_check"] == {}


class TestPlanDriftIsSurfaced:
    """Приёмка FIX-6: дрейф не теряется на пути к пользователю."""

    @staticmethod
    def _window(monkeypatch):
        """Реальный main.py под моками CTk (конвенция conftest._ensure_main)."""
        return _ensure_main(monkeypatch).KeyboardLayoutCleaner

    def test_drift_in_success_dialog(self, monkeypatch):
        cls = self._window(monkeypatch)
        window = cls.__new__(cls)
        report = {
            "power_sync": False,
            "power_sync_detail": "NOCHANGE",
            "language_sync_blocked": False,
            "language_sync_block_detail": "Пропущено",
            "plan_drift": ["3", "1", "Preload\\2"],
            "retry_cleaned": 0,
            "ctfmon_restarted": None,
        }
        msg = window._build_success_message(report, 1)
        assert "3" in msg
        assert "Preload\\2" in msg

    def test_drift_in_failure_dialog(self, monkeypatch):
        """Регрессия FIX-6: сбой операции — типичный источник дрейфа, и раньше
        предупреждение показывалось только при успехе."""
        cls = self._window(monkeypatch)
        window = cls.__new__(cls)
        report = {"error": "OSError: сбой", "plan_drift": ["2", "0", "Preload\\2"]}
        msg = window._build_failure_message(report, "NOCHANGE")
        assert "Preload\\2" in msg, msg

    def test_no_drift_adds_no_warning(self, monkeypatch):
        cls = self._window(monkeypatch)
        window = cls.__new__(cls)
        assert window._format_plan_drift({}) == ""
        assert window._format_plan_drift({"plan_drift": []}) == ""

    def test_switcher_keys_exist_in_all_locales(self):
        """FIX-26/27: ключи сверки переключателя — во всех локалях."""
        import json
        from pathlib import Path

        keys = (
            "dlg_switcher_missing_tip",
            "dlg_switcher_stale_preload",
            "dlg_switcher_advice",
            "dlg_switcher_ctf_missing",
            "dlg_switcher_ctf_advice",
        )
        for p in sorted((Path(__file__).parent / "locales").glob("*.json")):
            data = json.loads(p.read_text(encoding="utf-8"))
            for key in keys:
                assert key in data, f"{p.name}: нет {key}"
                assert data[key].strip(), f"{p.name}: {key} пуст"

    def test_ctf_switcher_keys_placeholder_consistent(self):
        """FIX-27: у ключей про ctf_missing тот же контракт, что у прочих.

        ``{klids}`` обязателен в сообщении (пользователь должен видеть, о
        каких раскладках речь), а совет — без плейсхолдеров. Регрессия
        ловит и «забытый {klids}», и случайно вставленный плейсхолдер.
        """
        import json
        from pathlib import Path

        for p in sorted((Path(__file__).parent / "locales").glob("*.json")):
            data = json.loads(p.read_text(encoding="utf-8"))
            assert "{klids}" in data["dlg_switcher_ctf_missing"], p.name
            assert "{" not in data["dlg_switcher_ctf_advice"], p.name

    def test_ctf_missing_reaches_the_user(self, monkeypatch):
        """FIX-27: ctf_missing больше не уходит только в лог.

        Именно это и было целью диагностики: на живой машине ветка
        CTF\\Assemblies\\0x00000409 отсутствует, а приложение рапортовало
        «успех» — пользователь не понимал, почему раскладка не появилась.
        """
        cls = self._window(monkeypatch)
        window = cls.__new__(cls)
        msg = window._format_switcher_check(
            {"switcher_check": {"ctf_missing": ["00000409"]}}
        )
        assert "00000409" in msg
        # Совет про ctfmon — свой, а не общий «перезагрузите Windows».
        assert "ctfmon" in msg
        assert "00000409" in msg

    def test_ctf_missing_does_not_hide_other_mismatches(self, monkeypatch):
        """Оба вида расхождения показываются, а advice выбирается по ctf."""
        cls = self._window(monkeypatch)
        window = cls.__new__(cls)
        msg = window._format_switcher_check(
            {
                "switcher_check": {
                    "tips_without_preload": ["00000407"],
                    "ctf_missing": ["00000409"],
                }
            }
        )
        assert "00000407" in msg
        assert "00000409" in msg

    def test_clean_switcher_report_is_silent(self, monkeypatch):
        """Согласованная система — никаких предупреждений."""
        cls = self._window(monkeypatch)
        window = cls.__new__(cls)
        assert window._format_switcher_check({}) == ""
        assert (
            window._format_switcher_check(
                {
                    "switcher_check": {
                        "tips_without_preload": [],
                        "preload_without_tip": [],
                        "ctf_missing": [],
                    }
                }
            )
            == ""
        )

    def test_switcher_placeholder_consistent(self):
        """Все локали используют один и тот же плейсхолдер {klids}."""
        import json
        from pathlib import Path

        for p in sorted((Path(__file__).parent / "locales").glob("*.json")):
            data = json.loads(p.read_text(encoding="utf-8"))
            for key in ("dlg_switcher_missing_tip", "dlg_switcher_stale_preload"):
                assert "{klids}" in data[key], f"{p.name}:{key} нет {{klids}}"
            # dlg_switcher_advice — без плейсхолдеров.
            assert "{" not in data["dlg_switcher_advice"], p.name

    def test_drift_key_exists_in_all_locales(self):
        for p in sorted((Path(__file__).parent / "locales").glob("*.json")):
            data = json.loads(p.read_text(encoding="utf-8"))
            text = data.get("dlg_result_plan_drift", "")
            assert text, p.name
            for placeholder in ("{planned}", "{actual}", "{remaining}"):
                assert placeholder in text, f"{p.name}: нет {placeholder}"
