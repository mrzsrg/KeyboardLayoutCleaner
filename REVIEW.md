# Отчёт о ревью проекта — Keyboard Layout Cleaner

Дата: 13.09.2026 · Объём: main.py (2042 стр.), cleaner.py (~1950), scanner.py (~730), ui_theme.py, gui_widgets.py, i18n.py, applog.py, config.py, 5 тест-файлов (103 теста), 2 CI-workflow, спека PyInstaller.

Проверка выполнена фактически: запущены все тесты (`test_scanner` — 55 passed; `test_sandbox` — 26 passed; `test_elevation/mutex/restart_integration` — 19 passed / **3 failed**), прогнан `ruff check` (30 замечаний), выполнены точечные рантайм-проверки подозрительных мест.

> **Статус исправлений:** работа идёт по плану из раздела 6, по одному пункту за итерацию. Выполненные пункты помечены ✅ в разделе 6 с кратким описанием изменений. Текущий прогресс: **4/7** (все P0 закрыты; P1: окна консоли — исправлены; 13.09.2026).

---

## 1. Общая оценка

| Категория | Оценка | Комментарий |
|---|---|---|
| Функциональность | ★★★★☆ | Полный цикл: скан → dry-run → бэкап → удаление → синхронизация → откат |
| Архитектура | ★★★☆☆ | Хорошее разделение scanner/cleaner/UI, но глобальные флаги и stale-binding ломают sandbox |
| Безопасность удаления | ★★★★☆ | Бэкап-до-удаления, guard последней раскладки, остановка ctfmon — грамотно; **но sandbox-режим не защищает delete_layout** |
| Тесты | ★★★☆☆ | Хорошее покрытие scanner/cleaner (fake winreg + live sandbox), GUI-логика почти не покрыта, есть падающие тесты |
| Инфраструктура | ★★☆☆☆ | CI-конфиги есть, но **git-репозитория нет — CI никогда не запустится**; нет coverage/mypy/pre-commit |
| Документация | ★★★★☆ | README и ARCHITECTURE.md подробные; местами расходятся с кодом |

---

## 2. Критические проблемы (P0)

### 2.1. Sandbox-режим НЕ изолирует удаление (утечка в реальные ветки реестра) — ✅ ИСПРАВЛЕНО 13.09.2026
`cleaner.py:21` делает `from scanner import AFFECTED_BRANCHES` — привязка по ссылке на момент импорта. `scanner.activate_sandbox()` (scanner.py:300–309) переприсваивает `scanner.AFFECTED_BRANCHES` новой sandbox-версии, но `cleaner.AFFECTED_BRANCHES` продолжает указывать на **реальные** ветки. `cleaner.activate_sandbox()` (cleaner.py:1947) подменяет только `_INTL_SUBKEY` и `_RECURSIVE_SUBKEYS`.

Рантайм-проверка:

```text
scanner sandbox branch: ('HKCU', 'Software\KeyboardCleanerTest\Keyboard Layout\Preload', ...)
cleaner sees branch:    ('HKCU', 'Keyboard Layout\Preload', ...)   ← реальные ветки!
```

Последствия при запуске с `--sandbox` / `SANDBOX_MODE=1`:
- `delete_layout()` → `paths_to_backup` (cleaner.py:1656) и `_wipe()` (cleaner.py:1705) работают с **настоящими** ветками `HKCU\Keyboard Layout\Preload`, `Substitutes`, `CTF`, `User Profile`, `SettingSync` и (при админ-правах) `HKU\.DEFAULT`;
- `plan_layout_removal()` (cleaner.py:1853) показывает сухой план по реальным веткам;
- справка `--sandbox` («все операции реестра изолированы в HKCU\Software\KeyboardCleanerTest») и ARCHITECTURE.md не соответствуют действительности;
- попутно ветки `CTF`/`User Profile` перестают распознаваться как рекурсивные (`subkey in _RECURSIVE_SUBKEYS` уже не совпадает с реальными путями) — семантика очистки меняется непредсказуемо.

То же в GUI: `main.py:236` импортирует `AFFECTED_BRANCHES` из scanner до вызова `activate_sandbox()`, поэтому панель деталей (main.py:1141) в sandbox показывает реальные пути, которых нет в данных скана → всё «не обнаружено».

