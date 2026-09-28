#
# layout_cleaner_cleanup.ps1
#
# PowerShell-скрипт для очистки фантомной раскладки из списка языков.
# Запускается из cleaner.py через subprocess.run с параметрами:
#   -LayoutKlid <8-символьный HEX KLID>
#   [-CjkMapJson <путь к JSON-файлу с картой CJK>]  # см. ниже
#   [-LanguageTags <BCP-47-теги языка удаляемой раскладки через запятую>]  # FIX-37i
#   [-Apply]  # если нет — только dry-run (план изменений)
#
# Вывод (в stdout):
#   DROP|<tag>|<tip> — раскладка будет удалена из языка
#   REMOVELANG|<tag> — язык будет удалён полностью (нет раскладок)
#   NOCHANGE — ничего не изменено
#   DRYRUN — план готов, изменения НЕ применены
#   EMPTY_SKIPPED — нельзя удалить все языки
#   INVALID_TAG: <тег> — некорректный BCP-47-тег в -LanguageTags, WinRT-API
#     НЕ вызывался (fail-closed)
#   SUCCESS — изменения применены

param(
    [Parameter(Mandatory=$true)]
    [string]$LayoutKlid,

    # FIX-8: CJK-карта LanguageTag -> KLID приходит из Python
    # (layout_ids.cjk_map_json()), а не хранится копией в этом файле:
    # расхождение копий означало бы «сканер видит CJK-раскладку, очистка — нет».
    [string]$CjkMapJson,

    [switch]$Apply,

    # FIX-37i: BCP-47-теги языка удаляемой раскладки через запятую
    # (например "en-US"). Приходят из layout_ids.klid_to_tags() — того же
    # единственного источника, что и карта CJK (FIX-8).
    #
    # Зачем: язык может остаться в списке БЕЗ единого метода ввода
    # (осиротеть). Прежний скрипт умел убрать язык только если убрал его
    # раскладку в этом же прогоне, поэтому на осиротевшем языке отвечал
    # NOCHANGE, и Windows продолжал показывать его в «Предпочитаемых
    # языках» (живой прогон 28.09.2026: en-US с InputMethodTips=[]).
    # Теги ограничивают уборку ТОЛЬКО языком удаляемой раскладки: чужие
    # языки без раскладок (например, добавленные ради проверки орфографии)
    # не трогаются.
    [string]$LanguageTags,

    # FIX-25: песочница. Реестр изолирован, но Set-WinUserLanguageList —
    # WinRT-API, минующий реестр, и песочницей НЕ изолируется. Флаг
    # запрещает вызов: в песочнице скрипт доводит план до конца и
    # возвращает SANDBOX_SKIPPED, не меняя реальный список языков.
    [switch]$Sandbox
)

$ErrorActionPreference = 'Stop'

# Вывод в UTF-8: Python (winproc.run_hidden) читает дочерний вывод как UTF-8.
# Без этого кириллица в stdout/stderr декодируется системной кодовой страницей
# и превращается в мусор. try/catch — на PowerShell 5.1 без консоли сеттер
# [Console]::OutputEncoding может бросить IOException.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# Защита от инъекции: KLID обязан быть ровно 8 HEX-символов
if ($LayoutKlid -notmatch '^[0-9a-fA-F]{8}$') {
    Write-Output "INVALID_KLID: $LayoutKlid"
    exit 1
}

# FIX-37i: языки, которые умеем убирать целиком, — по BCP-47-тегам из
# -LanguageTags. Практическая форма тега та же, что в restore.ps1: пробел
# или склейка нескольких тегов сюда не попадает. Валидация fail-closed и
# ДО любой работы со списком: кривой тег не должен доехать до WinRT-API,
# который заменяет список целиком.
$TagPattern = '^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*$'
$targetTags = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
if ($LanguageTags) {
    foreach ($raw in ($LanguageTags -split ',')) {
        $candidate = $raw.Trim()
        if (-not $candidate) { continue }
        if ($candidate -notmatch $TagPattern) {
            Write-Output "INVALID_TAG: $candidate"
            exit 1
        }
        [void]$targetTags.Add($candidate)
    }
}

# CJK-раскладки: InputMethodTips хранят GUID вместо KLID, поэтому нужен
# маппинг LanguageTag -> KLID. Источник — layout_ids.CJK_KLIDS (Python).
$cjk = @{}
if ($CjkMapJson) {
    try {
        $loaded = Get-Content -LiteralPath $CjkMapJson -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($prop in $loaded.PSObject.Properties) {
            $cjk[$prop.Name] = [string]$prop.Value
        }
    } catch {
        Write-Output "INVALID_CJK_MAP: $CjkMapJson"
        exit 1
    }
}

