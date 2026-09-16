# ТЗ: Windows Keyboard Layout Cleaner (GUI Tool)

## 1. Цель проекта
Разработать GUI-приложение для Windows, позволяющее находить, анализировать и полностью удалять фантомные и ненужные раскладки клавиатуры из профиля пользователя и системных разделов реестра.

## 2. Стек технологий
- **Язык**: Python 3.10+
- **GUI**: CustomTkinter (современный интерфейс)
- **Системные библиотеки**: `winreg`, `ctypes`, `subprocess` (PowerShell integration)

---

## 3. Архитектура и Этапы Разработки

### Этап 1: Модуль поиска и сопоставления (scanner.py)
- [x] Динамическое чтение названий раскладок из `HKLM:\SYSTEM\CurrentControlSet\Control\Keyboard Layouts` + fallback-словарь `LAYOUT_MAP`.
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
├── config.py          # Центральные константы (SANDBOX_ROOT, SANDBOX_MODE)
├── main.py            # GUI-приложение (CustomTkinter)
├── scanner.py         # Модуль сканирования реестра
├── cleaner.py         # Модуль бэкапа и удаления
├── gui_widgets.py     # Переиспользуемые GUI-компоненты
├── i18n.py            # Локализация
├── ui_theme.py        # Темы оформления
├── applog.py          # Настройка журналирования
├── locales/           # Файлы переводов (en, ru, es, de, zh, pt)
├── conftest.py        # Фикстуры pytest
├── test_scanner.py    # Unit-тесты сканера и чистильщика
├── test_sandbox.py    # Live-тесты в sandbox-режиме
├── test_elevation.py  # Тесты механизма UAC
└── ARCHITECTURE.md    # Этот файл
```

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
| Языковой список (WinRT `Get-/Set-WinUserLanguageList`) | ✅ | ✅ | ⚠️ только через PowerShell-API | прямая мутация raw-реестра списка запрещена |
| `HKCU\Software\Microsoft\CTF` | ✅ | ✅ | ✅ | SECONDARY: TSF-профили (decimal-HKL), очистка с остановкой ctfmon |
| `HKCU\...\SettingSync\Namespace\Language` | ✅ | ✅ | ✅ | SECONDARY |
| `HKU\.DEFAULT\Keyboard Layout\Preload` | ✅ | ✅ | ✅ (Admin) | SECONDARY: раскладка до логина |
| `HKEY_USERS\<SID>` других пользователей | — | — | — | out of scope by design: hives могут быть не загружены, правки перетираются активной сессией |
| `HKLM\SYSTEM\...\Keyboard Layouts` | ✅ | ✅ (имена) | ❌ никогда | CATALOG: наличие в каталоге ≠ установлена у пользователя; справочник имён `LAYOUT_MAP` |

Роль CORRELATE выполняет агрегация `locations` по KLID (`scanner.py`) плюс
эвристика фантома в UI (`main.py`): если все записи KLID найдены только в
SECONDARY-источниках (нет в Preload / User Profile), раскладка — вероятный
орфан. Каталог участвует в корреляции только именем раскладки и никогда —
как доказательство её установки.