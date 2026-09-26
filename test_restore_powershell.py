"""FIX-3: restore списка языков — реальные PS-прогоны + unit-фоллбеки.

Реальные прогоны используют приём test_cleanup_powershell.py: в дочернем
процессе PowerShell переопределяются Get/New/Set-WinUserLanguageList, после
чего вызывается НАСТОЯЩИЙ scripts/layout_cleaner_restore.ps1. Живые
настройки языка не затрагиваются; без PowerShell работают unit-фоллбеки.
"""

import base64
import json
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import capabilities

# FIX-10 (шаг 3): список языков и PS-адаптер живут в langlist.py; cleaner
# только реэкспортирует. Подменять надо langlist.run_hidden — подмена
# cleaner.run_hidden на восстановление языков больше не влияет.
import langlist

SCRIPT = Path(__file__).resolve().parent / "scripts" / "layout_cleaner_restore.ps1"


def _run_restore(entries, tmp_path, extra_args=None):
    """Прогнать настоящий restore.ps1 с подменёнными языковыми командами.

    ``extra_args`` — дополнительные аргументы скрипту (например, -Sandbox).
    """
    json_file = tmp_path / "restore_in.json"
    json_file.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    # HARNESS: данные и код разделены — значения передаются аргументами.
    harness = r"""
param([string]$ScriptPath, [string]$JsonPath, [string]$ExtraCsv)
$ErrorActionPreference = 'Stop'
# PowerShell считает "-Sandbox" ИМЕНЕМ ПАРАМЕТРА, а не значением, поэтому
# доп. аргументы приходят одной строкой через "|". Сплет массива передаёт
# их ПОЗИЦИОННО ("-Sandbox" не находит параметр), поэтому switch-флаги
# собираем в именованный хеш — так они биндятся по имени.
$named = @{}
foreach ($a in @($ExtraCsv -split '\|' | Where-Object { $_ -ne '' })) {
    $named[$a.TrimStart('-')] = $true
}
$global:applied = $null
function global:Get-WinUserLanguageList { @() }
function global:New-WinUserLanguageList {
    param([string]$Language)
    $tips = New-Object 'System.Collections.Generic.List[string]'
    $o = [pscustomobject]@{
        LanguageTag = $Language
        InputMethodTips = $tips
        Handwriting = $true
        Spellchecking = $false
    }
    return @($o)
}
function global:Set-WinUserLanguageList {
    param($LanguageList, [switch]$Force)
    foreach ($l in @($LanguageList)) {
        [Console]::WriteLine('APPLIED|' + $l.LanguageTag + '|tips=' + ($l.InputMethodTips -join ',') + '|hw=' + $l.Handwriting + '|sc=' + $l.Spellchecking)
    }
    [Console]::WriteLine('MOCK_SET_OK')
}
& $ScriptPath -JsonPath $JsonPath @named
"""
    args = [SCRIPT, json_file, "|".join(extra_args or [])]
    invocation = (
        "& {\n"
        + harness
        + "\n} "
        + " ".join("'" + str(v).replace("'", "''") + "'" for v in args)
        + "\n"
    )
    encoded = base64.b64encode(invocation.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    return result.returncode, result.stdout.splitlines()


@pytest.fixture(autouse=True)
def _allow_destructive_module():
    """FIX-29: тесты этого модуля вызывают restore_language_list.

    PowerShell внутри подменён, но право требуется на входе в функцию —
    именно это и должно быть видно в тексте теста.
    """
    capabilities.grant(capabilities.Capability.LANGUAGE_LIST)


requires_ps = pytest.mark.skipif(
    shutil.which("powershell.exe") is None, reason="Windows PowerShell required"
)


@requires_ps
def test_restore_applies_handwriting_and_spellchecking(tmp_path):
    """(a)+(b): dict-записи читаются, новые поля проставляются на объект."""
    _code, lines = _run_restore(
        [
            {
                "LanguageTag": "ru",
                "InputMethodTips": ["0419:00000419"],
                "Handwriting": False,
                "Spellchecking": True,
            }
        ],
        tmp_path,
    )
    assert "SUCCESS" in lines
    assert "MOCK_SET_OK" in lines
    applied = [ln for ln in lines if ln.startswith("APPLIED|")]
    assert applied == ["APPLIED|ru|tips=0419:00000419|hw=False|sc=True"], lines


@requires_ps
def test_restore_legacy_list_format_never_blanks_the_list(tmp_path):
    """FIX-25: прежний формат записей-массивов не может вызвать Set.

    restore_language_list раньше писал [["ru", [...]]]; скрипт читал
    $entry.LanguageTag -> $null -> все записи пропускались -> Set мог
    получить ПУСТОЙ список (очистка всех языков). Теперь файл отклоняется
    на валидации ДО сборки $kept, так что Set не вызывается вовсе.
    """
    json_file = tmp_path / "legacy.json"
    json_file.write_text(json.dumps([["ru", ["0419:00000419"]]]), encoding="utf-8")
    _code, lines = _run_restore_legacy_shape(json_file)
    assert any(ln.startswith("INVALID_TAG") for ln in lines), lines
    assert "MOCK_SET_OK" not in lines
    assert "SUCCESS" not in lines
    assert "APPLIED" not in lines


def _run_restore_legacy_shape(json_file):
    """Прогнать restore.ps1 по файлу произвольного содержимого (harness)."""
    harness = r"""
param([string]$ScriptPath, [string]$JsonPath)
$ErrorActionPreference = 'Stop'
function global:Get-WinUserLanguageList { @() }
function global:New-WinUserLanguageList { param([string]$Language) @() }
function global:Set-WinUserLanguageList {
    param($LanguageList, [switch]$Force)
    [Console]::WriteLine('MOCK_SET_OK count=' + @($LanguageList).Count)
}
& $ScriptPath -JsonPath $JsonPath
"""
    invocation = (
        "& {\n"
        + harness
        + "\n} "
        + " ".join(
            "'" + str(v).replace("'", "''") + "'" for v in (SCRIPT, json_file)
        )
        + "\n"
    )
    encoded = base64.b64encode(invocation.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    return result.returncode, result.stdout.splitlines()


@requires_ps
def test_restore_glued_tag_never_touches_the_real_list(tmp_path):
    """FIX-25: склейка тегов отклоняется, Set НЕ вызывается.

    Прежде тест закреплял РАЗРУШИТЕЛЬНОЕ поведение: требовал, чтобы тег
    "ru en-US" (склейка тегов двух языков) применился как ОДИН язык. Такой
    Set-WinUserLanguageList -Force ЗАМЕНЯЛ список языков пользователя, и в
    реальном прогоне у пользователя пропала русская раскладка из
    переключателя при том, что в Параметрах языка она оставалась
    «установленной» (помогала только перезагрузка).
    """
    _code, lines = _run_restore(
        [{"LanguageTag": "ru en-US", "InputMethodTips": ["0419:00000419"]}],
        tmp_path,
    )
    assert not [ln for ln in lines if ln.startswith("APPLIED|")]
    assert "MOCK_SET_OK" not in lines
    assert "SUCCESS" not in lines
    assert any(ln.startswith("INVALID_TAG") for ln in lines), lines


@requires_ps
def test_restore_rejects_array_tag_without_calling_set(tmp_path):
    """FIX-25: LanguageTag-массив (дефект прежнего backup.ps1) отклоняется.

    Именно такой файл создавал старый backup.ps1:
        [{"LanguageTag": ["ru", "en-US"],
          "InputMethodTips": ["0419:00000419", "0409:00000409"]}]
    """
    _code, lines = _run_restore(
        [
            {
                "LanguageTag": ["ru", "en-US"],
                "InputMethodTips": ["0419:00000419", "0409:00000409"],
            }
        ],
        tmp_path,
    )
    assert not [ln for ln in lines if ln.startswith("APPLIED|")]
    assert "MOCK_SET_OK" not in lines
    assert any(ln.startswith("INVALID_TAG") for ln in lines), lines


@requires_ps
def test_restore_rejects_one_bad_entry_among_good_ones(tmp_path):
    """FIX-25: одна плохая запись отклоняет файл ЦЕЛИКОМ.

    Частичное применение опаснее отказа: список языков оказался бы
    частично заменённым без спроса пользователя.
    """
    _code, lines = _run_restore(
        [
            {"LanguageTag": "ru", "InputMethodTips": ["0419:00000419"]},
            {"LanguageTag": "en-US", "InputMethodTips": ["0409:00000409"]},
            {"LanguageTag": "ru en-US", "InputMethodTips": ["0419:00000419"]},
        ],
        tmp_path,
    )
    # Ни одна из двух корректных записей не применена.
    assert not [ln for ln in lines if ln.startswith("APPLIED|")]
    assert "MOCK_SET_OK" not in lines


@requires_ps
def test_restore_sandbox_flag_never_calls_set(tmp_path):
    """FIX-25: -Sandbox запрещает Set даже на валидном снимке.

    Песочница изолирует реестр, но Set-WinUserLanguageList — WinRT-API,
    минующий реестр. Без этого флага sandbox переустановил бы реальный
    список языков пользователя.
    """
    code, lines = _run_restore(
        [
            {"LanguageTag": "ru", "InputMethodTips": ["0419:00000419"]},
            {"LanguageTag": "en-US", "InputMethodTips": ["0409:00000409"]},
        ],
        tmp_path,
        extra_args=["-Sandbox"],
    )
    assert code == 0
    assert "SANDBOX_SKIPPED" in lines
    assert not [ln for ln in lines if ln.startswith("APPLIED|")]
    assert "MOCK_SET_OK" not in lines


@requires_ps
def test_restore_two_languages_applied_separately(tmp_path):
    """FIX-25: два языка доходят до Set ДВУМЯ объектами с верными раскладками."""
    _code, lines = _run_restore(
        [
            {"LanguageTag": "ru", "InputMethodTips": ["0419:00000419"]},
            {"LanguageTag": "en-US", "InputMethodTips": ["0409:00000409"]},
        ],
        tmp_path,
    )
    applied = [ln for ln in lines if ln.startswith("APPLIED|")]
    assert applied == [
        "APPLIED|ru|tips=0419:00000419|hw=True|sc=False",
        "APPLIED|en-US|tips=0409:00000409|hw=True|sc=False",
    ], applied


@requires_ps
def test_restore_language_without_tips_is_kept(tmp_path):
    """Язык без раскладок сохраняется в списке (раньше — отдельная ветка)."""
    _code, lines = _run_restore(
        [{"LanguageTag": "de-DE", "InputMethodTips": []}], tmp_path
    )
    assert "SUCCESS" in lines
    assert any(
        ln.startswith("APPLIED|de-DE|tips=") and ln.endswith("|hw=True|sc=False")
        for ln in lines
    ), lines


@requires_ps
def test_restore_legacy_snapshot_without_new_fields(tmp_path):
    """(c): старый снимок без Handwriting/Spellchecking восстанавливается."""
    _code, lines = _run_restore(
        [{"LanguageTag": "en-US", "InputMethodTips": ["0409:00000409"]}], tmp_path
    )
    assert "SUCCESS" in lines
    applied = [ln for ln in lines if ln.startswith("APPLIED|")]
    # Поля в снимке отсутствуют -> присвоений не было (дефолты мока).
    # Главное: восстановление не падает и список применён.
    assert applied == ["APPLIED|en-US|tips=0409:00000409|hw=True|sc=False"]


# ---------------------------------------------------------------------------
# Unit-фоллбеки (чистый Python, работают на любом окружении)
# ---------------------------------------------------------------------------
from cleaner import restore_language_list  # noqa: E402


def _write(tmp_path, data):
    jp = tmp_path / "langlist.json"
    jp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return jp


def _ok_run():
    """Фабрика: MagicMock с корректным результатом run_hidden (assert-able)."""
    return mock.MagicMock(
        return_value=mock.Mock(returncode=0, stdout="SUCCESS", stderr="")
    )


def _capture_run(captured):
    def fake_run(cmd, **kw):
        captured["payload"] = Path(cmd[-1]).read_text(encoding="utf-8")
        return mock.Mock(returncode=0, stdout="SUCCESS", stderr="")

    return fake_run


def test_unit_rejects_malformed_tip(tmp_path):
    """(d): TIP неверного формата -> False, PS не запускается."""
    jp = _write(
        tmp_path,
        [{"LanguageTag": "ru", "InputMethodTips": ["0419:00000419", "nonsense"]}],
    )
    run = _ok_run()
    with mock.patch.object(langlist, "run_hidden", run):
        assert restore_language_list(jp) is False
    run.assert_not_called()


def test_unit_rejects_partial_file_on_bad_tip(tmp_path):
    """Нет частичного восстановления: один плохой TIP отклоняет файл целиком."""
    jp = _write(
        tmp_path,
        [
            {"LanguageTag": "ru", "InputMethodTips": ["0419:00000419"]},
            {"LanguageTag": "en-US", "InputMethodTips": ["xxx:yyy"]},
        ],
    )
    run = _ok_run()
    with mock.patch.object(langlist, "run_hidden", run):
        assert restore_language_list(jp) is False
    run.assert_not_called()


def test_unit_accepts_guid_tip_form(tmp_path):
    jp = _write(
        tmp_path,
        [
            {
                "LanguageTag": "ja-JP",
                "InputMethodTips": [
                    "0411:{03B5835A-F4A1-11CF-8F82-444553540000}"
                    "{78700C17-8E93-4DBD-A098-71E2E8E38A18}"
                ],
            }
        ],
    )
    captured = {}
    with mock.patch.object(langlist, "run_hidden", _capture_run(captured)):
        assert restore_language_list(jp) is True
    payload = json.loads(captured["payload"])
    assert payload[0]["LanguageTag"] == "ja-JP"
    assert payload[0]["InputMethodTips"][0].startswith("0411:{")


def test_unit_passes_handwriting_through(tmp_path):
    jp = _write(
        tmp_path,
        [
            {
                "LanguageTag": "ru",
                "InputMethodTips": ["0419:00000419"],
                "Handwriting": 1,
                "Spellchecking": 0,
            }
        ],
    )
    captured = {}
    with mock.patch.object(langlist, "run_hidden", _capture_run(captured)):
        assert restore_language_list(jp) is True
    payload = json.loads(captured["payload"])
    assert payload[0]["Handwriting"] is True
    assert payload[0]["Spellchecking"] is False


def test_unit_ignores_invalid_handwriting_values(tmp_path):
    jp = _write(
        tmp_path,
        [
            {
                "LanguageTag": "ru",
                "InputMethodTips": ["0419:00000419"],
                "Handwriting": "yes",
                "Spellchecking": None,
            }
        ],
    )
    captured = {}
    with mock.patch.object(langlist, "run_hidden", _capture_run(captured)):
        assert restore_language_list(jp) is True
    payload = json.loads(captured["payload"])
    assert "Handwriting" not in payload[0]
    assert "Spellchecking" not in payload[0]


def test_unit_legacy_snapshot_unchanged_shape(tmp_path):
    """Обратная совместимость: старый снимок без новых полей — как раньше."""
    jp = _write(
        tmp_path, [{"LanguageTag": "en-US", "InputMethodTips": ["0409:00000409"]}]
    )
    captured = {}
    with mock.patch.object(langlist, "run_hidden", _capture_run(captured)):
        assert restore_language_list(jp) is True
    payload = json.loads(captured["payload"])
    assert payload[0] == {"LanguageTag": "en-US", "InputMethodTips": ["0409:00000409"]}
