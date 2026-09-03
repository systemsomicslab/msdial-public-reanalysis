@echo off
setlocal
cd /d "%~dp0"

set "CLAUDE_LAUNCHER="

where claude.cmd >nul 2>nul
if not errorlevel 1 set "CLAUDE_LAUNCHER=claude.cmd"

if not defined CLAUDE_LAUNCHER if exist "%APPDATA%\npm\claude.cmd" (
  set "CLAUDE_LAUNCHER=%APPDATA%\npm\claude.cmd"
)

if not defined CLAUDE_LAUNCHER if exist "%USERPROFILE%\AppData\Roaming\npm\claude.cmd" (
  set "CLAUDE_LAUNCHER=%USERPROFILE%\AppData\Roaming\npm\claude.cmd"
)

if not defined CLAUDE_LAUNCHER if exist "%APPDATA%\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe" (
  set "CLAUDE_LAUNCHER=%APPDATA%\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe"
)

rem npm invoked from the packaged Codex app can be redirected by Windows MSIX.
rem Search that physical LocalCache location when Explorer cannot see the
rem virtualized AppData\Roaming entry.
if not defined CLAUDE_LAUNCHER (
  for /d %%D in ("%LOCALAPPDATA%\Packages\OpenAI.Codex_*") do (
    if exist "%%~fD\LocalCache\Roaming\npm\claude.cmd" (
      set "CLAUDE_LAUNCHER=%%~fD\LocalCache\Roaming\npm\claude.cmd"
    )
  )
)

if not defined CLAUDE_LAUNCHER if exist "%LOCALAPPDATA%\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Roaming\npm\claude.cmd" (
  set "CLAUDE_LAUNCHER=%LOCALAPPDATA%\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Roaming\npm\claude.cmd"
)

if not defined CLAUDE_LAUNCHER (
  echo Claude Code was not found.
  echo Expected npm location: %APPDATA%\npm\claude.cmd
  echo Also checked the packaged Codex LocalCache npm location.
  echo Run setup-windows.ps1 and install Claude Code first.
  pause
  exit /b 1
)

echo Starting Claude Code in:
echo %CD%
echo.
echo First review command: /msdial-repository-batch audit
echo.
call "%CLAUDE_LAUNCHER%" %*
set EXIT_CODE=%ERRORLEVEL%

if not "%EXIT_CODE%"=="0" (
  echo.
  echo Claude Code exited with code %EXIT_CODE%.
  pause
)
exit /b %EXIT_CODE%
