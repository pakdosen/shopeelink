@echo off
REM Klik dua kali file ini untuk membuka Shopee Link Converter di Windows.
REM Saat pertama kali dijalankan, script ini akan membuat virtualenv dan
REM menginstal dependency (sekitar 2-3 menit untuk versi pertama; sekitar
REM 30-60 detik untuk upgrade ke versi baru karena perlu unduh playwright).
REM Setelah itu peluncuran berikutnya hanya butuh 1-2 detik.

setlocal
cd /d "%~dp0"

set "VENV_DIR=.venv"
set "PYEXE=%VENV_DIR%\Scripts\python.exe"
set "PYWEXE=%VENV_DIR%\Scripts\pythonw.exe"

if not exist "%PYEXE%" (
    echo [shopeelink] Menyiapkan environment untuk pertama kali...
    where py >nul 2>nul
    if %ERRORLEVEL%==0 (
        py -3 -m venv "%VENV_DIR%" || goto :fail
    ) else (
        where python >nul 2>nul || goto :nopython
        python -m venv "%VENV_DIR%" || goto :fail
    )
    "%PYEXE%" -m pip install --upgrade pip
    "%PYEXE%" -m pip install -r requirements.txt || goto :fail
)

REM Make sure new dependencies (e.g. playwright in v1.3.0) are installed even
REM if the venv was created by an older release. We intentionally do NOT pass
REM --quiet so the user sees download progress for big wheels like playwright.
echo [shopeelink] Memeriksa dependency...
"%PYEXE%" -m pip install -r requirements.txt --disable-pip-version-check || goto :fail

REM Quick import smoke-test BEFORE we hand off to pythonw.exe (which has no
REM console and would swallow any import error silently).
echo [shopeelink] Memvalidasi instalasi...
"%PYEXE%" -c "import customtkinter, playwright" 2>&1
if errorlevel 1 (
    echo.
    echo [shopeelink] Gagal mengimpor dependency yang baru saja di-install.
    echo Coba hapus folder ".venv" lalu jalankan ulang run.bat.
    echo.
    pause
    exit /b 1
)

echo [shopeelink] Membuka aplikasi...
start "" "%PYWEXE%" shopeelink_gui.py
exit /b 0

:nopython
echo Python tidak ditemukan. Install Python 3.10+ dari https://www.python.org/downloads/windows/
echo Pastikan saat instalasi mencentang "Add Python to PATH".
pause
exit /b 1

:fail
echo.
echo Gagal menyiapkan environment. Coba:
echo   1. Hapus folder ".venv" lalu jalankan ulang run.bat (clean rebuild).
echo   2. Atau jalankan secara manual untuk melihat error lengkap:
echo        .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
pause
exit /b 1
