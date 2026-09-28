"""Тесты профиля по умолчанию (FIX-37).

Проверяется не «функция вызвалась», а свойства, из-за которых модуль и
написан: шаблон не трогается без прав и в песочнице, hive выгружается при
любом исходе, а сбой выгрузки отменяет удаление.
"""
import subprocess
from typing import ClassVar

import pytest

import capabilities
import cleaner
import ddu


class FakeReg:
    """Запись вызовов reg.exe с настраиваемыми кодами возврата.

    ``reg query`` по умолчанию ПРОВАЛЕН (код 1) — то есть подгрузки нет.
    Иначе каждый тест считал бы hive уже загруженным и уходил в ветку
    «забытая подгрузка».
    """

    DEFAULTS: ClassVar[dict[str, int]] = {"query": 1}

    def __init__(self, returncodes=None):
        self.calls: list[list[str]] = []
        self.returncodes = {**self.DEFAULTS, **(returncodes or {})}

    def __call__(self, cmd, **kwargs):
        args = list(cmd[1:])  # без "reg"
        self.calls.append(args)
        code = self.returncodes.get(args[0].lower(), 0)
        return subprocess.CompletedProcess(cmd, code, b"", b"")

    def verbs(self) -> list[str]:
        return [c[0].lower() for c in self.calls]


@pytest.fixture
def fake_reg(monkeypatch):
    """Подменить reg.exe; по умолчанию все команды успешны."""
    reg = FakeReg()
    monkeypatch.setattr(ddu.winproc, "run_hidden", reg)
    return reg


@pytest.fixture
def allowed(monkeypatch, tmp_path):
    """Снять все запреты, выдать право на мутацию и подсунуть файл-заглушку.

    Право выдаётся явно: autouse-фикстура ``_no_capabilities_by_default``
    отзывает его перед каждым тестом, и тест обязан объявить, что пишет в
    реестр (FIX-29). Иначе новый модуль можно забыть закрыть — и это будет
    видно только по сломанной раскладке у человека.

    ``capabilities.granted`` — контекстный менеджер, а не прямая выдача,
    поэтому здесь именно ``grant``: фикстура должна оставить право на весь
    тест, а не на время ``with``.
    """
    hive = tmp_path / "NTUSER.DAT"
    hive.write_bytes(b"fake")
    monkeypatch.setenv("KLC_DDU_HIVE", str(hive))
    monkeypatch.setattr(ddu.winapi, "is_user_an_admin", lambda: True)
    monkeypatch.setattr(ddu.config, "is_sandbox_enabled", lambda: False)
    capabilities.grant(capabilities.Capability.REGISTRY_MUTATE)
    return hive


# ---------------------------------------------------------------------------
# Запреты: без них шаблон трогать нельзя
# ---------------------------------------------------------------------------


def test_sandbox_blocks_everything(fake_reg, monkeypatch, tmp_path):
    """Песочница — полный no-op: шаблон общий для машины."""
    monkeypatch.setenv("KLC_DDU_HIVE", str(tmp_path / "NTUSER.DAT"))
    monkeypatch.setattr(ddu.config, "is_sandbox_enabled", lambda: True)

    assert "песочница" in ddu.is_blocked_reason()
    assert ddu.wipe_preload("00000409") == []
    assert ddu.export_preload(tmp_path / "x.reg") is False
    assert ddu.restore_preload(tmp_path / "x.reg") is False
    # Подгрузка не запускалась ни разу — это главное, а не возвращённый [].
    assert fake_reg.calls == []


def test_without_admin_nothing_runs(fake_reg, monkeypatch, allowed):
    """Без прав администратора шаблон недоступен, и это не молчание."""
    monkeypatch.setattr(ddu.winapi, "is_user_an_admin", lambda: False)

    assert "прав" in ddu.is_blocked_reason()
    assert ddu.wipe_preload("00000409") == []
    assert fake_reg.calls == []


