"""Тесты профиля по умолчанию (FIX-37).

Проверяется не «функция вызвалась», а свойства, из-за которых модуль и
написан: шаблон не трогается без прав и в песочнице, hive выгружается при
любом исходе, а сбой выгрузки отменяет удаление.
"""
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest

import capabilities
import cleaner
import ddu
import mutate

# FIX-37j: единственный файл набора, которому шаблон профиля по умолчанию -
# предмет проверки. Все тесты здесь работают с подменёнными `ddu._reg` и
# путём к файлу, поэтому барьер conftest снимается целиком, на модуль.
pytestmark = pytest.mark.default_profile


class FakeReg:
    """Запись вызовов reg.exe с настраиваемыми кодами возврата.

    ``reg query`` по умолчанию ПРОВАЛЕН (код 1) — то есть подгрузки нет.
    Иначе каждый тест считал бы hive уже загруженным и уходил в ветку
    «забытая подгрузка».

    На ``reg save`` файл-адресат реально создаётся: настоящая команда тоже
    пишет файл, и без этого проверки «правка дошла до шаблона» и «пустой
    файл не принимается» проверяли бы несуществующий файл.
    """

    DEFAULTS: ClassVar[dict[str, int]] = {"query": 1}
    #: Содержимое, которым ``reg save`` «записывает» подгруженный hive.
    SAVED: ClassVar[bytes] = b"SAVED-HIVE"

    def __init__(self, returncodes=None, saved=SAVED):
        self.calls: list[list[str]] = []
        self.returncodes = {**self.DEFAULTS, **(returncodes or {})}
        self.saved = saved

    def __call__(self, cmd, **kwargs):
        args = list(cmd[1:])  # без "reg"
        self.calls.append(args)
        verb = args[0].lower()
        code = self.returncodes.get(verb, 0)
        if verb == "save" and code == 0:
            Path(args[2]).write_bytes(self.saved)
        return subprocess.CompletedProcess(cmd, code, b"", b"")

    def verbs(self) -> list[str]:
        return [c[0].lower() for c in self.calls]


@pytest.fixture(autouse=True)
def _clean_save_error():
    """Канал ошибки сохранения шаблона живёт в модуле — чистим между тестами.

    Иначе сбой из одного теста попадает в отчёт следующего, и упавший тест
    выглядит как не связанный с причиной.
    """
    ddu.take_save_error()
    yield
    ddu.take_save_error()


@pytest.fixture
def fake_reg(monkeypatch):
    """Подменить reg.exe; по умолчанию все команды успешны."""
    reg = FakeReg()
    monkeypatch.setattr(ddu.winproc, "run_hidden", reg)
    return reg


def _reg_ok(args, timeout=30):
    """Заглушка reg.exe для «всё успешно»; файловые команды пишут файлы.

    ``reg save``, не создавший файл, — это не то, что делает настоящая
    команда: такой модуль проверял бы ветку отказа вместо ветки успеха.
    """
    if args[0].lower() == "save":
        Path(args[2]).write_bytes(FakeReg.SAVED)
    return subprocess.CompletedProcess(["reg", *args], 0, b"", b"")


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


# ---------------------------------------------------------------------------
# reg save: правки шаблона обязаны попасть в файл (главный дефект инцидента)
# ---------------------------------------------------------------------------


def test_mutation_is_saved_to_disk_before_unload(fake_reg, allowed):
    """``reg save`` до ``reg unload``, иначе правки шаблона теряются.

    ``reg unload`` ОТБРАСЫВАЕТ изменения подгруженного hive. Без явного
    ``reg save`` чистка проходила, в журнале были строки об успехе, а
    ``NTUSER.DAT`` оставался байт в байт прежним — US возвращалась после
    перезагрузки. Порядок важен: после unload сохранять уже нечего.
    """
    with ddu.mounted(save=True):
        pass

    verbs = fake_reg.verbs()
    assert verbs == ["query", "load", "save", "unload"], verbs
    save_call = next(c for c in fake_reg.calls if c[0].lower() == "save")
    assert save_call[1] == f"HKU\\{ddu.MOUNT_NAME}"


