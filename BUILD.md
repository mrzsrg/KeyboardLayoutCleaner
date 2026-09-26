# Сборка KeyboardLayoutCleaner.exe

Приложение распространяется как портативная папка PyInstaller (onedir), а не
как pip-пакет. Нужны Windows 10+ x64 и Python 3.10–3.12.

## Окружение и проверки

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy main.py mutex.py scanner.py cleaner.py config.py winproc.py applog.py i18n.py gui_widgets.py ui_theme.py layout_ids.py winapi.py backup.py mutate.py langlist.py settings.py
.\.venv\Scripts\python.exe -m pytest -m "not live" --cov
```

PyInstaller устанавливается отдельно от dev-зависимостей. Live-тесты запускайте
в тестовой Windows-среде: они создают и удаляют sandbox-ключ реестра.
Изолированные CJK-тесты запускают настоящий PowerShell, но подменяют языковые
команды и не изменяют список языков пользователя.

## Версия и сборка

1. Синхронно обновите `[project] version` в `pyproject.toml` и fallback
   `__version__` в `config.py`.
2. Опишите изменения в `CHANGELOG.md`.
3. Закройте запущенный EXE перед пересборкой.
4. Выполните:

```powershell
.\.venv\Scripts\python.exe -m PyInstaller keyboard_cleaner.spec --noconfirm --clean
```

Либо используйте `build_exe.bat`. Спека включает локали, PowerShell-скрипты,
иконку и данные CustomTkinter. Версия ресурса Windows читается из pyproject;
оконная сборка запускается с правами вызывающего пользователя, без UAC при старте.

## Результат

```text
dist/KeyboardLayoutCleaner/
├── KeyboardLayoutCleaner.exe
└── _internal/
    ├── locales/
    ├── scripts/
    ├── assets/
    ├── customtkinter/
    ├── python312.dll
    └── ... runtime-файлы Tcl/Tk и Python
```

Состав runtime зависит от версии Python/PyInstaller. Распространяйте **всю
папку**, не один EXE. Перед публикацией проверьте версию в свойствах файла,
запуск окна, наличие данных и соответствие скриптов исходникам.

## Архив и контрольная сумма

```powershell
Compress-Archive -Path dist\KeyboardLayoutCleaner -DestinationPath KeyboardLayoutCleaner-portable.zip
Get-FileHash KeyboardLayoutCleaner-portable.zip -Algorithm SHA256
```

> **Хеши публикуются автоматически (FIX-33).** При публикации релиза workflow
> `build-release` считает SHA256 архива и EXE внутри него и подставляет их в
> README и в описание релиза — руками ничего править не нужно. Скрипт
> `scripts/release_hashes.py` можно запустить и вручную:
>
> ```powershell
> python scripts/release_hashes.py compute --archive KeyboardLayoutCleaner-portable.zip --tag v1.1.0
> python scripts/release_hashes.py readme  --archive KeyboardLayoutCleaner-portable.zip --tag v1.1.0
> ```
>
> Блок в README находится между маркерами `<!-- klc:hashes:begin -->` и
> `<!-- klc:hashes:end -->`: всё внутри них перезаписывается, всё снаружи —
> нет. **Маркеры удалять нельзя** (без них скрипт допишет таблицу в конец
> файла; за это отвечает тест `test_readme_still_has_markers`). Рядом с
> архивом workflow кладёт `SHA256SUMS.txt` — его понимает `sha256sum -c`.

В релизе укажите коммит исходников, результаты проверок и SHA256 архива.
Не включайте пользовательские логи, настройки и бэкапы реестра.
Неподписанный EXE может вызывать предупреждение SmartScreen; цифровая подпись
опциональна (см. `sign_exe.py` и workflow CI), но не гарантирует отсутствие
предупреждений или антивирусных обнаружений. FileVersion — метаданные, а не подпись.
При блокировке сборки не добавляйте исключения и не распространяйте её как
проверенную: сохраните сведения о детекте и направьте образец на анализ
производителю антивируса. Порядок действий и сведения для отчёта приведены в
[README — Антивирус / SmartScreen](README.md#антивирус--smartscreen).
