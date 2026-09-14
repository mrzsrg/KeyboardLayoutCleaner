#
# layout_cleaner_sync_welcome.ps1
#
# PowerShell-скрипт для копирования международных настроек текущего
# пользователя на Экран приветствия и в профиль новых пользователей.
# Запускается из cleaner.py без параметров.

$ErrorActionPreference = 'Stop'

Copy-UserInternationalSettingsToSystem -WelcomeScreen $true -NewUser $true
Write-Output "SUCCESS"