def test_save_goes_to_staging_file_not_into_the_template(fake_reg, allowed):
    """``reg save`` пишет во временный файл, а НЕ в сам ``NTUSER.DAT``.

    Регрессия к инциденту на живой машине (28.09.2026): ``reg save`` в файл,
    из которого hive подгружен, не возвращается. Команда отработала таймаут
    дважды подряд — удаление «зависало» на две минуты и обрывалось ошибкой,
    а пользователь видел только «Идёт обработка — подождите…».
    """
    with ddu.mounted(save=True):
        pass

    save_call = next(c for c in fake_reg.calls if c[0].lower() == "save")
    staged = Path(save_call[2])
    assert staged != ddu.hive_path(), "reg save снова пишет в сам шаблон"
    assert staged.name.endswith(ddu.STAGING_SUFFIX), staged
    # Рядом с шаблоном: перенос не пересекает границу томов, иначе это уже
    # не «перезапись файла», а «создание нового» с чужими правами.
    assert staged.parent == ddu.hive_path().parent, staged


def test_mutation_reaches_the_template_file(fake_reg, allowed):
    """Правка доходит до файла шаблона — ради этого всё и затевалось.

    Проверяется не вызов ``reg save``, а результат: содержимое файла шаблона
    после операции равно сохранённому hive.
    """
    assert allowed.read_bytes() == b"fake", "исходное состояние заглушки"

    with ddu.mounted(save=True):
        pass

    assert allowed.read_bytes() == FakeReg.SAVED, "шаблон не перезаписан"
    assert ddu.take_save_error() == "", "успешное сохранение обязано быть чистым"


def test_template_is_rewritten_only_after_unload(monkeypatch, allowed):
    """Пока hive подгружен, файл шаблона не трогаем.

    Файл может быть занят подгрузкой — копирование поверх занятого файла
    упало бы с «файл занят» и потеряло бы уже сохранённый hive.
    """
    seen: dict[str, bytes] = {}

    def run(cmd, **kwargs):
        args = list(cmd[1:])
        verb = args[0].lower()
        if verb == "query":
            return subprocess.CompletedProcess(cmd, 1, b"", b"")
        if verb == "save":
            Path(args[2]).write_bytes(FakeReg.SAVED)
        elif verb == "unload":
            seen["at_unload"] = allowed.read_bytes()
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(ddu.winproc, "run_hidden", run)

    with ddu.mounted(save=True):
        pass

    assert seen["at_unload"] == b"fake", "шаблон перезаписан до выгрузки hive"
    assert allowed.read_bytes() == FakeReg.SAVED


def test_read_only_mount_does_not_touch_the_file(fake_reg, allowed):
    """Выгрузка ради чтения (``export_preload``) файл переписывать не должна.

    Заодно это и защита от ``save=True`` по недосмотру: обратный путь
    от выгрузки — лишняя запись в общий для машины файл шаблона.
    """
    with ddu.mounted():
        pass
    assert "save" not in fake_reg.verbs()


def test_failed_save_still_unloads(monkeypatch, allowed):
    """Не удалось сохранить — hive всё равно выгружается, файл не блокируется.

    Возможность уйти с заблокированным ``NTUSER.DAT`` дороже потерянных
    правок, о которых честно сказано в журнале.
    """
    reg = FakeReg(returncodes={"save": 1})
    monkeypatch.setattr(ddu.winproc, "run_hidden", reg)

    with ddu.mounted(save=True):
        pass

    assert reg.verbs()[-1] == "unload"


def test_save_timeout_does_not_leave_the_hive_mounted(monkeypatch, allowed):
    """Таймаут ``reg save`` не оставляет подгрузку висеть (FIX-37e).

    Живая машина, 28.09.2026: ``reg save`` отработал таймаут, исключение
    вылетело из ``finally`` раньше строки ``reg unload`` — и подгрузка
    ``HKU\\_KLC_DDU`` осталась в реестре, заблокировав файл шаблона. Ошибка
    была видна пользователю («Удаление не удалось»), а вот заблокированный
    профиль по умолчанию — нет: чинить пришлось вручную.
    """
    calls: list[str] = []

    def run(cmd, **kwargs):
        args = list(cmd[1:])
        verb = args[0].lower()
        calls.append(verb)
        if verb == "save":
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)
        if verb == "query":
            return subprocess.CompletedProcess(cmd, 1, b"", b"")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(ddu.winproc, "run_hidden", run)

    # Исключение наружу не выходит: операция обязана завершиться отчётом.
    with ddu.mounted(save=True):
        pass

    assert calls[-1] == "unload", "выгрузка пропущена — файл шаблона заблокирован"
    assert ddu.take_save_error(), "о сбое сохранения должен узнать вызывающий"


