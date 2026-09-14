#
# layout_cleaner_backup.ps1
#
# PowerShell-скрипт для экспорта текущего списка языков в JSON.
# Запускается из cleaner.py через subprocess.run с параметром:
#   -OutputPath <путь к .json файлу>
#
# Вывод (в stdout): JSON-массив языков

param(
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'

$langs = Get-WinUserLanguageList

$out = @($langs | ForEach-Object {
    [pscustomobject]@{
        LanguageTag = $_.LanguageTag
        InputMethodTips = @($_.InputMethodTips)
    }
})

$out | ConvertTo-Json -Depth 4