def test_missing_hive_file_is_reported(monkeypatch, tmp_path):
    """Нет файла шаблона — внятная причина, а не пустой отчёт."""
    monkeypatch.setenv("KLC_DDU_HIVE", str(tmp_path / "нет.dat"))
    monkeypatch.setattr(ddu.winapi, "is_user_an_admin", lambda: True)
    monkeypatch.setattr(ddu.config, "is_sandbox_enabled", lambda: False)

    assert "не найден" in ddu.is_blocked_reason()


# ---------------------------------------------------------------------------
# Выгрузка hive: главное свойство — она обязана происходить
# ---------------------------------------------------------------------------


def test_mount_denied_without_capability(monkeypatch, tmp_path):
    """Без права модель защиты обязана остановить подгрузку hive.

    Ровно тот класс, что стоил пользователю русской раскладки: операция
    проходит там, где её не ждали. Барьер обязан стоять ВНУТРИ функции
    (FIX-29), иначе достаточно забыть подмену в новом тесте.
    """
    hive = tmp_path / "NTUSER.DAT"
    hive.write_bytes(b"fake")
    monkeypatch.setenv("KLC_DDU_HIVE", str(hive))
    monkeypatch.setattr(ddu.winapi, "is_user_an_admin", lambda: True)
    monkeypatch.setattr(ddu.config, "is_sandbox_enabled", lambda: False)
    calls: list[str] = []

    def run(cmd, **kwargs):
        calls.append(cmd[1])
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(ddu.winproc, "run_hidden", run)

    with pytest.raises(capabilities.CapabilityDeniedError), ddu.mounted():
        pass
    assert calls == [], "reg.exe не должен вызываться без права"


def test_hive_is_unloaded_after_success(fake_reg, allowed):
    with ddu.mounted():
        pass
    assert fake_reg.verbs() == ["query", "load", "unload"]


def test_hive_is_unloaded_after_exception(fake_reg, allowed):
    """Исключение в теле НЕ оставляет hive подгруженным.

    Незакрытый hive держит файл шаблона и может не отдать его следующему
    обновлению Windows — это цена одного забытого finally.
    """
    with pytest.raises(RuntimeError), ddu.mounted():
        raise RuntimeError("сбой в теле")
    assert fake_reg.verbs()[-1] == "unload"


def test_load_failure_raises_without_unload(monkeypatch, allowed):
    """Не загрузились — не выгружаемся: там была бы чужая подгрузка."""
    reg = FakeReg(returncodes={"load": 1})
    monkeypatch.setattr(ddu.winproc, "run_hidden", reg)

    with pytest.raises(ddu.DduError), ddu.mounted():
        pass
    assert "unload" not in reg.verbs()


def test_stale_mount_is_unloaded_before_reload(monkeypatch, allowed):
    """Подгрузка, оставшаяся после сбоя, снимается и не дублируется.

    Имя ``_KLC_DDU`` зарезервировано за модулем, поэтому «занято» значит
    «наше и забытое» — чистим и грузим заново, ровно один раз.
    """
    calls: list[str] = []

    def run(cmd, **kwargs):
        calls.append(next(iter(cmd[1:])).lower())
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(ddu.winproc, "run_hidden", run)
    # Подгрузка «осталась после сбоя» — дальше снимаем её и грузим заново.
    monkeypatch.setattr(ddu, "_is_mounted", lambda: True)
    with ddu.mounted():
        pass
    assert calls.count("load") == 1, calls
    assert calls[0] == "unload", calls
    assert calls[-1] == "unload", calls


# ---------------------------------------------------------------------------
# Выгрузка/импорт ветки Preload
# ---------------------------------------------------------------------------


