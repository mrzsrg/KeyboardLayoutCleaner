#
# layout_cleaner_stop_ctfmon.ps1
#
# PowerShell-скрипт для остановки служб текстового ввода.
# Запускается из cleaner.py без параметров.

$ErrorActionPreference = 'Stop'

Stop-Process -Name ctfmon -Force -ErrorAction SilentlyContinue
Stop-Process -Name TextInputHost -Force -ErrorAction SilentlyContinue
