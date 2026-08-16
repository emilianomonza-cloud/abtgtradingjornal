@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1
title Macro Scenario Engine - installazione MQL5 su tutti i terminali MT5

rem ============================================================================
rem  Installa i componenti MQL5 su TUTTI i terminali MetaTrader 5 del PC:
rem    - Scripts\CalendarHistoryExporter.mq5   (export storico calendario)
rem    - Include\MacroScenarioTypes.mqh        (strutture + parser condivisi)
rem    - Indicators\MacroScenarioBridge.mq5    (pannello sul grafico)
rem    - Experts\MacroScenarioEA.mq5           (filtro rischio, DRY-RUN)
rem
rem  Come trova i terminali: ogni installazione MT5 tiene i propri dati in
rem  %APPDATA%\MetaQuotes\Terminal\<ID>\MQL5. Lo script li enumera tutti.
rem  Se possibile compila anche (metaeditor64.exe individuato via origin.txt);
rem  quando non ci riesce lo dice e resta la F7 in MetaEditor.
rem
rem  Limite dichiarato: i terminali avviati in modalita' /portable tengono
rem  MQL5 accanto a terminal64.exe e NON compaiono in %APPDATA%: quelli vanno
rem  fatti a mano (copia della cartella mql5\ dentro MQL5\ del terminale).
rem ============================================================================

echo =====================================================
echo  Macro Scenario Engine - installazione componenti MQL5
echo =====================================================
echo.

set "SORGENTE=%~dp0mql5"
if not exist "%SORGENTE%\Scripts\CalendarHistoryExporter.mq5" goto CARTELLA_SBAGLIATA
if not exist "%SORGENTE%\Include\MacroScenarioTypes.mqh" goto CARTELLA_SBAGLIATA

if not exist "%APPDATA%\MetaQuotes\Terminal\" goto NESSUN_TERMINALE

set /a TERMINALI=0
set /a COPIE_OK=0
set /a COMPILATI=0
set /a COMPILAZIONI_SALTATE=0

for /d %%T in ("%APPDATA%\MetaQuotes\Terminal\*") do call :UN_TERMINALE "%%T"

echo.
echo -----------------------------------------------------
if !TERMINALI! EQU 0 goto NESSUN_TERMINALE
echo  ESITO
echo    Terminali MT5 trovati . . : !TERMINALI!
echo    Installazioni riuscite  . : !COPIE_OK!
echo    File compilati  . . . . . : !COMPILATI!
if !COMPILAZIONI_SALTATE! GTR 0 (
  echo    Compilazioni da fare a mano: !COMPILAZIONI_SALTATE! terminale/i
  echo    ^(MetaEditor -^> apri il file -^> F7. I file sono gia' al loro posto.^)
)
echo.
echo  PROSSIMO PASSO nel terminale MT5:
echo    Navigator -^> Scripts -^> CalendarHistoryExporter -^> trascina sul grafico.
echo    I file dello storico compaiono in MQL5\Files\ ^(File -^> Apri cartella dati^).
echo    Poi caricali nella pagina /storico della dashboard.
echo -----------------------------------------------------
echo.
pause
exit /b 0

rem ============================================================================
:UN_TERMINALE
set "TERMDIR=%~1"
if not exist "%TERMDIR%\MQL5\" goto :eof

set /a TERMINALI+=1
echo Terminale !TERMINALI!: %TERMDIR%

rem --- copia dei quattro componenti -----------------------------------------
set "ERRORE_COPIA=0"
if not exist "%TERMDIR%\MQL5\Scripts\"    mkdir "%TERMDIR%\MQL5\Scripts"    >nul 2>&1
if not exist "%TERMDIR%\MQL5\Include\"    mkdir "%TERMDIR%\MQL5\Include"    >nul 2>&1
if not exist "%TERMDIR%\MQL5\Indicators\" mkdir "%TERMDIR%\MQL5\Indicators" >nul 2>&1
if not exist "%TERMDIR%\MQL5\Experts\"    mkdir "%TERMDIR%\MQL5\Experts"    >nul 2>&1

copy /y "%SORGENTE%\Scripts\CalendarHistoryExporter.mq5" "%TERMDIR%\MQL5\Scripts\"    >nul || set "ERRORE_COPIA=1"
copy /y "%SORGENTE%\Include\MacroScenarioTypes.mqh"      "%TERMDIR%\MQL5\Include\"    >nul || set "ERRORE_COPIA=1"
copy /y "%SORGENTE%\Indicators\MacroScenarioBridge.mq5"  "%TERMDIR%\MQL5\Indicators\" >nul || set "ERRORE_COPIA=1"
copy /y "%SORGENTE%\Experts\MacroScenarioEA.mq5"         "%TERMDIR%\MQL5\Experts\"    >nul || set "ERRORE_COPIA=1"

if "!ERRORE_COPIA!"=="1" (
  echo    COPIA FALLITA: controlla i permessi della cartella. Terminale saltato.
  goto :eof
)
set /a COPIE_OK+=1
echo    Copiati: exporter, include, indicatore, EA.

rem --- compilazione, se metaeditor64.exe e' individuabile --------------------
rem origin.txt nel data folder contiene il percorso di installazione del
rem terminale. In alcune build e' UTF-16 e set /p lo leggerebbe corrotto:
rem si verifica SEMPRE che il percorso risolto esista prima di usarlo.
set "MEDITOR="
if exist "%TERMDIR%\origin.txt" (
  set /p ORIGINE=<"%TERMDIR%\origin.txt"
  if exist "!ORIGINE!\metaeditor64.exe" set "MEDITOR=!ORIGINE!\metaeditor64.exe"
)

if "!MEDITOR!"=="" (
  set /a COMPILAZIONI_SALTATE+=1
  echo    metaeditor64.exe non individuato da origin.txt: compila con F7.
  goto :eof
)

echo    Compilo con: !MEDITOR!
call :COMPILA "%TERMDIR%\MQL5\Scripts\CalendarHistoryExporter.mq5"
call :COMPILA "%TERMDIR%\MQL5\Indicators\MacroScenarioBridge.mq5"
call :COMPILA "%TERMDIR%\MQL5\Experts\MacroScenarioEA.mq5"
goto :eof

rem ============================================================================
:COMPILA
rem metaeditor64 /compile ritorna il numero di file compilati con successo:
rem 1 = ok, 0 = errori (il dettaglio finisce nel .log accanto al sorgente).
"!MEDITOR!" /compile:"%~1" /log >nul 2>&1
if exist "%~dpn1.ex5" (
  set /a COMPILATI+=1
  echo      OK  %~nx1
) else (
  echo      ERRORE su %~nx1 : leggi %~dpn1.log
)
goto :eof

rem ============================================================================
:CARTELLA_SBAGLIATA
echo ERRORE: questo script va lanciato dalla cartella macro-scenario-engine,
echo quella che contiene la sottocartella mql5\ con i sorgenti.
echo Percorso cercato: %SORGENTE%
echo.
pause
exit /b 1

:NESSUN_TERMINALE
echo Nessun terminale MetaTrader 5 trovato in:
echo   %APPDATA%\MetaQuotes\Terminal\
echo.
echo Cause possibili:
echo   - MT5 non e' installato su questo PC o non e' mai stato avviato;
echo   - i terminali girano in modalita' /portable: in quel caso copia a mano
echo     il contenuto di mql5\ dentro la cartella MQL5\ di ciascun terminale.
echo.
pause
exit /b 1
