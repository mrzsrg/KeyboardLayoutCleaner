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
#   SANDBOX_SKIPPED — песочница: Set-WinUserLanguageList не вызывался
#
# FIX-25: -Sandbox. Песочница изолирует РЕЕСТР, но Set-WinUserLanguageList —
# WinRT-API, минующий реестр, и песочницей НЕ изолируется. Без флага
# запуск из песочницы переустановил бы РЕАЛЬНЫЙ список языков.

param(
    [switch]$Sandbox
)

$ErrorActionPreference = 'Stop'

# FIX-25: запрет на вызов — до любых обращений к WinRT.
if ($Sandbox) {
    Write-Output "SANDBOX_SKIPPED"
    exit 0
}

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
