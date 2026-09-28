# ТЗ: Windows Keyboard Layout Cleaner (GUI Tool)

## 1. Цель проекта
Разработать GUI-приложение для Windows, позволяющее находить, анализировать и полностью удалять фантомные и ненужные раскладки клавиатуры из профиля пользователя и системных разделов реестра.

## 2. Стек технологий
- **Язык**: Python 3.10+
- **GUI**: CustomTkinter (современный интерфейс)
- **Системные библиотеки**: `winreg`, `ctypes`, `subprocess` (PowerShell integration)

---

## 3. Архитектура и Этапы Разработки

### Этап 0: Единый резолвер идентификаторов (layout_ids.py, FIX-8)
- [x] Единственное место, где живут таблицы и нормализаторы раскладок:
  `LAYOUT_MAP` (BCP-47 → KLID), `CJK_KLIDS`, `KLID_TO_TAGS`,
  `KLID_RE` / `TIP_KLID_RE` / `METADATA_VALUE_NAMES`,
  `normalize_klid_token`, `klid_variants`, `klid_to_tags`,
  `bcp47_to_klid`, `parse_tip`, HKL-математика (`decimal_to_hkl`,
  `hkl_to_decimal`, `hkl_string_forms`, `klid_low_word`), `cjk_map_json`.
- [x] `scanner.py` и `cleaner.py` импортируют модуль; локальные копии
  удалены, прежние имена сохранены тонкими обёртками. Расхождение копий
  означало бы «сканер нашёл раскладку, очистка её не тронула» — теперь
  такой класс дефекта невозможен конструктивно.
- [x] CJK-карта передаётся в PowerShell параметром `-CjkMapJson`, а не
  дублируется в `.ps1`.
- Инвариант «сканер и cleaner видят одно и то же» закреплён тестами
  `TestScanCleanParity` (FIX-7).

### Этап 1: Модуль поиска и сопоставления (scanner.py)
- [x] Динамическое чтение названий раскладок из `HKLM:\SYSTEM\CurrentControlSet\Control\Keyboard Layouts` + fallback-словарь `LAYOUT_MAP` (из `layout_ids`).
- [x] Конвертер/сопоставитель BCP-47 (например, `en-GB`) и KLID HEX-кодов (например, `00000809`).
- [x] Сканер реестра текущего пользователя (`HKCU`):
  - `HKCU:\Keyboard Layout\Preload`
  - `HKCU:\Keyboard Layout\Substitutes`
  - `HKCU:\Control Panel\International\User Profile`
  - `HKCU:\Software\Microsoft\CTF`
  - `HKCU:\Software\Microsoft\Windows\CurrentVersion\SettingSync\Namespace\Language`
- [x] Сканер системного реестра (`HKLM` и `HKU`):
  - `HKLM:\SYSTEM\CurrentControlSet\Control\Keyboard Layouts`
  - `HKEY_USERS\.DEFAULT\Keyboard Layout\Preload`
- [x] Интеграция с PowerShell: запрос `Get-WinUserLanguageList`.

### Этап 2: Модуль Бэкапа и Безопасного Удаления (cleaner.py)
- [x] Функция проверки прав Администратора (`IsUserAnAdmin`).
- [x] Функция экспорт-бэкапа: сохранение затрагиваемых веток реестра в `.reg` файл с поддержкой кодировки UTF-16 LE перед любым изменением.
- [x] Функция удаления:
  - Очистка ключей в `HKCU`.
  - Очистка системных ключей `HKU\.DEFAULT` (только при наличии Admin-прав).
  - Выполнение очищающей команды PowerShell (`Set-WinUserLanguageList`).

### Этап 3: Графический Интерфейс (ui.py / main.py)
- [x] **Шаг 1**: Кнопка «Сканировать систему».
- [x] **Шаг 2**: Список с найденными раскладками (кликабельные строки).
- [x] **Шаг 3**: Панель деталей — вывод всех путей реестра, где обнаружена выбранная раскладка.
- [x] **Шаг 4**: Индикатор прав доступа (Информационный баннер + кнопка «Перезапустить как Админ», если прав нет).
- [x] **Шаг 5**: Кнопка «Удалить раскладку» с подтверждением и отображением пути к созданной копии реестра.

### Этап 4: Тестирование и Безопасность (tests/)
- [x] **Режим Песочницы (Sandbox Mode)**:
  - Реализация флага `--sandbox` / переменной окружения `SANDBOX_MODE=1`.
  - В режиме песочницы изолировать работу с реестром: подменять реальные ветки `HKCU`/`HKLM` на временные тестовые ключи (например, `HKCU:\Software\KeyboardCleanerTest`) или mock-объекты.
