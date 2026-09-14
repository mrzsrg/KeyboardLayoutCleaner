#
# layout_cleaner_reg_import.ps1
#
# PowerShell-скрипт для безопасного импорта .reg-файла с запросом UAC.
# Запускается из cleaner.py через subprocess.run с параметром:
#   -RegPath <путь к .reg файлу>
#
# Безопасность: путь к файлу передаётся через параметр PowerShell,
# а не через интерполяцию в строку — защита от PS-инъекций.

param(
    [Parameter(Mandatory=$true)]
    [string]$RegPath
)

$ErrorActionPreference = 'Stop'

# Проверка существования файла — защита от опечаток в пути
if (-not (Test-Path -LiteralPath $RegPath)) {
    Write-Error "Файл не найден: $RegPath"
    exit 1
}

# Запуск reg import с UAC-повышением
$p = Start-Process reg.exe `
    -ArgumentList @('import', $RegPath) `
    -Verb RunAs `
    -Wait `
    -PassThru

if ($p) {
    exit $p.ExitCode
} else {
    exit 1
}
