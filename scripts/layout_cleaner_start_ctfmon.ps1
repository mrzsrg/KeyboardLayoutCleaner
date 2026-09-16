#
# layout_cleaner_start_ctfmon.ps1
#
# PowerShell-скрипт для запуска службы текстового ввода.
# Запускается из cleaner.py без параметров.

$ErrorActionPreference = 'Stop'

# Вывод в UTF-8: Python (winproc.run_hidden) читает дочерний вывод как UTF-8.
# Без этого кириллица в stdout/stderr декодируется системной кодовой страницей
# и превращается в мусор. try/catch — на PowerShell 5.1 без консоли сеттер
# [Console]::OutputEncoding может бросить IOException.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$ctfmon = "$env:WINDIR\System32\ctfmon.exe"
if (Test-Path $ctfmon) {
    Start-Process $ctfmon
}