- [x] **Модульное тестирование (pytest)**:
  - Покрытие `scanner.py` и `cleaner.py` unit-тестами, которые запускаются исключительно в песочнице без рисков для реальной системы Windows.

---

## 4. Структура проекта

```
keyboard-layout-cleaner/
├── main.py              # GUI-приложение (CustomTkinter), точка входа
├── __main__.py          # Запуск каталога: python .
├── config.py            # Центральные константы (SANDBOX_ROOT, SANDBOX_MODE, версия)
├── layout_ids.py        # Единый резолвер KLID/BCP-47/CJK (FIX-8)
├── winapi.py            # Прототипы WinAPI (проверка версии Windows)
├── scanner.py           # Сканер реестра (+ CLI: --sandbox / --json)
├── cleaner.py           # Оркестратор: план, удаление, отчёт, CLI
├── backup.py            # Регистрационный бэкап/восстановление .reg (FIX-10, шаг 1)
├── mutate.py            # Предикаты сопоставления и мутация реестра (FIX-10, шаг 2)
├── langlist.py          # Список языков: снимок/применение/восстановление (FIX-10, шаг 3)
├── settings.py          # SettingSync и Экран приветствия (FIX-10, шаг 4)
├── mutex.py             # Мьютекс защиты от параллельных запусков
├── winproc.py           # Запуск процессов/PowerShell (run_hidden)
├── gui_widgets.py       # Переиспользуемые GUI-компоненты (AdminBanner/ActionButtons/StatusBar)
├── ui_theme.py          # Темы оформления
├── i18n.py              # Локализация
├── applog.py            # Настройка журналирования и resource_path
├── sign_exe.py          # Подпись .exe через signtool (опционально в CI)
├── keyboard_cleaner.spec  # Спека PyInstaller
├── locales/             # Файлы переводов (en, ru, es, de, zh, pt)
├── scripts/             # PowerShell-скрипты (список языков, бэкап/восстановление)
├── conftest.py          # Фикстуры pytest (FakeWinreg, registry_modules и др.)
├── test_*.py            # Юнит-тесты (FakeWinreg/mock) и live-тесты (sandbox-ключ)
└── ARCHITECTURE.md      # Этот файл
```

### Границы модулей (FIX-10)

`cleaner.py` разделён по границам, которые можно доказать тестами
(`test_module_boundaries.py`). `cleaner` остаётся оркестратором и
**реэкспортирует** имена вынесенных модулей, поэтому
`cleaner.backup_registry` и прочий публичный API не изменились.

| Модуль | Зона ответственности |
|---|---|
| `backup.py` | Экспорт/импорт `.reg`, проверка файла, список бэкапов, `_ROOT_CONST` |
| `mutate.py` | Предикаты сопоставления, снимок и манифест FIX-6, сама мутация, резолверы веток |
| `langlist.py` | Снимок списка языков, разбор, восстановление, весь PS-адаптер списка языков |
| `settings.py` | `SettingSync\Groups\Language` (FIX-2) |
| `cleaner.py` | План, дрейф плана, `delete_layout`, отчётность, CTF-сервисы, CLI |

Правило, общее для всех этих модулей: **обращение к общему состоянию
через модуль** (`backup._ROOT_CONST`, `mutate._sandbox_active()`), а не
привязкой именем. Привязка копирует значение на момент импорта, и
подмена в тестах перестаёт действовать — молча, без исключения.

## 5. Безопасность

- Централизованное управление sandbox-режимом через `config.py`
- Все исключения логируются (нет подавленных исключений)
- Бэкап создаётся ДО любых изменений реестра
- Рекурсивная очистка TSF-профилей с остановкой ctfmon.exe
- Точечная очистка User Profile (не удаляет весь профиль языка)

## 6. Матрица источников раскладок

Каждый источник реестра имеет строго определённую роль: **SCAN** (сканирование),
**CORRELATE** (агрегация улик по KLID), **MUTATE** (очистка/удаление),
**DIAGNOSTIC ONLY** (только справочная информация). Состав веток закреплён
инвариант-тестами `TestSourceCoverageInvariants` (`test_scanner.py`):
изменение матрицы требует одновременного обновления кода, тестов и этой таблицы.

