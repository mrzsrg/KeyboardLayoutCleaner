@echo off
rem Build portable KeyboardLayoutCleaner.exe (onedir, no console).
rem Result: dist\KeyboardLayoutCleaner\KeyboardLayoutCleaner.exe
echo [1/3] Installing dependencies...
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :err
echo [2/3] Building (PyInstaller)...
python -m PyInstaller keyboard_cleaner.spec --noconfirm --clean
if errorlevel 1 goto :err
echo [3/3] Done.
echo Copy the WHOLE folder to a flash drive:
echo   dist\KeyboardLayoutCleaner\
echo Run: KeyboardLayoutCleaner.exe
goto :eof
:err
echo BUILD FAILED
exit /b 1