#
# layout_cleaner_sync.ps1
#
# PowerShell-скрипт для синхронизации (переустановки) списка языков.
# Запускается из cleaner.py через subprocess.run без параметров.
#
# Используется для обновления кэша TSF после удаления раскладки.
#
# Вывод (в stdout):
#   SUCCESS — успешно переустановлен

$ErrorActionPreference = 'Stop'

try {
    $langs = Get-WinUserLanguageList
    Set-WinUserLanguageList $langs -Force
    Write-Output "SUCCESS"
} catch {
    Write-Output "FAILED: $_"
}
