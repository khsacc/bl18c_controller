@echo off
:: Build radicon_dll.dll (Release x64).
:: Tries VS 2022 Insiders first, then any VS 2022, then VS 2019.

set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" (
    echo ERROR: vswhere.exe not found. Is Visual Studio installed?
    exit /b 1
)

:: VS 2022 Insiders (version 18.x)
for /f "tokens=*" %%i in ('"%VSWHERE%" -version "[18.0,19.0)" -property installationPath 2^>nul') do set "VS_PATH=%%i"

:: VS 2022 release (version 17.x)
if "%VS_PATH%"=="" (
    for /f "tokens=*" %%i in ('"%VSWHERE%" -version "[17.0,18.0)" -property installationPath 2^>nul') do set "VS_PATH=%%i"
)

:: VS 2019 (version 16.x)
if "%VS_PATH%"=="" (
    for /f "tokens=*" %%i in ('"%VSWHERE%" -version "[16.0,17.0)" -property installationPath 2^>nul') do set "VS_PATH=%%i"
)

if "%VS_PATH%"=="" (
    echo ERROR: Visual Studio 2019 or later not found.
    exit /b 1
)

echo Using Visual Studio at: %VS_PATH%
call "%VS_PATH%\VC\Auxiliary\Build\vcvars64.bat"
if errorlevel 1 (
    echo ERROR: vcvars64.bat failed.
    exit /b 1
)

set "PROJ=%~dp0RadiconDll_2019.vcxproj"
msbuild "%PROJ%" /p:Configuration=Release /p:Platform=x64 /m /nologo /v:minimal
if errorlevel 1 (
    echo.
    echo BUILD FAILED.
    exit /b 1
)

echo.
echo Build succeeded.  Output: %~dp0Release\radicon_dll.dll
