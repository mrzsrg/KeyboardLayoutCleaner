#
# layout_cleaner_list.ps1
#
# PowerShell-скрипт для получения списка языков из PowerShell.
# Запускается из scanner.py без параметров.
# Вывод (stdout): таблица с разделителем TAB — LanguageTag<TAB>InputMethodTips

$ErrorActionPreference = 'Stop'

$langs = Get-WinUserLanguageList
foreach ($l in $langs) {
    $tips = @($l.InputMethodTips) -join ';'
    Write-Output ("{0}`t{1}" -f $l.LanguageTag, $tips)
}
