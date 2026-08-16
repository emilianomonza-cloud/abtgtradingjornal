@echo off
title Macro Scenario Engine - dati dimostrativi

REM ===================================================================
REM  Carica dati DIMOSTRATIVI nel database, per vedere la dashboard piena.
REM  Da lanciare DOPO avvia_windows.bat almeno una volta.
REM
REM  I dati sono INVENTATI: servono solo a mostrare come si comporta il
REM  sistema. Per ripulire, cancella il file data\macro_scenario.db
REM ===================================================================

cd /d "%~dp0backend"

if not exist ".venv\Scripts\python.exe" goto NON_INSTALLATO

echo.
echo Caricamento dei dati dimostrativi in corso...
echo.
".venv\Scripts\python.exe" carica_dati_demo.py
if errorlevel 1 goto FALLITO

echo.
echo Fatto. Ora lancia avvia_windows.bat e apri http://127.0.0.1:8000
echo.
pause
exit /b 0

:NON_INSTALLATO
echo.
echo [ERRORE] Ambiente non ancora installato.
echo Lancia prima avvia_windows.bat, aspetta che finisca, poi riprova.
echo.
pause
exit /b 1

:FALLITO
echo.
echo [ERRORE] Caricamento dei dati dimostrativi fallito.
echo.
pause
exit /b 1
