<#
.SYNOPSIS
    FIX-12: проверка гипотезы «дыра в нумерации Preload отбрасывает
    раскладки после неё».

.DESCRIPTION
    Проверка требует настоящей смены пользователя (выход/вход), потому
    что кеш TSF/ctfmon пересобирается именно при входе. Скрипт
    трёхфазный и обратимый:

      Prepare - полный бэкап, три раскладки, удаление СРЕДНЕЙ (дыра
                1 и 3), сохранение ожидаемого состояния;
      Check   - ПОСЛЕ входа: читает фактическое состояние и сравнивает;
      Restore - возврат исходного состояния из бэкапа.

    Без ключа -IUnderstandThisIsATestMachine скрипт НЕ СТАРТУЕТ: он
    меняет профиль пользователя, а на единственной машине это тот же
    класс вреда, что и прогон тестов без песочницы.

    КОДИРОВКА. В отличие от scripts/*.ps1 этот файл С BOM и с кириллицей
    в выводе. Там соглашение строгое: без BOM + кириллица только в
    комментариях, stdout обязательно ASCII — потому что вывод парсит
    Python (см. комментарий про кодировку в layout_cleaner_restore.ps1).
    Здесь вывод читает человек, поэтому BOM честнее, чем выкидывать
    сообщения на английский.

.EXAMPLE
    .\FIX12_verify_hole.ps1 -Mode Prepare -IUnderstandThisIsATestMachine
    # выход из системы и вход
    .\FIX12_verify_hole.ps1 -Mode Check
    .\FIX12_verify_hole.ps1 -Mode Restore -IUnderstandThisIsATestMachine
#>

[CmdletBinding()]
param(
    [ValidateSet('Prepare', 'Check', 'Restore')]
    [string]$Mode = 'Check',

    # Третья раскладка, если в системе их меньше трёх. 0000040c = fr-FR.
    [string]$ExtraLanguage = 'fr-FR',
    [string]$ExtraKlid = '0000040c',

    # Пусто по умолчанию: $PSScriptRoot в значении параметра по умолчанию
    # вычисляется раньше, чем контекст скрипта определён, и даёт пустой
    # путь ('\state\...'). Реальный каталог подставляется в теле.
    [string]$StateDir = '',

    # Скрипт меняет профиль пользователя. Без этого ключа не стартует.
    [switch]$IUnderstandThisIsATestMachine
)

$ErrorActionPreference = 'Stop'

$PRELOAD = 'HKCU:\Keyboard Layout\Preload'
$SORTORDER = 'HKCU:\Software\Microsoft\CTF\SortOrder\Language'

if (-not $StateDir) {
    $here = $PSScriptRoot
    if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Path }
    $StateDir = Join-Path $here 'state'
}

function Get-PreloadMap {
    if (-not (Test-Path $PRELOAD)) { return @{} }
    $map = @{}
    (Get-Item $PRELOAD).GetValueNames() | ForEach-Object {
        $map[$_] = (Get-ItemProperty $PRELOAD -Name $_).$_
    }
    return $map
}

function Get-SortOrderMap {
    if (-not (Test-Path $SORTORDER)) { return @{} }
    $map = @{}
    (Get-Item $SORTORDER).GetValueNames() | ForEach-Object {
        $map[$_] = (Get-ItemProperty $SORTORDER -Name $_).$_
    }
    return $map
}

# GetKeyboardLayoutList показывает раскладки, доступные ПЕРЕКЛЮЧАТЕЛЮ.
# Это ровно то, что нужно: значение реестра может остаться на месте,
# а раскладка из переключателя — исчезнуть.
if (-not ('Win32KbNative' -as [type])) {
    Add-Type -Namespace Win32Kb -Name Native -MemberDefinition @'
[DllImport("user32.dll", SetLastError = true)]
public static extern uint GetKeyboardLayoutList(int nBuff,
    [Out(), MarshalAs(UnmanagedType.LPArray, SizeParamIndex = 0)]
    System.IntPtr[] lpBuff);
'@
}

function Get-SwitcherLayouts {
    $count = 8
    $buf = New-Object 'IntPtr[]' $count
    $n = [Win32Kb.Native]::GetKeyboardLayoutList($count, $buf)
    $out = @()
    for ($i = 0; $i -lt $n; $i++) { $out += ('{0:x8}' -f $buf[$i].ToInt64()) }
    return $out
}

function Get-LanguageList {
    return (Get-WinUserLanguageList | ForEach-Object {
        [pscustomobject]@{
            LanguageTag     = $_.LanguageTag
            InputMethodTips = ($_.InputMethodTips -join ',')
        }
    })
}

function Write-State($name, $data) {
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    $path = Join-Path $StateDir "$name.json"
    $data | ConvertTo-Json -Depth 6 | Set-Content -Path $path -Encoding UTF8
    Write-Host "  сохранено: $path" -ForegroundColor DarkGray
}

function Read-State($name) {
    $path = Join-Path $StateDir "$name.json"
    if (-not (Test-Path $path)) { throw "Нет сохранённого состояния '$name': $path" }
    return (Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Show-Current([string]$title) {
    Write-Host ""
    Write-Host "=== $title ===" -ForegroundColor Cyan
    Write-Host "Preload           : $((Get-PreloadMap).GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name)=$($_.Value)" })"
    Write-Host "CTF SortOrder     : $((Get-SortOrderMap).GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name)=$($_.Value)" })"
    Write-Host "Список языков     : $((Get-LanguageList | ForEach-Object { $_.LanguageTag }) -join ', ')"
    Write-Host "Переключатель     : $((Get-SwitcherLayouts) -join ', ')"
}


if ($Mode -ne 'Check' -and -not $IUnderstandThisIsATestMachine) {
    Write-Host @"
ОТКАЗ. Режим -Mode $Mode меняет профиль пользователя: список языков и
HKCU\Keyboard Layout\Preload.

Запускайте ТОЛЬКО на виртуальной машине или на отдельной одноразовой
учётной записи. На единственной машине разработчика этот скрипт - тот
же класс вреда, что и прогон тестов без песочницы.

Если вы понимаете риск, добавьте ключ:
    -IUnderstandThisIsATestMachine
"@ -ForegroundColor Red
    exit 2
}

switch ($Mode) {

    'Prepare' {
        Write-Host "ФАЗА 1: подготовка" -ForegroundColor Yellow
        Show-Current 'Исходное состояние'

        Write-State 'before_preload'   (Get-PreloadMap)
        Write-State 'before_sortorder' (Get-SortOrderMap)
        Write-State 'before_langlist'  (Get-LanguageList)
        Write-State 'before_switcher'  (Get-SwitcherLayouts)

        # Дыру нельзя создать в менее чем трёх раскладках.
        $langs = Get-LanguageList
        if ($langs.Count -lt 3) {
            Write-Host "Раскладок меньше трёх - добавляю $ExtraLanguage." -ForegroundColor Yellow
            $list = New-Object System.Collections.Generic.List[object]
            foreach ($l in $langs) {
                $nl = New-WinUserLanguageList -Language $l.LanguageTag
                $nl[0].InputMethodTips.Clear()
                foreach ($t in ($l.InputMethodTips -split ',')) {
                    if ($t) { [void]$nl[0].InputMethodTips.Add($t) }
                }
                $list.Add($nl[0])
            }
            $extra = New-WinUserLanguageList -Language $ExtraLanguage
            $extra[0].InputMethodTips.Clear()
            [void]$extra[0].InputMethodTips.Add("0000$($ExtraKlid.Substring(4))`:$ExtraKlid")
            $list.Add($extra[0])
            Set-WinUserLanguageList $list -Force
            $langs = Get-LanguageList
            Show-Current 'После добавления третьего языка'
        }

        if ($langs.Count -lt 3) {
            throw "Раскладок всё ещё меньше трёх: $($langs.Count)"
        }

        # Нормальная нумерация 1,2,3 - точка отсчёта до дыры.
        New-Item -Path $PRELOAD -Force | Out-Null
        (Get-PreloadMap).Keys | ForEach-Object {
            Remove-ItemProperty -Path $PRELOAD -Name $_ -ErrorAction SilentlyContinue
        }
        $tips = @($langs | Select-Object -First 3 | ForEach-Object { ($_.InputMethodTips -split ',')[0] })
        $i = 1
        foreach ($t in $tips) {
            Set-ItemProperty -Path $PRELOAD -Name "$i" -Value $t -Type String
            $i++
        }
        Show-Current 'Preload = {1,2,3} (норма, до дыры)'

        # Выход из системы скриптом НЕ делаем: нужен настоящий вход,
        # при котором ctfmon/TSF пересобирают кеш.

        # Дыра: удаляем СРЕДНЮЮ (2), оставляя 1 и 3.
        Remove-ItemProperty -Path $PRELOAD -Name '2' -ErrorAction SilentlyContinue
        Show-Current 'ДЫРА: значение 2 удалено, остались 1 и 3'

        Write-State 'expected_preload'  (Get-PreloadMap)
        Write-State 'expected_switcher' $tips

        Write-Host @"

ГОТОВО К ПРОВЕРКЕ.

  Что произошно   : Preload = { 1 = <первая>, 3 = <третья> } - дыра на 2.
  Ожидается       : все три раскладки в переключателе, дыра безвредна
                    -> гипотеза НЕ подтверждена.
  Ожидается       : раскладок стало две, третья пропала
                    -> гипотеза ПОДТВЕРЖДЕНА, нужна пере-нумерация.

  ДАЛЬШЕ:
    1) Выйдите из системы и войдите обратно. Смена пользователя
       обязательна: кеш TSF пересобирается именно при входе.
    2) .\FIX12_verify_hole.ps1 -Mode Check
    3) .\FIX12_verify_hole.ps1 -Mode Restore -IUnderstandThisIsATestMachine
"@ -ForegroundColor Green
    }

    'Check' {
        Write-Host "ФАЗА 2: проверка после входа" -ForegroundColor Yellow
        Show-Current 'Фактическое состояние'

        $expectedPreload = Read-State 'expected_preload'
        $expectedSwitcher = @(Read-State 'expected_switcher')
        $before = @(Read-State 'before_switcher')
        $actual = @(Get-SwitcherLayouts)

        $lost = @($expectedSwitcher | Where-Object { $actual -notcontains $_ })
        $kept = @($expectedSwitcher | Where-Object { $actual -contains $_ })

        Write-Host ""
        Write-Host "--- РЕЗУЛЬТАТ ---" -ForegroundColor Cyan
        Write-Host "Preload после дыры        : $((($expectedPreload.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" })) -join ' ')"
        Write-Host "Раскладок до опыта        : $($before.Count) ($($before -join ', '))"
        Write-Host "Ожидалось в переключателе : $($expectedSwitcher.Count) ($($expectedSwitcher -join ', '))"
        Write-Host "Фактически в переключателе: $($actual.Count) ($($actual -join ', '))"
        Write-Host "Уцелели после дыры        : $($kept.Count) ($($kept -join ', '))"
        if ($lost.Count -gt 0) {
            Write-Host "ПОТЕРЯНЫ после дыры       : $($lost.Count) ($($lost -join ', '))" -ForegroundColor Red
        } else {
            Write-Host "ПОТЕРЯНО                  : нет" -ForegroundColor Green
        }

        $confirmed = $lost.Count -gt 0
        $conclusion = if ($confirmed) {
            'ПОДТВЕРЖДЕНО: дыра в Preload приводит к отбрасыванию раскладок после неё'
        } else {
            'НЕ ПОДТВЕРЖДЕНО: дыра в Preload не повлияла на переключатель'
        }
        Write-Host ""
        Write-Host "ВЫВОД: $conclusion" -ForegroundColor $(if ($confirmed) { 'Red' } else { 'Green' })

        Write-State ("result_" + (Get-Date -Format 'yyyyMMdd_HHmmss')) ([pscustomobject]@{
            when_utc             = (Get-Date).ToUniversalTime().ToString('o')
            os_caption           = (Get-CimInstance Win32_OperatingSystem).Caption
            os_build             = [string](Get-CimInstance Win32_OperatingSystem).BuildNumber
            hypothesis_confirmed = $confirmed
            expected_switcher    = $expectedSwitcher
            actual_switcher      = $actual
            lost_after_hole      = $lost
            conclusion           = $conclusion
        })
        Write-Host "Отчёт записан в $StateDir - приложите его к задаче FIX-12." -ForegroundColor Green
    }

    'Restore' {
        Write-Host "ОТКАТ" -ForegroundColor Yellow
        Show-Current 'Текущее состояние'

        $preload = Read-State 'before_preload'
        $sort = Read-State 'before_sortorder'
        $langs = @(Read-State 'before_langlist')

        New-Item -Path $PRELOAD -Force | Out-Null
        (Get-PreloadMap).Keys | ForEach-Object {
            Remove-ItemProperty -Path $PRELOAD -Name $_ -ErrorAction SilentlyContinue
        }
        foreach ($p in $preload.PSObject.Properties) {
            Set-ItemProperty -Path $PRELOAD -Name $p.Name -Value $p.Value -Type String
        }

        if ($sort.PSObject.Properties.Count -gt 0) {
            New-Item -Path $SORTORDER -Force | Out-Null
            (Get-SortOrderMap).Keys | ForEach-Object {
                Remove-ItemProperty -Path $SORTORDER -Name $_ -ErrorAction SilentlyContinue
            }
            foreach ($p in $sort.PSObject.Properties) {
                Set-ItemProperty -Path $SORTORDER -Name $p.Name -Value $p.Value -Type String
            }
        }

        $list = New-Object System.Collections.Generic.List[object]
        foreach ($l in $langs) {
            $nl = New-WinUserLanguageList -Language $l.LanguageTag
            $nl[0].InputMethodTips.Clear()
            foreach ($t in ($l.InputMethodTips -split ',')) {
                if ($t) { [void]$nl[0].InputMethodTips.Add($t) }
            }
            $list.Add($nl[0])
        }
        Set-WinUserLanguageList $list -Force

        Show-Current 'После отката'
        Write-Host "Список языков и Preload возвращены. Для полного вступления в силу" -ForegroundColor Green
        Write-Host "нужен выход и вход в систему." -ForegroundColor Green
    }
}
