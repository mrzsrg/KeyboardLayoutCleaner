#
# layout_cleaner_backup.ps1
#
# PowerShell-скрипт для экспорта текущего списка языков в JSON.
# Запускается из cleaner.py через subprocess.run с параметром:
#   -OutputPath <путь к .json файлу>
#
# Вывод (в stdout): JSON-массив языков (всегда array, даже для 1 языка)

param(
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'

# Получаем список языков и гарантируем, что это массив
$langs = @(Get-WinUserLanguageList)

# Формируем массив объектов (даже если язык один)
$out = @($langs | ForEach-Object {
    [pscustomobject]@{
        LanguageTag = $_.LanguageTag
        InputMethodTips = @($_.InputMethodTips)
    }
})

# Принудительно оборачиваем в массив и конвертируем в JSON
[array]$jsonArray = if ($out.Count -eq 0) { @() } else { @($out) }
$jsonArray | ConvertTo-Json -Depth 4 -Compress