**Исправление:** обращаться к веткам через модуль (`import scanner; scanner.AFFECTED_BRANCHES`) или единый аксессор `get_affected_branches()` в config/scanner, учитывающий sandbox; `cleaner.activate_sandbox()` должен согласовывать все используемые константы. Добавить регресс-тест.


### 2.2. Падающие тесты и порядкозависимость (global state) — ✅ ИСПРАВЛЕНО 13.09.2026
`python -m pytest test_elevation.py test_mutex_restart.py test_restart_integration.py` → **3 failed**:
- `TestRestartCommandLine::test_sandbox_from_env`
- `TestSandboxModeSync::test_synced_to_scanner`
- `TestSandboxModeSync::test_synced_to_cleaner`

Причина: тесты патчат окружение и делают `import main`, ожидая повторного исполнения кода модуля, но `main` уже в `sys.modules` после первого теста — флаги остаются от предыдущего запуска. При другом порядке падает `test_non_sandbox` (`config.SANDBOX_MODE is False` → фактически `True`). Тесты мутируют глобальное состояние (`config.SANDBOX_MODE`, `os.environ`) без сброса между тестами. В `.pytest_cache/lastfailed` остались ещё ~30 исторических падений — проект периодически «краснеет».

**Исправление:** в фикстуре сбрасывать `sys.modules` (`main`, `config`, `scanner`, `cleaner`) и мутированные глобалы; в идеале — вынести разбор CLI/активацию sandbox в чистые функции без побочных эффектов на импорте.

### 2.3. NameError в applog.py (ruff F821) — ✅ ИСПРАВЛЕНО 13.09.2026
`applog.py:55` — в обработчике `except` вызывается `logger.debug(...)`, но переменной `logger` в модуле нет (локальная — `log`). Если `handler.close()` упадёт, `setup_logging()` завершится `NameError` вместо деградации в stderr-only. Редкий, но реальный путь сбоя при старте приложения.

### 2.4. Каталог не является git-репозиторием
Есть `.gitignore`, `.github/workflows/*`, LICENSE, но `git status` → `fatal: not a git repository`. Ни CI, ни история, ни откат. Для утилиты, которая правит реестр, версионирование особенно важно. Локально также копится мусор: `build/`, `dist/`, 20-МБ zip, `layout_cleaner.log`, `backups/` с реальными .reg-снимками реестра пользователя, устаревший `__pycache__/cleaner_sb.*.pyc` от уже удалённого модуля `cleaner_sb`.

---

## 3. Существенные проблемы (P1)

1. **Окна консоли поверх GUI — ✅ ИСПРАВЛЕНО 13.09.2026 (см. план, п.4).** Историческая проблема: ни один `subprocess.run(["powershell", ...])` / `["reg", ...]` не задавал `creationflags=CREATE_NO_WINDOW` — в windowed-сборке (`console=False`) каждый скан/бэкап/синхронизацию/восстановление сопровождало мигающее чёрное окно консоли.

2. **`ctypes`-антипаттерн с GetLastError.** `main.py` `_acquire_mutex()` читает ошибку через `ctypes.windll.kernel32.GetLastError()`. ctypes между вызовами может выполнять внутренние WinAPI-вызовы — ошибка может быть потеряна/чужая. Корректно: `ctypes.WinDLL("kernel32", use_last_error=True)` + `ctypes.get_last_error()`. От этого зависит единственная защита от параллельного запуска и перезапуска с UAC.

3. **`RtlGetVersion` вызывается с лишним аргументом** (main.py:195: `ntdll.RtlGetVersion(byref(ver), byref(c_int()))`) — у функции один параметр. На x64 «прокатывает», но это мусор в коде.

4. **Мёртвая тестовая инфраструктура и зависимости:**
   - все фикстуры `conftest.py` (`mock_winreg`, `mock_winreg_with_values`, `mock_subprocess_run`, `mock_is_admin_*`, `setup_winreg_mocks`) не используются ни одним тестом — реальные тесты используют собственный `FakeWinreg`;
   - `pytest-asyncio` в requirements-dev — в проекте нет ни одного `async`;
   - маркер `pytest.mark.gui` объявлен в pyproject и в докстрингах, но ни один тест им не помечен.