def test_failed_save_reaches_the_caller_and_spares_the_template(
    monkeypatch, allowed
):
    """Сбой сохранения — это отказ, а не «в шаблоне ничего не нашлось».

    Пустой список удалённых значений одинаков для обоих случаев, поэтому
    статус идёт отдельным каналом (см. :func:`ddu.take_save_error`). Шаблон
    при этом обязан остаться нетронутым: писать в него нечем.
    """
    reg = FakeReg(returncodes={"save": 1})
    monkeypatch.setattr(ddu.winproc, "run_hidden", reg)

    ddu.wipe_preload("00000409")

    assert allowed.read_bytes() == b"fake", "шаблон изменён без сохранённого hive"
    assert "reg save" in ddu.take_save_error()


def test_empty_staging_file_is_not_written_into_the_template(monkeypatch, allowed):
    """Пустой файл от ``reg save`` не переносится в шаблон.

    Пустой шаблон хуже нетронутого: из него Windows не соберёт нормальный
    профиль, то есть пострадало бы создание учётных записей на всей машине.
    """
    reg = FakeReg(saved=b"")
    monkeypatch.setattr(ddu.winproc, "run_hidden", reg)

    with ddu.mounted(save=True):
        pass

    assert allowed.read_bytes() == b"fake", "шаблон затёрт пустым файлом"
    assert "пуст" in ddu.take_save_error()


def test_staging_file_never_survives(monkeypatch, allowed):
    """В каталоге профиля не остаётся мусора — ни после успеха, ни после сбоя.

    ``C:\\Users\\Default`` — не рабочий каталог приложения, и лишний файл там
    остался бы навсегда.
    """
    for reg in (FakeReg(), FakeReg(returncodes={"save": 1})):
        monkeypatch.setattr(ddu.winproc, "run_hidden", reg)
        with ddu.mounted(save=True):
            pass
        ddu.take_save_error()
        assert not ddu.staging_path().exists(), f"остался {ddu.staging_path()}"


def test_save_is_not_called_when_load_failed(monkeypatch, allowed):
    """Не подгрузились — сохранять нечего: подгрузки не было."""
    reg = FakeReg(returncodes={"load": 1})
    monkeypatch.setattr(ddu.winproc, "run_hidden", reg)

    with pytest.raises(ddu.DduError), ddu.mounted(save=True):
        pass
    assert "save" not in reg.verbs()


def test_wipe_saves_the_template(monkeypatch, allowed):
    """Чистка шаблона обязана завершаться ``reg save`` — иначе её нет."""
    monkeypatch.setattr(ddu.mutate, "_clean_preload_keys", lambda *a, **k: ["x"])
    monkeypatch.setattr(
        ddu.mutate, "_clean_substitutes_keys", lambda *a, **k: []
    )
    monkeypatch.setattr(ddu.mutate, "_clean_intl_profile", lambda *a, **k: [])
    monkeypatch.setattr(ddu.mutate, "_clean_ctf_profiles", lambda *a, **k: [])
    monkeypatch.setattr(ddu.mutate, "_clean_branch_recursive", lambda *a, **k: [])
    reg = FakeReg()
    monkeypatch.setattr(ddu.winproc, "run_hidden", reg)

    ddu.wipe_preload("00000409")

    assert "save" in reg.verbs(), reg.verbs()
    assert reg.verbs()[-1] == "unload", reg.verbs()


def test_restore_saves_the_template(monkeypatch, allowed, tmp_path):
    """Откат шаблона тоже обязан сохраниться, иначе он фиктивен."""
    dest = tmp_path / "ddu.reg"
    reg = FakeReg()
    monkeypatch.setattr(ddu.winproc, "run_hidden", reg)

    assert ddu.restore_preload(dest) is False, "нет файла — выгрузки не было"
    assert reg.verbs() == [], reg.verbs()

    dest.write_text("hdr", encoding="utf-16")
    assert ddu.restore_preload(dest) is True
    assert "save" in reg.verbs(), reg.verbs()
    assert reg.verbs()[-1] == "unload", reg.verbs()


def test_restore_reports_unsaved_template(monkeypatch, allowed, tmp_path):
    """Импорт прошёл, а в файл hive не записался — это не восстановление.

    Иначе откат рапортовал бы об успехе, а шаблон остался бы прежним, и US
    вернулась бы при следующей загрузке — то есть откат не пережил бы
    перезагрузку, ради которой он и сделан.
    """
    dest = tmp_path / "ddu.reg"
    dest.write_text("hdr", encoding="utf-16")
    reg = FakeReg(returncodes={"save": 1})
    monkeypatch.setattr(ddu.winproc, "run_hidden", reg)

    assert ddu.restore_preload(dest) is False
    assert allowed.read_bytes() == b"fake", "шаблон изменён без сохранённого hive"
    # Причина возвращена внутри restore_preload и в канале не остаётся: иначе
    # она всплыла бы в отчёте следующей операции.
    assert ddu.take_save_error() == ""


