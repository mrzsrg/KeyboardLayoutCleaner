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
from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("customtkinter")
# Файлы локализации locales/<код>.json — рядом с кодом в _internal
datas += [("locales", "locales")]
# PowerShell-скрипты scripts/*.ps1 — нужны в рантайме (cleaner._PS_DIR)
datas += [("scripts", "scripts")]
# Иконка приложения
datas += [("assets/keyboard_cleaner.ico", "assets")]

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