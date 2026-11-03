@echo off
setlocal
title PDF ke EPUB
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo Persiapan pertama kali: membuat lingkungan Python...
    py -3.13 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul || python -m venv .venv
    if not exist "%PY%" (
        echo.
        echo [GAGAL] Python tidak ditemukan. Pasang Python 3 dari https://www.python.org lalu coba lagi.
        pause
        exit /b 1
    )
    echo Memasang library yang dibutuhkan...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [GAGAL] Library tidak bisa dipasang. Periksa koneksi internet lalu coba lagi.
        pause
        exit /b 1
    )
)

echo ==============================================
echo   PDF ke EPUB
echo   Browser akan terbuka otomatis.
echo   Tutup jendela ini untuk mematikan server.
echo ==============================================
"%PY%" app.py --open
echo.
pause
