#
# layout_cleaner_backup.ps1
#
# PowerShell-скрипт для экспорта текущего списка языков в JSON.
# Запускается из cleaner.py через subprocess.run с параметром:
#   -OutputPath <путь к .json файлу>
#
# Вывод (в stdout): JSON-массив языков (всегда array, даже для 1 языка).
# ВАЖНО: ConvertTo-Json через pipe разворачивает одиночный элемент в голый
# объект. Поэтому используется -InputObject: он сохраняет массив всегда.

param(
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'

# @(...) гарантирует, что $langs — массив, даже если язык один или ноль
$langs = @(Get-WinUserLanguageList)

$out = @($langs | ForEach-Object {
    @{
        LanguageTag = $_.LanguageTag
        InputMethodTips = @($_.InputMethodTips)
    }
})

# -InputObject сохраняет форму массива для любого числа элементов (0/1/N)
ConvertTo-Json -InputObject $out -Depth 4 -Compress
