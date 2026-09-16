#
# layout_cleaner_restore.ps1
#
# PowerShell-скрипт для восстановления списка языков из JSON-бэкапа.
# Запускается из cleaner.py через subprocess.run с параметром:
#   -JsonPath <путь к JSON-файлу>
#
# Ожидается, что JSON-файл содержит массив объектов с полями:
#   { "LanguageTag": "en-US", "InputMethodTips": ["0409:00000409", ...] }
#
# Вывод (в stdout):
#   SUCCESS — восстановлено
#
param(
    [Parameter(Mandatory=$true)]
    [string]$JsonPath
)

$ErrorActionPreference = 'Stop'

# Вывод в UTF-8: Python (winproc.run_hidden) читает дочерний вывод как UTF-8.
# Без этого кириллица в stdout/stderr декодируется системной кодовой страницей
# и превращается в мусор. try/catch — на PowerShell 5.1 без консоли сеттер
# [Console]::OutputEncoding может бросить IOException.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# Читаем JSON-бэкап списка языков
$entries = Get-Content -LiteralPath $JsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($entries -is [PSCustomObject]) {
    $entries = @($entries)
}

if ($entries.Count -eq 0) {
    Write-Output "EMPTY_ENTRIES: $JsonPath"
    exit 1
}

$kept = @()

foreach ($entry in $entries) {
    $tag = $entry.LanguageTag
    if (-not $tag) { continue }

    $tag_ps = $tag -replace "'", "''"

    # Создаём новый объект языка с текущими раскладками
    if ($entry.InputMethodTips -and $entry.InputMethodTips.Count -gt 0) {
        $nl = New-WinUserLanguageList -Language $tag_ps
        $nl[0].InputMethodTips.Clear()
        foreach ($tip in $entry.InputMethodTips) {
            $tip_ps = $tip -replace "'", "''"
            [void]$nl[0].InputMethodTips.Add($tip_ps)
        }
        $kept = @($kept) + $nl[0]
    } else {
        # Если нет раскладок — всё равно сохраняем язык
        $nl = New-WinUserLanguageList -Language $tag_ps
        $kept = @($kept) + $nl[0]
    }
}

# Применяем restored список
Set-WinUserLanguageList -LanguageList @($kept) -Force | Out-Null
Write-Output "SUCCESS"
