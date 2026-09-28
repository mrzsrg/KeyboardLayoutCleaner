"""Execute the real cleanup script against in-memory language commands only."""

import base64
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from layout_ids import CJK_KLIDS, LAYOUT_MAP

SCRIPT = Path(__file__).resolve().parent / "scripts" / "layout_cleaner_cleanup.ps1"


def _strip_ps_comments(source: str) -> str:
    """Убрать строки-комментарии PowerShell.

    Проверки ниже ищут ИСПОЛНЯЕМЫЙ код: описание старого (неверного) способа
    в комментарии — нормальная практика, и наличие такой строки в тексте
    не должно валить тест.
    """
    return "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )


@pytest.mark.parametrize("tag", ["zh-CN", "zh-TW", "ja-JP", "ko-KR"])
@pytest.mark.parametrize("apply", [False, True])
def test_cjk_guid_tip_mapping(tag, apply):
    """GUID tips must be dropped; the unrelated English language must survive.

    FIX-8: the CJK map is no longer a literal dictionary inside the .ps1 file —
    it is written to a temporary JSON file and passed as ``-CjkMapJson``,
    exactly the way the application does it. The map is taken from
    ``layout_ids.CJK_KLIDS`` so a divergence between the two copies is
    impossible by construction.
    """
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell is required")
    klid = LAYOUT_MAP[tag]
    # Keep data and executable code separate using a fixed harness and arguments.
    harness = r"""
param([string]$ScriptPath, [string]$Tag, [string]$Klid, [string]$ApplyMode, [string]$CjkMap)
$ErrorActionPreference = 'Stop'
$global:testTag = $Tag
function global:Get-WinUserLanguageList {
    # Форма повторяет НАСТОЯЩИЙ WinUserLanguageList, проверенный на живой
    # Windows: команда отдаёт СПИСОК List[WinUserLanguage] ОДНИМ объектом
    # (оператор , запрещает PowerShell развернуть его в поток), а
    # InputMethodTips — мутабельный List[string] при read-only свойстве.
    # Прежний мок отдавал два отдельных объекта, чем маскировал баг с
    # @(...)-обёрткой списка.
    $englishTips = New-Object 'System.Collections.Generic.List[string]'
    $englishTips.Add('0409:00000409')
    $english = [pscustomobject]@{LanguageTag='en-US'; InputMethodTips=$englishTips}
    $tips = New-Object 'System.Collections.Generic.List[string]'
    $tips.Add('0411:{11111111-2222-3333-4444-555555555555}{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}')
    $cjk = [pscustomobject]@{LanguageTag=$global:testTag; InputMethodTips=$tips}
    $list = New-Object 'System.Collections.Generic.List[object]'
    # FIX-37i: первым в списке стоит язык интерфейса, и он неприкосновенен.
    # CJK-язык добавлен ВТОРЫМ - ровно так его и добавляет Windows.
    [void]$list.Add($english)
    [void]$list.Add($cjk)
    return ,$list
}
function global:Set-WinUserLanguageList {
    param($LanguageList, [switch]$Force)
    if (@($LanguageList).Count -ne 1 -or $LanguageList[0].LanguageTag -ne 'en-US' -or
        $LanguageList[0].InputMethodTips[0] -ne '0409:00000409') {
        throw 'Incorrect retained languages'
    }
    [Console]::WriteLine('MOCK_SET_OK')
}
& $ScriptPath -LayoutKlid $Klid -CjkMapJson $CjkMap -Apply:($ApplyMode -eq 'yes')
"""
    # A child process confines command shadowing and the script's exit statements.
    with tempfile.TemporaryDirectory() as tmp:
        cjk_map_path = Path(tmp) / "cjk.json"
        cjk_map_path.write_text(
            json.dumps(CJK_KLIDS, ensure_ascii=False), encoding="utf-8"
        )
        invocation = (
            "& {\n"
            + harness
            + "\n} "
            + " ".join(
                "'" + str(value).replace("'", "''") + "'"
                for value in (
                    SCRIPT,
                    tag,
                    klid,
                    "yes" if apply else "no",
                    cjk_map_path,
                )
            )
        )
        encoded = base64.b64encode(invocation.encode("utf-16-le")).decode("ascii")
        result = subprocess.run(
            [
                powershell,
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
            timeout=30,
        )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert any(line.startswith(f"DROP|{tag}|0411:{{") for line in lines), result.stdout
    assert f"REMOVELANG|{tag}" in lines
    assert "NOCHANGE" not in lines
    assert not any(line.startswith("DROP|en-US|") for line in lines)
    assert ("MOCK_SET_OK" in lines) is apply
    assert ("SUCCESS" if apply else "DRYRUN") in lines


def test_cleanup_script_has_no_literal_cjk_dictionary():
    """Acceptance of FIX-8: the CJK mapping lives in layout_ids.py only.

    A literal ``'zh-CN' = '00000804'`` in the PowerShell script would be a
    second copy of the table, which is exactly the duplication FIX-8 removes.
    """
    text = SCRIPT.read_text(encoding="utf-8-sig")
    assert "CjkMapJson" in text
    for klid in CJK_KLIDS.values():
        assert klid not in text, f"CJK KLID {klid} продублирован в .ps1"


def test_cleanup_script_handles_language_list_as_single_object():
    """Регрессия: `Get-WinUserLanguageList` возвращает СПИСОК одним объектом.

    Прежний скрипт делал ``$langs = @(Get-WinUserLanguageList)`` и получал
    массив из одного элемента — сам список. Тогда ``foreach`` делал одну
    итерацию, в которой ``$l`` был списком, а не языком: ``$l.LanguageTag``
    склеивал все теги, а правка ``$l.InputMethodTips`` падала
    ``MethodInvocationException``. Синхронизация языков при этом не
    работала ни на одной живой системе.

    Мок в тесте отдавал список двумя отдельными объектами и баг маскировал;
    теперь форма мока совпадает с настоящей.
    """
    code = _strip_ps_comments(SCRIPT.read_text(encoding="utf-8-sig"))
    assert "$langs = @(Get-WinUserLanguageList)" not in code
    assert "foreach ($item in (Get-WinUserLanguageList))" in code


def test_cleanup_script_does_not_assign_readonly_tips_property():
    """Свойство ``InputMethodTips`` у WinUserLanguage только для чтения.

    Присваивание даёт ``RuntimeException``; мутировать можно сам список
    (``Clear``/``Add``). Мок на это не смотрит — property-объект PowerShell
    позволяет присваивать что угодно, — поэтому способ правки закреплён
    здесь явно.
    """
    code = _strip_ps_comments(SCRIPT.read_text(encoding="utf-8-sig"))
    assert "InputMethodTips =" not in code
    assert "$l.InputMethodTips.Clear()" in code
    assert "$l.InputMethodTips.Add($t)" in code


def test_real_language_list_shape_matches_mock():
    """Форма настоящего списка языков зафиксирована на живой Windows.

    Тест только ЧИТАЕТ список — пользовательские языки не меняются.
    Он защищает мок от «удобного», но неверного упрощения: если команда
    вернёт что-то иное, тест напомнит привести мок в соответствие.
    """
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell is required")
    probe = (
        "$list = Get-WinUserLanguageList;"
        "Write-Output ('LIST=' + $list.GetType().Name);"
        "Write-Output ('COUNT=' + $list.Count);"
        "Write-Output ('TIPS=' + $list[0].InputMethodTips.GetType().Name)"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if result.returncode != 0 or "LIST=" not in result.stdout:
        pytest.skip("нет доступных языков для проверки формы")
    values = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    # Список приходит одним объектом типа List`1 — не поток языков.
    assert values["LIST"].startswith("List"), values
    # InputMethodTips — List (мутабельный), а не массив.
    assert values["TIPS"].startswith("List"), values


# ---------------------------------------------------------------------------
# FIX-37i: язык, у которого не осталось методов ввода, тоже должен уходить.
#
# Живой прогон 28.09.2026 показал, что «Английский (США)» остаётся в
# Параметрах, хотя раскладка US удалена и пережила две перезагрузки. Причина
# видна в самом списке: `Get-WinUserLanguageList` отдаёт `en-US` с
# `InputMethodTips=[]` - язык осиротел, потому что его привязку вычистил
# реестровый шаг (`Control Panel\International\User Profile\en-US`).
# Прежний скрипт умел убрать язык только если убрал его раскладку в этом же
# прогоне, поэтому на осиротевшем языке отвечал NOCHANGE.
# ---------------------------------------------------------------------------

_LANG_HARNESS = r"""
param(
    [string]$ScriptPath, [string]$Klid, [string]$Tags,
    [string]$ApplyMode, [string]$Scenario, [string]$CjkMap
)
$ErrorActionPreference = 'Stop'
function global:Get-WinUserLanguageList {
    $list = New-Object 'System.Collections.Generic.List[object]'
    $ru = New-Object 'System.Collections.Generic.List[string]'
    $ru.Add('0419:00000419')
    $orphan = [pscustomobject]@{
        LanguageTag='en-US'
        InputMethodTips=(New-Object 'System.Collections.Generic.List[string]')
    }
    if ($Scenario -eq 'display-orphan') {
        # en-US ПЕРВЫМ: первый элемент списка - язык интерфейса.
        [void]$list.Add($orphan)
        [void]$list.Add([pscustomobject]@{LanguageTag='ru'; InputMethodTips=$ru})
    } else {
        [void]$list.Add([pscustomobject]@{LanguageTag='ru'; InputMethodTips=$ru})
        [void]$list.Add($orphan)
    }
    return ,$list
}
function global:Set-WinUserLanguageList {
    param($LanguageList, [switch]$Force)
    $kept = @()
    foreach ($item in @($LanguageList)) {
        $kept += ($item.LanguageTag + '[' + (@($item.InputMethodTips) -join '+') + ']')
    }
    [Console]::WriteLine('MOCK_SET:' + ($kept -join ','))
}
& $ScriptPath -LayoutKlid $Klid -CjkMapJson $CjkMap -LanguageTags $Tags -Apply:($ApplyMode -eq 'yes')
"""


def _run_with_mock(harness: str, *values: str):
    """Запустить настоящий .ps1 против подменённых функций списка языков.

    Дочерний процесс ограничивает область видимости подменённых команд и
    `exit` самого скрипта - ровно тем же приёмом, что и тест CJK выше.
    """
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell is required")
    with tempfile.TemporaryDirectory() as tmp:
        cjk_map_path = Path(tmp) / "cjk.json"
        cjk_map_path.write_text(
            json.dumps(CJK_KLIDS, ensure_ascii=False), encoding="utf-8"
        )
        invocation = (
            "& {\n" + harness + "\n} "
            + " ".join(
                "'" + str(value).replace("'", "''") + "'"
                for value in (SCRIPT, *values, cjk_map_path)
            )
        )
        encoded = base64.b64encode(invocation.encode("utf-16-le")).decode("ascii")
        result = subprocess.run(
            [
                powershell,
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
            timeout=30,
        )
    return result, result.stdout.splitlines()


@pytest.mark.parametrize("apply", [False, True])
def test_orphan_language_of_removed_layout_is_dropped(apply):
    """Язык удаляемой раскладки без методов ввода уходит из списка.

    Именно этот случай и наблюдался на живой машине: раскладка US удалена,
    а `en-US` с пустым `InputMethodTips` продолжал висеть в Параметрах.
    """
    result, lines = _run_with_mock(
        _LANG_HARNESS,
        "00000409",
        "en-US",
        "yes" if apply else "no",
        "orphan",
    )

    assert result.returncode == 0, result.stderr
    assert "REMOVELANG|en-US" in lines
    assert "NOCHANGE" not in lines
    assert not any(line.startswith("DROP|") for line in lines)
    # В список уходит только русский - с его раскладкой на месте.
    assert ("MOCK_SET:ru[0419:00000419]" in lines) is apply
    assert ("SUCCESS" if apply else "DRYRUN") in lines


def test_display_language_is_never_removed():
    """Первый элемент списка - язык интерфейса; его не убираем.

    `Set-WinUserLanguageList` делает интерфейсным тот язык, что остался
    первым, поэтому удаление языка интерфейса молча сменило бы язык
    Windows - чего удаление раскладки не обещает.
    """
    result, lines = _run_with_mock(
        _LANG_HARNESS,
        "00000409",
        "en-US",
        "yes",
        "display-orphan",
    )

    assert result.returncode == 0, result.stderr
    assert "NOCHANGE" in lines
    assert "REMOVELANG|en-US" not in lines
    assert not any(line.startswith("MOCK_SET:") for line in lines)


def test_orphan_language_of_another_layout_survives():
    """Чужой язык без раскладок не трогаем.

    Язык могли добавить ради проверки орфографии; удаление одной раскладки
    не должно выбрасывать из списка то, что к ней не относится.
    """
    result, lines = _run_with_mock(
        _LANG_HARNESS,
        "00000407",
        "de-DE",
        "yes",
        "orphan",
    )

    assert result.returncode == 0, result.stderr
    assert "NOCHANGE" in lines
    assert not any(line.startswith("REMOVELANG|") for line in lines)
    assert not any(line.startswith("MOCK_SET:") for line in lines)


def test_invalid_language_tag_is_rejected_before_winrt():
    """Склеенные теги не доезжают до WinRT-API (fail-closed).

    `Set-WinUserLanguageList` заменяет список целиком, поэтому кривой тег
    обязан остановить скрипт ДО вызова, а не после. Код возврата процесса
    здесь не проверяется: `exit 1` внутри вызванного скрипта не
    propagates в код `-EncodedCommand`, и проверять надо свойство,
    ради которого барьер и нужен, - WinRT не вызван.
    """
    _result, lines = _run_with_mock(
        _LANG_HARNESS,
        "00000409",
        "ru en-US",
        "yes",
        "orphan",
    )

    assert "INVALID_TAG: ru en-US" in lines
    assert not any(line.startswith("MOCK_SET:") for line in lines)
    assert "SUCCESS" not in lines
    assert "DRYRUN" not in lines
    assert "NOCHANGE" not in lines


def test_cleanup_script_validates_language_tags():
    """Барьер BCP-47 в .ps1 обязателен: скрипт могут вызвать и в обход Python."""
    text = SCRIPT.read_text(encoding="utf-8-sig")
    assert "[string]$LanguageTags" in text
    assert "$TagPattern" in text
    assert "INVALID_TAG" in text
    assert "$targetTags.Contains($tag)" in text
