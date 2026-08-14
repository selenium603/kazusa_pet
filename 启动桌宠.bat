@echo off
cd /d "%~dp0"
where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw -3 "%~dp0kazusa_pet.py"
    exit /b 0
)
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0kazusa_pet.py"
    exit /b 0
)
echo 未找到 Python。请安装 Python 3，并在安装时勾选 Add Python to PATH。
pause
