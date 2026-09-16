#
# layout_cleaner_list.ps1
#
# PowerShell-скрипт для получения списка языков из PowerShell.
# Запускается из scanner.py без параметров.
# Вывод (stdout): таблица с разделителем TAB — LanguageTag<TAB>InputMethodTips

$ErrorActionPreference = 'Stop'

# Вывод в UTF-8: Python (winproc.run_hidden) читает дочерний вывод как UTF-8.
# Без этого кириллица в stdout/stderr декодируется системной кодовой страницей
# и превращается в мусор. try/catch — на PowerShell 5.1 без консоли сеттер
# [Console]::OutputEncoding может бросить IOException.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$langs = Get-WinUserLanguageList
foreach ($l in $langs) {
    $tips = @($l.InputMethodTips) -join ';'
    Write-Output ("{0}`t{1}" -f $l.LanguageTag, $tips)
}
