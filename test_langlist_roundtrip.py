"""

Round-trip тест FIX-25: реальный scripts/layout_cleaner_backup.ps1 ->
реальный scripts/layout_cleaner_restore.ps1.

Зачем сквозной тест. Оба дефекта живут в РАЗНЫХ файлах и по отдельности
выглядят исправными:
  * backup.ps1 склеивал все языки в одну запись (LanguageTag = ["ru","en-US"]);
  * restore.ps1 принимал такой файл и заменял им список языков.
Тесты проверяли каждый скрипт по отдельности и подсовывали заранее
написанные корректные снимки, поэтому связка «снимок, который создаёт
сам backup.ps1, восстановить нельзя» не проверялась ни разу. Именно она
и стоила пользователю русской раскладки.

Все обращения к WinRT подменены заглушками: реальный список языков
не читается и не меняется.
"""

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest

BACKUP_SCRIPT = (
    Path(__file__).resolve().parent / "scripts" / "layout_cleaner_backup.ps1"
)
RESTORE_SCRIPT = (
    Path(__file__).resolve().parent / "scripts" / "layout_cleaner_restore.ps1"
)

requires_ps = pytest.mark.skipif(
    shutil.which("powershell.exe") is None, reason="Windows PowerShell required"
)


def _run_ps(harness_body, *args):
    """Запустить PowerShell с подменёнными языковыми командами.

    Данные и код разделены: значения передаются позиционными аргументами,
    а не интерполяцией в исходник harness.
    """
    harness = "param([string]$ScriptPath, [string[]]$Extra)\n" + harness_body
    quoted = " ".join("'" + str(a).replace("'", "''") + "'" for a in args)
    invocation = "& {\n" + harness + "\n} '" + str(BACKUP_SCRIPT) + "' " + quoted + "\n"
    encoded = base64.b64encode(invocation.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
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
        timeout=90,
    )


# Get-WinUserLanguageList отдаёт СПИСОК как ОДИН объект. Заглушка обязана
# воспроизводить именно это, иначе тест не поймает исходный дефект.
_MOCK_GET = r"""
function global:Get-WinUserLanguageList {
    $list = New-Object System.Collections.Generic.List[object]
    foreach ($pair in $global:LangData) {
        $tips = New-Object 'System.Collections.Generic.List[string]'
        foreach ($t in $pair.Tips) { [void]$tips.Add($t) }
        $list.Add([pscustomobject]@{
            LanguageTag = $pair.Tag
            InputMethodTips = $tips
            Handwriting = $true
            Spellchecking = $false
        })
    }
    # Именно так ведёт себя WinRT: список приходит одним объектом.
    return ,$list.ToArray()
}
"""

# Вложенные массивы PowerShell СПЛЮЩИВАЕТ: @(@('ru', @(...)), @('en-US', ...))
# разворачивается в плоский список строк. Поэтому пары — объекты.
_TWO = (
    "$global:LangData = @("
    "[pscustomobject]@{Tag='ru'; Tips=@('0419:00000419')},"
    "[pscustomobject]@{Tag='en-US'; Tips=@('0409:00000409')})\n"
)


@requires_ps
def test_backup_ps1_writes_one_object_per_language(tmp_path):
    """FIX-25: два языка -> ДВА объекта, LanguageTag скалярный."""
    out = tmp_path / "langlist.json"
    r = _run_ps(_MOCK_GET + _TWO + "& $ScriptPath -OutputPath $Extra[0]\n", str(out))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SUCCESS" in r.stdout
    data = json.loads(out.read_text(encoding="utf-8"))
    # Ключевая регрессия: был ОДИН элемент с массивом в LanguageTag.
    assert isinstance(data, list)
    assert len(data) == 2, data
    assert [e["LanguageTag"] for e in data] == ["ru", "en-US"]
    for entry in data:
        assert isinstance(entry["LanguageTag"], str)
        assert isinstance(entry["InputMethodTips"], list)
    assert data[0]["InputMethodTips"] == ["0419:00000419"]
    assert data[1]["InputMethodTips"] == ["0409:00000409"]


@requires_ps
def test_backup_ps1_single_language_keeps_array_shape(tmp_path):
    """FIX-25: один язык — тоже массив (ConvertTo-Json -InputObject)."""
    out = tmp_path / "one.json"
    r = _run_ps(
        _MOCK_GET
        + "$global:LangData = @([pscustomobject]@{Tag='ru'; Tips=@('0419:00000419')})\n"
        "& $ScriptPath -OutputPath $Extra[0]\n",
        str(out),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) == 1, data
    assert data[0]["LanguageTag"] == "ru"
    assert data[0]["InputMethodTips"] == ["0419:00000419"]


