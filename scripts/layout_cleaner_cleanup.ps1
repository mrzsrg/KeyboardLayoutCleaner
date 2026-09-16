#
# layout_cleaner_cleanup.ps1
#
# PowerShell-скрипт для очистки фантомной раскладки из списка языков.
# Запускается из cleaner.py через subprocess.run с параметрами:
#   -layout_klid <8-символьный HEX KLID>
#   [-apply]  # если нет — только dry-run (план изменений)
#
# Вывод (в stdout):
#   DROP|<tag>|<tip> — раскладка будет удалена из языка
#   REMOVELANG|<tag> — язык будет удалён полностью (нет раскладок)
#   NOCHANGE — ничего не изменено
#   DRYRUN — план готов, изменения НЕ применены
#   EMPTY_SKIPPED — нельзя удалить все языки
#   SUCCESS — изменения применены

param(
    [Parameter(Mandatory=$true)]
    [string]$LayoutKlid,

    [switch]$Apply
)

$ErrorActionPreference = 'Stop'

# Вывод в UTF-8: Python (winproc.run_hidden) читает дочерний вывод как UTF-8.
# Без этого кириллица в stdout/stderr декодируется системной кодовой страницей
# и превращается в мусор. try/catch — на PowerShell 5.1 без консоли сеттер
# [Console]::OutputEncoding может бросить IOException.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# Защита от инъекции: KLID обязан быть ровно 8 HEX-символов
if ($LayoutKlid -notmatch '^[0-9a-fA-F]{8}$') {
    Write-Output "INVALID_KLID: $LayoutKlid"
    exit 1
}

# Пара CJK-раскладок (Chinese/Japanese/Korean) — требуют сопоставления тегов
$cjk = @{
    'zh-CN' = 'zh-CN:00000804'
    'zh-TW' = 'zh-TW:00000404'
    'ja-JP' = 'ja-JP:00000411'
    'ko-KR' = 'ko-KR:00001008'
}

try {
    $langs = @(Get-WinUserLanguageList)
} catch {
    Write-Output "FAILED: Get-WinUserLanguageList"
    exit 0
}

$changed = $false
$kept = New-Object System.Collections.Generic.List[object]
$report = New-Object System.Collections.Generic.List[string]

foreach ($l in $langs) {
    $tag = $l.LanguageTag
    $tips = @($l.InputMethodTips)
    $dropTips = @()

    foreach ($tip in $tips) {
        $drop = $false
        $parts = $tip -split ':'
        if ($parts.Count -eq 2 -and $parts[1] -ieq $LayoutKlid) {
            $drop = $true
        }
        if (-not $drop -and $tip.Contains('{')) {
            $mapped = $cjk[$tag]
            if ($mapped -and $mapped -ieq $LayoutKlid) {
                $drop = $true
            }
        }

        if ($drop) {
            $dropTips += $tip
            $report.Add('DROP|' + $tag + '|' + $tip)
        }
    }

    if ($dropTips.Count -gt 0) {
        $changed = $true
        foreach ($tip in $dropTips) {
            [void]$l.InputMethodTips.Remove($tip)
        }
        if (@($l.InputMethodTips).Count -eq 0) {
            $report.Add('REMOVELANG|' + $tag)
        } else {
            $kept.Add($l)
        }
    } else {
        $kept.Add($l)
    }
}

if (-not $changed) {
    Write-Output 'NOCHANGE'
    exit 0
}

foreach ($r in $report) {
    Write-Output $r
}

if ($kept.Count -eq 0) {
    Write-Output 'EMPTY_SKIPPED'
    exit 0
}

if (-not $Apply) {
    Write-Output 'DRYRUN'
    exit 0
}

Set-WinUserLanguageList -LanguageList @($kept) -Force | Out-Null
Write-Output 'SUCCESS'
