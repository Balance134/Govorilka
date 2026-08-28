@echo off
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
pyinstaller VoiceDictation.spec --noconfirm
echo.
echo Build finished: dist\VoiceDictation.exe
