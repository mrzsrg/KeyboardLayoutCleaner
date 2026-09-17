# Contributing — Keyboard Layout Cleaner

Спасибо за интерес к проекту! Ниже — как настроить окружение, гонять тесты и
не наломать дров.

## Окружение

- Python **3.10+** (Windows). Только Windows — утилита работает с реестром.
- Установка зависимостей:

```bat
python -m pip install -r requirements-dev.txt
```

- Рекомендуется venv (`.venv/` уже в `.gitignore`).

## Запуск

```bat
python main.py                 # GUI
python scanner.py              # скан раскладок в консоли
python scanner.py --json       # скан в JSON
python cleaner.py <KLID>       # удалить раскладку по KLID (например 00000419)
python cleaner.py <KLID> --plan   # dry-run, без изменений
python cleaner.py <KLID> --sandbox --json
```

## Логирование в dev-режиме

В запуске из исходников журнал пишется в `layout_cleaner.log` **рядом с
исходным кодом**. Это нормально для портативной утилиты, но:

- **НЕ коммитьте этот файл** — он уже в `.gitignore`, но всё равно не `git add .`
  бездумно.
- Для отладки можно удалять его локально: `del layout_cleaner.log`.

В собранном `.exe` лог создаётся рядом с exe (например, на флешке).

## Тесты

```bat
python -m pytest test_scanner.py test_elevation.py -v   # unit-тесты (быстро)
python -m pytest test_sandbox.py -m live -v             # live-тесты (sandbox-ключ реестра)
python -m pytest -q                                     # всё
```

- **Unit-тесты** (без маркера `live`) не трогают реальные ветки реестра.
- **Live-тесты** (`-m live`) работают только с тестовым ключом
  `HKCU\Software\KeyboardCleanerTest` — реальные раскладки не затрагиваются.
  Реальное удаление проверяйте в VM (Windows Sandbox / Hyper-V).

## Линт и форматирование

Проект использует **ruff** (единственный согласованный линтер):

```bat
python -m ruff check .          # линт
python -m ruff format --check . # проверка форматирования
```

`max-complexity = 15` (см. `ruff.toml`) — не обходите его, а декомпозируйте
сложные функции на мелкие хелперы. Новые сложные функции не приветствуются.

## Статическая типизация (mypy)

Мypy проверяется на двух уровнях:

| Уровень | Где | Зачем |
|---------|-----|-------|
| **pre-commit** | локально, на коммите | мгновенная обратная связь для автора |
| **CI** | GitHub Actions, job `mypy` | страховка — не пропускает ошибки в мейнстрим |

Запуск вручную:

```bat
python -m mypy main.py mutex.py scanner.py cleaner.py config.py winproc.py applog.py i18n.py gui_widgets.py ui_theme.py
```

Если mypy локально зелёный, а в CI красится — проверяй `.mypy_cache` (очисти `Remove-Item -Recurse -Force .mypy_cache`) и версию Python (CI гоняет на 3.12).

## Покрытие тестами (coverage)

Покрытие контролируется через `fail_under = 50` в `[tool.coverage.report]` файла `pyproject.toml`. Текущий порог — **50%** для основных модулей: `main`, `scanner`, `cleaner`, `config`, `winproc`, `applog`, `i18n`, `gui_widgets`, `ui_theme`, `mutex`.

```bat
python -m pytest -m "not live" --cov --cov-report=term-missing
```

Если покрытие падает ниже порога — CI (job `test`) не пройдёт. Напишите тесты на ключевые сценарии перед отправкой PR.

## Сборка .exe

```bat
build_exe.bat
```

или вручную:

```bat
python -m PyInstaller keyboard_cleaner.spec --noconfirm --clean
```

Результат — папка `dist\KeyboardLayoutCleaner\` (onedir). Спека включает
`locales/`, `scripts/*.ps1` и `assets/` — при добавлении новых runtime-данных
не забудьте про `datas` в `keyboard_cleaner.spec`.

> `pip install .` **не** является поддерживаемым способом установки (см.
> комментарий в `pyproject.toml`). Поддерживаются запуск из исходников и .exe.

## Архитектура

См. `ARCHITECTURE.md`. Ключевые модули: `scanner.py` (поиск), `cleaner.py`
(бэкап/удаление), `main.py` (GUI), `config.py` (sandbox-режим), `i18n.py` +
`locales/` (локализация).
