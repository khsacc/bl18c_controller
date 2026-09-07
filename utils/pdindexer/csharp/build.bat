@echo off
rem utils/pdindexer/csharp/build.bat
rem Builds PdiSender.exe (self-contained, single-file, win-x64) into
rem ../bin/. Paths are all %~dp0-relative so this can be invoked from any
rem working directory (e.g. by utils/pdindexer/service.py or a CI runner),
rem matching apps/Rad_icon_2022/dll/build.bat's convention.

dotnet publish "%~dp0PdiSender.csproj" -c Release -o "%~dp0..\bin"
if errorlevel 1 (
    echo.
    echo [FAILED] PdiSender build.
    exit /b 1
)

echo.
echo [OK] Built "%~dp0..\bin\PdiSender.exe"
