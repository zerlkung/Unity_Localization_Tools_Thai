@echo off
setlocal

set "VENV_DIR=venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

if not exist "%VENV_PY%" (
  echo [build] Creating virtual environment: %VENV_DIR%
  py -3.12 -m venv "%VENV_DIR%" 2>nul
  if errorlevel 1 (
    python -m venv "%VENV_DIR%" 2>nul
  )
)

if not exist "%VENV_PY%" (
  echo Failed to create or find venv python at "%VENV_PY%".
  echo Ensure Python 3.12 or python is installed and available.
  exit /b 1
)

echo [build] Using venv python: %VENV_PY%

"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install pyinstaller TypeTreeGeneratorAPI Pillow fmod_toolkit archspec numpy scipy fonttools
"%VENV_PY%" -m pip install --upgrade git+https://github.com/snowyegret23/UnityPy.git
"%VENV_PY%" -c "import UnityPy,sys; print(sys.version); print(UnityPy.__file__)"

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist unity_font_replacer.spec del unity_font_replacer.spec
if exist export_fonts.spec del export_fonts.spec
if exist make_sdf.spec del make_sdf.spec

"%VENV_PY%" -m PyInstaller --onefile --name unity_font_replacer ^
  --clean ^
  --noconfirm ^
  --collect-all UnityPy ^
  --collect-all TypeTreeGeneratorAPI ^
  --collect-all fmod_toolkit ^
  --collect-all archspec ^
  unity_font_replacer.py

"%VENV_PY%" -m PyInstaller --onefile --name export_fonts ^
  --clean ^
  --noconfirm ^
  --collect-all UnityPy ^
  --collect-all TypeTreeGeneratorAPI ^
  --collect-all fmod_toolkit ^
  --collect-all archspec ^
  export_fonts.py

"%VENV_PY%" -m PyInstaller --onefile --name make_sdf ^
  --clean ^
  --noconfirm ^
  --collect-all numpy ^
  --collect-all scipy ^
  --collect-all fontTools ^
  make_sdf.py

if exist release rmdir /s /q release
mkdir release
copy dist\unity_font_replacer.exe release\ >nul
copy dist\export_fonts.exe release\ >nul
copy dist\make_sdf.exe release\ >nul
xcopy KR_ASSETS release\KR_ASSETS\ /E /I >nul
xcopy Il2CppDumper release\Il2CppDumper\ /E /I >nul
copy README.md release\ >nul
if exist CharList_3911.txt copy CharList_3911.txt release\ >nul

echo Build complete. Output in release\ and dist\
pause
endlocal
