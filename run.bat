@echo off
REM Klik dua kali file ini untuk membuka Shopee Link Converter di Windows.
REM Saat pertama kali dijalankan, script ini akan membuat virtualenv dan
REM menginstal customtkinter (sekitar 1-2 menit). Setelah itu peluncuran
REM berikutnya hanya butuh 1-2 detik.

setlocal
cd /d "%~dp0"

set "VENV_DIR=.venv"
set "PYEXE=%VENV_DIR%\Scripts\python.exe"

if not exist "%PYEXE%" (
    echo [shopeelink] Menyiapkan environment untuk pertama kali...
    where py >nul 2>nul
    if %ERRORLEVEL%==0 (
        py -3 -m venv "%VENV_DIR%" || goto :fail
    ) else (
        where python >nul 2>nul || goto :nopython
        python -m venv "%VENV_DIR%" || goto :fail
    )
    "%PYEXE%" -m pip install --upgrade pip >nul
    "%PYEXE%" -m pip install -r requirements.txt || goto :fail
)

REM Make sure new dependencies (e.g. playwright in v1.3.0) are installed even
REM if the venv was created by an older release.
"%PYEXE%" -m pip install -r requirements.txt --quiet --disable-pip-version-check || goto :fail

start "" "%VENV_DIR%\Scripts\pythonw.exe" shopeelink_gui.py
exit /b 0

:nopython
echo Python tidak ditemukan. Install Python 3.10+ dari https://www.python.org/downloads/windows/
echo Pastikan saat instalasi mencentang "Add Python to PATH".
pause
exit /b 1

:fail
echo.
echo Gagal menyiapkan environment. Coba jalankan run.bat dari Command Prompt
echo untuk melihat pesan error lengkap.
pause
exit /b 1