try {
    # Get-WinUserLanguageList возвращает СПИСОК List<WinUserLanguage> как
    # ОДИН объект, а не поток языков. Прежний код делал
    #     $langs = @(Get-WinUserLanguageList)
    # и получал массив ИЗ ОДНОГО элемента — самого списка. Тогда
    # `foreach ($l in $langs)` выполнял одну итерацию, в которой $l был
    # списком, а не языком: $l.LanguageTag склеивал все теги ("ru en-US"),
    # а $l.InputMethodTips — все раскладки сразу. Любая правка такого
    # $l заканчивалась MethodInvocationException, и скрипт падал, ничего
    # не применяя. foreach по коллекции разворачивает её сам, поэтому
    # элементы собираем явно.
    $langs = @()
    foreach ($item in (Get-WinUserLanguageList)) { $langs += $item }
} catch {
    Write-Output "FAILED: Get-WinUserLanguageList"
    exit 0
}

$changed = $false
$kept = New-Object System.Collections.Generic.List[object]
$report = New-Object System.Collections.Generic.List[string]

# FIX-37i: индекс нужен для защиты языка интерфейса — первый элемент списка
# и есть язык интерфейса, а Set-WinUserLanguageList делает интерфейсным тот,
# что останется первым. Удалять его вместе с раскладкой значит молча сменить
# язык интерфейса, чего удаление раскладки не обещает. Windows и сам
# оставляет язык, из которого убрали последнюю раскладку, если он интерфейсный.
for ($i = 0; $i -lt $langs.Count; $i++) {
    $l = $langs[$i]
    $isDisplay = ($i -eq 0)
    $tag = $l.LanguageTag
    $tips = @($l.InputMethodTips)
    $dropTips = @()

    foreach ($tip in $tips) {
        $drop = $false
        $parts = $tip -split ':'
        if ($parts.Count -eq 2 -and $parts[1] -ieq $LayoutKlid) {
            $drop = $true
        }
        if (-not $drop -and $tip.Contains('{')) {
            $mapped = $cjk[$tag]
            if ($mapped -and $mapped -ieq $LayoutKlid) {
                $drop = $true
            }
        }

        if ($drop) {
            $dropTips += $tip
            $report.Add('DROP|' + $tag + '|' + $tip)
        }
    }

    $removeLang = $false
    if ($dropTips.Count -gt 0) {
        $changed = $true
        # Свойство InputMethodTips у WinUserLanguage ТОЛЬКО ДЛЯ ЧТЕНИЯ
        # (присваивание даёт RuntimeException), но сам список мутабелен.
        # Поэтому свойство не присваиваем, а очищаем и заполняем заново —
        # так работает и List<string>, и обычный массив.
        $keptTips = @()
        foreach ($t in $tips) {
            if ($dropTips -notcontains $t) { $keptTips += $t }
        }
        $l.InputMethodTips.Clear()
        foreach ($t in $keptTips) { [void]$l.InputMethodTips.Add($t) }
        $removeLang = ($keptTips.Count -eq 0) -and -not $isDisplay
        if (-not $removeLang) {
            $kept.Add($l)
        }
    } elseif ($tips.Count -eq 0 -and $targetTags.Contains($tag) -and -not $isDisplay) {
        # FIX-37i: ОСИРОТЕВШИЙ язык удаляемой раскладки. Его подсказки уже
        # вычищены реестровым шагом (Control Panel\International\User
        # Profile\<tag>), поэтому $dropTips пуст и прежний код сюда не
        # доходил: скрипт отвечал NOCHANGE, а язык оставался в списке
        # навсегда — Windows продолжал показывать его в «Предпочитаемых
        # языках» (живой прогон 28.09.2026: en-US, InputMethodTips=[]).
        $changed = $true
        $removeLang = $true
    } else {
        $kept.Add($l)
    }

    if ($removeLang) {
        $report.Add('REMOVELANG|' + $tag)
    }
}

if (-not $changed) {
    Write-Output 'NOCHANGE'
    exit 0
}

foreach ($r in $report) {
    Write-Output $r
}

if ($kept.Count -eq 0) {
    Write-Output 'EMPTY_SKIPPED'
    exit 0
}

if (-not $Apply) {
    Write-Output 'DRYRUN'
    exit 0
}

# FIX-25: последний рубеж перед Set-WinUserLanguageList. Все проверки выше
# читали только реестр, который песочница изолирует; сам Set — WinRT-API,
# и в песочнице он бьёт по РЕАЛЬНОМУ списку языков пользователя.
if ($Sandbox) {
    Write-Output 'SANDBOX_SKIPPED'
    exit 0
}

Set-WinUserLanguageList -LanguageList $kept.ToArray() -Force | Out-Null
Write-Output 'SUCCESS'
