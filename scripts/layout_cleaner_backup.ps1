#
# layout_cleaner_backup.ps1
#
# PowerShell-скрипт для экспорта текущего списка языков в JSON.
# Запускается из cleaner.py через subprocess.run с параметром:
#   -OutputPath <путь к .json файлу>
#
# Вывод: при -OutputPath JSON пишется строго в файл (не зависит от чистоты
# stdout), а в stdout выводится маркер "SUCCESS". Без -OutputPath (совместимый
# режим) JSON выводится в stdout.
# ВАЖНО: ConvertTo-Json через pipe разворачивает одиночный элемент в голый
# объект. Поэтому используется -InputObject: он сохраняет массив всегда.
# ВАЖНО (FIX-25): результат этого скрипта — вход для restore.ps1, который
# применяет Set-WinUserLanguageList. Снимок обязан содержать по ОДНОМУ
# объекту на язык со СКАЛЯРНЫМ LanguageTag; иначе restore склеит теги в
# несуществующий тег и заменит список языков целиком. Форма закреплена
# round-trip-тестом test_langlist_roundtrip.py.

param(
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'

# Вывод в UTF-8: Python (winproc.run_hidden) читает дочерний вывод как UTF-8.
# Без этого кириллица в stdout/stderr декодируется системной кодовой страницей
# и превращается в мусор. try/catch — на PowerShell 5.1 без консоли сеттер
# [Console]::OutputEncoding может бросить IOException.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# FIX-25: Get-WinUserLanguageList возвращает СПИСОК List<WinUserLanguage>
# как ОДИН объект, а не поток языков. Прежний код делал
#     $langs = @(Get-WinUserLanguageList)
# и получал массив ИЗ ОДНОГО элемента — самого списка. Тогда
# `$langs | ForEach-Object` выполнял ОДНУ итерацию, в которой
# $_.LanguageTag отдавал ВСЕ теги сразу, а $_.InputMethodTips — все
# раскладки всех языков. Снимок получался из одной записи вида
#     {"LanguageTag":["ru","en-US"],
#      "InputMethodTips":["0419:00000419","0409:00000409"]}
# restore.ps1 склеивал такой тег в строку "ru en-US", New-WinUserLanguageList
# создавал ОДИН язык с НЕСУЩЕСТВУЮЩИМ тегом, и Set-WinUserLanguageList
# -Force ЗАМЕНЯЛ весь список языков пользователя — с рапортом SUCCESS.
# Коллекцию разворачиваем явно (как в layout_cleaner_cleanup.ps1, FIX-24).
try {
    $langs = @()
    foreach ($item in (Get-WinUserLanguageList)) { $langs += $item }
} catch {
    Write-Output "FAILED: Get-WinUserLanguageList"
    exit 1
}

# FIX-3: снимаем ПОЛНЫЙ изменяемый набор WinUserLanguage. Handwriting/
# Spellchecking New-WinUserLanguageList сбрасывает в дефолт, поэтому без
# них откат возвращал бы язык с чужими настройками письма. Чтение — через
# PSObject.Properties: на части сборок Windows свойства могут отсутствовать
# (тогда в снимке останется null, restore пропустит присвоение).
$out = @($langs | ForEach-Object {
    $hw = $null
    $sc = $null
    try {
        if ($_.PSObject.Properties['Handwriting']) { $hw = [bool]$_.Handwriting }
    } catch { }
    try {
        if ($_.PSObject.Properties['Spellchecking']) { $sc = [bool]$_.Spellchecking }
    } catch { }
    @{
        LanguageTag = $_.LanguageTag
        InputMethodTips = @($_.InputMethodTips)
        Handwriting = $hw
        Spellchecking = $sc
    }
})

# -InputObject сохраняет форму массива для любого числа элементов (0/1/N)
$json = ConvertTo-Json -InputObject $out -Depth 4 -Compress

if ($OutputPath) {
    # Пишем строго в файл; UTF-8 без BOM (Python читает как utf-8).
    [System.IO.File]::WriteAllText(
        $OutputPath,
        $json,
        [System.Text.UTF8Encoding]::new($false)
    )
    Write-Output "SUCCESS"
    exit 0
}

# Без -OutputPath — совместимый режим: JSON в stdout
Write-Output $json