| Источник | SCAN | CORRELATE | MUTATE | Комментарий |
|---|:-:|:-:|:-:|---|
| `HKCU\Keyboard Layout\Preload` | ✅ | ✅ | ✅ | PRIMARY: прямые записи KLID |
| `HKCU\Keyboard Layout\Substitutes` | ✅ | ✅ | ✅ | PRIMARY: подмены KLID→KLID |
| `HKCU\Control Panel\International\User Profile` | ✅ | ✅ | ✅ | PRIMARY: BCP-47-теги + InputMethodOverride/KLP; значения-метаданные (`FeaturesToInstall` и др.) KLID-кандидатами не считаются (регрессия 000006ff) |
| Языковой список (WinRT `Get-/Set-WinUserLanguageList`) | ✅ | ✅ | ⚠️ PowerShell-API **+ реестр** (FIX-37h) | FIX-37h: значение `Languages` и профиль языка правятся и напрямую, когда в `Preload` не осталось других раскладок этого языка. Порядок обязателен: PowerShell-шаг идёт **до** чистки веток, иначе `Get-WinUserLanguageList` не видит подключённых методов ввода и отвечает `NOCHANGE` |
| `HKCU\Software\Microsoft\CTF` | ✅ | ✅ | ✅ | SECONDARY: TSF-профили (decimal-HKL), очистка с остановкой ctfmon |
| `HKCU\...\SettingSync\Namespace\Language` | ✅ | ✅ | ✅ | SECONDARY |
| `HKU\.DEFAULT\Keyboard Layout\Preload` | ✅ | ✅ | ✅ (Admin) | SECONDARY: раскладка до логина |
| `HKU\.DEFAULT\…\User Profile` (+ `System Backup`, `Software\Microsoft\CTF`) | ❌ | ✅ | ✅ (Admin) | FIX-37h, `scanner.CLEAN_ONLY_BRANCHES`: **чистятся, но не сканируются** — язык профиля по умолчанию не язык пользователя, а выключение записывает живой hive обратно в `C:\Users\Default\NTUSER.DAT` |
| `C:\Users\Default\NTUSER.DAT` → 5 веток (`Preload`, `Substitutes`, `…\User Profile`, `…\User Profile System Backup`, `Software\Microsoft\CTF`) **+ живой `HKU\.DEFAULT` в трёх ветках (`…\User Profile`, `…\User Profile System Backup`, `Software\Microsoft\CTF`)** | ✅ | ✅ | ✅ (Admin) | FIX-37: источник, из которого Windows пересобирает `HKU\.DEFAULT` при загрузке; без чистки удаление отменяется перезагрузкой. В шаблоне носитель языка — сразу три вещи: значение `Languages`, ключи профилей с привязками методов ввода (значения, **имя** которых равно TIP `0409:00000409`, FIX-37g) и нумерация CTF; порядок обязателен — `Languages` → профили. Правка файла: `reg load` → правка → **`reg save` во временный файл** → `reg unload` → перезапись шаблона (`ddu.py`); без `reg save` выгрузка отбрасывает правки, `reg save` **в сам шаблон** не возвращается и вешает операцию (FIX-37e), сбой сохранения обязан провалить удаление (FIX-37f). FIX-37h: `HKU\.DEFAULT` — живой hive, и выключение записывает его память **обратно в файл**, поэтому чистится и он (через `scanner.CLEAN_ONLY_BRANCHES` — эти ветки не сканируются, иначе язык шаблона выдавался бы за раскладку пользователя; в песочнице список отбрасывается) |
| `HKEY_USERS\<SID>` других пользователей | — | — | — | out of scope by design: hives могут быть не загружены, правки перетираются активной сессией |
| `HKLM\SYSTEM\...\Keyboard Layouts` | ✅ | ✅ (имена) | ❌ никогда | CATALOG: наличие в каталоге ≠ установлена у пользователя; справочник имён `LAYOUT_MAP` |

Роль CORRELATE выполняет агрегация `locations` по KLID (`scanner.py`) плюс
эвристика фантома в UI (`main.py`): если все записи KLID найдены только в
SECONDARY-источниках (нет в Preload / User Profile), раскладка — вероятный
орфан. Каталог участвует в корреляции только именем раскладки и никогда —
как доказательство её установки.

### 6.1. «Фантом» ≠ «false positive классификации»

Разные явления, смешивать которые нельзя:

- **Фантом (orphan)** — следы *реальной* раскладки, оставшиеся только во
  SECONDARY-источниках (типично — после удаления языка). KLID подлинный,
  в PRIMARY записей нет. Кандидат на очистку после подтверждения
  пользователем — именно этот случай распознаёт эвристика UI выше.
- **False positive классификации** — данные, вовсе не являющиеся раскладкой.
  Регрессия `000006ff`: значение `FeaturesToInstall = REG_DWORD 0x6ff` —
  битовая маска флагов; сканер верно нашёл значение, но неверно
  интерпретировал его как KLID (строка «000006ff» возникает только при
  рендеринге int в нормализаторе). Чистить нечего — ошибка была в
  интерпретации и исправлена фильтром метаданных.

Практическое следствие: если «раскладка» исчезла из списка после обновления
сканера без каких-либо действий по очистке — это был false positive,
а не фантом.