@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 交易新聞監測：更新
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 update.py
  goto :done
)
python --version >nul 2>nul
if %errorlevel%==0 (
  python update.py
  goto :done
)
echo.
echo 找不到 Python。請先到 https://www.python.org/downloads/ 下載安裝，
echo 安裝畫面最下方務必勾選「Add python.exe to PATH」，裝完再雙擊這個檔案。
:done
echo.
pause