_MOCK_SET = r"""
function global:New-WinUserLanguageList {
    param([string]$Language)
    $tips = New-Object 'System.Collections.Generic.List[string]'
    return @([pscustomobject]@{
        LanguageTag = $Language; InputMethodTips = $tips
        Handwriting = $true; Spellchecking = $false
    })
}
function global:Set-WinUserLanguageList {
    param($LanguageList, [switch]$Force)
    [Console]::WriteLine('SET_COUNT=' + @($LanguageList).Count)
    foreach ($l in @($LanguageList)) {
        [Console]::WriteLine('APPLIED|' + $l.LanguageTag + '|tips=' + ($l.InputMethodTips -join ','))
    }
}
"""


def _restore_with_mock(json_path, *extra):
    """Прогнать настоящий restore.ps1 с заглушками New/Set."""
    harness = (
        "param([string]$ScriptPath, [string]$JsonPath, [string]$ExtraCsv)\n"
        "$ErrorActionPreference = 'Stop'\n"
        # switch-флаги биндятся только по имени — сплет массива даёт
        # PositionalParameterNotFound, поэтому собираем хеш.
        "$named = @{}\n"
        "foreach ($a in @($ExtraCsv -split '\\|' | Where-Object { $_ -ne '' })) "
        "{ $named[$a.TrimStart('-')] = $true }\n"
        + _MOCK_SET
        + "& $ScriptPath -JsonPath $JsonPath @named\n"
    )
    quoted = " ".join(
        "'" + str(a).replace("'", "''") + "'"
        for a in (RESTORE_SCRIPT, json_path, "|".join(extra))
    )
    invocation = "& {\n" + harness + "\n} " + quoted + "\n"
    encoded = base64.b64encode(invocation.encode("utf-16-le")).decode("ascii")
    r = subprocess.run(
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
        timeout=90,
    )
    return r.returncode, r.stdout.splitlines()


@requires_ps
def test_roundtrip_backup_then_restore_preserves_both_languages(tmp_path):
    """FIX-25: снимок, который создаёт backup.ps1, восстанавливается БЕЗ потерь.

    Сквозной прогон: реальный backup.ps1 пишет файл, реальный restore.ps1
    применяет его. Set заглушена и печатает, что получила.
    """
    snap = tmp_path / "langlist.json"
    r = _run_ps(_MOCK_GET + _TWO + "& $ScriptPath -OutputPath $Extra[0]\n", str(snap))
    assert r.returncode == 0, r.stdout + r.stderr

    code, lines = _restore_with_mock(snap)
    assert code == 0, lines
    assert "SET_COUNT=2" in lines, lines
    applied = [ln for ln in lines if ln.startswith("APPLIED|")]
    assert applied == [
        "APPLIED|ru|tips=0419:00000419",
        "APPLIED|en-US|tips=0409:00000409",
    ], applied
    assert "SUCCESS" in lines


@requires_ps
def test_real_corrupted_snapshot_is_rejected_not_applied(tmp_path):
    """FIX-25: снимок из инцидента отклоняется, Set не вызывается.

    Форма — ровно та, что создавал прежний backup.ps1 на живой машине.
    """
    corrupted = tmp_path / "corrupt.json"
    corrupted.write_text(
        json.dumps(
            [
                {
                    "LanguageTag": ["ru", "en-US"],
                    "InputMethodTips": ["0419:00000419", "0409:00000409"],
                }
            ]
        ),
        encoding="utf-8",
    )
    _code, lines = _restore_with_mock(corrupted)
    assert not [ln for ln in lines if ln.startswith("APPLIED|")]
    assert "SET_COUNT=" not in lines
    assert "SUCCESS" not in lines
    assert any(ln.startswith("INVALID_TAG") for ln in lines), lines


@requires_ps
def test_roundtrip_with_sandbox_flag_never_calls_set(tmp_path):
    """FIX-25: песочница запрещает Set на снимке, который сам же создал backup.ps1."""
    snap = tmp_path / "langlist.json"
    r = _run_ps(_MOCK_GET + _TWO + "& $ScriptPath -OutputPath $Extra[0]\n", str(snap))
    assert r.returncode == 0, r.stdout + r.stderr

    code, lines = _restore_with_mock(snap, "-Sandbox")
    assert code == 0, lines
    assert "SANDBOX_SKIPPED" in lines
    assert not [ln for ln in lines if ln.startswith("APPLIED|")]
    assert "SET_COUNT=" not in lines

