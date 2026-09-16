# -*- mode: python ; coding: utf-8 -*-
"""
Спека сборки портативного KeyboardLayoutCleaner.exe (PyInstaller).

Тип: onedir — быстрый старт с флешки (без распаковки во временную папку,
как у onefile). Оконный режим (без консоли). Данные customtkinter
(темы, шрифты, иконки) собираются в комплект автоматически.

Права: asInvoker — UAC при старте не требуется; повышение прав доступно
кнопкой «🔑 Перезапустить как Администратор» внутри приложения
(нужно только для ветки HKU\.DEFAULT).

Сборка:  python -m PyInstaller keyboard_cleaner.spec --noconfirm --clean
Результат: dist\\KeyboardLayoutCleaner\\KeyboardLayoutCleaner.exe
"""
from pathlib import Path

import re as _re

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

datas = collect_data_files("customtkinter")
# Файлы локализации locales/<код>.json — рядом с кодом в _internal
datas += [("locales", "locales")]
# PowerShell-скрипты scripts/*.ps1 — нужны в рантайме (cleaner._PS_DIR)
datas += [("scripts", "scripts")]
# Иконка приложения
datas += [("assets/keyboard_cleaner.ico", "assets")]

# Version-ресурс exe: версия берётся из pyproject.toml (единый источник
# истины). Полноценные метаданные («Свойства» файла) снижают число ложных
# срабатываний антивирусов и помогают корректной атрибуции файла.
# В spec-файле нет __file__ — PyInstaller определяет SPECPATH (каталог spec).
_version = _re.search(
    r'^version\s*=\s*"([^"]+)"',
    Path(SPECPATH).joinpath("pyproject.toml").read_text(encoding="utf-8"),
    _re.MULTILINE,
).group(1)

version_resource = VSVersionInfo(
    ffi=FixedFileInfo(
        # (1,2,3,4) → «1.2.3.4»; версия = pyproject + «.0»
        filevers=tuple(int(x) for x in _version.split(".")) + (0,),
        prodvers=tuple(int(x) for x in _version.split(".")) + (0,),
        mask=0x3F,
        flags=0x0,
        OS=0x40004,  # VOS_NT_WINDOWS32
        fileType=0x1,  # VFT_APP
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",
                    [
                        StringStruct(
                            "CompanyName", "Keyboard Layout Cleaner Contributors"
                        ),
                        StringStruct(
                            "FileDescription",
                            "Keyboard Layout Cleaner — поиск и безопасное "
                            "удаление фантомных раскладок клавиатуры",
                        ),
                        StringStruct("FileVersion", f"{_version}.0"),
                        StringStruct("InternalName", "KeyboardLayoutCleaner"),
                        StringStruct("LegalCopyright", "MIT License"),
                        StringStruct("OriginalFilename", "KeyboardLayoutCleaner.exe"),
                        StringStruct("ProductName", "Keyboard Layout Cleaner"),
                        StringStruct("ProductVersion", f"{_version}.0"),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=["config"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Транзитивный «мусор» из site-packages: приложению не нужен
        "numpy",
        "pygame",
        "psutil",
        "yaml",
        "charset_normalizer",
        "pyreadline3",
        "pytest",
        "setuptools",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="KeyboardLayoutCleaner",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=version_resource,
    icon="assets/keyboard_cleaner.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="KeyboardLayoutCleaner",
)