def test_languages_value_is_cleaned_in_template(monkeypatch, allowed):
    """Шаблон чистится и по значению ``Languages``, а не только по профилям.

    Регрессия к исходному дефекту: профилей-языков (``en-US``) в шаблоне
    нет вообще, есть только значение ``Languages`` в ``User Profile`` и
    ``User Profile System Backup``. Из него Windows собирает ``HKU\\.DEFAULT``
    при загрузке, поэтому чистки подключей было мало — US возвращалась.
    """
    calls: list[str] = []

    def record_langs(root, subkey, klid, delete=True):
        calls.append(subkey)
        return ["x"]

    def record_noop(root, subkey, klid, delete=True):
        return []

    monkeypatch.setattr(ddu.mutate, "_clean_preload_keys", record_noop)
    monkeypatch.setattr(ddu.mutate, "_clean_substitutes_keys", record_noop)
    monkeypatch.setattr(ddu.mutate, "_clean_intl_profile", record_noop)
    monkeypatch.setattr(ddu.mutate, "_clean_intl_languages", record_langs)
    monkeypatch.setattr(ddu.mutate, "_clean_ctf_profiles", lambda *a, **k: [])
    monkeypatch.setattr(
        ddu.mutate, "_clean_branch_recursive", lambda *a, **k: []
    )
    monkeypatch.setattr(
        ddu,
        "_reg",
        lambda args, timeout=30: _reg_ok(args),
    )

    ddu.wipe_preload("00000409")

    prefix = f"{ddu.MOUNT_NAME}\\"
    cleaned = {sub[len(prefix):] for sub in calls if sub.startswith(prefix)}
    for branch in (
        r"Control Panel\International\User Profile",
        r"Control Panel\International\User Profile System Backup",
    ):
        assert branch in cleaned, f" Languages не чистится в {branch}"



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
        return _reg_ok(args)

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
    # FIX-37h: delete_layout берёт ветки через _branches_for_mutation, а она
    # добавляет к списку CLEAN_ONLY_BRANCHES. Без заглушки тест ушёл бы
    # писать в ЖИВОЙ HKU\.DEFAULT мимо песочницы.
    monkeypatch.setattr(cleaner, "_branches_for_mutation", lambda: [])
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


def test_delete_is_not_successful_when_template_not_saved(
    monkeypatch, tmp_path, allowed
):
    """Шаблон не записан на диск — операция неуспешна (FIX-37f).

    Пользовательская ветка к этому моменту уже вычищена, так что «успех» был
    бы ровно тем обещанием, которое инцидент и оправдал: в журнале — чистка,
    а после перезагрузки раскладка возвращается.
    """
    _patch_delete_env(monkeypatch, tmp_path)
    monkeypatch.setattr(ddu, "export_preload", lambda dest: True)
    monkeypatch.setattr(
        ddu.winproc, "run_hidden", FakeReg(returncodes={"save": 1})
    )

    result = cleaner.delete_layout("00000409")

    assert result["ddu_save_error"], "сбой сохранения обязан попасть в отчёт"
    assert result["success"] is False
    assert "шаблон" in result["error"].lower(), result["error"]


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


def test_language_sync_runs_before_registry_wipe(monkeypatch, tmp_path, allowed):
    """FIX-37h: список языков синхронизируется, ПОКА привязки ещё в реестре.

    Живой прогон 28.09.2026: шаг стоял после чистки веток, поэтому
    ``Get-WinUserLanguageList`` не увидел подсказки ``0409:00000409``,
    ответил ``NOCHANGE`` — и язык en-US остался в ``Languages``. Windows
    считал его установленным и возвращал US после каждой перезагрузки.

    Порядок фиксируется здесь намеренно: он не виден ни в отчёте, ни в
    Journal, а именно он стоил инцидента.
    """
    _patch_delete_env(monkeypatch, tmp_path)
    # Шаблон профиля по умолчанию — живой файл: подменяем, чтобы тест не
    # трогал C:\Users\Default\NTUSER.DAT (иначе он отменяет операцию).
    monkeypatch.setattr(ddu, "export_preload", lambda dest: True)
    monkeypatch.setattr(ddu, "wipe_preload", lambda klid: [])
    order: list[str] = []
    monkeypatch.setattr(
        cleaner,
        "_sync_language_list_via_powershell",
        lambda klid: (order.append("ps") or (True, "SUCCESS")),
    )
    monkeypatch.setattr(
        cleaner, "_wipe_branches", lambda *a, **k: order.append("wipe") or 0
    )
    monkeypatch.setattr(cleaner, "_wipe_ddu", lambda *a, **k: 0)

    cleaner.delete_layout("00000409")

    # Шаг 5 повторяет чистку (шаг 4 — верификация), поэтому важно, чтобы
    # синхронизация была ПЕРВОЙ и ровно один раз.
    assert order.count("ps") == 1
    assert order[0] == "ps", order


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


