@echo off
title Macro Scenario Engine
color 0F

REM ===================================================================
REM  Macro Scenario Engine - avvio automatico su Windows
REM
REM  COME SI USA: fai doppio clic su questo file.
REM  NON incollare questi comandi dentro Python (il prompt ">>>"):
REM  questo e' uno script di Windows, non codice Python.
REM
REM  Se la finestra si chiudesse subito, aprila cosi' per leggere l'errore:
REM  tasto destro sulla cartella > "Apri nel Terminale", poi scrivi
REM  avvia_windows.bat e premi Invio.
REM ===================================================================

echo.
echo ==========================================================
echo   MACRO SCENARIO ENGINE - avvio
echo ==========================================================
echo.

cd /d "%~dp0backend"
if not exist "requirements.txt" goto CARTELLA_SBAGLIATA

REM --- 1. Cerca una versione di Python adatta -------------------------
REM  Le versioni appena uscite (es. 3.14) a volte non hanno ancora tutti
REM  i pacchetti pronti: si preferiscono quelle collaudate.
set "PYEXE="

py -3.13 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYEXE=py -3.13"
if defined PYEXE goto PYTHON_TROVATO

py -3.12 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYEXE=py -3.12"
if defined PYEXE goto PYTHON_TROVATO

py -3.11 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYEXE=py -3.11"
if defined PYEXE goto PYTHON_TROVATO

py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYEXE=py -3"
if defined PYEXE goto PYTHON_TROVATO

python -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYEXE=python"
if defined PYEXE goto PYTHON_TROVATO

goto PYTHON_MANCANTE

:PYTHON_TROVATO
echo [1/5] Python trovato:
%PYEXE% -V
echo.

REM --- 2. Ambiente virtuale -------------------------------------------
if exist ".venv\Scripts\python.exe" goto VENV_PRONTO
echo [2/5] Creazione dell'ambiente virtuale ^(solo la prima volta^)...
%PYEXE% -m venv .venv
if errorlevel 1 goto VENV_FALLITO
goto VENV_OK

:VENV_PRONTO
echo [2/5] Ambiente virtuale gia' presente.

:VENV_OK
set "VPY=.venv\Scripts\python.exe"

REM --- 3. Dipendenze ---------------------------------------------------
if exist ".venv\.dipendenze_ok" goto DIPENDENZE_PRONTE
echo [3/5] Installazione delle dipendenze.
echo       Richiede qualche minuto, solo la prima volta. Attendi...
echo.
"%VPY%" -m pip install --upgrade pip
"%VPY%" -m pip install -r requirements.txt
echo.
echo       Verifica dell'installazione...
"%VPY%" -c "import fastapi, uvicorn, sqlalchemy, httpx, bs4, apscheduler, yaml, jinja2, pydantic_settings" 2>nul
if errorlevel 1 goto DIPENDENZE_FALLITE
echo ok > ".venv\.dipendenze_ok"
goto DIPENDENZE_OK

:DIPENDENZE_PRONTE
echo [3/5] Dipendenze gia' installate.

:DIPENDENZE_OK

REM --- 4. Configurazione -----------------------------------------------
if exist ".env" goto CONFIG_OK
copy ".env.example" ".env" >nul
echo [4/5] File di configurazione .env creato.
goto AVVIO

:CONFIG_OK
echo [4/5] File di configurazione gia' presente.

REM --- 5. Avvio ---------------------------------------------------------
:AVVIO
echo [5/5] Avvio del server...
echo.
echo ==========================================================
echo   Dashboard : http://127.0.0.1:8000
echo   API       : http://127.0.0.1:8000/docs
echo.
echo   Il browser si apre da solo fra pochi secondi.
echo   Per fermare il server: chiudi questa finestra o CTRL+C.
echo ==========================================================
echo.
start "" "http://127.0.0.1:8000"
"%VPY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
echo.
echo Server fermato.
pause
exit /b 0

REM ===================== Errori ======================================

:CARTELLA_SBAGLIATA
echo [ERRORE] Non trovo la cartella "backend".
echo.
echo Causa piu' frequente: hai aperto il file DENTRO il file ZIP.
echo Estrai prima tutto lo ZIP ^(tasto destro ^> Estrai tutto^),
echo poi apri la cartella estratta e rilancia questo file.
echo.
echo Cartella da cui sto partendo:
echo   %~dp0
echo.
pause
exit /b 1

:PYTHON_MANCANTE
echo [ERRORE] Python non e' installato, oppure non e' nel PATH.
echo.
echo Scarica Python 3.12 da:
echo   https://www.python.org/downloads/release/python-3128/
echo.
echo IMPORTANTE: nella PRIMA schermata dell'installer spunta la casella
echo             "Add python.exe to PATH", in basso.
echo.
echo Poi rilancia questo file.
echo.
pause
exit /b 1

:VENV_FALLITO
echo.
echo [ERRORE] Creazione dell'ambiente virtuale fallita.
echo Prova a cancellare la cartella backend\.venv e rilancia.
echo.
pause
exit /b 1

:DIPENDENZE_FALLITE
echo.
echo ==========================================================
echo  [ERRORE] Installazione delle dipendenze non riuscita.
echo ==========================================================
echo.
echo  Salvo il dettaglio completo in:
echo    %~dp0installazione.log
"%VPY%" -m pip install -r requirements.txt > "%~dp0installazione.log" 2>&1
echo.
echo  Causa piu' frequente: la versione di Python in uso e' troppo
echo  recente e alcuni pacchetti non esistono ancora pronti per essa.
echo  Versione in uso:
%PYEXE% -V
echo.
echo  Rimedio: installa Python 3.12 da
echo    https://www.python.org/downloads/release/python-3128/
echo  poi CANCELLA la cartella backend\.venv e rilancia questo file.
echo.
pause
exit /b 1