5. **Локали de/pt/zh неполные:** по 10 ключей отсутствуют (`chk_block_cloud_sync`, `dlg_result_cloud_sync`, `dlg_result_welcome_sync`, `progress_*`, `status_sync_block_failed`, +2) — пользователи увидят смесь языков (fallback на en работает, но это не перевод).

6. **Дублирующиеся CI-workflow с разными наборами тестов.**
   - `ci-cd.yml`: тесты = `test_scanner.py test_sandbox.py`, Python только 3.12;
   - `tests.yml`: матрица 3.10/3.11/3.12, тесты = `test_scanner.py test_elevation.py` + `test_sandbox.py -m live`. `test_elevation.py` сейчас падает → **PR-пайплайн красный**; матрица создаёт ложное впечатление поддержки 3.10/3.11 (локально проверен только 3.12);
   - job build в `ci-cd.yml` срабатывает при пуше в main — но репозитория нет, workflow в текущем виде неработоспособны;
   - `actions/upload-release-asset@v1` — заархивированный/deprecated экшен;
   - flake8/pylint в CI, но ни `setup.cfg`, ни `.pylintrc` в репозитории нет; локально используется ruff — выбор линтеров не согласован.

7. **Расхождение имён sandbox-ключей.** Приложение: `HKCU\Software\KeyboardCleanerTest` (config.py, help `--sandbox`), живые тесты: `HKCU\Software\TestLayoutCleaner` (test_sandbox.py), README упоминает `TestLayoutCleaner`. Путаница при диагностике.

8. **pyproject.toml без секции `[build-system]`.** `pip install -e ".[dev]"` в `tests.yml` полагается на дефолт setuptools; плоский модульный layout — pip соберёт и тесты в пакет. Версия только в pyproject, в коде `__version__` нет.

9. **Неприкрытые тестами риски очистки:** эвристика «активного кеша» раскладки (`_active_lang_cache`, main.py:301) может ошибочно считать живую раскладку фантомом — юнит-тестов нет. `restore_language_list` (генерация PS-скрипта из JSON, единственная защита — экранирование `'`) тоже не покрыта тестами.

---

## 4. Мелкие проблемы (P2)

- `main.py`: неиспользуемые импорты (`subprocess`, `setup_logging`, `sync_welcome_screen_settings`, `config._SANDBOX_ROOT`, `config.is_sandbox_enabled`, `cleaner._SCANNER_SANDBOX`) — ruff F401; 8×E402 (импорты не в начале — обусловлены контролем порядка, стоит пометить `noqa` с комментарием);
- `test_scanner.py`: E702 (несколько инструкций через «;»), E741 (переменная `l`), F841, неиспользуемые импорты `json`/`re`; `cleaner.py:1785` — `not _start_ctfmon() is False` → писать `is not False`;
- опечатки: «зависбать» (test_sandbox.py:19), «префаксируются» (scanner.py:245);
- `KeyboardLayoutCleaner.__init__`: `self.geometry("900x780")`/`minsize` задаются, а затем перезаписываются темой в `_build_ui` — дубль;
- README: смешение имён sandbox-ключей; `requirements.txt` (customtkinter==6.0.0) vs README «CustomTkinter 5.2+»;
- в `backups/` лежат реальные .reg-снимки реестра (персональные данные) — `.gitignore` их закрывает, но репозитория нет, а папка лежит рядом с дистрибутивом;
- нет `CHANGELOG`, нет версионирования в коде, exe не подписан (задокументировано).

---

## 5. Что сделано хорошо