def test_languages_are_cleaned_before_profiles(monkeypatch, allowed):
    """В шаблоне сначала ``Languages``, потом профили (FIX-37g).

    `_clean_intl_profile` решает судьбу подключа по тому, значится ли его язык
    в ``Languages``. При обратном порядке ``en-US`` ещё числился активным, и
    опустевший ключ ``User Profile\\en-US`` оставался в шаблоне — вместе с
    привязкой к US-раскладке, из-за чего US возвращалась после перезагрузки.
    """
    order: list[str] = []
    monkeypatch.setattr(
        ddu.mutate,
        "_clean_intl_languages",
        lambda *a, **k: order.append("languages") or [],
    )
    monkeypatch.setattr(
        ddu.mutate,
        "_clean_intl_profile",
        lambda *a, **k: order.append("profile") or [],
    )
    monkeypatch.setattr(
        ddu.mutate, "_clean_preload_keys", lambda *a, **k: []
    )
    monkeypatch.setattr(
        ddu.mutate, "_clean_substitutes_keys", lambda *a, **k: []
    )
    monkeypatch.setattr(ddu.mutate, "_clean_ctf_profiles", lambda *a, **k: [])
    monkeypatch.setattr(
        ddu.mutate, "_clean_branch_recursive", lambda *a, **k: []
    )
    monkeypatch.setattr(ddu, "_reg", lambda args, timeout=30: _reg_ok(args))

    ddu.wipe_preload("00000409")

    intl_branches = sum(1 for _s, mode in ddu.BRANCHES if mode == "intl")
    assert intl_branches >= 2, "в шаблоне должно быть обе international-ветки"
    assert order == ["languages", "profile"] * intl_branches, order


def test_tip_in_value_name_is_matched(allowed):
    """TIP в ИМЕНИ значения — тоже раскладка (FIX-37g).

    Так Windows хранит установленные методы ввода в
    ``Control Panel\\International\\User Profile\\<язык>``: имя значения
    ``0409:00000409``, тип REG_DWORD, данные ``0x1``. Предикат распознавал
    формат ``LANGID:KLID`` только в теле значения — из-за чего в шаблоне
    профиля по умолчанию оставался ``en-US`` с привязкой к US-раскладкой, и
    US возвращалась после каждой перезагрузки.
    """
    variants = mutate._klid_variants("00000409")

    assert mutate._branch_value_matches("0409:00000409", "1", variants, "preload")
    # Форма в теле значения работала и раньше — регрессия на неё.
    assert mutate._branch_value_matches(
        "KeyboardLayoutPreload", "0409:00000409", variants, "preload"
    )
    # Чужая раскладка под тем же LANGID не трогается.
    assert not mutate._branch_value_matches(
        "0409:00000419", "1", variants, "preload"
    )


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
        lambda args, timeout=30: _reg_ok(args),
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
    intl = r"Control Panel\International\User Profile"
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


def test_intl_branch_is_nested_under_user_profile(monkeypatch, allowed):
    """Ветка international должна быть на уровне профилей, а не выше.

    Регрессия, найденная на живой машине: с путём
    ``Control Panel\\International`` функция ищет подключи-языки СРАЗУ под
    ним, то есть ``International\\en-US``. Такого подключа нет — профили
    лежат уровнем ниже, в ``International\\User Profile\\en-US``. Результат:
    тихо ничего не удалено, и раскладка возвращалась после перезагрузки.

    Тест сверяет путь с ветками сканера: если они разойдутся, чистка шаблона
    снова станет выборочной и молчаливой.
    """
    import scanner

    _, intl_sub, _, _ = next(
        (r, s, m, a)
        for r, s, m, a in scanner.get_affected_branches()
        if s.endswith("User Profile")
    )
    ddu_intl = {sub for sub, mode in ddu.BRANCHES if mode == "intl"}
    assert intl_sub in ddu_intl, (
        f"у сканера {intl_sub!r}, в шаблоне чистим {ddu_intl!r} - "
        "профили языков не будут найдены"
    )
    # Отдельно: ветка, которой нет у пользователя, но она есть в шаблоне.
    assert r"Control Panel\International\User Profile System Backup" in ddu_intl


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