def test_export_and_restore_run_reg(monkeypatch, allowed, tmp_path):
    dest = tmp_path / "ddu.reg"

    def fake_reg_cmd(args, timeout=30):
        if args[0] == "export":
            dest.write_text("Windows Registry Editor Version 5.00", encoding="utf-16")
        return subprocess.CompletedProcess(["reg", *args], 0, b"", b"")

    monkeypatch.setattr(ddu, "_reg", fake_reg_cmd)

    assert ddu.export_preload(dest) is True
    assert ddu.restore_preload(dest) is True


def test_restore_without_file_is_not_an_error(monkeypatch, allowed, tmp_path):
    """Нет выгрузки — нечего восстанавливать, и это не сбой."""
    monkeypatch.setattr(
        ddu, "_reg", lambda args, timeout=30: pytest.fail("reg не должен вызываться")
    )
    assert ddu.restore_preload(tmp_path / "нет.reg") is False


def test_export_failure_is_reported(monkeypatch, allowed, tmp_path):
    """reg export вернул ошибку — выгрузки нет, это False, а не молчание."""
    monkeypatch.setattr(
        ddu,
        "_reg",
        lambda args, timeout=30: subprocess.CompletedProcess(
            ["reg", *args], 1, b"", b"denied"
        ),
    )
    assert ddu.export_preload(tmp_path / "ddu.reg") is False


# ---------------------------------------------------------------------------
# Интеграция с delete_layout / restore_backup
# ---------------------------------------------------------------------------


def _patch_delete_env(monkeypatch, tmp_path):
    """Минимальное окружение для реального delete_layout."""
    backup_file = tmp_path / "backup.reg"

    def stub_backup(registry_paths, backup_dir=None, report=None):
        backup_file.write_text("hdr", encoding="utf-16")
        if report is not None:
            report["exported"] = []
            report["failed"] = []
            report["skipped"] = []
        return str(backup_file)

    class _NoopSuspender:
        started = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(cleaner, "_system_info", lambda: {})
    monkeypatch.setattr(cleaner, "is_admin", lambda: True)
    monkeypatch.setattr(cleaner, "CtfmonSuspender", _NoopSuspender)
    monkeypatch.setattr(cleaner, "backup_registry", stub_backup)
    monkeypatch.setattr(
        cleaner, "_sync_language_list_via_powershell", lambda klid: (True, "SUCCESS")
    )
    monkeypatch.setattr(cleaner, "_build_manifest", lambda *a, **k: "")
    monkeypatch.setattr(
        cleaner, "_snapshot_values_before_delete", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        cleaner, "_backup_language_list", lambda base: str(tmp_path / "langlist.json")
    )
    monkeypatch.setattr(cleaner, "_detect_plan_drift", lambda *a, **k: [])
    monkeypatch.setattr(cleaner, "_affected_branches", lambda: [])
    monkeypatch.setattr(cleaner, "_current_preload_klids", lambda admin: {"00000419"})
    monkeypatch.setattr(cleaner, "check_switcher_consistency", lambda: {})
    monkeypatch.setattr(ddu, "is_blocked_reason", lambda: "")
    return backup_file


