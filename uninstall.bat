@echo off
title MixCaps uninstall
echo This removes MixCaps, its Python and its models from %LOCALAPPDATA%\MixCaps
echo and the MixCaps script from DaVinci Resolve. Your timelines are not touched.
choice /M "Uninstall MixCaps"
if errorlevel 2 exit /b
del /q "%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\MixCaps.lua" 2>nul
rmdir /s /q "%LOCALAPPDATA%\MixCaps"
echo Done.
pause
