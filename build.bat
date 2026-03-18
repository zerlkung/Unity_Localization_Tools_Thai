@echo off
setlocal

set "VENV_DIR=venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "LOCAL_UNITYPY=%~dp0..\UnityPy"

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
if exist "%LOCAL_UNITYPY%\pyproject.toml" (
  echo [build] Installing local custom UnityPy: %LOCAL_UNITYPY%
  "%VENV_PY%" -m pip install --upgrade --force-reinstall "%LOCAL_UNITYPY%"
) else if exist "%LOCAL_UNITYPY%\setup.py" (
  echo [build] Installing local custom UnityPy: %LOCAL_UNITYPY%
  "%VENV_PY%" -m pip install --upgrade --force-reinstall "%LOCAL_UNITYPY%"
) else (
  echo [build] Local custom UnityPy not found. Falling back to remote repository.
  "%VENV_PY%" -m pip install --upgrade git+https://github.com/snowyegret23/UnityPy.git
)
"%VENV_PY%" -c "import UnityPy,sys; from UnityPy.files.BundleFile import BundleFile; from UnityPy.files.SerializedFile import SerializedFile; print(sys.version); print(UnityPy.__file__); assert callable(getattr(BundleFile,'save_to',None)) and callable(getattr(SerializedFile,'save_to',None)), 'Custom UnityPy save_to() APIs are missing'"
if errorlevel 1 (
  echo [build] ERROR: Installed UnityPy does not expose BundleFile.save_to / SerializedFile.save_to
  exit /b 1
)

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist unity_font_replacer_ko.spec del unity_font_replacer_ko.spec
if exist export_fonts_ko.spec del export_fonts_ko.spec
if exist unity_font_replacer_en.spec del unity_font_replacer_en.spec
if exist export_fonts_en.spec del export_fonts_en.spec
if exist make_sdf.spec del make_sdf.spec

"%VENV_PY%" -m PyInstaller --onefile --name unity_font_replacer_ko ^
  --clean ^
  --noconfirm ^
  --collect-all UnityPy ^
  --collect-all TypeTreeGeneratorAPI ^
  --collect-all fmod_toolkit ^
  --collect-all archspec ^
  unity_font_replacer_ko.py

"%VENV_PY%" -m PyInstaller --onefile --name export_fonts_ko ^
  --clean ^
  --noconfirm ^
  --collect-all UnityPy ^
  --collect-all TypeTreeGeneratorAPI ^
  --collect-all fmod_toolkit ^
  --collect-all archspec ^
  export_fonts_ko.py

"%VENV_PY%" -m PyInstaller --onefile --name unity_font_replacer_en ^
  --clean ^
  --noconfirm ^
  --collect-all UnityPy ^
  --collect-all TypeTreeGeneratorAPI ^
  --collect-all fmod_toolkit ^
  --collect-all archspec ^
  unity_font_replacer_en.py

"%VENV_PY%" -m PyInstaller --onefile --name export_fonts_en ^
  --clean ^
  --noconfirm ^
  --collect-all UnityPy ^
  --collect-all TypeTreeGeneratorAPI ^
  --collect-all fmod_toolkit ^
  --collect-all archspec ^
  export_fonts_en.py

"%VENV_PY%" -m PyInstaller --onefile --name make_sdf ^
  --clean ^
  --noconfirm ^
  --collect-all numpy ^
  --collect-all scipy ^
  --collect-all fontTools ^
  make_sdf.py

if exist release rmdir /s /q release
if exist release_en rmdir /s /q release_en
if exist release_make_sdf rmdir /s /q release_make_sdf
mkdir release
mkdir release_en
mkdir release_make_sdf
copy dist\unity_font_replacer_ko.exe release\ >nul
copy dist\export_fonts_ko.exe release\ >nul
xcopy KR_ASSETS release\KR_ASSETS\ /E /I >nul
xcopy Il2CppDumper release\Il2CppDumper\ /E /I >nul
copy README.md release\ >nul
if exist CharList_3911.txt copy CharList_3911.txt release\ >nul
copy dist\unity_font_replacer_en.exe release_en\ >nul
copy dist\export_fonts_en.exe release_en\ >nul
xcopy KR_ASSETS release_en\KR_ASSETS\ /E /I >nul
xcopy Il2CppDumper release_en\Il2CppDumper\ /E /I >nul
copy README_EN.md release_en\ >nul
if exist CharList_3911.txt copy CharList_3911.txt release_en\ >nul
copy dist\make_sdf.exe release_make_sdf\ >nul
copy README.md release_make_sdf\ >nul
copy README_EN.md release_make_sdf\ >nul
if exist CharList_3911.txt copy CharList_3911.txt release_make_sdf\ >nul

echo Build complete. Output in release\, release_en\, release_make_sdf\, and dist\
pause
endlocal