def test_delete_wipes_default_profile(monkeypatch, tmp_path, allowed):
    """Ключевое свойство FIX-37: удаление чистит и шаблон.

    Без этого удаление переживалось ровно до перезагрузки — Windows
    собирал HKU\\.DEFAULT заново и раскладка возвращалась.
    """
    _patch_delete_env(monkeypatch, tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(ddu, "export_preload", lambda dest: True)
    monkeypatch.setattr(ddu, "wipe_preload", lambda klid: calls.append(klid) or ["2"])

    result = cleaner.delete_layout("00000409")

    assert calls == ["00000409", "00000409"], "чистка шаблона должна идти в оба прохода"
    assert result["ddu_preload_deleted"] == ["2", "2"]


def test_delete_is_aborted_when_template_backup_fails(monkeypatch, tmp_path, allowed):
    """Сбой выгрузки шаблона отменяет удаление — fail-closed.

    Шаблон общий для машины: удалить его и не суметь вернуть нельзя.
    """
    _patch_delete_env(monkeypatch, tmp_path)
    monkeypatch.setattr(ddu, "export_preload", lambda dest: False)
    touched: list[str] = []
    monkeypatch.setattr(ddu, "wipe_preload", lambda klid: touched.append(klid) or [])

    result = cleaner.delete_layout("00000409")

    assert result["backup_ok"] is False
    assert result["ddu_backup_error"]
    # Диалог результата читает именно backup_error. Записать причину
    # только в ddu_backup_error — значит показать отказ без объяснения.
    assert result["backup_error"] == result["ddu_backup_error"]
    assert result["success"] is False
    # Ключевое: мутация не должна была начаться вообще. Одной проверки
    # success=False мало — сломанный путь тоже даёт её, поймав исключение
    # широким except в delete_layout.
    assert touched == [], "удаление пошло дальше, хотя шаблон не забэкапен"


def test_delete_continues_when_template_absent(monkeypatch, tmp_path):
    """Шаблона нет (нет прав/файла) — это пропуск, а не отказ операции."""
    _patch_delete_env(monkeypatch, tmp_path)
    monkeypatch.setattr(ddu, "is_blocked_reason", lambda: "нет прав администратора")

    result = cleaner.delete_layout("00000409")

    assert result["success"] is True
    assert result["ddu_preload_deleted"] == []


def test_restore_brings_template_back(monkeypatch, tmp_path, allowed):
    """Откат возвращает и шаблон, иначе пережил бы только до перезагрузки."""
    monkeypatch.setattr(cleaner, "CtfmonSuspender", _NoopSuspender)
    monkeypatch.setattr(cleaner, "restore_registry_backup", lambda p: {"ok": True})
    monkeypatch.setattr(cleaner, "check_switcher_consistency", lambda: {})
    restored: list[str] = []
    monkeypatch.setattr(ddu, "restore_preload", lambda f: restored.append(str(f)) or True)

    reg = tmp_path / "backup.reg"
    reg.write_text("hdr", encoding="utf-16")
    sidecar = tmp_path / "backup.ddu.reg"
    sidecar.write_text("hdr", encoding="utf-16")

    result = cleaner.restore_backup(reg)

    assert restored == [str(sidecar)]
    assert result["ddu_restored"] is True


def test_restore_without_sidecar_is_fine(monkeypatch, tmp_path):
    """Старые бэкапы без сайдкара — обычный откат, без ошибки."""
    monkeypatch.setattr(cleaner, "CtfmonSuspender", _NoopSuspender)
    monkeypatch.setattr(cleaner, "restore_registry_backup", lambda p: {"ok": True})
    monkeypatch.setattr(cleaner, "check_switcher_consistency", lambda: {})
    monkeypatch.setattr(
        ddu, "restore_preload", lambda f: pytest.fail("сайдкара быть не должно")
    )

    reg = tmp_path / "backup.reg"
    reg.write_text("hdr", encoding="utf-16")

    result = cleaner.restore_backup(reg)

    assert result["ddu_restored"] is None
    assert result["success"] is True


class _NoopSuspender:
    started = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# Окно результата: последствие для будущих учётных записей видно пользователю
# ---------------------------------------------------------------------------


def test_wipe_covers_every_branch_not_only_preload(monkeypatch, allowed):
    """Шаблон чистится целиком, а не только по Preload (главный дефект).

    На живой машине выяснилось: чистки ``Keyboard Layout\\Preload``
    хватало ровно до перезагрузки. Windows пересобирает ``HKU\\.DEFAULT``
    из остальных веток шаблона, где раскладка оставалась — а именно из
    ``Control Panel\\International`` и ``Software\\Microsoft\\CTF``.

    Тест фиксирует СПИСОК веток, а не факт вызова функции: вызов и раньше
    был, а вот охват был неполным, и это ничем не ловилось.
    """
    calls: list[tuple[str, str]] = []

    def record(root, subkey, klid, match_mode="preload", delete=True):
        calls.append((subkey, "recursive"))
        return []

    def record_simple(root, subkey, klid, delete=True):
        calls.append((subkey, "simple"))
        return ["x"]

    monkeypatch.setattr(ddu.mutate, "_clean_preload_keys", record_simple)
    monkeypatch.setattr(ddu.mutate, "_clean_substitutes_keys", record_simple)
    monkeypatch.setattr(ddu.mutate, "_clean_intl_profile", record_simple)
    monkeypatch.setattr(ddu.mutate, "_clean_ctf_profiles", lambda *a, **k: [])
    monkeypatch.setattr(ddu.mutate, "_clean_branch_recursive", record)
    # Фейковый hive не подгрузить: без заглушки reg load вернёт ошибку и
    # чистка не начнётся — тест проверял бы не то.
    monkeypatch.setattr(
        ddu,
        "_reg",
        lambda args, timeout=30: subprocess.CompletedProcess(
            ["reg", *args], 0, b"", b""
        ),
    )

    ddu.wipe_preload("00000409")

    # Вызывается полный путь с префиксом подгрузки, а BRANCHES хранит
    # «голые» подключи. Сравнивать надо одно с одним — иначе тест всегда
    # падает, даже когда чистка полная.
    prefix = f"{ddu.MOUNT_NAME}\\"
    touched = {sub[len(prefix):] for sub, _ in calls if sub.startswith(prefix)}
    expected = {sub for sub, _mode in ddu.BRANCHES}
    assert expected <= touched, f"не вычищены ветки: {expected - touched}"
    # Антирегрессия к исходному дефекту: шаблон ТОЛЬКО с Preload.
    assert len(expected) > 1, "BRANCHES снова схлопнулся до одной ветки"
    intl = r"Control Panel\International"
    assert intl in touched, r"шаблон не чистится по Control Panel\International"
    ctf = r"Software\Microsoft\CTF"
    assert ctf in touched, r"шаблон не чистится по Software\Microsoft\CTF"


def test_export_covers_whole_template(monkeypatch, allowed, tmp_path):
    """Выгрузка берёт поддерево целиком, иначе откат будет неполным.

    Список чистимых веток и список выгружаемых — две независимые вещи.
    Если выгрузка отстанет от чистки, откат не покроет ровно то, что было
    стёрто.
    """
    seen: list[str] = []
    dest = tmp_path / "t.reg"

    def fake_reg_cmd(args, timeout=30):
        seen.append(args[1])
        dest.write_text("hdr", encoding="utf-16")
        return subprocess.CompletedProcess(["reg", *args], 0, b"", b"")

    monkeypatch.setattr(ddu, "_reg", fake_reg_cmd)
    assert ddu.export_preload(dest) is True
    assert any(s == f"HKU\\{ddu.MOUNT_NAME}" for s in seen), seen


def test_result_dialog_mentions_default_profile(monkeypatch):
    """Чистка шаблона — заметное последствие, и оно должно быть в окне.

    Иначе приложение меняет машину (шаблон на все будущие учётные записи)
    и сообщает об этом только в журнале.
    """
    import i18n
    from conftest import _ensure_main

    cls = _ensure_main(monkeypatch).KeyboardLayoutCleaner
    window = cls.__new__(cls)
    report = {
        "power_sync": True,
        "power_sync_detail": "SUCCESS",
        "language_sync_blocked": False,
        "language_sync_block_detail": "Пропущено",
        "retry_cleaned": 0,
        "ctfmon_restarted": True,
        "ddu_preload_deleted": ["2"],
    }
    msg = window._build_success_message(report, 6)
    assert i18n.fmt("dlg_result_ddu") in msg

    report["ddu_preload_deleted"] = []
    assert i18n.fmt("dlg_result_ddu") not in window._build_success_message(
        report, 6
    )