- Пайплайн безопасности удаления: бэкап всех веток до изменений (объединение .reg-секций в UTF-16 LE с BOM), JSON-снимок списка языков, `BackupError` при полном провале экспорта, отчёт о неполном бэкапе, guard «последней раскладки», сухой план перед удалением.
- Потоковая модель GUI: фоновые потоки + `queue.Queue` + опрос в UI-потоке — корректный подход для Tkinter.
- Единый источник веток (`AFFECTED_BRANCHES`) и общая нормализация KLID между сканером и очистителем (`_klid_variants` ↔ `_normalize_klid_token`, decimal-HKL) — идея правильная (но см. P0-1).
- Тесты: собственный in-memory `FakeWinreg` — быстрый и безопасный; live-тесты ограничены ключом `Software\TestLayoutCleaner`; фантомный KLID `d001dead`; teardown гарантированно чистит sandbox-ключ.
- Логирование: общий логгер, NullHandler до init, файл рядом с exe (портативность).
- Документация README/ARCHITECTURE — честно описаны ограничения (HKLM не трогаем, CJK IME, требуется перелогин).

---

## 6. Приоритетный план действий

1. ✅ **(P0) ИСПРАВЛЕНО 13.09.2026 — Починить sandbox: единый аксессор веток; регресс-тест «activate_sandbox → cleaner видит только sandbox-ветки».**
   Что сделано:
   - `scanner.py`: добавлен публичный аксессор `get_affected_branches()` (учитывает `scanner.SANDBOX_MODE`, `config.SANDBOX_MODE` и env `SANDBOX_MODE`); `_get_sandbox_affected_branches()` теперь идемпотентна (повторная активация не двойнопрефиксует); добавлен `deactivate_sandbox()` с восстановлением оригиналов веток (оригиналы сохраняются в `_ORIGINAL_HKLM_BRANCHES` / `_ORIGINAL_RECURSIVE_SCAN`);
   - `cleaner.py`: убрана утечка — больше нет `from scanner import AFFECTED_BRANCHES` (stale-binding); все обращения к веткам идут через резолверы `_affected_branches()`, `_intl_subkey()`, `_ctf_subkey()`, `_recursive_subkeys()`; константы `_INTL_SUBKEY`/`_CTF_SUBKEY`/`_RECURSIVE_SUBKEYS` больше никогда не мутируются; `cleaner.activate_sandbox()` теперь только ставит флаг в config; добавлены `deactivate_sandbox()`, хелперы `_paths_to_backup()`, `_report_field_for()` (маппинг sandbox-веток на поля отчёта, добавлено поле `other_deleted`); guard «последней раскладки» (`_current_preload_klids`) тоже стал sandbox-осведомлённым;
   - `main.py`: панель деталей использует `get_affected_branches()` — в sandbox показывает те же пути, что возвращает сканер;
   - тесты: в `test_scanner.py` добавлен класс `TestSandboxIsolation` (9 регрессионных тестов: аксессор, идемпотентность, отсутствие stale-binding, резолверы, маппинг отчёта, пути бэкапа sandbox/real);
   - проверка: `test_scanner` — 64 passed (было 55), `test_sandbox` — 26 passed, рантайм-проверка подтверждает `cleaner._affected_branches()` == sandbox-ветки; попутно устранены 2 замечания ruff F401 в cleaner.py.
   Оставшиеся 3 падающих теста `test_elevation.py` — отдельный пункт 2 плана (не связаны с этим фиксом, падали и до него).
2. ✅ **(P0) ИСПРАВЛЕНО 13.09.2026 — Зафиксировать падающие тесты: сброс глобального состояния в фикстуре.**
   Что сделано (test_elevation.py):
   - `_patch_main()` теперь даёт детерминизм: настоящий `pop` утечек `SANDBOX_MODE` из окружения (раньше `monkeypatch.delenv` «восстанавливал» утечки предыдущих тестов), удаление `main`/`config` из `sys.modules` через `monkeypatch.delitem` (`import main` исполняет код модуля заново, после теста monkeypatch возвращает исходные объекты), явное детерминированное значение env («1»/«0»);
   - добавлена autouse-фикстура `_reset_sandbox_state` — после каждого теста файла сбрасывает флаги на канонических экземплярах config/scanner (на этот момент monkeypatch уже восстановил sys.modules);
   - `test_synced_to_cleaner` теперь импортирует `main` — раньше утверждение проверялось без активации синхронизации и имело смысл только в определённом порядке;
   - устаревший кеш `.pytest_cache` (lastfailed ~30 исторических падений) удалён.
   Проверка: ранее падавшие 3 теста — passed при запуске первыми; файл целиком — 18 passed; файл в нестандартном порядке классов — 10 passed; весь набор — 112 passed.
   ⚠️ **Попутно обнаружено (вне объёма пункта 2):** тесты с реальными задержками флакают под нагрузкой (в полных прогонах падают разные, затем проходят): `test_mutex_acquire_with_polling` (поллинг 0.1 с), `test_two_process_restart_scenario` (два реальных процесса), live-тесты ctfmon/PowerShell (`test_delete_layout_end_to_end_noop_on_real_system` — утверждает `power_sync == True`, т.е. падает при таймауте PowerShell). Требуют укрепления (увеличенные таймауты/ретраи/пометка slow) — критично для CI, учесть в пункте 5.
