#
# layout_cleaner_sync_welcome.ps1
#
# PowerShell-скрипт для копирования международных настроек текущего
# пользователя на Экран приветствия и в профиль новых пользователей.
# Запускается из cleaner.py без параметров.

$ErrorActionPreference = 'Stop'

# Вывод в UTF-8: Python (winproc.run_hidden) читает дочерний вывод как UTF-8.
# Без этого кириллица в stdout/stderr декодируется системной кодовой страницей
# и превращается в мусор. try/catch — на PowerShell 5.1 без консоли сеттер
# [Console]::OutputEncoding может бросить IOException.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

Copy-UserInternationalSettingsToSystem -WelcomeScreen $true -NewUser $true
Write-Output "SUCCESS"
