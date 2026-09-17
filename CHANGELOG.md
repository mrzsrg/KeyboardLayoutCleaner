# Changelog

Все заметные изменения в этом проекте задокументированы в этом файле.

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
проект придерживается [Semantic Versioning](https://semver.org/).

## [1.0.4] — 2026-09-17

## [1.0.5] — 2026-09-17

### Исправлено
- Мьютекс single-instance перенесён из `Global\` в `Local\`: независимый запуск в разных сессиях Windows. Требование `SeCreateGlobalPrivilege` для глобальных file-mapping объектов не относится к мьютексам; прежний отказ стандартной учётной записи не воспроизводился.
- CJK-словарь PowerShell хранит голые KLID; ko-KR исправлен на `00000412`, в соответствии со сканером.
- Применение очистки языков: `.ToArray()` вместо `@($kept)`, вызывавшего `Argument types do not match` для `List[object]`.
- Галочка блокировки облачной синхронизации при создании окна отражает реестр. Регрессионный тест проверяет оба состояния.
- `RtlGetVersion`: один аргумент, явные `argtypes`/`restype`, корректный комментарий NTSTATUS.
- `FakeWinreg.QueryValueEx` и `SetValueEx` работают с int-константами корня; добавлена регрессия.
- GUI-тесты вызывают реальные методы очереди и обработки сканирования. Общий `_ensure_main` используется в GUI-наборах, подмены модулей и окружения откатываются; нативная проверка версии подменяется до импорта, без сбрасывающего патч reload.
- Sandbox-тест записывает настоящий двухбайтовый BOM FF FE вместо восьми ASCII-символов.
- Coverage: действующий `fail_under = 50` вместо игнорируемого `failunder`; политика описана в CONTRIBUTING.
- Ruff: удалено ошибочное `known-first-party = ["layout_cleaner"]`.

### Добавлено
- Восемь изолированных тестов настоящего PowerShell: GUID-TIP для zh-CN, zh-TW, ja-JP, ko-KR, dry-run и apply. Get/Set-WinUserLanguageList подменены в дочернем процессе; языки пользователя не меняются.
- Mypy в CI и в dev lock-файле (версия согласована с pre-commit); сборки зависят от проверки типов.
- Исключение локальной распакованной копии релиза из обнаружения pytest; оригиналы не удаляются.
- BUILD.md с инструкциями onedir-сборки. Портативный EXE 1.0.5 собран и проверен запуском.

### Удалено
- Неиспользуемые `_build_cleanup_script` и `_load_ps1_script`.
- Дублирующий блок `[project.optional-dependencies] dev`: зависимости разработки управляются через requirements-dev.in / requirements-dev.txt; PyInstaller устанавливается отдельно.
- Пустой корневой `__init__.py`: проект использует плоскую структуру модулей.

## [1.0.4] — 2026-09-17

### Исправлено (по итогам ревью)
- Cleaner: внедрён контекстный менеджер `CtfmonSuspender` для гарантированного перезапуска `ctfmon.exe` и `TextInputHost.exe` даже при исключениях во время очистки.
- Cleaner: операции очистки (`_wipe_branches`) приведены к идемпотентному виду — безопасный многократный вызов без побочных эффектов.
- GUI: переход на потокобезопасную передачу данных из фоновых потоков через `queue.Queue` — исключены краши Tkinter при многопоточности.
- Scanner: добавлен `frozenset _METADATA_VALUE_NAMES` для фильтрации метаданных языковых профилей (`FeaturesToInstall`, `CachedLanguageName`, `ShowCasing`, `WindowsOverride`) — защита от ложного определения как KLID.

### Добавлено
- Config: добавлена таблица `TIMEOUTS` с централизованными настраиваемыми таймаутами для операций (reg export, reg import, PowerShell, ожидание мьютекса).
- Cleaner: добавлена функция `_check_reg_backup_file()` для строгой валидации .reg бэкапов перед восстановлением (проверка размера файла, наличия BOM, корректности кодировки UTF-16 LE).
- Cleaner: добавлена функция `_system_info()` для сбора системной информации (версия Windows, Python, приложения) — используется в отчётах об ошибках и логах.
- Main: масштабное обновление GUI — улучшена обработка состояний кнопок, статус-бара и прогресс-индикаторов при длительных операциях.

## [1.0.3] — 2026-09-16

### Добавлено
- Version-ресурс в exe (`FileVersion`/`ProductVersion`/`CompanyName` из pyproject.toml): корректная атрибуция в «Свойствах» файла и снижение числа ложных срабатываний антивирусов.
- README: раздел «Антивирус и ложные срабатывания» — почему эвристики реагируют на утилиту (реестр + скрытый PowerShell + UAC), что делать пользователям (восстановление, исключения, заявка FP в Microsoft), верификация по SHA256 из релиза.
- `ARCHITECTURE.md`: матрица источников раскладок (SCAN / CORRELATE / MUTATE / DIAGNOSTIC ONLY) с ролями PRIMARY/SECONDARY/CATALOG; в список источников возвращён SettingSync (был в коде, но отсутствовал в документации).
- `ARCHITECTURE.md` §6.1: разграничение «фантом (orphan)» vs «false positive классификации» — регрессия `000006ff` относится ко второму случаю (значение найдено верно, ошибкой была интерпретация как KLID).
- Регрессионный тест DWORD-пути `FeaturesToInstall`: в живом реестре значение хранится как `REG_DWORD 0x6ff`, а строка «000006ff» возникает лишь при рендеринге int в нормализаторе сканера; фильтр метаданных срабатывает по имени значения независимо от типа (`test_features_to_install_dword_not_klid`).
- Инвариант-тесты `TestSourceCoverageInvariants`: состав `AFFECTED_BRANCHES` (ровно 6 источников), Admin-гвард только на `HKU\.DEFAULT`, каталог `HKLM Keyboard Layouts` никогда не является источником мутации, `_REPORT_FIELD` покрывает все ветки, рекурсивная очистка — только внутри HKCU-веток.

### Исправлено
- Тесты: автофикстура `test_main_gui_extended.py` больше не «протекает» в глобальное состояние — `ctypes.windll`, `ctypes.wintypes`, `winreg.OpenKeyEx/EnumValue` подменяются через `monkeypatch` с гарантированным откатом. Раньше мок `kernel32` переживал тесты и ломал `test_mutex_restart.py` / `test_restart_integration.py` / `test_elevation.py` при алфавитном порядке прогона; ручной список файлов в CI маскировал поломку.
- CLI `python scanner.py --sandbox` теперь действительно переключает ветки: вызывается `activate_sandbox()` (подменяет модульные `HKCU_BRANCHES`/`HKU_BRANCHES`/`_RECURSIVE_SCAN`), а не только флаг `enable_sandbox()` — раньше молча сканировался живой реестр вопреки `--help`. Заодно закрыта протечка живых данных в sandbox: PowerShell-источник (`Get-WinUserLanguageList`, не изолируемый sandbox-ключом) при активной песочнице исключается из скана.
- Снятие галочки блокировки облачной синхронизации теперь реально разблокирует: добавлен `cleaner.enable_language_sync()` (восстанавливает `SettingSync\Groups\Language\Enabled = 1`); при неудаче статус показывает причину, галочка возвращается в отмеченное состояние.
- CI: юнит-джоба переведена с ручного списка файлов на `pytest -m "not live"` — новые тест-файлы попадают в пайплайн автоматически (`test_mutex_is_elevate.py` раньше вообще не запускался в CI); в комментарии live-джобы исправлено имя тестового ключа (`KeyboardCleanerTest`).
- `sign_exe.py`: `/du` указывает на реальный репозиторий; TSA-сервер по умолчанию — `https://timestamp.digicert.com` (был `http://`); в CI `build-release` добавлен опциональный шаг подписи + верификации (активируется секретами `WIN_CERT_PFX`/`WIN_CERT_PASS`).
- Докстринги: `backup_registry` (папка бэкапов — `<приложение>/backups`, а не `cwd`), `delete_layout` (в отчёте добавлены `klid`, `last_layout_guard`, `retry_cleaned`), `__main__.py` (запуск — `python .` / `python main.py`; `python -m keyboard-layout-cleaner` невозможен из-за дефиса в имени).
- `pyproject.toml`: `mutex` добавлен в coverage `source` и `py-modules`; `.pre-commit-config.yaml`: убран архивный пакет `types-all` из mypy (не устанавливается в чистом окружении).
- `conftest.FakeWinreg`: добавлен алиас `CreateKey = CreateKeyEx` (реальный winreg экспортирует оба имени) — нужен для тестов `disable/enable_language_sync`.

### Изменено
- `ARCHITECTURE.md` §4 «Структура проекта» актуализирована: добавлены mutex, winproc, sign_exe, `__main__`, scripts/, полный состав тестов и спека PyInstaller.

### Удалено
- Мёртвые полуфичи: `SystemTray` и `TreeviewLayoutList` в `gui_widgets.py` (нигде не инстанцировались; несуществующий путь иконки `assets/icon.ico`; две идентичные ветки в `_build_layout_name_from_klid`) и трей-методы `main.py` (`_show_window`/`_hide_window`/`quit_from_tray` — обращение к несуществующему `self.pid`); неиспользуемый `_restart_ctfmon` в cleaner.
- Устаревший `BUGFIX_PLAN.md`: итоговая таблица противоречила фактическому зелёному состоянию (метрики от 2026-09-14); история исправлений — в CHANGELOG и git-истории.

## [1.0.1] — 2026-09-16

### Добавлено
- Режим песочницы (sandbox) для безопасного тестирования
- Два UI-интерфейса (Classic + Modern)
- Локализация на 6 языков (en, ru, es, de, zh, pt)
- Автосохранение настроек
- Горячие клавиши и Drag & Drop
- Подсветка строк (пульсирующая анимация)
- Полная поддержка портативного режима (запуск с флешки)
- `CONTRIBUTING.md` — гайд для разработчиков

### Исправлено
- P0-1: Добавлен `__main__.py` для запуска через `python -m`
- P0-3: Добавлена ротация логов (RotatingFileHandler, 10MB, 5 бэкапов)
- P0-4: Заменено `except Exception: pass` на логирование (5 мест)
- P0-5: Проверен `.gitignore` (корректен)
- P1-1: Проверка версии Python — только для dev-режима
- P1-2: `threading.Lock` для защиты `SANDBOX_MODE`
- P1-4: Обработка `FileNotFoundError` в `_load_ps1_script`
- P2-1: Создан `.ruff.toml` с конфигурацией линтинга
- P2-3: Добавлены fallback-шрифты
- P2-4: Добавлен pytest-cov в dev-зависимости
- P2-5: Версия читается через `importlib.metadata.version()`
- Аудит: `scripts/*.ps1` и `locales/*.json` включены в сборку PyInstaller
  (спека `keyboard_cleaner.spec`)
- Аудит: убран неработоспособный консольный entry-point — `pip install .`
  не является поддерживаемым способом установки (запуск из исходников / .exe)
- Аудит: исправлена бесконечная рекурсия `SandboxModeProxy.__repr__`
- Аудит: `tempfile.mktemp` → безопасный `mkstemp` (TOCTOU / Bandit B306)
- Аудит: JSON списка языков пишется строго в файл (backup.ps1), а не зависит
  от чистоты stdout
- Аудит: мягкий fallback при повреждённом/отсутствующем `locales/en.json`
- Аудит: устранён повторный запуск ctfmon (двойной `_start_ctfmon`)
- Аудит: уникальность имён бэкапов (микросекунды) + фильтр `list_backups`
- Сканер: значения-метаданные языкового профиля (`FeaturesToInstall`,
  `CachedLanguageName`, `ShowCasing`, `WindowsOverride`) больше не
  классифицируются как KLID-раскладки — регрессия `000006ff` (битовая маска
  флагов в `FeaturesToInstall` ошибочно показывалась как
  «Layout (000006ff)»). Контекстная фильтрация по имени значения применена
  и в cleaner (plan/delete не трогают метаданные)

### Изменено
- Аудит: `max-complexity` в ruff снижен с 50 до 15, сложные функции
  декомпозированы на конвейеры мелких хелперов
- Аудит: CLI `scanner.py`/`cleaner.py` переведены на полноценный argparse
  (`--plan`, `--json`, `--sandbox`)

### Удалено
- Аудит: мёртвый код — параметр `collect_permission_errors`, поле
  `CtfmonSuspender._was_started`, мёртвый цикл `permission_errors`, лишний
  `idx += 1` в dry-run

---

## [1.0.0] — 2026-09-14

### Добавлено
- Сканирование реестра Windows для поиска фантомных раскладок
- Удаление раскладок с бэкапом реестра (.reg + .json)
- GUI на CustomTkinter (Dark theme)
- Проверка прав администратора с автоперезапуском
- Mutex для предотвращения параллельных запусков
- Проверка Windows 10+ (build 10240)
- Логирование в файл и stderr
- Командная строка и аргументы (--sandbox, --log-level, --debug)
- Система предупреждений (popup при конфликтах, скрытых раскладках)
- CI/CD пайплайн (lint, test, build, release)
- Полное покрытие тестами (unit + integration + live)

[Unreleased]: https://github.com/mrzsrg/KeyboardLayoutCleaner/compare/v1.0.5...HEAD
[1.0.5]: https://github.com/mrzsrg/KeyboardLayoutCleaner/compare/v1.0.4...v1.0.5
[1.0.4]: https://github.com/mrzsrg/KeyboardLayoutCleaner/releases/tag/v1.0.4
[1.0.3]: https://github.com/mrzsrg/KeyboardLayoutCleaner/releases/tag/v1.0.3
[1.0.1]: https://github.com/mrzsrg/KeyboardLayoutCleaner/releases/tag/v1.0.1
[1.0.0]: https://github.com/mrzsrg/KeyboardLayoutCleaner/releases/tag/v1.0.0
