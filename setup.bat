@echo off
rem Creates the Python environment and builds the FilmScan.exe launcher. Run once.
cd /d "%~dp0"
where python >nul 2>nul || (echo Python 3.10 or newer is required: https://www.python.org/downloads/ & pause & exit /b 1)
python -m venv .venv || (echo Could not create the virtual environment. & pause & exit /b 1)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo Installing packages failed. & pause & exit /b 1)
set CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe
if exist "%CSC%" (
  "%CSC%" /nologo /target:winexe /optimize+ /win32icon:launcher\filmscan.ico /r:System.Windows.Forms.dll /out:FilmScan.exe launcher\FilmScan.cs
) else (
  echo .NET Framework compiler not found. Start the app with:  .venv\Scripts\pythonw.exe server.py
)
echo.
echo Done. Double-click FilmScan.exe to start.
pause
