"""Execute the real cleanup script against in-memory language commands only."""

import base64
import shutil
import subprocess
from pathlib import Path

import pytest

from scanner import LAYOUT_MAP

SCRIPT = Path(__file__).resolve().parent / "scripts" / "layout_cleaner_cleanup.ps1"


@pytest.mark.parametrize("tag", ["zh-CN", "zh-TW", "ja-JP", "ko-KR"])
@pytest.mark.parametrize("apply", [False, True])
def test_cjk_guid_tip_mapping(tag, apply):
    """GUID tips must be dropped; the unrelated English language must survive."""
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell is required")
    klid = LAYOUT_MAP[tag]
    # Keep data and executable code separate using a fixed harness and arguments.
    harness = r"""
param([string]$ScriptPath, [string]$Tag, [string]$Klid, [string]$ApplyMode)
$ErrorActionPreference = 'Stop'
$global:testTag = $Tag
function global:Get-WinUserLanguageList {
    $tips = New-Object 'System.Collections.Generic.List[string]'
    $tips.Add('0411:{11111111-2222-3333-4444-555555555555}{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}')
    [pscustomobject]@{LanguageTag=$global:testTag; InputMethodTips=$tips}
    $english = New-Object 'System.Collections.Generic.List[string]'
    $english.Add('0409:00000409')
    [pscustomobject]@{LanguageTag='en-US'; InputMethodTips=$english}
}
function global:Set-WinUserLanguageList {
    param($LanguageList, [switch]$Force)
    if (@($LanguageList).Count -ne 1 -or $LanguageList[0].LanguageTag -ne 'en-US' -or
        $LanguageList[0].InputMethodTips[0] -ne '0409:00000409') {
        throw 'Incorrect retained languages'
    }
    [Console]::WriteLine('MOCK_SET_OK')
}
& $ScriptPath -LayoutKlid $Klid -Apply:($ApplyMode -eq 'yes')
"""
    # A child process confines command shadowing and the script's exit statements.
    invocation = (
        "& {\n"
        + harness
        + "\n} "
        + " ".join(
            "'" + str(value).replace("'", "''") + "'"
            for value in (SCRIPT, tag, klid, "yes" if apply else "no")
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
