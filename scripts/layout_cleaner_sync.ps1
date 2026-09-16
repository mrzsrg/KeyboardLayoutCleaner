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

# Вывод в UTF-8: Python (winproc.run_hidden) читает дочерний вывод как UTF-8.
# Без этого кириллица в stdout/stderr декодируется системной кодовой страницей
# и превращается в мусор. try/catch — на PowerShell 5.1 без консоли сеттер
# [Console]::OutputEncoding может бросить IOException.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

try {
    $langs = Get-WinUserLanguageList
    Set-WinUserLanguageList $langs -Force
    Write-Output "SUCCESS"
} catch {
    Write-Output "FAILED: $_"
}
