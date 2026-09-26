#
# layout_cleaner_restore.ps1
#
# PowerShell-скрипт для восстановления списка языков из JSON-бэкапа.
# Запускается из cleaner.py через subprocess.run с параметром:
#   -JsonPath <путь к JSON-файлу>
#
# Ожидается, что JSON-файл содержит массив объектов с полями:
#   { "LanguageTag": "en-US", "InputMethodTips": ["0409:00000409", ...],
#     "Handwriting": true|false (опционально), "Spellchecking": ... (опц.) }
# FIX-3: Handwriting/Spellchecking — изменяемые свойства WinUserLanguage,
# которые New-WinUserLanguageList сбрасывает в дефолт; если поля есть в
# снимке — они проставляются обратно (в try/catch: на части сборок Windows
# свойства могут отсутствовать). Старые снимки без этих полей работают
# как раньше.
#
# Вывод (в stdout):
#   SUCCESS — восстановлено
#   INVALID_TAG: <описание> — снимок отклонён, Set-WinUserLanguageList НЕ
#     вызывался (fail-closed)
#   SANDBOX_SKIPPED — песочница: WinRT-API не вызывается
#   EMPTY_ENTRIES: <путь> — нечего восстанавливать
#
# КОДИРОВКА (FIX-25). Файл БЕЗ UTF-8 BOM, поэтому Windows PowerShell 5.1
# читает его в системной ANSI-кодировке (у нас CP1251). Кириллица допустима
# ТОЛЬКО в комментариях: UTF-8-байты «—» (E2 80 94) в CP1251 дают «в”», а
# PowerShell считает «”» разделителем строки — литерал с этим символом роняет
# разбор всего скрипта. Все строки в stdout здесь ASCII намеренно.
#
# БЕЗОПАСНОСТЬ (FIX-25). Скрипт вызывает Set-WinUserLanguageList -Force,
# который ЗАМЕНЯЕТ список языков пользователя целиком. Поэтому он fail-closed:
# снимок проверяется ПОЛНОСТЬЮ до первого вызова WinRT, и любой дефект
# приводит к отказу, а не к применению мусора. Раньше снимок, испорченный
# самим же backup.ps1 (LanguageTag = ["ru","en-US"]), доходил до Set:
# PowerShell склеивал массив в строку "ru en-US", создавался ОДИН язык с
# несуществующим тегом, -Force заменял весь список — и печатался SUCCESS.
# Именно этим путём у пользователя исчезла раскладка из переключателя при
# том, что в Параметрах языка она оставалась «установленной».
#
# Второй контур (FIX-25): песочница изолирует РЕЕСТР (ветки уводятся под
# _SANDBOX_ROOT), но Set-WinUserLanguageList — WinRT-API, минующий реестр,
# и песочницей НЕ изолируется. Флаг -Sandbox запрещает вызов, чтобы
# sandbox никогда не менял реальный список языков.

param(
    [Parameter(Mandatory=$true)]
    [string]$JsonPath,

    # FIX-25: песочница — реальный список языков не трогаем никогда.
    [switch]$Sandbox
)

# BCP-47 в практической форме: "ru", "en-US", "zh-Hans-CN", "sr-Latn-RS".
# Пробел или несколько тегов подряд сюда не попадают — именно это и было
# причиной потери языка ("ru en-US").
$TagPattern = '^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*$'

$ErrorActionPreference = 'Stop'

# Вывод в UTF-8: Python (winproc.run_hidden) читает дочерний вывод как UTF-8.
# Без этого кириллица в stdout/stderr декодируется системной кодовой страницей
# и превращается в мусор. try/catch — на PowerShell 5.1 без консоли сеттер
# [Console]::OutputEncoding может бросить IOException.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# Читаем JSON-бэкап списка языков
$entries = Get-Content -LiteralPath $JsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($entries -is [PSCustomObject]) {
    $entries = @($entries)
}

if ($entries.Count -eq 0) {
    Write-Output "EMPTY_ENTRIES: $JsonPath"
    exit 1
}

# FIX-25: песочница. Реестр изолирован, а Set-WinUserLanguageList — нет.
# Возвращаемся ДО чтения снимка, чтобы песочница не могла вызвать WinRT ни
# при каком содержимом файла.
if ($Sandbox) {
    Write-Output 'SANDBOX_SKIPPED'
    exit 0
}

# Фаза 1 (FIX-25): полная валидация снимка ДО первого вызова WinRT.
# Любой дефект отклоняет файл целиком — частичного применения нет.
$valid = @()
$index = 0
foreach ($entry in $entries) {
    $tag = $entry.LanguageTag

    if ($null -eq $tag) {
        Write-Output ('INVALID_TAG: entry ' + $index + ' - LanguageTag missing')
        exit 1
    }

    # Нескалярный тег = ровно тот дефект, что давал потерю языка:
    # backup.ps1 отдавал LanguageTag массивом всех тегов сразу.
    if ($tag -isnot [string]) {
        Write-Output ('INVALID_TAG: entry ' + $index + ' - LanguageTag is not a string (' + $tag.GetType().Name + ')')
        exit 1
    }
    if ($tag -notmatch $TagPattern) {
        Write-Output ('INVALID_TAG: entry ' + $index + ' - malformed tag [' + $tag + ']')
        exit 1
    }

    $tips = @()
    if ($entry.InputMethodTips) { $tips = @($entry.InputMethodTips) }

    $valid += [pscustomobject]@{
        Index = $index
        Tag = $tag
        Tips = $tips
        Entry = $entry
    }
    $index++
}

# Фаза 2: сборка объектов языков. К этому моменту каждый тег прошёл
# проверку, поэтому New-WinUserLanguageList не может получить мусор.
$kept = @()

foreach ($item in $valid) {
    $tag_ps = $item.Tag -replace "'", "''"

    # Создаём новый объект языка (с раскладками или без них)
    $nl = New-WinUserLanguageList -Language $tag_ps
    if ($item.Tips.Count -gt 0) {
        $nl[0].InputMethodTips.Clear()
        foreach ($tip in $item.Tips) {
            $tip_ps = $tip -replace "'", "''"
            [void]$nl[0].InputMethodTips.Add($tip_ps)
        }
    }

    # FIX-3: возвращаем настройки письма. В старых снимках полей нет —
    # $item.Entry.Handwriting даёт $null и присвоение пропускается.
    if ($null -ne $item.Entry.Handwriting) {
        try { $nl[0].Handwriting = [bool]$item.Entry.Handwriting } catch { }
    }
    if ($null -ne $item.Entry.Spellchecking) {
        try { $nl[0].Spellchecking = [bool]$item.Entry.Spellchecking } catch { }
    }

    $kept = @($kept) + $nl[0]
}

# Защита (FIX-3): Set-WinUserLanguageList с пустым списком очистил бы ВСЕ
# языки пользователя. Раньше такое было возможно при рассинхроне формата
# (Python писал массивы массивов, скрипт читал свойства — все записи
# пропускались).
if ($kept.Count -eq 0) {
    Write-Output "EMPTY_ENTRIES: $JsonPath"
    exit 1
}

# Применяем restored список
Set-WinUserLanguageList -LanguageList @($kept) -Force | Out-Null
Write-Output "SUCCESS"
