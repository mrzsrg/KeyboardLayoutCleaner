#
# layout_cleaner_backup.ps1
#
# PowerShell-скрипт для экспорта текущего списка языков в JSON.
# Запускается из cleaner.py через subprocess.run с параметром:
#   -OutputPath <путь к .json файлу>
#
# Вывод: при -OutputPath JSON пишется строго в файл (не зависит от чистоты
# stdout), а в stdout выводится маркер "SUCCESS". Без -OutputPath (совместимый
# режим) JSON выводится в stdout.
# ВАЖНО: ConvertTo-Json через pipe разворачивает одиночный элемент в голый
# объект. Поэтому используется -InputObject: он сохраняет массив всегда.

param(
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'

# Вывод в UTF-8: Python (winproc.run_hidden) читает дочерний вывод как UTF-8.
# Без этого кириллица в stdout/stderr декодируется системной кодовой страницей
# и превращается в мусор. try/catch — на PowerShell 5.1 без консоли сеттер
# [Console]::OutputEncoding может бросить IOException.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# @(...) гарантирует, что $langs — массив, даже если язык один или ноль
$langs = @(Get-WinUserLanguageList)

$out = @($langs | ForEach-Object {
    @{
        LanguageTag = $_.LanguageTag
        InputMethodTips = @($_.InputMethodTips)
    }
})

# -InputObject сохраняет форму массива для любого числа элементов (0/1/N)
$json = ConvertTo-Json -InputObject $out -Depth 4 -Compress

if ($OutputPath) {
    # Пишем строго в файл; UTF-8 без BOM (Python читает как utf-8).
    [System.IO.File]::WriteAllText(
        $OutputPath,
        $json,
        [System.Text.UTF8Encoding]::new($false)
    )
    Write-Output "SUCCESS"
    exit 0
}

# Без -OutputPath — совместимый режим: JSON в stdout
Write-Output $json
