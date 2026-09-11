@echo off
setlocal

echo ===================================================
echo   Compiling DesktopAvatar (Windows Child Session Host)
echo ===================================================

cd /d "%~dp0"

set "CSC="
set "ROSLYN_DEFAULT=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\Roslyn\csc.exe"
if exist "%ROSLYN_DEFAULT%" set "CSC=%ROSLYN_DEFAULT%"

if not defined CSC if exist "C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe" set "CSC=C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"

if not defined CSC (
    echo [ERROR] No suitable C# compiler found.
    exit /b 1
)

echo Using C# Compiler: "%CSC%"

set "OUTPUT_DIR=%~dp0bin"
if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"
set "OUT_EXE=%OUTPUT_DIR%\DesktopAvatar.exe"
set "DIST_EXE=%~dp0DesktopAvatar.exe"

"%CSC%" /nologo /target:winexe /out:"%OUT_EXE%" /optimize+ /langversion:latest ^
  /r:System.dll ^
  /r:System.Windows.Forms.dll ^
  /r:System.Drawing.dll ^
  /r:System.Core.dll ^
  /r:System.Web.Extensions.dll ^
  /r:Microsoft.CSharp.dll ^
  src\Compatibility.cs ^
  src\ChildSessionNativeMethods.cs ^
  src\ChildSessionProcessLauncher.cs ^
  src\RdpActiveXHost.cs ^
  src\AvatarForm.cs ^
  src\Program.cs

if errorlevel 1 (
    echo [ERROR] Compilation failed.
    exit /b 1
)

copy /y "%OUT_EXE%" "%DIST_EXE%" >nul
echo.
echo [SUCCESS] DesktopAvatar compiled successfully!
echo Output: %DIST_EXE%
exit /b 0