3. ✅ **(P0) ИСПРАВЛЕНО 13.09.2026 — `applog.py:55` → `log.debug(...)`.**
   - `applog.py`: в обработчике `except` при сбое `handler.close()` вызов несуществующего `logger.debug` заменён на `log.debug` (раньше — NameError, теперь штатная деградация в stderr-only); добавлен поясняющий комментарий;
   - добавлен новый тест-файл `test_applog.py` (3 теста): сбой `close()` у хендлера не роняет `setup_logging()`, идемпотентность (хендлеры заменяются, а не накапливаются), запись в указанный каталог (`log_dir`);
   - проверка: `ruff check applog.py` — чисто (F821 устранён), `test_applog` — 3 passed, полный набор — **115 passed** (было 112).
   Примечание: новый файл пока не включён в CI-workflow (они запускают фиксированные файлы) — будет добавлен при консолидации CI в пункте 5.
4. ✅ **(P1) ИСПРАВЛЕНО 13.09.2026 — `CREATE_NO_WINDOW` для всех `subprocess.run` (общий хелпер `run_hidden()`).**
   - создан новый модуль **`winproc.py`**: `run_hidden()` — обёртка над `subprocess.run`, на Windows объединяет (OR) `creationflags` с `CREATE_NO_WINDOW` (0x08000000); на не-Windows флаг не передаётся. Важно: вызов идёт по атрибуту модуля (`subprocess.run(...)`), поэтому существующие моки тестов (`monkeypatch.setattr(scanner.subprocess, "run", ...)`) продолжают перехват;
   - заменены **все 11 вызовов**: scanner.py — 1 (Get-WinUserLanguageList); cleaner.py — 10 (reg export, cleanup-скрипт, sync языка, stop/start ctfmon, бэкап списка языков, restore списка языков, sync экрана приветствия, reg import c UAC и без);
   - регрессионные тесты `test_scanner.py::TestWinprocHelper` (5 шт.): флаг добавляется; существующие флаги объединяются; сквозной запуск реального процесса; **статическая регрессия** — в scanner.py/cleaner.py запрещён прямой `subprocess.run(`; мок через `scanner.subprocess.run` перехватывает вызовы из `run_hidden`;
   - проверка: `TestWinprocHelper` — 5 passed; рантайм-проверка — `creationflags=0x0800000`; полный набор — **120 passed** (было 115); ruff — новых замечаний нет (winproc.py чист).
   Примечание: модуль `winproc` подхватывается PyInstaller автоматически (обычный импорт); в `--noconsole`-сборке окна консоли больше не появляются.
5. **(P1)** Инициализировать git, почистить артефакты (build/dist/zip/log/pyc), свести CI к одному workflow, заменить deprecated release-экшен, добавить coverage + ruff в CI.
6. **(P1)** Согласовать имена sandbox-ключей, дописать de/pt/zh локали, убрать pytest-asyncio и мёртвые фикстуры conftest.
7. **(P2)** Добавить `[build-system]`, `__version__`, `noqa`-комментарии, тесты на `_active_lang_cache` и `restore_language_list`.

**Итог:** проект зрелый по домену (работа с реестром/PowerShell продумана), но «опасен по обещаниям»: sandbox-режим, который должен страховать от порчи системы, не страхует главный риск — удаление. Его и тестовую порядкозависимость следует чинить в первую очередь.
