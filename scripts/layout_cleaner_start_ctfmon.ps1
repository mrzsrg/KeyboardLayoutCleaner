#
# layout_cleaner_start_ctfmon.ps1
#
# PowerShell-скрипт для запуска службы текстового ввода.
# Запускается из cleaner.py без параметров.

$ErrorActionPreference = 'Stop'

$ctfmon = "$env:WINDIR\System32\ctfmon.exe"
if (Test-Path $ctfmon) {
    Start-Process $ctfmon
}
