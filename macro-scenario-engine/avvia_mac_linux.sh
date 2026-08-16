#!/usr/bin/env bash
# ===================================================================
#  Macro Scenario Engine - avvio automatico su macOS e Linux
#
#  COME SI USA, dal Terminale:
#      cd percorso/della/cartella/macro-scenario-engine
#      chmod +x avvia_mac_linux.sh      # solo la prima volta
#      ./avvia_mac_linux.sh
#
#  NON incollare questi comandi dentro Python (il prompt ">>>"):
#  questo e' uno script di shell, non codice Python.
# ===================================================================
set -euo pipefail

cd "$(dirname "$0")/backend"

echo
echo "=========================================================="
echo "  MACRO SCENARIO ENGINE - avvio"
echo "=========================================================="
echo

# --- 1. Python -------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "[ERRORE] Python non trovato. Installa Python 3.11 o superiore."
    exit 1
fi
echo "[1/5] Python trovato: $($PY --version)"

# --- 2. Ambiente virtuale --------------------------------------------
if [ ! -f ".venv/bin/activate" ]; then
    echo "[2/5] Creazione dell'ambiente virtuale (solo la prima volta)..."
    "$PY" -m venv .venv
else
    echo "[2/5] Ambiente virtuale gia' presente."
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# --- 3. Dipendenze ----------------------------------------------------
if [ ! -f ".venv/.dipendenze_ok" ]; then
    echo "[3/5] Installazione delle dipendenze (qualche minuto, solo la prima volta)..."
    python -m pip install --upgrade pip --quiet
    pip install -r requirements.txt
    touch .venv/.dipendenze_ok
else
    echo "[3/5] Dipendenze gia' installate."
fi

# --- 4. Configurazione ------------------------------------------------
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "[4/5] File di configurazione .env creato."
else
    echo "[4/5] File di configurazione gia' presente."
fi

# --- 5. Avvio ---------------------------------------------------------
echo "[5/5] Avvio del server..."
echo
echo "=========================================================="
echo "  Dashboard : http://127.0.0.1:8000"
echo "  API       : http://127.0.0.1:8000/docs"
echo
echo "  Per fermare il server: CTRL+C"
echo "=========================================================="
echo

# Apre il browser senza bloccare l'avvio del server.
( sleep 3
  if command -v open >/dev/null 2>&1; then open http://127.0.0.1:8000
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open http://127.0.0.1:8000
  fi ) >/dev/null 2>&1 &

exec python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
